"""Tests for reading Mail's Envelope Index (envelope_index.py) and the tools on it.

A synthetic Envelope Index with the tables and columns the module reads is
built in a temporary ~/Library/Mail/V10/MailData. Mail's account list, its
mailbox order and osascript's date formatting are stubbed; every test fails
if a tool reaches for Mail over AppleScript.
"""

import json
import sqlite3
import time
from unittest.mock import patch

import pytest

from apple_mail_mcp import emlx, envelope_index
from apple_mail_mcp.core import FIELD_SEP, RECORD_SEP
from apple_mail_mcp.envelope_index import IndexUnavailable
from apple_mail_mcp.tools import analytics, inbox, search, smart_inbox

NOW = int(time.time())
DAY = 86400

SCHEMA = """
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0, unread_count INTEGER NOT NULL DEFAULT 0,
    source INTEGER);
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
    comment TEXT NOT NULL);
CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER,
    message_id_header TEXT);
CREATE TABLE messages (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL DEFAULT 0,
    global_message_id INTEGER NOT NULL, sender INTEGER, subject_prefix TEXT, subject INTEGER NOT NULL,
    date_sent INTEGER, date_received INTEGER, mailbox INTEGER NOT NULL, flags INTEGER NOT NULL DEFAULT 0,
    read INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0, conversation_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER, PRIMARY KEY(message_id, mailbox_id)) WITHOUT ROWID;
CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER NOT NULL, address INTEGER NOT NULL,
    type INTEGER, position INTEGER);
CREATE TABLE attachments (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, message INTEGER NOT NULL,
    attachment_id TEXT, name TEXT);
"""

MAILBOXES = [
    (1, "ews://UUID-W/Inbox"),
    (2, "ews://UUID-W/Sent%20Items"),
    (3, "ews://UUID-W/Inbox/Projects"),
    (4, "ews://UUID-W/Deleted%20Items"),
    (5, "ews://UUID-W/Archive"),
    (10, "imap://UUID-G/%5BGmail%5D/All%20Mail"),
    (11, "imap://UUID-G/INBOX"),
    (12, "imap://UUID-G/Kva%CC%88tton"),  # "Kvätton" with a decomposed "ä"
]
ACCOUNTS = [("Work", "UUID-W"), ("Gmail", "UUID-G")]
TREE = [
    ("Work", [("Inbox", ["Projects"]), ("Sent Items", []), ("Deleted Items", []), ("Archive", [])]),
    ("Gmail", [("INBOX", []), ("[Gmail]", ["All Mail"]), ("Kvätton", [])]),
]
ADDRESSES = {
    "alice": ("alice@example.com", "Alice"),
    "bob": ("bob@example.com", ""),
    "news": ("news@substack.com", "Weekly"),
    "carol": ("carol@example.com", "Carol"),
    "dave": ("dave@example.com", "Dave D"),
    "noreply": ("noreply@shop.example", ""),
    "eve": ("eve@example.org", "Eve"),
    "me": ("me@example.com", "Me"),
}
FLAG_BLUE = 4 << envelope_index.FLAG_COLOR_SHIFT

# (id, sender, prefix, subject, received, sent, mailbox, read, flagged, flags, deleted, header, conversation)
MESSAGES = [
    (101, "alice", None, "Budget?", NOW - 100, NOW - 110, 1, 0, 1, FLAG_BLUE, 0, "<a1@example.com>", 7),
    (102, "bob", "Re: ", "Lunch plans", NOW - 200, NOW - 210, 1, 1, 0, 0, 0, "<b1@example.com>", 8),
    (103, "alice", None, "Deleted one", NOW - 50, NOW - 50, 1, 0, 0, 0, 1, "<d@example.com>", 0),
    (104, "news", None, "Weekly digest", NOW - 300, NOW - 300, 1, 0, 0, 0, 0, None, 0),
    (105, "carol", None, "Old thing?", NOW - 40 * DAY, NOW - 40 * DAY, 1, 0, 0, 0, 0, "<c1@example.com>", 0),
    (201, "me", None, "Lunch plans", NOW - 1000, NOW - 1000, 2, 1, 0, 0, 0, "<s1@example.com>", 8),
    (202, "me", None, "Proposal", NOW - 900, NOW - 900, 2, 1, 0, 0, 0, "<s2@example.com>", 0),
    (203, "me", None, "Receipt", NOW - 800, NOW - 800, 2, 1, 0, 0, 0, "<s3@example.com>", 0),
    (301, "alice", "Re: ", "Budget?", NOW - 150, NOW - 150, 3, 1, 0, 0, 0, "<a2@example.com>", 7),
    (1001, "eve", None, "Hello ||| there", NOW - 10, NOW - 10, 10, 0, 0, 0, 0, "<e1@example.org>", 0),
    (1002, "eve", None, "Only in All Mail", NOW - 20, NOW - 20, 10, 1, 0, 0, 0, "<e2@example.org>", 0),
    (1003, "eve", None, "Deleted label", NOW - 5, NOW - 5, 10, 0, 0, 0, 1, "<e3@example.org>", 0),
    (1201, "eve", None, "Receipt April", NOW - 30, NOW - 30, 12, 1, 0, 0, 0, None, 0),
]
LABELS = [(1001, 11), (1003, 11)]
RECIPIENTS = [(201, "bob", 0, 0), (201, "carol", 1, 0), (202, "dave", 0, 0), (203, "noreply", 0, 0)]
ATTACHMENTS = [101, 101, 301]


