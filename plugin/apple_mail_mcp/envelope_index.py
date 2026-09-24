"""Answer listing queries from Mail's Envelope Index instead of Mail.app.

AppleScript ``every message of <mailbox>``, ``count of messages``, bulk
property reads and whose-queries make Mail run a full database query on its
main thread (``-[MFMailbox(ScriptingSupport) messages]`` ->
``copyOfAllMessagesWithOptions``). On large mailboxes that hangs Mail and runs
into the osascript timeout. Mail keeps the same data in the SQLite database
``~/Library/Mail/V<n>/MailData/Envelope Index``; this module reads it directly,
read-only, and the tools format the rows exactly as their scripts did.

What still comes from Mail, because the database does not have it or has it
in a different form:

* account names: ``id of every account`` / ``name of every account``, once
  per process (``mail_accounts``). The mailbox urls carry the account UUID,
  which is the AppleScript ``id`` of the account. ~/Library/Accounts/
  Accounts4.sqlite also maps UUIDs to descriptions, but it has no name for
  sub-accounts (the description is on the parent) and does not see names
  changed in Mail's settings, so Mail's own answer is used;
* mailbox order and names (``mailbox_tree``), which only matter where a tool
  prints or walks every mailbox. Reading mailbox names is cheap; only reading
  messages is not;
* date strings: a tool that printed ``date as string`` gets the same text
  from ``osascript`` outside Mail (``date_strings``), because the format
  follows the user's locale settings.

Schema (Mail V10), columns this module relies on (validated at open):

* messages: ROWID (= AppleScript message id), sender -> addresses.ROWID,
  subject -> subjects.ROWID, subject_prefix ("Re: "), date_sent and
  date_received (unix seconds), mailbox -> mailboxes.ROWID, flags, read,
  flagged, deleted, conversation_id, global_message_id ->
  message_global_data.ROWID;
* message_global_data: message_id_header (the RFC Message-ID);
* mailboxes: ROWID, url (``imap://<ACCOUNT-UUID>/<percent-encoded path>``);
* labels(message_id, mailbox_id): Gmail keeps a message in "All Mail" and
  lists it in INBOX etc. through a label, so a mailbox holds the messages
  stored in it plus the ones labelled with it;
* recipients(message, address, type, position): type 0 is To, 1 is Cc;
* attachments(message): one row per attachment.

Deleted rows (``deleted = 1``) are left out, as Mail does. Mail's order for
``every message`` is newest first: ``date_received DESC, ROWID DESC``.

Any problem (no database, no Full Disk Access, an unexpected schema, an
account or mailbox that cannot be resolved) raises IndexUnavailable and the
tool runs its AppleScript unchanged. Why the database is not used is logged
to stderr once. Setting APPLE_MAIL_MCP_NO_INDEX=1 always uses AppleScript.
"""

import os
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from apple_mail_mcp import emlx

# Columns every query here depends on; a database without them is not used.
REQUIRED_COLUMNS = {
    "messages": {
        "ROWID", "sender", "subject", "subject_prefix", "date_sent", "date_received",
        "mailbox", "flags", "read", "flagged", "deleted", "conversation_id", "type",
        "global_message_id",
    },
    "subjects": {"ROWID", "subject"},
    "addresses": {"ROWID", "address", "comment"},
    "mailboxes": {"ROWID", "url"},
    "labels": {"message_id", "mailbox_id"},
    "recipients": {"message", "address", "type", "position"},
    "attachments": {"message"},
    "message_global_data": {"ROWID", "message_id_header"},
}

# The flag colour is stored in bits 39-41 of messages.flags (0 red ... 6
# gray, the AppleScript flag index); messages.flag_color only says "has one".
FLAG_COLOR_SHIFT = 39

RECIPIENT_TO = 0

# messages.type of a note that Apple Notes keeps in an IMAP "Notes" folder
MESSAGE_TYPE_NOTE = 2

# How long to wait before trying to open the database again after it failed.
REOPEN_INTERVAL_S = 60

_lock = threading.RLock()
_index: Optional["EnvelopeIndex"] = None
_failed_at: Optional[float] = None
_warned: set = set()
_accounts: Optional[List[Tuple[str, str]]] = None
_date_cache: Dict[int, str] = {}


class IndexUnavailable(Exception):
    """The Envelope Index cannot answer this query; use AppleScript."""


