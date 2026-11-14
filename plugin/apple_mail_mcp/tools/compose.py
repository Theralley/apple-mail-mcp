"""Composition tools: sending, replying, forwarding, and drafts."""

import os
import subprocess
import tempfile
import re
from email.message import EmailMessage
from html import escape as html_escape
from email.headerregistry import Address
from pathlib import Path
from typing import Optional, List, Tuple, Union

from apple_mail_mcp import draft_gui, draft_mime, emlx, server
from apple_mail_mcp import envelope_index as ei
from apple_mail_mcp.server import mcp
from apple_mail_mcp.paths import validate_read_path, validate_write_path
from apple_mail_mcp.tools.raw_source import get_email_source
from apple_mail_mcp.tools.reply_verification import (
    verify_reply_body_outside_quote,
    verify_recipients,
)
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    FIELD_SEP,
    inject_preferences,
    escape_applescript,
    run_applescript,
    build_mailbox_ref,
    mail_automation_lock,
    MAIL_AUTOMATION_BUSY_ERROR,
)

# Explicit font stack for HTML fragments that get pasted into Mail's compose
# editor via NSPasteboard. Cocoa's HTML->NSAttributedString importer does not
# resolve CSS shorthand like `font: inherit`, and a fragment with no
# font-family at all resolves even worse (falls back to Times-Roman). Every
# fragment needs a concrete font-family value or it silently renders in a
# small serif font instead of the sans-serif Mail normally composes in.
# See #100.
_COMPOSE_FONT_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, "
    "sans-serif; font-size: 13px;"
)


def _split_addresses(value):
    """Return trimmed recipient addresses preserving order."""
    if not value:
        return []
    return [addr.strip() for addr in value.split(",") if addr and addr.strip()]


def _safe_eml_name(subject):
    """Return a filesystem-safe filename stem for draft exports."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (subject or "rich-email-draft").strip())
    cleaned = cleaned.strip("-._") or "rich-email-draft"
    return cleaned[:80]


def _default_rich_draft_path(subject):
    """A new private file for a generated rich draft (never an existing one)."""
    drafts_dir = Path.home() / "Library" / "Caches" / "apple-mail-mcp" / "rich-drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(drafts_dir, 0o700)
    fd, name = tempfile.mkstemp(prefix=_safe_eml_name(subject) + "-", suffix=".eml", dir=drafts_dir)
    os.close(fd)
    return Path(name)


def _checked_rich_draft_path(output_path):
    """(path, error): an .eml path the caller may write, inside the allowed folders."""
    if not str(output_path).lower().endswith(".eml"):
        return None, "Error: 'output_path' must end in .eml"
    _, guard_error = validate_write_path(str(output_path))
    if guard_error:
        return None, guard_error
    try:
        resolved = draft_mime.checked_local_path(str(output_path), what="output_path")
    except draft_mime.DraftError as exc:
        return None, f"Error: {exc}"
    if os.path.isdir(resolved):
        return None, f"Error: 'output_path' {output_path!r} is a folder"
    return Path(output_path).expanduser(), None


def _account_default_alias_if_single(account):
    """Return the sole alias of `account` when it has exactly one configured
    email address, else None. Used when no explicit sender is requested so
    that single-address accounts still send from their own alias rather than
    Mail's global "Send new messages from" preference.
    """
    safe_account = escape_applescript(account)
    script = f'''
    tell application "Mail"
        try
            set targetAccount to account "{safe_account}"
            set emailAddrs to email addresses of targetAccount
            if (count of emailAddrs) is 1 then
                return item 1 of emailAddrs
            end if
            return ""
        on error
            return ""
        end try
    end tell
    '''
    result = (run_applescript(script) or "").strip()
    return result or None


def _compose_sender_script(variable, account_ref, sender_override):
    """Return AppleScript that sets the sender for a compose/reply/forward
    outgoing message variable, respecting Mail's account-level defaults.

    With `sender_override` the value is applied unconditionally. Without an
    override, Mail's global composing preference may otherwise win over the
    caller's account choice, so the sender is pinned to the account's only
    alias when the account has a single address, and left untouched for
    multi-alias accounts so the user's Mail preference stays in effect.
    """
    if sender_override:
        safe_sender = escape_applescript(sender_override)
        return f'set sender of {variable} to "{safe_sender}"'
    return (
        f"set emailAddrs to email addresses of {account_ref}\n"
        f"if (count of emailAddrs) is 1 then\n"
        f"    set sender of {variable} to item 1 of emailAddrs\n"
        f"end if"
    )


def _validate_from_address(account, from_address):
    """Return (validated_address, error_message) for a sender override.

    When `from_address` is blank the override is skipped and both values
    are None. Otherwise the candidate is matched case-insensitively
    against the account's configured email addresses, and the original
    casing from Mail is returned on success.
    """
    if from_address is None:
        return None, None
    candidate = from_address.strip()
    if not candidate:
        return None, None
    safe_account = escape_applescript(account)
    script = f'''
    tell application "Mail"
        try
            set targetAccount to account "{safe_account}"
            set emailAddrs to email addresses of targetAccount
            set AppleScript's text item delimiters to linefeed
            set addrText to emailAddrs as text
            set AppleScript's text item delimiters to ""
            return addrText
        on error
            return ""
        end try
    end tell
    '''
    raw = run_applescript(script) or ""
    aliases = [line.strip() for line in raw.splitlines() if line.strip()]
    if not aliases:
        return None, (
            f"Error: Could not read email addresses for account {account!r}."
        )
    lowered = {alias.lower(): alias for alias in aliases}
    match = lowered.get(candidate.lower())
    if not match:
        return None, (
            f"Error: 'from_address' {candidate!r} is not configured on account "
            f"{account!r}. Known addresses: {', '.join(aliases)}"
        )
    return match, None


_CDATA_BLOCK_PATTERN = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _strip_cdata_wrappers(text):
    """Remove XML CDATA section markers from user-provided body content.

    LLM callers occasionally wrap email bodies in `<![CDATA[...]]>`. HTML
    parsers treat the opening `<![CDATA[` as a bogus comment that ends at
    the first `>` in the actual content, so it's invisible — but the
    trailing `]]>` has no preceding `<` and renders as literal text at the
    end of the message. Strip both forms so callers don't have to know.
    """
    if not text:
        return text
    text = _CDATA_BLOCK_PATTERN.sub(r"\1", text)
    return text.replace("<![CDATA[", "").replace("]]>", "")


def _build_html_from_text(text_body):
    """Return a simple HTML wrapper for plain text content."""
    safe_body = html_escape(text_body or "")
    return (
        f'<html><body style="{_COMPOSE_FONT_STYLE} line-height: 1.45; '
        'color: #111111;">'
        f'<pre style="white-space: pre-wrap; margin: 0; {_COMPOSE_FONT_STYLE}">'
        + safe_body
        + "</pre></body></html>"
    )


def _html_to_pasteboard_script(html_temp_path, pb_var="pb", old_clip_var="oldClip"):
    """Return an AppleScriptObjC snippet that loads HTML and places it on the
    general pasteboard as RICH TEXT (RTF) plus a plain-text fallback.

    Why RTF and not raw HTML: writing the HTML *source* under
    ``NSPasteboardTypeHTML`` (the previous approach) leaves Mail's compose
    editor with only an ambiguous ``public.html`` flavor and no plain-text
    representation. For anything other than a complete, well-formed HTML
    document Mail renders a best-effort version AND surfaces the literal
    markup, so the recipient sees the message twice — once rendered, once as
    raw ``<p>``/``<b>`` source. Converting the HTML to an
    ``NSAttributedString`` and writing it back as RTF gives Mail a single,
    unambiguous rich-text flavor it pastes deterministically as one rendered
    body; the accompanying plain-text flavor is the *rendered* text (tags
    stripped), so even a plain-text fallback never shows markup.

    The snippet stores the prior clipboard string in ``old_clip_var`` so the
    caller can restore it, and reads the HTML from ``html_temp_path`` (written
    by the Python caller to avoid AppleScript string-escaping issues).
    """
    return f"""set htmlString to do shell script "cat '{html_temp_path}'"
