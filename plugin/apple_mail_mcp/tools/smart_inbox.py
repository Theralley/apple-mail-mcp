"""Smart inbox tools: follow-up tracking, actionable email detection, and sender analytics."""

from typing import Optional

from apple_mail_mcp.server import mcp
from apple_mail_mcp import envelope_index
from apple_mail_mcp.envelope_index import IndexUnavailable, contains_ci, fold
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    AS_RECORD_SEP,
    FIELD_SEP,
    RECORD_SEP,
    clean_script_output,
    inject_preferences,
    escape_applescript,
    read_flag_index_script,
    run_applescript,
    inbox_mailbox_script,
    date_cutoff_script,
    LOWERCASE_HANDLER,
)
from apple_mail_mcp.emlx import get_message_body
from apple_mail_mcp.constants import (
    NEWSLETTER_PLATFORM_PATTERNS,
    NEWSLETTER_KEYWORD_PATTERNS,
    THREAD_PREFIXES,
    FLAG_COLOR_NAMES,
    SENT_MAILBOX_NAMES,
)

# AppleScript list literal of color names indexed by flag index, e.g.
# {"red", "orange", ...} — item (flagIndex + 1) of this list is the name.
_FLAG_COLOR_NAME_LIST = ", ".join(
    f'"{FLAG_COLOR_NAMES[i]}"' for i in sorted(FLAG_COLOR_NAMES)
)


def _strip_subject_prefixes_script() -> str:
    """Return AppleScript handler to strip Re:/Fwd:/etc prefixes from a subject."""
    # Build a list of prefixes to strip
    prefix_checks = ""
    for prefix in THREAD_PREFIXES:
        escaped = escape_applescript(prefix)
        prefix_checks += f'''
                if baseSubj starts with "{escaped}" then
                    set baseSubj to text {len(prefix) + 1} thru -1 of baseSubj
                    -- trim leading space
                    repeat while baseSubj starts with " "
                        set baseSubj to text 2 thru -1 of baseSubj
                    end repeat
                    set didStrip to true
                end if
'''
    return f'''
    on stripPrefixes(subj)
        set baseSubj to subj
        set didStrip to true
        repeat while didStrip
            set didStrip to false
            {prefix_checks}
        end repeat
        return baseSubj
    end stripPrefixes
'''


def _sent_mailbox_script() -> str:
    """AppleScript setting sentMailbox to the first of SENT_MAILBOX_NAMES (or missing value)."""
    names = ", ".join(f'"{escape_applescript(n)}"' for n in SENT_MAILBOX_NAMES)
    return f"""set sentMailbox to missing value
            repeat with sentName in {{{names}}}
                try
                    set sentMailbox to mailbox (sentName as string) of targetAccount
                    exit repeat
                end try
            end repeat"""


def _newsletter_filter_condition(sender_var: str = "lowerSender") -> str:
    """Return AppleScript condition that evaluates to true if email is a newsletter."""
    platform_checks = " or ".join(
        f'{sender_var} contains "{escape_applescript(p)}"'
        for p in NEWSLETTER_PLATFORM_PATTERNS
    )
    keyword_checks = " or ".join(
        f'{sender_var} contains "{escape_applescript(k)}"'
        for k in NEWSLETTER_KEYWORD_PATTERNS
    )
    return f"({platform_checks} or {keyword_checks})"


