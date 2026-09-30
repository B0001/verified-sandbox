# verified-sandbox

The containerised beads worker loop, as a package instead of a file copied into
every repo.

Six repos had their own `sandbox.sh`, 202–337 lines each. Diffing them, the
only things that genuinely differed were which secrets to forward, which extra
volumes to mount, and one repo's extra env value. Everything else — the lock,
the Ctrl-C trap, the epic filter, the parking logic, the stranded-bead report —
was the same logic at five different vintages, because a fix made in one repo
was never backported to the rest.

## Use

```toml
# pyproject.toml
[dependency-groups]
dev = ["verified-sandbox"]

[tool.sandbox]
# every key optional; these are the defaults
image = "claude"
max-attempts = 2                       # same bead unclosed this often -> park it
max-workers = 25                       # backstop against a runaway queue
prompt-file = "sandbox-prompt.md"
handoff-dir = "sandbox-handoffs"
volumes = []                           # extra named volumes, "name:/path"

[tool.sandbox.forward-env]
# Secrets. Emitted as a bare `-e NAME`, so the value is taken from your shell
# and never written into docker's argv where `ps` can read it. The text is the
# warning printed when the variable is unset.
GITHUB_TOKEN = "GitHub fetcher beads get 60 req/hr"

[tool.sandbox.env]
# Non-secret values. These DO appear in docker's argv. ANTHROPIC_BASE_URL and
# ANTHROPIC_SMALL_FAST_MODEL default to the local proxy; set either to "" to
# drop the flag and talk to the real API.
```

```bash
uv run sandbox install-hooks   # once per repo, and after bd rewrites hooks
uv run sandbox                 # drain the queue
```

## Branch per bead

Each worker runs on its own `sandbox/<bead-id>` branch, cut from whatever branch
the run started on, and commits its work and handoff there. After it exits the
loop commits anything it left uncommitted onto that branch as a `WIP` commit,
then returns to the base branch for the next bead. Workers never push and never
merge: review a bead with `git log --stat main..sandbox/<bead-id>`, merge what
passes.

The run refuses to start on a dirty tree, since those edits would be swept into
the first bead's branch. It stops mid-run if a worker leaves HEAD on another
branch or moves any branch outside `sandbox/` (a fast-forward merge runs no
pre-commit hook, so the loop checks the refs itself). Worker commits are
authored as `sandbox <sandbox@verified-sandbox.invalid>`.

A per-task git policy is appended to every worker prompt and says it supersedes
the repo prompt's. Older `sandbox-prompt.md` files that say "do not commit"
still work, but are worth updating to match.

## What `install-hooks` does

Adds a `BEADS_ACTOR=sandbox` guard to `.beads/hooks/pre-commit` and `pre-push`,
above the beads-managed markers so `bd` regenerating the file does not eat it,
then sets `core.hooksPath` to the **relative** path `.beads/hooks`. The
pre-commit guard refuses a sandbox commit on any branch but `sandbox/<id>`;
the pre-push guard refuses every sandbox push. Re-running it replaces an older
guard in place, and `sandbox run` refuses to start until it has been re-run.

Relative matters. An absolute path resolves on the host and points at nothing
inside the container, and git runs no hook at all — silently, no error — when
`core.hooksPath` is missing. That is how the commit guard sat disarmed for
weeks in one repo. `sandbox run` re-checks it before dispatching anything and
refuses to start if it is not armed.

## Upgrading

Refine the loop here; bump the version in each repo. No repo file changes.
