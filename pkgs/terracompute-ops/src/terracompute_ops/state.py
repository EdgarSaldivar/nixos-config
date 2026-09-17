"""Durable incident bundle and notification outbox storage."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .incidents import MAX_MODEL_REQUEST_BYTES, bounded_evidence, canonical_json


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StateStore:
    """SQLite index plus immutable, atomically published incident directories."""

    def __init__(self, root: Path, clock: Callable[[], datetime] = utc_now):
        self.root = root
        self.incident_root = root / "incidents"
        self.clock = clock
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.incident_root.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents (
              dedup_key TEXT PRIMARY KEY,
              bundle_name TEXT NOT NULL,
              created_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dedup_key TEXT NOT NULL UNIQUE,
              message TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_utc TEXT NOT NULL,
              last_error TEXT,
              sent_utc TEXT,
              FOREIGN KEY(dedup_key) REFERENCES incidents(dedup_key)
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def has_incident(self, key: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM incidents WHERE dedup_key = ?", (key,)
        ).fetchone()
        return row is not None

    def create_incident(
        self,
        key: str,
        incident: dict[str, Any],
        evidence: Any,
        notification: str,
        model_request: dict[str, Any] | None,
    ) -> Path | None:
        """Publish one new append-only bundle and enqueue exactly one notification."""
        created = utc_text(self.clock())
        stamp = created.replace(":", "").replace("-", "").replace(".", "")
        bundle_name = f"{stamp}_{key[:16]}"
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO incidents(dedup_key, bundle_name, created_utc) VALUES(?, ?, ?)",
                (key, bundle_name, created),
            )
        except sqlite3.IntegrityError:
            self.db.rollback()
            return None

        temp = Path(tempfile.mkdtemp(prefix=".incident-", dir=self.incident_root))
        final = self.incident_root / bundle_name
        try:
            files: dict[str, bytes] = {
                "incident.json": canonical_json(incident) + b"\n",
                "evidence.json": canonical_json(bounded_evidence(evidence)),
            }
            if model_request is not None:
                request = canonical_json(bounded_evidence(model_request, MAX_MODEL_REQUEST_BYTES))
                files["model-analysis-request.json"] = request
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
            self.db.execute(
                """INSERT INTO outbox(dedup_key, message, next_attempt_utc)
                   VALUES(?, ?, ?)""",
                (key, notification[:3500], created),
            )
            self.db.commit()
            return final
        except Exception:
            self.db.rollback()
            if temp.exists():
                shutil.rmtree(temp)
            # Once the atomic rename publishes a bundle, preserve it even if the
            # later SQLite commit fails. It may be an unindexed recovery artifact,
            # but deleting captured incident evidence is never an acceptable
            # rollback operation.
            raise

    def due_notifications(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                """SELECT id, message, attempts FROM outbox
                   WHERE state = 'pending' AND next_attempt_utc <= ?
                   ORDER BY id LIMIT ?""",
                (utc_text(self.clock()), limit),
            )
        )

    def mark_sent(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE outbox SET state = 'sent', sent_utc = ?, last_error = NULL WHERE id = ?",
            (utc_text(self.clock()), item_id),
        )
        self.db.commit()

    def mark_failed(self, item_id: int, attempts: int, error: str) -> None:
        next_attempts = attempts + 1
        delay = min(3600, 60 * (2 ** min(next_attempts - 1, 6)))
        next_at = self.clock() + timedelta(seconds=delay)
        self.db.execute(
            """UPDATE outbox SET attempts = ?, next_attempt_utc = ?, last_error = ?
               WHERE id = ?""",
            (next_attempts, utc_text(next_at), error[:160], item_id),
        )
        self.db.commit()
