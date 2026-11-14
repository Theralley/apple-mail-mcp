---
name: apple-mail-inbox-triage
description: Triage the Apple Mail inbox through the Apple Mail MCP server. Use when the user asks what is new in their mail, wants an inbox summary, asks "what needs my attention", wants to process unread mail, wants to work through unread mails as tasks and mark each one read when it is done, or asks for inbox zero help across one or more Mail accounts.
---

# Apple Mail inbox triage

Give the user a short, prioritised picture of their inbox without reading
every message. The triage below is read-only: nothing is moved, marked or sent.
Marking mail read is a separate, explicit step (see "Unread mail as tasks").

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

## Unread mail as tasks

When the user hands over a set of unread mails to act on, for example "go
through the unread Railway update mails, fix each one and mark it read once
it is solved":

1. **Find the open ones.** `search_mail_index(query="railway", read_status="unread", folder="inbox")`
   lists unread matches newest first with their message IDs (run
   `index_mail()` once first if it says there is no index). The read state
   is live, so mail the user already opened in Mail is not listed.
2. **Read each one.** `read_email(message_id=<id>)`, plus
   `list_email_attachments` / `read_email_attachment` when it carries a file.
   Reading here does not mark anything read.
3. **Do the task** the mail asks for, with whatever tools the work needs.
4. **Mark only what is solved.**
   `set_email_read_status(message_ids=[<id>, ...], read=true)` for exactly the
   mails whose task is done, and check each line of its report (✓ changed,
   = already in that state, ✗ failed). Leave unsolved ones unread and tell
   the user why. `read=false` puts a mail back to unread.

## Rules

- Do not call `list_inbox_emails` with `max_emails=0` or `include_content=true`
  on a large inbox. Both are slow, and the output is too big to be useful.
- Apart from marking mail read or unread by message ID, this skill only
  reports. The read-only server cannot move, flag or delete mail; suggest
  those actions for the user to do in Mail. Mark a mail read only when its
  task is done or the user asks, never as a side effect of reading it.
- If a call times out, narrow it with `account`, a shorter `days_back`, or a
  smaller `max_emails`. The `apple-mail-troubleshooting` skill covers the rest.
