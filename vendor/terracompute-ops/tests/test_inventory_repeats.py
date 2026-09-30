from __future__ import annotations

import sqlite3
import unittest

from terracompute_ops.inventory import (
    HARD_MAX_NODES,
    Inventory,
    InventoryLimitError,
    capture_probe,
)
from tests.test_inventory import T0, T1, T2, T3, probe_snapshot


GPU_ID = "gpu-00000000-0000-4000-8000-000000000001"


def later(hour: int) -> str:
    return f"2026-09-15T{hour:02d}:00:00Z"


class RepeatedCaptureTests(unittest.TestCase):
    """A capture of unchanged hardware must not add attribute rows."""

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.inventory = Inventory(self.db, "repeat")

    def tearDown(self) -> None:
        self.db.close()

    def attribute_rows(self) -> int:
        return self.db.execute("SELECT count(*) FROM repeat_attribute_assertions").fetchone()[0]

    def active(self, asset_id: str, kind: str, at: str) -> list[tuple[object, int]]:
        stamp = at.replace("Z", ".000000Z")
        return self.db.execute(
            """SELECT a.attribute_value, a.explicit_unknown
               FROM repeat_attribute_assertions a
               WHERE a.asset_id = ? AND a.attribute_kind = ?
                 AND a.valid_from <= ? AND (a.valid_to IS NULL OR a.valid_to > ?)
                 AND NOT EXISTS (
                   SELECT 1 FROM repeat_corrections c
                   WHERE c.assertion_table = 'attribute'
                     AND c.assertion_id = a.assertion_id
                     AND c.effective_at <= ?)
               ORDER BY a.assertion_id""",
            (asset_id, kind, stamp, stamp, stamp),
        ).fetchall()

    def test_unchanged_later_captures_insert_no_attributes(self) -> None:
        first = capture_probe(self.inventory, probe_snapshot(observed_at=T0))
        rows = self.attribute_rows()
        for stamp in (T1, T2, T3):
            repeated = capture_probe(self.inventory, probe_snapshot(observed_at=stamp))
            self.assertEqual(repeated.inserted_attributes, 0)
        self.assertGreater(first.inserted_attributes, 0)
        self.assertEqual(self.attribute_rows(), rows)
        for kind in ("driver_version", "vbios_version", "gpu_model", "pci_bdf"):
            with self.subTest(kind=kind):
                self.assertEqual(len(self.active(GPU_ID, kind, T3)), 1)

    def test_changed_value_still_retracts_the_old_one(self) -> None:
        capture_probe(self.inventory, probe_snapshot(observed_at=T0))
        moved = probe_snapshot(observed_at=T1, bdf="0000:21:00.0")
        capture_probe(self.inventory, moved)
        capture_probe(self.inventory, probe_snapshot(observed_at=T2, bdf="0000:21:00.0"))
        self.assertEqual(self.active(GPU_ID, "pci_bdf", T2), [("0000:21:00.0", 0)])
        # The move back is a change again, not a repeat of the retracted value.
        capture_probe(self.inventory, probe_snapshot(observed_at=T3))
        self.assertEqual(self.active(GPU_ID, "pci_bdf", T3), [("0000:20:00.0", 0)])

    def test_repeated_unknown_is_asserted_once(self) -> None:
        for stamp in (T0, T1, T2, T3):
            capture_probe(
                self.inventory,
                probe_snapshot(observed_at=stamp, serial="To Be Filled By O.E.M."),
            )
        self.assertEqual(self.active(GPU_ID, "serial_status", T3), [(None, 1)])
        unknown_rows = self.db.execute(
            "SELECT count(*) FROM repeat_attribute_assertions "
            "WHERE attribute_kind = 'serial_status'"
        ).fetchone()[0]
        self.assertEqual(unknown_rows, 1)

    def test_repair_collapses_repeats_without_deleting_or_changing_history(self) -> None:
        capture_probe(self.inventory, probe_snapshot(observed_at=T0))
        # Reproduce the pre-fix behaviour: the same value re-asserted every capture.
        for stamp in (T1, T2, T3):
            self.inventory.assert_attribute(
                GPU_ID,
                "driver_version",
                "550.90.07",
                valid_from=stamp,
                observed_at=stamp,
                provenance=f"legacy:{stamp}",
            )
        self.assertEqual(len(self.active(GPU_ID, "driver_version", T3)), 4)
        rows = self.attribute_rows()

        retracted = self.inventory.collapse_duplicate_attributes(
            at=later(0), provenance="test-repair"
        )

        self.assertEqual(retracted, 3)
        self.assertEqual(self.attribute_rows(), rows)
        for stamp in (T0, T1, T2, T3):
            with self.subTest(at=stamp):
                self.assertEqual(
                    self.active(GPU_ID, "driver_version", stamp), [("550.90.07", 0)]
                )
        self.assertEqual(
            self.inventory.collapse_duplicate_attributes(at=later(1), provenance="again"),
            0,
        )

    def test_repair_lets_a_capture_past_the_row_bound_succeed_again(self) -> None:
        capture_probe(self.inventory, probe_snapshot(observed_at=T0))
        for index in range(HARD_MAX_NODES):
            stamp = f"2026-09-14T12:{index // 60 % 60:02d}:{index % 60:02d}.{index:06d}Z"
            self.inventory.assert_attribute(
                GPU_ID,
                "driver_version",
                "550.90.07",
                valid_from=stamp,
                observed_at=stamp,
                provenance="legacy-repeat",
            )
        with self.assertRaises(InventoryLimitError):
            capture_probe(self.inventory, probe_snapshot(observed_at=later(0)))

        self.inventory.collapse_duplicate_attributes(at=later(1), provenance="test-repair")

        result = capture_probe(self.inventory, probe_snapshot(observed_at=later(2)))
        self.assertEqual(result.inserted_attributes, 0)
        self.assertEqual(
            self.active(GPU_ID, "driver_version", later(2)), [("550.90.07", 0)]
        )


if __name__ == "__main__":
    unittest.main()
