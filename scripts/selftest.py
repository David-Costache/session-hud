"""Self-test. `python selftest.py` needs the HUD running; `python selftest.py down` needs it stopped.

Drives the real hook.py with simulated hook JSON for a fake session. The Approve/Deny
step calls POST /decide, which runs the same decide() the buttons call; it does not click.
"""
import json
import os
import subprocess
import sys
import time

import common

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")
SID = f"selftest-{os.getpid()}"
BASE = {"session_id": SID, "cwd": "C:/fake/my-app"}
BASH = {"tool_name": "Bash", "tool_input": {"command": "npm test"}}


def payload(event, **extra):
    return json.dumps({**BASE, "hook_event_name": event, **extra}).encode()


def hook(event, **extra):
    t = time.time()
    r = subprocess.run([sys.executable, HOOK], input=payload(event, **extra), capture_output=True)
    return r.returncode, r.stdout.decode().strip(), time.time() - t


def session():
    return next((s for s in common.call("/state")["sessions"] if s["id"] == SID), None)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        sys.exit(1)


def approval(decision):
    p = subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.stdin.write(payload("PermissionRequest", **BASH))
    p.stdin.close()
    s = None
    for _ in range(50):
        s = session()
        if s and s["pending"]:
            break
        time.sleep(0.1)
    check(f"{decision}: hook is blocked and HUD shows the request",
          p.poll() is None and s["status"] == "Waiting for Approval", s["pending"][0]["preview"])
    t = time.time()
    common.call("/decide", {"id": s["pending"][0]["id"], "decision": decision})
    out = p.stdout.read().decode().strip()
    code = p.wait(5)
    got = json.loads(out)["hookSpecificOutput"]
    check(f"{decision}: hook unblocked with the right JSON, exit {code}",
          code == 0 and got["hookEventName"] == "PermissionRequest"
          and got["decision"]["behavior"] == decision, f"{time.time() - t:.2f}s  {out}")


def up():
    for event, extra, status, action in [
        ("SessionStart", {}, "Idle", ""),
        ("UserPromptSubmit", {"prompt": "hi"}, "Thinking", ""),
        ("PreToolUse", BASH, "Running Tool", "Running: npm test"),
        ("PostToolUse", BASH, "Thinking", ""),
        ("Stop", {}, "Idle", ""),
    ]:
        code, out, dt = hook(event, **extra)
        s = session()
        check(f"{event} -> {status}", code == 0 and out == "" and s["project"] == "my-app"
              and s["status"] == status and s["action"] == action,
              f'{s["project"]} — {s["action"] or s["status"]}, hook {dt:.2f}s')
    # what the AI is doing: tool line + Claude's latest message from a fake transcript
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for typ, content in [("assistant", [{"type": "text", "text": "old turn"}]),
                             ("user", "fix the bug"),
                             ("assistant", [{"type": "text", "text": "Fixing the parser now."},
                                            {"type": "tool_use", "name": "Edit"}])]:
            f.write(json.dumps({"type": typ, "message": {"content": content}}) + "\n")
    hook("PreToolUse", tool_name="Edit", tool_input={"file_path": "C:/fake/my-app/src/parser.py"},
         transcript_path=f.name)
    s = session()
    os.unlink(f.name)
    check("tool line and Claude's message are shown",
          s["action"] == "Editing parser.py" and s["note"] == "Fixing the parser now.",
          f'{s["action"]} / {s["note"]}')
    common.call("/dismiss", {"id": SID})  # same dismiss() the row's remove button calls
    check("remove hides the session", session() is None)
    hook("PostToolUse", **BASH)
    check("removed session stays hidden while it keeps working", session() is None)
    approval("allow")  # also proves a hidden session comes back when it needs permission
    approval("deny")
    try:
        common.call("/state", tok="wrong")
        check("wrong token rejected", False)
    except RuntimeError as e:
        check("wrong token rejected", "403" in str(e), str(e))
    hook("SessionEnd")
    check("SessionEnd removes the session", session() is None)


def down():
    try:
        common.call("/state")
        check("daemon is stopped", False, "it is still running")
    except OSError:
        pass
    code, out, dt = hook("PermissionRequest", **BASH)
    check("daemon down: approval hook exits 0 with no decision, fast",
          code == 0 and out == "" and dt < 2, f"exit {code}, stdout {out!r}, {dt:.2f}s")


if __name__ == "__main__":
    down() if sys.argv[1:] == ["down"] else up()
