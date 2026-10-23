# claude-code-memory-and-guards

Two small pieces of tooling I built for my own daily work with [Claude Code](https://docs.claude.com/en/docs/claude-code):

1. **A persistent memory system** so the agent stops forgetting what it learned in earlier sessions.
2. **Guard hooks** that block risky agent actions at the tool layer, so safety rules hold even when the agent has forgotten them.

I use both every day on a monorepo of 50+ projects, several of which trade real money. The memory store there holds 2,300+ entries. This repository contains the code only; my own memories are not included.

## Why

Written rules in `CLAUDE.md` ("never force-push", "ask before running this skill") work most of the time. They fail in two predictable situations:

- **A new session.** Everything the agent was corrected on yesterday is gone.
- **Context compaction.** In a long session, earlier instructions get summarized away, sometimes including the rule itself.

The fixes follow from that. Corrections should be captured to a store that outlives the session. Rules that must never break should be enforced by the harness, before the tool runs, rather than depend on the model remembering them.

## 1. Memory system (`memory/`)

| File | What it does |
|---|---|
| `memory_cli.py` | CLI over an append-only JSONL store: `add`, `search`, `show`, `list`, `delete` (soft), `review`, `export`, `gc`, `stats`. Last write wins per ID, so updates never rewrite history. |
| `auto_save.py` | A **Stop hook**. When a turn ends it reads the transcript and sorts what it finds into three tiers. |

The three tiers:

- **Tier 1, explicit requests** ("remember: ..."): saved straight to the store.
- **Tier 2, corrections and confirmations** from the user: written to `pending_review.jsonl` for a human to approve.
- **Tier 3, key findings** in the agent's own messages (root causes, design decisions, hard rules): also sent to review.

Lessons from running it for months are built in:

- **Cross-run dedup.** A Stop hook fires every turn and re-scans the same recent messages. Without dedup, 56% of the review queue was duplicates, and one paragraph was queued 17 times.
- **Only what the user actually typed.** Task notifications and system reminders also arrive under the `user` role. They are stripped, because their text tripped the correction detector.
- **Length cap for corrections.** Long messages are pasted prompts or logs, not corrections.
- **A per-run cap**, so one noisy turn cannot flood the queue.
- **UTF-8 stdin on Windows.** The default code page silently broke the hook for non-ASCII transcripts.

Typical session start, in `CLAUDE.md`:

```
Before starting: python memory/memory_cli.py review, then python memory/memory_cli.py search <task keywords>.
```

Trigger words cover Traditional Chinese (my working language) and English. The CLI's output text is in Traditional Chinese.

## 2. Guard hooks (`guards/`)

Every guard reads the hook JSON on stdin and exits `2` to block, `0` to allow. Each one is deliberately conservative. **Anything it cannot parse is allowed**, so a broken guard never stops normal work.

| Guard | Hook | Blocks |
|---|---|---|
| `guard_dangerous_git.py` | PreToolUse (Bash/PowerShell) | `git push`, `reset --hard`, `clean -f`, `branch -D`, `checkout .`, `restore .`, including forms with global options such as `git -C dir push` |
| `guard_stale_skill.py` | PreToolUse (Skill) | Skills that must be requested explicitly but were triggered by a leftover from before compaction. It checks the last message the user actually typed. Configure the list with `CLAUDE_GUARDED_SKILLS`. |
| `guard_injection_paranoia.py` | PreToolUse (Write/Bash/PowerShell) | The signature move of a failure mode where the agent, after compaction, decides its tool output is being "tampered with" and starts writing evidence dumps |
| `guard_browser_preview.py` | PreToolUse (Claude desktop Browser pane) | Loading external sites into the desktop app's preview pane. A tab left open there crashed the app three times in one day. |
| `guard_browser_cleanup.py` | PostToolUse + Stop | Ending a turn while the preview pane may still be open. It records each opening and refuses the stop until the agent has cleaned up. |

Each guard's docstring explains the incident behind it and the design trade-offs.

**Known limitation:** matching is done on text, not a full shell parse. A command that merely *contains* a dangerous one, such as a commit message mentioning `git push`, is blocked too. That errs on the safe side, and a test documents it.

## Install

1. Copy `memory/` and `guards/` into your project (for example under `tools/`).
2. Merge `examples/settings.json` into your project's `.claude/settings.json`, adjusting the paths.
3. Add the session-start lines above to your `CLAUDE.md`.

Optional environment variables:

| Variable | Effect |
|---|---|
| `CLAUDE_MEMORY_DIR` | Where `memories.jsonl` and `pending_review.jsonl` live (default: next to the scripts) |
| `CLAUDE_MEMORY_PROJECT_RE` | A regex with one group, used to tag pending memories with a project ID, e.g. for numbered project folders |
| `CLAUDE_GUARDED_SKILLS` | Comma-separated skill names that `guard_stale_skill.py` protects |

Requires Python 3.9+ and no third-party packages. The tests need `pytest`.

## Tests

```bash
python -m pytest -q
```

The 36 black-box tests feed each script the same JSON Claude Code sends to hooks and check the exit codes and the files written.

## License

MIT
