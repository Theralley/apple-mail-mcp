"""Attachments and inline pictures of a Mail message, read from disk.

Mail keeps most received mail as ``<id>.partial.emlx``: the text parts are in
the file, and every attachment or inline picture is left out (an empty part
with an ``X-Apple-Content-Length`` header) and stored next to it as
``<Data dir>/<sub>/Attachments/<id>/<part>/<file name>``, where ``<part>`` is
the IMAP part number ("2", "1.2", ...). This module resolves a part from the
``.emlx`` itself or from that folder, without asking Mail.

Only when Mail has not downloaded a part yet does :func:`part_bytes` fall back
to AppleScript (``save`` of the ``mail attachment`` with that name), which
makes Mail fetch it.
"""

import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from apple_mail_mcp import emlx

BODY_TYPES = ("text/plain", "text/html")


@dataclass
class AttachmentPart:
    part: str                  # IMAP part number, e.g. "2" or "1.2"
    filename: str              # from MIME, else from disk, else made up from the type
    content_type: str
    disposition: Optional[str]  # "attachment", "inline" or None
    content_id: Optional[str]
    size: Optional[int]        # decoded bytes; None when unknown
    source: str                # "file" (inside the .emlx), "disk" (Attachments folder) or "missing"

    @property
    def is_inline_picture(self) -> bool:
        return self.content_type.startswith("image/") and (
            self.disposition == "inline" or bool(self.content_id)
        )


class AttachmentError(Exception):
    """A message or part that cannot be read; the text is meant for the user."""


# ---------------------------------------------------------------------------
# MIME walking
# ---------------------------------------------------------------------------


def iter_leaves(msg) -> Iterator[Tuple[str, object]]:
    """(IMAP part number, part) for every leaf part of *msg*.

    A single-part message is part "1". Children of a multipart are numbered
    1..n below their parent; an attached message (message/rfc822) is one leaf.
    """
    if not msg.is_multipart():
        yield "1", msg
        return

    def walk(part, number):
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            for index, child in enumerate(part.get_payload(), 1):
                yield from walk(child, f"{number}.{index}" if number else str(index))
        else:
            yield number, part

    yield from walk(msg, "")


def _is_body_part(part) -> bool:
    return (
        part.get_content_type() in BODY_TYPES
        and part.get_filename() is None
        and part.get_content_disposition() != "attachment"
    )


def _decoded_payload(part) -> bytes:
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            return payload[0].as_bytes()
        return b""
    try:
        data = part.get_payload(decode=True)
    except Exception:
        return b""
    return data if isinstance(data, bytes) else b""


def _disk_file(message_file: Path, message_id: int, part: str, filename: Optional[str]) -> Optional[Path]:
    """The file Mail stored for *part* of *message_id*, or None."""
    if not re.fullmatch(r"\d+(\.\d+)*", part):
        return None
    folder = message_file.parent.parent / "Attachments" / str(int(message_id)) / part
    try:
        files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")]
    except OSError:
        return None
    if not files:
        return None
    chosen = None
    if filename:
        chosen = next((f for f in files if f.name == filename), None)
    if chosen is None:
        if len(files) != 1:
            return None
        chosen = files[0]
    # Never follow a link out of Mail's store.
    root = emlx._store_root()
    try:
        resolved = chosen.resolve(strict=True)
        if root is not None and not str(resolved).startswith(str(root.resolve()) + os.sep):
            return None
    except OSError:
        return None
    return resolved