def build_index(path, drop_column=None):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO mailboxes (ROWID, url) VALUES (?, ?)", MAILBOXES)
    address_ids = {}
    for key, (address, comment) in ADDRESSES.items():
        address_ids[key] = con.execute(
            "INSERT INTO addresses (address, comment) VALUES (?, ?)", (address, comment)
        ).lastrowid
    subject_ids = {}
    for (rowid, sender, prefix, subject, received, sent, mailbox, read, flagged, flags,
         deleted, header, conversation) in MESSAGES:
        if subject not in subject_ids:
            subject_ids[subject] = con.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,)).lastrowid
        global_id = con.execute(
            "INSERT INTO message_global_data (message_id, message_id_header) VALUES (?, ?)", (rowid, header)
        ).lastrowid
        con.execute(
            "INSERT INTO messages (ROWID, global_message_id, sender, subject_prefix, subject, date_sent,"
            " date_received, mailbox, flags, read, flagged, deleted, conversation_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rowid, global_id, address_ids[sender], prefix, subject_ids[subject], sent, received,
             mailbox, flags, read, flagged, deleted, conversation),
        )
    con.executemany("INSERT INTO labels VALUES (?, ?)", LABELS)
    con.executemany(
        "INSERT INTO recipients (message, address, type, position) VALUES (?, ?, ?, ?)",
        [(m, address_ids[a], t, p) for m, a, t, p in RECIPIENTS],
    )
    con.executemany("INSERT INTO attachments (message) VALUES (?)", [(m,) for m in ATTACHMENTS])
    if drop_column:
        con.execute(f"ALTER TABLE messages RENAME COLUMN {drop_column} TO unused_{drop_column}")
    con.commit()
    con.close()


def fake_dates(timestamps):
    return [f"D{t - NOW}" for t in timestamps]


def date_of(offset):
    return f"D{offset}"


def no_mail(*args, **kwargs):
    raise AssertionError("a tool asked Mail over AppleScript")


@pytest.fixture
def mail_db(tmp_path, monkeypatch):
    """A synthetic Envelope Index in a fake ~/Library/Mail; returns its path."""
    data = tmp_path / "V10" / "MailData"
    data.mkdir(parents=True)
    db = data / "Envelope Index"
    build_index(db)
    monkeypatch.setattr(emlx, "MAIL_DIR", tmp_path)
    monkeypatch.delenv("APPLE_MAIL_MCP_NO_INDEX", raising=False)
    envelope_index.reset()
    monkeypatch.setattr(envelope_index, "_accounts", list(ACCOUNTS))
    monkeypatch.setattr(
        envelope_index, "mailbox_tree",
        lambda account=None, with_subs=True: [
            (name, [(box, subs if with_subs else []) for box, subs in boxes])
            for name, boxes in TREE
            if account is None or name.lower() == account.lower()
        ],
    )
    monkeypatch.setattr(envelope_index, "_format_dates_via_osascript", fake_dates)
    for module in (inbox, analytics, search, smart_inbox):
        monkeypatch.setattr(module, "run_applescript", no_mail)
    monkeypatch.setattr(emlx, "_source_via_applescript", lambda *a, **k: None)
    return db


# ---------------------------------------------------------------------------
# Opening, validation, fallback
# ---------------------------------------------------------------------------


def test_opened_read_only(mail_db):
    index = envelope_index.get_index()
    before = mail_db.read_bytes()
    for statement in ("INSERT INTO subjects (subject) VALUES ('x')", "UPDATE messages SET read = 1",
                      "CREATE TABLE t (a)"):
        with pytest.raises(sqlite3.OperationalError):
            index.conn.execute(statement)
    assert mail_db.read_bytes() == before


