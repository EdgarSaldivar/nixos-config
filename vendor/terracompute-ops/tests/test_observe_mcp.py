from __future__ import annotations

import io
import json
import unittest

from terracompute_ops.observe_mcp import (
    FALLBACK_PROTOCOL_VERSION,
    MAX_COMMAND_CHARS,
    TOOL_NAME,
    ObserveServer,
    serve,
)


class ObserveProtocolTests(unittest.TestCase):
    def server(self, run=None):
        self.calls: list[str] = []

        def default(command: str) -> str:
            self.calls.append(command)
            return f"ran: {command}"

        return ObserveServer(run or default)

    def test_initialize_advertises_the_tool_capability_and_echoes_the_version(self) -> None:
        server = self.server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        })
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", reply["result"]["capabilities"])
        # A client that names no version still gets a usable one.
        reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        self.assertEqual(reply["result"]["protocolVersion"], FALLBACK_PROTOCOL_VERSION)

    def test_initialized_notification_is_not_answered(self) -> None:
        self.assertIsNone(self.server().handle({"method": "notifications/initialized"}))

    def test_tools_list_describes_observe(self) -> None:
        reply = self.server().handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = reply["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [TOOL_NAME])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["command"])

    def test_a_call_runs_the_command_and_returns_its_output(self) -> None:
        server = self.server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"command": "cat /proc/uptime"}},
        })
        self.assertEqual(self.calls, ["cat /proc/uptime"])
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(reply["result"]["content"][0]["text"], "ran: cat /proc/uptime")

    def test_a_failing_runner_becomes_an_error_result_not_a_crash(self) -> None:
        def boom(command: str) -> str:
            raise OSError("channel down")

        reply = self.server(boom).handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"command": "x"}},
        })
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("OSError", reply["result"]["content"][0]["text"])

    def test_a_bad_call_is_rejected_without_running_anything(self) -> None:
        server = self.server()
        for arguments in ({}, {"command": ""}, {"command": "   "}, {"command": "x" * (MAX_COMMAND_CHARS + 1)}):
            reply = server.handle({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": arguments},
            })
            self.assertTrue(reply["result"]["isError"], arguments)
        self.assertEqual(self.calls, [], "nothing should have run")
        # A tool that is not ours is a protocol error, not a tool result.
        reply = server.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "delete_everything", "arguments": {}},
        })
        self.assertIn("error", reply)

    def test_unknown_request_errors_but_unknown_notification_is_silent(self) -> None:
        server = self.server()
        self.assertIn("error", server.handle({"jsonrpc": "2.0", "id": 8, "method": "nope"}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "nope"}))

    def test_serve_reads_lines_and_writes_replies_until_eof(self) -> None:
        script = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"method": "notifications/initialized"}),
            "",  # blank line tolerated
            "{not json",  # unparseable line dropped
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": TOOL_NAME, "arguments": {"command": "uptime"}}}),
        ])
        out = io.StringIO()
        ran: list[str] = []
        serve(lambda c: ran.append(c) or "ok", stdin=io.StringIO(script), stdout=out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        # Two answers: initialize and the tool call. The notification and junk produced none.
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(ran, ["uptime"])
        self.assertEqual(replies[1]["result"]["content"][0]["text"], "ok")


if __name__ == "__main__":
    unittest.main()
