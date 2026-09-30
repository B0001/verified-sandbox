"""Shared containerised beads worker loop.

The loop itself lives in loop.sh next to this file and is identical in every
repo. Everything that legitimately differs per repo -- which secrets to
forward, which extra volumes to mount, how many workers -- comes from
`[tool.sandbox]` in the repo's pyproject.toml. Nothing in a consuming repo
needs editing when the loop is refined; only this package is bumped.
"""

import os
import shlex
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path

LOOP = Path(__file__).with_name("loop.sh")

# Every repo pointed these at the same local proxy, so they are defaults rather
# than boilerplate each repo re-declares. Set either to "" in [tool.sandbox.env]
# to drop the flag entirely and talk to the real API.
DEFAULT_ENV = {
    "ANTHROPIC_BASE_URL": "http://host.docker.internal:4000",
    "ANTHROPIC_SMALL_FAST_MODEL": "local-ollama-fast",
}

# Prose in a prompt is advisory; this is the wall. It must sit OUTSIDE the
# beads-managed markers, because bd rewrites everything between them.
#
# Workers commit, but only on their own sandbox/<bead-id> branch (the loop puts
# them there); a commit on any other branch, or a detached HEAD, is refused.
# Push is refused outright: the host pushes, a human merges. Commits from the
# host are unaffected: BEADS_ACTOR is only set inside the container.
GUARD_START = "# --- sandbox guard"
GUARD_END = "# --- end sandbox guard ---\n"
# Bumped whenever the guard's rule changes, so check_hooks can refuse a repo
# still carrying an older rule (v1 refused every sandbox commit, which would
# leave each bead branch empty and every worker's work in a WIP commit).
GUARD_VERSION = "v2"
GUARDS = {
    "pre-commit": """
# --- sandbox guard v2 (NOT managed by beads; keep outside the markers below) ---
# Managed by `sandbox install-hooks`; re-run it after `bd` regenerates hooks.
if [ "${BEADS_ACTOR:-}" = "sandbox" ]; then
  case "$(git symbolic-ref --short -q HEAD)" in
    sandbox/?*) ;;
    *)
      echo >&2 "pre-commit: refusing -- BEADS_ACTOR=sandbox may only commit on"
      echo >&2 "its own sandbox/<bead-id> branch. Switch back to the branch the"
      echo >&2 "loop put you on and commit there. This is not a bug in your bead."
      exit 1
      ;;
  esac
fi
# --- end sandbox guard ---
""",
    "pre-push": """
# --- sandbox guard v2 (NOT managed by beads; keep outside the markers below) ---
# Managed by `sandbox install-hooks`; re-run it after `bd` regenerates hooks.
if [ "${BEADS_ACTOR:-}" = "sandbox" ]; then
  echo >&2 "pre-push: refusing -- BEADS_ACTOR=sandbox never pushes. Commit on"
  echo >&2 "your sandbox/<bead-id> branch; the host pushes and a human merges."
  exit 1
fi
# --- end sandbox guard ---
""",
}

# Worker commits need an identity -- the container has no git config -- and a
# fixed one makes sandbox-authored commits obvious in `git log`.
SANDBOX_AUTHOR = ("sandbox", "sandbox@verified-sandbox.invalid")

HOOKS_DIR = ".beads/hooks"
GUARDED_HOOKS = tuple(GUARDS)


def die(*lines):
    for line in lines:
        print(line, file=sys.stderr)
    raise SystemExit(1)


def repo_root():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def load_config(root):
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    data = tomllib.loads(pyproject.read_text())
    return data.get("tool", {}).get("sandbox", {}) or {}


