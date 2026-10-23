#!/usr/bin/env python
"""PreToolUse guard: block skill triggers left over from before context compaction.

Why it exists: after compaction, a skill invocation from an earlier turn can survive
inside a <system-reminder> and get re-run as if the user had just asked for it.
A written rule ("compare against the last user message first") fails exactly when
it is needed most, because compaction may have removed the rule itself. This hook
checks in the harness instead.

Only skills in GUARDED are checked: the ones that must never run unless the user
names them in the current turn. Situational skills (debugging, TDD, research...)
are left to the agent. Configure the list with $CLAUDE_GUARDED_SKILLS
(comma-separated). If the transcript cannot be read, the call is allowed:
better to miss a block than to block real work.
"""
import json
import os
import re
import sys
from pathlib import Path

for _s in (sys.stderr, sys.stdout):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

DEFAULT_GUARDED = "implement,to-spec,to-tickets,triage,deploy"
GUARDED = {
    s.strip().lower()
    for s in (os.environ.get("CLAUDE_GUARDED_SKILLS") or DEFAULT_GUARDED).split(",")
    if s.strip()
}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _last_user_text(transcript_path):
    """The last message the user actually typed.

    Tool results also live under the user role and are skipped. <system-reminder>
    blocks are inserted by the system and are exactly where stale triggers survive,
    so they are stripped too; otherwise a stale trigger would approve itself.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line).get("message")
        except Exception:
            continue
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue

        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            continue  # a tool result, not something the user typed

        text = _text_of(content)
        text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
        if text.strip():
            return text
    return None


try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

skill = ((data.get("tool_input") or {}).get("skill") or "").strip()
if not skill:
    sys.exit(0)

bare = skill.split(":")[-1].lower()  # plugin:skill -> skill
if bare not in GUARDED:
    sys.exit(0)

last_user = _last_user_text(data.get("transcript_path") or "")
if last_user is None:
    sys.exit(0)  # unreadable transcript: allow

# Loose name match: "-", "_" or space are interchangeable, leading slash optional.
pattern = re.escape(bare).replace(r"\-", "[-_ ]?")
if re.search(rf"/?{pattern}", last_user, re.I):
    sys.exit(0)

sys.stderr.write(
    f"[guard] Blocked skill '{skill}': the last user message does not mention it.\n"
    "Skills on this list only run when the user asks for them in the current turn.\n"
    "The usual cause is a trigger from before context compaction surviving in a\n"
    "<system-reminder>. Go back to what the last user message actually asks for.\n"
    "If you believe the user does want this skill, ask first instead of retrying.\n"
    f"Last user message: {last_user.strip()[:150]}\n"
)
sys.exit(2)  # PreToolUse exit 2 = block the tool call