def _made_up_name(part: str, content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    return f"part-{part}{ext}"


def _load(message_id) -> Tuple[int, Path, object]:
    try:
        message_id = int(str(message_id).strip())
    except ValueError:
        raise AttachmentError(f"message_id must be a number, got {message_id!r}") from None
    path = emlx.find_message_file(message_id)
    if path is None:
        raise AttachmentError(
            f"Message {message_id} has no file in Mail's store (not downloaded yet, "
            "or no Full Disk Access). Open it in Mail once, then retry."
        )
    try:
        msg = emlx.parse_emlx(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise AttachmentError(f"Message {message_id}: cannot read its file ({exc}).") from exc
    return message_id, path, msg


def list_parts(message_id) -> List[AttachmentPart]:
    """Every attachment and inline picture of *message_id* (body text excluded)."""
    message_id, path, msg = _load(message_id)
    parts = []
    for number, part in iter_leaves(msg):
        if _is_body_part(part):
            continue
        content_type = part.get_content_type()
        filename = part.get_filename()
        payload = _decoded_payload(part)
        size: Optional[int] = None
        if payload:
            source, size = "file", len(payload)
        else:
            disk = _disk_file(path, message_id, number, filename)
            if disk is not None:
                source, size = "disk", disk.stat().st_size
                filename = filename or disk.name
            else:
                source = "missing"
                encoded = part.get("X-Apple-Content-Length")
                if encoded and str(encoded).strip().isdigit():
                    size = int(str(encoded).strip()) * 3 // 4  # base64 on the wire
        cid = part.get("Content-ID")
        parts.append(AttachmentPart(
            part=number,
            filename=filename or _made_up_name(number, content_type),
            content_type=content_type,
            disposition=part.get_content_disposition(),
            content_id=str(cid).strip().strip("<>") if cid else None,
            size=size,
            source=source,
        ))
    return parts


def find_part(message_id, part: Optional[str] = None, name: Optional[str] = None) -> AttachmentPart:
    """The one part named by *part* (exact) or *name* (exact, else unique substring)."""
    parts = list_parts(message_id)
    if part:
        for item in parts:
            if item.part == str(part).strip():
                return item
        raise AttachmentError(f"Message {message_id} has no attachment part {part!r}. Parts: "
                              + ", ".join(f"{p.part} {p.filename}" for p in parts))
    if not name:
        if len(parts) == 1:
            return parts[0]
        raise AttachmentError("Pass part (from list_email_attachments) or attachment_name.")
    exact = [p for p in parts if p.filename == name]
    if len(exact) == 1:
        return exact[0]
    wanted = name.casefold()
    loose = [p for p in parts if wanted in p.filename.casefold()]
    if len(loose) == 1:
        return loose[0]
    if not loose:
        raise AttachmentError(f"No attachment named {name!r} in message {message_id}.")
    raise AttachmentError(
        f"{len(loose)} attachments match {name!r}; pass part instead: "
        + ", ".join(f"{p.part} {p.filename}" for p in loose)
    )


# ---------------------------------------------------------------------------
# Bytes
# ---------------------------------------------------------------------------


def message_location(message_id: int) -> Tuple[Optional[str], Optional[str]]:
    """(account name, mailbox path) of *message_id* from the Envelope Index, else (None, None)."""
    from apple_mail_mcp import envelope_index as ei

    try:
        index = ei.get_index()
        rows = index.query("SELECT mailbox FROM messages WHERE ROWID = ?", (int(message_id),))
        if not rows:
            return None, None
        mailbox = next((m for m in index.mailboxes() if m.id == rows[0][0]), None)
        if mailbox is None:
            return None, None
        names = {uuid: name for name, uuid in ei.mail_accounts()}
        return names.get(mailbox.account_uuid), mailbox.path
    except Exception:
        return None, None


def _fetch_via_mail(message_id: int, filename: str) -> Optional[bytes]:
    """Have Mail save attachment *filename* of *message_id* to a private temp file."""
    from apple_mail_mcp.core import (
        build_mailbox_ref,
        escape_applescript,
        run_applescript,
    )

    account, mailbox = message_location(message_id)
    if not account or not mailbox:
        return None
    with tempfile.TemporaryDirectory(prefix="apple-mail-mcp-") as tmp:
        os.chmod(tmp, 0o700)
        target = Path(tmp) / "attachment"
        script = f'''
        tell application "Mail"
            try
                set targetAccount to account "{escape_applescript(account)}"
                {build_mailbox_ref(mailbox, account_var="targetAccount", var_name="targetMailbox")}
                set theMessage to first message of targetMailbox whose id is {int(message_id)}
                repeat with anAttachment in (mail attachments of theMessage)
                    if (name of anAttachment) is "{escape_applescript(filename)}" then
                        save anAttachment in POSIX file "{escape_applescript(str(target))}"
                        return "OK"
                    end if
                end repeat
                return "NOT FOUND"
            on error errMsg
                return "Error: " & errMsg
            end try
        end tell
        '''
        try:
            result = run_applescript(script, timeout=120)
        except Exception:
            return None
        if result.strip() != "OK" or not target.is_file():
            return None
        return target.read_bytes()


def part_bytes(message_id, item: AttachmentPart, allow_mail: bool = True) -> bytes:
    """Decoded bytes of *item*: from the .emlx, the Attachments folder, or Mail."""
    message_id, path, msg = _load(message_id)
    for number, part in iter_leaves(msg):
        if number != item.part:
            continue
        payload = _decoded_payload(part)
        if payload:
            return payload
        disk = _disk_file(path, message_id, number, part.get_filename())
        if disk is not None:
            return disk.read_bytes()
        break
    if allow_mail:
        # Mail saves attachments by name only, so two with the same name
        # cannot be told apart: never return one as the other.
        same_name = [p for p in list_parts(message_id) if p.filename == item.filename]
        if len(same_name) > 1:
            raise AttachmentError(
                f"Part {item.part} ({item.filename}) of message {message_id} is not downloaded, and "
                f"{len(same_name)} attachments share that name. Open the message in Mail so it "
                "downloads them, then retry."
            )
        data = _fetch_via_mail(message_id, item.filename)
        if data:
            return data
    raise AttachmentError(
        f"Part {item.part} ({item.filename}) of message {message_id} is not downloaded. "
        "Open the message in Mail (or run synchronize_account) and retry."
    )


# ---------------------------------------------------------------------------
# Reading: PDF text and pictures, all on this Mac
# ---------------------------------------------------------------------------

# Pictures larger than this, or in a format MCP clients may not show, are
# converted to JPEG with macOS `sips` first.
MAX_IMAGE_BYTES = 3_500_000
MAX_IMAGE_SIDE = 2000
CLIENT_IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}


def _private_tempdir() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="apple-mail-mcp-")
    os.chmod(tmp.name, 0o700)
    return tmp


