"""Turn a built message into a real Mail draft without the shared-content wrapper.

Measured on macOS 27 (2026-11-14):

* any body or attachment a script puts into an ``outgoing message`` is stored
  as shared content (``URLShareWrapperClass`` blockquote) and is sent with
  "> " in front of every line;
* an outgoing message made with ``visible:false`` can never be closed by
  script and keeps re-saving its draft until Mail quits;
* an ``.eml`` file opens as a read-only viewer, and Message > Send Again on that
  viewer opens an ordinary compose window holding exactly that message
  (body, attachments, sender). Saving that window stores a clean draft.

So a draft is made like this, as one transaction under the Mail lock:
open the ``.eml`` in the background, bring Mail to the front, press Send Again
(only after checking the item really is Send Again and the focused window is
our viewer: in a compose window the same item is Send), press File > Save on
the new compose window (after checking it is ours), close both windows and
give the front back to the app that had it. Accessibility presses only; no
keystrokes are typed, so nothing can land in another app. The stored draft is
then read back from disk and checked before success is reported.
"""

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from apple_mail_mcp import emlx
from apple_mail_mcp.app_lock import AppBusyError, app_lock
from apple_mail_mcp.core import _run_applescript_unlocked, escape_applescript

DRAFT_DIR = Path(os.environ.get(
    "APPLE_MAIL_MCP_DRAFT_DIR", Path.home() / "Library" / "Caches" / "apple-mail-mcp" / "drafts"
))
SEND_AGAIN_ID = "Mail.menuBar.messageMenu.send"
SAVE_ID = "Mail.menuBar.fileMenu.save"
WRAPPER_MARK = "Apple-Mail-URLShare"
DRAFT_BOX_NAMES = ("drafts", "utkast", "entwürfe", "brouillons", "borradores", "bozze", "concepten", "kladder")
VERIFY_WAIT_S = 30
REID_SETTLE_S = 2  # Exchange may store a fresh draft again under a new id
POLL_S = 1
USER_PAUSE_S = 1.5
USER_PAUSE_MAX_WAIT_S = 10


@dataclass
class DraftResult:
    ok: bool
    lines: List[str] = field(default_factory=list)
    draft_id: Optional[int] = None
    eml_path: Optional[Path] = None


