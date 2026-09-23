---
name: apple-mail-attachments
description: Find and save email attachments from Apple Mail through the Apple Mail MCP server. Use when the user wants a file someone emailed them, such as an invoice PDF, a contract or a spreadsheet, or asks what attachments a message has, or asks to save an attachment to disk.
---

# Retrieve an Apple Mail attachment

## Workflow

1. **Find the message.** Start with
   `search_emails(account="<name>", subject_keyword="<keyword>", output_format="json", limit=10)`.
   Add `sender=` or `date_from=` to narrow it. Note the exact subject of the
   right message.
2. **List its attachments.**
   `list_email_attachments(account="<name>", subject_keyword="<exact subject fragment>", max_results=3)`
   returns each attachment's name and size in KB. Check that it is the
   message the user means (sender and date) before you save anything.
3. **Save.** Ask where to save if the user has not said, then call
   `save_email_attachment(account="<name>", subject_keyword="<subject fragment>", attachment_name="<file name>", save_path="~/Downloads/<file name>")`.
   `save_path` must be a full file path under the home directory. The tool
   refuses sensitive directories such as `~/.ssh` and `~/.config`.
4. **Confirm.** Report the saved path. Open or read the file only if the user
   asks.

## Rules

- Both attachment tools match on subject substring in the inbox and use the
  first hit. Use a subject fragment unique enough to pick the right message.
- Do not use `search_emails(has_attachments=true)` to find candidates. Mail
  cannot evaluate that filter server-side, and it returns no results. Use
  step 2 instead.
- Attachments can contain personal or financial data. Save them only where
  the user asked.
