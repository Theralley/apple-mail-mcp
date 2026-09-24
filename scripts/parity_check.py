#!/usr/bin/env python3
"""Compare the tools' Envelope Index path with their AppleScript path (manual, live).

Each rewritten read-only tool is called twice in this process on a small
window of one account: once reading Mail's Envelope Index, once with
APPLE_MAIL_MCP_NO_INDEX=1 so it runs its original AppleScript. The two
outputs must be identical. The script prints SAME, DIFF (with a unified
diff), FELLBACK (the index path ran the script anyway) or ERROR per check,
with both timings.

This talks to the real Mail.app and prints real mail in the diffs; use
--quiet to print only the status lines. It only reads: every tool it calls
is read-only. Arguments (the account name, filters) are taken from the
command line or derived from the newest inbox message; nothing about the
mailbox is stored in this file.

    uv run --python 3.12 --with 'mcp<2' python scripts/parity_check.py \\
        --account "Work" [--window 20] [--only get_top_senders ...] \\
        [--heavy] [--quiet]

--heavy adds the checks whose AppleScript path walks whole mailboxes or all
accounts (get_inbox_overview, the statistics account overview, the
dashboard's recent emails); on large mailboxes those scripts can take
minutes or time out, which is what the index path fixes.

Exit status: 0 when every check is SAME, 1 otherwise, 2 when the index
cannot be opened (then there is nothing to compare).
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from apple_mail_mcp import envelope_index  # noqa: E402
from apple_mail_mcp.tools import analytics, inbox, search, smart_inbox  # noqa: E402

TOOL_MODULES = (analytics, inbox, search, smart_inbox)


def run_mode(use_index, func, kwargs):
    """(output, seconds, script calls made by the tool itself)."""
    if use_index:
        os.environ.pop("APPLE_MAIL_MCP_NO_INDEX", None)
    else:
        os.environ["APPLE_MAIL_MCP_NO_INDEX"] = "1"
    script_calls = []
    originals = {}
    for module in TOOL_MODULES:
        original = module.run_applescript
        originals[module] = original

        def counting(script, *args, _original=original, **kw):
            script_calls.append(1)
            return _original(script, *args, **kw)

        module.run_applescript = counting
    started = time.monotonic()
    try:
        output = func(**kwargs)
    except Exception as exc:  # report and carry on
        output = f"<exception {type(exc).__name__}: {exc}>"
    finally:
        for module, original in originals.items():
            module.run_applescript = original
        os.environ.pop("APPLE_MAIL_MCP_NO_INDEX", None)
    elapsed = time.monotonic() - started
    if not isinstance(output, str):
        output = json.dumps(output, indent=2, ensure_ascii=False, default=str)
    return output, elapsed, len(script_calls)


def normalise(text):
    """Make JSON output comparable line by line."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        return text


def newest_inbox_message(account):
    index = envelope_index.get_index()
    uuid = envelope_index.account_uuid(account)
    box = envelope_index.find_inbox(index, uuid)
    if box is None:
        return None
    messages = index.messages([box.id], limit=1)
    return messages[0] if messages else None


