from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import threading
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.investigator import (
    AppServerClient,
    ESCALATION_MODEL,
    InvestigationTimeout,
    InvestigationStore,
    Investigator,
    ProtocolError,
    RuntimeUnavailable,
    SubprocessJsonRpcTransport,
    TurnResult,
    helper_route,
    private_codex_environment,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, incoming, *, termination_confirmed=False):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.terminated = False
        self.termination_confirmed = termination_confirmed

    def send(self, message):
        self.sent.append(message)

    def receive(self, _timeout):
        if not self.incoming:
            raise TimeoutError
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True
        return self.termination_confirmed


def initialized_client(events):
    transport = FakeTransport([{"id": 1, "result": {"userAgent": "fake"}}, *events])
    client = AppServerClient(transport)
    client.initialize()
    return client, transport


class AppServerClientTests(unittest.TestCase):
    def test_subprocess_transport_uses_injected_process_and_private_environment(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'{"id":1,"result":{}}\n')

            def wait(self, timeout):
                return 0

            def terminate(self):
                pass

        process = Process()
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        transport = SubprocessJsonRpcTransport(
            ("/opt/codex", "app-server"),
            popen_factory=popen,
            environment={"HOME": "/private", "PATH": "/bin"},
        )
        self.assertEqual(transport.receive(1), {"id": 1, "result": {}})
        transport.send({"method": "initialized", "params": {}})
        self.assertEqual(json.loads(process.stdin.getvalue()), {"method": "initialized", "params": {}})
        self.assertEqual(calls[0][0], ("/opt/codex", "app-server"))
        self.assertEqual(calls[0][1]["env"], {"HOME": "/private", "PATH": "/bin"})
        transport.close()

    def test_blocked_pipe_write_terminates_process_and_writer_boundedly(self):
        released = threading.Event()

        class BlockingInput:
            def write(self, _value):
                released.wait(1)

            def flush(self):
                pass

            def close(self):
                released.set()

        class Process:
            def __init__(self):
                self.stdin = BlockingInput()
                self.stdout = io.BytesIO()
                self.exited = False
                self.terminate_calls = 0

            def wait(self, timeout):
                if not self.exited:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return 0

            def terminate(self):
                self.terminate_calls += 1
                self.exited = True
                released.set()

            def kill(self):
                self.exited = True
                released.set()

        process = Process()
        transport = SubprocessJsonRpcTransport(
            ("/synthetic/app-server",),
            popen_factory=lambda *_args, **_kwargs: process,
            environment={"HOME": "/synthetic"},
            io_timeout=0.02,
        )
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeUnavailable, "write-timeout"):
            transport.send({"method": "bounded"})
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertFalse(transport._writer.is_alive())

    def test_close_escalates_through_kill_and_confirms_exit(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.killed = False
                self.terminated = False

            def wait(self, timeout):
                if not self.killed:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = Process()
        transport = SubprocessJsonRpcTransport(
            ("/synthetic/app-server",),
            popen_factory=lambda *_args, **_kwargs: process,
            environment={"HOME": "/synthetic"},
            io_timeout=0.01,
        )
        transport.close()
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(transport._termination_confirmed)
        self.assertFalse(transport._writer.is_alive())

    def test_response_notification_demux_and_initialize_order(self):
        client, transport = initialized_client(
            [
                {"method": "warning", "params": {"message": "bounded"}},
                {"id": 2, "result": {"data": []}},
            ]
        )
        result = client.request("model/list", {"limit": 100, "includeHidden": False})
        self.assertEqual(result, {"data": []})
        self.assertEqual(transport.sent[0]["method"], "initialize")
        self.assertEqual(transport.sent[1], {"method": "initialized", "params": {}})
        self.assertEqual(client._notifications[0]["method"], "warning")

    def test_dynamic_completion_usage_dedup_and_unexpected_approval_denial(self):
        client, transport = initialized_client(
            [
                {
                    "id": 99,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thr", "turnId": "turn", "command": "do-not-run"},
                },
                {"id": 2, "result": {"turn": {"id": "turn", "status": "inProgress"}}},
                {"method": "item/agentMessage/delta", "params": {"threadId": "thr", "turnId": "turn", "delta": "done"}},
                {"method": "thread/tokenUsage/updated", "params": {"threadId": "thr", "turnId": "turn", "tokenUsage": {"total": {"inputTokens": 80, "cachedInputTokens": 40, "outputTokens": 20, "reasoningOutputTokens": 10, "totalTokens": 100}, "last": {"totalTokens": 100}, "modelContextWindow": 1000}}},
                {"method": "thread/tokenUsage/updated", "params": {"threadId": "thr", "turnId": "turn", "tokenUsage": {"total": {"totalTokens": 100}, "last": {"totalTokens": 100}}}},
                {"method": "turn/completed", "params": {"threadId": "thr", "turn": {"id": "turn", "status": "completed", "items": []}}},
            ]
        )
        result = client.run_turn("thr", "Analyze approved evidence", model="gpt-5.6-sol", effort="high", timeout=60)
        self.assertEqual((result.status, result.agent_text, result.cumulative_tokens), ("completed", "done", 100))
        self.assertIn({"id": 99, "result": {"decision": "decline"}}, transport.sent)
        turn_request = next(item for item in transport.sent if item.get("method") == "turn/start")
        self.assertEqual(turn_request["params"]["sandboxPolicy"]["access"]["readableRoots"], [])
        self.assertEqual(turn_request["params"]["approvalPolicy"], "never")

    def test_timeout_interrupts_the_exact_turn(self):
        client, transport = initialized_client(
            [
                {"id": 2, "result": {"turn": {"id": "turn-timeout", "status": "inProgress"}}},
                TimeoutError(),
                {"id": 3, "result": {}},
            ]
        )
        with self.assertRaisesRegex(TimeoutError, "investigation-timeout"):
            client.run_turn("thr", "Analyze", model="gpt-5.6-sol", effort="high")
        interrupt = next(item for item in transport.sent if item.get("method") == "turn/interrupt")
        self.assertEqual(interrupt["params"], {"threadId": "thr", "turnId": "turn-timeout"})
        self.assertTrue(transport.terminated)

    def test_interrupt_ack_is_not_terminal_but_matching_completion_is(self):
        client, transport = initialized_client(
            [
                {"id": 2, "result": {"turn": {"id": "turn-timeout", "status": "inProgress"}}},
                TimeoutError(),
                {"id": 3, "result": {}},
                {"method": "thread/tokenUsage/updated", "params": {"threadId": "thr", "turnId": "turn-timeout", "tokenUsage": {"total": {"totalTokens": 321}}}},
                {"method": "turn/completed", "params": {"threadId": "thr", "turn": {"id": "turn-timeout", "status": "interrupted"}}},
            ]
        )
        with self.assertRaises(InvestigationTimeout) as caught:
            client.run_turn("thr", "Analyze", model="gpt-5.6-sol", effort="high")
        self.assertTrue(caught.exception.execution_terminated)
        self.assertEqual(caught.exception.terminal_status, "interrupted")
        self.assertEqual(caught.exception.cumulative_tokens, 321)
        self.assertFalse(transport.terminated)

    def test_unknown_server_request_gets_method_not_allowed(self):
        client, transport = initialized_client(
            [{"id": "danger", "method": "account/chatgptAuthTokens/refresh", "params": {}}, {"id": 2, "result": {}}]
        )
        client.request("account/rateLimits/read")
        self.assertIn({"id": "danger", "error": {"code": -32601, "message": "request not allowed"}}, transport.sent)

    def test_malformed_or_oversized_messages_fail_closed(self):
        client, _ = initialized_client([["not", "an", "object"]])
        with self.assertRaises(ProtocolError):
            client.request("model/list")
        huge = {"method": "warning", "params": {"message": "x" * 2048}}
        client, _ = initialized_client([huge])
        client.max_message_bytes = 1024
        with self.assertRaisesRegex(ProtocolError, "too-large"):
            client.request("model/list")

    def test_account_and_model_interfaces_use_documented_fields(self):
        client, _ = initialized_client(
            [
                {"id": 2, "result": {"account": {"type": "chatgpt", "planType": "pro"}, "requiresOpenaiAuth": True}},
                {"id": 3, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 99}, "rateLimitReachedType": None}}},
                {"id": 4, "result": {"data": [{"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None}},
            ]
        )
        self.assertTrue(client.account_available())
        self.assertTrue(client.account_limits_available())
        self.assertTrue(client.model_available("gpt-5.6-sol", "high"))


class InvestigationStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA user_version=37")
        self.store = InvestigationStore(self.connection)

    def tearDown(self):
        self.store.close()

    def test_namespaced_tables_do_not_change_global_user_version(self):
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 37)
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("terracompute_investigation_episodes", tables)
        self.assertIn("terracompute_investigation_turns", tables)

    def test_duplicate_cumulative_usage_does_not_duplicate_totals(self):
        episode, _ = self.store.episode("inc-1", "hash-1", "error", NOW)
        self.store.set_thread(episode["id"], "thr")
        first = self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        self.assertEqual(self.store.record_usage(first.turn_row_id, "thr", 40_000), 40_000)
        self.assertEqual(self.store.record_usage(first.turn_row_id, "thr", 40_000), 0)
        self.store.finish_turn(first.turn_row_id, "turn-1", "completed", NOW)
        second = self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW + timedelta(minutes=1))
        self.assertEqual(self.store.record_usage(second.turn_row_id, "thr", 55_000), 15_000)
        total = self.connection.execute("SELECT SUM(reported_tokens) FROM terracompute_investigation_turns").fetchone()[0]
        self.assertEqual(total, 55_000)

    def test_rolling_day_rollover_and_critical_reserve(self):
        old, _ = self.store.episode("old", "old-hash", "error", NOW - timedelta(hours=25))
        for index in range(4):
            decision = self.store.admit(old["id"], "lead", "gpt-5.6-sol", NOW - timedelta(hours=25, minutes=-index))
            self.connection.execute("UPDATE terracompute_investigation_turns SET reported_tokens=50000,status='completed' WHERE id=?", (decision.turn_row_id,))
            self.connection.commit()
        self.connection.commit()
        current, _ = self.store.episode("current", "new-hash", "error", NOW)
        self.assertTrue(self.store.admit(current["id"], "lead", "gpt-5.6-sol", NOW).admitted)

        self.connection.execute("UPDATE terracompute_investigation_turns SET reported_tokens=200000,status='completed' WHERE episode_id=?", (current["id"],))
        self.connection.commit()
        noncritical, _ = self.store.episode("other", "other-hash", "warning", NOW)
        self.assertEqual(self.store.admit(noncritical["id"], "lead", "gpt-5.6-sol", NOW).reason, "critical-reserve")
        critical, _ = self.store.episode("critical", "critical-hash", "critical", NOW)
        self.assertTrue(self.store.admit(critical["id"], "lead", "gpt-5.6-sol", NOW).admitted)

    def test_inflight_overshoot_is_reported_between_turns(self):
        episode, _ = self.store.episode("inc", "hash", "critical", NOW)
        decision = self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        self.store.record_usage(decision.turn_row_id, "thr", 70_000)
        overshoot = self.store.finish_turn(decision.turn_row_id, "turn", "completed", NOW)
        self.assertEqual(overshoot, 10_000)
        self.assertEqual(self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW).reason, "episode-token-cap")

    def test_unknown_usage_blocks_episode_and_rolling_day_across_restart(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "investigation.sqlite3"
        store = InvestigationStore(database)
        episode, _ = store.episode("inc", "hash", "critical", NOW)
        decision = store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        store.record_usage(decision.turn_row_id, "thr", None)
        store.finish_turn(decision.turn_row_id, "turn", "completed", NOW)
        store.close()

        restarted = InvestigationStore(database)
        same, _ = restarted.episode("inc", "hash", "critical", NOW)
        self.assertEqual(
            restarted.admit(same["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "episode-token-accounting-unavailable",
        )
        other, _ = restarted.episode("other", "hash-2", "critical", NOW)
        self.assertEqual(
            restarted.admit(other["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "rolling-token-accounting-unavailable",
        )
        after_window, _ = restarted.episode(
            "later", "hash-3", "critical", NOW + timedelta(hours=25)
        )
        self.assertTrue(
            restarted.admit(
                after_window["id"],
                "lead",
                "gpt-5.6-sol",
                NOW + timedelta(hours=25),
            ).admitted
        )
        restarted.close()

    def test_astra_limits_and_concurrency(self):
        episode, _ = self.store.episode("inc", "hash", "critical", NOW)
        first = self.store.admit(episode["id"], "lead", ESCALATION_MODEL, NOW)
        self.assertTrue(first.admitted)
        helper = self.store.admit(episode["id"], "helper", "gpt-5.6-terra", NOW)
        self.assertTrue(helper.admitted)
        self.store.finish_turn(first.turn_row_id, "a", "completed", NOW)
        self.store.finish_turn(helper.turn_row_id, "h", "completed", NOW)
        self.assertEqual(self.store.admit(episode["id"], "lead", ESCALATION_MODEL, NOW).reason, "astra-cap")


class StubClient:
    def __init__(self, *, auth=True):
        self.auth = auth
        self.calls = []

    def account_available(self): self.calls.append("account"); return self.auth
    def account_limits_available(self): self.calls.append("limits"); return True
    def model_available(self, model, effort): self.calls.append((model, effort)); return True
    def start_thread(self, model): self.calls.append(("start", model)); return "thr"
    def resume_thread(self, thread): self.calls.append(("resume", thread))
    def run_turn(self, thread, prompt, *, model, effort, timeout, on_started=None):
        self.calls.append(("turn", thread, prompt, model, effort, timeout))
        if on_started is not None:
            on_started("turn")
        return TurnResult(thread, "turn", "completed", "analysis", 123)


class TimeoutClient(StubClient):
    def run_turn(self, thread, prompt, *, model, effort, timeout, on_started=None):
        if on_started is not None:
            on_started("turn-persisted-before-timeout")
        raise TimeoutError("provider detail must not escape")


class ConfirmedTimeoutClient(StubClient):
    def run_turn(self, thread, prompt, *, model, effort, timeout, on_started=None):
        if on_started is not None:
            on_started("turn-confirmed-stopped")
        raise InvestigationTimeout(
            cumulative_tokens=123,
            execution_terminated=True,
            terminal_status="interrupted",
        )


class KilledTimeoutClient(StubClient):
    def run_turn(self, thread, prompt, *, model, effort, timeout, on_started=None):
        if on_started is not None:
            on_started("turn-killed-without-terminal")
        raise InvestigationTimeout(
            cumulative_tokens=123,
            execution_terminated=True,
        )


class InvestigatorTests(unittest.TestCase):
    def test_auth_unavailable_does_not_start_runtime_turn(self):
        store = InvestigationStore(":memory:")
        client = StubClient(auth=False)
        result = Investigator(client, store, now=lambda: NOW).investigate("inc", "hash", "prompt")
        self.assertEqual((result.status, result.reason), ("unavailable", "auth-or-quota-unavailable"))
        self.assertEqual(client.calls, ["account"])
        store.close()

    def test_timeout_preserves_runtime_turn_mapping_and_sanitizes_error(self):
        store = InvestigationStore(":memory:")
        result = Investigator(TimeoutClient(), store, now=lambda: NOW).investigate(
            "inc", "hash", "prompt"
        )
        row = store.db.execute(
            "SELECT status,runtime_turn_id FROM terracompute_investigation_turns"
        ).fetchone()
        self.assertEqual(result.reason, "investigation-timeout-execution-unknown")
        self.assertEqual(
            (row["status"], row["runtime_turn_id"]),
            ("in_flight", "turn-persisted-before-timeout"),
        )
        second = Investigator(StubClient(), store, now=lambda: NOW).investigate(
            "other", "other-hash", "prompt"
        )
        self.assertEqual((second.status, second.reason), ("rejected", "lead-concurrency-cap"))
        store.close()

    def test_confirmed_timeout_releases_admission_after_usage_is_recorded(self):
        store = InvestigationStore(":memory:")
        result = Investigator(ConfirmedTimeoutClient(), store, now=lambda: NOW).investigate(
            "inc", "hash", "prompt"
        )
        row = store.db.execute(
            "SELECT status,runtime_turn_id,reported_tokens,usage_available FROM terracompute_investigation_turns"
        ).fetchone()
        self.assertEqual((result.status, result.reason), ("timeout", "investigation-timeout"))
        self.assertEqual(
            tuple(row),
            ("timeout", "turn-confirmed-stopped", 123, 1),
        )
        store.close()

    def test_kill_without_terminal_retains_usage_lower_bound_but_degrades_accounting(self):
        store = InvestigationStore(":memory:")
        result = Investigator(KilledTimeoutClient(), store, now=lambda: NOW).investigate(
            "inc", "hash", "prompt"
        )
        row = store.db.execute(
            "SELECT status,reported_tokens,usage_available FROM terracompute_investigation_turns"
        ).fetchone()
        self.assertEqual((result.status, result.reason), ("timeout", "investigation-timeout"))
        self.assertEqual(tuple(row), ("timeout", 123, 0))
        other, _ = store.episode("other", "hash-2", "critical", NOW)
        self.assertEqual(
            store.admit(other["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "rolling-token-accounting-unavailable",
        )
        store.close()

    def test_unchanged_completed_evidence_makes_no_redundant_calls(self):
        store = InvestigationStore(":memory:")
        client = StubClient()
        investigator = Investigator(client, store, now=lambda: NOW)
        first = investigator.investigate("inc", "hash", "prompt")
        calls = list(client.calls)
        second = investigator.investigate("inc", "hash", "prompt")
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(client.calls, calls)
        store.close()

    def test_helper_gate_fails_closed_and_routes_are_explicit(self):
        store = InvestigationStore(":memory:")
        investigator = Investigator(StubClient(), store, native_helpers_verified=False)
        with self.assertRaisesRegex(RuntimeUnavailable, "not-commissioned"):
            investigator.run_helper()
        self.assertEqual(helper_route("comparison"), ("gpt-5.6-terra", "medium"))
        self.assertEqual(helper_route("summary"), ("gpt-5.6-luna", "low"))
        store.close()

    def test_justified_astra_escalation_reuses_completed_episode_thread(self):
        store = InvestigationStore(":memory:")
        client = StubClient()
        investigator = Investigator(client, store, now=lambda: NOW)
        first = investigator.investigate("inc", "hash", "lead")
        second = investigator.investigate(
            "inc",
            "hash",
            "resolve contradiction",
            model="gpt-6-astra",
            effort="high",
            escalation_justified=True,
        )
        self.assertEqual((first.status, second.status), ("completed", "completed"))
        self.assertIn(("resume", "thr"), client.calls)
        turns = store.db.execute(
            "SELECT COUNT(*) FROM terracompute_investigation_turns"
        ).fetchone()[0]
        self.assertEqual(turns, 2)
        store.close()

    def test_private_home_uses_default_codex_location_without_provider_state(self):
        environment = private_codex_environment(Path("/var/lib/imladris/terracompute-codex"), {"CODEX_HOME": "/seat", "CLAUDE_CONFIG_DIR": "/claude", "PATH": "/bin"})
        self.assertEqual(environment["HOME"], "/var/lib/imladris/terracompute-codex")
        self.assertNotIn("CODEX_HOME", environment)
        self.assertNotIn("CLAUDE_CONFIG_DIR", environment)


if __name__ == "__main__":
    unittest.main()
