---
name: apple-mail-safe-drafts
description: Compose email safely as Apple Mail drafts through the Apple Mail MCP server, never sending without explicit approval. Use when the user asks to write, draft or prepare an email or a reply, or to clean up drafts, or when another workflow wants to suggest a reply for the user to review.
---

# Safe draft composition

The default output is a draft the user reviews in Mail. Sending is a separate
step and needs the user's explicit go-ahead for that specific message.

## Workflow

1. **Context.** If this is a reply, read the thread first with
   `get_email_thread(account="<name>", subject_keyword="<subject>")` so the
   draft answers what was asked.
2. **Sender identity.** `list_account_addresses()` shows which addresses each
   account can send from. Pass one as `from_address` only if the user wants a
   specific alias.
3. **Create the draft.**
   - Plain text: `manage_drafts(account="<name>", action="create", subject="...", to="...", body="...")`.
   - Formatted or HTML: `create_rich_email_draft(account="<name>", subject="...", to="...", html_body="...", open_in_mail=true)`.
     This writes an unsent `.eml` and opens it in Mail. It is not saved to
     Drafts.
4. **Show it.** `manage_drafts(account="<name>", action="list")` confirms the
   draft exists. `manage_drafts(account="<name>", action="open", draft_subject="...")`
   opens it for the user to review.
5. **Clean up** only drafts you created in this session:
   `manage_drafts(account="<name>", action="delete", draft_subject="<unique subject>")`.
   The match is a subject substring, so use a subject that cannot match other
   drafts. Mail keeps the hidden compose object from `action="create"` open
   until Mail quits, and it can save the draft again later. Check with
   `action="list"` and tell the user if a draft comes back.

## Rules

- Never send, reply or forward without the user's explicit instruction for
  that message. When the server runs with `--read-only`, sending is disabled:
  the send tools are not registered and `manage_drafts(action="send")` refuses.
- Put the recipients, subject and full body in chat before you create the
  draft whenever the user has not already dictated them.
- Do not put secrets, passwords or data the user has not approved into a
  draft. Drafts sync to the mail server.
