"""The checks that fail if the loop's real invariants break.

Not coverage for its own sake -- each of these encodes a bug that actually
happened in one of the hand-maintained copies.
"""

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
