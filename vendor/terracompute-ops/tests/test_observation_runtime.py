from __future__ import annotations

import hashlib
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from terracompute_ops.capacity import MarketAssessment
from terracompute_ops.cli import (
    TARGET_PROBE_MAX_AGE_SECONDS,
    DaemonRuntime,
    PrometheusFailure,
    RuntimeConfig,
    TelegramConfig,
    WebhookConfig,
    _prometheus_alerts_probe,
    _redfish_probe,
    run_notify,
)
from terracompute_ops.maintenance import evaluate_watchdog
from terracompute_ops.incidents import stable_signature
from terracompute_ops.observation_runtime import (
    HEARTBEAT_FILENAME,
    HeartbeatPublisher,
    LocalStorageAccounting,
    ObservationArchive,
    ObservationRuntimeError,
    RuntimeProgress,
    StorageMeasurement,
)
from terracompute_ops.prometheus import (
    AlertSnapshot,
    AlertState,
    MetricBatch,
    PrometheusAlert,
    Sample,
)
from terracompute_ops.redfish import (
    HardwareIdentity,
    LogEntry,
    RedfishSnapshot,
    ResourceObservation,
    SensorReading,
)
from terracompute_ops.scheduler import (
    CollectionObservation,
    CollectionStatus,
    MAX_EXTERNAL_COLLECTIONS,
    daemon_collectors,
)
from terracompute_ops.state import StateStore
from terracompute_ops.telegram import NotificationDrainResult
from terracompute_ops.vast import (
    MachineObservation,
    MarketObservation,
    VastSnapshot,
)

try:  # unittest discover puts tests/ on sys.path; module-path runs do not.
    from test_capacity import metric_batch as capacity_metric_batch
    from test_capacity import target_probe as capacity_target_probe
except ImportError:
    from tests.test_capacity import metric_batch as capacity_metric_batch
    from tests.test_capacity import target_probe as capacity_target_probe


NOW = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
GIB = 1024**3


class FixedAccounting:
    def __init__(self, used: int = 0, free: int = 30 * GIB):
        self.measurement = StorageMeasurement(used, free)

    def measure(self) -> StorageMeasurement:
        return self.measurement

    def note_written(self, count: int) -> None:
        self.measurement = StorageMeasurement(
            self.measurement.used_bytes + count,
            self.measurement.free_bytes - count,
        )


class IdleExecution:
    def start(self, _name, _collector):
        return SimpleNamespace(
            poll=lambda: (False, None, None), cancel=lambda: True
        )


class CaptureSupervisor:
    def __init__(self) -> None:
        self.probes: list[dict[str, object]] = []

    def observe(self, probe):
        self.probes.append(probe)
        return SimpleNamespace(material_changed=bool(probe.get("events")))


def runtime_config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        root,
        None,
        None,
        None,
        None,
        TelegramConfig(False, None, None, None, None, False, 0),
        WebhookConfig(False, None, None, "127.0.0.1", 0, 4, 5),
        0.02,
    )


def ssh_probe(observed_at: datetime = NOW, bdf: str = "0000:20:00.0") -> dict[str, object]:
    return {
        "target": "terracompute",
        "machine_id": 17049,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "boot_id": "boot-test",
        "healthy": True,
        "events": [],
        "snapshot": {
            "gpu": {
                "pci_count": 1,
                "nvidia_count": 1,
                "gpus": [
                    {
                        "uuid": "GPU-00000000-0000-4000-8000-000000000001",
                        "pci_bdf": bdf,
                        "name": "Synthetic GPU",
                    }
                ],
                "pci_devices": [{"pci_bdf": bdf, "driver": "nvidia"}],
            },
            "system_identity": {
                "motherboard": {"vendor": "Synthetic"},
                "bios": {"version": "1.0"},
            },
        },
    }


class ObservationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".observation-runtime-", dir=Path.cwd(), ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        for directory, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(directory) / name).chmod(0o600)
            for name in directories:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temp.cleanup()

    def test_future_inventory_cannot_poison_valid_capture_and_changes_are_protected(self) -> None:
        store = StateStore(self.root / "ordering", clock=lambda: NOW)
        runtime = DaemonRuntime(
            runtime_config(self.root / "ordering"), store=store,
            execution=IdleExecution(), collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting()
        future = ssh_probe(NOW + timedelta(days=1))
        runtime.on_collection(CollectionObservation(
            "ssh", "ssh", CollectionStatus.SUCCESS, 0, 1, future,
        ))
        self.assertIsNone(runtime.latest_ssh)
        valid = ssh_probe()
        runtime.on_collection(CollectionObservation(
            "ssh", "ssh", CollectionStatus.SUCCESS, 1, 2, valid,
        ))
        self.assertIsNotNone(runtime.latest_ssh)
        count = store.db.execute(
            "SELECT COUNT(*) FROM inventory_probe_captures WHERE state='complete'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        routine = dict(valid, source="ssh", status="healthy")
        routine = copy.deepcopy(routine)
        routine["snapshot"]["gpu"]["gpus"][0]["temperature_c"] = 47
        routine["snapshot"]["gpu"]["gpus"][0]["pstate"] = "P2"
        self.assertFalse(runtime._protected_capture(routine))
        changed = copy.deepcopy(routine)
        changed["snapshot"]["gpu"]["gpus"][0]["pci_bdf"] = "0000:21:00.0"
        self.assertTrue(runtime._protected_capture(changed))
        runtime.close()
        store.close()

    def test_archive_restart_idempotence_exact_inventory_hash_and_uuid_move(self) -> None:
        observed = datetime.now(timezone.utc)
        store = StateStore(self.root / "state")
        runtime = DaemonRuntime(
            runtime_config(self.root / "state"),
            store=store,
            execution=IdleExecution(),
            collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting()  # type: ignore[union-attr]
        first = ssh_probe(observed)
        runtime.on_collection(
            CollectionObservation("ssh", "ssh", CollectionStatus.SUCCESS, 0, 1, first)
        )
        runtime.close()
        store.close()

        reopened = StateStore(self.root / "state")
        runtime = DaemonRuntime(
            runtime_config(self.root / "state"),
            store=reopened,
            execution=IdleExecution(),
            collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting()  # type: ignore[union-attr]
        runtime.on_collection(
            CollectionObservation("ssh", "ssh", CollectionStatus.SUCCESS, 2, 3, first)
        )
        moved = ssh_probe(observed + timedelta(microseconds=1), "0000:21:00.0")
        runtime.on_collection(
            CollectionObservation("ssh", "ssh", CollectionStatus.SUCCESS, 4, 5, moved)
        )
        rows = reopened.db.execute(
            """SELECT sha256,document_json,capture_class
               FROM terracompute_observation_artifacts WHERE source='ssh'
               ORDER BY artifact_id"""
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[2] for row in rows], ["protected", "protected"])
        self.assertEqual(rows[0][0], hashlib.sha256(rows[0][1]).hexdigest())
        provenance = reopened.db.execute(
            """SELECT DISTINCT evidence_ref FROM inventory_attribute_assertions
               WHERE asset_id=?""",
            ("gpu-00000000-0000-4000-8000-000000000001",),
        ).fetchall()
        self.assertTrue(any(row[0] == f"sha256:{rows[0][0]}" for row in provenance))
        self.assertTrue(any(row[0] == f"sha256:{rows[1][0]}" for row in provenance))
        connections = reopened.db.execute(
            "SELECT COUNT(*) FROM inventory_connection_assertions"
        ).fetchone()[0]
        self.assertEqual(connections, 0)
        runtime.close()
        reopened.close()

    def test_archive_disk_pressure_protects_critical_and_rejects_oversize(self) -> None:
        database = sqlite3.connect(self.root / "archive.sqlite3")
        accounting = FixedAccounting(18 * GIB, 30 * GIB)
        archive = ObservationArchive(database, self.root, accounting=accounting, clock=lambda: NOW)
        probe = {
            "target": "terracompute",
            "machine_id": "17049",
            "source": "ssh",
            "status": "healthy",
        }
        routine = archive.archive(probe)
        self.assertFalse(routine.saved)
        protected = archive.archive(probe, capture_class="protected")
        self.assertTrue(protected.saved)
        stored_class = database.execute(
            "SELECT capture_class FROM terracompute_observation_artifacts"
        ).fetchone()[0]
        self.assertEqual(stored_class, "protected")
        oversized = dict(probe)
        oversized["snapshot"] = ["x" * 4096 for _ in range(256)]
        with self.assertRaisesRegex(ObservationRuntimeError, "source_document_oversize"):
            archive.archive(oversized, capture_class="protected")
        database.close()

        state_root = self.root / "pressure"
        store = StateStore(state_root)
        supervisor = CaptureSupervisor()
        runtime = DaemonRuntime(
            runtime_config(state_root),
            store=store,
            supervisor=supervisor,
            execution=IdleExecution(),
            collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting(18 * GIB, 9 * GIB)  # type: ignore[union-attr]
        observed = datetime.now(timezone.utc)
        for index in range(2):
            runtime.on_collection(
                CollectionObservation(
                    "ssh", "ssh", CollectionStatus.SUCCESS, index, index + 1,
                    ssh_probe(observed + timedelta(microseconds=index)),
                )
            )
        self.assertIsNone(runtime.latest_ssh)
        self.assertIsNone(store.db.execute(
            "SELECT collection_progress_at FROM terracompute_runtime_progress"
        ).fetchone()[0])
        storage_events = [
            probe["events"][0]
            for probe in supervisor.probes
            if probe.get("source") == "observation-storage" and probe.get("events")
        ]
        self.assertEqual(len(storage_events), 2)
        self.assertEqual(
            stable_signature(storage_events[0]), stable_signature(storage_events[1])
        )
        self.assertEqual(
            store.db.execute(
                "SELECT COUNT(*) FROM terracompute_observation_artifacts WHERE source='ssh'"
            ).fetchone()[0],
            0,
        )
        runtime.close()
        store.close()

    def test_accounting_skips_private_backup_root_but_fails_closed_elsewhere(self) -> None:
        free = SimpleNamespace(free=30 * GIB)
        root = self.root / "state"
        (root / "incidents").mkdir(parents=True)
        (root / "state.sqlite3").write_bytes(b"s" * 100)
        (root / "incidents" / "bundle.json").write_bytes(b"i" * 20)
        backups = root / "backups"
        (backups / "20260916T164818439733Z").mkdir(parents=True)
        (backups / "20260916T164818439733Z" / "state.sqlite3").write_bytes(b"b" * 5000)
        (root / "incidents" / "backups").mkdir()
        (root / "incidents" / "backups" / "nested.json").write_bytes(b"n" * 7)
        backups.chmod(0o000)

        measured = LocalStorageAccounting(root, disk_usage=lambda _path: free).measure()
        self.assertEqual(measured, StorageMeasurement(127, 30 * GIB))

        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        (root / "other").mkdir()
        (root / "other").chmod(0o000)
        with self.assertRaisesRegex(ObservationRuntimeError, "storage_accounting_unavailable"):
            LocalStorageAccounting(root, disk_usage=lambda _path: free).measure()

    def test_rejected_archive_is_not_a_protected_inventory_baseline(self) -> None:
        store = StateStore(self.root / "rejected-baseline", clock=lambda: NOW)
        runtime = DaemonRuntime(
            runtime_config(self.root / "rejected-baseline"), store=store,
            execution=IdleExecution(), collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting(18 * GIB, 30 * GIB)
        future = dict(ssh_probe(NOW + timedelta(days=1)), source="ssh", status="healthy")
        runtime.archive.archive(future, capture_class="protected")
        valid = dict(ssh_probe(), source="ssh", status="healthy")
        self.assertTrue(runtime._protected_capture(valid))
        runtime.on_collection(CollectionObservation(
            "ssh", "ssh", CollectionStatus.SUCCESS, 0, 1, valid,
        ))
        self.assertIsNotNone(runtime.latest_ssh)
        self.assertEqual(store.db.execute(
            "SELECT COUNT(*) FROM inventory_probe_captures WHERE state='complete'"
        ).fetchone()[0], 1)
        runtime.close()
        store.close()

    def test_malformed_shapes_preserve_raw_archive_after_valid_baseline(self) -> None:
        observed = datetime.now(timezone.utc)
        store = StateStore(self.root / "malformed")
        runtime = DaemonRuntime(
            runtime_config(self.root / "malformed"), store=store,
            execution=IdleExecution(), collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting()
        valid = ssh_probe(observed)
        runtime.on_collection(CollectionObservation(
            "ssh", "ssh", CollectionStatus.SUCCESS, 0, 1, valid,
        ))
        for index, malformed in enumerate(([], {"gpu": {"gpus": [42]}}), 1):
            value = dict(ssh_probe(observed), snapshot=malformed)
            runtime.on_collection(CollectionObservation(
                "ssh", "ssh", CollectionStatus.SUCCESS, index, index + 1, value,
            ))
            row = store.db.execute(
                "SELECT document_json FROM terracompute_observation_artifacts WHERE source='ssh' ORDER BY artifact_id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(json.loads(row[0])["snapshot"], malformed)
        self.assertEqual(runtime.persistence_failures, 2)
        self.assertEqual(store.db.execute(
            "SELECT COUNT(*) FROM inventory_probe_captures WHERE state='complete'"
        ).fetchone()[0], 1)
        runtime.close()
        store.close()

    def test_delayed_capture_preserves_history_without_new_current_failure(self) -> None:
        store = StateStore(self.root / "delayed", clock=lambda: NOW)
        runtime = DaemonRuntime(
            runtime_config(self.root / "delayed"), store=store,
            execution=IdleExecution(), collector_overrides={"ssh": lambda: None},
        )
        runtime.archive.accounting = FixedAccounting()
        for value in (ssh_probe(), ssh_probe(NOW - timedelta(seconds=30), "0000:21:00.0")):
            runtime.on_collection(CollectionObservation(
                "ssh", "ssh", CollectionStatus.SUCCESS, 0, 1, value,
            ))
        self.assertEqual(runtime.persistence_failures, 0)
        self.assertEqual(runtime.latest_ssh["observed_at"], ssh_probe()["observed_at"])
        self.assertEqual(store.db.execute(
            "SELECT status FROM source_state WHERE source='ssh'"
        ).fetchone()[0], "healthy")
        self.assertEqual(store.db.execute(
            "SELECT COUNT(*) FROM inventory_probe_captures WHERE state='complete'"
        ).fetchone()[0], 1)
        self.assertEqual(store.db.execute(
            "SELECT COUNT(*) FROM terracompute_observation_artifacts WHERE source='ssh'"
        ).fetchone()[0], 2)
        runtime.close()
        store.close()

    def test_source_archive_preserves_arrays_and_long_evidence_with_redaction(self) -> None:
        database = sqlite3.connect(self.root / "full-source.sqlite3")
        archive = ObservationArchive(database, self.root, accounting=FixedAccounting())
        probe = {
            "machine_id": "17049", "source": "prometheus", "observed_at": "2026-09-14T20:00:00Z",
            "snapshot": {"samples": tuple({"value": n} for n in range(300)),
                         "message": "x" * 5000, "api_key": "synthetic-secret"},
        }
        result = archive.archive(probe)
        stored = json.loads(database.execute(
            "SELECT document_json FROM terracompute_observation_artifacts"
        ).fetchone()[0])
        self.assertTrue(result.saved)
        self.assertEqual(len(stored["snapshot"]["samples"]), 300)
        self.assertEqual(len(stored["snapshot"]["message"]), 5000)
        self.assertEqual(stored["snapshot"]["api_key"], "[REDACTED]")
        database.close()

    def test_alerts_are_independent_of_failed_metrics_and_unknown_is_not_hardware(self) -> None:
        specs = daemon_collectors(
            ssh=lambda: None,
            prometheus=lambda: None,
            prometheus_alerts=lambda: None,
            vast=lambda: None,
            bmc=lambda: None,
        )
        alerts_spec = next(spec for spec in specs if spec.name == "prometheus-alerts")
        metrics_spec = next(spec for spec in specs if spec.name == "prometheus")
        self.assertEqual(alerts_spec.cadence_seconds, 30)
        self.assertNotEqual(alerts_spec.concurrency_source, metrics_spec.concurrency_source)
        self.assertEqual((len(specs), MAX_EXTERNAL_COLLECTIONS), (5, 4))

        firing = PrometheusAlert(
            {"alertname": "GpuTemperature", "gpu_uuid": "GPU-one", "instance": "host:9400", "severity": "critical"},
            {"summary": "synthetic"},
            AlertState.FIRING,
            NOW,
            "1",
        )
        unknown = PrometheusAlert(
            {"alertname": "FutureState", "instance": "host:9400"},
            {},
            AlertState.UNKNOWN,
            NOW,
            None,
        )
        alert_probe = _prometheus_alerts_probe(
            AlertSnapshot(NOW, (firing, unknown)), now=NOW
        )
        self.assertEqual(alert_probe["snapshot"]["firing"], 1)
        self.assertEqual(alert_probe["snapshot"]["unknown"], 1)
        self.assertEqual(alert_probe["events"][0]["device"], "GPU-one")
        self.assertEqual(alert_probe["events"][0]["severity"], "critical")
        self.assertEqual(alert_probe["events"][1]["fault_family"], "source")

        observed = datetime.now(timezone.utc)
        firing_runtime = PrometheusAlert(
            firing.labels, firing.annotations, firing.state, observed, firing.value
        )
        unknown_runtime = PrometheusAlert(
            unknown.labels, unknown.annotations, unknown.state, observed, unknown.value
        )
        store = StateStore(self.root / "alerts")
        runtime = DaemonRuntime(
            runtime_config(self.root / "alerts"),
            store=store,
            execution=IdleExecution(),
            collector_overrides={
                "prometheus": lambda: None,
                "prometheus-alerts": lambda: None,
            },
        )
        runtime.archive.accounting = FixedAccounting()  # type: ignore[union-attr]
        runtime.on_collection(
            CollectionObservation(
                "prometheus", "prometheus", CollectionStatus.SUCCESS, 0, 1,
                PrometheusFailure("request_failed", "vast"),
            )
        )
        runtime.on_collection(
            CollectionObservation(
                "prometheus-alerts", "prometheus-alerts", CollectionStatus.SUCCESS,
                0, 1, AlertSnapshot(observed, (firing_runtime, unknown_runtime)),
            )
        )
        sources = {
            row[0]
            for row in store.db.execute(
                "SELECT source FROM terracompute_observation_artifacts"
            )
        }
        self.assertEqual(sources, {"prometheus", "prometheus-alerts"})
        self.assertIsNone(runtime.latest_prometheus)
        runtime.close()
        store.close()

    def test_latest_prometheus_batch_is_wired_into_market_reconciliation(self) -> None:
        store = StateStore(self.root / "market-prometheus")
        supervisor = CaptureSupervisor()
        runtime = DaemonRuntime(
            runtime_config(self.root / "market-prometheus"),
            store=store,
            supervisor=supervisor,
            execution=IdleExecution(),
            collector_overrides={
                "prometheus": lambda: None,
                "vast": lambda: None,
            },
        )
        runtime.archive.accounting = FixedAccounting()  # type: ignore[union-attr]
        batch = MetricBatch((), (), (), (), ())
        observed = datetime.now(timezone.utc)
        snapshot = VastSnapshot(
            observed,
            MachineObservation(
                17049, observed, "terracompute", True, True, False, 1, 0
            ),
            (),
            MarketObservation(
                17049,
                observed,
                True,
                (),
                False,
                False,
                None,
                0,
                0,
                None,
                None,
                None,
            ),
            (),
        )
        with mock.patch.object(runtime, "_reconcile_market") as reconcile_latest:
            runtime.on_collection(
                CollectionObservation(
                    "vast", "vast", CollectionStatus.SUCCESS, 0, 1, snapshot
                )
            )
        reconcile_latest.assert_called_once_with()

        runtime.latest_prometheus = batch
        supervisor.probes.clear()
        with mock.patch(
            "terracompute_ops.cli.assess_market", return_value=MarketAssessment([], True)
        ) as reconcile:
            runtime._reconcile_market()
        self.assertIs(reconcile.call_args.kwargs["metrics"], batch)
        self.assertEqual(
            reconcile.call_args.kwargs["probe_max_age_seconds"], TARGET_PROBE_MAX_AGE_SECONDS
        )
        market_sources = lambda: [
            probe["source"]
            for probe in supervisor.probes
            if probe["source"] == "market-reconciliation"
        ]
        self.assertEqual(market_sources(), ["market-reconciliation"])
        supervisor.probes.clear()
        with mock.patch(
            "terracompute_ops.cli.assess_market", return_value=MarketAssessment([], False)
        ):
            runtime._reconcile_market()
        self.assertEqual(market_sources(), [])

        with mock.patch.object(runtime, "_reconcile_market") as reconcile_on_metrics:
            runtime.on_collection(
                CollectionObservation(
                    "prometheus",
                    "prometheus",
                    CollectionStatus.SUCCESS,
                    1,
                    2,
                    batch,
                )
            )
        reconcile_on_metrics.assert_called_once_with()
        runtime.close()
        store.close()

    def market_lifecycle(
        self,
        name: str,
        *,
        fault_until: int,
        prometheus_outage: range = range(0),
        with_ssh: bool = True,
    ) -> StateStore:
        """Drive production cadences for one hour against a real store and supervisor."""
        start = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        clock = [start]

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        store = StateStore(self.root / name, clock=lambda: clock[0])
        with mock.patch("terracompute_ops.cli.datetime", FrozenDatetime):
            runtime = DaemonRuntime(
                runtime_config(self.root / name),
                store=store,
                execution=IdleExecution(),
                collector_overrides={
                    "ssh": lambda: None,
                    "prometheus": lambda: None,
                    "vast": lambda: None,
                },
            )
            runtime.archive.accounting = FixedAccounting()  # type: ignore[union-attr]
            sequence = 0
            for second in range(0, 3600, 30):
                clock[0] = start + timedelta(seconds=second)
                rented = 7 if second < fault_until else 8
                stamp = clock[0].timestamp()
                results = []
                if with_ssh and second % 300 == 0:
                    probe = capacity_target_probe()
                    probe.update(
                        target="terracompute",
                        machine_id="17049",
                        observed_at=clock[0].isoformat().replace("+00:00", "Z"),
                    )
                    results.append(("ssh", probe))
                if second in prometheus_outage:
                    results.append(("prometheus", PrometheusFailure("request_failed", "vast")))
                else:
                    batch = capacity_metric_batch(total=8, rented=rented)
                    results.append((
                        "prometheus",
                        MetricBatch(*(
                            tuple(Sample(item.labels, stamp, item.value) for item in part)
                            for part in (
                                batch.vast, batch.vast_errors, batch.vast_up,
                                batch.dcgm, batch.dcgm_up,
                            )
                        )),
                    ))
                if second % 60 == 0:
                    results.append((
                        "vast",
                        VastSnapshot(
                            clock[0],
                            MachineObservation(
                                17049, clock[0], "terracompute", True, rented < 8, True, 8, rented
                            ),
                            (),
                            MarketObservation(
                                17049, clock[0], True, (), False, False,
                                None, None, 0, None, None, None,
                            ),
                            (),
                        ),
                    ))
                for name_, value in results:
                    runtime.on_collection(
                        CollectionObservation(
                            name_, name_, CollectionStatus.SUCCESS, sequence, sequence + 1, value
                        )
                    )
                    sequence += 2
            self.assertEqual(runtime.persistence_failures, 0)
            runtime.close()
        return store

    def test_market_fault_recovers_without_stale_probe_flapping(self) -> None:
        store = self.market_lifecycle("market-recovers", fault_until=600)
        rows = store.db.execute(
            "SELECT source, status, stable_signature FROM incidents ORDER BY source"
        ).fetchall()
        fault = stable_signature(
            {"fault_family": "capacity", "code": "physical_free_vast_market_unavailable"}
        )
        stale = stable_signature(
            {"fault_family": "capacity", "code": "target_probe_stale", "component": "target"}
        )
        self.assertNotIn(stale, {row["stable_signature"] for row in rows})
        market = [row for row in rows if row["source"] == "market-reconciliation"]
        self.assertEqual(
            [(row["status"], row["stable_signature"]) for row in market], [("recovered", fault)]
        )
        self.assertTrue(all(row["status"] == "recovered" for row in rows), [tuple(r) for r in rows])
        store.close()

    def test_prometheus_outage_during_market_fault_does_not_fake_recovery(self) -> None:
        # Without a target probe only Prometheus can decide; its outage is not recovery.
        store = self.market_lifecycle(
            "market-outage",
            fault_until=10**9,
            prometheus_outage=range(600, 1500),
            with_ssh=False,
        )
        fault = stable_signature(
            {"fault_family": "capacity", "code": "physical_free_vast_market_unavailable"}
        )
        key = store.db.execute(
            """SELECT dedup_key FROM incidents
               WHERE source='market-reconciliation' AND stable_signature=?""",
            (fault,),
        ).fetchone()[0]
        transitions = [
            row[0]
            for row in store.db.execute(
                """SELECT transition FROM transitions
                   WHERE incident_key=? AND transition!='repeated' ORDER BY id""",
                (key,),
            )
        ]
        self.assertNotIn("recovered", transitions)
        self.assertNotIn("reopened", transitions)
        store.close()

    def test_partial_bmc_retains_typed_evidence_and_sensor_event(self) -> None:
        sensor = SensorReading(
            "temperature", "temp-1", "GPU inlet", 93.0, "C", "Enabled", "Critical",
            reading_celsius=93.0, hardware_identity=HardwareIdentity(model="T1"),
        )
        resource = ResourceObservation(
            "/redfish/v1/Chassis/1/Thermal",
            "Thermal",
            "Thermal",
            "Enabled",
            "OK",
            power_state="On",
            hardware_identity=HardwareIdentity(serial_number="SERIAL"),
            thermal=(sensor,),
            log_entries=(
                LogEntry("1", "Historical", "2020-01-01T00:00:00Z", "Critical", "old", "x", "Event", True),
            ),
        )
        probe = _redfish_probe(
            RedfishSnapshot(NOW, (resource,), False, ("descendant:deadline_exceeded",))
        )
        self.assertEqual(probe["status"], "unhealthy")
        stored = probe["snapshot"]["resources"][0]
        self.assertEqual(stored["power_state"], "On")
        self.assertEqual(stored["hardware_identity"]["serial_number"], "SERIAL")
        self.assertEqual(stored["thermal"][0]["reading_celsius"], 93.0)
        self.assertEqual(stored["log_entries"][0]["message"], "old")
        self.assertTrue(any(event["code"] == "redfish_sensor_unhealthy" for event in probe["events"]))
        self.assertEqual(next(event["severity"] for event in probe["events"]
                              if event["code"] == "redfish_sensor_unhealthy"), "critical")

    def test_absent_unpopulated_bmc_sensor_is_inventory_not_incident(self) -> None:
        sensor = SensorReading(
            "fan", "0", "FAN5_1", None, "RPM", "Absent", None
        )
        resource = ResourceObservation(
            "/redfish/v1/Chassis/Self/Thermal",
            "Thermal",
            "Thermal",
            "Enabled",
            "OK",
            thermal=(sensor,),
        )
        probe = _redfish_probe(RedfishSnapshot(NOW, (resource,), True, ()))
        self.assertEqual(probe["status"], "healthy")
        self.assertEqual(probe["events"], [])
        self.assertEqual(probe["snapshot"]["resources"][0]["thermal"][0]["state"], "Absent")

    def test_heartbeat_restart_sequence_and_failed_notify_does_not_progress(self) -> None:
        state_root = self.root / "heartbeat"
        store = StateStore(state_root, clock=lambda: NOW)
        progress = RuntimeProgress(store.db, clock=lambda: NOW)
        progress.record_collection()
        publisher = HeartbeatPublisher(
            state_root,
            progress,
            boot_id="controller-one",
            clock=lambda: NOW,
            monotonic=lambda: 0,
        )
        self.assertIsNone(publisher.publish_if_due(force=True))
        progress.record_notification()
        first = publisher.publish_if_due(force=True)
        self.assertEqual(first["sequence"], 1)
        self.assertTrue((state_root / HEARTBEAT_FILENAME).is_file())
        self.assertEqual(
            (state_root / HEARTBEAT_FILENAME).stat().st_mode & 0o777,
            0o640,
        )
        self.assertTrue(
            evaluate_watchdog(self.root / "watchdog.json", first, now=NOW).accepted
        )
        store.close()

        reopened = StateStore(state_root, clock=lambda: NOW + timedelta(seconds=30))
        restarted = RuntimeProgress(reopened.db, clock=lambda: NOW + timedelta(seconds=30))
        second = HeartbeatPublisher(
            state_root,
            restarted,
            boot_id="controller-two",
            clock=lambda: NOW + timedelta(seconds=30),
            monotonic=lambda: 30,
        ).publish_if_due(force=True)
        self.assertEqual(second["sequence"], 2)
        reopened.close()

        notify_root = self.root / "notify"
        config = RuntimeConfig(
            notify_root,
            None,
            None,
            None,
            None,
            TelegramConfig(True, Path("/token"), Path("/chat"), None, None, False, 0),
            WebhookConfig(False, None, None, "127.0.0.1", 0, 4, 5),
            0.02,
        )
        checks = iter((False, True))
        with (
            mock.patch("terracompute_ops.cli.read_credential", return_value="synthetic"),
            mock.patch("terracompute_ops.cli.TelegramClient") as client_factory,
            mock.patch(
                "terracompute_ops.cli.drain_outbox_semantic",
                return_value=NotificationDrainResult(0, 1, None),
            ) as drain,
            mock.patch(
                "terracompute_ops.cli._stop_flag",
                return_value=(lambda: next(checks), lambda: None),
            ),
            mock.patch("terracompute_ops.cli.time.sleep"),
        ):
            self.assertEqual(run_notify(config), 0)
            client_factory.assert_called_once_with("synthetic", timeout=5)
            self.assertEqual(drain.call_args.kwargs["limit"], 100)
        notify_store = StateStore(notify_root)
        row = notify_store.db.execute(
            "SELECT notification_progress_at FROM terracompute_runtime_progress"
        ).fetchone()
        self.assertIsNone(row[0])
        notify_store.close()

        checks = iter((False, True))
        with (
            mock.patch("terracompute_ops.cli.read_credential", return_value="synthetic"),
            mock.patch("terracompute_ops.cli.TelegramClient"),
            mock.patch(
                "terracompute_ops.cli.drain_outbox_semantic",
                return_value=NotificationDrainResult(0, 0, None),
            ),
            mock.patch(
                "terracompute_ops.cli._stop_flag",
                return_value=(lambda: next(checks), lambda: None),
            ),
            mock.patch("terracompute_ops.cli.time.sleep"),
        ):
            self.assertEqual(run_notify(config), 0)
        notify_store = StateStore(notify_root)
        row = notify_store.db.execute(
            "SELECT notification_progress_at FROM terracompute_runtime_progress"
        ).fetchone()
        self.assertIsNotNone(row[0])
        notify_store.close()


if __name__ == "__main__":
    unittest.main()
