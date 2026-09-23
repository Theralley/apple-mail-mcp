#!/usr/bin/env python3
"""Minimal stdio JSON-RPC client for timing and exercising this MCP server.

Stdlib only, so it runs under any Python 3.9+. It launches the server command,
performs the MCP handshake, and optionally calls tools, printing wall-clock
timings. Tool *outputs are not printed* by default (they contain real mail);
only their size and an error flag are reported. Use --show to print them.

Examples:
    # Startup + tools/list timing for the published package
    python3 scripts/mcp_stdio_client.py -- uvx --from mcp-apple-mail \
        --with 'mcp<2' mcp-apple-mail --read-only

    # Call tools against the local checkout
    python3 scripts/mcp_stdio_client.py \
        --call list_accounts '{}' \
        --call get_mailbox_unread_counts '{"summary_only": true}' \
        -- uv run --directory . mcp-apple-mail --read-only
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time


class McpStdioClient:
    def __init__(self, cmd, cwd=None, env=None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
        )
        self._next_id = 1
        self._buf = b""

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_message(self, deadline):
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no response before deadline")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError("server closed stdout")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line) if line.strip() else None

    def request(self, method, params=None, timeout=300.0):
        req_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.monotonic() + timeout
        while True:
            resp = self._read_message(deadline)
            if resp and resp.get("id") == req_id:
                return resp

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def initialize(self, timeout=120.0):
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-stdio-client", "version": "0"},
            },
            timeout=timeout,
        )
        self.notify("notifications/initialized")
        return resp

    def list_tools(self, timeout=60.0):
        return self.request("tools/list", {}, timeout=timeout)["result"]["tools"]

    def call_tool(self, name, arguments, timeout=300.0):
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def result_text(resp):
    """Return (is_error, text) for a tools/call response."""
    if "error" in resp:
        return True, json.dumps(resp["error"])
    result = resp.get("result", {})
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return bool(result.get("isError")), "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--call", nargs=2, action="append", default=[],
                        metavar=("TOOL", "JSON_ARGS"))
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="per-request timeout in seconds")
    parser.add_argument("--show", action="store_true",
                        help="print tool output (contains real mail data)")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("server command required after --")

    t0 = time.monotonic()
    client = McpStdioClient(cmd)
    try:
        client.initialize(timeout=args.timeout)
        t_init = time.monotonic() - t0
        print(f"initialize: {t_init:.2f}s")
        t1 = time.monotonic()
        tools = client.list_tools(timeout=args.timeout)
        print(f"tools/list: {time.monotonic() - t1:.2f}s ({len(tools)} tools)")
        for name, raw in args.call:
            t2 = time.monotonic()
            try:
                resp = client.call_tool(name, json.loads(raw), timeout=args.timeout)
                is_err, text = result_text(resp)
                status = "ERROR" if is_err else "ok"
                print(f"{name}: {time.monotonic() - t2:.2f}s {status} ({len(text)} chars)")
                if args.show or is_err:
                    print(text[:4000] if not args.show else text)
            except TimeoutError:
                print(f"{name}: TIMEOUT after {time.monotonic() - t2:.2f}s")
                break
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
