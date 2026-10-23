"""The Model Context Protocol over plain HTTP: the small JSON-RPC subset a read-only tool server needs.

Streamable HTTP lets a server answer each POST with one JSON body; there is no session to
keep, nothing to stream and no server-initiated message, so this is stateless and every
request stands alone. Pure: a message and its headers in, a status and a reply out. The
caller owns the transport, the sign-in and the database; the tools live in mcptools.py.

Two eras are served on the one endpoint. The modern one (revision 2026-07-28) puts the
protocol version in every request's `_meta` and an `MCP-Protocol-Version` header, mirrors the
method and tool name into `Mcp-Method` and `Mcp-Name`, and has `server/discover` instead of
`initialize`. The legacy ones (2025-11-25 and earlier) open with `initialize`. A request that
carries the modern `_meta` is modern; anything else is legacy.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Callable, Mapping

LOG = logging.getLogger("cc.mcp")

VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VERSIONS = MODERN + LEGACY

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, BAD_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022


class ToolError(Exception):
    """A tool that could not answer, in words for the model to read: it comes back as a
    result flagged isError, not as a protocol error, so the model can correct itself."""


class Tool:
    """One tool: its name, what it does, its JSON-schema arguments (`required` names the ones that
    must be sent) and the function that runs it (`run(arguments, scope)` returns anything JSON can hold)."""

    def __init__(self, name: str, description: str, schema: dict, run: Callable[[dict, object], object], required: tuple[str, ...] = ()):
        self.name, self.description, self.schema, self.run, self.required = name, description, schema, run, required

    def listing(self) -> dict:
        schema = {"type": "object", "properties": self.schema, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
            # every tool here only reads
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        }


Reply = tuple[int, "dict | None"]


def _error(status: int, ident, code: int, message: str, data=None) -> Reply:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return status, {"jsonrpc": "2.0", "id": ident, "error": err}


def _ok(ident, result: dict) -> Reply:
    return 200, {"jsonrpc": "2.0", "id": ident, "result": result}


def _text(value, error: bool = False) -> dict:
    body = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return {"content": [{"type": "text", "text": body}], "isError": error}


def check_arguments(tool: Tool, args) -> str | None:
    """Why the arguments do not fit the tool's schema, or None. Checks what a model gets wrong:
    a missing required argument, an unknown name, the wrong JSON type, a value outside an enum,
    a number out of range."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in tool.required:
        if args.get(key) is None:
            return f"{key} is required"
    for key, value in args.items():
        spec = tool.schema.get(key)
        if spec is None:
            return f"unknown argument {key!r}; this tool takes {', '.join(tool.schema) or 'none'}"
        if value is None:
            continue
        kind = spec.get("type")
        ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool), "boolean": isinstance(value, bool)}.get(kind, True)
        if not ok:
            return f"{key} must be a {kind}"
        if "enum" in spec and value not in spec["enum"]:
            return f"{key} must be one of: {', '.join(spec['enum'])}"
        if kind == "integer" and not spec.get("minimum", -(2**31)) <= value <= spec.get("maximum", 2**31):
            return f"{key} must be between {spec.get('minimum')} and {spec.get('maximum')}"
        if kind == "string" and len(value) > 200:
            return f"{key} is too long"
    return None


def _call(ident, params: dict, tools: dict[str, Tool], scope, modern: bool) -> Reply:
    name = params.get("name")
    tool = tools.get(name) if isinstance(name, str) else None
    if tool is None:
        return _error(200, ident, BAD_PARAMS, f"unknown tool {str(name)[:60]!r}")
    problem = check_arguments(tool, params.get("arguments"))
    if problem:
        result = _text(problem, error=True)
    else:
        try:
            result = _text(tool.run(params.get("arguments") or {}, scope))
        except ToolError as err:
            result = _text(str(err), error=True)
        except Exception:  # noqa: BLE001 - a broken tool is one failed call, never a dropped connection
            LOG.exception("tool %s failed", tool.name)
            result = _text("That failed on the server; try again, or ask for less.", error=True)
    return _ok(ident, {"resultType": "complete", **result} if modern else result)


