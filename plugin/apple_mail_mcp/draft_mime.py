"""Build a complete draft message for Mail: body, signature and attachments.

Mail stores any body that a script puts into an outgoing message as "shared
content" (a ``URLShareWrapperClass`` blockquote), which turns into "> " quoted
text when the message is sent. So the server never hands Mail a body through
AppleScript. It builds the whole RFC 822 message here instead, with the
account's own signature embedded the way Mail does (``div#AppleMailSignature``),
and lets Mail turn that message into a draft (see draft_gui.py).
"""

import hashlib
import mimetypes
import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import getaddresses
from html import escape as html_escape
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from apple_mail_mcp import emlx

MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_TOTAL_BYTES = 25 * 1024 * 1024

# Folders below the home folder that a draft may never attach from.
_DENIED_HOME_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".config", ".claude", ".cc-accounts", ".codex",
    ".gemini", ".qwen", ".docker", ".kube", ".password-store", ".netrc",
    os.path.join("Library", "Keychains"),
    os.path.join("Library", "Mail"),
    os.path.join("Library", "Messages"),
    os.path.join("Library", "Cookies"),
    os.path.join("Library", "Containers", "com.apple.mail"),
    os.path.join("Library", "Caches", "apple-mail-mcp"),
)
_DENIED_NAME_RE = re.compile(
    r"^\.env($|\.)|\.(pem|key|p12|pfx|kdbx|keychain|keychain-db|ovpn)$|^id_(rsa|dsa|ecdsa|ed25519)|^credentials",
    re.IGNORECASE,
)

BODY_STYLE = "overflow-wrap: break-word; -webkit-nbsp-mode: space; line-break: after-white-space;"


class DraftError(Exception):
    """Invalid draft input; the text is meant for the user."""


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@dataclass
class DraftAttachment:
    path: str
    filename: str
    content_type: str
    data: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _allowed_roots() -> List[str]:
    roots = {os.path.realpath(os.path.expanduser("~")), "/private/tmp", os.path.realpath(tempfile.gettempdir())}
    return sorted(roots)


def split_attachment_arg(attachments: Union[None, str, Sequence[str]]) -> List[str]:
    """A list of paths from a list, or from one string (comma or newline separated).

    A single string that is itself an existing path is never split, so a file
    name with a comma in it still works.
    """
    if attachments is None:
        return []
    if isinstance(attachments, str):
        text = attachments.strip()
        if not text:
            return []
        if os.path.exists(os.path.expanduser(text)):
            return [text]
        return [p.strip() for p in re.split(r"[,\n]", text) if p.strip()]
    return [str(p).strip() for p in attachments if str(p).strip()]


def checked_local_path(raw: str, what: str = "Attachment") -> str:
    """The resolved path of *raw* if a draft may read or write it, else DraftError.

    Allowed: below the home folder or the temp folders, outside the protected
    folders (keys, credentials, Mail's own store, this server's cache) and not
    named like a key or secrets file.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(raw))
    # macOS file systems are case-insensitive (~/.CONFIG is ~/.config), so
    # every comparison is made on case-folded paths.
    folded = resolved.casefold()

    def inside(root: str) -> bool:
        root = root.casefold()
        return folded == root or folded.startswith(root + os.sep)

    if not any(inside(r) for r in _allowed_roots()):
        raise DraftError(f"{what} {raw!r} is outside your home folder and the temp folders.")
    for relative in _DENIED_HOME_DIRS:
        denied = os.path.join(home, relative)
        if inside(denied):
            raise DraftError(f"{what} {raw!r} is in a protected folder ({denied}).")
    if _DENIED_NAME_RE.search(os.path.basename(resolved)):
        raise DraftError(f"{what} {raw!r} looks like a key or secrets file; attach it by hand if you mean it.")
    return resolved


def load_attachments(attachments: Union[None, str, Sequence[str]]) -> List[DraftAttachment]:
    """Validate and read every attachment once (the bytes sent are the bytes checked)."""
    paths = split_attachment_arg(attachments)
    if len(paths) > MAX_ATTACHMENTS:
        raise DraftError(f"At most {MAX_ATTACHMENTS} attachments per draft (got {len(paths)}).")
    loaded: List[DraftAttachment] = []
    total = 0
    from apple_mail_mcp.paths import validate_read_path

    for raw in paths:
        resolved = checked_local_path(raw)
        _, guard_error = validate_read_path(raw)  # secrets denylist, --attach-dir
        if guard_error:
            raise DraftError(guard_error.removeprefix("Error: "))
        name = os.path.basename(resolved)
        if not os.path.isfile(resolved):
            raise DraftError(f"Attachment {raw!r} is not a file.")
        size = os.path.getsize(resolved)
        total += size
        if total > MAX_ATTACHMENT_TOTAL_BYTES:
            raise DraftError(
                f"Attachments add up to more than {MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)} MB; "
                "share large files as a link instead."
            )
        with open(resolved, "rb") as handle:
            data = handle.read()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        loaded.append(DraftAttachment(path=resolved, filename=name, content_type=content_type, data=data))
    return loaded


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


@dataclass
class Signature:
    id: str
    html: str  # what goes inside <div id="AppleMailSignature">
    related: List[Tuple[bytes, str, str, str]] = field(default_factory=list)  # (data, maintype, subtype, cid)


def signatures_dir() -> Optional[Path]:
    override = os.environ.get("APPLE_MAIL_SIGNATURES_DIR")
    if override:
        return Path(override)
    root = emlx._store_root()
    return root / "MailData" / "Signatures" if root is not None else None


def selected_signatures() -> Dict[str, str]:
    """Mail's per-account signature choice: {account UUID: signature id}."""
    try:
        out = subprocess.run(["defaults", "export", "com.apple.mail", "-"], capture_output=True, timeout=10, check=False)
        prefs = plistlib.loads(out.stdout) if out.returncode == 0 and out.stdout else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return {}
    selected = prefs.get("SignaturesSelected") or {}
    return {str(k): str(v) for k, v in selected.items() if isinstance(v, str)}


