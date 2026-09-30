"""The checks that fail if the loop's real invariants break.

Not coverage for its own sake -- each of these encodes a bug that actually
happened in one of the hand-maintained copies.
"""

import json
import shlex
import subprocess
from pathlib import Path

import verified_sandbox as vs

ROOT = Path("/repo")


def test_secrets_never_reach_dockers_argv():
    # `-e NAME=value` is readable by any user on the host via `ps`. Anything
    # declared as a secret must come out as a bare `-e NAME`.
    args = vs.docker_args(ROOT, {"forward-env": {"GITHUB_TOKEN": "", "HF_TOKEN": "why"}})
    for name in ("GITHUB_TOKEN", "HF_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert "-e" in args and name in args, f"{name} not forwarded"
        assert not any(a.startswith(f"{name}=") for a in args), f"{name} leaked into argv"


def test_env_values_are_emitted_and_clearable():
    args = vs.docker_args(ROOT, {"env": {"MATHGRAPH_DATA": "/workspace/data"}})
    assert "MATHGRAPH_DATA=/workspace/data" in args
    assert "ANTHROPIC_BASE_URL=http://host.docker.internal:4000" in args, "default dropped"
    # Empty string means "drop the flag", not "pass an empty value".
    cleared = vs.docker_args(ROOT, {"env": {"ANTHROPIC_BASE_URL": ""}})
    assert not any(a.startswith("ANTHROPIC_BASE_URL") for a in cleared)


def test_the_repo_is_always_mounted_and_the_worker_is_always_marked():
    args = vs.docker_args(ROOT, {})
    assert f"{ROOT}:/workspace" in args
    # Without this the commit guard has nothing to key off and workers can push.
    assert "BEADS_ACTOR=sandbox" in args


def test_conf_survives_a_value_containing_a_space():
    # The whole reason DOCKER_ARGS is a bash array and not a string.
    conf = vs.conf_text(Path("/a path/repo"), {"env": {"X": "one two"}})
    out = subprocess.run(
        ["bash", "-c", f'{conf}\nprintf "%s\\n" "${{DOCKER_ARGS[@]}}"'],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "X=one two" in out
    assert "/a path/repo:/workspace" in out


def test_conf_defaults_and_overrides():
    assert "MAX_ATTEMPTS=2" in vs.conf_text(ROOT, {})
    assert "MAX_WORKERS=7" in vs.conf_text(ROOT, {"max-workers": 7})
    assert f"IMAGE={shlex.quote('claude')}" in vs.conf_text(ROOT, {})


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)],
                   check=True, capture_output=True)
    return tmp_path


def test_install_hooks_is_idempotent_and_survives_a_bd_rewrite(tmp_path, capsys):
    root = _git_repo(tmp_path)
    hooks = root / vs.HOOKS_DIR
    hooks.mkdir(parents=True)
    # bd owns everything between its markers, so the guard has to live above.
    (hooks / "pre-commit").write_text(
        "#!/usr/bin/env sh\n# --- BEGIN BEADS INTEGRATION ---\nexit 0\n")

    vs.cmd_install_hooks(root)
    first = (hooks / "pre-commit").read_text()
    assert first.index("BEADS_ACTOR") < first.index("BEGIN BEADS INTEGRATION")
    assert first.startswith("#!/usr/bin/env sh\n")
    assert "BEGIN BEADS INTEGRATION" in first, "clobbered the beads block"

    vs.cmd_install_hooks(root)
    assert (hooks / "pre-commit").read_text() == first, "not idempotent"

    # Relative: an absolute path resolves on the host and vanishes in the
    # container, which is the one place the guard has to fire.
    got = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                         cwd=root, capture_output=True, text=True).stdout.strip()
    assert got == vs.HOOKS_DIR

    # And the installed hook must actually refuse a sandbox commit.
    for hook in vs.GUARDED_HOOKS:
        refused = subprocess.run(["sh", str(hooks / hook)],
                                 env={"BEADS_ACTOR": "sandbox", "PATH": "/usr/bin:/bin"},
                                 capture_output=True, text=True)
        assert refused.returncode != 0, f"{hook} let a sandbox worker through"