def docker_args(root, cfg):
    """Flags for `docker run`, in the order the hand-written scripts used them.

    Secrets go through `forward-env` and are emitted as a bare `-e NAME`, which
    takes the value from this process's environment. `-e NAME=value` would put
    it in docker's argv, where any user on the host reads it out of `ps`.
    `[tool.sandbox.env]` is for values that are safe to be seen there.
    """
    env = {**DEFAULT_ENV, **cfg.get("env", {})}
    args = ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]
    for name, value in env.items():
        if value != "":
            args += ["-e", f"{name}={value}"]
    args += ["--add-host=host.docker.internal:host-gateway"]
    args += ["-v", f"{root}:/workspace"]
    args += ["-e", "UV_PROJECT_ENVIRONMENT=/tmp/venv"]
    for name in cfg.get("forward-env", {}):
        args += ["-e", name]
    args += ["-e", "BEADS_ACTOR=sandbox"]
    # The bind mount's root shows up root-owned inside the container, so git
    # refuses the repo outright ("dubious ownership") -- not just commits, even
    # `git status`. Confirmed in the claude-chem image, git 2.39. Env-supplied
    # config is honoured for safe.directory where a repo-level one is not.
    args += ["-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
             "-e", "GIT_CONFIG_VALUE_0=/workspace"]
    name, email = SANDBOX_AUTHOR
    for role in ("AUTHOR", "COMMITTER"):
        args += ["-e", f"GIT_{role}_NAME={name}", "-e", f"GIT_{role}_EMAIL={email}"]
    args += ["-v", "claude-uv-cache:/home/node/.cache/uv"]
    args += ["-v", "claude-uv-python:/home/node/.local/share/uv/python"]
    for volume in cfg.get("volumes", []):
        # A source starting with `.` or `~` is a host path relative to the repo
        # root, so pyproject.toml can say `../narrator` instead of hardcoding
        # one machine's home directory. Resolved here, before named_volumes()
        # sees it, or a relative bind mount would be mistaken for a named one.
        src, sep, rest = volume.partition(":")
        if src.startswith((".", "~")):
            volume = str((Path(root) / Path(src).expanduser()).resolve()) + sep + rest
        args += ["-v", volume]
    return args


def named_volumes(args):
    """The named volumes in a `docker run` arg list, in order.

    A named volume is a `-v NAME:/path` whose source is not a host path. These
    are the ones docker creates root-owned, so every one of them has to be
    chowned for the uid-1000 worker -- not just the two uv volumes. A toolchain
    cache the worker cannot write to is worse than no cache: it re-downloads
    silently, every session, and reads as a slow bead rather than a broken mount.
    A bind mount of a host directory already carries the host's ownership.
    """
    return [a.split(":", 1)[0] for prev, a in zip(args, args[1:])
            if prev == "-v" and not a.startswith("/")]


def conf_text(root, cfg):
    """A sourceable snippet. An array, not a string: shlex.quote plus bash's
    "${a[@]}" is the only pairing that survives a value containing a space."""
    args = docker_args(root, cfg)
    quoted = " ".join(shlex.quote(a) for a in args)
    volumes = " ".join(shlex.quote(v) for v in named_volumes(args))
    return "\n".join([
        f"DOCKER_ARGS=({quoted})",
        f"CHOWN_VOLUMES=({volumes})",
        f"PARK_ALWAYS={shlex.quote(' '.join(cfg.get('park', [])))}",
        f"IMAGE={shlex.quote(cfg.get('image', 'claude'))}",
        f"MAX_ATTEMPTS={int(cfg.get('max-attempts', 2))}",
        f"MAX_WORKERS={int(cfg.get('max-workers', 25))}",
        # Workers running at once, each in its own git worktree. 1 is the
        # original loop, byte for byte: one worker, in the repo's own tree.
        f"MAX_PARALLEL={max(1, int(cfg.get('max-parallel', 1)))}",
        # Shared queue (verified-sandbox-4z7): the git remote that referees
        # claims between machines draining the same beads. Empty = off.
        f"SHARED_REMOTE={shlex.quote(cfg.get('shared-queue', ''))}",
        f"CLAIM_LEASE={int(float(cfg.get('claim-lease-hours', 24)) * 3600)}",
        f"CLAIM_ACTOR={shlex.quote('sandbox@' + socket.gethostname().split('.')[0])}",
        # A non-zero worker exit inside this many seconds is treated as
        # infrastructure (rate limit, dead token, dead proxy), not as the bead
        # failing -- see the fast-failure block in loop.sh. The floor for a
        # real attempt is minutes, so 90s is generous; raise it if a repo's
        # workers legitimately finish faster than that.
        f"MIN_WORKER_SECONDS={int(cfg.get('min-worker-seconds', 90))}",
        f"FAST_FAIL_SLEEP={int(cfg.get('fast-fail-sleep', 60))}",
        f"SANDBOX_AUTHOR_NAME={shlex.quote(SANDBOX_AUTHOR[0])}",
        f"SANDBOX_AUTHOR_EMAIL={shlex.quote(SANDBOX_AUTHOR[1])}",
        f"PROMPT_FILE={shlex.quote(cfg.get('prompt-file', 'sandbox-prompt.md'))}",
        f"HANDOFF_DIR={shlex.quote(cfg.get('handoff-dir', 'sandbox-handoffs'))}",
        "",
    ])


