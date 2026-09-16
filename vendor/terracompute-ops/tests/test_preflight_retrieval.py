from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.preflight_retrieval import (
    SFTP_PREFLIGHT_PATH,
    SFTP_TARGET,
    PreflightRetrievalError,
    fetch_preflight,
    publish_preflight,
)
from terracompute_ops.runtime_entrypoints import (
    BACKUP_COMMISSIONING_ATTESTATION,
    RuntimeConfigError,
)


class FakeSFTP:
    payload = b""
    returncode = 0
    command: tuple[str, ...] = ()
    batch = b""

    def __init__(self, command, *, stdout, **_kwargs):
        type(self).command = tuple(command)
        if stdout != subprocess.DEVNULL:
            raise AssertionError("sftp stdout must be discarded")
        self.pid = os.getpid()

    def communicate(self, batch, timeout):
        self.__class__.batch = batch
        self.timeout = timeout
        remote, local = batch.decode("ascii").strip().split()[1:]
        if remote != SFTP_PREFLIGHT_PATH:
            raise AssertionError("unexpected remote path")
        Path(local).write_bytes(type(self).payload)


class PreflightRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".preflight-retrieval-", dir=Path.cwd(), ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary.name).absolute()
        self.incoming_root = self.root / "incoming"
        self.publication_root = self.root / "published"
        self.incoming_root.mkdir(mode=0o700)
        self.publication_root.mkdir(mode=0o700)
        self.incoming = self.incoming_root / "pelargir-preflight.json"
        self.publication = self.publication_root / "pelargir-preflight.json"
        self.config = self.root / "backup.json"
        self._write_json(self.config, self._config())
        self.sftp = self.root / "sftp"
        self.sftp.write_bytes(b"test executable")
        self.sftp.chmod(0o700)
        self.identity = self.root / "ssh-identity"
        self.identity.write_bytes(b"test identity")
        self.identity.chmod(0o400)
        self.known_hosts = self.root / "known-hosts"
        self.known_hosts.write_bytes(b"pelargir ssh-ed25519 test")
        self.known_hosts.chmod(0o400)

    def tearDown(self) -> None:
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                path = Path(directory) / filename
                if not path.is_symlink():
                    path.chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temporary.cleanup()

    def _config(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "observation_only": True,
            "machine_id": "17049",
            "commissioning_attestation": BACKUP_COMMISSIONING_ATTESTATION,
            "state_dir": str(self.root / "state"),
            "snapshot_root": str(self.root / "snapshots"),
            "repository": "sftp:terracompute-backup@pelargir:/terracompute-ops",
            "repository_quota_bytes": 250 * 1024**3,
            "minimum_quota_free_bytes": 5 * 1024**3,
            "lock_file": str(self.root / "snapshots" / "backup.lock"),
            "deadline_seconds": 600,
        }

    def _attestation(self) -> dict[str, object]:
        current = datetime.now(timezone.utc)
        return {
            "schema_version": 1,
            "kind": "pelargir-sftp-quota-preflight-v1",
            "machine_id": "17049",
            "commissioning_attestation": BACKUP_COMMISSIONING_ATTESTATION,
            "repository": "sftp:terracompute-backup@pelargir:/terracompute-ops",
            "quota_bytes": 250 * 1024**3,
            "free_bytes": 200 * 1024**3,
            "measured_at": (current - timedelta(minutes=1)).isoformat().replace(
                "+00:00", "Z"
            ),
            "expires_at": (current + timedelta(minutes=5)).isoformat().replace(
                "+00:00", "Z"
            ),
        }

    @staticmethod
    def _write_json(path: Path, document: object) -> None:
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)

    def _write_incoming(self, document: object | None = None) -> None:
        self._write_json(self.incoming, document or self._attestation())

    def _publish(self) -> None:
        publish_preflight(
            config_path=self.config,
            incoming_path=self.incoming,
            publication_path=self.publication,
        )

    def test_publisher_revalidates_then_atomically_publishes(self) -> None:
        expected = self._attestation()
        self._write_incoming(expected)
        self._publish()
        self.assertEqual(
            json.loads(self.publication.read_text(encoding="utf-8")),
            expected,
        )
        status = self.publication.stat()
        self.assertEqual(stat_mode(status.st_mode), 0o640)
        self.assertEqual(status.st_nlink, 1)

    def test_unsafe_parent_clears_old_publication_before_failure(self) -> None:
        self._write_incoming()
        self.publication.write_text("old", encoding="ascii")
        self.publication_root.chmod(0o777)
        with self.assertRaisesRegex(PreflightRetrievalError, "directory-unsafe"):
            self._publish()
        self.assertFalse(self.publication.exists())

    def test_symlink_and_hardlink_inputs_are_rejected(self) -> None:
        source = self.root / "source.json"
        self._write_json(source, self._attestation())
        self.incoming.symlink_to(source)
        with self.assertRaises(PreflightRetrievalError):
            self._publish()
        self.incoming.unlink()
        os.link(source, self.incoming)
        with self.assertRaisesRegex(PreflightRetrievalError, "input-unsafe"):
            self._publish()
        self.assertFalse(self.publication.exists())

    def test_stale_symlink_and_hardlink_destinations_are_unlinked_not_followed(self) -> None:
        self._write_incoming()
        unrelated = self.root / "unrelated"
        unrelated.write_text("do-not-change", encoding="ascii")
        self.publication.symlink_to(unrelated)
        self._publish()
        self.assertEqual(unrelated.read_text(encoding="ascii"), "do-not-change")
        self.assertFalse(self.publication.is_symlink())

        self.publication.unlink()
        os.link(unrelated, self.publication)
        self._publish()
        self.assertEqual(unrelated.read_text(encoding="ascii"), "do-not-change")
        self.assertEqual(unrelated.stat().st_nlink, 1)
        self.assertEqual(self.publication.stat().st_nlink, 1)

    def test_validation_failure_cannot_reuse_old_publication(self) -> None:
        self._write_incoming({"schema_version": 1})
        self.publication.write_text("old-valid-looking-state", encoding="ascii")
        with self.assertRaises(RuntimeConfigError):
            self._publish()
        self.assertFalse(self.publication.exists())
        self.assertFalse((self.publication_root / ".pelargir-preflight.json.publishing").exists())

    def test_fetch_uses_fixed_sftp_target_path_and_removes_stale_input(self) -> None:
        payload = json.dumps(self._attestation()).encode("utf-8")
        FakeSFTP.payload = payload
        FakeSFTP.returncode = 0
        self.incoming.write_text("stale", encoding="ascii")
        with mock.patch(
            "terracompute_ops.preflight_retrieval.subprocess.Popen", FakeSFTP
        ):
            fetch_preflight(
                config_path=self.config,
                incoming_path=self.incoming,
                sftp_executable=self.sftp,
                identity_path=self.identity,
                known_hosts_path=self.known_hosts,
            )
        self.assertEqual(FakeSFTP.command[-1], SFTP_TARGET)
        self.assertEqual(FakeSFTP.command[-2], "--")
        self.assertIn("/dev/null", FakeSFTP.command)
        self.assertIn("-oIdentityAgent=none", FakeSFTP.command)
        self.assertIn("-oPasswordAuthentication=no", FakeSFTP.command)
        self.assertIn("-oKbdInteractiveAuthentication=no", FakeSFTP.command)
        self.assertIn("-oStrictHostKeyChecking=yes", FakeSFTP.command)
        self.assertIn("-oPermitLocalCommand=no", FakeSFTP.command)
        self.assertNotIn("accept-new", " ".join(FakeSFTP.command).lower())
        self.assertEqual(
            FakeSFTP.batch,
            f"get {SFTP_PREFLIGHT_PATH} {self.incoming_root / '.pelargir-preflight.json.fetching'}\n".encode(
                "ascii"
            ),
        )
        self.assertEqual(self.incoming.read_bytes(), payload)
        self.assertEqual(stat_mode(self.incoming.stat().st_mode), 0o640)

    def test_fetch_failure_leaves_no_stale_or_partial_input(self) -> None:
        FakeSFTP.payload = b"not-json"
        FakeSFTP.returncode = 1
        self.incoming.write_text("stale", encoding="ascii")
        with mock.patch(
            "terracompute_ops.preflight_retrieval.subprocess.Popen", FakeSFTP
        ), self.assertRaisesRegex(PreflightRetrievalError, "fetch-failed"):
            fetch_preflight(
                config_path=self.config,
                incoming_path=self.incoming,
                sftp_executable=self.sftp,
                identity_path=self.identity,
                known_hosts_path=self.known_hosts,
            )
        self.assertFalse(self.incoming.exists())
        self.assertFalse((self.incoming_root / ".pelargir-preflight.json.fetching").exists())

    def test_fetch_rejects_batch_injection_and_linked_credentials(self) -> None:
        unsafe = self.incoming_root / "bad\nquit"
        with self.assertRaisesRegex(PreflightRetrievalError, "incoming-path-invalid"):
            fetch_preflight(
                config_path=self.config,
                incoming_path=unsafe,
                sftp_executable=self.sftp,
                identity_path=self.identity,
                known_hosts_path=self.known_hosts,
            )

        linked_identity = self.root / "linked-identity"
        linked_identity.symlink_to(self.identity)
        with self.assertRaisesRegex(PreflightRetrievalError, "credential-invalid"):
            fetch_preflight(
                config_path=self.config,
                incoming_path=self.incoming,
                sftp_executable=self.sftp,
                identity_path=linked_identity,
                known_hosts_path=self.known_hosts,
            )

    def test_fetch_timeout_kills_process_group_and_clears_spool(self) -> None:
        class TimedOutSFTP(FakeSFTP):
            def communicate(self, batch, timeout):
                self.__class__.batch = batch
                raise subprocess.TimeoutExpired("sftp", timeout)

            def wait(self):
                self.returncode = -9

        self.incoming.write_text("stale", encoding="ascii")
        with mock.patch(
            "terracompute_ops.preflight_retrieval.subprocess.Popen", TimedOutSFTP
        ), mock.patch("terracompute_ops.preflight_retrieval.os.killpg") as killpg:
            with self.assertRaisesRegex(PreflightRetrievalError, "fetch-timeout"):
                fetch_preflight(
                    config_path=self.config,
                    incoming_path=self.incoming,
                    sftp_executable=self.sftp,
                    identity_path=self.identity,
                    known_hosts_path=self.known_hosts,
                )
        killpg.assert_called_once_with(os.getpid(), 9)
        self.assertFalse(self.incoming.exists())
        self.assertFalse(
            (self.incoming_root / ".pelargir-preflight.json.fetching").exists()
        )


def stat_mode(mode: int) -> int:
    return mode & 0o777


if __name__ == "__main__":
    unittest.main()
