from __future__ import annotations

import copy
import dataclasses
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops import shadow_rollout as shadow_module
from terracompute_ops.plan_authorization import Authority
from terracompute_ops.plans import ContractError, Effect
from terracompute_ops.rollout import CommissioningAttestation, RolloutCapability
from terracompute_ops.shadow_rollout import (
    CAPABILITY_COVERAGE,
    CoverageClass,
    CanaryCriteria,
    CanarySummary,
    CaptureExpectation,
    CaptureOutcome,
    CaptureResult,
    ComparisonOrigin,
    DivergenceClass,
    PolicyDecision,
    PolicyOutcome,
    RouteKind,
    RoutingDecision,
    ShadowComparison,
    ShadowConflict,
    ShadowDecision,
    ShadowError,
    ShadowLedger,
    ShadowRecord,
    ShadowTampered,
    classify_divergence,
    derive_coverage,
    required_authority,
)
from terracompute_ops.tasks import TaskStore


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
WINDOW_START = NOW
WINDOW_END = NOW + timedelta(hours=2)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float = 1) -> None:
        self.now += timedelta(seconds=seconds)


def effect(name: str = "e", **flags: bool) -> Effect:
    return Effect(effect_id=name, description=f"{name} description", **flags)


def policy(
    outcome: PolicyOutcome = PolicyOutcome.PERMIT,
    authority: Authority | None = Authority.AGENT,
    refs: tuple[str, ...] = (),
    *,
    name: str = "e",
    **flags: bool,
) -> PolicyDecision:
    return PolicyDecision(effect=effect(name, **flags), outcome=outcome,
                          authority=authority, approval_refs=refs)


def approval(authority: Authority = Authority.EXACT_HUMAN, refs=("step-1",), **flags) -> PolicyDecision:
    return policy(PolicyOutcome.REQUIRE_APPROVAL, authority, refs, **flags)


def decision(
    policy_decision: PolicyDecision | None = None,
    route: RouteKind = RouteKind.NEW_TASK,
    task_id: str | None = None,
) -> ShadowDecision:
    return ShadowDecision(routing=RoutingDecision(route=route, task_id=task_id),
                          policy=policy_decision)


READ = decision(policy())
HOST = decision(approval(host=True))
UNSAFE_NEW = decision(policy(PolicyOutcome.PERMIT, Authority.AGENT, host=True))
UNKNOWN = decision(policy(PolicyOutcome.UNKNOWN, None))


def criteria(**overrides) -> CanaryCriteria:
    values = dict(
        policy_revision="policy-1", config_revision="config-1", evidence_revision="evidence-1",
        source_instances=("gateway-a",), window_start=WINDOW_START, window_end=WINDOW_END,
        minimum_samples=10, minimum_samples_per_source=10, minimum_span_seconds=3600,
        max_evidence_age_seconds=3600, max_safer_new=0, max_routing_mismatch=0,
    )
    values.update(overrides)
    values.setdefault("capability", RolloutCapability.OPERATOR_READONLY)
    values.setdefault("coverage_minimums", {
        kind: 10 if kind in CAPABILITY_COVERAGE[values["capability"]] else 0
        for kind in CoverageClass
    })
    return CanaryCriteria(**values)


class ShadowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        self.ledger = ShadowLedger(self.tasks, clock=self.clock)
        self._nonce = 0

    def comparison(self, old: ShadowDecision = READ, new: ShadowDecision = READ, **overrides) -> ShadowComparison:
        self._nonce += 1
        values = dict(
            origin=ComparisonOrigin.OPERATOR, operator_id="operator:edgar", task_id=None,
            request_id=f"request-{self._nonce}", parent_hash="0" * 64,
            policy_revision="policy-1",
            config_revision="config-1", evidence_revision="evidence-1",
            observed_at=self.clock.now, source_instance="gateway-a",
            nonce=f"nonce-{self._nonce}", old=old, new=new,
        )
        values.update(overrides)
        return ShadowComparison(**values)

    def record(self, old: ShadowDecision = READ, new: ShadowDecision = READ, **overrides) -> ShadowRecord:
        return self.ledger.record(self.comparison(old, new, **overrides))

    def fill(self, count: int, *, span: float = 5400, **overrides) -> None:
        """Record ``count`` equivalent samples evenly across ``span`` seconds."""
        step = span / (count - 1)
        for index in range(count):
            self.record(**overrides)
            if index < count - 1:
                self.clock.advance(step)

    def close_window(self) -> None:
        # Strictly past window_end plus the default five-minute clock skew.
        self.clock.now = WINDOW_END + timedelta(minutes=6)

    def reopen(self) -> ShadowLedger:
        self.tasks.close()
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        self.ledger = ShadowLedger(self.tasks, clock=self.clock)
        return self.ledger

    def raw(self, *statements: str) -> None:
        for statement in statements:
            self.tasks.db.execute(statement)
        self.tasks.db.commit()

    def reads(self):
        ledger = self.ledger
        return (ledger.status, ledger.verify_chain, ledger.records,
                lambda: ledger.get("nonce-1"), lambda: ledger.get_by_hash("0" * 64),
                lambda: ledger.summary(criteria()))


class ContractTest(ShadowCase):
    def test_contracts_are_strict_immutable_and_round_trip(self) -> None:
        comparison = self.comparison(HOST, HOST, task_id="task-1")
        self.assertEqual(ShadowComparison.from_json(comparison.to_json()), comparison)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            comparison.nonce = "other"  # type: ignore[misc]
        document = comparison.to_document()
        for mutate in (
            lambda d: d.update(extra=1),
            lambda d: d.pop("nonce"),
            lambda d: d.update(machine_id="17050"),
            lambda d: d.update(old_hash="0" * 64),
            lambda d: d.update(origin="cron"),
            lambda d: d.update(observed_at="2026-09-21T12:00:00+00:00"),
            lambda d: d["new"]["routing"].update(route="teleport"),
            lambda d: d["new"]["policy"].update(authority="root"),
            lambda d: d["new"]["policy"]["effect"].update(host="yes"),
        ):
            damaged = ShadowComparison.from_json(comparison.to_json()).to_document()
            mutate(damaged)
            with self.assertRaises(ContractError):
                ShadowComparison.from_document(damaged)
        self.assertEqual(document["old_hash"], HOST.content_hash)

    def test_old_documents_are_refused_at_every_level(self) -> None:
        # Old versions classified/accepted differently; none of them parse now.
        self.assertEqual(shadow_module.SCHEMA_VERSION, 5)
        self.fill(10)
        self.close_window()
        record = self.ledger.records()[0]
        summary = self.ledger.summary(criteria())
        paths = {
            ShadowRecord: ((), ("comparison",), ("comparison", "old"),
                           ("comparison", "new", "routing"), ("comparison", "new", "policy")),
            CanarySummary: ((), ("criteria",)),
        }
        for contract, document in ((ShadowRecord, record.to_document()),
                                   (CanarySummary, summary.to_document())):
            self.assertEqual(contract.from_document(document).content_hash,
                             (record if contract is ShadowRecord else summary).content_hash)
            for version in (1, 2, 3, 4):
                for path in paths[contract]:
                    with self.subTest(contract=contract.__name__, path=path):
                        damaged = copy.deepcopy(document)
                        target = damaged
                        for key in path:
                            target = target[key]
                        self.assertEqual(target["schema_version"], 5)
                        target["schema_version"] = version
                        with self.assertRaises(ContractError):
                            contract.from_document(damaged)

    def test_identity_binding_is_as_applicable(self) -> None:
        with self.assertRaises(ContractError):
            self.comparison(operator_id=None)
        with self.assertRaises(ContractError):
            self.comparison(origin=ComparisonOrigin.DETECTOR)
        detector = self.comparison(origin=ComparisonOrigin.DETECTOR, operator_id=None, task_id="task-9")
        self.assertIsNone(detector.operator_id)
        for name in ("request_id", "policy_revision", "config_revision", "evidence_revision",
                     "source_instance", "nonce"):
            with self.assertRaises(ContractError):
                self.comparison(**{name: ""})
        with self.assertRaises(ContractError):
            self.comparison(machine_id="17050")
        with self.assertRaises(ContractError):
            self.comparison(observed_at=datetime(2026, 9, 21, 12, 0))
        with self.assertRaises(ContractError):
            self.comparison(observed_at=NOW.astimezone(timezone(timedelta(hours=2))))

    def test_incoherent_decisions_are_rejected(self) -> None:
        for build in (
            lambda: policy(PolicyOutcome.PERMIT, Authority.EXACT_HUMAN),
            lambda: policy(PolicyOutcome.REQUIRE_APPROVAL, Authority.AGENT, ("s",)),
            lambda: policy(PolicyOutcome.REQUIRE_APPROVAL, Authority.EXACT_HUMAN, ()),
            lambda: policy(PolicyOutcome.DENY, Authority.EXACT_HUMAN, ("s",)),
            lambda: policy(PolicyOutcome.UNKNOWN, Authority.AGENT),
            lambda: policy(PolicyOutcome.PERMIT, None),
            lambda: policy(PolicyOutcome.REQUIRE_APPROVAL, Authority.EXACT_HUMAN, ("s", "s")),
            lambda: RoutingDecision(RouteKind.EXISTING_TASK),
            lambda: RoutingDecision(RouteKind.NEW_TASK, "task-1"),
            lambda: RoutingDecision("new-task"),  # type: ignore[arg-type]
        ):
            with self.assertRaises(ContractError):
                build()

    def test_no_callbacks_or_foreign_objects_are_accepted(self) -> None:
        called = []

        def path(*args, **kwargs):
            called.append(args)
            return READ

        class EffectSubclass(Effect):
            pass

        for build in (
            lambda: self.comparison(old=path),  # type: ignore[arg-type]
            lambda: self.comparison(new=path),  # type: ignore[arg-type]
            lambda: ShadowDecision(routing=path),  # type: ignore[arg-type]
            lambda: ShadowDecision(routing=RoutingDecision(RouteKind.NO_ROUTE), policy=path),  # type: ignore[arg-type]
            lambda: PolicyDecision(effect=path, outcome=PolicyOutcome.DENY, authority=Authority.AGENT),  # type: ignore[arg-type]
            lambda: PolicyDecision(effect=EffectSubclass("e", "d"), outcome=PolicyOutcome.DENY,
                                   authority=Authority.AGENT),
            lambda: classify_divergence(path, READ),  # type: ignore[arg-type]
        ):
            with self.assertRaises(ContractError):
                build()
        with self.assertRaises(ContractError):
            self.ledger.record(path)  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            self.ledger.record(self.comparison().to_document())  # type: ignore[arg-type]
        self.assertEqual(called, [])


