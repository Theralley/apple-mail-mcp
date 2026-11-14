"""Management tools: moving, status updates, trash, and attachments."""

import os
import re
from typing import List, Optional, Union

from apple_mail_mcp import server
from apple_mail_mcp.server import mcp
from apple_mail_mcp.paths import validate_write_path
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    AS_RECORD_SEP,
    FIELD_SEP,
    RECORD_SEP,
    resolve_flag_color,
    contains_any_condition,
    equals_any_numeric_condition,
    inject_preferences,
    escape_applescript,
    normalize_message_ids,
    normalize_search_terms,
    run_applescript,
    build_mailbox_ref,
    build_filter_condition,
)

# Names Mail gives an account's trash: IMAP and iCloud, Exchange, some IMAP
# servers, and Gmail in British English.
TRASH_MAILBOX_NAMES = ("Trash", "Deleted Items", "Deleted Messages", "Bin")

# Mail's AppleScript interface has no command that erases a message. `delete`
# means "move to the trash": on a message that is already there it does
# nothing, and on an Exchange account (macOS 27, 2026-11-14) a batch of such
# deletes made Mail abort in EmailDaemon (deleteMessages -> transferMessages).
# So no tool here sends `delete` or `move` to a message that is already in the
# trash; the trash actions report what is there and say how to erase it.
TRASH_ERASE_REFUSAL = (
    "Nothing was deleted. Mail's AppleScript interface cannot erase messages "
    "that are already in the trash: `delete` only moves a message to the trash, "
    "does nothing to one that is already there, and a batch of such deletes has "
    "crashed Mail on an Exchange account. Erase them in Mail itself (Mailbox > "
    "Erase Deleted Items for the whole trash, or select the messages in the "
    "trash and delete them there), or leave them for the server's retention "
    "policy."
)


def _normalize_mailbox_name(name: Optional[str]) -> str:
    return (name or "").strip().strip("/").casefold()


def _one_line(text: str) -> str:
    """A header value on one line: tabs and line breaks become single spaces."""
    return re.sub(r"[\t\r\n]+", " ", text).strip()


def _is_trash_name(name: Optional[str]) -> bool:
    return _normalize_mailbox_name(name) in {n.casefold() for n in TRASH_MAILBOX_NAMES}


def _trash_lookup_script(var_name: str = "trashMailbox") -> str:
    """AppleScript that sets ``var_name`` to targetAccount's trash mailbox."""
    names = ", ".join(f'"{n}"' for n in TRASH_MAILBOX_NAMES)
    tried = ", ".join(TRASH_MAILBOX_NAMES)
    return f'''set {var_name} to missing value
                    repeat with trashName in {{{names}}}
                        try
                            set {var_name} to mailbox (trashName as text) of targetAccount
                            exit repeat
                        end try
                    end repeat
                    if {var_name} is missing value then error "no trash mailbox in this account (tried {tried})"'''


