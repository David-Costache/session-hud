"""Session HUD: localhost daemon + always-on-top tkinter window in one process."""
import ctypes
import ctypes.wintypes
import hmac
import json
import os
import select
import socket
import sys
import threading
import time
import tkinter as tk
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common

WAIT = 120   # seconds an approval waits for a click before falling back to the normal prompt
DIM = 300    # idle seconds before a session is dimmed
DROP = 1800  # idle seconds before a session is removed
WAITING = "Waiting for Approval"

lock = threading.Lock()
sessions = {}  # session_id -> {cwd, project, status, action, last}
pending = {}   # approval id -> {sid, tool, preview, ev, decision}
hidden = set()  # session ids the user removed from view
dirty = threading.Event()
quit_flag = threading.Event()


def preview(inp):
    if not isinstance(inp, dict):
        return str(inp or "")
    for k in ("command", "file_path", "url", "pattern", "query", "prompt"):
        if inp.get(k):
            return str(inp[k])
    return json.dumps(inp)[:400]


def describe(tool, inp):
    """One plain line for what a tool call is doing."""
    inp = inp if isinstance(inp, dict) else {}
    name = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if tool in ("Bash", "PowerShell"):
        return f"Running: {inp.get('command', '')}"
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {name}"
    if tool == "Write":
        return f"Writing {name}"
    if tool == "Read":
        return f"Reading {name}"
    if tool in ("Grep", "Glob"):
        return f"Searching for {inp.get('pattern', '')}"
    if tool == "WebFetch":
        return f"Fetching {inp.get('url', '')}"
    if tool == "WebSearch":
        return f"Searching the web: {inp.get('query', '')}"
    if tool in ("Agent", "Task"):
        return f"Delegating: {inp.get('description', '')}"
    if tool == "Skill":
        return f"Using skill {inp.get('skill', '')}"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"Calling {parts[1]}: {parts[-1]}"
    return f"Running: {tool} {preview(inp)}".strip()


