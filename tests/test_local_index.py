"""The local Inbox/Sent index, built from a small fake Envelope Index.

Accounts: "Work" (Exchange: Inbox, Sent Items, Inbox/Notes) and "Gmail"
(messages stored in All Mail, listed in INBOX / Sent Mail by label).
No Mail.app is touched.
"""

import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

import pytest
from apple_mail_mcp import envelope_index as ei
from apple_mail_mcp import local_index as li

ACCOUNTS = [("Work", "U1"), ("Gmail", "U2")]
MAILBOXES = {
    1: "ews://U1/Inbox",
    2: "ews://U1/Sent%20Items",
    3: "ews://U1/Inbox/Notes",
    4: "imap://U2/INBOX",
    5: "imap://U2/%5BGmail%5D/All%20Mail",
    6: "imap://U2/%5BGmail%5D/Sent%20Mail",
}


def _ts(day):
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp()) + 12 * 3600


# id: (mailbox, labels, subject, sender, day, body or None (not on disk), attachments)
MESSAGES = {
    101: (1, [], "Offert takbyte", ("bo@kund.se", "Bo Kund"), "2026-11-10",
          "Hej, här kommer offerten för takbytet. Pris 120 000 kr.", ["Offert 12.pdf"]),
    102: (1, [], "Möte på fredag", ("eva@kund.se", "Eva"), "2026-11-12",
          "Kan vi ses på fredag? Jag får inte till torsdag.", []),
    103: (2, [], "Re: Offert takbyte", ("me@work.se", "Me"), "2026-11-11",
          "Tack Bo, vi far dit på måndag.", []),
    104: (3, [], "Notes subfolder", ("x@y.se", "X"), "2026-11-09", "should not be indexed", []),
    105: (1, [], "Not downloaded", ("z@kund.se", "Z"), "2026-11-13", None, []),
    201: (5, [4], "Gmail inbox mail", ("anna@gmail.com", "Anna"), "2026-11-08",
          "Faktura bifogad, betala senast fredag.", ["faktura.pdf"]),
    202: (5, [6], "Gmail sent mail", ("me@gmail.com", "Me"), "2026-11-07", "Skickar kvittot.", []),
    203: (5, [], "Only in All Mail", ("q@gmail.com", "Q"), "2026-11-06", "archived", []),
}


