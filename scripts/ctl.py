"""session-hud control: python ctl.py install|uninstall|start|stop|status"""
import json
import os
import shutil
import subprocess
import sys
import time

import common

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(SCRIPTS, "hook.py")
PY_DIR = os.path.dirname(sys.executable)
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
          "PostToolUse", "Notification", "Stop", "SessionEnd"]


def ours(entry):
    return any("session-hud" in str(a) for h in entry.get("hooks", []) for a in h.get("args", []))


def entry(event):
    h = {"type": "command", "command": os.path.join(PY_DIR, "python.exe"), "args": [HOOK]}
    if event == "PermissionRequest":
        h["timeout"] = 150  # hook.py waits at most 130s
    else:
        h["timeout"] = 5
        if event != "SessionEnd":
            h["async"] = True  # status only: never delay a tool call
    return {"hooks": [h]}


def save(settings, original):
    new = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if new == original:
        print("settings.json already up to date, nothing changed")
        return
    backup = SETTINGS + time.strftime(".bak-%Y%m%d-%H%M%S")
    shutil.copy2(SETTINGS, backup)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        f.write(new)
    print("backup:", backup)
    print("updated:", SETTINGS)


def load():
    with open(SETTINGS, encoding="utf-8") as f:
        original = f.read()
    return json.loads(original), original


def install():
    common.token()
    settings, original = load()
    hooks = settings.setdefault("hooks", {})
    for ev in EVENTS:
        entries = hooks.setdefault(ev, [])
        if not any(ours(e) for e in entries):
            entries.append(entry(ev))
    save(settings, original)


def uninstall():
    settings, original = load()
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        hooks[ev] = [e for e in hooks[ev] if not ours(e)]
        if not hooks[ev]:
            del hooks[ev]
    if "hooks" in settings and not hooks:
        del settings["hooks"]
    save(settings, original)


def status():
    try:
        rows = common.call("/state")["sessions"]
    except Exception:
        print("HUD is not running")
        return False
    print(f"HUD is running on 127.0.0.1:{common.PORT}, {len(rows)} session(s)")
    for r in rows:
        print(f'  {r["title"]} — {r["action"] or r["status"]}')
    return True


def start():
    if status():
        return
    exe = os.path.join(PY_DIR, "pythonw.exe")
    subprocess.Popen([exe if os.path.exists(exe) else sys.executable, os.path.join(SCRIPTS, "hud.py")],
                     creationflags=0x00000008 | 0x00000200,  # detached, own process group
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(25):
        time.sleep(0.2)
        try:
            common.call("/state")
            break
        except Exception:
            pass
    if not status():
        sys.exit(f"HUD failed to start, see {common.LOG_FILE}")


def stop():
    try:
        common.call("/quit", {})
        print("HUD stopped")
    except Exception:
        print("HUD is not running")


if __name__ == "__main__":
    cmds = {"install": install, "uninstall": uninstall, "start": start, "stop": stop, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