class InertTest(ShadowCase):
    def test_recording_executes_nothing_and_touches_no_task_state(self) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("shadow comparison must never execute or route anything")

        before = {
            name: self.tasks.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for (name,) in self.tasks.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'tc_shadow_%'"
            ).fetchall()
        }
        with mock.patch.object(subprocess, "run", forbidden), \
                mock.patch.object(subprocess, "Popen", forbidden), \
                mock.patch("terracompute_ops.operator_gateway.OperatorGateway.handle", forbidden), \
                mock.patch("terracompute_ops.task_service.TaskService.handle", forbidden), \
                mock.patch("terracompute_ops.plan_authorization.authorize_plan", forbidden), \
                mock.patch("terracompute_ops.plan_authorization.derive_step", forbidden), \
                mock.patch.object(TaskStore, "create_task", forbidden, create=True):
            self.clock.now = NOW
            self.fill(10)
            self.record(READ, UNSAFE_NEW)
            self.close_window()
            self.ledger.status()
            self.ledger.summary(criteria())
        after = {name: self.tasks.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                 for name in before}
        self.assertEqual(before, after)

    def test_shadow_output_is_never_execution_authority(self) -> None:
        record = self.record(HOST, HOST)
        self.assertIs(record.execution_authority, False)
        self.assertIs(self.ledger.status().execution_authority, False)
        document = record.to_document()
        self.assertIs(document["execution_authority"], False)
        document["execution_authority"] = True
        with self.assertRaises(ContractError):
            ShadowRecord.from_document(document)
        for name in ("permitted", "approve", "lease", "execute", "activate", "grant"):
            self.assertFalse(hasattr(record, name))
            self.assertFalse(hasattr(self.ledger, name))

    def test_module_is_only_imported_by_the_unwired_evidence_bridge(self) -> None:
        forbidden = {"subprocess", "os", "acting", "actions", "action_service", "step_executor",
                     "operator_gateway", "task_service", "telegram", "vast", "vast_actions",
                     "http_client", "rollout"}
        self.assertFalse(forbidden & set(vars(shadow_module)))
        root = Path(__file__).resolve().parents[1]
        wired = [
            str(path.relative_to(root))
            for pattern in ("src/**/*.py", "nix/**/*", "target/*", "pyproject.toml", "default.nix")
            for path in root.glob(pattern)
            if path.is_file() and path.name != "shadow_rollout.py"
            and b"shadow_rollout" in path.read_bytes()
        ]
        self.assertEqual(set(wired), {
            "src/terracompute_ops/commissioning_evidence.py",
            "src/terracompute_ops/shadow_adapter.py",
            "src/terracompute_ops/legacy_shadow_adapter.py",
            "src/terracompute_ops/incident_shadow_adapter.py",
            "src/terracompute_ops/shadow_policy_adapter.py",
            "src/terracompute_ops/shadow_producer.py",
            # Pure, unwired preview/capture seam that returns shadow RoutingDecision
            # values for already-derived task results; no runtime imports it.
            "src/terracompute_ops/task_routing_preview.py",
        })


class ClassificationTest(unittest.TestCase):
    def assertClass(self, old, new, expected: DivergenceClass) -> tuple[str, ...]:
        divergence, reasons = classify_divergence(old, new)
        self.assertIs(divergence, expected, reasons)
        self.assertEqual((divergence, reasons), classify_divergence(old, new))
        self.assertEqual(bool(reasons), expected is not DivergenceClass.EQUIVALENT)
        return reasons

    def test_equivalent(self) -> None:
        for side in (READ, HOST, decision(None, RouteKind.REJECTED),
                     decision(policy(PolicyOutcome.DENY, Authority.ALWAYS_APPROVE_TENANT, tenant=True)),
                     decision(policy(PolicyOutcome.PERMIT, Authority.STANDING_CONSENT, owned_component=True),
                              RouteKind.EXISTING_TASK, "task-1")):
            self.assertClass(side, side, DivergenceClass.EQUIVALENT)

    def test_safer_new(self) -> None:
        owned = decision(policy(PolicyOutcome.PERMIT, Authority.STANDING_CONSENT, owned_component=True))
        cases = (
            (owned, decision(approval(owned_component=True))),
            (HOST, decision(policy(PolicyOutcome.DENY, Authority.EXACT_HUMAN, host=True))),
            (HOST, decision(approval(host=True, irreversible=True))),
            (HOST, decision(approval(refs=("step-1", "step-2"), host=True))),
            (READ, decision(policy(PolicyOutcome.DENY, Authority.AGENT))),
        )
        for old, new in cases:
            self.assertClass(old, new, DivergenceClass.SAFER_NEW)

    def test_more_permissive_new_is_unsafe(self) -> None:
        tenant = decision(approval(Authority.ALWAYS_APPROVE_TENANT, tenant=True))
        cases = (
            # permits without approval what the old path gated
            (decision(approval(owned_component=True)),
             decision(policy(PolicyOutcome.PERMIT, Authority.STANDING_CONSENT, owned_component=True))),
            # asks for approval where the old path denies
            (decision(policy(PolicyOutcome.DENY, Authority.EXACT_HUMAN, host=True)), HOST),
            # under-classifies the effect
            (decision(approval(host=True, reachability=True)), HOST),
            # lower authority for the same effect
            (tenant, decision(approval(Authority.EXACT_HUMAN, tenant=True))),
            # omits an approval the old path binds
            (decision(approval(refs=("step-1", "step-2"), host=True)), HOST),
            # violates the floor of its own effect even though the old path agrees
            (UNSAFE_NEW, UNSAFE_NEW),
            # unsafe outranks a routing mismatch
            (decision(None, RouteKind.REJECTED), UNSAFE_NEW),
        )
        for old, new in cases:
            self.assertClass(old, new, DivergenceClass.MORE_PERMISSIVE_NEW)

    def test_suppressed_request_becoming_executable_is_unsafe(self) -> None:
        suppressed = (
            decision(None, RouteKind.REJECTED), decision(None, RouteKind.NO_ROUTE),
            decision(None, RouteKind.AMBIGUOUS), decision(None, RouteKind.DUPLICATE, "task-1"),
            # a suppressed request is suppressed whatever policy the old path derived
            decision(policy(), RouteKind.DUPLICATE, "task-1"),
            decision(approval(host=True), RouteKind.AMBIGUOUS),
            decision(policy(PolicyOutcome.DENY, Authority.AGENT), RouteKind.REJECTED),
        )
        executable = (
            READ, HOST, decision(policy(), RouteKind.EXISTING_TASK, "task-1"),
            decision(approval(host=True), RouteKind.EXISTING_TASK, "task-1"),
            # routed for execution with no policy decision at all
            decision(None), decision(None, RouteKind.EXISTING_TASK, "task-1"),
        )
        for old in suppressed:
            for new in executable:
                with self.subTest(old=old.routing.route, new=new.routing.route):
                    reasons = self.assertClass(old, new, DivergenceClass.MORE_PERMISSIVE_NEW)
                    self.assertIn("suppressed", reasons[0])
                    self.assertIn(old.routing.route.value, reasons[0])

    def test_policy_free_old_path_gaining_a_live_policy_is_unsafe(self) -> None:
        cases = (
            # even a well-gated approval is new authority the old path never derived
            (decision(None), decision(approval(host=True), RouteKind.EXISTING_TASK, "task-1")),
            (decision(None, RouteKind.EXISTING_TASK, "task-1"), READ),
            (decision(None, RouteKind.REJECTED), decision(policy(), RouteKind.AMBIGUOUS)),
            (decision(None, RouteKind.NO_ROUTE), decision(approval(host=True), RouteKind.REJECTED)),
        )
        for old, new in cases:
            reasons = self.assertClass(old, new, DivergenceClass.MORE_PERMISSIVE_NEW)
            self.assertTrue(any("derived none" in reason for reason in reasons), reasons)

    def test_executable_new_path_cannot_drop_an_old_policy_across_routes(self) -> None:
        for old in (
            READ,
            HOST,
            decision(policy(PolicyOutcome.DENY, Authority.AGENT)),
        ):
            for new in (
                decision(None, RouteKind.EXISTING_TASK, "task-1"),
                decision(None, RouteKind.NEW_TASK),
            ):
                if old.routing == new.routing:
                    # Same-route one-sided policy is already malformed and blocking.
                    continue
                with self.subTest(old=old, new=new):
                    reasons = self.assertClass(
                        old, new, DivergenceClass.MORE_PERMISSIVE_NEW)
                    self.assertTrue(any("drops the policy decision" in reason for reason in reasons))

    def test_safe_denied_transitions_are_not_called_unsafe(self) -> None:
        deny = policy(PolicyOutcome.DENY, Authority.AGENT)
        deny_host = policy(PolicyOutcome.DENY, Authority.EXACT_HUMAN, host=True)
        # A suppressed or policy-free request the new path routes but denies
        # executes nothing: a routing mismatch, never more-permissive-new.
        for old in (decision(None, RouteKind.REJECTED), decision(None, RouteKind.NO_ROUTE),
                    decision(None, RouteKind.AMBIGUOUS), decision(None, RouteKind.DUPLICATE, "task-1"),
                    decision(policy(), RouteKind.DUPLICATE, "task-1")):
            for new in (decision(deny), decision(deny_host, RouteKind.EXISTING_TASK, "task-2")):
                self.assertClass(old, new, DivergenceClass.ROUTING_MISMATCH)
        self.assertClass(decision(None), decision(deny, RouteKind.EXISTING_TASK, "task-1"),
                         DivergenceClass.ROUTING_MISMATCH)
        # Executable becoming suppressed, and suppressed staying suppressed, are benign.
        for old, new in (
            (READ, decision(None, RouteKind.REJECTED)), (HOST, decision(None, RouteKind.NO_ROUTE)),
            (READ, decision(policy(), RouteKind.DUPLICATE, "task-1")),
            (decision(None, RouteKind.REJECTED), decision(None, RouteKind.AMBIGUOUS)),
            (decision(None, RouteKind.NO_ROUTE), decision(deny, RouteKind.REJECTED)),
        ):
            self.assertClass(old, new, DivergenceClass.ROUTING_MISMATCH)
        # Same route, stricter outcome stays safer-new.
        self.assertClass(HOST, decision(deny_host), DivergenceClass.SAFER_NEW)
        self.assertClass(decision(approval(host=True), RouteKind.AMBIGUOUS),
                         decision(deny_host, RouteKind.AMBIGUOUS), DivergenceClass.SAFER_NEW)

    def test_routing_mismatch(self) -> None:
        cases = (
            (READ, decision(policy(), RouteKind.EXISTING_TASK, "task-1")),
            (decision(policy(), RouteKind.EXISTING_TASK, "task-1"),
             decision(policy(), RouteKind.EXISTING_TASK, "task-2")),
            (HOST, decision(None, RouteKind.AMBIGUOUS)),
        )
        for old, new in cases:
            self.assertClass(old, new, DivergenceClass.ROUTING_MISMATCH)

    def test_a_dropped_approval_is_unsafe_even_when_others_are_added(self) -> None:
        for old_refs, new_refs in (
            (("step-1", "step-2"), ("step-1", "step-3")),      # substitution
            (("step-1",), ("step-2",)),                          # wholesale replacement
            (("step-1", "step-2"), ("step-1", "step-3", "step-4", "step-5")),  # outnumbered
            (("step-1", "step-2"), ("step-1",)),                 # plain omission
        ):
            with self.subTest(old=old_refs, new=new_refs):
                reasons = self.assertClass(decision(approval(refs=old_refs, host=True)),
                                           decision(approval(refs=new_refs, host=True)),
                                           DivergenceClass.MORE_PERMISSIVE_NEW)
                for ref in set(old_refs) - set(new_refs):
                    self.assertIn(ref, reasons[0])
        # Substitution across routes is still unsafe, not a routing mismatch.
        self.assertClass(decision(approval(refs=("step-1",), host=True)),
                         decision(approval(refs=("step-9",), host=True), RouteKind.EXISTING_TASK, "t"),
                         DivergenceClass.MORE_PERMISSIVE_NEW)
        # Only a superset is safer.
        self.assertClass(decision(approval(refs=("step-1",), host=True)),
                         decision(approval(refs=("step-1", "step-2"), host=True)),
                         DivergenceClass.SAFER_NEW)

    def test_approval_mismatch_no_longer_exists_as_a_tolerated_class(self) -> None:
        self.assertNotIn("approval-mismatch", {item.value for item in DivergenceClass})
        self.assertFalse(hasattr(DivergenceClass, "APPROVAL_MISMATCH"))
        self.assertNotIn("max_approval_mismatch",
                         {item.name for item in dataclasses.fields(CanaryCriteria)})
        self.assertEqual(set(shadow_module.BENIGN_CLASSES),
                         {DivergenceClass.SAFER_NEW, DivergenceClass.ROUTING_MISMATCH})
        document = criteria().to_document()
        document["max_approval_mismatch"] = 10**6
        with self.assertRaises(ContractError):
            CanaryCriteria.from_document(document)

    def test_malformed_unknown(self) -> None:
        cases = (
            (READ, UNKNOWN), (UNKNOWN, READ), (UNKNOWN, UNKNOWN),
            (READ, decision(policy(), RouteKind.UNKNOWN)),
            (decision(None, RouteKind.UNKNOWN), READ),
            (READ, decision(None)), (decision(None), READ),
            # unknown outranks even an unsafe new decision
            (UNKNOWN, UNSAFE_NEW),
        )
        for old, new in cases:
            self.assertClass(old, new, DivergenceClass.MALFORMED_UNKNOWN)

    def test_novel_operations_are_classified_by_effect_not_name(self) -> None:
        # Neither path's operation has ever been named anywhere in the repository.
        old = decision(approval(name="quantum-defragment-lattice", host=True))
        new = decision(approval(name="rebalance-flux-capacitor", host=True))
        self.assertClass(old, new, DivergenceClass.EQUIVALENT)
        # A familiar-sounding read name does not make a host effect safe ...
        disguised = decision(policy(PolicyOutcome.PERMIT, Authority.AGENT,
                                    name="observe_target", host=True))
        self.assertClass(old, disguised, DivergenceClass.MORE_PERMISSIVE_NEW)
        # ... and an alarming name does not make a flag-free effect unsafe.
        scary = decision(policy(name="reboot-everything-now"))
        self.assertClass(READ, scary, DivergenceClass.EQUIVALENT)
        for flag in shadow_module.EFFECT_FLAG_NAMES:
            novel = decision(policy(PolicyOutcome.PERMIT, Authority.AGENT,
                                    name=f"novel-{flag}", **{flag: True}))
            self.assertClass(READ, novel, DivergenceClass.MORE_PERMISSIVE_NEW)

    def test_required_authority_floor_covers_every_flag(self) -> None:
        self.assertIs(required_authority(effect()), Authority.AGENT)
        self.assertIs(required_authority(effect(owned_component=True)), Authority.STANDING_CONSENT)
        self.assertIs(required_authority(effect(owned_component=True, tenant=True)),
                      Authority.ALWAYS_APPROVE_TENANT)
        for flag in set(shadow_module.EFFECT_FLAG_NAMES) - {"owned_component", "tenant"}:
            self.assertIs(required_authority(effect(**{flag: True})), Authority.EXACT_HUMAN)
            self.assertIs(required_authority(effect(owned_component=True, **{flag: True})),
                          Authority.EXACT_HUMAN)

    def test_a_record_cannot_assert_a_class_it_did_not_derive(self) -> None:
        comparison = ShadowComparison(
            origin=ComparisonOrigin.DETECTOR, operator_id=None, task_id=None, request_id="r",
            parent_hash="0" * 64,
            policy_revision="p", config_revision="c", evidence_revision="e", observed_at=NOW,
            source_instance="s", nonce="n", old=READ, new=UNSAFE_NEW)
        with self.assertRaises(ContractError):
            ShadowRecord(sequence=1, comparison=comparison, divergence=DivergenceClass.EQUIVALENT,
                         reasons=(), recorded_at=NOW, prior_record_hash=None)


