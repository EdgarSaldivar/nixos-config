"""Phase 5 contract/state-machine tests; all external interfaces are fakes."""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from terracompute_ops.independent_verification import (
    AUTHENTICATOR_CONTRACT, SOURCE_CONTRACT, DurableVerificationBreaker,
    IndependentVerificationCoordinator, Postcondition, PostconditionKind,
    PostconditionResult, TypedIndependentVerifier, VerificationFoundationError,
    build_independent_verification,
)
from terracompute_ops.plan_authorization import ApprovalDecision, PlanAuthorizationService
from terracompute_ops.plans import VerificationStatus, stable_hash, utc_text
from terracompute_ops.step_executor import Domain, ExecutionBlocked, Outcome, StepExecutor
from terracompute_ops.tasks import Task, TaskState, TaskStore
from test_plan_authorization import EVIDENCE, GROUP, NOW, POLICY, FakeMembership, FakeResolver, plan, rental
from test_step_executor import Crash, Worker, executable


def condition_documents():
    return (
        {"check": "machine_reachability", "machine_id": "17049", "max_age_seconds": 120,
         "vantage": "controller", "protocol": "tcp", "endpoint": "host:22"},
        {"check": "network", "machine_id": "17049", "max_age_seconds": 120,
         "interface": "enp8s0", "expected_address": "198.51.100.10/24",
         "expected_route": "default-via-198.51.100.1"},
        {"check": "gpu_inventory", "machine_id": "17049", "max_age_seconds": 120,
         "expected_count": 2, "expected_pci_ids": ["0000:01:00.0", "0000:02:00.0"]},
        {"check": "exporter_health", "machine_id": "17049", "max_age_seconds": 120,
         "exporter": "dcgm", "endpoint": "controller-proxy:9400/metrics"},
        {"check": "metrics_cardinality", "machine_id": "17049", "max_age_seconds": 120,
         "metric": "DCGM_FI_DEV_GPU_UTIL", "label": "gpu", "minimum": 2, "maximum": 2},
        {"check": "vast_availability", "machine_id": "17049", "max_age_seconds": 120,
         "expected_available": True},
        {"check": "rental_continuity", "machine_id": "17049", "max_age_seconds": 120,
         "rental_id": "51217040", "generation": "generation-1", "expected_state": "running"},
        {"check": "controller_health", "machine_id": "17049", "max_age_seconds": 120,
         "service": "terracompute-controller", "expected_revision": "revision-1"},
    )


class Authenticator:
    contract = AUTHENTICATOR_CONTRACT

    def __init__(self):
        self.signed_digests = set()

    def authenticate(self, result, authenticated_digest):
        return authenticated_digest in self.signed_digests


class Source:
    contract = SOURCE_CONTRACT
    source_id = "independent-probe"
    credential_domains = frozenset()

    def __init__(self, clock, authenticator):
        self.clock = clock
        self.authenticator = authenticator
        self.outcome = VerificationStatus.SUCCEEDED
        self.requests = []
        self.observed_at = None
        self.mutate = lambda result: result

    def observe(self, request):
        self.requests.append(request)
        results = tuple(PostconditionResult(
            **request.binding,
            check=condition.kind, condition_hash=condition.content_hash,
            outcome=self.outcome, evidence_revision=request.evidence_revision,
            source_id=self.source_id, observed_at=self.observed_at or self.clock(),
            evidence_digest=stable_hash({
                "source": self.source_id, "condition": condition.content_hash,
                "lease": request.lease_hash, "outcome": self.outcome.value,
            }),
        ) for condition in request.conditions)
        self.authenticator.signed_digests.update(result.authentication_digest for result in results)
        return tuple(self.mutate(result) for result in results)


