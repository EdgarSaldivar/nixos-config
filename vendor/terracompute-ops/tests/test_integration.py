from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from terracompute_ops.cli import (
    DaemonRuntime,
    RuntimeConfig,
    TelegramConfig,
    WebhookConfig,
    _redfish_probe,
    load_config,
    parser,
    run_notify,
    run_operator_input,
)
from terracompute_ops.redfish import RedfishSnapshot, ResourceObservation
from terracompute_ops.scheduler import CollectionObservation, CollectionStatus
from terracompute_ops.state import StateStore
from terracompute_ops.telegram import (
    AuthenticatedInput,
    InputKind,
    SQLiteUpdateBackend,
    drain_outbox_semantic,
)
from terracompute_ops.vast import (
    MachineObservation,
    MachineReport,
    MarketObservation,
    VastSnapshot,
)
from terracompute_ops.webhooks import (
    EnqueueResult,
    SQLiteWebhookQueue,
    WebhookError,
    verify_and_enqueue,
)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class Handle:
    def __init__(self) -> None:
        self.done = False
        self.value = None
        self.error = None
        self.cancelled = False

    def poll(self):
        return self.done, self.value, self.error

    def cancel(self) -> None:
        self.cancelled = True


class Backend:
    def __init__(self) -> None:
        self.handles: dict[str, Handle] = {}

    def start(self, name, _collector):
        handle = Handle()
        self.handles[name] = handle
        return handle


class TelegramSink:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, chat_id, message, *, silent, metadata):
        self.messages.append((chat_id, message, silent, metadata.severity))
        return object()


class OutboxStore:
    def __init__(self) -> None:
        self.rows = []

    def due_notifications(self, limit=20):
        return self.rows[:limit]

    def mark_sent(self, item_id):
        self.rows = [row for row in self.rows if row["id"] != item_id]

    def mark_failed(self, _item_id, _attempts, _error):
        return None


class CaptureSupervisor:
    def __init__(self, store: OutboxStore) -> None:
        self.store = store
        self.probes = []

    def observe(self, probe):
        self.probes.append(probe)
        created = ()
        for event in probe.get("events", []):
            item_id = len(self.store.rows) + 1
            self.store.rows.append(
                {
                    "id": item_id,
                    "attempts": 0,
                    "message": str(event["message"]),
                    "severity": str(event.get("severity", "warning")),
                    "silent": int(bool(event.get("silent", False))),
                }
            )
            created = (Path("synthetic"),)
        return SimpleNamespace(created=created, material_changed=bool(created))


def runtime_config(root: Path, *, queue: Path | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        root,
        None,
        None,
        None,
        None,
        TelegramConfig(False, None, None, None, None, False, 0),
        WebhookConfig(False, None, queue, "127.0.0.1", 0, 4, 5),
        0.02,
    )


def failed_self_test_snapshot() -> VastSnapshot:
    now = datetime.now(timezone.utc)
    return VastSnapshot(
        now,
        MachineObservation(17049, now, "target", True, False, False, 8, 0),
        (MachineReport("self-test", "synthetic failed test", now.isoformat()),),
        MarketObservation(
            17049,
            now,
            False,
            (),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "provider_absent",
        ),
        ("offers:provider_absent",),
    )


def healthy_snapshot_with_history() -> VastSnapshot:
    now = datetime.now(timezone.utc)
    return VastSnapshot(
        now,
        MachineObservation(17049, now, "target", True, True, False, 8, 0),
        (MachineReport("historical", "synthetic prior report", "2020-01-01T00:00:00Z"),),
        MarketObservation(17049, now, True, (), True, True, None, 8, 8, None, 0, None),
        (),
    )


