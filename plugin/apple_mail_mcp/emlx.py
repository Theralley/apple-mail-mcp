"""Read message bodies from Mail's on-disk store instead of asking Mail.app.

Asking Mail for ``content of <message>`` makes Mail convert HTML to plain text
on its main thread through legacy WebKit (NSHTMLReader). That costs roughly
0.35-0.7 s per message, blocks every other client while it runs, and is the
frame in Mail's recurring ExcUserFault crash reports. This server therefore
never requests ``content``. AppleScript returns message ids and metadata only,
and the body is read here:

1. from ``~/Library/Mail/V<n>/<account>/**/<Box>.mbox/<uuid>/Data/<sub>/
   Messages/<id>.emlx`` (or ``<id>.partial.emlx``), where ``V<n>`` is the
   newest store that holds ``MailData/Envelope Index`` (ids of older stores
   mean other messages) and ``<sub>`` is the digits of ``id // 1000``
   reversed, one per directory level (empty below 1000);
2. if the file is missing, unreadable or incomplete (no Full Disk Access, not
   downloaded, truncated, a partial file without a text part), from
   ``source of <message>`` over AppleScript: raw MIME, which Mail returns
   without any HTML conversion.

Both are parsed with the stdlib email package. Every inline text/plain part
is used in order; HTML parts are converted to text with html.parser only when
there is no plain text.

Tool scripts that need a body inline in their output emit a token built by
``body_token_script`` and the caller replaces it with ``fill_body_tokens``,
passing the same per-call nonce from ``new_body_nonce`` so text inside a
message (a subject, say) can never forge a token.
"""

import email
import os
import re
import secrets
import time
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional

MAIL_DIR = Path(os.environ.get("APPLE_MAIL_DIR", Path.home() / "Library" / "Mail"))

# A missing id rescans the store at most this often (the first lookup always
# scans): Mail may have written the file since, but a tree walk per miss would
# make every unknown id cost a full scan.
RESCAN_INTERVAL_S = 60

_path_cache: Dict[int, Path] = {}
_data_dirs: Optional[List[Path]] = None
_last_scan = 0.0


# ---------------------------------------------------------------------------
# Locating the file
# ---------------------------------------------------------------------------


def message_subpath(message_id: int) -> Path:
    """Directory of *message_id* below a mailbox's ``Data`` directory.

    >>> message_subpath(52102)
    PosixPath('2/5/Messages')
    """
    bucket = int(message_id) // 1000
    digits = list(str(bucket))[::-1] if bucket else []
    return Path(*digits, "Messages")


def _store_root() -> Optional[Path]:
    """Mail's live store: the newest ``V<n>`` holding ``MailData/Envelope Index``.

    Older stores left behind by upgrades number their messages independently,
    so the same id there is a different message.
    """
    try:
        roots = [
            p for p in MAIL_DIR.iterdir()
            if re.fullmatch(r"V\d+", p.name) and (p / "MailData" / "Envelope Index").is_file()
        ]
    except OSError:
        return None
    return max(roots, key=lambda p: int(p.name[1:]), default=None)


def _scan_data_dirs() -> List[Path]:
    """Every ``<Box>.mbox/<uuid>/Data`` directory of every account."""
    found = []
    root = _store_root()
    if root is not None:
        for dirpath, dirnames, _ in os.walk(root):
            if os.path.basename(dirpath) == "Data":
                found.append(Path(dirpath))
                dirnames[:] = []  # message files live below; nothing else to find
                continue
            dirnames[:] = [d for d in dirnames if d not in ("Attachments", "MailData")]
    return found


def _rescan() -> None:
    global _data_dirs, _last_scan
    _data_dirs = _scan_data_dirs()
    _last_scan = time.monotonic()


