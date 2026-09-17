"""Credential-free local backup and deterministic watchdog maintenance CLI.

The module performs no restic operation, retention change, deletion, remote
write, or notification delivery.  Its JSON output is deliberately small and
contains fixed status codes rather than exception text or configured paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .backup import (
    DEFAULT_MAX_FILES,
    BackupResult,
    ValidationResult,
    create_backup,
    validate_backup,
)
from .state import CURRENT_SCHEMA_VERSION, MACHINE_ID
from .http_client import HttpRequest, StdlibTransport
from .observation_runtime import (
    HEARTBEAT_CADENCE_SECONDS,
    HEARTBEAT_SCHEMA_VERSION,
    MAX_HEARTBEAT_BYTES,
)


WATCHDOG_STATE_VERSION = 2
WATCHDOG_CADENCE_SECONDS = int(HEARTBEAT_CADENCE_SECONDS)
MISSING_AFTER_SECONDS = 90
RECOVERY_STABLE_SECONDS = 5 * 60
PROGRESS_STALE_SECONDS = 90
MAX_WATCHDOG_STATE_BYTES = 32 * 1024
MAX_RETIRED_BOOT_IDS = 128
HTTP_TIMEOUT_SECONDS = 5.0
_BOOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEARTBEAT_FIELDS = frozenset(
    {
        "schema_version",
        "machine_id",
        "controller_boot_id",
        "sequence",
        "sent_at",
        "collection_progress_at",
        "notification_progress_at",
    }
)


@dataclass(frozen=True)
class WatchdogResult:
    status: str
    accepted: bool
    reason: str
    transition: str
    heartbeat_age_seconds: int | None
    collection_age_seconds: int | None
    notification_age_seconds: int | None
    alert_required: bool
    cadence_seconds: int = WATCHDOG_CADENCE_SECONDS
    missing_after_seconds: int = MISSING_AFTER_SECONDS
    recovery_stable_seconds: int = RECOVERY_STABLE_SECONDS
    machine_id: str = MACHINE_ID
    telegram_send: str = "disabled"


class MaintenanceError(ValueError):
    """A maintenance input failed its bounded, credential-free contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise MaintenanceError("timestamp must be timezone aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise MaintenanceError("timestamp is not bounded UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise MaintenanceError("timestamp is malformed") from exc
    return parsed.astimezone(timezone.utc)


def _bounded_age(now: datetime, value: datetime) -> int:
    return max(0, min(int((now - value).total_seconds()), 2_147_483_647))


def _approved_bundle_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT bundle_name FROM incidents ORDER BY bundle_name LIMIT ?",
        (DEFAULT_MAX_FILES + 1,),
    ).fetchall()
    if len(rows) > DEFAULT_MAX_FILES:
        raise MaintenanceError("approved incident bundle count exceeds backup bound")
    names = tuple(str(row[0]) for row in rows)
    if len(names) != len(set(names)):
        raise MaintenanceError("approved incident bundle names are not unique")
    return names