def last_note(path):
    """Claude's latest visible message in this turn, read from the session transcript.

    Best effort: the transcript format is not a documented contract, so any
    surprise returns "" and the row just shows the tool line.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue  # the first line of the tail is usually cut in half
            if d.get("isSidechain") or d.get("type") not in ("assistant", "user"):
                continue
            content = d.get("message", {}).get("content")
            blocks = content if isinstance(content, list) else []
            if d["type"] == "user":
                if not any(b.get("type") == "tool_result" for b in blocks):
                    return ""  # reached the prompt that started this turn
                continue
            for b in reversed(blocks):
                if b.get("type") == "text" and b.get("text", "").strip():
                    return b["text"].strip()
    except Exception:
        pass
    return ""


def decide(pid, decision):
    """The single path for the Approve/Deny buttons and POST /decide."""
    with lock:
        p = pending.get(pid)
        if not p or p["ev"].is_set() or decision not in ("allow", "deny"):
            return False
        p["decision"] = decision
        p["ev"].set()
    return True


def dismiss(sid):
    """The single path for the row's remove button and POST /dismiss.

    The session stays hidden until it ends, or until it asks for permission:
    a hidden session must not sit blocked where nobody can see it.
    """
    with lock:
        hidden.add(sid)
        dirty.set()


def cancel(sid):  # caller holds lock
    for p in pending.values():
        if p["sid"] == sid:
            p["ev"].set()


def snapshot():  # caller holds lock
    now = time.time()
    rows = []
    for sid, s in sessions.items():
        if sid in hidden:
            continue
        pend = [{"id": k, "tool": p["tool"], "preview": p["preview"]}
                for k, p in pending.items() if p["sid"] == sid and not p["ev"].is_set()]
        rows.append({"id": sid, "cwd": s["cwd"], "project": s["project"],
                     "status": WAITING if pend else s["status"], "action": s["action"],
                     "note": s.get("note", ""),
                     "idle_s": int(now - s["last"]), "pending": pend})
    return rows


def handle_event(d, sock):
    ev, sid = d["hook_event_name"], d["session_id"]
    tool, pv = d.get("tool_name", ""), preview(d.get("tool_input"))
    doing = describe(tool, d.get("tool_input"))
    note = last_note(d.get("transcript_path")) if ev in ("PreToolUse", "PostToolUse") else ""
    with lock:
        if ev == "SessionEnd":
            sessions.pop(sid, None)
            hidden.discard(sid)
            cancel(sid)
            dirty.set()
            return {}
        s = sessions.setdefault(sid, {"cwd": "", "status": "Idle", "action": ""})
        s["cwd"] = d.get("cwd") or s["cwd"]
        s["project"] = os.path.basename(s["cwd"].rstrip("/\\")) or "?"
        s["last"] = time.time()
        if ev == "PermissionRequest":
            hidden.discard(sid)
            pid = uuid.uuid4().hex
            p = pending[pid] = {"sid": sid, "tool": tool, "preview": pv,
                                "ev": threading.Event(), "decision": None}
        elif ev == "PreToolUse":
            s["status"], s["action"], s["note"] = "Running Tool", doing, note
        elif ev == "PostToolUse":
            s["status"], s["action"], s["note"] = "Thinking", "", note
        elif ev == "UserPromptSubmit":
            s["status"], s["action"], s["note"] = "Thinking", "", ""
        elif ev == "Notification":
            msg = str(d.get("message", ""))
            if "permission" in msg.lower():
                s["status"], s["action"] = WAITING, "Answer in the terminal"
            else:
                s["status"], s["action"] = "Idle", msg
        elif ev in ("Stop", "SessionStart"):
            s["status"], s["action"] = "Idle", ""
        if ev in ("Stop", "UserPromptSubmit"):
            cancel(sid)  # the turn moved on, so any approval still shown is stale
        dirty.set()
    if ev != "PermissionRequest":
        return {}

    end = time.time() + WAIT
    while time.time() < end and not p["ev"].wait(0.5):
        # the hook sends nothing more, so a readable socket means it went away
        if select.select([sock], [], [], 0)[0]:
            try:
                if not sock.recv(1, socket.MSG_PEEK):
                    break
            except OSError:
                break
    with lock:
        p["ev"].set()
        del pending[pid]
        decision = p["decision"]
        if sid in sessions:
            s = sessions[sid]
            s["last"] = time.time()
            if decision == "allow":
                s["status"], s["action"] = "Running Tool", doing
            elif decision == "deny":
                s["status"], s["action"] = "Thinking", ""
            else:
                s["status"], s["action"] = WAITING, "Answer in the terminal"
        dirty.set()
    return {"decision": decision}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def authed(self):
        if hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return True
        self.reply(403, {"error": "bad token"})
        return False

    def do_GET(self):
        if not self.authed():
            return
        if self.path == "/state":
            with lock:
                self.reply(200, {"sessions": snapshot()})
        else:
            self.reply(404, {})

    def do_POST(self):
        if not self.authed():
            return
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/event":
                self.reply(200, handle_event(d, self.connection))
            elif self.path == "/decide":
                self.reply(200, {"ok": decide(d["id"], d["decision"])})
            elif self.path == "/dismiss":
                dismiss(d["id"])
                self.reply(200, {"ok": True})
            elif self.path == "/quit":
                self.reply(200, {"ok": True})
                quit_flag.set()
            else:
                self.reply(404, {})
        except Exception as e:
            print("bad request:", repr(e))
            self.reply(400, {"error": "bad request"})


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second instance bind
    daemon_threads = True


BG, CARD, CODE, LINE, BORDER = "#17171b", "#222229", "#131316", "#26262d", "#34343d"
FG, MUTED, FAINT = "#ececf1", "#9a9aa6", "#5f5f6b"
COLORS = {"wait": "#fbbf24", "work": "#4ade80", "idle": "#6b7280"}
UI, BOLD, MONO = "Segoe UI", "Segoe UI Semibold", "Consolas"


def kind(status):
    return "wait" if status == WAITING else "work" if status in ("Thinking", "Running Tool") else "idle"


def clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def hover(w, **over):
    normal = {k: w.cget(k) for k in over}
    w.bind("<Enter>", lambda e: w.config(**over))
    w.bind("<Leave>", lambda e: w.config(**normal))


class Hud:
    def __init__(self):
        try:  # without this Windows bitmap-stretches the window on scaled displays (blurry)
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        r = self.root = tk.Tk()
        self.s = r.winfo_fpixels("1i") / 96  # pixel sizes below are written for 100% scaling
        r.overrideredirect(True)
        r.attributes("-alpha", 0.96)
        r.configure(bg=BORDER)
        self.body = tk.Frame(r, bg=BG)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.collapsed = False
        self.last = 0
        r.update_idletasks()
        try:  # Windows 11 rounded corners
            hwnd = ctypes.wintypes.HWND(ctypes.windll.user32.GetParent(r.winfo_id()))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass
        rect = ctypes.wintypes.RECT()  # work area = screen minus taskbar
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):
            self.right, self.bottom = rect.right - self.px(14), rect.bottom - self.px(14)
        else:
            self.right, self.bottom = r.winfo_screenwidth() - 14, r.winfo_screenheight() - 60
        self.tick()

    def px(self, n):
        return round(n * self.s)

    def tick(self):
        if quit_flag.is_set():
            self.root.destroy()
            return
        if dirty.is_set() or time.time() - self.last > 5:
            dirty.clear()
            self.render()
        self.root.after(200, self.tick)

    def draggable(self, w, on_click=None):
        def press(e):
            self.drag = (e.x_root, e.y_root, self.right, self.bottom, False)

        def move(e):
            x, y, r, b, _ = self.drag
            self.right, self.bottom = r + e.x_root - x, b + e.y_root - y
            self.drag = (x, y, r, b, True)
            self.place()

        def release(e):
            if on_click and not self.drag[4]:
                on_click()

        w.bind("<Button-1>", press)
        w.bind("<B1-Motion>", move)
        w.bind("<ButtonRelease-1>", release)

    def toggle(self):
        self.collapsed = not self.collapsed
        self.render()

    def place(self):
        self.root.update_idletasks()
        w = self.body.winfo_reqwidth() + 2 if self.collapsed else self.px(320)
        h = self.body.winfo_reqheight() + 2
        self.root.geometry(f"{w}x{h}+{self.right - w}+{self.bottom - h}")

    def icon_button(self, parent, text, command, over_fg, bg=BG):
        b = tk.Button(parent, text=text, command=command, fg=FAINT, bg=bg, bd=0, relief="flat",
                      activebackground=bg, activeforeground=over_fg, font=(UI, 9),
                      padx=self.px(5), pady=0, cursor="hand2", takefocus=0)
        hover(b, fg=over_fg)
        return b

    def render(self):
        px = self.px
        self.last = now = time.time()
        with lock:
            for sid in [k for k, s in sessions.items() if now - s["last"] > DROP]:
                if not any(p["sid"] == sid for p in pending.values()):
                    del sessions[sid]
                    hidden.discard(sid)
            rows = snapshot()
        kinds = [kind(r["status"]) for r in rows]
        overall = next((k for k in ("wait", "work") if k in kinds), "idle")
        if any(r["pending"] for r in rows):
            self.collapsed = False  # pop open whenever an approval is waiting
        for w in self.body.winfo_children():
            w.destroy()

        if self.collapsed:
            pill = tk.Frame(self.body, bg=BG, cursor="hand2")
            pill.pack(padx=px(12), pady=px(6))
            parts = [tk.Label(pill, text="●", fg=COLORS[overall], bg=BG, font=(UI, 9)),
                     tk.Label(pill, text=f" {len(rows)}", fg=FG, bg=BG, font=(BOLD, 10))]
            for w in [pill] + parts:
                self.draggable(w, self.toggle)
            for w in parts:
                w.pack(side="left")
        else:
            head = tk.Frame(self.body, bg=BG)
            head.pack(fill="x", padx=(px(14), px(8)), pady=(px(10), px(6)))
            title = tk.Label(head, text="CLAUDE SESSIONS", fg=MUTED, bg=BG, font=(BOLD, 8))
            title.pack(side="left")
            count = tk.Label(head, text=str(len(rows)), fg=FG, bg=CARD, font=(BOLD, 8), padx=px(6))
            count.pack(side="left", padx=px(8))
            for w in (head, title, count):
                self.draggable(w)
            self.icon_button(head, "—", self.toggle, FG).pack(side="right")
            if not rows:
                tk.Label(self.body, text="No active sessions", fg=FAINT, bg=BG,
                         font=(UI, 9)).pack(pady=(px(6), px(14)))
            for i, r in enumerate(rows):
                tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", padx=px(14))
                dim = r["idle_s"] > DIM and not r["pending"]
                row = tk.Frame(self.body, bg=BG)
                row.pack(fill="x", padx=(px(14), px(8)), pady=(px(8), px(8)))
                tk.Label(row, text="●", fg=FAINT if dim else COLORS[kind(r["status"])], bg=BG,
                         font=(UI, 13)).pack(side="left", anchor="n", pady=(0, 0))
                if not r["pending"]:
                    self.icon_button(row, "✕", lambda i=r["id"]: dismiss(i),
                                     "#ff7b72").pack(side="right", anchor="n")
                text = tk.Frame(row, bg=BG)
                text.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
                tk.Label(text, text=clip(r["project"], 34), fg=MUTED if dim else FG, bg=BG,
                         font=(BOLD, 10), anchor="w").pack(fill="x")
                tk.Label(text, text=clip(r["action"] or r["status"], 44),
                         fg=FAINT if dim else COLORS["wait"] if r["pending"] else MUTED, bg=BG,
                         font=(UI, 9), anchor="w").pack(fill="x")
                if r["note"] and kind(r["status"]) == "work" and not dim:
                    tk.Label(text, text="“" + clip(r["note"], 120) + "”", fg="#80808c", bg=BG,
                             font=(UI, 8, "italic"), wraplength=px(240), justify="left",
                             anchor="w").pack(fill="x", pady=(px(2), 0))
                for p in r["pending"]:
                    card = tk.Frame(self.body, bg=CARD)
                    card.pack(fill="x", padx=px(14), pady=(0, px(10)))
                    tk.Frame(card, bg=COLORS["wait"], width=px(3)).pack(side="left", fill="y")
                    inner = tk.Frame(card, bg=CARD)
                    inner.pack(side="left", fill="both", expand=True, padx=px(10), pady=px(9))
                    tk.Label(inner, text=f'PERMISSION  ·  {p["tool"]}'.upper(), fg=COLORS["wait"],
                             bg=CARD, font=(BOLD, 7), anchor="w").pack(fill="x")
                    tk.Label(inner, text=clip(p["preview"], 190), fg=FG, bg=CODE, font=(MONO, 9),
                             wraplength=px(250), justify="left", anchor="w", padx=px(8),
                             pady=px(6)).pack(fill="x", pady=(px(6), px(8)))
                    btns = tk.Frame(inner, bg=CARD)
                    btns.pack(fill="x")
                    btns.columnconfigure((0, 1), weight=1, uniform="b")
                    for col, (label, dec, bg, fg, obg) in enumerate((
                            ("Approve", "allow", "#238636", "#ffffff", "#2ea043"),
                            ("Deny", "deny", "#30303a", "#ff7b72", "#da3633"))):
                        b = tk.Button(btns, text=label, command=lambda i=p["id"], d=dec: decide(i, d),
                                      bg=bg, fg=fg, activebackground=obg, activeforeground="#ffffff",
                                      bd=0, relief="flat", font=(BOLD, 9), pady=px(4), cursor="hand2")
                        b.grid(row=0, column=col, sticky="ew", padx=(0, px(6)) if col == 0 else 0)
                        hover(b, bg=obg, fg="#ffffff")
            tk.Frame(self.body, bg=BG, height=px(2)).pack()
        self.place()
        self.root.attributes("-topmost", True)


def main():
    global TOKEN
    sys.stdout = sys.stderr = open(common.LOG_FILE, "a", buffering=1, encoding="utf-8")
    TOKEN = common.token()
    try:
        srv = Server(("127.0.0.1", common.PORT), Handler)
    except OSError:
        print(time.strftime("%F %T"), "port in use: already running, exiting")
        return
    print(time.strftime("%F %T"), "started, pid", os.getpid())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        Hud().root.mainloop()
    finally:
        with lock:  # release every waiting hook so sessions fall back to the normal prompt
            for p in pending.values():
                p["ev"].set()
        time.sleep(0.3)
        print(time.strftime("%F %T"), "stopped")


if __name__ == "__main__":
    main()
