"""Crash/state-machine tests with type-contract fakes only; no external effects."""
from __future__ import annotations

import random
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from terracompute_ops.plan_authorization import ApprovalDecision, PlanAuthorizationService
from terracompute_ops.plans import Effect, Verification, VerificationStatus, canonical_json, stable_hash
from terracompute_ops.step_executor import (
    Domain, ExecutionBlocked, Inspection, Outcome, StepExecutor, WorkerResult,
    WORKER_CONTRACT, WORKSPACE_CONTRACT, VERIFIER_CONTRACT, build_step_executor,
)
from terracompute_ops.tasks import IllegalTransition, Task, TaskConflict, TaskState, TaskStore
from test_plan_authorization import (
    NOW, GROUP, POLICY, EVIDENCE, FakeMembership, FakeResolver, plan, step, rental,
)


class Crash(BaseException):
    pass


def executable(name="step-1", domain=Domain.TARGET_HOST, *, safe=True, rollback=False,
               checkpoint=False, tenant=False):
    scope, flags, resources = {
        Domain.TARGET_HOST: ("host", {"host": True}, ("host:17049",)),
        Domain.VAST_WRITE: ("vast", {"external_commitment": True}, ("vast:17049",)),
        Domain.BMC: ("bmc", {"reachability": True}, ("bmc:17049",)),
        Domain.CONTROLLER_DEPLOYMENT: ("deployment", {"host": True}, ("controller:imladris",)),
        Domain.WORKSPACE: ("workspace", {}, ("workspace:task-1",)),
    }[domain]
    if tenant:
        scope, flags, resources = "tenant", {"tenant": True}, ("C.51217040",)
    execution = dict(domain=domain.value, effect_scope=scope, purpose="repair exact resource",
                     safe_boundary_after=safe, max_output_bytes=128)
    arguments = {"execution": execution, "payload": {"argv": ["novel-operation", "exact-resource"]}}
    undo = dict(operation="run_shell", arguments=arguments, postconditions=[{"check": "restored"}])
    return step(name, operation="never_before_coded", arguments=arguments,
                effects=(Effect("effect-1", "declared effect", **flags),), resources=resources,
                rollback=undo if rollback else None,
                rollback_reason=None if rollback else "not reversible",
                checkpoint={"kind": "read_only_snapshot"} if checkpoint else None)


class Worker:
    def __init__(self, domain):
        self.domain = domain
        self.contract = WORKSPACE_CONTRACT if domain is Domain.WORKSPACE else WORKER_CONTRACT
        self.credential_domains = frozenset() if domain is Domain.WORKSPACE else frozenset({domain})
        self.calls = []
        self.reconciliations = []
        self.inspections = []
        self.machine = "17049"
        self.rentals = ()
        self.outcome = Outcome.APPLIED
        self.reconciled = Outcome.UNKNOWN
        self.output = b"ok"
        self.hook = lambda request: None

    def inspect(self, request):
        self.inspections.append(request)
        return Inspection(self.machine, True, self.rentals, "sha256:" + "a" * 64)

    def dispatch(self, request):
        self.calls.append(request)
        self.hook(request)
        return WorkerResult(request.idempotency_key, self.outcome, self.output)

    def reconcile(self, request):
        self.reconciliations.append(request)
        return WorkerResult(request.idempotency_key, self.reconciled)


