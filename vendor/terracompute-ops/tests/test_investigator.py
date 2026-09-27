from __future__ import annotations

import inspect
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

from terracompute_ops.charter import CHARTER
from terracompute_ops.investigator import (
    LEAD_MODEL,
    RequestRejected,
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


class RefusalCostsNothingTests(unittest.TestCase):
    """Being refused is not losing contact, and must not take the loop offline."""

    def investigate_with(self, failure, *, started=None):
        class Client:
            def account_available(self):
                return True

            def account_limits_available(self):
                return True

            def model_available(self, model, effort):
                return True

            def resume_thread(self, thread_id, **_kwargs):
                raise failure

            def start_thread(self, model, **_kwargs):
                raise failure

            def run_turn(self, *args, **kwargs):
                raise AssertionError("a turn should not have begun")

            def close(self):
                pass

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = InvestigationStore(Path(temporary.name) / "s.sqlite3")
        self.addCleanup(store.close)
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        result = Investigator(Client(), store, now=lambda: now).investigate(
            "incident-1", "a" * 64, "Diagnose this.", severity="critical"
        )
        turns = list(store.db.execute(
            "SELECT status,usage_available,reported_tokens FROM terracompute_investigation_turns"))
        return result, turns

    def test_a_refusal_leaves_no_lease_and_no_spend(self):
        result, turns = self.investigate_with(RequestRejected("app-server-rpc-error"))
        self.assertEqual(result.reason, "app-server-rejected")
        self.assertEqual(turns[0][0], "rejected", "the lease was left in flight")
        self.assertEqual((turns[0][1], turns[0][2]), (1, 0), "a refusal was charged for")

    def test_losing_contact_is_still_treated_carefully(self):
        """The careful path is for not knowing, and it stays exactly as it was."""
        result, turns = self.investigate_with(RuntimeUnavailable("app-server-start-failed"))
        self.assertEqual(result.reason, "runtime-failure-execution-unknown")
        self.assertEqual(turns[0][0], "in_flight", "a lease was released without proof")


class RememberedConclusionTests(unittest.TestCase):
    def test_an_episode_says_again_what_it_concluded(self):
        """Unchanged evidence means the answer stands, not that there is none."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = InvestigationStore(Path(temporary.name) / "s.sqlite3")
        self.addCleanup(store.close)
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        episode, _created = store.episode("incident-1", "a" * 64, "critical", now)
        store.complete_episode(episode["id"], now, '{"summary": "it is the exporter"}')
        again, created = store.episode("incident-1", "a" * 64, "critical", now)
        self.assertFalse(created)
        self.assertEqual(again["report"], '{"summary": "it is the exporter"}')

    def test_an_old_database_gains_the_column_in_place(self):
        """The machine already has one of these; it must not need rebuilding."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "s.sqlite3"
        store = InvestigationStore(path)
        store.db.execute("ALTER TABLE terracompute_investigation_episodes DROP COLUMN report")
        store.db.commit()
        store.close()
        reopened = InvestigationStore(path)
        self.addCleanup(reopened.close)
        have = {row[1] for row in reopened.db.execute(
            "PRAGMA table_info(terracompute_investigation_episodes)")}
        self.assertIn("report", have)


class OrphanedThreadTests(unittest.TestCase):
    def test_a_lost_thread_fails_explicitly_without_replacement(self):
        calls = []

        class Client:
            failure = ProtocolError("app-server-invalid-thread")

            def account_available(self):
                return True

            def account_limits_available(self):
                return True

            def model_available(self, model, effort):
                return True

            def resume_thread(self, thread_id, **_kwargs):
                calls.append(("resume", thread_id))
                raise self.failure

            def start_thread(self, model, **_kwargs):
                calls.append(("start", model))
                return "thread-new"

            def run_turn(self, thread_id, prompt, **kwargs):
                calls.append(("turn", thread_id))
                on_started = kwargs.get("on_started")
                if on_started:
                    on_started("runtime-turn-1")
                return TurnResult(thread_id, "runtime-turn-1", "completed", "answer", 10)

            def close(self):
                pass

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = InvestigationStore(Path(temporary.name) / "s.sqlite3")
        self.addCleanup(store.close)
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        episode, _created = store.episode("incident-1", "a" * 64, "critical", now)
        store.set_thread(episode["id"], "thread-orphaned")

        client = Client()
        investigator = Investigator(client, store, now=lambda: now)
        for failure in (ProtocolError("bad thread"), RequestRejected("private detail"),
                        RuntimeUnavailable("offline"), TimeoutError(), OSError()):
            with self.subTest(failure=type(failure).__name__):
                client.failure = failure
                calls.clear()
                result = investigator.investigate("incident-1", "a" * 64, "Diagnose this.", severity="critical")
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.reason, "thread-resume-failed")
                self.assertEqual(calls, [("resume", "thread-orphaned")])
                self.assertEqual(store.db.execute(
                    "SELECT thread_id FROM terracompute_investigation_episodes"
                ).fetchone()[0], "thread-orphaned")
                self.assertEqual(store.db.execute(
                    "SELECT count(*) FROM terracompute_investigation_turns WHERE status='in_flight'"
                ).fetchone()[0], 0)
                self.assertEqual(store.db.execute(
                    "SELECT sum(reported_tokens) FROM terracompute_investigation_turns"
                ).fetchone()[0], 0)

    def test_resume_rejects_wrong_identity(self):
        client, transport = initialized_client([
            {"id": 2, "result": {"thread": {"id": "different"}}},
        ])
        with self.assertRaisesRegex(ProtocolError, "thread-mismatch"):
            client.resume_thread("original")
        self.assertEqual(transport.sent[-1]["params"]["threadId"], "original")


