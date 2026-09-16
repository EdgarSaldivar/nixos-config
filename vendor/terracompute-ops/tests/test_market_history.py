from datetime import datetime, timezone
import unittest

from terracompute_ops.capacity import reconcile_market
from terracompute_ops.vast import MachineObservation, MachineReport, MarketObservation, VastSnapshot


class MarketHistoryTests(unittest.TestCase):
    def test_historical_report_does_not_assert_current_self_test_failure(self):
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)
        snapshot = VastSnapshot(
            now, MachineObservation(17049, now, "terracompute", True, True, False, 8, 0),
            (MachineReport("self-test", "old failure", "2025-01-01T00:00:00Z"),),
            MarketObservation(17049, now, True, (), True, True, None, 8, 8, None, None, None),
            (),
        )
        self.assertEqual(reconcile_market(None, snapshot, now=now), [])
