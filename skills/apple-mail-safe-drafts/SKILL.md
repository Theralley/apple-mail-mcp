---
name: apple-mail-safe-drafts
description: Compose email safely as Apple Mail drafts through the Apple Mail MCP server; the user reviews and sends from Mail. Use when the user asks to write, draft or prepare an email or a reply, or when another workflow wants to suggest a reply for the user to review.
---

# Safe draft composition

Only a draft comes out of this skill. The user reviews, edits, sends or
deletes it in Mail. Under `--read-only` the server allows only
`manage_drafts` actions `create` and `list`, plus `create_rich_email_draft`.
Sending, replying, forwarding, and opening or deleting drafts are all blocked.

## Workflow

1. **Context.** If this is a reply, read the thread first with
   `get_email_thread(account="<name>", subject_keyword="<subject>")` so the
   draft answers what was asked.
2. **Sender identity.** `list_account_addresses()` shows which addresses each
   account can send from. Pass one as `from_address` only if the user wants a
   specific alias.
3. **Show the text first.** Put the recipients, subject and full body in chat
   before you create anything, unless the user already dictated them.
4. **Create the draft.**
   - Plain text: `manage_drafts(account="<name>", action="create", subject="...", to="...", body="...")`.
   - Formatted or HTML: `create_rich_email_draft(account="<name>", subject="...", to="...", html_body="...", open_in_mail=true)`.
     This writes an unsent `.eml` file and opens it in Mail. It is not saved
     to Drafts.
5. **Confirm.** Call `manage_drafts(account="<name>", action="list")` to check
   the draft exists. Then tell the user it is in the account's Drafts folder
   for them to review and send.

## Rules

- Never send. If the user asks you to send, reply or forward, tell them to
  send the draft from Mail. The read-only server has no tool that sends.
- Do not try to delete or tidy up drafts; the user does that in Mail.
- Do not put secrets, passwords or data the user has not approved into a
  draft. Drafts sync to the mail server.