def find_message_file(message_id: int) -> Optional[Path]:
    """Return the ``.emlx``/``.partial.emlx`` file of *message_id*, or None."""
    message_id = int(message_id)
    cached = _path_cache.get(message_id)
    if cached is not None and cached.exists():
        return cached

    subpath = message_subpath(message_id)
    names = (f"{message_id}.emlx", f"{message_id}.partial.emlx")
    for attempt in range(2):
        if _data_dirs is None:
            _rescan()
        elif attempt:
            if time.monotonic() - _last_scan < RESCAN_INTERVAL_S:
                return None
            _rescan()
        for data_dir in _data_dirs:
            for name in names:
                candidate = data_dir / subpath / name
                if candidate.is_file():
                    _path_cache[message_id] = candidate
                    return candidate
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_emlx(data: bytes) -> EmailMessage:
    """Parse an ``.emlx`` file: a byte-count line, the RFC 822 bytes, a plist.

    Raises ValueError when the file holds fewer bytes than it declares (Mail is
    still writing it, or it was truncated): a partial body is never returned
    as if it were complete.
    """
    newline = data.index(b"\n")
    length = int(data[:newline].strip())
    raw = data[newline + 1:newline + 1 + length]
    if len(raw) < length:
        raise ValueError(f"truncated .emlx: {len(raw)} of {length} bytes")
    return parse_rfc822(raw)


def parse_rfc822(raw) -> EmailMessage:
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="surrogateescape")
    return email.message_from_bytes(raw, policy=policy.default)


