#!/usr/bin/env python
"""
auto_save.py - Claude Code Stop hook that captures memories automatically.

Runs when the agent loop ends (Stop hook).
Input:  stdin JSON { session_id, transcript_path, last_assistant_message, cwd }
        transcript_path is a FILE PATH (JSONL, one event per line), not a message list.
Output: always exit 0 (never blocks the session from ending).

Three tiers:
  Tier 1 - explicit requests ("remember X" / 記住 X)        -> saved straight to memories.jsonl
  Tier 2 - corrections and confirmations from the user      -> pending_review.jsonl for human review
  Tier 3 - key findings in assistant messages (root causes,
           design decisions, hard rules)                    -> pending_review.jsonl

Trigger words cover Traditional Chinese (the author's working language) and English.
Set CLAUDE_MEMORY_PROJECT_RE to a regex with one group (e.g. "(5\\d{3})[_\\-/ ]")
if your project folders follow a numbering scheme; pending items are then tagged
with the project they came from.
"""

import json
import os
import sys
import re
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone

MEMORY_DIR = Path(os.environ.get("CLAUDE_MEMORY_DIR") or Path(__file__).parent)
MEMORY_CLI = Path(__file__).parent / "memory_cli.py"
PENDING_FILE = MEMORY_DIR / "pending_review.jsonl"

# 這個 hook 掛在 Stop 事件上 = 每一輪對話結束都跑一次，而每次都重掃最近 40 條訊息。
# 同一則 assistant 訊息因此會在後續每一輪被重複掃到，若不跨執行去重就會暴增
# （實測 465 筆待審中 56% 是純重複，同一段最多被寫入 17 次）。
MAX_PER_RUN = 5          # 單次執行最多新增幾筆，防單輪爆量
MAX_CORRECTION_LEN = 400  # 超過此長度的 user 訊息多半是貼 prompt／貼資料，不是糾正

# Tier 1：明確要求記住某事
EXPLICIT_RE = re.compile(
    r"(?:記住|記下來?|永遠記得|請記住|你要記住|\b[Rr]emember(?: that)?)[：:，,、]?\s*(.{4,200})",
    re.DOTALL,
)

# Tier 2：糾正訊號
CORRECTION_RE = re.compile(
    r"不對|你搞錯|應該是|不是這樣|錯了|別再|以後不要|下次不要|禁止.{0,10}以後|你又|又忘了|已經說過|"
    r"(?i:that's wrong|you got it wrong|don't do that again|stop doing|I already told you)"
)

# Tier 2：確認訊號（Claude 做了某件非顯而易見的事，用戶肯定）
CONFIRM_RE = re.compile(
    r"對了|就是這樣|完美|很好|這才對|這樣才對|keep.{0,20}doing|繼續這樣|(?i:exactly right|that's perfect)"
)