@mcp.tool()
@inject_preferences
def move_email(
    account: str,
    to_mailbox: str,
    subject_keyword: Optional[str] = None,
    from_mailbox: str = "INBOX",
    max_moves: int = 50,
    subject_keywords: Optional[List[str]] = None,
    sender: Optional[str] = None,
    older_than_days: Optional[int] = None,
    dry_run: bool = False,
    only_read: bool = False,
    message_ids: Optional[List[str]] = None,
) -> str:
    """
    Move email(s) matching filters from one mailbox to another.

    Supports subject, sender, and date filters. Use dry_run=True to preview
    matches without moving. Set only_read=True to skip unread emails (useful
    for archiving). For archiving to "Archive", just set to_mailbox="Archive".
    Pass message_ids to move specific messages by exact id (ignores other
    filters; same fast path as update_email_status).

    Args:
        account: Account name (e.g., "Gmail", "Work")
        to_mailbox: Destination mailbox name. For nested mailboxes, use "/" separator (e.g., "Projects/Amplify Impact")
        subject_keyword: Optional keyword to search for in email subjects
        from_mailbox: Source mailbox name (default: "INBOX")
        max_moves: Maximum number of emails to move (default: 50, safety limit)
        subject_keywords: Optional list of keywords to match in subjects; matches any keyword
        sender: Optional sender to filter emails by
        older_than_days: Optional age filter - only move emails older than N days
        dry_run: If True, preview what would be moved without acting (default: False)
        only_read: If True, only move emails that have been read (default: False)
        message_ids: Optional list of exact Mail message ids for precise targeting. When set, all other filters are ignored.

    Returns:
        Confirmation message with details of moved emails
    """
    blocked = server.read_only_block("move_email")
    if blocked:
        return blocked

    if _normalize_mailbox_name(from_mailbox) == _normalize_mailbox_name(to_mailbox):
        return (
            f"Error: from_mailbox and to_mailbox are the same mailbox ('{to_mailbox}'). "
            "Nothing was moved: the messages are already there."
        )

    safe_account = escape_applescript(account)
    safe_from = escape_applescript(from_mailbox)
    safe_to = escape_applescript(to_mailbox)

    # Destination mailbox reference — built once and reused by both the
    # filter-based path and the id-based fast path below.
    mailbox_parts = to_mailbox.split("/")
    if len(mailbox_parts) > 1:
        dest_ref = f'mailbox "{escape_applescript(mailbox_parts[-1])}" of '
        for i in range(len(mailbox_parts) - 2, -1, -1):
            dest_ref += f'mailbox "{escape_applescript(mailbox_parts[i])}" of '
        dest_ref += "targetAccount"
    else:
        dest_ref = f'mailbox "{safe_to}" of targetAccount'

    # --- ID-based fast path: ignore subject/sender/date filters entirely.
    # Mirrors the message_ids branch in update_email_status so callers
    # who already know the message id (e.g. an inbox app's "archive"
    # button) don't have to round-trip through subject filtering.
    if message_ids is not None:
        normalized_ids = normalize_message_ids(message_ids)
        if not normalized_ids:
            return "Error: 'message_ids' must contain one or more numeric Mail ids"

        id_condition = equals_any_numeric_condition("id", normalized_ids)
        mode_label = "DRY RUN - PREVIEW MOVE BY IDS" if dry_run else "MOVING EMAILS BY IDS"
        move_action = "" if dry_run else "move aMessage to destMailbox"
        result_prefix = "Would move" if dry_run else "Moved"

        script = f'''
        tell application "Mail"
            with timeout of 300 seconds
                set outputText to "{mode_label}: {safe_from} -> {safe_to}" & return & return
                set moveCount to 0

                try
                    set targetAccount to account "{safe_account}"
                    {build_mailbox_ref(from_mailbox, var_name="sourceMailbox")}
                    set destMailbox to {dest_ref}

                    set targetMessages to every message of sourceMailbox whose {id_condition}
                    set requestedCount to {len(normalized_ids)}

                    repeat with aMessage in targetMessages
                        try
                            set messageSubject to subject of aMessage
                            set messageSender to sender of aMessage
                            set messageDate to date received of aMessage

                            {move_action}

                            set outputText to outputText & "{result_prefix}: " & messageSubject & return
                            set outputText to outputText & "   From: " & messageSender & return
                            set outputText to outputText & "   Date: " & (messageDate as string) & return & return

                            set moveCount to moveCount + 1
                        end try
                    end repeat

                    set outputText to outputText & "========================================" & return
                    set outputText to outputText & "REQUESTED: " & requestedCount & " id(s), MATCHED: " & moveCount & return
                    set outputText to outputText & "========================================" & return

                on error errMsg
                    return "Error: " & errMsg
                end try

                return outputText
            end timeout
        end tell
        '''
        return run_applescript(script, timeout=300)

    subject_terms = normalize_search_terms(subject_keyword, subject_keywords)
    # Only a positive age adds a date condition below, so only a positive age
    # counts as a filter (same rule as update_email_status and manage_trash).
    has_age_filter = older_than_days is not None and older_than_days > 0
    if not subject_terms and not sender and not has_age_filter:
        return (
            "Error: At least one filter is required (subject_keyword, sender, "
            "older_than_days, or message_ids). This prevents accidentally moving everything."
        )

    # Build filter condition for the loop body (uses local vars)
    condition_str = build_filter_condition(
        subject=subject_keyword if not subject_keywords else None,
        sender=sender,
    )
    # For multi-keyword subject matching, override the subject part
    if subject_terms:
        subj_cond = " or ".join(
            f'messageSubject contains "{escape_applescript(t)}"' for t in subject_terms
        )
        subj_cond = f"({subj_cond})"
        if sender:
            condition_str = f'{subj_cond} and messageSender contains "{escape_applescript(sender)}"'
        else:
            condition_str = subj_cond

    if only_read:
        read_cond = "messageRead is true"
        condition_str = (
            f"{condition_str} and {read_cond}" if condition_str != "true" else read_cond
        )

    # Date filter
    date_setup = ""
    date_cond = ""
    if older_than_days and older_than_days > 0:
        date_setup = f"set cutoffDate to (current date) - ({older_than_days} * days)"
        date_cond = " and messageDate < cutoffDate"

    # The same filters as a whose clause, so Mail selects the candidates in
    # one Apple Event instead of the loop reading every message's properties
    # (which timed out on large mailboxes). The loop re-checks condition_str.
    whose_parts = []
    if subject_terms:
        whose_parts.append(contains_any_condition("subject", subject_terms))
    if sender:
        whose_parts.append(f'sender contains "{escape_applescript(sender)}"')
    if only_read:
        whose_parts.append("read status is true")
    if date_setup:
        whose_parts.append("date received < cutoffDate")
    whose_clause = " and ".join(whose_parts)

    # dest_ref already computed at the top of the function (shared with
    # the id-based fast path).

    if dry_run:
        mode_label = "DRY RUN - PREVIEW MOVE"
        move_action = ""
        result_prefix = "Would move"
    else:
        mode_label = "MOVING EMAILS"
        move_action = "move aMessage to destMailbox"
        result_prefix = "Moved"

    dest_setup = "" if dry_run else f"""
            set destMailbox to {dest_ref}"""

    script = f'''
    tell application "Mail"
        with timeout of 300 seconds
            set outputText to "{mode_label}: {safe_from} -> {safe_to}" & return & return
            set moveCount to 0

            try
                set targetAccount to account "{safe_account}"
                {build_mailbox_ref(from_mailbox, var_name="sourceMailbox")}
                {dest_setup}
                {date_setup}

                set mailboxMessages to (every message of sourceMailbox whose {whose_clause})

                repeat with aMessage in mailboxMessages
                    if moveCount >= {max_moves} then exit repeat

                    try
                        set messageSubject to subject of aMessage
                        set messageSender to sender of aMessage
                        set messageDate to date received of aMessage
                        set messageRead to read status of aMessage

                        if {condition_str}{date_cond} then
                            {move_action}

                            set outputText to outputText & "{result_prefix}: " & messageSubject & return
                            set outputText to outputText & "   From: " & messageSender & return
                            set outputText to outputText & "   Date: " & (messageDate as string) & return & return

                            set moveCount to moveCount + 1
                        end if
                    end try
                end repeat

                set outputText to outputText & "========================================" & return
                set outputText to outputText & "TOTAL: " & moveCount & " email(s) {result_prefix.lower()}" & return
                if moveCount >= {max_moves} then
                    set outputText to outputText & "(max_moves limit reached)" & return
                end if
                set outputText to outputText & "========================================" & return

            on error errMsg
                return "Error: " & errMsg & return & "Check that account and mailbox names are correct. For nested mailboxes, use '/' separator."
            end try

            return outputText
        end timeout
    end tell
    '''

    result = run_applescript(script, timeout=300)
    return result