def check_proxy(cfg):
    """Refuse to start if ANTHROPIC_BASE_URL points at nothing.

    Every worker's API traffic goes through this, so when it is down each of
    the 25 dispatches fails in turn and the run reports beads as unfinished --
    which reads as "the work is hard", not "the proxy died". Checking once here
    costs a TCP connect and turns 25 confusing failures into one clear line.

    The URL is written from the container's point of view, so the hostname
    docker resolves via --add-host has to be mapped back to the loopback the
    host can actually reach. An empty base URL means the real API, so there is
    nothing local to check.
    """
    url = {**DEFAULT_ENV, **cfg.get("env", {})}.get("ANTHROPIC_BASE_URL", "")
    if not url:
        return
    parsed = urllib.parse.urlparse(url)
    host = "127.0.0.1" if parsed.hostname == "host.docker.internal" else parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=3):
            return
    except OSError as exc:
        die(f"FATAL: nothing is listening at {url} ({host}:{port} -- {exc}).",
            "       Every worker routes its API traffic through it, so the whole",
            "       run would fail one bead at a time and look like hard work",
            "       rather than a dead proxy. Start it, or set",
            '       ANTHROPIC_BASE_URL = "" in [tool.sandbox.env] to use the real API.')


def check_hooks(root):
    """The commit guard is enforced by a git hook, and git runs NO hook at all
    -- silently, no error -- when core.hooksPath points at a directory that does
    not exist. That is how the guard sat disarmed for weeks (einstein-do6): the
    path was absolute and host-only, so it resolved on the host and vanished
    inside every container, which is the one place it has to work.

    core.hooksPath lives in .git/config, which git does not track, so a fresh
    clone starts with no guard whatsoever and nothing else would notice.
    """
    got = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        capture_output=True, text=True, cwd=root,
    ).stdout.strip()
    if not got or got.startswith("/"):
        die(f"FATAL: core.hooksPath is '{got or 'unset'}', so the commit guard does",
            "       not fire inside worker containers. An absolute path is by",
            "       definition not portable into one. Fix with:",
            "         sandbox install-hooks")
    for hook in GUARDED_HOOKS:
        path = root / got / hook
        if not path.is_file() or not os.access(path, os.X_OK):
            die(f"FATAL: {got}/{hook} is missing or not executable. Workers could",
                "       commit and push. Fix with: sandbox install-hooks")
        text = path.read_text()
        if "BEADS_ACTOR" not in text:
            die(f"FATAL: {got}/{hook} has lost its BEADS_ACTOR guard -- bd rewrites",
                "       hooks and can drop it. Fix with: sandbox install-hooks")
        if f"{GUARD_START} {GUARD_VERSION}" not in text:
            die(f"FATAL: {got}/{hook} carries an older sandbox guard. Workers now",
                "       commit on sandbox/<bead-id> branches, which it refuses.",
                "       Fix with: sandbox install-hooks")


def cmd_install_hooks(root, cfg=None):
    cfg = cfg or {}
    hooks = root / HOOKS_DIR
    hooks.mkdir(parents=True, exist_ok=True)
    for hook, guard in GUARDS.items():
        path = hooks / hook
        text = path.read_text() if path.is_file() else "#!/usr/bin/env sh\n"
        # Replace, not skip, an existing guard: an older version's rule would
        # otherwise survive every re-install.
        if GUARD_START in text:
            start = text.index(GUARD_START)
            end = text.index(GUARD_END, start) + len(GUARD_END)
            # The blank line the guard was inserted with goes too, or each
            # re-install would grow the file by one.
            start -= text[:start].endswith("\n\n")
            text = text[:start] + text[end:]
        head, _, tail = text.partition("\n")
        new_text = head + "\n" + guard + tail
        path.write_text(new_text)
        print(f"  {HOOKS_DIR}/{hook}: guard {GUARD_VERSION} installed")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    install_append_only(hooks / "pre-commit", cfg.get("append-only", []))

    # Relative, so it resolves the same on the host and inside the container.
    subprocess.run(["git", "config", "core.hooksPath", HOOKS_DIR], cwd=root, check=True)
    print(f"  core.hooksPath = {HOOKS_DIR}")


