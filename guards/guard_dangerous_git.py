#!/usr/bin/env python
"""PreToolUse guard: block high-risk git commands
(push / reset --hard / clean -f / branch -D / checkout . / restore .).

Why it exists: a written rule ("never force-push, never reset --hard") depends on
the agent remembering and obeying it, and that fails during long tasks or after
context compaction. This hook runs in the harness before the tool executes, so it
does not depend on the agent's memory.

Matching is conservative: it only fires when `git` is directly followed by a
dangerous subcommand. Everyday commands such as `git checkout <branch>` or
`git restore <file>` pass; only a target of `.` (the whole working tree) is blocked.
Any parse failure lets the call through (exit 0).
"""
import json
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

cmd = ((data.get("tool_input") or {}).get("command") or "")
if not cmd:
    sys.exit(0)

# (pattern, human label). `git` must be followed by the subcommand, which keeps
# plain text that merely mentions these commands from being blocked.
# Global options before the subcommand, including the ones that take a value
# (`git -C <dir> push`, `git -c key=val push`), which would otherwise slip past.
GIT = r"\bgit\s+(?:-[Cc]\s+\S+\s+|-\S+\s+)*"
PATTERNS = [
    (GIT + r"push\b",                       "git push (publishes to a remote)"),
    (GIT + r"reset\s+.*--hard\b",          "git reset --hard (discards uncommitted work)"),
    (GIT + r"clean\s+-\S*f",               "git clean -f (deletes untracked files)"),
    (GIT + r"branch\s+-D\b",               "git branch -D (force-deletes a branch)"),
    (GIT + r"checkout\s+(--\s+)?\.(\s|$)", "git checkout . (reverts the whole working tree)"),
    (GIT + r"restore\s+(--\s+)?\.(\s|$)",  "git restore . (reverts the whole working tree)"),
]

for pattern, label in PATTERNS:
    if re.search(pattern, cmd, re.I):
        sys.stderr.write(
            f"[guard] Blocked a high-risk git command: {label}\n"
            "These commands can lose uncommitted work or publish without consent.\n"
            "The user has blocked them at the tool layer. If this run really needs it,\n"
            "hand the exact command to the user to run themselves. Do not work around\n"
            "the block (for example by wrapping it in another shell).\n"
            f"Blocked command: {cmd[:200]}\n"
        )
        sys.exit(2)  # PreToolUse exit 2 = block the tool call

sys.exit(0)