@mcp.tool()
@inject_preferences
def get_awaiting_reply(
    account: str,
    days_back: int = 7,
    exclude_noreply: bool = True,
    max_results: int = 20,
) -> str:
    """Find sent emails that haven't received a reply yet.

    Scans the Sent mailbox for outgoing emails and cross-references with
    the Inbox to see if a reply (matching subject) was received from the
    same recipient. Useful for follow-up tracking.

    Args:
        account: Account name (e.g., "Gmail", "Work", "Personal")
        days_back: How many days back to check sent emails (default: 7)
        exclude_noreply: Skip emails sent to noreply/no-reply addresses (default: True)
        max_results: Maximum results to return (default: 20)

    Returns:
        List of sent emails still awaiting a reply with subject, recipient, and date sent
    """
    try:
        return _awaiting_reply_from_index(account, days_back, exclude_noreply, max_results)
    except IndexUnavailable:
        pass
    escaped_account = escape_applescript(account)

    noreply_filter = ""
    if exclude_noreply:
        noreply_filter = '''
                            set lowerRecip to my lowercase(recipAddr)
                            if lowerRecip contains "noreply" or lowerRecip contains "no-reply" or lowerRecip contains "do-not-reply" or lowerRecip contains "donotreply" then
                                set skipThis to true
                            end if
'''

    if days_back > 0:
        inbox_fetch = (
            "set {rawSubjects, rawSenders} to {subject, sender} of "
            "(every message of inboxMailbox whose date received > cutoffDate)"
        )
        sent_fetch = (
            "set sentMessages to (every message of sentMailbox "
            "whose date sent > cutoffDate)"
        )
    else:
        inbox_fetch = (
            "set {rawSubjects, rawSenders} to {subject, sender} of "
            "every message of inboxMailbox"
        )
        sent_fetch = "set sentMessages to every message of sentMailbox"

    script = f'''
    tell application "Mail"
        set outputText to "EMAILS AWAITING REPLY" & return
        set outputText to outputText & "Account: {escaped_account} | Last {days_back} days" & return
        set outputText to outputText & "========================================" & return & return

        {date_cutoff_script(days_back, "cutoffDate")}

        try
            set targetAccount to account "{escaped_account}"

            -- Get Sent mailbox
            {_sent_mailbox_script()}
            if sentMailbox is missing value then
                return "Error: Could not find Sent mailbox for account {escaped_account}"
            end if

            -- Get Inbox mailbox
            {inbox_mailbox_script("inboxMailbox", "targetAccount")}

            -- Collect subjects from inbox for matching. A reply cannot predate
            -- the sent message, so only the window is read, and each property
            -- is fetched for all messages in one Apple Event (per-message
            -- reads over a whole inbox timed out on large mailboxes).
            set inboxSubjects to {{}}
            set inboxSenders to {{}}
            {inbox_fetch}

            repeat with i from 1 to count of rawSubjects
                try
                    set lowerBase to my lowercase(my stripPrefixes(item i of rawSubjects))
                    set lowerInboxSender to my lowercase(item i of rawSenders)
                    set end of inboxSubjects to lowerBase
                    set end of inboxSenders to lowerInboxSender
                end try
            end repeat

            -- Now scan sent emails
            {sent_fetch}
            set resultCount to 0
            set checkedCount to 0

            repeat with aMessage in sentMessages
                if resultCount >= {max_results} then exit repeat

                try
                    set messageDate to date sent of aMessage
                    {"if messageDate < cutoffDate then exit repeat" if days_back > 0 else ""}

                    set messageSubject to subject of aMessage
                    set messageRecipients to every to recipient of aMessage

                    if (count of messageRecipients) > 0 then
                        set recipAddr to address of item 1 of messageRecipients
                        set recipName to ""
                        try
                            set recipName to name of item 1 of messageRecipients
                        end try

                        set skipThis to false
                        {noreply_filter}

                        if not skipThis then
                            -- Strip prefixes from sent subject and check inbox
                            set baseSubject to my stripPrefixes(messageSubject)
                            set lowerBase to my lowercase(baseSubject)
                            set lowerRecipAddr to my lowercase(recipAddr)

                            -- Check if there is a reply in inbox from this recipient about this subject
                            set foundReply to false
                            set idx to 1
                            repeat with inboxSubj in inboxSubjects
                                -- contents of: compare the text, not the list-item reference
                                set inboxText to contents of inboxSubj
                                if inboxText contains lowerBase or lowerBase contains inboxText then
                                    set inboxSender to item idx of inboxSenders
                                    if inboxSender contains lowerRecipAddr then
                                        set foundReply to true
                                        exit repeat
                                    end if
                                end if
                                set idx to idx + 1
                            end repeat

                            if not foundReply then
                                set resultCount to resultCount + 1
                                set displayRecip to recipAddr
                                if recipName is not "" then
                                    set displayRecip to recipName & " <" & recipAddr & ">"
                                end if
                                set outputText to outputText & resultCount & ". " & messageSubject & return
                                set outputText to outputText & "   To: " & displayRecip & return
                                set outputText to outputText & "   Sent: " & (messageDate as string) & return & return
                            end if
                        end if
                    end if
                end try
            end repeat

            set outputText to outputText & "========================================" & return
            set outputText to outputText & "Found " & resultCount & " sent email(s) awaiting reply." & return

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell

    {LOWERCASE_HANDLER}
    {_strip_subject_prefixes_script()}
    '''

    return run_applescript(script)


