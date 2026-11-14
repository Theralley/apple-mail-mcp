"""A local full-text index of all mail (or only Inbox and Sent), kept on this Mac only.

Searching through Mail with AppleScript takes 4-15 s per mailbox and up to
100 s (incomplete) across accounts. This index answers in milliseconds. It is
built from Mail's Envelope Index and the message files on disk; AppleScript
never reads a body here, and nothing leaves the Mac.

* Where: ``~/Library/Caches/apple-mail-mcp/mail-index.sqlite3`` (folder 0700,
  file 0600; Time Machine skips Caches). APPLE_MAIL_MCP_INDEX_PATH moves it.
* What: per message its subject, sender, recipients (To/Cc), attachment
  names, dates and the body text, in an FTS5 table (``unicode61`` with
  diacritics kept, so "får" and "far" stay different words). Folder groups:
  "inbox", "sent" and "other" (every other mailbox: archives, subfolders,
  Drafts, Trash, Junk). Read/unread is never stored; searches read it live
  from Mail's database (attach_mail_state).
* How: newest first, in batches committed one by one, within a time budget,
  so a large first run resumes where it stopped. One writer at a time
  (an flock next to the database); readers never wait for it (WAL).
* A message whose body is not on disk yet (Mail has not downloaded it) is
  indexed without a body, counted as missing, and retried on the next run.
"""

import contextlib
import fcntl
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from apple_mail_mcp import emlx
from apple_mail_mcp import envelope_index as ei

SCHEMA_VERSION = "2"
BATCH = 500
MAX_BODY_CHARS = 50_000
FULL_CHECK_S = 600  # refresh compares every message at least this often
FOLDERS = ("inbox", "sent", "other")
DEFAULT_FOLDERS = ("inbox", "sent")


class MailIndexError(Exception):
    """A problem with the local index; the text is meant for the user."""


class IndexBusy(MailIndexError):
    """Another process is updating the index right now."""


def index_path() -> Path:
    override = os.environ.get("APPLE_MAIL_MCP_INDEX_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Caches" / "apple-mail-mcp" / "mail-index.sqlite3"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,           -- Mail's message id (Envelope Index ROWID)
    account_uuid TEXT NOT NULL,
    account TEXT NOT NULL,
    folders TEXT NOT NULL,            -- "inbox", "sent", "inbox,sent" or "other"
    mailbox TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    sender_address TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '',
    date_received INTEGER NOT NULL DEFAULT 0,
    date_sent INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    body_state TEXT NOT NULL,         -- ok | missing | empty
    indexed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_date ON docs(date_received DESC);
CREATE INDEX IF NOT EXISTS docs_account ON docs(account_uuid);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    subject, sender, recipients, attachments, body,
    tokenize = "unicode61 remove_diacritics 0"
);
"""


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the index for writing, creating it (private) when needed.

    Only under writer_lock: this may replace an index of another version.
    Searches use connect_reader, which never writes.
    """
    path = path or index_path()
    _private_dir(path.parent)
    conn = _open(path)
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.OperationalError:
        version = None
    if version is not None and version[0] != SCHEMA_VERSION:
        # An index from another version is rebuilt from scratch, in a new file
        # (dropping tables would leave the old pages behind).
        conn.close()
        _unlink_files(path)
        conn = _open(path)
    try:
        conn.executescript(_SCHEMA)
    except sqlite3.OperationalError as exc:
        raise MailIndexError(f"cannot create the index ({exc}); this Python's SQLite needs FTS5") from exc
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def connect_reader(path: Optional[Path] = None) -> sqlite3.Connection:
    """A read-only connection; it never takes SQLite's write lock (WAL readers
    do not wait for a writer)."""
    from urllib.parse import quote

    path = path or index_path()
    if not path.exists():
        raise MailIndexError("There is no local mail index yet. Run index_mail first.")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("contains_ci", 2, ei.contains_ci, deterministic=True)
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        version = None
    if version is None or version[0] != SCHEMA_VERSION:
        conn.close()
        raise MailIndexError("The local mail index is from another version or incomplete; run index_mail to rebuild it.")
    return conn


