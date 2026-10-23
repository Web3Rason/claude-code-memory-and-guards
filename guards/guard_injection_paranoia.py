#!/usr/bin/env python
"""PreToolUse guard: stop the "injection paranoia" loop at its signature move.

Why it exists: after context compaction an agent can misremember what a file
contained, invent its own "anchors" (hashes, line counts), and conclude that tool
output is being tampered with. From there it spirals: writing "evidence" dumps such
as INJECTION_EVIDENCE.txt and asking the user to verify things out of band.
A written rule does not survive compaction, so this hook blocks the behaviour.

Matching is conservative. It only blocks WRITING a plain-text dump file
(.txt/.md/.log/.json) whose name contains both an injection-type word and an
evidence-type word, e.g. injection_evidence.txt or tamper_scan.log.
  - Reads (cat/grep) always pass; only the Write tool or a shell write is checked.
  - Code files (.py etc.) and names with only one of the two word types
    (e.g. dependency_injection.md) pass.
Any parse failure lets the call through.
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

tool = data.get("tool_name") or ""
ti = data.get("tool_input") or {}

INJECT = re.compile(r"(injection|inject|注入|tamper|竄改|篡改|forged|偽造)", re.I)
EVIDENCE = re.compile(r"(evidence|scan|proof|dump|證據|掃描|證明)", re.I)
DUMPEXT = re.compile(r"\.(txt|md|log|json)\b", re.I)
# Shell write operations (so plain reads like cat/grep are never blocked)
WRITE_OP = re.compile(
    r"(>>?|\bOut-File\b|\bSet-Content\b|\bAdd-Content\b|\bNew-Item\b|\btee\b|\bExport-Csv\b)",
    re.I,
)


def _is_injection_dump(name):
    return bool(INJECT.search(name) and EVIDENCE.search(name) and DUMPEXT.search(name))


hit = ""
if tool == "Write":
    fp = ti.get("file_path") or ""
    if _is_injection_dump(fp):
        hit = fp
elif tool in ("Bash", "PowerShell"):
    cmd = ti.get("command") or ""
    if WRITE_OP.search(cmd) and _is_injection_dump(cmd):
        hit = cmd

if hit:
    sys.stderr.write(
        "[guard] Blocked: writing an injection/tamper 'evidence' dump.\n"
        "If you suspect tool output has been tampered with, stop, show the user what\n"
        "you observed and ask whether to continue. Do not write evidence files and do\n"
        "not ask the user to verify things out of band. First:\n"
        "  1) Re-read the whole target file with a tool, not from memory. Compaction\n"
        "     distorting your memory is the usual explanation.\n"
        "  2) You cannot compute hashes in your head. Any SHA or line count you\n"
        "     'remember' is not evidence.\n"
        "  3) Local tampering needs a PostToolUse hook. Check .claude/settings*.json;\n"
        "     if there is none, local tampering is not possible.\n"
        f"Blocked target: {hit[:200]}\n"
    )
    sys.exit(2)  # PreToolUse exit 2 = block the tool call

sys.exit(0)
