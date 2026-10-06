"""Bounded age-based retention for routine observation history.

Collection stores every source sample and routine capture. Without retention
the state database grew about 100 MB a day until its backup exceeded the per-file
bound (2026-09-21). Only routine history older than the window is removed:

* observations linked to an incident or a state transition stay;
* protected captures stay;
* the newest artifact of each source stays, because readers take the latest;
* the artifact of the latest completed inventory capture stays, because the
  collector compares new captures with it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

OBSERVATION_RETENTION_DAYS = 30
PRUNE_BATCH_ROWS = 5_000


@dataclass(frozen=True)
class PruneResult:
    observations: int
    artifacts: int


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _cutoff_text(now: datetime, days: int) -> str:
    if now.tzinfo is None:
        raise ValueError("retention clock must be timezone-aware")
    cutoff = now.astimezone(timezone.utc) - timedelta(days=days)
    # Stored stamps use this exact shape, so text order is time order.
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def prune_observation_history(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    days: int = OBSERVATION_RETENTION_DAYS,
    batch_rows: int = PRUNE_BATCH_ROWS,
) -> PruneResult:
    """Delete at most ``batch_rows`` of each kind of expired routine history."""
    if not 1 <= days <= 3650 or not 1 <= batch_rows <= 100_000:
        raise ValueError("retention bounds are invalid")
    cutoff = _cutoff_text(now, days)
    observations = artifacts = 0
    with connection:
        if _table_exists(connection, "observations"):
            linked = (
                "AND o.id NOT IN (SELECT observation_id FROM transitions)"
                if _table_exists(connection, "transitions")
                else ""
            )
            if _table_exists(connection, "incident_conditions"):
                linked += " AND o.id NOT IN (SELECT observation_id FROM incident_conditions)"
            if _table_exists(connection, "recovery_evidence"):
                linked += " AND o.id NOT IN (SELECT observation_id FROM recovery_evidence WHERE observation_id IS NOT NULL)"
            observations = connection.execute(
                f"""DELETE FROM observations WHERE id IN (
                      SELECT o.id FROM observations o
                      WHERE o.receipt_utc < ? AND o.incident_key IS NULL {linked}
                      ORDER BY o.id LIMIT ?)""",
                (cutoff, batch_rows),
            ).rowcount
        if _table_exists(connection, "observation_batches"):
            connection.execute("""DELETE FROM observation_batches WHERE id IN (
                SELECT b.id FROM observation_batches b WHERE b.receipt_utc < ?
                AND b.id NOT IN (SELECT batch_id FROM observations WHERE batch_id IS NOT NULL)
                AND b.id NOT IN (SELECT batch_id FROM recovery_evidence WHERE batch_id IS NOT NULL)
                AND b.id NOT IN (SELECT batch_id FROM incident_conditions)
                AND b.id NOT IN (SELECT recovery_batch_id FROM incidents WHERE recovery_batch_id IS NOT NULL)
                AND b.id NOT IN (SELECT MAX(id) FROM observation_batches WHERE ordering='current' GROUP BY target,source)
                AND b.id NOT IN (SELECT CAST(value AS INTEGER) FROM observation_batches, json_each(dependencies_json))
                ORDER BY b.id LIMIT ?)""", (cutoff, batch_rows))
        if _table_exists(connection, "terracompute_observation_artifacts"):
            reference = (
                """AND a.sha256 NOT IN (
                     SELECT payload_hash FROM inventory_probe_captures
                     WHERE state='complete' ORDER BY observed_at DESC LIMIT 1)"""
                if _table_exists(connection, "inventory_probe_captures")
                else ""
            )
            artifacts = connection.execute(
                f"""DELETE FROM terracompute_observation_artifacts WHERE artifact_id IN (
                      SELECT a.artifact_id FROM terracompute_observation_artifacts a
                      WHERE a.observed_utc < ? AND a.capture_class = 'routine'
                        AND a.artifact_id NOT IN (
                          SELECT MAX(artifact_id) FROM terracompute_observation_artifacts
                          GROUP BY source)
                        {reference}
                      ORDER BY a.artifact_id LIMIT ?)""",
                (cutoff, batch_rows),
            ).rowcount
    return PruneResult(max(observations, 0), max(artifacts, 0))