@mcp.tool()
@inject_preferences
def get_needs_response(
    account: str,
    mailbox: str = "INBOX",
    days_back: int = 7,
    max_results: int = 20,
) -> str:
    """Identify unread emails that likely need a response from you.

    Filters out newsletters, automated emails, and noreply senders.
    Prioritises direct emails (To: you) with question marks as likely
    needing a reply.

    Args:
        account: Account name (e.g., "Gmail", "Work", "Personal")
        mailbox: Mailbox to scan (default: "INBOX")
        days_back: How many days back to look (default: 7)
        max_results: Maximum results to return (default: 20)

    Returns:
        Ranked list of emails likely needing a response, with priority hints
    """
    try:
        return _needs_response_from_index(account, mailbox, days_back, max_results)
    except IndexUnavailable:
        pass
    escaped_account = escape_applescript(account)
    escaped_mailbox = escape_applescript(mailbox)

    newsletter_condition = _newsletter_filter_condition("lowerSender")

    if days_back > 0:
        mailbox_fetch = (
            "set mailboxMessages to (every message of targetMailbox whose "
            "read status is false and date received > cutoffDate)"
        )
    else:
        mailbox_fetch = (
            "set mailboxMessages to (every message of targetMailbox whose "
            "read status is false)"
        )

    script = f'''
    tell application "Mail"
        set outputText to "EMAILS NEEDING RESPONSE" & return
        set outputText to outputText & "Account: {escaped_account} | Mailbox: {escaped_mailbox} | Last {days_back} days" & return
        set outputText to outputText & "========================================" & return & return

        {date_cutoff_script(days_back, "cutoffDate")}

        try
            set targetAccount to account "{escaped_account}"

            -- Get target mailbox
            try
                set targetMailbox to mailbox "{escaped_mailbox}" of targetAccount
            on error
                if "{escaped_mailbox}" is "INBOX" then
                    set targetMailbox to mailbox "Inbox" of targetAccount
                else
                    error "Mailbox not found: {escaped_mailbox}"
                end if
            end try

            -- Collect sent subjects for "already replied" detection
            set sentSubjects to {{}}
            {_sent_mailbox_script()}

            if sentMailbox is not missing value then
                -- One Apple Event for all sent subjects, then keep the newest 200
                set rawSentSubjects to subject of every message of sentMailbox
                set sentIdx to 0
                repeat with sentSubj in rawSentSubjects
                    set sentIdx to sentIdx + 1
                    if sentIdx > 200 then exit repeat
                    try
                        set baseSent to my stripPrefixes(sentSubj as string)
                        set end of sentSubjects to my lowercase(baseSent)
                    end try
                end repeat
            end if

            -- Scan target mailbox: let Mail pre-filter to unread (and the date
            -- window) instead of reading every message one by one
            {mailbox_fetch}
            set candidateEntries to {{}}
            set totalChecked to 0
            set flagColorNames to {{{_FLAG_COLOR_NAME_LIST}}}

            repeat with aMessage in mailboxMessages
                if (count of candidateEntries) >= {max_results} then exit repeat

                try
                    set messageDate to date received of aMessage
                    {"if messageDate < cutoffDate then exit repeat" if days_back > 0 else ""}

                    -- Only look at unread emails
                    if not (read status of aMessage) then
                        set messageSender to sender of aMessage
                        set messageSubject to subject of aMessage
                        set lowerSender to my lowercase(messageSender)

                        -- Filter out newsletters and automated senders
                        set isNewsletter to {newsletter_condition}
                        set isAutomated to (lowerSender contains "noreply" or lowerSender contains "no-reply" or lowerSender contains "donotreply" or lowerSender contains "do-not-reply" or lowerSender contains "notifications@" or lowerSender contains "mailer-daemon" or lowerSender contains "postmaster@")

                        if not isNewsletter and not isAutomated then
                            -- Check if user already replied
                            set baseSubject to my stripPrefixes(messageSubject)
                            set lowerBase to my lowercase(baseSubject)
                            set alreadyReplied to false
                            repeat with sentSubj in sentSubjects
                                -- contents of: compare the text, not the list-item reference
                                set sentText to contents of sentSubj
                                if sentText contains lowerBase or lowerBase contains sentText then
                                    set alreadyReplied to true
                                    exit repeat
                                end if
                            end repeat

                            if not alreadyReplied then
                                -- Priority is decided in Python: whether the body asks a
                                -- question is read from disk (emlx.py), never from Mail.
                                {read_flag_index_script("flagIndex")}
                                set flagLabel to ""
                                if flagIndex is not -1 then
                                    set flagLabel to "flagged"
                                    if flagIndex >= 0 and flagIndex < 7 then
                                        set flagLabel to "flagged " & item (flagIndex + 1) of flagColorNames
                                    end if
                                end if

                                set fs to {AS_FIELD_SEP}
                                set end of candidateEntries to "ENTRY" & fs & ((id of aMessage) as string) & fs & messageSubject & fs & messageSender & fs & (messageDate as string) & fs & flagLabel
                            end if
                        end if
                    end if
                end try
            end repeat

            -- One record per candidate, after the header
            set AppleScript's text item delimiters to {AS_RECORD_SEP}
            set outputText to outputText & {AS_RECORD_SEP} & (candidateEntries as string)
            set AppleScript's text item delimiters to ""

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell

    {LOWERCASE_HANDLER}
    {_strip_subject_prefixes_script()}
    '''

    result = run_applescript(script)
    if result.startswith("Error:"):
        return result
    return _format_needs_response(result, account, mailbox)


