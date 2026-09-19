"""Carrying out the monitoring work that is the agent's own, through a session."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from terracompute_ops.acting import RESTART_TIMEOUT_SECONDS, MonitoringActor
from terracompute_ops.monitor_restart import EvidenceStore

REQUEST = "00000000-0000-4000-8000-000000000042"
NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)


class MonitoringActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.evidence = EvidenceStore(self.db, lambda: NOW)
        self.calls: list[tuple] = []

    def actor(self, reply):
        outer = self

        class Client:
            def session(self, script, request_id, *, writable=False, timeout=None):
                outer.calls.append((script, request_id, writable, timeout))
                if isinstance(reply, Exception):
                    raise reply
                return dict(reply, id=request_id)

        return MonitoringActor(
            Client(), self.evidence, request_id_factory=lambda: REQUEST, clock=lambda: NOW
        )

    def envelope(self, **changes):
        return dict({
            "schema_version": 1, "operation": "session", "component": "host",
            "machine_id": 17049, "ok": True, "lines": ["node-exporter"],
            "truncated": False, "exit_code": 0, "writable": True,
        }, **changes)

    def test_it_restarts_through_a_writable_session(self) -> None:
        result = self.actor(self.envelope()).restart("node-exporter")
        self.assertTrue(result.ok)
        script, _id, writable, timeout = self.calls[0]
        self.assertEqual(script, "docker restart node-exporter")
        self.assertTrue(writable, "a restart cannot happen on the profile that cannot write")
        self.assertEqual(timeout, RESTART_TIMEOUT_SECONDS)

    def test_a_tenants_container_is_refused_without_reaching_the_machine(self) -> None:
        """The catalogue checks this too. Stated twice on purpose: a future caller that
        skips the catalogue still cannot name somebody's rental here."""
        result = self.actor(self.envelope()).restart("C.51217040")
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "not ours to restart")
        self.assertEqual(self.calls, [], "it opened a session for a tenant's container")

    def test_what_docker_said_when_it_failed_is_kept(self) -> None:
        """"No such container" and "permission denied" want different answers."""
        result = self.actor(self.envelope(
            exit_code=1, lines=["Error response from daemon: No such container: node-exporter"]
        )).restart("node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("No such container", result.detail)

    def test_a_refused_session_is_a_result_not_a_crash(self) -> None:
        result = self.actor(
            self.envelope(ok=False, reason="session_boundary_unavailable")
        ).restart("node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("session_boundary_unavailable", result.detail)

    def test_an_answer_from_somewhere_else_is_not_a_success(self) -> None:
        result = self.actor(self.envelope(machine_id=17050)).restart("node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("did not run", result.detail)

    def test_an_unreachable_host_is_a_result_not_a_crash(self) -> None:
        result = self.actor(OSError("no route")).restart("node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("did not run", result.detail)

    def test_every_attempt_is_recorded_whether_it_worked_or_not(self) -> None:
        self.actor(self.envelope()).restart("node-exporter", subject="incident:x")
        self.actor(self.envelope(exit_code=1)).restart("cadvisor", subject="incident:x")
        rows = self.db.execute(
            "SELECT subject FROM tc_action_evidence WHERE kind='action-result'"
        ).fetchall()
        self.assertEqual(len(rows), 2, "something this service did left no record")


if __name__ == "__main__":
    unittest.main()
