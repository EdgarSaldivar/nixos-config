from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops import backup as backup_module
from terracompute_ops.backup import (
    GIB,
    assess_disk_admission,
    create_backup,
    restore_backup,
    validate_backup,
)
from terracompute_ops.inventory import Inventory


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
T0 = "2026-09-14T11:00:00Z"


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence-source"
        self.publish = self.root / "published"
        self.evidence.mkdir(mode=0o700)
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT INTO observations(value) VALUES ('before')")
        self.db.commit()
        # Exercise real atomic publication while counting the rename boundary.
        self.rename_patcher = mock.patch(
            "terracompute_ops.backup.os.rename", wraps=os.rename
        )
        self.rename = self.rename_patcher.start()

    def tearDown(self) -> None:
        self.rename_patcher.stop()
        self.db.close()
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                (Path(directory) / filename).chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.root.chmod(0o700)
        self.temp.cleanup()

    def bundle(self, name: str = "incident-1", content: bytes = b"evidence\n") -> str:
        bundle = self.evidence / name
        bundle.mkdir(mode=0o700)
        evidence = bundle / "evidence.json"
        evidence.write_bytes(content)
        evidence.chmod(0o400)
        bundle.chmod(0o500)
        return name

    def create(self, bundles: tuple[str, ...] = (), **kwargs: object):
        return create_backup(
            self.db,
            self.evidence,
            bundles,
            self.publish,
            backup_id="backup-1",
            clock=lambda: NOW,
            available_bytes=30 * GIB,
            **kwargs,
        )

    def test_consistent_snapshot_manifest_private_publish_and_no_transport_claim(self) -> None:
        bundle = self.bundle()
        result = self.create((bundle,))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.transfer_status, "not_attempted")
        self.assertEqual(self.rename.call_count, 1)
        self.assertEqual(result.verification, "isolated_restore_and_hashes_ok")
        assert result.published_path is not None
        manifest = json.loads((result.published_path / "manifest.json").read_text())
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"state.sqlite3", "evidence/incident-1/evidence.json"})
        self.assertEqual(manifest["transport"]["status"], "not_attempted")
        self.assertEqual(
            stat.S_IMODE(result.published_path.stat().st_mode), 0o500
        )
        self.assertEqual(
            stat.S_IMODE((result.published_path / "state.sqlite3").stat().st_mode),
            0o400,
        )
        self.assertEqual((self.evidence / bundle / "evidence.json").read_bytes(), b"evidence\n")
        self.db.execute("INSERT INTO observations(value) VALUES ('after')")
        self.db.commit()
        restored = sqlite3.connect(":memory:")
        try:
            verified = restore_backup(result.published_path, restored)
            self.assertTrue(verified.valid, verified.error)
            values = [row[0] for row in restored.execute("SELECT value FROM observations")]
            self.assertEqual(values, ["before"])
        finally:
            restored.close()

    def test_integrity_check_reads_the_snapshot_in_place_not_into_memory(self) -> None:
        result = self.create()
        self.assertTrue(result.success, result.error)
        assert result.published_path is not None
        original = Path.read_bytes

        def guarded(path: Path) -> bytes:
            if path.name == "state.sqlite3":
                raise AssertionError("snapshot must not be loaded whole into memory")
            return original(path)

        with mock.patch.object(Path, "read_bytes", guarded):
            validation = validate_backup(result.published_path)
        self.assertTrue(validation.valid, validation.error)
        self.assertEqual(
            sorted(path.name for path in result.published_path.iterdir()),
            ["manifest.json", "manifest.sha256", "state.sqlite3"],
        )

    def test_integrity_check_rejects_a_file_that_is_not_sqlite(self) -> None:
        bogus = self.root / "not-sqlite.sqlite3"
        bogus.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
        with self.assertRaises(backup_module.BackupError):
            backup_module._sqlite_integrity(bogus)

    def test_corruption_is_detected_before_restore(self) -> None:
        result = self.create((self.bundle(),))
        self.assertTrue(result.success, result.error)
        assert result.published_path is not None
        evidence = result.published_path / "evidence" / "incident-1" / "evidence.json"
        evidence.chmod(0o600)
        evidence.write_bytes(b"tampered")
        evidence.chmod(0o400)
        validation = validate_backup(result.published_path)
        self.assertFalse(validation.valid)
        self.assertIn("mismatch", validation.error or "")
        destination = sqlite3.connect(":memory:")
        try:
            self.assertFalse(restore_backup(result.published_path, destination).valid)
        finally:
            destination.close()

    def test_traversal_secret_and_symlink_bundles_fail_closed(self) -> None:
        traversal = self.create(("../escape",))
        self.assertFalse(traversal.success)
        self.assertEqual(traversal.transfer_status, "not_attempted")

        secret_bundle = self.evidence / "secret-bundle"
        secret_bundle.mkdir()
        secret = secret_bundle / "auth.json"
        secret.write_text("sentinel")
        secret.chmod(0o400)
        secret_bundle.chmod(0o500)
        secret_result = create_backup(
            self.db,
            self.evidence,
            ("secret-bundle",),
            self.publish,
            backup_id="secret-attempt",
            clock=lambda: NOW,
            available_bytes=30 * GIB,
        )
        self.assertFalse(secret_result.success)
        self.assertIn("excluded", secret_result.error or "")

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "data").write_text("outside")
        symlink_bundle = self.evidence / "linked-bundle"
        symlink_bundle.symlink_to(outside, target_is_directory=True)
        linked_result = create_backup(
            self.db,
            self.evidence,
            ("linked-bundle",),
            self.publish,
            backup_id="link-attempt",
            clock=lambda: NOW,
            available_bytes=30 * GIB,
        )
        self.assertFalse(linked_result.success)
        self.assertIn("symlink", linked_result.error or "")

    def test_mutable_or_oversized_bundle_is_not_partially_published(self) -> None:
        bundle = self.evidence / "mutable"
        bundle.mkdir()
        path = bundle / "data.json"
        path.write_bytes(b"12345")
        bundle.chmod(0o500)
        mutable = create_backup(
            self.db,
            self.evidence,
            ("mutable",),
            self.publish,
            backup_id="mutable-attempt",
            clock=lambda: NOW,
            available_bytes=30 * GIB,
        )
        self.assertFalse(mutable.success)
        self.assertFalse((self.publish / "mutable-attempt").exists())
        path.chmod(0o400)
        oversized = create_backup(
            self.db,
            self.evidence,
            ("mutable",),
            self.publish,
            backup_id="large-attempt",
            clock=lambda: NOW,
            available_bytes=30 * GIB,
            max_file_bytes=4,
        )
        self.assertFalse(oversized.success)
        self.assertFalse((self.publish / "large-attempt").exists())

    def test_writable_descendant_directory_fails_closed(self) -> None:
        bundle = self.evidence / "nested-mutable"
        nested = bundle / "nested"
        nested.mkdir(parents=True)
        evidence = nested / "evidence.json"
        evidence.write_bytes(b"bounded")
        evidence.chmod(0o400)
        nested.chmod(0o700)
        bundle.chmod(0o500)

        result = self.create((bundle.name,))

        self.assertFalse(result.success)
        self.assertIn("directory is mutable", result.error or "")
        self.assertEqual(self.rename.call_count, 0)

    def test_file_inode_swap_after_approved_scan_fails_closed(self) -> None:
        bundle_name = self.bundle()
        source = self.evidence / bundle_name / "evidence.json"
        original_snapshot = backup_module._snapshot

        def swap_then_snapshot(*args: object, **kwargs: object) -> None:
            bundle = source.parent
            bundle.chmod(0o700)
            source.rename(bundle / "old-evidence.json")
            # Identical bytes prove that inode identity, not only the hash,
            # binds the approved source.
            source.write_bytes(b"evidence\n")
            source.chmod(0o400)
            (bundle / "old-evidence.json").unlink()
            bundle.chmod(0o500)
            original_snapshot(*args, **kwargs)

        with mock.patch(
            "terracompute_ops.backup._snapshot", side_effect=swap_then_snapshot
        ):
            result = self.create((bundle_name,))

        self.assertFalse(result.success)
        self.assertIn("changed before copy", result.error or "")
        self.assertFalse((self.publish / "backup-1").exists())

    def test_file_replaced_by_symlink_after_scan_fails_closed(self) -> None:
        bundle = self.evidence / "symlink-swap"
        bundle.mkdir()
        evidence = bundle / "evidence.json"
        evidence.write_bytes(b"approved\n")
        evidence.chmod(0o400)
        bundle.chmod(0o500)
        outside = self.root / "outside-evidence.json"
        outside.write_bytes(b"approved\n")
        original_snapshot = backup_module._snapshot

        def swap_then_snapshot(*args: object, **kwargs: object) -> None:
            bundle.chmod(0o700)
            evidence.unlink()
            evidence.symlink_to(outside)
            bundle.chmod(0o500)
            original_snapshot(*args, **kwargs)

        with mock.patch(
            "terracompute_ops.backup._snapshot", side_effect=swap_then_snapshot
        ):
            result = self.create((bundle.name,))

        self.assertFalse(result.success)
        self.assertIn("approved file changed before copy", result.error or "")
        self.assertFalse((self.publish / "backup-1").exists())

    def test_every_staged_regular_file_and_directory_is_fsynced_before_publish(self) -> None:
        bundle = self.evidence / "nested"
        nested = bundle / "level"
        nested.mkdir(parents=True)
        evidence = nested / "evidence.json"
        evidence.write_bytes(b"durable\n")
        evidence.chmod(0o400)
        nested.chmod(0o500)
        bundle.chmod(0o500)
        synced_modes: list[int] = []
        real_fsync = os.fsync

        def record_fsync(descriptor: int) -> None:
            synced_modes.append(os.fstat(descriptor).st_mode)
            real_fsync(descriptor)

        # The managed-worker sandbox can reject rename even within its writable
        # temporary root. This test keeps the real fsync calls and isolates only
        # the already-covered atomic rename boundary.
        with mock.patch("terracompute_ops.backup.os.rename"), mock.patch(
            "terracompute_ops.backup.os.fsync", side_effect=record_fsync
        ):
            result = self.create((bundle.name,))

        self.assertTrue(result.success, result.error)
        self.assertEqual(sum(stat.S_ISREG(mode) for mode in synced_modes), 4)
        self.assertEqual(sum(stat.S_ISDIR(mode) for mode in synced_modes), 5)

    def test_restored_inventory_query_and_hashes_match(self) -> None:
        inventory = Inventory(self.db, "hardware")
        inventory.add_asset("psu-1", "gpu_psu", observed_at=T0, provenance="fixture")
        inventory.add_asset("gpu-1", "gpu", observed_at=T0, provenance="fixture")
        inventory.assert_connection(
            "psu-1",
            "output",
            "gpu-1",
            "power",
            "power_path",
            valid_from=T0,
            observed_at=T0,
            provenance="fixture",
        )
        result = self.create()
        self.assertTrue(result.success, result.error)
        assert result.published_path is not None
        validation = validate_backup(result.published_path)
        self.assertTrue(validation.valid, validation.error)
        self.assertEqual(validation.snapshot_sha256, result.snapshot_sha256)
        self.assertEqual(validation.manifest_sha256, result.manifest_sha256)
        digest = hashlib.sha256((result.published_path / "state.sqlite3").read_bytes()).hexdigest()
        self.assertEqual(digest, result.snapshot_sha256)
        restored = sqlite3.connect(":memory:")
        try:
            self.assertTrue(restore_backup(result.published_path, restored).valid)
            restored_inventory = Inventory(restored, "hardware")
            query = restored_inventory.upstream_dependencies("gpu-1", NOW)
            self.assertEqual(query.asset_ids, ("psu-1",))
        finally:
            restored.close()

    def test_disk_warning_routine_restriction_and_protected_history(self) -> None:
        warning = assess_disk_admission(15 * GIB, 30 * GIB)
        self.assertTrue(warning.admitted)
        self.assertTrue(warning.warning)
        routine = assess_disk_admission(18 * GIB, 30 * GIB)
        self.assertFalse(routine.admitted)
        protected = assess_disk_admission(
            19 * GIB, 30 * GIB, capture_class="protected"
        )
        self.assertTrue(protected.admitted)
        self.assertTrue(protected.warning)
        self.assertTrue(protected.protected_history_preserved)
        no_reserve = assess_disk_admission(
            1 * GIB, 10 * GIB, 1, capture_class="protected"
        )
        self.assertFalse(no_reserve.admitted)

    def test_wrong_target_and_open_transaction_report_failure(self) -> None:
        wrong = create_backup(
            self.db,
            self.evidence,
            (),
            self.publish,
            machine_id="999",
            backup_id="wrong",
            available_bytes=30 * GIB,
        )
        self.assertFalse(wrong.success)
        self.assertIn("17049", wrong.error or "")
        self.db.execute("INSERT INTO observations(value) VALUES ('uncommitted')")
        active = create_backup(
            self.db,
            self.evidence,
            (),
            self.publish,
            backup_id="transaction",
            available_bytes=30 * GIB,
        )
        self.assertFalse(active.success)
        self.assertIn("open transaction", active.error or "")
        self.db.rollback()


if __name__ == "__main__":
    unittest.main()