class BudgetCountsRealTurnsTests(unittest.TestCase):
    """A budget is for work that happened, not for attempts that never began."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = InvestigationStore(Path(self.temporary.name) / "s.sqlite3")
        self.now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        self.store.db.execute(
            "INSERT INTO terracompute_investigation_episodes(incident_id,evidence_hash,severity,status,created_utc)"
            " VALUES(?,?,?,?,?)", ("incident-1", "a" * 64, "critical", "open", self.store._utc(self.now)))
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add_turn(self, runtime_turn_id):
        self.store.db.execute(
            "INSERT INTO terracompute_investigation_turns(episode_id,role,model,status,started_utc,runtime_turn_id)"
            " VALUES(1,?,?,?,?,?)",
            ("lead", LEAD_MODEL, "runtime-failure", self.store._utc(self.now), runtime_turn_id))
        self.store.db.commit()

    def test_turns_the_app_server_never_accepted_do_not_spend_the_allowance(self):
        for _ in range(6):
            self.add_turn(None)
        decision = self.store.admit(1, "lead", LEAD_MODEL, self.now)
        self.assertTrue(decision.admitted, f"refused as {decision.reason} after no real turn ran")

    def test_turns_that_really_ran_do_spend_it(self):
        for index in range(InvestigationStore.MAX_INVESTIGATION_TURNS):
            self.add_turn(f"turn-{index}")
        decision = self.store.admit(1, "lead", LEAD_MODEL, self.now)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason, "investigation-turn-cap")

    def test_a_turn_whose_cost_was_never_reported_is_charged_as_a_dear_one(self):
        """Losing count must cost the investigation, not close it.

        Refusing outright meant one unreported result took the whole rolling day of
        diagnosis with it, including every other fault's.
        """
        # Charged as expensive turns, an investigation of nothing but unmeasured work
        # runs out of tokens before it runs out of turns -- which is the pessimism we
        # want, and it still ends in a refusal rather than in silence.
        for index in range(7):
            self.add_turn(f"turn-{index}")
        self.store.db.execute("UPDATE terracompute_investigation_turns SET usage_available=0")
        self.store.db.commit()
        decision = self.store.admit(1, "lead", LEAD_MODEL, self.now)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason, "investigation-token-cap")
        # And a different fault is untouched by it.
        other, _ = self.store.episode("incident-2", "b" * 64, "error", self.now)
        self.assertTrue(self.store.admit(other["id"], "lead", LEAD_MODEL, self.now).admitted)


class ReadinessBudgetTests(unittest.TestCase):
    """Three phases, three budgets, and none of them thirty seconds.

    Getting a thread, being told a turn started, and the turn itself are different
    waits. A single cold-start budget applied to all of them meant the first
    diagnosis after any restart failed on a timer, reported as the model being
    unavailable, and fell back to the rule.
    """

    def test_no_phase_still_carries_the_cold_start_timer(self):
        import inspect as _inspect

        from terracompute_ops.investigator import (
            APP_SERVER_READY_SECONDS,
            TURN_START_ACK_SECONDS,
        )

        self.assertGreaterEqual(APP_SERVER_READY_SECONDS, 90)
        self.assertGreaterEqual(TURN_START_ACK_SECONDS, 90)
        for method in (AppServerClient.start_thread, AppServerClient.resume_thread):
            default = _inspect.signature(method).parameters["timeout"].default
            self.assertEqual(default, APP_SERVER_READY_SECONDS, method.__name__)
        self.assertNotIn("min(timeout, 30)", _inspect.getsource(AppServerClient.run_turn))


class UnacknowledgedSpendTests(unittest.TestCase):
    """One refused call must not cost a day of diagnosis."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "investigator.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def rows(self, store):
        turns = list(store.db.execute(
            "SELECT id,usage_available,reported_tokens FROM terracompute_investigation_turns ORDER BY id"))
        episodes = list(store.db.execute(
            "SELECT id,accounting_available FROM terracompute_investigation_episodes ORDER BY id"))
        return turns, episodes

    def build(self, turns):
        store = InvestigationStore(self.path)
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        for index, (runtime_turn_id, usage_available) in enumerate(turns):
            store.db.execute(
                "INSERT INTO terracompute_investigation_episodes(incident_id,evidence_hash,severity,status,accounting_available,created_utc)"
                " VALUES(?,?,?,?,?,?)",
                (f"incident-{index}", "a" * 64 + str(index), "error", "open", 0, store._utc(now)),
            )
            store.db.execute(
                "INSERT INTO terracompute_investigation_turns(episode_id,role,model,status,started_utc,runtime_turn_id,usage_available)"
                " VALUES((SELECT MAX(id) FROM terracompute_investigation_episodes),?,?,?,?,?,?)",
                ("lead", "gpt-5.6-sol", "runtime-failure", store._utc(now), runtime_turn_id, usage_available),
            )
        store.db.commit()
        store.close()
        return InvestigationStore(self.path)  # Reopened: the correction runs at startup.

    def test_a_turn_never_acknowledged_is_recorded_as_spending_nothing(self):
        store = self.build([(None, 0)])
        turns, episodes = self.rows(store)
        self.assertEqual((turns[0][1], turns[0][2]), (1, 0), "unknown spend for a turn that never ran")
        self.assertEqual(episodes[0][1], 1, "the episode stayed blocked")
        store.close()

    def test_a_turn_that_really_ran_keeps_its_unknown_spend(self):
        """Only the provable case is corrected: a real turn's lost count still counts."""
        store = self.build([("turn-abc", 0)])
        turns, episodes = self.rows(store)
        self.assertEqual(turns[0][1], 0, "invented a spend for a turn that did run")
        self.assertEqual(episodes[0][1], 0, "unblocked an episode whose spend is unknown")
        store.close()