set {pb_var} to current application's NSPasteboard's generalPasteboard()
set {old_clip_var} to {pb_var}'s stringForType:(current application's NSPasteboardTypeString)
set htmlNSStr to current application's NSString's stringWithString:htmlString
set htmlBytes to htmlNSStr's dataUsingEncoding:(current application's NSUTF8StringEncoding)
-- Pin the HTML importer to UTF-8. Without NSCharacterEncodingDocumentAttribute the
-- Cocoa HTML reader auto-detects the charset and defaults to Latin-1/Windows-1252,
-- so the UTF-8 bytes for ä/ö/ü/ß/emoji get double-decoded into mojibake
-- (e.g. "Grüße" -> "GrÃ¼ÃŸe"). Passing NSUTF8StringEncoding fixes all multibyte text.
set htmlOptKeys to {{current application's NSDocumentTypeDocumentAttribute, current application's NSCharacterEncodingDocumentAttribute}}
set htmlOptVals to {{current application's NSHTMLTextDocumentType, current application's NSUTF8StringEncoding}}
set htmlOpts to current application's NSDictionary's dictionaryWithObjects:htmlOptVals forKeys:htmlOptKeys
set attrStr to current application's NSAttributedString's alloc()'s initWithData:htmlBytes options:htmlOpts documentAttributes:(missing value) |error|:(missing value)
{pb_var}'s clearContents()
if attrStr is not missing value then
    set attrRange to current application's NSMakeRange(0, attrStr's |length|())
    set rtfData to attrStr's RTFFromRange:attrRange documentAttributes:(current application's NSDictionary's dictionary())
    {pb_var}'s setData:rtfData forType:(current application's NSPasteboardTypeRTF)
    {pb_var}'s setString:(attrStr's |string|()) forType:(current application's NSPasteboardTypeString)
else
    -- Conversion failed (malformed HTML): fall back to pasting the raw string
    -- as plain text so the body is never silently empty.
    {pb_var}'s setString:htmlString forType:(current application's NSPasteboardTypeString)
