"""Regression tests for the large-mailbox timeout fixes.

Mail answers one Apple Event per property read, and every read costs several
milliseconds (far more while Mail is busy), so scripts that walk a whole
mailbox message-by-message ran into the osascript timeout on inboxes with
thousands of messages. These tests pin the bounded shapes: whose-clause
pre-filters, one bulk property fetch per selection, and no per-message shell
forks.
"""

import unittest
from unittest.mock import patch

from apple_mail_mcp import core
from apple_mail_mcp.tools import analytics as analytics_tools
from apple_mail_mcp.tools import inbox as inbox_tools
from apple_mail_mcp.tools import search as search_tools
from apple_mail_mcp.tools import smart_inbox as smart_inbox_tools


def _capture(module_path):
    captured = {}

    def fake_run(script, timeout=120):
        captured["script"] = script
        captured["timeout"] = timeout
        return ""

    return captured, patch(f"{module_path}.run_applescript", side_effect=fake_run)


class LowercaseHandlerTests(unittest.TestCase):
    def test_lowercase_handler_does_not_fork_a_shell(self):
        self.assertIn("on lowercase(str)", core.LOWERCASE_HANDLER)
        self.assertNotIn("do shell script", core.LOWERCASE_HANDLER)
        self.assertIn("considering case", core.LOWERCASE_HANDLER)


class TimeoutMessageTests(unittest.TestCase):
    def test_timeout_error_is_actionable(self):
        import subprocess
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("osascript", 7)
        with patch.object(core, "_popen_factory", return_value=proc):
            with self.assertRaises(Exception) as ctx:
                core.run_applescript('tell application "Mail" to count accounts', timeout=7)

        message = str(ctx.exception)
        self.assertIn("timed out after 7s", message)
        self.assertIn("Narrow the request", message)
        proc.kill.assert_called_once()


class ListInboxBoundedTests(unittest.TestCase):
    def test_default_is_bounded(self):
        captured, patcher = _capture("apple_mail_mcp.tools.inbox")
        with patcher:
            inbox_tools.list_inbox_emails()

        script = captured["script"]
        self.assertIn("if fetchCount > 20 then set fetchCount to 20", script)
        self.assertIn("repeat with currentIndex from 1 to fetchCount", script)
        # No open-ended per-message walk over the whole inbox.
        self.assertNotIn("repeat with aMessage in inboxMessages", script)

    def test_all_messages_uses_bulk_property_fetch(self):
        captured, patcher = _capture("apple_mail_mcp.tools.inbox")
        with patcher:
            inbox_tools.list_inbox_emails(max_emails=0, output_format="json")

        script = captured["script"]
        self.assertIn(
            "{subject, sender, date received, read status} of every message of inboxMailbox",
            script,
        )
        self.assertIn("set messageSubject to item currentIndex of subjList", script)

    def test_unread_only_uses_whose_filter(self):
        captured, patcher = _capture("apple_mail_mcp.tools.inbox")
        with patcher:
            inbox_tools.list_inbox_emails(include_read=False, output_format="json")

        self.assertIn(
            "(every message of inboxMailbox whose read status is false)",
            captured["script"],
        )

    def test_text_output_honours_account_filter(self):
        captured, patcher = _capture("apple_mail_mcp.tools.inbox")
        with patcher:
            inbox_tools.list_inbox_emails(account="Work")

        self.assertIn('if accountName is "Work" then', captured["script"])


class SmartInboxBoundedTests(unittest.TestCase):
    def test_awaiting_reply_reads_only_the_window_in_bulk(self):
        captured, patcher = _capture("apple_mail_mcp.tools.smart_inbox")
        with patcher:
            smart_inbox_tools.get_awaiting_reply(account="Work", days_back=7)

        script = captured["script"]
        self.assertIn(
            "set {rawSubjects, rawSenders} to {subject, sender} of "
            "(every message of inboxMailbox whose date received > cutoffDate)",
            script,
        )
        self.assertIn(
            "(every message of sentMailbox whose date sent > cutoffDate)", script
        )
        self.assertNotIn("do shell script", script)

    def test_needs_response_prefilters_unread_in_window(self):
        captured, patcher = _capture("apple_mail_mcp.tools.smart_inbox")
        with patcher:
            smart_inbox_tools.get_needs_response(account="Work", days_back=7)

        script = captured["script"]
        self.assertIn(
            "whose read status is false and date received > cutoffDate", script
        )
        self.assertIn("subject of every message of sentMailbox", script)

    def test_top_senders_fetches_senders_in_one_event(self):
        captured, patcher = _capture("apple_mail_mcp.tools.smart_inbox")
        with patcher:
            smart_inbox_tools.get_top_senders(account="Work", days_back=0)

        self.assertIn(
            "set mailboxSenders to sender of every message of targetMailbox",
            captured["script"],
        )


class SubjectPrefilterTests(unittest.TestCase):
    def test_thread_uses_whose_subject_filter(self):
        captured, patcher = _capture("apple_mail_mcp.tools.search")
        with patcher:
            search_tools.get_email_thread(account="Work", subject_keyword="Re: Budget")

        self.assertIn(
            '(every message of currentMailbox whose subject contains "Budget")',
            captured["script"],
        )

    def test_attachments_uses_whose_subject_filter(self):
        captured, patcher = _capture("apple_mail_mcp.tools.analytics")
        with patcher:
            analytics_tools.list_email_attachments(account="Work", subject_keyword="Invoice")

        self.assertIn(
            '(every message of inboxMailbox whose subject contains "Invoice")',
            captured["script"],
        )

    def test_save_attachment_uses_whose_subject_filter(self):
        from apple_mail_mcp.tools import manage as manage_tools

        captured, patcher = _capture("apple_mail_mcp.tools.manage")
        with patcher:
            manage_tools.save_email_attachment(
                account="Work",
                subject_keyword="Invoice",
                attachment_name="a.pdf",
                save_path="~/Downloads/a.pdf",
            )

        self.assertIn(
            '(every message of inboxMailbox whose subject contains "Invoice")',
            captured["script"],
        )

    def test_export_single_email_uses_whose_subject_filter(self):
        captured, patcher = _capture("apple_mail_mcp.tools.analytics")
        with patcher:
            analytics_tools.export_emails(
                account="Work", scope="single_email", subject_keyword="Invoice"
            )

        self.assertIn(
            '(every message of targetMailbox whose subject contains "Invoice")',
            captured["script"],
        )

    def test_move_email_prefilters_with_whose_clause(self):
        from apple_mail_mcp.tools import manage as manage_tools

        captured, patcher = _capture("apple_mail_mcp.tools.manage")
        with patcher:
            manage_tools.move_email(
                account="Work",
                to_mailbox="Archive",
                subject_keyword="Invoice",
                sender="billing",
                only_read=True,
                older_than_days=30,
                dry_run=True,
            )

        self.assertIn(
            '(every message of sourceMailbox whose (subject contains "Invoice") '
            'and sender contains "billing" and read status is true '
            "and date received < cutoffDate)",
            captured["script"],
        )

    def test_statistics_overview_bulk_fetches_properties(self):
        captured, patcher = _capture("apple_mail_mcp.tools.analytics")
        with patcher:
            analytics_tools.get_statistics(account="Work")

        self.assertIn(
            "{id, read status, flagged status, sender} of "
            "(every message of aMailbox whose date received > targetDate)",
            captured["script"],
        )


if __name__ == "__main__":
    unittest.main()
