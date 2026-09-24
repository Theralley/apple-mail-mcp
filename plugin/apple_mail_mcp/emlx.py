"""Read message bodies from Mail's on-disk store instead of asking Mail.app.

Asking Mail for ``content of <message>`` makes Mail convert HTML to plain text
on its main thread through legacy WebKit (NSHTMLReader). That costs roughly
0.35-0.7 s per message, blocks every other client while it runs, and is the
frame in Mail's recurring ExcUserFault crash reports. This server therefore
never requests ``content``. AppleScript returns message ids and metadata only,
and the body is read here:

1. from ``~/Library/Mail/V*/<account>/**/<Box>.mbox/<uuid>/Data/<sub>/Messages/
   <id>.emlx`` (or ``<id>.partial.emlx``), where ``<sub>`` is the digits of
   ``id // 1000`` reversed, one per directory level (empty below 1000);
2. if the file is missing or unreadable (no Full Disk Access, not downloaded,
   a partial file without a text part), from ``source of <message>`` over
   AppleScript: raw MIME, which Mail returns without any HTML conversion.

Both are parsed with the stdlib email package; text/plain is preferred and
HTML is converted to text here with html.parser.

Tool scripts that need a body inline in their output emit a token built by
``body_token_script`` and the caller replaces it with ``fill_body_tokens``.
"""

import email
import os
import re
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional

MAIL_DIR = Path(os.environ.get("APPLE_MAIL_DIR", Path.home() / "Library" / "Mail"))

# Built in AppleScript by body_token_script(); account and mailbox names are
# only used for the source fallback.
BODY_TOKEN_RE = re.compile(r"⟦body:(\d+)\|([^|⟧]*)\|([^⟧]*)⟧")

_path_cache: Dict[int, Path] = {}
_data_dirs: Optional[List[Path]] = None


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


def _store_roots() -> List[Path]:
    """Mail's versioned store directories (``V10`` etc.), newest first."""
    try:
        roots = [p for p in MAIL_DIR.iterdir() if re.fullmatch(r"V\d+", p.name)]
    except OSError:
        return []
    return sorted(roots, key=lambda p: int(p.name[1:]), reverse=True)


def _scan_data_dirs() -> List[Path]:
    """Every ``<Box>.mbox/<uuid>/Data`` directory of every account."""
    found = []
    for root in _store_roots():
        for dirpath, dirnames, _ in os.walk(root):
            if os.path.basename(dirpath) == "Data":
                found.append(Path(dirpath))
                dirnames[:] = []  # message files live below; nothing else to find
                continue
            dirnames[:] = [d for d in dirnames if d not in ("Attachments", "MailData")]
    return found


def find_message_file(message_id: int) -> Optional[Path]:
    """Return the ``.emlx``/``.partial.emlx`` file of *message_id*, or None."""
    global _data_dirs
    message_id = int(message_id)
    cached = _path_cache.get(message_id)
    if cached is not None and cached.exists():
        return cached

    subpath = message_subpath(message_id)
    names = (f"{message_id}.emlx", f"{message_id}.partial.emlx")
    for rescan in (False, True):
        if _data_dirs is None or rescan:
            _data_dirs = _scan_data_dirs()
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
    """Parse an ``.emlx`` file: a byte-count line, the RFC 822 bytes, a plist."""
    newline = data.index(b"\n")
    length = int(data[:newline].strip())
    raw = data[newline + 1:newline + 1 + length]
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


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError, ValueError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def body_text(msg: EmailMessage) -> Optional[str]:
    """Plain-text body of *msg*: text/plain if present, else HTML as text.

    Some senders ship an empty text/plain alternative next to the real HTML
    body; the HTML is used then.
    """
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return None
    text = _part_text(part)
    if not text.strip() and part.get_content_type() == "text/plain":
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            part, text = html_part, _part_text(html_part)
    if not text.strip() and part.get("X-Apple-Content-Length"):
        return None  # .partial.emlx whose body part was left on the server
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _source_via_applescript(message_id: int, account: Optional[str], mailbox: Optional[str]) -> Optional[str]:
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
        result = run_applescript(script, timeout=60)
    except Exception:
        return None
    return result or None


def get_message(
    message_id,
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
    allow_fallback: bool = True,
) -> Optional[EmailMessage]:
    """Parsed Mail message *message_id*: from disk, else from its source."""
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
    source = _source_via_applescript(message_id, account, mailbox)
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


def body_token_script(message_expr: str = "aMessage", account_expr: str = "accountName", mailbox_expr: str = '"INBOX"') -> str:
    """AppleScript expression producing a body token for fill_body_tokens()."""
    return (
        f'"⟦body:" & ((id of {message_expr}) as string) & "|" & {account_expr} '
        f'& "|" & {mailbox_expr} & "⟧"'
    )


def fill_body_tokens(text: str, max_length: int, missing: Optional[str] = None, strip_tabs: bool = False) -> str:
    """Replace body tokens in *text* with body previews read from disk.

    When a body is unavailable the token becomes *missing*; with
    ``missing=None`` the whole line holding the token is dropped instead.
    """
    if "⟦body:" not in text:
        return text
    out_lines = []
    for line in text.split("\n"):
        match = BODY_TOKEN_RE.search(line)
        if not match:
            out_lines.append(line)
            continue
        body = get_message_body(match.group(1), match.group(2) or None, match.group(3) or None)
        snippet = preview(body, max_length, strip_tabs)
        if snippet is None:
            if missing is None:
                continue
            snippet = missing
        out_lines.append(line[:match.start()] + snippet + line[match.end():])
    return "\n".join(out_lines)
