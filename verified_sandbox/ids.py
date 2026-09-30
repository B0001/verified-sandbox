"""Read `bd ... --json` on stdin, print the issue ids, space-separated.

bd 1.1 wrapped --json output in {"data": [...]}; older versions emitted a bare
list. Both are accepted. Anything else exits 3 rather than printing nothing --
"I do not understand this shape" and "the queue is empty" are not the same
fact, and the loop runs a bead-filing triage pass on the second one. bd 1.1.2
shipped the wrapper against a loop that only knew the bare list: every
selector went quiet, the loop believed the queue was empty, and triage filed
beads on top of a live one.

argv is the list of issue_type values to drop (the loop passes `epic`).
"""

import json
import sys

data = json.load(sys.stdin)
if isinstance(data, dict):
    data = data.get("data", data.get("issues"))
if not isinstance(data, list):
    sys.exit(3)
skip = sys.argv[1:]
print(" ".join(i["id"] for i in data if i.get("issue_type") not in skip))
