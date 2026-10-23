#!/usr/bin/env python
"""
memory_cli.py - persistent memory for Claude Code (JSONL-based)
Inspired by the GitHub Copilot Memory mechanism.

One JSON object per line (JSONL), append-only, last write wins per ID.
The agent searches and loads only what it needs instead of reading the
whole store into context. CLI output is in Traditional Chinese (the
author's working language); the data format is language-neutral.

Store location: the folder of this file, or $CLAUDE_MEMORY_DIR.

Commands:
  list    [--type TYPE] [--category CAT] [--tag TAG] [-v]
  search  <keywords...> [-n N]
  add     --type TYPE --category CAT --title TITLE --content CONTENT [--tags t1,t2] [--id ID]
  delete  <id> [--reason REASON]
  show    <id>
  export  [--output PATH]
  gc      （壓縮 JSONL，移除已刪除記錄）
  stats
"""

import json
import os
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone

MEMORY_DIR = Path(os.environ.get("CLAUDE_MEMORY_DIR") or Path(__file__).parent)
MEMORY_FILE = MEMORY_DIR / "memories.jsonl"

# Windows 的 stdout 預設走 locale 編碼（zh-TW 是 cp950），輸出的中文記憶內容
# 會以 Big5 位元組送出、被 UTF-8 的呼叫端解成亂碼——search/show/stats 的結果
# 因此完全不可讀（等於整個讀取端失效）。強制以 UTF-8 輸出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── Core I/O ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load():
    """讀取所有記憶。同 ID 以最後一行為準（append-only 語意）。"""
    if not MEMORY_FILE.exists():
        return {}
    records = {}
    with MEMORY_FILE.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                records[record["id"]] = record
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[warn] line {lineno}: {e}", file=sys.stderr)
    return records


def _active(records):
    """返回所有活躍記憶，按 updated_at 降序。"""
    return sorted(
        [r for r in records.values() if r.get("active", True)],
        key=lambda r: str(r.get("updated_at", "")),
        reverse=True,
    )


def _append(record):
    """Append 一條記錄到 JSONL 檔案。"""
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _make_id(type_, title):
    """從 type + title 產生穩定短 slug ID。"""
    import hashlib
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title.lower()).strip("-")[:32]
    h = hashlib.md5(f"{type_}:{title}".encode()).hexdigest()[:4]
    return f"{slug}-{h}"


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(args):
    records = _load()
    items = _active(records)

    if args.type:
        items = [r for r in items if r.get("type") == args.type]
    if args.category:
        items = [r for r in items if args.category in r.get("category", "")]
    if args.tag:
        items = [r for r in items if args.tag in r.get("tags", [])]

    if not items:
        print("（無符合記憶）")
        return

    # 依分類分組
    by_cat = {}
    for r in items:
        by_cat.setdefault(r.get("category", "未分類"), []).append(r)

    for cat in sorted(by_cat):
        print(f"\n【{cat}】")
        for r in by_cat[cat]:
            tag_str = "  " + " ".join(f"#{t}" for t in r.get("tags", [])) if args.verbose else ""
            type_badge = r.get("type", "?")[:3]
            print(f"  {r['id']:<38s} [{type_badge}] {r['title']}{tag_str}")


def cmd_search(args):
    records = _load()
    items = _active(records)
    query = " ".join(args.query).lower()
    keywords = query.split()

    results = []
    for r in items:
        haystack = " ".join([
            r.get("title", ""),
            r.get("content", ""),
            " ".join(r.get("tags", [])),
            r.get("category", ""),
        ]).lower()
        score = sum(1 for kw in keywords if kw in haystack)
        if score > 0:
            results.append((score, r))

    results.sort(key=lambda x: -x[0])

    if not results:
        print("（無符合記憶）")
        return

    for score, r in results[: args.limit]:
        print(f"\n{'─'*60}")
        print(f"ID: {r['id']}  [{r.get('type','?')}] [{r.get('category','?')}]  相關度:{score}")
        print(f"標題: {r['title']}")
        tags = " ".join(f"#{t}" for t in r.get("tags", []))
        if tags:
            print(f"標籤: {tags}")
        lines = r.get("content", "").split("\n")
        preview = "\n".join(lines[:4])
        if len(lines) > 4:
            preview += f"\n... (共 {len(lines)} 行，用 show {r['id']} 看全文)"
        print(f"\n{preview}")