def test_check_hooks_rejects_an_absolute_hookspath(tmp_path):
    root = _git_repo(tmp_path)
    vs.cmd_install_hooks(root)
    vs.check_hooks(root)  # passes once installed

    subprocess.run(["git", "config", "core.hooksPath", str(root / ".beads/hooks")],
                   cwd=root, check=True)
    try:
        vs.check_hooks(root)
    except SystemExit:
        pass
    else:
        raise AssertionError("absolute hooksPath accepted; guard would be dead in-container")


def test_loop_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(vs.LOOP)], check=True)


def test_every_named_volume_is_chowned_not_just_the_uv_pair():
    """A config-added volume comes up root-owned exactly like the uv ones. Miss
    it and the uid-1000 worker silently re-downloads the cache every session --
    which is what four days of certkit Lean beads actually were.
    """
    cfg = {"volumes": ["claude-elan:/home/node/.elan"]}
    vols = vs.named_volumes(vs.docker_args(ROOT, cfg))
    assert vols == ["claude-uv-cache", "claude-uv-python", "claude-elan"]
    # The repo bind mount is a host path and already has the host's ownership.
    assert not any(v.startswith("/") for v in vols)


def test_relative_volume_is_a_bind_mount_not_a_named_volume():
    """`../sibling` in pyproject.toml resolves against the repo root. Left raw,
    it doesn't start with `/`, so named_volumes() would chown it as a volume."""
    cfg = {"volumes": ["../sibling:/workspace-sibling:ro"]}
    args = vs.docker_args(ROOT, cfg)
    expected = f"{(Path(ROOT) / '..' / 'sibling').resolve()}:/workspace-sibling:ro"
    assert expected in args
    assert vs.named_volumes(args) == ["claude-uv-cache", "claude-uv-python"]