class Verifier:
    contract = VERIFIER_CONTRACT

    def validate(self, request, verification):
        return {c["check"] for c in verification.checks} == {c["check"] for c in request.step.postconditions}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite"
        self.resolver = FakeResolver({"C.51217040": (rental(),), "51217040": (rental(),)})
        self.workers = {domain: Worker(domain) for domain in Domain}
        self.allowed = True
        self.permit_post_deadline = False
        self.documents = {}
        self.open()

    def open(self):
        self.store = TaskStore(self.path, clock=lambda: self.now, policy_revision=lambda: POLICY,
                               permit_post_deadline_observation=self.permit_post_deadline)
        self.addCleanup(self.store.close)
        self.auth = PlanAuthorizationService(self.store.db, membership=FakeMembership(),
                                             resolver=self.resolver, policy_revision=POLICY,
                                             approval_group_id=GROUP, clock=lambda: self.now)
        self.executor = StepExecutor(self.store, self.auth, self.workers, Verifier(),
                                     mutation_allowed=lambda: self.allowed)

    def setup_plan(self, steps=None, approve=True, *, task_id="task-1", deadline=None):
        self.document = plan(tuple(steps or [executable()]))
        if task_id != "task-1":
            self.document = replace(self.document, task_id=task_id, plan_id="plan-" + task_id)
        self.documents[task_id] = self.document
        self.store.create_task(Task(
            task_id=task_id, requester_id="operator:1", requester_group_id=GROUP,
            origin_message_id="message-" + task_id, created_at=NOW, objective="repair machine",
            constraints=(), state=TaskState.INVESTIGATING, evidence_revision=EVIDENCE,
            model_thread=None, attempt_count=0, deadline=deadline, budgets={}, incident_ids=(),
        ))
        with self.store.transaction():
            self.executor._transition(task_id, TaskState.PLANNING)
        self.store.add_plan(self.document)
        requirements = self.auth.issue_requirements(
            self.document, self.auth.authorize(self.document), requester_id="operator:1",
            current_evidence_revision=EVIDENCE,
        )
        self.requirements = requirements
        if approve:
            for requirement in requirements:
                self.approve(requirement)
        self.executor.register(self.document, {s: r.requirement_id for r in requirements for s in r.step_ids})

    def approve(self, requirement):
        self.auth.membership.verified_at = self.now
        self.auth.record_decision(ApprovalDecision(
            requirement_id=requirement.requirement_id, card_hash=requirement.card_hash,
            nonce=requirement.nonce, group_id=GROUP, user_id=777, display_name="Operator",
            via_callback=True, occurred_at=self.now,
        ), current_evidence_revision=EVIDENCE)

    def advance(self):
        return self.executor.advance("task-1")

    def snapshot(self):
        return self.executor.snapshot("task-1")

    def verification(self, status=VerificationStatus.SUCCEEDED, task_id="task-1"):
        document = self.document if task_id == self.document.task_id else self.documents[task_id]
        attempt = self.executor.snapshot(task_id)["attempts"][-1]
        request = self.executor._request(document, attempt)
        return Verification(
            verification_id="verify-" + request.idempotency_key, task_id=task_id, plan_id=document.plan_id,
            plan_hash=document.content_hash, step_id=request.step.step_id,
            lease_id=request.lease.lease_id, lease_hash=request.lease.content_hash, status=status,
            checks=request.step.postconditions, evidence_revision=EVIDENCE, performed_at=self.now,
        )

    def verify(self, status=VerificationStatus.SUCCEEDED):
        return self.executor.accept_verification(self.verification(status))

    def test_default_off_missing_interfaces_fail_closed(self):
        self.assertIsNone(build_step_executor({}))
        self.assertIsNone(build_step_executor({"TERRACOMPUTE_STEP_EXECUTOR": "true"}))
        with self.assertRaises(ExecutionBlocked):
            build_step_executor({"TERRACOMPUTE_STEP_EXECUTOR": "1"})

    def test_unknown_operation_executes_only_exact_approved_step(self):
        self.setup_plan()
        self.assertEqual(self.advance(), "verifying")
        request = self.workers[Domain.TARGET_HOST].calls[0]
        self.assertEqual(request.step.operation, "never_before_coded")
        self.assertEqual(request.lease.plan_hash, self.document.content_hash)
        self.assertEqual(self.verify(), "succeeded")

    def test_duplicate_dispatch_and_automatic_verified_continuation(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        for _ in range(12):
            self.assertEqual(self.advance(), "verifying")
        first = self.verification()
        self.assertEqual(self.executor.accept_verification(first), "verifying")
        self.executor.accept_verification(first)
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 2)
        self.assertEqual(self.verify(), "succeeded")

    def test_crash_before_intent_rolls_back_lease_and_journal(self):
        self.setup_plan()
        original = self.executor._save
        def crash(doc, event):
            if event == "executor-intent":
                raise Crash()
            original(doc, event)
        with patch.object(self.executor, "_save", side_effect=crash), self.assertRaises(Crash):
            self.advance()
        self.assertEqual(self.store.leases("task-1"), ())
        self.assertEqual(self.snapshot()["attempts"], [])
        self.assertEqual(self.advance(), "verifying")

    def test_crash_after_intent_before_marker_is_retryable_on_restart(self):
        self.setup_plan()
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        key = self.store.leases("task-1")[0].idempotency_key
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {"task-1": "verifying"})
        worker = self.workers[Domain.TARGET_HOST]
        self.assertEqual(worker.calls[0].idempotency_key, key)
        self.assertEqual(worker.reconciliations, [])

    def test_crash_after_dispatch_never_blind_repeats(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.hook = lambda _: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.advance()
        self.store.close()
        self.open()
        self.assertEqual(self.advance(), "dispatched")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "uncertain")
        self.assertEqual(self.advance(), "uncertain")
        self.assertEqual(len(worker.calls), 1)
        worker.reconciled = Outcome.APPLIED
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.verify(), "succeeded")

    def test_crash_after_marker_before_call_requires_reconciliation(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        with patch.object(worker, "dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        self.now += timedelta(seconds=61)
        worker.reconciled = Outcome.NOT_APPLIED
        self.assertEqual(self.advance(), "not_applied")
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.advance(), "verifying")
        self.assertNotEqual(self.store.leases("task-1")[0].idempotency_key, worker.calls[0].idempotency_key)

    def test_expired_intent_uses_new_lease_without_reconciliation(self):
        self.setup_plan()
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(len(self.store.leases("task-1")), 2)
        self.assertEqual(self.workers[Domain.TARGET_HOST].reconciliations, [])

    def test_approval_expiry_revocation_and_policy_change_before_dispatch(self):
        for change in ("expiry", "revocation", "policy"):
            with self.subTest(change=change):
                # Separate durable task histories for each fault.
                if change != "expiry":
                    self.tearDown()
                    self.setUp()
                self.setup_plan()
                with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
                    self.advance()
                if change == "expiry":
                    self.now += timedelta(minutes=6)
                elif change == "revocation":
                    self.auth.revoke_grant(self.requirements[0].requirement_id, revoked_by="operator", reason="stop")
                else:
                    self.auth.policy_revision = "new-policy"
                with self.assertRaises(ExecutionBlocked):
                    self.advance()
                self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_lease_check_is_repeated_after_inspection(self):
        self.setup_plan()
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        self.now += timedelta(seconds=30)
        worker = self.workers[Domain.TARGET_HOST]
        original = worker.inspect
        def expired(request):
            result = original(request)
            self.now += timedelta(seconds=31)  # Within the inspect bound, past the lease.
            return result
        worker.inspect = expired
        with self.assertRaises(TaskConflict):
            self.advance()
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.snapshot()["attempts"][-1]["state"], "intent")

    def test_cancel_at_safe_boundary_and_defer_at_unsafe_boundary(self):
        self.setup_plan([executable("one", safe=False), executable("two"), executable("three")])
        self.advance()
        self.assertEqual(self.executor.request_cancel("task-1"), "verifying")
        self.assertEqual(self.verify(), "verifying")
        self.assertEqual(self.verify(), "cancelled")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 2)

    def test_cancel_before_dispatch(self):
        self.setup_plan()
        self.assertEqual(self.executor.request_cancel("task-1"), "cancelled")
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_partial_plan_workspace_without_production_approval_then_deployment(self):
        self.setup_plan([executable("patch-test-build", Domain.WORKSPACE),
                         executable("deploy", Domain.CONTROLLER_DEPLOYMENT)], approve=False)
        self.assertEqual(self.advance(), "verifying")
        with self.assertRaisesRegex(ExecutionBlocked, "no exact human approval"):
            self.verify()
        self.assertEqual(self.snapshot()["cursor"], 1)
        self.assertEqual(self.workers[Domain.CONTROLLER_DEPLOYMENT].calls, [])
        self.approve(self.requirements[0])
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.verify(), "succeeded")

    def test_rollback_is_exact_same_domain_effects_with_checkpoint(self):
        self.setup_plan([executable(rollback=True, checkpoint=True)])
        self.advance()
        self.assertEqual(self.verify(VerificationStatus.FAILED), "lease_wait")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "verifying")
        calls = self.workers[Domain.TARGET_HOST].calls
        self.assertEqual(calls[-1].phase, "rollback")
        self.assertEqual(calls[-1].checkpoint_digest, calls[0].checkpoint_digest)
        self.assertEqual(calls[-1].step.effects, calls[0].step.effects)
        self.assertEqual(self.verify(), "rolled_back")

    def test_rollback_cannot_expand_domain_or_approval(self):
        original = executable(rollback=True)
        rollback = dict(original.rollback)
        arguments = dict(rollback["arguments"])
        execution = dict(arguments["execution"], domain="bmc", effect_scope="bmc")
        arguments["execution"] = execution
        rollback["arguments"] = arguments
        with self.assertRaises(ExecutionBlocked):
            self.setup_plan([replace(original, rollback=rollback)])

    def test_worker_credential_domain_isolation(self):
        for domain in Domain:
            with self.subTest(domain=domain):
                worker = self.workers[domain]
                expected = worker.credential_domains
                worker.credential_domains = frozenset(Domain)
                with self.assertRaises(ExecutionBlocked):
                    StepExecutor(self.store, self.auth, self.workers, Verifier(), mutation_allowed=lambda: True)
                worker.credential_domains = expected
        worker = self.workers[Domain.TARGET_HOST]
        worker.contract = "unattested"
        with self.assertRaises(ExecutionBlocked):
            StepExecutor(self.store, self.auth, self.workers, Verifier(), mutation_allowed=lambda: True)

    def test_tenant_replacement_refuses_stale_binding(self):
        self.setup_plan([executable(tenant=True)])
        self.workers[Domain.TARGET_HOST].rentals = (rental(),)
        changed = rental(generation="replacement")
        self.resolver.records = {"C.51217040": (changed,), "51217040": (changed,)}
        with self.assertRaises(ExecutionBlocked):
            self.advance()
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_tenant_worker_receives_exact_binding_and_purpose(self):
        self.setup_plan([executable(tenant=True)])
        worker = self.workers[Domain.TARGET_HOST]
        worker.rentals = (rental(),)
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(worker.calls[0].rentals, (rental(),))
        self.assertEqual(worker.calls[0].spec.purpose, "repair exact resource")

    def test_machine_mismatch_and_independent_breaker(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.machine = "another-machine"
        with self.assertRaisesRegex(ExecutionBlocked, "machine"):
            self.advance()
        worker.machine = "17049"
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.advance()
        self.assertEqual(worker.calls, [])

    def test_unknown_domain_and_effect_need_replan(self):
        for field in ("domain", "effect_scope"):
            original = executable()
            args = dict(original.arguments)
            args["execution"] = dict(args["execution"], **{field: "unknown"})
            from terracompute_ops.step_executor import ExecutionSpec
            with self.assertRaises(ExecutionBlocked):
                ExecutionSpec.from_step(replace(original, arguments=args))

    def test_output_overrun_and_timeout_are_uncertain(self):
        self.setup_plan([executable("one"), executable("two")])
        worker = self.workers[Domain.TARGET_HOST]
        worker.output = b"x" * 129
        self.assertEqual(self.advance(), "uncertain")
        self.now += timedelta(seconds=61)
        worker.reconciled = Outcome.APPLIED
        self.assertEqual(self.advance(), "verifying")
        worker.output = b"bounded"
        worker.hook = lambda _: setattr(self, "now", self.now + timedelta(seconds=61))
        self.assertEqual(self.verify(), "uncertain")
        self.assertEqual(len(worker.calls), 2)
        self.assertNotIn("xxxx", str(self.snapshot()))

    def test_forged_or_early_verification_does_not_continue(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        for verification in (
            replace(self.verification(), lease_hash="a" * 64),
            replace(self.verification(), checks=({"check": "fake"},)),
            replace(self.verification(), performed_at=NOW - timedelta(seconds=1)),
        ):
            with self.assertRaises(ExecutionBlocked):
                self.executor.accept_verification(verification)
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_restart_waits_for_verification_and_keeps_checkpoint(self):
        self.setup_plan([executable(checkpoint=True)])
        self.advance()
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {"task-1": "verifying"})
        self.assertEqual(self.snapshot()["attempts"][0]["checkpoint"], "sha256:" + "a" * 64)
        self.assertEqual(self.verify(), "succeeded")

    def test_lease_acquisition_with_advancing_wall_clock(self):
        self.setup_plan()
        def live_clock():
            self.now += timedelta(microseconds=1)
            return self.now
        self.store.clock = live_clock
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)
        self.store.recover()

    def test_slow_reconciliation_cannot_clear_uncertainty(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.outcome = Outcome.UNKNOWN
        self.advance()
        self.now += timedelta(seconds=61)
        def slow(request):
            self.now += timedelta(seconds=61)
            return WorkerResult(request.idempotency_key, Outcome.NOT_APPLIED)
        worker.reconcile = slow
        self.assertEqual(self.advance(), "uncertain")
        self.assertEqual(len(worker.calls), 1)

    def test_each_domain_gets_only_its_step(self):
        self.setup_plan([executable(domain.value, domain) for domain in Domain])
        self.advance()
        for domain in Domain:
            worker = self.workers[domain]
            self.assertEqual(len(worker.calls), 1)
            self.assertIs(worker.calls[0].spec.domain, domain)
            self.assertEqual(worker.calls[0].step.step_id, domain.value)
            self.verify()
        self.assertEqual(self.snapshot()["status"], "succeeded")

    def test_second_executor_observes_committed_dispatch_without_duplication(self):
        self.setup_plan()
        other_store = TaskStore(self.path, clock=lambda: self.now, policy_revision=lambda: POLICY)
        self.addCleanup(other_store.close)
        other_auth = PlanAuthorizationService(
            other_store.db, membership=FakeMembership(), resolver=self.resolver,
            policy_revision=POLICY, approval_group_id=GROUP, clock=lambda: self.now,
        )
        other = StepExecutor(other_store, other_auth, self.workers, Verifier(), mutation_allowed=lambda: True)
        self.workers[Domain.TARGET_HOST].hook = lambda _: self.assertEqual(other.advance("task-1"), "dispatched")
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_exception_after_dispatch_must_reconcile_before_retry(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.hook = lambda _: (_ for _ in ()).throw(TimeoutError("untrusted exception text"))
        self.assertEqual(self.advance(), "uncertain")
        self.assertEqual(self.advance(), "uncertain")
        self.assertEqual(worker.reconciliations, [])
        self.now += timedelta(seconds=61)
        worker.reconciled = Outcome.NOT_APPLIED
        self.assertEqual(self.advance(), "not_applied")
        worker.hook = lambda _: None
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(len(worker.calls), 2)
        self.assertNotIn("untrusted exception", str(self.snapshot()))

    def test_retry_budget_is_durable_and_bounded(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.outcome = Outcome.NOT_APPLIED
        for _ in range(3):
            self.assertEqual(self.advance(), "not_applied")
            self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "needs_replan")
        self.assertEqual(len(worker.calls), 3)

    def test_revoked_approval_allows_reconcile_but_never_retry(self):
        self.setup_plan()
        worker = self.workers[Domain.TARGET_HOST]
        worker.outcome = Outcome.UNKNOWN
        self.advance()
        self.auth.revoke_grant(self.requirements[0].requirement_id, revoked_by="operator", reason="stop")
        self.now += timedelta(seconds=61)
        worker.reconciled = Outcome.NOT_APPLIED
        self.assertEqual(self.advance(), "not_applied")
        with self.assertRaises(ExecutionBlocked):
            self.advance()
        self.assertEqual(len(worker.calls), 1)

    def test_tenant_replacement_at_worker_inspection_blocks(self):
        self.setup_plan([executable(tenant=True)])
        worker = self.workers[Domain.TARGET_HOST]
        worker.rentals = (rental(generation="replacement"),)
        with self.assertRaisesRegex(ExecutionBlocked, "RentalRecord"):
            self.advance()
        self.assertEqual(worker.calls, [])

    def test_tenant_payload_approval_does_not_enable_executor_payload_access(self):
        from terracompute_ops.plan_authorization import TENANT_PAYLOAD_OPERATION
        original = executable(tenant=True)
        args = dict(original.arguments, rental_id="51217040", purpose="diagnose", source="logs")
        self.setup_plan([replace(original, operation=TENANT_PAYLOAD_OPERATION, arguments=args)])
        with self.assertRaisesRegex(ExecutionBlocked, "payload"):
            self.advance()
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_multi_step_rollback_is_reverse_and_automatic(self):
        self.setup_plan([executable("one", rollback=True), executable("two", rollback=True)])
        self.advance()
        self.verify()
        self.assertEqual(self.verify(VerificationStatus.FAILED), "lease_wait")
        self.now += timedelta(seconds=61)
        self.advance()
        self.assertEqual(self.verify(), "verifying")
        self.assertEqual(self.verify(), "rolled_back")
        self.assertEqual([(r.step.step_id, r.phase) for r in self.workers[Domain.TARGET_HOST].calls],
                         [("one", "forward"), ("two", "forward"), ("two", "rollback"), ("one", "rollback")])

    def test_rollback_needs_current_approval(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.auth.revoke_grant(self.requirements[0].requirement_id, revoked_by="operator", reason="stop")
        self.now += timedelta(seconds=61)
        with self.assertRaises(ExecutionBlocked):
            self.advance()
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_restart_between_verification_commit_and_continuation(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        with patch.object(self.executor, "advance", side_effect=Crash), self.assertRaises(Crash):
            self.verify()
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {"task-1": "verifying"})
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 2)

    def test_cancel_leased_but_undispatched_intent(self):
        self.setup_plan()
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        self.assertEqual(self.executor.request_cancel("task-1"), "cancelled")
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_plan_end_and_bounds_are_explicit(self):
        with self.assertRaisesRegex(ExecutionBlocked, "safe boundary"):
            self.setup_plan([executable(safe=False)])
        from terracompute_ops.step_executor import ExecutionSpec
        original = executable()
        for bound in (0, 65537, True, "128"):
            args = dict(original.arguments)
            args["execution"] = dict(args["execution"], max_output_bytes=bound)
            with self.assertRaises(ExecutionBlocked):
                ExecutionSpec.from_step(replace(original, arguments=args))

    def test_randomized_duplicate_wakes_preserve_single_dispatch_per_key(self):
        self.setup_plan([executable("one"), executable("two"), executable("three")])
        rng = random.Random(17049)
        for _ in range(100):
            state = self.advance()
            if state == "verifying" and rng.randrange(5) == 0:
                self.verify()
            worker = self.workers[Domain.TARGET_HOST]
            keys = [r.idempotency_key for r in worker.calls]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertLessEqual(len(worker.calls), self.snapshot()["cursor"] + 1)
        self.assertEqual(self.snapshot()["status"], "succeeded")

    def second_executor(self):
        store = TaskStore(self.path, clock=lambda: self.now, policy_revision=lambda: POLICY)
        self.addCleanup(store.close)
        auth = PlanAuthorizationService(store.db, membership=FakeMembership(), resolver=self.resolver,
                                        policy_revision=POLICY, approval_group_id=GROUP, clock=lambda: self.now)
        return StepExecutor(store, auth, self.workers, Verifier(), mutation_allowed=lambda: self.allowed)

    def test_dispatch_entrypoints_reject_enclosing_rollback(self):
        self.setup_plan([executable("one"), executable("two")])
        for call in (self.advance, lambda: self.executor.request_cancel("task-1"),
                     self.executor.recover, lambda: self.executor._dispatch("task-1")):
            with self.subTest(call=call), self.assertRaises(Crash):
                with self.store.transaction():
                    with self.assertRaisesRegex(ExecutionBlocked, "ambient transaction"):
                        call()
                    raise Crash()
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])
        self.assertEqual(self.snapshot()["attempts"], [])
        self.assertEqual(self.advance(), "verifying")
        verification = self.verification()
        with self.assertRaises(Crash):
            with self.store.transaction():
                with self.assertRaisesRegex(ExecutionBlocked, "ambient transaction"):
                    self.executor.accept_verification(verification)
                raise Crash()
        self.store.close()
        self.open()
        other = self.second_executor()
        self.assertEqual(other.recover(), {"task-1": "verifying"})
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)
        self.assertEqual(self.verify(), "verifying")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 2)

    def test_marker_lease_and_intent_are_outermost_committed_at_dispatch(self):
        self.setup_plan()
        other = self.second_executor()
        def observed(request):
            self.assertFalse(self.store.db.in_transaction)
            snapshot = other.snapshot("task-1")
            self.assertEqual(snapshot["attempts"][-1]["state"], "dispatched")
            self.assertEqual(other.store.leases("task-1")[0], request.lease)
            events = [e.event_type for e in other.store.events("task-1")]
            self.assertIn("executor-intent", events)
            self.assertIn("executor-dispatched", events)
            self.assertEqual(other.advance("task-1"), "dispatched")
            raise Crash()
        self.workers[Domain.TARGET_HOST].hook = observed
        with self.assertRaises(Crash):
            self.advance()
        self.store.close()
        self.open()
        self.assertEqual(self.advance(), "dispatched")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_failed_forward_then_fenced_rollback_cannot_cancel_or_replan(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        worker = self.workers[Domain.TARGET_HOST]
        worker.outcome = Outcome.NOT_APPLIED
        self.assertEqual(self.advance(), "not_applied")
        self.store.close()
        self.open()
        self.assertEqual(self.snapshot()["effects"][0]["state"], "failed")
        self.assertEqual(self.executor.request_cancel("task-1"), "lease_wait")
        self.assertNotEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
        with self.assertRaisesRegex(ExecutionBlocked, "resolved effects"):
            self.executor.prepare_replan("task-1")
        self.now += timedelta(seconds=61)
        worker.outcome = Outcome.APPLIED
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.verify(), "rolled_back")
        self.assertTrue(all(e["state"] == "resolved" for e in self.snapshot()["effects"]))

    def test_failed_rollback_remains_unsafe_until_independent_recovery(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        self.advance()
        self.assertEqual(self.verify(VerificationStatus.FAILED), "needs_replan")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.request_cancel("task-1"), "needs_replan")
        with self.assertRaises(ExecutionBlocked):
            self.executor.prepare_replan("task-1")
        self.allowed = False
        recovery = replace(self.verification(), verification_id="rollback-recovery")
        self.assertEqual(self.executor.accept_verification(recovery), "rolled_back")
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        self.store.close()
        self.open()  # Audited FAILED -> PLANNING is replayable after restart.
        self.assertEqual(self.store.get_task("task-1").state, TaskState.PLANNING)

    def test_failed_forward_without_rollback_requires_fresh_verification(self):
        self.setup_plan()
        self.advance()
        self.assertEqual(self.verify(VerificationStatus.FAILED), "needs_replan")
        self.now += timedelta(seconds=61)
        with self.assertRaises(ExecutionBlocked):
            self.executor.prepare_replan("task-1")
        self.assertEqual(self.executor.request_cancel("task-1"), "needs_replan")
        recovery = replace(self.verification(), verification_id="forward-recovery")
        self.assertEqual(self.executor.accept_verification(recovery), "succeeded")
        self.assertEqual(self.snapshot()["effects"][0]["state"], "resolved")

    def test_replan_archives_old_attempts_and_requires_fresh_phase3_binding(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        self.advance()
        self.assertEqual(self.verify(), "rolled_back")
        old_plan, old_requirements = self.document, self.requirements
        old_keys = {a.lease_id for a in self.store.leases("task-1")}
        with self.assertRaises(ExecutionBlocked):
            self.executor.prepare_replan("task-1")  # Still-live rollback lease.
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        archived = self.executor.snapshot("task-1", old_plan.content_hash)
        self.assertEqual(archived["status"], "superseded")
        self.assertEqual(len(archived["attempts"]), 2)
        self.store.close()
        self.open()
        self.document = replace(old_plan, plan_id="plan-2", version=2, created_at=self.now)
        self.store.add_plan(self.document)
        with self.assertRaisesRegex(ExecutionBlocked, "fresh plan"):
            self.executor.register(self.document, {"step-1": old_requirements[0].requirement_id})
        self.requirements = self.auth.issue_requirements(
            self.document, self.auth.authorize(self.document), requester_id="operator:1",
            current_evidence_revision=EVIDENCE,
        )
        self.executor.register(self.document, {"step-1": self.requirements[0].requirement_id})
        with self.assertRaises(ExecutionBlocked):
            self.advance()
        self.approve(self.requirements[0])
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.advance()
        self.store.close()
        self.open()
        other = self.second_executor()
        self.assertEqual(other.recover(), {"task-1": "verifying"})
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.snapshot()["attempts"][0]["attempt"], 1)
        self.assertNotIn(self.snapshot()["attempts"][0]["lease_id"], old_keys)
        self.assertEqual(self.executor.snapshot("task-1", old_plan.content_hash), archived)
        self.assertEqual(self.verify(), "succeeded")
        self.store.recover()

    def test_safe_verifying_execution_can_replan_but_cannot_be_replaced_directly(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        self.allowed = False
        with self.assertRaises(ExecutionBlocked):
            self.verify()
        self.now += timedelta(seconds=61)
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.PLANNING)
        replacement = replace(self.document, plan_id="plan-2", version=2, created_at=self.now)
        with self.assertRaisesRegex(TaskConflict, "safely superseded"):
            self.store.add_plan(replacement)
        # Restore the verifying lifecycle for the audited path under test.
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.EXECUTING)
            self.executor._transition("task-1", TaskState.VERIFYING)
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        self.store.add_plan(replacement)

    def test_applied_during_pause_reconciles_lifecycle_on_resume(self):
        self.setup_plan()
        def pause(_):
            with self.store.transaction():
                self.executor._transition("task-1", TaskState.PAUSED)
        self.workers[Domain.TARGET_HOST].hook = pause
        self.advance()
        self.assertEqual(self.store.get_task("task-1").state, TaskState.PAUSED)
        self.store.close()
        self.open()
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.EXECUTING)
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.store.get_task("task-1").state, TaskState.VERIFYING)
        self.assertEqual(self.verify(), "succeeded")
        self.store.recover()

    def test_verification_ingested_while_paused_survives_restart_and_later_resume(self):
        self.setup_plan()
        def pause(_):
            with self.store.transaction():
                self.executor._transition("task-1", TaskState.PAUSED)
        self.workers[Domain.TARGET_HOST].hook = pause
        self.advance()
        self.allowed = False
        self.assertEqual(self.verify(), "verifying")
        self.assertIn("pending_verification", self.snapshot()["attempts"][-1])
        self.store.close()
        self.open()
        self.now += timedelta(seconds=61)
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.EXECUTING)
        self.assertEqual(self.advance(), "succeeded")
        self.assertEqual(len(self.store.verifications("task-1")), 1)
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)
        self.store.recover()

    def test_open_breaker_records_success_but_blocks_next_forward(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.verify()
        self.assertEqual(self.snapshot()["cursor"], 1)
        self.assertEqual(len(self.store.verifications("task-1")), 1)
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)
        self.allowed = True
        self.assertEqual(self.advance(), "verifying")

    def test_open_breaker_records_failure_but_blocks_rollback(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.verify(VerificationStatus.FAILED)
        self.assertEqual(self.snapshot()["effects"][0]["state"], "failed")
        self.assertEqual(self.snapshot()["rollback_queue"], ["step-1"])
        self.now += timedelta(seconds=61)
        with self.assertRaises(ExecutionBlocked):
            self.advance()
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_uncertain_and_invalid_verification_under_open_breaker(self):
        self.setup_plan()
        self.advance()
        self.allowed = False
        with self.assertRaises(ExecutionBlocked):
            self.executor.accept_verification(replace(self.verification(), checks=({"check": "forged"},)))
        self.assertEqual(self.verify(VerificationStatus.UNCERTAIN), "verifying")
        self.assertEqual(len(self.snapshot()["attempts"][-1]["verifications"]), 1)
        self.assertEqual(self.snapshot()["effects"][0]["state"], "unverified")

    def test_verifier_deadline_and_evidence_size_are_checked(self):
        self.setup_plan()
        self.advance()
        verification = replace(self.verification(), checks=tuple({"check": "active", "data": "x" * 16000} for _ in range(5)))
        with self.assertRaises(ExecutionBlocked):
            self.executor.accept_verification(verification)
        def slow(request, verification):
            self.now += timedelta(seconds=61)
            return True
        self.executor.verifier.validate = slow
        with self.assertRaisesRegex(ExecutionBlocked, "deadline"):
            self.verify()
        self.assertNotIn("verifications", self.snapshot()["attempts"][-1])


    def test_raw_transaction_is_also_rejected(self):
        self.setup_plan()
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(ExecutionBlocked, "ambient transaction"):
                self.advance()
        finally:
            self.store.db.rollback()
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])
        self.assertEqual(self.second_executor().advance("task-1"), "verifying")
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_no_effect_attempt_budget_can_be_safely_superseded(self):
        self.setup_plan()
        self.workers[Domain.TARGET_HOST].outcome = Outcome.NOT_APPLIED
        for _ in range(3):
            self.assertEqual(self.advance(), "not_applied")
            self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "needs_replan")
        self.assertEqual(self.snapshot()["effects"], [])
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        self.assertEqual(len(self.executor.snapshot("task-1", self.document.content_hash)["attempts"]), 3)
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {})

    def test_failed_task_cannot_reopen_without_archived_resolution(self):
        self.setup_plan()
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.FAILED)
        with self.assertRaises(IllegalTransition), self.store.transaction():
            self.executor._transition("task-1", TaskState.PLANNING)
        with self.assertRaises(IllegalTransition), self.store.transaction():
            self.executor._transition("task-1", TaskState.PLANNING,
                                      event_type="executor-replan",
                                      payload={"plan_hash": self.document.content_hash, "journal_hash": "fake"})
        self.assertEqual(self.store.get_task("task-1").state, TaskState.FAILED)

    def test_multiple_effects_survive_partial_rollback_and_cancellation(self):
        self.setup_plan([executable("one", safe=False, rollback=True),
                         executable("two", rollback=True)])
        self.advance()
        self.verify()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        self.advance()
        # Restoring step two cannot resolve step one's outstanding effect.
        self.verify()
        self.assertEqual(self.snapshot()["attempts"][-1]["step_id"], "one")
        self.assertEqual(self.snapshot()["effects"][0]["state"], "verified")
        self.assertEqual(self.executor.request_cancel("task-1"), "verifying")
        self.assertEqual(self.verify(), "rolled_back")
        self.assertTrue(all(e["state"] == "resolved" for e in self.snapshot()["effects"]))

    def test_recovery_verification_cannot_rewrite_or_predate_failed_evidence(self):
        self.setup_plan()
        self.advance()
        self.now += timedelta(seconds=1)
        failed = self.verification(VerificationStatus.FAILED)
        self.executor.accept_verification(failed)
        with self.assertRaisesRegex(ExecutionBlocked, "identity"):
            self.executor.accept_verification(replace(failed, status=VerificationStatus.SUCCEEDED))
        with self.assertRaises(ExecutionBlocked):
            self.executor.accept_verification(replace(failed, verification_id="old-recovery",
                                                      status=VerificationStatus.SUCCEEDED, performed_at=NOW))
        self.assertEqual(self.snapshot()["effects"][0]["state"], "failed")

    def test_legacy_journal_does_not_infer_effect_safety(self):
        self.setup_plan()
        doc = self.snapshot()
        doc["schema_version"] = 1
        del doc["effects"]
        with self.store.transaction():
            self.store.db.execute("UPDATE tc_step_runs SET document=?,digest=? WHERE task_id=?",
                                  (canonical_json(doc), stable_hash(doc), "task-1"))
        self.store.close()
        self.open()
        self.assertEqual(self.executor.recover(), {"task-1": "blocked"})
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    # Review a641afc7 regressions: disk-backed, two-task and restart shaped.

    def crash_after_intent(self, task_id="task-1"):
        with patch.object(self.executor, "_dispatch", side_effect=Crash), self.assertRaises(Crash):
            self.executor.advance(task_id)

    def reopen(self):
        self.store.close()
        self.open()

    def test_expired_intent_is_released_before_revoked_authority_blocks(self):
        for change in ("revocation", "expiry"):
            with self.subTest(change=change):
                if change != "revocation":
                    self.tearDown()
                    self.setUp()
                self.setup_plan()
                self.crash_after_intent()
                if change == "revocation":
                    self.auth.revoke_grant(self.requirements[0].requirement_id, revoked_by="operator", reason="stop")
                    self.now += timedelta(seconds=61)
                else:
                    self.now += timedelta(minutes=6)
                with self.assertRaises(ExecutionBlocked):
                    self.advance()
                self.reopen()  # The release survived the refused retry on disk.
                self.assertEqual(self.snapshot()["attempts"][-1]["state"], "expired_intent")
                self.assertEqual(len(self.snapshot()["attempts"]), 1)
                self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
                self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])
                self.store.recover()

    def test_prepare_replan_alone_releases_expired_intent_after_revocation(self):
        self.setup_plan()
        self.crash_after_intent()
        self.auth.revoke_grant(self.requirements[0].requirement_id, revoked_by="operator", reason="stop")
        with self.assertRaisesRegex(ExecutionBlocked, "live attempts or leases"):
            self.executor.prepare_replan("task-1")  # Still-live undispatched lease.
        self.now += timedelta(seconds=61)
        self.reopen()
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        archived = self.executor.snapshot("task-1", self.document.content_hash)
        self.assertEqual(archived["attempts"][-1]["state"], "expired_intent")
        self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])

    def test_cancel_of_undispatched_intent_precedes_breaker_and_pause(self):
        for gate in ("breaker", "pause", "dispatch-breaker"):
            with self.subTest(gate=gate):
                if gate != "breaker":
                    self.tearDown()
                    self.setUp()
                self.setup_plan()
                self.crash_after_intent()
                if gate == "pause":
                    with self.store.transaction():
                        self.executor._transition("task-1", TaskState.PAUSED)
                else:
                    self.allowed = False
                if gate == "dispatch-breaker":
                    # Cancel lands after settlement: the dispatch gate itself must yield.
                    with self.store.transaction():
                        doc = self.snapshot()
                        doc["cancel_requested"] = True
                        self.executor._save(doc, "executor-cancel-requested")
                    self.assertEqual(self.executor._dispatch("task-1"), "cancelled")
                else:
                    self.assertEqual(self.executor.request_cancel("task-1"), "cancelled")
                self.reopen()
                self.assertEqual(self.snapshot()["status"], "cancelled")
                self.assertEqual(self.snapshot()["attempts"][-1]["state"], "cancelled_intent")
                self.assertEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
                self.assertEqual(self.workers[Domain.TARGET_HOST].calls, [])
                self.assertEqual(self.workers[Domain.TARGET_HOST].inspections, [])
                self.store.recover()

    def test_cancel_at_already_safe_boundary_precedes_breaker_and_pause(self):
        self.setup_plan([executable("one"), executable("two")])
        self.advance()
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.verify()
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.PAUSED)
        self.assertEqual(self.executor.request_cancel("task-1"), "cancelled")
        self.reopen()
        self.assertEqual(self.executor.recover(), {"task-1": "cancelled"})
        self.assertEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_breaker_still_blocks_cancel_at_unsafe_boundary(self):
        self.setup_plan([executable("one", safe=False), executable("two")])
        self.advance()
        self.allowed = False
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.verify()
        with self.assertRaisesRegex(ExecutionBlocked, "breaker"):
            self.executor.request_cancel("task-1")
        self.assertTrue(self.snapshot()["cancel_requested"])
        self.assertEqual(self.snapshot()["status"], "running")
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), 1)

    def test_arbitrary_failed_task_with_safe_journal_cannot_reopen(self):
        self.setup_plan()
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.FAILED)
        with self.assertRaisesRegex(ExecutionBlocked, "rolled-back"):
            self.executor.prepare_replan("task-1")
        # Even a complete archive/audit trail is refused by the store unless the
        # durable pre-supersede status is exactly rolled_back.
        for prior in ("running", "needs_replan", None):
            with self.subTest(prior=prior):
                with self.assertRaises(IllegalTransition), self.store.transaction():
                    doc = self.snapshot()
                    if prior is not None:
                        doc["superseded_from"] = prior
                    doc["status"] = "superseded"
                    self.executor._save(doc, "executor-superseded")
                    self.store.db.execute(
                        "INSERT INTO tc_step_run_history VALUES(?,?,?,?)",
                        ("task-1", doc["plan_hash"], canonical_json(doc), stable_hash(doc)))
                    self.executor._transition(
                        "task-1", TaskState.PLANNING, event_type="executor-replan",
                        payload={"plan_hash": doc["plan_hash"], "journal_hash": stable_hash(doc)})
        self.reopen()
        self.assertEqual(self.store.get_task("task-1").state, TaskState.FAILED)
        self.assertEqual(self.snapshot()["status"], "running")
        self.store.recover()

    def test_rolled_back_prior_status_is_persisted_in_archive(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        self.advance()
        self.assertEqual(self.verify(), "rolled_back")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        self.reopen()
        archived = self.executor.snapshot("task-1", self.document.content_hash)
        self.assertEqual((archived["status"], archived["superseded_from"]), ("superseded", "rolled_back"))
        self.store.recover()

    def pause_during_dispatch(self, task_id="task-1"):
        def pause(_):
            with self.store.transaction():
                self.executor._transition(task_id, TaskState.PAUSED)
        self.workers[Domain.TARGET_HOST].hook = pause
        self.executor.advance(task_id)
        self.workers[Domain.TARGET_HOST].hook = lambda _: None

    def test_pending_verification_reconciles_any_legal_resume_state(self):
        for resumed in (TaskState.PLANNING, TaskState.AWAITING_APPROVAL, TaskState.INVESTIGATING,
                        TaskState.VERIFYING):
            with self.subTest(resumed=resumed):
                if resumed is not TaskState.PLANNING:
                    self.tearDown()
                    self.setUp()
                self.setup_plan()
                self.pause_during_dispatch()
                self.assertEqual(self.verify(), "verifying")
                self.reopen()
                with self.store.transaction():
                    self.executor._transition("task-1", resumed)
                self.assertEqual(self.executor.recover(), {"task-1": "succeeded"})
                self.assertEqual(self.store.get_task("task-1").state, TaskState.SUCCEEDED)
                self.assertEqual(len(self.store.verifications("task-1")), 1)
                self.store.recover()

    def test_unreconcilable_pending_verification_is_durably_blocked_and_recovery_continues(self):
        self.setup_plan(task_id="task-0")
        self.setup_plan()
        self.pause_during_dispatch("task-0")
        pending = self.verification(task_id="task-0")
        self.assertEqual(self.executor.accept_verification(pending), "verifying")
        with self.store.transaction():  # Coordinator terminates instead of resuming.
            self.executor._transition("task-0", TaskState.CANCELLED)
        for _ in range(2):
            self.reopen()
            self.assertEqual(self.executor.recover(), {"task-0": "needs_replan", "task-1": "verifying"})
        doc = self.executor.snapshot("task-0")
        self.assertEqual(doc["blocked_reason"], "verification-rejected")
        self.assertEqual(doc["attempts"][-1]["rejected_verifications"],
                         [{"verification_id": pending.verification_id, "reason": "TaskStoreError"}])
        self.assertNotIn("pending_verification", doc["attempts"][-1])
        self.assertEqual(doc["effects"][0]["state"], "unverified")
        self.assertEqual(self.store.verifications("task-0"), ())
        with self.assertRaises(ExecutionBlocked):
            self.executor.prepare_replan("task-0")
        self.assertEqual(self.verify(), "succeeded")
        self.store.recover()

    def test_store_error_in_one_task_never_aborts_recovery_of_later_tasks(self):
        self.setup_plan(task_id="task-0")
        self.setup_plan()
        self.crash_after_intent("task-0")
        with self.store.transaction():  # Replayed lease now meets an illegal lifecycle.
            self.executor._transition("task-0", TaskState.VERIFYING)
        with self.assertRaises(IllegalTransition):
            self.executor.advance("task-0")
        self.reopen()
        self.assertEqual(self.executor.recover(), {"task-0": "blocked", "task-1": "verifying"})
        calls = self.workers[Domain.TARGET_HOST].calls
        self.assertEqual([r.lease.task_id for r in calls], ["task-1"])
        with self.store.transaction():  # A corrupt journal is equally contained.
            self.store.db.execute("UPDATE tc_step_runs SET document=? WHERE task_id=?", (b"{}", "task-0"))
        self.reopen()
        self.assertEqual(self.executor.recover(), {"task-0": "blocked", "task-1": "verifying"})

    def exhaust_budget(self):
        self.workers[Domain.TARGET_HOST].outcome = Outcome.NOT_APPLIED
        for _ in range(3):
            self.assertEqual(self.advance(), "not_applied")
            self.now += timedelta(seconds=61)
        self.assertEqual(self.advance(), "needs_replan")

    def test_cancel_on_safe_needs_replan_completes_instead_of_vanishing(self):
        self.setup_plan()
        self.exhaust_budget()
        self.allowed = False
        self.assertEqual(self.executor.request_cancel("task-1"), "cancelled")
        self.reopen()
        self.assertEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
        self.assertEqual(self.executor.prepare_replan("task-1"), "cancelled")
        self.assertEqual(self.snapshot()["status"], "cancelled")
        self.store.recover()

    def test_cancel_recorded_before_replan_is_completed_by_prepare_replan(self):
        self.setup_plan()
        self.exhaust_budget()
        with self.store.transaction():  # Crash between recording the cancel and advancing.
            doc = self.snapshot()
            doc["cancel_requested"] = True
            self.executor._save(doc, "executor-cancel-requested")
        self.reopen()
        self.assertEqual(self.executor.prepare_replan("task-1"), "cancelled")
        self.assertEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
        with self.assertRaises(ExecutionBlocked):
            self.executor.snapshot("task-1", self.document.content_hash + "x")

    def test_cancel_on_unsafe_needs_replan_survives_replan_and_reregistration(self):
        self.setup_plan([executable(rollback=True)])
        self.advance()
        self.verify(VerificationStatus.FAILED)
        self.now += timedelta(seconds=61)
        self.advance()
        self.assertEqual(self.verify(VerificationStatus.FAILED), "needs_replan")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.executor.request_cancel("task-1"), "needs_replan")
        recovery = replace(self.verification(), verification_id="rollback-recovery")
        self.assertEqual(self.executor.accept_verification(recovery), "rolled_back")
        self.assertEqual(self.executor.prepare_replan("task-1"), "planning")
        self.reopen()
        old = self.document
        self.assertTrue(self.executor.snapshot("task-1", old.content_hash)["cancel_requested"])
        self.document = replace(old, plan_id="plan-2", version=2, created_at=self.now)
        self.store.add_plan(self.document)
        self.requirements = self.auth.issue_requirements(
            self.document, self.auth.authorize(self.document), requester_id="operator:1",
            current_evidence_revision=EVIDENCE,
        )
        self.approve(self.requirements[0])
        calls = len(self.workers[Domain.TARGET_HOST].calls)
        self.executor.register(self.document, {"step-1": self.requirements[0].requirement_id})
        self.assertTrue(self.snapshot()["cancel_requested"])
        self.reopen()
        self.assertEqual(self.executor.recover(), {"task-1": "cancelled"})
        self.assertEqual(self.store.get_task("task-1").state, TaskState.CANCELLED)
        self.assertEqual(len(self.workers[Domain.TARGET_HOST].calls), calls)
        self.store.recover()

    def test_inspect_runs_without_the_database_write_lock(self):
        self.setup_plan(task_id="task-0")
        self.setup_plan()
        other = self.second_executor()
        worker = self.workers[Domain.TARGET_HOST]
        original, seen = worker.inspect, []
        def inspect(request):
            if request.lease.task_id == "task-1" and not seen:
                seen.append(self.store.db.in_transaction)
                # Another connection commits a write for a different task.
                self.assertEqual(other.advance("task-0"), "verifying")
            return original(request)
        worker.inspect = inspect
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(seen, [False])
        self.assertEqual(sorted(r.lease.task_id for r in worker.calls), ["task-0", "task-1"])

    def test_cancel_during_inspection_is_rechecked_before_dispatch(self):
        self.setup_plan()
        other = self.second_executor()
        worker = self.workers[Domain.TARGET_HOST]
        original = worker.inspect
        def inspect(request):
            self.assertEqual(other.request_cancel("task-1"), "cancelled")
            return original(request)
        worker.inspect = inspect
        self.assertEqual(self.advance(), "cancelled_intent")
        self.assertEqual(worker.calls, [])
        self.reopen()
        self.assertEqual(self.snapshot()["status"], "cancelled")

    def test_concurrent_dispatch_during_inspection_is_fenced(self):
        self.setup_plan()
        other = self.second_executor()
        worker = self.workers[Domain.TARGET_HOST]
        original, nested = worker.inspect, []
        def inspect(request):
            if not nested:
                nested.append(True)
                self.assertEqual(other.advance("task-1"), "verifying")
            return original(request)
        worker.inspect = inspect
        self.assertEqual(self.advance(), "applied")
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(self.snapshot()["attempts"]), 1)

    def test_revocation_pause_and_breaker_during_inspection_block_dispatch(self):
        for change in ("revocation", "pause", "breaker"):
            with self.subTest(change=change):
                if change != "revocation":
                    self.tearDown()
                    self.setUp()
                self.setup_plan()
                worker = self.workers[Domain.TARGET_HOST]
                original = worker.inspect
                def inspect(request):
                    if change == "revocation":
                        self.auth.revoke_grant(self.requirements[0].requirement_id,
                                               revoked_by="operator", reason="stop")
                    elif change == "pause":
                        with self.store.transaction():
                            self.executor._transition("task-1", TaskState.PAUSED)
                    else:
                        self.allowed = False
                    return original(request)
                worker.inspect = inspect
                with self.assertRaises(ExecutionBlocked):
                    self.advance()
                self.assertEqual(worker.calls, [])
                self.assertEqual(self.snapshot()["attempts"][-1]["state"], "intent")

    def test_slow_or_failing_inspection_blocks_without_dispatch_or_text(self):
        self.setup_plan()
        self.crash_after_intent()
        worker = self.workers[Domain.TARGET_HOST]
        original = worker.inspect
        def slow(request):
            self.now += timedelta(seconds=60)
            return original(request)
        worker.inspect = slow
        with self.assertRaisesRegex(ExecutionBlocked, "bound"):
            self.advance()
        def failing(request):
            raise RuntimeError("untrusted inspection text")
        worker.inspect = failing
        self.now += timedelta(seconds=1)
        with self.assertRaises(ExecutionBlocked) as caught:
            self.advance()
        self.assertNotIn("untrusted", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(worker.calls, [])

    DEADLINE = NOW + timedelta(hours=1)

    def test_post_deadline_executor_verification_fails_closed_by_default(self):
        self.setup_plan(deadline=self.DEADLINE)
        self.advance()
        self.now = self.DEADLINE + timedelta(seconds=1)
        self.assertEqual(self.verify(), "needs_replan")
        for _ in range(2):
            self.reopen()
            self.assertEqual(self.executor.recover(), {"task-1": "needs_replan"})
        attempt = self.snapshot()["attempts"][-1]
        self.assertEqual([r["reason"] for r in attempt["rejected_verifications"]], ["TaskConflict"])
        self.assertEqual(self.snapshot()["effects"][0]["state"], "unverified")
        self.assertEqual(self.store.verifications("task-1"), ())
        self.assertEqual(self.store.get_task("task-1").state, TaskState.VERIFYING)

    def test_paused_evidence_does_not_bypass_the_deadline_on_resume(self):
        self.setup_plan(deadline=self.DEADLINE)
        self.pause_during_dispatch()
        self.assertEqual(self.verify(), "verifying")  # Collected before the deadline.
        self.now = self.DEADLINE
        self.reopen()
        with self.store.transaction():
            self.executor._transition("task-1", TaskState.EXECUTING)
        self.assertEqual(self.advance(), "needs_replan")
        self.assertEqual(self.store.verifications("task-1"), ())

    def test_explicit_policy_permits_post_deadline_observation_of_pre_deadline_result(self):
        self.permit_post_deadline = True
        self.reopen()
        self.setup_plan(deadline=self.DEADLINE)
        self.advance()
        self.now = self.DEADLINE + timedelta(seconds=1)
        self.assertEqual(self.verify(), "succeeded")
        self.assertEqual(len(self.store.verifications("task-1")), 1)
        self.reopen()
        self.store.recover()

    def test_policy_never_admits_a_result_not_proven_before_the_deadline(self):
        self.permit_post_deadline = True
        self.reopen()
        self.setup_plan(deadline=self.DEADLINE)
        worker = self.workers[Domain.TARGET_HOST]
        worker.outcome = Outcome.UNKNOWN
        self.advance()
        self.now = self.DEADLINE
        worker.reconciled = Outcome.APPLIED  # Durable result time is not pre-deadline.
        self.assertEqual(self.advance(), "verifying")
        self.assertEqual(self.verify(), "needs_replan")
        self.assertEqual(self.store.verifications("task-1"), ())
        self.assertEqual(self.snapshot()["effects"][0]["state"], "unverified")


if __name__ == "__main__":
    unittest.main()