def test_schema_mismatch_falls_back_to_applescript(tmp_path, monkeypatch, capsys):
    data = tmp_path / "V10" / "MailData"
    data.mkdir(parents=True)
    build_index(data / "Envelope Index", drop_column="flagged")
    monkeypatch.setattr(emlx, "MAIL_DIR", tmp_path)
    monkeypatch.delenv("APPLE_MAIL_MCP_NO_INDEX", raising=False)
    envelope_index.reset()

    with pytest.raises(IndexUnavailable, match="messages lacks flagged"):
        envelope_index.get_index()
    with patch.object(smart_inbox, "run_applescript", return_value="TOP SENDERS") as run:
        assert smart_inbox.get_top_senders(account="Work") == "TOP SENDERS"
        smart_inbox.get_top_senders(account="Work")
    assert run.call_count == 2
    assert capsys.readouterr().err.count("table messages lacks flagged") == 1  # logged once


def test_missing_database_falls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(emlx, "MAIL_DIR", tmp_path)
    monkeypatch.delenv("APPLE_MAIL_MCP_NO_INDEX", raising=False)
    envelope_index.reset()
    with patch.object(analytics, "run_applescript", return_value="MAILBOX STATISTICS") as run:
        assert analytics.get_statistics(account="Work", scope="mailbox_breakdown") == "MAILBOX STATISTICS"
    run.assert_called_once()
    assert "no Envelope Index found" in capsys.readouterr().err


def test_unknown_account_or_mailbox_falls_back(mail_db):
    with patch.object(smart_inbox, "run_applescript", return_value="from script") as run:
        assert smart_inbox.get_top_senders(account="Nope") == "from script"
        assert smart_inbox.get_top_senders(account="Work", mailbox="Missing") == "from script"
    assert run.call_count == 2


def test_busy_database_is_retried(mail_db):
    index = envelope_index.get_index()
    real = index.conn
    calls = []

    class Flaky:
        def execute(self, sql, params=()):
            calls.append(sql)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, params)

    index.conn = Flaky()
    try:
        assert index.counts([1]) == (4, 3)
    finally:
        index.conn = real
    assert len(calls) == 2


def test_mail_accounts_reads_ids_and_names_once(monkeypatch):
    monkeypatch.setattr(envelope_index, "_accounts", None)
    output = RECORD_SEP.join([FIELD_SEP.join(["UUID-W", "Work"]), FIELD_SEP.join(["UUID-G", "Gmail"])])
    with patch("apple_mail_mcp.core.run_applescript", return_value=output) as run:
        assert envelope_index.mail_accounts() == ACCOUNTS
        assert envelope_index.mail_accounts() == ACCOUNTS
    run.assert_called_once()
    script = run.call_args.args[0]
    assert "id of every account" in script and "name of every account" in script
    assert "message" not in script


def test_date_strings_come_from_osascript_outside_mail(monkeypatch):
    scripts = []

    def fake_osascript(script, timeout):
        scripts.append(script)
        return RECORD_SEP.join(["first", "second"])

    monkeypatch.setattr("apple_mail_mcp.core._run_applescript_unlocked", fake_osascript)
    assert envelope_index.date_strings([200, 100, 200]) == {100: "first", 200: "second"}
    assert envelope_index.date_strings([100]) == {100: "first"}  # cached
    assert len(scripts) == 1
    assert 'tell application "Mail"' not in scripts[0]
    assert "return dt as string" in scripts[0]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def ids(messages):
    return [m.id for m in messages]


def test_messages_newest_first_without_deleted(mail_db):
    index = envelope_index.get_index()
    assert ids(index.messages([1])) == [101, 102, 104, 105]
    assert ids(index.messages([1], limit=2, offset=1)) == [102, 104]
    assert ids(index.messages([1], unread_only=True)) == [101, 104, 105]
    assert ids(index.messages([1], read=True)) == [102]
    assert index.counts([1]) == (4, 3)


def test_gmail_labels_put_messages_in_inbox(mail_db):
    index = envelope_index.get_index()
    inbox_box = envelope_index.find_inbox(index, "UUID-G")
    assert inbox_box.id == 11 and inbox_box.path == "INBOX"
    assert ids(index.messages([11])) == [1001]  # labelled; 1002 is only in All Mail, 1003 deleted
    assert ids(index.messages([10])) == [1001, 1002]
    assert index.counts([11]) == (1, 1)


