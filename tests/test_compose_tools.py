"""Tests for compose and rich draft helpers."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apple_mail_mcp.tools import compose as compose_tools


def _make_subprocess_result(returncode=0, stdout=b"", stderr=b""):
    """Build a MagicMock shaped like subprocess.CompletedProcess."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class ComposeToolTests(unittest.TestCase):
    def test_create_rich_email_draft_writes_multipart_eml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "weekly-update.eml"

            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="sender@example.com",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run") as mock_run,
            ):
                result = compose_tools.create_rich_email_draft(
                    account="Work",
                    subject="Weekly Update",
                    to="team@example.com",
                    text_body="Plain fallback",
                    html_body="<html><body><h1>Weekly Update</h1></body></html>",
                    output_path=str(output_path),
                    open_in_mail=True,
                )

            payload = output_path.read_text()
            self.assertIn("multipart/alternative", payload)
            self.assertIn("<h1>Weekly Update</h1>", payload)
            self.assertIn("Subject: Weekly Update", payload)
            self.assertIn("Opened in Mail: yes", result)
            mock_run.assert_called_once_with(
                ["open", "-a", "Mail", str(output_path)], check=True
            )

    def test_create_rich_email_draft_allows_partial_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "partial.eml"

            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="sender@example.com",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                result = compose_tools.create_rich_email_draft(
                    account="Work",
                    output_path=str(output_path),
                    open_in_mail=False,
                )

            payload = output_path.read_text()
            self.assertIn("Draft outline", payload)
            self.assertIn("Missing details: subject, to, body", result)
            self.assertIn("Opened in Mail: no", result)

    def test_create_rich_email_draft_save_as_draft_makes_a_real_draft(self):
        # save_as_draft used to be unsupported (#83): an opened `.eml` is a
        # read-only viewer. It now goes through the same path as manage_drafts.
        with (
            patch("apple_mail_mcp.tools.compose._create_clean_draft", return_value="DRAFT OK") as create,
            patch("apple_mail_mcp.tools.compose.subprocess.run") as mock_run,
        ):
            result = compose_tools.create_rich_email_draft(
                account="Work",
                subject="Saved Draft",
                to="team@example.com",
                html_body="<p>Hi</p>",
                open_in_mail=True,
                save_as_draft=True,
            )

        self.assertEqual(result, "DRAFT OK")
        mock_run.assert_not_called()
        self.assertEqual(create.call_args.kwargs["html_body"], "<p>Hi</p>")

    def test_create_rich_email_draft_refuses_output_path_outside_allowed_folders(self):
        with patch("apple_mail_mcp.tools.compose.run_applescript", return_value="a@example.com"):
            for bad in ("/etc/x.eml", "~/.ssh/x.eml", "~/notes.txt"):
                result = compose_tools.create_rich_email_draft(
                    account="Work", subject="S", output_path=bad, open_in_mail=False)
                self.assertTrue(result.startswith("Error:"), (bad, result))

    def test_create_rich_email_draft_default_paths_never_collide(self):
        with patch("apple_mail_mcp.tools.compose.run_applescript", return_value="a@example.com"):
            paths = []
            for _ in range(2):
                result = compose_tools.create_rich_email_draft(
                    account="Work", subject="Same subject", to="t@example.com",
                    text_body="x", open_in_mail=False)
                line = next(l for l in result.splitlines() if l.startswith("EML path: "))
                paths.append(Path(line[len("EML path: "):]))
        try:
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(all(p.exists() for p in paths))
        finally:
            for p in paths:
                p.unlink(missing_ok=True)

    def test_create_rich_email_draft_omits_save_note_when_not_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "unsaved.eml"

            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="sender@example.com",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                result = compose_tools.create_rich_email_draft(
                    account="Work",
                    subject="Unsaved Draft",
                    output_path=str(output_path),
                    open_in_mail=True,
                )

            self.assertIn("Saved in Drafts: no", result)
            self.assertNotIn("save_as_draft could not be honored", result)


class StripCdataTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(compose_tools._strip_cdata_wrappers(None))

    def test_empty_passes_through(self):
        self.assertEqual("", compose_tools._strip_cdata_wrappers(""))

    def test_unwraps_symmetric_block(self):
        self.assertEqual(
            "<p>Hello</p>",
            compose_tools._strip_cdata_wrappers("<![CDATA[<p>Hello</p>]]>"),
        )

    def test_unwraps_multiline_block(self):
        self.assertEqual(
            "\n<p>Hi</p>\n",
            compose_tools._strip_cdata_wrappers("<![CDATA[\n<p>Hi</p>\n]]>"),
        )

    def test_strips_stray_closing_marker(self):
        # This is the symptom users actually see — HTML parsers hide the
        # opening `<![CDATA[`, but the trailing `]]>` renders as text.
        self.assertEqual(
            "<p>Hello</p>",
            compose_tools._strip_cdata_wrappers("<p>Hello</p>]]>"),
        )

    def test_strips_stray_opening_marker(self):
        self.assertEqual(
            "<p>Hello</p>",
            compose_tools._strip_cdata_wrappers("<![CDATA[<p>Hello</p>"),
        )

    def test_leaves_normal_html_untouched(self):
        html = '<html><body><h1>Weekly Update</h1></body></html>'
        self.assertEqual(html, compose_tools._strip_cdata_wrappers(html))


class CreateRichEmailDraftCdataTests(unittest.TestCase):
    def test_cdata_wrapped_html_body_is_stripped_in_eml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "cdata.eml"

            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="sender@example.com",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                compose_tools.create_rich_email_draft(
                    account="Work",
                    subject="CDATA Test",
                    to="team@example.com",
                    text_body="Plain fallback",
                    html_body="<![CDATA[<html><body><h1>Hi</h1></body></html>]]>",
                    output_path=str(output_path),
                    open_in_mail=False,
                )

            payload = output_path.read_text()
            self.assertIn("<h1>Hi</h1>", payload)
            self.assertNotIn("<![CDATA[", payload)
            self.assertNotIn("]]>", payload)


class ValidateFromAddressTests(unittest.TestCase):
    def test_none_skips_lookup(self):
        with patch("apple_mail_mcp.tools.compose.run_applescript") as mock_run:
            override, error = compose_tools._validate_from_address("Work", None)
        self.assertIsNone(override)
        self.assertIsNone(error)
        mock_run.assert_not_called()

    def test_blank_skips_lookup(self):
        with patch("apple_mail_mcp.tools.compose.run_applescript") as mock_run:
            override, error = compose_tools._validate_from_address("Work", "   ")
        self.assertIsNone(override)
        self.assertIsNone(error)
        mock_run.assert_not_called()

    def test_matches_case_insensitively_and_trims(self):
        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            return_value="Default@Example.com\nSecondary@Example.org",
        ):
            override, error = compose_tools._validate_from_address(
                "Work", "  SECONDARY@example.ORG "
            )
        self.assertEqual(override, "Secondary@Example.org")
        self.assertIsNone(error)

    def test_unknown_alias_returns_error(self):
        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            return_value="default@example.com",
        ):
            override, error = compose_tools._validate_from_address(
                "Work", "other@example.com"
            )
        self.assertIsNone(override)
        self.assertIn("is not configured on account", error)
        self.assertIn("default@example.com", error)

    def test_missing_aliases_returns_error(self):
        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            return_value="",
        ):
            override, error = compose_tools._validate_from_address(
                "Work", "anything@example.com"
            )
        self.assertIsNone(override)
        self.assertIn("Could not read email addresses", error)


