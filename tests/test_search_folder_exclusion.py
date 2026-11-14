"""System mailbox exclusions must compare names, not repeat references.

Adapted from upstream's test of the same bug: this fork compares the name with
`is in {list}` (text equality) instead of looping over list-item references,
so the check is on that line. It runs only on the AppleScript fallback, used
when Mail's database cannot be read.
"""

import shutil
import subprocess
import unittest
from unittest.mock import patch

from apple_mail_mcp.envelope_index import IndexUnavailable
from apple_mail_mcp.tools import search


class SearchFolderExclusionTests(unittest.TestCase):
    def _skip_line(self):
        with patch.object(search, "_search_records_from_index", side_effect=IndexUnavailable("test")), \
                patch.object(search, "run_applescript", return_value="") as run:
            search._search_mail_records(account="Work", mailbox="All", limit=1)
        script = run.call_args.args[0]
        line = next(line for line in script.splitlines() if "set shouldSkip to true" in line)
        return line.strip()

    def test_names_are_compared_as_text(self):
        line = self._skip_line()
        self.assertTrue(line.startswith("if mailboxName is in {"), line)
        self.assertNotIn("skipFolder", line)

    @unittest.skipUnless(shutil.which("osascript"), "requires AppleScript")
    def test_native_comparison_skips_system_folders_and_keeps_regular_mail(self):
        line = self._skip_line()
        for mailbox, expected in (
            ("Trash", "true"),
            ("Drafts", "true"),
            ("Junk Email", "true"),
            ("Sent Messages", "true"),
            ("trash", "true"),
            ("INBOX", "false"),
            ("Archive", "false"),
            ("Trash receipts", "false"),
        ):
            with self.subTest(mailbox=mailbox):
                script = f'set mailboxName to "{mailbox}"\nset shouldSkip to false\n{line}\nreturn shouldSkip'
                result = subprocess.run(
                    ["osascript", "-"], input=script, text=True,
                    capture_output=True, check=True, timeout=10,
                )
                self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
