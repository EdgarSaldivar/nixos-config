"""Focused tests for the Phase 2 capability-broker foundation.

The broker is disabled by default and holds no credential; these tests prove
the containment, identity, durability, budget, redaction, provenance, and
recovery properties the rearchitecture plan requires, entirely locally --
including the adversarial cases: credential path reads, sandbox-runner
absence, task spoofing, replay and crash recovery, cancellation, and
symlink races against the direct file primitives.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import stat
import threading
import time
import unittest
import unittest.mock
import tempfile
from pathlib import Path

from terracompute_ops.capability_broker import (
    BROKER_SCHEMA_VERSION,
    EVIDENCE_AUTHORITY,
    EVIDENCE_TRUST,
    FAMILIES,
    FEATURE_FLAG_ENV,
    MACHINE_ID,
    PROVENANCE,
    READ_ONLY_ADAPTER_CONTRACT,
    SANDBOX_RUNNER_CONTRACT,
    SANDBOX_RUNNER_ENV,
    SAFE_ENVIRONMENT_NAMES,
    STATE_ROOT_ENV,
    WORKSPACE_ROOT_ENV,
    BrokerBudgets,
    BrokerConfig,
    BrokerError,
    BrokerToolServer,
    CapabilityBroker,
    EvidenceLedger,
    ReadOnlyAdapter,
    SandboxCommand,
    SandboxHandle,
    SandboxOutcome,
    SandboxRunner,
    WorkspaceRegistry,
    broker_enabled,
    build_capability_broker,
    config_from_environment,
    parse_broker_request,
    parse_broker_request_json,
    redact_secrets,
    safe_subprocess_environment,
    sanitize_controller_state,
)

_REQUEST_COUNTER = itertools.count()


def request_document(**overrides):
    document = {
        "schema_version": BROKER_SCHEMA_VERSION,
        "request_id": f"req-{next(_REQUEST_COUNTER)}",
        "task_id": "task-a",
        "machine_id": MACHINE_ID,
        "family": "observe_target",
        "operation": "run",
        "arguments": {"command": "nvidia-smi"},
        "effects": [],
        "timeout_seconds": 5.0,
        "max_output_bytes": 4096,
    }
    document.update(overrides)
    return document


class RecordingAdapter(ReadOnlyAdapter):
    """A contract-conforming read-only adapter that records every invocation."""

    def __init__(self, reply="output", raises=None):
        self.reply = reply
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []
        self.cancel_called = False

    def invoke(self, operation, arguments):
        self.calls.append((operation, dict(arguments)))
        if self.raises is not None:
            raise self.raises
        if callable(self.reply):
            return self.reply(operation, arguments)
        return self.reply

    def cancel(self):
        self.cancel_called = True


class BlockingAdapter(ReadOnlyAdapter):
    """Blocks until cancelled: a slow adapter that honors its contract."""

    def __init__(self):
        self._release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.cancel_called = False

    def invoke(self, operation, arguments):
        self.entered.set()
        self._release.wait(30)
        self.finished.set()
        raise BrokerError("adapter-cancelled")

    def cancel(self):
        self.cancel_called = True
        self._release.set()


class DefiantAdapter(ReadOnlyAdapter):
    """Ignores cancel: sleeps briefly past the grace to prove quarantine."""

    def __init__(self, sleep_seconds=1.0):
        self.sleep_seconds = sleep_seconds
        self.cancel_called = False

    def invoke(self, operation, arguments):
        time.sleep(self.sleep_seconds)
        return "late"

    def cancel(self):
        self.cancel_called = True


class FakeSandboxHandle(SandboxHandle):
    """A finished external process: wait reports its outcome immediately, and
    kill (a no-op on dead work) makes wait confirm a well-formed dead state."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.killed = False

    def wait(self, timeout_seconds):
        return self.outcome

    def kill(self):
        self.killed = True
        self.outcome = SandboxOutcome("error", None, "")


class FakeSandboxRunner(SandboxRunner):
    """An attested runner double that records the typed command it receives."""

    contract = SANDBOX_RUNNER_CONTRACT

    def __init__(self, outcome=None):
        self.commands: list[SandboxCommand] = []
        self.handles: list[FakeSandboxHandle] = []
        self.outcome = outcome or SandboxOutcome("ok", 0, "sandboxed output")

    def start(self, command):
        self.commands.append(command)
        outcome = self.outcome(command) if callable(self.outcome) else self.outcome
        handle = FakeSandboxHandle(outcome)
        self.handles.append(handle)
        return handle


class HangingSandboxHandle(SandboxHandle):
    """A command that never exits on its own but dies honestly when killed."""

    def __init__(self):
        self._dead = threading.Event()
        self.kill_called = False

    def wait(self, timeout_seconds):
        if self._dead.wait(timeout_seconds):
            return SandboxOutcome("error", None, "killed at broker deadline")
        return None

    def kill(self):
        self.kill_called = True
        self._dead.set()


class HangingSandboxRunner(SandboxRunner):
    """Every start hangs until the broker kills it: contract-honoring but slow."""

    contract = SANDBOX_RUNNER_CONTRACT

    def __init__(self):
        self.handles: list[HangingSandboxHandle] = []
        self._lock = threading.Lock()

    def start(self, command):
        handle = HangingSandboxHandle()
        with self._lock:
            self.handles.append(handle)
        return handle


class UnkillableSandboxRunner(SandboxRunner):
    """Attests the contract but breaks it: work survives kill()."""

    contract = SANDBOX_RUNNER_CONTRACT

    def __init__(self):
        self.kill_called = False
        runner = self

        class Handle(SandboxHandle):
            def wait(self, timeout_seconds):
                time.sleep(min(timeout_seconds, 0.05))
                return None  # Still running, before and after kill.

            def kill(self):
                runner.kill_called = True

        self._handle_type = Handle

    def start(self, command):
        return self._handle_type()


class BrokerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        base = Path(self._temp.name)
        self.state_root = base / "state"
        self.workspace_root = base / "workspaces"
        self.outside = base / "outside"
        self.outside.mkdir()

    def config(self, **overrides) -> BrokerConfig:
        values = {
            "state_root": self.state_root,
            "allowed_workspace_roots": (self.workspace_root,),
            "enabled": True,
        }
        values.update(overrides)
        return BrokerConfig(**values)

    def broker(self, **adapters) -> CapabilityBroker:
        config = adapters.pop("config", None) or self.config()
        return CapabilityBroker(config, **adapters)