class LedgerTest(ShadowCase):
    def test_empty_ledger_is_inert(self) -> None:
        status = self.ledger.status()
        self.assertEqual((status.record_count, status.head_hash), (0, None))
        self.assertEqual(set(status.counts.values()), {0})
        self.assertEqual(self.ledger.records(), ())
        self.assertIsNone(self.ledger.get("nonce-1"))
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertFalse(summary.accepted)
        with self.assertRaises(ShadowError):
            self.ledger.attestation_evidence(criteria())

    def test_records_bind_identity_revisions_time_source_nonce_and_hashes(self) -> None:
        self.clock.advance(30)
        comparison = self.comparison(HOST, HOST, task_id="task-7",
                                     observed_at=self.clock.now - timedelta(seconds=20))
        record = self.ledger.record(comparison)
        self.assertEqual(record.recorded_at, self.clock.now)
        self.assertEqual(self.ledger.get(comparison.nonce), record)
        self.assertEqual(self.ledger.get_by_hash(record.record_hash), record)
        row = self.tasks.db.execute("SELECT * FROM tc_shadow_comparisons").fetchone()
        self.assertEqual(row["machine_id"], "17049")
        self.assertEqual(row["old_hash"], HOST.content_hash)
        self.assertEqual(row["comparison_hash"], comparison.content_hash)
        self.assertEqual(row["record_hash"], record.content_hash)
        document = record.to_document()["comparison"]
        for name in ("operator_id", "task_id", "request_id", "policy_revision", "config_revision",
                     "evidence_revision", "observed_at", "source_instance", "nonce", "machine_id"):
            self.assertIsNotNone(document[name])
        second = self.record()
        self.assertEqual(second.prior_record_hash, record.record_hash)
        self.assertEqual([item.sequence for item in self.ledger.records()], [1, 2])
        self.assertEqual(self.ledger.records(after_sequence=1, limit=1), (second,))
        with self.assertRaises(ContractError):
            self.ledger.records(limit=shadow_module.MAX_PAGE_RECORDS + 1)

    def test_stale_and_future_observations_fail_closed(self) -> None:
        with self.assertRaises(ShadowError):
            self.record(observed_at=self.clock.now + timedelta(minutes=5, seconds=1))
        with self.assertRaises(ShadowError):
            self.record(observed_at=self.clock.now - timedelta(minutes=10, seconds=1))
        self.record(observed_at=self.clock.now + timedelta(minutes=5))
        self.record(observed_at=self.clock.now - timedelta(minutes=10))
        self.assertEqual(self.ledger.verify_chain(), 2)
        for kwargs in ({"max_clock_skew": timedelta(hours=2)}, {"max_observation_age": timedelta(hours=2)},
                       {"max_clock_skew": timedelta(seconds=-1)}, {"max_clock_skew": 5}):
            with self.assertRaises(ContractError):
                ShadowLedger(self.tasks, clock=self.clock, **kwargs)

    def test_history_ahead_of_the_clock_fails_closed(self) -> None:
        self.clock.advance(7200)
        self.record()
        self.clock.now = NOW
        for read in self.reads():
            with self.assertRaises(ShadowError):
                read()
        with self.assertRaises(ShadowError):
            self.record()
        with self.assertRaises(ShadowError):
            self.reopen()

    def test_record_time_is_monotonic_under_bounded_clock_regression(self) -> None:
        self.clock.advance(60)
        first = self.record()
        self.clock.advance(-30)
        second = self.record()
        self.assertEqual(second.recorded_at, first.recorded_at)
        self.assertEqual(self.ledger.verify_chain(), 2)

    def test_restart_preserves_records_and_status(self) -> None:
        self.record(HOST, HOST)
        self.record(READ, UNSAFE_NEW)
        before = (self.ledger.status(), self.ledger.records())
        self.reopen()
        self.assertEqual((self.ledger.status(), self.ledger.records()), before)
        third = self.record()
        self.assertEqual(third.sequence, 3)
        self.assertEqual(third.prior_record_hash, before[0].head_hash)

    def test_exact_replay_is_idempotent_and_changed_replay_conflicts(self) -> None:
        comparison = self.comparison(HOST, HOST)
        first = self.ledger.record(comparison)
        self.clock.advance(3000)  # a replay stays idempotent after it would be stale
        self.assertEqual(self.ledger.record(comparison), first)
        self.assertEqual(self.reopen().record(ShadowComparison.from_json(comparison.to_json())), first)
        self.assertEqual(self.ledger.verify_chain(), 1)
        self.clock.now = NOW
        for change in (
            {"new": READ}, {"old": READ}, {"request_id": "other"}, {"operator_id": "operator:eve"},
            {"task_id": "task-2"}, {"policy_revision": "policy-2"}, {"config_revision": "config-2"},
            {"evidence_revision": "evidence-2"}, {"source_instance": "gateway-b"},
            {"observed_at": comparison.observed_at + timedelta(microseconds=1)},
        ):
            with self.subTest(change=change), self.assertRaises(ShadowConflict):
                self.ledger.record(dataclasses.replace(comparison, **change))
        self.assertEqual(self.ledger.verify_chain(), 1)

    def test_one_source_request_is_one_sample_whatever_the_nonce(self) -> None:
        comparison = self.comparison(request_id="request-x")
        first = self.ledger.record(comparison)
        for index in range(20):
            self.clock.advance(60)
            with self.assertRaises(ShadowConflict) as caught:
                self.ledger.record(dataclasses.replace(
                    comparison, nonce=f"fresh-{index}", observed_at=self.clock.now))
            self.assertIn("different capture facts", str(caught.exception))
        # Byte-identical content under a fresh nonce is still not a replay.
        with self.assertRaises(ShadowConflict):
            self.ledger.record(dataclasses.replace(comparison, nonce="fresh-identical"))
        self.assertEqual(self.ledger.verify_chain(), 1)
        # The same request id from another source instance is a different request.
        other = self.ledger.record(dataclasses.replace(
            comparison, nonce="other-source", source_instance="gateway-b",
            observed_at=self.clock.now))
        self.assertEqual(other.sequence, 2)
        # One nonce and another row's request can never both be "the" replay.
        with self.assertRaises(ShadowConflict):
            self.ledger.record(dataclasses.replace(
                comparison, source_instance="gateway-b", observed_at=self.clock.now))
        # The exact replay is idempotent, and all of it survives a restart.
        self.assertEqual(self.ledger.record(comparison), first)
        self.reopen()
        self.assertEqual(self.ledger.record(ShadowComparison.from_json(comparison.to_json())), first)
        with self.assertRaises(ShadowConflict):
            self.ledger.record(dataclasses.replace(comparison, nonce="after-restart"))
        self.assertEqual(self.ledger.verify_chain(), 2)

    def test_fresh_nonces_cannot_inflate_canary_samples(self) -> None:
        comparison = self.comparison()
        self.ledger.record(comparison)
        for index in range(9):
            self.clock.advance(600)
            with self.assertRaises(ShadowConflict):
                self.ledger.record(dataclasses.replace(
                    comparison, nonce=f"inflate-{index}", observed_at=self.clock.now))
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertEqual(summary.sample_count, 1)
        self.assertFalse(summary.accepted)

    def test_request_binding_is_durable_in_the_schema(self) -> None:
        first = self.record()
        row = self.tasks.db.execute(
            f"SELECT {shadow_module._COLUMNS} FROM tc_shadow_comparisons").fetchone()
        self.assertEqual((row["source_instance"], row["request_id"]), ("gateway-a", "request-1"))
        values = dict(zip(shadow_module._COLUMNS.split(","), tuple(row)))
        values.update(sequence=2, record_hash="0" * 64, prior_record_hash=first.record_hash,
                      nonce="raw-fresh-nonce")
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.tasks.db.execute(
                f"INSERT INTO tc_shadow_comparisons({shadow_module._COLUMNS}) VALUES("
                + ",".join("?" for _ in values) + ")", tuple(values.values()))
        self.tasks.db.rollback()
        self.assertIn("source_instance", str(caught.exception))
        self.assertIn("request_id", str(caught.exception))
        self.assertEqual(self.ledger.verify_chain(), 1)

    def test_concurrent_inserts_form_one_valid_chain(self) -> None:
        workers, each = 6, 5
        errors: list[BaseException] = []
        barrier = threading.Barrier(workers)

        def work(worker: int) -> None:
            try:
                tasks = TaskStore(self.root, clock=self.clock)
                try:
                    ledger = ShadowLedger(tasks, clock=self.clock)
                    barrier.wait(timeout=30)
                    for index in range(each):
                        ledger.record(ShadowComparison(
                            origin=ComparisonOrigin.DETECTOR, operator_id=None, task_id=None,
                            request_id=f"request-{worker}-{index}", parent_hash="0" * 64,
                            policy_revision="policy-1",
                            config_revision="config-1", evidence_revision="evidence-1",
                            observed_at=NOW, source_instance=f"gateway-{worker}",
                            nonce=f"nonce-{worker}-{index}", old=READ, new=READ))
                        # Every worker also races the same shared nonce.
                        ledger.record(ShadowComparison(
                            origin=ComparisonOrigin.DETECTOR, operator_id=None, task_id=None,
                            request_id="shared", parent_hash="0" * 64,
                            policy_revision="policy-1",
                            config_revision="config-1", evidence_revision="evidence-1",
                            observed_at=NOW, source_instance="gateway-shared",
                            nonce="nonce-shared", old=READ, new=READ))
                finally:
                    tasks.close()
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=work, args=(worker,)) for worker in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertEqual(self.ledger.verify_chain(), workers * each + 1)
        records = self.ledger.records()
        self.assertEqual([item.sequence for item in records], list(range(1, workers * each + 2)))
        self.assertEqual(len({item.comparison.nonce for item in records}), len(records))