end if"""


def _prepare_rich_bodies(subject, text_body, html_body):
    """Return plain-text and HTML bodies, filling sensible placeholders."""
    plain_body = text_body or ""
    rich_body = html_body or ""

    if not plain_body and not rich_body:
        plain_body = (
            "Draft outline\n\n"
            "- Add recipients\n"
            "- Add the final rich-text content\n"
            "- Review before sending"
        )
        rich_body = _build_html_from_text(plain_body)
        return plain_body, rich_body, ["body"]

    if rich_body and not plain_body:
        plain_body = (
            (subject.strip() + "\n\n" if subject and subject.strip() else "")
            + "This message contains rich HTML content. Open it in Mail for the rendered version."
        )

    if plain_body and not rich_body:
        rich_body = _build_html_from_text(plain_body)

    return plain_body, rich_body, []


@mcp.tool()
@inject_preferences
def create_rich_email_draft(
    account: str,
    subject: str = "",
    to: Optional[str] = None,
    text_body: Optional[str] = None,
    html_body: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    output_path: Optional[str] = None,
    open_in_mail: bool = True,
    save_as_draft: bool = False,
    from_address: Optional[str] = None,
) -> str:
    """
    Create a rich-text email draft by generating an unsent `.eml` message and optionally opening it in Mail.

    This is the preferred path for HTML or richly formatted emails because Mail reliably renders `.eml`
    content, while setting raw HTML through AppleScript often stores the literal markup instead.

    Args:
        account: Account name to use for the sender identity (e.g., "Work", "Oracle")
        subject: Subject line for the draft (optional; defaults to empty)
        to: Optional recipient email address(es), comma-separated for multiple
        text_body: Optional plain-text body. If omitted but html_body is provided, a fallback plain body is generated.
        html_body: Optional HTML body. If omitted but text_body is provided, a basic HTML wrapper is generated.
        cc: Optional CC recipients, comma-separated for multiple
        bcc: Optional BCC recipients, comma-separated for multiple
        output_path: Optional path for the generated `.eml` file (must end in .eml, inside your
            home folder or a temp folder; protected locations are rejected as for save_email_attachment)
        open_in_mail: If True, open the generated `.eml` in Mail (default: True)
        save_as_draft: If True, save a real draft in the account's Drafts mailbox instead,
            exactly as manage_drafts(action="create") does (with the account's signature;
            needs subject, to and a body). No `.eml` viewer is left open.
        from_address: Optional sender address to stamp into the `.eml` `From:` header. Must be one of the account's configured email addresses. When omitted, Mail fills the account's default "Send new messages from" address on open.

    Returns:
        Confirmation with the generated `.eml` path, missing details, and Mail-open/save status
    """
    if not account or not account.strip():
        return "Error: 'account' is required"

    text_body = _strip_cdata_wrappers(text_body)
    html_body = _strip_cdata_wrappers(html_body)

    if save_as_draft:
        return _create_clean_draft(
            account,
            subject=subject,
            to=to,
            cc=cc,
            bcc=bcc,
            text_body=text_body,
            html_body=html_body,
            from_address=from_address,
        )

    if output_path:
        draft_path, path_error = _checked_rich_draft_path(output_path)
        if path_error:
            return path_error

    sender_override, sender_error = _validate_from_address(account, from_address)
    if sender_error:
        return sender_error

    sender_address = sender_override or _account_default_alias_if_single(account)

    recipients_to = _split_addresses(to)
    recipients_cc = _split_addresses(cc)
    recipients_bcc = _split_addresses(bcc)
    plain_body, rich_body, body_missing = _prepare_rich_bodies(
        subject, text_body, html_body
    )

    missing_details = []
    if not subject or not subject.strip():
        missing_details.append("subject")
    if not recipients_to:
        missing_details.append("to")
    missing_details.extend(body_missing)

    message = EmailMessage()
    if subject:
        message["Subject"] = subject
    if sender_address:
        message["From"] = sender_address
    if recipients_to:
        message["To"] = ", ".join(recipients_to)
    if recipients_cc:
        message["Cc"] = ", ".join(recipients_cc)
    if recipients_bcc:
        message["Bcc"] = ", ".join(recipients_bcc)
    message["X-Unsent"] = "1"
    message.set_content(plain_body)
    message.add_alternative(rich_body, subtype="html")

    if not output_path:
        draft_path = _default_rich_draft_path(subject)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_bytes(bytes(message))

    opened = False
    if open_in_mail:
        subprocess.run(["open", "-a", "Mail", str(draft_path)], check=True)
        opened = True

    output_lines = ["RICH EMAIL DRAFT", "", "✓ Rich draft prepared successfully!", ""]
    output_lines.append("Account: " + account)
    output_lines.append("Subject: " + (subject if subject else "[empty]"))
    output_lines.append("EML path: " + str(draft_path))
    output_lines.append("Opened in Mail: " + ("yes" if opened else "no"))
    if open_in_mail:
        output_lines.append("Saved in Drafts: no (use save_as_draft=True for a real draft)")
    if sender_address:
        output_lines.append("From: " + sender_address)
    if recipients_to:
        output_lines.append("To: " + ", ".join(recipients_to))
    if recipients_cc:
        output_lines.append("CC: " + ", ".join(recipients_cc))
    if recipients_bcc:
        output_lines.append("BCC: " + ", ".join(recipients_bcc))
    output_lines.append(
        "Missing details: "
        + (", ".join(missing_details) if missing_details else "none")
    )
    output_lines.append(
        "Note: Prefer this `.eml` workflow for HTML email drafts; Mail renders it more reliably than raw HTML injected via AppleScript content."
    )
    return "\n".join(output_lines)


def _send_html_email(
    account: str,
    to: str,
    subject: str,
    body_plain: str,
    body_html: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments_script: str = "",
    attachment_info: str = "",
    mode: str = "send",
    sender_override: Optional[str] = None,
) -> str:
    """Send an HTML-formatted email via NSPasteboard clipboard injection.

    Uses AppleScriptObjC to place HTML on the clipboard with the proper
    pasteboard type, creates a compose window, tabs into the body, and
    pastes.  Then sends, saves as draft, or leaves open for review.
    """
    safe_account = escape_applescript(account)
    escaped_subject = escape_applescript(subject)

    # Build recipient scripts
    to_lines = ""
    for addr in [a.strip() for a in to.split(",") if a.strip()]:
        to_lines += f'make new to recipient at end of to recipients with properties {{address:"{escape_applescript(addr)}"}}\n'

    cc_lines = ""
    if cc:
        for addr in [a.strip() for a in cc.split(",") if a.strip()]:
            cc_lines += f'make new cc recipient at end of cc recipients with properties {{address:"{escape_applescript(addr)}"}}\n'

    bcc_lines = ""
    if bcc:
        for addr in [a.strip() for a in bcc.split(",") if a.strip()]:
            bcc_lines += f'make new bcc recipient at end of bcc recipients with properties {{address:"{escape_applescript(addr)}"}}\n'

    sender_script = _compose_sender_script(
        "newMsg", f'account "{safe_account}"', sender_override
    )

    # Mode-specific behaviour after paste
    if mode == "send":
        post_paste_script = """
            -- Send via keyboard shortcut
            keystroke "d" using {command down, shift down}
        """
        success_text = "Email sent successfully (HTML)"
    elif mode == "draft":
        post_paste_script = """
            -- Save as draft: Cmd+S then close
            keystroke "s" using command down
            delay 0.5
        """
        success_text = "Email saved as draft (HTML)"
    else:  # open
        post_paste_script = "-- Leaving open for review"
        success_text = (
            "Email opened in Mail for review (HTML). Edit and send when ready."
        )

    # Write HTML to temp file so the AppleScript can read it without
    # worrying about escaping quotes/special chars in the HTML string.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".html",
        prefix="mail_html_",
        delete=False,
        encoding="utf-8",
    )
    tmp.write(body_html)
    tmp.close()
    html_temp_path = tmp.name

    # Build the attachments block. Must run AFTER the HTML paste so that
    # Cmd+A in the body doesn't select-and-replace the attachment placeholder.
    # Empty block when no attachments — keeps AppleScript valid either way.
    if attachments_script.strip():
        attachments_block = (
            'tell application "Mail"\n'
            '    tell newMsg\n'
            f'        {attachments_script}\n'
            '    end tell\n'
            'end tell'
        )
    else:
        attachments_block = "-- (no attachments)"

    script = f'''
use framework "Foundation"
use framework "AppKit"
use scripting additions

-- Step 1: Read HTML from temp file and place it on the clipboard as RTF
-- (rendered rich text) plus a plain-text fallback. Writing the raw HTML
-- *source* under NSPasteboardTypeHTML made Mail paste the markup twice —
-- once rendered, once as literal tags; the RTF conversion fixes that.
{_html_to_pasteboard_script(html_temp_path)}

-- Step 2: Create compose window (empty body so signature doesn't interfere).
-- Attachments are deliberately NOT added here — see Step 4b for why.
tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{escaped_subject}", content:"", visible:true}}
    {sender_script}
    tell newMsg
        {to_lines}
        {cc_lines}
        {bcc_lines}
    end tell
    activate
end tell

-- Step 3: Wait for compose window to render
delay 2.5

-- Step 4a: Tab from header fields into body, then paste HTML.
-- The Cmd+A here selects the entire body (which is why we can't have the
-- attachment placed yet — it would be selected and replaced by the paste).
tell application "System Events"
    -- Poll until Mail is genuinely frontmost before driving the keyboard.
    -- A single `set frontmost` + fixed delay can fire the tab/paste keystrokes
    -- before the compose window has focus, producing a silent empty send.
    set focusWaited to 0
    repeat until (frontmost of process "Mail") or (focusWaited is greater than or equal to 40)
        try
            set frontmost of process "Mail" to true
        end try
        delay 0.1
        set focusWaited to focusWaited + 1
    end repeat
    delay 0.5
    tell process "Mail"
        -- Tab through: To -> Cc -> Bcc -> Subject -> Body
        -- 7 tabs covers all combinations of visible/hidden CC/BCC fields
        repeat 7 times
            key code 48
            delay 0.1
        end repeat
        delay 0.3

        -- Select all in body and paste HTML
        keystroke "a" using command down
        delay 0.2
        keystroke "v" using command down
        delay 0.5
    end tell
end tell

-- Step 4b: Attach files AFTER the HTML paste so they aren't clobbered by Cmd+A.
{attachments_block}

-- Step 4c: Final action (send / save draft / leave open)
tell application "System Events"
    tell process "Mail"
        {post_paste_script}
    end tell
end tell

-- Step 5: Clean up temp file
do shell script "rm -f '{html_temp_path}'"

-- Step 6: Restore clipboard
if oldClip is not missing value then
    pb's clearContents()
    pb's setString:oldClip forType:(current application's NSPasteboardTypeString)
end if

return "{success_text}"
'''

    try:
        with mail_automation_lock():
            result = subprocess.run(
                ["osascript", "-"],
                input=script.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            return f"Error sending HTML email: {stderr}"
        output = result.stdout.decode("utf-8", errors="replace").strip()
        # Build confirmation message
        confirm = f"{output}\n\nFrom: {account}\nTo: {to}\nSubject: {subject}"
        if cc:
            confirm += f"\nCC: {cc}"
        if bcc:
            confirm += f"\nBCC: {bcc}"
        if attachment_info:
            confirm += f"\nAttachments:\n{attachment_info.rstrip()}"
        return confirm
    except subprocess.TimeoutExpired:
        return "Error: HTML email script timed out"
    except TimeoutError:
        return MAIL_AUTOMATION_BUSY_ERROR
    finally:
        if os.path.exists(html_temp_path):
            os.unlink(html_temp_path)


def _validate_attachment_paths(attachments: str) -> Tuple[List[str], Optional[str]]:
    """Validate and resolve attachment file paths.

    Splits comma-separated paths, expands tildes, resolves symlinks, and
    enforces both guards: this fork's draft rule (home or temp folders only,
    no protected folders such as ~/.ssh or ~/.config, no key-like file
    names; draft_mime.checked_local_path) and the shared read guard from
    ``apple_mail_mcp.paths`` (secrets denylist, --attach-dir). The file must
    exist. Upstream 3.3.0 dropped the first rule, which made credential files
    under ~/.config attachable again; it is kept here on purpose.

    Returns:
        A tuple of (resolved_paths, error_message).
        If error_message is not None, resolved_paths should be ignored.
    """
    resolved_paths: List[str] = []
    raw_paths = [p.strip() for p in attachments.split(",")]

    for raw_path in raw_paths:
        if not raw_path:
            continue

        try:
            draft_mime.checked_local_path(raw_path)
        except draft_mime.DraftError as exc:
            return [], f"Error: {exc}"
        resolved, path_error = validate_read_path(raw_path)
        if path_error:
            return [], path_error

        # File must exist
        if not os.path.isfile(resolved):
            return [], f"Error: Attachment file does not exist: {resolved}"

        resolved_paths.append(resolved)

    if not resolved_paths:
        return [], "Error: No valid attachment paths provided."

    return resolved_paths, None


def _verify_saved_reply_body(
    account: str,
    message_id: Optional[str],
    reply_body: str,
    expected_cc: Optional[str] = None,
    expected_from: Optional[str] = None,
    body_html: Optional[str] = None,
) -> str:
    """Verify body placement and headers using the saved draft's RFC Message-ID.

    The save script excludes pre-existing Drafts IDs and accepts only one
    new draft with the exact reply subject. Never fall back to a subject
    lookup when that identity is unavailable or ambiguous. Send/open modes
    do not have a saved draft to inspect and do not call this helper.
    """
    if not message_id:
        return "Status: FAILED — could not uniquely identify the saved draft."
    try:
        raw_source = get_email_source(
            account=account, message_id=message_id, mailbox="Drafts"
        )
    except TimeoutError:
        # The draft is already saved; a busy automation lock only prevents
        # re-reading it. Report that, rather than letting the caller's lock
        # handler mask the successful save as a busy error.
        return (
            "Status: FAILED — Verification: could not re-read the saved draft "
            f"({MAIL_AUTOMATION_BUSY_ERROR})"
        )
    if raw_source.startswith("Error"):
        return f"Status: FAILED — Verification: could not re-read the saved draft ({raw_source})"

    body_verified, body_detail = verify_reply_body_outside_quote(
        raw_source, reply_body, body_html=body_html
    )
    body_status = "PASSED" if body_verified else "FAILED"
    lines = [f"Verification ({body_status}): {body_detail}"]

    problems = [] if body_verified else [body_detail]
    recipients_verified, recipients_detail = verify_recipients(
        raw_source, expected_cc=expected_cc, expected_from=expected_from
    )
    if not recipients_verified:
        problems.append(recipients_detail)
    recipients_status = "PASSED" if recipients_verified else "FAILED"
    lines.append(f"Recipients ({recipients_status}): {recipients_detail}")

    status = "Status: FAILED — " + "; ".join(problems) if problems else "Status: VERIFIED"
    return "\n".join([status] + lines)


@mcp.tool()
@inject_preferences
def reply_to_email(
    account: str,
    subject_keyword: str,
    reply_body: str,
    reply_to_all: bool = False,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    send: bool = True,
    mode: Optional[str] = None,
    attachments: Optional[str] = None,
    body_html: Optional[str] = None,
    from_address: Optional[str] = None,
    mailbox: str = "INBOX",
) -> str:
    """
    Reply to an email matching a subject keyword.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        subject_keyword: Keyword to search for in email subjects
        reply_body: The body text of the reply
        reply_to_all: If True, reply to all recipients; if False, reply only to sender (default: False)
        cc: Optional CC recipients, comma-separated for multiple
        bcc: Optional BCC recipients, comma-separated for multiple
        send: If True (default), send immediately; if False, save as draft. Ignored if mode is set.
        mode: Delivery mode — "send" (send immediately), "draft" (save silently), or "open" (open compose window for review). Overrides send parameter when set.
        attachments: Optional file paths to attach, comma-separated for multiple (e.g., "/path/to/file1.png,/path/to/file2.pdf")
        body_html: Optional HTML body for rich formatting (bold, headings, links, colors). When provided, the reply is pasted as HTML. The plain 'reply_body' field is still required as fallback text.
        from_address: Optional sender address to use for this reply. Must be one of the account's configured email addresses. When omitted, Mail uses the account's default "Send new messages from" setting.
        mailbox: Mailbox to look for the message in (default: "INBOX", which also matches localized inbox names). Use "/" for nested folders, e.g. "Projects/2026/Private".

    Returns:
        Confirmation message with details of the reply sent, saved draft, or opened draft
    """
    blocked = server.read_only_block("reply_to_email")
    if blocked:
        return blocked

    # Resolve the delivery mode and apply the read-only guard before anything
    # else: the temp files written below would otherwise leak on refusal.
    if mode is not None:
        if mode not in ("send", "draft", "open"):
            return f"Error: Invalid mode '{mode}'. Use: send, draft, open"
        effective_mode = mode
    else:
        effective_mode = "send" if send else "draft"

    if server.READ_ONLY and effective_mode == "send":
        return (
            "Error: Sending is disabled in read-only mode. "
            'Use mode="draft" to save the reply, or mode="open" to review it in Mail.'
        )

    reply_body = _strip_cdata_wrappers(reply_body) or ""
    body_html = _strip_cdata_wrappers(body_html)

    sender_override, sender_error = _validate_from_address(account, from_address)
    if sender_error:
        return sender_error

    # Escape all user inputs for AppleScript
    safe_account = escape_applescript(account)
    safe_subject_keyword = escape_applescript(subject_keyword)

    # Write reply body to a temp file to avoid AppleScript string escaping
    # issues with special characters (em dashes, curly quotes, colons, etc.)
    body_tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="mail_reply_",
        delete=False,
        encoding="utf-8",
    )
    body_tmp.write(reply_body)
    body_tmp.close()
    body_temp_path = body_tmp.name

    # If body_html provided, write it to a temp file for the AppleScript to read.
    # If plain text only, wrap it in basic HTML so the clipboard paste renders
    # properly in Mail's HTML compose view (preserving line breaks and gap).
    html_temp_path = None
    # Append an empty paragraph to create a visible gap before the quoted original.
    # Mail strips trailing <br> tags, so we use a <p> with &nbsp; instead.
    gap_html = "<div><br></div><div><br></div>"
    if body_html:
        html_content = body_html + gap_html
    else:
        # Wrap plain text in HTML, converting newlines to <br>
        escaped_plain = html_escape(reply_body)
        escaped_plain = escaped_plain.replace("\n", "<br>")
        html_content = (
            f'<div style="{_COMPOSE_FONT_STYLE}">{escaped_plain}</div>{gap_html}'
        )
    html_tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".html",
        prefix="mail_reply_html_",
        delete=False,
        encoding="utf-8",
    )
    html_tmp.write(html_content)
    html_tmp.close()
    html_temp_path = html_tmp.name

    # Build the reply command based on reply_to_all flag
    if reply_to_all:
        reply_command = "set replyMessage to reply foundMessage with opening window and reply to all"
    else:
        reply_command = "set replyMessage to reply foundMessage with opening window"

    # Build CC recipients if provided
    cc_script = ""
    if cc:
        cc_addresses = [addr.strip() for addr in cc.split(",")]
        for addr in cc_addresses:
            safe_addr = escape_applescript(addr)
            cc_script += f'''
            make new cc recipient at end of cc recipients of replyMessage with properties {{address:"{safe_addr}"}}
            '''

    # Build BCC recipients if provided
    bcc_script = ""
    if bcc:
        bcc_addresses = [addr.strip() for addr in bcc.split(",")]
        for addr in bcc_addresses:
            safe_addr = escape_applescript(addr)
            bcc_script += f'''
            make new bcc recipient at end of bcc recipients of replyMessage with properties {{address:"{safe_addr}"}}
            '''

    # Build attachment script if provided
    attachment_script = ""
    attachment_info = ""
    if attachments:
        validated_paths, error = _validate_attachment_paths(attachments)
        if error:
            return error
        for path in validated_paths:
            safe_path = escape_applescript(path)
            attachment_script += f'''
                set theFile to POSIX file "{safe_path}"
                make new attachment with properties {{file name:theFile}} at after the last paragraph
                delay 1
            '''
            attachment_info += f"  {path}\n"

    safe_cc = escape_applescript(cc) if cc else ""
    safe_bcc = escape_applescript(bcc) if bcc else ""
    safe_attachment_info = (
        escape_applescript(attachment_info) if attachment_info else ""
    )

    # Read body from temp file in AppleScript (avoids all string escaping issues)
    read_body_script = f'set replyBodyText to do shell script "cat " & quoted form of "{body_temp_path}"'

    # Determine behavior per mode
    # All modes use HTML clipboard paste (via NSPasteboard) to insert the reply body.
    # This preserves Mail.app's native quoted original in the HTML layer.
    # (setting `content` via AppleScript overwrites the HTML layer entirely,
    # destroying the email thread history.)

    if effective_mode == "send":
        header_text = "SENDING REPLY"
        post_paste_action = """
                delay 0.5
                tell application "Mail"
                    send replyMessage
                end tell"""
        success_text = "Reply sent successfully!"
    elif effective_mode == "open":
        header_text = "OPENING REPLY FOR REVIEW"
        # Nothing follows the paste in open mode, so the clipboard restore
        # would run only 0.5s after Cmd+V. Wait as long as send/draft do
        # before restoring, so a slow paste can't pick up the restored
        # clipboard instead of the reply body (#106).
        post_paste_action = """
                delay 0.5"""
        success_text = "Reply opened in Mail for review. Edit and send when ready."
    else:  # draft
        header_text = "SAVING REPLY AS DRAFT"
        post_paste_action = """
                delay 0.5
                tell application "Mail"
                    close window 1 saving yes
                end tell"""
        success_text = "Reply saved as draft!"

    draft_snapshot_script = ""
    draft_identity_script = ""
    if effective_mode == "draft":
        # Outgoing composer IDs are not RFC Message-IDs. Snapshot mailbox
        # IDs before creating the reply (which can autosave), then resolve
        # the one new exact-subject draft after close/save. Reject ambiguity
        # rather than risk verifying an older or concurrently created reply.
        # The snapshot has its own try: an account without a mailbox named
        # "Drafts" must still save the reply. With no snapshot the identity
        # lookup is skipped, so verification reports FAILED instead of
        # risking a match against an older draft.
        draft_snapshot_script = f"""
            set draftMailbox to missing value
            set priorDraftIDs to {{}}
            set draftSnapshotOK to false
            try
                {build_mailbox_ref("Drafts", var_name="draftMailbox")}
                set priorDraftIDs to id of every message of draftMailbox
                set draftSnapshotOK to true
            end try
        """
        post_paste_action = """
                set savedReplySubject to subject of replyMessage
                delay 0.5
                close window 1 saving yes
        """
        # New candidates are grouped by RFC Message-ID: Gmail can leave
        # several autosave/sync copies of one draft, all sharing a
        # Message-ID. Two distinct Message-IDs are still rejected as ambiguous.
        draft_identity_script = """
            set savedDraftMessageID to ""
            if draftSnapshotOK then
                repeat 20 times
                    set newDraftMessageIDs to {}
                    set matchingDrafts to (messages of draftMailbox whose subject is savedReplySubject)
                    repeat with candidate in matchingDrafts
                        if priorDraftIDs does not contain (id of candidate) then
                            set candidateMessageID to message id of candidate
                            if candidateMessageID is missing value then set candidateMessageID to ""
                            if newDraftMessageIDs does not contain candidateMessageID then
                                set end of newDraftMessageIDs to candidateMessageID
                            end if
                        end if
                    end repeat
                    if (count of newDraftMessageIDs) is greater than 1 then exit repeat
                    if (count of newDraftMessageIDs) is 1 then
                        set candidateMessageID to item 1 of newDraftMessageIDs
                        if candidateMessageID is not "" then
                            set savedDraftMessageID to candidateMessageID
                            exit repeat
                        end if
                    end if
                    delay 0.25
                end repeat
            else
                set outputText to outputText & "Note: could not read the Drafts mailbox, so the saved draft cannot be verified." & return
            end if
            set outputText to outputText & "Saved draft Message-ID: " & savedDraftMessageID & return
        """

    cleanup_script = f'do shell script "rm -f " & quoted form of "{body_temp_path}"'
    html_cleanup_script = f'do shell script "rm -f \'{html_temp_path}\'"'

    sender_script = _compose_sender_script(
        "replyMessage", "targetAccount", sender_override
    )

    script = f'''
use framework "Foundation"
use framework "AppKit"
use scripting additions

-- Step 1: Place the reply body on the clipboard as RTF (rendered rich text)
-- plus a plain-text fallback. The previous raw-HTML-source approach made
-- Mail paste the body twice (rendered + literal tags); RTF pastes once.
{_html_to_pasteboard_script(html_temp_path)}

-- Step 2: Find the email and create reply
tell application "Mail"
    set outputText to "{header_text}" & return & return

    try
        -- Read reply body from temp file (for output text only)
        {read_body_script}

        set targetAccount to account "{safe_account}"
        {build_mailbox_ref(mailbox, account_var="targetAccount", var_name="inboxMailbox")}
        set inboxMessages to every message of inboxMailbox
        set foundMessage to missing value

        -- Find the first matching message
        repeat with aMessage in inboxMessages
            try
                set messageSubject to subject of aMessage

                if messageSubject contains "{safe_subject_keyword}" then
                    set foundMessage to aMessage
                    exit repeat
                end if
            end try
        end repeat

        if foundMessage is not missing value then
            set messageSubject to subject of foundMessage
            set messageSender to sender of foundMessage
            set messageDate to date received of foundMessage

            -- Snapshot before creation, since Mail may autosave the reply.
            {draft_snapshot_script}

            -- Create reply
            {reply_command}
            delay 0.5

            {sender_script}

            -- Add CC/BCC recipients
            {cc_script}
            {bcc_script}

            -- Add attachments (must be scoped to replyMessage so `last paragraph` resolves;
            -- bare `make new attachment ... at after the last paragraph` inside `tell application "Mail"`
            -- raises "Can't get last paragraph" because the reference has no implicit owner)
            tell replyMessage
                {attachment_script}
            end tell

            -- Paste reply body (HTML already on clipboard from Step 1).
            -- The body is inserted via a blind Cmd+V into the compose window, so the
            -- window MUST have keyboard focus when the keystroke fires. If Mail is not
            -- frontmost at that moment (common on the first compose of a session, when
            -- another app holds focus, or when the machine is busy), the paste lands
            -- nowhere and an EMPTY reply is sent with no error returned.
            --
            -- We deliberately do NOT verify the paste by reading the reply's scriptable
            -- `content` property afterwards: Mail's GUI editor buffer is decoupled from
            -- that property, so reading it always returns the pre-paste
            -- value (verified empirically — it reports 0 chars even after a successful
            -- paste). A content-read-back guard would therefore false-fail every send.
            -- Instead we make the paste deterministic by polling until Mail is genuinely
            -- frontmost (re-asserting focus each iteration) before issuing the keystroke.
            set visible of replyMessage to true
            activate

            tell application "System Events"
                set focusWaited to 0
                repeat until (frontmost of process "Mail") or (focusWaited is greater than or equal to 40)
                    try
                        set frontmost of process "Mail" to true
                    end try
                    delay 0.1
                    set focusWaited to focusWaited + 1
                end repeat
                -- settle delay so the compose window's body holds keyboard focus
                delay 0.8
                tell process "Mail"
                    keystroke "v" using command down
                end tell
            end tell
            delay 0.5

            {post_paste_action}
            {draft_identity_script}

            set outputText to outputText & "{success_text}" & return
            set outputText to outputText & "To: " & messageSender & return
            set outputText to outputText & "Subject: " & messageSubject & return
    '''

    if cc:
        script += f"""
                set outputText to outputText & "CC: {safe_cc}" & return
    """

    if bcc:
        script += f"""
                set outputText to outputText & "BCC: {safe_bcc}" & return
    """

    if attachments:
        script += f'''
                set outputText to outputText & "Attachments:" & return & "{safe_attachment_info}" & return
    '''

    script += f"""
            else
                set outputText to outputText & "No email found matching: {safe_subject_keyword} (searched mailbox: {escape_applescript(mailbox)})" & return
            end if

            -- Clean up temp files
            {cleanup_script}
            {html_cleanup_script}

        on error errMsg
            -- Clean up temp files even on error
            try
                {cleanup_script}
                {html_cleanup_script}
            end try
            set outputText to "Error: " & errMsg & return & "Please check that the account name is correct and the email exists."
        end try
    end tell

    -- Restore clipboard. Every path must fall through to here: a `return`
    -- inside the tell block above would skip this and leave the reply body
    -- on the user's clipboard (#106).
    if oldClip is not missing value then
        pb's clearContents()
        pb's setString:oldClip forType:(current application's NSPasteboardTypeString)
    end if

    return outputText
    """

    try:
        # Use osascript directly for AppleScriptObjC (use framework) support
        with mail_automation_lock():
            result = subprocess.run(
                ["osascript", "-"],
                input=script.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            return f"Error in reply: {stderr}"
        output_text = result.stdout.decode("utf-8", errors="replace").strip()
        # Checking for the absence of "Error" is not enough: "No email found
        # matching: <keyword>" is also a failure to reach a saved draft, and
        # does not start with "Error" either. `success_text` is the one
        # positive signal this function's own AppleScript emits only on the
        # branch that actually saved something.
        if effective_mode == "draft" and success_text in output_text:
            identity_lines = [
                line.removeprefix("Saved draft Message-ID: ").strip()
                for line in output_text.splitlines()
                if line.startswith("Saved draft Message-ID: ")
            ]
            saved_message_id = identity_lines[0] if len(identity_lines) == 1 else None
            verification = _verify_saved_reply_body(
                account,
                saved_message_id,
                reply_body,
                expected_cc=cc,
                expected_from=sender_override,
                body_html=body_html,
            )
            output_text = verification + "\n" + output_text
        return output_text
    except subprocess.TimeoutExpired:
        return "Error: Reply script timed out"
    except TimeoutError:
        return MAIL_AUTOMATION_BUSY_ERROR
    finally:
        # Belt-and-suspenders cleanup in case AppleScript didn't run
        if os.path.exists(body_temp_path):
            os.unlink(body_temp_path)
        if html_temp_path and os.path.exists(html_temp_path):
            os.unlink(html_temp_path)


@mcp.tool()
@inject_preferences
def compose_email(
    account: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[str] = None,
    mode: str = "send",
    body_html: Optional[str] = None,
    from_address: Optional[str] = None,
) -> str:
    """
    Compose and send a new email from a specific account.

    Args:
        account: Account name to send from (e.g., "Gmail", "Work", "Personal")
        to: Recipient email address(es), comma-separated for multiple
        subject: Email subject line
        body: Email body text (used as plain-text fallback when body_html is provided)
        cc: Optional CC recipients, comma-separated for multiple
        bcc: Optional BCC recipients, comma-separated for multiple
        attachments: Optional file paths to attach, comma-separated for multiple (e.g., "/path/to/file1.png,/path/to/file2.pdf")
        mode: Delivery mode — "send" (send immediately, default), "draft" (save to Drafts exactly as manage_drafts(action="create") does; Mail comes to the front for a few seconds), or "open" (open compose window for review before sending)
        body_html: Optional HTML body for rich formatting (bold, headings, links, colors). When provided, the email is sent as HTML. The plain 'body' field is still required as fallback text.
        from_address: Optional sender address to use for this message. Must be one of the account's configured email addresses. When omitted, Mail uses the account's default "Send new messages from" setting.

    Returns:
        Confirmation message with details of the email
    """
    blocked = server.read_only_block("compose_email")
    if blocked:
        return blocked

    # Validate mode
    if mode not in ("send", "draft", "open"):
        return f"Error: Invalid mode '{mode}'. Use: send, draft, open"

    if server.READ_ONLY and mode == "send":
        return (
            "Error: Sending is disabled in read-only mode. "
            'Use mode="draft" to save the message, or mode="open" to review it in Mail.'
        )

    body = _strip_cdata_wrappers(body) or ""
    body_html = _strip_cdata_wrappers(body_html)

    if mode == "draft":
        # A body scripted into an outgoing message is stored as quoted
        # "shared content"; drafts go through Mail's own compose window.
        return _create_clean_draft(
            account,
            subject=subject,
            to=to,
            cc=cc,
            bcc=bcc,
            text_body=body or None,
            html_body=body_html,
            attachments=attachments,
            from_address=from_address,
        )

    # Validate optional sender override
    sender_override, sender_error = _validate_from_address(account, from_address)
    if sender_error:
        return sender_error

    # Validate and resolve attachments early
    attachment_script = ""
    attachment_info = ""
    if attachments:
        validated_paths, error = _validate_attachment_paths(attachments)
        if error:
            return error
        for path in validated_paths:
            safe_path = escape_applescript(path)
            attachment_script += f'''
                set theFile to POSIX file "{safe_path}"
                make new attachment with properties {{file name:theFile}} at after the last paragraph
                delay 1
            '''
            attachment_info += f"  {path}\n"

    # --- HTML path: use NSPasteboard clipboard injection ---
    if body_html:
        return _send_html_email(
            account=account,
            to=to,
            subject=subject,
            body_plain=body,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            attachments_script=attachment_script,
            attachment_info=attachment_info,
            mode=mode,
            sender_override=sender_override,
        )

    # --- Plain-text path: existing AppleScript approach ---
    safe_account = escape_applescript(account)
    escaped_subject = escape_applescript(subject)
    escaped_body = escape_applescript(body)

    # Build TO recipients (split comma-separated addresses)
    to_script = ""
    to_addresses = [addr.strip() for addr in to.split(",")]
    for addr in to_addresses:
        safe_addr = escape_applescript(addr)
        to_script += f'''
                make new to recipient at end of to recipients with properties {{address:"{safe_addr}"}}
        '''

    # Build CC recipients if provided
    cc_script = ""
    if cc:
        cc_addresses = [addr.strip() for addr in cc.split(",")]
        for addr in cc_addresses:
            safe_addr = escape_applescript(addr)
            cc_script += f'''
                make new cc recipient at end of cc recipients with properties {{address:"{safe_addr}"}}
            '''

    # Build BCC recipients if provided
    bcc_script = ""
    if bcc:
        bcc_addresses = [addr.strip() for addr in bcc.split(",")]
        for addr in bcc_addresses:
            safe_addr = escape_applescript(addr)
            bcc_script += f'''
                make new bcc recipient at end of bcc recipients with properties {{address:"{safe_addr}"}}
            '''

    safe_to = escape_applescript(to)
    safe_cc = escape_applescript(cc) if cc else ""
    safe_bcc = escape_applescript(bcc) if bcc else ""
    safe_attachment_info = (
        escape_applescript(attachment_info) if attachment_info else ""
    )

    sender_script = _compose_sender_script(
        "newMessage", "targetAccount", sender_override
    )

    # Determine behavior per mode
    if mode == "send":
        header_text = "COMPOSING EMAIL"
        visible = "false"
        send_command = "send newMessage"
        success_text = "✓ Email sent successfully!"
    elif mode == "open":
        header_text = "OPENING EMAIL FOR REVIEW"
        visible = "true"
        send_command = "activate"
        success_text = "✓ Email opened in Mail for review. Edit and send when ready."
    else:  # draft
        header_text = "SAVING EMAIL AS DRAFT"
        visible = "false"
        send_command = "close window 1 saving yes"
        success_text = "✓ Email saved as draft!"

    script = f'''
    tell application "Mail"
        set outputText to "{header_text}" & return & return

        try
            set targetAccount to account "{safe_account}"

            -- Create new outgoing message
            set newMessage to make new outgoing message with properties {{subject:"{escaped_subject}", content:"{escaped_body}", visible:{visible}}}

            {sender_script}

            -- Add TO/CC/BCC recipients
            tell newMessage
                {to_script}
                {cc_script}
                {bcc_script}
            end tell

            -- Add attachments
            tell newMessage
                {attachment_script}
            end tell

            -- Send, save as draft, or leave open for review
            {send_command}

            set outputText to outputText & "{success_text}" & return
            set outputText to outputText & "To: {safe_to}" & return
            set outputText to outputText & "Subject: {escaped_subject}" & return
    '''

    if cc:
        script += f"""
            set outputText to outputText & "CC: {safe_cc}" & return
    """

    if bcc:
        script += f"""
            set outputText to outputText & "BCC: {safe_bcc}" & return
    """

    if attachments:
        script += f'''
            set outputText to outputText & "Attachments:" & return & "{safe_attachment_info}" & return
    '''

    script += f'''

        on error errMsg
            return "Error: " & errMsg & return & "Please check that the account name and email addresses are correct."
        end try

        return outputText
    end tell
    '''

    result = run_applescript(script)
    return result


@mcp.tool()
@inject_preferences
def forward_email(
    account: str,
    subject_keyword: str,
    to: str,
    message: Optional[str] = None,
    mailbox: str = "INBOX",
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    from_address: Optional[str] = None,
) -> str:
    """
    Forward an email to one or more recipients.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        subject_keyword: Keyword to search for in email subjects
        to: Recipient email address(es), comma-separated for multiple
        message: Optional message to add before forwarded content
        mailbox: Mailbox to search in (default: "INBOX")
        cc: Optional CC recipients, comma-separated for multiple
        bcc: Optional BCC recipients, comma-separated for multiple
        from_address: Optional sender address to use when forwarding. Must be one of the account's configured email addresses. When omitted, Mail uses the account's default "Send new messages from" setting.

    Returns:
        Confirmation message with details of forwarded email
    """
    blocked = server.read_only_block("forward_email")
    if blocked:
        return blocked

    # forward_email is removed from the tool list under --read-only; this
    # guard keeps it safe even if that removal ever fails.
    if server.READ_ONLY:
        return "Error: Forwarding is disabled in read-only mode."

    message = _strip_cdata_wrappers(message)

    sender_override, sender_error = _validate_from_address(account, from_address)
    if sender_error:
        return sender_error

    # Escape all user inputs for AppleScript
    safe_account = escape_applescript(account)
    safe_subject_keyword = escape_applescript(subject_keyword)
    safe_to = escape_applescript(to)
    safe_mailbox = escape_applescript(mailbox)
    escaped_message = escape_applescript(message) if message else ""

    sender_script = _compose_sender_script(
        "forwardMessage", "targetAccount", sender_override
    )

    # Build CC recipients if provided
    cc_script = ""
    if cc:
        cc_addresses = [addr.strip() for addr in cc.split(",")]
        for addr in cc_addresses:
            safe_addr = escape_applescript(addr)
            cc_script += f'''
            make new cc recipient at end of cc recipients of forwardMessage with properties {{address:"{safe_addr}"}}
            '''

    # Build BCC recipients if provided
    bcc_script = ""
    if bcc:
        bcc_addresses = [addr.strip() for addr in bcc.split(",")]
        for addr in bcc_addresses:
            safe_addr = escape_applescript(addr)
            bcc_script += f'''
            make new bcc recipient at end of bcc recipients of forwardMessage with properties {{address:"{safe_addr}"}}
            '''

    safe_cc = escape_applescript(cc) if cc else ""
    safe_bcc = escape_applescript(bcc) if bcc else ""

    # Build TO recipients (split comma-separated)
    to_script = ""
    to_addresses = [addr.strip() for addr in to.split(",")]
    for addr in to_addresses:
        safe_addr = escape_applescript(addr)
        to_script += f'''
                make new to recipient at end of to recipients of forwardMessage with properties {{address:"{safe_addr}"}}
        '''

    # If an optional message is provided, write it as HTML to a temp file
    # for NSPasteboard clipboard injection (preserves forwarded content).
    fwd_html_temp_path = None
    fwd_html_paste_script = ""
    fwd_html_cleanup_script = ""
    if message:
        escaped_plain = html_escape(message)
        escaped_plain = escaped_plain.replace("\n", "<br>")
        fwd_html_content = (
            f'<div style="{_COMPOSE_FONT_STYLE}">{escaped_plain}<br><br></div>'
        )
        fwd_html_tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            prefix="mail_fwd_html_",
            delete=False,
            encoding="utf-8",
        )
        fwd_html_tmp.write(fwd_html_content)
        fwd_html_tmp.close()
        fwd_html_temp_path = fwd_html_tmp.name
        fwd_html_cleanup_script = f'do shell script "rm -f \'{fwd_html_temp_path}\'"'
        fwd_html_paste_script = f"""
                set visible of forwardMessage to true
                activate
                delay 1.5

                -- Place the prepended note on the clipboard as RTF (rendered)
                -- plus a plain-text fallback, instead of raw HTML source which
                -- Mail would paste twice (rendered + literal tags).
                {_html_to_pasteboard_script(fwd_html_temp_path)}

                tell application "System Events"
                    tell process "Mail"
                        keystroke "v" using command down
                    end tell
                end tell
                delay 0.5

                if oldClip is not missing value then
                    pb's clearContents()
                    pb's setString:oldClip forType:(current application's NSPasteboardTypeString)
                end if
        """

    # If the script fails after the note was put on the clipboard, restore the
    # user's clipboard before returning. Wrapped in `try` because oldClip is
    # undefined when the error happens before the paste step (#106).
    fwd_clipboard_restore_on_error = ""
    if message:
        fwd_clipboard_restore_on_error = """try
                if oldClip is not missing value then
                    pb's clearContents()
                    pb's setString:oldClip forType:(current application's NSPasteboardTypeString)
                end if
            end try"""

    use_frameworks = ""
    if message:
        use_frameworks = """use framework "Foundation"