def pdf_text(data: bytes, max_pages: int = 50, timeout: int = 90) -> Tuple[int, str]:
    """(page count, text of the first *max_pages* pages) using macOS PDFKit.

    Runs locally through AppleScriptObjC; Mail is not involved.
    """
    import subprocess

    with _private_tempdir() as tmp:
        pdf = Path(tmp) / "attachment.pdf"
        pdf.write_bytes(data)
        os.chmod(pdf, 0o600)
        script = f'''use framework "Foundation"
use framework "PDFKit"
use scripting additions
set u to current application's NSURL's fileURLWithPath:"{pdf!s}"
set d to current application's PDFDocument's alloc()'s initWithURL:u
if d is missing value then return "ERROR unreadable PDF"
if (d's isLocked()) as boolean then return "ERROR the PDF is password protected"
set n to (d's pageCount()) as integer
set out to current application's NSMutableString's |string|()
repeat with i from 0 to (n - 1)
    if i ≥ {int(max_pages)} then exit repeat
    set s to ((d's pageAtIndex:i)'s |string|())
    (out's appendString:("\\n--- page " & (i + 1) & " ---\\n"))
    if s is not missing value then (out's appendString:s)
end repeat
return (n as string) & linefeed & (out as string)'''
        try:
            proc = subprocess.run(["osascript", "-"], input=script.encode("utf-8"),
                                  capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AttachmentError(f"Reading the PDF took longer than {timeout} s.") from None
        out = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0 or out.startswith("ERROR"):
            reason = out.strip() or proc.stderr.decode("utf-8", errors="replace").strip()
            raise AttachmentError(f"Cannot read the PDF: {reason[:200]}")
        count, _, text = out.partition("\n")
        try:
            pages = int(count.strip())
        except ValueError:
            pages = 0
        return pages, text.rstrip("\n")


def _sips_dimensions(path: Path) -> Tuple[int, int]:
    import subprocess

    try:
        proc = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0, 0
    text = proc.stdout.decode("utf-8", errors="replace")
    width = re.search(r"pixelWidth:\s*(\d+)", text)
    height = re.search(r"pixelHeight:\s*(\d+)", text)
    return (int(width.group(1)) if width else 0, int(height.group(1)) if height else 0)


def image_for_client(data: bytes, content_type: str) -> Tuple[bytes, str, str]:
    """(bytes, format, note) of a picture an MCP client can show.

    PNG/JPEG/GIF/WebP within the size limits pass through unchanged; anything
    else (HEIC, TIFF, BMP, oversized) is converted to JPEG locally with sips.
    """
    import subprocess

    fmt = CLIENT_IMAGE_FORMATS.get(content_type)
    with _private_tempdir() as tmp:
        src = Path(tmp) / ("in." + (fmt or "img"))
        src.write_bytes(data)
        width, height = _sips_dimensions(src)
        if fmt and len(data) <= MAX_IMAGE_BYTES and max(width, height) <= MAX_IMAGE_SIDE:
            return data, fmt, f"{width}x{height}"
        out = Path(tmp) / "out.jpg"
        try:
            proc = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                 "-Z", str(MAX_IMAGE_SIDE), str(src), "--out", str(out)],
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise AttachmentError(f"Cannot convert this {content_type} picture for display.") from None
        if proc.returncode != 0 or not out.is_file():
            raise AttachmentError(f"Cannot convert this {content_type} picture for display.")
        converted = out.read_bytes()
        new_w, new_h = _sips_dimensions(out)
        return converted, "jpeg", f"{width}x{height}, shown as JPEG {new_w}x{new_h}"


TEXT_TYPES = {
    "application/json", "application/xml", "application/x-yaml", "application/yaml",
    "application/csv", "application/ics", "application/javascript",
}


def is_text_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in TEXT_TYPES


def decode_text(data: bytes, content_type: str) -> str:
    text = None
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    if content_type == "text/html":
        text = emlx.html_to_text(text)
    return text


def format_size(size: Optional[int]) -> str:
    if size is None:
        return "size unknown"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