def _format_needs_response(result: str, account: str, mailbox: str) -> str:
    """Rank the candidates the script found and format the report.

    HIGH: flagged (plus "+ question" when it also asks one); MEDIUM: asks a
    question in the subject or in the first 500 characters of the body;
    NORMAL: the rest. The body comes from disk via emlx.py.
    """
    header = []
    high, normal = [], []
    for line in result.split(RECORD_SEP):
        if not line.startswith("ENTRY" + FIELD_SEP):
            header.append(line)
            continue
        parts = line.split(FIELD_SEP)
        if len(parts) < 5:
            continue
        message_id, subject, sender, date = parts[1], parts[2], parts[3], parts[4]
        flag_label = FIELD_SEP.join(parts[5:]).strip()
        has_question = "?" in subject
        if not has_question:
            body = get_message_body(message_id, account, mailbox)
            has_question = bool(body) and "?" in body[:500]
        if flag_label:
            label = f"HIGH ({flag_label} + question)" if has_question else f"HIGH ({flag_label})"
            high.append((subject, sender, date, label))
        elif has_question:
            high.append((subject, sender, date, "MEDIUM (contains question)"))
        else:
            normal.append((subject, sender, date, "NORMAL"))

    out = "\n".join(header).rstrip("\n") + "\n\n"
    for number, (subject, sender, date, label) in enumerate(high + normal, start=1):
        out += f"{number}. [{label}] {subject}\n   From: {sender}\n   Date: {date}\n\n"
    out += "========================================\n"
    out += f"Found {len(high) + len(normal)} email(s) needing response."
    return out


