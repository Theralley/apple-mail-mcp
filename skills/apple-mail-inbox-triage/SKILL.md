---
name: apple-mail-inbox-triage
description: Triage the Apple Mail inbox through the Apple Mail MCP server. Use when the user asks what is new in their mail, wants an inbox summary, asks "what needs my attention", wants to process unread mail, or asks for inbox zero help across one or more Mail accounts.
---

# Apple Mail inbox triage

Give the user a short, prioritised picture of their inbox without reading
every message. Every step below is read-only; nothing is moved, marked or sent
unless the user asks for it afterwards.

## Workflow

1. **Counts first (cheap).** Call `get_mailbox_unread_counts(summary_only=true)`
   for per-account inbox unread totals. Use `list_accounts()` if you need the
   exact account names for later calls.
2. **Unread, newest first.** For each account with unread mail call
   `list_inbox_emails(account="<name>", include_read=false, max_emails=20)`.
   Keep `max_emails` small; the newest messages come first.
3. **Needs a reply.** Call `get_needs_response(account="<name>", days_back=7)`.
   It skips newsletters and no-reply senders and puts flagged mail and
   questions first.
4. **Optional context.** `get_top_senders(account="<name>", days_back=30)`
   shows who fills the inbox. Suggest filters or unsubscribes from that.
5. **Report.** Group the results as: needs reply, FYI, can archive. Quote
   subjects and senders, not bodies.

## Rules

- Do not call `list_inbox_emails` with `max_emails=0` or `include_content=true`
  on a large inbox. Both are slow, and the output is too big to be useful.
- This skill only reports. The read-only server cannot move, flag, mark or
  delete mail. Suggest those actions for the user to do in Mail.
- If a call times out, narrow it with `account`, a shorter `days_back`, or a
  smaller `max_emails`. The `apple-mail-troubleshooting` skill covers the rest.
