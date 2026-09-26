from __future__ import annotations

import sqlite3
import unittest

from terracompute_ops.inventory import Inventory, InventoryError, capture_probe
from tests.test_inventory import T1, T2, T3, probe_snapshot


GPU_ID = "gpu-00000000-0000-4000-8000-000000000001"


class InventoryCaptureOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.inventory = Inventory(self.db, "ordering")

    def tearDown(self) -> None:
        self.db.close()

    def table_counts(self) -> dict[str, int]:
        return {
            table: self.db.execute(f"SELECT count(*) FROM ordering_{table}").fetchone()[0]
            for table in (
                "assets",
                "alias_assertions",
                "attribute_assertions",
                "corrections",
                "probe_captures",
            )
        }

    def active_bdfs(self) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                """SELECT a.attribute_value
                   FROM ordering_attribute_assertions a
                   WHERE a.asset_id = ? AND a.attribute_kind = 'pci_bdf'
                     AND a.valid_from <= ?
                     AND (a.valid_to IS NULL OR a.valid_to > ?)
                     AND NOT EXISTS (
                       SELECT 1 FROM ordering_corrections c
                       WHERE c.assertion_table = 'attribute'
                         AND c.assertion_id = a.assertion_id
                         AND c.effective_at <= ?)
                   ORDER BY a.assertion_id""",
                (GPU_ID, T3.replace("Z", ".000000Z"), T3.replace("Z", ".000000Z"),
                 T3.replace("Z", ".000000Z")),
            )
        ]

    def test_delayed_unseen_older_capture_is_rejected_before_mutation(self) -> None:
        capture_probe(
            self.inventory,
            probe_snapshot(observed_at=T2, bdf="0000:21:00.0"),
        )
        before = self.table_counts()

        with self.assertRaisesRegex(
            InventoryError, "older than latest completed capture"
        ):
            capture_probe(
                self.inventory,
                probe_snapshot(observed_at=T1, bdf="0000:20:00.0"),
            )

        self.assertEqual(self.table_counts(), before)
        self.assertEqual(self.active_bdfs(), ["0000:21:00.0"])

    def test_oldest_first_history_and_completed_payload_retry_are_idempotent(self) -> None:
        older_probe = probe_snapshot(observed_at=T1, bdf="0000:20:00.0")
        older = capture_probe(self.inventory, older_probe)
        capture_probe(
            self.inventory,
            probe_snapshot(observed_at=T2, bdf="0000:21:00.0"),
        )
        before_retry = self.table_counts()

        retained = capture_probe(self.inventory, older_probe)

        self.assertEqual(retained.asset_ids, older.asset_ids)
        self.assertEqual(retained.unresolved_gpu_asset_ids, older.unresolved_gpu_asset_ids)
        self.assertEqual(
            (retained.inserted_assets, retained.inserted_aliases,
             retained.inserted_attributes),
            (0, 0, 0),
        )
        self.assertEqual(self.table_counts(), before_retry)
        self.assertEqual(
            self.db.execute(
                """SELECT attribute_value FROM ordering_attribute_assertions
                   WHERE asset_id = ? AND attribute_kind = 'pci_bdf'
                   ORDER BY assertion_id""",
                (GPU_ID,),
            ).fetchall(),
            [("0000:20:00.0",), ("0000:21:00.0",)],
        )
        self.assertEqual(self.active_bdfs(), ["0000:21:00.0"])

    def test_failed_capture_rolls_back_assertions_and_completion_marker(self) -> None:
        failed_probe = probe_snapshot(observed_at=T2, bdf="0000:21:00.0")
        failed_probe["snapshot"]["gpu"]["gpus"].append(
            {
                "pci_bdf": "0000:22:00.0",
                "uuid": "GPU-00000000-0000-4000-8000-000000000002",
                "name": "NVIDIA Test GPU",
            }
        )
        self.db.execute(
            """CREATE TRIGGER fail_second_gpu
               BEFORE INSERT ON ordering_assets
               WHEN NEW.asset_id = 'gpu-00000000-0000-4000-8000-000000000002'
               BEGIN SELECT RAISE(ABORT, 'simulated interrupted capture'); END"""
        )
        self.db.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated interrupted"):
            capture_probe(self.inventory, failed_probe)

        self.assertEqual(self.table_counts(), {table: 0 for table in self.table_counts()})
        self.db.execute("DROP TRIGGER fail_second_gpu")
        self.db.commit()
        completed = capture_probe(self.inventory, failed_probe)
        self.assertEqual(completed.inserted_assets, 3)
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM ordering_probe_captures"
            ).fetchall(),
            [("complete",)],
        )

    def test_capture_respects_caller_owned_transaction(self) -> None:
        self.db.execute("BEGIN")
        capture_probe(self.inventory, probe_snapshot(observed_at=T1))
        self.assertTrue(self.db.in_transaction)

        self.db.rollback()

        self.assertEqual(self.table_counts(), {table: 0 for table in self.table_counts()})


if __name__ == "__main__":
    unittest.main()
