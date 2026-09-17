"""Bounded local persistence primitives for the observation processes.

This module owns only namespaced tables and local files.  It has no network,
notification, model, actuator, retention, or deletion capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .backup import DiskAdmission, assess_disk_admission
from .incidents import bounded_evidence


MACHINE_ID = "17049"
MAX_SOURCE_ARTIFACT_BYTES = 512 * 1024
SQLITE_ARTIFACT_OVERHEAD_BYTES = 16 * 1024
MAX_ACCOUNTING_FILES = 16_384
ACCOUNTING_CACHE_SECONDS = 30.0
# The backup role owns this private 0700 child of the shared state root
# (nix/nixos-module.nix backupRoot). Observation processes cannot read it, and its
# bytes belong to the backup role's own disk admission. Free space still counts them.
BACKUP_SNAPSHOT_DIRNAME = "backups"
HEARTBEAT_SCHEMA_VERSION = 1
HEARTBEAT_CADENCE_SECONDS = 30.0
HEARTBEAT_FILENAME = "controller-heartbeat.json"
MAX_HEARTBEAT_BYTES = 8 * 1024
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ObservationRuntimeError(ValueError):
    """A fixed-category local observation persistence failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StorageMeasurement:
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class ArtifactResult:
    sha256: str
    document: dict[str, object]
    saved: bool
    idempotent: bool
    admission: DiskAdmission


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ObservationRuntimeError("invalid_clock")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_document(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ObservationRuntimeError("source_document_malformed") from error


def _source_time(probe: Mapping[str, object], fallback: datetime) -> str:
    value = probe.get("source_timestamp", probe.get("observed_at"))
    if value is None:
        return _utc_text(fallback)
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ObservationRuntimeError("source_document_malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ObservationRuntimeError("source_document_malformed") from error
    return _utc_text(parsed)


class LocalStorageAccounting:
    """Boundedly measure state-root bytes and cache the result for one cadence."""

    def __init__(
        self,
        root: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        cache_seconds: float = ACCOUNTING_CACHE_SECONDS,
        max_files: int = MAX_ACCOUNTING_FILES,
    ):
        self.root = Path(root)
        self._monotonic = monotonic
        self._disk_usage = disk_usage
        self._cache_seconds = cache_seconds
        self._max_files = max_files
        self._cached: StorageMeasurement | None = None
        self._cached_at = float("-inf")

    def measure(self) -> StorageMeasurement:
        now = self._monotonic()
        if self._cached is not None and now - self._cached_at < self._cache_seconds:
            return self._cached
        try:
            used = 0
            entries_seen = 0
            pending = [self.root]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > self._max_files:
                            raise ObservationRuntimeError("storage_accounting_limit")
                        status = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(status.st_mode):
                            continue
                        if stat.S_ISDIR(status.st_mode):
                            if directory == self.root and entry.name == BACKUP_SNAPSHOT_DIRNAME:
                                continue
                            pending.append(Path(entry.path))
                            continue
                        if stat.S_ISREG(status.st_mode):
                            used += status.st_size
            usage = self._disk_usage(self.root)
            free = getattr(usage, "free")
            if not isinstance(free, int) or free < 0:
                raise ValueError
        except ObservationRuntimeError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise ObservationRuntimeError("storage_accounting_unavailable") from error
        self._cached = StorageMeasurement(used, free)
        self._cached_at = now
        return self._cached

    def note_written(self, byte_count: int) -> None:
        if self._cached is not None and isinstance(byte_count, int) and byte_count >= 0:
            self._cached = StorageMeasurement(
                self._cached.used_bytes + byte_count,
                max(0, self._cached.free_bytes - byte_count),
            )


class ObservationArchive:
    """Preserve complete sanitized source documents in a namespaced SQLite table."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        state_root: Path,
        *,
        accounting: LocalStorageAccounting | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.state_root = Path(state_root)
        self.accounting = accounting or LocalStorageAccounting(self.state_root)
        self.clock = clock
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS terracompute_observation_archive_schema (
              schema_version INTEGER NOT NULL CHECK(schema_version = 1)
            );
            INSERT INTO terracompute_observation_archive_schema(schema_version)
              SELECT 1 WHERE NOT EXISTS (
                SELECT 1 FROM terracompute_observation_archive_schema
              );
            CREATE TABLE IF NOT EXISTS terracompute_observation_artifacts (
              artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id TEXT NOT NULL CHECK(machine_id = '17049'),
              source TEXT NOT NULL,
              observed_utc TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              document_json BLOB NOT NULL,
              capture_class TEXT NOT NULL CHECK(capture_class IN ('routine','protected')),
              UNIQUE(machine_id, source, sha256)
            );
            CREATE INDEX IF NOT EXISTS terracompute_observation_artifacts_time
              ON terracompute_observation_artifacts(observed_utc, source);
            """
        )
        version = self.connection.execute(
            "SELECT schema_version FROM terracompute_observation_archive_schema"
        ).fetchone()
        if version is None or int(version[0]) != 1:
            raise ObservationRuntimeError("archive_schema_invalid")
        self.connection.commit()

    def archive(
        self,
        probe: Mapping[str, object],
        *,
        capture_class: str = "routine",
    ) -> ArtifactResult:
        if not isinstance(probe, Mapping):
            raise ObservationRuntimeError("source_document_malformed")
        if str(probe.get("machine_id", "")) != MACHINE_ID:
            raise ObservationRuntimeError("source_identity_mismatch")
        source = probe.get("source")
        if not isinstance(source, str) or not _SOURCE.fullmatch(source):
            raise ObservationRuntimeError("source_identity_malformed")
        if capture_class not in {"routine", "protected"}:
            raise ObservationRuntimeError("capture_class_invalid")

        try:
            raw = _canonical_document(dict(probe))
            if len(raw) > MAX_SOURCE_ARTIFACT_BYTES:
                raise ObservationRuntimeError("source_document_oversize")
            # JSON normalization preserves dataclass tuple fields as arrays.
            # The total bound replaces incident-preview field truncation here.
            sanitized = bounded_evidence(
                json.loads(raw), MAX_SOURCE_ARTIFACT_BYTES, preserve_fields=True,
            )
        except ObservationRuntimeError:
            raise
        except (TypeError, ValueError, RecursionError) as error:
            raise ObservationRuntimeError("source_document_malformed") from error
        if not isinstance(sanitized, dict):
            raise ObservationRuntimeError("source_document_malformed")
        encoded = _canonical_document(sanitized)
        if len(encoded) > MAX_SOURCE_ARTIFACT_BYTES or sanitized.get("truncated") is True:
            raise ObservationRuntimeError("source_document_oversize")
        digest = hashlib.sha256(encoded).hexdigest()
        duplicate = self.connection.execute(
            """SELECT 1 FROM terracompute_observation_artifacts
               WHERE machine_id=? AND source=? AND sha256=?""",
            (MACHINE_ID, source, digest),
        ).fetchone()
        if duplicate is not None:
            measurement = self.accounting.measure()
            admission = assess_disk_admission(
                measurement.used_bytes,
                measurement.free_bytes,
                0,
                capture_class=capture_class,
            )
            return ArtifactResult(digest, sanitized, True, True, admission)

        measurement = self.accounting.measure()
        planned_write = len(encoded) + SQLITE_ARTIFACT_OVERHEAD_BYTES
        admission = assess_disk_admission(
            measurement.used_bytes,
            measurement.free_bytes,
            planned_write,
            capture_class=capture_class,
        )
        if not admission.admitted:
            return ArtifactResult(digest, sanitized, False, False, admission)
        observed = _source_time(sanitized, self.clock())
        try:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO terracompute_observation_artifacts(
                     machine_id,source,observed_utc,sha256,document_json,capture_class)
                   VALUES(?,?,?,?,?,?)""",
                (MACHINE_ID, source, observed, digest, encoded, capture_class),
            )
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ObservationRuntimeError("source_archive_unavailable") from error
        inserted = cursor.rowcount == 1
        if inserted:
            self.accounting.note_written(planned_write)
        return ArtifactResult(digest, sanitized, True, not inserted, admission)


class RuntimeProgress:
    """Share actual collection/notifier progress through a namespaced state table."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.clock = clock
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS terracompute_runtime_progress (
                 machine_id TEXT PRIMARY KEY CHECK(machine_id = '17049'),
                 heartbeat_sequence INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_sequence >= 0),
                 collection_progress_at TEXT,
                 notification_progress_at TEXT
               )"""
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO terracompute_runtime_progress(
                 machine_id,heartbeat_sequence) VALUES(?,0)""",
            (MACHINE_ID,),
        )
        self.connection.commit()

    def record_collection(self, completed_at: datetime | None = None) -> None:
        self._record("collection_progress_at", completed_at)

    def record_notification(self, completed_at: datetime | None = None) -> None:
        self._record("notification_progress_at", completed_at)

    def _record(self, column: str, completed_at: datetime | None) -> None:
        value = _utc_text(completed_at or self.clock())
        try:
            self.connection.execute(
                f"""UPDATE terracompute_runtime_progress SET {column}=?
                    WHERE machine_id=? AND ({column} IS NULL OR {column} < ?)""",
                (value, MACHINE_ID, value),
            )
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ObservationRuntimeError("progress_persistence_unavailable") from error

    def next_heartbeat(self) -> tuple[int, str, str] | None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT heartbeat_sequence,collection_progress_at,
                          notification_progress_at
                   FROM terracompute_runtime_progress WHERE machine_id=?""",
                (MACHINE_ID,),
            ).fetchone()
            if row is None or row[1] is None or row[2] is None:
                self.connection.rollback()
                return None
            sequence = int(row[0]) + 1
            if sequence > 9_223_372_036_854_775_807:
                self.connection.rollback()
                raise ObservationRuntimeError("heartbeat_sequence_exhausted")
            self.connection.execute(
                """UPDATE terracompute_runtime_progress SET heartbeat_sequence=?
                   WHERE machine_id=?""",
                (sequence, MACHINE_ID),
            )
            self.connection.commit()
            return sequence, str(row[1]), str(row[2])
        except ObservationRuntimeError:
            raise
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ObservationRuntimeError("progress_persistence_unavailable") from error


class HeartbeatPublisher:
    """Atomically publish the local maintenance heartbeat at a fixed cadence."""

    def __init__(
        self,
        root: Path,
        progress: RuntimeProgress,
        *,
        boot_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.root = Path(root)
        self.path = self.root / HEARTBEAT_FILENAME
        self.progress = progress
        self.boot_id = boot_id or uuid.uuid4().hex
        if not _SOURCE.fullmatch(self.boot_id):
            raise ObservationRuntimeError("heartbeat_boot_id_invalid")
        self.clock = clock
        self.monotonic = monotonic
        self._next_publish = 0.0

    def publish_if_due(self, *, force: bool = False) -> dict[str, object] | None:
        now_monotonic = self.monotonic()
        if not force and now_monotonic < self._next_publish:
            return None
        self._next_publish = now_monotonic + HEARTBEAT_CADENCE_SECONDS
        progress = self.progress.next_heartbeat()
        if progress is None:
            return None
        sequence, collection, notification = progress
        document: dict[str, object] = {
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "machine_id": MACHINE_ID,
            "controller_boot_id": self.boot_id,
            "sequence": sequence,
            "sent_at": _utc_text(self.clock()),
            "collection_progress_at": collection,
            "notification_progress_at": notification,
        }
        content = _canonical_document(document) + b"\n"
        if len(content) > MAX_HEARTBEAT_BYTES:
            raise ObservationRuntimeError("heartbeat_oversize")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ObservationRuntimeError("heartbeat_path_invalid")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".controller-heartbeat-", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            # The credential-free heartbeat is consumed by the separately
            # sandboxed watchdog through the state root's setgid group.
            os.fchmod(descriptor, 0o640)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            parent_descriptor = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise ObservationRuntimeError("heartbeat_publish_unavailable") from error
        return document
