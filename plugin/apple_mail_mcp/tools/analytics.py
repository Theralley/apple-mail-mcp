"""Analytics tools: attachments, statistics, exports, and dashboard."""

import os
from typing import Optional, List, Dict, Any, NamedTuple, Tuple

from apple_mail_mcp.server import mcp
from apple_mail_mcp import envelope_index
from apple_mail_mcp.envelope_index import IndexUnavailable, fold
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    AS_RECORD_SEP,
    FIELD_SEP,
    RECORD_SEP,
    clean_script_output,
    inject_preferences,
    escape_applescript,
    run_applescript,
    inbox_mailbox_script,
)
from apple_mail_mcp.constants import SKIP_FOLDERS
from apple_mail_mcp.emlx import get_message_body, preview


@mcp.tool()
@inject_preferences
def list_email_attachments(
    account: str,
    subject_keyword: str,
    max_results: int = 1
) -> str:
    """
    List attachments for emails matching a subject keyword.

    Args:
        account: Account name (e.g., "Gmail", "Work", "Personal")
        subject_keyword: Keyword to search for in email subjects
        max_results: Maximum number of matching emails to check (default: 1)

    Returns:
        List of attachments with their names and sizes
    """

    # Escape for AppleScript
    escaped_keyword = escape_applescript(subject_keyword)
    escaped_account = escape_applescript(account)

    script = f'''
    tell application "Mail"
        set outputText to "ATTACHMENTS FOR: {escaped_keyword}" & return & return
        set resultCount to 0

        try
            set targetAccount to account "{escaped_account}"
            {inbox_mailbox_script("inboxMailbox", "targetAccount")}
            -- Let Mail filter by subject (one Apple Event) instead of reading
            -- every subject in the inbox one message at a time
            set inboxMessages to (every message of inboxMailbox whose subject contains "{escaped_keyword}")

            repeat with aMessage in inboxMessages
                if resultCount >= {max_results} then exit repeat

                try
                    set messageSubject to subject of aMessage

                    -- Check if subject contains keyword
                    if messageSubject contains "{escaped_keyword}" then
                        set messageSender to sender of aMessage
                        set messageDate to date received of aMessage

                        set outputText to outputText & "✉ " & messageSubject & return
                        set outputText to outputText & "   From: " & messageSender & return
                        set outputText to outputText & "   Date: " & (messageDate as string) & return & return

                        -- Get attachments
                        set msgAttachments to mail attachments of aMessage
                        set attachmentCount to count of msgAttachments

                        if attachmentCount > 0 then
                            set outputText to outputText & "   Attachments (" & attachmentCount & "):" & return

                            repeat with anAttachment in msgAttachments
                                set attachmentName to name of anAttachment
                                try
                                    set attachmentSize to size of anAttachment
                                    set sizeInKB to (attachmentSize / 1024) as integer
                                    set outputText to outputText & "   📎 " & attachmentName & " (" & sizeInKB & " KB)" & return
                                on error
                                    set outputText to outputText & "   📎 " & attachmentName & return
                                end try
                            end repeat
                        else
                            set outputText to outputText & "   No attachments" & return
                        end if

                        set outputText to outputText & return
                        set resultCount to resultCount + 1
                    end if
                end try
            end repeat

            set outputText to outputText & "========================================" & return
            set outputText to outputText & "FOUND: " & resultCount & " matching email(s)" & return
            set outputText to outputText & "========================================" & return

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
def get_statistics(
    account: str,
    scope: str = "account_overview",
    sender: Optional[str] = None,
    mailbox: Optional[str] = None,
    days_back: int = 30
) -> str:
    """
    Get comprehensive email statistics and analytics.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        scope: Analysis scope: "account_overview", "sender_stats", "mailbox_breakdown"
        sender: Specific sender for "sender_stats" scope
        mailbox: Specific mailbox for "mailbox_breakdown" scope
        days_back: Number of days to analyze (default: 30, 0 = all time)

    Returns:
        Formatted statistics report with metrics and insights
    """
    try:
        return _statistics_from_index(account, scope, sender, mailbox, days_back)
    except IndexUnavailable:
        pass

    # Escape user inputs for AppleScript
    escaped_account = escape_applescript(account)
    escaped_sender = escape_applescript(sender) if sender else None
    escaped_mailbox = escape_applescript(mailbox) if mailbox else None

    # Calculate date threshold if days_back > 0
    date_filter = ""
    if days_back > 0:
        date_filter = f'''
            set targetDate to (current date) - ({days_back} * days)
        '''

    # Build skip folders condition from constants
    skip_folder_checks = ' and '.join(
        f'mailboxName is not "{escape_applescript(f)}"' for f in SKIP_FOLDERS
    )

    if scope == "account_overview":
        if days_back > 0:
            overview_selection = "(every message of aMailbox whose date received > targetDate)"
        else:
            overview_selection = "every message of aMailbox"
        # One record per message; the report is built in Python
        # (_account_overview_report), which counts each message once.
        script = f'''
        tell application "Mail"
            {date_filter}

            try
                set targetAccount to account "{escaped_account}"
                set fs to {AS_FIELD_SEP}
                set outLines to {{}}

                repeat with aMailbox in (every mailbox of targetAccount)
                    try
                        set mailboxName to name of aMailbox

                        -- Skip system folders
                        if {skip_folder_checks} then
                            set mailboxMessages to {overview_selection}
                            set {{idList, readList, flaggedList, senderList}} to {{id, read status, flagged status, sender}} of {overview_selection}
                            set end of outLines to "MAILBOX" & fs & mailboxName
                            repeat with i from 1 to count of mailboxMessages
                                try
                                    set attachmentCount to count of mail attachments of item i of mailboxMessages
                                    set end of outLines to "MESSAGE" & fs & (item i of idList as string) & fs & (item i of readList as string) & fs & (item i of flaggedList as string) & fs & (attachmentCount as string) & fs & (item i of senderList as string)
                                end try
                            end repeat
                        end if
                    on error
                        -- Skip mailboxes that throw errors (smart mailboxes, etc.)
                    end try
                end repeat

                set AppleScript's text item delimiters to {AS_RECORD_SEP}
                set outputText to outLines as string
                set AppleScript's text item delimiters to ""
                return outputText
            on error errMsg
                return "Error: " & errMsg
            end try
        end tell
        '''

    elif scope == "sender_stats":
        if not sender:
            return "Error: 'sender' parameter required for sender_stats scope"

        # Build whose clause for fast app-level filtering
        whose_parts = [f'sender contains "{escaped_sender}"']
        if days_back > 0:
            whose_parts.append('date received > targetDate')
        whose_clause = ' and '.join(whose_parts)

        script = f'''
        tell application "Mail"
            {date_filter}

            try
                set targetAccount to account "{escaped_account}"
                set fs to {AS_FIELD_SEP}
                set outLines to {{}}

                repeat with aMailbox in (every mailbox of targetAccount)
                    try
                        set mailboxName to name of aMailbox

                        -- Skip system folders
                        if {skip_folder_checks} then
                            set matchedMessages to (every message of aMailbox whose {whose_clause})
                            set end of outLines to "MAILBOX" & fs & mailboxName
                            repeat with aMessage in matchedMessages
                                try
                                    set end of outLines to "MESSAGE" & fs & ((id of aMessage) as string) & fs & ((read status of aMessage) as string) & fs & "false" & fs & ((count of mail attachments of aMessage) as string) & fs & ""
                                end try
                            end repeat
                        end if
                    on error
                        -- Skip mailboxes that throw errors (smart mailboxes, etc.)
                    end try
                end repeat

                set AppleScript's text item delimiters to {AS_RECORD_SEP}
                set outputText to outLines as string
                set AppleScript's text item delimiters to ""
                return outputText
            on error errMsg
                return "Error: " & errMsg
            end try
        end tell
        '''

    elif scope == "mailbox_breakdown":
        mailbox_param = escaped_mailbox if mailbox else "INBOX"

        script = f'''
        tell application "Mail"
            set outputText to "MAILBOX STATISTICS" & return & return
            set outputText to outputText & "Mailbox: {mailbox_param}" & return
            set outputText to outputText & "Account: {escaped_account}" & return & return

            try
                set targetAccount to account "{escaped_account}"
                try
                    set targetMailbox to mailbox "{mailbox_param}" of targetAccount
                on error
                    if "{mailbox_param}" is "INBOX" then
                        set targetMailbox to mailbox "Inbox" of targetAccount
                    else
                        error "Mailbox not found"
                    end if
                end try

                set mailboxMessages to every message of targetMailbox
                set totalMessages to count of mailboxMessages
                set unreadMessages to unread count of targetMailbox

                set outputText to outputText & "Total messages: " & totalMessages & return
                set outputText to outputText & "Unread: " & unreadMessages & return
                set outputText to outputText & "Read: " & (totalMessages - unreadMessages) & return

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    else:
        return f"Error: Invalid scope '{scope}'. Use: account_overview, sender_stats, mailbox_breakdown"

    result = run_applescript(script)
    if scope == "mailbox_breakdown" or result.startswith("Error:"):
        return result
    mailboxes = _parse_statistics_rows(result)
    if scope == "account_overview":
        return _account_overview_report(account, mailboxes)
    return _sender_stats_report(account, sender, mailboxes)


