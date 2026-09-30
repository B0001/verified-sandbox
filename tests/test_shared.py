"""Shared queue across machines (verified-sandbox-4z7).

Claims are refs on a real (bare, local) git remote, and the loops race over
real clones of it. Only bd is faked -- except in the last two tests, which use
the real bd and a real Dolt remote to pin down the two facts the design rests
on.
"""

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import verified_sandbox as vs
from test_cli import CLOSE, HOST_IDENTITY, _git

CLAIMS = Path(vs.__file__).parent / "claims.py"


def _remote_and_clones(tmp_path, *names):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "prompt.md").write_text("standing context")
    vs.cmd_install_hooks(seed)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "push", "-q", str(remote), "main")
    clones = []
    for name in names:
        subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / name)], check=True)
        vs.cmd_install_hooks(tmp_path / name)   # core.hooksPath is local config
        clones.append(tmp_path / name)
    return remote, clones


def _claims(repo, *args):
    return subprocess.run(["python3", str(CLAIMS), *args], cwd=repo,
                          capture_output=True, text=True)


def _remote_refs(remote):
    out = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/claims/"],
                         cwd=remote, capture_output=True, text=True).stdout
    return out.split()


# --- claims.py ---------------------------------------------------------------

def test_one_winner_when_six_machines_race_for_a_bead(tmp_path):
    _, clones = _remote_and_clones(tmp_path, *(f"m{n}" for n in range(6)))
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(
            lambda c: _claims(c, "take", "origin", "t-1", c.name, "3600").returncode, clones))
    assert sorted(codes) == [0, 1, 1, 1, 1, 1], codes


def test_a_live_claim_is_held_and_an_expired_one_is_taken_over(tmp_path):
    remote, (a, b) = _remote_and_clones(tmp_path, "a", "b")
    assert _claims(a, "take", "origin", "t-1", "a", "3600").returncode == 0
    assert _claims(b, "take", "origin", "t-1", "b", "3600").returncode == 1
    assert _claims(b, "held", "origin", "b", "3600").stdout.split() == ["t-1"]
    assert _claims(a, "held", "origin", "a", "3600").stdout.split() == [], \
        "a machine's own claims are not 'held elsewhere'"
    # Lease 0: a's claim is already expired from b's point of view.
    assert _claims(b, "take", "origin", "t-1", "b", "0").returncode == 0
    # a's release must not delete the claim b now holds.
    assert _claims(a, "release", "origin", "t-1").returncode == 1
    assert _remote_refs(remote) == ["refs/claims/t-1"]
    assert _claims(b, "release", "origin", "t-1").returncode == 0
    assert _remote_refs(remote) == []


def test_a_machine_can_retake_its_own_claim(tmp_path):
    # A run that died leaves its claims behind; the same machine's next run
    # picks those beads up as stale claims and must be able to reclaim them.
    _, (a,) = _remote_and_clones(tmp_path, "a")
    assert _claims(a, "take", "origin", "t-1", "a", "3600").returncode == 0
    assert _claims(a, "take", "origin", "t-1", "a", "3600").returncode == 0


# --- two loops, one queue ----------------------------------------------------

def _shared_loop(tmp_path, repo, queue, log, max_parallel=1):
    bin_dir = tmp_path / f"bin-{repo.name}"
    bin_dir.mkdir()
    (bin_dir / "bd").write_text(
        '#!/usr/bin/env bash\n'
        # One queue file stands in for a Dolt database both machines sync.
        'case "$1 $2" in\n'
        '  "ready --json")\n'
        '    printf \'{"data":[\'; sep=\n'
        '    for i in $(cat "$FAKE_QUEUE"); do\n'
        '      [ -e "$FAKE_QUEUE.closed/$i" ] && continue\n'
        '      printf \'%s{"id":"%s","issue_type":"task"}\' "$sep" "$i"; sep=,\n'
        '    done; echo "]}" ;;\n'
        '  *) echo \'{"data":[]}\' ;;\n'
        'esac\n')
    (bin_dir / "docker").write_text(
        '#!/usr/bin/env bash\n'
        'for a in "$@"; do [ "$a" = "busybox" ] && exit 0; done\n'
        'for a in "$@"; do case "$a" in *:/workspace) cd "${a%:/workspace}";; esac; done\n'
        'for a in "$@"; do case "$a" in *"YOUR TASK THIS SESSION: bead "*)\n'
        '  ID="$(printf "%s" "$a" | sed -n "s/.*YOUR TASK THIS SESSION: bead //p" | head -1)";;\n'
        'esac; done\n'
        f'echo "$ID {repo.name}" >> {log}\n'
        'sleep 1\n' + CLOSE)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    conf = tmp_path / f"conf-{repo.name}.sh"
    mount = f" -v {repo}:/workspace" if max_parallel > 1 else ""
    conf.write_text(
        f"DOCKER_ARGS=(--rm{mount})\nCHOWN_VOLUMES=(uv-cache)\nPARK_ALWAYS=\nIMAGE=fake\n"
        "MAX_ATTEMPTS=2\nMAX_WORKERS=25\nPROMPT_FILE=prompt.md\nHANDOFF_DIR=handoffs\n"
        "MIN_WORKER_SECONDS=0\nFAST_FAIL_SLEEP=1\n"
        "SANDBOX_AUTHOR_NAME=sandbox\nSANDBOX_AUTHOR_EMAIL=s@s\n"
        f"MAX_PARALLEL={max_parallel}\nSHARED_REMOTE=origin\nCLAIM_LEASE=3600\n"
        f"CLAIM_ACTOR=sandbox@{repo.name}\n")
    return subprocess.Popen(
        ["bash", str(Path(vs.__file__).parent / "loop.sh")], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, **HOST_IDENTITY, "SANDBOX_CONF": str(conf),
             "FAKE_QUEUE": str(queue), "PATH": f"{bin_dir}:{os.environ['PATH']}"})


