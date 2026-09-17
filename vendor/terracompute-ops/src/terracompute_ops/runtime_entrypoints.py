"""Strict process entrypoints for the optional repository-owned runtimes.

The JSON files parsed here are nonsecret deployment metadata.  Secret paths and
the pinned executables are supplied by the service definition, not by JSON.
There is intentionally no generic command or argument-list configuration.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .backup_runtime import (
    BackupRuntime,
    BackupRuntimeError,
    ResticConfig,
    StoragePreflight,
)
from .investigator_runtime import (
    MAX_RUN_ITERATIONS,
    InvestigatorRuntimeConfig,
    InvestigatorRuntimeError,
    run_loop,
)
from .watchdog_runtime import (
    MAX_OPERATION_SECONDS,
    HeartbeatReceiver,
    HealthchecksPinger,
    HealthchecksWatchdogRuntime,
    WatchdogRuntimeError,
    operation_deadline,
    read_healthchecks_ping_url,
)


MACHINE_ID = "17049"
PELARGIR_REPOSITORY = "sftp:terracompute-backup@pelargir:/terracompute-ops"
SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
BACKUP_COMMISSIONING_ATTESTATION = (
    "backup-v2-pelargir-receiver-and-quota-probe-verified"
)
WATCHDOG_COMMISSIONING_ATTESTATION = (
    "watchdog-v2-local-heartbeat-and-healthchecks-verified"
)
INVESTIGATOR_COMMISSIONING_ATTESTATION = (
    "investigator-v1-linux-arm64-isolation-and-auth-seeding-verified"
)
INVESTIGATOR_HOME = Path("/var/lib/imladris/terracompute-codex")
INVESTIGATOR_ROOT = Path("/var/lib/terracompute-investigator")
WATCHDOG_ROOT = Path("/var/lib/terracompute-watchdog")
WATCHDOG_HEARTBEAT_PATH = Path(
    "/var/lib/imladris/terracompute-ops/controller-heartbeat.json"
)
_REPOSITORY = re.compile(r"^sftp:[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:/[A-Za-z0-9_./-]+$")
_DIRECT_NIX_STORE_FILE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/]+$")


class RuntimeConfigError(ValueError):
    """A fixed, nonsecret configuration failure."""


def _reject_constant(_value: str) -> None:
    raise RuntimeConfigError("config-json-invalid")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeConfigError("config-json-invalid")
        result[key] = value
    return result


def _trusted_nix_store_hardlink(path: Path, status: os.stat_result) -> bool:
    """Accept only immutable root-owned store files optimized by Nix."""

    return (
        status.st_nlink > 1
        and status.st_uid == 0
        and stat.S_IMODE(status.st_mode) & 0o222 == 0
        and _DIRECT_NIX_STORE_FILE.fullmatch(str(path)) is not None
    )


def _read_json(
    path: Path, *, label: str, allow_nix_store_hardlink: bool = False
) -> dict[str, Any]:
    if not path.is_absolute():
        raise RuntimeConfigError(f"{label}-path-invalid")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or (
            status.st_nlink != 1
            and not (
                allow_nix_store_hardlink
                and _trusted_nix_store_hardlink(path, status)
            )
        ):
            raise RuntimeConfigError(f"{label}-file-invalid")
        if status.st_size > MAX_CONFIG_BYTES:
            raise RuntimeConfigError(f"{label}-size-limit")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except RuntimeConfigError:
        raise
    except OSError as error:
        raise RuntimeConfigError(f"{label}-unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeConfigError(f"{label}-size-limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RuntimeConfigError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeConfigError(f"{label}-json-invalid") from error
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{label}-schema-invalid")
    return value


def _exact(document: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(document) != fields:
        raise RuntimeConfigError(f"{label}-schema-invalid")


def _base(document: Mapping[str, Any], attestation: str, label: str) -> None:
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or isinstance(document.get("schema_version"), bool)
        or document.get("observation_only") is not True
        or document.get("machine_id") != MACHINE_ID
        or document.get("commissioning_attestation") != attestation
    ):
        raise RuntimeConfigError(f"{label}-commissioning-invalid")


def _path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise RuntimeConfigError(f"{field}-invalid")
    result = Path(value)
    if not result.is_absolute() or ".." in result.parts:
        raise RuntimeConfigError(f"{field}-invalid")
    return result


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise RuntimeConfigError(f"{field}-invalid")
    return value


def _number(value: object, field: str, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise RuntimeConfigError(f"{field}-invalid")
    return float(value)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise RuntimeConfigError(f"{field}-invalid")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RuntimeConfigError(f"{field}-invalid") from error
    if result.tzinfo is None:
        raise RuntimeConfigError(f"{field}-invalid")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class BackupEntrypointConfig:
    state_dir: Path
    snapshot_root: Path
    repository: str
    repository_quota_bytes: int
    minimum_quota_free_bytes: int
    lock_file: Path
    deadline_seconds: float


@dataclass(frozen=True)
class BackupPreflightAttestation:
    repository: str
    quota_bytes: int
    free_bytes: int
    measured_at: datetime
    expires_at: datetime


def load_backup_config(path: Path) -> BackupEntrypointConfig:
    document = _read_json(path, label="backup-config", allow_nix_store_hardlink=True)
    _exact(
        document,
        {
            "schema_version",
            "observation_only",
            "machine_id",
            "commissioning_attestation",
            "state_dir",
            "snapshot_root",
            "repository",
            "repository_quota_bytes",
            "minimum_quota_free_bytes",
            "lock_file",
            "deadline_seconds",
        },
        "backup-config",
    )
    _base(document, BACKUP_COMMISSIONING_ATTESTATION, "backup")
    repository = document["repository"]
    if repository != PELARGIR_REPOSITORY or not _REPOSITORY.fullmatch(repository):
        raise RuntimeConfigError("backup-repository-invalid")
    quota = _integer(document["repository_quota_bytes"], "backup-quota", 1, 2**63 - 1)
    minimum = _integer(
        document["minimum_quota_free_bytes"], "backup-minimum-free", 1, quota
    )
    return BackupEntrypointConfig(
        _path(document["state_dir"], "backup-state-dir"),
        _path(document["snapshot_root"], "backup-snapshot-root"),
        repository,
        quota,
        minimum,
        _path(document["lock_file"], "backup-lock-file"),
        _number(document["deadline_seconds"], "backup-deadline", 30, 900),
    )


def load_backup_preflight(
    path: Path,
    config: BackupEntrypointConfig,
    *,
    now: datetime | None = None,
) -> BackupPreflightAttestation:
    document = _read_json(path, label="backup-preflight")
    _exact(
        document,
        {
            "schema_version",
            "kind",
            "machine_id",
            "commissioning_attestation",
            "repository",
            "quota_bytes",
            "free_bytes",
            "measured_at",
            "expires_at",
        },
        "backup-preflight",
    )
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or isinstance(document.get("schema_version"), bool)
        or document.get("kind") != "pelargir-sftp-quota-preflight-v1"
        or document.get("machine_id") != MACHINE_ID
        or document.get("commissioning_attestation") != BACKUP_COMMISSIONING_ATTESTATION
        or document.get("repository") != config.repository
    ):
        raise RuntimeConfigError("backup-preflight-invalid")
    quota = _integer(document["quota_bytes"], "backup-preflight-quota", 1, 2**63 - 1)
    free = _integer(document["free_bytes"], "backup-preflight-free", 0, quota)
    measured = _timestamp(document["measured_at"], "backup-preflight-measured-at")
    expires = _timestamp(document["expires_at"], "backup-preflight-expires-at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        quota != config.repository_quota_bytes
        or free < config.minimum_quota_free_bytes
        or measured > current
        or expires <= current
        or expires <= measured
        or expires - measured > timedelta(minutes=15)
    ):
        raise RuntimeConfigError("backup-preflight-invalid")
    return BackupPreflightAttestation(config.repository, quota, free, measured, expires)


def backup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terracompute-backup", allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--preflight-attestation", required=True, type=Path)
    parser.add_argument("--restic-executable", required=True, type=Path)
    parser.add_argument("--restic-password-file", required=True, type=Path)
    parser.add_argument("--ssh-executable", required=True, type=Path)
    parser.add_argument("--ssh-identity-file", required=True, type=Path)
    parser.add_argument("--ssh-known-hosts-file", required=True, type=Path)
    return parser


def backup_main(argv: list[str] | None = None) -> int:
    operation = "backup"
    try:
        args = backup_parser().parse_args(argv)
        config = load_backup_config(args.config)
        preflight = load_backup_preflight(args.preflight_attestation, config)
        restic = ResticConfig(
            executable=_path(str(args.restic_executable), "restic-executable"),
            repository=config.repository,
            credential_file=_path(str(args.restic_password_file), "restic-password-file"),
            ssh_executable=_path(str(args.ssh_executable), "ssh-executable"),
            ssh_identity_file=_path(str(args.ssh_identity_file), "ssh-identity-file"),
            ssh_known_hosts_file=_path(
                str(args.ssh_known_hosts_file), "ssh-known-hosts-file"
            ),
            repository_mount=None,
            repository_quota_bytes=config.repository_quota_bytes,
            lock_file=config.lock_file,
            minimum_quota_free_bytes=config.minimum_quota_free_bytes,
        )
        probe = lambda _config: StoragePreflight(  # noqa: E731 - fixed attestation adapter
            True, preflight.quota_bytes, preflight.free_bytes
        )
        runtime = BackupRuntime(restic, preflight_probe=probe)
        result = runtime.run_backup(
            config.state_dir,
            config.snapshot_root,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=config.deadline_seconds),
            machine_id=MACHINE_ID,
        )
        print(json.dumps(asdict(result), default=str, sort_keys=True, separators=(",", ":")))
        return 0
    except (RuntimeConfigError, BackupRuntimeError, OSError, ValueError):
        print(
            json.dumps(
                {"machine_id": MACHINE_ID, "operation": operation, "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1


@dataclass(frozen=True)
class WatchdogEntrypointConfig:
    handoff_file: Path
    state_file: Path
    operation_seconds: float
    notification_progress_required: bool = True


def load_watchdog_config(path: Path) -> WatchdogEntrypointConfig:
    document = _read_json(path, label="watchdog-config", allow_nix_store_hardlink=True)
    fields = {
        "schema_version",
        "observation_only",
        "machine_id",
        "commissioning_attestation",
        "handoff_file",
        "state_file",
        "operation_seconds",
    }
    # Optional so existing configs keep requiring notification progress.
    if "notification_progress_required" in document:
        fields.add("notification_progress_required")
    _exact(document, fields, "watchdog-config")
    notification_progress_required = document.get("notification_progress_required", True)
    if not isinstance(notification_progress_required, bool):
        raise RuntimeConfigError("watchdog-config-schema-invalid")
    _base(document, WATCHDOG_COMMISSIONING_ATTESTATION, "watchdog")
    paths = (
        _path(document["handoff_file"], "watchdog-handoff-file"),
        _path(document["state_file"], "watchdog-state-file"),
    )
    expected_paths = (
        WATCHDOG_HEARTBEAT_PATH,
        WATCHDOG_ROOT / "state" / "evaluator.json",
    )
    if paths != expected_paths:
        raise RuntimeConfigError("watchdog-path-contract-invalid")
    return WatchdogEntrypointConfig(
        *paths,
        _number(
            document["operation_seconds"],
            "watchdog-operation-seconds",
            1,
            MAX_OPERATION_SECONDS,
        ),
        notification_progress_required,
    )


def watchdog_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terracompute-watchdog", allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--healthchecks-ping-url-file", required=True, type=Path)
    return parser


def watchdog_main(argv: list[str] | None = None) -> int:
    try:
        args = watchdog_parser().parse_args(argv)
        config = load_watchdog_config(args.config)
        ping_url = read_healthchecks_ping_url(
            _path(
                str(args.healthchecks_ping_url_file),
                "healthchecks-ping-url-file",
            )
        )
        receiver = HeartbeatReceiver(handoff_path=config.handoff_file)
        pinger = HealthchecksPinger(ping_url)
        runtime = HealthchecksWatchdogRuntime(
            receiver,
            config.state_file,
            pinger,
            notification_progress_required=config.notification_progress_required,
        )
        result = runtime.tick(deadline=operation_deadline(config.operation_seconds))
        output = asdict(result)
        # This is a legacy evaluator field, not a runtime capability.  Omit it from
        # the packaged Healthchecks watchdog's output surface.
        output["evaluation"].pop("telegram_send", None)
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    except (RuntimeConfigError, WatchdogRuntimeError, OSError, ValueError):
        print(
            json.dumps(
                {"machine_id": MACHINE_ID, "operation": "watchdog", "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1


def load_investigator_config(path: Path, codex_executable: Path) -> InvestigatorRuntimeConfig:
    document = _read_json(
        path, label="investigator-config", allow_nix_store_hardlink=True
    )
    _exact(
        document,
        {
            "schema_version",
            "observation_only",
            "machine_id",
            "commissioning_attestation",
            "request_spool",
            "result_spool",
            "database_path",
            "service_home",
            "poll_seconds",
            "turn_timeout_seconds",
            "max_spool_entries",
        },
        "investigator-config",
    )
    _base(document, INVESTIGATOR_COMMISSIONING_ATTESTATION, "investigator")
    service_home = _path(document["service_home"], "investigator-service-home")
    if service_home != INVESTIGATOR_HOME:
        raise RuntimeConfigError("investigator-service-home-invalid")
    executable = _path(str(codex_executable), "codex-executable")
    request_spool = _path(document["request_spool"], "investigator-request-spool")
    result_spool = _path(document["result_spool"], "investigator-result-spool")
    database_path = _path(document["database_path"], "investigator-database-path")
    if (
        request_spool != INVESTIGATOR_ROOT / "requests"
        or result_spool != INVESTIGATOR_ROOT / "results"
        or database_path != INVESTIGATOR_ROOT / "database" / "investigator.sqlite3"
    ):
        raise RuntimeConfigError("investigator-runtime-path-invalid")
    return InvestigatorRuntimeConfig(
        request_spool=request_spool,
        result_spool=result_spool,
        database_path=database_path,
        service_home=service_home,
        app_server_argv=(str(executable), "app-server"),
        poll_seconds=_number(document["poll_seconds"], "investigator-poll-seconds", 0.1, 60),
        turn_timeout_seconds=_number(
            document["turn_timeout_seconds"], "investigator-turn-timeout-seconds", 1, 600
        ),
        max_spool_entries=_integer(
            document["max_spool_entries"], "investigator-max-spool-entries", 1, 128
        ),
    )


def investigator_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terracompute-investigator", allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--codex-executable", required=True, type=Path)
    return parser


def investigator_main(argv: list[str] | None = None) -> int:
    try:
        args = investigator_parser().parse_args(argv)
        config = load_investigator_config(args.config, args.codex_executable)
        run_loop(config, MAX_RUN_ITERATIONS)
        return 0
    except (RuntimeConfigError, InvestigatorRuntimeError, OSError, ValueError):
        print(
            json.dumps(
                {"machine_id": MACHINE_ID, "operation": "investigator", "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


ACTIONS_COMMISSIONING_ATTESTATION = (
    "actions-v1-monitor-restart-actor-telegram-and-live-dry-check-verified"
)
ACTIONS_STATE_ROOT = Path("/var/lib/imladris/terracompute-ops")
ACTIONS_PRIVATE_ROOT = Path("/var/lib/terracompute-actions")
ACTIONS_POLICY_REVISION = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")
ACTIONS_ACTOR_TARGET = "terracompute-actor@10.50.0.2"


@dataclass(frozen=True)
class ActionsEntrypointConfig:
    state_database: Path
    actions_database: Path
    inbox_path: Path
    backup_trigger_file: Path
    actor_target: str
    telegram_group_id: int
    telegram_bot_username: str
    policy_revision: str
    tick_seconds: float


def load_actions_config(path: Path) -> ActionsEntrypointConfig:
    """Load the approval-gated action service's strict nonsecret configuration."""
    from .policy import DEPLOYMENT_APPROVAL_GROUP_ID

    document = _read_json(path, label="actions-config", allow_nix_store_hardlink=True)
    _exact(
        document,
        {
            "schema_version", "machine_id", "commissioning_attestation", "state_database",
            "actions_database", "inbox_path", "backup_trigger_file", "actor_target",
            "telegram_group_id",
            "telegram_bot_username", "policy_revision", "tick_seconds",
        },
        "actions-config",
    )
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or isinstance(document.get("schema_version"), bool)
        or document.get("machine_id") != MACHINE_ID
        or document.get("commissioning_attestation") != ACTIONS_COMMISSIONING_ATTESTATION
    ):
        raise RuntimeConfigError("actions-commissioning-invalid")
    paths = (
        _path(document["state_database"], "actions-state-database"),
        _path(document["actions_database"], "actions-database"),
        _path(document["inbox_path"], "actions-inbox-path"),
        _path(document["backup_trigger_file"], "actions-backup-trigger-file"),
    )
    # Authority state lives only in the private root; evidence stays backed up.
    if paths != (
        ACTIONS_STATE_ROOT / "state.sqlite3",
        ACTIONS_PRIVATE_ROOT / "actions.sqlite3",
        ACTIONS_PRIVATE_ROOT / "telegram-inbox.sqlite3",
        ACTIONS_STATE_ROOT / "backup-expedited.trigger",
    ):
        raise RuntimeConfigError("actions-path-contract-invalid")
    if document["actor_target"] != ACTIONS_ACTOR_TARGET:
        raise RuntimeConfigError("actions-actor-target-invalid")
    if document["telegram_group_id"] != DEPLOYMENT_APPROVAL_GROUP_ID:
        raise RuntimeConfigError("actions-approval-group-invalid")
    bot = document["telegram_bot_username"]
    revision = document["policy_revision"]
    if not isinstance(bot, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot):
        raise RuntimeConfigError("actions-bot-username-invalid")
    if not isinstance(revision, str) or not ACTIONS_POLICY_REVISION.fullmatch(revision):
        raise RuntimeConfigError("actions-policy-revision-invalid")
    return ActionsEntrypointConfig(
        *paths,
        ACTIONS_ACTOR_TARGET,
        DEPLOYMENT_APPROVAL_GROUP_ID,
        bot,
        revision,
        _number(document["tick_seconds"], "actions-tick-seconds", 5, 120),
    )