def _inner_body(html: str) -> str:
    match = re.search(r"<body[^>]*>(.*)</body>", html, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else html).strip()


def resolve_signature(
    account_uuid: str,
    selected: Optional[Dict[str, str]] = None,
    directory: Optional[Path] = None,
) -> Tuple[Optional[Signature], str]:
    """(signature, note) for the account; None when it has none or it is ambiguous."""
    directory = directory or signatures_dir()
    if directory is None:
        return None, "no signature (Mail's signature folder was not found)"
    try:
        with open(directory / "AccountsMap.plist", "rb") as handle:
            accounts_map = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None, "no signature (cannot read Mail's signature settings)"
    ids = list((accounts_map.get(account_uuid) or {}).get("Signatures") or [])
    if not ids:
        return None, "no signature (none set for this account in Mail)"
    selected = selected_signatures() if selected is None else selected
    chosen = selected.get(account_uuid)
    if chosen not in ids:
        if len(ids) != 1:
            return None, "no signature (several signatures and none selected for this account)"
        chosen = ids[0]
    path = directory / f"{chosen}.mailsignature"
    try:
        msg = emlx.parse_rfc822(path.read_bytes())
    except (OSError, ValueError):
        return None, "no signature (cannot read the signature file)"
    html_part = None
    related = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/html" and html_part is None:
            html_part = part
        elif part.get("Content-ID") and part.get_content_maintype() == "image":
            data = part.get_payload(decode=True) or b""
            cid = str(part.get("Content-ID")).strip().strip("<>")
            related.append((data, part.get_content_maintype(), part.get_content_subtype(), cid))
    if html_part is None:
        return None, "no signature (the signature has no HTML part)"
    html = _inner_body(emlx._part_text(html_part))
    return Signature(id=chosen, html=html, related=related), f"signature {chosen[:8]} added"


# ---------------------------------------------------------------------------
# Body and message
# ---------------------------------------------------------------------------


def text_to_html(text: str) -> str:
    """Plain text as Mail's own compose HTML: one <div> per line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "".join(f"<div>{html_escape(line)}</div>" if line.strip() else "<div><br></div>" for line in lines)


def html_fragment(html: str) -> str:
    return _inner_body(html) if re.search(r"<body", html, re.IGNORECASE) else html


def parse_recipients(value: Optional[str], field_name: str) -> List[str]:
    if not value or not value.strip():
        return []
    if re.search(r"[\r\n]", value):
        raise DraftError(f"'{field_name}' may not contain line breaks.")
    found = []
    for name, addr in getaddresses([value]):
        addr = addr.strip()
        if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", addr):
            raise DraftError(f"'{field_name}' has an invalid address: {addr or value!r}")
        found.append(str(Address(display_name=name, addr_spec=addr)) if name else addr)
    return found


def build_draft(
    *,
    subject: str,
    sender: str,
    to: Optional[str],
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    text_body: Optional[str] = None,
    html_body: Optional[str] = None,
    attachments: Sequence[DraftAttachment] = (),
    signature: Optional[Signature] = None,
) -> EmailMessage:
    """The draft as an unsent RFC 822 message (``X-Unsent: 1``)."""
    if not subject or not subject.strip():
        raise DraftError("'subject' is required.")
    if re.search(r"[\r\n]", subject):
        raise DraftError("'subject' may not contain line breaks.")
    if not text_body and not html_body:
        raise DraftError("'body' (or 'html_body') is required.")
    to_list = parse_recipients(to, "to")
    if not to_list:
        raise DraftError("'to' is required.")

    fragment = html_fragment(html_body) if html_body else text_to_html(text_body or "")
    plain = text_body if text_body else emlx.html_to_text(fragment)
    if signature is not None:
        fragment += f'<div><br></div><div id="AppleMailSignature" dir="ltr">{signature.html}</div>'
        plain = plain.rstrip("\n") + "\n\n" + emlx.html_to_text(signature.html).strip() + "\n"
    html = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
        f'<body style="{BODY_STYLE}">{fragment}</body></html>'
    )

    msg = EmailMessage(policy=policy.default)
    msg["Subject"] = subject.strip()
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    cc_list = parse_recipients(cc, "cc")
    bcc_list = parse_recipients(bcc, "bcc")
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if bcc_list:
        msg["Bcc"] = ", ".join(bcc_list)
    msg["X-Unsent"] = "1"
    if signature is not None:
        msg["X-Apple-Mail-Signature"] = signature.id
    msg.set_content(plain, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    if signature is not None and signature.related:
        html_part = msg.get_payload()[1]
        for data, maintype, subtype, cid in signature.related:
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    for item in attachments:
        maintype, _, subtype = item.content_type.partition("/")
        msg.add_attachment(item.data, maintype=maintype, subtype=subtype or "octet-stream", filename=item.filename)
    return msg
