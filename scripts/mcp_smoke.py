"""Spawn the MCP server over stdio and exercise it like OpenCode would.

Usage: uv run python scripts/mcp_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def send(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def read_one(proc: subprocess.Popen, timeout_note: str = "") -> dict:
    line = proc.stdout.readline()
    if not line:
        raise SystemExit(f"server closed stdout unexpectedly {timeout_note}")
    return json.loads(line)


def main() -> int:
    env = dict(os.environ)
    env.setdefault("LB_DATA_DIR", str(ROOT / "data-test"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "lbm.mcp_server"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "lbm-smoke", "version": "1.0"},
        },
    })
    init = read_one(proc, "during initialize")
    server = init.get("result", {}).get("serverInfo", {})
    print(f"initialize     -> {server.get('name')} {server.get('version')}")

    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = read_one(proc, "during tools/list")["result"]["tools"]
    print(f"tools/list     -> {len(tools)} tools: {', '.join(t['name'] for t in tools)}")

    calls = [
        ("archive_status", {}),
        ("search_bookmarks", {"query": "pricing"}),
        ("recent_bookmarks", {"limit": 2}),
        ("list_folders", {}),
        ("top_authors", {"min_bookmarks": 1}),
        ("sql_query", {"sql": "SELECT COUNT(*) AS n FROM posts"}),
        ("check_session", {}),
        ("refresh_bookmarks", {"mode": "quick"}),
    ]
    failures = 0
    for index, (name, arguments) in enumerate(calls, start=3):
        send(proc, {
            "jsonrpc": "2.0", "id": index, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        reply = read_one(proc, f"during {name}")
        if "error" in reply:
            print(f"{name:<18} -> RPC ERROR {reply['error']}")
            failures += 1
            continue
        result = reply["result"]
        content = result.get("content") or []
        if content:
            payload = content[0].get("text", "")
        elif result.get("structuredContent") is not None:
            payload = json.dumps(result["structuredContent"])
        else:
            payload = json.dumps(result)
        preview = payload.replace("\n", " ")[:110]
        flag = "  [isError]" if result.get("is_error") or result.get("isError") else ""
        print(f"{name:<18} -> {preview}{flag}")

    proc.stdin.close()
    proc.wait(timeout=10)
    if proc.returncode != 0:
        print(f"server exit code {proc.returncode}", file=sys.stderr)
        print(proc.stderr.read(), file=sys.stderr)
        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