class ComposeEmailSenderOverrideTests(unittest.TestCase):
    def test_default_emits_single_alias_fallback_block(self):
        captured = []

        def fake_run(script, timeout=120):
            captured.append(script)
            return "✓ Email sent successfully!"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            compose_tools.compose_email(
                account="Work",
                to="self@example.com",
                subject="Test",
                body="Body",
                mode="open",
            )

        self.assertEqual(len(captured), 1)
        script = captured[0]
        self.assertIn("email addresses of targetAccount", script)
        self.assertIn("if (count of emailAddrs) is 1 then", script)
        self.assertIn(
            "set sender of newMessage to item 1 of emailAddrs", script
        )
        self.assertNotIn('set sender of newMessage to "', script)

    def test_injects_sender_when_from_address_is_valid(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            if len(scripts) == 1:
                return "default@example.com\nsecondary@example.org"
            return "✓ Email sent successfully!"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            compose_tools.compose_email(
                account="Work",
                to="self@example.com",
                subject="Test",
                body="Body",
                mode="open",
                from_address="secondary@example.org",
            )

        self.assertEqual(len(scripts), 2)
        main_script = scripts[1]
        self.assertIn(
            'set sender of newMessage to "secondary@example.org"', main_script
        )
        self.assertNotIn("if (count of emailAddrs) is 1 then", main_script)

    def test_rejects_invalid_from_address_without_sending(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return "default@example.com"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            result = compose_tools.compose_email(
                account="Work",
                to="self@example.com",
                subject="Test",
                body="Body",
                mode="open",
                from_address="unknown@example.com",
            )

        self.assertEqual(len(scripts), 1)
        self.assertTrue(result.startswith("Error: 'from_address'"))


class AccountDefaultAliasIfSingleTests(unittest.TestCase):
    def test_returns_sole_alias(self):
        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            return_value="solo@example.com",
        ):
            self.assertEqual(
                compose_tools._account_default_alias_if_single("Solo"),
                "solo@example.com",
            )

    def test_returns_none_when_empty(self):
        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            return_value="",
        ):
            self.assertIsNone(
                compose_tools._account_default_alias_if_single("Multi")
            )


class ComposeSenderScriptTests(unittest.TestCase):
    def test_override_sets_sender_directly(self):
        script = compose_tools._compose_sender_script(
            "newMessage", "targetAccount", "chosen@example.com"
        )
        self.assertEqual(
            script, 'set sender of newMessage to "chosen@example.com"'
        )

    def test_without_override_emits_single_alias_fallback(self):
        script = compose_tools._compose_sender_script(
            "newMessage", "targetAccount", None
        )
        self.assertIn("email addresses of targetAccount", script)
        self.assertIn("if (count of emailAddrs) is 1 then", script)
        self.assertIn(
            "set sender of newMessage to item 1 of emailAddrs", script
        )

    def test_override_value_is_escaped(self):
        script = compose_tools._compose_sender_script(
            "newMessage", "targetAccount", 'weird"quote@example.com'
        )
        self.assertIn(r'\"quote@example.com', script)


class CreateRichEmailDraftFromAddressTests(unittest.TestCase):
    def test_omits_from_header_for_multi_alias_account(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "multi.eml"
            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                compose_tools.create_rich_email_draft(
                    account="Multi",
                    subject="No From",
                    to="team@example.com",
                    text_body="Body",
                    output_path=str(output_path),
                    open_in_mail=False,
                )

            payload = output_path.read_text()
            header_block = payload.split("\n\n", 1)[0]
            self.assertNotIn("From:", header_block)

    def test_stamps_from_header_for_single_alias_account(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "single.eml"
            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="solo@example.com",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                compose_tools.create_rich_email_draft(
                    account="Solo",
                    subject="Single",
                    to="team@example.com",
                    text_body="Body",
                    output_path=str(output_path),
                    open_in_mail=False,
                )

            payload = output_path.read_text()
            self.assertIn("From: solo@example.com", payload)

    def test_stamps_from_header_when_address_is_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "stamped.eml"
            with (
                patch(
                    "apple_mail_mcp.tools.compose.run_applescript",
                    return_value="default@example.com\nsecondary@example.org",
                ),
                patch("apple_mail_mcp.tools.compose.subprocess.run"),
            ):
                compose_tools.create_rich_email_draft(
                    account="Work",
                    subject="Stamped",
                    to="team@example.com",
                    text_body="Body",
                    output_path=str(output_path),
                    open_in_mail=False,
                    from_address="secondary@example.org",
                )

            payload = output_path.read_text()
            self.assertIn("From: secondary@example.org", payload)


class ReplyToEmailSenderOverrideTests(unittest.TestCase):
    def test_default_emits_single_alias_fallback_for_reply_message(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript"
            ) as mock_applescript,
        ):
            compose_tools.reply_to_email(
                account="Work",
                subject_keyword="test",
                reply_body="Reply body",
                send=False,
            )

        mock_applescript.assert_not_called()
        script = mock_run.call_args.kwargs["input"].decode("utf-8")
        self.assertIn("if (count of emailAddrs) is 1 then", script)
        self.assertIn(
            "set sender of replyMessage to item 1 of emailAddrs", script
        )
        self.assertNotIn('set sender of replyMessage to "', script)

    def test_injects_sender_when_from_address_is_valid(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="default@example.com\nsecondary@example.org",
            ),
        ):
            compose_tools.reply_to_email(
                account="Work",
                subject_keyword="test",
                reply_body="Reply body",
                from_address="secondary@example.org",
                send=False,
            )

        script = mock_run.call_args.kwargs["input"].decode("utf-8")
        self.assertIn(
            'set sender of replyMessage to "secondary@example.org"', script
        )
        self.assertNotIn("if (count of emailAddrs) is 1 then", script)

    def test_rejects_invalid_from_address_without_running_main_script(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run"
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="default@example.com",
            ),
        ):
            result = compose_tools.reply_to_email(
                account="Work",
                subject_keyword="test",
                reply_body="Reply body",
                from_address="unknown@example.com",
                send=False,
            )

        mock_run.assert_not_called()
        self.assertTrue(result.startswith("Error: 'from_address'"))