@pytest.mark.parametrize("max_parallel", [1, 2])
def test_two_machines_never_run_the_same_bead(tmp_path, max_parallel):
    remote, (a, b) = _remote_and_clones(tmp_path, "a", "b")
    queue, log = tmp_path / "queue", tmp_path / "dispatched"
    beads = [f"t-{n}" for n in range(1, 7)]
    queue.write_text("".join(f"{t}\n" for t in beads))
    procs = [_shared_loop(tmp_path, r, queue, log, max_parallel) for r in (a, b)]
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], "\n".join(outs)
    runs = log.read_text().split("\n")[:-1]
    ran = sorted(line.split()[0] for line in runs)
    assert ran == beads, f"each bead exactly once, got {runs}\n" + "\n".join(outs)
    assert {line.split()[1] for line in runs} == {"a", "b"}, "one machine did everything"
    assert _remote_refs(remote) == [], "claims left behind"


def test_a_bead_another_machine_holds_is_not_dispatched(tmp_path):
    # b's live claim on t-1 (say, a cloud session working it) keeps a's loop
    # off it, even though a's own view says it is ready.
    remote, (a, b) = _remote_and_clones(tmp_path, "a", "b")
    assert _claims(b, "take", "origin", "t-1", "sandbox@b", "3600").returncode == 0
    queue, log = tmp_path / "queue", tmp_path / "dispatched"
    queue.write_text("t-1\nt-2\n")
    out = _shared_loop(tmp_path, a, queue, log).communicate(timeout=120)[0]
    assert log.read_text().split() == ["t-2", "a"], out
    assert _remote_refs(remote) == ["refs/claims/t-1"], "b's claim was touched"


# --- the two facts about real bd the design rests on --------------------------

def _bd(repo, *args, actor="x"):
    return subprocess.run(["bd", *args], cwd=repo, capture_output=True, text=True,
                          env={**os.environ, "BEADS_ACTOR": actor})


def _two_bd_clones(tmp_path):
    remote, (a,) = _remote_and_clones(tmp_path, "a")
    _bd(a, "init", "-q")
    for title in ("one", "two", "three"):
        _bd(a, "create", "--title", title, "--type", "task")
    _bd(a, "dolt", "remote", "add", "origin", f"git+file://{remote}")
    assert _bd(a, "dolt", "push").returncode == 0
    subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / "c")], check=True)
    c = tmp_path / "c"
    assert _bd(c, "bootstrap", "--yes").returncode == 0
    ids = {i["title"]: i["id"] for i in _issues(a)}
    return a, c, ids


def _issues(repo):
    data = json.loads(_bd(repo, "list", "--all", "--json").stdout)
    return data.get("data", data) if isinstance(data, dict) else data


# ~45s between them, so opt-in like the docker test: VS_BD_TESTS=1.
needs_bd = pytest.mark.skipif(
    os.environ.get("VS_BD_TESTS") != "1" or not shutil.which("bd"),
    reason="set VS_BD_TESTS=1 (needs the real bd)")


@needs_bd
def test_real_bd_merges_edits_to_different_beads(tmp_path):
    a, c, ids = _two_bd_clones(tmp_path)
    _bd(a, "close", ids["one"], actor="a")
    _bd(c, "update", ids["two"], "--claim", actor="c")
    assert _bd(a, "dolt", "push").returncode == 0
    assert _bd(c, "dolt", "push").returncode != 0, "a push from behind must be refused"
    assert _bd(c, "dolt", "pull").returncode == 0
    assert _bd(c, "dolt", "push").returncode == 0
    assert _bd(a, "dolt", "pull").returncode == 0
    state = lambda r: sorted((i["title"], i["status"]) for i in _issues(r))
    assert state(a) == state(c) == [("one", "closed"), ("three", "open"), ("two", "in_progress")]


@needs_bd
def test_real_bd_cannot_merge_two_edits_to_one_bead(tmp_path):
    # Why claims are settled on the git remote BEFORE anyone edits a bead: if
    # this ever starts passing a merge, the claim refs could be simplified.
    a, c, ids = _two_bd_clones(tmp_path)
    _bd(a, "update", ids["one"], "--claim", actor="a")
    _bd(c, "update", ids["one"], "--claim", actor="c")
    assert _bd(a, "dolt", "push").returncode == 0
    pulled = _bd(c, "dolt", "pull")
    assert pulled.returncode != 0 and "conflict" in (pulled.stdout + pulled.stderr)


def test_a_refused_push_is_an_error_not_a_lost_race(tmp_path):
    # A cloud session's git proxy answered 403 to refs/claims/*, and take
    # reported "someone else holds it" -- so every bead looked taken. A
    # pre-receive hook stands in for the proxy.
    remote, (a,) = _remote_and_clones(tmp_path, "a")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'proxy: refs/claims/* not allowed' >&2\nexit 1\n")
    hook.chmod(0o755)
    got = _claims(a, "take", "origin", "t-1", "a", "3600")
    assert got.returncode == 2, (got.returncode, got.stderr)
    assert "not allowed" in got.stderr