use framework "AppKit"
use scripting additions
"""

    script = f'''{use_frameworks}
tell application "Mail"
    set outputText to "FORWARDING EMAIL" & return & return

    try
        set targetAccount to account "{safe_account}"
        -- Try to get mailbox
        try
            set targetMailbox to mailbox "{safe_mailbox}" of targetAccount
        on error
            if "{safe_mailbox}" is "INBOX" then
                set targetMailbox to mailbox "Inbox" of targetAccount
            else
                error "Mailbox not found: {safe_mailbox}"
            end if
        end try

        set mailboxMessages to every message of targetMailbox
        set foundMessage to missing value

        -- Find the first matching message
        repeat with aMessage in mailboxMessages
            try
                set messageSubject to subject of aMessage

                if messageSubject contains "{safe_subject_keyword}" then
                    set foundMessage to aMessage
                    exit repeat
                end if
            end try
        end repeat

        if foundMessage is not missing value then
            set messageSubject to subject of foundMessage
            set messageSender to sender of foundMessage
            set messageDate to date received of foundMessage

            -- Create forward
            set forwardMessage to forward foundMessage with opening window

            {sender_script}

            -- Add recipients
            {to_script}

            -- Add CC/BCC recipients
            {cc_script}
            {bcc_script}

            -- Add optional message via HTML clipboard paste (preserves forwarded content)
            {fwd_html_paste_script}

            -- Send the forward
            send forwardMessage

            -- Clean up temp files
            {fwd_html_cleanup_script}

            set outputText to outputText & "Email forwarded successfully." & return
            set outputText to outputText & "To: {safe_to}" & return
            set outputText to outputText & "Subject: " & messageSubject & return
    '''

    if cc:
        script += f"""
                set outputText to outputText & "CC: {safe_cc}" & return
    """

    if bcc:
        script += f"""
                set outputText to outputText & "BCC: {safe_bcc}" & return
    """

    script += f"""
            else
                set outputText to outputText & "⚠ No email found matching: {safe_subject_keyword}" & return
            end if

        on error errMsg
            try
                {fwd_html_cleanup_script}
            end try
            {fwd_clipboard_restore_on_error}
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    """

    try:
        if message:
            # Use osascript directly for AppleScriptObjC (use framework) support
            with mail_automation_lock():
                result = subprocess.run(
                    ["osascript", "-"],
                    input=script.encode("utf-8"),
                    capture_output=True,
                    timeout=30,
                )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                return f"Error forwarding email: {stderr}"
            return result.stdout.decode("utf-8", errors="replace").strip()
        else:
            return run_applescript(script)
    except subprocess.TimeoutExpired:
        return "Error: Forward script timed out"
    except TimeoutError:
        return MAIL_AUTOMATION_BUSY_ERROR
    finally:
        if fwd_html_temp_path and os.path.exists(fwd_html_temp_path):
            os.unlink(fwd_html_temp_path)


def _drafts_matching(safe_account: str, draft_subject: str):
    """Drafts in the account whose subject contains ``draft_subject``.

    Returns ``[(id, subject, created), ...]`` or an error string. The decision
    about which draft to touch is made here in Python, not inside the script:
    the old delete took the first subject match, so two drafts with the same
    subject (a draft and its corrected copy) meant deleting one at random.
    """
    safe_draft_subject = escape_applescript(draft_subject)
    script = f'''
    tell application "Mail"
        try
            set draftsMailbox to mailbox "Drafts" of account "{safe_account}"
            set outputText to ""
            repeat with aDraft in (every message of draftsMailbox)
                try
                    set draftSubject to subject of aDraft
                    if draftSubject contains "{safe_draft_subject}" then
                        set draftCreated to ""
                        try
                            set draftCreated to (date sent of aDraft) as string
                        end try
                        set outputText to outputText & ((id of aDraft) as string) & tab & draftSubject & tab & draftCreated & linefeed
                    end if
                end try
            end repeat
            return "OK" & linefeed & outputText
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    raw = run_applescript(script) or ""
    lines = raw.strip().splitlines()
    if not lines or lines[0].strip() != "OK":
        return raw.strip() or "Error: could not read the Drafts mailbox"
    matches = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            created = parts[2].strip() if len(parts) > 2 else ""
            matches.append((int(parts[0]), parts[1], created))
    return matches


def _account_identity(account):
    """(full name, [addresses]) of a Mail account, addresses in Mail's order."""
    safe_account = escape_applescript(account)
    script = f'''
    tell application "Mail"
        try
            set targetAccount to account "{safe_account}"
            set fullName to ""
            try
                set fn to full name of targetAccount
                if fn is not missing value then set fullName to fn as text
            end try
            set AppleScript's text item delimiters to {AS_FIELD_SEP}
            set addrText to (email addresses of targetAccount) as text
            set AppleScript's text item delimiters to ""
            return "ID" & {AS_FIELD_SEP} & fullName & {AS_FIELD_SEP} & addrText
        on error
            return ""
        end try
    end tell
    '''
    # Fields are separated by the unit separator, which output cleaning keeps
    # (an empty name would otherwise vanish with the stripped whitespace).
    fields = (run_applescript(script) or "").split(FIELD_SEP)
    if len(fields) < 2 or fields[0].strip() != "ID":
        return "", []
    full_name = fields[1].strip()
    addresses = [field.strip() for field in fields[2:] if field.strip()]
    return full_name, addresses


