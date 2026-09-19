"""A one-tool MCP server that lets the investigator's model read the target.

The brain lets the model run its own reads mid-investigation. Those reads reach the
target through this server: codex launches it as an MCP stdio server, the model calls the
``observe`` tool with a command, and the command runs on the target under the read-only
profile -- every filesystem read-only, tenant data and the control sockets walled off,
no way to change the machine. What comes back is the command's output, as evidence.

The transport is deliberately small and self-contained: newline-delimited JSON-RPC 2.0
on stdin/stdout, which is the MCP stdio framing. It implements exactly the three methods
a tool server must answer -- ``initialize``, ``tools/list``, ``tools/call`` -- and the
``notifications/initialized`` acknowledgement, and nothing else. The command runner is
injected, so the protocol is testable without a target and the target reach is testable
without the protocol.

Nothing here can change the machine. The runner it is given is a read-only observe
channel; the server neither holds nor forwards any mutation capability.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, TextIO

SERVER_NAME = "terracompute-observe"
SERVER_VERSION = "1"
TOOL_NAME = "observe"
# The MCP protocol version this server speaks. It echoes the client's requested version
# when that is a string, so a newer codex negotiates itself; this is only the fallback.
FALLBACK_PROTOCOL_VERSION = "2025-06-18"
MAX_COMMAND_CHARS = 4096
MAX_RESULT_CHARS = 60_000

TOOL_DESCRIPTION = (
    "Run one read-only shell command on the target host and return its output. The "
    "command runs with every filesystem mounted read-only and tenant data walled off, "
    "so it cannot change the machine or read another tenant's data -- use it to look at "
    "processes, devices, drivers, logs and configuration while you work out what is "
    "wrong. Prefer specific reads over broad scans."
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "A read-only shell command, e.g. 'ls -l /proc/*/fd | grep nvidia'.",
        }
    },
    "required": ["command"],
    "additionalProperties": False,
}


class ObserveServer:
    """The protocol half: JSON-RPC over stdio, one tool, a runner it does not own."""

    def __init__(self, run: Callable[[str], str], *, name: str = SERVER_NAME):
        self._run = run
        self._name = name

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One request in, one response out -- or None for a notification."""
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            version = requested if isinstance(requested, str) else FALLBACK_PROTOCOL_VERSION
            return self._ok(message_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self._name, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._ok(message_id, {"tools": [{
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": TOOL_SCHEMA,
            }]})
        if method == "tools/call":
            return self._call(message_id, message.get("params") or {})
        if message_id is None:
            return None  # An unknown notification is ignored, not answered.
        return self._error(message_id, -32601, f"method not found: {method}")

    def _call(self, message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("name") != TOOL_NAME:
            return self._error(message_id, -32602, "unknown tool")
        arguments = params.get("arguments")
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not command.strip():
            return self._tool_result(message_id, "observe needs a non-empty command.", is_error=True)
        if len(command) > MAX_COMMAND_CHARS:
            return self._tool_result(message_id, "command is too long.", is_error=True)
        try:
            output = self._run(command)
        except Exception as error:  # A failed read is a result, never a crash of the turn.
            return self._tool_result(
                message_id, f"observe failed: {type(error).__name__}", is_error=True
            )
        text = output if isinstance(output, str) else str(output)
        return self._tool_result(message_id, text[:MAX_RESULT_CHARS] or "(no output)")

    def _tool_result(self, message_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
        return self._ok(message_id, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })

    @staticmethod
    def _ok(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def serve(run: Callable[[str], str], *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin, answer on stdout, until EOF."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    server = ObserveServer(run)
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue  # A line we cannot parse has no id to answer; drop it.
        if not isinstance(message, dict):
            continue
        response = server.handle(message)
        if response is not None:
            sink.write(json.dumps(response) + "\n")
            sink.flush()


def _runner_from_env() -> Callable[[str], str]:
    """Build the read-only observe runner the deployed server uses.

    Wired from the investigator's own read-only channel to the target -- its own SSH
    identity, forced-command-scoped so it can only observe. Imported lazily so the
    protocol and its tests never require the transport.
    """
    from .monitor_restart import SSHActorClient  # noqa: PLC0415 -- lazy by intent
    import uuid

    client = SSHActorClient(
        ssh_binary=os.environ["TERRACOMPUTE_OBSERVE_SSH"],
        target=os.environ["TERRACOMPUTE_OBSERVE_TARGET"],
        identity_file=os.environ["TERRACOMPUTE_OBSERVE_IDENTITY"],
        known_hosts_file=os.environ["TERRACOMPUTE_OBSERVE_KNOWN_HOSTS"],
    )

    def run(command: str) -> str:
        document = client.session(command, str(uuid.uuid4()), writable=False)
        if not document.get("ok"):
            return f"(observe unavailable: {document.get('reason', 'unknown')})"
        lines = document.get("lines") or []
        body = "\n".join(str(line) for line in lines)
        if document.get("truncated"):
            body += "\n(truncated)"
        return body

    return run


def main() -> int:
    serve(_runner_from_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
