---
name: session-hud
description: Floating always-on-top corner window that lists every active Claude Code session on this machine and lets the user approve or deny permission requests from the window. Use when the user asks to start, stop, check, install, uninstall or troubleshoot the session HUD / session monitor / approval window.
---

# session-hud

One Python process (standard library only, Windows) runs a localhost daemon on
`127.0.0.1:47615` and a small tkinter window. Claude Code hooks post each
session's events to it. All commands use `scripts/ctl.py` in this directory:

```
python ~/.claude/skills/session-hud/scripts/ctl.py start      # launch the window in the background
python ~/.claude/skills/session-hud/scripts/ctl.py stop       # close it
python ~/.claude/skills/session-hud/scripts/ctl.py status     # running? which sessions?
python ~/.claude/skills/session-hud/scripts/ctl.py install    # register the hooks
python ~/.claude/skills/session-hud/scripts/ctl.py uninstall  # remove only our hooks
python ~/.claude/skills/session-hud/scripts/selftest.py       # simulated end-to-end test (HUD running)
python ~/.claude/skills/session-hud/scripts/selftest.py down  # fail-safe test (HUD stopped)
```

The HUD does not start at login. Run `start` when the user wants it.

## Hooks

`install` merges into `~/.claude/settings.json`: it appends one entry running
`scripts/hook.py` to each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `Notification`, `Stop`, `SessionEnd`, and
leaves every other key and hook as it was. If anything changes it first writes
`settings.json.bak-YYYYMMDD-HHMMSS` next to the file. Running it again changes
nothing. `uninstall` removes only entries whose args point into `session-hud`.
Hooks load at session start, so sessions already open need a restart.

## Behaviour

- Row per session: dot (green working, amber needs approval, red done, gray
  idle) and the session's name (its title, else the folder name), then what the session is doing (`Running: npm test`,
  `Editing app.py`, `Thinking`, `Done` once the turn ends; `Idle` only before
  the first prompt), then Claude's latest message in that turn,
  read from the session transcript. Dimmed after 5 minutes idle, removed after 30 or on exit.
- A permission request shows the tool and command with Approve / Deny. The
  click releases the waiting `PermissionRequest` hook at once.
- `✕` on a row removes that session from view. It stays hidden until it ends,
  or comes back by itself if it needs a permission decision.
- `—` collapses the window to a pill; it reopens when an approval arrives.
  Drag the title (or the pill) to move it.

## Fail-safe

- Daemon down, slow or replying with garbage: the hook exits 0 with no output
  in about a second and Claude Code shows its normal prompt. A hook never
  approves by itself.
- No click within 120 seconds: same fallback, row shows "Answer in the terminal".
  While the hook is waiting, the terminal prompt may not appear until then.
- Claude Code's own deny and ask rules still apply after an Approve.
- Requests need the random token in `token` (created by `install`), so other
  local programs cannot approve.

## Troubleshooting

- Log: `~/.claude/skills/session-hud/hud.log` (start/stop and bad requests).
- No window: run `status`; if not running, `start`, then read the log.
- Window but no sessions: hooks missing (`install`) or session not restarted.
- "port in use" in the log: another copy is already running, or another
  program holds port 47615 (change `PORT` in `scripts/common.py`).
- Python moved: run `uninstall` then `install` to rewrite the interpreter path.