def _make_envelope_index(path: Path):
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT, source INTEGER);
        CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER);
        CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER, address INTEGER, type INTEGER, position INTEGER);
        CREATE TABLE attachments (ROWID INTEGER PRIMARY KEY, message INTEGER, attachment_id TEXT, name TEXT);
        CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, message_id_header TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, sender INTEGER, subject INTEGER,
            subject_prefix TEXT, date_sent INTEGER, date_received INTEGER, mailbox INTEGER,
            flags INTEGER DEFAULT 0, read INTEGER DEFAULT 1, flagged INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0, conversation_id INTEGER DEFAULT 0, type INTEGER DEFAULT 0,
            global_message_id INTEGER);
    """)
    for rowid, url in MAILBOXES.items():
        db.execute("INSERT INTO mailboxes VALUES (?, ?, ?)", (rowid, url, 5 if rowid in (4, 6) else None))
    db.execute("INSERT INTO addresses VALUES (900, 'ann@work.se', 'Ann')")
    for mid, (box, labels, subject, (addr, name), day, _, files) in MESSAGES.items():
        db.execute("INSERT INTO subjects VALUES (?, ?)", (mid, subject))
        db.execute("INSERT INTO addresses VALUES (?, ?, ?)", (mid, addr, name))
        db.execute("INSERT INTO message_global_data VALUES (?, ?)", (mid, f"<{mid}@test>"))
        db.execute(
            "INSERT INTO messages (ROWID, sender, subject, date_sent, date_received, mailbox, global_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, mid, mid, _ts(day), _ts(day), box, mid))
        for label in labels:
            db.execute("INSERT INTO labels VALUES (?, ?)", (mid, label))
        db.execute("INSERT INTO recipients (message, address, type, position) VALUES (?, 900, 0, 0)", (mid,))
        for i, name in enumerate(files):
            db.execute("INSERT INTO attachments (message, attachment_id, name) VALUES (?, ?, ?)", (mid, str(i), name))
    db.commit()
    db.close()


def _emlx(subject, body):
    raw = (f"Subject: {subject}\nContent-Type: text/plain; charset=utf-8\n"
           f"Content-Transfer-Encoding: 8bit\n\n{body}\n").encode()
    return f"{len(raw)}\n".encode() + raw + b"<plist/>"


@pytest.fixture
def mail(tmp_path, monkeypatch):
    db_path = tmp_path / "Envelope Index"
    _make_envelope_index(db_path)
    index = ei.EnvelopeIndex(db_path)
    files = tmp_path / "files"
    files.mkdir()
    for mid, (_, _, subject, _, _, body, _) in MESSAGES.items():
        if body is not None:
            (files / f"{mid}.emlx").write_bytes(_emlx(subject, body))
    monkeypatch.setattr(ei, "get_index", lambda: index)
    monkeypatch.setattr(ei, "mail_accounts", lambda: list(ACCOUNTS))
    monkeypatch.setattr("apple_mail_mcp.emlx.find_message_file",
                        lambda mid: (files / f"{mid}.emlx") if (files / f"{mid}.emlx").exists() else None)
    monkeypatch.setenv("APPLE_MAIL_MCP_INDEX_PATH", str(tmp_path / "cache" / "mail-index.sqlite3"))
    return files


def _search(query="", **kw):
    conn = li.connect_reader()
    try:
        total, hits = li.search(conn, query, **kw)
    finally:
        conn.close()
    return total, [h.id for h in hits], hits


def test_first_run_indexes_inbox_and_sent_only(mail):
    result = li.update()
    assert result.total == 6  # 101 102 103 105 201 202; not the subfolder, not All Mail only
    assert result.remaining == 0 and result.missing_bodies == 1
    _, ids, hits = _search()
    assert ids == [105, 102, 103, 101, 201, 202]  # newest first
    folders = {h.id: h.folders for h in hits}
    assert folders[103] == "sent" and folders[202] == "sent" and folders[201] == "inbox"


def test_index_file_is_private(mail):
    li.update()
    path = li.index_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_body_subject_attachment_and_people_are_searchable(mail):
    li.update()
    assert _search("120 000")[1] == [101]
    assert _search("offert*")[1] == [103, 101]
    assert _search("subject:offert", folder="sent")[1] == [103]
    assert _search("attachment:faktura")[1] == [201]
    assert _search("from:anna")[1] == [201]
    assert _search('"betala senast"')[1] == [201]
    assert _search("fredag")[1] == [102, 201]


def test_swedish_letters_stay_distinct_and_case_is_ignored(mail):
    li.update()
    assert _search("får")[1] == [102]
    assert _search("far")[1] == [103]
    assert _search("FAKTURA")[1] == [201]
    assert _search("möte")[1] == [102]


def test_filters(mail):
    li.update()
    assert _search(folder="sent")[1] == [103, 202]
    assert _search(account="gmail")[1] == [201, 202]
    assert _search(sender="kund.se")[1] == [105, 102, 101]
    assert _search(has_attachment=True)[1] == [101, 201]
    assert _search(date_from="2026-11-10", date_to="2026-11-11")[1] == [103, 101]
    total, ids, _ = _search(limit=2, offset=2)
    assert total == 6 and ids == [103, 101]


def test_query_syntax_is_never_passed_through(mail):
    li.update()
    for query in ('OR', 'NEAR(a b)', 'a AND', '"', 'subject:', '*', 'body:"', '-x', 'col:"x"', 'x"y'):
        li.match_expression(query)  # never raises
        conn = li.connect_reader()
        try:
            li.search(conn, query) if li.match_expression(query) else None
        finally:
            conn.close()
    assert li.match_expression('from:anna "two words" offer* plain') == \
        'sender : "anna" AND "two words" AND "offer"* AND "plain"'
    assert li.match_expression("https://x.se/a") == '"https://x.se/a"'


def test_missing_body_is_retried_when_it_arrives(mail):
    li.update()
    assert _search("downloaded now")[1] == []
    (mail / "105.emlx").write_bytes(_emlx("Not downloaded", "now downloaded now"))
    result = li.update()
    assert result.retried == 1 and result.missing_bodies == 0
    assert _search("downloaded now")[1] == [105]


def test_deleted_and_moved_messages_leave_the_index(mail, tmp_path):
    li.update()
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.execute("UPDATE messages SET deleted = 1 WHERE ROWID = 102")
    db.execute("UPDATE messages SET mailbox = 3 WHERE ROWID = 101")  # moved to Inbox/Notes
    db.commit()
    db.close()
    result = li.update()
    assert result.removed == 2
    assert _search("fredag")[1] == [201]
    assert 101 not in _search()[1]


def test_scope_and_rebuild(mail):
    li.update(accounts=["Work"], folders=["sent"])
    assert _search()[1] == [103]
    li.update(accounts=["Work"], folders=["inbox"])
    assert _search()[1] == [105, 102, 103, 101]  # sent kept
    result = li.update(accounts=["work"], folders=["inbox", "sent"], rebuild=True)
    assert result.added == 4
    with pytest.raises(li.MailIndexError, match="Unknown account"):
        li.update(accounts=["Nope"])


def _add_message(tmp_path, files, mid, mailbox, labels, subject, day):
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.execute("INSERT INTO subjects VALUES (?, ?)", (mid, subject))
    db.execute("INSERT INTO addresses VALUES (?, 'new@x.se', 'New')", (mid,))
    db.execute("INSERT INTO messages (ROWID, sender, subject, date_sent, date_received, mailbox) "
               "VALUES (?, ?, ?, ?, ?, ?)", (mid, mid, mid, _ts(day), _ts(day), mailbox))
    for label in labels:
        db.execute("INSERT INTO labels VALUES (?, ?)", (mid, label))
    db.commit()
    db.close()
    (files / f"{mid}.emlx").write_bytes(_emlx(subject, "brand new text"))


def test_refresh_keeps_to_the_indexed_scope(mail, tmp_path):
    assert li.refresh(5) is None  # nothing built yet
    li.update(accounts=["Gmail"])
    assert li.refresh(5).total == -1  # nothing new in Mail: no work at all
    _add_message(tmp_path, mail, 301, 5, [4], "New Gmail mail", "2026-11-14")
    _add_message(tmp_path, mail, 302, 1, [], "New Work mail", "2026-11-14")
    result = li.refresh(5)
    assert result.added == 1  # Work was never indexed, so it stays out
    assert _search("brand new")[1] == [301]


def test_refresh_does_a_full_check_after_a_while(mail, tmp_path, monkeypatch):
    li.update()
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.execute("UPDATE messages SET deleted = 1 WHERE ROWID = 102")
    db.commit()
    db.close()
    assert li.refresh(5).total == -1  # a deletion alone is not seen at once
    monkeypatch.setattr(li, "FULL_CHECK_S", -1)
    assert li.refresh(5).removed == 1


def test_time_budget_resumes_newest_first(mail, monkeypatch):
    monkeypatch.setattr(li, "BATCH", 2)
    clock = iter([0.0] + [0.0, 0.0, 99.0] + [99.0] * 50)
    monkeypatch.setattr(li.time, "monotonic", lambda: next(clock))
    result = li.update(max_seconds=10)
    assert (result.added, result.remaining) == (4, 2)
    assert _search()[1] == [105, 102, 103, 101]
    monkeypatch.setattr(li.time, "monotonic", lambda: 0.0)
    result = li.update()
    assert (result.added, result.remaining, result.total) == (2, 0, 6)


def test_one_writer_at_a_time(mail):
    with li.writer_lock(), pytest.raises(li.IndexBusy):
        li.update()


def test_delete_index(mail):
    li.update()
    assert li.delete_index()
    assert not li.index_path().exists()
    with pytest.raises(li.MailIndexError, match="index_mail"):
        li.connect_reader()


def test_tools_end_to_end(mail):
    from apple_mail_mcp.tools import mail_index

    assert "Run index_mail first" in mail_index.search_mail_index("offert")
    out = mail_index.index_mail(scope="inbox,sent")
    assert "Messages in the index: 6" in out and "1 message(s) have no body" in out
    out = mail_index.search_mail_index("offert", folder="inbox")
    assert "ID: 101" in out and "Offert 12.pdf" in out and "[offerten]" not in out
    assert "[Offert]" in out or "offerten" in out
    sent = mail_index.search_mail_index("offert", folder="sent")
    assert "To: Ann <ann@work.se>" in sent
    assert mail_index.index_mail(scope="drafts").startswith("Error")
    assert "deleted" in mail_index.index_mail(delete_index=True)


def test_read_email_by_id(mail):
    from apple_mail_mcp.tools import mail_index

    out = mail_index.read_email(101)
    assert "Subject: Offert takbyte" in out and "120 000 kr" in out
    assert mail_index.read_email(105).startswith("Error")
    assert mail_index.read_email("x").startswith("Error")


def test_index_from_another_version_starts_over(mail):
    li.update()
    with li.writer_lock():
        conn = li.connect()
        conn.execute("UPDATE meta SET value = 'old' WHERE key = 'schema'")
        conn.commit()
        conn.close()
    with pytest.raises(li.MailIndexError, match="another version"):
        li.connect_reader()  # a reader never resets anything
    result = li.update()  # the writer starts a fresh file
    assert result.added == 6 and result.total == 6


def test_readers_do_not_wait_for_a_writer(mail):
    li.update()
    with li.writer_lock():
        writer = li.connect()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE docs SET subject = subject")
        try:
            assert _search("fredag")[1] == [102, 201]  # while the write transaction is open
        finally:
            writer.rollback()
            writer.close()


def test_retry_keeps_folders_from_outside_this_run(mail, tmp_path):
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.execute("INSERT INTO labels VALUES (201, 6)")  # inbox and sent
    db.commit()
    db.close()
    (mail / "201.emlx").unlink()
    li.update()
    assert {h.id: h.folders for h in _search()[2]}[201] == "inbox,sent"
    (mail / "201.emlx").write_bytes(_emlx("Gmail inbox mail", "now here"))
    result = li.update(accounts=["Gmail"], folders=["inbox"])
    assert result.retried == 1
    assert {h.id: h.folders for h in _search()[2]}[201] == "inbox,sent"


def test_new_mail_goes_before_retries(mail, tmp_path, monkeypatch):
    li.update()  # 105 has no body
    _add_message(tmp_path, mail, 99, 1, [], "Older but new to the index", "2026-11-01")
    (mail / "105.emlx").write_bytes(_emlx("Not downloaded", "arrived"))
    monkeypatch.setattr(li, "BATCH", 1)
    clock = iter([0.0, 0.0, 99.0] + [99.0] * 50)  # start, batch 1, batch 2 (over budget)
    monkeypatch.setattr(li.time, "monotonic", lambda: next(clock))
    result = li.update(max_seconds=10)
    assert (result.added, result.retried, result.remaining) == (1, 0, 1)
    assert 99 in _search()[1]


def test_a_narrow_run_does_not_hide_new_mail_elsewhere(mail, tmp_path):
    li.update(accounts=["Work"])
    li.update(accounts=["Gmail"])
    _add_message(tmp_path, mail, 301, 5, [4], "New Gmail mail", "2026-11-14")
    li.update(accounts=["Work"])  # does not look at Gmail
    result = li.refresh(5)
    assert result.total != -1 and result.added == 1
    assert _search("brand new")[1] == [301]


# ---------------------------------------------------------------------------
# Read / unread: live from Mail's database, changed by message ID only
# ---------------------------------------------------------------------------


def _set_read(tmp_path, read, *ids):
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.executemany("UPDATE messages SET read = ? WHERE ROWID = ?", [(read, i) for i in ids])
    db.commit()
    db.close()


def _state_search(**kw):
    conn = li.connect_reader()
    try:
        assert li.attach_mail_state(conn)
        total, hits = li.search(conn, **kw)
        info = li.status(conn)
    finally:
        conn.close()
    return total, {h.id: h.read for h in hits}, info


def test_unread_filter_and_state_are_live(mail, tmp_path):
    li.update()
    _set_read(tmp_path, 0, 102, 201)
    total, hits, info = _state_search(read=False, folder="inbox")
    assert total == 2 and hits == {102: False, 201: False}
    assert info["unread_inbox"] == 2
    _, hits, _ = _state_search(query="offert")
    assert hits[101] is True
    _set_read(tmp_path, 1, 102)  # opened in Mail: no re-index needed
    total, hits, info = _state_search(read=False)
    assert total == 1 and list(hits) == [201] and info["unread_inbox"] == 1
    assert _state_search(read=True, folder="sent")[0] == 2


def test_unread_filter_without_mails_database_says_so(mail):
    li.update()
    conn = li.connect_reader()
    try:
        with pytest.raises(li.MailIndexError):
            li.search(conn, read=False)
        assert li.search(conn, "offert")[1][0].read is None
    finally:
        conn.close()


def test_unread_tools(mail, tmp_path):
    from apple_mail_mcp.tools import mail_index

    mail_index.index_mail()
    _set_read(tmp_path, 0, 102)
    out = mail_index.search_mail_index(read_status="unread", folder="inbox")
    assert "ID: 102" in out and "· unread" in out and "ID: 101" not in out
    assert "Unread in Inbox (all indexed accounts): 1" in out
    assert mail_index.search_mail_index(read_status="maybe").startswith("Error")
    assert "Status: unread" in mail_index.read_email(102)
    assert "Status: read" in mail_index.read_email(101)


def _fake_mail(monkeypatch, tmp_path, apply=True, answer="ok"):
    import re

    from apple_mail_mcp.core import FIELD_SEP, RECORD_SEP
    from apple_mail_mcp.tools import read_state

    scripts = []

    def run(script, timeout=60):
        scripts.append(script)
        ids = [int(x) for x in re.search(r"repeat with wantedId in \{([^}]*)\}", script).group(1).split(",")]
        value = 1 if "to true" in script else 0
        if apply:
            _set_read(tmp_path, value, *ids)
        return RECORD_SEP.join(f"{i}{FIELD_SEP}{answer}" for i in ids)

    monkeypatch.setattr(read_state, "run_applescript", run)
    monkeypatch.setattr(read_state, "POLL_S", 0)
    return read_state, scripts


def test_mark_read_by_id_only(mail, tmp_path, monkeypatch):
    read_state, scripts = _fake_mail(monkeypatch, tmp_path)
    _set_read(tmp_path, 0, 102, 201)
    out = read_state.set_email_read_status([102, "201", 101, 999])
    assert out.startswith("MARK AS READ: 2 of 4 changed")
    assert "✓ now read · ID 102" in out and "✓ now read · ID 201" in out
    assert "= was already read · ID 101" in out and "✗ not found in Mail" in out
    assert len(scripts) == 2  # one per mailbox: Work Inbox, Gmail All Mail
    for script in scripts:
        assert "«class mssg» id wanted" in script and "whose" not in script
        assert "set read status of theMessage to true" in script
        assert "flagged" not in script and "delete" not in script and "move" not in script
    assert any('mailbox "All Mail" of mailbox "[Gmail]"' in s for s in scripts)
    out = read_state.set_email_read_status("102", read=False)
    assert "✓ now unread · ID 102" in out and "to false" in scripts[-1]


def test_mark_read_reports_what_mail_did_not_do(mail, tmp_path, monkeypatch):
    _set_read(tmp_path, 0, 102)
    read_state, _ = _fake_mail(monkeypatch, tmp_path, apply=False)
    monkeypatch.setattr(read_state, "CONFIRM_WAIT_S", 0)
    assert "? Mail accepted it" in read_state.set_email_read_status([102])
    read_state, _ = _fake_mail(monkeypatch, tmp_path, apply=False, answer="Can't get message id 102")
    out = read_state.set_email_read_status([102])
    assert "✗ Mail refused: Can't get message id 102" in out and "0 of 1 changed" in out


def test_mark_read_input_limits(mail, tmp_path, monkeypatch):
    from apple_mail_mcp import server

    read_state, scripts = _fake_mail(monkeypatch, tmp_path)
    assert read_state.parse_ids("1, 2,2") == [1, 2]
    for bad in ("", [], "12a", "-1", list(range(read_state.MAX_IDS + 1))):
        assert read_state.set_email_read_status(bad).startswith("Error")
    assert not scripts
    monkeypatch.setattr(server, "READ_ONLY", True)
    _set_read(tmp_path, 0, 102)
    assert "✓ now read" in read_state.set_email_read_status([102])


# ---------------------------------------------------------------------------
# Whole mail: "other" is every mailbox besides Inbox and Sent
# ---------------------------------------------------------------------------


def test_whole_mail_adds_other_mailboxes_once(mail, tmp_path):
    result = li.update(folders=li.FOLDERS)
    # 104 (Inbox/Notes) and 203 (Gmail, All Mail only) join; Gmail inbox and
    # sent mail stored in All Mail are not counted twice.
    assert result.total == 8
    _, ids, hits = _search(folder="other")
    assert sorted(ids) == [104, 203]
    assert {h.id: h.mailbox for h in hits} == {104: "Inbox/Notes", 203: "[Gmail]/All Mail"}
    assert _search(folder="both")[0] == 6 and _search()[0] == 8
    assert _search(mailbox="notes")[1] == [104]
    assert _search("archived")[1] == [203]


def test_a_move_between_other_mailboxes_updates_the_mailbox(mail, tmp_path):
    li.update(folders=li.FOLDERS)
    db = sqlite3.connect(str(tmp_path / "Envelope Index"))
    db.execute("INSERT INTO mailboxes VALUES (7, 'ews://U1/Archive', NULL)")
    db.execute("UPDATE messages SET mailbox = 7 WHERE ROWID = 104")
    db.execute("UPDATE messages SET mailbox = 7 WHERE ROWID = 102")  # out of the inbox
    db.commit()
    db.close()
    result = li.update(folders=li.FOLDERS)
    assert result.moved == 2 and result.removed == 0
    _, _, hits = _search(folder="other")
    assert {h.id: h.mailbox for h in hits} == {102: "Archive", 104: "Archive", 203: "[Gmail]/All Mail"}
    assert 102 not in _search(folder="inbox")[1]


def test_index_mail_all_scope(mail):
    from apple_mail_mcp.tools import mail_index

    assert "Messages in the index: 8" in mail_index.index_mail()
    out = mail_index.search_mail_index("archived")
    assert "ID: 203" in out and "[Gmail]/All Mail" in out
    assert mail_index.index_mail(scope="inbox,sent,other").startswith("LOCAL MAIL INDEX")
    assert mail_index.index_mail(scope="trash").startswith("Error")


def test_read_email_json_and_upstream_arguments(mail):
    import json

    from apple_mail_mcp.tools import mail_index

    data = json.loads(mail_index.read_email(" 101 ", output_format="json", account="Work", mailbox="INBOX"))
    assert data["subject"] == "Offert takbyte" and data["message_id"] == "101"
    assert "120 000 kr" in data["body"] and data["body_truncated"] is False
    assert data["read_status"] == "read"
    short = json.loads(mail_index.read_email(101, output_format="json", max_chars=5))
    assert short["body"] == "Hej, " and short["body_truncated"] is True
    for bad in (dict(message_id="7x"), dict(message_id="１２"), dict(message_id=101, max_chars=0),
                dict(message_id=101, output_format="xml")):
        assert mail_index.read_email(**bad).startswith("Error")