def test_message_fields(mail_db):
    index = envelope_index.get_index()
    budget, lunch = index.messages([1], limit=2)
    assert budget.sender == "Alice <alice@example.com>"
    assert budget.internet_message_id == "a1@example.com"
    assert budget.flag_index == 4 and budget.flagged
    assert lunch.subject == "Re: Lunch plans"
    assert lunch.sender == "bob@example.com"
    assert lunch.flag_index == -1


def test_filters(mail_db):
    index = envelope_index.get_index()
    assert ids(index.messages([1], sender_contains="ALICE")) == [101]
    assert ids(index.messages([1, 3], subject_contains_any=["budget", "LUNCH"])) == [101, 301, 102]
    assert ids(index.messages([1], flag_index=4)) == [101]
    assert ids(index.messages([1], flag_index=0)) == []
    assert ids(index.messages([1], flagged=False)) == [102, 104, 105]
    assert ids(index.messages([1, 3], has_attachments=True)) == [101, 301]
    assert ids(index.messages([1], has_attachments=False)) == [102, 104, 105]
    assert ids(index.messages([1], received_after=NOW - 250)) == [101, 102]
    assert ids(index.messages([1, 3], conversation_id=7)) == [101, 301]
    assert ids(index.messages([2], sent_after=NOW - 950)) == [203, 202]
    assert index.to_recipients([201, 202]) == {
        201: [("bob@example.com", "")],
        202: [("dave@example.com", "Dave D")],
    }


def test_mailbox_paths_are_decoded_and_normalised(mail_db):
    index = envelope_index.get_index()
    assert index.find_mailbox("UUID-G", "[Gmail]/All Mail").id == 10
    assert index.find_mailbox("UUID-G", "kvätton").id == 12  # NFC query, NFD url
    assert index.find_mailbox("UUID-W", "Inbox/Projects").id == 3
    assert index.find_mailbox("UUID-W", "INBOX").id == 1
    assert index.find_mailbox("UUID-G", "Projects") is None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

RULE = "━" * 40


def test_list_inbox_emails_text(mail_db):
    result = inbox.list_inbox_emails(account="work", max_emails=2)
    assert result == "\n".join([
        "INBOX EMAILS - ALL ACCOUNTS",
        "",
        RULE,
        "📧 ACCOUNT: Work (4 messages)",
        RULE,
        "",
        "✉ Budget?",
        "   From: Alice <alice@example.com>",
        f"   Date: {date_of(-100)}",
        "   Link: message://%3Ca1@example.com%3E",
        "",
        "✓ Re: Lunch plans",
        "   From: bob@example.com",
        f"   Date: {date_of(-200)}",
        "   Link: message://%3Cb1@example.com%3E",
        "",
        "========================================",
        "TOTAL EMAILS: 2",
        "========================================",
    ])


def test_list_inbox_emails_unread_with_content(mail_db):
    with patch.object(emlx, "get_message_body", side_effect=lambda i, a, m: {"1001": "Hi\nthere"}.get(i)) as body:
        result = inbox.list_inbox_emails(include_read=False, include_content=True, max_emails=0)
    assert "📧 ACCOUNT: Gmail (1 messages)" in result
    assert "✉ Hello ||| there\n   From: Eve <eve@example.org>" in result
    assert "   Content: Hi there\n" in result
    assert "   Content: [Not available]\n" in result  # Work bodies are not on disk
    assert "Re: Lunch plans" not in result
    assert result.endswith("TOTAL EMAILS: 4\n========================================")
    assert ("1001", "Gmail", "INBOX") in [c.args for c in body.call_args_list]


def test_list_inbox_emails_json(mail_db):
    emails = json.loads(inbox.list_inbox_emails(output_format="json", max_emails=1))
    assert emails == [
        {
            "subject": "Budget?", "sender": "Alice <alice@example.com>", "date": date_of(-100),
            "is_read": False, "account": "Work", "message_id": "101",
            "internet_message_id": "a1@example.com", "mail_link": "message://%3Ca1@example.com%3E",
        },
        {
            "subject": "Hello ||| there", "sender": "Eve <eve@example.org>", "date": date_of(-10),
            "is_read": False, "account": "Gmail", "message_id": "1001",
            "internet_message_id": "e1@example.org", "mail_link": "message://%3Ce1@example.org%3E",
        },
    ]


