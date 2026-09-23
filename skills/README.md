# Claude Code skills for the Apple Mail MCP server

Each folder holds one skill: a `SKILL.md` with YAML frontmatter (`name`,
`description`) followed by instructions that call this server's tools by
their real names.

| Skill | Use it for |
|---|---|
| `apple-mail-inbox-triage` | Unread counts, newest unread, what needs a reply |
| `apple-mail-thread-summary` | Find a conversation and summarise it |
| `apple-mail-attachments` | List and save attachments |
| `apple-mail-follow-ups` | Sent mail with no reply, received mail you still owe a reply |
| `apple-mail-safe-drafts` | Create drafts for the user to review and send in Mail |
| `apple-mail-troubleshooting` | Timeouts, empty results, permission and startup problems |

The skills are written for a server running with `--read-only`. They call
only tools on the read-only allowlist, and `manage_drafts` only with `create`
and `list`. The server must be configured in Claude Code, for example as
`apple-mail`.
The skills name tools without the `mcp__<server>__` prefix. Claude resolves
them to whatever name the server is registered under.

## Install

Personal install, available in every project (symlinks keep the skills up to
date with this checkout):

```sh
mkdir -p ~/.claude/skills
for d in /path/to/apple-mail-mcp/skills/*/; do
  ln -sfn "$d" ~/.claude/skills/"$(basename "$d")"
done
```

For a single project, copy or symlink the same folders into that project's
`.claude/skills/`. To ship them with a plugin, put the folders in the
plugin's `skills/` directory, next to `plugin/skills/email-management`.

## Tests

- `tests/test_skills.py` runs in CI. It checks each `SKILL.md`'s frontmatter
  and that every tool a skill calls (written as `` `tool_name(` ``) is on the
  `--read-only` allowlist, with `manage_drafts` used only for `create`/`list`.
- `scripts/e2e_mail.py --skills` runs each skill's read-only steps against
  the real Mail.app on macOS. It prints timings only, never mail content.
