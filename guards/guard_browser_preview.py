#!/usr/bin/env python
"""PreToolUse guard: keep external websites out of the Claude desktop app's
built-in Browser preview pane.

Why it exists (a real incident): a session opened an external exchange website in
the preview pane to scrape it and never closed the tab. The tab kept retrying in
the background while the pane was hidden, so it was not being composited; the
app's preview screenshot timed out after 5 seconds and took the GPU process down,
crashing the desktop app. It happened three times in one day. That session called
browser tools 565 times and closed a tab zero times, so "remember to close it"
did not work.

The root cause is "tab left open"; external sites just make it trigger more often.
This guard blocks the high-risk pattern, and guard_browser_cleanup.py handles
closing. The two together are the fix.

Matching (anything that cannot be parsed is allowed):
  preview_start with url  -> blocked unless the host is localhost-family
  preview_start with name -> look the entry up in .claude/launch.json; blocked if
                             its url override is not localhost-family
  navigate with url       -> same; back / forward / about:* pass
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

tool = data.get("tool_name") or ""
args = data.get("tool_input") or {}

LADDER = (
    "To read web content, use this order instead of the preview pane:\n"
    "  1. Look for an API host first. A site behind Cloudflare often has an API\n"
    "     domain that a plain HTTP client can reach.\n"
    "  2. The WebFetch tool.\n"
    "  3. If a real browser is truly needed, use a browser that runs in its own\n"
    "     process (e.g. the Claude in Chrome extension), and close the tab after.\n"
)


def is_local(host):
    """localhost family: localhost, *.localhost, 127.0.0.0/8, ::1, 0.0.0.0."""
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    h = h.split("@")[-1]
    if ":" in h and not h.count(":") > 1:      # host:port (IPv6 has several colons)
        h = h.rsplit(":", 1)[0]
    if h in ("localhost", "::1", "0.0.0.0", "0"):
        return True
    if h.endswith(".localhost"):
        return True
    if re.match(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$", h):
        return True
    return False


def host_of(url):
    """The tool adds https:// when no scheme is given; mirror that."""
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    m = re.match(r"^[a-z0-9+.\-]+://([^/?#]*)", u, re.I)
    return m.group(1) if m else None


def block(what, detail):
    sys.stderr.write(
        "[guard] External pages may not be loaded into the desktop app's Browser preview pane.\n"
        f"Blocked: {what}\n"
        f"Reason: {detail}\n\n"
        "A tab left in the background makes the preview screenshot time out and can\n"
        "crash the app's GPU process. The user has blocked this at the tool layer;\n"
        "do not work around it.\n\n"
        + LADDER
        + "\nThe preview pane is for local dev servers only (localhost), and must be\n"
        "closed with preview_stop / tabs_close when done.\n"
    )
    sys.exit(2)


if tool.endswith("preview_start"):
    url = args.get("url")
    name = args.get("name")

    if url:
        h = host_of(url)
        if h and not is_local(h):
            block(f"preview_start url={url[:160]}", f"host `{h}` is not localhost")

    if name:
        # A launch.json entry may carry an optional url that overrides localhost:<port>
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        lj = os.path.join(root, ".claude", "launch.json")
        try:
            with open(lj, encoding="utf-8") as f:
                cfgs = json.load(f).get("configurations", [])
            for c in cfgs:
                if c.get("name") != name:
                    continue
                cu = c.get("url")
                if cu:
                    h = host_of(cu)
                    if h and not is_local(h):
                        block(
                            f"preview_start name={name}",
                            f"this entry in .claude/launch.json points to `{h}`, not localhost",
                        )
        except Exception:
            pass  # unreadable launch.json: allow, never block normal development

elif tool.endswith("navigate"):
    url = (args.get("url") or "").strip()
    if url.lower() in ("back", "forward") or url.lower().startswith("about:"):
        sys.exit(0)
    h = host_of(url)
    if h and not is_local(h):
        block(f"navigate url={url[:160]}", f"host `{h}` is not localhost")

sys.exit(0)
