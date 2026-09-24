from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor


class DeliveryPolicyTests(unittest.TestCase):
    def test_critical_priority_and_persisted_rate_limit(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        clock = [now]
        with tempfile.TemporaryDirectory() as root:
            store = StateStore(Path(root), clock=lambda: clock[0])
            try:
                supervisor = Supervisor(store)
                for event in ({"fault_family": "unknown", "code": "fixture"},
                              {"fault_family": "xid", "code": 79}):
                    supervisor.observe({"machine_id": "17049", "target": "terracompute",
                        "boot_id": "fixture", "observed_at": now.isoformat().replace("+00:00", "Z"),
                        "healthy": False, "events": [event]})
                critical, warning = store.due_notifications()
                self.assertEqual(critical["severity"], "critical")
                self.assertGreater(critical["id"], warning["id"])
                store.mark_failed(critical["id"], 0, "rate-limited", retry_after_seconds=600)
                store.close()
                clock[0] += timedelta(seconds=100)
                store = StateStore(Path(root), clock=lambda: clock[0])
                self.assertEqual([r["id"] for r in store.due_notifications()], [warning["id"]])
                clock[0] += timedelta(seconds=501)
                self.assertEqual(store.due_notifications()[0]["id"], critical["id"])
                self.assertTrue(store.acknowledge_incident(critical["incident_id"]))
            finally:
                store.close()