_SENSITIVE_HOME_DIRS = (
    ".ssh", ".gnupg", ".config", ".aws", ".claude", ".cc-accounts", ".codex",
    ".gemini", ".qwen", ".docker", ".kube",
    os.path.join("Library", "LaunchAgents"),
    os.path.join("Library", "LaunchDaemons"),
    os.path.join("Library", "Keychains"),
    os.path.join("Library", "Mail"),
    os.path.join("Library", "Caches", "apple-mail-mcp"),
)


def _checked_save_path(save_path: str):
    """(resolved path, None) or (None, error): under home, outside key folders,
    and allowed by the shared write guard (paths.validate_write_path)."""
    _, guard_error = validate_write_path(save_path)
    if guard_error:
        return None, guard_error
    # Expand tilde (POSIX file in AppleScript does not expand ~), resolve links
    resolved_path = os.path.realpath(os.path.expanduser(save_path))
    home_dir = os.path.realpath(os.path.expanduser("~"))

    # macOS file systems are case-insensitive: ~/.SSH is ~/.ssh
    folded = resolved_path.casefold()
    if not folded.startswith(home_dir.casefold() + os.sep) and folded != home_dir.casefold():
        return None, f"Error: Save path must be under your home directory ({home_dir}). Got: {resolved_path}"

    for relative in _SENSITIVE_HOME_DIRS:
        sensitive_dir = os.path.join(home_dir, relative)
        if folded.startswith(sensitive_dir.casefold() + os.sep) or folded == sensitive_dir.casefold():
            return None, f"Error: Cannot save attachments to sensitive directory: {sensitive_dir}"
    return resolved_path, None


def _save_attachment_by_id(message_id, part, attachment_name, resolved_path: str, overwrite: bool) -> str:
    from apple_mail_mcp import mail_attachments as ma

    try:
        item = ma.find_part(message_id, part=part, name=attachment_name)
        target = resolved_path
        if os.path.isdir(target):
            safe_name = os.path.basename(item.filename).lstrip(".") or f"part-{item.part}"
            target = os.path.join(target, safe_name)
        if os.path.basename(target).startswith("."):
            return "Error: will not write a hidden (dot) file"
        if os.path.islink(target):
            return f"Error: {target} is a symbolic link; will not write through it"
        # The final file, not only the folder given, must be an allowed place.
        _, path_error = _checked_save_path(target)
        if path_error:
            return path_error
        if os.path.lexists(target) and not overwrite:
            return f"Error: {target} already exists. Pass overwrite=True to replace it."
        if not os.path.isdir(os.path.dirname(target)):
            return f"Error: folder {os.path.dirname(target)} does not exist"
        data = ma.part_bytes(message_id, item)
    except ma.AttachmentError as exc:
        return f"Error: {exc}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError:
        return f"Error: {target} already exists. Pass overwrite=True to replace it."
    except OSError as exc:
        return f"Error: cannot write {target}: {exc.strerror or exc}"
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return (
        "✓ Attachment saved successfully!\n\n"
        f"Message ID: {message_id}\n"
        f"Attachment: {item.filename} (part {item.part}, {item.content_type}, {ma.format_size(len(data))})\n"
        f"Saved to: {target}"
    )