def _warn_once(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        print(f"apple-mail-mcp: not using Mail's Envelope Index ({reason}); using AppleScript", file=sys.stderr)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def fold(text: str) -> str:
    """Case-insensitive key, as AppleScript compares text by default."""
    return _nfc(text).casefold()


def contains_ci(haystack: Optional[str], needle: Optional[str]) -> bool:
    """AppleScript ``contains`` (case is ignored; nothing contains "")."""
    if not haystack or not needle:
        return False
    return fold(needle) in fold(haystack)


# A display name holding one of these is quoted, as Mail does ("Doe, Jane"
# <jane@...>). Mail leaves a name with a period unquoted.
_NAME_SPECIALS = set(',;:<>()[]@\\"')


def format_sender(address: Optional[str], name: Optional[str]) -> str:
    """The text AppleScript returns for ``sender of <message>``."""
    address = address or ""
    if not name:
        return address
    if _NAME_SPECIALS & set(name):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        name = f'"{escaped}"'
    return f"{name} <{address}>"


# ---------------------------------------------------------------------------
# Opening the database
# ---------------------------------------------------------------------------


def db_path() -> Optional[Path]:
    root = emlx._store_root()
    return root / "MailData" / "Envelope Index" if root is not None else None


@dataclass
class Message:
    id: int
    subject: str
    sender: str
    sender_address: str
    date_received: int
    date_sent: int
    read: bool
    flagged: bool
    flag_index: int  # -1 when not flagged, as read_flag_index_script
    internet_message_id: str
    mailbox_id: int
    conversation_id: int


@dataclass
class Mailbox:
    id: int
    account_uuid: str
    path: str  # decoded, NFC, e.g. "[Gmail]/All Mail"

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


_MESSAGE_COLUMNS = """
    m.ROWID, m.subject_prefix, s.subject, a.address, a.comment,
    m.date_received, m.date_sent, m.read, m.flagged, m.flags,
    g.message_id_header, m.mailbox, m.conversation_id
"""
_MESSAGE_JOINS = """
    FROM messages m
    LEFT JOIN subjects s ON s.ROWID = m.subject
    LEFT JOIN addresses a ON a.ROWID = m.sender
    LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id
"""


def _row_to_message(row) -> Message:
    (rowid, prefix, subject, address, comment, received, sent, read, flagged,
     flags, header, mailbox, conversation) = row
    flag_index = ((flags or 0) >> FLAG_COLOR_SHIFT) & 7 if flagged else -1
    return Message(
        id=rowid,
        subject=(prefix or "") + (subject or ""),
        sender=format_sender(address, comment),
        sender_address=address or "",
        date_received=received or 0,
        date_sent=sent or 0,
        read=bool(read),
        flagged=bool(flagged),
        flag_index=flag_index,
        internet_message_id=(header or "").strip().strip("<>"),
        mailbox_id=mailbox,
        conversation_id=conversation or 0,
    )


class EnvelopeIndex:
    """A read-only connection to Mail's Envelope Index."""

    BUSY_RETRIES = 5

    def __init__(self, path: Path):
        uri = f"file:{quote(str(path))}?mode=ro"
        # timeout makes SQLite wait out Mail's write locks (SQLITE_BUSY)
        self.conn = sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=False)
        self.conn.execute("PRAGMA query_only = ON")
        self.conn.create_function("contains_ci", 2, contains_ci, deterministic=True)
        self.conn.create_function("format_sender", 2, format_sender, deterministic=True)
        self._lock = threading.Lock()
        self._validate()

    def _validate(self) -> None:
        for table, columns in REQUIRED_COLUMNS.items():
            found = {row[1] for row in self.query(f"PRAGMA table_info({table})")}
            missing = columns - found
            if missing:
                raise IndexUnavailable(f"table {table} lacks {', '.join(sorted(missing))}")

    def query(self, sql: str, params: Sequence = ()) -> list:
        for attempt in range(self.BUSY_RETRIES):
            try:
                with self._lock:
                    return self.conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if ("locked" in text or "busy" in text) and attempt < self.BUSY_RETRIES - 1:
                    time.sleep(0.05 * 2 ** attempt)
                    continue
                raise IndexUnavailable(f"query failed: {exc}") from exc
        raise IndexUnavailable("database stayed busy")

    # -- mailboxes ---------------------------------------------------------

    def mailboxes(self) -> List[Mailbox]:
        found = []
        for rowid, url in self.query("SELECT ROWID, url FROM mailboxes ORDER BY ROWID"):
            scheme, sep, rest = (url or "").partition("://")
            if not sep or "/" not in rest:
                continue
            account_uuid, _, path = rest.partition("/")
            found.append(Mailbox(rowid, account_uuid, _nfc(unquote(path))))
        return found

    def all_mail_ids(self) -> set:
        """Ids of Gmail "All Mail" mailboxes, by role rather than name.

        A Gmail label mailbox (INBOX, Sent, ...) names the mailbox that
        stores its messages in mailboxes.source; that is All Mail. The
        schema has no other role (special-use) data, so other roles are
        known by name only (constants.SENT_MAILBOX_NAMES, SKIP_FOLDERS).
        """
        columns = {row[1] for row in self.query("PRAGMA table_info(mailboxes)")}
        if "source" not in columns:
            return set()
        return {row[0] for row in self.query("SELECT DISTINCT source FROM mailboxes WHERE source IS NOT NULL")}

    def find_mailbox(self, account_uuid: str, path: str) -> Optional[Mailbox]:
        """The mailbox Mail means by ``mailbox "<path>"`` of an account.

        A nested mailbox is not found by its name alone here (that is only
        for names Mail itself listed, see resolve_listed).
        """
        found = self._candidates(account_uuid, path, nested=False)
        return found[0] if found else None

    def resolve_listed(self, account_uuid: str, names: Sequence[str]) -> List[Optional[Mailbox]]:
        """The mailboxes behind the names of ``every mailbox of <account>``.

        Mail lists more than the top level there: every mailbox of an
        Exchange account, nested ones under their own name ("Notes" for
        "Inbox/Notes"), and the children of Gmail's "[Gmail]". Each listed
        name takes the best match not taken by an earlier name, so two
        listed "Notes" become "Notes" and "Inbox/Notes".
        """
        taken: set = set()
        resolved: List[Optional[Mailbox]] = []
        for name in names:
            match = next((m for m in self._candidates(account_uuid, name) if m.id not in taken), None)
            if match is not None:
                taken.add(match.id)
            resolved.append(match)
        return resolved

    def _candidates(self, account_uuid: str, path: str, nested: bool = True) -> List[Mailbox]:
        """Mailboxes *path* can mean, best first.

        1. the exact path ("INBOX", "Projects/2024");
        2. a child of a "[...]" container, which Mail shows at the top
           ("[Gmail]/All Mail" as "All Mail");
        3. a nested mailbox with that name ("Inbox/Projects" as
           "Projects"), oldest first.
        """
        wanted = fold(path)
        mailboxes = [m for m in self.mailboxes() if m.account_uuid == account_uuid]
        exact, container_child, nested_found = [], [], []
        for mailbox in mailboxes:
            parent, _, leaf = mailbox.path.rpartition("/")
            if fold(mailbox.path) == wanted:
                exact.append(mailbox)
            elif fold(leaf) == wanted and parent:
                if parent.startswith("[") and parent.endswith("]") and "/" not in parent:
                    container_child.append(mailbox)
                else:
                    nested_found.append(mailbox)
        return exact + container_child + (nested_found if nested else [])

    # -- messages ----------------------------------------------------------

    def messages(
        self,
        mailbox_ids: Iterable[int],
        *,
        unread_only: bool = False,
        read: Optional[bool] = None,
        received_after: Optional[float] = None,
        received_from: Optional[float] = None,
        received_to: Optional[float] = None,
        sent_after: Optional[float] = None,
        sender_contains: Optional[str] = None,
        subject_contains_any: Optional[Sequence[str]] = None,
        flagged: Optional[bool] = None,
        flag_index: Optional[int] = None,
        has_attachments: Optional[bool] = None,
        conversation_id: Optional[int] = None,
        ids: Optional[Iterable[int]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Message]:
        """Messages of the mailboxes, newest first (Mail's order)."""
        where, params = self._filters(
            mailbox_ids, unread_only, read, received_after, received_from, received_to,
            sent_after, sender_contains, subject_contains_any, flagged, flag_index,
            has_attachments, conversation_id, ids,
        )
        sql = f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_JOINS} WHERE {where} ORDER BY m.date_received DESC, m.ROWID DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(int(offset))
        return [_row_to_message(row) for row in self.query(sql, params)]

    def count(self, mailbox_ids: Iterable[int], **filters) -> int:
        where, params = self._filters(mailbox_ids, **{**_NO_FILTERS, **filters})
        return self.query(f"SELECT count(*) {_MESSAGE_JOINS} WHERE {where}", params)[0][0]

    def counts(self, mailbox_ids: Iterable[int]) -> Tuple[int, int]:
        """(messages, unread messages) of the mailboxes."""
        where, params = self._filters(mailbox_ids, **_NO_FILTERS)
        total, unread = self.query(
            f"SELECT count(*), coalesce(sum(m.read = 0), 0) {_MESSAGE_JOINS} WHERE {where}", params
        )[0]
        return total, unread

    def _filters(
        self, mailbox_ids, unread_only, read, received_after, received_from, received_to,
        sent_after, sender_contains, subject_contains_any, flagged, flag_index,
        has_attachments, conversation_id, ids,
    ) -> Tuple[str, list]:
        mailbox_ids = [int(i) for i in mailbox_ids]
        if not mailbox_ids:
            return "0", []
        marks = ",".join("?" * len(mailbox_ids))
        clauses = [
            "m.deleted = 0",
            # Stored in the mailbox, or (Gmail) labelled with it. Mail does not
            # list notes (type 2, Apple Notes over IMAP) in a label mailbox
            # such as "Notes", though All Mail, where they are stored, counts them.
            f"m.ROWID IN (SELECT ROWID FROM messages WHERE mailbox IN ({marks}) "
            f"UNION SELECT l.message_id FROM labels l JOIN messages lm ON lm.ROWID = l.message_id "
            f"WHERE l.mailbox_id IN ({marks}) AND coalesce(lm.type, 0) != {MESSAGE_TYPE_NOTE})",
        ]
        params: list = mailbox_ids + mailbox_ids
        if unread_only:
            clauses.append("m.read = 0")
        if read is not None:
            clauses.append("m.read = ?")
            params.append(1 if read else 0)
        if received_after is not None:
            clauses.append("m.date_received > ?")
            params.append(received_after)
        if received_from is not None:
            clauses.append("m.date_received >= ?")
            params.append(received_from)
        if received_to is not None:
            clauses.append("m.date_received <= ?")
            params.append(received_to)
        if sent_after is not None:
            clauses.append("m.date_sent > ?")
            params.append(sent_after)
        if sender_contains:
            clauses.append("contains_ci(format_sender(a.address, a.comment), ?)")
            params.append(sender_contains)
        if subject_contains_any:
            terms = [t for t in subject_contains_any if t]
            if terms:
                clauses.append(
                    "(" + " OR ".join("contains_ci(coalesce(m.subject_prefix, '') || s.subject, ?)" for _ in terms) + ")"
                )
                params += terms
        if flag_index is not None:
            clauses.append(f"m.flagged = 1 AND ((m.flags >> {FLAG_COLOR_SHIFT}) & 7) = ?")
            params.append(int(flag_index))
        elif flagged is not None:
            clauses.append("m.flagged = ?")
            params.append(1 if flagged else 0)
        if has_attachments is not None:
            exists = "EXISTS (SELECT 1 FROM attachments t WHERE t.message = m.ROWID)"
            clauses.append(exists if has_attachments else f"NOT {exists}")
        if conversation_id is not None:
            clauses.append("m.conversation_id = ?")
            params.append(int(conversation_id))
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return "0", []
            clauses.append(f"m.ROWID IN ({','.join('?' * len(ids))})")
            params += ids
        return " AND ".join(clauses), params

    def to_recipients(self, message_ids: Iterable[int]) -> Dict[int, List[Tuple[str, str]]]:
        """{message id: [(address, name), ...]} of the To recipients, in order."""
        ids = [int(i) for i in message_ids]
        out: Dict[int, List[Tuple[str, str]]] = {i: [] for i in ids}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = self.query(
                "SELECT r.message, a.address, a.comment FROM recipients r "
                "JOIN addresses a ON a.ROWID = r.address "
                f"WHERE r.type = ? AND r.message IN ({','.join('?' * len(chunk))}) "
                "ORDER BY r.message, r.position",
                [RECIPIENT_TO] + chunk,
            )
            for message, address, name in rows:
                out[message].append((address or "", name or ""))
        return out

    def with_attachments(self, message_ids: Iterable[int]) -> set:
        """The ids among *message_ids* that have at least one attachment."""
        ids = [int(i) for i in message_ids]
        found = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update(
                row[0] for row in self.query(
                    f"SELECT DISTINCT message FROM attachments WHERE message IN ({','.join('?' * len(chunk))})",
                    chunk,
                )
            )
        return found


