from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from terracompute_ops.backup_runtime import BackupRunResult
from terracompute_ops.maintenance import WatchdogResult
from terracompute_ops.runtime_entrypoints import (
    BACKUP_COMMISSIONING_ATTESTATION,
    INVESTIGATOR_COMMISSIONING_ATTESTATION,
    WATCHDOG_COMMISSIONING_ATTESTATION,
    RuntimeConfigError,
    backup_main,
    investigator_main,
    load_backup_config,
    load_backup_preflight,
    load_investigator_config,
    load_watchdog_config,
    watchdog_main,
    watchdog_parser,
)
from terracompute_ops.runtime_entrypoints import _trusted_nix_store_hardlink
from terracompute_ops.watchdog_runtime import HealthchecksPingReceipt, HealthchecksTick


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class RuntimeEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".entrypoints-", dir=Path.cwd(), ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary.name).absolute()

    def tearDown(self) -> None:
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                (Path(directory) / filename).chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temporary.cleanup()

    def write(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def backup_config(self) -> dict[str, object]:
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

    def backup_preflight(self, current: datetime = NOW) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "pelargir-sftp-quota-preflight-v1",
            "machine_id": "17049",
            "commissioning_attestation": BACKUP_COMMISSIONING_ATTESTATION,
            "repository": "sftp:terracompute-backup@pelargir:/terracompute-ops",
            "quota_bytes": 250 * 1024**3,
            "free_bytes": 200 * 1024**3,
            "measured_at": (current - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }

    def investigator_config(self) -> dict[str, object]:
        home = "/var/lib/imladris/terracompute-codex"
        runtime = "/var/lib/terracompute-investigator"
        return {
            "schema_version": 1,
            "observation_only": True,
            "machine_id": "17049",
            "commissioning_attestation": INVESTIGATOR_COMMISSIONING_ATTESTATION,
            "request_spool": f"{runtime}/requests",
            "result_spool": f"{runtime}/results",
            "database_path": f"{runtime}/database/investigator.sqlite3",
            "service_home": home,
            "poll_seconds": 1,
            "turn_timeout_seconds": 600,
            "max_spool_entries": 128,
        }

    def watchdog_config(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "observation_only": True,
            "machine_id": "17049",
            "commissioning_attestation": WATCHDOG_COMMISSIONING_ATTESTATION,
            "handoff_file": "/var/lib/imladris/terracompute-ops/controller-heartbeat.json",
            "state_file": "/var/lib/terracompute-watchdog/state/evaluator.json",
            "operation_seconds": 30,
        }

    def test_backup_config_is_exact_nonsecret_and_fixed_target(self) -> None:
        document = self.backup_config()
        parsed = load_backup_config(self.write("backup.json", document))
        self.assertEqual(parsed.repository, document["repository"])
        for key, value in (
            ("machine_id", "7"),
            ("observation_only", False),
            ("commissioning_attestation", "reviewed-but-not-the-fixed-contract"),
            ("repository", "sftp:backup@other:/terracompute-ops"),
        ):
            invalid = dict(document)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeConfigError):
                load_backup_config(self.write(f"invalid-{key}.json", invalid))
        unknown = dict(document, command=["restic", "forget"])
        with self.assertRaisesRegex(RuntimeConfigError, "schema-invalid"):
            load_backup_config(self.write("unknown.json", unknown))

    def test_json_duplicate_nan_and_symlink_are_rejected(self) -> None:
        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaises(RuntimeConfigError):
            load_backup_config(duplicate)
        nan = self.root / "nan.json"
        nan.write_text('{"schema_version":NaN}', encoding="utf-8")
        with self.assertRaises(RuntimeConfigError):
            load_backup_config(nan)
        target = self.write("target.json", self.backup_config())
        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaises(RuntimeConfigError):
            load_backup_config(link)
        hardlink = self.root / "hardlink.json"
        os.link(target, hardlink)
        with self.assertRaises(RuntimeConfigError):
            load_backup_config(hardlink)

    def test_only_immutable_root_owned_nix_store_hardlinks_are_trusted(self) -> None:
        def status(*, mode: int = 0o444, uid: int = 0) -> os.stat_result:
            return os.stat_result((stat.S_IFREG | mode, 0, 0, 2, uid, 0, 10, 0, 0, 0))

        store_path = Path("/nix/store/0123456789abcdefghijklmnopqrstuv-config.json")
        self.assertTrue(_trusted_nix_store_hardlink(store_path, status()))
        self.assertFalse(_trusted_nix_store_hardlink(self.root / "config.json", status()))
        self.assertFalse(_trusted_nix_store_hardlink(store_path, status(mode=0o644)))
        self.assertFalse(_trusted_nix_store_hardlink(store_path, status(uid=1000)))

    def test_backup_requires_matching_live_remote_attestation(self) -> None:
        config = load_backup_config(self.write("backup.json", self.backup_config()))
        attestation = self.backup_preflight()
        parsed = load_backup_preflight(self.write("preflight.json", attestation), config, now=NOW)
        self.assertEqual(parsed.free_bytes, 200 * 1024**3)
        for key, value in (
            ("repository", "sftp:terracompute-backup@pelargir:/somewhere-else"),
            ("quota_bytes", 249 * 1024**3),
            ("free_bytes", 1),
            ("expires_at", "2026-09-15T12:00:00Z"),
            ("expires_at", "2026-09-15T13:00:00Z"),
        ):
            invalid = dict(attestation)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeConfigError):
                load_backup_preflight(
                    self.write(f"bad-preflight-{key}.json", invalid), config, now=NOW
                )
        attestation_path = self.write("hardlinked-preflight.json", attestation)
        attestation_hardlink = self.root / "hardlinked-preflight-copy.json"
        os.link(attestation_path, attestation_hardlink)
        with self.assertRaises(RuntimeConfigError):
            load_backup_preflight(attestation_hardlink, config, now=NOW)

    def test_backup_entrypoint_injects_attestation_not_local_disk_probe(self) -> None:
        config_path = self.write("backup.json", self.backup_config())
        preflight_path = self.write(
            "preflight.json", self.backup_preflight(datetime.now(timezone.utc))
        )
        restic = self.root / "restic"
        password = self.root / "password"
        ssh = self.root / "ssh"
        identity = self.root / "ssh-identity"
        known_hosts = self.root / "ssh-known-hosts"
        runtime = mock.Mock()
        runtime.run_backup.return_value = BackupRunResult(
            True, "17049", "id", self.root / "snapshot", "a" * 64, "ok", "ok"
        )
        with mock.patch(
            "terracompute_ops.runtime_entrypoints.BackupRuntime", return_value=runtime
        ) as constructor:
            self.assertEqual(
                backup_main(
                    [
                        "--config", str(config_path),
                        "--preflight-attestation", str(preflight_path),
                        "--restic-executable", str(restic),
                        "--restic-password-file", str(password),
                        "--ssh-executable", str(ssh),
                        "--ssh-identity-file", str(identity),
                        "--ssh-known-hosts-file", str(known_hosts),
                    ]
                ),
                0,
            )
        restic_config = constructor.call_args.args[0]
        self.assertIsNone(restic_config.repository_mount)
        self.assertEqual(restic_config.ssh_executable, ssh)
        self.assertEqual(restic_config.ssh_identity_file, identity)
        self.assertEqual(restic_config.ssh_known_hosts_file, known_hosts)
        self.assertEqual(
            constructor.call_args.kwargs["preflight_probe"](restic_config).free_bytes,
            200 * 1024**3,
        )
        runtime.run_backup.assert_called_once()

    def test_watchdog_config_is_exact_and_contains_no_notification_secret(self) -> None:
        parsed = load_watchdog_config(self.write("watchdog.json", self.watchdog_config()))
        self.assertEqual(parsed.operation_seconds, 30)
        self.assertTrue(parsed.notification_progress_required)
        relaxed = dict(self.watchdog_config(), notification_progress_required=False)
        self.assertFalse(
            load_watchdog_config(self.write("relaxed.json", relaxed)).notification_progress_required
        )
        for value in (0, "false", None):
            with self.subTest(notification_progress_required=value):
                invalid = dict(self.watchdog_config(), notification_progress_required=value)
                with self.assertRaisesRegex(RuntimeConfigError, "schema-invalid"):
                    load_watchdog_config(self.write("invalid-relaxed.json", invalid))
        wrong = self.watchdog_config()
        wrong["machine_id"] = "999"
        with self.assertRaises(RuntimeConfigError):
            load_watchdog_config(self.write("wrong-watchdog.json", wrong))
        wrong_path = self.watchdog_config()
        wrong_path["handoff_file"] = "/var/lib/terracompute-watchdog/other.json"
        with self.assertRaises(RuntimeConfigError):
            load_watchdog_config(self.write("wrong-watchdog-path.json", wrong_path))
        for forbidden in ("outbox_database", "sender_identity", "ping_url", "token", "chat_id"):
            with self.subTest(forbidden=forbidden):
                invalid = dict(self.watchdog_config(), **{forbidden: "forbidden"})
                with self.assertRaisesRegex(RuntimeConfigError, "schema-invalid"):
                    load_watchdog_config(self.write(f"watchdog-{forbidden}.json", invalid))

    def test_packaged_watchdog_accepts_only_config_and_ping_url_credential_path(self) -> None:
        option_strings = {
            option
            for action in watchdog_parser()._actions
            for option in action.option_strings
        }
        self.assertIn("--healthchecks-ping-url-file", option_strings)
        self.assertNotIn("--telegram-token-file", option_strings)
        self.assertNotIn("--telegram-chat-id-file", option_strings)

        config_path = self.write(
            "watchdog.json", dict(self.watchdog_config(), notification_progress_required=False)
        )
        credential_path = self.root / "healthchecks-ping-url"
        credential_path.write_text("not-read-by-this-test", encoding="ascii")
        credential_path.chmod(0o600)
        ping_url = object()
        pinger = object()
        runtime = mock.Mock()
        runtime.tick.return_value = HealthchecksTick(
            WatchdogResult(
                "healthy",
                True,
                "heartbeat_healthy",
                "healthy_started",
                0,
                0,
                0,
                False,
            ),
            HealthchecksPingReceipt(True, 2),
        )
        output = StringIO()
        with mock.patch(
            "terracompute_ops.runtime_entrypoints.read_healthchecks_ping_url",
            return_value=ping_url,
        ) as read_url, mock.patch(
            "terracompute_ops.runtime_entrypoints.HealthchecksPinger", return_value=pinger
        ) as pinger_type, mock.patch(
            "terracompute_ops.runtime_entrypoints.HealthchecksWatchdogRuntime",
            return_value=runtime,
        ) as runtime_type, redirect_stdout(output):
            result = watchdog_main(
                [
                    "--config",
                    str(config_path),
                    "--healthchecks-ping-url-file",
                    str(credential_path),
                ]
            )
        self.assertEqual(result, 0)
        read_url.assert_called_once_with(credential_path)
        pinger_type.assert_called_once_with(ping_url)
        self.assertIs(runtime_type.call_args.kwargs["notification_progress_required"], False)
        rendered = output.getvalue().lower()
        self.assertNotIn("telegram", rendered)
        self.assertNotIn("not-read-by-this-test", rendered)
        self.assertEqual(json.loads(rendered)["ping"], {"response_bytes": 2, "success": True})

    def test_investigator_config_pins_home_and_exact_app_server_argv(self) -> None:
        codex = Path("/nix/store/pinned-codex/bin/codex")
        parsed = load_investigator_config(
            self.write("investigator.json", self.investigator_config()), codex
        )
        self.assertEqual(parsed.app_server_argv, (str(codex), "app-server"))
        bad = self.investigator_config()
        bad["service_home"] = "/var/lib/alternate-codex"
        with self.assertRaises(RuntimeConfigError):
            load_investigator_config(self.write("bad-home.json", bad), codex)
        bad_path = self.investigator_config()
        bad_path["request_spool"] = "/var/lib/terracompute-investigator-other/requests"
        with self.assertRaises(RuntimeConfigError):
            load_investigator_config(self.write("bad-runtime-path.json", bad_path), codex)

    def test_investigator_entrypoint_has_fixed_bounded_loop(self) -> None:
        config = self.write("investigator.json", self.investigator_config())
        with mock.patch("terracompute_ops.runtime_entrypoints.run_loop") as run:
            self.assertEqual(
                investigator_main(
                    ["--config", str(config), "--codex-executable", "/nix/store/pinned/bin/codex"]
                ),
                0,
            )
        runtime_config, iterations = run.call_args.args
        self.assertEqual(runtime_config.app_server_argv, ("/nix/store/pinned/bin/codex", "app-server"))
        self.assertEqual(iterations, 100_000)


if __name__ == "__main__":
    unittest.main()