class ForwardEmailSenderOverrideTests(unittest.TestCase):
    def test_default_emits_single_alias_fallback_for_forward_message(self):
        captured = []

        def fake_run(script, timeout=120):
            captured.append(script)
            return "✓ Forwarded"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            compose_tools.forward_email(
                account="Work",
                subject_keyword="test",
                to="recipient@example.com",
            )

        self.assertEqual(len(captured), 1)
        script = captured[0]
        self.assertIn("if (count of emailAddrs) is 1 then", script)
        self.assertIn(
            "set sender of forwardMessage to item 1 of emailAddrs", script
        )
        self.assertNotIn('set sender of forwardMessage to "', script)

    def test_injects_sender_when_from_address_is_valid(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            if len(scripts) == 1:
                return "default@example.com\nsecondary@example.org"
            return "✓ Forwarded"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            compose_tools.forward_email(
                account="Work",
                subject_keyword="test",
                to="recipient@example.com",
                from_address="secondary@example.org",
            )

        self.assertEqual(len(scripts), 2)
        main_script = scripts[1]
        self.assertIn(
            'set sender of forwardMessage to "secondary@example.org"',
            main_script,
        )
        self.assertNotIn("if (count of emailAddrs) is 1 then", main_script)

    def test_rejects_invalid_from_address_without_running_main_script(self):
        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return "default@example.com"

        with patch(
            "apple_mail_mcp.tools.compose.run_applescript",
            side_effect=fake_run,
        ):
            result = compose_tools.forward_email(
                account="Work",
                subject_keyword="test",
                to="recipient@example.com",
                from_address="unknown@example.com",
            )

        self.assertEqual(len(scripts), 1)
        self.assertTrue(result.startswith("Error: 'from_address'"))


class ManageDraftsCreateTests(unittest.TestCase):
    """manage_drafts(action="create") builds the message and hands it to
    draft_gui; it never scripts a body into an outgoing message."""

    def _run(self, identity="ID\x1fAnn Example\x1fdefault@example.com\x1fsecondary@example.org", **kwargs):
        from apple_mail_mcp import draft_gui

        scripts = []

        def fake_run(script, timeout=120):
            scripts.append(script)
            return identity

        calls = []

        def fake_create(msg, **kw):
            calls.append((msg, kw))
            return draft_gui.DraftResult(True, ["Checked."], draft_id=4242)

        args = dict(account="Work", action="create", subject="Draft",
                    to="recipient@example.com", body="Body")
        args.update(kwargs)
        with (
            patch("apple_mail_mcp.tools.compose.run_applescript", side_effect=fake_run),
            patch("apple_mail_mcp.tools.compose.ei.account_uuid", return_value="UUID-1"),
            patch("apple_mail_mcp.tools.compose.draft_mime.resolve_signature",
                  return_value=(None, "none (test)")),
            patch("apple_mail_mcp.tools.compose.draft_gui.create_in_mail", side_effect=fake_create),
        ):
            result = compose_tools.manage_drafts(**args)
        return result, scripts, calls

    def test_default_sender_is_the_accounts_first_address(self):
        result, scripts, calls = self._run()
        self.assertEqual(len(scripts), 1)
        msg, kw = calls[0]
        self.assertEqual(kw["sender"], "default@example.com")
        self.assertIn("default@example.com", str(msg["From"]))
        self.assertIn("Ann Example", str(msg["From"]))
        self.assertEqual(kw["account_uuid"], "UUID-1")
        self.assertIn("ID 4242", result)
        self.assertIn("It was not sent", result)

    def test_from_address_override(self):
        _, _, calls = self._run(from_address="Secondary@Example.org")
        _, kw = calls[0]
        self.assertEqual(kw["sender"], "secondary@example.org")

    def test_rejects_unknown_from_address_before_mail_is_touched(self):
        result, _, calls = self._run(from_address="unknown@example.com")
        self.assertTrue(result.startswith("Error: 'from_address'"))
        self.assertEqual(calls, [])

    def test_no_script_sets_content_or_makes_outgoing_message(self):
        _, scripts, _ = self._run()
        for script in scripts:
            self.assertNotIn("outgoing message", script)
            self.assertNotIn("content:", script)
            self.assertNotIn("keystroke", script)

    def test_requires_subject_to_and_body(self):
        result, scripts, calls = self._run(body=None)
        self.assertIn("required", result)
        self.assertEqual((scripts, calls), ([], []))

    def test_attachment_checks_happen_before_mail_is_touched(self):
        result, scripts, calls = self._run(attachments="~/.ssh/id_ed25519")
        self.assertTrue(result.startswith("Error:"))
        self.assertEqual((scripts, calls), ([], []))

    def test_attachments_are_passed_with_hashes(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "offer.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            result, _, calls = self._run(attachments=[str(pdf)])
        msg, kw = calls[0]
        self.assertEqual(kw["attachments"], [("offer.pdf", hashlib.sha256(b"%PDF-1.4 test").hexdigest())])
        self.assertIn("offer.pdf", result)
        names = [part.get_filename() for part in msg.iter_attachments()]
        self.assertEqual(names, ["offer.pdf"])

    def test_failure_keeps_the_eml_and_says_so(self):
        from apple_mail_mcp import draft_gui

        failed = draft_gui.DraftResult(False, ["Mail did not create the draft: x."],
                                       eml_path=Path("/tmp/draft-x.eml"))
        with (
            patch("apple_mail_mcp.tools.compose.run_applescript", return_value="ID\x1f\x1fa@example.com"),
            patch("apple_mail_mcp.tools.compose.ei.account_uuid", return_value="UUID-1"),
            patch("apple_mail_mcp.tools.compose.draft_mime.resolve_signature", return_value=(None, "none")),
            patch("apple_mail_mcp.tools.compose.draft_gui.create_in_mail", return_value=failed),
        ):
            result = compose_tools.manage_drafts(account="Work", action="create", subject="S",
                                                 to="b@example.com", body="B")
        self.assertIn("the draft was not created", result)
        self.assertIn("/tmp/draft-x.eml", result)

    def test_empty_account_name_keeps_the_first_address(self):
        _, _, calls = self._run(identity="ID\x1f\x1fsolo@example.com")
        msg, kw = calls[0]
        self.assertEqual(kw["sender"], "solo@example.com")
        self.assertEqual(str(msg["From"]), "solo@example.com")

    def test_html_body_is_checked_against_the_html(self):
        _, _, calls = self._run(body="Plain fallback", html_body="<h1>Weekly Update</h1>")
        self.assertEqual(calls[0][1]["plain_body"].strip(), "Weekly Update")

    def test_compose_email_draft_mode_uses_the_same_path(self):
        with patch("apple_mail_mcp.tools.compose._create_clean_draft", return_value="OK") as create, \
                patch("apple_mail_mcp.tools.compose.run_applescript") as run:
            result = compose_tools.compose_email(account="Work", to="a@example.com", subject="S",
                                                 body="B", mode="draft")
        self.assertEqual(result, "OK")
        run.assert_not_called()
        self.assertEqual(create.call_args.kwargs["text_body"], "B")


class PasteFocusHardeningTests(unittest.TestCase):
    """Regression guard for the silent empty-send bug.

    The HTML body is inserted via a blind Cmd+V into the compose window, which
    only lands if Mail is frontmost when the keystroke fires. The original code
    relied on a single `activate` + fixed `delay`, which intermittently sent an
    empty message when the window had not yet gained focus. The fix polls until
    Mail is genuinely frontmost before pasting. These tests assert the poll is
    present in the generated AppleScript and runs BEFORE the paste keystroke.

    They also lock in the deliberate decision NOT to verify via
    `content of <message>`: Mail's GUI editor buffer is decoupled from the
    scriptable `content` property, so a content-read-back guard would
    false-fail every send.
    """

    FOCUS_POLL = (
        'repeat until (frontmost of process "Mail") '
        "or (focusWaited is greater than or equal to 40)"
    )
    PASTE = 'keystroke "v" using command down'

    def _render_reply(self, **overrides):
        kwargs = dict(
            account="Work",
            subject_keyword="test",
            reply_body="Reply body text",
            body_html="<p>Reply body text</p>",
            send=True,
        )
        kwargs.update(overrides)
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="a@b.com",
            ),
        ):
            compose_tools.reply_to_email(**kwargs)
        return mock_run.call_args.kwargs["input"].decode("utf-8")

    def _render_compose_html(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="a@b.com",
            ),
        ):
            compose_tools.compose_email(
                account="Work",
                to="x@example.com",
                subject="s",
                body="Body text",
                body_html="<p>Body text</p>",
                mode="send",
            )
        return mock_run.call_args.kwargs["input"].decode("utf-8")

    def test_reply_polls_for_focus_before_pasting(self):
        script = self._render_reply()
        self.assertIn(self.FOCUS_POLL, script)
        self.assertLess(
            script.index(self.FOCUS_POLL),
            script.index(self.PASTE),
            "focus poll must run before the paste keystroke",
        )

    def test_reply_drops_fragile_fixed_predelay(self):
        # The fragile original inserted a single `delay 1.5` between `activate`
        # and the paste, with no frontmost re-assertion. The fix replaces that
        # fixed gamble with a focus poll, so the 1.5s pre-paste delay is gone.
        script = self._render_reply()
        self.assertNotIn("delay 1.5", script)

    def test_reply_does_not_read_back_content_as_guard(self):
        # Mail's `content` property does not reflect a GUI paste, so any
        # `content of replyMessage` verification would false-fail every send.
        script = self._render_reply()
        self.assertNotIn("content of replyMessage", script)

    def test_compose_html_polls_for_focus_before_pasting(self):
        script = self._render_compose_html()
        self.assertIn(self.FOCUS_POLL, script)
        self.assertLess(
            script.index(self.FOCUS_POLL),
            script.index(self.PASTE),
            "focus poll must run before the paste keystroke",
        )


