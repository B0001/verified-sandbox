"""Cross-machine bead claims, as refs on the git remote (verified-sandbox-4z7).

    python3 claims.py take REMOTE ID ACTOR LEASE_SECONDS   # exit 0 won, 1 lost
    python3 claims.py release REMOTE ID                     # exit 0 released
    python3 claims.py held REMOTE ACTOR LEASE_SECONDS       # ids live-claimed by others

Two machines draining one queue have to agree who works which bead, and the
bead database cannot be the referee: bd's embedded Dolt aborts a pull when both
sides edited the same issue row ("merge conflicts in issues require operator
resolution"), and in embedded mode there is no way to resolve one -- `bd sql`
is unsupported and `bd vc merge --strategy theirs` refuses under autocommit.
So a claim is settled BEFORE either side touches the row: creating a ref on
the git remote is atomic, and a push that would create an existing ref is
refused. Six clones racing for one ref, five rounds: one winner each time.

A claim is `refs/claims/<id>` pointing at an empty commit whose author is the
claimant and whose date is when it claimed. Older than the lease, it may be
taken over -- by compare-and-swap on the old commit, so two machines taking
over the same dead claim still produce one winner.

ponytail: no heartbeat. A worker that outlives the lease can have its bead
taken over; set the lease above the longest bead you expect (a chem bead ran
8.5h). Upgrade path: re-push the claim from the loop's poll.
"""

import subprocess
import sys
import time

PREFIX = "refs/claims/"
# Local record of the commit each of OUR claims points at, so release (and
# take-over) is a compare-and-swap, never a blind delete of someone else's.
MINE = "refs/sandbox-claims/"


def git(*args, check=True):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def remote_claims(remote):
    """{id: sha} for every claim on the remote -- one round trip."""
    out = git("ls-remote", remote, PREFIX + "*").stdout
    return {ref[len(PREFIX):]: sha for sha, ref in
            (line.split("\t") for line in out.splitlines())}


def claim_info(remote, id_, sha):
    """(actor, unix time) of a claim commit, fetching it if we lack it."""
    if git("cat-file", "-e", sha, check=False).returncode != 0:
        git("fetch", "-q", remote, PREFIX + id_)
    actor, when = git("log", "-1", "--format=%an%x00%at", sha).stdout.strip().split("\0")
    return actor, int(when)


def new_claim_commit(id_, actor):
    tree = git("hash-object", "-w", "-t", "tree", "/dev/null").stdout.strip()
    return git("-c", f"user.name={actor}", "-c", "user.email=sandbox@verified-sandbox.invalid",
               "commit-tree", "-m", f"claim {id_} by {actor}", tree).stdout.strip()


def take(remote, id_, actor, lease):
    current = remote_claims(remote).get(id_)
    if current:
        holder, when = claim_info(remote, id_, current)
        if holder != actor and time.time() - when < lease:
            return 1
        lease_flag = [f"--force-with-lease={PREFIX}{id_}:{current}"]
    else:
        lease_flag = []   # a plain push refuses to create a ref that exists
    sha = new_claim_commit(id_, actor)
    if git("push", "-q", *lease_flag, remote, f"{sha}:{PREFIX}{id_}", check=False).returncode:
        return 1
    git("update-ref", MINE + id_, sha)
    return 0


def release(remote, id_):
    mine = git("rev-parse", "-q", "--verify", MINE + id_, check=False).stdout.strip()
    if not mine:
        return 1
    # Deletes only if it is still ours; a claim taken over after our lease ran
    # out is left alone.
    got = git("push", "-q", f"--force-with-lease={PREFIX}{id_}:{mine}", remote,
              f":{PREFIX}{id_}", check=False).returncode
    git("update-ref", "-d", MINE + id_)
    return 1 if got else 0


def held(remote, actor, lease):
    now = time.time()
    for id_, sha in sorted(remote_claims(remote).items()):
        holder, when = claim_info(remote, id_, sha)
        if holder != actor and now - when < lease:
            print(id_)
    return 0


def main(argv):
    cmd, remote, *rest = argv
    if cmd == "take":
        id_, actor, lease = rest
        return take(remote, id_, actor, int(lease))
    if cmd == "release":
        return release(remote, rest[0])
    if cmd == "held":
        actor, lease = rest
        return held(remote, actor, int(lease))
    raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except subprocess.CalledProcessError as e:
        print(f"claims: git {' '.join(e.cmd[1:3])} failed: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