def _create_clean_draft(
    account,
    *,
    subject,
    to,
    cc=None,
    bcc=None,
    text_body=None,
    html_body=None,
    attachments=None,
    from_address=None,
    include_signature=True,
):
    """Save a draft through Mail's own compose window (see draft_gui). Never sends."""
    if not subject or not subject.strip() or not to or not (text_body or html_body):
        return "Error: 'subject', 'to', and 'body' (or 'html_body') are required for creating drafts"
    subject = subject.strip()
    try:
        loaded = draft_mime.load_attachments(attachments)
    except draft_mime.DraftError as exc:
        return f"Error: {exc}"

    full_name, addresses = _account_identity(account)
    if not addresses:
        return f"Error: Could not read email addresses for account {account!r}."
    if from_address and from_address.strip():
        sender_address = {a.lower(): a for a in addresses}.get(from_address.strip().lower())
        if not sender_address:
            return (
                f"Error: 'from_address' {from_address.strip()!r} is not configured on account "
                f"{account!r}. Known addresses: {', '.join(addresses)}"
            )
    else:
        sender_address = addresses[0]
    try:
        sender = str(Address(display_name=full_name, addr_spec=sender_address)) if full_name else sender_address
    except (ValueError, IndexError):
        sender = sender_address

    try:
        account_uuid = ei.account_uuid(account)
    except ei.IndexUnavailable as exc:
        return f"Error: Mail's index does not know account {account!r} ({exc}); the draft could not be checked, so none was made."

    signature, signature_note = None, "none (include_signature=False)"
    if include_signature:
        signature, signature_note = draft_mime.resolve_signature(account_uuid)

    try:
        msg = draft_mime.build_draft(
            subject=subject, sender=sender, to=to, cc=cc, bcc=bcc,
            text_body=text_body, html_body=html_body, attachments=loaded, signature=signature,
        )
    except draft_mime.DraftError as exc:
        return f"Error: {exc}"

    # Mail keeps only the HTML of a draft, so check against what the HTML says.
    plain_check = emlx.html_to_text(draft_mime.html_fragment(html_body)) if html_body else text_body
    result = draft_gui.create_in_mail(
        msg,
        account_uuid=account_uuid,
        subject=subject,
        plain_body=plain_check,
        attachments=[(item.filename, item.sha256) for item in loaded],
        sender=sender_address,
    )

    lines = ["CREATING DRAFT", ""]
    if result.ok:
        lines.append(f"✓ Draft saved in Drafts (ID {result.draft_id}). It was not sent.")
    elif result.draft_id is not None:
        lines.append(f"⚠ Draft saved in Drafts (ID {result.draft_id}), but check it in Mail before sending.")
    else:
        lines.append("Error: the draft was not created.")
    lines.extend(result.lines)
    lines.append("")
    lines.append(f"Subject: {subject}")
    lines.append(f"From: {sender}")
    lines.append(f"To: {msg['To']}")
    if msg["Cc"]:
        lines.append(f"CC: {msg['Cc']}")
    if msg["Bcc"]:
        lines.append(f"BCC: {msg['Bcc']}")
    if loaded:
        lines.append("Attachments: " + ", ".join(
            f"{item.filename} ({len(item.data):,} bytes)" for item in loaded))
    lines.append(f"Signature: {signature_note}")
    if result.eml_path is not None:
        lines.append(f"The finished message is kept at {result.eml_path}; open it in Mail and "
                     "use Message > Send Again to get an editable copy.")
    return "\n".join(lines)


