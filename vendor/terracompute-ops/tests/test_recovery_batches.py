from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.recovery_coverage import producer_coverage
from terracompute_ops.incidents import canonical_json, evidence_digest
from terracompute_ops.retention import prune_observation_history

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
EVENT = dict(fault_family="systemd", code="service_demo_not_active", component="demo")


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def coverage(result="pass", check="systemd:service_demo_not_active", resource="demo"):
    return dict(check=check, resource=resource, result=result, evidence_ref="/snapshot/services/demo")


class RecoveryBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = START
        self.store = StateStore(self.root, clock=lambda: self.now)
        self.supervisor = Supervisor(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def observe(self, seconds, events=(), **kwargs):
        self.now = START + timedelta(seconds=seconds)
        probe = dict(target="terracompute", machine_id="17049", source="ssh", boot_id="A",
                     boot_verified=True, observed_at=stamp(seconds), measured_at=stamp(seconds),
                     healthy=not events, events=list(events), complete=True,
                     snapshot={"services": {"demo": "active"}}, coverage=[coverage()])
        probe.update(kwargs)
        if probe["source"] not in {"ssh", "target-probe"} or probe.get("boot_verified") is not True:
            probe.pop("measured_at", None)
        if probe["source"] not in {"ssh", "target-probe"}:
            probe.pop("boot_verified", None)
        return self.supervisor.observe(probe)

    def incident(self):
        return dict(self.store.db.execute("SELECT * FROM incidents WHERE fault_family='systemd'").fetchone())

    def test_complete_partial_and_ineligible_interrupt_healthy_window(self):
        self.observe(0, [EVENT])
        self.observe(60)
        self.observe(120, complete=False, coverage=[])
        self.assertEqual(self.incident()["status"], "open")
        self.observe(180)
        self.observe(240, recovery_eligible=False)
        self.assertEqual(self.incident()["status"], "open")
        self.observe(300)
        self.observe(600)
        self.assertEqual(self.incident()["status"], "recovered")

    def test_unrelated_unknown_does_not_block_observed_incident(self):
        self.observe(0, [EVENT])
        for seconds in (60, 360):
            self.observe(seconds, complete=False, coverage=[coverage(), coverage("unknown", "gpu:test", "GPU-other")])
        self.assertEqual(self.incident()["status"], "recovered")
        event = self.store.recovery_events()[0]
        self.assertEqual(json.loads(event["payload_json"])["coverage"], [coverage()])

    def test_retired_a_cannot_roll_back_b_or_open_first_historical_fault(self):
        self.observe(0)
        self.observe(60, boot_id="B")
        self.observe(120, [EVENT], boot_id="A")
        self.assertEqual(self.store.current_epoch("terracompute")["boot_id"], "B")
        self.assertEqual(self.incident()["status"], "historical")
        self.assertEqual(self.store.db.execute("SELECT epoch FROM observation_batches ORDER BY id DESC").fetchone()[0], 1)
        self.assertEqual(self.store.due_notifications(), [])
        self.assertEqual(self.store.db.execute("SELECT ordering FROM observation_batches ORDER BY id DESC").fetchone()[0], "old_epoch")

    def test_unknown_and_external_sources_cannot_create_epochs(self):
        self.observe(0, boot_id="unknown")
        self.observe(60, source="vast", boot_id="invented")
        self.assertIsNone(self.store.current_epoch("terracompute"))
        self.observe(120, boot_id="A")
        self.assertEqual(self.store.current_epoch("terracompute")["epoch"], 1)

    def test_new_epoch_accepts_regressed_target_clock_but_not_late_measurement(self):
        self.observe(0, [EVENT])
        self.observe(60, boot_id="B", observed_at=stamp(-3600))
        self.assertEqual(self.store.current_epoch("terracompute")["epoch"], 2)
        self.assertEqual(self.incident()["status"], "recovery_pending")
        self.observe(120, boot_id="C", observed_at=stamp(-3700), measured_at=stamp(30))
        self.assertEqual(self.store.current_epoch("terracompute")["epoch"], 2)
        self.assertEqual(self.incident()["last_boot_id"], "B")

    def test_malformed_later_event_leaves_no_rows_or_bundles(self):
        with self.assertRaises(ValueError):
            self.observe(0, [EVENT, {"fault_family": "other", "code": "bad", "silent": "yes"}])
        for table in ("host_epochs", "observations", "observation_batches", "incidents", "outbox"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(list(self.store.incident_root.iterdir()), [])

    def test_invalid_coverage_reference_rejected_before_publication(self):
        row = coverage()
        row["evidence_ref"] = "/missing"
        with self.assertRaises(ValueError):
            self.observe(0, [EVENT], coverage=[row])
        self.assertIsNone(self.store.current_epoch("terracompute"))

    def test_settlement_failure_rolls_back_events_and_epoch_together(self):
        self.observe(0, [EVENT])
        self.observe(60)
        before = self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        with patch.object(self.store, "_recovery_event", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.observe(360)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], before)
        self.assertEqual(self.incident()["status"], "recovery_pending")
        self.assertEqual(self.store.recovery_events(), [])

    def test_retirement_between_validation_and_commit_uses_locked_epoch(self):
        self.observe(0, [EVENT])
        self.observe(60)
        peer = StateStore(self.root, clock=lambda: START + timedelta(seconds=120))
        try:
            Supervisor(peer).observe(dict(target="terracompute", machine_id="17049", source="ssh",
                                         boot_id="B", boot_verified=True, observed_at=stamp(120), healthy=True))
        finally:
            peer.close()
        self.observe(360, boot_id="A")
        self.assertEqual(self.incident()["status"], "open")
        self.assertEqual(self.store.recovery_events(), [])

    def test_silent_candidates_expire_without_another_source_sample(self):
        self.observe(0, [EVENT])
        self.observe(60)
        self.now = START + timedelta(seconds=391)
        self.assertEqual(self.store.expire_verifications(), 1)
        self.assertEqual(self.store.expire_verifications(), 0)
        self.assertEqual(self.incident()["status"], "open")

    def test_event_once_only_restart_replay_and_retention(self):
        self.observe(0, [EVENT])
        self.observe(60)
        self.observe(360, source_event_id="final")
        event = self.store.recovery_events()[0]
        self.observe(361, source_event_id="final")
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: self.now)
        self.supervisor = Supervisor(self.store)
        self.assertEqual(self.store.recovery_events(), [event])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE recovery_events SET episode=99")
        self.store.db.rollback()
        prune_observation_history(self.store.db, now=START + timedelta(days=60))
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["verification_interval"], [stamp(60), stamp(360)])
        for batch in payload["post_batches"]:
            self.assertIsNotNone(self.store.db.execute("SELECT evidence_json FROM observation_batches WHERE id=?", (batch,)).fetchone())
        condition = self.store.accepted_condition(self.incident()["dedup_key"])
        self.assertEqual(condition["event_code"], EVENT["code"])
        self.assertEqual(condition["condition"], "pass")
        self.assertEqual(condition["epoch"], 1)

    def test_replayed_dependencies_never_advance_verification(self):
        host = self.observe(0).batch_id
        metrics = self.observe(1, source="prometheus", boot_id="unknown").batch_id
        event = dict(fault_family="capacity", code="test", component="host")
        self.observe(2, [event], source="capacity-reconciliation", dependencies=[host, metrics], coverage=[])
        for seconds in (60, 360):
            self.observe(seconds, source="capacity-reconciliation", dependencies=[host, metrics],
                         coverage=[coverage("pass", "capacity:test", "host")])
        row = self.store.db.execute("SELECT status FROM incidents WHERE source='capacity-reconciliation'").fetchone()
        self.assertEqual(row[0], "open")
        batch = self.store.db.execute("SELECT * FROM observation_batches ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(batch["ordering"], "replayed_dependencies")
        self.assertNotEqual(batch["freshness"], "fresh")
        self.assertEqual(batch["measured_utc"], stamp(0))
        self.assertEqual(self.store.recovery_events(), [])

    def test_pre_epoch_dependencies_cannot_open_active_fault(self):
        host = self.observe(0).batch_id
        metrics = self.observe(1, source="prometheus").batch_id
        self.observe(60, boot_id="B")
        self.observe(61, [EVENT], source="capacity-reconciliation", dependencies=[host, metrics])
        self.assertEqual(self.incident()["status"], "historical")
        self.assertEqual(self.store.due_notifications(), [])

    def test_unknown_preboot_dependencies_cannot_open_active_fault(self):
        host = self.observe(0, boot_id="unknown", boot_verified=False).batch_id
        metrics = self.observe(1, source="prometheus", boot_id="unknown").batch_id
        fault = {"fault_family": "capacity", "code": "test", "component": "host"}
        self.observe(2, [fault], source="capacity-reconciliation", boot_id="unknown",
                     dependencies=[host, metrics], coverage=[])
        self.assertEqual(self.store.db.execute("SELECT status FROM incidents WHERE source='capacity-reconciliation'").fetchone()[0],
                         "historical")
        self.assertIsNone(self.store.current_epoch("terracompute"))

    def test_provider_outage_interrupts_derived_verification(self):
        fault = dict(fault_family="capacity", code="synthetic_predicate")
        proof = coverage(check="capacity:synthetic_predicate", resource="host")
        host = self.observe(0).batch_id
        metrics = self.observe(1, source="prometheus").batch_id
        self.observe(2, [fault], source="capacity-reconciliation", dependencies=[host, metrics], coverage=[])
        host = self.observe(300).batch_id
        metrics = self.observe(301, source="prometheus").batch_id
        self.observe(302, source="capacity-reconciliation", dependencies=[host, metrics], coverage=[proof])
        key = self.store.db.execute("SELECT dedup_key FROM incidents WHERE source='capacity-reconciliation'").fetchone()[0]
        self.assertEqual(self.store.accepted_condition(key)["status"], "recovery_pending")
        self.observe(480, source="prometheus", status="unknown", healthy=False, freshness="unknown", coverage=[])
        self.assertEqual(self.store.accepted_condition(key)["status"], "open")
        self.assertEqual(self.store.accepted_condition(key)["condition"], "unknown")
        host = self.observe(600).batch_id
        metrics = self.observe(601, source="prometheus").batch_id
        self.observe(602, source="capacity-reconciliation", dependencies=[host, metrics], coverage=[proof])
        self.assertEqual(self.store.accepted_condition(key)["status"], "recovery_pending")
        self.assertEqual(self.store.recovery_events(), [])
        host = self.observe(900).batch_id
        metrics = self.observe(901, source="prometheus").batch_id
        self.observe(902, source="capacity-reconciliation", dependencies=[host, metrics], coverage=[proof])
        self.assertEqual(self.store.accepted_condition(key)["status"], "recovered")

    def test_derived_sample_cannot_extend_metric_lifetime(self):
        fault = dict(fault_family="capacity", code="synthetic_predicate")
        proof = coverage(check="capacity:synthetic_predicate", resource="host")
        self.observe(0)
        metrics = self.observe(1, source="prometheus").batch_id
        host = self.observe(300).batch_id
        self.observe(301, [fault], source="capacity-reconciliation", dependencies=[host, metrics], coverage=[proof])
        row = self.store.db.execute("SELECT status FROM incidents WHERE source='capacity-reconciliation'").fetchone()
        self.assertEqual(row[0], "historical")
        self.assertEqual(self.store.recovery_events(), [])

    def test_out_of_order_first_fault_is_history_and_later_current_promotes(self):
        self.observe(100)
        self.observe(101, [EVENT], measured_at=stamp(50))
        self.assertEqual(self.incident()["status"], "historical")
        self.assertEqual(self.store.due_notifications(), [])
        self.observe(102, [EVENT])
        self.assertEqual(self.incident()["status"], "open")
        self.assertEqual(len(self.store.due_notifications()), 1)

    def test_device_absence_or_replacement_not_passing(self):
        prior = [dict(check_name="xid:79", resource="GPU-old")]
        probe = dict(source="ssh", complete=True, events=[], snapshot={"gpu": {"gpus": [{"uuid": "GPU-new"}]}})
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
        probe["snapshot"]["gpu"]["gpus"] = []
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")

    def test_host_count_requires_matching_original_expectation_and_full_pci_inventory(self):
        event = {"fault_family": "gpu", "code": "pci_gpu_count_mismatch",
                 "evidence": {"expected": 2, "observed": 1}}
        prior = [dict(check_name="gpu:pci_gpu_count_mismatch", resource="host",
                      original_document=event)]
        probe = dict(source="ssh", complete=False, events=[], snapshot={"gpu": {
            "expected_count": 2, "pci_count": 2, "gpus": [],
            "pci_devices": [{"pci_bdf": "a"}, {"pci_bdf": "b"}]}})
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "pass")
        probe["snapshot"]["gpu"]["expected_count"] = 3
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
        probe["snapshot"]["gpu"]["expected_count"] = 2
        probe["events"] = [{"fault_family": "probe", "code": "pci_gpu_inventory_failed"}]
        self.assertEqual(producer_coverage(probe, prior)[-1]["result"], "unknown")

    def test_bdf_recovery_requires_original_uuid_and_same_current_device(self):
        bdf = "0000:01:00.0"
        event = {"fault_family": "gpu", "code": "gpu_driver_unavailable",
                 "evidence": {"pci_bdf": bdf}}
        prior = [dict(check_name="gpu:gpu_driver_unavailable", resource=bdf,
                      original_document=event)]
        probe = dict(source="ssh", complete=False, events=[], snapshot={"gpu": {
            "pci_count": 1, "pci_devices": [{"pci_bdf": bdf, "driver": "nvidia"}],
            "gpus": [{"pci_bdf": bdf, "uuid": "GPU-new"}]}})
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
        prior[0]["original_document"]["snapshot"] = {"gpu": {"gpus": [{"pci_bdf": bdf, "uuid": "GPU-old"}]}}
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
        probe["snapshot"]["gpu"]["gpus"][0]["uuid"] = "GPU-old"
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "pass")
        probe["snapshot"]["gpu"]["pci_devices"][0]["driver"] = "vfio-pci"
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")

    def test_unknown_preboot_gpu_fault_is_history(self):
        self.observe(0, [{"fault_family": "gpu", "code": "pci_gpu_count_mismatch",
                          "evidence": {"expected": 8, "observed": 7}}], boot_id="unknown",
                     boot_verified=False, measured_at=stamp(0), freshness="unknown")
        self.assertEqual(self.store.db.execute("SELECT status FROM incidents").fetchone()[0], "historical")
        self.assertIsNone(self.store.current_epoch("terracompute"))

    def test_service_observation_is_independent_of_gpu_collector_failure(self):
        prior = [dict(check_name="systemd:service_demo_not_active", resource="demo")]
        probe = dict(source="ssh", complete=False, events=[dict(fault_family="probe", code="gpu_inventory_failed")],
                     snapshot={"services": {"demo": "active"}})
        self.assertEqual(producer_coverage(probe, prior)[-1]["result"], "pass")

    def test_bmc_disappearance_and_replacement_are_unknown(self):
        old_sensor = dict(kind="fan", member_id="1", health="Critical", state="Enabled",
                          hardware_identity={"serial_number": "original"})
        old = dict(path="/chassis", sensors=[old_sensor])
        prior = [dict(check_name="bmc:redfish_sensor_unhealthy", resource="/chassis:fan:1",
                      original_document={"snapshot": {"resources": [old]}})]
        new_sensor = dict(old_sensor, health="OK")
        probe = dict(source="bmc", complete=True, events=[], snapshot={"resources": [dict(old, sensors=[new_sensor])]})
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "pass")
        new_sensor["hardware_identity"] = {"serial_number": "replacement"}
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
        probe["snapshot"]["resources"] = []
        self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")

    def test_alert_and_vast_partial_discovery_never_proves_recovery(self):
        for source, check in (("prometheus-alerts", "prometheus-alert:prometheus_alert_firing"),
                              ("vast", "capacity:vast_machine_unlisted")):
            prior = [dict(check_name=check, resource="host")]
            probe = dict(source=source, complete=False, events=[], snapshot={"complete": False})
            self.assertEqual(producer_coverage(probe, prior)[0]["result"], "unknown")
            probe["complete"] = True
            self.assertEqual(producer_coverage(probe, prior)[0]["result"], "pass")

    def test_inventory_accepts_new_epoch_despite_regressed_target_clock(self):
        from terracompute_ops.inventory import Inventory, capture_probe, InventoryError
        from tests.test_inventory import probe_snapshot
        inventory = Inventory(self.store.db)
        probe = dict(probe_snapshot(observed_at=stamp(0)), target="terracompute", boot_id="A")
        first = self.observe(0, snapshot=probe["snapshot"], coverage=[])
        capture_probe(inventory, probe, accepted_batch_id=first.batch_id)
        probe = dict(probe_snapshot(observed_at=stamp(-3600)), target="terracompute", boot_id="B")
        second = self.observe(60, boot_id="B", observed_at=stamp(-3600), snapshot=probe["snapshot"], coverage=[])
        capture_probe(inventory, probe, accepted_batch_id=second.batch_id)
        rows = self.store.db.execute("SELECT host_epoch,source_observed_at,observed_at FROM inventory_probe_captures ORDER BY accepted_batch_id").fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertLess(rows[1][1], rows[0][1])
        self.assertGreater(rows[1][2], rows[0][2])
        with self.assertRaises(InventoryError):
            capture_probe(inventory, dict(probe, boot_id="A"), accepted_batch_id=first.batch_id)

    def test_migration_snapshots_and_resets_only_ambiguous_verification(self):
        # Make an actual v3 database using the existing migration functions.
        self.store.close()
        self.store = None
        with patch("terracompute_ops.state.CURRENT_SCHEMA_VERSION", 3):
            legacy_root = self.root / "legacy"
            old = StateStore(legacy_root, clock=lambda: self.now)
            key = "a" * 64
            old.create_incident(key, dict(target="terracompute", machine_id="17049"), {}, "legacy", None)
            old.db.execute("UPDATE incidents SET status='recovery_pending',recovery_started_utc=?,recovery_last_healthy_utc=?,acknowledged_utc=?", (stamp(0), stamp(0), stamp(0)))
            old.db.execute("UPDATE outbox SET attempts=3")
            old.db.commit()
            old.close()
        self.store = StateStore(legacy_root, clock=lambda: self.now)
        incident = self.store.db.execute("SELECT * FROM incidents").fetchone()
        self.assertEqual(incident["status"], "open")
        self.assertIsNone(incident["recovery_started_utc"])
        self.assertEqual(incident["acknowledged_utc"], stamp(0))
        self.assertEqual(self.store.db.execute("SELECT attempts FROM outbox").fetchone()[0], 3)
        self.assertIsNone(self.store.current_epoch("terracompute"))
        snapshots = list(legacy_root.glob("state.sqlite3.pre-migration-v3-*"))
        self.assertEqual(len(snapshots), 1)
        snapshot = sqlite3.connect(snapshots[0])
        self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(snapshot.execute("SELECT status FROM incidents").fetchone()[0], "recovery_pending")
        snapshot.close()
        self.assertTrue((self.store.incident_root / incident["bundle_name"]).exists())

    def test_migrated_structured_service_fault_recovers_without_losing_history(self):
        self.store.close()
        self.store = None
        legacy_root = self.root / "mapped-legacy"
        event = {"fault_family": "systemd", "code": "service_demo_not_active",
                 "evidence": {"service": "demo", "state": "failed"}}
        with patch("terracompute_ops.state.CURRENT_SCHEMA_VERSION", 3):
            old = StateStore(legacy_root, clock=lambda: self.now)
            key = "b" * 64
            old.create_incident(key, dict(target="terracompute", machine_id="17049", source="ssh"),
                                event, "legacy", None)
            old.db.execute("UPDATE incidents SET target='terracompute',source='ssh',fault_family='systemd' WHERE dedup_key=?", (key,))
            digest, _ = evidence_digest(event)
            old.db.execute("""INSERT INTO observations(target,machine_id,source,source_utc,receipt_utc,
                boot_id,status,freshness,ordering,evidence_sha256,evidence_json,incident_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", ("terracompute", "17049", "ssh", stamp(0), stamp(0),
                "A", "unhealthy", "fresh", "current", digest, canonical_json(event), key))
            old.db.execute("UPDATE outbox SET attempts=4, state='sent'")
            old.db.commit()
            old.close()
        self.store = StateStore(legacy_root, clock=lambda: self.now)
        self.supervisor = Supervisor(self.store)
        mapped = self.store.db.execute("SELECT * FROM incident_conditions WHERE incident_key=?", (key,)).fetchone()
        self.assertEqual((mapped["check_name"], mapped["resource"], mapped["condition"]),
                         ("systemd:service_demo_not_active", "demo", "unknown"))
        self.assertEqual(self.store.db.execute("SELECT attempts FROM outbox").fetchone()[0], 4)
        for seconds in (60, 360):
            probe = dict(target="terracompute", machine_id="17049", source="ssh", boot_id="A",
                         boot_verified=True, observed_at=stamp(seconds), measured_at=stamp(seconds),
                         healthy=True, events=[], snapshot={"services": {"demo": "active"}})
            probe["coverage"] = producer_coverage(probe, [dict(mapped)])
            self.now = START + timedelta(seconds=seconds)
            self.supervisor.observe(probe)
        self.assertEqual(self.store.db.execute("SELECT status FROM incidents WHERE dedup_key=?", (key,)).fetchone()[0],
                         "recovered")
        self.assertEqual(len(self.store.recovery_events()), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM observations WHERE incident_key=?", (key,)).fetchone()[0], 1)

    def test_unknown_source_sample_interrupts_after_verified_boot(self):
        self.observe(0, [EVENT])
        self.observe(60)
        self.now = START + timedelta(seconds=120)
        self.supervisor.observe(dict(target="terracompute", machine_id="17049", source="ssh", boot_id="unknown",
                                     observed_at=stamp(120), status="unknown", healthy=False, freshness="unknown"))
        self.assertEqual(self.incident()["status"], "open")
        self.assertEqual(self.store.current_epoch("terracompute")["boot_id"], "A")

    def test_external_recovery_reports_verified_host_boot_not_source_placeholder(self):
        self.observe(0)
        self.observe(1, [EVENT], source="vast", boot_id="external-placeholder")
        for seconds in range(60, 361, 60):
            self.observe(seconds, source="vast", boot_id="external-placeholder")
        event = self.store.recovery_events()[0]
        self.assertEqual(event["boot_id"], "A")
        self.assertEqual(event["epoch"], 1)

    def test_one_shot_cli_retains_real_dependencies_without_live_io(self):
        from terracompute_ops.cli import main
        from terracompute_ops.prometheus import MetricBatch, Sample
        fixture = self.root / "synthetic-input"
        fixture.write_text("fixture")
        probe = dict(target="terracompute", machine_id="17049", boot_id="A", observed_at=stamp(0),
                     healthy=True, events=[], snapshot={"services": {"demo": "active"}})
        batch = MetricBatch((Sample({}, START.timestamp(), 1),), (), (), (), ())
        args = ["run", "--state-dir", str(self.root), "--ssh-target", "fixture",
                "--ssh-identity", str(fixture), "--known-hosts", str(fixture),
                "--prometheus-endpoint", "http://127.0.0.1:9090",
                "--telegram-token", str(fixture), "--telegram-chat-id", str(fixture)]
        with (patch("terracompute_ops.cli.StateStore", return_value=self.store),
              patch.object(self.store, "close"),
              patch("terracompute_ops.cli.fixed_ssh_probe", return_value=probe),
              patch("terracompute_ops.cli.read_credential", return_value="fixture"),
              patch("terracompute_ops.cli.drain_outbox"),
              patch("terracompute_ops.cli.PrometheusClient") as client,
              patch("terracompute_ops.cli.reconcile_capacity", return_value=[])):
            client.return_value.fetch.return_value = batch
            self.assertEqual(main(args), 0)
        derived = self.store.latest_accepted_evidence("terracompute", "capacity-reconciliation")
        refs = json.loads(derived["dependencies_json"])
        self.assertEqual(len(refs), 2)
        self.assertEqual({self.store.db.execute("SELECT source FROM observation_batches WHERE id=?", (item,)).fetchone()[0]
                          for item in refs}, {"target-probe", "prometheus"})
        self.assertEqual(derived["measured_utc"], stamp(0))
        self.assertEqual(self.store.current_epoch("terracompute")["epoch"], 1)


if __name__ == "__main__":
    unittest.main()