class ContractTests(unittest.TestCase):
    def test_all_eight_postcondition_schemas_round_trip_strictly(self):
        parsed = tuple(Postcondition.from_document(item) for item in condition_documents())
        self.assertEqual({item.kind for item in parsed}, set(PostconditionKind))
        self.assertEqual(tuple(item.to_document() for item in parsed), condition_documents())
        for document in condition_documents():
            with self.subTest(check=document["check"]):
                malformed = dict(document, extra="not-allowed")
                with self.assertRaises(VerificationFoundationError):
                    Postcondition.from_document(malformed)

    def test_schema_rejects_wrong_machine_and_inconsistent_gpu_inventory(self):
        wrong = dict(condition_documents()[0], machine_id="17050")
        with self.assertRaisesRegex(VerificationFoundationError, "17049"):
            Postcondition.from_document(wrong)
        gpu = dict(condition_documents()[2], expected_count=8)
        with self.assertRaisesRegex(VerificationFoundationError, "disagree"):
            Postcondition.from_document(gpu)

    def test_phase5_factory_is_disabled_and_fails_closed_when_enabled(self):
        self.assertIsNone(build_independent_verification({}))
        self.assertIsNone(build_independent_verification(
            {"TERRACOMPUTE_INDEPENDENT_VERIFICATION": "true"}
        ))
        with self.assertRaises(VerificationFoundationError):
            build_independent_verification({"TERRACOMPUTE_INDEPENDENT_VERIFICATION": "1"})


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TaskStore(
            Path(temporary.name) / "state.sqlite", clock=lambda: self.now,
            policy_revision=lambda: POLICY,
        )
        self.addCleanup(self.store.close)
        self.resolver = FakeResolver({"C.51217040": (rental(),), "51217040": (rental(),)})
        self.auth = PlanAuthorizationService(
            self.store.db, membership=FakeMembership(), resolver=self.resolver,
            policy_revision=POLICY, approval_group_id=GROUP, clock=lambda: self.now,
        )
        self.workers = {domain: Worker(domain) for domain in Domain}
        self.breaker = DurableVerificationBreaker(self.store)
        self.authenticator = Authenticator()
        self.verifier = TypedIndependentVerifier(
            self.authenticator, source_id="independent-probe", breaker=self.breaker,
        )
        self.allowed = True
        self.executor = StepExecutor(
            self.store, self.auth, self.workers, self.verifier,
            mutation_allowed=lambda: self.allowed,
            verification_allowed=self.breaker.mutation_allowed,
            rollback_allowed=self.breaker.rollback_allowed,
        )
        self.source = Source(lambda: self.now, self.authenticator)
        self.identifiers = iter(f"verification-{number}" for number in range(20))
        self.coordinator = IndependentVerificationCoordinator(
            self.store, self.executor, self.source, self.breaker,
            id_factory=lambda: next(self.identifiers),
        )

    def register(self, item):
        self.document = plan(item if isinstance(item, tuple) else (item,))
        self.store.create_task(Task(
            task_id="task-1", requester_id="operator:1", requester_group_id=GROUP,
            origin_message_id="message-1", created_at=NOW, objective="repair machine",
            constraints=(), state=TaskState.INVESTIGATING, evidence_revision=EVIDENCE,
            model_thread=None, attempt_count=0, deadline=NOW + timedelta(hours=2),
            budgets={}, incident_ids=(),
        ))
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.PLANNING)
        self.store.add_plan(self.document)
        requirements = self.auth.issue_requirements(
            self.document, self.auth.authorize(self.document), requester_id="operator:1",
            current_evidence_revision=EVIDENCE,
        )
        for requirement in requirements:
            self.auth.membership.verified_at = self.now
            self.auth.record_decision(ApprovalDecision(
                requirement_id=requirement.requirement_id, card_hash=requirement.card_hash,
                nonce=requirement.nonce, group_id=GROUP, user_id=777,
                display_name="Operator", via_callback=True, occurred_at=self.now,
            ), current_evidence_revision=EVIDENCE)
        self.executor.register(
            self.document,
            {step_id: requirement.requirement_id for requirement in requirements
             for step_id in requirement.step_ids},
        )

    def typed_step(self, *, reachability=False, rollback=True):
        original = executable(
            domain=Domain.BMC if reachability else Domain.TARGET_HOST,
            rollback=rollback,
        )
        documents = condition_documents()[:2] if reachability else condition_documents()[2:5]
        arguments = dict(original.arguments)
        if reachability:
            hashes = [Postcondition.from_document(item).content_hash for item in documents]
            arguments["reachability_recovery"] = {
                "staged_apply": True, "deadline": utc_text(NOW + timedelta(seconds=30)),
                "health_confirmation": hashes,
                "out_of_band": {"available": True, "method": "bmc", "target": "bmc:17049"},
            }
        undo = None
        reason = "not reversible"
        if rollback:
            undo = {
                "operation": "run_shell", "arguments": arguments,
                "postconditions": list(documents),
            }
            reason = None
        return replace(
            original, arguments=arguments, postconditions=documents,
            rollback=undo, rollback_impossible_reason=reason,
        )

    def test_success_is_durable_and_worker_success_is_not_verification(self):
        self.register(self.typed_step())
        self.assertEqual(self.executor.advance("task-1"), "verifying")
        self.assertEqual(self.store.verifications("task-1"), ())
        self.assertEqual(self.coordinator.verify("task-1"), "succeeded")
        stored = self.store.verifications("task-1")[0]
        self.assertEqual(stored.status, VerificationStatus.SUCCEEDED)
        self.assertTrue(all("condition_hash" in item for item in stored.checks))
        self.assertFalse(self.breaker.is_open())

    def test_failed_verification_trips_breaker_and_runs_only_approved_rollback(self):
        self.register(self.typed_step(reachability=True))
        self.source.outcome = VerificationStatus.FAILED
        self.assertEqual(self.coordinator.advance("task-1"), "lease_wait")
        self.assertTrue(self.breaker.is_open())
        self.assertFalse(self.breaker.mutation_allowed())
        self.assertEqual(len(self.workers[Domain.BMC].calls), 1)
        self.now += timedelta(seconds=61)
        self.source.outcome = VerificationStatus.SUCCEEDED
        self.assertEqual(self.coordinator.advance("task-1"), "rolled_back")
        self.assertEqual(
            [request.phase for request in self.workers[Domain.BMC].calls],
            ["forward", "rollback"],
        )
        self.assertEqual(self.executor.snapshot("task-1")["status"], "rolled_back")
        self.assertTrue(self.breaker.is_open())

    def test_failure_without_approved_rollback_never_invents_recovery(self):
        self.register(self.typed_step(rollback=False))
        self.source.outcome = VerificationStatus.FAILED
        self.assertEqual(self.coordinator.advance("task-1"), "needs_replan")
        self.assertTrue(self.breaker.is_open())
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_uncertain_is_durable_and_does_not_trip_or_continue(self):
        self.register(self.typed_step())
        self.source.outcome = VerificationStatus.UNCERTAIN
        self.assertEqual(self.coordinator.advance("task-1"), "verifying")
        attempt = self.executor.snapshot("task-1")["attempts"][-1]
        self.assertEqual(attempt["verifications"][-1]["status"], "uncertain")
        self.assertFalse(self.breaker.is_open())
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_stale_or_wrong_attempt_evidence_cannot_trip_breaker(self):
        self.register(self.typed_step())
        self.assertEqual(self.executor.advance("task-1"), "verifying")
        self.source.outcome = VerificationStatus.FAILED
        self.source.mutate = lambda result: replace(result, condition_hash="a" * 64)
        with self.assertRaisesRegex(VerificationFoundationError, "authentication"):
            self.coordinator.verify("task-1")
        self.assertFalse(self.breaker.is_open())
        self.assertEqual(self.store.verifications("task-1"), ())
        self.source.mutate = lambda result: result
        self.source.observed_at = NOW
        self.now += timedelta(seconds=121)
        with self.assertRaisesRegex(VerificationFoundationError, "authentication"):
            self.coordinator.verify("task-1")
        self.assertFalse(self.breaker.is_open())

    def test_reachability_change_requires_staging_deadline_health_and_oob(self):
        item = self.typed_step(reachability=True)
        for field in ("staged_apply", "deadline", "health_confirmation", "out_of_band"):
            with self.subTest(field=field):
                arguments = dict(item.arguments)
                recovery = dict(arguments["reachability_recovery"])
                recovery.pop(field)
                arguments["reachability_recovery"] = recovery
                broken = replace(item, arguments=arguments)
                self.register(broken)
                with self.assertRaises(VerificationFoundationError):
                    self.coordinator.advance("task-1")
                self.assertEqual(self.workers[Domain.BMC].calls, [])
                if field != "out_of_band":
                    self.tearDown()
                    self.setUp()

    def test_deadline_fails_closed_without_calling_source_and_queues_rollback(self):
        self.register(self.typed_step(reachability=True))
        self.assertEqual(self.executor.advance("task-1"), "verifying")
        self.now = NOW + timedelta(seconds=31)
        self.assertEqual(self.coordinator.verify("task-1"), "lease_wait")
        self.assertEqual(self.source.requests, [])
        self.assertTrue(self.breaker.is_open())
        verification = self.store.verifications("task-1")[0]
        self.assertEqual(verification.status, VerificationStatus.FAILED)
        self.assertTrue(all(item["basis"] == "deadline" for item in verification.checks))

    def test_authenticated_results_bind_every_execution_field_and_pinned_source(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        request, _ = self.coordinator._request("task-1")
        originals = self.source.observe(request)
        changes = dict(task_id="other-task", plan_id="other-plan", plan_hash="a" * 64,
                       step_id="other-step", lease_id="other-lease", lease_hash="b" * 64,
                       phase="rollback", result_at=NOW - timedelta(seconds=1),
                       source_id="another-trusted-probe")
        for field, value in changes.items():
            with self.subTest(field=field):
                replay = tuple(replace(result, **{field: value}) for result in originals)
                # Validly authenticated evidence for another context still fails.
                self.authenticator.signed_digests.update(r.authentication_digest for r in replay)
                with patch.object(self.source, "observe", return_value=replay):
                    with self.assertRaisesRegex(VerificationFoundationError, "authentication"):
                        self.coordinator.verify("task-1")
                self.assertFalse(self.breaker.is_open())
                self.assertEqual(self.store.verifications("task-1"), ())

    def test_authentication_covers_context_outcome_time_and_evidence_digest(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        request, _ = self.coordinator._request("task-1")
        original = self.source.observe(request)[0]
        self.assertTrue(self.authenticator.authenticate(original, original.authentication_digest))
        for field, value in dict(
            task_id="other", plan_id="other", plan_hash="a" * 64, step_id="other",
            lease_id="other", lease_hash="b" * 64, phase="rollback",
            result_at=NOW + timedelta(seconds=1), source_id="other",
            observed_at=NOW + timedelta(seconds=1), outcome=VerificationStatus.FAILED,
            evidence_digest="c" * 64, evidence_revision="other", condition_hash="d" * 64,
        ).items():
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                self.assertFalse(self.authenticator.authenticate(changed, changed.authentication_digest))

    def test_forward_evidence_cannot_be_rewrapped_for_rollback_lease(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        forward, _ = self.coordinator._request("task-1")
        successful_forward = self.source.observe(forward)
        self.source.outcome = VerificationStatus.FAILED
        self.coordinator.verify("task-1")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.advance("task-1"), "verifying")
        rollback, _ = self.coordinator._request("task-1")
        for replay in (successful_forward, tuple(replace(r, **rollback.binding,
                                                        observed_at=self.now)
                                                for r in successful_forward)):
            with self.subTest(rewrapped=replay != successful_forward):
                with patch.object(self.source, "observe", return_value=replay):
                    with self.assertRaisesRegex(VerificationFoundationError, "authentication"):
                        self.coordinator.verify("task-1")
        self.assertEqual(self.executor.snapshot("task-1")["attempts"][-1]["state"], "applied")

    def test_deadline_evidence_binds_entire_context_and_cannot_replay_into_rollback(self):
        self.register(self.typed_step(reachability=True))
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        self.coordinator.verify("task-1")
        verification = self.store.verifications("task-1")[0]
        original = PostconditionResult.from_document(verification.checks[0])
        deadline = NOW + timedelta(seconds=30)
        self.assertEqual(original.deadline_digest(deadline), original.evidence_digest)
        for field, value in dict(task_id="other", plan_hash="a" * 64,
                                lease_id="other", lease_hash="b" * 64, phase="rollback",
                                result_at=NOW + timedelta(seconds=1)).items():
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                self.assertNotEqual(changed.deadline_digest(deadline), changed.evidence_digest)
        self.now = NOW + timedelta(seconds=61)
        self.executor.advance("task-1")
        rollback, _ = self.coordinator._request("task-1")
        replay = tuple(replace(PostconditionResult.from_document(item), **rollback.binding,
                               observed_at=self.now) for item in verification.checks)
        replay = tuple(replace(r, evidence_digest=r.deadline_digest(deadline)) for r in replay)
        with patch.object(self.source, "observe", return_value=replay):
            with self.assertRaisesRegex(VerificationFoundationError, "authentication"):
                self.coordinator.verify("task-1")

    def test_source_identity_must_match_pinned_configuration(self):
        self.source.source_id = "other-probe"
        with self.assertRaisesRegex(VerificationFoundationError, "pinned"):
            IndependentVerificationCoordinator(self.store, self.executor, self.source,
                                               self.breaker, id_factory=lambda: "unused")

    def test_direct_executor_recover_cannot_bypass_recovery_structure(self):
        item = self.typed_step(reachability=True)
        self.register(replace(item, arguments={"execution": item.arguments["execution"]}))
        self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(self.executor.snapshot("task-1")["attempts"], [])
        self.assertEqual(self.store.leases("task-1"), ())
        self.assertEqual(self.workers[Domain.BMC].calls, [])

    def test_expired_deadline_blocks_direct_intent_and_recover(self):
        self.register(self.typed_step(reachability=True))
        self.now += timedelta(seconds=31)
        with self.assertRaisesRegex(VerificationFoundationError, "live"):
            self.executor.advance("task-1")
        self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(self.executor.snapshot("task-1")["attempts"], [])

    def test_deadline_rechecked_after_intent_crash_and_inspection(self):
        self.register(self.typed_step(reachability=True))
        with patch.object(self.executor, "_dispatch", side_effect=Crash):
            with self.assertRaises(Crash):
                self.executor.advance("task-1")
        worker = self.workers[Domain.BMC]
        inspect = worker.inspect

        def cross_deadline(request):
            observation = inspect(request)
            self.now += timedelta(seconds=31)
            return observation

        with patch.object(worker, "inspect", side_effect=cross_deadline):
            self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.executor.snapshot("task-1")["attempts"][-1]["state"], "intent")
        self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(len(worker.inspections), 1)

    def test_automatic_continuation_checks_next_reachability_step(self):
        first = self.typed_step()
        second = replace(self.typed_step(reachability=True), step_id="step-2")
        self.register((first, second))
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        with self.assertRaisesRegex(VerificationFoundationError, "live"):
            self.coordinator.verify("task-1")
        self.assertEqual(len(self.store.verifications("task-1")), 1)
        self.assertEqual(self.executor.snapshot("task-1")["cursor"], 1)
        self.assertEqual(self.workers[Domain.BMC].calls, [])

    def test_later_expired_deadline_never_blocks_verification_or_approved_rollback(self):
        first = self.typed_step()
        second = replace(self.typed_step(reachability=True), step_id="step-2")
        self.register((first, second))
        self.executor.advance("task-1")
        self.now += timedelta(seconds=61)
        self.source.outcome = VerificationStatus.FAILED
        self.assertEqual(self.coordinator.advance("task-1"), "verifying")
        self.assertTrue(self.breaker.is_open())
        self.assertEqual([r.phase for r in self.workers[Domain.TARGET_HOST].calls],
                         ["forward", "rollback"])
        self.source.outcome = VerificationStatus.SUCCEEDED
        self.assertEqual(self.coordinator.advance("task-1"), "rolled_back")
        self.assertEqual(self.workers[Domain.BMC].calls, [])

    def assert_investigation(self):
        document = self.executor.snapshot("task-1")
        self.assertTrue(DurableVerificationBreaker(self.store).is_open())
        self.assertEqual(document["status"], "needs_investigation")
        self.assertEqual(document["rollback_queue"], [])
        self.assertTrue(document["investigation"]["out_of_band_needed"])
        self.assertEqual(document["investigation"]["effect_application"], "unproven")
        self.assertEqual(document["investigation"]["lease_id"], document["attempts"][-1]["lease_id"])
        self.assertEqual(document["effects"], [])
        self.assertEqual(self.executor.recover(), {"task-1": "needs_investigation"})
        self.assertEqual(len(self.workers[Domain.BMC].calls), 1)

    def test_unknown_dispatch_crossing_deadline_trips_and_records_investigation(self):
        self.register(self.typed_step(reachability=True))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        worker.hook = lambda request: setattr(self, "now", NOW + timedelta(seconds=31))
        self.assertEqual(self.executor.advance("task-1"), "needs_investigation")
        self.assert_investigation()

    def test_lost_result_dispatched_and_uncertain_deadlines_before_lease_expiry(self):
        for crashed in (True, False):
            with self.subTest(crashed=crashed):
                self.register(self.typed_step(reachability=True))
                worker = self.workers[Domain.BMC]
                worker.outcome = Outcome.UNKNOWN
                if crashed:
                    worker.hook = lambda request: (_ for _ in ()).throw(Crash())
                    with self.assertRaises(Crash):
                        self.executor.advance("task-1")
                else:
                    self.assertEqual(self.executor.advance("task-1"), "uncertain")
                self.now += timedelta(seconds=31)
                self.assertEqual(self.coordinator.advance("task-1"), "needs_investigation")
                self.assertEqual(worker.reconciliations, [])
                self.assert_investigation()
                if crashed:
                    self.setUp()

    def test_unknown_reconcile_crossing_deadline_records_investigation(self):
        item = self.typed_step(reachability=True)
        recovery = dict(item.arguments["reachability_recovery"],
                        deadline=utc_text(NOW + timedelta(seconds=90)))
        self.register(replace(item, arguments=dict(item.arguments, reachability_recovery=recovery)))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        self.assertEqual(self.executor.advance("task-1"), "uncertain")
        self.now += timedelta(seconds=61)
        reconcile = worker.reconcile

        def cross_deadline(request):
            self.now = NOW + timedelta(seconds=91)
            return reconcile(request)

        with patch.object(worker, "reconcile", side_effect=cross_deadline):
            self.assertEqual(self.executor.advance("task-1"), "needs_investigation")
        self.assert_investigation()

    def test_investigation_keeps_read_only_reconciliation_until_not_applied_is_fenced(self):
        self.register(self.typed_step(reachability=True))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        self.assertEqual(self.executor.advance("task-1"), "uncertain")
        self.now += timedelta(seconds=31)
        self.assertEqual(self.executor.advance("task-1"), "needs_investigation")
        self.assertEqual(worker.reconciliations, [])

        self.now += timedelta(seconds=31)
        worker.reconciled = Outcome.NOT_APPLIED
        self.assertEqual(self.executor.advance("task-1"), "needs_replan")
        self.assertEqual(len(worker.reconciliations), 1)
        document = self.executor.snapshot("task-1")
        self.assertEqual(document["attempts"][-1]["state"], "not_applied")
        self.assertEqual(document["investigation"]["resolution"], "not_applied_fenced")
        self.assertFalse(document["investigation"]["out_of_band_needed"])
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")

    def test_investigation_reconciliation_can_resume_independent_verification(self):
        self.register(self.typed_step(reachability=True))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        self.assertEqual(self.executor.advance("task-1"), "needs_investigation")

        self.now += timedelta(seconds=31)
        worker.reconciled = Outcome.APPLIED
        self.assertEqual(self.executor.advance("task-1"), "verifying")
        document = self.executor.snapshot("task-1")
        self.assertEqual(document["status"], "running")
        self.assertEqual(document["investigation"]["resolution"], "applied")
        self.assertEqual(document["attempts"][-1]["state"], "applied")
        # Application was only confirmed after the reachability deadline, so
        # independent verification fails closed and starts the exact approved
        # rollback. The rollback outcome is still unknown in this fake.
        self.assertEqual(self.coordinator.verify("task-1"), "uncertain")
        self.assertEqual(self.store.verifications("task-1")[0].status,
                         VerificationStatus.FAILED)
        self.assertTrue(self.breaker.is_open())
        self.assertEqual(self.executor.snapshot("task-1")["attempts"][-1]["phase"],
                         "rollback")

    def test_investigation_unknown_reconciliation_stays_fail_closed(self):
        self.register(self.typed_step(reachability=True))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        worker.reconciled = Outcome.UNKNOWN
        self.assertEqual(self.executor.advance("task-1"), "needs_investigation")
        self.assertEqual(len(worker.reconciliations), 1)
        self.assertEqual(self.executor.snapshot("task-1")["attempts"][-1]["state"], "uncertain")

    def test_deadline_investigation_commits_while_paused_and_with_expired_authority(self):
        self.register(self.typed_step(reachability=True))
        self.workers[Domain.BMC].outcome = Outcome.UNKNOWN
        self.executor.advance("task-1")
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.PAUSED)
        self.allowed = False
        self.now += timedelta(hours=3)
        self.assertEqual(self.executor.recover(), {"task-1": "needs_investigation"})
        self.assert_investigation()

    def test_deadline_investigation_and_breaker_commit_together(self):
        self.register(self.typed_step(reachability=True))
        self.workers[Domain.BMC].outcome = Outcome.UNKNOWN
        self.executor.advance("task-1")
        self.now += timedelta(seconds=31)
        with patch.object(self.executor, "_save", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.executor.advance("task-1")
        self.assertFalse(self.breaker.is_open())
        self.assertNotIn("investigation", self.executor.snapshot("task-1"))
        self.assertEqual(self.executor.recover(), {"task-1": "needs_investigation"})
        self.assert_investigation()

    def test_not_applied_cannot_retry_after_reachability_deadline(self):
        self.register(self.typed_step(reachability=True))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.NOT_APPLIED
        self.assertEqual(self.executor.advance("task-1"), "not_applied")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.recover(), {"task-1": "needs_replan"})
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(self.executor.snapshot("task-1")["attempts"]), 1)

    def test_post_deadline_not_applied_reconciliation_cannot_retry(self):
        item = self.typed_step(reachability=True)
        recovery = dict(item.arguments["reachability_recovery"],
                        deadline=utc_text(NOW + timedelta(seconds=90)))
        self.register(replace(item, arguments=dict(item.arguments, reachability_recovery=recovery)))
        worker = self.workers[Domain.BMC]
        worker.outcome = Outcome.UNKNOWN
        worker.reconciled = Outcome.NOT_APPLIED
        self.executor.advance("task-1")
        self.now += timedelta(seconds=61)
        reconcile = worker.reconcile

        def cross_deadline(request):
            self.now = NOW + timedelta(seconds=91)
            return reconcile(request)

        with patch.object(worker, "reconcile", side_effect=cross_deadline):
            self.assertEqual(self.executor.advance("task-1"), "needs_replan")
        self.assertEqual(self.executor.recover(), {"task-1": "needs_replan"})
        self.assertEqual(len(worker.calls), 1)

    def test_swapped_breaker_methods_rejected_at_setup_and_dispatch(self):
        self.register(self.typed_step())
        for gate, wrong in (("verification_allowed", self.breaker.rollback_allowed),
                            ("rollback_allowed", self.breaker.mutation_allowed)):
            with self.subTest(gate=gate), patch.object(self.executor, gate, wrong):
                with self.assertRaisesRegex(VerificationFoundationError, "exact breaker functions"):
                    IndependentVerificationCoordinator(self.store, self.executor, self.source,
                                                       self.breaker, id_factory=lambda: "unused")
                self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_rollback_never_bypasses_general_mutation_gate_or_task_pause(self):
        self.register(self.typed_step())
        self.source.outcome = VerificationStatus.FAILED
        self.coordinator.advance("task-1")
        self.now += timedelta(seconds=61)
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "general mutation"):
            self.executor.advance("task-1")
        self.allowed = True
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.PAUSED)
        with self.assertRaisesRegex(ExecutionBlocked, "terminal state"):
            self.executor.advance("task-1")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)
        self.assertTrue(self.breaker.is_open())

    def test_failure_breaker_and_evidence_ingestion_are_atomic(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        self.source.outcome = VerificationStatus.FAILED
        original_save = self.executor._save

        def fail_ingestion(document, event):
            if event == "executor-verification-received":
                raise RuntimeError("simulated durable ingestion failure")
            return original_save(document, event)

        with patch.object(self.executor, "_save", side_effect=fail_ingestion):
            with self.assertRaises(RuntimeError):
                self.coordinator.verify("task-1")
        self.assertFalse(self.breaker.is_open())
        self.assertNotIn("verifications", self.executor.snapshot("task-1")["attempts"][-1])
        self.coordinator.verify("task-1")
        self.assertTrue(self.breaker.is_open())
        self.assertEqual(len(self.store.verifications("task-1")), 1)

    def test_rejected_final_ingestion_does_not_trip_breaker(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        self.source.outcome = VerificationStatus.FAILED
        with patch.object(self.verifier, "validate", side_effect=[True, False]):
            with self.assertRaisesRegex(ExecutionBlocked, "validation"):
                self.coordinator.verify("task-1")
        self.assertFalse(self.breaker.is_open())
        self.assertNotIn("verifications", self.executor.snapshot("task-1")["attempts"][-1])

    def test_failure_survives_crash_between_evidence_commit_and_continuation(self):
        self.register(self.typed_step())
        self.executor.advance("task-1")
        self.source.outcome = VerificationStatus.FAILED
        with patch.object(self.executor, "advance", side_effect=Crash):
            with self.assertRaises(Crash):
                self.coordinator.verify("task-1")
        self.assertTrue(self.breaker.is_open())
        self.assertIn("pending_verification", self.executor.snapshot("task-1")["attempts"][-1])
        self.assertEqual(self.executor.recover(), {"task-1": "lease_wait"})
        self.assertEqual(len(self.breaker._events()), 1)
        self.assertEqual(len(self.store.verifications("task-1")), 1)

    def test_concurrent_failures_from_separate_connections_are_both_journaled(self):
        path = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        barrier = Barrier(2)

        def trip(number):
            store = TaskStore(path, clock=lambda: NOW, policy_revision=lambda: POLICY)
            try:
                breaker = DurableVerificationBreaker(store)
                barrier.wait(timeout=10)
                return breaker.trip(task_id=f"task-{number}", plan_hash="a" * 64,
                                    lease_hash=str(number) * 64, reason="concurrent-failure")
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(trip, (1, 2)))
        self.assertEqual(results, [True, True])
        self.assertEqual({e["task_id"] for e in self.breaker._events()}, {"task-1", "task-2"})
        self.assertTrue(self.breaker.is_open())

    def test_distinct_failures_are_journaled_while_breaker_is_open(self):
        for task, lease in (("task-1", "b" * 64), ("task-2", "c" * 64)):
            self.assertTrue(self.breaker.trip(task_id=task, plan_hash="a" * 64,
                                              lease_hash=lease, reason="failed"))
        self.assertFalse(self.breaker.trip(task_id="task-2", plan_hash="a" * 64,
                                           lease_hash="c" * 64, reason="failed"))
        self.assertEqual([e["task_id"] for e in self.breaker._events()], ["task-1", "task-2"])
        self.assertTrue(self.breaker.is_open())

    def test_out_of_band_target_requires_explicit_scheme_and_exact_machine(self):
        item = self.typed_step(reachability=True)
        for target in ("17049", ":17049", "bmc:other:17049", "bmc:17050", "bmc://17049"):
            with self.subTest(target=target):
                recovery = dict(item.arguments["reachability_recovery"])
                recovery["out_of_band"] = dict(recovery["out_of_band"], target=target)
                malformed = replace(item, arguments=dict(item.arguments, reachability_recovery=recovery))
                with self.assertRaisesRegex(VerificationFoundationError, "17049"):
                    self.verifier.dispatch_precondition(plan((malformed,)), malformed, "forward")

    def test_breaker_is_durable_tamper_evident_and_reset_needs_identity(self):
        self.breaker.trip(
            task_id="task-1", plan_hash="a" * 64, lease_hash="b" * 64, reason="test-failure"
        )
        reopened = DurableVerificationBreaker(self.store)
        self.assertTrue(reopened.is_open())
        with self.assertRaises(VerificationFoundationError):
            reopened.reset(approval_identity="", reason="operator reset")
        self.assertTrue(reopened.reset(approval_identity="operator:777", reason="reviewed"))
        self.assertTrue(reopened.mutation_allowed())
        self.store.db.execute(
            "UPDATE tc_verification_breaker_events SET event_json=? WHERE sequence=1", (b"{}",)
        )
        with self.assertRaisesRegex(VerificationFoundationError, "integrity"):
            reopened.is_open()


if __name__ == "__main__":
    unittest.main()
