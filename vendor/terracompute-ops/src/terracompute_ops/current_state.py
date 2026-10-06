"""Read-only projections of accepted collector evidence, never archive arrival."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .lifecycle import Lifecycle, dependency_fresh


class _Reads:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("read-only projection")
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters)


class CurrentState:
    """Lifecycle readers without the StateStore constructor or any migrations."""
    def __init__(self, db: sqlite3.Connection, clock):
        self.db, self.clock = _Reads(db), clock

    @property
    def available(self) -> bool:
        # The collector can migrate while the actions service is already running.
        return bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='observation_batches'"
        ).fetchone())

    current_epoch = Lifecycle.current_epoch
    accepted_condition = Lifecycle.accepted_condition
    recovery_events = Lifecycle.recovery_events
    latest_accepted_evidence = Lifecycle.latest_accepted_evidence

    def boot(self) -> str:
        row = self.current_epoch("terracompute") if self.available else None
        return str(row["boot_id"]) if row else ""

    def latest(self, source: str, *, fresh: bool = True) -> dict[str, Any] | None:
        if not self.available:
            return None
        row = self.latest_accepted_evidence("terracompute", source)
        if not row:
            return None
        epoch = self.current_epoch("terracompute")
        if epoch and row["epoch"] != epoch["epoch"]:
            return None
        if fresh and not dependency_fresh(row, self.clock()):
            return None
        return row

    def document(self, source: str, *, fresh: bool = True) -> tuple[dict | None, datetime | None]:
        row = self.latest(source, fresh=fresh)
        if not row:
            return None, None
        raw = json.loads(row["evidence_json"])
        return raw, datetime.fromisoformat(row["measured_utc"].replace("Z", "+00:00"))

    def condition(self, key: str) -> dict | None:
        return self.accepted_condition(key) if self.available else None
