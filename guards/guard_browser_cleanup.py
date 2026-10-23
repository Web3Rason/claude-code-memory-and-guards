#!/usr/bin/env python
"""Cleanup guard for the desktop app's Browser preview pane: record every opening,
and refuse to end the turn while the pane may still be open.

Pairs with guard_browser_preview.py. That one blocks risky pages from entering the
pane; this one makes sure whatever was opened gets closed.

Why two layers: what actually crashes the GPU process is "tab open, pane hidden,
screenshot times out". External sites only raise the frequency, so even a
localhost dev server left open in the background sits on the same failure path.

Mechanism:
  PostToolUse  pane opened -> write a per-session marker file
  Stop         marker still present -> exit 2 to refuse the stop and tell the
               agent how to clean up

Hooks are shell processes and cannot call MCP tools themselves, so the only lever
is exit 2, which hands the cleanup back to the agent.

Honest limits:
  - If the app crashes, the Stop hook never runs and tabs are restored on
    relaunch, so guard_browser_preview.py remains the first line of defence.
  - A tab can still stay open for a whole long turn. This shrinks exposure from
    "forever" to "one turn"; it does not make it zero.
  - tabs_close cannot close the very last tab; one about:blank tab is acceptable.
"""
import json
import os
import re
import sys

for _s in (sys.stderr, sys.stdout):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
sid = str(data.get("session_id") or "unknown")
sid = re.sub(r"[^A-Za-z0-9_.-]", "", sid)[:64] or "unknown"
mark_dir = os.path.join(root, ".claude", ".browser_open")
mark = os.path.join(mark_dir, sid)

event = data.get("hook_event_name") or ""
tool = data.get("tool_name") or ""

if event == "PostToolUse":
    if tool.endswith(("preview_start", "navigate", "tabs_create")):
        try:
            os.makedirs(mark_dir, exist_ok=True)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(tool)
        except Exception:
            pass
    sys.exit(0)

if event == "Stop":
    # Already blocked once by this hook and still not clean: allow, to avoid a loop
    if data.get("stop_hook_active"):
        sys.exit(0)
    if not os.path.exists(mark):
        sys.exit(0)
    sys.stderr.write(
        "[guard] The Browser preview pane was opened this turn and has not been confirmed closed.\n\n"
        "A tab left in the background can make the preview screenshot time out and\n"
        "crash the desktop app's GPU process.\n\n"
        "Please:\n"
        "  1. tabs_context to list the open tabs\n"
        "  2. tabs_close to close them (the last one cannot be closed; that is fine)\n"
        "  3. preview_list, then preview_stop to stop any dev server\n"
        f"  4. Delete this marker file when clean: {mark}\n"
        "     (Bash: rm -f \"" + mark.replace("\\", "/") + "\")\n\n"
        "If the pane turns out to be empty already, still delete the marker before stopping.\n"
    )
    sys.exit(2)

sys.exit(0)