def test_chown_covers_the_same_volumes_the_worker_mounts():
    conf = vs.conf_text(ROOT, {"volumes": ["claude-elan:/home/node/.elan"]})
    out = subprocess.run(
        ["bash", "-c", f'{conf}\nprintf "%s\\n" "${{CHOWN_VOLUMES[@]}}"'],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "claude-elan" in out and "claude-uv-cache" in out


def test_park_list_reaches_the_loop_and_a_missing_one_is_empty():
    """Beads a worker structurally cannot finish (one needing an independent
    human reviewer) must never be dispatched -- certkit carried this as a
    hand-edited PARKED= line, and a migration that dropped it would burn two
    sessions per run on work no session can do."""
    conf = vs.conf_text(ROOT, {"park": ["certkit-jcb", "certkit-xyz"]})
    out = subprocess.run(
        ["bash", "-c", f'{conf}\necho "[$PARK_ALWAYS]"'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "[certkit-jcb certkit-xyz]"
    assert "PARK_ALWAYS=''" in vs.conf_text(ROOT, {})


def test_a_pre_parked_bead_is_not_reported_as_a_failure():
    """`park` is a config choice, not a run failure. Reporting it under
    "dispatched N times and never finished" would be false, and exiting
    non-zero for it would mean a clean drain can never be observed."""
    script = (
        vs.conf_text(ROOT, {"park": ["certkit-jcb"]})
        + '\nPARKED="$PARK_ALWAYS certkit-real"\nexit_code=0\n'
        + open(vs.LOOP).read().split("# Only the ones this run actually gave up on.")[1]
          .split("# \"Queue drained\" must never")[0]
        + '\necho "exit_code=$exit_code"\n'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert "certkit-real" in out.split("Never dispatched")[0], "real failure not reported"
    assert "exit_code=1" in out, "a genuine parked bead must fail the run"
    assert "Never dispatched" in out and "certkit-jcb" in out.split("Never dispatched")[1]


def test_proxy_check_fails_fast_on_a_dead_port():
    """A dead proxy otherwise fails all 25 dispatches one at a time, and the run
    reports them as unfinished beads -- which reads as hard work, not a dead
    proxy. Port 1 is reserved and never listening."""
    try:
        vs.check_proxy({"env": {"ANTHROPIC_BASE_URL": "http://host.docker.internal:1"}})
    except SystemExit:
        pass
    else:
        raise AssertionError("dead proxy accepted")


def test_proxy_check_is_skipped_when_talking_to_the_real_api():
    vs.check_proxy({"env": {"ANTHROPIC_BASE_URL": ""}})  # must not raise


def test_proxy_check_maps_the_container_hostname_back_to_loopback():
    """The URL is written from inside the container, where --add-host resolves
    host.docker.internal. The host itself has to check the loopback instead."""
    import socket as s
    srv = s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        vs.check_proxy({"env": {"ANTHROPIC_BASE_URL": f"http://host.docker.internal:{port}"}})
    finally:
        srv.close()


def _repo_with_prereg(tmp_path):
    root = _git_repo(tmp_path)
    (root / "predictions").mkdir()
    f = root / "predictions" / "PREREGISTRATION.md"
    f.write_text("# Pre-registration\n\n## P1\nStatus: open\nClaim: X displaces from Y.\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "core.hooksPath=", "commit", "-qm", "init"],
                   cwd=root, check=True, capture_output=True)
    return root, f


def _commit(root, msg):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg],
        cwd=root, capture_output=True, text=True)


def test_appending_a_prediction_is_allowed(tmp_path):
    root, f = _repo_with_prereg(tmp_path)
    vs.cmd_install_hooks(root, {"append-only": ["predictions/PREREGISTRATION.md"]})
    f.write_text(f.read_text() + "\n## P2\nStatus: open\nClaim: Z.\n")
    assert _commit(root, "register P2").returncode == 0, "appending must be allowed"


def test_amending_a_registered_prediction_is_refused(tmp_path):
    """The whole value of a preregistration is that it could not have been
    changed after the measurement. This binds humans too -- a worker cannot
    commit, but it can leave an amended file for a human to commit unnoticed."""
    root, f = _repo_with_prereg(tmp_path)
    vs.cmd_install_hooks(root, {"append-only": ["predictions/PREREGISTRATION.md"]})
    f.write_text(f.read_text().replace("X displaces from Y", "X coincides with Y"))
    result = _commit(root, "quietly reword the claim")
    assert result.returncode != 0, "an amended prediction was committed"
    assert "append-only" in result.stderr


def test_deleting_a_prediction_is_refused(tmp_path):
    root, f = _repo_with_prereg(tmp_path)
    vs.cmd_install_hooks(root, {"append-only": ["predictions/PREREGISTRATION.md"]})
    f.write_text("# Pre-registration\n")
    assert _commit(root, "drop P1").returncode != 0, "a deleted prediction was committed"


def test_append_only_guard_is_idempotent_and_coexists_with_the_actor_guard(tmp_path):
    root, _ = _repo_with_prereg(tmp_path)
    cfg = {"append-only": ["predictions/PREREGISTRATION.md"]}
    vs.cmd_install_hooks(root, cfg)
    first = (root / vs.HOOKS_DIR / "pre-commit").read_text()
    vs.cmd_install_hooks(root, cfg)
    assert (root / vs.HOOKS_DIR / "pre-commit").read_text() == first, "not idempotent"
    assert "BEADS_ACTOR" in first and "append-only" in first
    assert first.startswith("#!/usr/bin/env sh\n")


IDS = Path(vs.__file__).with_name("ids.py")


def _ids(stdin, *skip):
    return subprocess.run(["python3", str(IDS), *skip], input=stdin,
                          capture_output=True, text=True)


def test_ids_reads_both_bd_json_envelopes_and_drops_epics():
    # bd 1.1 wrapped --json in {"data": [...]}; older versions emitted a bare
    # list. The loop only knew the bare list, so under bd 1.1.2 every selector
    # went silent, the loop believed the queue was empty, and the triage pass
    # filed beads on top of a live one (certkit, 2026-08-26).
    issues = [{"id": "a-1", "issue_type": "task"}, {"id": "e-1", "issue_type": "epic"}]
    for payload in (issues, {"data": issues}, {"issues": issues}):
        got = _ids(json.dumps(payload), "epic")
        assert got.returncode == 0, got.stderr
        assert got.stdout.split() == ["a-1"], payload


def test_ids_refuses_an_unknown_shape_rather_than_reporting_an_empty_queue():
    # Exiting nonzero is the whole point: queue_empty trusts a silent selector
    # and runs triage, which writes. "I cannot read this" must not look like
    # "there is nothing to do".
    assert _ids('{"unexpected": 1}').returncode != 0
    assert _ids("not json at all").returncode != 0


def _fake_run(tmp_path, docker_exit, docker_sleep=0, max_attempts=2, min_worker_seconds=90,
              queue=("t-1",), worker="", dirty_start=False, max_parallel=1,
              bd_export_dirty=False):
    """Drive the real loop.sh against a fake `bd` and a fake `docker`, in a real
    git repo with the real hooks installed.

    The rate-limit and branch-per-bead behaviour live in the loop, not in
    Python, so nothing short of running it actually checks them. `worker` is
    shell run in place of the container, in the repo, with $ID set to the bead
    it was dispatched on; a worker that runs CLOSE has closed its bead. With
    max_parallel > 1 the worker runs in the worktree the loop mounted as its
    /workspace, and $HOST_REPO / $SYNC are there for the parallel tests.
    """
    import os
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bd").write_text(
        '#!/usr/bin/env bash\n'
        'case "$1 $2" in\n'
        '  "ready --json")\n'
        '    printf \'{"data":[\'; sep=\n'
        '    for i in $(cat "$FAKE_QUEUE"); do\n'
        '      [ -e "$FAKE_QUEUE.closed/$i" ] && continue\n'
        '      printf \'%s{"id":"%s","issue_type":"task"}\' "$sep" "$i"; sep=,\n'
        '    done; echo "]}" ;;\n'
        '  *) echo \'{"data":[]}\' ;;\n'
        'esac\n'
    )
    (bin_dir / "docker").write_text(
        '#!/usr/bin/env bash\n'
        # The startup chown container must succeed; only the worker is faked.
        'for a in "$@"; do [ "$a" = "busybox" ] && exit 0; done\n'
        'for a in "$@"; do case "$a" in *"YOUR TASK THIS SESSION: bead "*)\n'
        '  ID="$(printf "%s" "$a" | sed -n "s/.*YOUR TASK THIS SESSION: bead //p" | head -1)";;\n'
        'esac; done\n'
        # Run where the real container would: in whatever the loop mounted.
        'for a in "$@"; do case "$a" in *:/workspace) cd "${a%:/workspace}" || exit 99;; esac; done\n'
        'export BEADS_ACTOR=sandbox GIT_AUTHOR_NAME=sandbox GIT_AUTHOR_EMAIL=s@s\n'
        'export GIT_COMMITTER_NAME=sandbox GIT_COMMITTER_EMAIL=s@s\n'
        f'{worker}\n'
        f'sleep {docker_sleep}\n'
        f'exit {docker_exit}\n'
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    fake_queue = tmp_path / "queue"
    fake_queue.write_text("".join(f"{q}\n" for q in queue))

    repo = _git_repo(tmp_path / "repo")
    (repo / "prompt.md").write_text("standing context")
    vs.cmd_install_hooks(repo)
    if bd_export_dirty:
        (repo / ".beads" / "interactions.jsonl").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    if dirty_start:
        (repo / "stray.txt").write_text("host edit nobody committed")
    if bd_export_dirty:
        with open(repo / ".beads" / "interactions.jsonl", "a") as fh:
            fh.write("{}\n")

    conf = tmp_path / "conf.sh"
    mount = f" -v {shlex.quote(str(repo))}:/workspace" if max_parallel > 1 else ""
    conf.write_text(
        f"DOCKER_ARGS=(--rm{mount})\nCHOWN_VOLUMES=(uv-cache)\nPARK_ALWAYS=\nIMAGE=fake\n"
        f"MAX_PARALLEL={max_parallel}\n"
        f"MAX_ATTEMPTS={max_attempts}\nMAX_WORKERS=25\n"
        "PROMPT_FILE=prompt.md\nHANDOFF_DIR=handoffs\n"
        f"MIN_WORKER_SECONDS={min_worker_seconds}\nFAST_FAIL_SLEEP=1\n"
        "SANDBOX_AUTHOR_NAME=sandbox\nSANDBOX_AUTHOR_EMAIL=s@s\n"
    )
    loop = Path(vs.__file__).parent / "loop.sh"
    out = subprocess.run(
        ["bash", str(loop)], cwd=repo, capture_output=True, text=True, timeout=120,
        env={**os.environ, **HOST_IDENTITY, "SANDBOX_CONF": str(conf),
             "FAKE_QUEUE": str(fake_queue), "HOST_REPO": str(repo),
             "SYNC": str(tmp_path / "sync"),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    out.repo = repo
    return out


HOST_IDENTITY = {"GIT_AUTHOR_NAME": "host", "GIT_AUTHOR_EMAIL": "h@h",
                 "GIT_COMMITTER_NAME": "host", "GIT_COMMITTER_EMAIL": "h@h"}


def _git(repo, *args, env=None):
    import os
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          env={**os.environ, **HOST_IDENTITY, **(env or {})})


def test_a_rate_limited_worker_is_not_charged_to_the_bead(tmp_path):
    # A worker that dies in seconds never reached the bead. Before this, two
    # such dispatches PARKED a healthy bead and the run reported it as work
    # that "came back unfinished twice" -- a dead credential reading as hard
    # work. It must abort instead, park nothing, and exit non-zero.
    out = _fake_run(tmp_path, docker_exit=1)
    combined = out.stdout + out.stderr
    assert "ABORT" in combined, combined
    assert "PARKED" not in combined, "a rate limit must never park a bead"
    assert out.returncode != 0, "an infrastructure abort must not look like a clean drain"


def test_a_slow_failure_is_still_the_beads_problem(tmp_path):
    # The mirror image, and the reason the guard is time-based rather than
    # "swallow every non-zero exit": a worker that ran long enough to have
    # actually tried and still failed IS the bead's problem, and must still
    # reach MAX_ATTEMPTS and park. A guard that ate this too would make a
    # genuinely undoable bead invisible.
    out = _fake_run(tmp_path, docker_exit=1, docker_sleep=2,
                    max_attempts=1, min_worker_seconds=1)
    combined = out.stdout + out.stderr
    assert "PARKED" in combined, combined
    assert "ABORT" not in combined, "a slow failure is not an infrastructure abort"


# --- branch per bead (verified-sandbox-rbz) ---------------------------------

SANDBOX = {"BEADS_ACTOR": "sandbox"}


def _hooked_repo(tmp_path):
    root = _git_repo(tmp_path)
    vs.cmd_install_hooks(root)
    _git(root, "add", "-A")
    assert _git(root, "commit", "-qm", "init").returncode == 0
    (root / "work.txt").write_text("a worker's change\n")
    _git(root, "add", "-A")
    return root


def test_a_sandbox_commit_on_its_bead_branch_is_allowed(tmp_path):
    # The whole feature: under the v1 guard this was refused, so every bead's
    # work piled up uncommitted in one shared tree.
    root = _hooked_repo(tmp_path)
    _git(root, "checkout", "-qb", "sandbox/t-1")
    got = _git(root, "commit", "-qm", "work", env=SANDBOX)
    assert got.returncode == 0, got.stderr


def test_a_sandbox_commit_anywhere_else_is_refused(tmp_path):
    root = _hooked_repo(tmp_path)
    for setup in ([], ["checkout", "-qb", "feature"], ["checkout", "-q", "--detach"],
                  # A bare "sandbox/" prefix is not a bead branch.
                  ["checkout", "-qb", "sandbox/"]):
        if setup:
            _git(root, *setup)
        got = _git(root, "commit", "-qm", "work", env=SANDBOX)
        assert got.returncode != 0, f"sandbox commit allowed after {setup or 'on main'}"
    # And the host is never affected.
    assert _git(root, "commit", "-qm", "host work").returncode == 0


def test_a_sandbox_push_is_still_refused_even_from_a_bead_branch(tmp_path):
    root = _hooked_repo(tmp_path)
    _git(root, "checkout", "-qb", "sandbox/t-1")
    refused = subprocess.run(["sh", str(root / vs.HOOKS_DIR / "pre-push")],
                             cwd=root, env={**SANDBOX, "PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True)
    assert refused.returncode != 0


def test_install_hooks_upgrades_a_v1_guard_and_check_hooks_rejects_it(tmp_path):
    # Repos installed before branch-per-bead carry the v1 guard, which refuses
    # every sandbox commit. Re-installing must replace it, not keep it because
    # "a guard is already present" -- and until then, run must refuse to start.
    root = _git_repo(tmp_path)
    hooks = root / vs.HOOKS_DIR
    hooks.mkdir(parents=True)
    v1 = ('\n# --- sandbox guard (NOT managed by beads; keep outside the markers below) ---\n'
          'if [ "${BEADS_ACTOR:-}" = "sandbox" ]; then\n  exit 1\nfi\n'
          '# --- end sandbox guard ---\n')
    for hook in vs.GUARDED_HOOKS:
        (hooks / hook).write_text("#!/usr/bin/env sh\n" + v1 + "# --- BEGIN BEADS INTEGRATION ---\n")
        (hooks / hook).chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", vs.HOOKS_DIR], cwd=root, check=True)
    try:
        vs.check_hooks(root)
    except SystemExit:
        pass
    else:
        raise AssertionError("a v1 guard passed the preflight")

    vs.cmd_install_hooks(root)
    vs.check_hooks(root)
    text = (hooks / "pre-commit").read_text()
    assert text.count("# --- sandbox guard") == 1, "v1 guard left alongside v2"
    assert "BEGIN BEADS INTEGRATION" in text


def test_workers_get_a_git_identity():
    # The container has no git config; without this every worker commit fails
    # with "Please tell me who you are" and all work lands as WIP.
    args = vs.docker_args(ROOT, {})
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        assert any(a.startswith(f"{var}=") for a in args), var
    # And git must accept the mount at all: /workspace is root-owned in there.
    assert "GIT_CONFIG_KEY_0=safe.directory" in args and "GIT_CONFIG_VALUE_0=/workspace" in args


# A marker file per closed bead, not a rewrite of the queue: two parallel
# workers closing at once would lose one of the rewrites.
CLOSE = 'mkdir -p "$FAKE_QUEUE.closed" && touch "$FAKE_QUEUE.closed/$ID"\n'


def _changed(repo, branch):
    return set(_git(repo, "diff", "--name-only", f"main...{branch}").stdout.split())


def test_two_beads_land_on_two_branches_and_main_does_not_move(tmp_path):
    worker = (
        'echo "$ID" > "$ID.txt"; mkdir -p handoffs; echo "done" > "handoffs/$ID.md"\n'
        'git add -A && git commit -qm "$ID" || exit 1\n'
        # t-2 also leaves something uncommitted: it must reach t-2's branch as
        # WIP, not vanish and not ride along onto the next bead.
        '[ "$ID" = t-2 ] && echo leftover > leftover.txt\n'
        + CLOSE
    )
    out = _fake_run(tmp_path, docker_exit=0, queue=("t-1", "t-2"), worker=worker)
    repo = out.repo
    assert out.returncode == 0, out.stdout + out.stderr
    assert _git(repo, "log", "--format=%s", "main").stdout.split() == ["init"], "main moved"
    assert _changed(repo, "sandbox/t-1") == {"t-1.txt", "handoffs/t-1.md"}
    assert _changed(repo, "sandbox/t-2") == {"t-2.txt", "handoffs/t-2.md", "leftover.txt"}
    assert _git(repo, "log", "-1", "--format=%s", "sandbox/t-2").stdout.startswith("WIP")
    assert _git(repo, "log", "-1", "--format=%an", "sandbox/t-2").stdout.strip() == "sandbox"
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout == "", "tree left dirty"


def test_a_worker_that_moves_main_stops_the_run(tmp_path):
    # A fast-forward merge runs no pre-commit hook, so the guard alone cannot
    # stop it. The loop has to notice, and must not dispatch t-2 onto it.
    worker = (
        'echo "$ID" > "$ID.txt"; git add -A && git commit -qm "$ID"\n'
        'git checkout -q main && git merge -q --ff-only "sandbox/$ID"\n'
        + CLOSE
    )
    out = _fake_run(tmp_path, docker_exit=0, queue=("t-1", "t-2"), worker=worker)
    combined = out.stdout + out.stderr
    assert out.returncode != 0 and "outside sandbox/ moved" in combined, combined
    assert "worker 2" not in combined, "dispatched another worker after main moved"


def test_a_dirty_tree_at_start_is_refused(tmp_path):
    # Those edits would be swept into the first bead's branch and reviewed as
    # that bead's work.
    out = _fake_run(tmp_path, docker_exit=0, worker=CLOSE, dirty_start=True)
    assert out.returncode != 0 and "dirty" in out.stdout, out.stdout
    assert "worker 1" not in out.stdout
