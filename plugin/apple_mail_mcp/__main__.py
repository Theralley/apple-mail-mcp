"""Entry point for `python -m apple_mail_mcp` and `apple-mail-mcp` CLI."""

import argparse
import asyncio
import os
import threading
import time

import apple_mail_mcp.server as server


# Workaround for modelcontextprotocol/python-sdk#526.
# When the MCP client (e.g. Claude) exits without closing stdin cleanly,
# the FastMCP server can keep running orphaned in the background. The
# orphaned server keeps polling Mail.app via Apple Events, which causes
# Mail to relaunch after the user quits it. Capture the initial PPID at
# startup and self-terminate when it changes (parent died, we have been
# reparented). Uses os._exit because sys.exit does not tear down FastMCP's
# background asyncio loop reliably. get_ppid and exit_fn are injectable
# for unit testing.
def _start_orphan_watcher(
    interval_sec: int = 10,
    get_ppid=os.getppid,
    exit_fn=os._exit,
) -> None:
    initial_ppid = get_ppid()

    def _watch() -> None:
        while True:
            if get_ppid() != initial_ppid:
                exit_fn(0)
                return
            time.sleep(interval_sec)

    threading.Thread(target=_watch, daemon=True).start()


def main():
    _start_orphan_watcher()

    parser = argparse.ArgumentParser(description="Apple Mail MCP Server")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Only read mail, save attachments/exports locally, sync, and "
             "create or list drafts. Every other tool is removed.",
    )
    args = parser.parse_args()

    configure(read_only=args.read_only)

    from apple_mail_mcp import mcp  # noqa: E402

    mcp.run()


def configure(read_only: bool) -> None:
    """Set the read-only flag and, when set, keep only allowlisted tools."""
    server.READ_ONLY = read_only
    if not read_only:
        return

    from apple_mail_mcp import mcp  # noqa: E402

    for tool in asyncio.run(mcp.list_tools()):
        if tool.name not in server.READ_ONLY_ALLOWED_TOOLS:
            mcp.remove_tool(tool.name)


if __name__ == "__main__":
    main()