_NO_FILTERS = dict(
    unread_only=False, read=None, received_after=None, received_from=None, received_to=None,
    sent_after=None, sender_contains=None, subject_contains_any=None, flagged=None,
    flag_index=None, has_attachments=None, conversation_id=None, ids=None,
)


def get_index() -> EnvelopeIndex:
    """The open Envelope Index, or IndexUnavailable (after logging why once)."""
    global _index, _failed_at
    if os.environ.get("APPLE_MAIL_MCP_NO_INDEX"):
        raise IndexUnavailable("APPLE_MAIL_MCP_NO_INDEX is set")
    with _lock:
        if _index is not None:
            return _index
        if _failed_at is not None and time.monotonic() - _failed_at < REOPEN_INTERVAL_S:
            raise IndexUnavailable("database unavailable")
        path = db_path()
        try:
            if path is None or not path.is_file():
                raise IndexUnavailable("no Envelope Index found under ~/Library/Mail")
            try:
                _index = EnvelopeIndex(path)
            except sqlite3.Error as exc:
                raise IndexUnavailable(f"cannot open it read-only: {exc}; grant Full Disk Access") from exc
        except IndexUnavailable as exc:
            _failed_at = time.monotonic()
            _warn_once(str(exc))
            raise
        _failed_at = None
        return _index


