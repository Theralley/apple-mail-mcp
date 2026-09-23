"""FastMCP server instance and user preferences."""

import os
from typing import Optional
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Apple Mail MCP")

# Load user preferences from environment
USER_PREFERENCES = os.environ.get("USER_EMAIL_PREFERENCES", "")

# Read-only mode flag — set via --read-only CLI argument. Always read it at
# call time as ``server.READ_ONLY``: the tool modules are imported before
# main() sets it, so a value imported at module level is always False.
READ_ONLY = False

# Tools registered under --read-only. This is an allowlist: any tool not named
# here (including tools added later) is removed at startup and refused at call
# time. Read-only means: read mail, save attachments / exports to local files,
# fetch from the server, and create or list drafts — nothing that sends,
# moves, flags, marks, deletes or creates mailboxes.
READ_ONLY_ALLOWED_TOOLS = frozenset({
    # Reading
    "list_accounts",
    "list_account_addresses",
    "list_mailboxes",
    "list_inbox_emails",
    "get_mailbox_unread_counts",
    "get_inbox_overview",
    "search_emails",
    "get_email_thread",
    "get_email_source",
    "list_email_attachments",
    "get_statistics",
    "get_top_senders",
    "get_awaiting_reply",
    "get_needs_response",
    "inbox_dashboard",
    # Local files only
    "save_email_attachment",
    "export_emails",
    # Fetches new mail; changes nothing on the server
    "synchronize_account",
    # Drafts (manage_drafts only for READ_ONLY_DRAFT_ACTIONS)
    "create_rich_email_draft",
    "manage_drafts",
})

READ_ONLY_DRAFT_ACTIONS = frozenset({"create", "list"})


def read_only_block(tool_name: str, action: Optional[str] = None) -> Optional[str]:
    """Return an error message if --read-only forbids this call, else None.

    Defence in depth for the startup allowlist: denied tools are not even
    registered under --read-only, but each mutating tool also checks here.
    """
    if not READ_ONLY:
        return None
    if tool_name not in READ_ONLY_ALLOWED_TOOLS:
        return f"Error: {tool_name} is blocked by --read-only."
    if tool_name == "manage_drafts" and action not in READ_ONLY_DRAFT_ACTIONS:
        allowed = ", ".join(sorted(READ_ONLY_DRAFT_ACTIONS))
        return (
            f"Error: manage_drafts action '{action}' is blocked by --read-only "
            f"(allowed: {allowed})."
        )
    return None