def actions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terracompute-actions", allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--telegram-token-file", required=True, type=Path)
    parser.add_argument("--actor-identity-file", required=True, type=Path)
    parser.add_argument("--actor-known-hosts-file", required=True, type=Path)
    parser.add_argument("--ssh-executable", required=True, type=Path)
    parser.add_argument("--systemctl-executable", required=True, type=Path)
    return parser


def actions_main(argv: list[str] | None = None) -> int:
    """Run the approval-gated monitoring-restart service until stopped."""
    import signal
    import sqlite3
    import time

    from .action_service import (
        ActionService,
        CycleStore,
        InboxApprovalAuthenticator,
        SystemdBackupProbe,
    )
    from .actions import ActionBroker
    from .monitor_restart import (
        EvidenceStore,
        MonitorRestartAdapter,
        SSHActorClient,
        TelegramMembershipVerifier,
    )
    from .policy import ActionClass, ActionPolicy, Mode
    from .telegram import (
        SQLiteUpdateBackend,
        TelegramClient,
        TelegramUpdateConsumer,
        read_credential,
    )

    namespace = "terracompute-actions-telegram-v1"
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    try:
        args = actions_parser().parse_args(argv)
        config = load_actions_config(args.config)
        for value, field in (
            (args.telegram_token_file, "telegram-token-file"),
            (args.actor_identity_file, "actor-identity-file"),
            (args.actor_known_hosts_file, "actor-known-hosts-file"),
            (args.ssh_executable, "ssh-executable"),
            (args.systemctl_executable, "systemctl-executable"),
        ):
            _path(str(value), field)
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
        state_db = sqlite3.connect(config.state_database, timeout=10)
        state_db.execute("PRAGMA busy_timeout=10000")
        actions_db = sqlite3.connect(config.actions_database, timeout=10)
        actions_db.execute("PRAGMA busy_timeout=10000")
        backend = SQLiteUpdateBackend(config.inbox_path)
        client = TelegramClient(read_credential(args.telegram_token_file))
        consumer = TelegramUpdateConsumer(
            client, backend, group_id=config.telegram_group_id, namespace=namespace,
            enabled=True, bot_username=config.telegram_bot_username,
        )
        evidence = EvidenceStore(state_db, clock)
        holder: dict[str, ActionService] = {}
        adapter = MonitorRestartAdapter(
            SSHActorClient(
                ssh_binary=str(args.ssh_executable),
                target=config.actor_target,
                identity_file=args.actor_identity_file,
                known_hosts_file=args.actor_known_hosts_file,
            ),
            evidence,
            backup_ref=lambda proposal: holder["service"].backup_ref(proposal),
            preflight_ref=lambda proposal: holder["service"].preflight_ref(proposal),
            clock=clock,
        )
        broker = ActionBroker(
            actions_db,
            policy=ActionPolicy(
                mode=Mode.APPROVE,
                revision=config.policy_revision,
                enabled_actions=frozenset({ActionClass.MONITOR_COMPONENT_RESTART}),
                approval_group_id=config.telegram_group_id,
            ),
            membership=TelegramMembershipVerifier(client, clock),
            authenticator=InboxApprovalAuthenticator(backend, namespace),
            adapter=adapter,
            clock=clock,
        )
        service = ActionService(
            actions_db=actions_db, state_db=state_db, broker=broker, adapter=adapter,
            evidence=evidence, cycles=CycleStore(actions_db),
            backup=SystemdBackupProbe(
                trigger_file=config.backup_trigger_file,
                systemctl=str(args.systemctl_executable),
            ),
            telegram=client, consumer=consumer, backend=backend, namespace=namespace,
            group_id=config.telegram_group_id, policy_revision=config.policy_revision,
            clock=clock,
        )
        holder["service"] = service
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        service.recover()
        while not stopped:
            service.tick()
            time.sleep(config.tick_seconds)
        return 0
    except (RuntimeConfigError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(
            json.dumps(
                {
                    "machine_id": MACHINE_ID,
                    "operation": "actions",
                    "status": "failed",
                    "category": type(error).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
