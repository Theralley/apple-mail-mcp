"""--read-only is a strict allowlist: read, save attachments, create, list and delete drafts.

The expected tool set is spelled out here independently of
server.READ_ONLY_ALLOWED_TOOLS, so widening the allowlist has to be done
deliberately in both places. The registration test starts the real CLI
(`python -m apple_mail_mcp --read-only`) over stdio; no Mail.app is touched.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_mail_mcp import mcp, server
from apple_mail_mcp.tools import compose as compose_tools
from apple_mail_mcp.tools import manage as manage_tools

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from mcp_stdio_client import McpStdioClient  # noqa: E402

EXPECTED_READ_ONLY_TOOLS = {
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
    "save_email_attachment",
    "export_emails",
    "synchronize_account",
    "create_rich_email_draft",
    "manage_drafts",
}

ALL_TOOLS = sorted(t.name for t in asyncio.run(mcp.list_tools()))


@pytest.fixture(scope="module")
def read_only_tools():
    env = dict(os.environ, PYTHONPATH=str(REPO / "plugin"))
    client = McpStdioClient(
        [sys.executable, "-m", "apple_mail_mcp", "--read-only"], env=env
    )
    try:
        client.initialize(timeout=60)
        yield {t["name"] for t in client.list_tools(timeout=30)}
    finally:
        client.close()


@pytest.fixture
def read_only_flag():
    original = server.READ_ONLY
    server.READ_ONLY = True
    yield
    server.READ_ONLY = original


def test_read_only_registers_exactly_the_allowlist(read_only_tools):
    assert read_only_tools == EXPECTED_READ_ONLY_TOOLS
    assert set(server.READ_ONLY_ALLOWED_TOOLS) == EXPECTED_READ_ONLY_TOOLS


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_each_tool_is_on_the_expected_side(read_only_tools, tool_name):
    assert (tool_name in read_only_tools) == (tool_name in EXPECTED_READ_ONLY_TOOLS)


@pytest.mark.parametrize(
    "call",
    [
        lambda: compose_tools.compose_email(account="Work", to="a@example.com", subject="s", body="b"),
        lambda: compose_tools.reply_to_email(account="Work", subject_keyword="s", reply_body="b"),
        lambda: compose_tools.forward_email(account="Work", subject_keyword="s", to="a@example.com"),
        lambda: manage_tools.move_email(account="Work", to_mailbox="Archive", subject_keyword="s", dry_run=True),
        lambda: manage_tools.update_email_status(account="Work", action="mark_read", subject_keyword="s"),
        lambda: manage_tools.manage_trash(account="Work", action="move_to_trash", subject_keyword="s"),
        lambda: manage_tools.manage_trash(account="Work", action="empty_trash", confirm_empty=True),
        lambda: manage_tools.create_mailbox(account="Work", name="New"),
    ],
    ids=[
        "compose_email", "reply_to_email", "forward_email", "move_email",
        "update_email_status", "manage_trash-move", "manage_trash-empty",
        "create_mailbox",
    ],
)
def test_denied_tools_refuse_at_call_time(read_only_flag, call):
    with patch.object(compose_tools, "run_applescript") as run_compose, \
            patch.object(manage_tools, "run_applescript") as run_manage:
        result = call()
    assert "blocked by --read-only" in result
    run_compose.assert_not_called()
    run_manage.assert_not_called()


@pytest.mark.parametrize("action", ["send", "open", "unknown"])
def test_manage_drafts_refuses_other_actions(read_only_flag, action):
    with patch.object(compose_tools, "run_applescript") as run:
        result = compose_tools.manage_drafts(
            account="Work", action=action, draft_subject="Anything"
        )
    assert "blocked by --read-only" in result
    run.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "list"},
        {"action": "create", "subject": "s", "to": "a@example.com", "body": "b"},
    ],
    ids=["list", "create"],
)
def test_manage_drafts_allows_create_and_list(read_only_flag, kwargs):
    with patch.object(compose_tools, "run_applescript", return_value="ok") as run:
        result = compose_tools.manage_drafts(account="Work", **kwargs)
    assert "blocked" not in result
    run.assert_called_once()


# ---- delete under --read-only: drafts only, never a guess between two -------

def _matches(*rows):
    """The match script's output: OK, then id<TAB>subject<TAB>created per draft."""
    return "OK\n" + "".join(f"{i}\t{subj}\t{created}\n" for i, subj, created in rows)


def test_delete_of_a_unique_match_deletes_that_draft_by_id(read_only_flag):
    with patch.object(
        compose_tools, "run_applescript",
        side_effect=[_matches((42, "Quarterly report", "26 Oct")), "✓ Draft deleted successfully!"],
    ) as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_subject="Quarterly")
    assert "blocked" not in result and "deleted" in result
    assert run.call_count == 2
    assert "whose id is 42" in run.call_args_list[1].args[0]


def test_delete_refuses_when_several_drafts_match_and_lists_them(read_only_flag):
    # 2026-10-26: a draft and its corrected copy shared a subject; the old code
    # deleted whichever came first.
    with patch.object(
        compose_tools, "run_applescript",
        return_value=_matches((41, "Quarterly report", "13:46"), (42, "Quarterly report", "15:20")),
    ) as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_subject="Quarterly")
    assert "2 drafts match" in result and "Nothing was deleted" in result
    assert "ID 41" in result and "ID 42" in result
    run.assert_called_once()  # only the lookup; no delete script ran


def test_delete_by_draft_id_skips_the_subject_search(read_only_flag):
    with patch.object(compose_tools, "run_applescript", return_value="✓ Draft deleted successfully!") as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_id=41)
    assert "deleted" in result
    run.assert_called_once()
    assert "whose id is 41" in run.call_args.args[0]


@pytest.mark.parametrize("bad", ["41; delete every message", -1, "abc"])
def test_delete_refuses_a_draft_id_that_is_not_a_number(read_only_flag, bad):
    with patch.object(compose_tools, "run_applescript") as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_id=bad)
    assert result.startswith("Error: draft_id must be a number")
    run.assert_not_called()


def test_delete_with_no_match_or_a_lookup_error_deletes_nothing(read_only_flag):
    with patch.object(compose_tools, "run_applescript", return_value="OK\n") as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_subject="missing")
    assert "No draft found" in result
    run.assert_called_once()
    with patch.object(compose_tools, "run_applescript", return_value="Error: no such account") as run:
        result = compose_tools.manage_drafts(account="Work", action="delete", draft_subject="x")
    assert result == "Error: no such account"
    run.assert_called_once()