def reset() -> None:
    """Forget the open database, the account map and cached date strings."""
    global _index, _failed_at, _accounts
    with _lock:
        if _index is not None:
            _index.conn.close()
        _index = None
        _failed_at = None
        _accounts = None
        _date_cache.clear()


# ---------------------------------------------------------------------------
# What only Mail knows: account names, mailbox order, date formatting
# ---------------------------------------------------------------------------


def mail_accounts() -> List[Tuple[str, str]]:
    """[(account name, account UUID), ...] in Mail's order, asked once per process."""
    global _accounts
    if _accounts is not None:
        return _accounts
    from apple_mail_mcp.core import AS_FIELD_SEP, AS_RECORD_SEP, FIELD_SEP, RECORD_SEP, run_applescript

    script = f"""
    tell application "Mail"
        set accountIds to id of every account
        set accountNames to name of every account
        set outLines to {{}}
        repeat with i from 1 to count of accountIds
            set end of outLines to (item i of accountIds as string) & {AS_FIELD_SEP} & (item i of accountNames as string)
        end repeat
        set AppleScript's text item delimiters to {AS_RECORD_SEP}
        return outLines as string
    end tell
    """
    try:
        output = run_applescript(script, timeout=30)
    except Exception as exc:
        raise IndexUnavailable(f"cannot list Mail accounts: {exc}") from exc
    accounts = []
    for record in output.split(RECORD_SEP):
        uuid, sep, name = record.partition(FIELD_SEP)
        if sep and uuid:
            accounts.append((name, uuid.strip()))
    _accounts = accounts
    return accounts