@mcp.tool()
@inject_preferences
def save_email_attachment(
    account: Optional[str] = None,
    subject_keyword: Optional[str] = None,
    attachment_name: Optional[str] = None,
    save_path: Optional[str] = None,
    mailbox: Optional[str] = None,
    message_id: Optional[Union[int, str]] = None,
    part: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """
    Save an attachment or inline picture from an email to disk.

    Preferred: message_id plus part (both from list_email_attachments), or
    message_id plus attachment_name. Works in any mailbox and copies the file
    Mail already stored on disk; only a part Mail has not downloaded yet is
    fetched through Mail. Without message_id: the older Inbox-by-subject route.

    Args:
        account: Account name; needed without message_id
        subject_keyword: Keyword in the subject; needed without message_id
        attachment_name: Name (or unique part of the name) of the attachment
        save_path: File path to write, or an existing folder to write into
            (under your home folder; protected locations such as dotfiles,
            LaunchAgents and ~/Library/Application Support are rejected, and
            --write-dir / APPLE_MAIL_MCP_WRITE_DIRS can narrow it further)
        mailbox: Mailbox for the subject route (default: the inbox; nested
            names use "/"). A message ID is found in any mailbox.
        message_id: Mail's message ID (preferred)
        part: Part number from list_email_attachments, e.g. "2" or "1.2"
        overwrite: Replace an existing file (default False; message_id route only)

    Returns:
        Confirmation message with save location
    """
    if not save_path:
        return "Error: 'save_path' is required"
    if message_id is None and (not account or not subject_keyword or not attachment_name):
        return (
            "Error: pass message_id (with part or attachment_name), or account, "
            "subject_keyword and attachment_name"
        )

    resolved_path, error = _checked_save_path(save_path)
    if error:
        return error

    if message_id is not None:
        text = str(message_id).strip()
        if not text.isascii() or not text.isdigit():
            return "Error: message_id must contain digits only"
        return _save_attachment_by_id(int(text), part, attachment_name, resolved_path, overwrite)

    expanded_path = resolved_path
    mailbox = mailbox or "INBOX"

    # Escape for AppleScript
    escaped_account = escape_applescript(account)
    escaped_keyword = escape_applescript(subject_keyword)
    escaped_attachment = escape_applescript(attachment_name)
    escaped_path = escape_applescript(expanded_path)
    escaped_mailbox = escape_applescript(mailbox)

    script = f'''
    tell application "Mail"
        set outputText to ""

        try
            set targetAccount to account "{escaped_account}"
            {build_mailbox_ref(mailbox, account_var="targetAccount", var_name="targetMailbox")}
            -- Let Mail filter by subject instead of reading every subject
            set mailboxMessages to (every message of targetMailbox whose subject contains "{escaped_keyword}")
            set foundAttachment to false

            repeat with aMessage in mailboxMessages
                try
                    set messageSubject to subject of aMessage

                    -- Check if subject contains keyword
                    if messageSubject contains "{escaped_keyword}" then
                        set msgAttachments to mail attachments of aMessage

                        repeat with anAttachment in msgAttachments
                            set attachmentFileName to name of anAttachment

                            if attachmentFileName contains "{escaped_attachment}" then
                                -- Save the attachment
                                save anAttachment in POSIX file "{escaped_path}"

                                set outputText to "✓ Attachment saved successfully!" & return & return
                                set outputText to outputText & "Email: " & messageSubject & return
                                set outputText to outputText & "Attachment: " & attachmentFileName & return
                                set outputText to outputText & "Saved to: {escaped_path}" & return

                                set foundAttachment to true
                                exit repeat
                            end if
                        end repeat

                        if foundAttachment then exit repeat
                    end if
                end try
            end repeat

            if not foundAttachment then
                set outputText to "⚠ Attachment not found" & return
                set outputText to outputText & "Email keyword: {escaped_keyword}" & return
                set outputText to outputText & "Attachment name: {escaped_attachment}" & return
                set outputText to outputText & "Searched mailbox: {escaped_mailbox}" & return
            end if

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    '''

    result = run_applescript(script)
    return result


@mcp.tool()
@inject_preferences
def update_email_status(
    account: str,
    action: str,
    subject_keyword: Optional[str] = None,
    subject_keywords: Optional[List[str]] = None,
    sender: Optional[str] = None,
    mailbox: str = "INBOX",
    max_updates: int = 10,
    apply_to_all: bool = False,
    message_ids: Optional[List[str]] = None,
    older_than_days: Optional[int] = None,
    flag_color: Optional[str] = None,
) -> str:
    """
    Update email status - mark as read/unread or flag/unflag emails.

    When message_ids is provided, uses exact ID matching (ignores other filters).
    Otherwise filters by subject, sender, and/or age.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        action: Action to perform: "mark_read", "mark_unread", "flag", "unflag"
        subject_keyword: Optional keyword to filter emails by subject
        subject_keywords: Optional list of subject keywords; matches any keyword
        sender: Optional sender to filter emails by
        mailbox: Mailbox to search in (default: "INBOX")
        max_updates: Maximum number of emails to update (safety limit, default: 10)
        apply_to_all: Must be True to allow updates without any filter
        message_ids: Optional list of exact Mail message ids for precise targeting
        older_than_days: Optional age filter - only update emails older than N days
        flag_color: Optional flag color for the "flag" action: "red", "orange",
            "yellow", "green", "blue", "purple", or "gray". Omit to flag
            without setting a color — Mail shows its default (red), or the
            message's previous color if one remains from an earlier flag.
            Re-colors messages that are already flagged.

    Returns:
        Confirmation message with details of updated emails
    """
    blocked = server.read_only_block("update_email_status")
    if blocked:
        return blocked

    safe_account = escape_applescript(account)

    flag_index: Optional[int] = None
    if flag_color is not None:
        if action != "flag":
            return "Error: 'flag_color' is only valid with action='flag'"
        flag_color = flag_color.strip().lower()
        try:
            flag_index = resolve_flag_color(flag_color)
        except ValueError as exc:
            return f"Error: {exc}"

    # Build action scripts
    if action == "mark_read":
        bulk_action_script = "set read status of targetMessages to true"
        single_action_script = "set read status of aMessage to true"
        action_label = "Marked as read"
    elif action == "mark_unread":
        bulk_action_script = "set read status of targetMessages to false"
        single_action_script = "set read status of aMessage to false"
        action_label = "Marked as unread"
    elif action == "flag":
        if flag_index is not None:
            # flag index and flagged status are independent properties; set
            # both so coloring an unflagged message activates the flag.
            bulk_action_script = (
                f"set flag index of targetMessages to {flag_index}\n"
                "                            set flagged status of targetMessages to true"
            )
            single_action_script = (
                f"set flag index of aMessage to {flag_index}\n"
                "                            set flagged status of aMessage to true"
            )
            action_label = f"Flagged ({flag_color})"
        else:
            bulk_action_script = "set flagged status of targetMessages to true"
            single_action_script = "set flagged status of aMessage to true"
            action_label = "Flagged"
    elif action == "unflag":
        bulk_action_script = "set flagged status of targetMessages to false"
        single_action_script = "set flagged status of aMessage to false"
        action_label = "Unflagged"
    else:
        return f"Error: Invalid action '{action}'. Use: mark_read, mark_unread, flag, unflag"

    # --- ID-based path (fast, ignores other filters) ---
    if message_ids is not None:
        normalized_ids = normalize_message_ids(message_ids)
        if not normalized_ids:
            return "Error: 'message_ids' must contain one or more numeric Mail ids"

        id_condition = equals_any_numeric_condition("id", normalized_ids)

        script = f'''
        tell application "Mail"
            with timeout of 300 seconds
                set outputText to "UPDATING EMAIL STATUS BY IDS: {action_label}" & return & return
                set updateCount to 0

                try
                    set targetAccount to account "{safe_account}"
                    {build_mailbox_ref(mailbox, var_name="targetMailbox")}

                    set targetMessages to every message of targetMailbox whose {id_condition}
                    set requestedCount to {len(normalized_ids)}

                    if (count of targetMessages) > 0 then
                        try
                            {bulk_action_script}
                        on error
                            repeat with aMessage in targetMessages
                                {single_action_script}
                            end repeat
                        end try

                        repeat with aMessage in targetMessages
                            try
                                set messageSubject to subject of aMessage
                                set messageSender to sender of aMessage
                                set messageDate to date received of aMessage

                                set outputText to outputText & "- {action_label}: " & messageSubject & return
                                set outputText to outputText & "   From: " & messageSender & return
                                set outputText to outputText & "   Date: " & (messageDate as string) & return & return
                                set updateCount to updateCount + 1
                            end try
                        end repeat
                    end if

                    set outputText to outputText & "========================================" & return
                    set outputText to outputText & "REQUESTED IDS: " & requestedCount & return
                    set outputText to outputText & "TOTAL UPDATED: " & updateCount & " email(s)" & return
                    set outputText to outputText & "========================================" & return

                on error errMsg
                    return "Error: " & errMsg
                end try

                return outputText
            end timeout
        end tell
        '''

        return run_applescript(script, timeout=300)

    # --- Filter-based path ---
    subject_terms = normalize_search_terms(subject_keyword, subject_keywords)

    # Safety check: require at least one filter or explicit apply_to_all
    has_filter = bool(subject_terms) or bool(sender) or (
        older_than_days is not None and older_than_days > 0
    )
    if not has_filter and not apply_to_all:
        return (
            "Error: No filter provided. Provide subject_keyword, sender, or older_than_days "
            "to filter emails, or set apply_to_all=True to update all messages in the mailbox."
        )

    # Pre-filter conditions (skip no-op updates)
    if action == "mark_read":
        conditions = ["read status is false"]
    elif action == "mark_unread":
        conditions = ["read status is true"]
    elif action == "flag":
        if flag_index is not None:
            # Match unflagged messages AND already-flagged ones needing a
            # re-color; also match a residual matching index whose flag is
            # inactive (flag index survives unflagging).
            conditions = [
                f"(flag index is not {flag_index} or flagged status is false)"
            ]
        else:
            conditions = ["flagged status is false"]
    else:  # unflag
        conditions = ["flagged status is true"]

    if subject_terms:
        conditions.append(contains_any_condition("subject", subject_terms))
    if sender:
        conditions.append(f'sender contains "{escape_applescript(sender)}"')

    search_condition = " and ".join(conditions)

    # Date filter
    date_setup = ""
    date_check_start = ""
    date_check_end = ""
    if older_than_days and older_than_days > 0:
        date_setup = f"set cutoffDate to (current date) - ({older_than_days} * days)"
        date_check_start = "if (date received of aMessage) < cutoffDate then"
        date_check_end = "end if"

    script = f'''
    tell application "Mail"
        with timeout of 300 seconds
            set outputText to "UPDATING EMAIL STATUS: {action_label}" & return & return
            set updateCount to 0

            try
                set targetAccount to account "{safe_account}"
                {build_mailbox_ref(mailbox, var_name="targetMailbox")}
                {date_setup}

                set matchingMessages to every message of targetMailbox whose {search_condition}
                set matchingCount to count of matchingMessages

                if matchingCount is 0 then
                    set targetMessages to {{}}
                else if matchingCount > {max_updates} then
                    set targetMessages to items 1 thru {max_updates} of matchingMessages
                else
                    set targetMessages to matchingMessages
                end if

                repeat with aMessage in targetMessages
                    try
                        {date_check_start}
                            {single_action_script}
                            set messageSubject to subject of aMessage
                            set messageSender to sender of aMessage
                            set messageDate to date received of aMessage

                            set outputText to outputText & "- {action_label}: " & messageSubject & return
                            set outputText to outputText & "   From: " & messageSender & return
                            set outputText to outputText & "   Date: " & (messageDate as string) & return & return
                            set updateCount to updateCount + 1
                        {date_check_end}
                    end try
                end repeat

                set outputText to outputText & "========================================" & return
                set outputText to outputText & "TOTAL UPDATED: " & updateCount & " email(s)" & return
                set outputText to outputText & "========================================" & return

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end timeout
    end tell
    '''

    result = run_applescript(script, timeout=300)
    return result


@mcp.tool()
@inject_preferences
def manage_trash(
    account: str,
    action: str,
    subject_keyword: Optional[str] = None,
    subject_keywords: Optional[List[str]] = None,
    sender: Optional[str] = None,
    mailbox: str = "INBOX",
    max_deletes: int = 5,
    confirm_empty: bool = False,
    apply_to_all: bool = False,
    older_than_days: Optional[int] = None,
    dry_run: bool = True,
) -> str:
    """
    Manage trash operations - delete emails or empty trash.

    When dry_run=True (default) and action is "move_to_trash", previews what
    would be deleted without acting. Set dry_run=False to actually move to trash.

    Mail's AppleScript interface has no command that erases a message: `delete`
    means "move to the trash", it does nothing to a message that is already
    there, and a batch of such deletes has crashed Mail on an Exchange account.
    "delete_permanent" and "empty_trash" therefore never delete anything: they
    report what is in the account's trash (Trash, Deleted Items, Deleted
    Messages or Bin) and say how to erase it in Mail. "move_to_trash" refuses a
    source mailbox that is itself the trash.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        action: Action to perform: "move_to_trash", "delete_permanent" (lists the matching trash messages), "empty_trash" (counts the trash)
        subject_keyword: Optional keyword to filter emails (not used for empty_trash)
        subject_keywords: Optional list of subject keywords; matches any keyword
        sender: Optional sender to filter emails (not used for empty_trash)
        mailbox: Source mailbox (default: "INBOX", not used for empty_trash or delete_permanent)
        max_deletes: Maximum number of emails to move, or to list for delete_permanent (default: 5)
        confirm_empty: Ignored; empty_trash no longer deletes anything
        apply_to_all: Must be True to allow operations without subject_keyword or sender filter
        older_than_days: Optional age filter - only affect emails older than N days
        dry_run: If True (default), preview what would be affected without acting

    Returns:
        Confirmation message with details of deleted emails
    """
    blocked = server.read_only_block("manage_trash")
    if blocked:
        return blocked

    # Anything unrecognised used to fall through to the move_to_trash branch.
    if action not in ("move_to_trash", "delete_permanent", "empty_trash"):
        return (
            f"Error: Invalid action '{action}'. "
            "Use: move_to_trash, delete_permanent, empty_trash"
        )

    # Escape all user inputs for AppleScript
    safe_account = escape_applescript(account)
    safe_mailbox = escape_applescript(mailbox)
    subject_terms = normalize_search_terms(subject_keyword, subject_keywords)

    if action == "empty_trash":
        # Read only: count the trash, never send it `delete` (see TRASH_ERASE_REFUSAL).
        script = f'''
        tell application "Mail"
            try
                set targetAccount to account "{safe_account}"
                {_trash_lookup_script()}
                return "TRASH" & {AS_FIELD_SEP} & (name of trashMailbox) & {AS_FIELD_SEP} & ((count of messages of trashMailbox) as string)
            on error errMsg
                return "Error: " & errMsg
            end try
        end tell
        '''
        raw = run_applescript(script) or ""
        parts = raw.split(FIELD_SEP)
        if len(parts) != 3 or parts[0] != "TRASH":
            return raw or "Error: could not read the trash mailbox"
        return (
            "EMPTY TRASH: not done\n\n"
            f"Account: {account}\n"
            f"Trash mailbox: {parts[1]}, {parts[2]} message(s)\n\n"
            f"⚠ {TRASH_ERASE_REFUSAL}"
        )
    elif action == "delete_permanent":
        # Safety check: require at least one filter or explicit apply_to_all
        if not subject_terms and not sender and not apply_to_all:
            return (
                "Error: No filter provided. Provide subject_keyword or sender to filter emails, "
                "or set apply_to_all=True to delete all matching messages."
            )

        # Build search condition with escaped inputs
        conditions = []
        if subject_terms:
            conditions.append(contains_any_condition("subject", subject_terms))
        if sender:
            conditions.append(f'sender contains "{escape_applescript(sender)}"')

        if conditions:
            matching_messages_script = f"set matchingMessages to every message of trashMailbox whose {' and '.join(conditions)}"
        else:
            matching_messages_script = (
                "set matchingMessages to every message of trashMailbox"
            )

        # Read only: list the matches, never send them `delete`
        # (see TRASH_ERASE_REFUSAL).
        script = f'''
        tell application "Mail"
            with timeout of 300 seconds
                try
                    set targetAccount to account "{safe_account}"
                    {_trash_lookup_script()}
                    {matching_messages_script}
                    set outputText to "TRASH" & {AS_FIELD_SEP} & (name of trashMailbox) & {AS_FIELD_SEP} & ((count of matchingMessages) as string)
                    set shownCount to 0
                    repeat with aMessage in matchingMessages
                        if shownCount >= {max_deletes} then exit repeat
                        try
                            set outputText to outputText & {AS_RECORD_SEP} & ((id of aMessage) as string) & {AS_FIELD_SEP} & (subject of aMessage) & {AS_FIELD_SEP} & (sender of aMessage)
                            set shownCount to shownCount + 1
                        end try
                    end repeat
                    return outputText
                on error errMsg
                    return "Error: " & errMsg
                end try
            end timeout
        end tell
        '''
        # Subjects can hold tabs and line breaks, so records are framed with
        # the ASCII separators, and run_applescript's own trim keeps an empty
        # last field (a second .strip() here would eat it).
        raw = run_applescript(script, timeout=300) or ""
        records = raw.split(RECORD_SEP)
        head = records[0].split(FIELD_SEP)
        if len(head) != 3 or head[0] != "TRASH":
            return raw or "Error: could not read the trash mailbox"
        rows = []
        for record in records[1:]:
            parts = record.split(FIELD_SEP)
            if len(parts) == 3 and parts[0].strip().isdigit():
                from_text = _one_line(parts[2])
                from_text = f" (from {from_text})" if from_text else ""
                rows.append(
                    f"  ID {parts[0].strip()}: {_one_line(parts[1])}{from_text}"
                )
        listing = "\n".join(rows) if rows else "  (none)"
        more = ""
        if head[2].strip().isdigit() and int(head[2]) > len(rows):
            more = f"\n  ... {int(head[2]) - len(rows)} more (max_deletes={max_deletes})"
        return (
            "DELETE PERMANENTLY: not done\n\n"
            f"Account: {account}\n"
            f"{head[2]} matching message(s) in {head[1]}:\n{listing}{more}\n\n"
            f"⚠ {TRASH_ERASE_REFUSAL}"
        )
    else:  # move_to_trash
        if _is_trash_name(mailbox):
            return (
                f"Error: '{mailbox}' is the trash. Nothing was moved: the messages "
                f"are already in the trash. {TRASH_ERASE_REFUSAL}"
            )
        # Safety check: require at least one filter or explicit apply_to_all
        has_filter = bool(subject_terms) or bool(sender) or (
            older_than_days is not None and older_than_days > 0
        )
        if not has_filter and not apply_to_all:
            return (
                "Error: No filter provided. Provide subject_keyword, sender, or older_than_days "
                "to filter emails, or set apply_to_all=True to move all messages to trash."
            )

        # Build search condition with escaped inputs
        conditions = []
        if subject_terms:
            conditions.append(contains_any_condition("subject", subject_terms))
        if sender:
            conditions.append(f'sender contains "{escape_applescript(sender)}"')

        if conditions:
            matching_messages_script = f"set matchingMessages to every message of sourceMailbox whose {' and '.join(conditions)}"
        else:
            matching_messages_script = (
                "set matchingMessages to every message of sourceMailbox"
            )

        # Date filter
        date_setup = ""
        date_check_start = ""
        date_check_end = ""
        if older_than_days and older_than_days > 0:
            date_setup = f"set cutoffDate to (current date) - ({older_than_days} * days)"
            date_check_start = "if (date received of aMessage) < cutoffDate then"
            date_check_end = "end if"

        if dry_run:
            mode_label = "DRY RUN - PREVIEW TRASH"
            move_script = ""
            result_verb = "Would trash"
        else:
            mode_label = "MOVING EMAILS TO TRASH"
            move_script = "move aMessage to trashMailbox"
            result_verb = "Moved to trash"

        trash_setup = "" if dry_run else _trash_lookup_script()

        script = f'''
        tell application "Mail"
            with timeout of 300 seconds
                set outputText to "{mode_label}" & return & return
                set deleteCount to 0

                try
                    set targetAccount to account "{safe_account}"
                    {build_mailbox_ref(mailbox, var_name="sourceMailbox")}
                    {trash_setup}
                    {date_setup}

                    {matching_messages_script}
                    set matchingCount to count of matchingMessages

                    if matchingCount is 0 then
                        set targetMessages to {{}}
                    else if matchingCount > {max_deletes} then
                        set targetMessages to items 1 thru {max_deletes} of matchingMessages
                    else
                        set targetMessages to matchingMessages
                    end if

                    repeat with aMessage in targetMessages
                        try
                            set messageSubject to subject of aMessage
                            set messageSender to sender of aMessage
                            set messageDate to date received of aMessage

                            {date_check_start}
                                {move_script}
                                set deleteCount to deleteCount + 1

                                set outputText to outputText & "{result_verb}: " & messageSubject & return
                                set outputText to outputText & "   From: " & messageSender & return
                                set outputText to outputText & "   Date: " & (messageDate as string) & return & return
                            {date_check_end}
                        end try
                    end repeat

                    set outputText to outputText & "========================================" & return
                    set outputText to outputText & "TOTAL: " & deleteCount & " email(s) {result_verb.lower()}" & return
                    set outputText to outputText & "========================================" & return

                on error errMsg
                    return "Error: " & errMsg
                end try

                return outputText
            end timeout
        end tell
        '''

    result = run_applescript(script, timeout=300)
    return result


# Characters that could break AppleScript strings or mailbox names
_INVALID_MAILBOX_CHARS = re.compile(r"[\\\"<>|?*:\x00-\x1f]")


@mcp.tool()
@inject_preferences
def create_mailbox(
    account: str,
    name: str,
    parent_mailbox: Optional[str] = None,
) -> str:
    """
    Create a new mailbox (folder) in the specified account.

    Supports nested paths via the parent_mailbox parameter (e.g.,
    parent_mailbox="Projects" + name="2024" creates Projects/2024).
    You can also pass a full slash-separated path as *name*
    (e.g., "Projects/2024/ClientName") and omit parent_mailbox.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        name: Name for the new mailbox. May contain "/" to create a
              nested path in one call (each segment is created if needed).
        parent_mailbox: Optional existing parent folder for nesting.

    Returns:
        Confirmation with the new mailbox path.
    """
    blocked = server.read_only_block("create_mailbox")
    if blocked:
        return blocked

    # Validate name
    if not name or not name.strip():
        return "Error: Mailbox name cannot be empty."

    # Split name into segments (support "A/B/C" shorthand)
    segments = [s.strip() for s in name.split("/") if s.strip()]
    if not segments:
        return "Error: Mailbox name cannot be empty."

    for seg in segments:
        if _INVALID_MAILBOX_CHARS.search(seg):
            return (
                f"Error: Invalid characters in mailbox name segment '{seg}'. "
                'Avoid \\ " < > | ? * : and control characters.'
            )

    safe_account = escape_applescript(account)

    # If parent_mailbox is given, prepend its segments
    if parent_mailbox:
        parent_segments = [s.strip() for s in parent_mailbox.split("/") if s.strip()]
        segments = parent_segments + segments

    # Build AppleScript to create each level one at a time
    create_blocks = ""
    for depth in range(len(segments)):
        seg = escape_applescript(segments[depth])
        if depth == 0:
            create_blocks += f'''
            try
                set parentRef to mailbox "{seg}" of targetAccount
            on error
                make new mailbox at targetAccount with properties {{name:"{seg}"}}
                set parentRef to mailbox "{seg}" of targetAccount
            end try
'''
        else:
            create_blocks += f'''
            try
                set parentRef to mailbox "{seg}" of parentRef
            on error
                make new mailbox at parentRef with properties {{name:"{seg}"}}
                set parentRef to mailbox "{seg}" of parentRef
            end try
'''

    full_path = "/".join(segments)
    safe_path = escape_applescript(full_path)

    script = f'''
    tell application "Mail"
        set outputText to "CREATING MAILBOX" & return & return

        try
            set targetAccount to account "{safe_account}"

            {create_blocks}

            set outputText to outputText & "OK Mailbox created successfully!" & return & return
            set outputText to outputText & "Account: {safe_account}" & return
            set outputText to outputText & "Path: {safe_path}" & return

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    '''

    return run_applescript(script)




@mcp.tool()
@inject_preferences
def synchronize_account(account: Optional[str] = None) -> str:
    """
    Force Mail.app to synchronize an account (or every account) with its
    IMAP / Exchange server right now. Equivalent to clicking the
    refresh button next to the account or selecting Mailbox → Synchronize.

    Use after `move_email`, `update_email_status`, or `manage_trash`
    when downstream clients (iPhone, web mail, etc.) need to see the
    change immediately. Mail.app's natural sync cadence is "automatic"
    which can be several minutes — this collapses that to one IMAP push.

    Implementation note:
    --------------------
    Uses the `synchronize with <account>` AppleScript verb (per Mail.sdef:
    "Command to trigger synchronizing of an IMAP account with the server")
    rather than `check for new mail`. The latter is receive-only — it
    pulls new messages but does NOT push pending IMAP commands like
    queued moves / archives / flag changes. With `check for new mail`,
    archives done via `move_email` could sit in Mail.app's local cache
    for several minutes before reaching the IMAP server, leaving iPhone
    Mail (which reads IMAP directly) showing already-archived messages
    still in INBOX. `synchronize with` is the bidirectional verb that
    drains pending IMAP commands AND fetches new mail.

    Mail.app's synchronize is potentially long-running. We wrap each
    invocation in `with timeout of N seconds` so the AppleScript returns
    promptly. When the timeout fires (error -1712) Mail.app keeps the
    sync running in the background — exactly the fire-and-forget
    semantics callers expect.

    Args:
        account: Account name (e.g., "Gmail", "Work"). Omit to sync every
                 configured account.

    Returns:
        Confirmation string with the account(s) synced or queued.
    """
    # 8 s is comfortably longer than a healthy IMAP sync but short enough
    # that a stuck account / network blip doesn't block the MCP call. On
    # timeout we report "queued" — the sync continues asynchronously.
    PER_ACCOUNT_TIMEOUT_S = 8

    if account is None or not account.strip():
        script = f'''
        tell application "Mail"
            set acctNames to {{}}
            set queuedNames to {{}}
            repeat with a in accounts
                set acctName to name of a
                set end of acctNames to acctName
                try
                    with timeout of {PER_ACCOUNT_TIMEOUT_S} seconds
                        synchronize with a
                    end timeout
                on error errMsg number errNum
                    if errNum is -1712 then
                        set end of queuedNames to acctName
                    end if
                end try
            end repeat
            set AppleScript's text item delimiters to ", "
            if (count of queuedNames) > 0 then
                return "Synchronized all accounts: " & (acctNames as string) & " (queued: " & (queuedNames as string) & ")"
            else
                return "Synchronized all accounts: " & (acctNames as string)
            end if
        end tell
        '''
        return run_applescript(script)

    acct_escaped = escape_applescript(account.strip())
    script = f'''
    tell application "Mail"
        try
            set targetAccount to first account whose name is "{acct_escaped}"
            try
                with timeout of {PER_ACCOUNT_TIMEOUT_S} seconds
                    synchronize with targetAccount
                end timeout
                return "Synchronized: {acct_escaped}"
            on error errMsg number errNum
                if errNum is -1712 then
                    return "Synchronizing: {acct_escaped} (queued — push in progress)"
                end if
                return "Error: " & errMsg
            end try
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)