def create_local_snapshot(
    state_dir: Path,
    output_dir: Path,
    *,
    backup_id: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> BackupResult:
    """Snapshot a live ``StateStore`` and its database-approved incident bundles."""
    database = Path(state_dir).absolute() / "state.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise MaintenanceError("state database is unavailable")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise MaintenanceError("state schema requires controller migration")
        # Hold one WAL read snapshot across bundle selection and SQLite backup.
        # Backup must neither migrate state nor reconcile live incident records.
        connection.execute("BEGIN")
        bundles = _approved_bundle_names(connection)
        return create_backup(
            connection,
            Path(state_dir).absolute() / "incidents",
            bundles,
            Path(output_dir),
            machine_id=MACHINE_ID,
            backup_id=backup_id,
            clock=clock,
        )
    finally:
        connection.close()


def verify_local_snapshot(path: Path) -> ValidationResult:
    """Hash every artifact file and restore SQLite into isolated memory."""
    return validate_backup(Path(path))


def _read_json_file(path: Path, limit: int) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError("input must be a real regular file")
    if path.stat().st_size > limit:
        raise MaintenanceError("input exceeds size bound")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise MaintenanceError("input exceeds size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError("input is not JSON") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("input must be a JSON object")
    return value


def fetch_heartbeat(url: str, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> Mapping[str, object]:
    """Perform one bounded, credential-free GET from an explicitly configured URL."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or len(url) > 2048
        or not 0 < timeout <= 10
    ):
        raise MaintenanceError("heartbeat URL is not allowed")
    response = StdlibTransport().request(
        HttpRequest("GET", url, {"Accept": "application/json"}),
        timeout_seconds=timeout,
        max_response_bytes=MAX_HEARTBEAT_BYTES,
    )
    raw = response.body
    if len(raw) > MAX_HEARTBEAT_BYTES:
        raise MaintenanceError("heartbeat exceeds size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError("heartbeat is not JSON") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("heartbeat must be a JSON object")
    return value


def _initial_state() -> dict[str, object]:
    return {
        "state_version": WATCHDOG_STATE_VERSION,
        "machine_id": MACHINE_ID,
        "status": "unknown",
        "degraded": False,
        "last_boot_id": None,
        "last_sequence": None,
        "last_sent_at": None,
        "last_received_at": None,
        "startup_grace_started_at": None,
        "collection_progress_at": None,
        "notification_progress_at": None,
        "recovery_started_at": None,
        "retired_boot_ids": [],
    }


def _load_watchdog_state(path: Path, *, now: datetime) -> dict[str, object]:
    if not path.exists():
        return _initial_state()
    value = dict(_read_json_file(path, MAX_WATCHDOG_STATE_BYTES))
    required = set(_initial_state())
    legacy_required = required - {"startup_grace_started_at"}
    if value.get("state_version") == 1 and set(value) == legacy_required:
        if (
            value.get("machine_id") != MACHINE_ID
            or not isinstance(value.get("degraded"), bool)
            or not isinstance(value.get("retired_boot_ids"), list)
            or len(value["retired_boot_ids"]) > MAX_RETIRED_BOOT_IDS
        ):
            raise MaintenanceError("watchdog state is invalid")
        # Version 1 had no durable first-receipt grace.  Its file mtime is the
        # only local evidence of when that state began.  A future mtime cannot
        # safely grant a fresh grace, so it is migrated as already expired.
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError as error:
            raise MaintenanceError("watchdog state is invalid") from error
        if modified > now:
            modified = now - timedelta(seconds=MISSING_AFTER_SECONDS + 1)
        value["state_version"] = WATCHDOG_STATE_VERSION
        value["startup_grace_started_at"] = (
            _utc_text(modified) if value.get("last_received_at") is None else None
        )
        _persist_watchdog_state(path, value)
    if (
        set(value) != required
        or value.get("state_version") != WATCHDOG_STATE_VERSION
        or value.get("machine_id") != MACHINE_ID
        or not isinstance(value.get("degraded"), bool)
        or not isinstance(value.get("retired_boot_ids"), list)
        or len(value["retired_boot_ids"]) > MAX_RETIRED_BOOT_IDS
    ):
        raise MaintenanceError("watchdog state is invalid")
    return value


def _evaluate_no_new_heartbeat(
    state_path: Path,
    state: dict[str, object],
    *,
    current: datetime,
    previous_status: str,
) -> WatchdogResult:
    received_text = state.get("last_received_at")
    if received_text is None:
        grace_text = state.get("startup_grace_started_at")
        if grace_text is None:
            grace_started = current
            state["startup_grace_started_at"] = _utc_text(current)
        else:
            grace_started = _parse_utc(grace_text)
            if grace_started > current:
                raise MaintenanceError("watchdog state is invalid")
        age = _bounded_age(current, grace_started)
    else:
        age = _bounded_age(current, _parse_utc(received_text))
    collection_age = (
        None
        if state.get("collection_progress_at") is None
        else _bounded_age(current, _parse_utc(state["collection_progress_at"]))
    )
    notification_age = (
        None
        if state.get("notification_progress_at") is None
        else _bounded_age(current, _parse_utc(state["notification_progress_at"]))
    )
    if age > MISSING_AFTER_SECONDS:
        status = "missing"
        transition = "missing_started" if previous_status != "missing" else "unchanged"
        state.update(status=status, degraded=True, recovery_started_at=None)
        _persist_watchdog_state(state_path, state)
        return _result(
            status,
            False,
            "heartbeat_missing",
            transition,
            age,
            collection_age,
            notification_age,
        )
    if received_text is None and previous_status in {"unknown", "starting"}:
        state.update(status="starting", degraded=False, recovery_started_at=None)
        _persist_watchdog_state(state_path, state)
        return _result(
            "starting",
            False,
            "startup_grace",
            "unchanged",
            age,
            None,
            None,
        )
    if received_text is None:
        _persist_watchdog_state(state_path, state)
    return _result(
        previous_status,
        False,
        "no_new_heartbeat",
        "unchanged",
        age,
        collection_age,
        notification_age,
    )


def _persist_watchdog_state(path: Path, state: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise MaintenanceError("watchdog state path is not a real file")
    content = json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"
    if len(content) > MAX_WATCHDOG_STATE_BYTES:
        raise MaintenanceError("watchdog state exceeds size bound")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_descriptor = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _result(
    status: str,
    accepted: bool,
    reason: str,
    transition: str,
    heartbeat_age: int | None,
    collection_age: int | None,
    notification_age: int | None,
) -> WatchdogResult:
    return WatchdogResult(
        status,
        accepted,
        reason,
        transition,
        heartbeat_age,
        collection_age,
        notification_age,
        status in {"invalid", "missing", "stale_work"},
    )


def evaluate_watchdog(
    state_path: Path,
    heartbeat: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
    notification_progress_required: bool = True,
) -> WatchdogResult:
    """Evaluate one 30-second tick and persist replay/recovery state atomically.

    Collection progress is always required. Notification progress is required unless
    delivery is deliberately disabled; otherwise its expected staleness would keep the
    watchdog failing and hide a real collection stall.
    """
    if not isinstance(notification_progress_required, bool):
        raise MaintenanceError("notification progress requirement must be boolean")
    current = (now or _utc_now()).astimezone(timezone.utc)
    state_path = Path(state_path)
    state = _load_watchdog_state(state_path, now=current)
    previous_status = str(state["status"])

    if heartbeat is None:
        return _evaluate_no_new_heartbeat(
            state_path,
            state,
            current=current,
            previous_status=previous_status,
        )

    rejection = ""
    sent: datetime | None = None
    collection: datetime | None = None
    notification: datetime | None = None
    boot_id = heartbeat.get("controller_boot_id")
    sequence = heartbeat.get("sequence")
    try:
        if set(heartbeat) != _HEARTBEAT_FIELDS:
            raise MaintenanceError("heartbeat fields are invalid")
        if heartbeat.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
            rejection = "schema_invalid"
        elif heartbeat.get("machine_id") != MACHINE_ID:
            rejection = "wrong_target"
        elif not isinstance(boot_id, str) or not _BOOT_ID.fullmatch(boot_id):
            rejection = "boot_id_invalid"
        elif (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence <= 9_223_372_036_854_775_807
        ):
            rejection = "sequence_invalid"
        else:
            sent = _parse_utc(heartbeat.get("sent_at"))
            collection = _parse_utc(heartbeat.get("collection_progress_at"))
            notification = _parse_utc(heartbeat.get("notification_progress_at"))
    except MaintenanceError:
        rejection = rejection or "malformed"

    unchanged_snapshot = False
    if not rejection and sent is not None and collection is not None and notification is not None:
        last_boot = state.get("last_boot_id")
        last_sequence = state.get("last_sequence")
        last_sent = state.get("last_sent_at")
        retired = state["retired_boot_ids"]
        assert isinstance(retired, list)
        if sent > current or collection > current or notification > current:
            rejection = "future_timestamp"
        elif collection > sent or notification > sent:
            rejection = "progress_after_send"
        elif boot_id in retired:
            rejection = "retired_boot_replay"
        elif (
            boot_id == last_boot
            and isinstance(last_sequence, int)
            and sequence == last_sequence
            and last_sent is not None
            and sent == _parse_utc(last_sent)
        ):
            unchanged_snapshot = True
        elif (current - sent).total_seconds() > MISSING_AFTER_SECONDS:
            rejection = "stale_heartbeat"
        else:
            if boot_id == last_boot and (
                not isinstance(last_sequence, int)
                or sequence <= last_sequence
                or (last_sent is not None and sent <= _parse_utc(last_sent))
            ):
                rejection = "sequence_replay"
            elif (
                boot_id != last_boot
                and last_boot is not None
                and len(retired) >= MAX_RETIRED_BOOT_IDS
            ):
                rejection = "boot_history_full"

    if unchanged_snapshot:
        return _evaluate_no_new_heartbeat(
            state_path,
            state,
            current=current,
            previous_status=previous_status,
        )

    if rejection:
        state.update(status="invalid", degraded=True, recovery_started_at=None)
        _persist_watchdog_state(state_path, state)
        transition = "invalid_started" if previous_status != "invalid" else "unchanged"
        return _result("invalid", False, rejection, transition, None, None, None)

    assert sent is not None and collection is not None and notification is not None
    assert isinstance(boot_id, str) and isinstance(sequence, int)
    retired = list(state["retired_boot_ids"])
    last_boot = state.get("last_boot_id")
    if last_boot is not None and boot_id != last_boot:
        retired.append(str(last_boot))
    collection_age = _bounded_age(current, collection)
    notification_age = _bounded_age(current, notification)
    heartbeat_age = _bounded_age(current, sent)
    last_received = state.get("last_received_at")
    receipt_gap = (
        last_received is not None
        and (current - _parse_utc(last_received)).total_seconds() > MISSING_AFTER_SECONDS
    )
    if receipt_gap:
        state.update(degraded=True, recovery_started_at=None)
    state.update(
        last_boot_id=boot_id,
        last_sequence=sequence,
        last_sent_at=_utc_text(sent),
        last_received_at=_utc_text(current),
        startup_grace_started_at=None,
        collection_progress_at=_utc_text(collection),
        notification_progress_at=_utc_text(notification),
        retired_boot_ids=retired,
    )

    notification_stale = (
        notification_progress_required and notification_age > PROGRESS_STALE_SECONDS
    )
    if collection_age > PROGRESS_STALE_SECONDS or notification_stale:
        status = "stale_work"
        transition = "stale_work_started" if previous_status != status else "unchanged"
        state.update(status=status, degraded=True, recovery_started_at=None)
        reason = "progress_stale"
    elif bool(state["degraded"]):
        recovery_text = state.get("recovery_started_at")
        if recovery_text is None:
            state["recovery_started_at"] = _utc_text(current)
            status = "recovering"
            transition = "recovery_started"
        elif (current - _parse_utc(recovery_text)).total_seconds() >= RECOVERY_STABLE_SECONDS:
            state.update(status="healthy", degraded=False, recovery_started_at=None)
            status = "healthy"
            transition = "recovered"
        else:
            status = "recovering"
            transition = "unchanged"
        state["status"] = status
        reason = "stable_recovery_pending" if status == "recovering" else "heartbeat_healthy"
    else:
        status = "healthy"
        transition = "healthy_started" if previous_status != status else "unchanged"
        state.update(status=status, degraded=False, recovery_started_at=None)
        reason = "heartbeat_healthy"

    _persist_watchdog_state(state_path, state)
    return _result(
        status,
        True,
        reason,
        transition,
        heartbeat_age,
        collection_age,
        notification_age,
    )


def _print_json(value: Mapping[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _snapshot_metadata(result: BackupResult) -> dict[str, object]:
    return {
        "operation": "snapshot",
        "status": "ok" if result.success else "failed",
        "scope": "LOCAL",
        "machine_id": MACHINE_ID,
        "backup_id": result.backup_id if result.success else None,
        "file_count": result.file_count,
        "total_bytes": result.total_bytes,
        "snapshot_sha256": result.snapshot_sha256,
        "manifest_sha256": result.manifest_sha256,
        "verification": result.verification,
        "restic_transfer": "not_configured",
    }


def _verify_metadata(result: ValidationResult) -> dict[str, object]:
    return {
        "operation": "verify",
        "status": "ok" if result.valid else "failed",
        "scope": "LOCAL",
        "machine_id": MACHINE_ID,
        "file_count": result.file_count,
        "total_bytes": result.total_bytes,
        "snapshot_sha256": result.snapshot_sha256,
        "manifest_sha256": result.manifest_sha256,
        "verification": "isolated_restore_and_hashes_ok" if result.valid else "failed",
        "restic_transfer": "not_configured",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m terracompute_ops.maintenance")
    commands = result.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="publish one verified local snapshot")
    snapshot.add_argument("--state-dir", required=True, type=Path)
    snapshot.add_argument("--output-dir", required=True, type=Path)
    snapshot.add_argument("--backup-id")
    verify = commands.add_parser("verify", help="verify and isolate-restore a local snapshot")
    verify.add_argument("--artifact", required=True, type=Path)
    watchdog = commands.add_parser("watchdog", help="evaluate one persistent heartbeat tick")
    watchdog.add_argument("--state", required=True, type=Path)
    source = watchdog.add_mutually_exclusive_group()
    source.add_argument("--heartbeat-file", type=Path)
    source.add_argument("--heartbeat-url")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            result = create_local_snapshot(
                args.state_dir, args.output_dir, backup_id=args.backup_id
            )
            _print_json(_snapshot_metadata(result))
            return 0 if result.success else 1
        if args.command == "verify":
            result = verify_local_snapshot(args.artifact)
            _print_json(_verify_metadata(result))
            return 0 if result.valid else 1
        heartbeat = None
        if args.heartbeat_file is not None:
            heartbeat = _read_json_file(args.heartbeat_file, MAX_HEARTBEAT_BYTES)
        elif args.heartbeat_url is not None:
            heartbeat = fetch_heartbeat(args.heartbeat_url)
        result = evaluate_watchdog(args.state, heartbeat)
        _print_json(asdict(result))
        return 0 if result.status in {"starting", "healthy", "recovering"} else 2
    except Exception:
        # This is the process boundary: backend and transport exceptions can
        # carry configured URLs or paths, so expose only a fixed failure code.
        _print_json(
            {
                "operation": args.command,
                "status": "failed",
                "machine_id": MACHINE_ID,
                "reason": "maintenance_error",
                "restic_transfer": "not_configured",
                "telegram_send": "disabled",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