class AppServerClientTests(unittest.TestCase):
    def test_start_and_resume_use_explicit_charter_without_local_project_discovery(self):
        client, transport = initialized_client([
            {"id": 2, "result": {"thread": {"id": "thr-1"}}},
            {"id": 3, "result": {"thread": {"id": "thr-1"}}},
        ])
        self.assertEqual(client.start_thread(LEAD_MODEL), "thr-1")
        client.resume_thread("thr-1")
        for message, method in zip(transport.sent[-2:], ("thread/start", "thread/resume")):
            with self.subTest(method=method):
                self.assertEqual(message["method"], method)
                params = message["params"]
                self.assertEqual(params["developerInstructions"], CHARTER)
                self.assertEqual(params["config"], {
                    "project_doc_max_bytes": 0,
                    "web_search": "live",
                    "features": {"shell_tool": False, "unified_exec": False},
                })
        self.assertFalse(transport.sent[-2]["params"]["ephemeral"])
        self.assertEqual(set(transport.sent[-1]["params"]),
                         {"threadId", "developerInstructions", "config"})
        self.assertEqual(transport.sent[-1]["params"]["threadId"], "thr-1")

    def test_the_two_sandbox_spellings_are_not_interchangeable(self):
        """The app server names the same idea two ways, and rejects the wrong one.

        `thread/start` takes `sandbox` in kebab-case; a turn takes `sandboxPolicy.type`
        in camelCase, beside `dangerFullAccess`. Sending either spelling to the other
        call is refused outright, which once cost a whole diagnosis: the turn died
        before it began and the loop reported only that the model was unavailable.
        """
        sent = []

        class Recording:
            def request(self, method, params, timeout=30):
                sent.append((method, params))
                return {"thread": {"id": "thr-1"}}

        client = AppServerClient.__new__(AppServerClient)
        client.request = Recording().request
        self.assertEqual(client.start_thread("gpt-5.6-sol"), "thr-1")
        method, params = sent[0]
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["sandbox"], "read-only")
        source = inspect.getsource(AppServerClient.run_turn)
        self.assertIn('"sandboxPolicy": {"type": "readOnly", "networkAccess": False}', source)
        self.assertNotIn('"access"', source, "readOnly.access is refused by the app server")

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
        # The turn is read-only and cannot reach the network, and it may approve
        # nothing. Restricting readable roots to none is no longer expressible here:
        # this App Server refuses `readOnly.access` and points at a permission
        # profile, which turn parameters do not carry.
        self.assertEqual(
            turn_request["params"]["sandboxPolicy"], {"type": "readOnly", "networkAccess": False}
        )
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

    def test_a_heavy_day_does_not_stop_the_next_fault_being_investigated(self):
        """A first attempt that failed must never be why the second cannot happen.

        The day used to be the budget: twenty turns or two hundred thousand tokens and
        the machine stopped diagnosing until the window rolled, whatever was wrong with
        it by then.
        """
        spent, _ = self.store.episode("spent", "spent-hash", "error", NOW)
        for _ in range(InvestigationStore.MAX_INVESTIGATION_TURNS):
            decision = self.store.admit(spent["id"], "lead", "gpt-5.6-sol", NOW)
            self.connection.execute(
                "UPDATE terracompute_investigation_turns SET reported_tokens=20000,status='completed'"
                " WHERE id=?", (decision.turn_row_id,))
            self.connection.commit()
        self.assertEqual(
            self.store.admit(spent["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "investigation-turn-cap",
        )
        fresh, _ = self.store.episode("fresh", "fresh-hash", "error", NOW)
        self.assertTrue(
            self.store.admit(fresh["id"], "lead", "gpt-5.6-sol", NOW).admitted,
            "a new investigation inherited the last one's exhaustion",
        )

    def test_the_backstop_stops_the_machine_and_never_the_operator(self):
        """Ten times a heavy day is a bug in our loop, not a day's work."""
        runaway, _ = self.store.episode("runaway", "runaway-hash", "error", NOW)
        decision = self.store.admit(runaway["id"], "lead", "gpt-5.6-sol", NOW)
        self.connection.execute(
            "UPDATE terracompute_investigation_turns SET reported_tokens=?,status='completed' WHERE id=?",
            (InvestigationStore.DAILY_BACKSTOP_TOKENS, decision.turn_row_id))
        self.connection.commit()
        fresh, _ = self.store.episode("fresh", "fresh-hash", "error", NOW)
        self.assertEqual(
            self.store.admit(fresh["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "daily-spend-backstop",
        )
        self.assertTrue(
            self.store.admit(fresh["id"], "lead", "gpt-5.6-sol", NOW, operator=True).admitted,
            "a person was locked out by the machine's own runaway",
        )

    def test_inflight_overshoot_is_reported_between_turns(self):
        episode, _ = self.store.episode("inc", "hash", "critical", NOW)
        decision = self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        self.store.record_usage(
            decision.turn_row_id, "thr", InvestigationStore.MAX_INVESTIGATION_TOKENS + 10_000
        )
        overshoot = self.store.finish_turn(decision.turn_row_id, "turn", "completed", NOW)
        self.assertEqual(overshoot, 10_000)
        self.assertEqual(
            self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "investigation-token-cap",
        )

    def test_unknown_usage_is_charged_across_a_restart_and_confined_to_its_own_fault(self):
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
        # The charge survives the restart: this investigation has spent an expensive
        # turn's worth, and it keeps working with what is left rather than stopping.
        same, _ = restarted.episode("inc", "hash", "critical", NOW)
        resumed = restarted.admit(same["id"], "lead", "gpt-5.6-sol", NOW)
        self.assertTrue(resumed.admitted)
        restarted.finish_turn(resumed.turn_row_id, "turn-2", "completed", NOW)
        charged = restarted.db.execute(
            """SELECT COUNT(*) FROM terracompute_investigation_turns t
               JOIN terracompute_investigation_episodes e ON e.id=t.episode_id
               WHERE e.investigation_id=? AND t.usage_available=0""",
            (str(same["investigation_id"]),),
        ).fetchone()[0]
        self.assertEqual(charged, 1)
        # Another fault never paid for it, which is what a rolling-day block did.
        other, _ = restarted.episode("other", "hash-2", "critical", NOW)
        self.assertTrue(restarted.admit(other["id"], "lead", "gpt-5.6-sol", NOW).admitted)
        restarted.close()

    def test_an_unmeasured_turn_is_never_charged_less_than_it_admitted_to(self):
        """A turn that timed out reports a lower bound and then loses its accounting.

        Charging every unmeasured turn a flat amount handed that budget straight back:
        three hundred thousand known tokens became forty thousand, and the investigation
        carried on spending.
        """
        episode, _ = self.store.episode("inc", "hash", "error", NOW)
        decision = self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        self.store.record_usage(decision.turn_row_id, "thr", 300_000)
        self.store.record_usage(decision.turn_row_id, "thr", None)  # and then lost count
        self.store.finish_turn(decision.turn_row_id, "turn", "timeout", NOW)
        self.assertEqual(
            self.store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "investigation-token-cap",
        )

    def test_the_backstop_counts_only_what_the_machine_spent_on_itself(self):
        """Talking must not meter the machine by the back door."""
        episode, _ = self.store.episode("inc", "hash", "error", NOW)
        for _ in range(4):
            decision = self.store.admit(
                episode["id"], "lead", "gpt-5.6-sol", NOW, operator=True
            )
            self.connection.execute(
                "UPDATE terracompute_investigation_turns SET reported_tokens=?,status='completed'"
                " WHERE id=?",
                (InvestigationStore.DAILY_BACKSTOP_TOKENS, decision.turn_row_id))
            self.connection.commit()
        fresh, _ = self.store.episode("fresh", "fresh-hash", "error", NOW)
        self.assertTrue(
            self.store.admit(fresh["id"], "lead", "gpt-5.6-sol", NOW).admitted,
            "conversation spent the machine's backstop for it",
        )

    def test_an_episode_joins_the_investigation_its_caller_names(self):
        """One investigation must not be capped as two because of who opened it first.

        An episode opened by a request from the older schema, or by a caller that names
        no investigation, keeps a synthesised id -- and the turns already spent under it
        would sit outside the budget the next caller is counting.
        """
        first, _ = self.store.episode("inc", "hash", "error", NOW)
        self.assertNotEqual(str(first["investigation_id"]), "inc#1#0")
        same, created = self.store.episode("inc", "hash", "error", NOW, "inc#1#0")
        self.assertFalse(created)
        self.assertEqual(str(same["investigation_id"]), "inc#1#0")

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
        self.assertTrue(
            store.admit(other["id"], "lead", "gpt-5.6-sol", NOW).admitted,
            "one lost result must not take another fault's diagnosis with it",
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

    def test_an_operator_is_heard_after_the_investigation_concluded(self):
        """The only moment worth talking to it is the one that used to be refused.

        A diagnosis completes its episode the moment the model answers, and admission
        refused any episode that was not open -- so every operator message sent after a
        conclusion was rejected without a turn, and the operator was told five minutes
        later that it had timed out.
        """
        store = InvestigationStore(":memory:")
        client = StubClient()
        investigator = Investigator(client, store, now=lambda: NOW)
        diagnosis = investigator.investigate("inc", "hash", "what is wrong")
        self.assertEqual(diagnosis.status, "completed")
        self.assertEqual(
            store.db.execute(
                "SELECT status FROM terracompute_investigation_episodes"
            ).fetchone()["status"],
            "completed",
        )
        answer = investigator.converse("inc", "hash", "dont restart it, replace it")
        self.assertEqual((answer.status, answer.reason), ("completed", None))
        self.assertIn(("resume", "thr"), client.calls, "it must speak in the same thread")
        # Talking to it changes nothing about what it concluded: the episode stays
        # finished, so the next diagnosis of the same evidence still says it again
        # rather than paying to reason about it twice.
        self.assertEqual(
            store.db.execute(
                "SELECT status FROM terracompute_investigation_episodes"
            ).fetchone()["status"],
            "completed",
        )
        self.assertEqual(investigator.investigate("inc", "hash", "what is wrong").status, "unchanged")
        store.close()

    def test_talking_is_measured_and_never_metered(self):
        """A person asking is never told to come back tomorrow.

        What a budget defends against is this system looping at three in the morning.
        An operator asks deliberately, one message at a time, from a verified member of
        the group -- so their turns are recorded, and counted separately, and no
        arithmetic about them can refuse one.
        """
        store = InvestigationStore(":memory:")
        investigator = Investigator(StubClient(), store, now=lambda: NOW)
        investigator.investigate("inc", "hash", "what is wrong", investigation_id="inv-1")
        # Spend everything the machine is allowed for this fault.
        store.db.execute(
            "UPDATE terracompute_investigation_turns SET reported_tokens=?",
            (InvestigationStore.MAX_INVESTIGATION_TOKENS,),
        )
        store.db.commit()
        reasons = [
            investigator.converse(
                "inc", "hash", f"message {index}", investigation_id="inv-1"
            ).reason
            for index in range(6)
        ]
        self.assertEqual(reasons, [None] * 6, "an operator was refused for spending")
        operator_turns = store.db.execute(
            "SELECT COUNT(*) FROM terracompute_investigation_turns WHERE operator=1"
        ).fetchone()[0]
        self.assertEqual(operator_turns, 6, "talking was not measured")
        # The machine itself is still stopped on this fault: unmetered is not uncounted,
        # and the six conversations did not buy it any more room either.
        later, _ = store.episode("inc", "b" * 64, "error", NOW, "inv-1")
        self.assertEqual(
            store.admit(later["id"], "lead", "gpt-5.6-sol", NOW).reason,
            "investigation-token-cap",
        )
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