class _HTMLToText(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {
        "br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "blockquote", "pre", "hr", "ul", "ol",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode(payload: bytes, charset: Optional[str]) -> str:
    """Decode with the declared charset, else UTF-8, else Latin-1; never raises."""
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            pass  # unknown or misspelled charset name
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        if isinstance(payload, str):
            return payload
        try:
            charset = part.get_content_charset()
        except (LookupError, ValueError):
            charset = None
        return _decode(payload, charset)


def _is_attachment(part) -> bool:
    return part.get_content_disposition() == "attachment" or part.get_filename() is not None


def _text_parts(part, subtype: str, root: bool = True) -> List:
    """Inline text/<subtype> parts of *part* in reading order.

    Attachments are skipped. Of a multipart/alternative only the first
    alternative that has such a part counts, so a message's plain and HTML
    versions are never both used.
    """
    if not root and _is_attachment(part):
        return []
    if part.is_multipart():
        children = part.get_payload()  # also covers an inline message/rfc822
        if part.get_content_type() == "multipart/alternative":
            for child in children:
                found = _text_parts(child, subtype, root=False)
                if found:
                    return found
            return []
        return [found for child in children for found in _text_parts(child, subtype, root=False)]
    if part.get_content_type() == f"text/{subtype}":
        return [part]
    return []


def body_text(msg: EmailMessage) -> Optional[str]:
    """Plain-text body of *msg*: every inline text/plain part, else HTML as text.

    A multipart/mixed message can carry text, then an attachment, then more
    text; all text parts are joined in order. Some senders ship an empty
    text/plain alternative next to the real HTML body; the HTML is used then.
    """
    plain = _text_parts(msg, "plain")
    html = _text_parts(msg, "html")
    if not plain and not html:
        return None
    texts = [text for text in (_part_text(p) for p in plain) if text.strip()]
    if not texts:
        texts = [text for text in (html_to_text(_part_text(p)) for p in html) if text.strip()]
    if not texts:
        if any(p.get("X-Apple-Content-Length") for p in plain + html):
            return None  # .partial.emlx whose body part was left on the server
        return ""
    if len(texts) == 1:
        return texts[0]
    return "\n".join(text.rstrip("\n") for text in texts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _source_via_applescript(
    message_id: int, account: Optional[str], mailbox: Optional[str], timeout: int = 60
) -> Optional[str]:
    """Raw MIME source of a message from Mail (never ``content``)."""
    from apple_mail_mcp.core import build_mailbox_ref, escape_applescript, run_applescript

    if account:
        accounts = f'{{account "{escape_applescript(account)}"}}'
    else:
        accounts = "every account"
    direct = ""
    if account and mailbox:
        direct = f"""
            try
                {build_mailbox_ref(mailbox, account_var="item 1 of searchAccounts", var_name="targetMailbox")}
                return source of (first message of targetMailbox whose id is {int(message_id)})
            end try"""
    script = f"""
    tell application "Mail"
        set searchAccounts to {accounts}
        {direct}
        repeat with anAccount in searchAccounts
            repeat with aMailbox in (every mailbox of anAccount)
                try
                    return source of (first message of aMailbox whose id is {int(message_id)})
                end try
            end repeat
        end repeat
        return ""
    end tell
    """
    try:
        result = run_applescript(script, timeout=max(1, int(timeout)))
    except Exception:
        return None
    return result or None


def get_message(
    message_id,
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
    allow_fallback: bool = True,
    timeout: int = 60,
) -> Optional[EmailMessage]:
    """Parsed Mail message *message_id*: from disk, else from its source.

    *timeout* bounds the AppleScript source fallback, in seconds.
    """
    try:
        message_id = int(str(message_id).strip())
    except ValueError:
        return None

    path = find_message_file(message_id)
    if path is not None:
        try:
            message = parse_emlx(path.read_bytes())
        except (OSError, ValueError):
            message = None
        if message is not None and body_text(message) is not None:
            return message

    if not allow_fallback:
        return None
    source = _source_via_applescript(message_id, account, mailbox, timeout)
    if not source:
        return None
    try:
        return parse_rfc822(source)
    except ValueError:
        return None


def get_message_body(
    message_id,
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
    allow_fallback: bool = True,
) -> Optional[str]:
    """Plain-text body of Mail message *message_id*, or None if unavailable."""
    message = get_message(message_id, account, mailbox, allow_fallback)
    if message is None:
        return None
    try:
        return body_text(message)
    except (ValueError, LookupError):
        return None


def preview(text: Optional[str], max_length: int, strip_tabs: bool = False) -> Optional[str]:
    """Collapse line breaks to spaces and truncate like the old AppleScript did."""
    if text is None:
        return None
    text = re.sub(r"[\r\n\t]" if strip_tabs else r"[\r\n]", " ", text)
    if max_length > 0 and len(text) > max_length:
        return text[:max_length] + "..."
    return text


def new_body_nonce() -> str:
    """A fresh secret for one script's body tokens (see fill_body_tokens)."""
    return secrets.token_hex(16)


def body_token_script(
    nonce: str,
    message_expr: str = "aMessage",
    account_expr: str = "accountName",
    mailbox_expr: str = '"INBOX"',
) -> str:
    """AppleScript expression producing a body token for fill_body_tokens().

    Account and mailbox names are only used for the source fallback.
    """
    return (
        f'"⟦body:{nonce}:" & ((id of {message_expr}) as string) & "|" & {account_expr} '
        f'& "|" & {mailbox_expr} & "⟧"'
    )


def fill_body_tokens(
    text: str, max_length: int, nonce: str, missing: Optional[str] = None, strip_tabs: bool = False
) -> str:
    """Replace the body tokens carrying *nonce* with body previews from disk.

    Only tokens with this call's nonce are replaced, so a token-shaped string
    in a subject or sender is left alone. When a body is unavailable the token
    becomes *missing*; with ``missing=None`` the whole line holding the token
    is dropped instead.
    """
    token_re = re.compile(r"⟦body:" + re.escape(nonce) + r":(\d+)\|([^|⟧]*)\|([^⟧]*)⟧")
    if not nonce or token_re.search(text) is None:
        return text
    out_lines = []
    for line in text.split("\n"):
        dropped = False

        def fill(match):
            nonlocal dropped
            body = get_message_body(match.group(1), match.group(2) or None, match.group(3) or None)
            snippet = preview(body, max_length, strip_tabs)
            if snippet is None:
                if missing is None:
                    dropped = True
                    return ""
                snippet = missing
            return snippet

        filled = token_re.sub(fill, line)
        if not dropped:
            out_lines.append(filled)
    return "\n".join(out_lines)
