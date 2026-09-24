"""Search tools: finding and filtering emails."""

import json
import re
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote

from apple_mail_mcp.server import mcp
from apple_mail_mcp import envelope_index
from apple_mail_mcp.constants import ALL_MAIL_NAMES, FLAG_COLOR_NAMES, SKIP_FOLDERS
from apple_mail_mcp.envelope_index import IndexUnavailable, contains_ci, fold
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    AS_RECORD_SEP,
    FIELD_SEP,
    RECORD_SEP,
    clean_script_output,
    contains_any_condition,
    inject_preferences,
    escape_applescript,
    normalize_search_terms,
    resolve_flag_color,
    read_flag_index_script,
    run_applescript,
    equals_any_numeric_condition,
)
from apple_mail_mcp.emlx import (
    body_text as message_body_text,
    body_token_script,
    fill_body_tokens,
    get_message,
    get_message_body,
    new_body_nonce,
    preview,
)


# Seconds a whole body_text search may take - listing candidates, reading
# bodies (source fallbacks included) and fetching the matches' metadata -
# before it stops and returns what it found so far. The last
# BODY_SEARCH_FETCH_RESERVE_S of it are kept for the metadata fetch.
BODY_SEARCH_BUDGET_S = 120
BODY_SEARCH_FETCH_RESERVE_S = 20
BODY_SEARCH_INCOMPLETE_NOTE = (
    f"Body search stopped after {BODY_SEARCH_BUDGET_S}s before scanning every "
    "message; results may be incomplete. Narrow it with date_from/date_to, "
    "sender, subject_keyword or read_status."
)

MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _build_applescript_date(
    var_name: str, date_value: Optional[str], end_of_day: bool = False
) -> str:
    """Build AppleScript to create a date from an ISO day string."""
    if not date_value:
        return ""

    try:
        parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{date_value}'. Use YYYY-MM-DD")

    month_name = MONTH_NAMES[parsed_date.month - 1]
    seconds = 86399 if end_of_day else 0
    return f"""
                set {var_name} to current date
                set year of {var_name} to {parsed_date.year}
                set month of {var_name} to {month_name}
                set day of {var_name} to {parsed_date.day}
                set time of {var_name} to {seconds}
    """


def _timed_out(exc: Exception, deadline: float) -> bool:
    return "timed out" in str(exc) or time.monotonic() >= deadline


def _parse_search_records(output: str) -> List[Dict[str, Any]]:
    """Parse structured search output into dict records.

    Fields are separated by FIELD_SEP and records by RECORD_SEP, so a subject
    or mailbox name can hold any printable text.
    """
    if not output:
        return []

    records = []
    for line in output.split(RECORD_SEP):
        parts = line.split(FIELD_SEP, 9)
        if len(parts) < 9:
            continue

        internet_message_id = parts[1].strip()
        try:
            flag_index = int(parts[8].strip())
        except ValueError:
            flag_index = -1
        record = {
            "message_id": parts[0].strip(),
            "internet_message_id": internet_message_id,
            "subject": parts[2].strip(),
            "sender": parts[3].strip(),
            "mailbox": parts[4].strip(),
            "account": parts[5].strip(),
            "is_read": parts[6].strip().lower() == "true",
            "received_date": parts[7].strip(),
            "is_flagged": flag_index >= 0,
        }
        if flag_index in FLAG_COLOR_NAMES:
            record["flag_color"] = FLAG_COLOR_NAMES[flag_index]
        if internet_message_id:
            # Apple Mail requires: message:// scheme, angle brackets (percent-encoded),
            # and raw @ in the Message-ID. Normalize ID in case angle brackets are
            # present or missing (AppleScript returns both forms).
            msg_id = internet_message_id.strip("<>")
            record["mail_link"] = f"message://%3C{quote(msg_id, safe='@')}%3E"
        if len(parts) > 9 and parts[9].strip():
            record["content_preview"] = parts[9].strip()
        records.append(record)

    return records


def _sort_search_records(
    records: List[Dict[str, Any]], sort: str
) -> List[Dict[str, Any]]:
    """Sort records by received date."""
    reverse = sort == "date_desc"
    return sorted(
        records, key=lambda item: item.get("received_date", ""), reverse=reverse
    )


