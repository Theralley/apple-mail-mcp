"""Mark messages read or unread by their message IDs.

The only change to received mail allowed under --read-only: it touches the
read flag of the messages named by ID and nothing else (no flags, moves or
deletes, no filter-based bulk changes). Mail syncs the flag to the server
as if the message had been opened or marked in Mail.
"""

import re
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Union

from apple_mail_mcp import envelope_index as ei
from apple_mail_mcp import server
from apple_mail_mcp.core import (
    AS_FIELD_SEP,
    AS_RECORD_SEP,
    FIELD_SEP,
    RECORD_SEP,
    build_mailbox_ref,
    escape_applescript,
    inject_preferences,
    run_applescript,
)
from apple_mail_mcp.server import mcp

MAX_IDS = 50
CONFIRM_WAIT_S = 8.0
POLL_S = 0.25


def parse_ids(value) -> List[int]:
    """Unique message IDs, in order, from a list or a comma-separated string."""
    items = value if isinstance(value, (list, tuple)) else str(value if value is not None else "").split(",")
    ids: List[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError(f"message IDs are numbers, got {text!r}")
        number = int(text)
        if number not in ids:
            ids.append(number)
    if not ids:
        raise ValueError("give one or more message IDs (from search_mail_index or a listing tool)")
    if len(ids) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} message IDs per call, got {len(ids)}")
    return ids


def mark_script(account: str, mailbox: str, ids: List[int], read: bool) -> str:
    """Set the read flag of *ids*, each addressed by its unique Mail id.

    ``message id N`` is resolved by id (an exact, cheap lookup); a
    ``whose id is N`` query would make Mail scan the whole mailbox on its
    main thread. The id is checked once more before the flag is set.
    """
    value = "true" if read else "false"
    id_list = ", ".join(str(int(i)) for i in ids)
    return f'''
    tell application "Mail"
        set outLines to {{}}
        try
            set targetAccount to account "{escape_applescript(account)}"
            {build_mailbox_ref(mailbox, account_var="targetAccount", var_name="targetMailbox")}
        on error errMsg
            return "MAILBOX" & {AS_FIELD_SEP} & errMsg
        end try
        repeat with wantedId in {{{id_list}}}
            set wanted to contents of wantedId
            try
                set theMessage to «class mssg» id wanted of targetMailbox
                if (id of theMessage) is not wanted then error "Mail returned another message"
                set read status of theMessage to {value}
                set end of outLines to (wanted as string) & {AS_FIELD_SEP} & "ok"
            on error errMsg
                set end of outLines to (wanted as string) & {AS_FIELD_SEP} & errMsg
            end try
        end repeat
        set AppleScript's text item delimiters to {AS_RECORD_SEP}
        return outLines as string
    end tell
    '''


def _parse(output: str) -> Dict[int, str]:
    results: Dict[int, str] = {}
    for record in (output or "").split(RECORD_SEP):
        key, sep, text = record.strip().partition(FIELD_SEP)
        if sep and key.strip().isdigit():
            results[int(key)] = text.strip()
    return results


def _current_state(index, ids: List[int]) -> Dict[int, bool]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = index.query(f"SELECT ROWID, read FROM messages WHERE deleted = 0 AND ROWID IN ({marks})", ids)
    return {rowid: bool(read) for rowid, read in rows}


@mcp.tool()
@inject_preferences
def set_email_read_status(
    message_ids: Union[int, str, List[Union[int, str]]],
    read: bool = True,
) -> str:
    """
    Mark emails as read (default) or unread, by message ID.

    For example after the task in a message is done: find unread mail with
    search_mail_index(read_status="unread"), handle each message, then mark
    it read here. Only the read flag changes; Mail syncs it to the server
    like a click in Mail. Reading a message through this server never marks
    it read by itself.

    Args:
        message_ids: One or more message IDs (a list, or "123,456"); at most 50
        read: True marks read (default), False marks unread

    Returns:
        What changed per message, confirmed from Mail's database
    """
    blocked = server.read_only_block("set_email_read_status")
    if blocked:
        return blocked
    try:
        ids = parse_ids(message_ids)
    except ValueError as exc:
        return f"Error: {exc}"
    word = "read" if read else "unread"
    try:
        index = ei.get_index()
        found = {m.id: m for m in index.messages_by_id(ids)}
        boxes = {box.id: box for box in index.mailboxes()}
        accounts = {uuid: name for name, uuid in ei.mail_accounts()}
    except ei.IndexUnavailable as exc:
        return f"Error: Mail's database cannot be read ({exc}); nothing was changed."

    outcome: Dict[int, str] = {}
    groups: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for message_id in ids:
        message = found.get(message_id)
        if message is None:
            outcome[message_id] = "✗ not found in Mail (deleted, or a wrong ID)"
        elif message.read == read:
            outcome[message_id] = f"= was already {word}"
        else:
            box = boxes.get(message.mailbox_id)
            account = accounts.get(box.account_uuid) if box else None
            if box is None or account is None:
                outcome[message_id] = "✗ in a mailbox outside Mail's accounts (On My Mac?); not changed"
            else:
                groups[(account, box.path)].append(message_id)

    for (account, path), group in groups.items():
        try:
            output = run_applescript(mark_script(account, path, group, read), timeout=60)
        except Exception as exc:  # noqa: BLE001 - reported per message
            output = ""
            failure = str(exc)
        else:
            failure = ""
        if output.startswith("MAILBOX" + FIELD_SEP):
            failure = output.partition(FIELD_SEP)[2]
        results = _parse(output)
        for message_id in group:
            text = results.get(message_id) or failure or "Mail gave no answer"
            outcome[message_id] = "pending" if text == "ok" else f"✗ Mail refused: {text[:200]}"

    pending = [i for i, text in outcome.items() if text == "pending"]
    deadline = time.monotonic() + CONFIRM_WAIT_S
    state: Dict[int, bool] = {}
    while pending:
        try:
            state = _current_state(index, pending)
        except ei.IndexUnavailable:
            state = {}
        if all(state.get(i) == read for i in pending) or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)
    changed = 0
    for message_id in pending:
        if state.get(message_id) == read:
            outcome[message_id] = f"✓ now {word}"
            changed += 1
        else:
            outcome[message_id] = f"? Mail accepted it, but its database does not show {word} yet; check again shortly"

    lines = [f"MARK AS {word.upper()}: {changed} of {len(ids)} changed", ""]
    for message_id in ids:
        message = found.get(message_id)
        lines.append(f"{outcome[message_id]} · ID {message_id}")
        if message is not None:
            lines.append(f"   {message.subject or '(no subject)'} · From: {message.sender}")
    return "\n".join(lines)