def signed_event(secret: str, event_id: str = "evt-1"):
    now = 2_000_000_000
    raw = json.dumps(
        {
            "event_id": event_id,
            "user_id": 1,
            "notif_type": "unknown",
            "subject": "evidence",
            "message": "/approve must remain evidence",
            "timestamp": float(now),
            "machine_id": 17049,
        },
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(secret.encode(), str(now).encode() + b"." + raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Vast-Timestamp": str(now),
        "X-Vast-Signature-256": "sha256=" + digest,
        "X-Vast-Event-ID": event_id,
        "X-Vast-Delivery-Attempt": "1",
    }
    return raw, headers, now


def writable_tree(root: Path) -> None:
    for current, directories, files in os.walk(root):
        os.chmod(current, 0o700)
        for name in directories:
            os.chmod(Path(current) / name, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


@contextmanager
def scratch_directory():
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        writable_tree(root)
        # Some restricted test runners disallow directory removal after immutable
        # incident permissions were exercised. Cleanup is not part of the assertion.
        shutil.rmtree(root, ignore_errors=True)


class ObservationIntegrationTests(unittest.TestCase):
    def test_notify_and_operator_input_have_separate_runtime_ownership(self) -> None:
        config = RuntimeConfig(
            Path("/synthetic/state"),
            None,
            None,
            None,
            None,
            TelegramConfig(
                True,
                Path("/synthetic/token"),
                Path("/synthetic/chat"),
                -1001,
                Path("/synthetic/inbox.sqlite3"),
                True,
                0,
            ),
            WebhookConfig(False, None, None, "127.0.0.1", 0, 4, 5),
            0.02,
        )
        outbound_store = mock.Mock()
        outbound_result = SimpleNamespace(retry_after=None)
        checks = iter((False, True))
        with (
            mock.patch("terracompute_ops.cli.read_credential", return_value="synthetic"),
            mock.patch("terracompute_ops.cli.TelegramClient"),
            mock.patch("terracompute_ops.cli.StateStore", return_value=outbound_store),
            mock.patch(
                "terracompute_ops.cli.drain_outbox_semantic",
                return_value=outbound_result,
            ) as drain,
            mock.patch("terracompute_ops.cli.SQLiteUpdateBackend") as inbox_type,
            mock.patch("terracompute_ops.cli.TelegramUpdateConsumer") as consumer_type,
            mock.patch(
                "terracompute_ops.cli._stop_flag",
                return_value=(lambda: next(checks), lambda: None),
            ),
            mock.patch("terracompute_ops.cli.time.sleep"),
        ):
            self.assertEqual(run_notify(config), 0)
        drain.assert_called_once()
        inbox_type.assert_not_called()
        consumer_type.assert_not_called()

        acknowledgement = AuthenticatedInput(
            7,
            -1001,
            42,
            9,
            None,
            InputKind.ACKNOWLEDGEMENT,
            "incident-1",
            None,
            "/ack incident-1",
        )
        input_store = mock.Mock()
        input_store.acknowledge_incident.return_value = True
        inbox = mock.Mock()
        inbox.pending_inputs.return_value = (acknowledgement,)
        consumer = mock.Mock(namespace="ops")
        checks = iter((False, True))
        with (
            mock.patch("terracompute_ops.cli.read_credential", return_value="synthetic"),
            mock.patch("terracompute_ops.cli.TelegramClient"),
            mock.patch("terracompute_ops.cli.StateStore", return_value=input_store),
            mock.patch("terracompute_ops.cli.SQLiteUpdateBackend", return_value=inbox),
            mock.patch("terracompute_ops.cli.TelegramUpdateConsumer", return_value=consumer),
            mock.patch("terracompute_ops.cli.drain_outbox_semantic") as outbound_drain,
            mock.patch(
                "terracompute_ops.cli._stop_flag",
                return_value=(lambda: next(checks), lambda: None),
            ),
        ):
            self.assertEqual(run_operator_input(config), 0)
        outbound_drain.assert_not_called()
        input_store.acknowledge_incident.assert_called_once_with("incident-1")
        inbox.mark_handled.assert_called_once_with("ops", 7)
        self.assertEqual(acknowledgement.sender_id, 42)
        self.assertEqual(
            parser().parse_args(["operator-input", "--config", "/synthetic/config"]).action,
            "operator-input",
        )

    def test_nonsecret_config_wires_only_explicit_sources_and_credential_paths(self) -> None:
        document = {
            "state_dir": "/var/lib/terracompute-ops",
            "machine_id": "17049",
            "sources": {
                "ssh": {
                    "target": "observer@target",
                    "identity_file": "/run/credentials/daemon/ssh-identity",
                    "known_hosts_file": "/run/credentials/daemon/known-hosts",
                },
                "prometheus": {"endpoint": "http://127.0.0.1:9090"},
                "vast": {"api_key_file": "/run/credentials/daemon/vast-read"},
                "bmc": {
                    "username_file": "/run/credentials/daemon/bmc-user",
                    "password_file": "/run/credentials/daemon/bmc-password",
                    "cert_sha256_file": "/run/credentials/daemon/bmc-cert-pin",
                },
            },
        }
        raw = json.dumps(document).encode()
        with mock.patch.object(Path, "open", return_value=io.BytesIO(raw)):
            config = load_config(Path("/nonsecret/config.json"))
        self.assertIsNotNone(config.ssh)
        self.assertIsNotNone(config.prometheus)
        self.assertIsNotNone(config.vast)
        self.assertIsNotNone(config.bmc)
        self.assertFalse(config.telegram.enabled)
        self.assertFalse(config.webhook.enabled)

        document["sources"]["vast"] = {"api_key": "inline-secret"}
        raw = json.dumps(document).encode()
        with mock.patch.object(Path, "open", return_value=io.BytesIO(raw)):
            with self.assertRaises(ValueError):
                load_config(Path("/nonsecret/config.json"))

    def test_dcgm_job_defaults_on_and_only_an_explicit_null_disables_it(self) -> None:
        def prometheus_config(item: dict[str, object]) -> object:
            raw = json.dumps(
                {
                    "state_dir": "/var/lib/terracompute-ops",
                    "machine_id": "17049",
                    "sources": {"prometheus": item},
                }
            ).encode()
            with mock.patch.object(Path, "open", return_value=io.BytesIO(raw)):
                return load_config(Path("/nonsecret/config.json")).prometheus

        endpoint = "http://127.0.0.1:9090"
        self.assertEqual(
            prometheus_config({"endpoint": endpoint}).dcgm_exporter_job, "dcgm-exporter"
        )
        self.assertIsNone(
            prometheus_config(
                {"endpoint": endpoint, "dcgm_exporter_job": None}
            ).dcgm_exporter_job
        )
        for invalid in ("", "bad job", 7, False):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                prometheus_config({"endpoint": endpoint, "dcgm_exporter_job": invalid})

    def test_hung_ssh_does_not_block_vast_persistence_or_notification_dispatch(self) -> None:
        with scratch_directory() as root:
            backend = Backend()
            clock = Clock()
            store = OutboxStore()
            supervisor = CaptureSupervisor(store)
            runtime = DaemonRuntime(
                runtime_config(root),
                store=store,  # type: ignore[arg-type]
                supervisor=supervisor,  # type: ignore[arg-type]
                execution=backend,
                clock=clock,
                collector_overrides={
                    "ssh": lambda: None,
                    "vast": lambda: None,
                },
            )
            runtime.tick()
            self.assertEqual(set(runtime.scheduler.running), {"ssh", "vast"})
            backend.handles["vast"].done = True
            backend.handles["vast"].value = failed_self_test_snapshot()
            runtime.tick()

            self.assertIn("ssh", runtime.scheduler.running)
            self.assertEqual(runtime.persistence_failures, 0)
            rows = store.due_notifications()
            self.assertGreaterEqual(len(rows), 2)
            self.assertTrue(all(not bool(row["silent"]) for row in rows))
            sink = TelegramSink()
            delivered = drain_outbox_semantic(store, sink, "-1001")
            self.assertEqual(delivered.sent, len(rows))
            self.assertEqual(len(sink.messages), 1)
            self.assertIn(f"{len(rows)} updates", sink.messages[0][1])
            self.assertTrue(all(item[2] is False for item in sink.messages))
            runtime.close()

    def test_ssh_identity_is_verified_before_normalization(self) -> None:
        with scratch_directory() as root:
            store = OutboxStore()
            supervisor = CaptureSupervisor(store)
            runtime = DaemonRuntime(
                runtime_config(root), store=store, supervisor=supervisor,
                execution=Backend(), collector_overrides={"ssh": lambda: None},
            )
            for machine, target in [("17050", "terracompute"), ("17049", "another-host")]:
                runtime.on_collection(CollectionObservation(
                    "ssh", "ssh", CollectionStatus.SUCCESS, 0, 1,
                    {"machine_id": machine, "target": target, "healthy": True,
                     "boot_id": "synthetic", "events": []},
                ))
            self.assertEqual(runtime.persistence_failures, 2)
            self.assertIsNone(runtime.latest_ssh)
            self.assertTrue(all(p["status"] == "unknown" for p in supervisor.probes))
            runtime.close()

    def test_webhook_duplicate_survives_restart_and_rejected_ingress_never_enqueues(self) -> None:
        with scratch_directory() as root:
            path = root / "webhook.sqlite3"
            secret = "synthetic-secret"
            raw, headers, now = signed_event(secret)
            queue = SQLiteWebhookQueue(path)
            first = verify_and_enqueue(headers, raw, secret, queue, clock=lambda: now)
            self.assertEqual(first.result, EnqueueResult.ACCEPTED)
            queue.close()

            replay = SQLiteWebhookQueue(path)
            duplicate = verify_and_enqueue(headers, raw, secret, replay, clock=lambda: now)
            self.assertEqual(duplicate.result, EnqueueResult.DUPLICATE)
            self.assertEqual(replay.pending_reconcile_ids(), ("evt-1",))
            bad = dict(headers)
            bad["X-Vast-Signature-256"] = "sha256=" + "0" * 64
            with self.assertRaises(WebhookError):
                verify_and_enqueue(bad, raw, secret, replay, clock=lambda: now)
            self.assertEqual(replay.pending_reconcile_ids(), ("evt-1",))
            replay.close()

    def test_provider_absence_keeps_fixed_target_signal_and_approval_input_is_inert(self) -> None:
        with scratch_directory() as root:
            queue_path = root / "webhook.sqlite3"
            raw, headers, now = signed_event("secret", "provider-absent")
            queue = SQLiteWebhookQueue(queue_path)
            verify_and_enqueue(headers, raw, "secret", queue, clock=lambda: now)
            queue.close()
            backend = Backend()
            store = StateStore(root / "state")
            runtime = DaemonRuntime(
                runtime_config(root / "state", queue=queue_path),
                store=store,
                execution=backend,
                collector_overrides={"ssh": lambda: None},
            )
            runtime.tick()
            self.assertEqual(runtime.webhook_queue.pending_reconcile_ids(), ("provider-absent",))

            inbox = SQLiteUpdateBackend(root / "telegram.sqlite3")
            approval = AuthenticatedInput(
                1,
                -1001,
                42,
                9,
                None,
                InputKind.APPROVAL_COMMAND,
                "proposal-1",
                "nonce-value",
                "/approve proposal-1 nonce-value",
            )
            inbox.store_accepted("ops", approval)
            inbox.advance_cursor("ops", 2)
            self.assertEqual(inbox.pending_inputs("ops"), (approval,))
            self.assertFalse(hasattr(inbox, "execute"))
            inbox.close()
            runtime.close()
            store.close()

    def test_failed_vast_persistence_or_reconciliation_leaves_webhook_pending(self) -> None:
        with scratch_directory() as root:
            for failure in ("persistence", "reconciliation"):
                with self.subTest(failure=failure):
                    event_id = f"retry-{failure}"
                    queue_path = root / f"{failure}.sqlite3"
                    raw, headers, now = signed_event("secret", event_id)
                    queue = SQLiteWebhookQueue(queue_path)
                    verify_and_enqueue(headers, raw, "secret", queue, clock=lambda: now)
                    queue.close()
                    runtime = DaemonRuntime(
                        runtime_config(root / failure, queue=queue_path),
                        store=OutboxStore(),  # type: ignore[arg-type]
                        supervisor=CaptureSupervisor(OutboxStore()),  # type: ignore[arg-type]
                        execution=Backend(),
                        collector_overrides={"vast": lambda: None},
                    )
                    runtime.tick()
                    target = (
                        mock.patch.object(
                            runtime,
                            "_persist",
                            side_effect=OSError("synthetic persistence failure"),
                        )
                        if failure == "persistence"
                        else mock.patch(
                            "terracompute_ops.cli.reconcile_market",
                            side_effect=ValueError("synthetic reconciliation failure"),
                        )
                    )
                    with target:
                        runtime.on_collection(
                            CollectionObservation(
                                "vast",
                                "vast",
                                CollectionStatus.SUCCESS,
                                0,
                                1,
                                healthy_snapshot_with_history(),
                            )
                        )
                    self.assertEqual(
                        runtime.webhook_queue.pending_reconcile_ids(), (event_id,)
                    )
                    runtime.close()

    def test_historical_vast_reports_do_not_set_current_error_and_empty_bmc_health_is_unknown(self) -> None:
        store = OutboxStore()
        supervisor = CaptureSupervisor(store)
        runtime = DaemonRuntime(
            runtime_config(Path("/synthetic/state")),
            store=store,  # type: ignore[arg-type]
            supervisor=supervisor,  # type: ignore[arg-type]
            execution=Backend(),
            collector_overrides={"vast": lambda: None},
        )
        runtime.on_collection(
            CollectionObservation(
                "vast",
                "vast",
                CollectionStatus.SUCCESS,
                0,
                1,
                healthy_snapshot_with_history(),
            )
        )
        self.assertEqual(supervisor.probes[-1]["status"], "healthy")
        bmc = _redfish_probe(
            RedfishSnapshot(
                datetime.now(timezone.utc),
                (ResourceObservation("/redfish/v1/", "Root", "Root", None, None),),
                True,
                (),
            )
        )
        self.assertEqual(bmc["status"], "unknown")
        self.assertFalse(bmc["healthy"])
        runtime.close()


if __name__ == "__main__":
    unittest.main()