@mcp.tool()
@inject_preferences
def manage_drafts(
    account: str,
    action: str,
    subject: Optional[str] = None,
    to: Optional[str] = None,
    body: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    draft_subject: Optional[str] = None,
    from_address: Optional[str] = None,
    draft_id: Optional[int] = None,
    html_body: Optional[str] = None,
    attachments: Optional[Union[str, List[str]]] = None,
    include_signature: bool = True,
) -> str:
    """
    Manage draft emails - list, create, send, open, or delete drafts.

    Under --read-only only "create", "list" and "delete" are allowed.

    action="create" saves a real draft in the account's Drafts mailbox, with
    the account's own Mail signature and any attachments, and never sends it.
    Mail comes to the front for a few seconds while it saves the draft, then
    the app you were in gets the front back; don't type meanwhile. The stored
    draft is read back and checked (body, sender, attachments, and no "> "
    quoting) before success is reported. If Mail cannot do it, the finished
    message is left as an .eml file whose path is returned.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        action: Action to perform: "list", "create", "send", "open", "delete". Use "open" to open a draft in a visible compose window for review before sending.
        subject: Email subject (required for create)
        to: Recipient email(s) for create (comma-separated)
        body: Plain-text email body (create; this or html_body is required)
        cc: Optional CC recipients for create
        bcc: Optional BCC recipients for create
        draft_subject: Subject keyword to find draft (required for send/open/delete)
        from_address: Optional sender address for new drafts (action="create"). Must be one of the account's configured email addresses. When omitted, the account's first address is used.
        draft_id: Exact draft ID as shown by action="list" (delete only). Required when several drafts match draft_subject; delete never guesses between them.
        html_body: Optional HTML body for create, instead of or with body (body is then the plain-text version)
        attachments: Files to attach for create: a list of paths, or one string with paths separated by commas or new lines. At most 10 files and 25 MB, from your home folder or a temp folder.
        include_signature: Add the account's Mail signature (create; default True). Skipped when the account has several signatures and none is selected.

    Returns:
        Formatted output based on action
    """
    blocked = server.read_only_block("manage_drafts", action)
    if blocked:
        return blocked

    body = _strip_cdata_wrappers(body)

    # Escape account for all paths
    safe_account = escape_applescript(account)

    if action == "list":
        script = f'''
        tell application "Mail"
            set outputText to "DRAFT EMAILS - {safe_account}" & return & return

            try
                set targetAccount to account "{safe_account}"
                set draftsMailbox to mailbox "Drafts" of targetAccount
                set draftMessages to every message of draftsMailbox
                set draftCount to count of draftMessages

                set outputText to outputText & "Found " & draftCount & " draft(s)" & return & return

                repeat with aDraft in draftMessages
                    try
                        set draftSubject to subject of aDraft
                        set draftDate to date sent of aDraft

                        set outputText to outputText & "✉ " & draftSubject & return
                        set outputText to outputText & "   Created: " & (draftDate as string) & return
                        set outputText to outputText & "   ID: " & ((id of aDraft) as string) & return & return
                    end try
                end repeat

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    elif action == "create":
        return _create_clean_draft(
            account,
            subject=subject,
            to=to,
            cc=cc,
            bcc=bcc,
            text_body=body,
            html_body=_strip_cdata_wrappers(html_body),
            attachments=attachments,
            from_address=from_address,
            include_signature=include_signature,
        )

    elif action == "send":
        if server.READ_ONLY:
            return "Error: Sending drafts is disabled in read-only mode."
        if not draft_subject:
            return "Error: 'draft_subject' is required for sending drafts"

        safe_draft_subject = escape_applescript(draft_subject)

        script = f'''
        tell application "Mail"
            set outputText to "SENDING DRAFT" & return & return

            try
                set targetAccount to account "{safe_account}"
                set draftsMailbox to mailbox "Drafts" of targetAccount
                set draftMessages to every message of draftsMailbox
                set foundDraft to missing value

                -- Find the draft
                repeat with aDraft in draftMessages
                    try
                        set draftSubject to subject of aDraft

                        if draftSubject contains "{safe_draft_subject}" then
                            set foundDraft to aDraft
                            exit repeat
                        end if
                    end try
                end repeat

                if foundDraft is not missing value then
                    set draftSubject to subject of foundDraft

                    -- Send the draft
                    send foundDraft

                    set outputText to outputText & "✓ Draft sent successfully!" & return
                    set outputText to outputText & "Subject: " & draftSubject & return

                else
                    set outputText to outputText & "⚠ No draft found matching: {safe_draft_subject}" & return
                end if

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    elif action == "open":
        if not draft_subject:
            return "Error: 'draft_subject' is required for opening drafts"

        safe_draft_subject = escape_applescript(draft_subject)

        script = f'''
        tell application "Mail"
            set outputText to "OPENING DRAFT FOR REVIEW" & return & return

            try
                set targetAccount to account "{safe_account}"
                set draftsMailbox to mailbox "Drafts" of targetAccount
                set draftMessages to every message of draftsMailbox
                set foundDraft to missing value

                -- Find the draft
                repeat with aDraft in draftMessages
                    try
                        set draftSubject to subject of aDraft

                        if draftSubject contains "{safe_draft_subject}" then
                            set foundDraft to aDraft
                            exit repeat
                        end if
                    end try
                end repeat

                if foundDraft is not missing value then
                    set draftSubject to subject of foundDraft

                    -- Open the draft in a visible compose window
                    set draftWindow to open foundDraft
                    activate

                    set outputText to outputText & "✓ Draft opened in Mail for review!" & return
                    set outputText to outputText & "Subject: " & draftSubject & return
                    set outputText to outputText & return & "Edit and send when ready." & return

                else
                    set outputText to outputText & "⚠ No draft found matching: {safe_draft_subject}" & return
                end if

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    elif action == "delete":
        if draft_id is None and not draft_subject:
            return "Error: 'draft_subject' or 'draft_id' is required for deleting drafts"
        if draft_id is None:
            matches = _drafts_matching(safe_account, draft_subject)
            if isinstance(matches, str):
                return matches
            if not matches:
                return f"⚠ No draft found matching: {draft_subject}"
            if len(matches) > 1:
                rows = "\n".join(
                    f"  ID {mid}: {subj} (created {created})" for mid, subj, created in matches
                )
                return (
                    f"Error: {len(matches)} drafts match '{draft_subject}'. Nothing was "
                    f"deleted. Pass draft_id for the one to delete:\n{rows}"
                )
            draft_id = matches[0][0]
        try:
            target_id = int(draft_id)
        except (TypeError, ValueError):
            return f"Error: draft_id must be a number from action='list', got {draft_id!r}"
        if target_id < 0:
            return f"Error: draft_id must be a number from action='list', got {draft_id!r}"

        script = f'''
        tell application "Mail"
            try
                set targetAccount to account "{safe_account}"
                set draftsMailbox to mailbox "Drafts" of targetAccount
                set foundDrafts to (every message of draftsMailbox whose id is {target_id})
                if (count of foundDrafts) is 0 then
                    return "⚠ No draft with ID {target_id} in Drafts of {safe_account}"
                end if
                set foundDraft to item 1 of foundDrafts
                set draftSubject to subject of foundDraft
                delete foundDraft
                return "DELETING DRAFT" & return & return & "✓ Draft deleted successfully!" & return & "Subject: " & draftSubject & return & "ID: {target_id}"
            on error errMsg
                return "Error: " & errMsg
            end try
        end tell
        '''

    else:
        return (
            f"Error: Invalid action '{action}'. Use: list, create, send, open, delete"
        )

    result = run_applescript(script)
    return result
