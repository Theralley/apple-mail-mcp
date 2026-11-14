"""A local search index of all mail, read/unread state, and reading a message by id, on this Mac only."""

import json
import sqlite3
from datetime import datetime
from email.utils import getaddresses
from typing import List, Optional, Union

from apple_mail_mcp import emlx
from apple_mail_mcp import envelope_index as ei
from apple_mail_mcp import local_index as li
from apple_mail_mcp import mail_attachments as ma
from apple_mail_mcp.core import inject_preferences
from apple_mail_mcp.server import mcp

SEARCH_REFRESH_S = 5.0
READ_STATES = {"any": None, "unread": False, "read": True}


def _when(timestamp: Optional[int]) -> str:
    if not timestamp:
        return "never"
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")


def _split(value: Union[None, str, List[str]]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p).strip() for p in value if str(p).strip()]


@mcp.tool()
@inject_preferences
def index_mail(
    scope: str = "all",
    accounts: Optional[Union[str, List[str]]] = None,
    rebuild: bool = False,
    max_seconds: int = 120,
    delete_index: bool = False,
) -> str:
    """
    Build or update the local search index of all mail (on this Mac only).

    Reads Mail's own database and message files; nothing is sent anywhere and
    Mail is not scripted for message bodies. The first run over all accounts
    can take a few minutes: it indexes the newest mail first and stops after
    max_seconds, and the next run continues where it stopped. Later runs only
    add what is new. search_mail_index also tops it up briefly by itself.

    The index lives in ~/Library/Caches/apple-mail-mcp/ (private to your user).

    Args:
        scope: "all" (default: every mailbox, including archives, subfolders,
            Drafts, Trash and Junk), or a comma list of "inbox", "sent", "other"
        accounts: Account name(s), comma-separated or a list; default all accounts
        rebuild: Re-index these accounts from scratch
        max_seconds: Time budget for this run (default 120)
        delete_index: Delete the whole index instead (it holds message text)

    Returns:
        What was indexed, what is left and what is missing
    """
    try:
        if delete_index:
            removed = li.delete_index()
            return "Local mail index deleted." if removed else "There was no local mail index."
        folders = [f.strip().casefold() for f in (scope or "all").split(",") if f.strip()]
        if folders == ["all"]:
            folders = list(li.FOLDERS)
        unknown = [f for f in folders if f not in li.FOLDERS]
        if unknown or not folders:
            return "Error: scope is all, or a comma list of inbox, sent and other."
        result = li.update(_split(accounts) or None, folders, rebuild=rebuild,
                           max_seconds=max(5, min(int(max_seconds), 600)))
    except li.MailIndexError as exc:
        return f"Error: {exc}"
    except ei.IndexUnavailable as exc:
        return f"Error: Mail's database cannot be read ({exc})."

    lines = ["LOCAL MAIL INDEX", ""]
    lines.append(f"Indexed now: {result.added} new" + (f", {result.retried} retried" if result.retried else ""))
    if result.removed or result.moved:
        lines.append(f"Removed: {result.removed} no longer in these folders; {result.moved} changed folder")
    lines.append(f"Messages in the index: {result.total:,}")
    if result.remaining:
        lines.append(f"⏳ {result.remaining:,} left; run index_mail again to continue (newest are done first).")
    else:
        lines.append("✓ Up to date.")
    if result.missing_bodies:
        lines.append(
            f"{result.missing_bodies:,} message(s) have no body on disk yet (Mail has not downloaded them); "
            "they are searchable by subject, people and attachment names, and are retried next run."
        )
    for note in result.notes:
        lines.append(f"Note: {note}")
    lines.append(f"Took {result.seconds:.1f} s.")
    return "\n".join(lines)


