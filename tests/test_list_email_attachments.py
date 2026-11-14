"""Direct message-id attachment listing and legacy script regression tests."""

from pathlib import Path
from unittest.mock import patch

import pytest

from apple_mail_mcp.tools import analytics


def test_subject_script_snapshot():
    with patch.object(analytics, "run_applescript", return_value="OK") as run:
        analytics.list_email_attachments("Work", "Invoice", 2)
    assert run.call_args.args[0] == (
        Path(__file__).parent / "fixtures/list_attachments_subject.applescript"
    ).read_text()


@pytest.mark.parametrize("kwargs", [{}, dict(message_id=""), dict(message_id="7x"),
                                    dict(message_id="７７７")])
def test_invalid_lookup_does_not_run_applescript(kwargs):
    with patch.object(analytics, "run_applescript") as run:
        assert analytics.list_email_attachments("Work", **kwargs).startswith("Error:")
    run.assert_not_called()


def test_list_by_id_reads_disk_and_never_runs_applescript():
    # This fork lists by ID from Mail's message file on disk, in any mailbox:
    # account, subject and mailbox are ignored and Mail is never asked.
    from apple_mail_mcp import mail_attachments as ma

    parts = [
        ma.AttachmentPart("2", "invoice.pdf", "application/pdf", "attachment", None, 2048, "file"),
        ma.AttachmentPart("3", "logo.png", "image/png", "inline", "cid1", None, "missing"),
    ]
    with patch.object(analytics, "run_applescript") as run, \
            patch.object(ma, "list_parts", return_value=parts) as list_parts:
        out = analytics.list_email_attachments("Work", "IGNORED", message_id=" 777 ", mailbox="Archive")
    run.assert_not_called()
    list_parts.assert_called_once_with(777)
    assert out.splitlines()[:4] == [
        "ATTACHMENTS OF MESSAGE 777",
        "",
        "📎 part 2: invoice.pdf (application/pdf, 2 KB)",
        "📎 part 3: logo.png (inline picture, size unknown) — not downloaded yet",
    ]
    assert "FOUND: 2 part(s)" in out


def test_list_by_id_reports_unreadable_message():
    from apple_mail_mcp import mail_attachments as ma

    with patch.object(analytics, "run_applescript") as run, \
            patch.object(ma, "list_parts", side_effect=ma.AttachmentError("message 777 not found")):
        assert analytics.list_email_attachments(message_id=777) == "Error: message 777 not found"
    run.assert_not_called()
