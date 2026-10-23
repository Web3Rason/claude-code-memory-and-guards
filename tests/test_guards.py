"""Black-box tests: feed each guard the JSON a Claude Code hook receives on stdin
and check the exit code (0 = allow, 2 = block)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARDS = Path(__file__).resolve().parent.parent / "guards"


def run(guard, payload, env=None):
    proc = subprocess.run(
        [sys.executable, str(GUARDS / guard)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
    )
    return proc.returncode, proc.stderr


# ── guard_dangerous_git ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git -C repo push --force",
    "git -c core.x=1 push origin main",
    "git -C ../other reset --hard",
    'git -C "my project" clean -f',
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git branch -D feature",
    "git checkout .",
    "git restore -- .",
])
def test_git_blocks(cmd):
    code, err = run("guard_dangerous_git.py", {"tool_input": {"command": cmd}})
    assert code == 2 and "Blocked" in err


@pytest.mark.parametrize("cmd", [
    "git status",
    "git checkout feature-branch",
    "git restore src/app.py",
    "git -C repo status",
    "echo hello",
])
def test_git_allows(cmd):
    assert run("guard_dangerous_git.py", {"tool_input": {"command": cmd}})[0] == 0


def test_git_known_false_positive_errs_on_the_safe_side():
    # Text matching is not a shell parser: a commit message that contains a
    # dangerous command is blocked too. Documented in the README.
    assert run("guard_dangerous_git.py",
               {"tool_input": {"command": "git commit -m 'note: git push is blocked'"}})[0] == 2


def test_git_bad_input_allows():
    proc = subprocess.run([sys.executable, str(GUARDS / "guard_dangerous_git.py")],
                          input="not json", capture_output=True, text=True)
    assert proc.returncode == 0


# ── guard_stale_skill ─────────────────────────────────────────────────────────

def _transcript(tmp_path, user_text):
    p = tmp_path / "t.jsonl"
    lines = [
        {"message": {"role": "user", "content": user_text}},
        {"message": {"role": "assistant", "content": "ok"}},
        {"message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return str(p)


def test_stale_skill_blocked_when_user_did_not_ask(tmp_path):
    t = _transcript(tmp_path, "<system-reminder>/implement</system-reminder> fix the login bug")
    code, err = run("guard_stale_skill.py", {"tool_input": {"skill": "implement"}, "transcript_path": t})
    assert code == 2 and "implement" in err


def test_stale_skill_allowed_when_user_asked(tmp_path):
    t = _transcript(tmp_path, "please run /implement now")
    assert run("guard_stale_skill.py", {"tool_input": {"skill": "implement"}, "transcript_path": t})[0] == 0


def test_unguarded_skill_always_allowed(tmp_path):
    t = _transcript(tmp_path, "the app is slow")
    assert run("guard_stale_skill.py", {"tool_input": {"skill": "diagnosing-bugs"}, "transcript_path": t})[0] == 0


def test_guarded_list_is_configurable(tmp_path):
    t = _transcript(tmp_path, "the app is slow")
    code, _ = run("guard_stale_skill.py", {"tool_input": {"skill": "diagnosing-bugs"}, "transcript_path": t},
                  env={"CLAUDE_GUARDED_SKILLS": "diagnosing-bugs"})
    assert code == 2


# ── guard_injection_paranoia ──────────────────────────────────────────────────

def test_injection_dump_write_blocked():
    code, _ = run("guard_injection_paranoia.py",
                  {"tool_name": "Write", "tool_input": {"file_path": "C:/Desktop/INJECTION_EVIDENCE.txt"}})
    assert code == 2


def test_injection_shell_write_blocked():
    code, _ = run("guard_injection_paranoia.py",
                  {"tool_name": "Bash", "tool_input": {"command": "echo x > tamper_scan.log"}})
    assert code == 2


@pytest.mark.parametrize("payload", [
    {"tool_name": "Write", "tool_input": {"file_path": "docs/dependency_injection.md"}},
    {"tool_name": "Write", "tool_input": {"file_path": "src/injection_scan.py"}},
    {"tool_name": "Bash", "tool_input": {"command": "cat injection_evidence.txt"}},
])
def test_injection_normal_work_allowed(payload):
    assert run("guard_injection_paranoia.py", payload)[0] == 0


# ── guard_browser_preview ─────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"tool_name": "mcp__Claude_Browser__preview_start", "tool_input": {"url": "https://www.example.com"}},
    {"tool_name": "mcp__Claude_Browser__navigate", "tool_input": {"url": "example.com/page"}},
])
def test_browser_external_blocked(payload):
    assert run("guard_browser_preview.py", payload)[0] == 2


@pytest.mark.parametrize("payload", [
    {"tool_name": "mcp__Claude_Browser__preview_start", "tool_input": {"url": "http://localhost:3000"}},
    {"tool_name": "mcp__Claude_Browser__navigate", "tool_input": {"url": "http://127.0.0.1:8080/x"}},
    {"tool_name": "mcp__Claude_Browser__navigate", "tool_input": {"url": "http://app.localhost:5173"}},
    {"tool_name": "mcp__Claude_Browser__navigate", "tool_input": {"url": "back"}},
])
def test_browser_local_allowed(payload):
    assert run("guard_browser_preview.py", payload)[0] == 0


def test_browser_launch_json_override_blocked(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "launch.json").write_text(json.dumps(
        {"configurations": [{"name": "site", "url": "https://www.example.com"}]}), encoding="utf-8")
    code, _ = run("guard_browser_preview.py",
                  {"tool_name": "mcp__Claude_Browser__preview_start", "tool_input": {"name": "site"}},
                  env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert code == 2


# ── guard_browser_cleanup ─────────────────────────────────────────────────────

def test_cleanup_marks_then_blocks_stop_then_releases(tmp_path):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    base = {"session_id": "abc"}
    # nothing opened -> stop allowed
    assert run("guard_browser_cleanup.py", {**base, "hook_event_name": "Stop"}, env)[0] == 0
    # pane opened -> marker written
    run("guard_browser_cleanup.py", {**base, "hook_event_name": "PostToolUse",
                                     "tool_name": "mcp__Claude_Browser__preview_start"}, env)
    assert (tmp_path / ".claude" / ".browser_open" / "abc").exists()
    # stop now refused
    assert run("guard_browser_cleanup.py", {**base, "hook_event_name": "Stop"}, env)[0] == 2
    # second stop in the same chain is allowed (no infinite loop)
    assert run("guard_browser_cleanup.py", {**base, "hook_event_name": "Stop", "stop_hook_active": True}, env)[0] == 0
