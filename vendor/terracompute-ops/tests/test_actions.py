from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from terracompute_ops.actions import (
    ActionBroker,
    ApprovalKind,
    DispatchResult,
    DispatchStatus,
    DryRunActionAdapter,
    FakeActionAdapter,
    HumanApprovalEvent,
    EventAuthentication,
    MembershipDecision,
)
from terracompute_ops.policy import (
    ActionClass,
    ActionPolicy,
    ActionProposal,
    Mode,
    Ownership,
    PolicyDenied,
    PreActionEvidence,
    RentalImpact,
    SourceBinding,
    SourceState,
)


START = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
GROUP = -4242


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class FakeMembership:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.current = True
        self.human = True
        self.independent = True
        self.calls: list[tuple[int, int]] = []

    def verify(self, group_id: int, user_id: int) -> MembershipDecision:
        self.calls.append((group_id, user_id))
        return MembershipDecision(
            group_id, user_id, self.current, self.human, self.independent, self.clock()
        )


class FakeAuthenticator:
    def __init__(self) -> None:
        self.authenticated = True

    def authenticate(self, event: HumanApprovalEvent) -> EventAuthentication:
        return EventAuthentication(
            event.event_id, event.group_id, event.user_id, self.authenticated
        )


def make_proposal(
    clock: Clock,
    proposal_id: str,
    *,
    action_class: ActionClass = ActionClass.GPU_RESET_REBIND,
    resource: str = "GPU-1",
    rentals: tuple[RentalImpact, ...] = (),
) -> ActionProposal:
    sources = {
        ActionClass.GPU_RESET_REBIND: ("host", "inventory", "rentals"),
        ActionClass.HOST_REBOOT: ("host", "bmc", "rentals"),
        ActionClass.BMC_POWER: ("bmc", "rentals"),
    }.get(action_class, ("host", "rentals"))
    power = action_class is ActionClass.BMC_POWER
    return ActionProposal.create(
        proposal_id=proposal_id,
        action_class=action_class,
        parameters={"operation": "reset", "target": resource},
        resource_ids=(resource,),
        rental_impacts=rentals,
        affected_domains=(f"shared:{resource}",),
        source_bindings=tuple(SourceBinding(name, f"{name}-r1") for name in sources),
        evidence_revision=f"evidence:{proposal_id}",
        policy_revision="policy-r1",
        stop_condition="stop unless the exact postcondition is verified",
        clock=clock,
        mappings_known=True,
        power_domain_proven=power,
    )


def preflight(proposal: ActionProposal, clock: Clock, **changes) -> PreActionEvidence:
    values = dict(
        machine_id=17049,
        target_identity_verified=True,
        evidence_revision=proposal.evidence_revision,
        sources=tuple(
            SourceState(binding.source, binding.revision, clock())
            for binding in proposal.source_bindings
        ),
        resource_ids=proposal.resource_ids,
        rental_impacts=proposal.rental_impacts,
        affected_domains=proposal.affected_domains,
        mappings_known=proposal.mappings_known,
        power_domain_proven=proposal.power_domain_proven,
        evidence_ref=f"pre:{proposal.proposal_id}",
        backup_ref=f"backup:{proposal.proposal_id}",
        backup_succeeded=True,
    )
    values.update(changes)
    return PreActionEvidence(**values)


def policy(*classes: ActionClass) -> ActionPolicy:
    return ActionPolicy(
        mode=Mode.APPROVE,
        revision="policy-r1",
        enabled_actions=frozenset(classes),
        approval_group_id=GROUP,
    )


