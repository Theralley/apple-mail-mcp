---
name: apple-mail-troubleshooting
description: Diagnose a slow, timing-out or failing Apple Mail MCP server. Use when Apple Mail tools time out, return "AppleScript execution timed out", return empty results, fail to start, report Automation or permission errors, or when the user says Apple Mail "always times out".
---

# Troubleshoot the Apple Mail MCP server

## How the server talks to Mail

Every tool runs an AppleScript through `osascript`. Mail answers each
property read (subject, sender, date and so on) as a separate Apple Event and
handles one event at a time, for all clients. So cost grows with the number
of messages a call touches:

- A per-message property read costs about 10 to 50 ms; far more while Mail is
  busy.
- Fetching one property for a whole mailbox in one event takes about 1 s for
  7,000 messages when Mail is warm. The first access to a large mailbox after
  Mail launches can take 45 s.
- Message bodies are read from Mail's `.emlx` files on disk, which is fast
  and does not touch Mail. The server never asks Mail for `content`, which
  renders HTML on Mail's main thread and can freeze or crash it. Without
  Full Disk Access it falls back to Mail's raw `source` of each message,
  which is slower.
- Listings, counts and searches read Mail's `Envelope Index` database
  (read-only) instead of asking Mail for every message. Without Full Disk
  Access, or when the database looks different, the server logs
  `not using Mail's Envelope Index (...)` to stderr once and uses
  AppleScript, which is slow on large mailboxes.
- The server kills a call after 120 s (180 s for `search_emails`) and returns
  `AppleScript execution timed out after Ns ...`.

## Checklist

1. **Is Mail up?** `list_accounts()` should answer in about 1 s. If it hangs,
   Mail is blocked: a modal dialog, a first-launch sync, or another client's
   long call. Wait, or ask the user to look at Mail.
2. **Narrow the call** instead of retrying it unchanged. Pass `account=`, a
   specific `mailbox`, `days_back` or `date_from`, and small
   `max_emails`/`max_results`/`limit`. Avoid `include_content=true`, and use
   `body_text` searches only with a date range.
3. **Other clients.** Every Claude, Codex or agent session starts its own
   server process, and they all queue on the same Mail. One heavy call blocks
   the rest. `pgrep -fl mcp-apple-mail` lists them; `pgrep -fl osascript`
   shows scripts in flight.
4. **Permissions.** An error that mentions `-1743`, or "Not authorized to
   send Apple events", means the host app (Terminal, iTerm, Claude, Codex) is
   missing the Automation permission for Mail. The user grants it under
   System Settings > Privacy & Security > Automation. Saving attachments
   needs file access to the target folder.
5. **Startup.** `uvx --from mcp-apple-mail mcp-apple-mail` takes about 1 s
   warm and about 15 s on a cold uv cache. Since 3.2.1 the server imports
   `FastMCP` from `fastmcp`, so no `mcp<2` pin is needed (an older install
   that fails with `No module named 'mcp.server.fastmcp'` needs an update).
   After an update, or when the tools vanish or fail with "server closed",
   reconnect the session; the `apple-mail-mcp` skill has the steps.
6. **Measure.** From a checkout of this repository:
   `python3 scripts/mcp_stdio_client.py --call list_accounts '{}' -- uvx --from mcp-apple-mail mcp-apple-mail --read-only`
   prints the startup time, `tools/list` time and per-call time without
   printing mail content.

## "Blocked by --read-only"

With `--read-only` the server registers only an allowlist:
- every read tool, from listing and searching to the thread and raw-source
  views, statistics, follow-ups and the dashboard;
- `save_email_attachment` and `export_emails`, which write local files only;
- `synchronize_account`, which fetches new mail;
- `index_mail`, which builds the local search index;
- `create_rich_email_draft`, and `manage_drafts` with `action` `create`,
  `list` or `delete`;
- `set_email_read_status`, which changes only the read flag, by message ID.

Everything else is not registered, and it is refused at call time with
"... is blocked by --read-only". That covers sending, replying, forwarding,
moving, flagging, filter-based marking, trash, creating mailboxes, and
sending or opening drafts. It is intended behaviour. The user does those in
Mail, or runs a server without `--read-only`.

## Known fixed causes (this fork)

Earlier versions walked whole mailboxes one message at a time and forked a
shell per lowercase conversion. Measured on a 7,018-message inbox, these
calls timed out or took 70 to 300 s: `list_inbox_emails()` with its old
"all messages" default, `get_awaiting_reply`, `get_email_thread`,
`get_statistics`, `save_email_attachment` and
`search_emails(body_text=...)`. They now use whose-clause filters and bulk
property fetches.

Mail itself is still the limit. For a few minutes after Mail launches, or
while an account syncs (including right after `synchronize_account`), calls
on that account can be 10 to 100 times slower. Retry after the sync, or work
on another account in the meantime. The `apple-mail-inbox-triage`
skill shows the fast calls to use.

## Deleting from the trash, and scripted selections

- **Nothing can erase mail by script.** Mail's AppleScript `delete` means
  "move to the trash". On a message that is already in the trash (Trash,
  Deleted Items, Deleted Messages, Bin) it returns without an error and
  does nothing. On an Exchange account a batch of 23 such deletes made Mail
  abort (SIGABRT in EmailDaemon `deleteMessages` -> `transferMessages`;
  macOS 27, 2026-11-14). No data changed, but Mail had to relaunch.
  `manage_trash` therefore never sends `delete` to the trash:
  `empty_trash` counts it, `delete_permanent` lists the matches, and both
  say how to erase them in Mail (Mailbox > Erase Deleted Items, or select
  the messages in the trash and delete them there). `move_to_trash` refuses
  a source that is the trash, and `move_email` refuses a move into the
  mailbox the messages are already in. Do not work around this with
  hand-written AppleScript.
- **A deleted draft lands in the trash.** `manage_drafts(action="delete")`
  moves the draft to the account's trash, where it stays until the user
  erases it or the server's retention policy removes it. Test and probe
  drafts accumulate there the same way; list them for the user instead of
  trying to erase them.
- **Never act on a scripted selection without checking it.** `set selected
  messages of message viewer 1 to {...}` in a threaded viewer selected one
  unrelated, real message and missed one of 23 targets. Before any menu or
  key action on a selection, read `selected messages` back and compare
  every ID and subject with the intended set. Stop when they differ.