def _format_search_records_text(
    records: List[Dict[str, Any]],
    subject_only: bool = False,
) -> str:
    """Format search records as human-readable text."""
    lines = []

    if subject_only:
        lines.append("SUBJECT SEARCH RESULTS")
        lines.append("")
        for item in records:
            lines.append(f"- {item['subject']}")
    else:
        lines.append("SEARCH RESULTS")
        lines.append("")
        for item in records:
            indicator = "\u2713" if item["is_read"] else "\u2709"
            subject_line = f"{indicator} {item['subject']}"
            if item.get("flag_color"):
                subject_line += f" \u2691 {item['flag_color']}"
            elif item.get("is_flagged"):
                subject_line += " \u2691"
            lines.append(subject_line)
            lines.append(f"   From: {item['sender']}")
            lines.append(f"   Date: {item['received_date']}")
            lines.append(f"   Mailbox: {item['mailbox']}")
            if item.get("mail_link"):
                lines.append(f"   Link: {item['mail_link']}")
            if item.get("content_preview"):
                lines.append(f"   Content: {item['content_preview']}")
            lines.append("")

    lines.append("========================================")
    lines.append(f"FOUND: {len(records)} matching email(s)")
    lines.append("========================================")
    return "\n".join(lines)


def _build_search_response(
    records: List[Dict[str, Any]],
    offset: int,
    limit: int,
    sort: str,
    output_format: str,
    subject_only: bool = False,
    incomplete: bool = False,
) -> str:
    """Return either JSON or text for search results."""
    sorted_records = _sort_search_records(records, sort)
    has_more = len(sorted_records) > limit
    items = sorted_records[:limit]
    next_offset = offset + len(items) if has_more else None

    if output_format == "json":
        payload = {
            "items": items,
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "has_more": has_more,
            "next_offset": next_offset,
            "sort": sort,
        }
        if incomplete:
            payload["incomplete"] = True
            payload["note"] = BODY_SEARCH_INCOMPLETE_NOTE
        return json.dumps(payload)

    text = _format_search_records_text(items, subject_only=subject_only)
    if incomplete:
        text += "\n" + BODY_SEARCH_INCOMPLETE_NOTE
    return text