class CaptureAccountingTest(ShadowCase):
    def test_success_is_exactly_one_parent_capture_and_one_sample(self) -> None:
        self.fill(10)
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertEqual(summary.expected_captures, 10)
        self.assertEqual(summary.captured_captures, 10)
        self.assertEqual(summary.failed_captures, 0)
        self.assertEqual(summary.pending_captures, 0)
        self.assertEqual(summary.unaccounted_samples, 0)
        self.assertEqual(summary.sample_count, 10)
        self.assertTrue(summary.accepted, summary.reasons)

    def test_pending_capture_survives_restart_and_blocks_the_window(self) -> None:
        comparison = self.comparison()
        expectation = CaptureExpectation.from_comparison(comparison)
        self.assertEqual(self.ledger.expect_capture(expectation), expectation)
        self.clock.advance(5400)
        for _ in range(10):
            self.record()
        self.reopen()
        self.close_window()
        summary = self.ledger.summary(criteria(minimum_span_seconds=1))
        self.assertEqual(summary.expected_captures, 11)
        self.assertEqual(summary.captured_captures, 10)
        self.assertEqual(summary.pending_captures, 1)
        self.assertEqual(summary.sample_count, 10)
        self.assertFalse(summary.accepted)
        self.assertTrue(any("unresolved" in reason for reason in summary.reasons))

    def test_failed_capture_is_idempotent_durable_and_blocks(self) -> None:
        comparison = self.comparison()
        expectation = CaptureExpectation.from_comparison(comparison)
        first = self.ledger.record_capture_failure(expectation, "adapter-unknown")
        self.assertIs(first.outcome, CaptureOutcome.FAILED)
        self.clock.advance(60)
        self.assertEqual(
            self.ledger.record_capture_failure(expectation, "adapter-unknown"), first
        )
        self.reopen()
        self.assertEqual(
            self.ledger.record_capture_failure(expectation, "adapter-unknown"), first
        )
        with self.assertRaises(ShadowConflict):
            self.ledger.record_capture_failure(expectation, "legacy-observation-failed")
        with self.assertRaises(ShadowConflict):
            self.ledger.record(comparison)
        self.clock.advance(5340)
        for _ in range(10):
            self.record()
        self.close_window()
        summary = self.ledger.summary(criteria(minimum_span_seconds=1))
        self.assertEqual(summary.failed_captures, 1)
        self.assertFalse(summary.accepted)
        self.assertTrue(any("captures failed" in reason for reason in summary.reasons))

    def test_selective_failed_capture_deletion_breaks_capture_chain(self) -> None:
        expectation = CaptureExpectation.from_comparison(self.comparison())
        result = self.ledger.record_capture_failure(expectation, "adapter-unknown")
        self.clock.advance(60)
        for _ in range(10):
            self.record()
        for trigger in (
            "tc_shadow_capture_expectations_immutable_delete",
            "tc_shadow_capture_results_immutable_delete",
            "tc_shadow_capture_events_immutable_delete",
        ):
            self.tasks.db.execute(f"DROP TRIGGER {trigger}")
        self.tasks.db.execute(
            "DELETE FROM tc_shadow_capture_results WHERE result_hash=?",
            (result.content_hash,),
        )
        self.tasks.db.execute(
            "DELETE FROM tc_shadow_capture_expectations WHERE expectation_hash=?",
            (expectation.content_hash,),
        )
        self.tasks.db.execute(
            "DELETE FROM tc_shadow_capture_events WHERE payload_hash IN (?,?)",
            (expectation.content_hash, result.content_hash),
        )
        for statement in (
            shadow_module._CAPTURE_EXPECTATIONS_DELETE_TRIGGER_SQL,
            shadow_module._CAPTURE_RESULTS_DELETE_TRIGGER_SQL,
            shadow_module._CAPTURE_EVENTS_DELETE_TRIGGER_SQL,
        ):
            self.tasks.db.execute(statement)
        self.tasks.db.commit()
        with self.assertRaises(ShadowTampered):
            self.ledger.verify_chain()

    def test_capture_verification_does_not_rescan_comparisons_per_result(self) -> None:
        self.fill(10)
        statements: list[str] = []
        self.tasks.db.set_trace_callback(statements.append)
        try:
            self.ledger.verify_chain()
        finally:
            self.tasks.db.set_trace_callback(None)
        self.assertFalse(any(
            "WHERE comparison_hash" in statement for statement in statements
        ))

    def test_parent_identity_conflict_cannot_create_domain_samples(self) -> None:
        comparison = self.comparison(
            origin=ComparisonOrigin.DETECTOR,
            operator_id=None,
            request_id="detector-batch-1",
            source_instance="detector-a",
        )
        first = self.ledger.record(comparison)
        with self.assertRaises(ShadowConflict):
            self.ledger.record(dataclasses.replace(
                comparison,
                nonce="domain-two",
                task_id="task-domain-two",
            ))
        self.assertEqual(self.ledger.record(comparison), first)
        self.assertEqual(self.ledger.verify_chain(), 1)
        rows = self.tasks.db.execute(
            "SELECT COUNT(*) FROM tc_shadow_capture_expectations"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_changed_authenticated_parent_facts_conflict(self) -> None:
        expectation = CaptureExpectation.from_comparison(self.comparison())
        self.ledger.expect_capture(expectation)
        for changed in (
            dataclasses.replace(expectation, source_instance="gateway-b"),
            dataclasses.replace(expectation, observed_at=expectation.observed_at + timedelta(seconds=1)),
            dataclasses.replace(expectation, policy_revision="policy-2"),
            dataclasses.replace(expectation, parent_hash="1" * 64),
            dataclasses.replace(expectation, origin=ComparisonOrigin.DETECTOR),
        ):
            if changed.source_instance != expectation.source_instance:
                # A source instance is part of the parent namespace, so the same
                # request spelling there is intentionally a different parent.
                self.assertEqual(self.ledger.expect_capture(changed), changed)
            else:
                with self.assertRaises(ShadowConflict):
                    self.ledger.expect_capture(changed)

    def test_capture_documents_are_strict_and_non_authoritative(self) -> None:
        expectation = CaptureExpectation.from_comparison(self.comparison())
        result = self.ledger.record_capture_failure(expectation, "adapter-failed")
        self.assertEqual(CaptureExpectation.from_json(expectation.to_json()), expectation)
        self.assertEqual(CaptureResult.from_json(result.to_json()), result)
        document = result.to_document()
        document["execution_authority"] = True
        with self.assertRaises(ContractError):
            CaptureResult.from_document(document)
        with self.assertRaises(ContractError):
            CaptureResult(
                expectation.source_instance,
                expectation.request_id,
                expectation.content_hash,
                CaptureOutcome.CAPTURED,
                self.clock.now,
                failure_code="both",
            )

    def test_capture_row_tampering_fails_closed(self) -> None:
        expectation = CaptureExpectation.from_comparison(self.comparison())
        self.ledger.record_capture_failure(expectation, "adapter-failed")
        self.raw(
            "DROP TRIGGER tc_shadow_capture_results_immutable_update",
            "UPDATE tc_shadow_capture_results SET outcome='captured'",
            shadow_module._CAPTURE_RESULTS_UPDATE_TRIGGER_SQL,
        )
        self.close_window()
        for call in (self.ledger.verify_chain, lambda: self.ledger.summary(criteria())):
            with self.assertRaises(ShadowTampered):
                call()


class TamperTest(ShadowCase):
    def setUp(self) -> None:
        super().setUp()
        self.record(HOST, HOST)
        self.record(READ, UNSAFE_NEW)

    def assertFailsClosed(self, kind=ShadowTampered) -> None:
        self.close_window()
        for read in self.reads():
            with self.assertRaises(kind):
                read()
        with self.assertRaises(kind):
            self.record()
        with self.assertRaises(kind):
            self.reopen()

    def tamper(self, statement: str) -> None:
        self.raw(
            "DROP TRIGGER tc_shadow_comparisons_immutable_update",
            "DROP TRIGGER tc_shadow_comparisons_immutable_delete",
            statement,
            shadow_module._UPDATE_TRIGGER_SQL, shadow_module._DELETE_TRIGGER_SQL,
        )

    def test_rows_are_immutable_in_process(self) -> None:
        for statement in ("UPDATE tc_shadow_comparisons SET divergence='equivalent'",
                          "DELETE FROM tc_shadow_comparisons"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.tasks.db.execute(statement)
            self.tasks.db.rollback()
        self.assertEqual(self.ledger.verify_chain(), 2)

    def test_edited_json_is_detected(self) -> None:
        self.tamper(
            "UPDATE tc_shadow_comparisons SET record_json=CAST(REPLACE(record_json,"
            "'operator:edgar','operator:mallory') AS BLOB) WHERE sequence=2")
        self.assertFailsClosed()

    def test_reclassifying_unsafe_as_equivalent_is_detected_even_rehashed(self) -> None:
        self.tamper(
            "UPDATE tc_shadow_comparisons SET divergence='equivalent', record_json=CAST(REPLACE("
            "record_json,'\"divergence\":\"more-permissive-new\"','\"divergence\":\"equivalent\"')"
            " AS BLOB) WHERE sequence=2")
        self.assertFailsClosed()

    def test_every_column_mismatch_is_detected(self) -> None:
        values = {
            "record_hash": "'" + "0" * 64 + "'", "prior_record_hash": "'" + "1" * 64 + "'",
            "nonce": "'nonce-x'", "comparison_hash": "'" + "2" * 64 + "'",
            "old_hash": "'" + "3" * 64 + "'", "new_hash": "'" + "4" * 64 + "'",
            "divergence": "'equivalent'", "policy_revision": "'policy-9'",
            "config_revision": "'config-9'", "evidence_revision": "'evidence-9'",
            "source_instance": "'gateway-z'", "request_id": "'request-z'", "observed_utc": "'2026-09-21T11:59:00.000000Z'",
            "recorded_utc": "'2026-09-21T12:30:00.000000Z'", "sequence": "7",
            "record_json": "CAST(record_json AS TEXT)",
        }
        self.assertEqual(set(values) | {"machine_id"}, set(shadow_module._COLUMNS.split(",")))
        for column, value in values.items():
            with self.subTest(column=column):
                root = Path(tempfile.mkdtemp(dir=self.root))
                tasks = TaskStore(root, clock=self.clock)
                self.addCleanup(tasks.close)
                ledger = ShadowLedger(tasks, clock=self.clock)
                ledger.record(self.comparison(HOST, HOST))
                ledger.record(self.comparison(READ, UNSAFE_NEW))
                for statement in (
                    "DROP TRIGGER tc_shadow_comparisons_immutable_update",
                    f"UPDATE tc_shadow_comparisons SET {column}={value} WHERE sequence=2",
                    shadow_module._UPDATE_TRIGGER_SQL,
                ):
                    tasks.db.execute(statement)
                tasks.db.commit()
                with self.assertRaises(ShadowTampered):
                    ledger.verify_chain()
                with self.assertRaises(ShadowTampered):
                    ledger.record(self.comparison())
                with self.assertRaises(ShadowTampered):
                    ShadowLedger(tasks, clock=self.clock)

    def test_machine_column_is_pinned_by_the_schema(self) -> None:
        self.raw("DROP TRIGGER tc_shadow_comparisons_immutable_update")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks.db.execute("UPDATE tc_shadow_comparisons SET machine_id='17050'")
        self.tasks.db.rollback()

    def test_interior_deletion_and_reordering_are_detected(self) -> None:
        self.record()
        self.tamper("DELETE FROM tc_shadow_comparisons WHERE sequence=2")
        self.assertFailsClosed()

    def test_schema_damage_fails_closed_and_is_never_repaired(self) -> None:
        damages = (
            ("DROP TRIGGER tc_shadow_comparisons_immutable_update",),
            ("DROP TRIGGER tc_shadow_comparisons_immutable_delete",),
            ("DROP INDEX tc_shadow_comparisons_recorded",),
            ("DROP TRIGGER tc_shadow_comparisons_immutable_update",
             "CREATE TRIGGER tc_shadow_comparisons_immutable_update BEFORE UPDATE ON "
             "tc_shadow_comparisons WHEN 0 BEGIN SELECT RAISE(ABORT, 'immutable shadow comparison'); END"),
            ("CREATE TRIGGER tc_shadow_extra AFTER INSERT ON tc_shadow_comparisons "
             "BEGIN SELECT 1; END",),
            ("CREATE INDEX tc_shadow_extra_index ON tc_shadow_comparisons(nonce)",),
            ("ALTER TABLE tc_shadow_comparisons ADD COLUMN note TEXT",),
            ("DELETE FROM tc_shadow_schema",),
            ("UPDATE tc_shadow_schema SET version=0",),
            ("DROP TABLE tc_shadow_schema",),
        )
        for statements in damages:
            with self.subTest(statements=statements):
                root = Path(tempfile.mkdtemp(dir=self.root))
                tasks = TaskStore(root, clock=self.clock)
                self.addCleanup(tasks.close)
                ledger = ShadowLedger(tasks, clock=self.clock)
                ledger.record(self.comparison())
                for statement in statements:
                    tasks.db.execute(statement)
                tasks.db.commit()
                for call in (ledger.status, ledger.verify_chain,
                             lambda: ledger.record(self.comparison()),
                             lambda: ShadowLedger(tasks, clock=self.clock)):
                    with self.assertRaises(ShadowTampered):
                        call()

    def test_newer_schema_version_fails_closed(self) -> None:
        self.assertEqual(shadow_module.SHADOW_SCHEMA_VERSION, 5)
        self.raw("UPDATE tc_shadow_schema SET version=6")
        self.assertFailsClosed(ShadowError)

    def test_version_one_database_fails_closed_and_is_never_migrated(self) -> None:
        # The exact version 1 comparisons table: no request binding.
        old_table = shadow_module._COMPARISONS_TABLE_SQL.replace(
            "request_id TEXT NOT NULL,", "").replace(
            ",\n                              UNIQUE(source_instance, request_id))", ")")
        self.assertNotIn("request_id", old_table)
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                root = Path(tempfile.mkdtemp(dir=self.root))
                tasks = TaskStore(root, clock=self.clock)
                self.addCleanup(tasks.close)
                for statement in (
                    shadow_module._SCHEMA_TABLE_SQL, old_table, shadow_module._RECORDED_INDEX_SQL,
                    shadow_module._UPDATE_TRIGGER_SQL, shadow_module._DELETE_TRIGGER_SQL,
                    f"INSERT INTO tc_shadow_schema(namespace,version) VALUES('shadow',{version})",
                ):
                    tasks.db.execute(statement)
                tasks.db.commit()
                with self.assertRaises(ShadowTampered):
                    ShadowLedger(tasks, clock=self.clock)
                columns = [row["name"] for row in tasks.db.execute(
                    "PRAGMA table_info(tc_shadow_comparisons)")]
                self.assertNotIn("request_id", columns)

    def test_weakened_request_binding_fails_closed(self) -> None:
        weakened = shadow_module._COMPARISONS_TABLE_SQL.replace(
            ",\n                              UNIQUE(source_instance, request_id))", ")")
        self.assertIn("request_id", weakened)
        root = Path(tempfile.mkdtemp(dir=self.root))
        tasks = TaskStore(root, clock=self.clock)
        self.addCleanup(tasks.close)
        for statement in (
            shadow_module._SCHEMA_TABLE_SQL, weakened, shadow_module._RECORDED_INDEX_SQL,
            shadow_module._UPDATE_TRIGGER_SQL, shadow_module._DELETE_TRIGGER_SQL,
            f"INSERT INTO tc_shadow_schema(namespace,version) VALUES('shadow',{shadow_module.SHADOW_SCHEMA_VERSION})",
        ):
            tasks.db.execute(statement)
        tasks.db.commit()
        with self.assertRaises(ShadowTampered):
            ShadowLedger(tasks, clock=self.clock)

    def test_a_repeated_source_request_in_history_is_detected(self) -> None:
        # A raw-database attacker who rebuilds the table without the UNIQUE
        # binding still cannot present a repeated request as valid history.
        self.assertEqual(self.ledger.verify_chain(), 2)
        head = self.ledger.records()[-1]
        forged = ShadowRecord(
            sequence=3, comparison=dataclasses.replace(head.comparison, nonce="forged-nonce"),
            divergence=head.divergence, reasons=head.reasons, recorded_at=head.recorded_at,
            prior_record_hash=head.record_hash)
        values = shadow_module._column_values(forged)
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks.db.execute(
                f"INSERT INTO tc_shadow_comparisons({shadow_module._COLUMNS}) VALUES("
                + ",".join("?" for _ in values) + ")", tuple(values.values()))
        self.tasks.db.rollback()
        with mock.patch.object(ShadowLedger, "_verify_schema", lambda self: None):
            rows = [tuple(row) for row in self.tasks.db.execute(
                f"SELECT {shadow_module._COLUMNS} FROM tc_shadow_comparisons ORDER BY sequence")]
            self.raw("DROP TABLE tc_shadow_comparisons",
                     shadow_module._COMPARISONS_TABLE_SQL.replace(
                         ",\n                              UNIQUE(source_instance, request_id))", ")"))
            for row in (*rows, tuple(values.values())):
                self.tasks.db.execute(
                    f"INSERT INTO tc_shadow_comparisons({shadow_module._COLUMNS}) VALUES("
                    + ",".join("?" for _ in row) + ")", row)
            self.tasks.db.commit()
            with self.assertRaises(ShadowTampered) as caught:
                self.ledger.verify_chain()
            self.assertIn("repeats a source request", str(caught.exception))
        with self.assertRaises(ShadowTampered):
            self.ledger.verify_chain()


class CoverageTest(ShadowCase):
    def test_rules_cover_every_capability_and_are_immutable(self) -> None:
        self.assertEqual(set(CAPABILITY_COVERAGE), set(RolloutCapability))
        self.assertTrue(all(CAPABILITY_COVERAGE.values()))
        self.assertEqual(set().union(*CAPABILITY_COVERAGE.values()), set(CoverageClass))
        with self.assertRaises(TypeError):
            CAPABILITY_COVERAGE[RolloutCapability.HOST_ACTIONS] = frozenset()

    def test_readonly_evidence_cannot_be_reused_for_any_other_capability(self) -> None:
        # Names and prose deliberately claim every other capability.
        named = decision(policy(name="host-tenant-external-detector-standing-legacy-handover"))
        self.fill(10, old=named, new=named)
        self.close_window()
        for capability in RolloutCapability:
            with self.subTest(capability=capability):
                bound = criteria(capability=capability)
                summary = self.ledger.summary(bound)
                self.assertEqual(summary.capability, capability)
                self.assertEqual(summary.coverage_counts, {
                    kind: 10 if kind is CoverageClass.OPERATOR_READONLY else 0
                    for kind in CoverageClass
                })
                self.assertEqual(summary.accepted, capability is RolloutCapability.OPERATOR_READONLY)
                if capability is not RolloutCapability.OPERATOR_READONLY:
                    with self.assertRaises(ShadowError):
                        self.ledger.attestation_evidence(bound)
                    with self.assertRaises(ShadowError):
                        self.ledger.verify_evidence(summary.to_document())

    def test_each_capability_has_positive_durable_evidence(self) -> None:
        owned = decision(approval(owned_component=True))
        standing = decision(policy(authority=Authority.STANDING_CONSENT, owned_component=True))
        tenant = decision(approval(Authority.ALWAYS_APPROVE_TENANT, tenant=True))
        external = decision(approval(external_commitment=True))
        cases = (
            (RolloutCapability.OPERATOR_READONLY, READ, READ, ComparisonOrigin.OPERATOR),
            (RolloutCapability.OWNED_COMPONENT_MUTATION, owned, owned, ComparisonOrigin.OPERATOR),
            (RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT, standing, standing, ComparisonOrigin.OPERATOR),
            (RolloutCapability.HOST_ACTIONS, HOST, HOST, ComparisonOrigin.OPERATOR),
            (RolloutCapability.TENANT_ACTIONS, tenant, tenant, ComparisonOrigin.OPERATOR),
            (RolloutCapability.EXTERNAL_WRITES, external, external, ComparisonOrigin.OPERATOR),
            (RolloutCapability.DETECTOR_CREATED_TASKS, READ, READ, ComparisonOrigin.DETECTOR),
            (RolloutCapability.LEGACY_LOOP_RETIREMENT,
             decision(approval(host=True), RouteKind.EXISTING_TASK, "task-1"),
             decision(approval(host=True), RouteKind.LEGACY_HANDOVER, "task-1"),
             ComparisonOrigin.DETECTOR),
        )
        for capability, old, new, origin in cases:
            with self.subTest(capability=capability):
                self.doCleanups()
                self.setUp()
                self.fill(10, old=old, new=new, origin=origin,
                          operator_id="operator:edgar" if origin is ComparisonOrigin.OPERATOR else None)
                self.close_window()
                bound = criteria(capability=capability, max_routing_mismatch=10)
                summary = self.ledger.summary(bound)
                self.assertTrue(summary.accepted, summary.reasons)
                for kind in CAPABILITY_COVERAGE[capability]:
                    self.assertEqual(summary.coverage_counts[kind], 10)
                self.assertEqual(CanarySummary.from_json(summary.to_json()).content_hash, summary.content_hash)
                self.assertEqual(self.reopen().verify_evidence(summary.to_document()).content_hash,
                                 summary.content_hash)
                self.assertFalse(summary.execution_authority)

    def test_tenant_coverage_requires_tenant_authority_even_when_paths_agree(self) -> None:
        for authority in Authority:
            with self.subTest(authority=authority):
                self.doCleanups()
                self.setUp()
                choice = (approval(authority, tenant=True)
                          if authority in (Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT)
                          else policy(authority=authority, tenant=True))
                sample = decision(choice)
                self.fill(10, old=sample, new=sample)
                self.close_window()
                bound = criteria(capability=RolloutCapability.TENANT_ACTIONS)
                summary = self.ledger.summary(bound)
                valid = authority is Authority.ALWAYS_APPROVE_TENANT
                self.assertEqual(summary.coverage_counts[CoverageClass.TENANT_EFFECT], 10 if valid else 0)
                self.assertEqual(summary.accepted, valid)
                if not valid:
                    self.assertEqual(summary.counts[DivergenceClass.MORE_PERMISSIVE_NEW], 10)
                    with self.assertRaises(ShadowError):
                        self.ledger.attestation_evidence(bound)

    def test_owned_coverage_requires_exact_reversible_flags_and_authority_mode(self) -> None:
        for authority in (Authority.STANDING_CONSENT, Authority.EXACT_HUMAN):
            for extra in (None, "host", "tenant", "external_commitment", "reachability", "secrets", "irreversible"):
                flags = {"owned_component": True}
                if extra:
                    flags[extra] = True
                choice = (approval(authority, **flags) if authority is Authority.EXACT_HUMAN
                          else policy(authority=authority, **flags))
                sample = decision(choice)
                coverage = derive_coverage(self.comparison(sample, sample))
                expected = (CoverageClass.OWNED_COMPONENT_MUTATION if authority is Authority.EXACT_HUMAN
                            else CoverageClass.OWNED_COMPONENT_STANDING_CONSENT)
                for kind in (CoverageClass.OWNED_COMPONENT_MUTATION, CoverageClass.OWNED_COMPONENT_STANDING_CONSENT):
                    self.assertEqual(kind in coverage, extra is None and kind is expected)

    def test_all_effect_combinations_use_flags_and_respect_tenant_floor(self) -> None:
        # Every combination of the seven current flags, with sufficient authority.
        names = shadow_module.EFFECT_FLAG_NAMES
        for mask in range(1 << len(names)):
            flags = {name: bool(mask & (1 << index)) for index, name in enumerate(names)}
            authority = Authority.ALWAYS_APPROVE_TENANT if flags["tenant"] else Authority.EXACT_HUMAN
            sample = decision(approval(authority, **flags))
            coverage = derive_coverage(self.comparison(sample, sample))
            for flag, kind in (("host", CoverageClass.HOST_EFFECT), ("tenant", CoverageClass.TENANT_EFFECT),
                               ("external_commitment", CoverageClass.EXTERNAL_COMMITMENT)):
                self.assertEqual(kind in coverage, flags[flag], (flags, kind))
            self.assertNotIn(CoverageClass.OWNED_COMPONENT_STANDING_CONSENT, coverage)
            self.assertNotIn(CoverageClass.OPERATOR_READONLY, coverage)

    def test_suppressed_missing_denied_and_unknown_policy_earn_no_coverage(self) -> None:
        for route in RouteKind:
            task = "task-1" if route in (RouteKind.EXISTING_TASK, RouteKind.DUPLICATE, RouteKind.LEGACY_HANDOVER) else None
            for choice in (None, policy(PolicyOutcome.DENY), policy(PolicyOutcome.UNKNOWN, None)):
                sample = decision(choice, route, task)
                self.assertEqual(derive_coverage(self.comparison(sample, sample)), frozenset())
            if route not in (RouteKind.NEW_TASK, RouteKind.EXISTING_TASK, RouteKind.LEGACY_HANDOVER):
                sample = decision(approval(host=True), route, task)
                self.assertEqual(derive_coverage(self.comparison(sample, sample)), frozenset())

    def test_detector_origin_cannot_be_asserted_by_names_or_operator_samples(self) -> None:
        self.assertNotIn(CoverageClass.DETECTOR_ROUTING, derive_coverage(self.comparison(HOST, HOST)))
        detector = self.comparison(READ, READ, origin=ComparisonOrigin.DETECTOR, operator_id=None)
        self.assertEqual(derive_coverage(detector), frozenset({CoverageClass.DETECTOR_ROUTING}))

    def test_retirement_requires_explicit_same_task_detector_handover_with_effect(self) -> None:
        old = decision(approval(host=True), RouteKind.EXISTING_TASK, "task-1")
        new = decision(approval(host=True), RouteKind.LEGACY_HANDOVER, "task-1")
        valid = self.comparison(old, new, origin=ComparisonOrigin.DETECTOR, operator_id=None)
        self.assertIn(CoverageClass.LEGACY_HANDOVER, derive_coverage(valid))
        for changed in (
            dataclasses.replace(valid, origin=ComparisonOrigin.OPERATOR, operator_id="operator:edgar"),
            dataclasses.replace(valid, new=old),
            dataclasses.replace(valid, old=new),
            dataclasses.replace(valid, old=HOST),
            dataclasses.replace(valid, new=decision(approval(host=True), RouteKind.LEGACY_HANDOVER, "task-2")),
            dataclasses.replace(valid, old=decision(policy(), RouteKind.EXISTING_TASK, "task-1"),
                                new=decision(policy(), RouteKind.LEGACY_HANDOVER, "task-1")),
            dataclasses.replace(valid, old=decision(approval(host=True), RouteKind.DUPLICATE, "task-1")),
        ):
            self.assertNotIn(CoverageClass.LEGACY_HANDOVER, derive_coverage(changed))
        with self.assertRaises(ContractError):
            RoutingDecision(RouteKind.LEGACY_HANDOVER)
        # Ordinary detector effect samples never substitute for a handover.
        self.fill(10, old=HOST, new=HOST, origin=ComparisonOrigin.DETECTOR, operator_id=None)
        self.close_window()
        bound = criteria(capability=RolloutCapability.LEGACY_LOOP_RETIREMENT)
        self.assertEqual(self.ledger.summary(bound).coverage_counts[CoverageClass.LEGACY_HANDOVER], 0)
        with self.assertRaises(ShadowError):
            self.ledger.attestation_evidence(bound)

    def test_coverage_minimum_is_exact_and_cannot_be_padded_with_other_samples(self) -> None:
        self.fill(9, old=HOST, new=HOST, span=4000)
        self.fill(10, span=1000)
        self.close_window()
        bound = criteria(capability=RolloutCapability.HOST_ACTIONS)
        summary = self.ledger.summary(bound)
        self.assertEqual(summary.sample_count, 19)
        self.assertEqual(summary.coverage_counts[CoverageClass.HOST_EFFECT], 9)
        self.assertFalse(summary.accepted)
        self.clock.now = NOW + timedelta(seconds=5500)
        self.record(HOST, HOST)
        self.close_window()
        accepted = self.ledger.summary(bound)
        self.assertTrue(accepted.accepted, accepted.reasons)
        tightened = dict(bound.coverage_minimums)
        tightened[CoverageClass.HOST_EFFECT] = 11
        self.assertFalse(self.ledger.summary(dataclasses.replace(bound, coverage_minimums=tightened)).accepted)

    def test_coverage_contracts_are_closed_strict_and_minimums_cannot_be_weakened(self) -> None:
        bound = criteria(capability=RolloutCapability.HOST_ACTIONS)
        for change in ({}, {"host-effect": 10}, dict(bound.coverage_minimums, unknown=0)):
            with self.assertRaises(ContractError):
                dataclasses.replace(bound, coverage_minimums=change)
        for invalid in (0, 9, -1, True, 10.0, 100001):
            damaged = dict(bound.coverage_minimums)
            damaged[CoverageClass.HOST_EFFECT] = invalid
            with self.assertRaises(ContractError):
                dataclasses.replace(bound, coverage_minimums=damaged)
        with self.assertRaises(ContractError):
            dataclasses.replace(bound, capability="host-actions")
        with self.assertRaises(TypeError):
            bound.coverage_minimums[CoverageClass.HOST_EFFECT] = 0
        self.fill(10)
        self.close_window()
        summary = self.ledger.summary(criteria())
        for invalid in (True, -1, 1.0, 11):
            damaged = dict(summary.coverage_counts)
            damaged[CoverageClass.OPERATOR_READONLY] = invalid
            with self.assertRaises(ContractError):
                dataclasses.replace(summary, coverage_counts=damaged)
        for field in ("coverage_counts", "capability"):
            damaged = summary.to_document()
            del damaged[field]
            with self.assertRaises(ContractError):
                CanarySummary.from_document(damaged)
        damaged = summary.to_document()
        damaged["capability"] = RolloutCapability.HOST_ACTIONS.value
        with self.assertRaises(ContractError):
            CanarySummary.from_document(damaged)
        for invalid in ({}, [], {"unknown": 10}):
            damaged = summary.to_document()
            damaged["coverage_counts"] = invalid
            with self.assertRaises(ContractError):
                CanarySummary.from_document(damaged)

    def test_forged_coverage_and_rebound_capability_fail_durable_rederivation(self) -> None:
        self.fill(10)
        self.close_window()
        summary = self.ledger.summary(criteria())
        forged_counts = dict(summary.coverage_counts)
        forged_counts[CoverageClass.HOST_EFFECT] = 10
        # All derived fields/hashes/reasons are internally coherent. Only durable
        # rederivation can show these asserted host samples never happened.
        forged = dataclasses.replace(summary, criteria=criteria(capability=RolloutCapability.HOST_ACTIONS),
                                     coverage_counts=forged_counts)
        self.assertTrue(forged.accepted)
        CanarySummary.from_document(forged.to_document())
        with self.assertRaises(ShadowTampered):
            self.ledger.verify_evidence(forged.to_document())
        # Even a forged optional count cannot sneak into otherwise accepted evidence.
        forged = dataclasses.replace(summary, coverage_counts=forged_counts)
        with self.assertRaises(ShadowTampered):
            self.ledger.verify_evidence(forged.to_document())

    def test_nonqualifying_records_never_contribute_coverage(self) -> None:
        self.fill(10)
        self.clock.now = NOW + timedelta(seconds=5500)
        self.record(HOST, HOST, config_revision="config-other")
        self.record(HOST, HOST, source_instance="unexpected")
        self.clock.now = WINDOW_END - timedelta(seconds=1)
        self.record(HOST, HOST, observed_at=WINDOW_END)
        self.clock.now = WINDOW_END
        self.record(HOST, HOST)
        self.close_window()
        summary = self.ledger.summary(criteria(capability=RolloutCapability.HOST_ACTIONS))
        self.assertEqual(summary.sample_count, 10)
        self.assertEqual(summary.coverage_counts[CoverageClass.HOST_EFFECT], 0)
        self.assertEqual((summary.cross_revision_samples, summary.foreign_source_samples,
                          summary.out_of_window_observations), (1, 1, 1))
        self.assertFalse(summary.accepted)

    def test_old_effects_and_unsafe_handovers_never_supply_new_coverage(self) -> None:
        self.assertEqual(derive_coverage(self.comparison(HOST, READ)), frozenset())
        for old in (decision(None, RouteKind.EXISTING_TASK, "task-1"),
                    decision(approval(host=True), RouteKind.REJECTED)):
            new = decision(approval(host=True), RouteKind.LEGACY_HANDOVER, "task-1")
            comparison = self.comparison(old, new, origin=ComparisonOrigin.DETECTOR, operator_id=None)
            self.assertEqual(derive_coverage(comparison), frozenset())

    def test_version_two_documents_and_database_are_refused(self) -> None:
        self.fill(10)
        self.close_window()
        for contract in (self.ledger.records()[0], self.ledger.summary(criteria()), criteria()):
            damaged = contract.to_document()
            damaged["schema_version"] = 2
            with self.assertRaises(ContractError):
                type(contract).from_document(damaged)
        self.raw("UPDATE tc_shadow_schema SET version=2")
        with self.assertRaises(ShadowError):
            self.reopen()
        self.assertEqual(self.tasks.db.execute("SELECT version FROM tc_shadow_schema").fetchone()[0], 2)


class CanaryTest(ShadowCase):
    def test_accepted_summary_is_attestation_evidence(self) -> None:
        self.fill(10)
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertTrue(summary.accepted)
        self.assertEqual(summary.sample_count, 10)
        self.assertEqual(summary.counts[DivergenceClass.EQUIVALENT], 10)
        self.assertEqual((summary.first_sequence, summary.last_sequence), (1, 10))
        evidence = self.ledger.attestation_evidence(criteria())
        self.assertIs(evidence["execution_authority"], False)
        self.assertEqual(evidence["machine_id"], "17049")
        attestation = CommissioningAttestation(
            capability=RolloutCapability.OPERATOR_READONLY, version=1,
            policy_revision="policy-1", config_revision="config-1",
            evidence_revision="evidence-1", canary_evidence=evidence,
            statement="shadow canary accepted")
        restored = CommissioningAttestation.from_json(attestation.to_json())
        self.assertEqual(restored.attestation_id, attestation.attestation_id)
        self.assertEqual(self.ledger.verify_evidence(restored.to_document()["canary_evidence"]).content_hash,
                         summary.content_hash)

    def test_summary_hash_is_stable_across_reads_restart_and_later_records(self) -> None:
        self.fill(12)
        self.close_window()
        first = self.ledger.summary(criteria())
        self.assertEqual(self.ledger.summary(criteria()).content_hash, first.content_hash)
        self.assertEqual(CanarySummary.from_json(first.to_json()).content_hash, first.content_hash)
        # Records after the window, even unsafe ones, belong to a later window.
        self.record(READ, UNSAFE_NEW)
        self.clock.advance(1800)
        self.record()
        self.assertEqual(self.reopen().summary(criteria()).content_hash, first.content_hash)
        self.assertEqual(self.ledger.summary(criteria()).canonical_json(), first.canonical_json())
        # Different criteria are different evidence.
        other = self.ledger.summary(criteria(minimum_samples=11))
        self.assertNotEqual(other.content_hash, first.content_hash)
        self.assertNotEqual(other.evidence_root, first.evidence_root)

    def test_minimum_sample_edge(self) -> None:
        self.fill(10)
        self.close_window()
        self.assertTrue(self.ledger.summary(criteria(minimum_samples=10)).accepted)
        rejected = self.ledger.summary(criteria(minimum_samples=11))
        self.assertFalse(rejected.accepted)
        self.assertEqual(len(rejected.reasons), 1)
        self.assertIn("below the required minimum", rejected.reasons[0])

    def test_window_membership_edges(self) -> None:
        tick = timedelta(microseconds=1)
        self.clock.now = WINDOW_START - tick
        self.record()                       # recorded just before the window: not a member
        self.clock.now = WINDOW_START
        self.fill(10, span=3600)            # first sample exactly at window_start: included
        self.clock.now = WINDOW_END - tick
        self.record()                       # final instant before window_end: included
        self.clock.now = WINDOW_END
        self.record(READ, UNSAFE_NEW)       # exactly window_end: next window
        self.clock.now = WINDOW_END + tick
        self.record(READ, UNSAFE_NEW)       # recorded just after: a later window's record
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertTrue(summary.accepted, summary.reasons)
        self.assertEqual(summary.sample_count, 11)
        self.assertEqual((summary.first_sequence, summary.last_sequence), (2, 12))
        self.assertEqual((summary.first_recorded_at, summary.last_recorded_at),
                         (WINDOW_START, WINDOW_END - tick))
        self.assertEqual((summary.first_observed_at, summary.last_observed_at),
                         (WINDOW_START, WINDOW_END - tick))
        self.assertEqual(summary.out_of_window_observations, 0)

    def test_an_unsafe_record_at_exactly_window_end_belongs_to_next_window(self) -> None:
        self.fill(10)
        self.clock.now = WINDOW_END
        self.record(READ, UNSAFE_NEW)
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertEqual(summary.counts[DivergenceClass.MORE_PERMISSIVE_NEW], 0)
        self.assertTrue(summary.accepted, summary.reasons)

    def test_observations_outside_the_window_block_instead_of_disappearing(self) -> None:
        tick = timedelta(microseconds=1)
        for label, recorded, observed in (
            ("observed just before the window", WINDOW_START, WINDOW_START - tick),
            ("observed well before the window", WINDOW_START + timedelta(minutes=9),
             WINDOW_START - timedelta(minutes=1)),
            ("observed just after the window", WINDOW_END - tick, WINDOW_END),
        ):
            with self.subTest(label=label):
                self.setUp()
                if label != "observed just after the window":
                    self.clock.now = recorded
                    self.record(READ, UNSAFE_NEW, observed_at=observed)
                    self.clock.now = WINDOW_START + timedelta(minutes=10)
                    self.fill(10)
                else:
                    self.fill(10)
                    self.clock.now = recorded
                    self.record(READ, UNSAFE_NEW, observed_at=observed)
                self.close_window()
                summary = self.ledger.summary(criteria())
                # Not a sample, so not counted in any class ...
                self.assertEqual(summary.sample_count, 10)
                self.assertEqual(summary.counts[DivergenceClass.MORE_PERMISSIVE_NEW], 0)
                # ... but it never silently disappears: it blocks acceptance.
                self.assertEqual(summary.out_of_window_observations, 1)
                self.assertFalse(summary.accepted)
                self.assertEqual(len(summary.reasons), 1)
                self.assertIn("observed outside", summary.reasons[0])
                with self.assertRaises(ShadowError):
                    self.ledger.attestation_evidence(criteria())
                forged = summary.to_document()
                forged.update(out_of_window_observations=0, accepted=True, reasons=[])
                with self.assertRaises(ShadowTampered):
                    self.ledger.verify_evidence(forged)

    def test_records_outside_the_recorded_window_never_affect_the_summary(self) -> None:
        # Observed inside the window but recorded after it: a later window's record.
        self.fill(10)
        self.close_window()
        before = self.ledger.summary(criteria())
        self.record(READ, UNSAFE_NEW, observed_at=WINDOW_END)
        after = self.ledger.summary(criteria())
        self.assertEqual(after.content_hash, before.content_hash)
        self.assertTrue(after.accepted)

    def test_span_must_be_supported_by_both_observed_and_recorded_time(self) -> None:
        relaxed = dict(max_observation_age=timedelta(hours=1), max_clock_skew=timedelta(hours=1))
        # Recorded across 90 minutes, but every observation made within one burst.
        self.ledger = ShadowLedger(self.tasks, clock=self.clock, **relaxed)
        burst = NOW + timedelta(minutes=45)
        for index in range(10):
            self.record(observed_at=burst + timedelta(seconds=index))
            if index < 9:
                self.clock.advance(600)
        self.clock.now = WINDOW_END + timedelta(hours=1, minutes=1)
        summary = self.ledger.summary(criteria(max_evidence_age_seconds=7200))
        self.assertEqual(summary.last_recorded_at - summary.first_recorded_at, timedelta(seconds=5400))
        self.assertEqual(summary.last_observed_at - summary.first_observed_at, timedelta(seconds=9))
        self.assertFalse(summary.accepted)
        self.assertEqual(len(summary.reasons), 1)
        self.assertIn("observed sample times do not span", summary.reasons[0])
        # Observed across 90 minutes, but all recorded in one burst.
        self.setUp()
        self.ledger = ShadowLedger(self.tasks, clock=self.clock, **relaxed)
        self.clock.now = NOW + timedelta(minutes=45)
        for index in range(10):
            self.record(observed_at=NOW + timedelta(minutes=10 * index))
        self.clock.now = WINDOW_END + timedelta(hours=1, minutes=1)
        summary = self.ledger.summary(criteria(max_evidence_age_seconds=7200))
        self.assertEqual(summary.last_observed_at - summary.first_observed_at, timedelta(seconds=5400))
        self.assertEqual(summary.first_recorded_at, summary.last_recorded_at)
        self.assertFalse(summary.accepted)
        self.assertEqual(len(summary.reasons), 1)
        self.assertIn("recorded sample times do not span", summary.reasons[0])

    def test_observed_bounds_are_extremes_and_are_strictly_bound(self) -> None:
        self.ledger = ShadowLedger(self.tasks, clock=self.clock,
                                   max_observation_age=timedelta(hours=1))
        self.clock.now = NOW + timedelta(minutes=30)
        self.record(observed_at=NOW + timedelta(minutes=20))
        self.record(observed_at=NOW)                       # earliest observation, second record
        self.clock.advance(1)
        self.fill(8, span=5000)
        last_observed = self.clock.now
        self.record(observed_at=self.clock.now - timedelta(minutes=30))   # last record, older observation
        self.close_window()
        summary = self.ledger.summary(criteria())
        self.assertTrue(summary.accepted, summary.reasons)
        self.assertEqual((summary.first_observed_at, summary.last_observed_at), (NOW, last_observed))
        evidence = summary.to_document()
        self.assertEqual(CanarySummary.from_document(evidence).content_hash, summary.content_hash)
        for mutate in (
            lambda d: d.pop("first_observed_at"),
            lambda d: d.pop("out_of_window_observations"),
            lambda d: d.update(first_observed_at=None),
            lambda d: d.update(last_observed_at="2026-09-21T12:00:00+00:00"),
            lambda d: d.update(first_observed_at="2026-09-21T11:59:59.999999Z"),   # before the window
            lambda d: d.update(last_observed_at="2026-09-21T14:00:00.000001Z"),    # after the window
            lambda d: d.update(last_recorded_at="2026-09-21T14:00:00.000001Z"),
            lambda d: d.update(first_observed_at=d["last_observed_at"],
                               last_observed_at=d["first_observed_at"]),           # inverted
            lambda d: d.update(out_of_window_observations=-1),
            lambda d: d.update(out_of_window_observations=True),
        ):
            damaged = copy.deepcopy(evidence)
            mutate(damaged)
            with self.assertRaises(ContractError):
                CanarySummary.from_document(damaged)
            with self.assertRaises(ShadowError):
                self.ledger.verify_evidence(damaged)
        # A well-formed but untrue observed bound parses and then fails re-derivation.
        widened = copy.deepcopy(evidence)
        widened["last_observed_at"] = "2026-09-21T13:59:59.999999Z"
        CanarySummary.from_document(widened)
        with self.assertRaises(ShadowTampered):
            self.ledger.verify_evidence(widened)
        self.assertEqual(self.reopen().summary(criteria()).content_hash, summary.content_hash)

    def test_window_does_not_close_until_the_clock_skew_has_elapsed(self) -> None:
        self.fill(10)
        skew = shadow_module.DEFAULT_MAX_CLOCK_SKEW
        for moment in (WINDOW_END - timedelta(microseconds=1), WINDOW_END,
                       WINDOW_END + timedelta(minutes=1), WINDOW_END + skew):
            self.clock.now = moment
            for call in (self.ledger.summary, self.ledger.attestation_evidence):
                with self.subTest(moment=moment), self.assertRaises(ShadowError) as caught:
                    call(criteria())
                self.assertIn("clock skew", str(caught.exception))
        self.clock.now = WINDOW_END + skew + timedelta(microseconds=1)
        self.assertTrue(self.ledger.summary(criteria()).accepted)
        # A wider configured skew widens the delay.
        wide = ShadowLedger(self.tasks, clock=self.clock, max_clock_skew=timedelta(minutes=30))
        with self.assertRaises(ShadowError):
            wide.summary(criteria())
        self.clock.now = WINDOW_END + timedelta(minutes=30, microseconds=1)
        self.assertTrue(wide.summary(criteria()).accepted)
        # Reducing future-observation tolerance cannot weaken the default
        # post-window regression margin.
        exact = ShadowLedger(self.tasks, clock=self.clock, max_clock_skew=timedelta(0))
        self.clock.now = WINDOW_END + skew
        with self.assertRaises(ShadowError):
            exact.summary(criteria())
        self.clock.now = WINDOW_END + skew + timedelta(microseconds=1)
        self.assertTrue(exact.summary(criteria()).accepted)

    def test_clock_regression_within_skew_cannot_add_to_issued_evidence(self) -> None:
        self.fill(10)
        skew = shadow_module.DEFAULT_MAX_CLOCK_SKEW
        self.clock.now = WINDOW_END + skew + timedelta(microseconds=1)
        evidence = self.ledger.attestation_evidence(criteria())
        issued = self.ledger.summary(criteria())
        # The clock then regresses by the whole tolerated skew and an unsafe
        # comparison arrives.  It lands after window_end, outside the evidence.
        self.clock.now -= skew
        self.assertGreater(self.clock.now, WINDOW_END)
        late = self.record(READ, UNSAFE_NEW)
        self.assertGreater(late.recorded_at, WINDOW_END)
        self.clock.now = WINDOW_END + skew + timedelta(minutes=1)
        self.assertEqual(self.ledger.summary(criteria()).content_hash, issued.content_hash)
        self.assertEqual(self.ledger.verify_evidence(evidence).content_hash, issued.content_hash)
        self.assertEqual(self.reopen().verify_evidence(evidence).content_hash, issued.content_hash)

    def test_every_criteria_source_must_contribute(self) -> None:
        self.fill(12)
        self.clock.now = NOW + timedelta(seconds=5500)
        self.record(source_instance="gateway-b")
        self.close_window()
        # gateway-a alone satisfies the total; a silent gateway-c must still block.
        silent = self.ledger.summary(criteria(
            source_instances=("gateway-a", "gateway-b", "gateway-c"), minimum_samples_per_source=1))
        self.assertEqual(silent.source_counts,
                         (("gateway-a", 12), ("gateway-b", 1), ("gateway-c", 0)))
        self.assertFalse(silent.accepted)
        self.assertEqual(len(silent.reasons), 1)
        self.assertIn("gateway-c contributed 0", silent.reasons[0])
        with self.assertRaises(ShadowError):
            self.ledger.attestation_evidence(silent.criteria)
        both = criteria(source_instances=("gateway-a", "gateway-b"), minimum_samples_per_source=1)
        self.assertTrue(self.ledger.summary(both).accepted)
        # The explicit per-source minimum is exact.
        thin = self.ledger.summary(criteria(source_instances=("gateway-a", "gateway-b"),
                                            minimum_samples_per_source=2))
        self.assertEqual(len(thin.reasons), 1)
        self.assertIn("gateway-b contributed 1", thin.reasons[0])
        # A summary document cannot omit, add, or reorder a source.
        document = self.ledger.summary(both).to_document()
        for mutate in (
            lambda d: d["source_counts"].pop(),
            lambda d: d["source_counts"].reverse(),
            lambda d: d["source_counts"].append({"source_instance": "gateway-z", "samples": 0}),
            lambda d: d["source_counts"][1].update(samples=0),
            lambda d: d["source_counts"][1].update(samples=-1),
        ):
            damaged = copy.deepcopy(document)
            mutate(damaged)
            with self.assertRaises(ContractError):
                CanarySummary.from_document(damaged)
        forged = silent.to_document()
        forged.update(accepted=True, reasons=[])
        with self.assertRaises(ShadowError):
            self.ledger.verify_evidence(forged)
        # The minimum never defaults and cannot be configured away.
        values = {item.name: getattr(both, item.name) for item in dataclasses.fields(CanaryCriteria)
                  if item.init and item.name != "minimum_samples_per_source"}
        with self.assertRaises(TypeError):
            CanaryCriteria(**values)
        for bad in (0, -1, True, 1.0, None, shadow_module.MAX_LEDGER_RECORDS):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                criteria(source_instances=("gateway-a", "gateway-b"), minimum_samples_per_source=bad)

    def test_stale_windows_fail_closed(self) -> None:
        self.fill(10)
        self.clock.now = WINDOW_END + timedelta(seconds=3600)
        evidence = self.ledger.attestation_evidence(criteria())
        self.clock.now = WINDOW_END + timedelta(seconds=3600, microseconds=1)
        for call in (lambda: self.ledger.summary(criteria()),
                     lambda: self.ledger.attestation_evidence(criteria()),
                     lambda: self.ledger.verify_evidence(evidence)):
            with self.assertRaises(ShadowError):
                call()
        # Evidence that would already be stale when the skew delay ends is
        # never issued: such criteria fail closed at every instant.
        brief = criteria(max_evidence_age_seconds=300)
        for offset in (0, 299, 300, 300.000001, 301):
            self.clock.now = WINDOW_END + timedelta(seconds=offset)
            with self.subTest(offset=offset), self.assertRaises(ShadowError):
                self.ledger.summary(brief)

    def test_span_edge(self) -> None:
        self.fill(10, span=3600)
        self.close_window()
        self.assertTrue(self.ledger.summary(criteria(minimum_span_seconds=3600)).accepted)
        rejected = self.ledger.summary(criteria(minimum_span_seconds=3601))
        self.assertFalse(rejected.accepted)
        self.assertIn("do not span", rejected.reasons[0])

    def test_benign_thresholds_are_explicit_and_exact(self) -> None:
        self.fill(10)
        self.clock.now = NOW + timedelta(seconds=5500)
        owned = decision(policy(PolicyOutcome.PERMIT, Authority.STANDING_CONSENT, owned_component=True))
        for _ in range(2):
            self.record(owned, decision(approval(owned_component=True)))          # safer-new
            self.record(READ, decision(policy(), RouteKind.AMBIGUOUS))             # routing
        self.close_window()
        at_threshold = dict(max_safer_new=2, max_routing_mismatch=2)
        summary = self.ledger.summary(criteria(**at_threshold))
        self.assertTrue(summary.accepted, summary.reasons)
        self.assertEqual(summary.sample_count, 14)
        for name, label in (("max_safer_new", "safer-new"),
                            ("max_routing_mismatch", "routing-mismatch")):
            rejected = self.ledger.summary(criteria(**{**at_threshold, name: 1}))
            self.assertEqual(len(rejected.reasons), 1)
            self.assertIn(label, rejected.reasons[0])
            with self.assertRaises(ShadowError):
                self.ledger.attestation_evidence(criteria(**{**at_threshold, name: 1}))
        # No threshold tolerates an approval substitution: it is unsafe, not benign.
        self.clock.now = NOW + timedelta(seconds=5600)
        self.record(decision(approval(refs=("a", "b"), host=True)),
                    decision(approval(refs=("a", "c"), host=True)))
        self.close_window()
        blocked = self.ledger.summary(criteria(max_safer_new=10**6, max_routing_mismatch=10**6))
        self.assertEqual(blocked.counts[DivergenceClass.MORE_PERMISSIVE_NEW], 1)
        self.assertFalse(blocked.accepted)
        # Thresholds never default: every one must be stated.
        for name in at_threshold:
            values = {item.name: getattr(criteria(), item.name) for item in dataclasses.fields(CanaryCriteria)
                      if item.init and item.name != name}
            with self.assertRaises(TypeError):
                CanaryCriteria(**values)

    def test_unsafe_or_malformed_divergence_prevents_acceptance(self) -> None:
        for new, label in ((UNSAFE_NEW, "more-permissive-new"), (UNKNOWN, "malformed-unknown")):
            with self.subTest(label=label):
                self.setUp()
                self.fill(200, span=5400)
                self.clock.now = NOW + timedelta(seconds=5500)
                self.record(READ, new)
                self.close_window()
                generous = criteria(max_safer_new=10**6, max_routing_mismatch=10**6)
                summary = self.ledger.summary(generous)
                self.assertFalse(summary.accepted)
                self.assertEqual(len(summary.reasons), 1)
                self.assertIn(label, summary.reasons[0])
                with self.assertRaises(ShadowError):
                    self.ledger.attestation_evidence(generous)
                forged = summary.to_document()
                forged.update(accepted=True, reasons=[])
                with self.assertRaises(ShadowError):
                    self.ledger.verify_evidence(forged)

    def test_revision_and_source_isolation(self) -> None:
        self.fill(10)
        self.close_window()
        accepted = self.ledger.summary(criteria())
        self.assertTrue(accepted.accepted)
        for change in ({"policy_revision": "policy-2"}, {"config_revision": "config-2"},
                       {"evidence_revision": "evidence-2"}):
            summary = self.ledger.summary(criteria(**change))
            self.assertEqual((summary.sample_count, summary.cross_revision_samples), (0, 10))
            self.assertFalse(summary.accepted)
            self.assertIn("other revisions", summary.reasons[0])
        foreign = self.ledger.summary(criteria(source_instances=("gateway-b",)))
        self.assertEqual((foreign.sample_count, foreign.foreign_source_samples), (0, 10))
        self.assertFalse(foreign.accepted)
        # Naming a second source that never contributed is no longer acceptable.
        both = self.ledger.summary(criteria(source_instances=("gateway-b", "gateway-a")))
        self.assertFalse(both.accepted)
        self.assertEqual(both.source_counts, (("gateway-a", 10), ("gateway-b", 0)))

    def test_cross_revision_or_foreign_source_inside_window_blocks(self) -> None:
        for change in ({"config_revision": "config-2"}, {"source_instance": "gateway-rogue"}):
            with self.subTest(change=change):
                self.setUp()
                self.fill(10)
                self.record(**change)
                self.close_window()
                summary = self.ledger.summary(criteria())
                self.assertEqual(summary.sample_count, 10)
                self.assertFalse(summary.accepted)
                self.assertEqual(len(summary.reasons), 1)

    def test_presented_evidence_is_rederived_fail_closed(self) -> None:
        self.fill(10)
        self.close_window()
        evidence = self.ledger.attestation_evidence(criteria())
        self.assertTrue(self.ledger.verify_evidence(evidence).accepted)
        mutations = (
            lambda d: d.update(sample_count=11),
            lambda d: d["counts"].update(equivalent=11),
            lambda d: d.update(evidence_root="0" * 64),
            lambda d: d.update(machine_id="17050"),
            lambda d: d["criteria"].update(machine_id="17050"),
            lambda d: d["criteria"].update(minimum_samples=5),
            lambda d: d.update(criteria_hash="0" * 64),
            lambda d: d.update(execution_authority=True),
            lambda d: d.update(last_sequence=9),
            lambda d: d.pop("evidence_root"),
            lambda d: d.update(extra=True),
            lambda d: d["source_counts"][0].update(source_instance="gateway-b"),
            lambda d: d["source_counts"].clear(),
            lambda d: d.update(out_of_window_observations=1),
            lambda d: d.update(first_observed_at=d["last_observed_at"]),
            lambda d: d.pop("last_observed_at"),
            lambda d: d["criteria"].update(minimum_samples_per_source=1),
            lambda d: d["criteria"].pop("minimum_samples_per_source"),
            lambda d: d["counts"].update({"approval-mismatch": 0}),
            lambda d: d.update(schema_version=1),
        )
        for mutate in mutations:
            damaged = copy.deepcopy(evidence)
            mutate(damaged)
            with self.assertRaises(ShadowError):
                self.ledger.verify_evidence(damaged)
        for junk in (None, [], "evidence", {"schema_version": 1}):
            with self.assertRaises(ShadowError):
                self.ledger.verify_evidence(junk)
        # Evidence from another database does not verify here.
        other_root = Path(tempfile.mkdtemp(dir=self.root))
        other_tasks = TaskStore(other_root, clock=self.clock)
        self.addCleanup(other_tasks.close)
        other = ShadowLedger(other_tasks, clock=self.clock)
        with self.assertRaises(ShadowError):
            other.verify_evidence(evidence)
        # Evidence stops verifying once the durable records are tampered with.
        self.raw("DROP TRIGGER tc_shadow_comparisons_immutable_delete",
                 "DELETE FROM tc_shadow_comparisons WHERE sequence=10",
                 shadow_module._DELETE_TRIGGER_SQL)
        with self.assertRaises(ShadowError):
            self.ledger.verify_evidence(evidence)

    def test_criteria_bounds_cannot_be_configured_away(self) -> None:
        for change in (
            {"minimum_samples": shadow_module.MINIMUM_SAMPLE_FLOOR - 1}, {"minimum_samples": True},
            {"minimum_samples": 10.0}, {"window_end": WINDOW_START + timedelta(minutes=59)},
            {"window_end": WINDOW_START + timedelta(days=31)}, {"window_end": WINDOW_START},
            {"minimum_span_seconds": 0}, {"minimum_span_seconds": 7201},
            {"minimum_span_seconds": 10**30},
            {"max_evidence_age_seconds": 0}, {"max_evidence_age_seconds": 8 * 86400},
            {"max_evidence_age_seconds": 10**30},
            {"max_safer_new": -1}, {"max_routing_mismatch": None}, {"max_routing_mismatch": 1.5},
            {"minimum_samples_per_source": 0},
            {"source_instances": ()}, {"source_instances": ("a", "a")},
            {"source_instances": tuple(f"s{i}" for i in range(17))},
            {"machine_id": "17050"}, {"policy_revision": ""},
            {"window_start": datetime(2026, 9, 21, 12, 0)},
        ):
            with self.subTest(change=change), self.assertRaises(ContractError):
                criteria(**change)
        self.assertEqual(CanaryCriteria.from_json(criteria().to_json()), criteria())


if __name__ == "__main__":
    unittest.main()
