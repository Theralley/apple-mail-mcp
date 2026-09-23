"""Static checks for the Claude Code skills in skills/.

Every skills/<name>/SKILL.md must have YAML frontmatter with a matching
`name` and a trigger-style `description`, and every tool it calls — written
as `tool_name(` in backticks — must be registered by this server. The live
counterpart (running each skill's read-only steps against Mail.app) is
scripts/e2e_mail.py --skills.
"""

import asyncio
import re
import unittest
from pathlib import Path

from apple_mail_mcp import mcp

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
TOOL_CALL_RE = re.compile(r"`([a-z][a-z0-9_]*)\(")
# Registered normally but removed by --read-only; skills must not rely on them.
SEND_TOOLS = {"compose_email", "reply_to_email", "forward_email"}


def _skill_files():
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def _registered_tools():
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


class SkillLibraryTests(unittest.TestCase):
    def test_library_is_present(self):
        self.assertGreaterEqual(len(_skill_files()), 4)
        self.assertTrue((SKILLS_DIR / "README.md").is_file())

    def test_frontmatter(self):
        for path in _skill_files():
            with self.subTest(skill=path.parent.name):
                fields = _frontmatter(path.read_text(encoding="utf-8"))
                self.assertIsNotNone(fields, "missing --- frontmatter block")
                self.assertEqual(fields.get("name"), path.parent.name)
                self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                description = fields.get("description", "")
                self.assertIn("Use when", description)
                self.assertLessEqual(len(description), 1024)

    def test_referenced_tools_exist(self):
        registered = _registered_tools()
        allowed = registered - SEND_TOOLS
        for path in _skill_files():
            with self.subTest(skill=path.parent.name):
                referenced = set(TOOL_CALL_RE.findall(path.read_text(encoding="utf-8")))
                self.assertTrue(referenced, "skill references no tools")
                self.assertEqual(referenced - allowed, set())


if __name__ == "__main__":
    unittest.main()
