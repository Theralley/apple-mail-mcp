"""Tests for structured email search and bulk update helpers."""

import json
import unittest
from unittest.mock import patch

from apple_mail_mcp.core import FIELD_SEP, RECORD_SEP
from apple_mail_mcp.tools import manage as manage_tools
from apple_mail_mcp.tools import search as search_tools
from apple_mail_mcp.tools import smart_inbox as smart_inbox_tools


def _record_line(
    message_id,
    subject,
    internet_message_id="<abc@example.com>",
    sender="sender@example.com",
    mailbox="INBOX",
    account="Work",
    is_read=False,
    received_date="2026-03-07T10:00:00",
    flag_index=-1,
    content_preview="",
):
    return FIELD_SEP.join(
        [
            str(message_id),
            internet_message_id,
            subject,
            sender,
            mailbox,
            account,
            "true" if is_read else "false",
            received_date,
            str(flag_index),
            content_preview,
        ]
    )


class SearchToolTests(unittest.TestCase):
    def test_search_emails_pagination_consistency(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return RECORD_SEP.join(
                [
                    _record_line(
                        100,
                        "Ticket 100",
                        received_date="2026-03-07T12:00:00",
                    ),
                    _record_line(
                        101,
                        "Ticket 101",
                        received_date="2026-03-07T11:00:00",
                    ),
                    _record_line(
                        102,
                        "Ticket 102",
                        received_date="2026-03-07T10:00:00",
                    ),
                ]
            )

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    output_format="json",
                    offset=1,
                    limit=2,
                    max_results=None,
                )
            )

        self.assertEqual(response["offset"], 1)
        self.assertEqual(response["returned"], 2)
        self.assertTrue(response["has_more"])
        self.assertEqual(response["next_offset"], 3)
        self.assertEqual(
            response["items"][0]["mail_link"],
            "message://%3Cabc@example.com%3E",
        )
        self.assertIn("set offsetRemaining to 1", captured["script"])
        self.assertIn("set collectLimit to 3", captured["script"])

    def test_search_emails_unread_only_filter(self):
        """Test that read_status='unread' adds the correct whose clause."""
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return _record_line(201, "Unread Ticket", is_read=False)

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    subject_keyword="Ticket",
                    read_status="unread",
                    output_format="json",
                    limit=1,
                )
            )

        self.assertEqual(len(response["items"]), 1)
        self.assertFalse(response["items"][0]["is_read"])
        self.assertIn("read status is false", captured["script"])

    def test_search_emails_builds_real_date_filters(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return _record_line(
                301,
                "Dated Ticket",
                received_date="2026-03-05T09:00:00",
            )

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    subject_keyword="Ticket",
                    date_from="2026-03-01",
                    date_to="2026-03-07",
                    output_format="json",
                    limit=1,
                    max_results=None,
                )
            )

        self.assertEqual(response["items"][0]["message_id"], "301")
        self.assertIn("set year of fromDate to 2026", captured["script"])
        self.assertIn("set month of fromDate to March", captured["script"])
        self.assertIn("date received >= fromDate", captured["script"])
        self.assertIn("date received <= toDate", captured["script"])

    def test_large_mailbox_search_uses_prefiltered_selection(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    subject_keywords=["INC-1", "INC-2"],
                    include_content=False,
                    output_format="json",
                    limit=50,
                    max_results=None,
                )
            )

        self.assertEqual(response["items"], [])
        self.assertIn(
            "set matchingMessages to every message of currentMailbox whose",
            captured["script"],
        )
        self.assertNotIn(
            "set mailboxMessages to every message of currentMailbox", captured["script"]
        )

    def test_search_emails_returns_mail_link_from_internet_message_id(self):
        def fake_run(script, timeout=120):
            return _record_line(
                401,
                "Linked Ticket",
                internet_message_id="<QwcH6OP9REaEX0pi8aR6-g@geopod-ismtpd-60>",
            )

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    subject_keyword="Linked",
                    output_format="json",
                    limit=1,
                    max_results=None,
                )
            )

        self.assertEqual(
            response["items"][0]["internet_message_id"],
            "<QwcH6OP9REaEX0pi8aR6-g@geopod-ismtpd-60>",
        )
        self.assertEqual(
            response["items"][0]["mail_link"],
            "message://%3CQwcH6OP9REaEX0pi8aR6-g@geopod-ismtpd-60%3E",
        )

    def test_search_emails_mail_link_normalizes_missing_angle_brackets(self):
        """AppleScript sometimes returns the Message-ID without angle brackets;
        the mail_link should still include them (percent-encoded)."""

        def fake_run(script, timeout=120):
            return _record_line(
                402,
                "Unbracketed Ticket",
                internet_message_id="abc@example.com",
            )

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            response = json.loads(
                search_tools.search_emails(
                    account="Work",
                    subject_keyword="Unbracketed",
                    output_format="json",
                    limit=1,
                    max_results=None,
                )
            )

        self.assertEqual(
            response["items"][0]["internet_message_id"],
            "abc@example.com",
        )
        self.assertEqual(
            response["items"][0]["mail_link"],
            "message://%3Cabc@example.com%3E",
        )

    def test_search_emails_account_none_iterates_all_accounts(self):
        """When account is None, the script should iterate all accounts."""
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(
                account=None,
                subject_keyword="Test",
                output_format="json",
                limit=5,
            )

        self.assertIn("set searchAccounts to every account", captured["script"])

    def test_search_emails_body_search_never_asks_mail_for_content(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(
                account="Work",
                body_text="invoice",
                sender="billing",
                read_status="unread",
                date_from="2026-01-01",
            )

        # Mail only lists candidate ids, with every other filter in the whose clause.
        self.assertEqual(len(scripts), 1)
        self.assertIn(
            'set idList to id of (every message of currentMailbox whose '
            'sender contains "billing" and read status is false '
            "and date received >= fromDate)",
            scripts[0],
        )
        self.assertNotIn("content of", scripts[0])
        self.assertNotIn("do shell script", scripts[0])

    def test_search_emails_body_search_matches_bodies_from_disk(self):
        from email.message import EmailMessage

        def make(body):
            message = EmailMessage()
            message.set_content(body)
            return message

        bodies = {"100": "nothing here", "101": "Your INVOICE is ready", "102": "invoice again"}
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            if len(scripts) == 1:
                return FIELD_SEP.join(["Work", "INBOX", "100,101,102"])
            return _record_line(101, "Receipt")

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message", side_effect=lambda i, a, m, timeout: make(bodies[i])):
            response = json.loads(
                search_tools.search_emails(
                    account="Work", body_text="invoice", output_format="json", limit=1
                )
            )

        # limit=1 needs limit + 1 matches: 101 and 102 (case-insensitive), not 100.
        self.assertIn("(id is 101 or id is 102)", scripts[1])
        self.assertNotIn("id is 100", scripts[1])
        self.assertNotIn("content of", scripts[1])
        self.assertEqual([item["message_id"] for item in response["items"]], ["101"])

    def test_search_emails_flag_color_in_body_search_path(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(
                account="Work", body_text="invoice", flag_color="red"
            )

        self.assertIn(
            "id of (every message of currentMailbox whose "
            "(flagged status is true and flag index is 0))",
            scripts[0],
        )

    def test_search_emails_body_search_reports_incomplete(self):
        from email.message import EmailMessage

        message = EmailMessage()
        message.set_content("invoice")

        def fake_run(script, timeout=120):
            return FIELD_SEP.join(["Work", "INBOX", "100,101"])

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message", return_value=message), \
                patch.object(search_tools, "BODY_SEARCH_BUDGET_S", -1):
            payload = json.loads(
                search_tools.search_emails(
                    account="Work", body_text="invoice", output_format="json"
                )
            )
            text = search_tools.search_emails(account="Work", body_text="invoice")

        self.assertEqual(payload["returned"], 0)
        self.assertTrue(payload["incomplete"])
        self.assertIn("results may be incomplete", payload["note"])
        self.assertIn("results may be incomplete", text)

    def test_search_emails_include_content_reads_preview_from_disk(self):
        def fake_run(script, timeout=120):
            self.assertNotIn("content of", script)
            return _record_line(100, "Report", mailbox="INBOX", account="Work")

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message_body", return_value="Line one\nLine\ttwo ||| end") as body:
            payload = json.loads(
                search_tools.search_emails(
                    account="Work", include_content=True, max_content_length=12, output_format="json"
                )
            )

        body.assert_called_once_with("100", "Work", "INBOX")
        self.assertEqual(payload["items"][0]["content_preview"], "Line one Lin...")

    def test_search_emails_reports_flag_color(self):
        def fake_run(script, timeout=120):
            return RECORD_SEP.join(
                [
                    _record_line(100, "Flagged orange", flag_index=1),
                    _record_line(101, "Not flagged", flag_index=-1),
                ]
            )

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            result = search_tools.search_emails(
                account="Work",
                output_format="json",
            )

        items = json.loads(result)["items"]
        flagged_item = next(i for i in items if i["message_id"] == "100")
        plain_item = next(i for i in items if i["message_id"] == "101")
        self.assertTrue(flagged_item["is_flagged"])
        self.assertEqual(flagged_item["flag_color"], "orange")
        self.assertFalse(plain_item["is_flagged"])
        self.assertNotIn("flag_color", plain_item)

    def test_search_emails_text_output_shows_flag_marker(self):
        def fake_run(script, timeout=120):
            return _record_line(100, "Budget review", flag_index=3)

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            result = search_tools.search_emails(account="Work")

        self.assertIn("Budget review ⚑ green", result)

    def test_search_emails_flagged_filter_builds_whose_clause(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(account="Work", flagged=True)

        self.assertIn("flagged status is true", captured["script"])

    def test_search_emails_flag_color_filter_uses_flag_index(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(account="Work", flag_color="purple")

        self.assertIn(
            "(flagged status is true and flag index is 5)", captured["script"]
        )

    def test_search_emails_rejects_invalid_flag_color(self):
        with patch("apple_mail_mcp.tools.search.run_applescript") as mock_run:
            result = search_tools.search_emails(account="Work", flag_color="magenta")

        self.assertIn("Invalid flag_color", result)
        mock_run.assert_not_called()

    def test_search_emails_rejects_flag_color_with_flagged_false(self):
        with patch("apple_mail_mcp.tools.search.run_applescript") as mock_run:
            result = search_tools.search_emails(
                account="Work", flagged=False, flag_color="blue"
            )

        self.assertIn("flag_color cannot be combined", result)
        mock_run.assert_not_called()


class ManageToolTests(unittest.TestCase):
    def test_update_email_status_with_message_ids_uses_exact_id_condition(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return "updated"

        with patch("apple_mail_mcp.tools.manage.run_applescript", side_effect=fake_run):
            result = manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                message_ids=["101", "202"],
                action="mark_read",
            )

        self.assertEqual(result, "updated")
        self.assertIn("id is 101", captured["script"])
        self.assertIn("id is 202", captured["script"])
        self.assertIn("set read status of targetMessages to true", captured["script"])

    def test_update_email_status_flag_color_sets_flag_index(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return "updated"

        with patch("apple_mail_mcp.tools.manage.run_applescript", side_effect=fake_run):
            result = manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                message_ids=["101"],
                action="flag",
                flag_color="Orange",
            )

        self.assertEqual(result, "updated")
        self.assertIn("set flag index of targetMessages to 1", captured["script"])
        self.assertIn("Flagged (orange)", captured["script"])
        # flag index alone doesn't activate the flag; both must be set.
        self.assertIn("set flagged status of targetMessages to true", captured["script"])

    def test_update_email_status_flag_without_color_uses_flagged_status(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return "updated"

        with patch("apple_mail_mcp.tools.manage.run_applescript", side_effect=fake_run):
            manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                message_ids=["101"],
                action="flag",
            )

        self.assertIn("set flagged status of targetMessages to true", captured["script"])
        self.assertNotIn("flag index", captured["script"])

    def test_update_email_status_flag_color_filter_path_matches_other_colors(self):
        captured = {}

        def fake_run(script, timeout=300):
            captured["script"] = script
            return "updated"

        with patch("apple_mail_mcp.tools.manage.run_applescript", side_effect=fake_run):
            manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                subject_keyword="invoice",
                action="flag",
                flag_color="green",
            )

        self.assertIn(
            "(flag index is not 3 or flagged status is false)", captured["script"]
        )
        self.assertIn("set flag index of aMessage to 3", captured["script"])
        self.assertIn("set flagged status of aMessage to true", captured["script"])

    def test_update_email_status_rejects_invalid_flag_color(self):
        with patch("apple_mail_mcp.tools.manage.run_applescript") as mock_run:
            result = manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                message_ids=["101"],
                action="flag",
                flag_color="magenta",
            )

        self.assertIn("Invalid flag_color", result)
        mock_run.assert_not_called()

    def test_update_email_status_rejects_flag_color_with_other_actions(self):
        with patch("apple_mail_mcp.tools.manage.run_applescript") as mock_run:
            result = manage_tools.update_email_status(
                account="Work",
                mailbox="INBOX",
                message_ids=["101"],
                action="mark_read",
                flag_color="red",
            )

        self.assertIn("only valid with action='flag'", result)
        mock_run.assert_not_called()


class SmartInboxToolTests(unittest.TestCase):
    def test_get_needs_response_priority_label_includes_flag_color(self):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return "no results"

        with patch(
            "apple_mail_mcp.tools.smart_inbox.run_applescript", side_effect=fake_run
        ):
            smart_inbox_tools.get_needs_response(account="Work")

        self.assertIn("set flagIndex to flag index of aMessage", captured["script"])
        self.assertIn(
            'set flagColorNames to {"red", "orange", "yellow", "green", "blue", "purple", "gray"}',
            captured["script"],
        )
        self.assertNotIn("content of", captured["script"])

    def test_get_needs_response_ranks_with_bodies_from_disk(self):
        header = (
            "EMAILS NEEDING RESPONSE\n"
            "Account: Work | Mailbox: INBOX | Last 7 days\n"
            "========================================\n\n"
        )
        entries = [
            ["1", "Plain note", "a@example.com", "Mon", ""],
            ["2", "Budget", "b@example.com", "Tue", "flagged red"],
            ["3", "Lunch?", "c@example.com", "Wed", ""],
            ["4", "Hello", "d@example.com", "Thu", ""],
        ]
        output = RECORD_SEP.join([header] + [FIELD_SEP.join(["ENTRY"] + e) for e in entries])
        bodies = {"1": "fyi", "2": "Can you check this?", "4": "x" * 600 + "?"}

        with patch("apple_mail_mcp.tools.smart_inbox.run_applescript", return_value=output), \
                patch("apple_mail_mcp.tools.smart_inbox.get_message_body",
                      side_effect=lambda i, a, m: bodies[i]) as body:
            result = smart_inbox_tools.get_needs_response(account="Work")

        # The subject already asks a question, so message 3's body is not read.
        self.assertNotIn("3", [c.args[0] for c in body.call_args_list])
        self.assertEqual(
            result,
            "EMAILS NEEDING RESPONSE\n"
            "Account: Work | Mailbox: INBOX | Last 7 days\n"
            "========================================\n\n"
            "1. [HIGH (flagged red + question)] Budget\n   From: b@example.com\n   Date: Tue\n\n"
            "2. [MEDIUM (contains question)] Lunch?\n   From: c@example.com\n   Date: Wed\n\n"
            "3. [NORMAL] Plain note\n   From: a@example.com\n   Date: Mon\n\n"
            "4. [NORMAL] Hello\n   From: d@example.com\n   Date: Thu\n\n"
            "========================================\n"
            "Found 4 email(s) needing response.",
        )


class _Clock:
    """Stand-in for the time module: monotonic() returns a settable value."""

    def __init__(self, t=0.0):
        self.t = t

    def monotonic(self):
        return self.t


def _message(body):
    from email.message import EmailMessage

    message = EmailMessage()
    message.set_content(body)
    return message


class SeparatorTests(unittest.TestCase):
    """Records use the ASCII unit/record separators, so '|||' in a name is data."""

    def test_search_records_keep_pipes_in_subject_and_mailbox(self):
        output = RECORD_SEP.join(
            [
                _record_line(100, "A ||| B", mailbox="Clients|||2026"),
                _record_line(101, "Second\nline?", mailbox="INBOX"),
            ]
        )
        records = search_tools._parse_search_records(output)
        self.assertEqual([r["subject"] for r in records], ["A ||| B", "Second\nline?"])
        self.assertEqual(records[0]["mailbox"], "Clients|||2026")
        self.assertEqual(records[0]["account"], "Work")

    def test_search_script_emits_separators(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.search_emails(account="Work", body_text="x")
            search_tools.search_emails(account="Work", subject_keyword="x")

        for script in scripts:
            self.assertNotIn("|||", script)
            self.assertIn("(character id 31)", script)
            self.assertIn("(character id 30)", script)

    def test_body_search_handles_pipes_in_mailbox_name(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            if len(scripts) == 1:
                return FIELD_SEP.join(["Work", "Clients|||2026", "100"])
            return _record_line(100, "Hit ||| here", mailbox="Clients|||2026")

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message",
                      side_effect=lambda i, a, m, timeout: _message("an invoice")) as get:
            payload = json.loads(
                search_tools.search_emails(account="Work", body_text="invoice", output_format="json")
            )

        self.assertEqual(get.call_args.args[:3], ("100", "Work", "Clients|||2026"))
        self.assertEqual(payload["items"][0]["subject"], "Hit ||| here")
        self.assertEqual(payload["items"][0]["mailbox"], "Clients|||2026")

    def test_export_entries_keep_pipes_in_subject(self):
        from apple_mail_mcp.tools import analytics as analytics_tools
        import tempfile

        output = RECORD_SEP.join(
            [
                FIELD_SEP.join(["COUNT", "2"]),
                FIELD_SEP.join(["ENTRY", "1", "Q3 ||| budget", "a@example.com", "Mon"]),
                FIELD_SEP.join(["ENTRY", "2", "Second", "b@example.com", "Tue"]),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp, \
                patch("apple_mail_mcp.tools.analytics.get_message_body", side_effect=lambda i, a, m: f"body {i}"):
            report = analytics_tools._write_mailbox_export(output, "Work", "INBOX", tmp, "txt", 10)
            import os
            files = sorted(os.listdir(f"{tmp}/INBOX_export"))
            with open(f"{tmp}/INBOX_export/1_Q3 ||| budget.txt", encoding="utf-8", newline="") as handle:
                first = handle.read()

        self.assertIn("Exported: 2", report)
        self.assertEqual(files, ["1_Q3 ||| budget.txt", "2_Second.txt"])
        self.assertEqual(first, "Subject: Q3 ||| budget\rFrom: a@example.com\rDate: Mon\r\rbody 1")

        single = FIELD_SEP.join(["FOUND", "7", "A ||| B", "c@example.com", "Wed"])
        self.assertEqual(
            analytics_tools._parse_export_entry(single), ("7", "A ||| B", "c@example.com", "Wed")
        )

    def test_recent_emails_keep_pipes_in_subject(self):
        from apple_mail_mcp.tools import analytics as analytics_tools

        output = RECORD_SEP.join(
            [
                FIELD_SEP.join(["A ||| B", "a@example.com", "Mon", "false", "Work", "5"]),
                FIELD_SEP.join(["Plain", "b@example.com", "Tue", "true", "Work", "6"]),
            ]
        )
        with patch("apple_mail_mcp.tools.analytics.run_applescript", return_value=output) as run, \
                patch("apple_mail_mcp.tools.analytics.get_message_body", side_effect=lambda i, a, m: f"body {i}"):
            emails = analytics_tools._get_recent_emails_structured()

        self.assertNotIn("|||", run.call_args.args[0])
        self.assertEqual([e["subject"] for e in emails], ["A ||| B", "Plain"])
        self.assertEqual([e["preview"] for e in emails], ["body 5", "body 6"])
        self.assertEqual(emails[0]["account"], "Work")

    def test_needs_response_keeps_pipes_in_subject(self):
        header = "EMAILS NEEDING RESPONSE\n========================================\n\n"
        output = RECORD_SEP.join(
            [header, FIELD_SEP.join(["ENTRY", "1", "Q3 ||| budget?", "a@example.com", "Mon", ""])]
        )
        with patch("apple_mail_mcp.tools.smart_inbox.run_applescript", return_value=output) as run:
            result = smart_inbox_tools.get_needs_response(account="Work")
        self.assertNotIn("|||", run.call_args.args[0])
        self.assertIn("1. [MEDIUM (contains question)] Q3 ||| budget?\n   From: a@example.com", result)

    def test_run_applescript_passes_separators_through(self):
        from apple_mail_mcp import core

        class Proc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return ("a\x1fb\x1f\x1ec\x1f\x01\r\n".encode(), b"")

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with patch.object(core, "_popen_factory", return_value=Proc()):
            # The trailing empty field survives; other control characters do not.
            self.assertEqual(core.run_applescript("x"), "a\x1fb\x1f\x1ec\x1f")


class BodySearchDeadlineTests(unittest.TestCase):
    """One budget covers listing, body reads, source fallbacks and the metadata fetch."""

    def _search(self, fake_run, get_message, clock):
        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message", side_effect=get_message), \
                patch.object(search_tools, "time", clock):
            return json.loads(
                search_tools.search_emails(account="Work", body_text="invoice", output_format="json")
            )

    def test_fallback_timeouts_follow_the_remaining_budget(self):
        clock = _Clock()
        timeouts = []
        budget = search_tools.BODY_SEARCH_BUDGET_S
        reserve = search_tools.BODY_SEARCH_FETCH_RESERVE_S

        def fake_run(script, timeout=120):
            timeouts.append(("script", timeout))
            if len(timeouts) == 1:
                return FIELD_SEP.join(["Work", "INBOX", "100,101,102"])
            return _record_line(100, "Hit")

        def get_message(i, a, m, timeout):
            timeouts.append((i, timeout))
            clock.t += (budget - reserve) / 2  # a slow source fallback
            return _message("invoice")

        payload = self._search(fake_run, get_message, clock)

        self.assertEqual(
            timeouts,
            [
                ("script", budget - reserve),
                ("100", budget - reserve),
                ("101", (budget - reserve) // 2),
                ("script", reserve),  # the metadata fetch gets what is left
            ],
        )
        self.assertTrue(payload["incomplete"])
        self.assertEqual([item["message_id"] for item in payload["items"]], ["100"])

    def test_metadata_fetch_timeout_reports_incomplete(self):
        clock = _Clock()

        def fake_run(script, timeout=120):
            if "set idList to" in script:
                return FIELD_SEP.join(["Work", "INBOX", "100"])
            clock.t += timeout
            raise Exception(f"AppleScript execution timed out after {timeout}s")

        payload = self._search(fake_run, lambda i, a, m, timeout: _message("invoice"), clock)
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["returned"], 0)

    def test_listing_timeout_reports_incomplete(self):
        def fake_run(script, timeout=120):
            raise Exception(f"AppleScript execution timed out after {timeout}s")

        payload = self._search(fake_run, lambda i, a, m, timeout: None, _Clock())
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["returned"], 0)

    def test_last_fallback_running_out_of_time_reports_incomplete(self):
        clock = _Clock()
        budget = search_tools.BODY_SEARCH_BUDGET_S

        def fake_run(script, timeout=120):
            return FIELD_SEP.join(["Work", "INBOX", "100"])

        def get_message(i, a, m, timeout):
            clock.t += budget  # the fallback used the whole budget and gave up
            return None

        payload = self._search(fake_run, get_message, clock)
        self.assertTrue(payload["incomplete"])

    def test_other_errors_still_raise(self):
        def fake_run(script, timeout=120):
            raise Exception("AppleScript error: Mail got an error")

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            with self.assertRaises(Exception):
                search_tools.search_emails(account="Work", body_text="invoice")

    def test_include_content_reuses_bodies_read_during_the_search(self):
        def fake_run(script, timeout=120):
            if "set idList to" in script:
                return FIELD_SEP.join(["Work", "INBOX", "100"])
            return _record_line(100, "Hit")

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
                patch("apple_mail_mcp.tools.search.get_message",
                      side_effect=lambda i, a, m, timeout: _message("the invoice text")), \
                patch("apple_mail_mcp.tools.search.get_message_body") as body:
            payload = json.loads(
                search_tools.search_emails(
                    account="Work", body_text="invoice", include_content=True, output_format="json"
                )
            )
        body.assert_not_called()
        self.assertEqual(payload["items"][0]["content_preview"], "the invoice text")


if __name__ == "__main__":
    unittest.main()
