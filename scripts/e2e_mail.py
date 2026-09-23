#!/usr/bin/env python3
"""End-to-end smoke test against the real Mail.app (macOS only, manual).

Starts the server over stdio, calls every registered tool at least once, and
prints one line per call: status, wall time, output size. Tool output is never
printed or written anywhere, because it is real mail.

Safety rules the script enforces:
  * never sends, replies, forwards, deletes, moves, archives or marks real
    mail and never empties the trash;
  * mutating tools only run in dry-run mode, or against a subject that matches
    nothing ("[mcp-e2e] no-such-message-<uuid>");
  * the write path is exercised only against a server without --read-only
    and when --draft-account and --draft-to are given: it creates one draft with subject "[mcp-e2e] <uuid>" and deletes
    that same draft again. Mail keeps the invisible outgoing message that
    manage_drafts(action="create") opens until Mail quits (AppleScript cannot
    close it), so the draft can be re-saved later; delete any leftover
    "[mcp-e2e]" draft by hand;
  * create_mailbox is skipped, because no tool can remove the folder again.

Account names come from the command line, never from this file:

    python3 scripts/e2e_mail.py --account "Work" \
        [--draft-account "Work" --draft-to you@example.com] [--skills]

Exit status is non-zero if any call fails.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_stdio_client import McpStdioClient, result_text  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CMD = [
    "uv", "run", "--quiet", "--directory", str(REPO), "--python", "3.12",
    "--with", "mcp<2", "mcp-apple-mail", "--read-only",
]
SKIPPED = {
    "create_mailbox": "creates a real folder that no tool can remove",
    "compose_email": "sends mail",
    "reply_to_email": "sends mail",
    "forward_email": "sends mail",
}


class Runner:
    def __init__(self, client, timeout):
        self.client = client
        self.timeout = timeout
        self.called = set()
        self.failures = 0
        self.read_only = False

    def call(self, tool, args, label=None, expect=None, allow_error=False):
        """Call *tool*; return its text. *expect* must occur in the output."""
        self.called.add(tool)
        start = time.monotonic()
        try:
            is_error, text = result_text(
                self.client.call_tool(tool, args, timeout=self.timeout)
            )
        except TimeoutError:
            is_error, text = True, "client timeout"
        elapsed = time.monotonic() - start
        looks_failed = is_error or text.lstrip().startswith("Error")
        ok = (not looks_failed or allow_error) and (expect is None or expect in text)
        if not ok:
            self.failures += 1
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {label or tool:<44} {elapsed:6.1f}s  {len(text):>7} chars", flush=True)
        if not ok:
            # Error text only: it comes from the server, not from a message.
            print(f"      {text.strip().splitlines()[0][:200] if text.strip() else '(empty)'}")
        return text


def first_message(runner, account):
    """Newest inbox message of *account* as a search record (kept in memory only)."""
    text = runner.call(
        "search_emails",
        {"account": account, "output_format": "json", "limit": 5},
        label="search_emails (newest, json)",
    )
    try:
        items = json.loads(text)["items"]
    except (ValueError, KeyError):
        return None
    return items[0] if items else None


def subject_fragment(subject):
    for prefix in ("Re:", "RE:", "Fwd:", "FW:", "Fw:", "SV:", "VB:"):
        if subject.startswith(prefix):
            subject = subject[len(prefix):].strip()
    return subject[:40]


def run_tools(runner, args, tmp):
    acct = args.account
    nomatch = f"[mcp-e2e] no-such-message-{uuid.uuid4().hex[:12]}"

    runner.call("list_accounts", {})
    runner.call("list_account_addresses", {})
    runner.call("get_mailbox_unread_counts", {"summary_only": True}, label="get_mailbox_unread_counts (summary)")
    runner.call("get_mailbox_unread_counts", {"account": acct})
    runner.call("list_mailboxes", {"account": acct})
    runner.call("list_inbox_emails", {}, label="list_inbox_emails (defaults)")
    runner.call("list_inbox_emails", {"account": acct, "max_emails": 5, "output_format": "json"}, label="list_inbox_emails (json)")
    runner.call("list_inbox_emails", {"account": acct, "include_read": False, "max_emails": 5}, label="list_inbox_emails (unread)")
    runner.call("get_inbox_overview", {})

    newest = first_message(runner, acct)
    fragment = subject_fragment(newest["subject"]) if newest else nomatch
    runner.call("search_emails", {"account": acct, "subject_keyword": fragment, "limit": 5}, label="search_emails (subject)")
    runner.call("search_emails", {"account": acct, "read_status": "unread", "date_from": "2020-01-01", "limit": 5}, label="search_emails (unread+date)")
    runner.call(
        "search_emails",
        {"account": acct, "body_text": "the", "date_from": time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400)), "limit": 3},
        label="search_emails (body_text, 2 days)",
    )
    runner.call("get_email_thread", {"account": acct, "subject_keyword": fragment, "max_messages": 5})
    if newest and newest.get("internet_message_id"):
        runner.call("get_email_source", {"account": acct, "message_id": newest["internet_message_id"], "headers_only": True}, label="get_email_source (headers)")
    else:
        runner.call("get_email_source", {"account": acct, "subject_keyword": fragment, "headers_only": True}, label="get_email_source (subject)")
    runner.call("list_email_attachments", {"account": acct, "subject_keyword": fragment})
    runner.call("save_email_attachment", {"account": acct, "subject_keyword": nomatch, "attachment_name": "none.pdf", "save_path": str(tmp / "none.pdf")}, label="save_email_attachment (no match)", allow_error=True)
    (tmp / "export").mkdir()
    runner.call("export_emails", {"account": acct, "scope": "single_email", "subject_keyword": fragment, "save_directory": str(tmp / "export")}, label="export_emails (single, temp dir)")

    runner.call("get_statistics", {"account": acct, "days_back": 7}, label="get_statistics (7 days)")
    runner.call("get_statistics", {"account": acct, "scope": "mailbox_breakdown"}, label="get_statistics (mailbox)")
    runner.call("get_top_senders", {"account": acct, "days_back": 30})
    runner.call("get_needs_response", {"account": acct, "days_back": 7})
    runner.call("get_awaiting_reply", {"account": acct, "days_back": 7})
    runner.call("inbox_dashboard", {}, allow_error=True)

    # Mutating tools (only registered without --read-only): dry run and/or a
    # subject that matches nothing.
    if not runner.read_only:
        runner.call("move_email", {"account": acct, "to_mailbox": "INBOX", "subject_keyword": nomatch, "dry_run": True, "max_moves": 1}, label="move_email (dry run, no match)")
        runner.call("update_email_status", {"account": acct, "action": "mark_read", "subject_keyword": nomatch, "max_updates": 1}, label="update_email_status (no match)")
        runner.call("manage_trash", {"account": acct, "action": "move_to_trash", "subject_keyword": nomatch, "dry_run": True, "max_deletes": 1}, label="manage_trash (dry run, no match)")
    runner.call("manage_drafts", {"account": acct, "action": "list"}, label="manage_drafts (list)")
    if runner.read_only:
        # Refused before Mail is touched; the subject matches nothing anyway.
        for action in ("send", "delete", "open"):
            runner.call("manage_drafts", {"account": acct, "action": action, "draft_subject": nomatch}, label=f"manage_drafts ({action} blocked)", expect="blocked by --read-only", allow_error=True)

    rich_path = tmp / "mcp-e2e.eml"
    runner.call(
        "create_rich_email_draft",
        {"account": acct, "subject": "[mcp-e2e] rich draft file", "text_body": "e2e", "output_path": str(rich_path), "open_in_mail": False},
        label="create_rich_email_draft (.eml only)",
    )

    if runner.read_only:
        print("SKIP  manage_drafts create/delete                (delete is blocked by --read-only)")
    elif args.draft_account and args.draft_to:
        run_draft_roundtrip(runner, args)
    else:
        print("SKIP  manage_drafts create/delete                (pass --draft-account and --draft-to)")

    # Last: a sync keeps Mail busy for a while and slows every later call.
    runner.call("synchronize_account", {"account": acct})


def run_draft_roundtrip(runner, args):
    subject = f"[mcp-e2e] {uuid.uuid4().hex}"
    acct = args.draft_account
    runner.call("manage_drafts", {"account": acct, "action": "create", "subject": subject, "to": args.draft_to, "body": "mcp e2e test draft, safe to delete"}, label="manage_drafts (create)", expect="Draft created")
    found = False
    for _ in range(10):
        listing = runner.call("manage_drafts", {"account": acct, "action": "list"}, label="manage_drafts (list, find e2e)")
        if subject in listing:
            found = True
            break
        time.sleep(3)
    if found:
        runner.call("manage_drafts", {"account": acct, "action": "delete", "draft_subject": subject}, label="manage_drafts (delete e2e)", expect="deleted")
        listing = runner.call("manage_drafts", {"account": acct, "action": "list"}, label="manage_drafts (list, verify gone)")
        if subject in listing:
            runner.failures += 1
            print("FAIL  e2e draft still listed after delete")
    else:
        runner.failures += 1
        print(f"FAIL  e2e draft never appeared in Drafts; remove any outgoing message titled '{subject}' by hand")


def run_skills(runner, args):
    """Execute the read-only steps of every skill in skills/."""
    acct = args.account
    print("-- skill: apple-mail-inbox-triage")
    runner.call("get_mailbox_unread_counts", {"summary_only": True})
    runner.call("list_accounts", {})
    runner.call("list_inbox_emails", {"account": acct, "include_read": False, "max_emails": 20})
    runner.call("get_needs_response", {"account": acct, "days_back": 7})
    runner.call("get_top_senders", {"account": acct, "days_back": 30})

    print("-- skill: apple-mail-thread-summary")
    newest = first_message(runner, acct)
    fragment = subject_fragment(newest["subject"]) if newest else "e2e"
    runner.call("search_emails", {"account": acct, "subject_keyword": fragment, "output_format": "json", "limit": 10})
    runner.call("get_email_thread", {"account": acct, "subject_keyword": fragment, "max_messages": 20})
    runner.call("get_email_source", {"account": acct, "subject_keyword": fragment, "headers_only": True})

    print("-- skill: apple-mail-attachments")
    runner.call("search_emails", {"account": acct, "subject_keyword": fragment, "output_format": "json", "limit": 10})
    runner.call("list_email_attachments", {"account": acct, "subject_keyword": fragment, "max_results": 3})

    print("-- skill: apple-mail-follow-ups")
    runner.call("list_accounts", {})
    runner.call("get_awaiting_reply", {"account": acct, "days_back": 7, "max_results": 20})
    runner.call("get_needs_response", {"account": acct, "days_back": 7, "max_results": 20})
    runner.call("get_email_thread", {"account": acct, "subject_keyword": fragment})

    print("-- skill: apple-mail-safe-drafts (read-only steps)")
    runner.call("get_email_thread", {"account": acct, "subject_keyword": fragment})
    runner.call("list_account_addresses", {})
    runner.call("manage_drafts", {"account": acct, "action": "list"})

    print("-- skill: apple-mail-troubleshooting")
    runner.call("list_accounts", {})


def main():
    parser = argparse.ArgumentParser(description="Apple Mail MCP end-to-end smoke test")
    parser.add_argument("--account", required=True, help="Mail account to exercise")
    parser.add_argument("--draft-account", help="account for the create/delete draft round trip")
    parser.add_argument("--draft-to", help="recipient address written into the test draft")
    parser.add_argument("--skills", action="store_true", help="also run every skill's read-only steps")
    parser.add_argument("--skills-only", action="store_true", help="run only the skill steps")
    parser.add_argument("--timeout", type=float, default=330.0)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="server command (after --)")
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    cmd = cmd or DEFAULT_CMD

    # The export/attachment tools only write under $HOME; the directory is
    # removed again on exit.
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="apple-mail-mcp-e2e-", dir=cache))
    start = time.monotonic()
    client = McpStdioClient(cmd)
    try:
        client.initialize(timeout=120)
        print(f"initialize: {time.monotonic() - start:.2f}s")
        tools = {t["name"] for t in client.list_tools()}
        print(f"tools/list: {len(tools)} tools")
        runner = Runner(client, args.timeout)
        # --read-only registers an allowlist without the mutating tools.
        runner.read_only = "move_email" not in tools
        if not args.skills_only:
            run_tools(runner, args, tmp)
            if runner.read_only:
                print(f"read-only server: {len(tools)} tools registered")
            for name in sorted(tools - runner.called):
                reason = SKIPPED.get(name)
                if reason:
                    print(f"SKIP  {name:<44} {reason}")
                else:
                    runner.failures += 1
                    print(f"FAIL  {name:<44} not exercised by this script")
        if args.skills or args.skills_only:
            run_skills(runner, args)
    finally:
        client.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"failures: {runner.failures}")
    sys.exit(1 if runner.failures else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
