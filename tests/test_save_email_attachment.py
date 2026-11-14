"""Tests for save_email_attachment's mailbox parameter."""

import os
import unittest
from unittest.mock import patch
from pathlib import Path

import pytest

from apple_mail_mcp.tools import manage as manage_tools


def _script_for(**kwargs):
    save_path = os.path.join(os.path.expanduser("~"), "Downloads", "invoice.pdf")
    with patch(
        "apple_mail_mcp.tools.manage.run_applescript", return_value="OK"
    ) as mock_run:
        manage_tools.save_email_attachment(
            account="Work",
            subject_keyword="Invoice",
            attachment_name="invoice.pdf",
            save_path=save_path,
            **kwargs,
        )
    mock_run.assert_called_once()
    return mock_run.call_args[0][0]


class SaveEmailAttachmentMailboxTests(unittest.TestCase):
    def test_defaults_to_the_inbox(self):
        script = _script_for()
        # INBOX goes through the localized-name fallback, as before.
        self.assertIn("set targetMailbox to missing value", script)
        self.assertIn("repeat with __mailboxLookupName in", script)

    def test_searches_the_requested_mailbox(self):
        script = _script_for(mailbox="Archive")
        self.assertIn('set targetMailbox to mailbox "Archive" of targetAccount', script)
        self.assertIn("Searched mailbox: Archive", script)

    def test_resolves_nested_mailboxes(self):
        script = _script_for(mailbox="Projects/2026")
        self.assertIn(
            'set targetMailbox to mailbox "2026" of mailbox "Projects" of targetAccount',
            script,
        )


def test_subject_script_snapshot():
    # This fork only saves under the home folder, so the snapshot uses ~/Downloads.
    save_path = os.path.join(os.path.expanduser("~"), "Downloads", "invoice.pdf")
    with patch.object(manage_tools, "run_applescript", return_value="OK") as run:
        manage_tools.save_email_attachment("Work", "Invoice", "invoice.pdf", save_path, "Archive")
    expected = (
        Path(__file__).parent / "fixtures/save_attachments_subject.applescript"
    ).read_text().replace("{SAVE_PATH}", os.path.realpath(save_path))
    assert run.call_args.args[0] == expected


def test_save_by_id_copies_from_disk_without_applescript():
    # By ID this fork copies the file Mail already stored (any mailbox), so
    # account, subject and mailbox are ignored and no subject script runs.
    save_path = os.path.join(os.path.expanduser("~"), "Downloads", 'inv"oice.pdf')
    with patch.object(manage_tools, "run_applescript") as run, \
            patch.object(manage_tools, "_save_attachment_by_id", return_value="saved") as by_id:
        result = manage_tools.save_email_attachment(
            "Work", subject_keyword="IGNORED", attachment_name='inv"oice.pdf',
            save_path=save_path, mailbox="Projects/2026", message_id=" 777 ")
    assert result == "saved"
    run.assert_not_called()
    by_id.assert_called_once_with(777, None, 'inv"oice.pdf', os.path.realpath(save_path), False)


@pytest.mark.parametrize("kwargs", [
    dict(message_id="7x"), dict(message_id=""), dict(message_id="７７７"),
    {}, dict(message_id="777", save_path="~/.zshrc"),
    dict(message_id="777", attachment_name=""),
    dict(message_id="777", save_path=""),
])
def test_invalid_save_does_not_run_applescript(kwargs):
    params = dict(account="Work", attachment_name="invoice.pdf",
                  save_path="/tmp/invoice.pdf")
    params.update(kwargs)
    with patch.object(manage_tools, "run_applescript") as run:
        assert manage_tools.save_email_attachment(**params).startswith("Error:")
    run.assert_not_called()