class FeatureFlagTests(BrokerTestCase):
    def test_disabled_by_default_and_never_constructed_without_the_flag(self) -> None:
        self.assertFalse(broker_enabled({}))
        self.assertIsNone(config_from_environment({}))
        self.assertIsNone(build_capability_broker(environment={}))
        with self.assertRaises(BrokerError) as caught:
            CapabilityBroker(self.config(enabled=False))
        self.assertEqual(caught.exception.reason, "capability-broker-disabled")

    def test_flag_plus_roots_builds_an_enabled_broker(self) -> None:
        environment = {
            FEATURE_FLAG_ENV: "1",
            WORKSPACE_ROOT_ENV: str(self.workspace_root),
            STATE_ROOT_ENV: str(self.state_root),
        }
        broker = build_capability_broker(environment=environment)
        self.assertIsNotNone(broker)
        self.assertTrue(broker.config.enabled)
        self.assertEqual(broker.config.machine_id, MACHINE_ID)
        self.assertIsNone(broker.config.sandbox_runner_command)
        self.assertIsNone(broker.sandbox_runner)

    def test_environment_can_name_the_sandbox_runner_but_never_invents_one(self) -> None:
        environment = {
            FEATURE_FLAG_ENV: "1",
            WORKSPACE_ROOT_ENV: str(self.workspace_root),
            STATE_ROOT_ENV: str(self.state_root),
            SANDBOX_RUNNER_ENV: "/run/current-system/sw/bin/sandbox-runner",
        }
        broker = build_capability_broker(environment=environment)
        self.assertEqual(
            broker.config.sandbox_runner_command,
            "/run/current-system/sw/bin/sandbox-runner",
        )
        # Naming a path is configuration, not injection: run still fails closed.
        self.assertIsNone(broker.sandbox_runner)

    def test_flag_without_roots_fails_closed(self) -> None:
        with self.assertRaises(BrokerError):
            config_from_environment({FEATURE_FLAG_ENV: "1"})


class RequestSchemaTests(BrokerTestCase):
    def test_valid_request_parses_and_digest_is_stable(self) -> None:
        document = request_document()
        request = parse_broker_request(document)
        self.assertEqual(request.family, "observe_target")
        self.assertEqual(request.digest(), parse_broker_request(document).digest())

    def test_unknown_schema_version_is_rejected(self) -> None:
        with self.assertRaises(BrokerError) as caught:
            parse_broker_request(request_document(schema_version=99))
        self.assertEqual(caught.exception.reason, "request-schema-version-unknown")

    def test_extra_and_missing_fields_are_rejected(self) -> None:
        with self.assertRaises(BrokerError):
            parse_broker_request({**request_document(), "extra": 1})
        short = request_document()
        del short["task_id"]
        with self.assertRaises(BrokerError):
            parse_broker_request(short)

    def test_duplicate_json_fields_are_rejected(self) -> None:
        text = json.dumps(request_document())[:-1] + ',"family":"workspace"}'
        with self.assertRaises(BrokerError) as caught:
            parse_broker_request_json(text)
        self.assertEqual(caught.exception.reason, "request-duplicate-field")

    def test_credential_shaped_arguments_are_rejected(self) -> None:
        for arguments in (
            {"api_key": "value"},
            {"note": "password=hunter2"},
            {"argv": ["curl", "-u", "user:pass"]},
        ):
            with self.assertRaises(BrokerError) as caught:
                parse_broker_request(request_document(arguments=arguments))
            self.assertEqual(caught.exception.reason, "request-arguments-unsafe")

    def test_malformed_effects_are_rejected(self) -> None:
        for effects in ("production", [1], ["no spaces allowed"], ["x" * 100]):
            with self.assertRaises(BrokerError) as caught:
                parse_broker_request(request_document(effects=effects))
            self.assertEqual(caught.exception.reason, "request-effects-invalid")

    def test_unknown_family_is_rejected_but_operations_are_free_text(self) -> None:
        with self.assertRaises(BrokerError):
            parse_broker_request(request_document(family="teleport"))
        novel = parse_broker_request(request_document(
            operation="zpool status -v && cat /sys/kernel/iommu_groups/0/type"
        ))
        self.assertIn("zpool", novel.operation)

    def test_argument_text_keeps_its_newlines(self) -> None:
        request = parse_broker_request(request_document(
            family="workspace", operation="write_file",
            arguments={"path": "a.txt", "content": "line one\nline two\n"},
        ))
        self.assertIn("\n", request.arguments["content"])


class IdentityBindingTests(BrokerTestCase):
    def test_wrong_machine_is_denied_and_result_names_the_bound_machine(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        result = broker.handle(parse_broker_request(request_document(machine_id="17050")))
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "machine-identity-mismatch")
        self.assertEqual(result.machine_id, MACHINE_ID)

    def test_configured_machine_id_is_used_everywhere(self) -> None:
        adapter = RecordingAdapter()
        broker = self.broker(
            config=self.config(machine_id="99999"), observe_adapter=adapter
        )
        denied = broker.handle(parse_broker_request(request_document()))
        self.assertEqual((denied.status, denied.reason), ("denied", "machine-identity-mismatch"))
        self.assertEqual(denied.machine_id, "99999")
        self.assertEqual(adapter.calls, [])
        served = broker.handle(parse_broker_request(request_document(machine_id="99999")))
        self.assertEqual(served.status, "ok")
        self.assertEqual(served.machine_id, "99999")
        broker.bind_workspace("task-a")
        grant_body = json.loads((self.state_root / "grants" / "task-a.json").read_text())
        self.assertEqual(grant_body["machine_id"], "99999")
        record_path = self.state_root / "requests" / "task-a" / f"{served.request_id}.json"
        self.assertEqual(json.loads(record_path.read_text())["machine_id"], "99999")

    def test_workspace_request_for_an_unbound_task_is_denied(self) -> None:
        broker = self.broker()
        result = broker.handle(parse_broker_request(request_document(
            family="workspace", operation="list_dir", arguments={"path": "."},
        )))
        self.assertEqual((result.status, result.reason), ("denied", "task-not-bound"))


class AdapterContractTests(BrokerTestCase):
    def test_bare_callables_are_refused_at_construction(self) -> None:
        for kwargs in (
            {"observe_adapter": lambda command: "output"},
            {"controller_state_adapter": lambda operation, arguments: {}},
            {"research_adapters": {"web": lambda operation, arguments: "x"}},
        ):
            with self.assertRaises(BrokerError) as caught:
                self.broker(**kwargs)
            self.assertEqual(caught.exception.reason, "adapter-contract-mismatch")

    def test_wrong_contract_string_is_refused(self) -> None:
        adapter = RecordingAdapter()
        adapter.contract = "some-other-contract"
        with self.assertRaises(BrokerError):
            self.broker(observe_adapter=adapter)
        self.assertEqual(READ_ONLY_ADAPTER_CONTRACT.split(":")[0],
                         "read-only-cancellable-adapter-v2")

    def test_stale_v1_adapter_attestation_is_refused(self) -> None:
        # The v1 contract predates the credential-path and network walls; an
        # adapter still attesting it has not accepted them and gets no work.
        adapter = RecordingAdapter()
        adapter.contract = (
            "read-only-cancellable-adapter-v1:no-mutation,no-workspace-access,"
            "cancel-terminates-promptly"
        )
        with self.assertRaises(BrokerError) as caught:
            self.broker(observe_adapter=adapter)
        self.assertEqual(caught.exception.reason, "adapter-contract-mismatch")

    def test_adapter_contract_names_the_credential_and_network_walls(self) -> None:
        # The attested contract itself must state: no credential-path access,
        # no direct network, and only the purpose-limited broker-owned
        # transport the deployment wires in.
        self.assertIn("no-credential-paths", READ_ONLY_ADAPTER_CONTRACT)
        self.assertIn("no-direct-network", READ_ONLY_ADAPTER_CONTRACT)
        self.assertIn("broker-owned-transport-only", READ_ONLY_ADAPTER_CONTRACT)
        self.assertIn("no-mutation", READ_ONLY_ADAPTER_CONTRACT)
        self.assertIn("no-workspace-access", READ_ONLY_ADAPTER_CONTRACT)
        self.assertIn("cancel-terminates-promptly", READ_ONLY_ADAPTER_CONTRACT)


