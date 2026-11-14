---
name: apple-mail-safe-drafts
description: Compose email safely as Apple Mail drafts through the Apple Mail MCP server; the user reviews and sends from Mail. Use when the user asks to write, draft or prepare an email or a reply, or when another workflow wants to suggest a reply for the user to review.
---

# Safe draft composition

Only a draft comes out of this skill. The user reviews, edits, sends or
deletes it in Mail. Under `--read-only` the server allows only
`manage_drafts` actions `create`, `list` and `delete`, plus
`create_rich_email_draft`. Sending, replying, forwarding and opening drafts
are blocked.

## Workflow

1. **Context.** If this is a reply, read the thread first with
   `get_email_thread(account="<name>", subject_keyword="<subject>")`, or find
   the message with `search_mail_index(query="...")` and read it with
   `read_email(message_id=<id>)`, so the draft answers what was asked.
2. **Sender identity.** `list_account_addresses()` shows which addresses each
   account can send from. Pass one as `from_address` only if the user wants a
   specific alias; otherwise the account's first address is used.
3. **Show the text first.** Put the recipients, subject, full body and any
   attachments in chat before you create anything, unless the user already
   dictated them.
4. **Create the draft.**
   `manage_drafts(account="<name>", action="create", subject="...", to="...", body="...")`.
   - Formatted text: add `html_body="..."` (then `body` is the plain-text
     version, or leave it out).
   - Files: `attachments=["~/Documents/Offert.pdf"]` (home or temp folders
     only; at most 10 files, 25 MB).
   - The account's Mail signature is added; pass `include_signature=false`
     if the body already ends with one.
   Tell the user beforehand that Mail comes to the front for a few seconds
   and that they should not type meanwhile.
5. **Read the result.** It says "Draft saved in Drafts (ID n)" only after the
   stored draft was read back and checked. If it reports a problem, relay it
   as given: nothing is retried automatically, and the finished `.eml` path it
   returns can be opened in Mail by the user.

## Rules

- Never send. If the user asks you to send, reply or forward, tell them to
  send the draft from Mail. The read-only server has no tool that sends.
- Create a draft once. Do not retry a failed create in a loop; report the
  reason (for example another window took the focus, or Accessibility
  permission is missing).
- Delete only a draft you just created by mistake, by its `draft_id`; leave
  the user's other drafts alone.
- Do not put secrets, passwords or data the user has not approved into a
  draft. Drafts sync to the mail server.