@mcp.tool()
@inject_preferences
def get_top_senders(
    account: str,
    mailbox: str = "INBOX",
    days_back: int = 30,
    top_n: int = 10,
    group_by_domain: bool = False,
) -> str:
    """Analyse a mailbox to find the most frequent senders.

    Useful for identifying key contacts, high-volume senders to filter,
    or newsletter sources to unsubscribe from.

    Args:
        account: Account name (e.g., "Gmail", "Work", "Personal")
        mailbox: Mailbox to analyse (default: "INBOX")
        days_back: How many days back to look (default: 30, 0 = all time)
        top_n: Number of top senders to return (default: 10)
        group_by_domain: Group results by domain instead of individual sender (default: False)

    Returns:
        Ranked list of senders (or domains) with email counts
    """
    try:
        return _top_senders_from_index(account, mailbox, days_back, top_n, group_by_domain)
    except IndexUnavailable:
        pass
    escaped_account = escape_applescript(account)
    escaped_mailbox = escape_applescript(mailbox)

    date_cutoff = date_cutoff_script(days_back, "cutoffDate")
    if days_back > 0:
        sender_fetch = (
            "set mailboxSenders to sender of (every message of targetMailbox "
            "whose date received > cutoffDate)"
        )
    else:
        sender_fetch = "set mailboxSenders to sender of every message of targetMailbox"

    # Build the extraction key: either full sender or domain
    if group_by_domain:
        # Extract domain from email address
        extract_key = '''
                            -- Extract domain from sender address
                            set senderKey to ""
                            set atPos to 0
                            set senderLen to length of messageSender
                            repeat with i from 1 to senderLen
                                if character i of messageSender is "@" then
                                    set atPos to i
                                end if
                            end repeat
                            if atPos > 0 then
                                -- Find the closing > if present
                                set endPos to senderLen
                                repeat with i from atPos to senderLen
                                    if character i of messageSender is ">" then
                                        set endPos to i - 1
                                        exit repeat
                                    end if
                                end repeat
                                set senderKey to text (atPos + 1) thru endPos of messageSender
                            else
                                set senderKey to messageSender
                            end if
'''
        title_label = "TOP SENDER DOMAINS"
    else:
        extract_key = '''
                            set senderKey to messageSender
'''
        title_label = "TOP SENDERS"

    script = f'''
    tell application "Mail"
        set outputText to "{title_label}" & return
        set outputText to outputText & "Account: {escaped_account} | Mailbox: {escaped_mailbox} | Last {days_back} days" & return
        set outputText to outputText & "========================================" & return & return

        {date_cutoff}

        try
            set targetAccount to account "{escaped_account}"

            -- Get target mailbox
            try
                set targetMailbox to mailbox "{escaped_mailbox}" of targetAccount
            on error
                if "{escaped_mailbox}" is "INBOX" then
                    set targetMailbox to mailbox "Inbox" of targetAccount
                else
                    error "Mailbox not found: {escaped_mailbox}"
                end if
            end try

            -- One Apple Event for every sender in the window
            {sender_fetch}
            set senderKeys to {{}}
            set senderCounts to {{}}
            set totalAnalysed to 0

            repeat with rawSender in mailboxSenders
                try
                    set messageSender to rawSender as string
                    set totalAnalysed to totalAnalysed + 1

                    {extract_key}

                    -- Update count
                    set foundSender to false
                    set idx to 1
                    repeat with existingKey in senderKeys
                        if existingKey as string is senderKey then
                            set item idx of senderCounts to (item idx of senderCounts) + 1
                            set foundSender to true
                            exit repeat
                        end if
                        set idx to idx + 1
                    end repeat
                    if not foundSender then
                        set end of senderKeys to senderKey
                        set end of senderCounts to 1
                    end if
                end try
            end repeat

            -- Sort by count (simple selection sort, we only need top N)
            set topN to {top_n}
            repeat with i from 1 to (count of senderCounts)
                if i > topN then exit repeat
                -- Find max from i to end
                set maxIdx to i
                set maxVal to item i of senderCounts
                repeat with j from (i + 1) to (count of senderCounts)
                    if item j of senderCounts > maxVal then
                        set maxIdx to j
                        set maxVal to item j of senderCounts
                    end if
                end repeat
                -- Swap
                if maxIdx is not i then
                    set tmpCount to item i of senderCounts
                    set item i of senderCounts to item maxIdx of senderCounts
                    set item maxIdx of senderCounts to tmpCount
                    set tmpKey to item i of senderKeys as string
                    set item i of senderKeys to (item maxIdx of senderKeys as string)
                    set item maxIdx of senderKeys to tmpKey
                end if
            end repeat

            -- Format output
            set displayCount to topN
            if (count of senderKeys) < displayCount then
                set displayCount to (count of senderKeys)
            end if

            repeat with i from 1 to displayCount
                set senderKey to item i of senderKeys
                set sCount to item i of senderCounts
                set pctText to ""
                if totalAnalysed > 0 then
                    set pct to round ((sCount / totalAnalysed) * 100)
                    set pctText to " (" & pct & "%)"
                end if
                set outputText to outputText & i & ". " & senderKey & ": " & sCount & " emails" & pctText & return
            end repeat

            set outputText to outputText & return & "========================================" & return
            set outputText to outputText & "Total emails analysed: " & totalAnalysed & return
            set outputText to outputText & "Unique senders: " & (count of senderKeys) & return

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    '''

    return run_applescript(script)