def _name_header(value: str | None) -> str | None:
    """The tool name a client mirrored into Mcp-Name, decoded when it used the base64 sentinel."""
    if value and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _modern(ident, method: str, params: dict, version, headers: Mapping, tools, scope, info: dict, instructions: str) -> Reply:
    if version is None:
        return _error(400, ident, HEADER_MISMATCH, f"Header mismatch: _meta[{VERSION_KEY!r}] is missing")
    if not isinstance(version, str) or version not in MODERN:
        return _error(400, ident, UNSUPPORTED_VERSION, "Unsupported protocol version", {"supported": list(VERSIONS), "requested": str(version)[:40]})
    if headers.get("mcp-protocol-version") != version:
        return _error(400, ident, HEADER_MISMATCH, "Header mismatch: MCP-Protocol-Version is missing or differs from _meta")
    if headers.get("mcp-method") != method:
        return _error(400, ident, HEADER_MISMATCH, "Header mismatch: Mcp-Method is missing or differs from the method")
    if method == "tools/call" and _name_header(headers.get("mcp-name")) != params.get("name"):
        return _error(400, ident, HEADER_MISMATCH, "Header mismatch: Mcp-Name is missing or differs from params.name")
    if method == "server/discover":
        return _ok(
            ident,
            {
                "resultType": "complete",
                "supportedVersions": list(VERSIONS),
                "capabilities": {"tools": {}},
                "_meta": {SERVER_INFO_KEY: info},
                "instructions": instructions,
                "ttlMs": 0,
                "cacheScope": "private",
            },
        )
    if method == "tools/list":
        return _ok(ident, {"resultType": "complete", "tools": [t.listing() for t in tools.values()], "ttlMs": 0, "cacheScope": "private"})
    if method == "tools/call":
        return _call(ident, params, tools, scope, True)
    return _error(404, ident, NO_METHOD, f"no method {method[:60]!r}")


def respond(message, headers: Mapping, tools: dict[str, Tool], scope, info: dict, instructions: str = "") -> Reply:
    """(HTTP status, JSON body or None) for one POSTed message. `headers` answers .get() with
    lower-case names. A batch, or anything that is not one JSON-RPC 2.0 message, is refused
    outright: batching left the protocol in 2025-06-18."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(400, None, INVALID_REQUEST, "send one JSON-RPC 2.0 message, not a batch")
    method, ident = message.get("method"), message.get("id")
    if method is None:
        # a client's reply to something of ours: this server asks nothing, so it is only polite to take it
        return (202, None) if "id" in message and ("result" in message or "error" in message) else _error(400, None, INVALID_REQUEST, "no method")
    if not isinstance(method, str):
        return _error(400, None, INVALID_REQUEST, "method must be a string")
    if "id" not in message:
        return 202, None  # a notification (notifications/initialized and the like)
    if ident is None or isinstance(ident, bool) or not isinstance(ident, (str, int)):
        return _error(400, None, INVALID_REQUEST, "id must be a string or a number")
    params = message.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return _error(400, ident, BAD_PARAMS, "params must be an object")
    meta = params.get("_meta")
    version = meta.get(VERSION_KEY) if isinstance(meta, dict) else None
    asked = headers.get("mcp-protocol-version")
    if method == "initialize":
        want = params.get("protocolVersion")
        return _ok(
            ident,
            {
                "protocolVersion": want if want in LEGACY else LEGACY[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": info,
                "instructions": instructions,
            },
        )
    if version is not None or asked in MODERN:
        return _modern(ident, method, params, version, headers, tools, scope, info, instructions)
    if asked is not None and asked not in LEGACY:
        return _error(400, ident, UNSUPPORTED_VERSION, "Unsupported protocol version", {"supported": list(VERSIONS), "requested": str(asked)[:40]})
    if method == "ping":
        return _ok(ident, {})
    if method == "tools/list":
        return _ok(ident, {"tools": [t.listing() for t in tools.values()]})
    if method == "tools/call":
        return _call(ident, params, tools, scope, False)
    return _error(200, ident, NO_METHOD, f"no method {method[:60]!r}")
