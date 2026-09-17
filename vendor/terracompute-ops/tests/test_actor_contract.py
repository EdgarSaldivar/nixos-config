"""Contract tests: the real target helper's output against the controller's parser."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import unittest
import uuid
from datetime import timedelta

from terracompute_ops.actions import ActionBroker, ApprovalKind, HumanApprovalEvent
from terracompute_ops.monitor_restart import (
    ActorError,
    EvidenceStore,
    MonitorRestartAdapter,
    build_proposal,
    parse_status,
)
from terracompute_ops.policy import ActionClass, ActionPolicy, Mode

try:  # unittest discover puts tests/ on sys.path; module-path runs do not.
    import target_action_test as helper
except ImportError:
    from tests import target_action_test as helper

try:
    from test_monitor_restart import Authenticator, Membership
except ImportError:
    from tests.test_monitor_restart import Authenticator, Membership

GROUP = -1004484415005


class HarnessClient:
    """Reach the real helper code the way the forced SSH command does."""

    def __init__(self, harness: helper.Harness) -> None:
        self.harness = harness
        self.commands: list[str] = []

    def run(self, operation: str, request_id: str) -> dict:
        command = f"{operation} dcgm-exporter {request_id}"
        self.commands.append(command)
        document, _exit_code, _text = self.harness.run(command)
        return document


class Clock:
    def __init__(self) -> None:
        self.value = helper.NOW + timedelta(seconds=5)

    def __call__(self):
        return self.value


class ActorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = helper.Harness(self)
        self.harness.sysfs.block(helper.BLOCKED_BDF)
        self.client = HarnessClient(self.harness)
        self.clock = Clock()
        # The target helper reads the same simulated time as the controller.
        self.harness.env = dataclasses.replace(self.harness.env, clock=lambda: self.clock.value)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.backups: dict[str, str] = {}
        self.adapter = MonitorRestartAdapter(
            self.client,
            EvidenceStore(self.db, self.clock),
            backup_ref=lambda proposal: self.backups.get(proposal.proposal_id),
            request_id_factory=lambda: str(uuid.uuid4()),
            clock=self.clock,
        )
        self.broker = ActionBroker(
            self.db,
            policy=ActionPolicy(
                mode=Mode.APPROVE, revision="monitor-restart-r1",
                enabled_actions=frozenset({ActionClass.MONITOR_COMPONENT_RESTART}),
                approval_group_id=GROUP,
            ),
            membership=Membership(self.clock),
            authenticator=Authenticator(),
            adapter=self.adapter,
            clock=self.clock,
        )

    def test_helper_status_parses_with_the_blocked_gpu_and_vm_rental(self) -> None:
        request_id = str(uuid.uuid4())
        status = parse_status(self.client.run("status", request_id), request_id)
        self.assertEqual(status.handover_blocked, (helper.BLOCKED_BDF,))
        self.assertEqual(status.vm_containers, ("C.26000001",))
        self.assertEqual(status.tenants.names, ("C.26000001", "C.26000002"))
        self.assertEqual(status.nvidia_visible_count, 7)
        self.assertTrue(status.identity_verified)
        self.assertTrue(status.container.running)

    def test_approved_restart_end_to_end_through_the_real_helper(self) -> None:
        def after_restart(docker: helper.FakeDocker) -> None:
            # Releasing the handles lets Vast finish removing the stuck VM rental.
            docker.containers = [c for c in docker.containers if c["name"] != "C.26000001"]
            self.harness.sysfs.release(helper.BLOCKED_BDF)
            self.harness.sysfs.bind(helper.BLOCKED_BDF, "nvidia")
            self.harness.sysfs.bind(helper.BLOCKED_BDF[:-1] + "1", "snd_hda_intel")

        self.harness.docker.after_restart = after_restart
        proposal = build_proposal(
            self.adapter.status(), helper.BLOCKED_BDF,
            policy_revision="monitor-restart-r1", clock=self.clock,
        )
        self.broker.submit_proposal(proposal)
        self.backups[proposal.proposal_id] = "terracompute-backup.service@1789620000"
        self.broker.record_human_event(HumanApprovalEvent(
            event_id="telegram:contract:1", kind=ApprovalKind.APPROVE, group_id=GROUP,
            user_id=4242, display_name="telegram:4242", proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest, nonce="contract-nonce", occurred_at=self.clock(),
        ))
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "succeeded", attempt.result_detail)
        self.assertIn("handover cleared", attempt.result_detail)
        self.assertEqual(self.harness.docker.commands().count("exporter_restart"), 1)
        self.assertEqual(len(self.harness.ledger_files()), 1)
        record = self.harness.record(attempt.execution_id)
        self.assertEqual((record["state"], record["ok"]), ("executed", True))
        # Reconciling reads the ledger and never restarts again.
        self.assertEqual(
            self.adapter.reconcile(proposal, attempt.execution_id).status.value, "succeeded"
        )
        self.assertEqual(self.harness.docker.commands().count("exporter_restart"), 1)

    def approved_proposal(self, nonce: str):
        proposal = build_proposal(
            self.adapter.status(), helper.BLOCKED_BDF,
            policy_revision="monitor-restart-r1", clock=self.clock,
        )
        self.broker.submit_proposal(proposal)
        self.backups[proposal.proposal_id] = "terracompute-backup.service@1789620000"
        self.broker.record_human_event(HumanApprovalEvent(
            event_id=f"telegram:contract:{nonce}", kind=ApprovalKind.APPROVE, group_id=GROUP,
            user_id=4242, display_name="telegram:4242", proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest, nonce=nonce, occurred_at=self.clock(),
        ))
        return proposal

    def test_timed_out_restart_that_took_effect_is_settled_by_reconciliation(self) -> None:
        proposal = self.approved_proposal("contract-nonce-3")
        self.harness.docker.overrides["exporter_restart"] = helper.act.CommandResult(None, "", "timeout")
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "unknown", attempt.result_detail)
        # docker finishes the restart after the CLI gave up, and Vast completes the handover.
        docker = self.harness.docker
        docker.exporter["started_at"] = docker.restart_started_at
        docker.containers = [c for c in docker.containers if c["name"] != "C.26000001"]
        self.harness.sysfs.release(helper.BLOCKED_BDF)
        self.harness.sysfs.bind(helper.BLOCKED_BDF, "nvidia")
        self.harness.sysfs.bind(helper.BLOCKED_BDF[:-1] + "1", "snd_hda_intel")
        self.clock.value += timedelta(seconds=30)
        self.assertEqual(self.broker.reconcile(attempt.execution_id).state, "unknown")
        self.clock.value += timedelta(minutes=3)
        settled = self.broker.reconcile(attempt.execution_id)
        self.assertEqual(settled.state, "succeeded", settled.result_detail)
        self.assertIn("restart took effect after restart_timeout", settled.result_detail)
        self.assertEqual(docker.commands().count("exporter_restart"), 1)

    def test_timed_out_restart_without_effect_fails_and_releases_the_lock(self) -> None:
        proposal = self.approved_proposal("contract-nonce-4")
        self.harness.docker.overrides["exporter_restart"] = helper.act.CommandResult(None, "", "timeout")
        attempt = self.broker.execute(proposal.proposal_id)
        self.clock.value += timedelta(minutes=3)
        settled = self.broker.reconcile(attempt.execution_id)
        self.assertEqual(settled.state, "failed", settled.result_detail)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0)

    def dead_run(self, nonce: str, record: dict, took_effect: bool = False):
        proposal = self.approved_proposal(nonce)
        original = self.client.run

        def die_after_claim(operation: str, request_id: str) -> dict:
            if operation != "restart":
                return original(operation, request_id)
            # The helper wrote this record for the ID and was killed.
            path = self.harness.ledger / f"{request_id}.json"
            path.write_text(json.dumps({
                "schema_version": 1, "id": request_id, "component": "dcgm-exporter",
                "requested_at": "2026-09-17T04:00:05Z", "execution_boot_id": helper.BOOT_ID,
                **record,
            }), encoding="ascii")
            os.chmod(path, 0o600)
            if took_effect:
                docker = self.harness.docker
                docker.exporter["started_at"] = docker.restart_started_at
            raise ActorError("actor_timeout")

        self.client.run = die_after_claim
        attempt = self.broker.execute(proposal.proposal_id)
        self.client.run = original
        self.assertEqual(attempt.state, "unknown", attempt.result_detail)
        return attempt

    def test_a_run_that_died_before_arming_is_proven_not_started(self) -> None:
        attempt = self.dead_run("contract-nonce-unarmed", {"state": "pending"})
        settled = self.broker.reconcile(attempt.execution_id)
        self.assertEqual((settled.state, settled.result_detail), ("refused", "execution_not_started"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0)
        self.assertEqual(self.harness.docker.commands().count("exporter_restart"), 0)

    def test_a_run_that_died_after_arming_is_finalized_and_judged(self) -> None:
        armed = {"state": "armed", "running_before": True, "started_at_before": helper.STARTED_BEFORE}
        for took_effect in (False, True):
            with self.subTest(took_effect=took_effect):
                self.setUp()
                attempt = self.dead_run(f"contract-nonce-armed-{took_effect}", armed, took_effect)
                # The first read finalizes the dead claim; docker gets time to settle.
                self.assertEqual(self.broker.reconcile(attempt.execution_id).state, "unknown")
                self.assertEqual(self.harness.record(attempt.execution_id)["state"], "interrupted")
                self.clock.value += timedelta(minutes=3)
                settled = self.broker.reconcile(attempt.execution_id)
                self.assertEqual(settled.state, "succeeded" if took_effect else "failed", settled.result_detail)
                self.assertIn(
                    "restart took effect after execution_interrupted" if took_effect
                    else "execution_interrupted; restart did not take effect",
                    settled.result_detail,
                )
                # A late request with the same execution ID can never restart.
                replay = self.client.run("restart", attempt.execution_id)
                self.assertEqual(replay["state"], "interrupted")
                self.assertEqual(self.harness.docker.commands().count("exporter_restart"), 0)

    def test_an_armed_run_across_a_reboot_is_not_reported_as_taking_effect(self) -> None:
        armed = {"state": "armed", "running_before": True, "started_at_before": helper.STARTED_BEFORE,
                 "execution_boot_id": "0b5f7c1e-9d2a-4c1b-8f3e-7a6d5c4b3a21"}
        attempt = self.dead_run("contract-nonce-reboot", armed, took_effect=True)
        self.broker.reconcile(attempt.execution_id)
        self.clock.value += timedelta(minutes=3)
        settled = self.broker.reconcile(attempt.execution_id)
        self.assertEqual(settled.state, "failed", settled.result_detail)
        self.assertIn("target rebooted before the restart could be confirmed", settled.result_detail)

    def test_helper_refusal_is_refused_without_restart(self) -> None:
        proposal = build_proposal(
            self.adapter.status(), helper.BLOCKED_BDF,
            policy_revision="monitor-restart-r1", clock=self.clock,
        )
        self.broker.submit_proposal(proposal)
        self.backups[proposal.proposal_id] = "terracompute-backup.service@1789620000"
        self.broker.record_human_event(HumanApprovalEvent(
            event_id="telegram:contract:2", kind=ApprovalKind.APPROVE, group_id=GROUP,
            user_id=4242, display_name="telegram:4242", proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest, nonce="contract-nonce-2", occurred_at=self.clock(),
        ))
        original = self.client.run

        def refuse_on_restart(operation: str, request_id: str) -> dict:
            if operation == "restart":
                self.harness.hostname = "somewhere-else\n"
            return original(operation, request_id)

        self.client.run = refuse_on_restart
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "refused", attempt.result_detail)
        self.assertEqual(attempt.result_detail, "identity_mismatch")
        self.assertEqual(self.harness.docker.commands().count("exporter_restart"), 0)


if __name__ == "__main__":
    unittest.main()
