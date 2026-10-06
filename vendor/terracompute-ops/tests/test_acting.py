"""Carrying out the monitoring work that is the agent's own, through a session."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from terracompute_ops.acting import RESTART_TIMEOUT_SECONDS, MonitoringActor
from terracompute_ops.monitor_restart import ActorError, EvidenceStore

REQUEST = "00000000-0000-4000-8000-000000000042"
NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
BOOT = "11111111-2222-4333-8444-555555555555"


class MonitoringActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.evidence = EvidenceStore(self.db, lambda: NOW)
        self.calls: list[tuple] = []

    def actor(self, reply):
        outer = self

        class Client:
            def session(self, script, request_id, *, writable=False, timeout=None,
                        expected_boot_id=None):
                outer.assertEqual(expected_boot_id, BOOT)
                outer.calls.append((script, request_id, writable, timeout))
                if isinstance(reply, Exception):
                    raise reply
                return dict(reply, id=request_id)

        class BoundActor(MonitoringActor):
            def run(self, command, subject=None, *, approved=False,
                    expected_boot_id=BOOT):
                return super().run(command, subject, approved=approved,
                                   expected_boot_id=expected_boot_id)

        return BoundActor(
            Client(), self.evidence, request_id_factory=lambda: REQUEST, clock=lambda: NOW
        )

    def envelope(self, **changes):
        return dict({
            "schema_version": 1, "operation": "session-v2", "component": "host",
            "machine_id": 17049, "ok": True, "lines": ["node-exporter"],
            "truncated": False, "exit_code": 0, "writable": True,
            "session_capability": "boot-bound-v2",
        }, **changes)

    def test_it_restarts_through_a_writable_session(self) -> None:
        result = self.actor(self.envelope()).run("docker restart node-exporter")
        self.assertTrue(result.ok)
        script, _id, writable, timeout = self.calls[0]
        self.assertEqual(script, "docker restart node-exporter")
        self.assertTrue(writable, "a restart cannot happen on the profile that cannot write")
        self.assertEqual(timeout, RESTART_TIMEOUT_SECONDS)

    def test_writable_session_without_approval_time_boot_binding_does_not_dispatch(self) -> None:
        result = self.actor(self.envelope()).run("docker restart node-exporter",
                                                  expected_boot_id=None)
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_no_approval_makes_a_tenants_container_ours(self) -> None:
        """`approved` widens what may run. It must not widen past a refusal.

        A person tapping Approve says they have read this exact command. It is not a
        power to hand somebody else's rental over, and the actor is the last place
        that can still say so.
        """
        result = self.actor(self.envelope()).run("docker restart C.51217040", approved=True)
        self.assertFalse(result.ok)
        self.assertIn("not something I will do at all", result.detail)
        self.assertEqual(self.calls, [], "it reached the machine")

    def test_an_approved_command_runs_though_it_is_not_unattended_work(self) -> None:
        """The whole point of an approval: a reboot is answerable now."""
        result = self.actor(self.envelope()).run("systemctl reboot", approved=True)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.calls[0][0], "systemctl reboot")
        self.assertIs(self.calls[0][2], True, "a change ran on the read-only profile")

    def test_without_an_approval_the_same_command_is_refused(self) -> None:
        result = self.actor(self.envelope()).run("systemctl reboot")
        self.assertFalse(result.ok)
        self.assertIn("not mine to do alone", result.detail)
        self.assertEqual(self.calls, [], "it rebooted the box unattended")

    def test_a_tenants_container_is_refused_without_reaching_the_machine(self) -> None:
        """The catalogue checks this too. Stated twice on purpose: a future caller that
        skips the catalogue still cannot name somebody's rental here."""
        result = self.actor(self.envelope()).run("docker restart C.51217040")
        self.assertFalse(result.ok)
        self.assertIn("not something I will do at all", result.detail)
        self.assertEqual(self.calls, [], "it opened a session for a tenant's container")

    def test_what_docker_said_when_it_failed_is_kept(self) -> None:
        """"No such container" and "permission denied" want different answers."""
        result = self.actor(self.envelope(
            exit_code=1, lines=["Error response from daemon: No such container: node-exporter"]
        )).run("docker restart node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("No such container", result.detail)

    def test_a_refused_session_is_a_result_not_a_crash(self) -> None:
        result = self.actor(
            self.envelope(ok=False, reason="session_boundary_unavailable")
        ).run("docker restart node-exporter")
        self.assertFalse(result.ok)
        self.assertIn("session_boundary_unavailable", result.detail)

    def test_an_answer_from_somewhere_else_is_not_a_success(self) -> None:
        result = self.actor(self.envelope(machine_id=17050)).run("docker restart node-exporter")
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)
        self.assertIn("may have run", result.detail)

    def test_an_unreachable_host_is_a_result_not_a_crash(self) -> None:
        result = self.actor(OSError("no route")).run("docker restart node-exporter")
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)
        self.assertIn("may have run", result.detail)

    def test_a_reboot_that_drops_its_reply_is_unknown_not_failed(self) -> None:
        result = self.actor(ActorError("actor_output_invalid")).run(
            "systemctl reboot", approved=True
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)
        self.assertIn("may have run", result.detail)

    def test_an_approved_action_that_drops_its_reply_may_have_run(self) -> None:
        """Only the command's text said whether it disconnects, and `echo ok; reboot`
        does not start with a reboot. A lost reply after dispatch is unknown, not failed:
        calling it failed invites running it again."""
        result = self.actor(ActorError("actor_output_invalid")).run(
            "docker restart node-exporter", approved=True
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)
        self.assertIn("may have run", result.detail)

    def test_an_unattended_action_that_drops_its_reply_is_uncertain(self) -> None:
        result = self.actor(ActorError("actor_output_invalid")).run(
            "docker restart node-exporter"
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)

    def test_every_attempt_is_recorded_whether_it_worked_or_not(self) -> None:
        self.actor(self.envelope()).run("docker restart node-exporter", subject="incident:x")
        self.actor(self.envelope(exit_code=1)).run("docker restart cadvisor", subject="incident:x")
        rows = self.db.execute(
            "SELECT subject FROM tc_action_evidence WHERE kind='action-result'"
        ).fetchall()
        self.assertEqual(len(rows), 2, "something this service did left no record")


if __name__ == "__main__":
    unittest.main()