def install_append_only(path, files):
    """Refuse commits that delete or rewrite lines in an append-only file.

    Unlike the BEADS_ACTOR guard, this one binds humans too, and it has to.
    A worker cannot commit -- but it CAN edit a preregistration in the working
    tree and leave it for a human to commit inside a large diff, which is the
    same outcome by a slower route. The file's own policy is the rule being
    enforced: adding an entry is fine, changing a registered one is not, and
    outcomes belong in a separate file. A prediction that can be amended after
    the measurement is worth exactly nothing, which is the whole reason the
    timestamp on it has value.
    """
    text = path.read_text() if path.is_file() else "#!/usr/bin/env sh\n"
    marker = "# --- append-only guard"
    if marker in text:
        start = text.index(marker)
        end = text.index("# --- end append-only guard ---\n") + len("# --- end append-only guard ---\n")
        text = text[:start] + text[end:]
    if not files:
        path.write_text(text)
        print(f"  {path.parent.name}/{path.name}: no append-only files configured")
        return

    quoted = " ".join(shlex.quote(f) for f in files)
    block = f"""{marker} (managed by `sandbox install-hooks`) ---
# These paths are append-only by policy. Adding lines is fine; deleting or
# rewriting existing ones is refused -- for workers and humans alike, because
# a worker leaves edits in the tree for a human to commit.
for _ao_f in {quoted}; do
  _ao_del="$(git diff --cached --numstat -- "$_ao_f" | awk '{{print $2}}')"
  # numstat prints "-" for a binary file; only a real nonzero count is a delete.
  case "$_ao_del" in
    ""|0|-) ;;
    *)
      echo >&2 "pre-commit: refusing -- $_ao_f is append-only and this commit"
      echo >&2 "  removes or rewrites $_ao_del existing line(s). Add a new entry,"
      echo >&2 "  or record the result in a separate outcomes file."
      exit 1
      ;;
  esac
done
# --- end append-only guard ---
"""
    head, _, tail = text.partition("\n")
    path.write_text(head + "\n" + block + tail)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {path.parent.name}/{path.name}: append-only guard on {', '.join(files)}")


def cmd_run(root, cfg):
    if shutil.which("docker") is None:
        die("FATAL: docker is not on PATH; every worker runs in a container.")
    prompt = root / cfg.get("prompt-file", "sandbox-prompt.md")
    if not prompt.is_file():
        die(f"FATAL: missing {prompt.name} -- it is the standing context every",
            "       worker session is held to. Nothing to dispatch without it.")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        die("FATAL: export CLAUDE_CODE_OAUTH_TOKEN first.")
    for name, why in cfg.get("forward-env", {}).items():
        if not os.environ.get(name):
            print(f"warning: {name} unset{' -- ' + why if why else ''}")

    check_proxy(cfg)
    check_hooks(root)

    # Written, not piped: loop.sh sources it, and a temp file is the one channel
    # that keeps values out of both the environment and any process's argv.
    fd, path = tempfile.mkstemp(prefix="sandbox-conf-", suffix=".sh")
    with os.fdopen(fd, "w") as fh:
        fh.write(conf_text(root, cfg))
    try:
        proc = subprocess.Popen(
            ["bash", str(LOOP)],
            cwd=root, env={**os.environ, "SANDBOX_CONF": path},
        )
        # Ctrl-C reaches the loop directly (same process group), and its trap
        # winds the run down: WIP-commits leftovers, removes worktrees, drops
        # the lock. subprocess.run() would SIGKILL it 0.25s into that, leaving
        # the lock and worktrees behind -- so wait it out instead.
        while True:
            try:
                returncode = proc.wait()
                break
            except KeyboardInterrupt:
                continue
    finally:
        os.unlink(path)
    raise SystemExit(returncode)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "run"
    root = repo_root()
    if cmd == "install-hooks":
        cmd_install_hooks(root, load_config(root))
    elif cmd == "run":
        cmd_run(root, load_config(root))
    else:
        die("usage: sandbox [run | install-hooks]",
            "",
            "  run            drain the beads queue, one container per bead (default)",
            "  install-hooks  install the BEADS_ACTOR commit guard and point",
            "                 core.hooksPath at it, relatively")