def cmd_show(args):
    records = _load()
    r = records.get(args.id)
    if not r:
        print(f"找不到 ID: {args.id}", file=sys.stderr)
        sys.exit(1)
    if not r.get("active", True):
        print(f"[已刪除] {args.id}", file=sys.stderr)

    print(f"ID       : {r['id']}")
    print(f"類型     : {r.get('type')}  分類: {r.get('category')}")
    print(f"標籤     : {' '.join('#'+t for t in r.get('tags', []))}")
    print(f"建立     : {r.get('created_at')}  更新: {r.get('updated_at')}")
    print(f"來源     : {r.get('source', '?')}")
    print(f"\n{'─'*60}")
    print(r.get("content", ""))


def cmd_add(args):
    records = _load()

    mem_id = args.id if args.id else _make_id(args.type, args.title)

    if mem_id in records and records[mem_id].get("active", True):
        print(f"[更新] {mem_id} 已存在，追加新版本", file=sys.stderr)

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    record = {
        "id": mem_id,
        "type": args.type,
        "category": args.category,
        "title": args.title,
        "content": args.content,
        "tags": tags,
        "created_at": records.get(mem_id, {}).get("created_at", _now()),
        "updated_at": _now(),
        "active": True,
        "source": "manual",
    }

    _append(record)
    print(f"已儲存: {mem_id}")
    print(f"  [{args.type}] {args.title}")


def cmd_delete(args):
    records = _load()
    if args.id not in records:
        print(f"找不到 ID: {args.id}", file=sys.stderr)
        sys.exit(1)

    _append({
        "id": args.id,
        "active": False,
        "updated_at": _now(),
        "_deleted_reason": args.reason or "手動刪除",
    })
    print(f"已刪除: {args.id}")


def cmd_export(args):
    records = _load()
    items = _active(records)

    out = Path(args.output) if args.output else MEMORY_DIR / "MEMORY_EXPORT.md"

    lines = [
        "# Memory Index (JSONL Export)\n",
        f"> 生成時間: {_now()}  共 {len(items)} 條記憶\n",
        "> 此檔案由 `python .memory/memory_cli.py export` 自動生成，請勿手動編輯\n",
    ]

    by_cat = {}
    for r in items:
        by_cat.setdefault(r.get("category", "未分類"), []).append(r)

    for cat in sorted(by_cat):
        lines.append(f"\n## {cat}\n")
        for r in by_cat[cat]:
            first_line = r.get("content", "").split("\n")[0][:100]
            tags = " ".join(f"`{t}`" for t in r.get("tags", []))
            lines.append(f"- **{r['title']}**  `{r['id']}`\n")
            lines.append(f"  {first_line}\n")
            if tags:
                lines.append(f"  標籤: {tags}\n")
            lines.append("\n")

    out.write_text("".join(lines), encoding="utf-8")
    print(f"已匯出 {len(items)} 條記憶 → {out}")


def cmd_review(args):
    """審查 auto_save.py 偵測到的待辦記憶。"""
    pending_file = MEMORY_DIR / "pending_review.jsonl"
    if not pending_file.exists():
        print("（無待審查項目）")
        return

    items = []
    with pending_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    unreviewed = [r for r in items if not r.get("reviewed", False)]
    if not unreviewed:
        print("（無待審查項目）")
        return

    print(f"待審查：{len(unreviewed)} 條自動偵測記憶\n")
    for i, r in enumerate(unreviewed):
        print(f"{'─'*60}")
        signal = r.get("signal", "?")
        proj = r.get("project") or "未知"
        print(f"[{i+1}] 訊號類型: {signal}  專案: {proj}  偵測時間: {r.get('detected_at','?')}")
        if signal == "assistant_discovery":
            # 這類項目沒有 user_msg，內容在 snippet；原本只印 user_msg，
            # 導致審查時整段空白、看不到到底要不要存。
            print(f"命中關鍵字: {r.get('keyword','?')}")
            print(f"當時提問: {(r.get('trigger_user_msg') or '(無)')[:120]}")
            print(f"內容: {r.get('snippet','')[:300]}")
        else:
            print(f"用戶訊息: {r.get('user_msg','')[:200]}")
            if signal == "confirmation" and r.get("assistant_action"):
                print(f"Claude 動作: {r.get('assistant_action','')[:200]}")
        print()

    print("─" * 60)
    print("處理方式：")
    print("  儲存為正式記憶 → python .memory/memory_cli.py add --type feedback ...")
    print("  清空待審清單   → python .memory/memory_cli.py review --clear")

    if args.clear:
        # 將所有項目標記為已審查
        reviewed = [{**r, "reviewed": True} for r in items]
        with pending_file.open("w", encoding="utf-8") as f:
            for r in reviewed:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n已清空 {len(unreviewed)} 條待審項目")