class HtmlBodyNoDuplicatePasteTests(unittest.TestCase):
    """Regression guard for the dual-body bug (#reported 2026-06-23).

    When both a plain body and ``body_html`` were supplied, the clipboard
    paste path wrote the raw HTML *source* under ``NSPasteboardTypeHTML``.
    For anything but a complete HTML document, Mail rendered a best-effort
    version AND surfaced the literal ``<p>``/``<b>`` markup, so the message
    appeared twice — once rendered, once as source. The fix converts the
    HTML to an NSAttributedString and writes it back as RTF (a single
    unambiguous rich-text flavor) plus a *rendered* plain-text fallback.

    These tests assert, at the script-generation level, that:
      * the raw-HTML flavor (``NSPasteboardTypeHTML``) is no longer written;
      * RTF is written via NSAttributedString -> RTFFromRange;
      * the plain body is never injected as a second visible paste.
    """

    BODY_PLAIN = "Plain fallback text"
    BODY_HTML = "<p>Rendered <b>HTML</b></p>"

    def _render_reply(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="a@b.com",
            ),
        ):
            compose_tools.reply_to_email(
                account="Work",
                subject_keyword="test",
                reply_body=self.BODY_PLAIN,
                body_html=self.BODY_HTML,
                mode="open",
            )
        return mock_run.call_args.kwargs["input"].decode("utf-8")

    def _render_compose(self):
        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                return_value=_make_subprocess_result(),
            ) as mock_run,
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="a@b.com",
            ),
        ):
            compose_tools.compose_email(
                account="Work",
                to="x@example.com",
                subject="s",
                body=self.BODY_PLAIN,
                body_html=self.BODY_HTML,
                mode="open",
            )
        return mock_run.call_args.kwargs["input"].decode("utf-8")

    def _render_forward(self):
        captured = {}

        def fake_run(*args, **kwargs):
            captured["input"] = kwargs.get("input")
            return _make_subprocess_result()

        with (
            patch(
                "apple_mail_mcp.tools.compose.subprocess.run",
                side_effect=fake_run,
            ),
            patch(
                "apple_mail_mcp.tools.compose.run_applescript",
                return_value="a@b.com",
            ),
        ):
            compose_tools.forward_email(
                account="Work",
                subject_keyword="test",
                to="x@example.com",
                message="A note before the forward",
            )
        return captured["input"].decode("utf-8")

    def _assert_rtf_not_raw_html(self, script):
        self.assertNotIn(
            "setData:htmlData forType:(current application's NSPasteboardTypeHTML)",
            script,
            "raw HTML source must not be placed on the pasteboard",
        )
        self.assertIn("NSPasteboardTypeRTF", script)
        self.assertIn("RTFFromRange", script)
        self.assertIn("NSAttributedString", script)
        # The HTML importer must be pinned to UTF-8, otherwise Cocoa auto-detects
        # the charset and defaults to Latin-1/Windows-1252, double-decoding the
        # UTF-8 bytes for ä/ö/ü/ß/emoji into mojibake (e.g. "Grüße" -> "GrÃ¼ÃŸe").
        self.assertIn("NSCharacterEncodingDocumentAttribute", script)
        self.assertIn("NSUTF8StringEncoding", script)

    def test_reply_uses_rtf_not_raw_html(self):
        self._assert_rtf_not_raw_html(self._render_reply())

    def test_compose_uses_rtf_not_raw_html(self):
        self._assert_rtf_not_raw_html(self._render_compose())

    def test_forward_uses_rtf_not_raw_html(self):
        self._assert_rtf_not_raw_html(self._render_forward())

    def test_reply_pastes_body_only_once(self):
        # Exactly one Cmd+V paste of the body — never the plain text as a
        # second visible paste alongside the HTML.
        script = self._render_reply()
        self.assertEqual(script.count('keystroke "v" using command down'), 1)

    def test_reply_plain_body_not_injected_as_content(self):
        # The plain reply_body must not be set as the message `content`
        # (which would render as a second body under the pasted HTML).
        script = self._render_reply()
        self.assertNotIn(f'content:"{self.BODY_PLAIN}"', script)