def account_uuid(name: str) -> str:
    """The UUID of the account called *name* (as AppleScript ``account "X"``)."""
    wanted = fold(name)
    for account_name, uuid in mail_accounts():
        if fold(account_name) == wanted:
            return uuid
    raise IndexUnavailable(f"unknown account {name!r}")


def mailbox_tree(account: Optional[str] = None, with_subs: bool = True) -> List[Tuple[str, List[Tuple[str, List[str]]]]]:
    """[(account, [(mailbox name, [sub-mailbox names])])] in Mail's order.

    Only names are read (``every mailbox``), never messages.
    """
    from apple_mail_mcp.core import AS_FIELD_SEP, AS_RECORD_SEP, FIELD_SEP, RECORD_SEP, escape_applescript, run_applescript

    if account:
        accounts = f'{{account "{escape_applescript(account)}"}}'
    else:
        accounts = "every account"
    subs = ""
    if with_subs:
        subs = """
                    try
                        set subNames to name of every mailbox of aMailbox
                    end try"""
    script = f"""
    tell application "Mail"
        set outLines to {{}}
        repeat with anAccount in {accounts}
            set accountName to name of anAccount
            set end of outLines to accountName
            try
                repeat with aMailbox in (every mailbox of anAccount)
                    set subNames to {{}}{subs}
                    set AppleScript's text item delimiters to {AS_FIELD_SEP}
                    set end of outLines to accountName & {AS_FIELD_SEP} & (name of aMailbox) & {AS_FIELD_SEP} & (subNames as string)
                    set AppleScript's text item delimiters to ""
                end repeat
            end try
        end repeat
        set AppleScript's text item delimiters to {AS_RECORD_SEP}
        return outLines as string
    end tell
    """
    try:
        output = run_applescript(script, timeout=60)
    except Exception as exc:
        raise IndexUnavailable(f"cannot list mailboxes: {exc}") from exc
    tree: List[Tuple[str, List[Tuple[str, List[str]]]]] = []
    for record in output.split(RECORD_SEP):
        parts = record.split(FIELD_SEP)
        if len(parts) == 1:
            tree.append((parts[0], []))
        elif tree:
            tree[-1][1].append((parts[1], [p for p in parts[2:] if p]))
    return tree


