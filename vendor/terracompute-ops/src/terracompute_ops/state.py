"""Versioned SQLite history, immutable incident bundles, and durable outbox."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .incidents import MAX_MODEL_REQUEST_BYTES, bounded_evidence, canonical_json

CURRENT_SCHEMA_VERSION = 3
TARGET = "terracompute"
LEGACY_TARGET = "vast-machine-17049"
MACHINE_ID = "17049"
RECOVERY_WINDOW_SECONDS = 5 * 60
REMINDER_INTERVAL_SECONDS = 15 * 60
MAX_REMINDERS = 3
MAX_HEALTHY_GAP_SECONDS = 330
DEFAULT_HEALTHY_GAP_SECONDS = 90
DEFAULT_HEALTHY_GAPS = {"target-probe": 330, "ssh": 330}
MAX_HISTORY_LIMIT = 1000
MAX_ORPHAN_BUNDLES = 10_000
MAX_BUNDLE_BYTES = 256 * 1024
MAX_BUNDLE_FILES = 16
_BUNDLE_FILE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")


@dataclass(frozen=True)
class ObservationWriteResult:
    """Result exposed only after an observation transaction commits.

    Iteration deliberately retains the historical ``(bundle, duplicate)``
    unpacking contract while making material lifecycle changes explicit.
    """

    bundle: Path | None
    duplicate: bool
    material_changed: bool = False
    observation_id: int | None = None
    current: bool = False

    def __iter__(self) -> Iterator[Path | bool | None]:
        yield self.bundle
        yield self.duplicate


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _ensure_shared_sqlite_mode(path: Path) -> None:
    """Keep the shared SQLite files writable by the fixed service group."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("unsafe shared SQLite file")
    if stat.S_IMODE(metadata.st_mode) != 0o660:
        try:
            os.chmod(path, 0o660)
        except PermissionError:
            # A peer service may own the file. It is acceptable only when that
            # owner already installed the exact shared-service mode.
            if stat.S_IMODE(path.lstat().st_mode) != 0o660:
                raise


