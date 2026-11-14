# Apple Mail MCP Server

<!-- mcp-name: io.github.patrickfreyer/apple-mail -->

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI](https://img.shields.io/pypi/v/mcp-apple-mail)](https://pypi.org/project/mcp-apple-mail/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![GitHub stars](https://img.shields.io/github/stars/patrickfreyer/apple-mail-mcp?style=social)](https://github.com/patrickfreyer/apple-mail-mcp/stargazers)

## Star History

<a href="https://star-history.com/#patrickfreyer/apple-mail-mcp&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=patrickfreyer/apple-mail-mcp&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=patrickfreyer/apple-mail-mcp&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=patrickfreyer/apple-mail-mcp&type=Date" />
 </picture>
</a>

An MCP server that gives AI assistants full access to Apple Mail -- read, search, compose, organize, and analyze emails via natural language. Built with [FastMCP](https://github.com/jlowin/fastmcp).

## Quick Install

**Prerequisites:** macOS with Apple Mail configured, Python 3.10+

### Claude Code Plugin (Recommended)

Two commands — gets you the MCP server, `/email-management` slash command, and the Email Management Expert skill:

```bash
claude plugin marketplace add patrickfreyer/apple-mail-mcp
claude plugin install apple-mail@apple-mail-mcp
```

Then restart Claude Code.

### Other Install Methods

<details>
<summary><strong>uvx (zero install, MCP server only)</strong></summary>

```bash
claude mcp add apple-mail -- uvx mcp-apple-mail
```

Or for Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "uvx",
      "args": ["mcp-apple-mail"]
    }
  }
}
```

</details>

<details>
<summary><strong>pip install (MCP server only)</strong></summary>

```bash
pip install mcp-apple-mail
claude mcp add apple-mail -- mcp-apple-mail
```

</details>

<details>
<summary><strong>Claude Desktop MCPB</strong></summary>

1. Download the latest `apple-mail-mcp-vX.Y.Z.mcpb` from [Releases](https://github.com/patrickfreyer/apple-mail-mcp/releases)
2. Open Claude Desktop → Settings → Developer → MCP Servers → Install from file
3. Select the `.mcpb` file and grant Mail.app permissions

</details>

<details>
<summary><strong>Manual setup</strong></summary>

```bash
git clone https://github.com/patrickfreyer/apple-mail-mcp.git
cd apple-mail-mcp/plugin
python3 -m venv venv
venv/bin/pip install -r requirements.txt

