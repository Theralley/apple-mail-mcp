---
name: apple-mail-attachments
description: Find, read and save email attachments and inline pictures from Apple Mail through the Apple Mail MCP server. Use when the user wants a file someone emailed them, such as an invoice PDF, a contract or a spreadsheet, asks what a PDF or picture in an email says or shows, asks what attachments a message has, or asks to save an attachment to disk.
---

# Read or save an Apple Mail attachment

## Workflow

1. **Find the message and its ID.** Fastest:
   `search_mail_index(query="attachment:<name or word> <other words>")`
   (run `index_mail()` once first if it says there is no index). Or
   `search_emails(account="<name>", subject_keyword="<keyword>", output_format="json", limit=10)`.
   Check sender and date so it is the message the user means.
2. **List its parts.** `list_email_attachments(message_id=<id>)` lists every
   attachment and inline picture with its part number, type and size, and
   says when one is not downloaded yet.
3. **Read it** when the user wants to know what it says or shows:
   `read_email_attachment(message_id=<id>, part="<part>")`. Pictures come back
   as images, PDFs and text files as text. Docx, xlsx and zip cannot be read
   here; save them instead.
4. **Save it** when the user wants the file. Ask where if they have not said,
   then `save_email_attachment(message_id=<id>, part="<part>", save_path="~/Downloads/")`.
   A folder keeps the attachment's own name. It refuses paths outside the
   home folder, dotfile folders such as `~/.ssh`, and existing files unless
   `overwrite=true`.
5. **Confirm.** Report what you read or the saved path.

## Rules

- Prefer `message_id` over `subject_keyword`. The subject form searches the
  inbox only and takes the first message whose subject contains the words.
- A scanned PDF has little or no text layer; the tool says so. Save it and
  let the user open it instead of guessing its content.
- Attachments can contain personal or financial data. Quote only what the
  user asked for, and save files only where the user asked.