def event(
    proposal: ActionProposal,
    clock: Clock,
    *,
    event_id: str | None = None,
    nonce: str | None = None,
    kind: ApprovalKind = ApprovalKind.APPROVE,
    **changes,
) -> HumanApprovalEvent:
    values = dict(
        event_id=event_id or f"event:{proposal.proposal_id}:{kind.value}",
        kind=kind,
        group_id=GROUP,
        user_id=1001,
        display_name="Test Operator",
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.digest,
        nonce=nonce or f"nonce:{proposal.proposal_id}:{kind.value}",
        occurred_at=clock(),
    )
    values.update(changes)
    return HumanApprovalEvent(**values)


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uri = f"file:actions-{uuid.uuid4()}?mode=memory&cache=shared"
        self.clock = Clock()
        self.member = FakeMembership(self.clock)
        self.authenticator = FakeAuthenticator()
        self.db = sqlite3.connect(self.uri, uri=True)
        self.adapter = FakeActionAdapter(lambda value: preflight(value, self.clock))
        self.broker = ActionBroker(
            self.db,
            policy=policy(ActionClass.GPU_RESET_REBIND),
            membership=self.member,
            authenticator=self.authenticator,
            adapter=self.adapter,
            clock=self.clock,
            execution_id_factory=lambda: "execution-1",
        )

    def tearDown(self) -> None:
        self.db.close()

    def approve(self, proposal: ActionProposal) -> None:
        self.broker.submit_proposal(proposal)
        self.broker.record_human_event(event(proposal, self.clock))

    def test_constructor_creates_namespaced_tables_without_changing_user_version(self) -> None:
        self.db.execute("PRAGMA user_version = 73")
        self.db.commit()
        ActionBroker(
            self.db, membership=self.member, authenticator=self.authenticator,
            clock=self.clock,
        )
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 73)
        tables = {
            row[0]
            for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("tc_action_attempts", tables)
        self.assertFalse(any(name.startswith("actions_") for name in tables))

    def test_unconfigured_and_observe_modes_fail_before_consuming_approval(self) -> None:
        proposal = make_proposal(self.clock, "p-observe")
        observe = ActionBroker(
            self.db, membership=self.member, authenticator=self.authenticator,
            clock=self.clock,
        )
        observe.submit_proposal(proposal)
        with self.assertRaisesRegex(PolicyDenied, "observe-only"):
            observe.execute(proposal.proposal_id)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM tc_action_attempts").fetchone()[0], 0
        )

    def test_exact_approval_executes_once_and_records_before_after_evidence(self) -> None:
        proposal = make_proposal(self.clock, "p1")
        self.approve(proposal)
        result = self.broker.execute(proposal.proposal_id)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.pre_evidence_ref, "pre:p1")
        self.assertEqual(result.post_evidence_ref, "post:execution-1")
        self.assertEqual(len(self.member.calls), 2)  # approval and execution checks
        with self.assertRaisesRegex(PolicyDenied, "already consumed"):
            self.broker.execute(proposal.proposal_id)
        self.assertEqual(len(self.adapter.dispatches), 1)

    def test_malicious_adapter_cannot_mutate_nested_approved_parameters(self) -> None:
        proposal = replace(
            make_proposal(self.clock, "nested-immutable"),
            parameters={"plan": [{"target": "GPU-1", "force": False}]},
        )

        class MutatingAdapter(FakeActionAdapter):
            def __init__(self, capture):
                super().__init__(capture)
                self.mutation_failures = 0
                self.dispatched_parameters = None

            def _attempt_mutation(self, value: ActionProposal) -> None:
                try:
                    value.parameters["plan"][0]["target"] = "GPU-2"
                except TypeError:
                    self.mutation_failures += 1

            def validate(self, value: ActionProposal) -> None:
                self._attempt_mutation(value)

            def preflight(self, value: ActionProposal) -> PreActionEvidence:
                self._attempt_mutation(value)
                return super().preflight(value)

            def dispatch(
                self, value: ActionProposal, execution_id: str
            ) -> DispatchResult:
                self.dispatched_parameters = value.exact_document()["parameters"]
                return super().dispatch(value, execution_id)

        adapter = MutatingAdapter(lambda value: preflight(value, self.clock))
        self.broker.adapter = adapter
        self.approve(proposal)

        result = self.broker.execute(proposal.proposal_id)

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(adapter.mutation_failures, 2)
        self.assertEqual(
            adapter.dispatched_parameters,
            {"plan": [{"target": "GPU-1", "force": False}]},
        )

    def test_durable_proposal_digest_is_revalidated_before_adapter_calls(self) -> None:
        proposal = make_proposal(self.clock, "tampered-document")
        self.approve(proposal)
        document = proposal.exact_document()
        document["parameters"]["target"] = "GPU-2"
        self.db.execute(
            "UPDATE tc_action_proposals SET document_json = ? WHERE proposal_id = ?",
            (json.dumps(document), proposal.proposal_id),
        )
        self.db.commit()

        with self.assertRaisesRegex(PolicyDenied, "stored proposal digest"):
            self.broker.execute(proposal.proposal_id)
        self.assertEqual(self.adapter.dispatches, [])

    def test_approval_digest_mismatch_acknowledgment_and_inauthentic_sender_rejected(self) -> None:
        variants = (
            (dict(proposal_digest="0" * 64), "exact proposal"),
            (dict(kind=ApprovalKind.ACKNOWLEDGE), "not approval"),
            (dict(group_id=-999), "wrong group"),
            (dict(sender_is_bot=True), "identifiable human"),
            (dict(sender_is_anonymous=True), "identifiable human"),
            (dict(chat_migrated=True), "migrated"),
            (dict(user_id=None), "identifiable human"),
        )
        for index, (changes, message) in enumerate(variants):
            with self.subTest(changes=changes):
                proposal = make_proposal(self.clock, f"reject-{index}")
                self.broker.submit_proposal(proposal)
                attempt = event(proposal, self.clock, event_id=f"bad-{index}", **changes)
                with self.assertRaisesRegex(PolicyDenied, message):
                    self.broker.record_human_event(attempt)

    def test_nonce_replay_is_rejected_across_proposals(self) -> None:
        first = make_proposal(self.clock, "nonce-1")
        second = make_proposal(self.clock, "nonce-2", resource="GPU-2")
        self.broker.submit_proposal(first)
        self.broker.submit_proposal(second)
        self.broker.record_human_event(event(first, self.clock, nonce="shared-nonce"))
        with self.assertRaisesRegex(PolicyDenied, "already used"):
            self.broker.record_human_event(
                event(second, self.clock, event_id="second-event", nonce="shared-nonce")
            )

    def test_raw_event_without_trusted_ingress_authentication_cannot_approve(self) -> None:
        proposal = make_proposal(self.clock, "unauthenticated")
        self.broker.submit_proposal(proposal)
        self.authenticator.authenticated = False
        with self.assertRaisesRegex(PolicyDenied, "did not authenticate"):
            self.broker.record_human_event(event(proposal, self.clock))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM tc_action_approvals").fetchone()[0], 0
        )

    def test_membership_is_independently_rechecked_and_change_blocks_execution(self) -> None:
        proposal = make_proposal(self.clock, "membership")
        self.approve(proposal)
        self.member.current = False
        with self.assertRaisesRegex(PolicyDenied, "currently verified"):
            self.broker.execute(proposal.proposal_id)
        self.assertEqual(len(self.adapter.dispatches), 0)
        row = self.db.execute(
            "SELECT consumed_execution_id FROM tc_action_approvals WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
        self.assertIsNone(row[0])

    def test_five_minute_expiry_blocks_dispatch_without_consumption(self) -> None:
        proposal = make_proposal(self.clock, "expired")
        self.approve(proposal)
        self.clock.advance(minutes=5)
        with self.assertRaisesRegex(PolicyDenied, "expired"):
            self.broker.execute(proposal.proposal_id)
        self.assertEqual(self.adapter.dispatches, [])

    def test_stale_or_changed_preflight_fails_before_approval_consumption(self) -> None:
        for index, mutation in enumerate(("stale", "changed")):
            proposal = make_proposal(self.clock, f"pre-{index}", resource=f"GPU-{index}")
            self.approve(proposal)
            if mutation == "stale":
                def bad(value, now=self.clock()):
                    base = preflight(value, self.clock)
                    return replace(
                        base,
                        sources=tuple(
                            replace(item, observed_at=now - timedelta(seconds=61))
                            for item in base.sources
                        ),
                    )
            else:
                def bad(value):
                    base = preflight(value, self.clock)
                    return replace(base, evidence_revision="changed")
            self.broker.adapter = FakeActionAdapter(bad)
            with self.assertRaises(PolicyDenied):
                self.broker.execute(proposal.proposal_id)
            consumed = self.db.execute(
                "SELECT consumed_execution_id FROM tc_action_approvals WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchone()[0]
            self.assertIsNone(consumed)

    def test_unknown_rental_and_active_unapproved_impact_never_reach_adapter(self) -> None:
        cases = (
            RentalImpact("r1", True, Ownership.UNKNOWN, True),
            RentalImpact("r2", True, Ownership.TENANT, False),
        )
        for index, rental in enumerate(cases):
            proposal = make_proposal(self.clock, f"rental-{index}", rentals=(rental,))
            self.broker.submit_proposal(proposal)
            with self.assertRaises(PolicyDenied):
                self.broker.record_human_event(event(proposal, self.clock))
        self.assertEqual(self.adapter.dispatches, [])

    def test_backup_exception_is_separate_exact_human_event_and_consumed(self) -> None:
        proposal = make_proposal(self.clock, "backup-ex")
        self.broker.submit_proposal(proposal)
        self.broker.record_human_event(event(proposal, self.clock))
        self.broker.record_human_event(
            event(
                proposal,
                self.clock,
                kind=ApprovalKind.BACKUP_EXCEPTION,
                event_id="backup-exception-event",
                nonce="backup-exception-nonce",
            )
        )
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock, backup_succeeded=False, backup_ref=None)
        )
        result = self.broker.execute(proposal.proposal_id)
        self.assertEqual(result.state, "succeeded")
        consumed = self.db.execute(
            """SELECT consumed_execution_id FROM tc_action_backup_exceptions
               WHERE proposal_id = ?""",
            (proposal.proposal_id,),
        ).fetchone()[0]
        self.assertEqual(consumed, "execution-1")

    def test_dispatch_exception_is_unknown_locked_and_never_blindly_retried(self) -> None:
        proposal = make_proposal(self.clock, "timeout")
        self.approve(proposal)
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock), fail_dispatch=TimeoutError()
        )
        result = self.broker.execute(proposal.proposal_id)
        self.assertEqual(result.state, "unknown")
        self.assertGreater(
            self.db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0
        )
        with self.assertRaises(PolicyDenied):
            self.broker.execute(proposal.proposal_id)

    def test_adapter_details_are_redacted_before_durable_audit(self) -> None:
        proposal = make_proposal(self.clock, "redaction")
        self.approve(proposal)
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock),
            dispatch_status=DispatchStatus.UNKNOWN,
        )
        self.broker.adapter.dispatch = lambda value, execution_id: DispatchResult(
            execution_id, DispatchStatus.UNKNOWN, "Bearer do-not-store"
        )
        result = self.broker.execute(proposal.proposal_id)
        durable = self.db.execute(
            "SELECT result_detail FROM tc_action_attempts WHERE execution_id = ?",
            (result.execution_id,),
        ).fetchone()[0]
        self.assertNotIn("do-not-store", durable)
        self.assertIn("REDACTED", durable)

    def test_reconciliation_uses_execution_id_and_releases_only_on_known_result(self) -> None:
        proposal = make_proposal(self.clock, "reconcile")
        self.approve(proposal)
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock),
            dispatch_status=DispatchStatus.UNKNOWN,
            reconciled_status=DispatchStatus.SUCCEEDED,
        )
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "unknown")
        result = self.broker.reconcile(attempt.execution_id)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(self.broker.adapter.reconciliations, [attempt.execution_id])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0
        )

    def test_postcondition_false_or_exception_stops_without_success(self) -> None:
        for index, settings in enumerate(
            ({"postcondition_ok": False}, {"fail_postflight": OSError("lost")})
        ):
            proposal = make_proposal(self.clock, f"post-{index}", resource=f"GPU-{index}")
            self.approve(proposal)
            self.broker.execution_id_factory = lambda index=index: f"post-execution-{index}"
            self.broker.adapter = FakeActionAdapter(
                lambda value: preflight(value, self.clock), **settings
            )
            result = self.broker.execute(proposal.proposal_id)
            self.assertEqual(result.state, "postcondition-failed")

    def test_dry_run_adapter_never_claims_success(self) -> None:
        proposal = make_proposal(self.clock, "dry")
        self.approve(proposal)
        self.broker.adapter = DryRunActionAdapter(lambda value: preflight(value, self.clock))
        self.assertEqual(self.broker.execute(proposal.proposal_id).state, "dry-run")

    def test_two_connections_contend_on_persistent_machine_lock(self) -> None:
        first = make_proposal(self.clock, "connection-1", resource="GPU-1")
        self.approve(first)
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock), dispatch_status=DispatchStatus.UNKNOWN
        )
        self.broker.execute(first.proposal_id)

        second_db = sqlite3.connect(self.uri, uri=True)
        second_member = FakeMembership(self.clock)
        second = ActionBroker(
            second_db,
            policy=policy(ActionClass.GPU_RESET_REBIND),
            membership=second_member,
            authenticator=self.authenticator,
            adapter=FakeActionAdapter(lambda value: preflight(value, self.clock)),
            clock=self.clock,
        )
        try:
            proposal = make_proposal(self.clock, "connection-2", resource="GPU-2")
            second.submit_proposal(proposal)
            second.record_human_event(event(proposal, self.clock))
            with self.assertRaisesRegex(PolicyDenied, "locked"):
                second.execute(proposal.proposal_id)
        finally:
            second_db.close()

    def test_actual_two_connection_race_allows_one_dispatch(self) -> None:
        # Each broker reaches preflight; BEGIN IMMEDIATE and the unique machine
        # lock serialize the decision. The winner stays unknown and locked.
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        brokers = []
        connections = []
        for index in (1, 2):
            connection = sqlite3.connect(
                self.uri, uri=True, timeout=2, check_same_thread=False
            )
            connections.append(connection)
            local_member = FakeMembership(self.clock)
            proposal = make_proposal(self.clock, f"race-{index}", resource=f"GPU-{index}")

            def racing_preflight(value: ActionProposal) -> PreActionEvidence:
                barrier.wait(timeout=2)
                return preflight(value, self.clock)

            broker = ActionBroker(
                connection,
                policy=policy(ActionClass.GPU_RESET_REBIND),
                membership=local_member,
                authenticator=self.authenticator,
                adapter=FakeActionAdapter(
                    racing_preflight, dispatch_status=DispatchStatus.UNKNOWN
                ),
                clock=self.clock,
                execution_id_factory=lambda index=index: f"race-execution-{index}",
            )
            broker.submit_proposal(proposal)
            broker.record_human_event(event(proposal, self.clock))
            brokers.append((broker, proposal))

        def runner(broker: ActionBroker, proposal: ActionProposal) -> None:
            try:
                outcomes.append(broker.execute(proposal.proposal_id).state)
            except PolicyDenied:
                outcomes.append("denied")

        threads = [threading.Thread(target=runner, args=item) for item in brokers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, ["unknown", "denied"])
        for connection in connections:
            connection.close()

    def test_restart_recovery_marks_dispatch_boundary_unknown_and_keeps_lock(self) -> None:
        proposal = make_proposal(self.clock, "restart")
        self.approve(proposal)
        self.broker.adapter = FakeActionAdapter(
            lambda value: preflight(value, self.clock), dispatch_status=DispatchStatus.UNKNOWN
        )
        result = self.broker.execute(proposal.proposal_id)
        self.db.execute(
            "UPDATE tc_action_attempts SET state = 'dispatching' WHERE execution_id = ?",
            (result.execution_id,),
        )
        self.db.commit()
        durable_image = self.db.serialize()
        self.db.close()
        self.db = sqlite3.connect(":memory:")
        self.db.deserialize(durable_image)
        restarted = ActionBroker(
            self.db,
            policy=self.broker.policy,
            membership=self.member,
            authenticator=self.authenticator,
            adapter=self.broker.adapter,
            clock=self.clock,
        )
        self.assertEqual(restarted.recover_interrupted_attempts(), (result.execution_id,))
        self.assertEqual(restarted.get_attempt(result.execution_id).state, "unknown")
        self.assertGreater(
            self.db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0
        )

    def test_repeat_cooldown_blocks_same_class_resource(self) -> None:
        first = make_proposal(self.clock, "cooldown-1")
        self.approve(first)
        self.broker.execute(first.proposal_id)
        self.clock.advance(minutes=10)
        second = make_proposal(self.clock, "cooldown-2")
        self.approve(second)
        self.broker.execution_id_factory = lambda: "execution-2"
        with self.assertRaisesRegex(PolicyDenied, "30-minute"):
            self.broker.execute(second.proposal_id)

    def test_reboot_power_cooldown_and_combined_daily_limit(self) -> None:
        power_policy = policy(ActionClass.HOST_REBOOT, ActionClass.BMC_POWER)
        self.broker.policy = power_policy
        for index, action_class in enumerate(
            (ActionClass.HOST_REBOOT, ActionClass.BMC_POWER)
        ):
            proposal = make_proposal(
                self.clock, f"power-{index}", action_class=action_class,
                resource=f"host-{index}",
            )
            self.approve(proposal)
            self.broker.execution_id_factory = lambda index=index: f"power-execution-{index}"
            self.broker.execute(proposal.proposal_id)
            self.clock.advance(minutes=61)
        third = make_proposal(
            self.clock, "power-3", action_class=ActionClass.HOST_REBOOT, resource="host-3"
        )
        self.approve(third)
        with self.assertRaisesRegex(PolicyDenied, "two per 24 hours"):
            self.broker.execute(third.proposal_id)


if __name__ == "__main__":
    unittest.main()