@mcp.tool()
@inject_preferences
def export_emails(
    account: str,
    scope: str,
    subject_keyword: Optional[str] = None,
    mailbox: str = "INBOX",
    save_directory: str = "~/Desktop",
    format: str = "txt",
    max_emails: int = 1000
) -> str:
    """
    Export emails to files for backup or analysis.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        scope: Export scope: "single_email" (requires subject_keyword) or "entire_mailbox"
        subject_keyword: Keyword to find email (required for single_email)
        mailbox: Mailbox to export from (default: "INBOX")
        save_directory: Directory to save exports (default: "~/Desktop")
        format: Export format: "txt", "html" (default: "txt")
        max_emails: Maximum number of emails to export for entire_mailbox (default: 1000, safety cap)

    Returns:
        Confirmation message with export location
    """

    # Expand home directory
    save_dir = os.path.expanduser(save_directory)

    # Path validation: resolve to absolute path and enforce safety constraints
    resolved_path = os.path.realpath(save_dir)
    home_dir = os.path.expanduser('~')

    # Must be under the user's home directory
    if not resolved_path.startswith(home_dir + os.sep) and resolved_path != home_dir:
        return f"Error: Save path must be under your home directory ({home_dir}). Got: {resolved_path}"

    # Block sensitive directories
    sensitive_dirs = [
        os.path.join(home_dir, '.ssh'),
        os.path.join(home_dir, '.gnupg'),
        os.path.join(home_dir, '.config'),
        os.path.join(home_dir, '.aws'),
        os.path.join(home_dir, '.claude'),
        os.path.join(home_dir, 'Library', 'LaunchAgents'),
        os.path.join(home_dir, 'Library', 'LaunchDaemons'),
        os.path.join(home_dir, 'Library', 'Keychains'),
    ]
    for sensitive_dir in sensitive_dirs:
        if resolved_path.startswith(sensitive_dir + os.sep) or resolved_path == sensitive_dir:
            return f"Error: Cannot export emails to sensitive directory: {sensitive_dir}"

    save_dir = resolved_path

    # Escape all user inputs for AppleScript
    safe_account = escape_applescript(account)
    safe_mailbox = escape_applescript(mailbox)

    if format not in ("txt", "html"):
        return f"Error: Invalid format '{format}'. Use: txt, html"

    if scope == "single_email":
        if not subject_keyword:
            return "Error: 'subject_keyword' required for single_email scope"

        safe_subject_keyword = escape_applescript(subject_keyword)

        script = f'''
        tell application "Mail"
            set outputText to "EXPORTING EMAIL" & return & return

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

                -- Let Mail filter by subject instead of reading every subject
                set mailboxMessages to (every message of targetMailbox whose subject contains "{safe_subject_keyword}")
                set foundMessage to missing value

                -- Find the email
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
                    -- The body is read from disk and the file written in Python
                    set fs to {AS_FIELD_SEP}
                    return "FOUND" & fs & ((id of foundMessage) as string) & fs & (subject of foundMessage) & fs & (sender of foundMessage) & fs & ((date received of foundMessage) as string)
                else
                    set outputText to outputText & "⚠ No email found matching: {safe_subject_keyword}" & return
                end if

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    elif scope == "entire_mailbox":
        script = f'''
        tell application "Mail"
            set outputText to "EXPORTING MAILBOX" & return & return

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
                set messageCount to count of mailboxMessages

                -- Bodies are read from disk and files written in Python
                set fs to {AS_FIELD_SEP}
                set outputText to "COUNT" & fs & messageCount
                set exportCount to 0
                repeat with aMessage in mailboxMessages
                    if exportCount >= {max_emails} then exit repeat
                    try
                        set messageSubject to subject of aMessage
                        set messageSender to sender of aMessage
                        set messageDate to date received of aMessage
                        set outputText to outputText & {AS_RECORD_SEP} & "ENTRY" & fs & ((id of aMessage) as string) & fs & messageSubject & fs & messageSender & fs & (messageDate as string)
                        set exportCount to exportCount + 1
                    end try
                end repeat

            on error errMsg
                return "Error: " & errMsg
            end try

            return outputText
        end tell
        '''

    else:
        return f"Error: Invalid scope '{scope}'. Use: single_email, entire_mailbox"

    result = run_applescript(script)
    if result.startswith("Error:"):
        return result
    try:
        if scope == "single_email":
            return _write_single_export(result, account, mailbox, save_dir, format)
        return _write_mailbox_export(result, account, mailbox, save_dir, format, max_emails)
    except OSError as exc:
        return f"Error: {exc}"


def _export_document(subject: str, sender: str, date: str, body: str, format: str) -> str:
    """File contents in the layout the AppleScript exporter used (CR line ends)."""
    if format == "txt":
        return f"Subject: {subject}\rFrom: {sender}\rDate: {date}\r\r{body}"
    return (
        f"<html><body><h2>{subject}</h2><p><strong>From:</strong> {sender}</p>"
        f"<p><strong>Date:</strong> {date}</p><hr>{body}</body></html>"
    )


def _parse_export_entry(line: str):
    """(id, subject, sender, date) of a FIELD_SEP-separated FOUND/ENTRY record."""
    parts = line.split(FIELD_SEP)
    if len(parts) < 5:
        return None
    return parts[1], parts[2], parts[3], FIELD_SEP.join(parts[4:])


def _write_single_export(result, account, mailbox, save_dir, format):
    entry = _parse_export_entry(result) if result.startswith("FOUND" + FIELD_SEP) else None
    if entry is None:
        return result
    message_id, subject, sender, date = entry
    body = get_message_body(message_id, account, mailbox)
    if body is None:
        return "Error: could not read the message body"
    file_path = f"{save_dir}/{subject.replace('/', '-')}.{format}"
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(_export_document(subject, sender, date, body, format))
    return (
        "EXPORTING EMAIL\n\n✓ Email exported successfully!\n\n"
        f"Subject: {subject}\nSaved to: {file_path}"
    )


def _write_mailbox_export(result, account, mailbox, save_dir, format, max_emails):
    lines = result.split(RECORD_SEP)
    message_count = int(lines[0].split(FIELD_SEP, 1)[1]) if lines[0].startswith("COUNT" + FIELD_SEP) else 0
    export_dir = f"{save_dir}/{mailbox}_export"
    os.makedirs(export_dir, exist_ok=True)
    export_count = 0
    for line in lines[1:]:
        entry = _parse_export_entry(line) if line.startswith("ENTRY" + FIELD_SEP) else None
        if entry is None:
            continue
        message_id, subject, sender, date = entry
        body = get_message_body(message_id, account, mailbox)
        if body is None:
            continue  # the AppleScript exporter skipped messages without content
        export_count += 1
        file_name = f"{export_count}_{subject}.{format}".replace("/", "-")
        try:
            with open(f"{export_dir}/{file_name}", "w", encoding="utf-8") as handle:
                handle.write(_export_document(subject, sender, date, body, format))
        except OSError:
            continue  # continue with the next email if one fails
    out = "EXPORTING MAILBOX\n\n✓ Mailbox exported successfully!\n\n"
    out += f"Mailbox: {mailbox}\nTotal emails in mailbox: {message_count}\nExported: {export_count}\n"
    if export_count < message_count:
        out += f"(capped at max_emails={max_emails})\n"
    out += f"Location: {export_dir}"
    return out


def _dashboard_preview(message_id: str, account: str) -> str:
    """First 150 characters of the body with line breaks as spaces."""
    body = get_message_body(message_id, account, "INBOX") if message_id.isdigit() else None
    return preview(body[:150], 0) if body else ""


def _get_recent_emails_structured(
    max_total: int = 20,
    max_per_account: int = 10
) -> List[Dict[str, Any]]:
    """
    Internal helper to get recent emails from all accounts as structured data.

    Returns list of dicts with keys:
    - subject: str
    - sender: str
    - date: str
    - is_read: bool
    - account: str
    - preview: str
    """
    try:
        return _recent_emails_from_index(max_total, max_per_account)
    except IndexUnavailable:
        pass
    script = f'''
    tell application "Mail"
        set allEmails to {{}}
        set allAccounts to every account

        repeat with anAccount in allAccounts
            set accountName to name of anAccount
            set emailCount to 0

            try
                {inbox_mailbox_script("inboxMailbox", "anAccount")}

                set inboxMessages to every message of inboxMailbox

                repeat with aMessage in inboxMessages
                    if emailCount >= {max_per_account} then exit repeat

                    try
                        set messageSubject to subject of aMessage
                        set messageSender to sender of aMessage
                        set messageDate to date received of aMessage
                        set messageRead to read status of aMessage

                        -- Preview is read from disk in Python (emlx.py); pass the id
                        set messagePreview to (id of aMessage) as string

                        -- Fields SUBJECT SENDER DATE READ ACCOUNT PREVIEW, separated by the unit separator
                        set fs to {AS_FIELD_SEP}
                        set emailRecord to messageSubject & fs & messageSender & fs & (messageDate as string) & fs & messageRead & fs & accountName & fs & messagePreview
                        set end of allEmails to emailRecord
                        set emailCount to emailCount + 1
                    end try
                end repeat
            end try
        end repeat

        -- Join all emails with the record separator
        set AppleScript's text item delimiters to {AS_RECORD_SEP}
        set emailOutput to allEmails as string
        set AppleScript's text item delimiters to ""
        return emailOutput
    end tell
    '''

    result = run_applescript(script)

    # Parse the result into structured data
    emails = []
    if result:
        for line in result.split(RECORD_SEP):
            if FIELD_SEP in line:
                parts = line.split(FIELD_SEP, 5)
                if len(parts) >= 5:
                    emails.append({
                        'subject': parts[0].strip(),
                        'sender': parts[1].strip(),
                        'date': parts[2].strip(),
                        'is_read': parts[3].strip().lower() == 'true',
                        'account': parts[4].strip(),
                        'preview': _dashboard_preview(parts[5].strip(), parts[4].strip()) if len(parts) > 5 else ''
                    })

    # Emails arrive in inbox order (newest first per account)
    # Limit to max_total
    return emails[:max_total]


@mcp.tool()
@inject_preferences
def inbox_dashboard() -> Any:
    """
    Get an interactive dashboard view of your email inbox.

    Returns an interactive UI dashboard resource that displays:
    - Unread email counts by account (visual cards with badges)
    - Recent emails across all accounts (filterable list)
    - Quick action buttons for common operations (Mark Read, Archive, Delete)
    - Search functionality to filter emails

    This tool returns a UIResource that can be rendered by compatible
    MCP clients (like Claude Desktop with MCP Apps support) to provide
    an interactive dashboard experience.

    Note: Requires mcp-ui-server package and a compatible MCP client.

    Returns:
        UIResource with uri "ui://apple-mail/inbox-dashboard" containing
        an interactive HTML dashboard, or error message if UI is unavailable.
    """
    from apple_mail_mcp import UI_AVAILABLE
    if not UI_AVAILABLE:
        return "Error: UI module not available. Please install mcp-ui-server package."

    from apple_mail_mcp.tools.inbox import get_mailbox_unread_counts
    from ui import create_inbox_dashboard_ui

    # Get unread counts per account (summary_only gives flat account->count dict)
    accounts_data = get_mailbox_unread_counts(summary_only=True)

    # Get recent emails across all accounts as structured data
    recent_emails = _get_recent_emails_structured(
        max_total=20,
        max_per_account=10
    )

    # Create and return the UI resource
    return create_inbox_dashboard_ui(
        accounts_data=accounts_data,
        recent_emails=recent_emails
    )


# ---------------------------------------------------------------------------
# The same reports from Mail's Envelope Index (see envelope_index.py).
# ---------------------------------------------------------------------------

RULE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _counted_mailboxes(index, account: str):
    """Top-level mailboxes of *account* the scripts analyse (system folders skipped)."""
    uuid = envelope_index.account_uuid(account)
    skip = {fold(name) for name in SKIP_FOLDERS}
    for _, mailboxes in envelope_index.mailbox_tree(account, with_subs=False):
        for mailbox_name, _ in mailboxes:
            if fold(mailbox_name) in skip:
                continue
            mailbox = index.find_mailbox(uuid, mailbox_name)
            if mailbox is not None:
                yield mailbox_name, mailbox


def _percent(part: int, whole: int) -> int:
    """AppleScript ``round ((part / whole) * 100)`` (halves to even)."""
    return round((part / whole) * 100)


def _statistics_from_index(account, scope, sender, mailbox, days_back) -> str:
    if scope not in ("account_overview", "sender_stats", "mailbox_breakdown"):
        raise IndexUnavailable("the script reports the invalid scope")
    if scope == "sender_stats" and not sender:
        raise IndexUnavailable("the script reports the missing sender")
    index = envelope_index.get_index()
    cutoff = envelope_index.days_back_cutoff(days_back)

    if scope in ("account_overview", "sender_stats"):
        mailboxes = []
        for mailbox_name, box in _counted_mailboxes(index, account):
            if scope == "account_overview":
                messages = index.messages([box.id], received_after=cutoff)
            else:
                messages = index.messages([box.id], sender_contains=sender, received_after=cutoff)
            attached = index.with_attachments(m.id for m in messages)
            mailboxes.append((mailbox_name, [
                StatRow(str(m.id), m.read, m.flagged, m.id in attached, m.sender) for m in messages
            ]))
        if scope == "account_overview":
            return _account_overview_report(account, mailboxes)
        return _sender_stats_report(account, sender, mailboxes)

    mailbox_param = mailbox if mailbox else "INBOX"
    box = envelope_index.resolve_mailbox(index, envelope_index.account_uuid(account), mailbox_param)
    total, unread = index.counts([box.id])
    out = f"MAILBOX STATISTICS\n\nMailbox: {mailbox_param}\nAccount: {account}\n\n"
    out += f"Total messages: {total}\nUnread: {unread}\nRead: {total - unread}\n"
    return clean_script_output(out)


def _recent_emails_from_index(max_total: int, max_per_account: int) -> List[Dict[str, Any]]:
    index = envelope_index.get_index()
    rows = []
    for name, uuid in envelope_index.mail_accounts():
        inbox = envelope_index.find_inbox(index, uuid)
        if inbox is not None and max_per_account > 0:
            rows += [(name, m) for m in index.messages([inbox.id], limit=max_per_account)]
    rows = rows[:max_total]
    dates = envelope_index.date_strings(m.date_received for _, m in rows)
    return [
        {
            "subject": clean_script_output(message.subject),
            "sender": clean_script_output(message.sender),
            "date": dates[message.date_received],
            "is_read": message.read,
            "account": clean_script_output(name),
            "preview": _dashboard_preview(str(message.id), name),
        }
        for name, message in rows
    ]


class StatRow(NamedTuple):
    """One message as the statistics see it."""

    id: str
    read: bool
    flagged: bool
    has_attachments: bool
    sender: str


def _parse_statistics_rows(output: str) -> List[Tuple[str, List[StatRow]]]:
    """[(mailbox name, [StatRow])] from the statistics scripts' records."""
    mailboxes: List[Tuple[str, List[StatRow]]] = []
    for record in output.split(RECORD_SEP):
        parts = record.split(FIELD_SEP)
        if parts[0] == "MAILBOX" and len(parts) >= 2:
            mailboxes.append((FIELD_SEP.join(parts[1:]), []))
        elif parts[0] == "MESSAGE" and len(parts) >= 6 and mailboxes:
            _, message_id, read, flagged, attachments, *sender = parts
            mailboxes[-1][1].append(StatRow(
                message_id.strip(),
                read.strip() == "true",
                flagged.strip() == "true",
                attachments.strip() not in ("", "0"),
                FIELD_SEP.join(sender),
            ))
    return mailboxes


# A Gmail message is in All Mail and in every mailbox it is labelled with,
# which are all top-level mailboxes. The statistics count each message once
# (by id); the mailbox distribution still gives each mailbox its own count.


def _account_overview_report(account: str, mailboxes: List[Tuple[str, List[StatRow]]]) -> str:
    seen: set = set()
    total = unread = flagged = with_attachments = 0
    senders: list = []  # [sender, count] in the order first seen
    sender_slot: dict = {}
    mailbox_counts = []
    for mailbox_name, rows in mailboxes:
        if rows:
            mailbox_counts.append((mailbox_name, len(rows)))
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            total += 1
            if not row.read:
                unread += 1
            if row.flagged:
                flagged += 1
            if row.has_attachments:
                with_attachments += 1
            key = fold(row.sender)
            if key in sender_slot:
                senders[sender_slot[key]][1] += 1
            else:
                sender_slot[key] = len(senders)
                senders.append([row.sender, 1])
    read = total - unread

    out = "╔══════════════════════════════════════════╗\n"
    out += f"║      EMAIL STATISTICS - {account}       ║\n"
    out += "╚══════════════════════════════════════════╝\n\n"
    out += f"📊 VOLUME METRICS\n{RULE}\n"
    out += f"Total Emails: {total}\n"
    if total > 0:
        out += f"Unread: {unread} ({_percent(unread, total)}%)\n"
        out += f"Read: {read} ({_percent(read, total)}%)\n"
        out += f"Flagged: {flagged}\n"
        out += f"With Attachments: {with_attachments} ({_percent(with_attachments, total)}%)\n"
    else:
        out += "Unread: 0\nRead: 0\nFlagged: 0\nWith Attachments: 0\n"
    out += "\n"
    out += f"👥 TOP SENDERS\n{RULE}\n"
    for name, count in senders[:5]:
        out += f"{name}: {count} emails\n"
    out += "\n"
    out += f"📁 MAILBOX DISTRIBUTION\n{RULE}\n"
    for name, count in mailbox_counts[:5]:
        if total > 0:
            out += f"{name}: {count} ({_percent(count, total)}%)\n"
        else:
            out += f"{name}: {count}\n"
    return clean_script_output(out)


def _sender_stats_report(account: str, sender: str, mailboxes: List[Tuple[str, List[StatRow]]]) -> str:
    distinct = {row.id: row for _, rows in mailboxes for row in rows}
    total = len(distinct)
    unread = sum(1 for row in distinct.values() if not row.read)
    with_attachments = sum(1 for row in distinct.values() if row.has_attachments)
    out = f"SENDER STATISTICS\n\nSender: {sender}\nAccount: {account}\n\n"
    out += f"Total emails: {total}\nUnread: {unread}\nWith attachments: {with_attachments}\n"
    return clean_script_output(out)
