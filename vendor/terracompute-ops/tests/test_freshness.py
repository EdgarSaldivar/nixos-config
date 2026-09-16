from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor


class FreshnessTests(unittest.TestCase):
    def test_stale_claim_and_future_clock_cannot_establish_health(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            store = StateStore(Path(root), clock=lambda: now)
            try:
                supervisor = Supervisor(store)
                probe = {"target": "terracompute", "machine_id": "17049",
                         "boot_id": "fixture", "healthy": True, "events": []}
                for explicit in (False, True):
                    stale = dict(probe, observed_at=(now-timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
                    if explicit:
                        stale["freshness"] = "fresh"
                    self.assertFalse(supervisor.observe(stale).healthy)
                    self.assertEqual(store.current_snapshot()["sources"][0]["status"], "stale")
                future = dict(probe, observed_at=(now+timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
                with self.assertRaises(ValueError):
                    supervisor.observe(future)
                current = dict(probe, observed_at=now.isoformat().replace("+00:00", "Z"))
                self.assertTrue(supervisor.observe(current).healthy)
                self.assertEqual(store.current_snapshot()["sources"][0]["status"], "healthy")
                with self.assertRaises(ValueError):
                    supervisor.observe(probe)
            finally:
                store.close()
