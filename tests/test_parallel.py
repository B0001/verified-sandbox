"""Parallel workers within one repo (verified-sandbox-ihq).

Same shape as test_cli.py's loop tests: the real loop.sh against a fake bd and
a fake docker, in a real git repo with the real hooks.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import verified_sandbox as vs
from test_cli import CLOSE, ROOT, _changed, _fake_run, _git, _git_repo


def test_workers_overlap_each_on_its_own_worktree_and_branch(tmp_path):
    # Each worker waits (bounded) for the other to start. Both seeing the other
    # is what proves they ran at the same time, not merely that both ran.
    worker = (
        'mkdir -p "$SYNC"; touch "$SYNC/$ID"\n'
        'other=t-1; [ "$ID" = t-1 ] && other=t-2\n'
        'for i in $(seq 1 100); do [ -e "$SYNC/$other" ] && break; sleep 0.1; done\n'
        '[ -e "$SYNC/$other" ] && touch "$SYNC/$ID.saw-other"\n'
        # The host tree must stay on main the whole time.
        'git -C "$HOST_REPO" symbolic-ref --short HEAD > "$SYNC/$ID.host-branch"\n'
        'pwd -P > "$SYNC/$ID.cwd"\n'
        'echo "$ID" > "$ID.txt"; mkdir -p handoffs; echo "done" > "handoffs/$ID.md"\n'
        'git add -A && git commit -qm "$ID" || exit 1\n'
        '[ "$ID" = t-2 ] && echo leftover > leftover.txt\n'
        + CLOSE
    )
    out = _fake_run(tmp_path, docker_exit=0, queue=("t-1", "t-2"), worker=worker,
                    max_parallel=2)
    repo, sync = out.repo, tmp_path / "sync"
    assert out.returncode == 0, out.stdout + out.stderr
    assert (sync / "t-1.saw-other").exists() and (sync / "t-2.saw-other").exists(), \
        "workers did not overlap:\n" + out.stdout
    for bead in ("t-1", "t-2"):
        assert (sync / f"{bead}.host-branch").read_text().strip() == "main"
        assert f"sandbox-worktrees/{bead}" in (sync / f"{bead}.cwd").read_text(), \
            "worker ran in the host tree, not its worktree"
    assert _changed(repo, "sandbox/t-1") == {"t-1.txt", "handoffs/t-1.md"}
    assert _changed(repo, "sandbox/t-2") == {"t-2.txt", "handoffs/t-2.md", "leftover.txt"}
    assert _git(repo, "log", "-1", "--format=%s", "sandbox/t-2").stdout.startswith("WIP")
    assert _git(repo, "log", "--format=%s", "main").stdout.split() == ["init"], "main moved"
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout == "", "host tree left dirty"
    assert len(_git(repo, "worktree", "list").stdout.splitlines()) == 1, "worktree left behind"


def test_a_bead_is_never_dispatched_twice_at_once(tmp_path):
    # A dispatched bead stays in `bd ready` until its worker claims it (this
    # fake never claims), so without in-flight tracking every free slot takes
    # t-1, and the second `git worktree add` fails on a branch already out.
    worker = 'mkdir -p "$SYNC"; echo x >> "$SYNC/$ID.runs"; sleep 1\n' + CLOSE
    out = _fake_run(tmp_path, docker_exit=0, worker=worker, max_parallel=3)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "sync" / "t-1.runs").read_text().count("x") == 1


def test_a_worker_that_moves_main_stops_dispatch(tmp_path):
    # From a worktree, main is checked out in the host tree, so a worker
    # cannot check it out -- but it can still move the ref directly.
    worker = ('echo "$ID" > "$ID.txt"; git add -A && git commit -qm "$ID"\n'
              'git update-ref refs/heads/main HEAD\n' + CLOSE)
    out = _fake_run(tmp_path, docker_exit=0, queue=("t-1", "t-2"), worker=worker,
                    max_parallel=2)
    combined = out.stdout + out.stderr
    assert out.returncode != 0 and "outside sandbox/ moved" in combined, combined


def test_a_rate_limit_is_not_charged_and_aborts(tmp_path):
    out = _fake_run(tmp_path, docker_exit=1, queue=("t-1", "t-2"), max_parallel=2)
    combined = out.stdout + out.stderr
    assert "ABORT" in combined and "PARKED" not in combined, combined
    assert out.returncode != 0


def test_a_bead_that_keeps_coming_back_is_parked(tmp_path):
    out = _fake_run(tmp_path, docker_exit=1, docker_sleep=2, max_attempts=1,
                    min_worker_seconds=1, max_parallel=2)
    combined = out.stdout + out.stderr
    assert "PARKED t-1" in combined and "ABORT" not in combined, combined


def test_max_parallel_reaches_the_loop_and_defaults_to_one():
    assert "MAX_PARALLEL=1" in vs.conf_text(ROOT, {})
    assert "MAX_PARALLEL=4" in vs.conf_text(ROOT, {"max-parallel": 4})
    assert "MAX_PARALLEL=1" in vs.conf_text(ROOT, {"max-parallel": 0})


# --- the bd lock -------------------------------------------------------------

BD_LOCK = Path(vs.__file__).parent / "bdlock" / "bd"


def _real_bd(tmp_path, body):
    real = tmp_path / "real"
    real.mkdir()
    (real / "bd").write_text("#!/bin/sh\n" + body)
    (real / "bd").chmod(0o755)
    return {**os.environ, "T": str(tmp_path), "BD_LOCK_DIR": str(tmp_path / "lock"),
            "PATH": f"{BD_LOCK.parent}:{real}:{os.environ['PATH']}"}


def test_bd_lock_serializes_calls_and_runs_the_real_bd(tmp_path):
    # The real bd is found by skipping the lock itself on PATH; finding itself
    # would recurse forever. `inside` is a critical section: a second caller
    # getting in while one is there is exactly the overlap the lock prevents.
    env = _real_bd(tmp_path,
                   'mkdir "$T/inside" 2>/dev/null || echo OVERLAP >> "$T/log"\n'
                   'sleep 0.2; echo "ran $1" >> "$T/log"; rmdir "$T/inside"\n')
    procs = [subprocess.Popen(["bd", str(n)], env=env) for n in range(4)]
    assert [p.wait(timeout=30) for p in procs] == [0, 0, 0, 0]
    log = (tmp_path / "log").read_text()
    assert "OVERLAP" not in log, log
    assert sorted(log.splitlines()) == [f"ran {n}" for n in range(4)]
    assert not (tmp_path / "lock").exists(), "lock left behind"


def test_bd_lock_passes_the_real_exit_code_through(tmp_path):
    env = _real_bd(tmp_path, "exit 3\n")
    assert subprocess.run(["bd"], env=env).returncode == 3


def test_bd_lock_holds_across_containers_on_a_real_embedded_dolt(tmp_path):
    """The failure the lock exists for only happens across Docker Desktop's
    file sharing, so only a real run shows it: unlocked, 47 of 60 concurrent
    container `bd create`s failed with Dolt panics. Slow, and needs docker, bd
    and the `claude` image, so it is opt-in: VS_DOCKER_TESTS=1."""
    if os.environ.get("VS_DOCKER_TESTS") != "1" or not shutil.which("bd"):
        pytest.skip("set VS_DOCKER_TESTS=1 (needs docker, bd and the claude image)")
    repo = _git_repo(tmp_path / "repo")
    subprocess.run(["bd", "init", "-q"], cwd=repo, capture_output=True, check=True)
    root = str(repo.resolve())
    procs = [subprocess.Popen(
        ["docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{root}:{root}", "-w", root,
         "-v", f"{BD_LOCK}:/usr/local/sbin/bd:ro", "-e", f"BD_LOCK_DIR={root}/.beads/.bd-lock",
         "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
         "-e", "GIT_CONFIG_VALUE_0=*", "claude",
         "-c", f"for i in 1 2 3 4 5 6 7 8; do bd create --title c{w}-$i --type task"
               " >/dev/null || exit 1; done"])
        for w in range(4)]
    assert [p.wait(timeout=600) for p in procs] == [0, 0, 0, 0]
    listed = json.loads(subprocess.run(["bd", "list", "--all", "--json"], cwd=repo,
                                       capture_output=True, text=True, check=True).stdout)
    issues = listed.get("data", listed) if isinstance(listed, dict) else listed
    assert sorted(i["title"] for i in issues) == sorted(
        f"c{w}-{i}" for w in range(4) for i in range(1, 9))


def test_bd_exports_left_by_a_parallel_run_do_not_block_the_next(tmp_path):
    # Workers' bd calls write .beads/*.jsonl in the HOST tree, where the shared
    # database lives, so every parallel run ends with them modified.
    out = _fake_run(tmp_path, docker_exit=0, worker=CLOSE, max_parallel=2,
                    bd_export_dirty=True)
    assert out.returncode == 0 and "worker 1" in out.stdout, out.stdout + out.stderr
    assert "bd's exports changed" in out.stdout


def test_any_other_dirt_still_blocks_a_parallel_run(tmp_path):
    out = _fake_run(tmp_path, docker_exit=0, worker=CLOSE, max_parallel=2,
                    bd_export_dirty=True, dirty_start=True)
    assert out.returncode != 0 and "dirty" in out.stdout, out.stdout
    assert "worker 1" not in out.stdout


def test_bd_exports_still_block_a_sequential_run(tmp_path):
    # max-parallel = 1 is the original loop: there, a worker's bd writes land
    # in its own tree and ride on its branch, so dirt here is still dirt.
    out = _fake_run(tmp_path, docker_exit=0, worker=CLOSE, bd_export_dirty=True)
    assert out.returncode != 0 and "dirty" in out.stdout, out.stdout