def test_list_mailboxes(mail_db):
    assert inbox.list_mailboxes(account="Work") == "\n".join([
        "MAILBOXES",
        "",
        RULE,
        "📁 ACCOUNT: Work",
        RULE,
        "",
        "  📂 Inbox (4 total, 3 unread)",
        "    └─ Projects [Path: Inbox/Projects] (1 total, 0 unread)",
        "  📂 Sent Items (3 total, 0 unread)",
        "  📂 Deleted Items (0 total, 0 unread)",
        "  📂 Archive (0 total, 0 unread)",
    ])
    gmail = inbox.list_mailboxes(account="Gmail", include_counts=False)
    assert "  📂 [Gmail]\n    └─ All Mail [Path: [Gmail]/All Mail]\n  📂 Kvätton" in gmail


def test_inbox_overview(mail_db):
    result = inbox.get_inbox_overview()
    assert "  ⚠️  Work: 3 unread (4 total)\n  ⚠️  Gmail: 1 unread (1 total)\n" in result
    assert "📈 TOTAL UNREAD: 4 across all accounts" in result
    assert "Account: Work\n  📂 Inbox (3 unread)\n  📂 Sent Items\n" in result
    assert "Account: Gmail\n  📂 INBOX (1 unread)\n  📂 [Gmail]\n     └─ All Mail (1 unread)\n" in result
    # Ten per account are collected, the first ten shown: Work's four, then Gmail's.
    recent = result.split("(10 Most Recent)")[1]
    assert recent.index("✉ Budget?\n   Account: Work") < recent.index("✉ Hello ||| there\n   Account: Gmail")
    assert "1. 📧 Review unread emails" in result


def test_statistics(mail_db):
    overview = analytics.get_statistics(account="Work", days_back=0)
    # Inbox and Archive only: Sent Items and Deleted Items are skipped; sub-mailboxes are not walked.
    assert "Total Emails: 4\nUnread: 3 (75%)\nRead: 1 (25%)\nFlagged: 1\nWith Attachments: 1 (25%)\n" in overview
    assert "👥 TOP SENDERS\n" + RULE + "\nAlice <alice@example.com>: 1 emails\nbob@example.com: 1 emails\n" in overview
    assert "📁 MAILBOX DISTRIBUTION\n" + RULE + "\nInbox: 4 (100%)" in overview
    assert "Total Emails: 3\n" in analytics.get_statistics(account="Work", days_back=30)

    sender = analytics.get_statistics(account="Work", scope="sender_stats", sender="alice", days_back=0)
    assert sender == (
        "SENDER STATISTICS\n\nSender: alice\nAccount: Work\n\n"
        "Total emails: 1\nUnread: 1\nWith attachments: 1"
    )
    box = analytics.get_statistics(account="Gmail", scope="mailbox_breakdown")
    assert box == "MAILBOX STATISTICS\n\nMailbox: INBOX\nAccount: Gmail\n\nTotal messages: 1\nUnread: 1\nRead: 0"


def test_awaiting_reply(mail_db):
    result = smart_inbox.get_awaiting_reply(account="Work")
    # 201 was answered ("Re: Lunch plans" from bob), 203 went to a noreply address.
    assert result == "\n".join([
        "EMAILS AWAITING REPLY",
        "Account: Work | Last 7 days",
        "========================================",
        "",
        "1. Proposal",
        "   To: Dave D <dave@example.com>",
        f"   Sent: {date_of(-900)}",
        "",
        "========================================",
        "Found 1 sent email(s) awaiting reply.",
    ])
    everyone = smart_inbox.get_awaiting_reply(account="Work", exclude_noreply=False)
    assert "1. Receipt\n   To: noreply@shop.example\n" in everyone  # newest sent first
    assert "2. Proposal\n" in everyone


def test_needs_response(mail_db):
    with patch.object(smart_inbox, "get_message_body", return_value="fyi"):
        result = smart_inbox.get_needs_response(account="Work", days_back=0)
    # 104 is a newsletter; 105 asks a question in its subject; 101 is flagged blue.
    assert result == "\n".join([
        "EMAILS NEEDING RESPONSE",
        "Account: Work | Mailbox: INBOX | Last 0 days",
        "========================================",
        "",
        "1. [HIGH (flagged blue + question)] Budget?",
        "   From: Alice <alice@example.com>",
        f"   Date: {date_of(-100)}",
        "",
        "2. [MEDIUM (contains question)] Old thing?",
        "   From: Carol <carol@example.com>",
        f"   Date: {date_of(-40 * DAY)}",
        "",
        "========================================",
        "Found 2 email(s) needing response.",
    ])
    assert "Found 1 email(s)" in smart_inbox.get_needs_response(account="Work", days_back=7)


