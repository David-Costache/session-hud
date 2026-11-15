"""Claude Code hook: forwards the hook JSON on stdin to the HUD daemon.

Always exits 0. Prints a decision only for PermissionRequest and only when the
HUD returned allow/deny; any failure prints nothing, so Claude Code shows its
normal permission prompt.
"""
import json
import sys

WAIT = 130  # a little above the daemon's own 120s limit, below the 150s hook timeout


def main():
    raw = sys.stdin.buffer.read()
    approval = json.loads(raw).get("hook_event_name") == "PermissionRequest"
    import common

    decision = common.call("/event", raw, WAIT if approval else 1).get("decision")
    if not approval or decision not in ("allow", "deny"):
        return
    d = {"behavior": decision}
    if decision == "deny":
        d["message"] = "Denied from the session HUD"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))


try:
    main()
except Exception:
    pass
sys.exit(0)
