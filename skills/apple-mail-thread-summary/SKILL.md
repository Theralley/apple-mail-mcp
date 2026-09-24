---
name: apple-mail-thread-summary
description: Find an email conversation in Apple Mail and summarise it through the Apple Mail MCP server. Use when the user asks what was said in a thread, wants the latest on a topic, a person or a project discussed by email, or asks to catch up on a conversation.
---

# Find and summarise an Apple Mail thread

## Workflow

1. **Locate candidates.** Search the headers first; it is fast because Mail
   filters server-side:
   `search_emails(account="<name>", subject_keyword="<topic>", output_format="json", limit=10)`.
   Use `sender="<name or address>"` for a person. Use `date_from="YYYY-MM-DD"`
   to limit it to recent mail. Leave out `account` only when the user does not
   know which account holds the thread (searching every account is slower).
2. **Pull the thread.** Take the distinctive part of the subject, without
   `Re:`/`Fwd:`, and call
   `get_email_thread(account="<name>", subject_keyword="<subject>", max_messages=20)`.
   Pass `mailbox="All"` only if the replies are filed outside the inbox.
3. **Exact detail, if needed.** For headers, recipients or links in one
   message, call
   `get_email_source(account="<name>", message_id="<internet_message_id>", headers_only=true)`
   with the `internet_message_id` from step 1, or pass
   `subject_keyword="<unique subject fragment>"` instead. Mail may have to
   download the raw message first, so this call can take up to a minute. Use
   it only when the thread view is not enough.
4. **Summarise.** Give the participants, a dated timeline of the key points,
   any open questions or commitments, and who is expected to act next. Include
   the `mail_link` (message://) of the latest message so the user can open it.

## Rules

- `body_text` in `search_emails` reads candidate bodies from disk: about 11 s
  for a 2,300-message inbox. Add a date range or sender on bigger mailboxes.
  It stops after two minutes and marks the results as partial.
- Summarise and paraphrase. Do not paste whole bodies back unless the user asks.
