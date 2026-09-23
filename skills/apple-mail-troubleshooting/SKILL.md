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
- Reading a message body (`content`) takes 1 to 7 s per message.
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
5. **Startup.** `uvx --from mcp-apple-mail --with 'mcp<2' mcp-apple-mail`
   takes about 1 s warm and about 15 s on a cold uv cache. The `mcp<2` pin is
   required, because the server imports `mcp.server.fastmcp`.
6. **Measure.** From a checkout of this repository:
   `python3 scripts/mcp_stdio_client.py --call list_accounts '{}' -- uvx --from mcp-apple-mail --with 'mcp<2' mcp-apple-mail --read-only`
   prints the startup time, `tools/list` time and per-call time without
   printing mail content.

## Known fixed causes (this fork)

Earlier versions walked whole mailboxes one message at a time and forked a
shell per lowercase conversion. Measured on a 7,018-message inbox, these
calls timed out or took 70 to 300 s: `list_inbox_emails()` with its old
"all messages" default, `get_awaiting_reply`, `get_email_thread`,
`get_statistics`, `move_email`, `save_email_attachment` and
`search_emails(body_text=...)`. They now use whose-clause filters and bulk
property fetches.

Mail itself is still the limit. For a few minutes after Mail launches, or
while an account syncs (including right after `synchronize_account`), calls
on that account can be 10 to 100 times slower. Retry after the sync, or work
on another account in the meantime. The `apple-mail-inbox-triage`
skill shows the fast calls to use.
