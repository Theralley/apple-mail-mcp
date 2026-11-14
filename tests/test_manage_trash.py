"""Tests for manage_trash: never send `delete` or `move` to the trash itself.

Mail's AppleScript `delete` does nothing to a message that is already in the
trash, and on an Exchange account a batch of such deletes made Mail abort.
The trash actions therefore only report, and move_to_trash and move_email
refuse a move into the mailbox the messages are already in.
"""

import re
import unittest
from unittest.mock import patch

from apple_mail_mcp.core import FIELD_SEP as F
from apple_mail_mcp.core import RECORD_SEP as R
from apple_mail_mcp.core import clean_script_output
from apple_mail_mcp.tools import manage as manage_tools

# A line of AppleScript that starts with a delete or move command.
_MUTATING_COMMAND = re.compile(r"^\s*(delete|move)\b", re.MULTILINE)


def _reply(*records):
    """Script output the way run_applescript hands it back."""
    return clean_script_output(R.join(F.join(fields) for fields in records))


def _run(return_value="", **kwargs):
    with patch(
        "apple_mail_mcp.tools.manage.run_applescript", return_value=return_value
    ) as mock_run:
        result = manage_tools.manage_trash(account="Work", **kwargs)
    return result, mock_run


class TrashLookupTests(unittest.TestCase):
    def test_finds_the_exchange_trash_too(self):
        script = manage_tools._trash_lookup_script()
        for name in ("Trash", "Deleted Items", "Deleted Messages", "Bin"):
            self.assertIn(f'"{name}"', script)
        self.assertIn('error "no trash mailbox', script)

    def test_trash_names_are_recognised_case_insensitively(self):
        self.assertTrue(manage_tools._is_trash_name("deleted items"))
        self.assertTrue(manage_tools._is_trash_name(" Trash/"))
        self.assertFalse(manage_tools._is_trash_name("INBOX"))
        self.assertFalse(manage_tools._is_trash_name(None))


class EmptyTrashTests(unittest.TestCase):
    def test_sends_no_delete_and_needs_no_confirmation(self):
        result, mock_run = _run(
            return_value=_reply(("TRASH", "Deleted Items", "76")),
            action="empty_trash",
        )
        mock_run.assert_called_once()
        script = mock_run.call_args[0][0]
        self.assertIsNone(_MUTATING_COMMAND.search(script))
        self.assertIn("count of messages of trashMailbox", script)
        self.assertIn("(character id 31)", script)
        self.assertIn("EMPTY TRASH: not done", result)
        self.assertIn("Deleted Items, 76 message(s)", result)
        self.assertIn("Nothing was deleted", result)
        self.assertIn("Erase Deleted Items", result)

    def test_a_script_error_is_passed_through(self):
        result, _ = _run(return_value="Error: no trash mailbox", action="empty_trash")
        self.assertEqual(result, "Error: no trash mailbox")


class DeletePermanentTests(unittest.TestCase):
    def test_lists_matches_without_deleting(self):
        reply = _reply(
            ("TRASH", "Deleted Items", "3"),
            ("101", "[probe] one", "me@example.invalid"),
            ("102", "[probe] two", "me@example.invalid"),
        )
        result, mock_run = _run(
            return_value=reply,
            action="delete_permanent",
            subject_keyword="[probe]",
            max_deletes=2,
            dry_run=False,
        )
        script = mock_run.call_args[0][0]
        self.assertIsNone(_MUTATING_COMMAND.search(script))
        self.assertIn("if shownCount >= 2 then exit repeat", script)
        self.assertIn("(character id 30)", script)
        self.assertIn("DELETE PERMANENTLY: not done", result)
        self.assertIn("3 matching message(s) in Deleted Items", result)
        self.assertIn("ID 101: [probe] one", result)
        self.assertIn("ID 102: [probe] two", result)
        self.assertIn("1 more (max_deletes=2)", result)
        self.assertIn("Nothing was deleted", result)

    def test_tabs_and_line_breaks_in_a_subject_do_not_split_the_record(self):
        reply = _reply(
            ("TRASH", "Deleted Items", "2"),
            ("101", "Quarter\treport", "a@example.invalid"),
            ("102", "Line one\nline two", "b@example.invalid"),
        )
        result, _ = _run(
            return_value=reply,
            action="delete_permanent",
            subject_keyword="report",
            max_deletes=5,
        )
        self.assertIn("ID 101: Quarter report (from a@example.invalid)", result)
        self.assertIn("ID 102: Line one line two (from b@example.invalid)", result)
        self.assertNotIn("more (max_deletes", result)
        self.assertNotIn("(none)", result)

    def test_an_empty_sender_on_the_last_record_is_kept(self):
        reply = _reply(
            ("TRASH", "Deleted Items", "1"),
            ("101", "[probe] draft", ""),
        )
        result, _ = _run(
            return_value=reply,
            action="delete_permanent",
            subject_keyword="[probe]",
        )
        self.assertIn("ID 101: [probe] draft\n", result)
        self.assertNotIn("(from", result)
        self.assertNotIn("(none)", result)

    def test_still_requires_a_filter(self):
        result, mock_run = _run(action="delete_permanent", dry_run=False)
        mock_run.assert_not_called()
        self.assertTrue(result.startswith("Error: No filter provided"))


class MoveToTrashTests(unittest.TestCase):
    def test_refuses_a_source_that_is_the_trash(self):
        for name in ("Deleted Items", "Trash", "bin"):
            result, mock_run = _run(
                action="move_to_trash",
                mailbox=name,
                subject_keyword="[probe]",
                dry_run=False,
            )
            mock_run.assert_not_called()
            self.assertIn("is the trash. Nothing was moved", result)

    def test_moves_into_the_looked_up_trash(self):
        _, mock_run = _run(
            action="move_to_trash", subject_keyword="Invoice", dry_run=False
        )
        script = mock_run.call_args[0][0]
        self.assertIn('repeat with trashName in {"Trash", "Deleted Items"', script)
        self.assertIn("move aMessage to trashMailbox", script)
        self.assertNotIn('mailbox "Trash" of targetAccount', script)


class MoveEmailSameMailboxTests(unittest.TestCase):
    def test_a_move_into_the_source_mailbox_is_refused(self):
        with patch("apple_mail_mcp.tools.manage.run_applescript") as mock_run:
            for src, dst in (("INBOX", "Inbox"), ("Deleted Items", "deleted items/")):
                result = manage_tools.move_email(
                    account="Work",
                    to_mailbox=dst,
                    from_mailbox=src,
                    message_ids=[1],
                    dry_run=False,
                )
                self.assertIn("are the same mailbox", result)
        mock_run.assert_not_called()


class InvalidActionTests(unittest.TestCase):
    def test_an_unknown_action_is_rejected_before_any_script_runs(self):
        # An unrecognised action used to fall through to the move_to_trash
        # branch, so action="list" with dry_run=False moved matches to Trash.
        with patch("apple_mail_mcp.tools.manage.run_applescript") as mock_run:
            result = manage_tools.manage_trash(
                account="Work",
                action="list",
                subject_keyword="Invoice",
                dry_run=False,
            )
        mock_run.assert_not_called()
        self.assertTrue(result.startswith("Error: Invalid action 'list'"))


if __name__ == "__main__":
    unittest.main()