def date_strings(timestamps: Iterable[int]) -> Dict[int, str]:
    """{unix time: the text of AppleScript ``<date> as string``}.

    The format follows the user's language and region settings, so it is
    produced by osascript itself (without talking to Mail) and cached.
    """
    wanted = {int(t) for t in timestamps}
    missing = sorted(t for t in wanted if t not in _date_cache)
    for start in range(0, len(missing), 400):
        chunk = missing[start:start + 400]
        formatted = _format_dates_via_osascript(chunk)
        if len(formatted) != len(chunk):
            raise IndexUnavailable("osascript returned the wrong number of dates")
        _date_cache.update(zip(chunk, formatted))
    return {t: _date_cache[t] for t in wanted}


def _format_dates_via_osascript(timestamps: List[int]) -> List[str]:
    from apple_mail_mcp.core import AS_RECORD_SEP, RECORD_SEP, _run_applescript_unlocked

    calls = []
    for t in timestamps:
        d = datetime.fromtimestamp(t)
        seconds = d.hour * 3600 + d.minute * 60 + d.second
        calls.append(f"set end of out to my fmt({d.year}, {d.month}, {d.day}, {seconds})")
    script = f"""
    on fmt(y, m, d, s)
        set dt to current date
        set day of dt to 1
        set year of dt to y
        set month of dt to m
        set day of dt to d
        set time of dt to s
        return dt as string
    end fmt
    set out to {{}}
    {chr(10).join(calls)}
    set AppleScript's text item delimiters to {AS_RECORD_SEP}
    return out as string
    """
    try:
        # Not addressed to Mail, so the Mail lock is not taken.
        output = _run_applescript_unlocked(script, 30)
    except Exception as exc:
        raise IndexUnavailable(f"cannot format dates: {exc}") from exc
    return output.split(RECORD_SEP) if output else []


def iso_datetime(timestamp: int) -> str:
    """Local time as YYYY-MM-DDTHH:MM:SS, like the search script's iso_datetime."""
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%dT%H:%M:%S")


def days_back_cutoff(days_back: int) -> Optional[float]:
    """Unix time of ``(current date) - (days_back * days)``, or None for 0."""
    return time.time() - days_back * 86400 if days_back > 0 else None


def find_inbox(index: EnvelopeIndex, uuid: str) -> Optional[Mailbox]:
    """The inbox as inbox_mailbox_script finds it (localised names in order)."""
    from apple_mail_mcp.core import INBOX_NAMES

    for name in INBOX_NAMES:
        mailbox = index.find_mailbox(uuid, name)
        if mailbox is not None:
            return mailbox
    return None


def resolve_mailbox(index: EnvelopeIndex, uuid: str, name: str) -> Mailbox:
    """``mailbox "<name>"`` of an account, with the scripts' INBOX -> Inbox fallback."""
    mailbox = index.find_mailbox(uuid, name)
    if mailbox is None and name == "INBOX":
        mailbox = index.find_mailbox(uuid, "Inbox")
    if mailbox is None:
        raise IndexUnavailable(f"mailbox {name!r} not in the index")
    return mailbox