class ObserveTargetTests(BrokerTestCase):
    def test_novel_read_reaches_the_adapter_verbatim_without_any_topic_list(self) -> None:
        adapter = RecordingAdapter(reply="eight GPUs visible")
        broker = self.broker(observe_adapter=adapter)
        command = "ls -l /proc/*/fd 2>/dev/null | grep nvidia | head -5"
        result = broker.handle(parse_broker_request(request_document(
            arguments={"command": command},
        )))
        self.assertEqual(result.status, "ok")
        self.assertEqual(adapter.calls[0][0], command)
        self.assertEqual(result.provenance, PROVENANCE["observe_target"])
        self.assertEqual(result.trust, EVIDENCE_TRUST)
        self.assertEqual(result.authority, EVIDENCE_AUTHORITY)

    def test_output_is_redacted(self) -> None:
        broker = self.broker(
            observe_adapter=RecordingAdapter("ok line\napi_key: SECRETVALUE\nlast line")
        )
        result = broker.handle(parse_broker_request(request_document()))
        self.assertNotIn("SECRETVALUE", result.output)
        self.assertIn("[sensitive-content-redacted]", result.output)
        self.assertIn("ok line", result.output)

    def test_output_budget_truncates(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter("many words here " * 6000))
        result = broker.handle(parse_broker_request(request_document(max_output_bytes=1000)))
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output.encode("utf-8")), 1000)

    def test_time_budget_cancels_the_adapter_and_returns_timeout(self) -> None:
        adapter = BlockingAdapter()
        broker = self.broker(observe_adapter=adapter)
        result = broker.handle(parse_broker_request(request_document(timeout_seconds=0.2)))
        self.assertEqual((result.status, result.reason), ("timeout", "time-budget-exceeded"))
        self.assertTrue(adapter.cancel_called)
        # The worker thread was joined inside the grace period: no abandonment.
        self.assertTrue(adapter.finished.is_set())

    def test_uncancellable_adapter_is_quarantined_not_abandoned(self) -> None:
        adapter = DefiantAdapter(sleep_seconds=1.0)
        broker = self.broker(
            observe_adapter=adapter, adapter_cancel_grace_seconds=0.2
        )
        violated = broker.handle(parse_broker_request(request_document(timeout_seconds=0.2)))
        self.assertEqual((violated.status, violated.reason), ("error", "adapter-cancel-violation"))
        self.assertTrue(adapter.cancel_called)
        revoked = broker.handle(parse_broker_request(request_document()))
        self.assertEqual((revoked.status, revoked.reason), ("error", "adapter-revoked"))
        time.sleep(1.0)  # Let the defiant worker exit before the test ends.


