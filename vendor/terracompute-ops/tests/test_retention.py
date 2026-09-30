from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from terracompute_ops.retention import prune_observation_history


NOW = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
OLD = "2026-09-20T00:00:00.000000Z"
NEW = "2026-10-20T00:00:00.000000Z"


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              receipt_utc TEXT NOT NULL,
              incident_key TEXT);
            CREATE TABLE transitions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              incident_key TEXT NOT NULL,
              observation_id INTEGER NOT NULL REFERENCES observations(id));
            CREATE TABLE terracompute_observation_artifacts (
              artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              observed_utc TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              capture_class TEXT NOT NULL);
            CREATE TABLE inventory_probe_captures (
              payload_hash TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              state TEXT NOT NULL);
            """
        )

    def tearDown(self) -> None:
        self.db.close()

    def observation(self, stamp: str, incident: str | None = None) -> int:
        return self.db.execute(
            "INSERT INTO observations(receipt_utc, incident_key) VALUES (?, ?)",
            (stamp, incident),
        ).lastrowid

    def artifact(self, source: str, stamp: str, sha: str, capture_class: str = "routine") -> int:
        return self.db.execute(
            "INSERT INTO terracompute_observation_artifacts"
            "(source, observed_utc, sha256, capture_class) VALUES (?, ?, ?, ?)",
            (source, stamp, sha, capture_class),
        ).lastrowid

    def ids(self, table: str, column: str) -> list[int]:
        return [row[0] for row in self.db.execute(f"SELECT {column} FROM {table} ORDER BY 1")]

    def test_expired_routine_history_goes_and_everything_referenced_stays(self) -> None:
        routine_old = self.observation(OLD)
        incident_old = self.observation(OLD, "incident-1")
        transition_old = self.observation(OLD)
        self.db.execute(
            "INSERT INTO transitions(incident_key, observation_id) VALUES ('incident-2', ?)",
            (transition_old,),
        )
        recent = self.observation(NEW)

        expired = self.artifact("prometheus", OLD, "p-old")
        protected = self.artifact("prometheus", OLD, "p-protected", "protected")
        newest_prometheus = self.artifact("prometheus", NEW, "p-new")
        inventory_reference = self.artifact("ssh", OLD, "ssh-captured")
        ssh_expired = self.artifact("ssh", OLD, "ssh-old")
        only_bmc = self.artifact("bmc", OLD, "bmc-only")
        newest_ssh = self.artifact("ssh", NEW, "ssh-new")
        self.db.execute(
            "INSERT INTO inventory_probe_captures VALUES ('ssh-captured', ?, 'complete')", (OLD,)
        )
        self.db.commit()

        result = prune_observation_history(self.db, now=NOW)

        self.assertEqual((result.observations, result.artifacts), (1, 2))
        self.assertEqual(
            self.ids("observations", "id"), [incident_old, transition_old, recent]
        )
        self.assertNotIn(routine_old, self.ids("observations", "id"))
        remaining = self.ids("terracompute_observation_artifacts", "artifact_id")
        self.assertEqual(
            remaining,
            sorted([protected, newest_prometheus, inventory_reference, only_bmc, newest_ssh]),
        )
        self.assertNotIn(expired, remaining)
        self.assertNotIn(ssh_expired, remaining)

    def test_batches_are_bounded_and_repeat_until_done(self) -> None:
        for _ in range(7):
            self.observation(OLD)
        self.observation(NEW)
        self.db.commit()
        first = prune_observation_history(self.db, now=NOW, batch_rows=3)
        second = prune_observation_history(self.db, now=NOW, batch_rows=3)
        third = prune_observation_history(self.db, now=NOW, batch_rows=3)
        done = prune_observation_history(self.db, now=NOW, batch_rows=3)
        self.assertEqual(
            [first.observations, second.observations, third.observations, done.observations],
            [3, 3, 1, 0],
        )
        self.assertEqual(len(self.ids("observations", "id")), 1)

    def test_nothing_inside_the_window_is_removed(self) -> None:
        self.observation(NEW)
        self.artifact("vast", NEW, "v1")
        self.artifact("vast", NEW, "v2")
        self.db.commit()
        result = prune_observation_history(self.db, now=NOW)
        self.assertEqual((result.observations, result.artifacts), (0, 0))

    def test_invalid_bounds_and_naive_clock_are_rejected(self) -> None:
        for kwargs in ({"days": 0}, {"batch_rows": 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                prune_observation_history(self.db, now=NOW, **kwargs)
        with self.assertRaises(ValueError):
            prune_observation_history(self.db, now=datetime(2026, 11, 1))


if __name__ == "__main__":
    unittest.main()
