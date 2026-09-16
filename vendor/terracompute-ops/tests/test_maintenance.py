from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.maintenance import (
    MISSING_AFTER_SECONDS,
    RECOVERY_STABLE_SECONDS,
    WATCHDOG_CADENCE_SECONDS,
    create_backup,
    create_local_snapshot,
    evaluate_watchdog,
    main,
    verify_local_snapshot,
)
from terracompute_ops.state import StateStore


NOW = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)


def utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def heartbeat(
    sequence: int,
    when: datetime,
    *,
    machine_id: str = "17049",
    boot_id: str = "boot-a",
    progress: datetime | None = None,
) -> dict[str, object]:
    progress = progress or when
    return {
        "schema_version": 1,
        "machine_id": machine_id,
        "controller_boot_id": boot_id,
        "sequence": sequence,
        "sent_at": utc_text(when),
        "collection_progress_at": utc_text(progress),
        "notification_progress_at": utc_text(progress),
    }


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".maintenance-test-", dir=Path.cwd(), ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                (Path(directory) / filename).chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temp.cleanup()

    def evaluate(self, value, now=NOW):
        return evaluate_watchdog(self.root / "watchdog.json", value, now=now)

    def test_snapshot_uses_live_database_and_approved_immutable_bundle(self) -> None:
        state_root = self.root / "state"
        output_root = self.root / "backups"
        store = StateStore(state_root, clock=lambda: NOW)
        bundle_name = "incident-approved"
        bundle = store.incident_root / bundle_name
        bundle.mkdir(mode=0o700)
        payloads = {
            "incident.json": b'{"machine_id":"17049"}\n',
            "evidence.json": b'{"bounded":true}\n',
        }
        for name, content in payloads.items():
            path = bundle / name
            path.write_bytes(content)
            path.chmod(0o400)
        manifest = b"".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode("ascii")
            for name, content in sorted(payloads.items())
        )
        (bundle / "manifest.sha256").write_bytes(manifest)
        (bundle / "manifest.sha256").chmod(0o400)
        bundle.chmod(0o500)
        store.db.execute(
            "INSERT INTO incidents(dedup_key,bundle_name,created_utc) VALUES(?,?,?)",
            ("a" * 64, bundle_name, utc_text(NOW)),
        )
        store.db.commit()
        store.close()

        disk_usage = shutil.disk_usage(output_root.parent)
        generous_usage = type(disk_usage)(disk_usage.total, disk_usage.used, 30 * 1024**3)
        def snapshot_during_concurrent_write(connection, *args, **kwargs):
            # A new committed row appears after the bundle list was selected.
            # The backup must still represent the same earlier WAL snapshot.
            writer = sqlite3.connect(state_root / "state.sqlite3")
            try:
                writer.execute(
                    "INSERT INTO incidents(dedup_key,bundle_name,created_utc) VALUES(?,?,?)",
                    ("b" * 64, "not-in-this-snapshot", utc_text(NOW)),
                )
                writer.commit()
            finally:
                writer.close()
            return create_backup(connection, *args, **kwargs)

        with mock.patch(
            "terracompute_ops.backup.shutil.disk_usage", return_value=generous_usage
        ), mock.patch(
            "terracompute_ops.maintenance.create_backup", side_effect=snapshot_during_concurrent_write
        ):
            result = create_local_snapshot(
                state_root, output_root, backup_id="local-1", clock=lambda: NOW
            )

        self.assertTrue(result.success, result.error)
        assert result.published_path is not None
        manifest_json = json.loads((result.published_path / "manifest.json").read_text())
        self.assertIn(
            "evidence/incident-approved/evidence.json",
            {entry["path"] for entry in manifest_json["files"]},
        )
        verified = verify_local_snapshot(result.published_path)
        self.assertTrue(verified.valid, verified.error)
        self.assertEqual(manifest_json["transport"]["status"], "not_attempted")
        snapshot = sqlite3.connect(result.published_path / "state.sqlite3")
        try:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
        finally:
            snapshot.close()

    def test_watchdog_rejects_future_stale_wrong_target_and_replayed_sequence(self) -> None:
        future = self.evaluate(heartbeat(1, NOW + timedelta(microseconds=1)))
        self.assertEqual((future.accepted, future.reason), (False, "future_timestamp"))

        (self.root / "watchdog.json").unlink()
        stale = self.evaluate(heartbeat(1, NOW - timedelta(seconds=91)))
        self.assertEqual((stale.accepted, stale.reason), (False, "stale_heartbeat"))

        (self.root / "watchdog.json").unlink()
        wrong = self.evaluate(heartbeat(1, NOW, machine_id="999"))
        self.assertEqual((wrong.accepted, wrong.reason), (False, "wrong_target"))

        (self.root / "watchdog.json").unlink()
        self.assertTrue(self.evaluate(heartbeat(7, NOW)).accepted)
        unchanged = self.evaluate(heartbeat(7, NOW), NOW + timedelta(seconds=30))
        self.assertEqual(
            (unchanged.status, unchanged.accepted, unchanged.reason),
            ("healthy", False, "no_new_heartbeat"),
        )
        older = self.evaluate(
            heartbeat(6, NOW + timedelta(seconds=30)), NOW + timedelta(seconds=30)
        )
        self.assertEqual((older.accepted, older.reason), (False, "sequence_replay"))
        replay = self.evaluate(
            heartbeat(7, NOW + timedelta(seconds=30)), NOW + timedelta(seconds=30)
        )
        self.assertEqual((replay.accepted, replay.reason), (False, "sequence_replay"))
        new_boot_time = NOW + timedelta(seconds=60)
        self.assertTrue(
            self.evaluate(
                heartbeat(0, new_boot_time, boot_id="boot-b"), new_boot_time
            ).accepted
        )
        old_boot_replay_time = NOW + timedelta(seconds=90)
        old_boot = self.evaluate(
            heartbeat(8, old_boot_replay_time, boot_id="boot-a"),
            old_boot_replay_time,
        )
        self.assertEqual(
            (old_boot.accepted, old_boot.reason), (False, "retired_boot_replay")
        )

    def test_watchdog_missing_threshold_and_stable_recovery(self) -> None:
        first = self.evaluate(heartbeat(1, NOW))
        self.assertEqual(first.status, "healthy")
        at_threshold = self.evaluate(None, NOW + timedelta(seconds=MISSING_AFTER_SECONDS))
        self.assertEqual(at_threshold.status, "healthy")
        missing = self.evaluate(None, NOW + timedelta(seconds=MISSING_AFTER_SECONDS + 1))
        self.assertEqual((missing.status, missing.transition), ("missing", "missing_started"))

        recovery_start = NOW + timedelta(seconds=120)
        recovering = self.evaluate(heartbeat(2, recovery_start), recovery_start)
        self.assertEqual(
            (recovering.status, recovering.transition), ("recovering", "recovery_started")
        )
        sequence = 3
        for seconds in range(30, RECOVERY_STABLE_SECONDS, 30):
            tick = recovery_start + timedelta(seconds=seconds)
            self.assertEqual(self.evaluate(heartbeat(sequence, tick), tick).status, "recovering")
            sequence += 1
        stable = recovery_start + timedelta(seconds=RECOVERY_STABLE_SECONDS)
        recovered = self.evaluate(heartbeat(sequence, stable), stable)
        self.assertEqual((recovered.status, recovered.transition), ("healthy", "recovered"))
        self.assertEqual(recovered.cadence_seconds, WATCHDOG_CADENCE_SECONDS)

    def test_watchdog_persists_startup_grace_without_claiming_health(self) -> None:
        first = self.evaluate(None)
        self.assertEqual(
            (first.status, first.reason, first.alert_required),
            ("starting", "startup_grace", False),
        )
        persisted = json.loads((self.root / "watchdog.json").read_text())
        self.assertEqual(persisted["state_version"], 2)
        self.assertEqual(
            persisted["startup_grace_started_at"],
            NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

        for seconds in (30, 60, MISSING_AFTER_SECONDS):
            restarted = self.evaluate(None, NOW + timedelta(seconds=seconds))
            self.assertEqual(restarted.status, "starting")
            self.assertFalse(restarted.alert_required)
        missing = self.evaluate(
            None, NOW + timedelta(seconds=MISSING_AFTER_SECONDS + 1)
        )
        self.assertEqual(
            (missing.status, missing.transition), ("missing", "missing_started")
        )

    def test_version_one_state_migrates_without_resetting_old_startup_grace(self) -> None:
        legacy = {
            "state_version": 1,
            "machine_id": "17049",
            "status": "unknown",
            "degraded": False,
            "last_boot_id": None,
            "last_sequence": None,
            "last_sent_at": None,
            "last_received_at": None,
            "collection_progress_at": None,
            "notification_progress_at": None,
            "recovery_started_at": None,
            "retired_boot_ids": [],
        }
        state_path = self.root / "watchdog.json"
        state_path.write_text(json.dumps(legacy))
        old = NOW - timedelta(seconds=MISSING_AFTER_SECONDS + 1)
        os.utime(state_path, (old.timestamp(), old.timestamp()))

        result = self.evaluate(None)

        self.assertEqual(result.status, "missing")
        migrated = json.loads(state_path.read_text())
        self.assertEqual(migrated["state_version"], 2)
        self.assertEqual(
            migrated["startup_grace_started_at"],
            old.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

    def test_current_heartbeat_with_stale_progress_is_not_healthy(self) -> None:
        result = self.evaluate(
            heartbeat(1, NOW, progress=NOW - timedelta(seconds=91))
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "stale_work")
        self.assertTrue(result.alert_required)

    def test_cli_failure_metadata_omits_path_and_exception_text(self) -> None:
        secret_shaped_path = self.root / "token-super-secret"
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["verify", "--artifact", str(secret_shaped_path)])
        rendered = output.getvalue()
        self.assertEqual(status, 1)
        self.assertNotIn("token-super-secret", rendered)
        self.assertNotIn("No such file", rendered)
        self.assertEqual(json.loads(rendered)["restic_transfer"], "not_configured")


if __name__ == "__main__":
    unittest.main()
