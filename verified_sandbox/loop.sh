#!/usr/bin/env bash
# Drain the beads queue, one containerised worker per bead.
#
# Preflight (config, token checks, the hooksPath guard) runs in __init__.py
# before this is exec'd; it arrives with $SANDBOX_CONF sourced-ready and the
# cwd already at the repo root.
#
# Each bead gets a FRESH container and a fresh context window -- that is the
# point. A single -p session trying to do a whole phase runs out of context
# mid-task; one bead per session does not.
#
# Notes on the docker invocation (DOCKER_ARGS is assembled in Python):
#   * The mounted host .venv symlinks into the host's python install and is
#     dead in the container, so uv is pointed at /tmp/venv instead.
#   * The uv volumes keep the interpreter + wheel downloads from repeating on
#     every worker.
#   * The container gets the host's network but NOT the host's shell env, so
#     any API token a bead needs is forwarded explicitly -- an unauthenticated
#     worker hits a rate limit and files a bug that is really a 403. Secrets
#     go through `forward-env`, which emits a bare `-e NAME`, NOT
#     `-e NAME="$NAME"`: the second form puts the value in docker's argv where
#     any user on the host can read it out of `ps`.
#   * Do NOT add a volume for /home/node/.claude. The config claude reads is
#     /home/node/.claude.json, which sits OUTSIDE that directory; persisting
#     only the directory leaves a stale .claude/backups/ next to a missing
#     config and every run after the first dies with "Claude configuration
#     file not found". Durable output is sandbox-handoffs/, not session state.

set -uo pipefail

# shellcheck disable=SC1090
source "$SANDBOX_CONF"   # DOCKER_ARGS, IMAGE, MAX_ATTEMPTS, MAX_WORKERS, PROMPT_FILE,
                         # HANDOFF_DIR, MIN_WORKER_SECONDS, FAST_FAIL_SLEEP,
                         # SANDBOX_AUTHOR_NAME, SANDBOX_AUTHOR_EMAIL

LOCK_DIR=".sandbox.lock"

mkdir -p "$HANDOFF_DIR"

# Two concurrent loops would re-dispatch each other's in-progress beads, so
# take an exclusive lock. mkdir is atomic; a stale dir after a hard kill is
# removed by hand, and the message says so.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "another sandbox run appears to be in progress."
  echo "if it is not, remove the stale lock: rmdir $LOCK_DIR"
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