class PasteboardUtf8EncodingTests(unittest.TestCase):
    """Guards the multibyte-UTF-8 fix in _html_to_pasteboard_script.

    Regression: without NSCharacterEncodingDocumentAttribute -> NSUTF8StringEncoding,
    the Cocoa HTML importer defaulted to Latin-1 and turned German umlauts/ß and
    emoji into mojibake when relaying a body to Mail (reply/compose/forward).
    """

    def test_helper_pins_html_importer_to_utf8(self):
        snippet = compose_tools._html_to_pasteboard_script("/tmp/example.html")
        self.assertIn("NSCharacterEncodingDocumentAttribute", snippet)
        self.assertIn("NSUTF8StringEncoding", snippet)
        # Both the document-type and the encoding key must be in the options dict.
        self.assertIn("NSDocumentTypeDocumentAttribute", snippet)
        self.assertIn("NSHTMLTextDocumentType", snippet)
        self.assertIn("dictionaryWithObjects:", snippet)



class ManageDraftsReadOnlyTests(unittest.TestCase):
    def test_send_is_refused_when_read_only_is_set_after_import(self):
        """__main__ sets server.READ_ONLY after the tool modules are imported;
        the send guard must read the flag at call time."""
        from apple_mail_mcp import server

        original = server.READ_ONLY
        server.READ_ONLY = True
        try:
            with patch.object(compose_tools, "run_applescript") as run:
                result = compose_tools.manage_drafts(
                    account="Work", action="send", draft_subject="Anything"
                )
        finally:
            server.READ_ONLY = original

        self.assertIn("blocked by --read-only", result)
        run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