class StateStore:
    """Own the versioned state index and immutable incident evidence.

    Construction migrates baseline databases transactionally, refuses schemas
    newer than this binary, and reconciles verified crash-orphaned bundles. The
    public baseline outbox methods remain compatible with CLI and Telegram code.
    """

    def __init__(
        self,
        root: Path,
        clock: Callable[[], datetime] = utc_now,
        healthy_gap_seconds: int | dict[str, int] | None = None,
    ):
        self.root = root
        self.incident_root = root / "incidents"
        self.clock = clock
        self._healthy_gaps = self._validated_healthy_gaps(healthy_gap_seconds)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = root / "state.sqlite3"
        existed = self.db_path.exists() and self.db_path.stat().st_size > 0
        self.db = sqlite3.connect(self.db_path)
        _ensure_shared_sqlite_mode(self.db_path)
        self.db.row_factory = sqlite3.Row
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_SCHEMA_VERSION:
            self.db.close()
            raise RuntimeError(
                f"state schema {version} is newer than supported schema "
                f"{CURRENT_SCHEMA_VERSION}"
            )
        self.incident_root.mkdir(mode=0o700, exist_ok=True)
        if existed and version < CURRENT_SCHEMA_VERSION and self._has_user_tables():
            self._write_migration_snapshot(version)
        self._migrate(version)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        for suffix in ("-wal", "-shm"):
            shared_file = Path(f"{self.db_path}{suffix}")
            if shared_file.exists():
                _ensure_shared_sqlite_mode(shared_file)
        self.db.execute("PRAGMA synchronous=FULL")
        self.recovery_report = self.recover_orphan_bundles()

    def _has_user_tables(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone() is not None

    def _write_migration_snapshot(self, version: int) -> None:
        """Create a consistent, non-overwriting pre-migration rollback database."""
        stamp = utc_text(self.clock()).replace(":", "").replace("-", "").replace(".", "")
        base = self.root / f"state.sqlite3.pre-migration-v{version}-{stamp}"
        destination = base
        suffix = 0
        while destination.exists():
            suffix += 1
            destination = Path(f"{base}-{suffix}")
        snapshot = sqlite3.connect(destination)
        try:
            self.db.backup(snapshot)
            snapshot.commit()
        finally:
            snapshot.close()
        os.chmod(destination, 0o600)

    def _migrate(self, version: int) -> None:
        migrations = (self._migrate_0_to_1, self._migrate_1_to_2, self._migrate_2_to_3)
        while version < CURRENT_SCHEMA_VERSION:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                migrations[version]()
                version += 1
                self.db.execute(f"PRAGMA user_version={version}")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def _migrate_0_to_1(self) -> None:
        """Install the imported baseline schema without changing its API."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS incidents (
                 dedup_key TEXT PRIMARY KEY,
                 bundle_name TEXT NOT NULL,
                 created_utc TEXT NOT NULL
               )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS outbox (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 dedup_key TEXT NOT NULL UNIQUE,
                 message TEXT NOT NULL,
                 state TEXT NOT NULL DEFAULT 'pending',
                 attempts INTEGER NOT NULL DEFAULT 0,
                 next_attempt_utc TEXT NOT NULL,
                 last_error TEXT,
                 sent_utc TEXT,
                 FOREIGN KEY(dedup_key) REFERENCES incidents(dedup_key)
               )"""
        )
        required = {
            "incidents": {"dedup_key", "bundle_name", "created_utc"},
            "outbox": {"id", "dedup_key", "message", "state", "attempts"},
        }
        for table, columns in required.items():
            actual = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise RuntimeError(f"unrecognized version-0 {table} schema")

    def _add_column(self, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_1_to_2(self) -> None:
        incident_columns = (
            "target TEXT NOT NULL DEFAULT 'vast-machine-17049'",
            "machine_id TEXT NOT NULL DEFAULT '17049'",
            "source TEXT NOT NULL DEFAULT 'legacy'",
            "fault_family TEXT NOT NULL DEFAULT 'unknown'",
            "stable_signature TEXT NOT NULL DEFAULT ''",
            "severity TEXT NOT NULL DEFAULT 'warning'",
            "status TEXT NOT NULL DEFAULT 'open'",
            "first_occurrence_utc TEXT",
            "last_occurrence_utc TEXT",
            "last_boot_id TEXT",
            "occurrence_count INTEGER NOT NULL DEFAULT 1",
            "recovery_started_utc TEXT",
            "recovered_utc TEXT",
        )
        for definition in incident_columns:
            self._add_column("incidents", definition)
        self.db.execute(
            """UPDATE incidents SET first_occurrence_utc=created_utc,
                       last_occurrence_utc=created_utc
               WHERE first_occurrence_utc IS NULL OR last_occurrence_utc IS NULL"""
        )
        self._add_column("outbox", "severity TEXT NOT NULL DEFAULT 'warning'")
        self._add_column("outbox", "silent INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            """CREATE TABLE observations (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 target TEXT NOT NULL,
                 machine_id TEXT NOT NULL,
                 source TEXT NOT NULL,
                 source_event_id TEXT,
                 delivery_key TEXT UNIQUE,
                 source_utc TEXT NOT NULL,
                 receipt_utc TEXT NOT NULL,
                 boot_id TEXT NOT NULL,
                 status TEXT NOT NULL CHECK(status IN ('healthy','unhealthy','unknown','stale')),
                 freshness TEXT NOT NULL,
                 ordering TEXT NOT NULL DEFAULT 'current',
                 evidence_sha256 TEXT NOT NULL,
                 evidence_json BLOB NOT NULL,
                 incident_key TEXT,
                 FOREIGN KEY(incident_key) REFERENCES incidents(dedup_key)
               )"""
        )
        self.db.execute(
            """CREATE TABLE transitions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 incident_key TEXT NOT NULL,
                 observation_id INTEGER NOT NULL,
                 transition TEXT NOT NULL,
                 occurred_utc TEXT NOT NULL,
                 boot_id TEXT NOT NULL,
                 FOREIGN KEY(incident_key) REFERENCES incidents(dedup_key),
                 FOREIGN KEY(observation_id) REFERENCES observations(id),
                 UNIQUE(incident_key, observation_id, transition)
               )"""
        )
        self.db.execute(
            """CREATE TABLE source_state (
                 target TEXT NOT NULL,
                 source TEXT NOT NULL,
                 last_source_utc TEXT NOT NULL,
                 last_receipt_utc TEXT NOT NULL,
                 status TEXT NOT NULL,
                 boot_id TEXT NOT NULL,
                 PRIMARY KEY(target, source)
               )"""
        )
        self.db.execute("CREATE INDEX observations_incident_id ON observations(incident_key,id)")
        self.db.execute("CREATE INDEX transitions_incident_id ON transitions(incident_key,id)")

    def _migrate_2_to_3(self) -> None:
        """Separate incident identity from event-level outbox idempotency."""
        for definition in (
            "recovery_last_healthy_utc TEXT",
            "acknowledged_utc TEXT",
            "notification_episode INTEGER NOT NULL DEFAULT 1",
        ):
            self._add_column("incidents", definition)
        self.db.execute(
            """CREATE TABLE outbox_v3 (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 incident_key TEXT NOT NULL,
                 event_key TEXT NOT NULL UNIQUE,
                 event_type TEXT NOT NULL,
                 episode INTEGER NOT NULL DEFAULT 1,
                 reminder_number INTEGER NOT NULL DEFAULT 0,
                 message TEXT NOT NULL,
                 state TEXT NOT NULL DEFAULT 'pending',
                 attempts INTEGER NOT NULL DEFAULT 0,
                 next_attempt_utc TEXT NOT NULL,
                 last_error TEXT,
                 sent_utc TEXT,
                 severity TEXT NOT NULL DEFAULT 'warning',
                 silent INTEGER NOT NULL DEFAULT 1,
                 FOREIGN KEY(incident_key) REFERENCES incidents(dedup_key)
               )"""
        )
        self.db.execute(
            """INSERT INTO outbox_v3(
                 id,incident_key,event_key,event_type,episode,reminder_number,
                 message,state,attempts,next_attempt_utc,last_error,sent_utc,severity,silent)
               SELECT id,dedup_key,'legacy-opened:' || dedup_key,'opened',1,0,
                      message,state,attempts,next_attempt_utc,last_error,sent_utc,severity,silent
               FROM outbox"""
        )
        self.db.execute("DROP TABLE outbox")
        self.db.execute("ALTER TABLE outbox_v3 RENAME TO outbox")
        self.db.execute("CREATE INDEX outbox_incident_id ON outbox(incident_key,id)")
        self.db.execute("CREATE INDEX outbox_due ON outbox(state,next_attempt_utc,id)")

    @staticmethod
    def _validated_healthy_gaps(
        configured: int | dict[str, int] | None,
    ) -> dict[str, int]:
        gaps = {"*": DEFAULT_HEALTHY_GAP_SECONDS, **DEFAULT_HEALTHY_GAPS}
        values = (
            {source: configured for source in gaps}
            if isinstance(configured, int) else configured
        )
        if values is None:
            return gaps
        if not isinstance(values, dict):
            raise ValueError("healthy_gap_seconds must be an integer or source mapping")
        for raw_source, raw_seconds in values.items():
            source = str(raw_source).strip().lower()
            if (source != "*" and (not source or len(source) > 128)) or isinstance(
                raw_seconds, bool
            ):
                raise ValueError("healthy gap source or value is invalid")
            seconds = int(raw_seconds)
            if seconds < 1 or seconds > MAX_HEALTHY_GAP_SECONDS:
                raise ValueError(
                    f"healthy gaps must be between 1 and {MAX_HEALTHY_GAP_SECONDS} seconds"
                )
            gaps[source] = seconds
        return gaps

    def _healthy_gap_for(self, source: str) -> int:
        return self._healthy_gaps.get(source.lower(), self._healthy_gaps["*"])

    def close(self) -> None:
        self.db.close()

    def has_incident(self, key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM incidents WHERE dedup_key=?", (key,)
        ).fetchone() is not None

    def _bundle_files(
        self,
        incident: dict[str, Any],
        evidence: Any,
        model_request: dict[str, Any] | None,
        recovery: dict[str, Any],
    ) -> dict[str, bytes]:
        files = {
            "incident.json": canonical_json(bounded_evidence(incident)) + b"\n",
            "evidence.json": canonical_json(bounded_evidence(evidence)),
            "recovery.json": canonical_json(bounded_evidence(recovery)),
        }
        if model_request is not None:
            files["model-analysis-request.json"] = canonical_json(
                bounded_evidence(model_request, MAX_MODEL_REQUEST_BYTES)
            )
        return files

    def _publish_bundle(self, bundle_name: str, files: dict[str, bytes]) -> Path:
        temp = Path(tempfile.mkdtemp(prefix=".incident-", dir=self.incident_root))
        final = self.incident_root / bundle_name
        try:
            for name, content in files.items():
                path = temp / name
                with path.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, 0o440)
            manifest = b"".join(
                f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode("ascii")
                for name, content in sorted(files.items())
            )
            manifest_path = temp / "manifest.sha256"
            with manifest_path.open("xb") as handle:
                handle.write(manifest)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(manifest_path, 0o440)
            directory_fd = os.open(temp, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.chmod(temp, 0o550)
            os.replace(temp, final)
            parent_fd = os.open(self.incident_root, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return final
        except Exception:
            if temp.exists():
                os.chmod(temp, 0o700)
                for child in temp.iterdir():
                    if child.is_file() and not child.is_symlink():
                        os.chmod(child, 0o600)
                shutil.rmtree(temp)
            raise

    def create_incident(
        self,
        key: str,
        incident: dict[str, Any],
        evidence: Any,
        notification: str,
        model_request: dict[str, Any] | None,
        severity: str = "warning",
        silent: bool | None = None,
    ) -> Path | None:
        """Publish one baseline-compatible incident and enqueue one notification."""
        incident_target = str(incident.get("target", ""))
        if (
            not incident_target or len(incident_target) > 255
            or str(incident.get("machine_id", "")) != MACHINE_ID
        ):
            raise ValueError("incidents are scoped only to Vast machine 17049")
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("incident key must be a SHA-256 digest")
        created = utc_text(self.clock())
        stamp = created.replace(":", "").replace("-", "").replace(".", "")
        bundle_name = f"{stamp}_{key[:16]}"
        recovery = {
            "schema_version": 1,
            "kind": "legacy-incident",
            "key": key,
            "bundle_name": bundle_name,
            "created_utc": created,
            "target": incident_target,
            "machine_id": MACHINE_ID,
            "notification": notification[:3500],
            "severity": severity,
            "silent": self._notification_silent(severity, silent),
        }
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO incidents(dedup_key,bundle_name,created_utc) VALUES(?,?,?)",
                (key, bundle_name, created),
            )
        except sqlite3.IntegrityError:
            self.db.rollback()
            return None
        try:
            final = self._publish_bundle(
                bundle_name, self._bundle_files(incident, evidence, model_request, recovery)
            )
            self._enqueue_notification(
                key, f"opened:{key}", "opened", notification, created, severity,
                silent, episode=1,
            )
            self.db.commit()
            return final
        except Exception:
            self.db.rollback()
            raise

    def record_observation(
        self,
        observation: dict[str, Any],
        incident: dict[str, Any] | None = None,
        evidence: Any = None,
        notification: str = "",
        model_request: dict[str, Any] | None = None,
        severity: str = "warning",
        silent: bool | None = None,
        interrupt_other_recoveries: bool = True,
        apply_healthy_recovery: bool = True,
    ) -> ObservationWriteResult:
        """Retain one observation and update its incident lifecycle atomically.

        A non-healthy sample normally cancels every pending recovery of its source.
        Callers recording one event of a complete observation pass
        ``interrupt_other_recoveries=False`` and settle absent incidents afterwards
        with :meth:`settle_absent_incidents`. A healthy sample that could not observe
        every device passes ``apply_healthy_recovery=False``: it is retained, but it
        neither starts nor advances any recovery.

        ``delivery_key`` is optional. When present it provides source-level
        idempotency and a repeated delivery returns ``(None, True)`` without a
        second observation or transition. Without it, equal observations are
        genuine samples and remain independently countable.
        """
        target = str(observation["target"])
        machine_id = str(observation["machine_id"])
        if not target or len(target) > 255 or machine_id != MACHINE_ID:
            raise ValueError("state accepts observations only for Vast machine 17049")
        source = str(observation["source"])
        source_utc = str(observation["source_utc"])
        receipt_utc = str(observation["receipt_utc"])
        boot_id = str(observation["boot_id"])
        status = str(observation["status"])
        freshness = str(observation["freshness"])
        effective_status = status if freshness == "fresh" else freshness
        event_id = observation.get("source_event_id")
        delivery_key = observation.get("delivery_key")
        digest = str(observation["evidence_sha256"])
        evidence_json = canonical_json(bounded_evidence(evidence))
        key = None if incident is None else str(incident["dedup_key"])
        if (
            not source or len(source) > 128 or not boot_id or len(boot_id) > 128
            or status not in {"healthy", "unhealthy", "unknown", "stale"}
            or freshness not in {"fresh", "stale", "unknown"}
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("observation contains invalid or unbounded provenance")
        for timestamp in (source_utc, receipt_utc):
            if len(timestamp) > 64 or not timestamp.endswith("Z"):
                raise ValueError("observation timestamps must be bounded UTC values")
            _parse_utc(timestamp)
        if event_id is not None and (not str(event_id) or len(str(event_id)) > 256):
            raise ValueError("source event identity is invalid or unbounded")
        if delivery_key is not None and not re.fullmatch(r"[0-9a-f]{64}", str(delivery_key)):
            raise ValueError("delivery key must be a SHA-256 digest")
        if key is not None and not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("incident key must be a SHA-256 digest")
        severity = self._normalize_severity(severity)
        bundle: Path | None = None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            watermark = self.db.execute(
                """SELECT last_source_utc,boot_id FROM source_state
                   WHERE target=? AND source=?""",
                (target, source),
            ).fetchone()
            ordering = (
                "out_of_order"
                if watermark is not None
                and _parse_utc(source_utc) < _parse_utc(watermark["last_source_utc"])
                else "current"
            )
            material_changed = bool(
                ordering == "current"
                and watermark is not None
                and watermark["boot_id"]
                and watermark["boot_id"] != boot_id
            )
            existing = None
            bundle_name = ""
            if key is not None:
                existing = self.db.execute(
                    "SELECT * FROM incidents WHERE dedup_key=?", (key,)
                ).fetchone()
                if existing is None:
                    created = receipt_utc
                    stamp = created.replace(":", "").replace("-", "").replace(".", "")
                    bundle_name = f"{stamp}_{key[:16]}"
                    self.db.execute(
                        """INSERT INTO incidents(
                             dedup_key,bundle_name,created_utc,target,machine_id,source,
                             fault_family,stable_signature,severity,status,
                             first_occurrence_utc,last_occurrence_utc,last_boot_id,
                             occurrence_count)
                           VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,1)""",
                        (
                            key, bundle_name, created, target, machine_id, source,
                            str(incident["fault_family"]), str(incident["stable_signature"]),
                            severity[:16], source_utc, source_utc, boot_id,
                        ),
                    )
            try:
                cursor = self.db.execute(
                    """INSERT INTO observations(
                         target,machine_id,source,source_event_id,delivery_key,
                         source_utc,receipt_utc,boot_id,status,freshness,ordering,
                         evidence_sha256,evidence_json,incident_key)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        target, machine_id, source, event_id, delivery_key, source_utc,
                        receipt_utc, boot_id, status, freshness, ordering, digest,
                        evidence_json, key,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if delivery_key and "observations.delivery_key" in str(error):
                    self.db.rollback()
                    return ObservationWriteResult(None, True)
                raise
            observation_id = int(cursor.lastrowid)
            if ordering == "current":
                self.db.execute(
                    """INSERT INTO source_state(target,source,last_source_utc,last_receipt_utc,status,boot_id)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(target,source) DO UPDATE SET
                         last_source_utc=excluded.last_source_utc,
                         last_receipt_utc=excluded.last_receipt_utc,
                         status=excluded.status,boot_id=excluded.boot_id""",
                    (target, source, source_utc, receipt_utc, effective_status, boot_id),
                )
                if effective_status != "healthy" and interrupt_other_recoveries:
                    self._interrupt_recoveries(
                        target, source, source_utc, boot_id, observation_id, key
                    )
            if key is not None:
                if existing is None:
                    transition = "opened"
                    material_changed = material_changed or ordering == "current"
                elif ordering == "out_of_order":
                    transition = "out_of_order_repeat"
                elif existing["status"] == "recovered":
                    transition = "reopened"
                elif existing["status"] == "recovery_pending":
                    transition = "flapped"
                else:
                    transition = "repeated"
                if existing is not None:
                    lifecycle_status = existing["status"]
                    recovery_started = existing["recovery_started_utc"]
                    recovered = existing["recovered_utc"]
                    last_boot = existing["last_boot_id"]
                    if ordering == "current":
                        lifecycle_status = "open"
                        recovery_started = None
                        recovered = None
                    if last_boot and last_boot != boot_id:
                        self._insert_transition(key, observation_id, "boot_changed", source_utc, boot_id)
                    last_occurrence = existing["last_occurrence_utc"]
                    if _parse_utc(source_utc) > _parse_utc(last_occurrence):
                        last_occurrence = source_utc
                    old_severity = self._normalize_severity(existing["severity"])
                    worsened = ordering == "current" and (
                        self._severity_rank(severity) > self._severity_rank(old_severity)
                    )
                    material_changed = material_changed or (
                        ordering == "current"
                        and (existing["status"] == "recovered" or worsened)
                    )
                    new_episode = ordering == "current" and (
                        existing["status"] == "recovered" or worsened
                    )
                    episode = int(existing["notification_episode"]) + int(new_episode)
                    if new_episode:
                        self.db.execute(
                            """UPDATE outbox SET state='cancelled'
                               WHERE incident_key=? AND event_type='reminder'
                                 AND state='pending'""",
                            (key,),
                        )
                    self.db.execute(
                        """UPDATE incidents SET status=?,last_occurrence_utc=?,severity=?,
                             last_boot_id=CASE WHEN ?='current' THEN ? ELSE last_boot_id END,
                             occurrence_count=occurrence_count+1,
                             recovery_started_utc=?,
                             recovery_last_healthy_utc=CASE
                               WHEN ?='current' THEN NULL ELSE recovery_last_healthy_utc END,
                             recovered_utc=?,notification_episode=?,
                             acknowledged_utc=CASE WHEN ? THEN NULL ELSE acknowledged_utc END
                           WHERE dedup_key=?""",
                        (
                            lifecycle_status, last_occurrence,
                            severity if worsened else old_severity,
                            ordering, boot_id, recovery_started, ordering, recovered, episode,
                            int(new_episode), key,
                        ),
                    )
                self._insert_transition(key, observation_id, transition, source_utc, boot_id)
                if existing is None:
                    recovery = {
                        "schema_version": 1,
                        "kind": "lifecycle-incident",
                        "incident": incident,
                        "observation": observation,
                        "notification": notification[:3500],
                        "severity": severity[:16],
                        "silent": self._notification_silent(severity, silent),
                    }
                    bundle = self._publish_bundle(
                        bundle_name, self._bundle_files(incident, evidence, model_request, recovery)
                    )
                    self._enqueue_notification(
                        key, f"opened:{key}:{observation_id}", "opened", notification,
                        receipt_utc, severity, silent, episode=1,
                    )
                elif ordering == "current":
                    episode = int(self.db.execute(
                        "SELECT notification_episode FROM incidents WHERE dedup_key=?", (key,)
                    ).fetchone()[0])
                    if existing["status"] == "recovered":
                        self._enqueue_notification(
                            key, f"reopened:{key}:{observation_id}", "reopened",
                            f"terracompute incident reopened: {notification}", receipt_utc,
                            severity, silent, episode=episode,
                        )
                    if self._severity_rank(severity) > self._severity_rank(existing["severity"]):
                        self._insert_transition(
                            key, observation_id, "severity_worsened", source_utc, boot_id
                        )
                        self._enqueue_notification(
                            key, f"severity-worsened:{key}:{observation_id}",
                            "severity_worsened",
                            f"terracompute incident severity worsened to {severity}: {notification}",
                            receipt_utc, severity, silent, episode=episode,
                        )
            elif effective_status == "healthy" and ordering == "current" and apply_healthy_recovery:
                self._apply_healthy_observation(target, source, source_utc, boot_id, observation_id)
            self.db.commit()
            return ObservationWriteResult(
                bundle, False, material_changed, observation_id, ordering == "current"
            )
        except Exception:
            self.db.rollback()
            raise

    def _insert_transition(
        self, key: str, observation_id: int, transition: str, occurred: str, boot_id: str
    ) -> None:
        self.db.execute(
            """INSERT INTO transitions(incident_key,observation_id,transition,occurred_utc,boot_id)
               VALUES(?,?,?,?,?)""",
            (key, observation_id, transition, occurred, boot_id),
        )

    @staticmethod
    def _normalize_severity(severity: Any) -> str:
        value = str(severity).strip().lower()
        return value if value in {"info", "warning", "error", "critical"} else "warning"

    @classmethod
    def _severity_rank(cls, severity: Any) -> int:
        return {"info": 0, "warning": 1, "error": 2, "critical": 3}[
            cls._normalize_severity(severity)
        ]

    @classmethod
    def _notification_silent(cls, severity: Any, override: bool | None) -> bool:
        if override is not None:
            if not isinstance(override, bool):
                raise ValueError("silent metadata must be boolean")
        if cls._normalize_severity(severity) == "critical":
            return False
        if override is not None:
            return override
        return cls._normalize_severity(severity) in {"info", "warning"}

    def _enqueue_notification(
        self,
        incident_key: str,
        event_key: str,
        event_type: str,
        message: str,
        next_attempt_utc: str,
        severity: str,
        silent: bool | None,
        *,
        episode: int,
        reminder_number: int = 0,
    ) -> bool:
        cursor = self.db.execute(
            """INSERT INTO outbox(
                 incident_key,event_key,event_type,episode,reminder_number,message,
                 next_attempt_utc,severity,silent)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (
                incident_key, event_key[:255], event_type[:32], episode, reminder_number,
                message[:3500], next_attempt_utc, self._normalize_severity(severity),
                int(self._notification_silent(severity, silent)),
            ),
        )
        return cursor.rowcount == 1

    def settle_absent_incidents(
        self,
        target: str,
        source: str,
        present_keys: frozenset[str],
        source_utc: str,
        boot_id: str,
        observation_id: int,
    ) -> None:
        """Count a complete observation as healthy for each incident it no longer shows.

        The caller guarantees that the observation evaluated every check of its source,
        so an absent incident's fault is known to be clear. Present incidents are
        untouched; their own writes already reopened them.
        """
        _parse_utc(source_utc)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._apply_healthy_observation(
                target, source, source_utc, boot_id, observation_id, exclude=present_keys
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _apply_healthy_observation(
        self,
        target: str,
        source: str,
        source_utc: str,
        boot_id: str,
        observation_id: int,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        active = [
            row
            for row in self.db.execute(
                """SELECT dedup_key,status,recovery_started_utc,recovery_last_healthy_utc,
                          last_boot_id,severity,notification_episode FROM incidents
                   WHERE target=? AND source=? AND status IN ('open','recovery_pending')""",
                (target, source),
            )
            if row["dedup_key"] not in exclude
        ]
        now = _parse_utc(source_utc)
        for row in active:
            key = row["dedup_key"]
            boot_changed = bool(
                row["last_boot_id"] and row["last_boot_id"] != boot_id
            )
            if boot_changed:
                self._insert_transition(key, observation_id, "boot_changed", source_utc, boot_id)
            started = row["recovery_started_utc"]
            if row["status"] == "open" or started is None:
                self.db.execute(
                    """UPDATE incidents SET status='recovery_pending',recovery_started_utc=?,
                              recovery_last_healthy_utc=?,last_boot_id=? WHERE dedup_key=?""",
                    (source_utc, source_utc, boot_id, key),
                )
                self._insert_transition(key, observation_id, "recovery_started", source_utc, boot_id)
                continue
            if boot_changed:
                self._insert_transition(
                    key, observation_id, "recovery_interrupted", source_utc, boot_id
                )
                self.db.execute(
                    """UPDATE incidents SET recovery_started_utc=?,
                              recovery_last_healthy_utc=?,last_boot_id=? WHERE dedup_key=?""",
                    (source_utc, source_utc, boot_id, key),
                )
                self._insert_transition(
                    key, observation_id, "recovery_started", source_utc, boot_id
                )
                continue
            last_healthy = row["recovery_last_healthy_utc"] or started
            gap = (now - _parse_utc(last_healthy)).total_seconds()
            if gap > self._healthy_gap_for(source):
                self._insert_transition(
                    key, observation_id, "recovery_interrupted", source_utc, boot_id
                )
                self.db.execute(
                    """UPDATE incidents SET recovery_started_utc=?,
                              recovery_last_healthy_utc=?,last_boot_id=? WHERE dedup_key=?""",
                    (source_utc, source_utc, boot_id, key),
                )
                self._insert_transition(
                    key, observation_id, "recovery_started", source_utc, boot_id
                )
            elif (now - _parse_utc(started)).total_seconds() >= RECOVERY_WINDOW_SECONDS:
                self.db.execute(
                    """UPDATE incidents SET status='recovered',recovered_utc=?,
                              recovery_last_healthy_utc=?,last_boot_id=?
                       WHERE dedup_key=?""",
                    (source_utc, source_utc, boot_id, key),
                )
                self._insert_transition(key, observation_id, "recovered", source_utc, boot_id)
                self.db.execute(
                    """UPDATE outbox SET state='cancelled'
                       WHERE incident_key=? AND event_type='reminder' AND state='pending'""",
                    (key,),
                )
                self._enqueue_notification(
                    key, f"recovered:{key}:{observation_id}", "recovered",
                    f"terracompute incident recovered on {target} "
                    f"(machine {MACHINE_ID}, incident {key[:12]})",
                    utc_text(self.clock()), row["severity"], None,
                    episode=int(row["notification_episode"]),
                )
            else:
                self.db.execute(
                    "UPDATE incidents SET recovery_last_healthy_utc=?,last_boot_id=? WHERE dedup_key=?",
                    (source_utc, boot_id, key),
                )

    def _interrupt_recoveries(
        self,
        target: str,
        source: str,
        source_utc: str,
        boot_id: str,
        observation_id: int,
        matching_incident: str | None,
    ) -> None:
        """Cancel healthy windows when a current non-healthy source sample arrives."""
        pending = list(self.db.execute(
            """SELECT dedup_key FROM incidents
               WHERE target=? AND source=? AND status='recovery_pending'""",
            (target, source),
        ))
        for row in pending:
            key = row["dedup_key"]
            if key == matching_incident:
                continue
            self.db.execute(
                """UPDATE incidents SET status='open',recovery_started_utc=NULL,
                          recovery_last_healthy_utc=NULL,
                          recovered_utc=NULL WHERE dedup_key=?""",
                (key,),
            )
            self._insert_transition(
                key, observation_id, "recovery_interrupted", source_utc, boot_id
            )

    def _verified_bundle(self, directory: Path) -> dict[str, Any] | None:
        try:
            if directory.is_symlink() or not directory.is_dir():
                return None
            manifest_path = directory / "manifest.sha256"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                return None
            if manifest_path.stat().st_size > 4096:
                return None
            raw_manifest = manifest_path.read_bytes()
            listed: dict[str, str] = {}
            total = 0
            for line in raw_manifest.decode("ascii").splitlines():
                if len(listed) >= MAX_BUNDLE_FILES:
                    return None
                digest, separator, name = line.partition("  ")
                if (
                    separator != "  " or len(digest) != 64 or not _BUNDLE_FILE.fullmatch(name)
                    or name in listed
                ):
                    return None
                path = directory / name
                if not path.is_file() or path.is_symlink():
                    return None
                size = path.stat().st_size
                total += size
                if total > MAX_BUNDLE_BYTES:
                    return None
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != digest:
                    return None
                listed[name] = digest
            if "incident.json" not in listed or "evidence.json" not in listed:
                return None
            actual_entries = list(itertools.islice(directory.iterdir(), MAX_BUNDLE_FILES + 2))
            if len(actual_entries) > MAX_BUNDLE_FILES + 1:
                return None
            actual = {p.name for p in actual_entries if p.name != "manifest.sha256"}
            if actual != set(listed):
                return None
            evidence_json = (directory / "evidence.json").read_bytes()
            json.loads(evidence_json)
            if "recovery.json" in listed:
                value = json.loads((directory / "recovery.json").read_bytes())
                if not isinstance(value, dict):
                    return None
                value["_verified_evidence_json"] = evidence_json
                return value
            incident = json.loads((directory / "incident.json").read_bytes())
            if not isinstance(incident, dict):
                return None
            return {
                "kind": "baseline-orphan",
                "incident": incident,
                "evidence_sha256": listed["evidence.json"],
                "_verified_evidence_json": evidence_json,
            }
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None

    def recover_orphan_bundles(self) -> dict[str, int]:
        """Index verified published bundles left by a pre-commit crash.

        Corrupt, unsupported, and excess bundles are preserved untouched and
        counted as skipped. Recovery is idempotent and recreates an outbox row
        only in the same transaction which restores the missing incident.
        """
        recovered = 0
        skipped = 0
        bounded_entries = list(itertools.islice(self.incident_root.iterdir(), MAX_ORPHAN_BUNDLES + 1))
        overflow = len(bounded_entries) > MAX_ORPHAN_BUNDLES
        entries = sorted(bounded_entries[:MAX_ORPHAN_BUNDLES], key=lambda path: path.name)
        skipped = int(overflow)
        for directory in entries:
            if directory.name.startswith("."):
                continue
            metadata = self._verified_bundle(directory)
            if metadata is None:
                skipped += 1
                continue
            kind = metadata.get("kind")
            if kind == "legacy-incident":
                key = str(metadata.get("key", ""))
                created = str(metadata.get("created_utc", ""))
                recovered_target = str(metadata.get("target", ""))
                incident = None
                observation = None
                restore_notification = True
            elif kind == "baseline-orphan":
                incident = metadata.get("incident")
                if not isinstance(incident, dict):
                    skipped += 1
                    continue
                key = str(incident.get("dedup_key", ""))
                created = str(incident.get("observed_at", ""))
                recovered_target = str(incident.get("target", ""))
                observation = {
                    "target": recovered_target,
                    "machine_id": str(incident.get("machine_id", "")),
                    "source": str(incident.get("source", "target-probe")),
                    "source_event_id": None,
                    "delivery_key": None,
                    "source_utc": created,
                    "receipt_utc": created,
                    "boot_id": str(incident.get("boot_id", "unknown")),
                    "status": "unhealthy",
                    "freshness": "unknown",
                    "evidence_sha256": str(metadata.get("evidence_sha256", "")),
                }
                metadata["severity"] = str(
                    incident.get("classification", {}).get("severity", "warning")
                    if isinstance(incident.get("classification"), dict) else "warning"
                )
                metadata["machine_id"] = observation["machine_id"]
                restore_notification = False
            elif kind == "lifecycle-incident":
                incident = metadata.get("incident")
                observation = metadata.get("observation")
                if not isinstance(incident, dict) or not isinstance(observation, dict):
                    skipped += 1
                    continue
                key = str(incident.get("dedup_key", ""))
                created = str(observation.get("receipt_utc", ""))
                recovered_target = str(observation.get("target", ""))
                if (
                    not recovered_target or len(recovered_target) > 255
                    or str(observation.get("machine_id")) != MACHINE_ID
                    or str(incident.get("target", "")) != recovered_target
                ):
                    skipped += 1
                    continue
                restore_notification = True
            else:
                skipped += 1
                continue
            if (
                not re.fullmatch(r"[0-9a-f]{64}", key) or not created.endswith("Z")
                or not recovered_target or len(recovered_target) > 255
                or str(metadata.get("machine_id", MACHINE_ID)) != MACHINE_ID
            ):
                skipped += 1
                continue
            try:
                _parse_utc(created)
            except ValueError:
                skipped += 1
                continue
            if observation is not None:
                recovered_source = str(observation.get("source", ""))
                recovered_boot = str(observation.get("boot_id", ""))
                recovered_digest = str(observation.get("evidence_sha256", ""))
                if (
                    not recovered_source or len(recovered_source) > 128
                    or not recovered_boot or len(recovered_boot) > 128
                    or str(observation.get("status")) not in {
                        "healthy", "unhealthy", "unknown", "stale"
                    }
                    or str(observation.get("freshness")) not in {
                        "fresh", "stale", "unknown"
                    }
                    or not re.fullmatch(r"[0-9a-f]{64}", recovered_digest)
                ):
                    skipped += 1
                    continue
                try:
                    _parse_utc(str(observation.get("source_utc", "")))
                    _parse_utc(str(observation.get("receipt_utc", "")))
                except ValueError:
                    skipped += 1
                    continue
            if self.has_incident(key):
                continue
            try:
                self.db.execute("BEGIN IMMEDIATE")
                if incident is None:
                    self.db.execute(
                        "INSERT INTO incidents(dedup_key,bundle_name,created_utc) VALUES(?,?,?)",
                        (key, directory.name, created),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO incidents(
                             dedup_key,bundle_name,created_utc,target,machine_id,source,
                             fault_family,stable_signature,severity,status,first_occurrence_utc,
                             last_occurrence_utc,last_boot_id,occurrence_count)
                           VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,1)""",
                        (
                            key, directory.name, created, recovered_target, MACHINE_ID,
                            str(observation["source"]), str(incident["fault_family"]),
                            str(incident["stable_signature"]), str(metadata.get("severity", "warning"))[:16],
                            str(observation["source_utc"]), str(observation["source_utc"]),
                            str(observation["boot_id"]),
                        ),
                    )
                    cursor = self.db.execute(
                        """INSERT INTO observations(
                             target,machine_id,source,source_event_id,delivery_key,source_utc,
                             receipt_utc,boot_id,status,freshness,evidence_sha256,evidence_json,
                             incident_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            recovered_target, MACHINE_ID, str(observation["source"]),
                            observation.get("source_event_id"), observation.get("delivery_key"),
                            str(observation["source_utc"]), str(observation["receipt_utc"]),
                            str(observation["boot_id"]), str(observation["status"]),
                            str(observation["freshness"]), str(observation["evidence_sha256"]),
                            metadata["_verified_evidence_json"], key,
                        ),
                    )
                    self._insert_transition(
                        key, int(cursor.lastrowid), "opened", str(observation["source_utc"]),
                        str(observation["boot_id"]),
                    )
                    current = self.db.execute(
                        "SELECT last_source_utc FROM source_state WHERE target=? AND source=?",
                        (recovered_target, str(observation["source"])),
                    ).fetchone()
                    if current is None or _parse_utc(str(observation["source_utc"])) >= _parse_utc(current["last_source_utc"]):
                        self.db.execute(
                            """INSERT INTO source_state(
                                 target,source,last_source_utc,last_receipt_utc,status,boot_id)
                               VALUES(?,?,?,?,?,?) ON CONFLICT(target,source) DO UPDATE SET
                                 last_source_utc=excluded.last_source_utc,
                                 last_receipt_utc=excluded.last_receipt_utc,
                                 status=excluded.status,boot_id=excluded.boot_id""",
                            (
                                recovered_target, str(observation["source"]), str(observation["source_utc"]),
                                str(observation["receipt_utc"]), str(observation["status"]),
                                str(observation["boot_id"]),
                            ),
                        )
                if restore_notification:
                    self._enqueue_notification(
                        key, f"restored-opened:{key}:{directory.name}", "opened",
                        str(metadata.get("notification", "")), created,
                        str(metadata.get("severity", "warning")),
                        metadata.get("silent"), episode=1,
                    )
                self.db.commit()
                recovered += 1
            except (KeyError, sqlite3.Error, ValueError):
                self.db.rollback()
                skipped += 1
        return {"recovered": recovered, "skipped": skipped}

    def due_notifications(self, limit: int = 20) -> list[sqlite3.Row]:
        """Return due lifecycle events while suppressing acknowledged reminders."""
        limit = max(0, min(int(limit), 100))
        return list(
            self.db.execute(
                """SELECT o.id,o.message,o.attempts,o.severity,o.silent,
                          o.incident_key,o.incident_key AS incident_id,
                          o.event_key,o.event_type,o.reminder_number
                   FROM outbox o JOIN incidents i ON i.dedup_key=o.incident_key
                   WHERE o.state='pending' AND o.next_attempt_utc<=?
                     AND (o.event_type!='reminder' OR
                          (i.acknowledged_utc IS NULL AND i.status!='recovered'
                           AND o.episode=i.notification_episode))
                   ORDER BY CASE lower(o.severity)
                     WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                     WHEN 'warning' THEN 2 ELSE 3 END, o.id LIMIT ?""",
                (utc_text(self.clock()), limit),
            )
        )

    def mark_sent(self, item_id: int) -> None:
        row = self.db.execute(
            """SELECT o.*,i.status,i.acknowledged_utc,i.notification_episode
               FROM outbox o JOIN incidents i ON i.dedup_key=o.incident_key
               WHERE o.id=?""",
            (item_id,),
        ).fetchone()
        if row is None:
            return
        sent_at = self.clock()
        self.db.execute(
            "UPDATE outbox SET state='sent',sent_utc=?,last_error=NULL WHERE id=?",
            (utc_text(sent_at), item_id),
        )
        if (
            row["event_type"] in {"opened", "reopened", "severity_worsened", "reminder"}
            and row["status"] != "recovered"
            and row["acknowledged_utc"] is None
            and int(row["episode"]) == int(row["notification_episode"])
        ):
            reminder_number = (
                int(row["reminder_number"]) + 1
                if row["event_type"] == "reminder" else 1
            )
            if reminder_number <= MAX_REMINDERS:
                self._enqueue_notification(
                    row["incident_key"],
                    f"reminder:{row['incident_key']}:{row['episode']}:{reminder_number}",
                    "reminder",
                    f"terracompute incident remains unacknowledged "
                    f"(machine {MACHINE_ID}, incident {row['incident_key'][:12]}, "
                    f"reminder {reminder_number}/{MAX_REMINDERS})",
                    utc_text(sent_at + timedelta(seconds=REMINDER_INTERVAL_SECONDS)),
                    row["severity"], bool(row["silent"]), episode=int(row["episode"]),
                    reminder_number=reminder_number,
                )
        self.db.commit()

    def mark_failed(
        self, item_id: int, attempts: int, error: str,
        *, retry_after_seconds: int | None = None,
    ) -> None:
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int)
            or not 0 <= retry_after_seconds <= 86400
        ):
            raise ValueError("retry delay must be between 0 and 86400 seconds")
        next_attempts = attempts + 1
        delay = min(3600, 60 * (2 ** min(next_attempts - 1, 6)))
        delay = max(delay, retry_after_seconds or 0)
        next_at = self.clock() + timedelta(seconds=delay)
        self.db.execute(
            "UPDATE outbox SET attempts=?,next_attempt_utc=?,last_error=? WHERE id=?",
            (next_attempts, utc_text(next_at), error[:160], item_id),
        )
        self.db.commit()

    def acknowledge_incident(self, incident_id: str) -> bool:
        """Acknowledge one stable incident scoped to the fixed Vast 17049 target."""
        if not isinstance(incident_id, str) or not re.fullmatch(r"[0-9a-f]{64}", incident_id):
            raise ValueError("incident_id must be a stable SHA-256 incident identifier")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.db.execute(
                """UPDATE incidents SET acknowledged_utc=?
                   WHERE dedup_key=? AND target IN (?,?) AND machine_id=?
                     AND acknowledged_utc IS NULL""",
                (utc_text(self.clock()), incident_id, TARGET, LEGACY_TARGET, MACHINE_ID),
            )
            changed = cursor.rowcount == 1
            if changed:
                self.db.execute(
                    """UPDATE outbox SET state='cancelled'
                       WHERE incident_key=? AND event_type='reminder' AND state='pending'""",
                    (incident_id,),
                )
            self.db.commit()
            return changed
        except Exception:
            self.db.rollback()
            raise

    def incident_history(
        self, incident_id: str | None = None, *, limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return a bounded newest-first observation/transition timeline.

        ``before_id`` provides stable backwards pagination. Each call runs in a
        read transaction, so observations and transition labels use one view.
        """
        limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        clauses: list[str] = []
        values: list[Any] = []
        if incident_id is not None:
            clauses.append(
                "(o.incident_key=? OR EXISTS(SELECT 1 FROM transitions tx "
                "WHERE tx.observation_id=o.id AND tx.incident_key=?))"
            )
            values.extend((incident_id, incident_id))
        if before_id is not None:
            clauses.append("o.id<?")
            values.append(int(before_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        self.db.execute("BEGIN")
        try:
            rows = list(self.db.execute(
                "SELECT o.* FROM observations o" + where + " ORDER BY o.id DESC LIMIT ?",
                values,
            ))
            transition_rows: dict[int, list[str]] = {}
            for row in rows:
                transition_sql = "SELECT transition FROM transitions WHERE observation_id=?"
                transition_values: list[Any] = [row["id"]]
                if incident_id is not None:
                    transition_sql += " AND incident_key=?"
                    transition_values.append(incident_id)
                transition_sql += " ORDER BY id"
                transition_rows[row["id"]] = [
                    item["transition"] for item in self.db.execute(
                        transition_sql, transition_values
                    )
                ]
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        result = []
        for row in rows:
            item = dict(row)
            item["evidence_json"] = json.loads(item["evidence_json"])
            item["transitions"] = transition_rows[row["id"]]
            result.append(item)
        return result

    def current_snapshot(self, *, limit: int = 1000) -> dict[str, list[dict[str, Any]]]:
        """Return bounded current source/incident rows from one read view."""
        limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        self.db.execute("BEGIN")
        try:
            incidents = [dict(row) for row in self.db.execute(
                "SELECT * FROM incidents ORDER BY dedup_key LIMIT ?", (limit,)
            )]
            sources = [dict(row) for row in self.db.execute(
                "SELECT * FROM source_state ORDER BY target,source LIMIT ?", (limit,)
            )]
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"incidents": incidents, "sources": sources}

    def consistent_snapshot(self, destination: Path) -> Path:
        """Write a consistent SQLite backup for protected-storage integration.

        The destination must not exist, preventing accidental replacement of a
        known-good backup. SQLite supplies a consistent view while online.
        """
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
            target.commit()
        except Exception:
            target.close()
            destination.unlink(missing_ok=True)
            raise
        else:
            target.close()
        os.chmod(destination, 0o600)
        return destination
