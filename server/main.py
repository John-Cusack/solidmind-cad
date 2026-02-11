from __future__ import annotations

import argparse
import sys
from typing import Any

from server.jsonutil import dumps as json_dumps
from server.jsonutil import loads as json_loads
from server.prompts import get_prompt, list_prompts
from server.resources import list_resources, read_resource
from server.tools import (
    spec_apply_answer,
    spec_export_brief,
    spec_export_rfq_summary,
    spec_finalize,
    spec_next_question,
    spec_select_schema,
    spec_validate,
)


def _json_dumps(obj: Any) -> bytes:
    return json_dumps(obj)


def _send(msg: dict[str, Any]) -> None:
    payload = _json_dumps(msg)
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _read_message() -> dict[str, Any] | None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            continue
        if line.lower().startswith(b"content-length:"):
            # LSP-style framing: read headers until blank line, then body.
            headers = {}
            while line not in (b"\r\n", b"\n"):
                k, _, v = line.partition(b":")
                headers[k.strip().lower()] = v.strip()
                line = stdin.readline()
                if not line:
                    return None
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                return None
            body = stdin.read(length)
            if not body:
                return None
            return json_loads(body)

        # Fallback: assume newline-delimited JSON for manual debugging.
        return json_loads(line.decode("utf-8").strip())


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _rpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _tool_list() -> list[dict[str, Any]]:
    # Minimal, stable tool registry for hosts. Tool schemas can be expanded later.
    return [
        {
            "name": "spec.select_schema",
            "description": "Select schema/question bank and coverage threshold for a process+maturity.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "process": {"type": "string"},
                    "maturity_level": {"type": "string"},
                    "spec_version": {"type": "string"},
                },
                "required": ["process", "maturity_level", "spec_version"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spec.apply_answer",
            "description": "Atomically mutate spec_draft via JSON Pointer + op (set|append|remove).",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.validate",
            "description": "Validate shape + compute coverage + run deterministic rules.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.next_question",
            "description": "Deterministically select the next best question (skip-aware).",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.finalize",
            "description": "Freeze spec (strip internals), compute deterministic hash, changelog, provenance.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.export_brief",
            "description": "Export a CAD/design brief as Markdown from a finalized spec.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "spec.export_rfq_summary",
            "description": "Export an RFQ-ready summary as Markdown from a finalized spec.",
            "inputSchema": {"type": "object"},
        },
    ]


def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "spec.select_schema":
        return spec_select_schema(**arguments)
    if name == "spec.apply_answer":
        return spec_apply_answer(**arguments)
    if name == "spec.validate":
        return spec_validate(**arguments)
    if name == "spec.next_question":
        return spec_next_question(**arguments)
    if name == "spec.finalize":
        return spec_finalize(**arguments)
    if name == "spec.export_brief":
        return spec_export_brief(**arguments)
    if name == "spec.export_rfq_summary":
        return spec_export_rfq_summary(**arguments)
    raise KeyError(f"Unknown tool: {name}")


def serve() -> int:
    while True:
        msg = _read_message()
        if msg is None:
            return 0

        rpc_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        # Notifications: ignore (no id).
        if rpc_id is None:
            continue

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "0.1",
                    "serverInfo": {"name": "mcp-spec-gatherer", "version": "0.1.0"},
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                }
                _send(_rpc_result(rpc_id, result))
                continue

            if method == "tools/list":
                _send(_rpc_result(rpc_id, {"tools": _tool_list()}))
                continue

            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    _send(_rpc_result(rpc_id, {"isError": True, "content": [{"type": "text", "text": "Invalid params"}]}))
                    continue
                out = _call_tool(name, arguments)
                _send(_rpc_result(rpc_id, {"isError": False, "content": [{"type": "json", "json": out}]}))
                continue

            if method == "resources/list":
                _send(_rpc_result(rpc_id, {"resources": list_resources()}))
                continue

            if method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str):
                    _send(_rpc_error(rpc_id, -32602, "Invalid params"))
                    continue
                content = read_resource(uri)
                _send(_rpc_result(rpc_id, {"contents": [content]}))
                continue

            if method == "prompts/list":
                _send(_rpc_result(rpc_id, {"prompts": list_prompts()}))
                continue

            if method == "prompts/get":
                name = params.get("name")
                if not isinstance(name, str):
                    _send(_rpc_error(rpc_id, -32602, "Invalid params"))
                    continue
                _send(_rpc_result(rpc_id, {"prompt": get_prompt(name)}))
                continue

            _send(_rpc_error(rpc_id, -32601, f"Method not found: {method}"))
        except Exception as e:
            _send(_rpc_error(rpc_id, -32603, f"Internal error: {e}"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MCP spec gatherer (CNC + 3D print) JSON-RPC server over stdio.")
    parser.add_argument("--serve", action="store_true", help="Run the stdio server (default).")
    args = parser.parse_args(argv)

    raise SystemExit(serve())


if __name__ == "__main__":
    main()