def build_checks(args):
    account = args.account
    window = args.window
    newest = newest_inbox_message(account)
    sender_address = args.sender or (newest.sender_address if newest else None)
    subject = args.subject or (newest.subject if newest else None)
    keyword = None
    if subject:
        words = [w for w in re.split(r"\W+", subject) if len(w) >= 4]
        keyword = words[0] if words else None
    recent = (date.today() - timedelta(days=3)).isoformat()

    checks = [
        ("list_inbox_emails text", inbox.list_inbox_emails, dict(account=account, max_emails=window)),
        ("list_inbox_emails unread", inbox.list_inbox_emails,
         dict(account=account, max_emails=window, include_read=False)),
        ("list_inbox_emails content", inbox.list_inbox_emails,
         dict(account=account, max_emails=min(window, 5), include_content=True)),
        ("list_inbox_emails json", inbox.list_inbox_emails,
         dict(account=account, max_emails=window, output_format="json")),
        ("list_mailboxes", inbox.list_mailboxes, dict(account=account)),
        ("get_statistics mailbox_breakdown", analytics.get_statistics,
         dict(account=account, scope="mailbox_breakdown")),
        ("get_awaiting_reply", smart_inbox.get_awaiting_reply,
         dict(account=account, days_back=3, max_results=window)),
        ("get_needs_response", smart_inbox.get_needs_response,
         dict(account=account, days_back=3, max_results=window)),
        ("get_top_senders", smart_inbox.get_top_senders, dict(account=account, days_back=7)),
        ("get_top_senders by domain", smart_inbox.get_top_senders,
         dict(account=account, days_back=7, group_by_domain=True)),
        ("search_emails recent", search.search_emails,
         dict(account=account, limit=window, output_format="json", date_from=recent)),
        ("search_emails unread text", search.search_emails,
         dict(account=account, limit=window, read_status="unread")),
        ("search_emails flagged", search.search_emails,
         dict(account=account, limit=window, flagged=True, output_format="json")),
        ("search_emails All mailboxes", search.search_emails,
         dict(account=account, mailbox="All", limit=window, date_from=recent, output_format="json")),
    ]
    if sender_address:
        checks += [
            ("search_emails sender", search.search_emails,
             dict(account=account, sender=sender_address, limit=window, output_format="json")),
            ("get_statistics sender_stats", analytics.get_statistics,
             dict(account=account, scope="sender_stats", sender=sender_address, days_back=30)),
        ]
    if keyword:
        checks += [
            ("search_emails subject", search.search_emails,
             dict(account=account, subject_keyword=keyword, limit=window, output_format="json")),
            ("search_emails body_text", search.search_emails,
             dict(account=account, body_text=keyword, date_from=recent, limit=min(window, 5),
                  output_format="json")),
            ("get_email_thread", search.get_email_thread,
             dict(account=account, subject_keyword=keyword, max_messages=window)),
        ]
    if args.heavy:
        checks += [
            ("get_inbox_overview", inbox.get_inbox_overview, {}),
            ("get_statistics account_overview", analytics.get_statistics,
             dict(account=account, scope="account_overview", days_back=1)),
            ("dashboard recent emails", analytics._get_recent_emails_structured, dict(max_total=20)),
        ]
    if args.only:
        checks = [c for c in checks if any(c[0].startswith(o) for o in args.only)]
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, help="Mail account name, as Mail shows it")
    parser.add_argument("--window", type=int, default=20, help="messages per check (default 20)")
    parser.add_argument("--sender", help="sender filter (default: newest inbox message's address)")
    parser.add_argument("--subject", help="subject to take a keyword from (default: newest inbox message)")
    parser.add_argument("--only", nargs="+", help="run only checks whose name starts with one of these")
    parser.add_argument("--heavy", action="store_true", help="also run whole-mailbox checks")
    parser.add_argument("--quiet", action="store_true", help="print status lines only, no diffs")
    args = parser.parse_args()

    os.environ.pop("APPLE_MAIL_MCP_NO_INDEX", None)
    try:
        envelope_index.get_index()
        envelope_index.account_uuid(args.account)
    except envelope_index.IndexUnavailable as exc:
        print(f"Envelope Index not usable: {exc}", file=sys.stderr)
        return 2

    failures = 0
    for name, func, kwargs in build_checks(args):
        via_index, index_s, fallback_calls = run_mode(True, func, kwargs)
        via_script, script_s, _ = run_mode(False, func, kwargs)
        a, b = normalise(via_index), normalise(via_script)
        if fallback_calls:
            status = "FELLBACK"
        elif via_index.startswith("<exception") or via_script.startswith("<exception"):
            status = "ERROR"
        elif a == b:
            status = "SAME"
        else:
            status = "DIFF"
        if status != "SAME":
            failures += 1
        print(f"{status:8} {name:36} index {index_s:7.2f}s  script {script_s:7.2f}s")
        if status == "ERROR" and not args.quiet:
            for label, text in (("index", via_index), ("script", via_script)):
                if text.startswith("<exception"):
                    print(f"    {label}: {text}")
        if status in ("DIFF", "FELLBACK") and not args.quiet:
            diff = difflib.unified_diff(
                b.splitlines(), a.splitlines(), "applescript", "envelope-index", lineterm="", n=1
            )
            for line in diff:
                print("    " + line)
    print(f"\n{failures} check(s) differ" if failures else "\nall checks identical")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
