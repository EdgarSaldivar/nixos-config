from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops import investigator_runtime as runtime_module
from terracompute_ops.investigator import InvestigationStore
from terracompute_ops.investigator_runtime import (
    MAX_REQUEST_BYTES,
    InvestigatorRuntime,
    InvestigatorRuntimeConfig,
    InvestigatorRuntimeError,
)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64


class ScriptedTransport:
    def __init__(
        self,
        *,
        account=True,
        quota=True,
        model=True,
        report="GPU evidence is internally consistent.",
        fail=None,
        blocker=None,
    ):
        self.account = account
        self.quota = quota
        self.model = model
        self.report = report
        self.fail = fail
        self.blocker = blocker
        self.incoming = []
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)
        method = message.get("method")
        if method == "initialized":
            return
        if self.fail == method:
            raise RuntimeError("synthetic detail that must not be persisted")
        if self.blocker is not None and method == "account/read":
            self.blocker[0].set()
            self.blocker[1].wait(5)
        request_id = message.get("id")
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "account/read":
            result = (
                {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
                if self.account
                else {"account": None, "requiresOpenaiAuth": True}
            )
        elif method == "account/rateLimits/read":
            result = {
                "rateLimits": {
                    "primary": {"usedPercent": 0 if self.quota else 100}
                }
            }
        elif method == "model/list":
            result = {
                "data": [
                    {
                        "model": "gpt-5.6-sol" if self.model else "unavailable-model",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    }
                ]
            }
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "thread/resume":
            result = {"thread": {"id": message["params"]["threadId"]}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1", "status": "inProgress"}}
            self.incoming.extend(
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "delta": self.report,
                        },
                    },
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "tokenUsage": {"total": {"totalTokens": 123}},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    },
                ]
            )
        else:
            raise AssertionError(f"unexpected method category: {method}")
        self.incoming.append({"id": request_id, "result": result})

    def receive(self, _timeout):
        if not self.incoming:
            raise TimeoutError
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


class InvestigatorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".investigator-runtime-", dir=Path.cwd()
        )
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.home = self.root / "service-home"
        self.home.mkdir(mode=0o700)
        self.config = InvestigatorRuntimeConfig(
            request_spool=self.root / "requests",
            result_spool=self.root / "results",
            database_path=self.root / "state" / "investigator.sqlite",
            service_home=self.home,
            app_server_argv=("/nix/store/pinned-codex/bin/codex", "app-server"),
            poll_seconds=0.25,
        )
        self.transports = []

    def tearDown(self):
        self.temporary.cleanup()

    def factory(self, **options):
        def create(*_args, **_kwargs):
            transport = ScriptedTransport(**options)
            self.transports.append(transport)
            return transport

        return create

    def runtime(self, factory=None, **kwargs):
        return InvestigatorRuntime(
            self.config,
            clock=lambda: NOW,
            transport_factory=factory or self.factory(),
            **kwargs,
        )

    @staticmethod
    def document(request_id="request-1", **changes):
        value = {
            "schema_version": 1,
            "request_id": request_id,
            "machine_id": "17049",
            "incident_id": "incident-1",
            "evidence_hash": HASH,
            "severity": "error",
            "prompt": "Analyze the sanitized evidence.",
        }
        value.update(changes)
        return value

    def publish_as_producer(self, runtime, **kwargs):
        """As the real producer publishes: readable by the group both services share."""
        path = self.publish(runtime, **kwargs)
        path.chmod(0o640)
        return path

    def publish(self, runtime, document=None, *, name=None, raw=None):
        value = document or self.document()
        filename = name or f"{value.get('request_id', 'request-1')}.json"
        path = runtime.pending / filename
        if raw is None:
            raw = json.dumps(value)
        path.write_text(raw, encoding="utf-8")
        path.chmod(0o600)
        return path

    def result(self, request_id="request-1"):
        return json.loads(
            (self.config.result_spool / "completed" / f"{request_id}.json").read_text()
        )

    def quarantine_result(self):
        paths = list((self.config.result_spool / "quarantine").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    # -- the producer bridge ------------------------------------------------------

    PRODUCER = 4242

    # The group bits the commissioned bridge grants. Production additionally sets
    # sticky on pending and setgid on completed; the runtime ignores bits outside
    # 0o077 by construction, and an unprivileged build sandbox cannot set setgid.
    SPOOLS = (
        ("requests", 0o710), ("requests/pending", 0o730), ("requests/claimed", 0o700),
        ("results", 0o710), ("results/completed", 0o770), ("results/quarantine", 0o700),
        ("state", 0o700),
    )

    def shut_spools(self):
        """Every directory closed, as a runtime with no producer requires."""
        os.chmod(self.root, 0o700)
        for relative, _mode in self.SPOOLS:
            path = self.root / relative
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)

    def open_spools(self, producer=PRODUCER):
        """The directory modes the commissioned bridge installs, and a runtime on them."""
        # The root is traverse-only for the bridge; a private root would put every
        # grant below it out of reach.
        os.chmod(self.root, 0o710)
        for relative, mode in self.SPOOLS:
            path = self.root / relative
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(mode)
        self.config = replace(self.config, producer_uid=producer)
        return self.runtime()

    def owned_by(self, uid):
        """Make request files look owned by `uid`, as another service's would."""
        real = os.fstat

        def fake(fd):
            status = real(fd)
            fields = list(status)[:10]
            fields[4] = uid
            return os.stat_result(fields)

        return mock.patch.object(runtime_module.os, "fstat", fake)

    def test_a_named_producer_may_ask_and_may_read_the_answer(self) -> None:
        runtime = self.open_spools()
        self.publish_as_producer(runtime)
        with self.owned_by(self.PRODUCER):
            outcome = runtime.run_iteration()
        self.assertEqual(outcome.state, "completed")
        answer = self.config.result_spool / "completed" / "request-1.json"
        self.assertEqual(stat.S_IMODE(answer.stat().st_mode), 0o640, "the producer cannot read it")

    def test_a_request_the_runtime_could_not_open_is_refused(self) -> None:
        """A producer's request is owned by the producer, so 0600 shuts us out.

        This is the shape of a live failure: the file was published exactly as the
        contract then demanded, and the runtime could not read a word of it.
        """
        runtime = self.open_spools()
        request = self.publish(runtime)
        request.chmod(0o600)
        with self.owned_by(self.PRODUCER):
            outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("quarantined", "request-file-invalid"))
        self.assertFalse(self.transports)

    def test_without_a_named_producer_another_users_request_is_refused(self) -> None:
        runtime = self.runtime()
        self.publish(runtime)
        with self.owned_by(self.PRODUCER):
            outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("quarantined", "request-file-invalid"))
        self.assertFalse(self.transports, "a foreign request reached the model")

    def test_a_third_user_is_refused_although_a_producer_is_named(self) -> None:
        runtime = self.open_spools()
        self.publish_as_producer(runtime)
        with self.owned_by(self.PRODUCER + 1):
            outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("quarantined", "request-file-invalid"))
        self.assertFalse(self.transports, "a foreign request reached the model")

    def test_answers_stay_private_when_nobody_is_named(self) -> None:
        runtime = self.runtime()
        self.publish(runtime)
        runtime.run_iteration()
        answer = self.config.result_spool / "completed" / "request-1.json"
        self.assertEqual(stat.S_IMODE(answer.stat().st_mode), 0o600)

    def test_the_grant_reaches_the_two_spool_leaves_and_nothing_else(self) -> None:
        """Claimed work, the quarantine, the database and the home stay unreachable."""
        for relative in ("requests/claimed", "results/quarantine", "state"):
            self.open_spools()  # The commissioned modes, then one directory too many.
            (self.root / relative).chmod(0o750)
            with self.assertRaises(InvestigatorRuntimeError) as raised:
                self.runtime()
            self.assertEqual(raised.exception.reason, "filesystem-permissions-invalid")
        self.open_spools()
        self.home.chmod(0o750)
        with self.assertRaises(InvestigatorRuntimeError) as raised:
            self.runtime()
        self.assertEqual(raised.exception.reason, "filesystem-permissions-invalid")
        self.home.chmod(0o700)

    def test_no_grant_ever_reaches_other_users(self) -> None:
        self.open_spools()
        (self.root / "results" / "completed").chmod(0o777)
        with self.assertRaises(InvestigatorRuntimeError) as raised:
            self.runtime()
        self.assertEqual(raised.exception.reason, "filesystem-permissions-invalid")

    def test_the_spools_stay_shut_when_nobody_is_named(self) -> None:
        """No producer means no grant: each widened directory is refused on its own."""
        for relative, mode in self.SPOOLS:
            if mode == 0o700:
                continue
            self.shut_spools()
            (self.root / relative).chmod(mode)
            with self.assertRaises(InvestigatorRuntimeError) as raised:
                self.runtime()
            self.assertEqual(raised.exception.reason, "filesystem-permissions-invalid", relative)

    def test_a_strict_umask_cannot_take_the_grant_away(self) -> None:
        runtime = self.open_spools()
        self.publish_as_producer(runtime)
        previous = os.umask(0o077)
        try:
            with self.owned_by(self.PRODUCER):
                runtime.run_iteration()
        finally:
            os.umask(previous)
        answer = self.config.result_spool / "completed" / "request-1.json"
        self.assertEqual(stat.S_IMODE(answer.stat().st_mode), 0o640, "the producer cannot read it")

    def test_a_grant_nobody_can_reach_is_refused_at_startup(self) -> None:
        """The bug this check exists for: leaves opened under a private root.

        Every grant below an unreachable ancestor is worthless, and the failure is
        silent -- requests never arrive and diagnosis quietly falls back -- so it must
        stop the runtime rather than be discovered in production.
        """
        self.open_spools()
        os.chmod(self.root, 0o700)
        with self.assertRaises(InvestigatorRuntimeError) as raised:
            self.runtime()
        self.assertEqual(raised.exception.reason, "producer-path-unreachable")
        # With no producer named there is nobody to shut out, so it is not a fault.
        self.config = replace(self.config, producer_uid=None)
        self.shut_spools()
        self.runtime()

    def test_a_producer_that_stops_reading_stops_the_work(self) -> None:
        """Unconsumed answers are backpressure, never something to delete."""
        runtime = self.open_spools()
        completed = self.config.result_spool / "completed"
        for index in range(self.config.max_spool_entries + 1):
            (completed / f"stale-{index}.json").write_text("{}")
        self.publish_as_producer(runtime)
        with self.owned_by(self.PRODUCER), self.assertRaises(InvestigatorRuntimeError) as raised:
            runtime.run_iteration()
        self.assertEqual(raised.exception.reason, "spool-entry-limit")
        self.assertTrue((runtime.pending / "request-1.json").exists(), "the request was dropped")

    def test_a_producer_identity_is_bounded(self) -> None:
        for uid in (0, os.geteuid(), -1, True, "4242", 2**31):
            with self.assertRaises(InvestigatorRuntimeError) as raised:
                replace(self.config, producer_uid=uid)
            self.assertEqual(raised.exception.reason, "producer-uid-invalid", uid)

    def test_complete_request_uses_strict_target_and_fixed_result_schema(self):
        runtime = self.runtime()
        self.publish(runtime)
        outcome = runtime.run_iteration()
        self.assertEqual(outcome.state, "completed")
        result = self.result()
        self.assertEqual(result["machine_id"], "17049")
        self.assertEqual(result["reported_tokens"], 123)
        self.assertEqual(result["report"], "GPU evidence is internally consistent.")
        self.assertNotIn("prompt", result)
        self.assertFalse(any(runtime.claims.iterdir()))
        self.assertEqual(self.config.database_path.stat().st_mode & 0o777, 0o600)

    def test_nonprivate_request_file_is_quarantined_before_runtime(self):
        runtime = self.runtime()
        request = self.publish(runtime)
        request.chmod(0o644)  # Readable by anyone at all.
        outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("quarantined", "request-file-invalid"))
        self.assertFalse(self.transports)

    def test_wrong_target_is_quarantined_without_runtime(self):
        runtime = self.runtime()
        self.publish(runtime, self.document(machine_id="17050"))
        outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("quarantined", "request-target-mismatch"))
        self.assertEqual(self.quarantine_result()["reason"], "request-target-mismatch")
        self.assertFalse(self.transports)

    def test_schema_size_path_and_duplicate_key_rejection(self):
        cases = [
            (self.document(path="../private"), None, "request-schema-invalid"),
            (self.document(incident_id="../private"), None, "request-schema-invalid"),
            (self.document(schema_version=True), None, "request-schema-invalid"),
            (self.document(severity=[]), None, "request-schema-invalid"),
            (self.document(prompt="\ud800"), None, "request-schema-invalid"),
            (None, "x" * (MAX_REQUEST_BYTES + 1), "request-size-limit"),
            (
                None,
                '{"schema_version":1,"schema_version":1}',
                "request-schema-invalid",
            ),
        ]
        for index, (document, raw, reason) in enumerate(cases):
            with self.subTest(index=index):
                if index:
                    self.tearDown()
                    self.setUp()
                runtime = self.runtime()
                self.publish(runtime, document, raw=raw)
                outcome = runtime.run_iteration()
                self.assertEqual((outcome.state, outcome.reason), ("quarantined", reason))
                self.assertFalse(self.transports)

    def test_symlink_and_invalid_filename_are_never_followed(self):
        runtime = self.runtime()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.document()), encoding="utf-8")
        link = runtime.pending / "request-1.json"
        link.symlink_to(outside)
        outcome = runtime.run_iteration()
        self.assertEqual(outcome.state, "quarantined")
        self.assertFalse(link.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(self.quarantine_result()["reason"], "request-file-invalid")

        invalid = runtime.pending / "bad name.json"
        invalid.write_text("{}", encoding="utf-8")
        outcome = runtime.run_iteration()
        self.assertEqual(outcome.state, "quarantined")
        self.assertFalse(invalid.exists())

    def test_spool_count_is_bounded(self):
        config = InvestigatorRuntimeConfig(
            request_spool=self.config.request_spool,
            result_spool=self.config.result_spool,
            database_path=self.config.database_path,
            service_home=self.config.service_home,
            app_server_argv=self.config.app_server_argv,
            max_spool_entries=2,
        )
        runtime = InvestigatorRuntime(config, clock=lambda: NOW, transport_factory=self.factory())
        for index in range(3):
            (runtime.pending / f"request-{index}.json").write_text("{}")
        with self.assertRaisesRegex(InvestigatorRuntimeError, "spool-entry-limit"):
            runtime.run_iteration()

    def test_claimed_request_recovers_after_restart(self):
        first = self.runtime()
        self.publish(first)
        os.rename(first.pending / "request-1.json", first.claims / "request-1.json")
        second = self.runtime()
        self.assertEqual(second.run_iteration().state, "completed")
        self.assertEqual(len(self.transports), 1)

    def test_incident_and_evidence_hash_are_idempotent(self):
        runtime = self.runtime()
        self.publish(runtime)
        self.assertEqual(runtime.run_iteration().state, "completed")
        self.publish(runtime, self.document("request-2"))
        self.assertEqual(runtime.run_iteration().state, "unchanged")
        turn_starts = [
            message
            for transport in self.transports
            for message in transport.sent
            if message.get("method") == "turn/start"
        ]
        self.assertEqual(len(turn_starts), 1)
        self.assertEqual(self.result("request-2")["reason"], "unchanged-evidence")

    def test_existing_result_consumes_replayed_claim_without_runtime(self):
        runtime = self.runtime()
        self.publish(runtime)
        self.assertEqual(runtime.run_iteration().state, "completed")
        self.publish(runtime)
        os.rename(runtime.pending / "request-1.json", runtime.claims / "request-1.json")
        prior_count = len(self.transports)
        self.assertEqual(runtime.run_iteration().state, "idempotent")
        self.assertEqual(len(self.transports), prior_count)

    def test_unknown_in_flight_is_fail_closed_before_process_start(self):
        runtime = self.runtime()
        store = InvestigationStore(self.config.database_path)
        episode, _ = store.episode("older", "b" * 64, "error", NOW)
        admitted = store.admit(episode["id"], "lead", "gpt-5.6-sol", NOW)
        self.assertTrue(admitted.admitted)
        store.close()
        self.publish(runtime)
        outcome = runtime.run_iteration()
        self.assertEqual((outcome.state, outcome.reason), ("unavailable", "unknown-in-flight"))
        self.assertFalse(self.transports)
        self.assertEqual(self.result()["reason"], "unknown-in-flight")

    def test_private_home_exact_argv_and_no_api_key_fallback(self):
        calls = []

        def factory(argv, **kwargs):
            calls.append((argv, kwargs))
            transport = ScriptedTransport()
            self.transports.append(transport)
            return transport

        runtime = self.runtime(factory)
        self.publish(runtime)
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "not-forwarded", "CODEX_HOME": "/not-forwarded"},
            clear=False,
        ):
            runtime.run_iteration()
        self.assertEqual(calls[0][0], self.config.app_server_argv)
        environment = calls[0][1]["environment"]
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_HOME", environment)

    def test_nonprivate_or_symlink_home_is_rejected_without_auth_inspection(self):
        os.chmod(self.home, 0o755)
        with self.assertRaisesRegex(InvestigatorRuntimeError, "filesystem-permissions-invalid"):
            self.runtime()
        os.chmod(self.home, 0o700)
        alternate = self.root / "alternate-home"
        alternate.mkdir(mode=0o700)
        self.home.rmdir()
        self.home.symlink_to(alternate, target_is_directory=True)
        with self.assertRaisesRegex(InvestigatorRuntimeError, "filesystem-symlink-rejected"):
            self.runtime()

    def test_database_symlink_and_configuration_traversal_are_rejected(self):
        state = self.config.database_path.parent
        state.mkdir(mode=0o700)
        target = state / "target.sqlite"
        target.touch(mode=0o600)
        self.config.database_path.symlink_to(target)
        with self.assertRaisesRegex(InvestigatorRuntimeError, "database-path-invalid"):
            self.runtime()
        with self.assertRaisesRegex(InvestigatorRuntimeError, "configuration-path-traversal"):
            InvestigatorRuntimeConfig(
                request_spool=self.root / "segment" / ".." / "requests-two",
                result_spool=self.root / "results-two",
                database_path=self.root / "state-two" / "investigator.sqlite",
                service_home=self.home,
                app_server_argv=self.config.app_server_argv,
            )

    def test_app_server_argv_rejects_non_server_and_inline_secret_arguments(self):
        for argv in (
            ("/nix/store/pinned-codex/bin/codex",),
            ("/nix/store/pinned-codex/bin/codex", "--quiet", "app-server"),
            ("/nix/store/pinned-codex/bin/codex", "app-server", "--api-key=value"),
        ):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(InvestigatorRuntimeError, "app-server-argv-invalid"):
                    InvestigatorRuntimeConfig(
                        request_spool=self.root / "request-other",
                        result_spool=self.root / "result-other",
                        database_path=self.root / "state-other" / "investigator.sqlite",
                        service_home=self.home,
                        app_server_argv=argv,
                    )

    def test_auth_quota_and_model_unavailability_never_start_turn(self):
        for label, options, expected in (
            ("auth", {"account": False}, "auth-or-quota-unavailable"),
            ("quota", {"quota": False}, "auth-or-quota-unavailable"),
            ("model", {"model": False}, "model-unavailable"),
        ):
            with self.subTest(label=label):
                if label != "auth":
                    self.tearDown()
                    self.setUp()
                runtime = self.runtime(self.factory(**options))
                self.publish(runtime)
                outcome = runtime.run_iteration()
                self.assertEqual(outcome.reason, expected)
                methods = [message.get("method") for message in self.transports[0].sent]
                self.assertNotIn("turn/start", methods)

    def test_report_and_runtime_failures_are_sanitized(self):
        prompt = "Unique request prose that must not be copied."
        report = (
            f"{prompt}\nInspect /var/lib/private/item\n"
            "Authorization: synthetic-marker\nSafe diagnosis remains."
        )
        runtime = self.runtime(self.factory(report=report))
        self.publish(runtime, self.document(prompt=prompt))
        self.assertEqual(runtime.run_iteration().state, "completed")
        encoded = json.dumps(self.result())
        self.assertNotIn(prompt, encoded)
        self.assertNotIn("/var/lib/private/item", encoded)
        self.assertNotIn("synthetic-marker", encoded)
        self.assertIn("Safe diagnosis remains.", encoded)

        self.tearDown()
        self.setUp()
        runtime = self.runtime(self.factory(fail="initialize"))
        self.publish(runtime)
        self.assertEqual(runtime.run_iteration().reason, "runtime-unavailable")
        encoded = json.dumps(self.result())
        self.assertNotIn("synthetic detail", encoded)

    def test_helpers_are_disabled_and_no_helper_path_is_called(self):
        runtime = self.runtime()
        self.publish(runtime)
        runtime.run_iteration()
        methods = [message.get("method", "") for message in self.transports[0].sent]
        self.assertFalse(any("helper" in method or "agent" in method for method in methods))
        turn = next(message for message in self.transports[0].sent if message.get("method") == "turn/start")
        self.assertEqual(turn["params"]["approvalPolicy"], "never")
        self.assertEqual(turn["params"]["sandboxPolicy"]["access"]["readableRoots"], [])

    def test_bounded_loop_uses_injected_sleeper(self):
        sleeps = []
        runtime = self.runtime(sleeper=sleeps.append)
        self.assertEqual(runtime.run(3), 3)
        self.assertEqual(sleeps, [0.25, 0.25])
        with self.assertRaisesRegex(InvestigatorRuntimeError, "iteration-bound-invalid"):
            runtime.run(0)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires fork")
    def test_two_processes_cannot_contend_for_one_claim(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        runtime = self.runtime()
        self.publish(runtime)

        def child():
            child_runtime = InvestigatorRuntime(
                self.config,
                clock=lambda: NOW,
                transport_factory=lambda *_args, **_kwargs: ScriptedTransport(
                    blocker=(ready, release)
                ),
            )
            child_runtime.run_iteration()

        process = context.Process(target=child)
        process.start()
        self.assertTrue(ready.wait(5))
        contender = self.runtime()
        self.assertEqual(contender.run_iteration().state, "busy")
        release.set()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue((runtime.completed / "request-1.json").is_file())


if __name__ == "__main__":
    unittest.main()
