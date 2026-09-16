from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.cli import SSH_IO_TIMEOUT_SECONDS, fixed_ssh_probe
from terracompute_ops.incidents import bounded_evidence, classify, stable_signature
from terracompute_ops.scheduler import FULL_SSH_TIMEOUT_SECONDS
from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.telegram import drain_outbox


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def probe(
    event: dict | None = None,
    *,
    healthy: bool = False,
    source_event_id: str | None = None,
    observed_at: str = "2026-09-14T12:00:00Z",
) -> dict:
    value = {
        "target": "vast-machine-17049",
        "machine_id": 17049,
        "boot_id": "boot-a",
        "observed_at": observed_at,
        "healthy": healthy,
        "events": [] if event is None else [event],
    }
    if source_event_id is not None:
        value["source_event_id"] = source_event_id
    return value


class IncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_healthy_probe_records_history_without_incident_or_alert(self) -> None:
        result = self.supervisor.observe(probe(healthy=True))
        self.assertTrue(result.healthy)
        self.assertEqual(result.created, ())
        self.assertEqual(list(Path(self.temp.name, "incidents").iterdir()), [])
        self.assertEqual(self.store.due_notifications(), [])
        history = self.store.incident_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "healthy")

    def test_known_xid_is_classified_without_model_request(self) -> None:
        result = self.supervisor.observe(
            probe({"fault_family": "xid", "code": 79, "device": "gpu0"})
        )
        bundle = result.created[0]
        incident = json.loads((bundle / "incident.json").read_text())
        self.assertEqual(incident["classification"]["label"], "gpu-fallen-off-bus")
        self.assertFalse((bundle / "model-analysis-request.json").exists())

    def test_known_capacity_discrepancy_is_classified_without_model_request(self) -> None:
        result = self.supervisor.observe(
            probe(
                {
                    "fault_family": "capacity",
                    "code": "vast_rented_exceeds_healthy",
                    "message": "bounded reconciliation mismatch",
                }
            )
        )
        bundle = result.created[0]
        incident = json.loads((bundle / "incident.json").read_text())
        self.assertEqual(
            incident["classification"]["label"], "vast-rented-exceeds-healthy"
        )
        self.assertFalse((bundle / "model-analysis-request.json").exists())

    def test_aer_classification_is_deterministic(self) -> None:
        self.assertEqual(
            classify({"fault_family": "AER", "severity": "non-fatal"})["label"],
            "pcie-aer-nonfatal",
        )

    def test_cdi_classification_is_deterministic(self) -> None:
        self.assertEqual(
            classify({"fault_family": "cdi", "code": "device_unavailable"})["label"],
            "cdi-device-unavailable",
        )

    def test_duplicate_delivery_is_suppressed_but_boot_change_is_history(self) -> None:
        event = {"fault_family": "xid", "code": 48, "device": "gpu0"}
        first = self.supervisor.observe(probe(event, source_event_id="event-1"))
        duplicate = self.supervisor.observe(probe(event, source_event_id="event-1"))
        changed_boot = probe(event, source_event_id="event-2")
        changed_boot["boot_id"] = "boot-b"
        third = self.supervisor.observe(changed_boot)
        self.assertEqual(len(first.created), 1)
        self.assertEqual(duplicate.duplicates, 1)
        self.assertEqual(len(third.created), 0)
        self.assertEqual(len(self.store.due_notifications()), 1)
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["occurrence_count"], 2)
        transitions = [row["transition"] for row in self.store.db.execute(
            "SELECT transition FROM transitions ORDER BY id"
        )]
        self.assertEqual(transitions, ["opened", "boot_changed", "repeated"])

    def test_evidence_does_not_change_stable_signature(self) -> None:
        base = {"fault_family": "xid", "code": 31, "device": "gpu0"}
        self.assertEqual(
            stable_signature({**base, "evidence": {"counter": 1}}),
            stable_signature({**base, "evidence": {"counter": 99}}),
        )

    def test_unknown_event_gets_bounded_analysis_request_not_execution(self) -> None:
        result = self.supervisor.observe(
            probe({"fault_family": "mystery", "message": "odd"})
        )
        request = json.loads(
            (result.created[0] / "model-analysis-request.json").read_text()
        )
        self.assertEqual(request["execution"], "disabled")
        self.assertLessEqual(
            (result.created[0] / "model-analysis-request.json").stat().st_size,
            16 * 1024,
        )

    def test_contradictory_probe_gets_analysis_request(self) -> None:
        result = self.supervisor.observe(
            probe({"fault_family": "xid", "code": 43}, healthy=True)
        )
        self.assertTrue((result.created[0] / "model-analysis-request.json").exists())

    def test_explicit_healthy_with_false_healthy_flag_is_downgraded(self) -> None:
        value = probe(healthy=False)
        value["status"] = "healthy"
        result = self.supervisor.observe(value)
        self.assertFalse(result.healthy)
        incident = json.loads((result.created[0] / "incident.json").read_text())
        self.assertTrue(incident["contradictory"])
        self.assertEqual(incident["status"], "unhealthy")

    def test_evidence_is_redacted_and_capped(self) -> None:
        result = bounded_evidence(
            {"token": "top-secret", "log": "Bearer abcdef " + ("x" * 100_000)}
        )
        encoded = json.dumps(result).encode()
        self.assertNotIn(b"top-secret", encoded)
        self.assertNotIn(b"abcdef", encoded)
        self.assertLess(len(encoded), 64 * 1024)

    def test_bundle_manifest_hashes_every_payload_file(self) -> None:
        result = self.supervisor.observe(
            probe({"fault_family": "xid", "code": 13})
        )
        bundle = result.created[0]
        manifest = (bundle / "manifest.sha256").read_text().splitlines()
        listed = {}
        for line in manifest:
            digest, name = line.split("  ", 1)
            listed[name] = digest
        self.assertEqual(set(listed), {"incident.json", "evidence.json", "recovery.json"})
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), digest)

    def test_published_bundle_survives_later_database_failure(self) -> None:
        self.store.db.execute(
            """CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
               BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
        )
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.supervisor.observe(probe({"fault_family": "xid", "code": 79}))
        bundles = list(Path(self.temp.name, "incidents").iterdir())
        self.assertEqual(len(bundles), 1)
        self.assertTrue((bundles[0] / "manifest.sha256").is_file())

    def test_outbox_success_is_durable_and_not_resent(self) -> None:
        self.supervisor.observe(probe({"fault_family": "xid", "code": 74}))
        deliveries = []
        sent, failed = drain_outbox(
            self.store, "token", "chat", lambda *args: deliveries.append(args)
        )
        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(self.store.due_notifications(), [])

    def test_outbox_failure_backoff_retains_pending_item(self) -> None:
        self.supervisor.observe(probe({"fault_family": "xid", "code": 119}))

        def fail(*_args: str) -> None:
            raise OSError("secret-bearing transport detail")

        sent, failed = drain_outbox(self.store, "token", "chat", fail)
        self.assertEqual((sent, failed), (0, 1))
        row = self.store.db.execute(
            "SELECT state, attempts, last_error, next_attempt_utc FROM outbox"
        ).fetchone()
        self.assertEqual((row["state"], row["attempts"], row["last_error"]),
                         ("pending", 1, "delivery-failed"))
        self.assertEqual(row["next_attempt_utc"], "2026-09-14T12:01:00Z")

    def test_outbox_exposes_severity_and_silent_metadata(self) -> None:
        value = probe({"fault_family": "xid", "code": 74, "silent": True})
        self.supervisor.observe(value)
        item = self.store.due_notifications()[0]
        self.assertEqual(item["severity"], "critical")
        self.assertEqual(item["silent"], 0)

    def test_default_sound_policy_silences_warning_but_pings_critical(self) -> None:
        self.assertTrue(StateStore._notification_silent("info", None))
        self.assertFalse(StateStore._notification_silent("error", None))
        self.supervisor.observe(probe({"fault_family": "mystery", "message": "bounded"}))
        second = probe(
            {"fault_family": "xid", "code": 79},
            observed_at="2026-09-14T12:01:00Z",
        )
        self.store.clock = lambda: datetime(2026, 9, 14, 12, 1, tzinfo=timezone.utc)
        self.supervisor.observe(second)
        rows = list(self.store.db.execute(
            "SELECT severity,silent FROM outbox ORDER BY id"
        ))
        self.assertEqual([tuple(row) for row in rows], [("warning", 1), ("critical", 0)])

    def test_wrong_machine_identity_fails_closed(self) -> None:
        value = probe(healthy=True)
        value["machine_id"] = 999
        with self.assertRaises(ValueError):
            self.supervisor.observe(value)

    def test_non_utc_observation_timestamp_fails_closed(self) -> None:
        value = probe(healthy=True)
        value["observed_at"] = "2026-09-14T12:00:00-07:00"
        with self.assertRaises(ValueError):
            self.supervisor.observe(value)


class SshProbeTests(unittest.TestCase):
    @mock.patch("terracompute_ops.cli._terminate_subprocess_group", return_value=True)
    @mock.patch("terracompute_ops.cli.os.read")
    @mock.patch("terracompute_ops.cli.selectors.DefaultSelector")
    @mock.patch("terracompute_ops.cli.subprocess.Popen")
    def test_ssh_invocation_has_pinned_trust_and_no_remote_command(
        self,
        popen: mock.Mock,
        selector: mock.Mock,
        read: mock.Mock,
        _cleanup: mock.Mock,
    ) -> None:
        process = popen.return_value
        process.stdout.fileno.return_value = 7
        process.poll.return_value = 0
        process.wait.return_value = 0
        selector.return_value.select.return_value = [(object(), 1)]
        read.side_effect = [json.dumps(probe(healthy=True)).encode("utf-8"), b""]
        fixed_ssh_probe(
            "/nix/store/openssh/bin/ssh",
            "terracompute-observer@10.50.0.2",
            Path("/run/credentials/unit/identity"),
            Path("/run/credentials/unit/known-hosts"),
        )
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-1], "terracompute-observer@10.50.0.2")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("GlobalKnownHostsFile=/dev/null", argv)
        self.assertNotIn("sh", argv)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertGreaterEqual(SSH_IO_TIMEOUT_SECONDS, 60)
        self.assertGreater(FULL_SSH_TIMEOUT_SECONDS, SSH_IO_TIMEOUT_SECONDS)

    @mock.patch("terracompute_ops.cli._terminate_subprocess_group", return_value=True)
    @mock.patch("terracompute_ops.cli.os.read")
    @mock.patch("terracompute_ops.cli.selectors.DefaultSelector")
    @mock.patch("terracompute_ops.cli.subprocess.Popen")
    def test_ssh_stdout_is_rejected_while_streaming_before_unbounded_accumulation(
        self,
        popen: mock.Mock,
        selector: mock.Mock,
        read: mock.Mock,
        cleanup: mock.Mock,
    ) -> None:
        from terracompute_ops.cli import MAX_PROBE_BYTES

        process = popen.return_value
        process.stdout.fileno.return_value = 7
        process.poll.side_effect = [None, None]
        selector.return_value.select.return_value = [(object(), 1)]
        read.return_value = b"x" * (MAX_PROBE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            fixed_ssh_probe("ssh", "observer@target", Path("id"), Path("hosts"))
        cleanup.assert_called_once_with(process)

    def test_ssh_failure_cleans_real_descendant_process_group(self) -> None:
        from terracompute_ops.cli import MAX_PROBE_BYTES

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            marker = root / "ssh-child.pid"
            executable = root / "synthetic-ssh"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                "pathlib.Path(os.environ['TC_SYNTHETIC_SSH_MARKER']).write_text(str(child.pid))\n"
                f"sys.stdout.buffer.write(b'x' * {MAX_PROBE_BYTES + 1})\n"
                "sys.stdout.buffer.flush()\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            with mock.patch.dict(
                os.environ, {"TC_SYNTHETIC_SSH_MARKER": str(marker)}, clear=False
            ):
                with self.assertRaisesRegex(ValueError, "512 KiB"):
                    fixed_ssh_probe(
                        str(executable), "observer@target", Path("id"), Path("hosts")
                    )
            self.assertTrue(marker.exists())
            child_pid = int(marker.read_text(encoding="ascii"))
            gone_deadline = time.monotonic() + 1
            while time.monotonic() < gone_deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_ssh_target_rejects_option_injection(self) -> None:
        with self.assertRaises(ValueError):
            fixed_ssh_probe("ssh", "-oProxyCommand=anything", Path("id"), Path("hosts"))


if __name__ == "__main__":
    unittest.main()
