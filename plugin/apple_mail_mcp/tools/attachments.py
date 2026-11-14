"""Reading attachments and inline pictures, locally."""

from typing import Optional

from fastmcp.utilities.types import Image

from apple_mail_mcp import mail_attachments as ma
from apple_mail_mcp.core import inject_preferences
from apple_mail_mcp.server import mcp

MAX_ATTACHMENT_READ_BYTES = 40 * 1024 * 1024


@mcp.tool(output_schema=None)
@inject_preferences
def read_email_attachment(
    message_id: int,
    part: Optional[str] = None,
    attachment_name: Optional[str] = None,
    max_chars: int = 20000,
    max_pages: int = 30,
):
    """
    Read an attachment or inline picture of an email, on this Mac.

    - Pictures (PNG, JPEG, GIF, WebP, HEIC, TIFF, ...) are returned as an image
      you can look at; large or unusual formats are converted to JPEG first.
    - PDFs are returned as text (macOS PDFKit), page by page. A scanned PDF
      without a text layer returns little text; save it with
      save_email_attachment and open the file instead.
    - Text-like files (txt, csv, html, ics, json, xml) are returned as text.
    - Other types (docx, xlsx, zip, ...) cannot be read here; save them.

    Get message_id from any listing or search tool, and part from
    list_email_attachments(message_id=...). With a single attachment, part
    can be omitted.

    Args:
        message_id: Mail's message ID
        part: Part number from list_email_attachments, e.g. "2" or "1.2"
        attachment_name: Name (or unique part of it) instead of part
        max_chars: Maximum characters of text returned (default 20000)
        max_pages: Maximum PDF pages read (default 30)

    Returns:
        Text, or a short description plus the picture
    """
    try:
        item = ma.find_part(message_id, part=part, name=attachment_name)
        if item.size is not None and item.size > MAX_ATTACHMENT_READ_BYTES:
            return (
                f"Error: {item.filename} is {ma.format_size(item.size)}; too large to read here. "
                "Save it with save_email_attachment instead."
            )
        header = f"Message {message_id}, part {item.part}: {item.filename} ({item.content_type})"
        content_type = item.content_type

        if content_type.startswith("image/"):
            data = ma.part_bytes(message_id, item)
            shown, fmt, note = ma.image_for_client(data, content_type)
            return [f"{header}, {ma.format_size(len(data))}, {note}", Image(data=shown, format=fmt)]

        if content_type == "application/pdf" or item.filename.lower().endswith(".pdf"):
            data = ma.part_bytes(message_id, item)
            pages, text = ma.pdf_text(data, max_pages=max(1, int(max_pages)))
            text = text.strip()
            note = f"{pages} page(s)" + (f", first {max_pages} read" if pages > max_pages else "")
            if len(text.replace("--- page", "").strip()) < 20 * min(pages or 1, max_pages):
                note += "; little or no text layer (scanned?). Save the file and open it to see the pages."
            return _clip(f"{header}, {note}\n\n{text}", max_chars)

        if ma.is_text_type(content_type):
            data = ma.part_bytes(message_id, item)
            return _clip(f"{header}\n\n{ma.decode_text(data, content_type)}", max_chars)

        return (
            f"{header}: this type cannot be read as text or a picture here. "
            f"Save it with save_email_attachment(message_id={message_id}, part=\"{item.part}\", save_path=...)."
        )
    except ma.AttachmentError as exc:
        return f"Error: {exc}"


def _clip(text: str, max_chars: int) -> str:
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + f"\n\n[... cut at {max_chars} characters; raise max_chars or max_pages for more]"
    return text
