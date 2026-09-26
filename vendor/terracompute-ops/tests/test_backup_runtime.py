from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from terracompute_ops.backup import BackupResult, ValidationResult
from terracompute_ops.backup_runtime import (
    BASE_TAG,
    MACHINE_TAG,
    BackupRuntime,
    BackupRuntimeError,
    RepositorySnapshot,
    ResticConfig,
    StoragePreflight,
    build_backup_command,
    build_check_command,
    build_restore_verification_command,
    build_snapshot_verification_command,
    default_storage_preflight,
    parse_backup_json,
    parse_snapshot_json,
    plan_retention,
)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_ID = "a" * 64


class FakeProcess:
    next_pid = 30000

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0, timeout=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._final_returncode = returncode
        self._timeout = timeout
        self.killed = False
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def wait(self, timeout=None):
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired("restic", timeout)
        self.returncode = -9 if self.killed else self._final_returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class BackupRuntimeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.temporary = tempfile.TemporaryDirectory(
                prefix=".backup-runtime-", dir=Path.cwd(), ignore_cleanup_errors=True
            )
        except TypeError:  # Local macOS runner is older than the project minimum.
            self.temporary = tempfile.TemporaryDirectory(
                prefix=".backup-runtime-", dir=Path.cwd()
            )
            self.temporary._finalizer.detach()
            self.temporary.cleanup = lambda: None
        self.root = Path(self.temporary.name).absolute()
        self.executable = self.root / "restic"
        self.executable.write_bytes(b"fake")
        self.executable.chmod(0o700)
        self.credential = self.root / "restic-pass"
        self.credential.write_bytes(b"not-a-real-secret")
        self.credential.chmod(0o400)
        self.ssh_executable = self.root / "ssh"
        self.ssh_executable.write_bytes(b"fake")
        self.ssh_executable.chmod(0o700)
        self.ssh_identity = self.root / "ssh-identity"
        self.ssh_identity.write_bytes(b"not-a-real-private-key")
        self.ssh_identity.chmod(0o400)
        self.ssh_known_hosts = self.root / "ssh-known-hosts"
        self.ssh_known_hosts.write_bytes(b"pelargir ssh-ed25519 not-a-real-host-key")
        self.ssh_known_hosts.chmod(0o400)
        self.mount = self.root / "mount"
        self.mount.mkdir()
        self.lock = self.root / "runtime.lock"
        self.config = ResticConfig(
            self.executable,
            "sftp:terracompute-backup@pelargir:/terracompute-ops",
            self.credential,
            self.ssh_executable,
            self.ssh_identity,
            self.ssh_known_hosts,
            self.mount,
            250 * 1024**3,
            self.lock,
            minimum_quota_free_bytes=1024,
        )
        self.local = self.root / "snapshots" / "local-1"
        self.local.mkdir(parents=True)
        self.validation = ValidationResult(True, "b" * 64, "c" * 64, 1, 42)
        self.snapshot_result = BackupResult(
            True,
            "local-1",
            self.local,
            self.validation.snapshot_sha256,
            self.validation.manifest_sha256,
            1,
            42,
            "isolated_restore_and_hashes_ok",
            "not_attempted",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def preflight(self, _config):
        return StoragePreflight(True, self.config.repository_quota_bytes, 10 * 1024**3)

    def summary(self):
        return json.dumps({"message_type": "summary", "snapshot_id": SNAPSHOT_ID}).encode() + b"\n"

    def listing(self, *, path=None, tags=None, snapshot_id=SNAPSHOT_ID):
        return json.dumps(
            [
                {
                    "id": snapshot_id,
                    "time": "2026-09-15T12:00:00Z",
                    "paths": [str(path or self.local)],
                    "tags": tags or [BASE_TAG, MACHINE_TAG, "backup:local-1"],
                }
            ]
        ).encode()

    def runtime(self, processes, **overrides):
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return processes.pop(0)

        runtime = BackupRuntime(
            self.config,
            preflight_probe=overrides.get("preflight_probe", self.preflight),
            snapshot_creator=overrides.get("snapshot_creator", lambda *a, **k: self.snapshot_result),
            snapshot_verifier=overrides.get("snapshot_verifier", lambda path: self.validation),
            popen_factory=popen,
            clock=lambda: NOW,
        )
        return runtime, calls

    def run_with_root_credential(self, runtime):
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ):
            return runtime.run_backup(
                self.root / "state",
                self.root / "snapshots",
                deadline=NOW + timedelta(minutes=2),
            )

    def test_exact_argv_minimal_env_and_redaction(self):
        runtime, calls = self.runtime([FakeProcess(self.summary()), FakeProcess(self.listing())])
        result = self.run_with_root_credential(runtime)
        self.assertTrue(result.success)
        self.assertEqual(
            calls[0][0],
            (
                str(self.executable),
                "--option",
                self.sftp_option(),
                "backup",
                "--json",
                "--tag",
                BASE_TAG,
                "--tag",
                MACHINE_TAG,
                "--tag",
                "backup:local-1",
                str(self.local),
            ),
        )
        self.assertEqual(
            calls[1][0],
            (
                str(self.executable),
                "--option",
                self.sftp_option(),
                "snapshots",
                "--json",
                SNAPSHOT_ID,
            ),
        )
        for _argv, kwargs in calls:
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(
                kwargs["env"],
                {
                    "LC_ALL": "C",
                    "RESTIC_PASSWORD_FILE": str(self.credential),
                    "RESTIC_REPOSITORY": self.config.repository,
                },
            )
            for inherited in ("HOME", "PATH", "SSH_AUTH_SOCK", "SSH_AGENT_PID"):
                self.assertNotIn(inherited, kwargs["env"])
            self.assertNotIn(self.credential.read_text(), repr((_argv, kwargs)))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def sftp_option(self):
        return " ".join(
            (
                "sftp.command=" + str(self.ssh_executable),
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.ssh_known_hosts}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-i",
                str(self.ssh_identity),
                "terracompute-backup@pelargir",
                "-s",
                "sftp",
            )
        )

    def test_systemd_0440_credentials_are_accepted(self):
        for path in (self.credential, self.ssh_identity, self.ssh_known_hosts):
            path.chmod(0o440)
        runtime, _calls = self.runtime(
            [FakeProcess(self.summary()), FakeProcess(self.listing())]
        )
        result = runtime.run_backup(
            self.root / "state",
            self.root / "snapshots",
            deadline=NOW + timedelta(minutes=2),
        )
        self.assertTrue(result.success)

    def test_wrong_target_stops_before_process(self):
        runtime, calls = self.runtime([])
        with self.assertRaisesRegex(BackupRuntimeError, "wrong_target"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1), machine_id="7")
        self.assertEqual(calls, [])

    def test_deadline_kills_and_reaps_process(self):
        process = FakeProcess(b"", timeout=True)
        runtime, _calls = self.runtime([process])
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ), mock.patch("terracompute_ops.backup_runtime.os.killpg") as killpg:
            with self.assertRaisesRegex(BackupRuntimeError, "deadline_exceeded"):
                runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(seconds=1))
        self.assertTrue(process.killed)
        killpg.assert_called_once_with(process.pid, 9)

    def test_output_cap_fails_closed(self):
        config = replace(self.config, output_limit_bytes=128)
        process = FakeProcess(b"x" * 129)
        runtime, _ = self.runtime([process])
        runtime.config = config
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ), self.assertRaisesRegex(BackupRuntimeError, "restic_output_exceeded"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))

    def test_single_flight_rejects_concurrent_run(self):
        entered = threading.Event()
        release = threading.Event()

        def creator(*args, **kwargs):
            entered.set()
            release.wait(2)
            return self.snapshot_result

        first, _ = self.runtime([FakeProcess(self.summary()), FakeProcess(self.listing())], snapshot_creator=creator)
        second, _ = self.runtime([])
        errors = []

        def run_first():
            try:
                self.run_with_root_credential(first)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(entered.wait(1))
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ), self.assertRaisesRegex(BackupRuntimeError, "backup_already_running"):
            second.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_malformed_and_unknown_json_fail_closed(self):
        for raw in (b"not-json", b'{"message_type":"future"}\n', b'{"message_type":"status"}\n'):
            with self.subTest(raw=raw), self.assertRaises(BackupRuntimeError):
                parse_backup_json(raw)
        with self.assertRaises(BackupRuntimeError):
            parse_snapshot_json(b'{"id":"not-a-list"}')
        unknown = json.loads(self.listing())
        unknown[0]["future_field"] = True
        with self.assertRaisesRegex(BackupRuntimeError, "restic_json_unknown"):
            parse_snapshot_json(json.dumps(unknown).encode())

    def test_mount_quota_and_credentials_preflight(self):
        for status in (
            StoragePreflight(False, self.config.repository_quota_bytes, 10 * 1024**3),
            StoragePreflight(True, None, 10 * 1024**3),
            StoragePreflight(True, self.config.repository_quota_bytes, 1),
        ):
            runtime, calls = self.runtime([], preflight_probe=lambda config, value=status: value)
            with mock.patch(
                "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
            ), self.assertRaisesRegex(BackupRuntimeError, "repository_preflight_failed"):
                runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
            self.assertEqual(calls, [])
        runtime = BackupRuntime(
            self.config,
            preflight_probe=self.preflight,
            snapshot_creator=lambda *args, **kwargs: self.snapshot_result,
            snapshot_verifier=lambda path: self.validation,
            clock=lambda: NOW,
        )
        self.credential.chmod(0o460)
        try:
            with self.assertRaisesRegex(BackupRuntimeError, "credential_file_invalid"):
                runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        finally:
            self.credential.chmod(0o400)

    def test_runtime_rejects_unsafe_transport_files(self):
        cases = (
            ("ssh_executable", "ssh_executable_invalid", 0o600),
            ("ssh_identity_file", "ssh_identity_file_invalid", 0o460),
            ("ssh_known_hosts_file", "ssh_known_hosts_file_invalid", 0o404),
        )
        for field, code, mode in cases:
            path = getattr(self.config, field)
            original = path.stat().st_mode & 0o777
            path.chmod(mode)
            runtime, calls = self.runtime([])
            try:
                with self.subTest(field=field), self.assertRaisesRegex(
                    BackupRuntimeError, code
                ):
                    runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
            finally:
                path.chmod(original)
            self.assertEqual(calls, [])

        link = self.root / "linked-identity"
        link.symlink_to(self.ssh_identity)
        runtime, calls = self.runtime([])
        runtime.config = replace(self.config, ssh_identity_file=link)
        with self.assertRaisesRegex(BackupRuntimeError, "ssh_identity_file_invalid"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        self.assertEqual(calls, [])

        executable_link = self.root / "linked-ssh"
        executable_link.symlink_to(self.ssh_executable)
        runtime, calls = self.runtime([])
        runtime.config = replace(self.config, ssh_executable=executable_link)
        with self.assertRaisesRegex(BackupRuntimeError, "ssh_executable_invalid"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        self.assertEqual(calls, [])

        hardlink = self.root / "hardlinked-known-hosts"
        hardlink.hardlink_to(self.ssh_known_hosts)
        runtime, calls = self.runtime([])
        runtime.config = replace(self.config, ssh_known_hosts_file=hardlink)
        with self.assertRaisesRegex(BackupRuntimeError, "ssh_known_hosts_file_invalid"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        self.assertEqual(calls, [])

    def test_config_rejects_unsafe_transport_values(self):
        for field, value in (
            ("ssh_executable", Path("/nix/store/ssh bad/bin/ssh")),
            ("ssh_identity_file", Path("/run/credentials/key;option")),
            ("ssh_known_hosts_file", Path("/run/credentials/../known-hosts")),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                BackupRuntimeError, "ssh_transport_path_invalid"
            ):
                replace(self.config, **{field: value})
        with self.assertRaisesRegex(BackupRuntimeError, "repository_identity_invalid"):
            replace(self.config, repository="sftp:-oProxyCommand=bad@minas:/repo")

    def test_default_preflight_requires_mounted_exact_quota(self):
        exact = SimpleNamespace(
            total=self.config.repository_quota_bytes,
            used=self.config.repository_quota_bytes - 2048,
            free=2048,
        )
        with mock.patch(
            "terracompute_ops.backup_runtime.os.path.ismount", return_value=True
        ), mock.patch("terracompute_ops.backup_runtime.shutil.disk_usage", return_value=exact):
            self.assertEqual(
                default_storage_preflight(self.config),
                StoragePreflight(True, self.config.repository_quota_bytes, 2048),
            )
        wrong_total = SimpleNamespace(
            total=self.config.repository_quota_bytes + 1,
            used=self.config.repository_quota_bytes - 2048,
            free=2049,
        )
        with mock.patch(
            "terracompute_ops.backup_runtime.os.path.ismount", return_value=True
        ), mock.patch(
            "terracompute_ops.backup_runtime.shutil.disk_usage", return_value=wrong_total
        ):
            self.assertIsNone(default_storage_preflight(self.config).quota_bytes)

    def test_local_and_remote_postconditions_are_explicit(self):
        bad_local = ValidationResult(False, None, None, 0, 0, "bad")
        runtime, calls = self.runtime([], snapshot_verifier=lambda path: bad_local)
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ), self.assertRaisesRegex(BackupRuntimeError, "local_snapshot_verification_failed"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))
        self.assertEqual(calls, [])
        runtime, _ = self.runtime(
            [FakeProcess(self.summary()), FakeProcess(self.listing(path=self.root / "wrong"))]
        )
        with mock.patch(
            "terracompute_ops.backup_runtime._credential_file_is_private", return_value=True
        ), self.assertRaisesRegex(BackupRuntimeError, "snapshot_postcondition_failed"):
            runtime.run_backup(self.root, self.root, deadline=NOW + timedelta(1))

    def test_config_rejects_credential_leakage_shapes(self):
        for repository in (
            "https://user:guess@example/repo",
            "sftp:user:guess@minas:/backups/terracompute-ops",
            "sftp:user@host:/repo?token=guess",
            "password=guess",
            "sftp:user@host:/repo\nnext",
        ):
            with self.subTest(repository=repository), self.assertRaises(BackupRuntimeError):
                replace(self.config, repository=repository)

    def test_only_read_only_verification_commands_can_be_constructed(self):
        prefix = (str(self.executable), "--option", self.sftp_option())
        backup = build_backup_command(self.config, self.local, "local-1")
        snapshots = build_snapshot_verification_command(self.config, SNAPSHOT_ID)
        self.assertEqual(build_check_command(self.config), prefix + ("check", "--json"))
        restore = build_restore_verification_command(self.config, SNAPSHOT_ID, self.root / "restore")
        self.assertEqual(backup[:3], prefix)
        self.assertEqual(snapshots[:3], prefix)
        self.assertEqual(restore[:3], prefix)
        self.assertEqual(restore[3], "restore")
        self.assertIn("--verify", restore)
        for command in (backup, snapshots, build_check_command(self.config), restore):
            self.assertEqual(command.count("--option"), 1)
            self.assertEqual(command.count(self.sftp_option()), 1)
            self.assertFalse({"init", "forget", "prune", "delete"}.intersection(command))
            self.assertNotIn(self.config.repository, command)
            self.assertNotIn(str(self.credential), command)
            self.assertNotIn("ssh", command)
        runtime, calls = self.runtime([])
        with self.assertRaisesRegex(BackupRuntimeError, "restic_command_not_allowed"):
            runtime._run(build_check_command(self.config), NOW + timedelta(1))
        self.assertEqual(calls, [])


class RetentionPlannerTests(unittest.TestCase):
    def snapshot(self, number, when, tags=()):
        return RepositorySnapshot(
            f"{number:064x}",
            when,
            ("/var/lib/backups/local",),
            (BASE_TAG, MACHINE_TAG, *tags),
        )

    def test_policy_buckets_and_protected_history(self):
        latest = datetime(2026, 9, 15, 12, 45, tzinfo=timezone.utc)
        snapshots = [self.snapshot(index + 1, latest - timedelta(hours=index)) for index in range(60)]
        protected = self.snapshot(1000, latest - timedelta(days=4000), ("open-incident:incident-7",))
        plan = plan_retention([*snapshots, protected])
        self.assertEqual(plan.policy, {"hourly": 48, "daily": 30, "weekly": 12, "monthly": 12, "yearly": 5})
        self.assertIn(protected.snapshot_id, plan.retained_ids)
        protected_decision = next(item for item in plan.decisions if item.snapshot_id == protected.snapshot_id)
        self.assertTrue(protected_decision.protected)
        self.assertIn("protected-tag", protected_decision.reasons)
        hourly = [item for item in plan.decisions if "hourly" in item.reasons]
        self.assertEqual(len(hourly), 48)
        self.assertTrue(plan.unretained_ids)

    def test_retention_wrong_target_fails_closed_and_never_executes(self):
        wrong = RepositorySnapshot(
            SNAPSHOT_ID,
            NOW,
            ("/snapshot",),
            (BASE_TAG, "machine:999"),
        )
        with self.assertRaisesRegex(BackupRuntimeError, "wrong_target"):
            plan_retention([wrong])


if __name__ == "__main__":
    unittest.main()