# ---------------------------------------------------------------------------
# The same reports from Mail's Envelope Index (see envelope_index.py). The
# helpers below mirror the scripts' handlers so matching is unchanged.
# ---------------------------------------------------------------------------

_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ"
_LOWER = "abcdefghijklmnopqrstuvwxyzàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþ"
_LOWERCASE_TABLE = str.maketrans(_UPPER, _LOWER)

AUTOMATED_SENDER_PATTERNS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications@", "mailer-daemon", "postmaster@",
)
NOREPLY_RECIPIENT_PATTERNS = ("noreply", "no-reply", "do-not-reply", "donotreply")
RULE = "========================================"


def _lowercase(text: str) -> str:
    """LOWERCASE_HANDLER: only ASCII and Latin-1 capitals are lowered."""
    return text.translate(_LOWERCASE_TABLE)


def _strip_prefixes(subject: str) -> str:
    """The stripPrefixes handler; ValueError where the script errors out.

    AppleScript's ``text n thru -1`` fails when nothing is left, which the
    scripts' ``try`` turns into skipping that message.
    """
    base = subject
    did_strip = True
    while did_strip:
        did_strip = False
        for prefix in THREAD_PREFIXES:
            if fold(base).startswith(fold(prefix)):
                if len(base) <= len(prefix):
                    raise ValueError("nothing after the prefix")
                base = base[len(prefix):]
                while base.startswith(" "):
                    if len(base) == 1:
                        raise ValueError("only a space left")
                    base = base[1:]
                did_strip = True
    return base


def _either_contains(a: str, b: str) -> bool:
    return contains_ci(a, b) or contains_ci(b, a)


def _sent_mailbox(index, uuid: str):
    for name in SENT_MAILBOX_NAMES:
        mailbox = index.find_mailbox(uuid, name)
        if mailbox is not None:
            return mailbox
    return None


def _awaiting_reply_from_index(account, days_back, exclude_noreply, max_results) -> str:
    index = envelope_index.get_index()
    uuid = envelope_index.account_uuid(account)
    # The script's answers when a mailbox is missing (Sent is looked up first)
    sent_box = _sent_mailbox(index, uuid)
    if sent_box is None:
        return f"Error: Could not find Sent mailbox for account {account}"
    inbox = envelope_index.find_inbox(index, uuid)
    if inbox is None:
        canonical = next(name for name, u in envelope_index.mail_accounts() if u == uuid)
        return f"Error: No inbox mailbox found for account {canonical}"
    cutoff = envelope_index.days_back_cutoff(days_back)

    inbox_keys = []
    for message in index.messages([inbox.id], received_after=cutoff):
        try:
            inbox_keys.append((_lowercase(_strip_prefixes(message.subject)), _lowercase(message.sender)))
        except ValueError:
            continue

    sent_messages = index.messages([sent_box.id], sent_after=cutoff)
    recipients = index.to_recipients(m.id for m in sent_messages)
    found = []
    for message in sent_messages:
        if len(found) >= max_results:
            break
        if cutoff is not None and message.date_sent < cutoff:
            break
        to = recipients.get(message.id) or []
        if not to:
            continue
        address, name = to[0]
        if exclude_noreply and any(contains_ci(_lowercase(address), p) for p in NOREPLY_RECIPIENT_PATTERNS):
            continue
        try:
            lower_base = _lowercase(_strip_prefixes(message.subject))
        except ValueError:
            continue
        lower_address = _lowercase(address)
        replied = any(
            _either_contains(subject, lower_base) and contains_ci(sender, lower_address)
            for subject, sender in inbox_keys
        )
        if not replied:
            found.append((message, f"{name} <{address}>" if name else address))

    dates = envelope_index.date_strings(m.date_sent for m, _ in found)
    out = f"EMAILS AWAITING REPLY\nAccount: {account} | Last {days_back} days\n{RULE}\n\n"
    for number, (message, recipient) in enumerate(found, start=1):
        out += f"{number}. {message.subject}\n   To: {recipient}\n   Sent: {dates[message.date_sent]}\n\n"
    out += f"{RULE}\nFound {len(found)} sent email(s) awaiting reply.\n"
    return clean_script_output(out)