@mcp.tool()
@inject_preferences
def search_mail_index(
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
    read_status: str = "any",
    mailbox: Optional[str] = None,
) -> str:
    """
    Search your mail instantly through the local index (run index_mail once first).

    Searches subject, sender, recipients, attachment names and the full body
    text. Words are ANDed and matched whole; add * for a prefix (offer*), use
    "quotes" for a phrase, and field:word to look in one field only
    (subject:, from:, to:, attachment:, body:). Case is ignored; å, ä and ö
    are kept (får and far are different words).

    Each hit gives the message ID, for read_email, list_email_attachments,
    read_email_attachment, save_email_attachment and set_email_read_status.
    Unread messages are marked "unread"; the state is read live from Mail.
    To list unread mail without words, leave query empty and set
    read_status="unread" (for example folder="inbox").

    Args:
        query: Words to find (may be empty when filtering only)
        folder: "all" (default), "inbox", "sent", "both" (inbox and sent) or
            "other" (archives, subfolders, Drafts, Trash, Junk)
        mailbox: Only mailboxes whose name contains this (e.g. "Archive")
        account: Only this account
        sender: Only senders whose name or address contains this
        date_from: Received on or after this day (YYYY-MM-DD)
        date_to: Received on or before this day (YYYY-MM-DD)
        has_attachment: True or False to filter on attachments
        sort: "date" (newest first, default) or "relevance"
        limit: Hits per page (default 20, max 200)
        offset: Hits to skip, for the next page
        read_status: "unread", "read" or "any" (default)

    Returns:
        Matching messages with a text snippet
    """
    wanted = (read_status or "any").strip().casefold()
    if wanted not in READ_STATES:
        return "Error: read_status is unread, read or any."
    try:
        if not li.index_path().exists():
            return "There is no local mail index yet. Run index_mail first (a few minutes the first time)."
        refreshed = ""
        try:
            result = li.refresh(SEARCH_REFRESH_S)
            if result is None or result.total < 0:
                pass  # nothing indexed yet, or nothing new in Mail
            elif result.added:
                refreshed = f" ({result.added} new message(s) indexed just now)"
            if result is not None and result.remaining:
                refreshed += f"; {result.remaining:,} not indexed yet, run index_mail"
        except li.IndexBusy:
            refreshed = " (an update is running in another process)"
        except (li.MailIndexError, ei.IndexUnavailable) as exc:
            refreshed = f" (could not update first: {exc})"
        conn = li.connect_reader()
        try:
            li.attach_mail_state(conn)
            total, hits = li.search(
                conn, query, folder=folder, account=account, sender=sender,
                date_from=date_from, date_to=date_to, has_attachment=has_attachment,
                sort=sort, limit=limit, offset=offset, read=READ_STATES[wanted], mailbox=mailbox,
            )
            info = li.status(conn)
        finally:
            conn.close()
    except li.MailIndexError as exc:
        return f"Error: {exc}"
    except sqlite3.Error as exc:
        return f"Error: the local mail index could not be read ({exc}); try again, or run index_mail."

    title = query or "(filters only)"
    if wanted != "any":
        title += f" [{wanted} only]"
    lines = [f"MAIL INDEX SEARCH: {title}", ""]
    if not hits:
        lines.append("No matches." if total == 0 else f"No more matches ({total} in all).")
    else:
        lines.append(f"{total:,} match(es); showing {offset + 1}–{offset + len(hits)}.")
    lines.append(f"Index: {info['total']:,} messages, updated {_when(info['updated_at'])}{refreshed}.")
    if info.get("unread_inbox") is not None:
        lines.append(f"Unread in Inbox (all indexed accounts): {info['unread_inbox']:,}")
    lines.append("")
    for hit in hits:
        groups = hit.folders.split(",")
        where = " + ".join([f.capitalize() for f in groups if f != "other"]
                           + ([hit.mailbox or "Other"] if "other" in groups else []))
        lines.append(f"✉ {hit.subject or '(no subject)'}")
        state = " · unread" if hit.read is False else ""
        lines.append(f"   ID: {hit.id} · {_when(hit.date_received)} · {where} · {hit.account}{state}")
        if "sent" in hit.folders.split(",") and hit.recipients:
            lines.append(f"   To: {hit.recipients[:200]}")
        else:
            lines.append(f"   From: {hit.sender}")
        if hit.attachments:
            lines.append(f"   📎 {hit.attachments[:200]}")
        if hit.snippet:
            lines.append(f"   {' '.join(hit.snippet.split())}")
        elif hit.body_state == "missing":
            lines.append("   (body not downloaded by Mail yet)")
        lines.append("")
    if hits and offset + len(hits) < total:
        lines.append(f"More: offset={offset + len(hits)}")
    return "\n".join(lines).rstrip()