def test_top_senders(mail_db):
    result = smart_inbox.get_top_senders(account="Work", mailbox="Sent Items", days_back=0)
    assert "1. Me <me@example.com>: 3 emails (100%)" in result
    by_domain = smart_inbox.get_top_senders(account="Work", days_back=0, group_by_domain=True)
    assert by_domain == "\n".join([
        "TOP SENDER DOMAINS",
        "Account: Work | Mailbox: INBOX | Last 0 days",
        "========================================",
        "",
        "1. example.com: 3 emails (75%)",
        "2. substack.com: 1 emails (25%)",
        "",
        "========================================",
        "Total emails analysed: 4",
        "Unique senders: 2",
    ])


def test_search_emails(mail_db):
    payload = json.loads(search.search_emails(account="Gmail", output_format="json"))
    assert [i["message_id"] for i in payload["items"]] == ["1001"]
    item = payload["items"][0]
    assert item["subject"] == "Hello ||| there" and item["mailbox"] == "INBOX" and item["account"] == "Gmail"
    assert item["mail_link"] == "message://%3Ce1@example.org%3E"

    everywhere = json.loads(search.search_emails(mailbox="All", output_format="json", limit=50))
    # Sent Items and Deleted Items are skipped, sub-mailboxes are not searched
    assert sorted(i["message_id"] for i in everywhere["items"]) == ["1001", "101", "102", "104", "105", "1201"]

    flagged = json.loads(search.search_emails(account="Work", flag_color="blue", output_format="json"))
    assert [(i["message_id"], i["flag_color"]) for i in flagged["items"]] == [("101", "blue")]

    page = json.loads(search.search_emails(account="Work", output_format="json", limit=2, offset=1,
                                           read_status="unread", sender="e"))
    assert [i["message_id"] for i in page["items"]] == ["104", "105"]
    assert page["has_more"] is False

    dated = json.loads(search.search_emails(
        account="Work", subject_keywords=["budget", "old"], has_attachments=True, output_format="json",
        date_from=time.strftime("%Y-%m-%d", time.localtime(NOW - DAY)),
    ))
    assert [i["message_id"] for i in dated["items"]] == ["101"]


def test_search_emails_body_text_candidates_from_index(mail_db):
    from email.message import EmailMessage

    def message(body):
        m = EmailMessage()
        m.set_content(body)
        return m

    bodies = {"101": "see the invoice", "102": "nothing", "104": "INVOICE inside", "105": "no"}
    with patch.object(search, "get_message", side_effect=lambda i, a, m, timeout: message(bodies[i])) as get:
        payload = json.loads(search.search_emails(
            account="Work", body_text="invoice", include_content=True, output_format="json"
        ))
    assert [c.args[:3] for c in get.call_args_list] == [
        ("101", "Work", "Inbox"), ("102", "Work", "Inbox"), ("104", "Work", "Inbox"), ("105", "Work", "Inbox"),
    ]
    assert [(i["message_id"], i["content_preview"]) for i in payload["items"]] == [
        ("101", "see the invoice"), ("104", "INVOICE inside"),
    ]


def test_email_thread(mail_db):
    with patch.object(emlx, "get_message_body", side_effect=lambda i, a, m: "body of " + i if i == "101" else None):
        result = search.get_email_thread(account="Work", subject_keyword="Re: Budget", mailbox="Inbox")
    assert result == "\n".join([
        "EMAIL THREAD VIEW",
        "",
        "Thread topic: Budget",
        "Account: Work",
        "",
        "━" * 40,
        "FOUND 1 MESSAGE(S) IN THREAD",
        "━" * 40,
        "",
        "✉ Budget?",
        "   From: Alice <alice@example.com>",
        f"   Date: {date_of(-100)}",
        "   Preview: body of 101",
    ])
    everywhere = search.get_email_thread(account="Work", subject_keyword="Budget", mailbox="All")
    assert "FOUND 1 MESSAGE(S)" in everywhere  # Projects is a sub-mailbox, not walked


def test_dashboard_recent_emails(mail_db):
    with patch.object(analytics, "get_message_body", return_value=None):
        emails = analytics._get_recent_emails_structured(max_total=3, max_per_account=2)
    assert [(e["subject"], e["account"]) for e in emails] == [
        ("Budget?", "Work"), ("Re: Lunch plans", "Work"), ("Hello ||| there", "Gmail"),
    ]
