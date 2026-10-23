"""Tests for the memory CLI and the auto_save Stop hook, run against a temp store."""
import json
import os
import subprocess
import sys
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "memory"


def cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(MEM / "memory_cli.py"), *args],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "CLAUDE_MEMORY_DIR": str(tmp_path)},
    )


def records(tmp_path):
    f = tmp_path / "memories.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_add_search_delete_roundtrip(tmp_path):
    r = cli(tmp_path, "add", "--type", "feedback", "--category", "git",
            "--title", "Never force-push", "--content", "Use a new branch instead.")
    assert r.returncode == 0
    mem_id = records(tmp_path)[0]["id"]

    out = cli(tmp_path, "search", "force-push").stdout
    assert "Never force-push" in out

    assert cli(tmp_path, "delete", mem_id).returncode == 0
    assert "Never force-push" not in cli(tmp_path, "search", "force-push").stdout


def test_append_only_last_write_wins(tmp_path):
    for content in ("v1", "v2"):
        cli(tmp_path, "add", "--type", "project", "--category", "c",
            "--title", "same title", "--content", content)
    assert len(records(tmp_path)) == 2          # both lines kept on disk
    assert "v2" in cli(tmp_path, "search", "same").stdout


def test_gc_compacts_deleted(tmp_path):
    cli(tmp_path, "add", "--type", "user", "--category", "c", "--title", "a", "--content", "x")
    mem_id = records(tmp_path)[0]["id"]
    cli(tmp_path, "delete", mem_id)
    assert cli(tmp_path, "gc").returncode == 0
    assert records(tmp_path) == []


def _stop_hook(tmp_path, messages):
    t = tmp_path / "transcript.jsonl"
    t.write_text("\n".join(json.dumps({"message": m}) for m in messages), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(MEM / "auto_save.py")],
        input=json.dumps({"transcript_path": str(t), "cwd": str(tmp_path)}).encode("utf-8"),
        capture_output=True,
        env={**os.environ, "CLAUDE_MEMORY_DIR": str(tmp_path)},
    )


def test_auto_save_explicit_remember_is_saved(tmp_path):
    r = _stop_hook(tmp_path, [{"role": "user", "content": "remember: deploy only after tests pass"}])
    assert r.returncode == 0
    assert any("deploy only after tests pass" in x["content"] for x in records(tmp_path))


def test_auto_save_correction_goes_to_review_and_dedups(tmp_path):
    msgs = [{"role": "assistant", "content": "I pushed the fix."},
            {"role": "user", "content": "That's wrong, I already told you to ask first."}]
    _stop_hook(tmp_path, msgs)
    _stop_hook(tmp_path, msgs)  # the Stop hook re-scans the same messages every turn
    pending = (tmp_path / "pending_review.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(pending) == 1
    assert json.loads(pending[0])["signal"] == "correction"


def test_auto_save_ignores_system_inserted_text(tmp_path):
    _stop_hook(tmp_path, [{"role": "user",
                           "content": "<task-notification>build failed, that's wrong</task-notification>"}])
    assert not (tmp_path / "pending_review.jsonl").exists()