# Branch per bead (verified-sandbox-rbz). Each worker runs on its own
# sandbox/<bead-id> branch cut from the branch the run started on, and commits
# its work and handoff there; the pre-commit guard refuses a sandbox commit on
# any other branch. Without this every bead in a run landed in one shared dirty
# tree, and six beads' interleaved edits had to go in as one commit that could
# not be reviewed, reverted or bisected per bead (einstein, 2026-09-29).
#
# Workers never merge and never push. The loop checks that no branch outside
# sandbox/ moved while a worker ran, because a fast-forward merge runs no
# pre-commit hook and would otherwise slip past the guard.
BASE="$(git symbolic-ref --short -q HEAD)"
case "$BASE" in
  "")
    echo "FATAL: not on a branch (detached HEAD, or not a git repo). Check out"
    echo "       the branch bead work should start from, then re-run."
    exit 1 ;;
  sandbox/*)
    echo "FATAL: on $BASE, a bead branch -- probably left by an interrupted run."
    echo "       Check out the base branch (e.g. main), then re-run."
    exit 1 ;;
esac
if [ -n "$(git status --porcelain)" ]; then
  echo "FATAL: the working tree is dirty. Every bead branches from $BASE, so these"
  echo "       changes would be swept into the first bead's branch. Commit or"
  echo "       stash them, then re-run."
  git status --short | head -20
  exit 1
fi

protected_refs() {
  git for-each-ref --format='%(refname) %(objectname)' refs/heads \
    | grep -v '^refs/heads/sandbox/'
}

CURRENT_BRANCH=""
REFS_BEFORE=""

# Put the next worker on $1, creating it from $BASE if this is the bead's first
# dispatch. A stale re-dispatch reuses the branch, so the previous attempt's
# commits are exactly what the prompt tells the worker to inspect.
enter_branch() {
  REFS_BEFORE="$(protected_refs)"
  if git show-ref --verify -q "refs/heads/$1"; then
    git checkout -q "$1"
  else
    git checkout -q -b "$1" "$BASE"
  fi || { echo "==> ABORT: could not check out $1."; return 1; }
  CURRENT_BRANCH="$1"
}

# After a worker exits: commit anything it left uncommitted onto ITS branch, so
# it is neither lost nor carried onto the next bead's branch, then return to
# $BASE. Any surprise stops the run rather than guessing -- the next worker must
# start from a known tree.
leave_branch() {
  local b="$CURRENT_BRANCH" now
  [ -n "$b" ] || return 0
  CURRENT_BRANCH=""
  if [ "$(protected_refs)" != "$REFS_BEFORE" ]; then
    echo "==> ABORT: a branch outside sandbox/ moved while the worker on $b ran."
    echo "    Workers never merge. Before vs after:"
    diff <(printf '%s\n' "$REFS_BEFORE") <(protected_refs) | sed 's/^/    /'
    return 1
  fi
  now="$(git symbolic-ref --short -q HEAD)"
  if [ "$now" != "$b" ]; then
    echo "==> ABORT: the worker on $b left HEAD on '${now:-detached HEAD}'."
    echo "    Nothing was committed or switched; inspect by hand."
    return 1
  fi
  if [ -n "$(git status --porcelain)" ]; then
    echo "==> worker left uncommitted changes; committing them to $b as WIP"
    git add -A &&
      GIT_AUTHOR_NAME="$SANDBOX_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$SANDBOX_AUTHOR_EMAIL" \
      git commit -q -m "WIP ($b): uncommitted changes the worker left behind

Committed by the sandbox loop so they are neither lost nor carried onto the
next bead's branch. Review before merging." || {
        echo "==> ABORT: could not commit the leftovers on $b (a hook refused?)."
        echo "    The tree is still on $b, dirty. Resolve it, then check out $BASE."
        return 1
      }
    if [ -n "$(git status --porcelain)" ]; then
      echo "==> ABORT: $b is still dirty after the WIP commit (a hook wrote files?)."
      return 1
    fi
  fi
  git checkout -q "$BASE" || { echo "==> ABORT: could not return to $BASE."; return 1; }
}

# Ctrl-C must stop the RUN, not just the worker. `docker run -it` forwards the
# terminal's SIGINT to the container, claude catches it and exits non-zero, and
# docker itself returns normally -- so bash sees an ordinary failed command and
# dispatches the next worker. You cannot interrupt the loop, and every Ctrl-C
# burns an attempt until MAX_ATTEMPTS parks a bead that was never actually
# tried. The trap fires once the foreground docker returns, which is enough.
trap 'echo; echo "==> interrupted; stopping."; leave_branch; exit 130' INT

# Docker initialises a named volume root-owned when its mount path doesn't
# already exist in the image, so the two uv volumes come up as root:root and
# uv is unusable for the node (uid 1000) user every worker runs as. Relocating
# the mounts does not help -- a fresh volume at any new path is root-owned too
# (tested). chown the volume contents once from a throwaway root container;
# it persists into every later mount, so this is a no-op after the first run.
#
# Every NAMED volume needs this, not just the two uv ones -- a volume added
# through [tool.sandbox] volumes is root-owned exactly the same way, and a
# toolchain cache the worker cannot write to is worse than none at all: it
# re-downloads silently, every session, and looks like a slow bead.
chown_mounts=()
chown_paths=()
mount_i=0
for vol in "${CHOWN_VOLUMES[@]}"; do
  chown_mounts+=(-v "$vol:/chown$mount_i")
  chown_paths+=("/chown$mount_i")
  mount_i=$((mount_i + 1))
done
docker run --rm "${chown_mounts[@]}" busybox \
  chown -R 1000:1000 "${chown_paths[@]}" || { echo "could not chown volumes"; exit 1; }

run_worker() {
  docker run -it --rm --init "${DOCKER_ARGS[@]}" "$IMAGE" \
    -p "$1" --dangerously-skip-permissions
}

# Appended to every worker prompt. Repo prompts written before branch-per-bead
# say "do not commit"; this says which rule wins, so a worker is not left
# choosing between two instructions.
git_policy() {
  printf '%s\n' "## Git on this run (supersedes any git policy above)

You are on branch \`$1\`, cut from \`$BASE\` for this task alone. Commit your
work AND your handoff file here, in as many commits as make the diff easy to
review; the pre-commit hook refuses a commit on any other branch. Do not switch
branches, do not merge, do not rebase onto or reset another branch, and do not
push -- the host pushes and a human merges. Anything you leave uncommitted is
committed for you as WIP, which is worse for your reviewer than a real message."
}

# Beads that hit MAX_ATTEMPTS. They are skipped for the rest of the run and
# reported at the end. This list is the whole reason a bad bead no longer kills
# the run: parking one and moving on drains the queue, aborting on it does not.
#
# It starts from `park` in [tool.sandbox]: beads a worker structurally cannot
# finish, no matter how many sessions it gets -- one asking for an independent
# human reviewer, say. Those are never dispatched, so they never burn an
# attempt, and they are reported separately at the end because "deliberately
# never dispatched" and "tried twice and failed" need different reactions.
PARKED="$PARK_ALWAYS"

is_parked() { case " $PARKED " in *" $1 "*) return 0;; *) return 1;; esac; }

# Shape handling and the id extraction live in ids.py, so a change in bd's
# --json envelope is one edit rather than three, and is testable. It exits
# nonzero on a shape it does not recognise; pipefail carries that out of these
# functions, which is what queue_empty checks before trusting an empty queue.
IDS="$(dirname "$0")/ids.py"

ids_by_status() {
  bd list --status="$1" --json 2>/dev/null | python3 "$IDS"
}

# Epics are excluded from dispatch: a parent is marked in_progress as soon as
# any child is claimed, so it sits in_progress permanently and is not work a
# worker can finish. Dispatching one burns a whole session on nothing.
stale_ids() {
  bd list --status=in_progress --json 2>/dev/null | python3 "$IDS" epic
}

# Epics are excluded here for the same reason as above. `bd ready` surfaces an
# epic whose children are all still unstarted -- it only disappears from the
# ready queue once a child is claimed and the parent flips to in_progress -- so
# without this filter the loop can dispatch a worker onto a bead nobody can
# finish.
ready_ids() {
  bd ready --json 2>/dev/null | python3 "$IDS" epic
}

# Both selectors skip parked ids. Skipping them in the READY path matters as
# much as in the stale path: a worker that leaves its bead open (rather than
# claimed) puts it straight back at the head of `bd ready`, and without the
# skip the loop re-dispatches it forever.
first_unparked() {
  for id in $1; do
    is_parked "$id" && continue
    echo "$id"
    return
  done
}

next_ready() { first_unparked "$(ready_ids)"; }

# bd ready EXCLUDES in_progress beads. A worker that claims one and then dies,
# stalls, or hits a limit leaves it invisible to the queue forever -- so
# "nothing ready" is not the same as "nothing left". This is the fallback that
# makes the difference visible, and it is what gives MAX_ATTEMPTS something to
# count: a stale claim that keeps failing now comes back instead of vanishing.
#
# It fires only once the ready queue is drained, so strays accumulate during a
# long run and are swept at the end. That is deliberate -- fresh work first --
# but it means a run that ends early (MAX_WORKERS, a kill) never reaches the
# sweep. The end-of-run report names anything left, so it is visible either way.
next_stale() { first_unparked "$(stale_ids)"; }

# Emptiness is the trigger for the triage pass below, and triage FILES BEADS
# -- it is the only branch of this script that writes. The selectors above
# swallow any error from bd (deliberately: one unparseable record must not
# abort a whole drain), so an empty string from them means EITHER "the queue
# is empty" OR "bd failed", and those are not the same fact. Confirm bd is
# healthy before trusting emptiness; otherwise a transient bd failure files a
# duplicate queue on top of the real one. Observed doing exactly that.
# The health check has to run the SAME pipeline the selectors do, parse and
# all. Checking only `bd ready`'s exit code does not: bd 1.1.2 exits 0 while
# emitting an envelope the old parser could not read, so the check passed, the
# selectors returned nothing, and triage filed beads over a live queue --
# exactly the failure this function exists to prevent.
queue_empty() {
  ready_ids >/dev/null || {
    echo "FATAL: 'bd ready --json' failed, or returned a shape ids.py does not"
    echo "       understand, so an empty queue cannot be trusted. Refusing to"
    echo "       run triage -- it would file beads over a queue that may well"
    echo "       exist. Fix bd (or ids.py), then re-run."
    exit 1
  }
  [ -z "$(next_ready)" ] && [ -z "$(next_stale)" ]
}

# Empty queue on the first pass means the phase has not been triaged yet, not
# that the work is done. Seed it: one worker that files beads and writes no
# code.
if queue_empty; then
  echo "==> queue empty; running triage pass to file beads"
  triage_branch="sandbox/triage-$(date +%Y%m%d-%H%M%S)"
  enter_branch "$triage_branch" || exit 1
  run_worker "$(cat "$PROMPT_FILE")

$(git_policy "$triage_branch")

---

# YOUR TASK THIS SESSION: triage only

Do NOT write or modify any code, test, or document this session. Your entire
job is to turn the objectives above into a work queue.

File one bead per discrete, independently-completable unit of work with
\`bd create\`, and use \`bd dep add\` where one genuinely blocks another. Each
bead's description must carry enough detail that a fresh session with no
memory of this one can execute it from \`bd show\` alone: what is wrong, how
you confirmed it, and what evidence would close it. Reproduce before you
file -- a bead asserting a problem you did not actually observe wastes a whole
worker session.

Leave every bead open and unclaimed. Then stop and report the list."
  leave_branch || exit 1
  echo "==> triage complete"
fi

last_id=""
attempts=0
workers=0

# Consecutive workers that died too fast to have attempted their bead. See the
# fast-failure block at the bottom of the loop for what this is defending
# against.
fast_failures=0
aborted=""

while :; do
  TASK_ID="$(next_ready)"

  if [ -z "$TASK_ID" ]; then
    TASK_ID="$(next_stale)"
    if [ -n "$TASK_ID" ]; then
      echo "==> nothing ready; re-dispatching stale claim: $TASK_ID"
    fi
  fi

  if [ -z "$TASK_ID" ]; then
    echo "==> queue drained after $workers worker(s)"
    break
  fi

  # Livelock guard. Counting consecutive re-dispatches of the SAME id is what
  # detects a bead a worker cannot finish; parking it (rather than exiting) is
  # what lets the other five stranded beads still get their turn.
  if [ "$TASK_ID" = "$last_id" ]; then
    attempts=$((attempts + 1))
  else
    attempts=1
    last_id="$TASK_ID"
  fi
  if [ "$attempts" -gt "$MAX_ATTEMPTS" ]; then
    echo "==> PARKED $TASK_ID: came back unfinished $MAX_ATTEMPTS times."
    echo "    Inspect with: bd show $TASK_ID"
    echo "    Its branch and handoff (if any): git show sandbox/$TASK_ID:$HANDOFF_DIR/$TASK_ID.md"
    PARKED="$PARKED $TASK_ID"
    last_id=""
    attempts=0
    continue
  fi

  workers=$((workers + 1))
  if [ "$workers" -gt "$MAX_WORKERS" ]; then
    echo "==> ABORT: hit MAX_WORKERS=$MAX_WORKERS. Queue is growing, not draining."
    break
  fi

  echo "==> worker $workers: $TASK_ID (attempt $attempts)"

  enter_branch "sandbox/$TASK_ID" || { aborted="could not branch for $TASK_ID"; break; }
  started_at="$(date +%s)"
  run_worker "$(cat "$PROMPT_FILE")

$(git_policy "sandbox/$TASK_ID")

---

# YOUR TASK THIS SESSION: bead $TASK_ID

Run \`bd show $TASK_ID\` first -- it is the specification. Everything above is
standing context for this repo; the objectives section is background, not your
assignment. Do only this bead.

If it is already marked in_progress, a previous worker claimed it and did not
finish. Read its notes, do not assume its partial work is correct, and check
the working tree for what it left behind before continuing.

Claim it with \`bd update $TASK_ID --claim\` before you start.

Close it with \`bd close $TASK_ID\` ONLY when the evidence the bead's
acceptance criteria ask for exists and the test suite passes. If you cannot
finish it, leave it open, say why in \`bd update $TASK_ID --notes=...\`, and
stop -- do not close a bead to make the queue move. If the bead turns out to
be wrong or already done, close it with \`--reason\` explaining that, which is
a real outcome and not a failure.

If you discover work outside this bead's scope, file it as a new bead. Do not
do it now.

Write your handoff to \`$HANDOFF_DIR/$TASK_ID.md\` BEFORE you close the bead.
A session that dies after closing and before writing leaves no trace of how
the work was done; one that dies the other way round is merely unfinished."

  status=$?
  elapsed=$(( $(date +%s) - started_at ))
  leave_branch || { aborted="the tree was not safe to hand to the next worker"; break; }
  [ "$status" -ne 0 ] && echo "==> worker exited $status after ${elapsed}s"

  # A worker cannot fail a bead in seconds. It has to read the bead, look at
  # the repo, and try something; the floor on that is minutes. So a non-zero
  # exit inside MIN_WORKER_SECONDS did not come from the work -- it is a usage
  # or rate limit, an expired CLAUDE_CODE_OAUTH_TOKEN, or a proxy that died
  # mid-run. The loop cannot tell those apart, and it does not need to: what it
  # must not do is charge them to the bead.
  #
  # Without this, a rate limit drains the whole queue in under a minute.
  # MAX_ATTEMPTS defaults to 2, so each bead is dispatched, dies instantly,
  # is re-dispatched, dies again, and is PARKED -- and the run reports a queue
  # of beads that "came back unfinished twice", which reads as hard work rather
  # than a dead credential. This is the same failure check_proxy() exists to
  # prevent at startup, arriving mid-run instead.
  #
  # One fast failure gets a short wait and a free retry: transient things
  # happen, and the bead did nothing wrong. Two in a row is not transient, and
  # the run stops rather than guessing at a limit window whose length nobody
  # here knows.
  if [ "$status" -ne 0 ] && [ "$elapsed" -lt "$MIN_WORKER_SECONDS" ]; then
    fast_failures=$((fast_failures + 1))
    # Not the bead's fault, so it must not carry the attempt: reset the
    # livelock counter, or two rate-limited dispatches park a healthy bead.
    last_id=""
    attempts=0
    if [ "$fast_failures" -ge 2 ]; then
      aborted="two workers in a row exited in under ${MIN_WORKER_SECONDS}s"
      echo "==> ABORT: $aborted."
      echo "    That is too fast to be the bead. Usual causes, in order:"
      echo "      * a usage or rate limit on the account behind CLAUDE_CODE_OAUTH_TOKEN"
      echo "      * that token expired or revoked"
      echo "      * the proxy at ANTHROPIC_BASE_URL died after the startup check"
      echo "    No bead was parked and nothing was charged against MAX_ATTEMPTS."
      echo "    $TASK_ID is still claimed; re-running picks it up as a stale claim."
      break
    fi
    echo "==> not counting that against $TASK_ID; retrying in ${FAST_FAIL_SLEEP}s"
    sleep "$FAST_FAIL_SLEEP"
    continue
  fi
  fast_failures=0
done

stranded="$(ids_by_status in_progress)"
open_left="$(ids_by_status open)"

echo
echo "Handoffs:   $HANDOFF_DIR/ on each sandbox/<bead-id> branch"
echo "Open beads: $(printf '%s' "$open_left" | wc -w | tr -d ' ')"
echo "Nothing was pushed or merged. Each bead's work is on its own branch:"
echo "  git for-each-ref --sort=-committerdate --format='%(refname:short)' refs/heads/sandbox/"
echo "  git log --stat $BASE..sandbox/<bead-id>"

exit_code=0

# An infrastructure abort must never look like a clean drain. The queue was not
# emptied; the run was stopped, and the open beads printed above are untouched
# work, not leftovers.
if [ -n "$aborted" ]; then
  echo
  echo "RUN STOPPED: $aborted."
  echo "  Fix the cause above, then re-run -- claimed beads come back as stale claims."
  exit_code=1
fi

# Only the ones this run actually gave up on. A bead from `park` was never
# dispatched, so reporting it as "tried twice and failed" would be a lie, and
# failing the run over a deliberate config choice would make a clean drain
# impossible to ever observe.
failed_park=""
for id in $PARKED; do
  case " $PARK_ALWAYS " in *" $id "*) continue;; esac
  failed_park="$failed_park $id"
done

if [ -n "$failed_park" ]; then
  echo
  echo "PARKED -- dispatched $MAX_ATTEMPTS times and never finished:"
  for id in $failed_park; do echo "    $id"; done
  echo "These need a human. Start with the bead and its handoff."
  exit_code=1
fi

if [ -n "$PARK_ALWAYS" ]; then
  echo
  echo "Never dispatched (park list in [tool.sandbox]):"
  for id in $PARK_ALWAYS; do echo "    $id"; done
fi

# "Queue drained" must never be reported while beads sit claimed-but-unclosed.
# Parked ids are listed above; anything here that is not parked is a claim no
# worker ever came back to -- usually a session that died mid-bead.
if [ -n "$stranded" ]; then
  unswept=""
  for id in $stranded; do
    is_parked "$id" || unswept="$unswept $id"
  done
  if [ -n "$unswept" ]; then
    echo
    echo "STRANDED -- claimed but never closed:"
    for id in $unswept; do echo "    $id"; done
    echo "These are NOT done. Inspect with: bd show <id>"
    exit_code=1
  fi
fi

exit "$exit_code"