claude mcp add apple-mail -- /bin/bash $(pwd)/start_mcp.sh
```

</details>

## Tools (31)

### Reading & Search
| Tool | Description |
|------|-------------|
| `get_inbox_overview` | Dashboard with unread counts, folders, and recent emails |
| `list_inbox_emails` | List emails with account/read-status filtering and optional content preview |
| `get_mailbox_unread_counts` | Unread counts per mailbox or per-account summary |
| `list_accounts` | List all configured Mail accounts |
| `search_emails` | Unified search — subject, sender, body text, dates, attachments, flag status/color, cross-account |
| `get_email_thread` | Conversation thread view |
| `read_email` | Read one email by its message ID from Mail's files (headers, body, attachment parts) |

### Local search index (Inbox and Sent)
| Tool | Description |
|------|-------------|
| `index_mail` | Build or update a full-text index of Inbox and Sent on this Mac (newest first, resumable) |
| `search_mail_index` | Search it in milliseconds: subject, people, attachment names and body text, with filters |

### Organization
| Tool | Description |
|------|-------------|
| `list_mailboxes` | Folder hierarchy with message counts |
| `create_mailbox` | Create new mailboxes (supports nested paths) |
| `move_email` | Move/archive emails with filters (subject, sender, date, read status, dry-run) |
| `update_email_status` | Mark read/unread, flag/unflag (optional flag color) — by filters or message IDs |
| `manage_trash` | Soft delete, permanent delete, empty trash (with dry-run) |

### Composition
| Tool | Description |
|------|-------------|
| `compose_email` | Send new emails (plain text or HTML body) |
| `reply_to_email` | Reply or reply-all with optional HTML body |
| `forward_email` | Forward with optional message, CC/BCC |
| `manage_drafts` | Create (with signature and attachments), list, send, and delete drafts |
| `create_rich_email_draft` | Build a rich HTML `.eml` file and open it in Mail, or save it as a real draft (`save_as_draft=True`) |

### Attachments
| Tool | Description |
|------|-------------|
| `list_email_attachments` | List attachments and inline pictures, by message ID (with part numbers) or by subject |
| `read_email_attachment` | Read an attachment or inline picture: pictures as images, PDFs and text files as text |
| `save_email_attachment` | Save an attachment to disk, by message ID and part or by subject and name |

### Smart Inbox
| Tool | Description |
|------|-------------|
| `get_awaiting_reply` | Find sent emails that haven't received a reply |
| `get_needs_response` | Identify emails that likely need your response |
| `get_top_senders` | Analyse most frequent senders by count or domain |

### Analytics & Export
| Tool | Description |
|------|-------------|
| `get_statistics` | Email analytics (volume, top senders, read ratios) |
| `export_emails` | Export single emails or mailboxes to TXT/HTML |
| `inbox_dashboard` | Interactive UI dashboard (requires mcp-ui-server) |

## Configuration

### Read-Only Mode

Pass `--read-only` to restrict the server to reading mail, saving attachments and creating, listing or deleting drafts. It is an allowlist: only these tools are registered, and any other tool, including one added in a future version, is removed at startup and refused at call time with a "blocked by --read-only" error.

| Allowed under `--read-only` | Tools |
|---|---|
| Read | `list_accounts`, `list_account_addresses`, `list_mailboxes`, `list_inbox_emails`, `get_mailbox_unread_counts`, `get_inbox_overview`, `search_emails`, `get_email_thread`, `get_email_source`, `read_email`, `list_email_attachments`, `read_email_attachment`, `search_mail_index`, `get_statistics`, `get_top_senders`, `get_awaiting_reply`, `get_needs_response`, `inbox_dashboard` |
| Write local files only | `save_email_attachment`, `export_emails`, `index_mail` (its own cache) |
| Fetch new mail | `synchronize_account` |
| Drafts | `create_rich_email_draft`, `manage_drafts` with `action` `create`, `list` or `delete` only. `delete` removes one draft: it refuses when more than one draft matches `draft_subject` and lists them, so pass `draft_id` (shown by `list`) |

Blocked: `compose_email`, `reply_to_email`, `forward_email`, `move_email`, `update_email_status`, `manage_trash` (every action), `create_mailbox`, and `manage_drafts` `send` and `open`.

#### How drafts are made

On macOS 27 Mail stores any body that a script puts into an outgoing message as "shared content": the HTML gets an `Apple-Mail-URLShareWrapperClass` blockquote and, when the draft is sent, every line of the plain-text part starts with "> ". That happens with `content`, `set content`, `make new paragraph` and scripted attachments alike. A compose window made with `visible:false` can also never be closed by script and keeps saving its draft until Mail quits.

So `manage_drafts` `create` (and `compose_email` `mode="draft"`, and `create_rich_email_draft` `save_as_draft=True`) never script a body. The server builds the whole message itself (text and HTML, the account's own Mail signature, attachments), saves it as a private `.eml` file, and lets Mail turn it into a draft:

1. Mail opens the `.eml` in the background (a read-only viewer) and comes to the front for a few seconds.
2. The server presses **Message › Send Again** on that viewer, only after checking that the menu item really reads "Send Again" and that the focused window is that viewer (in a compose window the same item is Send). Mail opens an ordinary compose window holding exactly that message.
3. It presses **File › Save** on that compose window, again only after checking it is the focused window, and closes it. Then it closes the viewer and gives the front back to the app that had it.
4. It reads the stored draft back from disk and checks it: no quote wrapper, the body text, the sender, each attachment's SHA-256, and that nothing with that subject appeared in Sent or Outbox.

Only accessibility presses are used, never typed keystrokes, so nothing can land in another app. If a check fails before a press, nothing is pressed; the `.eml` file is kept and its path returned. This needs **Accessibility** permission for the app that runs the server (System Settings › Privacy & Security › Accessibility). Don't type while Mail is in front. Attachments must be in your home folder or a temp folder (not `~/.ssh`, `~/.config`, Mail's own store and similar), at most 10 files and 25 MB.

The signature is the one Mail uses for the account (`~/Library/Mail/V10/MailData/Signatures`, chosen per account in Mail's settings). It is left out when the account has several signatures and none is selected, or with `include_signature=False`.

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/path/to/venv/bin/python3",
      "args": ["/path/to/apple_mail_mcp.py", "--read-only"]
    }
  }
}
```

