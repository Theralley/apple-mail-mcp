---
name: apple-mail-follow-ups
description: Review email follow-ups in Apple Mail through the Apple Mail MCP server. Use when the user asks who has not replied to them, which of their sent emails are still waiting for an answer, what they still owe replies on, or wants a weekly follow-up or needs-response review.
---

# Follow-up and needs-response review

Two lists: mail the user sent that got no reply, and mail the user received
that still needs a reply. Everything here is read-only.

## Workflow

1. **Accounts.** `list_accounts()`. Review the accounts the user cares about.
   Do not scan every account by default.
2. **Waiting on others.** For each account:
   `get_awaiting_reply(account="<name>", days_back=7, max_results=20)`.
   It matches sent subjects against inbox replies from the same recipient in
   the same window. No-reply recipients are excluded by default.
3. **Waiting on the user.** For each account:
   `get_needs_response(account="<name>", days_back=7, max_results=20)`.
   HIGH means flagged or contains a question; NORMAL means direct mail with
   no newsletter or automation signals.
4. **Check an item if unsure.** `get_email_thread(account="<name>", subject_keyword="<subject>")`
   shows whether a reply exists that the subject match missed, for example
   one sent from another account.
5. **Report.** List the two groups with subject, counterpart and age. For the
   oldest waiting items, suggest a nudge. Offer to prepare drafts with the
   `apple-mail-safe-drafts` skill.

## Rules

- Keep `days_back` short (7 to 14). Longer windows are slower and pull in
  noise.
- Matching is heuristic: subject plus sender. A different subject in the
  reply, or a reply from a colleague, shows up as "awaiting". Say so when it
  matters.
