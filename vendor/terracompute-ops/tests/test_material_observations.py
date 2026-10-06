from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.recovery_coverage import condition


EVENT = {"fault_family": "hardware", "code": "fan", "severity": "warning"}


def timestamp(minute: int, second: int = 0) -> str:
    return f"2026-09-14T12:{minute:02d}:{second:02d}Z"


def probe(
    observed_at: str,
    *,
    event: dict | None = None,
    event_id: str | None = None,
    boot_id: str = "boot-a",
) -> dict:
    value = {
        "target": "vast-machine-17049",
        "machine_id": 17049,
        "source": "target-probe",
        "boot_id": boot_id,
        "boot_verified": True,
        "observed_at": observed_at,
        "healthy": event is None,
        "events": [] if event is None else [event],
        "coverage": [dict(check=condition(EVENT)[0], resource=condition(EVENT)[1],
                          result="pass" if event is None else "fail", evidence_ref="/events")],
    }
    if event_id is not None:
        value["source_event_id"] = event_id
    return value


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: str) -> None:
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00"))


class MaterialObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.clock = MutableClock()
        self.store = StateStore(self.root, clock=self.clock)
        self.supervisor = Supervisor(self.store)

    def tearDown(self) -> None:
        self.store.close()
        for current, directories, files in os.walk(self.root):
            os.chmod(current, 0o700)
            for name in directories:
                os.chmod(Path(current) / name, 0o700)
            for name in files:
                os.chmod(Path(current) / name, 0o600)
        # Some restricted test runners refuse removal after immutable incident
        # permissions were exercised. Cleanup is not part of the assertion.
        shutil.rmtree(self.root, ignore_errors=True)

    def observe(self, value: dict):
        self.clock.set(value["observed_at"])
        return self.supervisor.observe(value)

    def test_reopened_incident_is_material_without_creating_another_bundle(self) -> None:
        event = {"fault_family": "hardware", "code": "fan", "severity": "warning"}
        opened = self.observe(probe(timestamp(0), event=event))
        self.assertTrue(opened.material_changed)

        self.assertFalse(self.observe(probe(timestamp(0, 1))).material_changed)
        recovered = self.observe(probe(timestamp(5, 1)))
        self.assertFalse(recovered.material_changed)
        self.assertEqual(
            self.store.current_snapshot()["incidents"][0]["status"], "recovered"
        )

        reopened = self.observe(probe(timestamp(5, 2), event=event))
        self.assertTrue(reopened.material_changed)
        self.assertEqual(reopened.created, ())
        self.assertIn(
            "reopened",
            [
                row["transition"]
                for row in self.store.db.execute("SELECT transition FROM transitions")
            ],
        )

    def test_severity_worsening_is_material_without_changing_identity(self) -> None:
        warning = {"fault_family": "hardware", "code": "fan", "severity": "warning"}
        critical = {**warning, "severity": "critical"}
        opened = self.observe(probe(timestamp(0), event=warning))
        worsened = self.observe(probe(timestamp(0, 1), event=critical))

        self.assertTrue(opened.material_changed)
        self.assertTrue(worsened.material_changed)
        self.assertEqual(worsened.created, ())
        incident = self.store.current_snapshot()["incidents"][0]
        self.assertEqual(incident["severity"], "critical")
        self.assertEqual(incident["occurrence_count"], 2)

    def test_repeated_sample_is_durable_but_not_material(self) -> None:
        event = {"fault_family": "hardware", "code": "fan", "severity": "warning"}
        self.observe(probe(timestamp(0), event=event))
        repeated = self.observe(probe(timestamp(0, 1), event=event))

        self.assertFalse(repeated.material_changed)
        self.assertEqual(repeated.duplicates, 0)
        self.assertEqual(
            self.store.current_snapshot()["incidents"][0]["occurrence_count"], 2
        )

    def test_source_delivery_replay_and_out_of_order_history_are_not_material(self) -> None:
        event = {"fault_family": "hardware", "code": "fan", "severity": "warning"}
        original = probe(timestamp(1), event=event, event_id="source-1")
        self.observe(original)

        duplicate = self.observe(original)
        self.assertEqual(duplicate.duplicates, 1)
        self.assertFalse(duplicate.material_changed)

        self.observe(probe(timestamp(2), event=event, event_id="source-2"))
        replay = self.observe(
            probe(
                timestamp(1, 30),
                event=event,
                event_id="source-3",
                boot_id="historical-boot",
            )
        )
        self.assertFalse(replay.material_changed)
        ordering = self.store.db.execute(
            "SELECT ordering FROM observations ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(ordering, "out_of_order")

    def test_current_boot_change_is_material_even_without_an_incident(self) -> None:
        initial = self.observe(probe(timestamp(0), boot_id="boot-a"))
        changed = self.observe(probe(timestamp(0, 1), boot_id="boot-b"))

        self.assertFalse(initial.material_changed)
        self.assertTrue(changed.material_changed)
        self.assertEqual(changed.created, ())

    def test_source_cannot_silence_a_critical_notification(self) -> None:
        critical = {"fault_family": "xid", "code": 74, "silent": True}
        result = self.observe(probe(timestamp(0), event=critical))

        self.assertTrue(result.material_changed)
        notification = self.store.due_notifications()[0]
        self.assertEqual(notification["severity"], "critical")
        self.assertEqual(notification["silent"], 0)


if __name__ == "__main__":
    unittest.main()