def _read_state(message_id: int) -> Optional[str]:
    """Live state from Mail's database: "read", "unread", or None when unknown."""
    try:
        rows = ei.get_index().query("SELECT read, deleted FROM messages WHERE ROWID = ?", (int(message_id),))
    except ei.IndexUnavailable:
        return None
    if not rows or rows[0][1]:
        return None
    return "read" if rows[0][0] else "unread"


@mcp.tool()
@inject_preferences
def read_email(
    message_id: Union[int, str],
    max_chars: int = 20000,
    include_headers: bool = False,
    output_format: str = "text",
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> str:
    """
    Read one email by its message ID, from Mail's files on this Mac.

    Fast and local: no mailbox search, and Mail is not asked for the body
    (Mail's own HTML conversion is what made it hang). Gives sender,
    recipients, date, subject, read/unread state, the body as text and the
    attachments (with part numbers for read_email_attachment /
    save_email_attachment). Reading here never marks the message read. Get
    the ID from search_mail_index, search_emails or any listing tool.

    Args:
        message_id: Mail's numeric message ID (not the Message-ID header)
        max_chars: Maximum body characters returned (default 20000)
        include_headers: Also list every header (text output)
        output_format: "text" (default) or "json"
        account: Optional; accepted for compatibility, the ID alone finds the message
        mailbox: Optional; accepted for compatibility, the ID alone finds the message

    Returns:
        The message as text, or as a JSON object
    """
    text_id = str(message_id).strip()
    if not text_id.isascii() or not text_id.isdigit():
        return "Error: message_id must contain only digits"
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        return "Error: max_chars must be a positive integer"
    if output_format not in ("text", "json"):
        return "Error: output_format must be text or json"
    message_id = int(text_id)
    msg = emlx.get_message(message_id, allow_fallback=False)
    path = emlx.find_message_file(message_id)
    if msg is None and path is not None:
        try:
            msg = emlx.parse_emlx(path.read_bytes())
        except (OSError, ValueError):
            msg = None
    if msg is None:
        return (
            f"Error: message {message_id} is not on disk (not downloaded yet, deleted, or no Full Disk "
            "Access). Open it in Mail once, or use search_emails / get_email_source."
        )
    try:
        body = emlx.body_text(msg)
    except (ValueError, LookupError):
        body = None
    try:
        parts = ma.list_parts(message_id)
    except ma.AttachmentError:
        parts = []
    state = _read_state(message_id)
    text = (body or "").strip()

    if output_format == "json":
        def header(name):
            return " ".join(str(msg.get(name) or "").split())

        def addresses(name):
            return [addr for _, addr in getaddresses([str(v) for v in msg.get_all(name, [])]) if addr]

        return json.dumps({
            "subject": header("Subject"),
            "sender": header("From"),
            "date_received": header("Date"),
            "message_id": str(message_id),
            "internet_message_id": header("Message-ID"),
            "to": addresses("To"),
            "cc": addresses("Cc"),
            "attachments": [
                {"name": item.filename, "mime_type": item.content_type or None, "file_size": item.size,
                 "part": item.part, "inline_picture": item.is_inline_picture,
                 "downloaded": item.source != "missing"}
                for item in parts
            ],
            "read_status": state,
            "body": text[:max_chars] if body is not None else None,
            "body_truncated": body is not None and len(text) > max_chars,
            "body_total_chars": len(text) if body is not None else None,
            "body_downloaded": body is not None,
        }, ensure_ascii=False)

    lines = [f"EMAIL {message_id}", ""]
    for name in ("From", "To", "Cc", "Date", "Subject"):
        if msg.get(name):
            lines.append(f"{name}: {' '.join(str(msg.get(name)).split())}")
    if state is not None:
        lines.append(f"Status: {state} (reading it here does not change that)")
    if include_headers:
        lines.append("")
        lines.extend(f"{k}: {' '.join(str(v).split())[:500]}" for k, v in msg.items())
    if parts:
        lines.append("")
        lines.append("Attachments:")
        for item in parts:
            kind = "inline picture" if item.is_inline_picture else item.content_type
            where = "" if item.source != "missing" else ", not downloaded"
            lines.append(f"  part {item.part}: {item.filename} ({kind}, {ma.format_size(item.size)}{where})")
    lines.append("")
    if body is None:
        lines.append("(The body is not downloaded yet; open the message in Mail once.)")
    else:
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... cut at {max_chars} characters]"
        lines.append(text or "(empty body)")
    return "\n".join(lines)