def cmd_gc(args):
    records = _load()
    active_list = [r for r in records.values() if r.get("active", True)]
    deleted_cnt = len(records) - len(active_list)

    backup = MEMORY_FILE.with_suffix(".jsonl.bak")
    MEMORY_FILE.rename(backup)

    with MEMORY_FILE.open("w", encoding="utf-8") as f:
        for r in sorted(active_list, key=lambda x: x.get("created_at", "")):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    size_kb = MEMORY_FILE.stat().st_size / 1024
    print(f"GC 完成：保留 {len(active_list)} 條，移除 {deleted_cnt} 條  ({size_kb:.1f} KB)")
    print(f"備份: {backup}")


def cmd_stats(args):
    records = _load()
    all_r = list(records.values())
    active_list = [r for r in all_r if r.get("active", True)]
    deleted_list = [r for r in all_r if not r.get("active", True)]

    size_str = f"  ({MEMORY_FILE.stat().st_size / 1024:.1f} KB)" if MEMORY_FILE.exists() else ""
    print(f"活躍: {len(active_list)} 條  |  已刪除: {len(deleted_list)} 條{size_str}")

    print("\n按類型:")
    by_type = {}
    for r in active_list:
        t = r.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    for t, cnt in sorted(by_type.items()):
        print(f"  {t:<20s}: {cnt}")

    print("\n按分類 (Top 10):")
    by_cat = {}
    for r in active_list:
        c = r.get("category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1
    for c, cnt in sorted(by_cat.items(), key=lambda x: -x[1])[:10]:
        print(f"  {c:<25s}: {cnt}")


# ── Argparse ──────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Claude Memory CLI (JSONL) — 靈感來自 GitHub Copilot Memory"
    )
    subs = p.add_subparsers(dest="command", required=True)

    # list
    pl = subs.add_parser("list", help="列出記憶")
    pl.add_argument("--type", help="feedback|user|project|reference")
    pl.add_argument("--category", help="分類名稱（部分匹配）")
    pl.add_argument("--tag", help="標籤（完全匹配）")
    pl.add_argument("-v", "--verbose", action="store_true", help="顯示標籤")

    # search
    ps = subs.add_parser("search", help="全文搜尋")
    ps.add_argument("query", nargs="+")
    ps.add_argument("-n", "--limit", type=int, default=10)

    # add
    pa = subs.add_parser("add", help="新增/更新記憶")
    pa.add_argument("--id", help="自訂 ID（選填）")
    pa.add_argument("--type", required=True, choices=["feedback", "user", "project", "reference"])
    pa.add_argument("--category", required=True)
    pa.add_argument("--title", required=True)
    pa.add_argument("--content", required=True)
    pa.add_argument("--tags", help="逗號分隔")

    # delete
    pd = subs.add_parser("delete", help="軟刪除")
    pd.add_argument("id")
    pd.add_argument("--reason")

    # show
    psh = subs.add_parser("show", help="顯示完整記憶")
    psh.add_argument("id")

    # export
    pe = subs.add_parser("export", help="匯出為 Markdown")
    pe.add_argument("--output")

    # gc
    subs.add_parser("gc", help="壓縮 JSONL")

    # stats
    subs.add_parser("stats", help="統計")

    # review
    pr = subs.add_parser("review", help="審查自動偵測的待辦記憶")
    pr.add_argument("--clear", action="store_true", help="清空待審清單")

    args = p.parse_args()
    {
        "list": cmd_list,
        "search": cmd_search,
        "add": cmd_add,
        "delete": cmd_delete,
        "show": cmd_show,
        "export": cmd_export,
        "gc": cmd_gc,
        "stats": cmd_stats,
        "review": cmd_review,
    }[args.command](args)


if __name__ == "__main__":
    main()
