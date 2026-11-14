---
name: apple-mail-mcp
description: Use, check, reconnect, reinstall and upgrade the Apple Mail MCP server itself. Use when a session needs to know which Apple Mail tool does what, when Apple Mail tools are missing, show an old tool list or fail with "server closed" or "Connection closed", after the server was updated, when installing or re-registering it in Claude Code, Claude Desktop, Codex or another agent, or when checking that it starts.
---

# Apple Mail MCP: use, check, reconnect, reinstall

The server drives Apple Mail on this Mac. Run with `--read-only` it has 25
tools: it reads mail, saves and reads attachments, keeps a local search index,
creates drafts, and marks mail read or unread by message ID. It never sends.

## Which tool for what

| Task | Tools | Skill with the workflow |
|---|---|---|
| What is new, unread counts | `get_mailbox_unread_counts(summary_only=true)`, `list_inbox_emails(account="...", include_read=false)`, `get_inbox_overview()` | `apple-mail-inbox-triage` |
| Find mail fast, any mailbox | `index_mail()` once, then `search_mail_index(query="...")` with `read_status`, `folder`, `mailbox`, `account`, `sender`, `date_from`/`date_to`, `has_attachment` | |
| Search without the index | `search_emails(account="...", subject_keyword="...")` | |
| Read one message | `read_email(message_id=...)`; raw source with `get_email_source(...)` | |
| A conversation | `get_email_thread(account="...", subject_keyword="...")` | `apple-mail-thread-summary` |
| Attachments and pictures | `list_email_attachments(message_id=...)`, `read_email_attachment(message_id=..., part="...")`, `save_email_attachment(message_id=..., part="...", save_path="...")` | `apple-mail-attachments` |
| Unread mail as tasks | `search_mail_index(read_status="unread")`, then `set_email_read_status(message_ids=[...])` for what is solved | `apple-mail-inbox-triage` |
| Who owes whom a reply | `get_awaiting_reply(account="...")`, `get_needs_response(account="...")` | `apple-mail-follow-ups` |
| Write an email | `manage_drafts(action="create", ...)`; the user sends it from Mail | `apple-mail-safe-drafts` |
| Accounts, folders, numbers | `list_accounts()`, `list_account_addresses()`, `list_mailboxes(account="...")`, `get_statistics(account="...")`, `get_top_senders(account="...")` | |
| Fetch new mail now | `synchronize_account(account="...")` | |

Message IDs from any listing or search work in every ID-based tool. Under
`--read-only` the tools that send, reply, forward, move, flag, trash or create
mailboxes are not registered at all (for example `compose_email`,
`move_email`, `update_email_status`); never look for a way around that.

## Check that it works

1. The tools are listed in the session (often as `mcp__apple-mail__<tool>`).
   If they are missing, reconnect (below).
2. `list_accounts()` answers within a few seconds.
3. From a shell, without touching any mail, in a checkout of this repository:
   `python3 scripts/mcp_stdio_client.py -- <the command the client runs>`
   prints the startup time, the `tools/list` time and the tool count (25 with
   `--read-only`).
4. Is Mail itself responsive? `osascript -e 'tell application "Mail" to count of accounts'`.
   If that hangs, Mail is stuck, not the server; see `apple-mail-troubleshooting`.

## Reconnect in a running session

Symptoms: "server closed", "Connection closed", the tools vanished, or the
tool list is old (for example no `set_email_read_status(` after an upgrade).
A session keeps the server process and the start command it began with, so
an update reaches it only after a reconnect.

- **Claude Code:** `/mcp`, choose `apple-mail`, then Reconnect. That starts
  the command that is in the config now. Restarting the session works too.
- **Claude Desktop:** quit and reopen it.
- **Codex, agy and other agents:** start a new session; they start their MCP
  servers once per session.

Reconnect from inside the affected session. Do not kill server processes
that belong to other sessions.

## Install or reinstall

This fork (local index, read/unread, attachment reading, clean drafts) runs
from a checkout. The PyPI package `mcp-apple-mail` is the upstream version and
does not have those tools.

```sh
git clone https://github.com/Theralley/apple-mail-mcp.git
cd apple-mail-mcp
claude mcp add -s user apple-mail -- uv run --quiet --directory "$PWD" mcp-apple-mail --read-only
```

- Reinstall: `claude mcp remove -s user apple-mail`, add it again, then
  reconnect every open session.
- A running Claude Code session can write its config file back from memory
  and undo an edit made while it ran. Check the entry again after the old
  sessions are closed.
- Skills: link the folders in `skills/` into `~/.claude/skills/` (see
  `skills/README.md`).
- Permissions for the app that starts the server (Terminal, Ghostty, Claude,
  ...): **Automation › Mail** (required); **Full Disk Access** (fast reads
  from Mail's database and message files, otherwise it falls back to slower
  AppleScript); **Accessibility** (creating drafts: the server presses Send
  Again and Save in Mail's menus, which must be in English).

## Upgrade

1. Update the checkout (`git pull`, or merge the new version on a branch).
2. Run the unit tests; Mail is mocked, nothing is sent:
   `uv run --quiet --with pytest python -m pytest -q tests/`.
3. Check startup with `scripts/mcp_stdio_client.py` as above.
4. Reconnect the open sessions.

**The `mcp<2` trap.** Since 3.2.1 the server imports `FastMCP` from the
`fastmcp` package and runs on the `mcp` 2.x SDK. A command that still adds
`--with 'mcp<2'` (an old workaround) makes the current code crash at start
with `No module named 'mcp.server.request_state'`. Remove the pin from the
config or launcher, then reconnect; sessions started with the pinned command
fail until they reconnect.

## Machine-specific setups

If the server is started through a launcher or a lazy-start wrapper, or is
registered in several config profiles, keep those paths and install scripts
in private notes next to that setup, not in this repository. When a skill
named `apple-mail-mcp-local` is available, read it for the current machine.