# Tier 3：assistant 訊息中的重要發現訊號
#
# 收斂原則（原版每天噴 40~125 筆，大多是雜訊）：
#   1. 只留「這是一條可複用的知識」的詞。純陳述結果的詞（修好了／修復完成／已修復）
#      一律砍掉——它們描述的是一次性事件，不是下次用得上的東西。實測「修好了」
#      單一關鍵字就貢獻 88 筆、「陷阱」60 筆，合計佔全部的 32%。
#   2. `.*` 一律改成有界的 `.{0,N}`。原本 `問題.*在於` 會跨整段吃進去，抓回來的
#      片段跟命中的關鍵字毫無關係。
#   3. 拿掉 IGNORECASE：中文詞用不到，只會讓 bug/BUG 這種英文片段誤命中。
DISCOVERY_RE = re.compile(
    r"根本原因|根本問題|真正.{0,4}原因|問題.{0,6}在於|"
    r"關鍵發現|重要發現|"
    r"架構.{0,4}決策|設計.{0,4}決定|必須.{0,12}否則|不能.{0,12}否則|"
    r"永遠不要|一律要|強制規則|絕對禁止"
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# user role 底下不是只有使用者打的字：背景任務通知、系統提醒都掛在同一個 role。
# 這些內容含大量「完成 / 失敗 / 應該」等字，會被誤判成糾正或確認訊號
# （實測 74 筆待審裡有 13 筆是 <task-notification>）。
SYSTEM_BLOCK_RE = re.compile(
    r"<(system-reminder|task-notification)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# 這個標頭代表「整則」都是系統插入的，剝掉標頭沒有用——後面整段都不是使用者打的。
SYSTEM_WHOLE_RE = re.compile(r"\[SYSTEM NOTIFICATION", re.IGNORECASE)


def _get_text(content):
    """從 content（str 或 content block list）提取純文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _user_typed_text(content):
    """只取「使用者真的打字」的部分：剝掉系統插入的區塊。

    未閉合的 <task-notification> 也要處理——通知常被截斷，剝不掉就整則捨棄。
    """
    raw = _get_text(content)
    if SYSTEM_WHOLE_RE.search(raw):
        return ""
    text = SYSTEM_BLOCK_RE.sub(" ", raw)
    if re.search(r"<(system-reminder|task-notification)\b", text, re.I):
        return ""
    return text.strip()


def _save_direct(title, content, category="自動提取"):
    """Tier 1：直接寫入 memories.jsonl。"""
    subprocess.run(
        [
            sys.executable, str(MEMORY_CLI),
            "add",
            "--type", "feedback",
            "--category", category,
            "--title", title[:60],
            "--content", content,
            "--tags", "auto-saved",
        ],
        capture_output=True,
        cwd=str(MEMORY_DIR.parent),
    )


_seen_keys = None
_written_this_run = 0
_project = None  # 本次對話推斷出的專案編號，由 main() 設定


def _dedup_key(text):
    return hashlib.sha1(text[:200].strip().encode("utf-8")).hexdigest()[:16]


def _existing_keys():
    """已寫過的 dedup_key。含已審查項目，避免審掉之後下一輪又被重新抓進來。"""
    keys = set()
    if PENDING_FILE.exists():
        with PENDING_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    k = json.loads(line).get("dedup_key")
                except Exception:
                    continue
                if k:
                    keys.add(k)
    return keys


def _append_pending(entry, key_text):
    """Tier 2/3：寫入 pending_review.jsonl（跨執行去重 + 單次上限）。"""
    global _seen_keys, _written_this_run

    if _written_this_run >= MAX_PER_RUN:
        return
    if _seen_keys is None:
        _seen_keys = _existing_keys()

    key = _dedup_key(key_text)
    if key in _seen_keys:
        return

    _seen_keys.add(key)
    _written_this_run += 1
    entry["dedup_key"] = key
    entry["project"] = _project  # 審查時要知道這條該歸到哪個專案的 category
    with PENDING_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _context_around(msgs, idx, window=1):
    """取 idx 前後 window 條訊息的文字摘要。"""
    start = max(0, idx - window)
    end = min(len(msgs), idx + window + 1)
    parts = []
    for m in msgs[start:end]:
        role = m.get("role", "?")
        text = _get_text(m.get("content", ""))[:300]
        parts.append(f"[{role}] {text}")
    return "\n".join(parts)


# Optional project tagging: a regex with one capture group, e.g. "(5\d{3})[_\-/ ]"
# when project folders are numbered. Unset = no project tagging.
_PROJECT_PATTERN = os.environ.get("CLAUDE_MEMORY_PROJECT_RE")
PROJECT_RE = re.compile(_PROJECT_PATTERN) if _PROJECT_PATTERN else None


def _guess_project(msgs, cwd):
    """判斷這段對話屬於哪個專案。

    Stop hook 的輸入本來就帶 cwd，但舊版從沒讀過它——導致 467 筆待審沒有任何來源
    資訊，事後只能靠內容猜。cwd 落在專案目錄底下時最可靠；但很多 session 是從
    repo 根目錄開的（cwd 沒有編號），這時退而取對話中出現最多次的專案編號。
    """
    if PROJECT_RE is None:
        return None
    m = PROJECT_RE.search((cwd or "").replace("\\", "/") + "/")
    if m:
        return m.group(1)

    counts = {}
    for msg in msgs:
        for num in PROJECT_RE.findall(_get_text(msg.get("content", ""))):
            counts[num] = counts.get(num, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _in_code_block(text, pos):
    """pos 是否落在 ``` 圍住的區塊內（奇數個 fence 在前 = 在區塊內）。

    貼進來的 JSON／程式碼常含關鍵字，抓出來的片段對記憶毫無價值。
    """
    return text.count("```", 0, pos) % 2 == 1


def _scan_assistant_discoveries(recent):
    """Tier 3：掃描 assistant 訊息，找出重要技術發現。

    去重靠 _append_pending 的 dedup_key（會跨執行比對）——這裡不能只用區域變數，
    因為 Stop hook 每一輪都會重跑並重掃同樣的訊息。
    """
    for i, msg in enumerate(recent):
        if msg.get("role") != "assistant":
            continue

        text = _get_text(msg.get("content", ""))
        if not text or len(text) < 30:
            continue

        matches = list(DISCOVERY_RE.finditer(text))
        if not matches:
            continue

        # 取包含關鍵字的段落（前後各 200 字）
        for m in matches:
            if _in_code_block(text, m.start()):
                continue

            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 200)
            snippet = text[start:end].strip()

            # 取觸發這個 assistant 回覆的 user 訊息作為 context
            trigger_user = next(
                (
                    _get_text(recent[j].get("content", ""))[:200]
                    for j in range(i - 1, -1, -1)
                    if recent[j].get("role") == "user"
                ),
                "",
            )

            _append_pending({
                "signal": "assistant_discovery",
                "keyword": m.group(0),
                "snippet": snippet[:600],
                "trigger_user_msg": trigger_user,
                "detected_at": _now(),
                "reviewed": False,
            }, key_text=snippet)


def _load_transcript(event):
    """從 Stop hook 的 transcript_path 讀出 [{role, content}, ...]。

    Claude Code 傳的是 JSONL 檔案路徑（每行一個事件、訊息在 `message` 欄位），
    不是訊息陣列；且該檔非同步寫入、可能還沒收錄本回合最後一則 assistant 訊息，
    故另外把 last_assistant_message 補在尾端。
    """
    msgs = []
    path = event.get("transcript_path")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        m = json.loads(line).get("message")
                    except Exception:
                        continue
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                        msgs.append({"role": m["role"], "content": m.get("content", "")})
        except Exception:
            pass
    last = event.get("last_assistant_message")
    if last:
        msgs.append({"role": "assistant", "content": last})
    return msgs


def main():
    try:
        # Windows 的 sys.stdin 預設走 cp950，含中文的 JSON 會解碼失敗、
        # 被下面的 except 靜默吞掉（這個 hook 曾因此完全沒作用），故以 UTF-8 讀 bytes。
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    transcript = _load_transcript(event)
    if not transcript:
        sys.exit(0)

    # 只看最後 40 條訊息（近期對話）
    recent = transcript[-40:]

    global _project
    _project = _guess_project(recent, event.get("cwd"))

    for i, msg in enumerate(recent):
        if msg.get("role") != "user":
            continue

        text = _user_typed_text(msg.get("content", ""))
        if not text or len(text) < 4:
            continue

        # ── Tier 1：明確記憶請求 ────────────────────────────────────
        m = EXPLICIT_RE.search(text)
        if m:
            extracted = m.group(1).strip()
            _save_direct(
                title=extracted[:50],
                content=extracted,
                category="自動提取",
            )
            continue  # 不再做 Tier 2 檢查

        # 糾正跟確認都是短句。長訊息幾乎都是貼 prompt 模板／貼報錯／貼文章，
        # 裡面碰巧出現「應該是」「不對」就被當成糾正收進來（實測某段 LLM prompt
        # 模板因此被重複收了 17 次）。
        if len(text) > MAX_CORRECTION_LEN:
            continue

        # ── Tier 2：糾正訊號 ────────────────────────────────────────
        if CORRECTION_RE.search(text):
            _append_pending({
                "signal": "correction",
                "user_msg": text[:400],
                "context": _context_around(recent, i),
                "detected_at": _now(),
                "reviewed": False,
            }, key_text=text)
            continue

        # ── Tier 2：確認訊號 ────────────────────────────────────────
        if CONFIRM_RE.search(text):
            prev_assistant = next(
                (
                    _get_text(recent[j].get("content", ""))
                    for j in range(i - 1, -1, -1)
                    if recent[j].get("role") == "assistant"
                ),
                None,
            )
            if prev_assistant:
                _append_pending({
                    "signal": "confirmation",
                    "user_msg": text[:400],
                    "assistant_action": prev_assistant[:400],
                    "context": _context_around(recent, i),
                    "detected_at": _now(),
                    "reviewed": False,
                }, key_text=text)

    # ── Tier 3：掃描 assistant 技術發現 ─────────────────────────────
    _scan_assistant_discoveries(recent)


if __name__ == "__main__":
    main()