def _needs_response_from_index(account, mailbox, days_back, max_results) -> str:
    index = envelope_index.get_index()
    uuid = envelope_index.account_uuid(account)
    target = envelope_index.resolve_mailbox(index, uuid, mailbox)
    cutoff = envelope_index.days_back_cutoff(days_back)

    sent_subjects = []
    sent_box = _sent_mailbox(index, uuid)
    if sent_box is not None:
        for message in index.messages([sent_box.id], limit=200):
            try:
                sent_subjects.append(_lowercase(_strip_prefixes(message.subject)))
            except ValueError:
                continue

    newsletter_patterns = NEWSLETTER_PLATFORM_PATTERNS + NEWSLETTER_KEYWORD_PATTERNS
    candidates = []
    for message in index.messages([target.id], unread_only=True, received_after=cutoff):
        if len(candidates) >= max_results:
            break
        lower_sender = _lowercase(message.sender)
        if any(contains_ci(lower_sender, p) for p in newsletter_patterns):
            continue
        if any(contains_ci(lower_sender, p) for p in AUTOMATED_SENDER_PATTERNS):
            continue
        try:
            lower_base = _lowercase(_strip_prefixes(message.subject))
        except ValueError:
            continue
        if any(_either_contains(sent, lower_base) for sent in sent_subjects):
            continue
        flag_label = ""
        if message.flag_index != -1:
            flag_label = "flagged"
            if 0 <= message.flag_index < 7:
                flag_label = f"flagged {FLAG_COLOR_NAMES[message.flag_index]}"
        candidates.append((message, flag_label))

    dates = envelope_index.date_strings(m.date_received for m, _ in candidates)
    entries = [
        FIELD_SEP.join(["ENTRY", str(m.id), m.subject, m.sender, dates[m.date_received], label])
        for m, label in candidates
    ]
    header = f"EMAILS NEEDING RESPONSE\nAccount: {account} | Mailbox: {mailbox} | Last {days_back} days\n{RULE}\n\n"
    result = clean_script_output(header + RECORD_SEP + RECORD_SEP.join(entries))
    return _format_needs_response(result, account, mailbox)


def _sender_domain(sender: str) -> str:
    at = sender.rfind("@")
    if at == -1:
        return sender
    end = sender.find(">", at)
    return sender[at + 1:end if end != -1 else len(sender)]


def _top_senders_from_index(account, mailbox, days_back, top_n, group_by_domain) -> str:
    index = envelope_index.get_index()
    target = envelope_index.resolve_mailbox(index, envelope_index.account_uuid(account), mailbox)
    messages = index.messages([target.id], received_after=envelope_index.days_back_cutoff(days_back))

    keys: list = []
    counts: list = []
    slot: dict = {}
    for message in messages:
        key = _sender_domain(message.sender) if group_by_domain else message.sender
        if fold(key) in slot:
            counts[slot[fold(key)]] += 1
        else:
            slot[fold(key)] = len(keys)
            keys.append(key)
            counts.append(1)
    total = len(messages)

    # The script's selection sort, so ties come out in the same order
    for i in range(min(len(counts), max(top_n, 0))):
        max_index = i
        for j in range(i + 1, len(counts)):
            if counts[j] > counts[max_index]:
                max_index = j
        if max_index != i:
            counts[i], counts[max_index] = counts[max_index], counts[i]
            keys[i], keys[max_index] = keys[max_index], keys[i]

    title = "TOP SENDER DOMAINS" if group_by_domain else "TOP SENDERS"
    out = f"{title}\nAccount: {account} | Mailbox: {mailbox} | Last {days_back} days\n{RULE}\n\n"
    for i in range(min(top_n, len(keys))):
        pct = f" ({round((counts[i] / total) * 100)}%)" if total > 0 else ""
        out += f"{i + 1}. {keys[i]}: {counts[i]} emails{pct}\n"
    out += f"\n{RULE}\nTotal emails analysed: {total}\nUnique senders: {len(keys)}\n"
    return clean_script_output(out)
