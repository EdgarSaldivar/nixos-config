from __future__ import annotations

import sqlite3
import unittest

from terracompute_ops.inventory import (
    Inventory,
    InventoryCycleError,
    InventoryLimitError,
    WrongTargetError,
    capture_probe,
)


T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T13:00:00Z"
T2 = "2026-09-14T14:00:00Z"
T3 = "2026-09-14T15:00:00Z"


def probe_snapshot(
    *,
    observed_at: str = T0,
    bdf: str = "0000:20:00.0",
    uuid: str | None = "GPU-00000000-0000-4000-8000-000000000001",
    serial: str = "SERIAL-0001",
    include_pci: bool = True,
    include_identity: bool = True,
    machine_id: int = 17049,
) -> dict[str, object]:
    gpu: dict[str, object] = {
        "pci_bdf": bdf,
        "name": "NVIDIA Test GPU",
        "serial": serial,
        "driver_version": "550.90.07",
        "vbios_version": "96.00.5E.00.01",
    }
    if uuid is not None:
        gpu["uuid"] = uuid
    snapshot: dict[str, object] = {
        "gpu": {
            "pci_count": 1 if include_pci else None,
            "nvidia_count": 1,
            "gpus": [gpu],
            "pci_devices": [
                {
                    "pci_bdf": bdf,
                    "driver": "nvidia",
                    "pci_root_path": f"/sys/devices/pci0000:00/0000:00:01.0/{bdf}",
                    "numa_node": 0,
                    "current_link_speed": "16.0 GT/s PCIe",
                    "current_link_width": 16,
                    "max_link_speed": "16.0 GT/s PCIe",
                    "max_link_width": 16,
                }
            ] if include_pci else [],
        }
    }
    if include_identity:
        snapshot["system_identity"] = {
            "motherboard": {
                "vendor": "Board Vendor",
                "name": "Board Name",
                "version": "1.0",
                "serial": "BOARD-1",
            },
            "bios": {"vendor": "BIOS Vendor", "version": "1.2.3", "date": "09/14/2026"},
        }
    return {
        "target": "terracompute.example",
        "machine_id": machine_id,
        "observed_at": observed_at,
        "snapshot": snapshot,
    }


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA user_version = 37")
        self.inventory = Inventory(self.db, "hwtest")

    def tearDown(self) -> None:
        self.db.close()

    def asset(self, asset_id: str, kind: str) -> None:
        self.inventory.add_asset(
            asset_id,
            kind,
            observed_at=T0,
            provenance="fixture",
            evidence_ref="bundle/base",
        )

    def connect(
        self,
        source: str,
        destination: str,
        relationship: str = "depends_on",
        confidence: float = 1.0,
    ) -> int:
        return self.inventory.assert_connection(
            source,
            "out",
            destination,
            "in",
            relationship,
            valid_from=T0,
            observed_at=T0,
            provenance="fixture",
            evidence_ref="bundle/base",
            confidence=confidence,
        ).assertion_id

    def test_namespaced_schema_does_not_change_global_user_version(self) -> None:
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 37)
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("hwtest_assets", tables)
        self.assertIn("hwtest_connection_assertions", tables)

    def test_missing_serial_and_alias_conflicts_are_preserved(self) -> None:
        self.asset("gpu-position-a", "gpu")
        self.asset("gpu-position-b", "gpu")
        missing = self.inventory.assert_attribute(
            "gpu-position-a",
            "serial_status",
            None,
            explicit_unknown=True,
            valid_from=T0,
            observed_at=T0,
            provenance="nvidia-smi",
        )
        first = self.inventory.assert_alias(
            "gpu-position-a",
            "uuid",
            "GPU-stable-uuid",
            valid_from=T0,
            observed_at=T0,
            provenance="nvidia-smi",
        )
        duplicate = self.inventory.assert_alias(
            "gpu-position-a",
            "uuid",
            "GPU-stable-uuid",
            valid_from=T0,
            observed_at=T0,
            provenance="nvidia-smi",
        )
        conflict = self.inventory.assert_alias(
            "gpu-position-b",
            "uuid",
            "GPU-stable-uuid",
            valid_from=T0,
            observed_at=T1,
            provenance="contradictory-source",
            confidence=0.5,
        )
        self.assertTrue(missing.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.assertion_id, first.assertion_id)
        self.assertEqual(conflict.contradictions, (first.assertion_id,))
        self.assertEqual(len(self.inventory.assertion_history("alias")), 2)
        self.assertEqual(len(self.inventory.assertion_history("attribute")), 1)

    def test_bdf_move_retains_one_asset_and_original_assertion(self) -> None:
        self.asset("gpu-stable-a", "gpu")
        original = self.inventory.assert_attribute(
            "gpu-stable-a",
            "pci_bdf",
            "0000:41:00.0",
            valid_from=T0,
            observed_at=T0,
            provenance="sysfs",
        )
        moved = self.inventory.move_attribute(
            "gpu-stable-a",
            "pci_bdf",
            "0000:81:00.0",
            effective_at=T1,
            observed_at=T2,
            provenance="sysfs-after-reboot",
        )
        self.assertTrue(moved.inserted)
        self.assertEqual(len(self.inventory.assertion_history("attribute")), 2)
        corrections = self.inventory.assertion_history("correction")
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0][3], original.assertion_id)
        asset_count = self.db.execute("SELECT count(*) FROM hwtest_assets").fetchone()[0]
        self.assertEqual(asset_count, 1)
        with self.assertRaisesRegex(ValueError, "PCI BDF"):
            self.inventory.add_asset(
                "0000:81:00.0", "gpu", observed_at=T0, provenance="bad"
            )

    def test_correction_and_move_preserve_connection_history(self) -> None:
        for asset, kind in (("psu-a", "gpu_psu"), ("psu-b", "gpu_psu"), ("gpu-a", "gpu")):
            self.asset(asset, kind)
        old = self.connect("psu-a", "gpu-a", "power_path")
        correction = self.inventory.correct(
            "connection",
            old,
            effective_at=T1,
            observed_at=T2,
            provenance="onsite-photo",
            reason="lead was traced to psu-b",
        )
        new = self.inventory.assert_connection(
            "psu-b",
            "output-1",
            "gpu-a",
            "power-1",
            "power_path",
            valid_from=T1,
            observed_at=T2,
            provenance="onsite-photo",
            corrects_assertion_id=old,
        )
        self.assertTrue(correction.inserted)
        self.assertTrue(new.inserted)
        self.assertEqual(len(self.inventory.assertion_history("connection")), 2)
        self.assertEqual(
            [row.source_asset_id for row in self.inventory.point_in_time(T0)], ["psu-a"]
        )
        self.assertEqual(
            [row.source_asset_id for row in self.inventory.point_in_time(T2)], ["psu-b"]
        )

    def test_two_cable_paths_are_independent_and_bounded(self) -> None:
        for asset, kind in (
            ("motherboard", "motherboard"),
            ("cable-a", "slimsas_cable"),
            ("cable-b", "slimsas_cable"),
            ("riser-a", "riser"),
            ("gpu-a", "gpu"),
        ):
            self.asset(asset, kind)
        self.inventory.assert_connection(
            "motherboard", "slimsas-a", "cable-a", "board-end", "data_path",
            valid_from=T0, observed_at=T0, provenance="trace"
        )
        self.inventory.assert_connection(
            "cable-a", "riser-end", "riser-a", "slimsas-a", "data_path",
            valid_from=T0, observed_at=T0, provenance="trace"
        )
        self.inventory.assert_connection(
            "motherboard", "slimsas-b", "cable-b", "board-end", "data_path",
            valid_from=T0, observed_at=T0, provenance="trace"
        )
        self.inventory.assert_connection(
            "cable-b", "riser-end", "riser-a", "slimsas-b", "data_path",
            valid_from=T0, observed_at=T0, provenance="trace"
        )
        self.connect("riser-a", "gpu-a", "data_path")
        result = self.inventory.upstream_dependencies(
            "gpu-a", T1, relationship_kinds={"data_path"}
        )
        self.assertEqual(
            set(result.asset_ids), {"motherboard", "cable-a", "cable-b", "riser-a"}
        )
        self.assertFalse(result.uncertain)
        with self.assertRaises(InventoryLimitError):
            self.inventory.upstream_dependencies("gpu-a", T1, max_depth=33)

    def test_shared_psu_and_branched_leads_are_reported(self) -> None:
        components = (
            ("psu-1", "gpu_psu"),
            ("breakout-1", "breakout_board"),
            ("output-1", "breakout_output"),
            ("output-2", "breakout_output"),
            ("lead-1", "power_lead"),
            ("lead-2", "power_lead"),
            ("adapter-1a", "adapter_leg"),
            ("adapter-1b", "adapter_leg"),
            ("gpu-1", "gpu"),
            ("gpu-2", "gpu"),
        )
        for asset, kind in components:
            self.asset(asset, kind)
        self.connect("psu-1", "breakout-1", "power_path")
        for output, lead, gpu in (
            ("output-1", "lead-1", "gpu-1"),
            ("output-2", "lead-2", "gpu-2"),
        ):
            self.connect("breakout-1", output, "power_path")
            self.connect(output, lead, "power_path")
            self.connect(lead, gpu, "power_path")
        self.connect("lead-1", "adapter-1a", "power_path")
        self.connect("lead-1", "adapter-1b", "power_path")
        self.connect("adapter-1a", "gpu-1", "power_path")
        self.connect("adapter-1b", "gpu-1", "power_path")
        sharing = self.inventory.gpu_sharing(
            "gpu-1", T1, relationship_kinds={"power_path"}
        )
        self.assertEqual(sharing.shared_dependencies["psu-1"], ("gpu-2",))
        self.assertEqual(sharing.shared_dependencies["breakout-1"], ("gpu-2",))
        self.assertFalse(sharing.uncertain)

    def test_unknown_relation_is_explicit_and_cycle_is_rejected(self) -> None:
        self.asset("gpu-a", "gpu")
        self.asset("riser-a", "riser")
        self.connect("riser-a", "gpu-a", "data_path")
        self.inventory.record_unknown(
            "riser-a",
            "slimsas-b",
            "data_path",
            "motherboard endpoint is inaccessible",
            valid_from=T0,
            observed_at=T0,
            provenance="onsite",
        )
        result = self.inventory.upstream_dependencies("gpu-a", T1)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.unknowns[0].unknown_detail, "motherboard endpoint is inaccessible")
        with self.assertRaises(InventoryCycleError):
            self.connect("gpu-a", "riser-a", "data_path")
        self.assertEqual(len(self.inventory.assertion_history("connection")), 2)

    def test_cycle_is_rejected_at_any_overlapping_future_interval(self) -> None:
        for asset_id in ("a", "b", "c"):
            self.asset(asset_id, "unknown")
        self.inventory.assert_connection(
            "b", None, "a", None, "depends_on",
            valid_from=T2, observed_at=T0, provenance="fixture",
        )
        with self.assertRaises(InventoryCycleError):
            self.inventory.assert_connection(
                "a", None, "b", None, "depends_on",
                valid_from=T1, observed_at=T0, provenance="fixture",
            )

        other = Inventory(sqlite3.connect(":memory:"), "nonoverlap")
        try:
            for asset_id in ("a", "b", "c"):
                other.add_asset(asset_id, "unknown", observed_at=T0, provenance="fixture")
            other.assert_connection(
                "b", None, "c", None, "depends_on",
                valid_from=T1, valid_to=T2, observed_at=T0, provenance="fixture",
            )
            other.assert_connection(
                "c", None, "a", None, "depends_on",
                valid_from=T2, valid_to=T3, observed_at=T0, provenance="fixture",
            )
            inserted = other.assert_connection(
                "a", None, "b", None, "depends_on",
                valid_from=T1, valid_to=T3, observed_at=T0, provenance="fixture",
            )
            self.assertTrue(inserted.inserted)
        finally:
            other.connection.close()

    def test_active_graph_queries_stop_at_database_bounds(self) -> None:
        for index in range(400):
            source = f"source-{index}"
            destination = f"destination-{index}"
            self.asset(source, "unknown")
            self.asset(destination, "unknown")
            self.connect(source, destination)
        self.asset("source-extra", "unknown")
        self.connect("source-extra", "destination-399")
        self.inventory.record_unknown(
            "destination-399",
            None,
            "depends_on",
            "one upstream component is unidentified",
            valid_from=T0,
            observed_at=T0,
            provenance="fixture",
        )
        complete = self.inventory.upstream_dependencies(
            "destination-399", T1, max_nodes=3
        )
        bounded = self.inventory.upstream_dependencies(
            "destination-399", T1, max_nodes=1
        )
        self.assertEqual(len(complete.unknowns), 1)
        self.assertTrue(complete.uncertain)
        self.assertFalse(complete.truncated)
        self.assertTrue(bounded.uncertain)
        self.assertTrue(bounded.truncated)

        steps = 0

        def stop_unbounded_query() -> int:
            nonlocal steps
            steps += 1
            return int(steps > 500)

        self.db.set_progress_handler(stop_unbounded_query, 1)
        try:
            with self.assertRaises(InventoryLimitError):
                self.inventory.point_in_time(T1, max_rows=1)
            steps = 0
            result = self.inventory.upstream_dependencies("destination-398", T1, max_nodes=2)
        finally:
            self.db.set_progress_handler(None, 0)
        self.assertEqual(result.asset_ids, ("source-398",))
        self.assertFalse(result.truncated)
        self.assertLessEqual(steps, 500)

    def test_capture_probe_tracks_uuid_across_bdf_move_and_provenance(self) -> None:
        first = capture_probe(self.inventory, probe_snapshot())
        moved = capture_probe(self.inventory, probe_snapshot(observed_at=T1, bdf="0000:81:00.0"))
        gpu_assets = self.db.execute(
            "SELECT asset_id FROM hwtest_assets WHERE component_kind = 'gpu'"
        ).fetchall()
        self.assertEqual(gpu_assets, [("gpu-00000000-0000-4000-8000-000000000001",)])
        bdfs = self.db.execute(
            "SELECT attribute_value FROM hwtest_attribute_assertions "
            "WHERE attribute_kind = 'pci_bdf' ORDER BY assertion_id"
        ).fetchall()
        self.assertEqual(bdfs, [("0000:20:00.0",), ("0000:81:00.0",)])
        aliases = self.db.execute(
            "SELECT alias_kind, alias_value FROM hwtest_alias_assertions "
            "WHERE valid_from = ? ORDER BY alias_kind",
            ("2026-09-14T12:00:00.000000Z",),
        ).fetchall()
        self.assertEqual(
            aliases,
            [
                ("serial", "SERIAL-0001"),
                ("uuid", "GPU-00000000-0000-4000-8000-000000000001"),
            ],
        )
        attribute_kinds = {
            row[0]
            for row in self.db.execute(
                "SELECT attribute_kind FROM hwtest_attribute_assertions"
            )
        }
        self.assertTrue(
            {
                "pci_bdf", "pci_root_path", "numa_node", "driver",
                "driver_version", "current_link_speed", "current_link_width",
                "max_link_speed", "max_link_width", "vbios_version",
                "board_vendor", "board_name", "board_version", "board_serial",
                "bios_vendor", "bios_version", "bios_date",
            } <= attribute_kinds
        )
        self.assertNotEqual(first.payload_hash, moved.payload_hash)
        provenances = self.db.execute(
            "SELECT DISTINCT provenance FROM hwtest_attribute_assertions"
        ).fetchall()
        self.assertIn((f"target-probe:sha256:{first.payload_hash}",), provenances)
        self.assertIn((f"target-probe:sha256:{moved.payload_hash}",), provenances)

    def test_probe_partial_and_missing_identity_never_remove_or_bdf_fuse(self) -> None:
        capture_probe(self.inventory, probe_snapshot())
        before = len(self.inventory.assertion_history("attribute"))
        partial = probe_snapshot(observed_at=T1, include_identity=False)
        partial["snapshot"] = {"gpu": {"pci_count": None, "nvidia_count": 0}}
        result = capture_probe(self.inventory, partial)
        self.assertTrue(result.partial)
        self.assertEqual(len(self.inventory.assertion_history("attribute")), before)

        pci_only = probe_snapshot(observed_at=T1, uuid=None)
        pci_only["snapshot"]["gpu"]["gpus"] = []
        first_unknown = capture_probe(self.inventory, pci_only)
        pci_only_later = probe_snapshot(observed_at=T2, uuid=None)
        pci_only_later["snapshot"]["gpu"]["gpus"] = []
        second_unknown = capture_probe(self.inventory, pci_only_later)
        self.assertEqual(len(first_unknown.unresolved_gpu_asset_ids), 1)
        self.assertEqual(len(second_unknown.unresolved_gpu_asset_ids), 1)
        self.assertNotEqual(
            first_unknown.unresolved_gpu_asset_ids,
            second_unknown.unresolved_gpu_asset_ids,
        )

    def test_probe_placeholder_serials_stay_unknown_and_repeat_is_idempotent(self) -> None:
        probe = probe_snapshot(serial="To Be Filled By O.E.M.")
        probe["snapshot"]["system_identity"]["motherboard"]["serial"] = "Default string"
        first = capture_probe(self.inventory, probe)
        counts = {
            table: self.db.execute(f"SELECT count(*) FROM hwtest_{table}").fetchone()[0]
            for table in ("assets", "alias_assertions", "attribute_assertions", "corrections")
        }
        repeated = capture_probe(self.inventory, probe)
        repeated_counts = {
            table: self.db.execute(f"SELECT count(*) FROM hwtest_{table}").fetchone()[0]
            for table in counts
        }
        aliases = self.db.execute(
            "SELECT alias_kind FROM hwtest_alias_assertions ORDER BY assertion_id"
        ).fetchall()
        unknown_kinds = self.db.execute(
            "SELECT attribute_kind FROM hwtest_attribute_assertions "
            "WHERE explicit_unknown = 1 ORDER BY assertion_id"
        ).fetchall()
        self.assertGreater(first.inserted_attributes, 0)
        self.assertEqual(repeated.inserted_assets, 0)
        self.assertEqual(repeated.inserted_aliases, 0)
        self.assertEqual(repeated.inserted_attributes, 0)
        self.assertEqual(counts, repeated_counts)
        self.assertEqual(aliases, [("uuid",)])
        self.assertIn(("serial_status",), unknown_kinds)
        self.assertIn(("board_serial",), unknown_kinds)

    def test_capture_probe_rejects_wrong_target_before_writes(self) -> None:
        with self.assertRaises(WrongTargetError):
            capture_probe(self.inventory, probe_snapshot(machine_id=999))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM hwtest_assets").fetchone()[0], 0
        )

    def test_wrong_target_rejected_everywhere(self) -> None:
        with self.assertRaises(WrongTargetError):
            self.inventory.add_asset(
                "gpu-a", "gpu", observed_at=T0, provenance="fixture", machine_id="999"
            )
        self.asset("gpu-a", "gpu")
        with self.assertRaises(WrongTargetError):
            self.inventory.point_in_time(T0, machine_id="999")


if __name__ == "__main__":
    unittest.main()