### User Preferences (Optional)

Set the `USER_EMAIL_PREFERENCES` environment variable to give the assistant context about your workflow:

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/path/to/venv/bin/python3",
      "args": ["/path/to/apple_mail_mcp.py"],
      "env": {
        "USER_EMAIL_PREFERENCES": "Default to BCG account, show max 50 emails, prefer Archive and Projects folders"
      }
    }
  }
}
```

For `.mcpb` installs, configure this in Claude Desktop under **Developer > MCP Servers > Apple Mail MCP**.

### Safety Limits

Batch operations have conservative defaults to prevent accidental bulk actions:

| Operation | Default Limit |
|-----------|---------------|
| `update_email_status` | 10 emails |
| `manage_trash` | 5 emails |
| `move_email` | 1 email |

Override via function parameters when needed.

### Attachments and inline pictures

Mail often keeps a downloaded message as a `.partial.emlx` whose attachment parts are empty; the bytes are in `Attachments/<message id>/<part>/` next to it. `list_email_attachments(message_id=...)`, `read_email_attachment` and `save_email_attachment(message_id=..., part=...)` read both places directly (and ask Mail to save the file only when it is not on disk yet), so they work for any message by ID, in any mailbox, including pictures embedded in the body. `read_email_attachment` returns pictures as images (HEIC, TIFF and very large pictures are converted to JPEG locally with `sips`), PDFs as text (PDFKit; a scanned PDF without a text layer returns little text), and text-like files as text.

### Local search index

`index_mail` builds a full-text index of every account's Inbox and Sent from Mail's own database and message files: no AppleScript for bodies, nothing sent anywhere. It is stored in `~/Library/Caches/apple-mail-mcp/mail-index.sqlite3` (folder 0700, file 0600; `APPLE_MAIL_MCP_INDEX_PATH` moves it) and holds message text, so `index_mail(delete_index=True)` removes it. The newest mail is indexed first in batches, so a first run that hits its time budget (`max_seconds`, default 120) continues on the next run; later runs only add what is new, and `search_mail_index` tops it up by itself (a check costs about 10 ms when nothing is new; deletions and moves are picked up by a full comparison every 10 minutes).

Measured on a Mac with five accounts: 48,226 Inbox and Sent messages indexed in about 2.5 minutes, about 280 MB on disk; searches take 2–40 ms, against 4–15 s per mailbox (up to 100 s across accounts) for Mail's own search through AppleScript.

Queries: words are ANDed and matched whole, case-insensitively; å, ä and ö are kept as letters of their own. `offer*` is a prefix, `"two words"` a phrase, and `subject:`, `from:`, `to:`, `attachment:` or `body:` limit a word to one field. Filters: `folder`, `account`, `sender`, `date_from`/`date_to`, `has_attachment`; `sort="relevance"` ranks by match instead of date.

## Usage Examples

```
Show me an overview of my inbox
Search for emails about "project update" in my Gmail
Reply to the email about "Domain name" with "Thanks for the update!"
Move emails with "invoice" in the subject to my Archive folder
Show me email statistics for the last 30 days
Create a rich HTML draft for a weekly update and open it in Mail
```

### Rich HTML Drafts

Use `create_rich_email_draft` when you need a visually formatted email, newsletter, or leadership update.

- It generates an unsent `.eml` file with multipart plain-text + HTML bodies (a new private file under `~/Library/Caches/apple-mail-mcp/rich-drafts/` unless `output_path`, an `.eml` path under your home or a temp folder, is given)
- It can open that file in Mail (a read-only viewer; Message › Send Again makes an editable copy)
- With `save_as_draft=True` it saves a real draft in Drafts instead, exactly like `manage_drafts` `create`
- It accepts partial details, so you can start with just an account and subject and fill in the rest later

This is more reliable than injecting raw HTML into AppleScript `content`, which Mail often stores as literal markup.

## Email Management Skill

A companion [Claude Code Skill](plugin/skills/email-management/) is included that teaches Claude expert email workflows (Inbox Zero, daily triage, folder organization). When installed as a plugin, the skill is loaded automatically. For standalone MCP installs, copy it manually:

```bash
cp -r plugin/skills/email-management ~/.claude/skills/email-management
```

## Requirements

- macOS with Apple Mail configured
- Python 3.7+
- `fastmcp` (+ optional `mcp-ui-server` for dashboard)
- Claude Desktop or any MCP-compatible client
- Mail.app permissions: Automation + Mail Data Access (grant in **System Settings > Privacy & Security > Automation**)
- Recommended: **Full Disk Access** for the app that launches the server (Terminal, Claude, …). Message bodies are read from Mail's on-disk `.emlx` files under `~/Library/Mail`; without access the server asks Mail for the raw `source` instead, which is slower. It never asks Mail for rendered `content`, because that makes Mail convert HTML through WebKit on its main thread and blocks every other client. Listings, counts and searches read Mail's `Envelope Index` database read-only for the same reason (asking Mail for every message of a large mailbox runs a full query on its main thread); without access they use AppleScript as before. Set `APPLE_MAIL_MCP_NO_INDEX=1` to always use AppleScript.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Mail.app not responding | Ensure Mail.app is running; check Automation permissions in System Settings |
| Slow searches | Set `include_content: false` and lower `max_results` |
| Mailbox not found | Use exact folder names; nested folders use `/` separator (e.g., `Projects/Alpha`) |
| Permission errors | Grant access in **System Settings > Privacy & Security > Automation** |
| Rich draft shows raw HTML | Pass the HTML as `html_body` to `manage_drafts` `create` |
| Sent mail arrives with every line quoted ("> ") | The draft was made by a script setting `content` (older versions of this server). Drafts made now go through Send Again, see "How drafts are made" |
| "Mail did not create the draft" | The reason is given; usually another window took the focus, or the app running the server lacks Accessibility permission. The finished `.eml` is kept and its path returned |
| Slow mail searches | Run `index_mail` once and use `search_mail_index` |

## Project Structure

```
apple-mail-mcp/
├── .claude-plugin/
│   └── marketplace.json       # Marketplace manifest (for plugin distribution)
├── plugin/                    # Claude Code plugin
│   ├── .claude-plugin/
│   │   └── plugin.json        # Plugin manifest
│   ├── skills/                # Email Management Expert skill (also invocable as /email-management)
│   ├── apple_mail_mcp/        # Python MCP server package (31 tools)
│   ├── apple_mail_mcp.py      # Entry point
│   ├── start_mcp.sh           # Startup wrapper (auto-creates venv)
│   └── requirements.txt
├── apple-mail-mcpb/           # MCPB build files (Claude Desktop)
├── LICENSE
└── README.md
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit and push
4. Open a Pull Request

## Releasing

Follow these steps when cutting a new release to keep all version strings in sync:

1. Bump `__version__` in `plugin/apple_mail_mcp/__init__.py` to the new version.
2. Update `apple-mail-mcpb/manifest.json` (`"version"` field) to match.
3. Update `pyproject.toml` (`version = ...` under `[project]`) to match.
4. Update both version fields in `server.json` (`"version"` and `"packages"[0]["version"]`) to match.
5. Run `python3 scripts/check_versions.py` — it must print **OK** before you proceed.
6. Commit, tag, and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
7. Create a GitHub release from the tag.
8. Build and publish to PyPI: `python -m build && twine upload dist/*` (requires PyPI credentials).

> The test suite also enforces version consistency (`tests/test_version_consistency.py`), so any CI run on a branch with mismatched versions will fail fast.

## License

MIT -- see [LICENSE](LICENSE).

## Links

- [Changelog](CHANGELOG.md)
- [Issues](https://github.com/patrickfreyer/apple-mail-mcp/issues)
- [Discussions](https://github.com/patrickfreyer/apple-mail-mcp/discussions)
- [FastMCP](https://github.com/jlowin/fastmcp)
- [Model Context Protocol](https://modelcontextprotocol.io)