def _search_mail_records(
    account: Optional[str] = None,
    mailbox: str = "INBOX",
    subject_terms: Optional[List[str]] = None,
    sender: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    flagged: Optional[bool] = None,
    flag_color: Optional[str] = None,
    read_status: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_content: bool = False,
    content_length: int = 300,
    offset: int = 0,
    limit: int = 100,
    sort: str = "date_desc",
    body_text: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return (records, incomplete) from Apple Mail.

    When account is None, iterates all accounts.
    When body_text is provided, uses per-message iteration with case-insensitive
    content matching (slower than subject/sender-only searches). *incomplete*
    is True when that body scan stopped at BODY_SEARCH_BUDGET_S.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit <= 0:
        return [], False
    if sort not in {"date_desc", "date_asc"}:
        raise ValueError("Invalid sort. Use: date_desc, date_asc")
    if read_status not in {"all", "read", "unread"}:
        raise ValueError("Invalid read_status. Use: all, read, unread")

    flag_index: Optional[int] = None
    if flag_color is not None:
        flag_index = resolve_flag_color(flag_color)
        if flagged is False:
            raise ValueError(
                "flag_color cannot be combined with flagged=False "
                "(a colored flag implies the message is flagged)"
            )

    escaped_sender = escape_applescript(sender) if sender else None

    # When body_text is provided, the body match must be checked per message
    # (Mail's whose clause on content is as slow as reading it). Every other
    # filter still goes into the whose clause so only candidates are read.
    use_body_search = body_text is not None

    # Build whose-clause filter conditions
    filter_conditions = []
    if subject_terms:
        filter_conditions.append(contains_any_condition("subject", subject_terms))
    if sender:
        filter_conditions.append(f'sender contains "{escaped_sender}"')
    if has_attachments is not None and not use_body_search:
        if has_attachments:
            filter_conditions.append("(count of mail attachments) > 0")
        else:
            filter_conditions.append("(count of mail attachments) = 0")
    if flag_index is not None:
        # flag index survives unflagging; require the active-flag boolean
        # so residual indexes on unflagged messages don't match.
        filter_conditions.append(
            f"(flagged status is true and flag index is {flag_index})"
        )
    elif flagged is not None:
        filter_conditions.append(
            f"flagged status is {'true' if flagged else 'false'}"
        )
    if read_status == "read":
        filter_conditions.append("read status is true")
    elif read_status == "unread":
        filter_conditions.append("read status is false")
    if date_from:
        filter_conditions.append("date received >= fromDate")
    if date_to:
        filter_conditions.append("date received <= toDate")

    if filter_conditions:
        matching_messages_script = f"set matchingMessages to every message of currentMailbox whose {' and '.join(filter_conditions)}"
    else:
        matching_messages_script = (
            "set matchingMessages to every message of currentMailbox"
        )

    if mailbox == "All":
        mailbox_script = """
                set searchMailboxes to every mailbox of targetAccount
        """
        skip_names = ", ".join(f'"{escape_applescript(name)}"' for name in SKIP_FOLDERS)
        skip_script = """
                        set skipFolders to {""" + skip_names + """}
                        repeat with skipFolder in skipFolders
                            if mailboxName is skipFolder then
                                set shouldSkip to true
                                exit repeat
                            end if
                        end repeat
        """
    else:
        escaped_mailbox = escape_applescript(mailbox)
        mailbox_script = f'''
                try
                    set searchMailbox to mailbox "{escaped_mailbox}" of targetAccount
                on error
                    if "{escaped_mailbox}" is "INBOX" then
                        set searchMailbox to mailbox "Inbox" of targetAccount
                    else
                        error "Mailbox not found: {escaped_mailbox}"
                    end if
                end try
                set searchMailboxes to {{searchMailbox}}
        '''
        skip_script = ""

    date_setup = _build_applescript_date("fromDate", date_from)
    date_setup += _build_applescript_date("toDate", date_to, end_of_day=True)

    # Build account iteration
    if account:
        escaped_account = escape_applescript(account)
        account_setup = f'''
                set searchAccounts to {{account "{escaped_account}"}}
        '''
    else:
        account_setup = """
                set searchAccounts to every account
        """

    try:
        records, incomplete, body_texts = _search_records_from_index(
            account=account,
            mailbox=mailbox,
            subject_terms=subject_terms,
            sender=sender,
            has_attachments=has_attachments,
            flagged=flagged,
            flag_index=flag_index,
            read_status=read_status,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
            body_text=body_text,
        )
    except IndexUnavailable:
        pass
    else:
        _add_content_previews(records, include_content, content_length, body_texts if use_body_search else None)
        return records, incomplete

    # Choose the message collection strategy. For a body search the script
    # below is run once the matching ids are known (see _body_search_ids).
    message_collection = f"                            {matching_messages_script}"
    script_offset = offset
    collect_limit = limit + 1
    incomplete = False
    deadline: Optional[float] = None
    body_texts: Dict[str, str] = {}

    chosen: Optional[Dict[str, Tuple[str, str]]] = None

    if use_body_search or mailbox == "All":
        # Mail lists candidate ids per mailbox first; the messages are chosen
        # here and their metadata fetched by id below. For a body search the
        # bodies are matched from disk in between; over all mailboxes each
        # message is kept once (see _dedupe_candidates).
        id_selection = (
            "id of (" + matching_messages_script.replace("set matchingMessages to ", "", 1) + ")"
        )
        listing = dict(
            id_selection=id_selection,
            date_setup=date_setup,
            account_setup=account_setup,
            mailbox_script=mailbox_script,
            skip_script=skip_script,
        )
        if use_body_search:
            # One budget for the whole body search: listing candidates, reading
            # bodies (source fallbacks included) and fetching the metadata below.
            deadline = time.monotonic() + BODY_SEARCH_BUDGET_S
            read_until = deadline - BODY_SEARCH_FETCH_RESERVE_S
            candidates, incomplete = _list_candidate_ids(**listing, until=read_until)
        else:
            candidates, _ = _list_candidate_ids(**listing, until=None)
        if mailbox == "All":
            candidates = _dedupe_candidates(
                candidates, [fold(name) in _ALL_MAIL_KEYS for _, name, _ in candidates]
            )
        if use_body_search:
            if not incomplete:
                body_texts, incomplete = _match_bodies(
                    candidates, body_text, has_attachments, offset + limit + 1, read_until
                )
            ordered = list(body_texts)
        else:
            ordered = [i for _, _, ids in candidates for i in ids]
        chosen = {i: (account_name, mailbox_name) for account_name, mailbox_name, ids in candidates for i in ids}
        selected = ordered[offset:offset + limit + 1]
        if not selected:
            return [], incomplete
        message_collection = (
            "                            set matchingMessages to every message of "
            f"currentMailbox whose {equals_any_numeric_condition('id', selected)}"
        )
        script_offset = 0
        # A message listed in several mailboxes is fetched from each; the
        # copies from mailboxes it was not chosen for are dropped below.
        collect_limit = len(selected) * max(1, len(candidates))

    timeout = 180
    if deadline is not None:
        timeout = int(deadline - time.monotonic())
        if timeout < 1:
            return [], True

    script = f'''

    on sanitize_field(value)
        try
            set valueText to value as string
        on error
            set valueText to ""
        end try

        set AppleScript's text item delimiters to {{return, linefeed, tab, {AS_FIELD_SEP}, {AS_RECORD_SEP}}}
        set valueParts to text items of valueText
        set AppleScript's text item delimiters to " "
        set valueText to valueParts as string
        set AppleScript's text item delimiters to ""
        return valueText
    end sanitize_field

    on pad2(numberValue)
        if numberValue < 10 then
            return "0" & (numberValue as string)
        end if
        return numberValue as string
    end pad2

    on month_number(monthValue)
        set monthValues to {{January, February, March, April, May, June, July, August, September, October, November, December}}
        repeat with monthIndex from 1 to 12
            if item monthIndex of monthValues is monthValue then
                return monthIndex
            end if
        end repeat
        return 0
    end month_number

    on iso_datetime(dateValue)
        set yearValue to year of dateValue as integer
        set monthValue to my month_number(month of dateValue)
        set dayValue to day of dateValue as integer
        set hourValue to hours of dateValue
        set minuteValue to minutes of dateValue
        set secondValue to seconds of dateValue
        return (yearValue as string) & "-" & my pad2(monthValue) & "-" & my pad2(dayValue) & "T" & my pad2(hourValue) & ":" & my pad2(minuteValue) & ":" & my pad2(secondValue)
    end iso_datetime

    tell application "Mail"
        with timeout of {timeout} seconds
            try
                set recordLines to {{}}
                set offsetRemaining to {script_offset}
                set collectLimit to {collect_limit}
                {date_setup}
                {account_setup}

                repeat with targetAccount in searchAccounts
                    if collectLimit <= 0 then exit repeat
                    set accountName to my sanitize_field(name of targetAccount)
                    {mailbox_script}

                    repeat with currentMailbox in searchMailboxes
                        if collectLimit <= 0 then exit repeat

                        try
                            set mailboxName to my sanitize_field(name of currentMailbox)
                            set shouldSkip to false
                            {skip_script}

                            if not shouldSkip then
                                {message_collection}
                                set matchingCount to count of matchingMessages

                                if offsetRemaining >= matchingCount then
                                    set offsetRemaining to offsetRemaining - matchingCount
                                else
                                    set startIndex to offsetRemaining + 1
                                    set availableCount to matchingCount - offsetRemaining
                                    if availableCount > collectLimit then
                                        set endIndex to startIndex + collectLimit - 1
                                    else
                                        set endIndex to startIndex + availableCount - 1
                                    end if

                                    if endIndex >= startIndex then
                                        set targetMessages to items startIndex thru endIndex of matchingMessages

                                        repeat with aMessage in targetMessages
                                            try
                                                set messageId to my sanitize_field(id of aMessage)
                                                set internetMessageId to ""
                                                try
                                                    set internetMessageId to my sanitize_field(message id of aMessage)
                                                end try
                                                set messageSubject to my sanitize_field(subject of aMessage)
                                                set messageSender to my sanitize_field(sender of aMessage)
                                                set messageRead to read status of aMessage
                                                set messageDate to date received of aMessage
                                                set receivedAt to my iso_datetime(messageDate)
                                                {read_flag_index_script()}
                                                -- Body previews are added from disk after the script (emlx.py)
                                                set contentPreview to ""

                                                set readValue to "false"
                                                if messageRead then
                                                    set readValue to "true"
                                                end if

                                                set fs to {AS_FIELD_SEP}
                                                set recordLine to messageId & fs & internetMessageId & fs & messageSubject & fs & messageSender & fs & mailboxName & fs & accountName & fs & readValue & fs & receivedAt & fs & (messageFlagIndex as string) & fs & contentPreview
                                                set end of recordLines to recordLine
                                                set collectLimit to collectLimit - 1
                                                if collectLimit <= 0 then exit repeat
                                            end try
                                        end repeat
                                    end if

                                    set offsetRemaining to 0
                                end if
                            end if
                        on error
                            -- Skip mailboxes that cannot be searched
                        end try
                    end repeat
                end repeat

                if (count of recordLines) is 0 then
                    return ""
                end if

                set AppleScript's text item delimiters to {AS_RECORD_SEP}
                set outputText to recordLines as string
                set AppleScript's text item delimiters to ""
                return outputText
            on error errMsg
                return "ERROR" & {AS_FIELD_SEP} & errMsg
            end try
        end timeout
    end tell
    '''

    try:
        result = run_applescript(script, timeout=timeout)
    except Exception as exc:
        if deadline is not None and _timed_out(exc, deadline):
            return [], True
        raise
    if result.startswith("ERROR" + FIELD_SEP):
        raise ValueError(result.split(FIELD_SEP, 1)[1])

    records = _parse_search_records(result)
    if chosen is not None:
        records = [r for r in records if _is_chosen(r, chosen)]
    _add_content_previews(records, include_content, content_length, body_texts if use_body_search else None)
    return records, incomplete


_ALL_MAIL_KEYS = {fold(name) for name in ALL_MAIL_NAMES}


def _clean_field(value: str) -> str:
    """A name as it reads in a search record (the script's sanitize_field)."""
    return re.sub(r"[\r\n\t\x1f\x1e]", " ", value).strip()


def _is_chosen(record: Dict[str, Any], chosen: Dict[str, Tuple[str, str]]) -> bool:
    where = chosen.get(record["message_id"])
    return where is not None and (
        _clean_field(where[0]) == record["account"] and _clean_field(where[1]) == record["mailbox"]
    )


def _dedupe_candidates(
    candidates: List[Tuple[str, str, List[str]]], all_mail: List[bool]
) -> List[Tuple[str, str, List[str]]]:
    """List each message once over all mailboxes.

    A Gmail message is in All Mail and in each mailbox it is labelled with
    (INBOX, ...). It is kept under the first label mailbox that lists it, in
    Mail's order, and under All Mail only when no other mailbox has it.
    """
    order = [c for c, is_all in zip(candidates, all_mail) if not is_all]
    order += [c for c, is_all in zip(candidates, all_mail) if is_all]
    seen: set = set()
    kept = []
    for account_name, mailbox_name, ids in order:
        fresh = [i for i in ids if i not in seen]
        seen.update(fresh)
        kept.append((account_name, mailbox_name, fresh))
    return kept


def _add_content_previews(
    records: List[Dict[str, Any]],
    include_content: bool,
    content_length: int,
    body_texts: Optional[Dict[str, str]],
) -> None:
    """Set content_preview from disk; a body search passes the bodies it read."""
    if not include_content:
        return
    for record in records:
        if body_texts is not None:
            body = body_texts.get(record["message_id"])  # already read
        else:
            body = get_message_body(record["message_id"], record["account"], record["mailbox"])
        text = preview(body, content_length, strip_tabs=True)
        if text:
            record["content_preview"] = text.strip()


def _list_candidate_ids(
    id_selection: str,
    date_setup: str,
    account_setup: str,
    mailbox_script: str,
    skip_script: str,
    until: Optional[float],
) -> Tuple[List[Tuple[str, str, List[str]]], bool]:
    """([(account, mailbox, [ids newest first])], timed out) listed by Mail.

    One Apple Event per mailbox, with every filter in the whose clause. With
    *until* (a monotonic deadline) the listing gets the time left and a
    timeout reports (nothing, True); otherwise it may take 180 s.
    """
    timeout = 180
    if until is not None:
        timeout = int(until - time.monotonic())
        if timeout < 1:
            return [], True

    script = f'''
    tell application "Mail"
        with timeout of {timeout} seconds
            try
                set outLines to {{}}
                {date_setup}
                {account_setup}
                repeat with targetAccount in searchAccounts
                    set accountName to name of targetAccount
                    {mailbox_script}
                    repeat with currentMailbox in searchMailboxes
                        try
                            set mailboxName to name of currentMailbox
                            set shouldSkip to false
                            {skip_script}
                            if not shouldSkip then
                                set idList to {id_selection}
                                set AppleScript's text item delimiters to ","
                                set end of outLines to accountName & {AS_FIELD_SEP} & mailboxName & {AS_FIELD_SEP} & (idList as string)
                                set AppleScript's text item delimiters to ""
                            end if
                        on error
                            -- Skip mailboxes that cannot be searched
                        end try
                    end repeat
                end repeat
                set AppleScript's text item delimiters to {AS_RECORD_SEP}
                set outputText to outLines as string
                set AppleScript's text item delimiters to ""
                return outputText
            on error errMsg
                return "ERROR" & {AS_FIELD_SEP} & errMsg
            end try
        end timeout
    end tell
    '''
    try:
        result = run_applescript(script, timeout=timeout)
    except Exception as exc:
        if until is not None and _timed_out(exc, until):
            return [], True
        raise
    if result.startswith("ERROR" + FIELD_SEP):
        raise ValueError(result.split(FIELD_SEP, 1)[1])

    candidates = []
    for line in result.split(RECORD_SEP):
        parts = line.split(FIELD_SEP)
        if len(parts) != 3:
            continue
        account_name, mailbox_name, id_text = parts
        ids = [i.strip() for i in id_text.split(",")]
        candidates.append((account_name, mailbox_name, [i for i in ids if i.isdigit()]))
    return candidates, False


def _match_bodies(
    candidates: List[Tuple[str, str, List[str]]],
    body_text: str,
    has_attachments: Optional[bool],
    wanted: int,
    read_until: float,
) -> Tuple[Dict[str, str], bool]:
    """Read candidate bodies from disk in order; ({id: body} of matches, incomplete)."""
    needle = body_text.lower()
    matches: Dict[str, str] = {}
    for account_name, mailbox_name, ids in candidates:
        for message_id in ids:
            if len(matches) >= wanted:
                return matches, False
            remaining = int(read_until - time.monotonic())
            if remaining < 1:
                return matches, True
            message = get_message(message_id, account_name, mailbox_name, timeout=remaining)
            if message is None:
                if time.monotonic() >= read_until:
                    return matches, True  # the source fallback ran out of time
                continue
            if has_attachments is not None:
                if bool(list(message.iter_attachments())) != has_attachments:
                    continue
            text = message_body_text(message)
            if text is not None and needle in text.lower():
                matches[message_id] = text
    return matches, False


@mcp.tool()
@inject_preferences
def search_emails(
    account: Optional[str] = None,
    mailbox: str = "INBOX",
    subject_keyword: Optional[str] = None,
    subject_keywords: Optional[List[str]] = None,
    sender: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    flagged: Optional[bool] = None,
    flag_color: Optional[str] = None,
    read_status: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_content: bool = False,
    max_content_length: int = 500,
    body_text: Optional[str] = None,
    max_results: Optional[int] = 20,
    output_format: str = "text",
    offset: int = 0,
    limit: Optional[int] = None,
    sort: str = "date_desc",
) -> str:
    """
    Unified search tool with JSON output, pagination, and real date filtering.

    Consolidates subject search, sender search, body content search, and
    cross-account search into a single tool.

    Args:
        account: Account name to search in (e.g., "Gmail", "Work").
            If None, searches ALL accounts (slower).
        mailbox: Mailbox to search (default: "INBOX", use "All" for all mailboxes, or specific folder name)
        subject_keyword: Optional keyword to search in subject
        subject_keywords: Optional list of subject keywords; matches any keyword
        sender: Optional sender email or name to filter by
        has_attachments: Optional filter for emails with attachments (True/False/None)
        flagged: Optional filter for flagged emails (True/False/None)
        flag_color: Optional filter for a specific flag color: "red", "orange",
            "yellow", "green", "blue", "purple", or "gray". Implies flagged=True.
        read_status: Filter by read status: "all", "read", "unread" (default: "all")
        date_from: Optional start date filter (format: "YYYY-MM-DD")
        date_to: Optional end date filter (format: "YYYY-MM-DD")
        include_content: Whether to include email content preview (slower)
        max_content_length: Maximum content length in characters when include_content=True (default: 500, 0 = unlimited)
        body_text: Optional text to search for in email body content (case-insensitive).
            WARNING: body search is significantly slower as it reads each message body.
        max_results: Backward-compatible alias for limit
        output_format: Output format: "text" or "json" (default: "text")
        offset: Number of matching results to skip before returning data
        limit: Maximum number of results to return per page
        sort: Result sort order: "date_desc" or "date_asc"

    Returns:
        Formatted list of matching emails or JSON payload with stable message metadata
    """
    if output_format not in {"text", "json"}:
        return "Error: Invalid output_format. Use: text, json"

    if limit is None:
        limit = max_results if max_results is not None else 100

    subject_terms = normalize_search_terms(subject_keyword, subject_keywords)

    try:
        records, incomplete = _search_mail_records(
            account=account,
            mailbox=mailbox,
            subject_terms=subject_terms,
            sender=sender,
            has_attachments=has_attachments,
            flagged=flagged,
            flag_color=flag_color,
            read_status=read_status,
            date_from=date_from,
            date_to=date_to,
            include_content=include_content,
            content_length=max_content_length,
            offset=offset,
            limit=limit,
            sort=sort,
            body_text=body_text,
        )
        return _build_search_response(
            records,
            offset=offset,
            limit=limit,
            sort=sort,
            output_format=output_format,
            subject_only=False,
            incomplete=incomplete,
        )
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
@inject_preferences
def get_email_thread(
    account: str, subject_keyword: str, mailbox: str = "INBOX", max_messages: int = 50
) -> str:
    """
    Get an email conversation thread - all messages with the same or similar subject.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        subject_keyword: Keyword to identify the thread (e.g., "Re: Project Update")
        mailbox: Mailbox to search in (default: "INBOX", use "All" for all mailboxes)
        max_messages: Maximum number of thread messages to return (default: 50)

    Returns:
        Formatted thread view with all related messages sorted by date
    """

    # Escape user inputs for AppleScript
    escaped_account = escape_applescript(account)
    escaped_mailbox = escape_applescript(mailbox)

    # For thread detection, we'll strip common prefixes
    thread_keywords = ["Re:", "Fwd:", "FW:", "RE:", "Fw:"]
    cleaned_keyword = subject_keyword
    for prefix in thread_keywords:
        cleaned_keyword = cleaned_keyword.replace(prefix, "").strip()
    escaped_keyword = escape_applescript(cleaned_keyword)
    nonce = new_body_nonce()

    try:
        return _email_thread_from_index(account, mailbox, cleaned_keyword, max_messages, nonce)
    except IndexUnavailable:
        pass

    mailbox_script = f'''
        try
            set searchMailbox to mailbox "{escaped_mailbox}" of targetAccount
        on error
            if "{escaped_mailbox}" is "INBOX" then
                set searchMailbox to mailbox "Inbox" of targetAccount
            else if "{escaped_mailbox}" is "All" then
                set searchMailboxes to every mailbox of targetAccount
                set useAllMailboxes to true
            else
                error "Mailbox not found: {escaped_mailbox}"
            end if
        end try

        if "{escaped_mailbox}" is not "All" then
            set searchMailboxes to {{searchMailbox}}
            set useAllMailboxes to false
        end if
    '''

    script = f'''
    tell application "Mail"
        set outputText to "EMAIL THREAD VIEW" & return & return
        set outputText to outputText & "Thread topic: {escaped_keyword}" & return
        set outputText to outputText & "Account: {escaped_account}" & return & return
        set threadMessages to {{}}

        try
            set targetAccount to account "{escaped_account}"
            {mailbox_script}

            -- Collect all matching messages from all mailboxes. Stripping a
            -- Re:/Fwd: prefix only shortens the subject, so a subject matches
            -- exactly when it contains the keyword: let Mail filter with a
            -- whose clause instead of reading every subject one event at a time.
            repeat with currentMailbox in searchMailboxes
                if (count of threadMessages) >= {max_messages} then exit repeat
                set mailboxMessages to (every message of currentMailbox whose subject contains "{escaped_keyword}")

                repeat with aMessage in mailboxMessages
                    if (count of threadMessages) >= {max_messages} then exit repeat

                    try
                        set messageSubject to subject of aMessage

                        -- Remove common prefixes for matching
                        set cleanSubject to messageSubject
                        if cleanSubject starts with "Re: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if
                        if cleanSubject starts with "RE: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if
                        if cleanSubject starts with "Fwd: " then
                            set cleanSubject to text 6 thru -1 of cleanSubject
                        else if cleanSubject starts with "FW: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        else if cleanSubject starts with "Fw: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if

                        -- Check if this message is part of the thread
                        if cleanSubject contains "{escaped_keyword}" or messageSubject contains "{escaped_keyword}" then
                            set end of threadMessages to aMessage
                        end if
                    end try
                end repeat
            end repeat

            -- Display thread messages
            set messageCount to count of threadMessages
            set outputText to outputText & "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501" & return
            set outputText to outputText & "FOUND " & messageCount & " MESSAGE(S) IN THREAD" & return
            set outputText to outputText & "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501" & return & return

            repeat with aMessage in threadMessages
                try
                    set messageSubject to subject of aMessage
                    set messageSender to sender of aMessage
                    set messageDate to date received of aMessage
                    set messageRead to read status of aMessage

                    if messageRead then
                        set readIndicator to "\u2713"
                    else
                        set readIndicator to "\u2709"
                    end if

                    set outputText to outputText & readIndicator & " " & messageSubject & return
                    set outputText to outputText & "   From: " & messageSender & return
                    set outputText to outputText & "   Date: " & (messageDate as string) & return

                    -- Body preview: filled in from disk after the script (emlx.py)
                    set outputText to outputText & "   Preview: " & {body_token_script(nonce, "aMessage", '"' + escaped_account + '"', '"' + escaped_mailbox + '"')} & return

                    set outputText to outputText & return
                end try
            end repeat

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    '''

    result = run_applescript(script)
    return fill_body_tokens(result, 150, nonce, missing=None)


# ---------------------------------------------------------------------------
# search_emails from Mail's Envelope Index (see envelope_index.py)
# ---------------------------------------------------------------------------

def _day_start(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d").timestamp()


def _search_mailboxes(index, account: Optional[str], mailbox: str):
    """[(account name, [(mailbox name, Mailbox)])] in the script's order."""
    if account:
        uuid = envelope_index.account_uuid(account)
        accounts = [(name, u) for name, u in envelope_index.mail_accounts() if u == uuid][:1]
    else:
        accounts = envelope_index.mail_accounts()
    if mailbox == "All":
        skip = {fold(name) for name in SKIP_FOLDERS}
        tree = dict(envelope_index.mailbox_tree(account, with_subs=False))
        found = []
        for name, uuid in accounts:
            boxes = []
            for mailbox_name, _ in tree.get(name, []):
                box = index.find_mailbox(uuid, mailbox_name)
                if box is not None and fold(mailbox_name) not in skip:
                    boxes.append((mailbox_name, box))
            found.append((name, boxes))
        return found
    found = []
    for name, uuid in accounts:
        box = envelope_index.resolve_mailbox(index, uuid, mailbox)
        found.append((name, [(box.name, box)]))
    return found


def _record_line(message, mailbox_name: str, account_name: str) -> str:
    def field(value) -> str:
        # the script's sanitize_field
        return re.sub(r"[\r\n\t\x1f\x1e]", " ", str(value))

    return FIELD_SEP.join(
        [
            str(message.id),
            field(message.internet_message_id),
            field(message.subject),
            field(message.sender),
            field(mailbox_name),
            field(account_name),
            "true" if message.read else "false",
            envelope_index.iso_datetime(message.date_received),
            str(message.flag_index),
            "",
        ]
    )


def _search_records_from_index(
    account, mailbox, subject_terms, sender, has_attachments, flagged, flag_index,
    read_status, date_from, date_to, offset, limit, body_text,
) -> Tuple[List[Dict[str, Any]], bool, Dict[str, str]]:
    """(records, incomplete, bodies read) as the search script would find them."""
    index = envelope_index.get_index()
    filters: Dict[str, Any] = dict(
        subject_contains_any=subject_terms or None,
        sender_contains=sender or None,
        flagged=flagged,
        flag_index=flag_index,
    )
    if read_status != "all":
        filters["read"] = read_status == "read"
    if date_from:
        filters["received_from"] = _day_start(date_from)
    if date_to:
        filters["received_to"] = _day_start(date_to) + 86399
    if body_text is None and has_attachments is not None:
        filters["has_attachments"] = has_attachments

    layout = _search_mailboxes(index, account, mailbox)

    incomplete = False
    body_texts: Dict[str, str] = {}
    lines = []
    if body_text is None and mailbox != "All":
        skip, collect = offset, limit + 1
        for account_name, boxes in layout:
            for mailbox_name, box in boxes:
                if collect <= 0:
                    break
                matching = index.count([box.id], **filters)
                if skip >= matching:
                    skip -= matching
                    continue
                page = index.messages([box.id], **filters, limit=collect, offset=skip)
                skip = 0
                collect -= len(page)
                lines += [_record_line(m, mailbox_name, account_name) for m in page]
        return _parse_search_records(clean_script_output(RECORD_SEP.join(lines))), incomplete, body_texts

    # As the script: candidate ids per mailbox, then (over all mailboxes)
    # each message once, then the bodies, then the chosen page.
    boxes_in_order = [(a, name, box) for a, boxes in layout for name, box in boxes]
    candidates = [
        (a, name, [str(m.id) for m in index.messages([box.id], **filters)]) for a, name, box in boxes_in_order
    ]
    if mailbox == "All":
        all_mail = index.all_mail_ids()
        candidates = _dedupe_candidates(
            candidates,
            [box.id in all_mail or fold(name) in _ALL_MAIL_KEYS for _, name, box in boxes_in_order],
        )
    if body_text is not None:
        deadline = time.monotonic() + BODY_SEARCH_BUDGET_S
        body_texts, incomplete = _match_bodies(
            candidates, body_text, has_attachments, offset + limit + 1,
            deadline - BODY_SEARCH_FETCH_RESERVE_S,
        )
        ordered = list(body_texts)
    else:
        ordered = [i for _, _, ids in candidates for i in ids]
    selected = set(ordered[offset:offset + limit + 1])
    where = {i: (a, name) for a, name, ids in candidates for i in ids}
    for account_name, mailbox_name, box in boxes_in_order:
        mine = [int(i) for i in selected if where.get(i) == (account_name, mailbox_name)]
        if mine:
            page = index.messages([box.id], **filters, ids=mine)
            lines += [_record_line(m, mailbox_name, account_name) for m in page]
    return _parse_search_records(clean_script_output(RECORD_SEP.join(lines))), incomplete, body_texts


THREAD_RULE = "\u2501" * 40


def _email_thread_from_index(account, mailbox, keyword, max_messages, nonce) -> str:
    """get_email_thread's report from the index (the script's collection order)."""
    if not keyword:
        raise IndexUnavailable("empty keyword")
    index = envelope_index.get_index()
    uuid = envelope_index.account_uuid(account)
    box = index.find_mailbox(uuid, mailbox)
    if box is None and mailbox == "INBOX":
        box = index.find_mailbox(uuid, "Inbox")
    if box is not None:
        boxes = [box]
    elif mailbox == "All":
        boxes = []
        for _, mailboxes in envelope_index.mailbox_tree(account, with_subs=False):
            for mailbox_name, _ in mailboxes:
                found = index.find_mailbox(uuid, mailbox_name)
                if found is not None:
                    boxes.append(found)
    else:
        raise IndexUnavailable(f"mailbox {mailbox!r} not in the index")

    thread = []
    for box in boxes:
        if len(thread) >= max_messages:
            break
        thread += index.messages([box.id], subject_contains_any=[keyword], limit=max_messages - len(thread))

    dates = envelope_index.date_strings(m.date_received for m in thread)
    out = f"EMAIL THREAD VIEW\n\nThread topic: {keyword}\nAccount: {account}\n\n"
    out += f"{THREAD_RULE}\nFOUND {len(thread)} MESSAGE(S) IN THREAD\n{THREAD_RULE}\n\n"
    for message in thread:
        indicator = "\u2713" if message.read else "\u2709"
        out += f"{indicator} {message.subject}\n"
        out += f"   From: {message.sender}\n"
        out += f"   Date: {dates[message.date_received]}\n"
        out += f"   Preview: \u27e6body:{nonce}:{message.id}|{account}|{mailbox}\u27e7\n"
        out += "\n"
    return fill_body_tokens(clean_script_output(out), 150, nonce, missing=None)