class EffectScreeningTests(BrokerTestCase):
    def test_tenant_effect_fails_closed_everywhere(self) -> None:
        adapter = RecordingAdapter()
        broker = self.broker(observe_adapter=adapter)
        result = broker.handle(parse_broker_request(request_document(
            effects=["TENANT"],
        )))
        self.assertEqual((result.status, result.reason), ("denied", "tenant-approval-required"))
        self.assertEqual(adapter.calls, [])

    def test_production_external_network_host_effects_fail_closed(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        for effect in ("production", "external", "network", "host"):
            result = broker.handle(parse_broker_request(request_document(
                effects=[effect],
            )))
            self.assertEqual(
                (result.status, result.reason),
                ("denied", "effect-approval-required"), effect,
            )

    def test_unknown_effects_fail_closed(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        result = broker.handle(parse_broker_request(request_document(
            effects=["quantum-entanglement"],
        )))
        self.assertEqual((result.status, result.reason), ("denied", "effect-approval-required"))

    def test_workspace_effect_is_denied_outside_the_workspace_family(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        result = broker.handle(parse_broker_request(request_document(
            effects=["workspace"],
        )))
        self.assertEqual((result.status, result.reason), ("denied", "effect-approval-required"))

    def test_workspace_and_read_effects_are_granted_to_the_workspace_family(self) -> None:
        broker = self.broker(sandbox_runner=FakeSandboxRunner())
        broker.bind_workspace("task-a")
        result = broker.handle(parse_broker_request(request_document(
            family="workspace", operation="run",
            arguments={"argv": ["true"]}, effects=["workspace", "read"],
        )))
        self.assertEqual(result.status, "ok")

    def test_ordinary_evidence_text_is_not_effect_screened(self) -> None:
        # Enforcement is typed, not keyword theater: argument text and key
        # names mentioning tenants or writability are data, not declarations.
        adapter = RecordingAdapter()
        broker = self.broker(observe_adapter=adapter)
        result = broker.handle(parse_broker_request(request_document(
            arguments={
                "command": "true",
                "note": "the tenant container C.12345 looked writable to production",
                "writable": True,
            },
        )))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(adapter.calls), 1)


class SandboxRunnerTests(BrokerTestCase):
    def bound_broker(self, **adapters) -> CapabilityBroker:
        broker = self.broker(**adapters)
        broker.bind_workspace("task-a")
        return broker

    def workspace_request(self, operation: str, arguments, **overrides):
        return parse_broker_request(request_document(
            family="workspace", operation=operation, arguments=arguments, **overrides
        ))

    def test_run_fails_closed_without_a_sandbox_runner(self) -> None:
        broker = self.bound_broker()
        result = broker.handle(self.workspace_request("run", {"argv": ["true"]}))
        self.assertEqual((result.status, result.reason), ("denied", "sandbox-runner-unavailable"))

    def test_runner_without_the_exact_attested_contract_is_refused(self) -> None:
        class UnattestedRunner:
            contract = "sandbox-runner-v1:mostly-isolated"

            def start(self, command):
                return FakeSandboxHandle(SandboxOutcome("ok", 0, ""))

        with self.assertRaises(BrokerError) as caught:
            self.broker(sandbox_runner=UnattestedRunner())
        self.assertEqual(caught.exception.reason, "sandbox-runner-contract-mismatch")
        # The attested contract itself promises an external, killable process
        # with no network and task-root-only IO.
        self.assertIn("external-process", SANDBOX_RUNNER_CONTRACT)
        self.assertIn("no-network", SANDBOX_RUNNER_CONTRACT)
        self.assertIn("no-read-outside-task-root", SANDBOX_RUNNER_CONTRACT)
        self.assertIn("no-write-outside-task-root", SANDBOX_RUNNER_CONTRACT)
        self.assertIn("no-credential-paths", SANDBOX_RUNNER_CONTRACT)
        self.assertIn("kill-terminates-all-work", SANDBOX_RUNNER_CONTRACT)

    def test_retired_synchronous_v1_runner_is_refused(self) -> None:
        # The v1 contract executed run() synchronously in this process: work
        # the broker could neither cancel nor kill. It is not grandfathered.
        class LegacySynchronousRunner:
            contract = (
                "sandbox-runner-v1:no-network,no-read-outside-task-root,"
                "no-write-outside-task-root,no-credential-paths,kill-on-deadline"
            )

            def run(self, command):
                return SandboxOutcome("ok", 0, "")

        with self.assertRaises(BrokerError) as caught:
            self.broker(sandbox_runner=LegacySynchronousRunner())
        self.assertEqual(caught.exception.reason, "sandbox-runner-contract-mismatch")

    def test_runner_with_the_contract_but_no_start_is_refused(self) -> None:
        class RunOnlyRunner:
            contract = SANDBOX_RUNNER_CONTRACT

            def run(self, command):
                return SandboxOutcome("ok", 0, "")

        with self.assertRaises(BrokerError) as caught:
            self.broker(sandbox_runner=RunOnlyRunner())
        self.assertEqual(caught.exception.reason, "sandbox-runner-contract-mismatch")

    def test_broker_module_has_no_network_or_process_surface(self) -> None:
        # An outbound network attempt or a locally spawned workspace process
        # cannot originate in this module: it imports no socket, HTTP, or
        # subprocess machinery. Execution exists only behind the attested
        # injected runner, and transport only behind injected adapters.
        import terracompute_ops.capability_broker as module

        source = Path(module.__file__).read_text()
        for forbidden in (
            "import socket", "import ssl", "import http", "import urllib",
            "import subprocess", "import asyncio",
        ):
            self.assertNotIn(forbidden, source)

    def test_runner_receives_only_the_typed_bounded_fields(self) -> None:
        runner = FakeSandboxRunner()
        base = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "AWS_SECRET_ACCESS_KEY": "leak-me-not",
            "TELEGRAM_TOKEN": "leak-me-not-either",
            "HOME": "/root",
            "CREDENTIALS_DIRECTORY": "/run/credentials/service",
        }
        broker = self.bound_broker(sandbox_runner=runner, base_environment=base)
        result = broker.handle(self.workspace_request(
            "run", {"argv": ["/usr/bin/env"]}, timeout_seconds=3.0, max_output_bytes=2048,
        ))
        self.assertEqual(result.status, "ok")
        command = runner.commands[0]
        field_names = {field.name for field in dataclasses.fields(SandboxCommand)}
        self.assertEqual(field_names, {
            "task_id", "task_root", "argv", "environment",
            "timeout_seconds", "max_output_bytes",
        })
        grant = broker.workspaces.grant("task-a")
        self.assertEqual(command.task_id, "task-a")
        self.assertEqual(command.task_root, grant.root)
        self.assertEqual(command.argv, ("/usr/bin/env",))
        self.assertLessEqual(command.timeout_seconds, 3.0)
        self.assertLessEqual(command.max_output_bytes, 2048)
        # The scrubbed environment: allowlist plus a HOME inside the worktree.
        self.assertLessEqual(
            set(command.environment) - {"HOME"}, set(SAFE_ENVIRONMENT_NAMES)
        )
        self.assertEqual(command.environment["HOME"], str(grant.root))
        flattened = json.dumps(dict(command.environment))
        self.assertNotIn("leak-me-not", flattened)
        self.assertNotIn("/run/credentials", flattened)
        self.assertNotIn("/root", flattened)

    def test_shell_text_travels_as_argv_to_the_runner(self) -> None:
        runner = FakeSandboxRunner()
        broker = self.bound_broker(sandbox_runner=runner)
        result = broker.handle(self.workspace_request(
            "run", {"shell": "printf 'novel-tool-%s' output"},
        ))
        self.assertEqual(result.status, "ok")
        self.assertEqual(runner.commands[0].argv[:2], ("/bin/sh", "-c"))
        self.assertIn("exit=0", result.output)
        self.assertIn("sandboxed output", result.output)

    def test_runner_timeout_and_error_outcomes_are_typed(self) -> None:
        broker = self.bound_broker(
            sandbox_runner=FakeSandboxRunner(outcome=SandboxOutcome("timeout", None, "partial"))
        )
        timed = broker.handle(self.workspace_request("run", {"argv": ["sleep", "99"]}))
        self.assertEqual((timed.status, timed.reason), ("timeout", "time-budget-exceeded"))

        second = self.broker(
            sandbox_runner=FakeSandboxRunner(outcome=SandboxOutcome("error", None, ""))
        )
        second.bind_workspace("task-a")
        failed = second.handle(self.workspace_request("run", {"argv": ["true"]}))
        self.assertEqual((failed.status, failed.reason), ("error", "sandbox-run-failed"))

    def test_malformed_runner_outcome_fails_closed(self) -> None:
        broker = self.bound_broker(sandbox_runner=FakeSandboxRunner(outcome=lambda c: "raw"))
        result = broker.handle(self.workspace_request("run", {"argv": ["true"]}))
        self.assertEqual((result.status, result.reason), ("error", "sandbox-outcome-invalid"))

    def test_runner_output_is_redacted_and_labelled(self) -> None:
        broker = self.bound_broker(sandbox_runner=FakeSandboxRunner(
            outcome=SandboxOutcome("ok", 0, "echoed api_key: SECRETVALUE\nplain")
        ))
        result = broker.handle(self.workspace_request("run", {"argv": ["cat", "x"]}))
        self.assertNotIn("SECRETVALUE", result.output)
        self.assertEqual(result.provenance, PROVENANCE["workspace"])
        self.assertEqual(result.trust, EVIDENCE_TRUST)
        self.assertEqual(result.authority, EVIDENCE_AUTHORITY)


class SandboxDeadlineTests(BrokerTestCase):
    """The broker, not the runner, bounds every dispatched sandbox execution."""

    def bound_broker(self, **adapters) -> CapabilityBroker:
        broker = self.broker(**adapters)
        broker.bind_workspace("task-a")
        return broker

    def run_request(self, **overrides):
        return parse_broker_request(request_document(
            family="workspace", operation="run",
            arguments={"argv": ["sleep", "999"]}, **overrides,
        ))

    def test_hanging_run_is_killed_at_the_broker_deadline(self) -> None:
        runner = HangingSandboxRunner()
        broker = self.bound_broker(sandbox_runner=runner)
        result = broker.handle(self.run_request(timeout_seconds=0.2))
        self.assertEqual((result.status, result.reason), ("timeout", "time-budget-exceeded"))
        self.assertTrue(runner.handles[0].kill_called)

    def test_two_hanging_runs_cannot_deadlock_capacity_or_leak_work(self) -> None:
        # Both concurrency slots fill with hanging commands. The broker must
        # kill both at their deadlines, confirm nothing survived, release both
        # slots, and admit later work normally.
        runner = HangingSandboxRunner()
        config = self.config(budgets=BrokerBudgets(max_concurrent=2))
        broker = self.broker(config=config, sandbox_runner=runner)
        broker.bind_workspace("task-a")
        results: list = []

        def issue() -> None:
            results.append(broker.handle(self.run_request(timeout_seconds=0.3)))

        threads = [threading.Thread(target=issue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(
                (result.status, result.reason), ("timeout", "time-budget-exceeded")
            )
        # No dispatched work survived: every handle was killed.
        self.assertEqual(len(runner.handles), 2)
        self.assertTrue(all(handle.kill_called for handle in runner.handles))
        # Capacity is fully released: a third request is admitted (and again
        # bounded), not refused with concurrency-limit.
        third = broker.handle(self.run_request(timeout_seconds=0.2))
        self.assertEqual((third.status, third.reason), ("timeout", "time-budget-exceeded"))

    def test_work_surviving_kill_quarantines_the_runner_and_frees_the_slot(self) -> None:
        runner = UnkillableSandboxRunner()
        adapter = RecordingAdapter("still serving reads")
        broker = self.broker(
            config=self.config(budgets=BrokerBudgets(max_concurrent=1)),
            sandbox_runner=runner, observe_adapter=adapter,
            runner_kill_grace_seconds=0.1,
        )
        broker.bind_workspace("task-a")
        violated = broker.handle(self.run_request(timeout_seconds=0.1))
        self.assertEqual(
            (violated.status, violated.reason), ("error", "sandbox-kill-violation")
        )
        self.assertTrue(runner.kill_called)
        # The violation fails closed for all later runs instead of dispatching
        # more work beside the survivor.
        quarantined = broker.handle(self.run_request(timeout_seconds=0.1))
        self.assertEqual(
            (quarantined.status, quarantined.reason), ("error", "sandbox-runner-quarantined")
        )
        # And the concurrency slot was released, not pinned by the survivor.
        observed = broker.handle(parse_broker_request(request_document()))
        self.assertEqual(observed.status, "ok")

    def test_uncontrollable_handle_is_quarantined(self) -> None:
        class HandlelessRunner(SandboxRunner):
            contract = SANDBOX_RUNNER_CONTRACT

            def start(self, command):
                return object()  # No wait, no kill: nothing to bound.

        broker = self.bound_broker(sandbox_runner=HandlelessRunner())
        result = broker.handle(self.run_request(timeout_seconds=0.1))
        self.assertEqual((result.status, result.reason), ("error", "sandbox-handle-invalid"))
        second = broker.handle(self.run_request(timeout_seconds=0.1))
        self.assertEqual(
            (second.status, second.reason), ("error", "sandbox-runner-quarantined")
        )

    def test_malformed_outcome_is_destroyed_before_failing_closed(self) -> None:
        runner = FakeSandboxRunner(outcome=lambda command: "raw")
        broker = self.bound_broker(sandbox_runner=runner)
        result = broker.handle(self.run_request(timeout_seconds=0.1))
        self.assertEqual((result.status, result.reason), ("error", "sandbox-outcome-invalid"))
        self.assertTrue(runner.handles[0].killed)
    def workspace_request(self, operation: str, arguments, **overrides):
        return parse_broker_request(request_document(
            family="workspace", operation=operation, arguments=arguments, **overrides
        ))

    def test_write_read_list_round_trip_inside_the_worktree(self) -> None:
        broker = self.bound_broker()
        wrote = broker.handle(self.workspace_request(
            "write_file", {"path": "notes/finding.txt", "content": "line\nline two\n"},
        ))
        self.assertEqual(wrote.status, "ok")
        read = broker.handle(self.workspace_request("read_file", {"path": "notes/finding.txt"}))
        self.assertIn("line two", read.output)
        listed = broker.handle(self.workspace_request("list_dir", {"path": "notes"}))
        self.assertIn("finding.txt", listed.output)

    def test_write_file_syncs_the_containing_directory_before_success(self) -> None:
        # File-content fsync alone is not durability: until the containing
        # directory entry is synced, a crash can lose the name. The write path
        # must fsync the directory (and each newly created parent) before it
        # reports success.
        broker = self.bound_broker()
        grant = broker.workspaces.grant("task-a")
        synced_inodes: set[int] = set()
        real_fsync = os.fsync

        def spying_fsync(fd):
            status = os.fstat(fd)
            if stat.S_ISDIR(status.st_mode):
                synced_inodes.add(status.st_ino)
            real_fsync(fd)

        with unittest.mock.patch("os.fsync", side_effect=spying_fsync):
            wrote = broker.handle(self.workspace_request(
                "write_file", {"path": "notes/deep/finding.txt", "content": "x\n"},
            ))
        self.assertEqual(wrote.status, "ok")
        containing = grant.root / "notes" / "deep"
        self.assertIn(os.stat(containing).st_ino, synced_inodes)
        # The created parent's entry was synced in its own parent too.
        self.assertIn(os.stat(grant.root / "notes").st_ino, synced_inodes)
        self.assertIn(os.stat(grant.root).st_ino, synced_inodes)

    def test_traversal_and_absolute_escapes_are_denied(self) -> None:
        broker = self.bound_broker()
        for path in ("../escape.txt", "a/../../escape.txt", str(self.outside / "x.txt")):
            result = broker.handle(self.workspace_request(
                "write_file", {"path": path, "content": "no"},
            ))
            self.assertEqual(
                (result.status, result.reason), ("denied", "workspace-containment"), path
            )
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_credential_path_read_is_denied(self) -> None:
        broker = self.bound_broker()
        result = broker.handle(self.workspace_request(
            "read_file", {"path": "/run/credentials/terracompute/api-token"},
        ))
        self.assertEqual((result.status, result.reason), ("denied", "workspace-containment"))

    def test_symlink_to_a_credential_file_is_denied(self) -> None:
        broker = self.bound_broker()
        secret = self.outside / "credential-store"
        secret.write_text("credential file content")
        grant = broker.workspaces.grant("task-a")
        (grant.root / "link.txt").symlink_to(secret)
        result = broker.handle(self.workspace_request("read_file", {"path": "link.txt"}))
        self.assertEqual((result.status, result.reason), ("denied", "workspace-containment"))

    def test_parent_directory_swapped_for_a_symlink_is_denied(self) -> None:
        # The TOCTOU shape: a lexically clean path whose parent component is a
        # symlink out of the worktree at open time. The dirfd walk refuses it.
        broker = self.bound_broker()
        grant = broker.workspaces.grant("task-a")
        target_dir = self.outside / "elsewhere"
        target_dir.mkdir()
        (target_dir / "x.txt").write_text("outside content")
        (grant.root / "sub").symlink_to(target_dir)
        read = broker.handle(self.workspace_request("read_file", {"path": "sub/x.txt"}))
        self.assertEqual((read.status, read.reason), ("denied", "workspace-containment"))
        wrote = broker.handle(self.workspace_request(
            "write_file", {"path": "sub/planted.txt", "content": "no"},
        ))
        self.assertEqual((wrote.status, wrote.reason), ("denied", "workspace-containment"))
        self.assertFalse((target_dir / "planted.txt").exists())
        listed = broker.handle(self.workspace_request("list_dir", {"path": "sub"}))
        self.assertEqual((listed.status, listed.reason), ("denied", "workspace-containment"))

    def test_cross_task_worktrees_are_walled(self) -> None:
        broker = self.bound_broker()
        broker.bind_workspace("task-b")
        grant_a = broker.workspaces.grant("task-a")
        (grant_a.root / "private.txt").write_text("belongs to a")
        result = broker.handle(self.workspace_request(
            "read_file", {"path": "../task-a/private.txt"},
            task_id="task-b",
        ))
        self.assertEqual((result.status, result.reason), ("denied", "workspace-containment"))

    def test_safe_environment_is_an_allowlist(self) -> None:
        environment = safe_subprocess_environment(
            Path("/tmp/home"), {"PATH": "/bin", "SECRET": "x", "SSH_AUTH_SOCK": "/sock"},
        )
        self.assertEqual(set(environment), {"PATH", "HOME"})

    def test_unknown_workspace_primitive_is_an_error_not_a_schema_wall(self) -> None:
        broker = self.bound_broker()
        result = broker.handle(self.workspace_request("defragment", {"path": "."}))
        self.assertEqual(
            (result.status, result.reason), ("error", "workspace-operation-unsupported")
        )


class ControllerStateTests(BrokerTestCase):
    def test_credential_shaped_fields_never_cross(self) -> None:
        adapter = RecordingAdapter(reply=lambda operation, arguments: {
            "status": "healthy",
            "api_key": "SECRETVALUE",
            "nested": {"password": "hunter2", "count": 3},
            "note": "Bearer abcdef123456",
        })
        broker = self.broker(controller_state_adapter=adapter)
        result = broker.handle(parse_broker_request(request_document(
            family="controller_state", operation="incidents",
        )))
        self.assertEqual(result.status, "ok")
        self.assertNotIn("SECRETVALUE", result.output)
        self.assertNotIn("hunter2", result.output)
        self.assertNotIn("abcdef123456", result.output)
        self.assertIn("healthy", result.output)
        document = json.loads(result.output)
        self.assertEqual(document["redacted_fields"], 1)
        self.assertEqual(document["nested"]["redacted_fields"], 1)
        self.assertEqual(document["nested"]["count"], 3)

    def test_sanitizer_bounds_depth(self) -> None:
        document: dict = {"leaf": 1}
        for _ in range(20):
            document = {"next": document}
        with self.assertRaises(BrokerError):
            sanitize_controller_state(document)

    def test_missing_adapter_is_an_error(self) -> None:
        broker = self.broker()
        result = broker.handle(parse_broker_request(request_document(
            family="controller_state", operation="incidents",
        )))
        self.assertEqual((result.status, result.reason), ("error", "controller-state-unavailable"))


class ResearchTests(BrokerTestCase):
    def test_injected_adapter_serves_research_with_untrusted_labels(self) -> None:
        adapter = RecordingAdapter(reply="release notes: driver 550 supports Blackwell")
        broker = self.broker(research_adapters={"web_search": adapter})
        result = broker.handle(parse_broker_request(request_document(
            family="research", operation="nvidia driver 550 release notes",
            arguments={"adapter": "web_search", "site": "docs.nvidia.com"},
        )))
        self.assertEqual(result.status, "ok")
        self.assertEqual(adapter.calls[0][0], "nvidia driver 550 release notes")
        self.assertEqual(result.provenance, PROVENANCE["research"])
        self.assertEqual(result.trust, EVIDENCE_TRUST)

    def test_unknown_adapter_is_expressible_but_unavailable(self) -> None:
        broker = self.broker(research_adapters={})
        request = parse_broker_request(request_document(
            family="research", operation="look at the kernel bugzilla",
            arguments={"adapter": "bugzilla"},
        ))
        result = broker.handle(request)  # The request itself validated fine.
        self.assertEqual((result.status, result.reason), ("error", "adapter-unavailable"))

    def test_adapter_output_is_redacted(self) -> None:
        broker = self.broker(research_adapters={
            "web": RecordingAdapter("page said: access-token: STOLENVALUE"),
        })
        result = broker.handle(parse_broker_request(request_document(
            family="research", operation="query", arguments={"adapter": "web"},
        )))
        self.assertNotIn("STOLENVALUE", result.output)


class RequestLedgerTests(BrokerTestCase):
    def test_completed_request_replays_without_executing(self) -> None:
        adapter = RecordingAdapter("observed once")
        broker = self.broker(observe_adapter=adapter)
        document = request_document()
        first = broker.handle(parse_broker_request(document))
        self.assertEqual(first.status, "ok")
        second = broker.handle(parse_broker_request(document))
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(second.output, first.output)
        self.assertEqual(second.artifact_digest, first.artifact_digest)
        self.assertEqual(second.request_digest, first.request_digest)

    def test_replay_survives_a_restart(self) -> None:
        document = request_document()
        first_adapter = RecordingAdapter("observed once")
        first = self.broker(observe_adapter=first_adapter)
        original = first.handle(parse_broker_request(document))
        self.assertEqual(original.status, "ok")

        second_adapter = RecordingAdapter("would be different")
        second = self.broker(observe_adapter=second_adapter)
        replayed = second.handle(parse_broker_request(document))
        self.assertEqual(second_adapter.calls, [])
        self.assertEqual(replayed.output, original.output)
        self.assertEqual(replayed.artifact_digest, original.artifact_digest)

    def test_same_id_with_a_different_digest_fails_closed(self) -> None:
        adapter = RecordingAdapter()
        broker = self.broker(observe_adapter=adapter)
        document = request_document()
        self.assertEqual(broker.handle(parse_broker_request(document)).status, "ok")
        altered = dict(document)
        altered["operation"] = "something else entirely"
        result = broker.handle(parse_broker_request(altered))
        self.assertEqual((result.status, result.reason), ("denied", "request-identity-conflict"))
        self.assertEqual(len(adapter.calls), 1)

    def test_pending_workspace_mutation_after_crash_is_uncertain_never_rerun(self) -> None:
        document = request_document(
            family="workspace", operation="write_file",
            arguments={"path": "a.txt", "content": "x"},
        )
        request = parse_broker_request(document)
        first = self.broker()
        first.bind_workspace("task-a")
        # Simulate the crash window: the pending record was published before
        # dispatch, and the process died before completing it.
        admission = first.requests.admit(request)
        self.assertEqual(admission.kind, "execute")

        runner = FakeSandboxRunner()
        second = self.broker(sandbox_runner=runner)
        result = second.handle(parse_broker_request(document))
        self.assertEqual((result.status, result.reason), ("uncertain", "workspace-outcome-uncertain"))
        self.assertEqual(runner.commands, [])
        grant = second.workspaces.grant("task-a")
        self.assertFalse((grant.root / "a.txt").exists())
        # The uncertainty is durable: replays keep reporting it, never executing.
        again = second.handle(parse_broker_request(document))
        self.assertEqual((again.status, again.reason), ("uncertain", "workspace-outcome-uncertain"))
        self.assertEqual(runner.commands, [])

    def test_pending_read_only_request_may_re_execute_after_crash(self) -> None:
        document = request_document()
        request = parse_broker_request(document)
        first = self.broker(observe_adapter=RecordingAdapter())
        self.assertEqual(first.requests.admit(request).kind, "execute")

        adapter = RecordingAdapter("recovered read")
        second = self.broker(observe_adapter=adapter)
        result = second.handle(parse_broker_request(document))
        self.assertEqual(result.status, "ok")
        self.assertIn("recovered read", result.output)
        self.assertEqual(len(adapter.calls), 1)

    def test_duplicate_in_flight_request_is_denied_not_duplicated(self) -> None:
        adapter = BlockingAdapter()
        broker = self.broker(observe_adapter=adapter)
        document = request_document(timeout_seconds=10.0)
        first: list = []
        thread = threading.Thread(
            target=lambda: first.append(broker.handle(parse_broker_request(document)))
        )
        thread.start()
        self.assertTrue(adapter.entered.wait(5))
        duplicate = broker.handle(parse_broker_request(document))
        adapter.cancel()
        thread.join(timeout=10)
        self.assertEqual((duplicate.status, duplicate.reason), ("denied", "request-in-flight"))
        self.assertEqual(len(first), 1)

    def test_budgets_are_durable_across_restarts(self) -> None:
        config = self.config(budgets=BrokerBudgets(max_requests_per_task=2))
        first = self.broker(config=config, observe_adapter=RecordingAdapter())
        first_document = request_document()
        self.assertEqual(first.handle(parse_broker_request(first_document)).status, "ok")
        self.assertEqual(first.handle(parse_broker_request(request_document())).status, "ok")

        second = self.broker(config=config, observe_adapter=RecordingAdapter("fresh"))
        exhausted = second.handle(parse_broker_request(request_document()))
        self.assertEqual((exhausted.status, exhausted.reason), ("denied", "task-request-budget"))
        # Replaying an admitted request costs nothing and still works.
        replay = second.handle(parse_broker_request(first_document))
        self.assertEqual(replay.status, "ok")

    def test_request_records_are_tamper_evident(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        document = request_document()
        served = broker.handle(parse_broker_request(document))
        self.assertEqual(served.status, "ok")
        record_path = self.state_root / "requests" / "task-a" / f"{served.request_id}.json"
        body = json.loads(record_path.read_text())
        body["request_digest"] = "0" * 64
        record_path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
        fresh = self.broker(observe_adapter=RecordingAdapter())
        result = fresh.handle(parse_broker_request(document))
        self.assertEqual((result.status, result.reason), ("error", "request-record-invalid"))


class BudgetTests(BrokerTestCase):
    def test_concurrency_budget_denies_the_extra_request(self) -> None:
        adapter = BlockingAdapter()
        config = self.config(budgets=BrokerBudgets(max_concurrent=1))
        broker = self.broker(config=config, observe_adapter=adapter)
        first: list = []
        thread = threading.Thread(
            target=lambda: first.append(
                broker.handle(parse_broker_request(request_document(timeout_seconds=10.0)))
            )
        )
        thread.start()
        self.assertTrue(adapter.entered.wait(5))
        second = broker.handle(parse_broker_request(request_document()))
        adapter.cancel()
        thread.join(timeout=10)
        self.assertEqual((second.status, second.reason), ("denied", "concurrency-limit"))
        self.assertEqual(len(first), 1)


class RejectionBoundingTests(BrokerTestCase):
    """Only an admitted request may write durable state.

    A rejection decided before (or instead of) durable admission returns a
    bounded, non-persisted result: no evidence artifact, no request record.
    Otherwise a denial flood -- wrong machine, screened effects, concurrency
    pressure, an exhausted budget -- would grow the ledger without ever
    passing budget admission.
    """

    def durable_footprint(self, broker: CapabilityBroker, task_id: str = "task-a"):
        requests_dir = self.state_root / "requests" / task_id
        records = sorted(p.name for p in requests_dir.glob("*.json")) if requests_dir.exists() else []
        return (broker.ledger.list(task_id), tuple(records))

    def test_machine_and_effect_denials_persist_nothing(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        before = self.durable_footprint(broker)
        mismatched = broker.handle(parse_broker_request(request_document(machine_id="17050")))
        screened = broker.handle(parse_broker_request(request_document(effects=["tenant"])))
        for result in (mismatched, screened):
            self.assertEqual(result.status, "denied")
            self.assertIsNone(result.artifact_digest)
            self.assertEqual(result.output, "")
        self.assertEqual(self.durable_footprint(broker), before)

    def test_budget_exhaustion_denial_flood_grows_nothing(self) -> None:
        config = self.config(budgets=BrokerBudgets(max_requests_per_task=1))
        broker = self.broker(config=config, observe_adapter=RecordingAdapter())
        admitted = broker.handle(parse_broker_request(request_document()))
        self.assertEqual(admitted.status, "ok")
        footprint = self.durable_footprint(broker)
        for _ in range(25):
            denied = broker.handle(parse_broker_request(request_document()))
            self.assertEqual((denied.status, denied.reason), ("denied", "task-request-budget"))
            self.assertIsNone(denied.artifact_digest)
        # Twenty-five uniquely identified denials left no artifact and no record.
        self.assertEqual(self.durable_footprint(broker), footprint)

    def test_concurrency_denial_is_non_persisted(self) -> None:
        adapter = BlockingAdapter()
        config = self.config(budgets=BrokerBudgets(max_concurrent=1))
        broker = self.broker(config=config, observe_adapter=adapter)
        first: list = []
        thread = threading.Thread(
            target=lambda: first.append(
                broker.handle(parse_broker_request(request_document(timeout_seconds=10.0)))
            )
        )
        thread.start()
        self.assertTrue(adapter.entered.wait(5))
        refused = broker.handle(parse_broker_request(request_document()))
        adapter.cancel()
        thread.join(timeout=10)
        self.assertEqual((refused.status, refused.reason), ("denied", "concurrency-limit"))
        self.assertIsNone(refused.artifact_digest)
        # Only the admitted request left durable evidence.
        self.assertEqual(len(self.durable_footprint(broker)[0]), 1)

    def test_identity_conflict_denial_is_non_persisted(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter())
        document = request_document()
        self.assertEqual(broker.handle(parse_broker_request(document)).status, "ok")
        footprint = self.durable_footprint(broker)
        altered = dict(document)
        altered["operation"] = "something else entirely"
        conflict = broker.handle(parse_broker_request(altered))
        self.assertEqual((conflict.status, conflict.reason), ("denied", "request-identity-conflict"))
        self.assertIsNone(conflict.artifact_digest)
        self.assertEqual(self.durable_footprint(broker), footprint)

    def test_admitted_uncertain_outcome_is_still_durably_ledgered(self) -> None:
        # The uncertain verdict rides an admitted pending record, so it is
        # budgeted and must stay durable: replays serve the recorded artifact.
        document = request_document(
            family="workspace", operation="write_file",
            arguments={"path": "a.txt", "content": "x"},
        )
        request = parse_broker_request(document)
        first = self.broker()
        first.bind_workspace("task-a")
        self.assertEqual(first.requests.admit(request).kind, "execute")
        second = self.broker()
        uncertain = second.handle(parse_broker_request(document))
        self.assertEqual(uncertain.status, "uncertain")
        self.assertIsNotNone(uncertain.artifact_digest)
        replay = second.handle(parse_broker_request(document))
        self.assertEqual(replay.artifact_digest, uncertain.artifact_digest)


class EvidenceTests(BrokerTestCase):
    def test_every_result_is_an_immutable_content_addressed_artifact(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter("observed output"))
        result = broker.handle(parse_broker_request(request_document()))
        self.assertIsNotNone(result.artifact_digest)
        loaded = broker.ledger.load("task-a", result.artifact_digest)
        self.assertEqual(loaded["output"], result.output)
        self.assertEqual(loaded["provenance"], PROVENANCE["observe_target"])
        self.assertEqual(loaded["request_digest"], result.request_digest)

    def test_tampered_artifact_is_refused(self) -> None:
        broker = self.broker(observe_adapter=RecordingAdapter("observed output"))
        result = broker.handle(parse_broker_request(request_document()))
        path = broker.ledger.root / "task-a" / f"{result.artifact_digest}.json"
        os.chmod(path, 0o600)
        path.write_text(path.read_text().replace("observed", "doctored"))
        with self.assertRaises(BrokerError) as caught:
            broker.ledger.load("task-a", result.artifact_digest)
        self.assertEqual(caught.exception.reason, "artifact-tampered")

    def test_recording_the_same_content_is_idempotent(self) -> None:
        ledger = EvidenceLedger(self.state_root / "evidence")
        document = {"kind": "task-artifact", "content": "same"}
        self.assertEqual(ledger.record("task-a", document), ledger.record("task-a", document))
        self.assertEqual(len(ledger.list("task-a")), 1)

    def test_partial_artifact_at_the_same_address_is_verified_and_repaired(self) -> None:
        # A crash can leave a partial file only if it bypassed the atomic
        # publish (say, an older writer). record() must never trust bytes at an
        # address without verifying them against that address.
        ledger = EvidenceLedger(self.state_root / "evidence")
        document = {"kind": "task-artifact", "content": "authoritative"}
        digest = ledger.record("task-z", document)
        path = self.state_root / "evidence" / "task-z" / f"{digest}.json"
        os.chmod(path, 0o600)
        path.write_bytes(b'{"kind": "task-ar')  # A torn partial write.
        fresh = EvidenceLedger(self.state_root / "evidence")
        self.assertEqual(fresh.record("task-z", document), digest)
        self.assertEqual(fresh.load("task-z", digest), document)

    def test_ledger_is_isolated_from_workspace_roots(self) -> None:
        with self.assertRaises(BrokerError) as caught:
            EvidenceLedger(
                self.workspace_root / "evidence",
                isolated_from=(self.workspace_root,),
            )
        self.assertEqual(caught.exception.reason, "ledger-not-isolated")


class RestartRecoveryTests(BrokerTestCase):
    def test_grants_and_evidence_survive_a_broker_restart(self) -> None:
        first = self.broker()
        first.bind_workspace("task-a")
        wrote = first.handle(parse_broker_request(request_document(
            family="workspace", operation="write_file",
            arguments={"path": "carried.txt", "content": "survives restart\n"},
        )))
        self.assertEqual(wrote.status, "ok")

        second = self.broker()  # A new process over the same durable state.
        read = second.handle(parse_broker_request(request_document(
            family="workspace", operation="read_file",
            arguments={"path": "carried.txt"},
        )))
        self.assertEqual(read.status, "ok")
        self.assertIn("survives restart", read.output)
        digests = second.ledger.list("task-a")
        self.assertIn(wrote.artifact_digest, digests)
        for digest in digests:
            second.ledger.load("task-a", digest)  # Every artifact still verifies.

    def test_tampered_grant_is_refused_on_recovery(self) -> None:
        first = self.broker()
        first.bind_workspace("task-a")
        grant_path = self.state_root / "grants" / "task-a.json"
        body = json.loads(grant_path.read_text())
        body["root"] = str(self.outside)
        grant_path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
        registry = WorkspaceRegistry((self.workspace_root,), self.state_root, MACHINE_ID)
        with self.assertRaises(BrokerError) as caught:
            registry.grant("task-a")
        self.assertEqual(caught.exception.reason, "grant-tampered")


class RedactionTests(unittest.TestCase):
    def test_credential_lines_and_opaque_tokens_are_removed(self) -> None:
        text = (
            "normal line\n"
            "Authorization: Bearer shhh\n"
            "value " + "A" * 48 + " end\n"
        )
        cleaned = redact_secrets(text)
        self.assertIn("normal line", cleaned)
        self.assertNotIn("shhh", cleaned)
        self.assertNotIn("A" * 48, cleaned)
        self.assertIn("[sensitive-content-redacted]", cleaned)
        self.assertIn("[opaque-value-redacted]", cleaned)


class ToolServerTests(BrokerTestCase):
    def server(self, task_id: str = "task-a") -> BrokerToolServer:
        self.adapter = RecordingAdapter(reply=lambda command, arguments: f"saw: {command}")
        broker = self.broker(observe_adapter=self.adapter)
        broker.bind_workspace("task-a")
        return BrokerToolServer(broker, task_id)

    def test_binds_exactly_one_task_at_construction(self) -> None:
        broker = self.broker()
        with self.assertRaises(BrokerError):
            BrokerToolServer(broker, "../escape")
        server = BrokerToolServer(broker, "task-a")
        self.assertEqual(server.task_id, "task-a")

    def test_lists_exactly_the_four_families_and_requires_request_id(self) -> None:
        server = self.server()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = reply["result"]["tools"]
        self.assertEqual(tuple(tool["name"] for tool in tools), FAMILIES)
        for tool in tools:
            self.assertIn("request_id", tool["inputSchema"]["required"])
            self.assertNotIn("task_id", tool["inputSchema"]["properties"])

    def test_call_routes_to_the_broker_and_labels_the_evidence(self) -> None:
        server = self.server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "observe_target", "arguments": {
                "request_id": "req-load", "operation": "cat /proc/loadavg",
            }},
        })
        self.assertFalse(reply["result"]["isError"])
        document = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["task_id"], "task-a")
        self.assertEqual(document["provenance"], PROVENANCE["observe_target"])
        self.assertEqual(document["trust"], EVIDENCE_TRUST)
        self.assertIn("saw: cat /proc/loadavg", document["output"])

    def test_missing_request_id_is_refused(self) -> None:
        server = self.server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "observe_target", "arguments": {"operation": "x"}},
        })
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("request-id-required", reply["result"]["content"][0]["text"])
        self.assertEqual(self.adapter.calls, [])

    def test_caller_supplied_identity_makes_retries_replay(self) -> None:
        server = self.server()
        message = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "observe_target", "arguments": {
                "request_id": "req-retry", "operation": "uptime",
            }},
        }
        first = server.handle(message)
        second = server.handle({**message, "id": 5})
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            json.loads(first["result"]["content"][0]["text"])["output"],
            json.loads(second["result"]["content"][0]["text"])["output"],
        )

    def test_spoofed_task_identity_is_refused_without_execution(self) -> None:
        server = self.server()
        server.broker.bind_workspace("task-b")
        grant_b = server.broker.workspaces.grant("task-b")
        (grant_b.root / "private.txt").write_text("belongs to b")
        reply = server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "workspace", "arguments": {
                "request_id": "req-spoof", "task_id": "task-b",
                "operation": "read_file", "arguments": {"path": "private.txt"},
            }},
        })
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("task-identity-mismatch", reply["result"]["content"][0]["text"])
        # Nothing was admitted for the spoofed task.
        self.assertFalse(
            (self.state_root / "requests" / "task-b" / "req-spoof.json").exists()
        )

    def test_malformed_call_is_a_tool_error_not_a_crash(self) -> None:
        server = self.server()
        reply = server.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "observe_target", "arguments": {
                "request_id": "req-bad", "operation": "x",
                "arguments": {"api_key": "v"},
            }},
        })
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("request-arguments-unsafe", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
