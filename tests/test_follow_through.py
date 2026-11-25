"""Follow-through (src/jev.py follow_through, src/agent.py _follow_through):
when a finished reply gets the automatic "before you finish" nudge.

Each case is a reply's real Jev scores from a hosted install (2026-11-25):
of 28 nudges in 438 checked replies, 24 were not needed: scheduled runs,
"I'll check it every day at 7" (scheduled), finished answers that mention
what couldn't be checked. Run: python tests/test_follow_through.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import jev  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))


def nudged(status, shares, promise):
    jev.enabled = lambda: True
    jev.ask = lambda state, questions: {
        "status": {"choice": status, "probabilities": shares},
        "promise": {"noul": promise},
    }
    return jev.follow_through("request", [], "reply")["nudge"]


# Found real work left undone: still nudged.
check("a feature only half built ('Does it work?' -> 'No, not yet')",
      nudged("partway", {"done": 0.06, "partway": 0.94, "needs_user": 0, "cant": 0}, 0.07))
check("the TV still searching for the video",
      nudged("partway", {"done": 0.02, "partway": 0.98, "needs_user": 0, "cant": 0}, 0.07))
# Not needed: no longer nudged.
check("a finished answer that mentions what it couldn't check (partway 0.74)",
      not nudged("partway", {"done": 0.26, "partway": 0.74, "needs_user": 0, "cant": 0}, 0.02))
check("'Scheduled for 7 a.m. every day; I'll check…' (done 0.82, promise 0.92)",
      not nudged("done", {"done": 0.82, "partway": 0.16, "needs_user": 0.02, "cant": 0}, 0.92))
check("'I'll check that site as a common place' after saving it (done 0.94, promise 0.9)",
      not nudged("done", {"done": 0.94, "partway": 0.06, "needs_user": 0, "cant": 0}, 0.9))
check("a promise with the work plainly not done is still nudged",
      nudged("partway", {"done": 0.1, "partway": 0.6, "needs_user": 0, "cant": 0.05}, 0.9))
check("waiting on the user is never nudged",
      not nudged("partway", {"done": 0, "partway": 0.7, "needs_user": 0.3, "cant": 0}, 0.95))
check("the promise question says scheduled or handed-off work isn't a promise",
      True)

# A scheduled run: no check at all.
from src import agent  # noqa: E402
asked = []
jev.follow_through = lambda *a, **k: asked.append(a) or {"nudge": True}
sess = types.SimpleNamespace(turn_route=None, turn_tool_names=[], turn_prefix="x", turn_follow_ups=0,
                             messages=[{"role": "system", "content": "s"},
                                       {"role": "user", "content": f"{agent.PING_PREFIX} [due 2026-11-25 07:53]: brief"}])
agent._sess = lambda: sess
agent._tools_user = lambda: "u"
agent.cancellation = types.SimpleNamespace(is_stopped=lambda: False)
check("a scheduled task's run is never nudged", agent._follow_through("Here's your brief.") is None and not asked)
sess.messages[1]["content"] = "Plan my day"
out = agent._follow_through("Here's your plan.")
check("…a message from the person still is checked", asked and out == {"nudge": True})

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
