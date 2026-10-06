from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.recovery_coverage import condition


START = datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc)
PERSISTENT = {"fault_family": "gpu", "code": "gpu_vfio_handover_blocked", "evidence": {"pci_bdf": "0000:a1:00.0"}}
TRANSIENT = {"fault_family": "aer", "code": "aer_correctable", "severity": "correctable", "evidence": {"pci_bdf": "0000:24:00.0"}}


class PerIncidentRecoveryTests(unittest.TestCase):
    """A complete observation recovers the incidents it no longer shows."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = START
        self.store = StateStore(Path(self.temp.name), clock=lambda: self.now)
        self.supervisor = Supervisor(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def observe(
        self,
        seconds: int,
        events: list[dict],
        *,
        complete: bool = True,
        eligible: bool = True,
        observed_offset: int = 0,
    ) -> None:
        self.now = START + timedelta(seconds=seconds)
        observed = self.now + timedelta(seconds=observed_offset)
        self.supervisor.observe({
            "recovery_eligible": eligible,
            "target": "terracompute",
            "machine_id": "17049",
            "source": "ssh",
            "boot_id": "boot-a",
            "boot_verified": True,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "status": "unhealthy" if events else "healthy",
            "freshness": "fresh",
            "healthy": not events,
            "complete": complete,
            "coverage": [dict(check=condition(event)[0], resource=condition(event)[1],
                              result="fail" if event in events else "pass", evidence_ref="/events")
                         for event in (PERSISTENT, TRANSIENT)] if complete else [],
            "events": [dict(event) for event in events],
        })

    def statuses(self) -> dict[str, str]:
        return {
            row["fault_family"]: row["status"]
            for row in self.store.db.execute("SELECT fault_family, status FROM incidents")
        }

    def transitions(self, family: str) -> list[str]:
        return [
            row[0]
            for row in self.store.db.execute(
                """SELECT t.transition FROM transitions t JOIN incidents i
                   ON t.incident_key=i.dedup_key
                   WHERE i.fault_family=? AND t.transition!='repeated' ORDER BY t.id""",
                (family,),
            )
        ]

    def test_cleared_fault_recovers_while_another_persists(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT])
        for seconds in (300, 600):
            self.observe(seconds, [PERSISTENT])
        self.assertEqual(self.statuses(), {"gpu": "open", "aer": "recovered"})
        self.assertEqual(self.transitions("aer"), ["opened", "recovery_started", "recovered"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='recovered'"
            ).fetchone()[0],
            1,
        )

    def test_incomplete_observation_interrupts_absence_recovery(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT])
        self.observe(300, [PERSISTENT])
        self.observe(450, [PERSISTENT], complete=False)
        self.observe(600, [PERSISTENT])
        self.assertEqual(self.statuses(), {"gpu": "open", "aer": "recovery_pending"})
        self.observe(900, [PERSISTENT])
        self.assertEqual(self.statuses()["aer"], "recovered")

    def test_absence_needs_the_usual_continuous_window(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT])
        self.observe(300, [PERSISTENT])
        # A gap longer than the ssh healthy gap restarts the recovery window.
        self.observe(700, [PERSISTENT])
        self.assertEqual(self.statuses()["aer"], "recovery_pending")
        self.assertEqual(
            self.transitions("aer"),
            ["opened", "recovery_started", "recovery_interrupted", "recovery_started"],
        )

    def test_reappearing_fault_flaps_back_open(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT])
        self.observe(300, [PERSISTENT])
        self.observe(600, [PERSISTENT, TRANSIENT])
        self.assertEqual(self.statuses(), {"gpu": "open", "aer": "open"})
        self.assertIn("flapped", self.transitions("aer"))

    def test_healthy_sample_that_cannot_see_every_device_is_not_recovery(self) -> None:
        self.observe(0, [TRANSIENT])
        for seconds in (300, 600, 900):
            self.observe(seconds, [], eligible=False)
        self.assertEqual(self.statuses(), {"aer": "open"})
        self.observe(1200, [])
        self.observe(1500, [])
        self.assertEqual(self.statuses(), {"aer": "recovered"})

    def test_out_of_order_or_stale_complete_observation_settles_nothing(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT])
        self.observe(300, [PERSISTENT])
        # An older capture delivered late is history, not a new absence sample.
        self.observe(620, [PERSISTENT], observed_offset=-500)
        self.assertEqual(self.statuses()["aer"], "recovery_pending")
        self.assertEqual(self.transitions("aer"), ["opened", "recovery_started"])
        # A current sample older than the freshness bound is stale: like any
        # non-fresh sample it interrupts pending recovery and never settles.
        self.observe(640, [PERSISTENT], observed_offset=-200)
        self.assertEqual(self.statuses()["aer"], "open")
        self.assertEqual(
            self.transitions("aer"), ["opened", "recovery_started", "recovery_interrupted"]
        )

    def test_observations_without_complete_keep_source_level_recovery(self) -> None:
        self.observe(0, [PERSISTENT, TRANSIENT], complete=False)
        for seconds in (300, 600, 900):
            self.observe(seconds, [PERSISTENT], complete=False)
        self.assertEqual(self.statuses(), {"gpu": "open", "aer": "open"})


if __name__ == "__main__":
    unittest.main()