def save_eml(msg: EmailMessage) -> Path:
    """Write *msg* to a new private file (folder 0700, file 0600)."""
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DRAFT_DIR, 0o700)
    fd, name = tempfile.mkstemp(prefix="draft-", suffix=".eml", dir=DRAFT_DIR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(bytes(msg))
    os.chmod(name, 0o600)
    return Path(name)


def gui_script(subject: str, eml_path: Path) -> str:
    """AppleScript for the transaction. Returns "OK ..." or "ABORT: <reason>"."""
    subj = escape_applescript(subject)
    path = escape_applescript(str(eml_path))
    return f'''
set subj to "{subj}"
set failure to ""
set notes to ""
set opened to false
set composerOpen to false
set prevApp to ""
tell application "System Events"
    try
        set prevApp to name of first application process whose frontmost is true
    end try
end tell
try
    -- 1. No window may already carry this title (it would be ambiguous).
    tell application "System Events"
        if exists process "Mail" then
            tell process "Mail"
                if (count of (every window whose name is subj)) > 0 then error "a Mail window titled like this draft is already open; close it, or use another subject"
            end tell
        end if
    end tell

    -- 2. Open the message in the background: Mail shows a read-only viewer.
    set opened to true
    do shell script "open -g -a Mail " & quoted form of "{path}"
    tell application "System Events"
        set viewer to missing value
        repeat 150 times
            if exists process "Mail" then
                tell process "Mail"
                    repeat with w in (every window)
                        try
                            if (name of w is subj) and ((count of text fields of w) is 0) then
                                set viewer to contents of w
                                exit repeat
                            end if
                        end try
                    end repeat
                end tell
            end if
            if viewer is not missing value then exit repeat
            delay 0.1
        end repeat
        if viewer is missing value then error "Mail did not open the message"

        tell process "Mail"
            -- 3. Mail to the front (menu items are disabled while it is in the back).
            set waited to 0
            repeat until frontmost or waited > 40
                set frontmost to true
                delay 0.1
                set waited to waited + 1
            end repeat
            if not frontmost then error "Mail could not be brought to the front"
            perform action "AXRaise" of viewer
            delay 0.4

            -- 4. Send Again, only on our viewer.
            set sendAgain to missing value
            repeat with mi in (menu items of menu "Message" of menu bar 1)
                try
                    if (value of attribute "AXIdentifier" of mi) is "{SEND_AGAIN_ID}" then
                        set sendAgain to contents of mi
                        exit repeat
                    end if
                end try
            end repeat
            if sendAgain is missing value then error "Mail has no Send Again menu item"
            if (name of sendAgain) is not "Send Again" then error "the menu item reads " & (name of sendAgain) & ", not Send Again (Mail's menus must be in English)"
            if not (enabled of sendAgain) then error "Send Again is disabled"
            set fw to value of attribute "AXFocusedWindow"
            if not ((name of fw is subj) and ((count of text fields of fw) is 0)) then error "another window took the focus"
            -- This item is Send whenever a compose window is the key window.
            -- So nothing is pressed while any compose window is open at all:
            -- then no window exists whose message the item could send.
            repeat with w in (every window)
                try
                    if (count of text fields of w) > 0 then error "a compose window is open in Mail (" & (name of w) & "); save or close it first" number 9001
                on error errMsg number errNum
                    if errNum is 9001 then error errMsg
                end try
            end repeat
            -- One System Events command looks the item up by its title and
            -- presses it, so there is no gap between the check and the press
            -- (an item titled Send would not be found and nothing is pressed).
            click menu item "Send Again" of menu "Message" of menu bar 1

            -- 5. The new compose window.
            set composer to missing value
            repeat 200 times
                repeat with w in (every window)
                    try
                        if (name of w is subj) and ((count of text fields of w) > 0) then
                            set composer to contents of w
                            exit repeat
                        end if
                    end try
                end repeat
                if composer is not missing value then exit repeat
                delay 0.1
            end repeat
            if composer is missing value then error "no compose window appeared after Send Again"
            set composerOpen to true
            perform action "AXRaise" of composer
            delay 0.4

            -- 6. File > Save, only on our compose window.
            set saveItem to missing value
            repeat with mi in (menu items of menu "File" of menu bar 1)
                try
                    if (value of attribute "AXIdentifier" of mi) is "{SAVE_ID}" then
                        set saveItem to contents of mi
                        exit repeat
                    end if
                end try
            end repeat
            if saveItem is missing value then error "Mail has no File > Save menu item"
            -- Mail's menus work only while Mail is in front; it may lose the
            -- front for a moment (a click elsewhere), so wait for it a little.
            set ready to false
            repeat 50 times
                if not frontmost then set frontmost to true
                if frontmost and (enabled of saveItem) then
                    set ready to true
                    exit repeat
                end if
                delay 0.1
            end repeat
            if not ready then error "Mail lost the front before saving (was another app clicked?)"
            set fw to value of attribute "AXFocusedWindow"
            if not ((name of fw is subj) and ((count of text fields of fw) > 0)) then error "another window took the focus before saving"
            click saveItem
            delay 1.5

            -- 7. Close the compose window; a sheet means it was not saved.
            click (first button of composer whose subrole is "AXCloseButton")
            delay 1
            set stillOpen to false
            repeat with w in (every window)
                try
                    if (name of w is subj) and ((count of text fields of w) > 0) then set stillOpen to true
                end try
            end repeat
            if stillOpen then error "the compose window did not close (unsaved changes?)"
            set composerOpen to false
            set notes to notes & "saved and closed"
        end tell
    end tell
on error errMsg
    set failure to errMsg
end try

-- Always: close the viewer this run opened (step 1 made sure no window had
-- this title before, so a viewer with it is ours) and give the front back.
if opened then
    try
        tell application "System Events"
            tell process "Mail"
                repeat with w in (every window)
                    try
                        if (name of w is subj) and ((count of text fields of w) is 0) then click (first button of w whose subrole is "AXCloseButton")
                    end try
                end repeat
            end tell
        end tell
    end try
end if
if prevApp is not "" and prevApp is not "Mail" then
    try
        tell application "System Events" to set frontmost of process prevApp to true
    end try
end if
if failure is not "" and composerOpen then set failure to failure & ". The compose window with the message is left open in Mail: save it (Command-S) or close it"
if failure is not "" then return "ABORT: " & failure
return "OK " & notes
'''


# ---------------------------------------------------------------------------
# Verification (reads Mail's index and files only)
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _box_kind(path: str) -> Optional[str]:
    last = path.rsplit("/", 1)[-1].casefold()
    if last in DRAFT_BOX_NAMES:
        return "drafts"
    if "sent" in last or "skickat" in last or "skickade" in last or last == "outbox" or last == "utkorg":
        return "sent"
    return None


def _snapshot_max_rowid() -> int:
    from apple_mail_mcp import envelope_index as ei

    return int(ei.get_index().query("SELECT coalesce(max(ROWID), 0) FROM messages")[0][0])


def _new_messages(account_uuid: str, subject: str, after_rowid: int) -> List[Tuple[int, str]]:
    """[(rowid, box kind)] of messages newer than *after_rowid* with this subject in this account.

    A Gmail message lives in All Mail and carries its Drafts/Sent role as a
    label, so labels count as well as the mailbox itself.
    """
    from apple_mail_mcp import envelope_index as ei

    index = ei.get_index()
    boxes = {m.id: m for m in index.mailboxes() if m.account_uuid == account_uuid}
    rows = index.query(
        "SELECT m.ROWID, m.mailbox, coalesce(m.subject_prefix, '') || coalesce(s.subject, '') "
        "FROM messages m LEFT JOIN subjects s ON s.ROWID = m.subject "
        "WHERE m.ROWID > ? AND m.deleted = 0",
        (after_rowid,),
    )
    candidates = {rowid: mailbox_id for rowid, mailbox_id, subj in rows
                  if mailbox_id in boxes and _norm(subj) == _norm(subject)}
    labels: dict = {}
    if candidates:
        marks = ",".join("?" * len(candidates))
        for message_id, mailbox_id in index.query(
            f"SELECT message_id, mailbox_id FROM labels WHERE message_id IN ({marks})", list(candidates)
        ):
            labels.setdefault(message_id, []).append(mailbox_id)
    found = []
    for rowid, mailbox_id in candidates.items():
        kinds = {_box_kind(boxes[b].path) for b in [mailbox_id, *labels.get(rowid, [])] if b in boxes}
        kind = "sent" if "sent" in kinds else "drafts" if "drafts" in kinds else "other"
        found.append((rowid, kind))
    return found


def verify_draft(
    account_uuid: str,
    subject: str,
    after_rowid: int,
    plain_body: str,
    attachments: Sequence[Tuple[str, str]],
    sender: str,
    wait_s: float = VERIFY_WAIT_S,
) -> DraftResult:
    """Find the stored draft and check it: no wrapper, body, attachments, sender."""
    from apple_mail_mcp import mail_attachments as ma

    deadline = time.monotonic() + wait_s
    draft_id = None
    sent_alarm = []
    while time.monotonic() < deadline:
        found = _new_messages(account_uuid, subject, after_rowid)
        sent_alarm = [rowid for rowid, kind in found if kind == "sent"]
        drafts = sorted(rowid for rowid, kind in found if kind == "drafts")
        if drafts and emlx.find_message_file(drafts[-1]) is not None:
            draft_id = drafts[-1]
            break
        time.sleep(POLL_S)
    if sent_alarm:
        return DraftResult(False, [f"⚠️ A message with this subject appeared in Sent/Outbox (id {sent_alarm}). Check Mail now."])
    if draft_id is None:
        return DraftResult(False, [f"The draft did not show up in Drafts within {int(wait_s)} s."])

    # Exchange may store the draft again under a new id right after the
    # upload; look once more, and judge only what is there now.
    time.sleep(REID_SETTLE_S)
    found = _new_messages(account_uuid, subject, after_rowid)
    sent_alarm = [rowid for rowid, kind in found if kind == "sent"]
    if sent_alarm:
        return DraftResult(False, [f"⚠️ A message with this subject appeared in Sent/Outbox (id {sent_alarm}). Check Mail now."])
    current = sorted(rowid for rowid, kind in found if kind == "drafts" and emlx.find_message_file(rowid) is not None)
    if not current:
        return DraftResult(False, [f"The draft (id {draft_id}) was in Drafts but is gone now. Check Mail."])
    draft_id = current[-1]

    problems = []
    try:
        stored = emlx.parse_emlx(emlx.find_message_file(draft_id).read_bytes())
    except (OSError, ValueError, AttributeError) as exc:
        return DraftResult(False, [f"Cannot read the stored draft {draft_id}: {exc}"], draft_id)
    html = "".join(emlx._part_text(p) for p in emlx._text_parts(stored, "html"))
    plain = "".join(emlx._part_text(p) for p in emlx._text_parts(stored, "plain"))
    if WRAPPER_MARK in html:
        problems.append("the stored draft has Mail's quote wrapper")
    stored_text = _norm(emlx.html_to_text(html) if html else plain)
    expected = _norm(plain_body)
    if expected and expected not in stored_text:
        problems.append("the stored body differs from the text given (was something typed into Mail meanwhile?)")
    stored_senders = [addr.casefold() for _, addr in getaddresses([str(stored.get("From", ""))]) if addr]
    if sender and stored_senders != [sender.strip().casefold()]:
        problems.append(f"the draft's sender is {stored.get('From')!r}, not {sender!r}")
    if attachments:
        try:
            parts = ma.list_parts(draft_id)
            have = set()
            for item in parts:
                try:
                    have.add((item.filename, hashlib.sha256(ma.part_bytes(draft_id, item, allow_mail=False)).hexdigest()))
                except ma.AttachmentError:
                    pass
            missing = [name for name, digest in attachments if (name, digest) not in have]
            if missing:
                problems.append("attachments missing or changed: " + ", ".join(missing))
        except ma.AttachmentError as exc:
            problems.append(f"cannot check attachments: {exc}")
    if problems:
        return DraftResult(False, ["Draft saved but failed the check: " + "; ".join(problems) + "."], draft_id)
    return DraftResult(True, ["Checked: no quote wrapper, body, sender" + (", attachments" if attachments else "") + " match."], draft_id)


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------


def _user_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard or mouse input (IOKit HIDIdleTime)."""
    import subprocess

    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True,
                             timeout=5, check=False).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def _wait_for_user_pause(quiet_s: float = USER_PAUSE_S, max_wait_s: float = USER_PAUSE_MAX_WAIT_S) -> None:
    """Mail takes the front for a few seconds; start when the user pauses
    typing or clicking, so Mail does not have to fight them for it."""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        idle = _user_idle_seconds()
        if idle is None or idle >= quiet_s:
            return
        time.sleep(min(0.5, quiet_s - idle + 0.05))


def _front_app() -> str:
    try:
        return _run_applescript_unlocked(
            'tell application "System Events" to return name of first application process whose frontmost is true',
            timeout=10,
        ).strip()
    except Exception:
        return ""


def _restore_front_app(name: str) -> None:
    if not name or name == "Mail":
        return
    try:
        _run_applescript_unlocked(
            f'tell application "System Events" to set frontmost of process "{escape_applescript(name)}" to true',
            timeout=10,
        )
    except Exception:
        pass


def create_in_mail(
    msg: EmailMessage,
    *,
    account_uuid: str,
    subject: str,
    plain_body: str,
    attachments: Sequence[Tuple[str, str]],
    sender: str,
) -> DraftResult:
    """Make *msg* a draft in Mail and verify it. Never sends; never uses the wrapped path."""
    eml_path = save_eml(msg)
    try:
        after_rowid = _snapshot_max_rowid()
    except Exception as exc:
        return DraftResult(False, [f"Cannot read Mail's index to verify the draft ({exc}); nothing was created."], eml_path=eml_path)
    _wait_for_user_pause()
    try:
        with app_lock("Mail", wait_s=60):
            front = _front_app()
            try:
                outcome = _run_applescript_unlocked(gui_script(subject, eml_path), timeout=90).strip()
            except Exception as exc:
                # The script restores the front app itself, unless it was
                # killed (timeout) before getting there.
                outcome = f"ABORT: {exc}"
                _restore_front_app(front)
    except AppBusyError as exc:
        return DraftResult(False, [str(exc)], eml_path=eml_path)
    if not outcome.startswith("OK"):
        reason = outcome[len("ABORT:"):].strip() if outcome.startswith("ABORT:") else outcome
        lines = [f"Mail did not create the draft: {reason.rstrip('.')}."]
        if re.search(r"assistive access|-25211|-1719|not allowed to send keystrokes|accessibility", reason, re.IGNORECASE):
            lines.append(
                "The app running this server needs Accessibility permission "
                "(System Settings > Privacy & Security > Accessibility)."
            )
        return DraftResult(False, lines, eml_path=eml_path)

    result = verify_draft(account_uuid, subject, after_rowid, plain_body, attachments, sender)
    if result.ok:
        try:
            eml_path.unlink()
        except OSError:
            pass
    else:
        result.eml_path = eml_path
    return result