def attach_mail_state(conn: sqlite3.Connection) -> bool:
    """Attach Mail's Envelope Index read-only as ``mail``, for live read/unread state.

    The index keeps no read flag of its own: a copy would be stale the moment
    the message is opened in Mail or on the phone. False when Mail's database
    cannot be opened (then searches work without read state).
    """
    from urllib.parse import quote

    if _has_mail_state(conn):
        return True
    try:
        path = getattr(ei.get_index(), "path", None)
    except ei.IndexUnavailable:
        return False
    if path is None:
        return False
    try:
        conn.execute("ATTACH DATABASE ? AS mail", (f"file:{quote(str(path))}?mode=ro",))
    except sqlite3.Error:
        return False
    try:
        conn.execute("SELECT ROWID, read, deleted FROM mail.messages LIMIT 0")
    except sqlite3.Error:
        conn.execute("DETACH DATABASE mail")
        return False
    return True


def _has_mail_state(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "mail" for row in conn.execute("PRAGMA database_list"))


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except OSError:
            pass
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.create_function("contains_ci", 2, ei.contains_ci, deterministic=True)
    return conn


def _unlink_files(path: Path) -> List[str]:
    removed = []
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
            removed.append(target.name)
    return removed


@contextlib.contextmanager
def writer_lock(path: Optional[Path] = None):
    """Exclusive right to update the index, or IndexBusy at once."""
    path = path or index_path()
    _private_dir(path.parent)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IndexBusy("Another process is updating the mail index right now; try again in a minute.") from None
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _meta(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def delete_index(path: Optional[Path] = None) -> List[str]:
    """Remove the index files; returns the names removed."""
    path = path or index_path()
    with writer_lock(path):
        return _unlink_files(path)


# ---------------------------------------------------------------------------
# What to index: Mail's own view of Inbox and Sent, and everything else
# ---------------------------------------------------------------------------


@dataclass
class Wanted:
    message: "ei.Message"
    account_uuid: str
    account: str
    mailbox: str
    folders: Set[str] = field(default_factory=set)


def _sent_box(index, uuid):
    from apple_mail_mcp.constants import SENT_MAILBOX_NAMES

    for name in SENT_MAILBOX_NAMES:
        box = index.find_mailbox(uuid, name)
        if box is not None:
            return box
    return None


def select_accounts(names: Optional[Sequence[str]]) -> List[Tuple[str, str]]:
    """[(name, uuid)] of Mail's accounts, or of the ones named."""
    accounts = ei.mail_accounts()
    if not names:
        return list(accounts)
    chosen = []
    for wanted in names:
        match = next(((n, u) for n, u in accounts if ei.fold(n) == ei.fold(wanted)), None)
        if match is None:
            raise MailIndexError(f"Unknown account {wanted!r}. Accounts: {', '.join(n for n, _ in accounts)}")
        if match not in chosen:
            chosen.append(match)
    return chosen


def wanted_messages(accounts: Sequence[Tuple[str, str]], folders: Sequence[str]) -> Tuple[Dict[int, Wanted], List[str]]:
    """{message id: Wanted} for the folders of the accounts, plus notes on missing mailboxes.

    "other" is every message of the account that is in neither its inbox
    nor its sent mailbox (for Gmail: not labelled with them), wherever it is
    stored; its mailbox is the one that stores it.
    """
    index = ei.get_index()
    paths = {box.id: box.path for box in index.mailboxes()}
    wanted: Dict[int, Wanted] = {}
    notes = []
    for name, uuid in accounts:
        taken: Set[int] = set()
        for folder in ("inbox", "sent"):
            if folder not in folders and "other" not in folders:
                continue
            box = ei.find_inbox(index, uuid) if folder == "inbox" else _sent_box(index, uuid)
            if box is None:
                if folder in folders:
                    notes.append(f"{name}: no {folder} mailbox")
                continue
            for message in index.messages([box.id]):
                taken.add(message.id)
                if folder not in folders:
                    continue
                item = wanted.get(message.id)
                if item is None:
                    item = wanted[message.id] = Wanted(message, uuid, name, box.path)
                item.folders.add(folder)
        if "other" in folders:
            stores = [box_id for box_id, _ in _account_boxes(index, uuid)]
            for message in index.messages(stores):
                if message.id in taken or message.id in wanted:
                    continue
                wanted[message.id] = Wanted(message, uuid, name, paths.get(message.mailbox_id, ""), {"other"})
    return wanted, notes


def _account_boxes(index, uuid: str) -> List[Tuple[int, str]]:
    return [(box.id, box.path) for box in index.mailboxes() if box.account_uuid == uuid]


def _bulk(index, sql: str, ids: List[int]) -> list:
    rows = []
    for start in range(0, len(ids), 900):  # SQLite's parameter limit
        chunk = ids[start:start + 900]
        rows += index.query(sql.format(marks=",".join("?" * len(chunk))), chunk)
    return rows


def _recipients(index, ids: List[int]) -> Dict[int, str]:
    found: Dict[int, List[str]] = {}
    rows = _bulk(index, (
        "SELECT r.message, a.address, a.comment FROM recipients r JOIN addresses a ON a.ROWID = r.address "
        "WHERE r.message IN ({marks}) ORDER BY r.message, r.type, r.position"), ids)
    for message, address, comment in rows:
        found.setdefault(message, []).append(ei.format_sender(address, comment))
    return {k: ", ".join(v) for k, v in found.items()}


def _attachment_names(index, ids: List[int]) -> Dict[int, str]:
    found: Dict[int, List[str]] = {}
    for message, name in _bulk(index, "SELECT message, name FROM attachments WHERE message IN ({marks}) ORDER BY ROWID", ids):
        if name:
            found.setdefault(message, []).append(name)
    return {k: ", ".join(v) for k, v in found.items()}


def _body(message_id: int) -> Tuple[str, str]:
    """(state, text) of the body on disk; never asks Mail."""
    path = emlx.find_message_file(message_id)
    if path is None:
        return "missing", ""
    try:
        text = emlx.body_text(emlx.parse_emlx(path.read_bytes()))
    except (OSError, ValueError, LookupError):
        return "missing", ""
    if text is None:
        return "missing", ""
    text = text.strip()
    return ("ok" if text else "empty"), text[:MAX_BODY_CHARS]


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------


@dataclass
class UpdateResult:
    added: int = 0
    retried: int = 0
    removed: int = 0
    moved: int = 0
    remaining: int = 0
    total: int = 0
    missing_bodies: int = 0
    seconds: float = 0.0
    notes: List[str] = field(default_factory=list)


def update(
    accounts: Optional[Sequence[str]] = None,
    folders: Sequence[str] = DEFAULT_FOLDERS,
    rebuild: bool = False,
    max_seconds: float = 120.0,
    path: Optional[Path] = None,
) -> UpdateResult:
    """Bring the index up to date with Mail (newest messages first)."""
    started = time.monotonic()
    folders = [f for f in FOLDERS if f in set(folders)]
    if not folders:
        raise MailIndexError("scope must name inbox, sent and/or other")
    chosen = select_accounts(accounts)
    path = path or index_path()
    with writer_lock(path):
        conn = connect(path)
        try:
            _remember_scope(conn, [n for n, _ in chosen] if accounts else None, folders)
            full = _covers_remembered_scope(conn, [n for n, _ in chosen] if accounts else None, folders)
            return _update_locked(conn, chosen, folders, rebuild, max_seconds, started, full)
        finally:
            conn.close()


def _covers_remembered_scope(conn, account_names: Optional[List[str]], folders: Sequence[str]) -> bool:
    """Whether this run looks at everything refresh keeps current, so that
    "Mail has nothing new since" may be recorded afterwards."""
    remembered = _meta(conn, "scope_accounts") or ""
    remembered_folders = set(filter(None, (_meta(conn, "scope_folders") or "").split(",")))
    if not remembered_folders <= set(folders):
        return False
    if account_names is None:
        return True
    if remembered == "*":
        return False
    return {ei.fold(n) for n in remembered.split("\n") if n} <= {ei.fold(n) for n in account_names}


def _remember_scope(conn, account_names: Optional[List[str]], folders: Sequence[str]) -> None:
    """Widen the stored scope (what refresh keeps current) by this run's scope."""
    old_accounts = _meta(conn, "scope_accounts")
    if account_names is None or old_accounts == "*":
        accounts_value = "*"
    else:
        known = set(filter(None, (old_accounts or "").split("\n")))
        accounts_value = "\n".join(sorted(known | set(account_names)))
    known_folders = set(filter(None, (_meta(conn, "scope_folders") or "").split(",")))
    _set_meta(conn, "scope_accounts", accounts_value)
    _set_meta(conn, "scope_folders", ",".join(f for f in FOLDERS if f in known_folders | set(folders)))
    conn.commit()


def refresh(max_seconds: float, path: Optional[Path] = None) -> Optional[UpdateResult]:
    """Top up the index for the scope it was built with; None if it has none yet."""
    path = path or index_path()
    if not path.exists():
        return None
    conn = connect_reader(path)
    try:
        accounts = _meta(conn, "scope_accounts")
        folders = [f for f in (_meta(conn, "scope_folders") or "").split(",") if f]
    finally:
        conn.close()
    if not accounts or not folders:
        return None
    if not new_mail_since_last_check(path):
        return UpdateResult(total=-1)
    names = None if accounts == "*" else [n for n in accounts.split("\n") if n]
    return update(names, folders, max_seconds=max_seconds, path=path)


def new_mail_since_last_check(path: Optional[Path] = None) -> bool:
    """False when Mail has added no message since the last complete update
    and that update is recent (deletions and moves wait for the next full check)."""
    conn = connect_reader(path or index_path())
    try:
        seen = _meta(conn, "mail_max_rowid")
        checked = _meta(conn, "checked_at")
        missing = conn.execute("SELECT count(*) FROM docs WHERE body_state = 'missing'").fetchone()[0]
    finally:
        conn.close()
    if seen is None or checked is None or time.time() - int(checked) > FULL_CHECK_S:
        return True
    return _mail_max_rowid() != int(seen) or (missing and time.time() - int(checked) > 60)


def _mail_max_rowid() -> int:
    return int(ei.get_index().query("SELECT coalesce(max(ROWID), 0) FROM messages")[0][0])


def _update_locked(conn, chosen, folders, rebuild, max_seconds, started, full_scope=True) -> UpdateResult:
    result = UpdateResult()
    seen_rowid = _mail_max_rowid()  # before reading, so nothing slips between
    wanted, result.notes = wanted_messages(chosen, folders)
    uuids = {uuid for _, uuid in chosen}
    scope = set(folders)

    if rebuild:
        marks = ",".join("?" * len(uuids))
        ids = [r[0] for r in conn.execute(f"SELECT id FROM docs WHERE account_uuid IN ({marks})", list(uuids))]
        _delete(conn, ids)
        conn.commit()

    existing = {
        row[0]: (set(row[1].split(",")), row[2], row[3], row[4])
        for row in conn.execute("SELECT id, folders, body_state, account_uuid, mailbox FROM docs")
    }

    # Messages gone from these folders (deleted, moved, re-numbered by Exchange)
    gone, moved = [], []
    reconciled: Dict[int, Set[str]] = {}
    for doc_id, (doc_folders, _, doc_uuid, doc_box) in existing.items():
        item = wanted.get(doc_id)
        box = doc_box
        if item is None:
            if doc_uuid not in uuids or not (doc_folders & scope):
                continue
            keep = doc_folders - scope
        else:
            keep = (doc_folders - scope) | item.folders
            if not (doc_folders - scope):  # this run sees all of it: take its mailbox
                box = item.mailbox
        reconciled[doc_id] = keep
        if not keep:
            gone.append(doc_id)
        elif keep != doc_folders or box != doc_box:
            moved.append((",".join(sorted(keep)), box, doc_id))
    _delete(conn, gone)
    conn.executemany("UPDATE docs SET folders = ?, mailbox = ? WHERE id = ?", moved)
    conn.commit()
    result.removed, result.moved = len(gone), len(moved)

    def newest_first(ids):
        return sorted(ids, key=lambda i: (wanted[i].message.date_received, i), reverse=True)

    # Messages never indexed come first; bodies still missing are retried
    # after them, so a pile of undownloaded mail cannot starve older mail.
    fresh = newest_first(i for i in wanted if i not in existing)
    retry = newest_first(i for i in wanted if i in existing and existing[i][1] == "missing")
    todo = fresh + retry

    index = ei.get_index()
    done = 0
    for start in range(0, len(todo), BATCH):
        if time.monotonic() - started > max_seconds:
            break
        batch = todo[start:start + BATCH]
        recipients = _recipients(index, batch)
        attachments = _attachment_names(index, batch)
        now = int(time.time())
        for doc_id in batch:
            item = wanted[doc_id]
            state, body = _body(doc_id)
            m = item.message
            doc_folders = reconciled.get(doc_id) or item.folders
            doc = (
                doc_id, item.account_uuid, item.account, ",".join(sorted(doc_folders)), item.mailbox,
                m.subject or "", m.sender or "", m.sender_address or "", recipients.get(doc_id, ""),
                attachments.get(doc_id, ""), m.date_received or 0, m.date_sent or 0,
                m.internet_message_id or "", state, now,
            )
            conn.execute("DELETE FROM fts WHERE rowid = ?", (doc_id,))
            conn.execute(
                "INSERT OR REPLACE INTO docs(id, account_uuid, account, folders, mailbox, subject, sender, "
                "sender_address, recipients, attachments, date_received, date_sent, message_id, body_state, "
                "indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", doc)
            conn.execute(
                "INSERT INTO fts(rowid, subject, sender, recipients, attachments, body) VALUES (?,?,?,?,?,?)",
                (doc_id, doc[5], doc[6] + " " + doc[7], doc[8], doc[9], body))
        _set_meta(conn, "updated_at", int(time.time()))
        conn.commit()
        done += len(batch)

    result.added = min(done, len(fresh))
    result.retried = done - result.added
    result.remaining = len(todo) - done
    if not result.remaining and full_scope:
        _set_meta(conn, "mail_max_rowid", seen_rowid)
        _set_meta(conn, "checked_at", int(time.time()))
        conn.commit()
    result.total = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    result.missing_bodies = conn.execute("SELECT count(*) FROM docs WHERE body_state = 'missing'").fetchone()[0]
    result.seconds = time.monotonic() - started
    return result


def _delete(conn, ids: Iterable[int]) -> None:
    ids = list(ids)
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM fts WHERE rowid IN ({marks})", chunk)
        conn.execute(f"DELETE FROM docs WHERE id IN ({marks})", chunk)


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------

FIELD_ALIASES = {
    "subject": "subject", "ämne": "subject",
    "from": "sender", "från": "sender", "sender": "sender",
    "to": "recipients", "till": "recipients", "cc": "recipients",
    "attachment": "attachments", "bilaga": "attachments", "file": "attachments",
    "body": "body", "text": "body",
}
_TOKEN_RE = re.compile(r'(?:(\w+):)?(?:"([^"]*)"|(\S+))', re.UNICODE)


def match_expression(query: str) -> str:
    """An FTS5 MATCH string for a search box query; every term is quoted.

    Terms are ANDed. ``"two words"`` is a phrase, ``offer*`` a prefix and
    ``from:anna`` / ``subject:offert`` / ``attachment:pdf`` limit a term to
    one field. FTS5 operators typed by the caller are taken literally.
    """
    parts = []
    for fieldname, phrase, word in _TOKEN_RE.findall(query or ""):
        column = FIELD_ALIASES.get(fieldname.casefold()) if fieldname else None
        if fieldname and column is None:
            word = f"{fieldname}:{word}" if word else word
            phrase = f"{fieldname}: {phrase}" if phrase else phrase
        text = phrase if phrase else word
        prefix = False
        if not phrase and text.endswith("*") and len(text.rstrip("*")) >= 2:
            text, prefix = text.rstrip("*"), True
        text = text.replace('"', '""').strip()
        if not re.search(r"\w", text):
            continue
        term = f'"{text}"' + ("*" if prefix else "")
        parts.append(f"{column} : {term}" if column else term)
    return " AND ".join(parts)


def parse_day(value: Optional[str], end: bool = False) -> Optional[int]:
    """Unix time of the start (or end) of a local YYYY-MM-DD day."""
    if not value:
        return None
    try:
        day = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise MailIndexError(f"Dates are YYYY-MM-DD, got {value!r}.") from None
    if end:
        day += timedelta(days=1)
    return int(day.timestamp()) - (1 if end else 0)


@dataclass
class Hit:
    id: int
    account: str
    folders: str
    subject: str
    sender: str
    recipients: str
    attachments: str
    date_received: int
    body_state: str
    snippet: str
    mailbox: str = ""
    read: Optional[bool] = None  # live from Mail; None when unknown


def search(
    conn: sqlite3.Connection,
    query: str = "",
    folder: str = "all",
    account: Optional[str] = None,
    sender: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    has_attachment: Optional[bool] = None,
    sort: str = "date",
    limit: int = 20,
    offset: int = 0,
    read: Optional[bool] = None,
    mailbox: Optional[str] = None,
) -> Tuple[int, List[Hit]]:
    """(total matches, one page of hits).

    *read* False keeps unread messages only, True read ones only. Read state
    comes live from Mail's database when attach_mail_state attached it.
    """
    where, params = [], []
    live = _has_mail_state(conn)
    if read is not None:
        if not live:
            raise MailIndexError("Read/unread state needs Mail's own database, which cannot be read now.")
        # A message deleted in Mail since the last refresh has no state.
        where.append("em.deleted = 0 AND em.read = ?")
        params.append(1 if read else 0)
    expression = match_expression(query)
    if query and query.strip() and not expression:
        raise MailIndexError("The query has no words to search for.")
    folder = (folder or "all").casefold()
    if folder in FOLDERS:
        where.append("instr(d.folders, ?) > 0")
        params.append(folder)
    elif folder == "both":
        where.append("(instr(d.folders, 'inbox') > 0 OR instr(d.folders, 'sent') > 0)")
    elif folder != "all":
        raise MailIndexError("folder is inbox, sent, both (inbox and sent), other or all.")
    if mailbox and mailbox.strip():
        where.append("contains_ci(d.mailbox, ?)")
        params.append(mailbox.strip())
    if account:
        where.append("lower(d.account) = lower(?)")
        params.append(account.strip())
    if sender:
        where.append("(contains_ci(d.sender, ?) OR contains_ci(d.sender_address, ?))")
        params += [sender, sender]
    start, end = parse_day(date_from), parse_day(date_to, end=True)
    if start is not None:
        where.append("d.date_received >= ?")
        params.append(start)
    if end is not None:
        where.append("d.date_received <= ?")
        params.append(end)
    if has_attachment is not None:
        where.append("d.attachments != ''" if has_attachment else "d.attachments = ''")

    order = "d.date_received DESC, d.id DESC"
    if expression:
        source = "fts JOIN docs d ON d.id = fts.rowid"
        where.insert(0, "fts MATCH ?")
        params.insert(0, expression)
        snippet = "snippet(fts, 4, '[', ']', '…', 14)"
        if sort == "relevance":
            order = "bm25(fts, 8.0, 4.0, 2.0, 4.0, 1.0), d.date_received DESC"
    else:
        source = "docs d"
        snippet = "''"
    state = "NULL"
    if live:
        source += " LEFT JOIN mail.messages em ON em.ROWID = d.id"
        state = "CASE WHEN em.deleted = 0 THEN em.read END"
    clause = " AND ".join(where) or "1"
    limit = max(1, min(int(limit), 200))
    try:
        total = conn.execute(f"SELECT count(*) FROM {source} WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT d.id, d.account, d.folders, d.subject, d.sender, d.recipients, d.attachments, "
            f"d.date_received, d.body_state, {snippet}, d.mailbox, {state} FROM {source} WHERE {clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, max(0, int(offset))],
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise MailIndexError(f"search failed: {exc}") from exc
    return total, [
        Hit(*row[:-2], mailbox=row[-2], read=None if row[-1] is None else bool(row[-1])) for row in rows
    ]


def status(conn: sqlite3.Connection) -> Dict[str, object]:
    rows = conn.execute(
        "SELECT account, sum(instr(folders, 'inbox') > 0), sum(instr(folders, 'sent') > 0), "
        "sum(body_state = 'missing') FROM docs GROUP BY account ORDER BY account"
    ).fetchall()
    updated = _meta(conn, "updated_at")
    unread = None
    if _has_mail_state(conn):
        unread = conn.execute(
            "SELECT count(*) FROM docs d JOIN mail.messages em ON em.ROWID = d.id "
            "WHERE instr(d.folders, 'inbox') > 0 AND em.deleted = 0 AND em.read = 0"
        ).fetchone()[0]
    return {
        "accounts": rows,
        "total": conn.execute("SELECT count(*) FROM docs").fetchone()[0],
        "updated_at": int(updated) if updated else None,
        "unread_inbox": unread,
    }
