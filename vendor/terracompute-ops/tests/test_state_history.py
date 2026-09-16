from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.state import CURRENT_SCHEMA_VERSION, StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.incidents import canonical_json, stable_signature, dedup_key


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
EVENT = {"fault_family": "xid", "code": 79, "device": "gpu0"}


def sample(
    observed_at: str,
    *,
    event: dict | None = EVENT,
    status: str | None = None,
    event_id: str | None = None,
    boot_id: str = "boot-a",
) -> dict:
    value = {
        "target": "vast-machine-17049",
        "machine_id": 17049,
        "boot_id": boot_id,
        "source": "target-probe",
        "observed_at": observed_at,
        "healthy": event is None,
        "events": [] if event is None else [event],
    }
    if status is not None:
        value["status"] = status
    if event_id is not None:
        value["source_event_id"] = event_id
    return value


class MigrationTests(unittest.TestCase):
    def test_baseline_database_migrates_with_rollback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = sqlite3.connect(root / "state.sqlite3")
            database.executescript(
                """
                CREATE TABLE incidents (
                  dedup_key TEXT PRIMARY KEY, bundle_name TEXT NOT NULL,
                  created_utc TEXT NOT NULL
                );
                CREATE TABLE outbox (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  dedup_key TEXT NOT NULL UNIQUE, message TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_utc TEXT NOT NULL, last_error TEXT, sent_utc TEXT
                );
                INSERT INTO incidents VALUES('legacy', 'bundle', '2026-09-14T11:00:00Z');
                INSERT INTO outbox(
                  dedup_key,message,state,attempts,next_attempt_utc,last_error,sent_utc
                ) VALUES(
                  'legacy','preserved','pending',2,'2026-09-14T11:01:00Z','old-error',NULL
                );
                """
            )
            database.commit()
            database.close()

            store = StateStore(root, clock=lambda: NOW)
            try:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION)
                self.assertTrue(store.has_incident("legacy"))
                self.assertEqual(store.db.execute("SELECT status FROM incidents").fetchone()[0], "open")
                outbox = store.db.execute("SELECT * FROM outbox").fetchone()
                self.assertEqual(
                    (outbox["id"], outbox["incident_key"], outbox["event_key"],
                     outbox["message"], outbox["attempts"], outbox["last_error"]),
                    (1, "legacy", "legacy-opened:legacy", "preserved", 2, "old-error"),
                )
                self.assertNotIn(
                    "dedup_key", {row[1] for row in store.db.execute("PRAGMA table_info(outbox)")}
                )
                foreign_key = store.db.execute("PRAGMA foreign_key_list(outbox)").fetchone()
                self.assertEqual(
                    (foreign_key["table"], foreign_key["from"], foreign_key["to"]),
                    ("incidents", "incident_key", "dedup_key"),
                )
                self.assertTrue(list(root.glob("state.sqlite3.pre-migration-v0-*")))
            finally:
                store.close()

    def test_future_database_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = sqlite3.connect(root / "state.sqlite3")
            database.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
            database.close()
            with self.assertRaises(RuntimeError):
                StateStore(root)


class HistoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root, clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)

    def observe(self, value):
        # These lifecycle fixtures arrive as their source clock advances. Tests
        # of stale/future input use a fixed receipt clock in test_freshness.py.
        received = max(self.store.clock(), datetime.fromisoformat(
            value["observed_at"].replace("Z", "+00:00")
        ))
        previous_clock = self.store.clock
        self.store.clock = lambda: max(previous_clock(), received)
        return self.supervisor.observe(value)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_duplicate_delivery_does_not_transition_but_repeat_counts(self) -> None:
        first = sample("2026-09-14T12:00:00Z", event_id="source-1")
        self.observe(first)
        self.assertEqual(self.observe(first).duplicates, 1)
        self.observe(sample("2026-09-14T12:00:01Z"))
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["occurrence_count"], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM transitions").fetchone()[0], 2)

    def test_flap_retains_stable_incident_until_five_minute_recovery(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        self.assertEqual(self.store.current_snapshot()["incidents"][0]["status"], "recovery_pending")
        self.observe(sample("2026-09-14T12:02:00Z"))
        self.observe(sample("2026-09-14T12:03:00Z", event=None))
        self.observe(sample("2026-09-14T12:07:59Z", event=None))
        self.assertEqual(self.store.current_snapshot()["incidents"][0]["status"], "recovery_pending")
        self.observe(sample("2026-09-14T12:08:00Z", event=None))
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["dedup_key"], incident_id)
        self.assertEqual(incident["status"], "recovered")
        self.assertEqual(
            [row["event_type"] for row in self.store.due_notifications()],
            ["opened", "recovered"],
        )
        transitions = [row["transition"] for row in self.store.db.execute("SELECT transition FROM transitions ORDER BY id")]
        self.assertIn("flapped", transitions)
        self.assertEqual(transitions[-1], "recovered")

    def test_recovery_gap_restarts_continuous_window_and_then_recovers(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        self.observe(sample("2026-09-14T12:06:31Z", event=None))
        row = self.store.db.execute(
            "SELECT status,recovery_started_utc FROM incidents WHERE dedup_key=?",
            (incident_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("recovery_pending", "2026-09-14T12:06:31Z"))
        self.observe(sample("2026-09-14T12:11:31Z", event=None))
        self.assertEqual(self.store.db.execute(
            "SELECT status FROM incidents WHERE dedup_key=?", (incident_id,)
        ).fetchone()[0], "recovered")
        transitions = [row[0] for row in self.store.db.execute(
            "SELECT transition FROM transitions WHERE incident_key=? ORDER BY id", (incident_id,)
        )]
        self.assertEqual(transitions.count("recovery_interrupted"), 1)

    def test_fast_source_gap_is_configurable_and_bounded(self) -> None:
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: NOW, healthy_gap_seconds=60)
        self.supervisor = Supervisor(self.store)
        first = sample("2026-09-14T12:00:00Z")
        first["source"] = "prometheus"
        self.observe(first)
        for timestamp in ("2026-09-14T12:01:00Z", "2026-09-14T12:02:01Z"):
            healthy = sample(timestamp, event=None)
            healthy["source"] = "prometheus"
            self.observe(healthy)
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["recovery_started_utc"], "2026-09-14T12:02:01Z")
        with self.assertRaises(ValueError):
            StateStore(self.root / "invalid-gap", healthy_gap_seconds=331)

    def test_reopened_incident_enqueues_a_distinct_event_once(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z", event_id="open"))
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        self.observe(sample("2026-09-14T12:06:00Z", event=None))
        reopened = sample("2026-09-14T12:07:00Z", event_id="reopen")
        self.observe(reopened)
        self.assertEqual(self.observe(reopened).duplicates, 1)
        self.assertEqual(
            [row[0] for row in self.store.db.execute(
                "SELECT event_type FROM outbox ORDER BY id"
            )],
            ["opened", "recovered", "reopened"],
        )

    def test_severity_worsening_enqueues_once_and_updates_default_sound(self) -> None:
        classifications = [
            {"known": True, "label": "same", "severity": "warning"},
            {"known": True, "label": "same", "severity": "critical"},
        ]
        with mock.patch("terracompute_ops.supervisor.classify", side_effect=classifications):
            self.observe(sample("2026-09-14T12:00:00Z"))
            self.observe(sample("2026-09-14T12:01:00Z"))
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["severity"], "critical")
        rows = list(self.store.db.execute(
            "SELECT event_type,severity,silent FROM outbox ORDER BY id"
        ))
        self.assertEqual([tuple(row) for row in rows], [
            ("opened", "warning", 1), ("severity_worsened", "critical", 0)
        ])

    def test_sent_notification_schedules_three_fifteen_minute_reminders(self) -> None:
        moments = [NOW]
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: moments[0])
        self.supervisor = Supervisor(self.store)
        self.observe(sample("2026-09-14T12:00:00Z"))
        initial = self.store.due_notifications()[0]
        self.store.mark_sent(initial["id"])
        seen = []
        for number in range(1, 4):
            moments[0] += timedelta(minutes=15)
            due = self.store.due_notifications()
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["reminder_number"], number)
            seen.append(number)
            self.store.mark_sent(due[0]["id"])
        moments[0] += timedelta(minutes=15)
        self.assertEqual(self.store.due_notifications(), [])
        self.assertEqual(seen, [1, 2, 3])

    def test_acknowledgement_is_fixed_target_idempotent_and_cancels_reminders(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.store.mark_sent(self.store.due_notifications()[0]["id"])
        self.assertTrue(self.store.acknowledge_incident(incident_id))
        self.assertFalse(self.store.acknowledge_incident(incident_id))
        self.assertEqual(self.store.db.execute(
            "SELECT state FROM outbox WHERE event_type='reminder'"
        ).fetchone()[0], "cancelled")
        with self.assertRaises(ValueError):
            self.store.acknowledge_incident("not-a-stable-id")

    def test_boot_change_is_provenance_not_new_incident(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z", boot_id="boot-a"))
        self.observe(sample("2026-09-14T12:01:00Z", boot_id="boot-b"))
        snapshot = self.store.current_snapshot()
        self.assertEqual(len(snapshot["incidents"]), 1)
        self.assertEqual(snapshot["incidents"][0]["last_boot_id"], "boot-b")
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM transitions WHERE transition='boot_changed'"
        ).fetchone()[0], 1)

    def test_unknown_interrupts_the_healthy_recovery_window(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        self.observe(sample("2026-09-14T12:02:00Z", event=None, status="unknown"))
        self.observe(sample("2026-09-14T12:06:00Z", event=None))
        incident = self.store.db.execute(
            "SELECT status,recovery_started_utc FROM incidents WHERE dedup_key=?",
            (incident_id,),
        ).fetchone()
        self.assertEqual((incident["status"], incident["recovery_started_utc"]),
                         ("recovery_pending", "2026-09-14T12:06:00Z"))
        self.observe(sample("2026-09-14T12:11:00Z", event=None))
        self.assertEqual(self.store.db.execute(
            "SELECT status FROM incidents WHERE dedup_key=?", (incident_id,)
        ).fetchone()[0], "recovered")

    def test_stale_healthy_claim_cannot_establish_recovery(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        for freshness in ("stale", "unknown"):
            value = sample("2026-09-14T12:08:00Z", event=None)
            value["freshness"] = freshness
            result = self.observe(value)
            self.assertFalse(result.healthy)
            row = self.store.db.execute(
                "SELECT status,recovery_started_utc FROM incidents WHERE dedup_key=?",
                (incident_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("open", None))

    def test_unknown_stale_and_out_of_order_healthy_remain_explicit(self) -> None:
        self.observe(sample("2026-09-14T12:10:00Z"))
        fault_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:11:00Z", event=None, status="stale"))
        self.observe(sample("2026-09-14T12:12:00Z", event=None, status="unknown"))
        self.observe(sample("2026-09-14T12:05:00Z", event=None))
        fault = self.store.db.execute("SELECT status FROM incidents WHERE dedup_key=?", (fault_id,)).fetchone()
        self.assertEqual(fault["status"], "open")
        last = self.store.incident_history(limit=1)[0]
        self.assertEqual(last["ordering"], "out_of_order")
        statuses = {row["status"] for row in self.store.db.execute("SELECT status FROM observations")}
        self.assertEqual(statuses, {"unhealthy", "stale", "unknown", "healthy"})

    def test_history_and_snapshot_are_bounded_and_consistent(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        incident_id = self.store.current_snapshot()["incidents"][0]["dedup_key"]
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        history = self.store.incident_history(incident_id, limit=5000)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["transitions"], ["recovery_started"])
        backup = self.root / "backup.sqlite3"
        self.assertEqual(self.store.consistent_snapshot(backup), backup)
        copied = sqlite3.connect(backup)
        try:
            self.assertEqual(copied.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
        finally:
            copied.close()
        with self.assertRaises(FileExistsError):
            self.store.consistent_snapshot(backup)

    def test_restart_preserves_pending_recovery(self) -> None:
        self.observe(sample("2026-09-14T12:00:00Z"))
        self.observe(sample("2026-09-14T12:01:00Z", event=None))
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)
        self.observe(sample("2026-09-14T12:06:00Z", event=None))
        self.assertEqual(self.store.current_snapshot()["incidents"][0]["status"], "recovered")

    def test_verified_orphan_recovers_once_and_corrupt_bundle_is_preserved(self) -> None:
        self.store.db.execute(
            """CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
               BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
        )
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe(sample("2026-09-14T12:00:00Z", event_id="orphan-event"))
        published = [path for path in (self.root / "incidents").iterdir() if not path.name.startswith(".")]
        self.assertEqual(len(published), 1)
        corrupt = self.root / "incidents" / "corrupt-bundle"
        corrupt.mkdir()
        (corrupt / "manifest.sha256").write_text("not a manifest")
        self.store.db.execute("DROP TRIGGER reject_outbox")
        self.store.db.commit()
        self.store.close()

        self.store = StateStore(self.root, clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)
        self.assertEqual(self.store.recovery_report, {"recovered": 1, "skipped": 1})
        self.assertEqual(len(self.store.due_notifications()), 1)
        self.assertTrue(corrupt.exists())
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)
        self.assertEqual(self.store.recovery_report["recovered"], 0)
        self.assertEqual(len(self.store.due_notifications()), 1)

    def test_verified_baseline_orphan_is_indexed_without_inventing_alert(self) -> None:
        event = dict(EVENT)
        signature = stable_signature(event)
        key = dedup_key("vast-machine-17049", "boot-a", "xid", signature)
        incident = {
            "schema_version": 1,
            "target": "vast-machine-17049",
            "machine_id": "17049",
            "boot_id": "boot-a",
            "observed_at": "2026-09-14T12:00:00Z",
            "fault_family": "xid",
            "stable_signature": signature,
            "dedup_key": key,
            "classification": {"known": True, "label": "gpu-fallen-off-bus", "severity": "critical"},
        }
        directory = self.root / "incidents" / "baseline-orphan"
        directory.mkdir()
        payloads = {
            "incident.json": canonical_json(incident) + b"\n",
            "evidence.json": canonical_json(event),
        }
        for name, content in payloads.items():
            (directory / name).write_bytes(content)
        manifest = b"".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode("ascii")
            for name, content in sorted(payloads.items())
        )
        (directory / "manifest.sha256").write_bytes(manifest)
        self.store.close()
        self.store = StateStore(self.root, clock=lambda: NOW)
        self.supervisor = Supervisor(self.store)
        self.assertEqual(self.store.recovery_report["recovered"], 1)
        self.assertTrue(self.store.has_incident(key))
        self.assertEqual(self.store.due_notifications(), [])
        self.assertEqual(self.store.incident_history(key)[0]["evidence_json"], EVENT)


if __name__ == "__main__":
    unittest.main()
