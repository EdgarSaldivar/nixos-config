from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.commissioning_evidence import (
    CommissioningEvidenceError,
    CommissioningEvidenceVerifier,
)
from terracompute_ops.plan_authorization import Authority
from terracompute_ops.plans import ContractError, Effect
from terracompute_ops import rollout as rollout_module
from terracompute_ops.rollout import (
    ROLLOUT_ORDER,
    CommissioningAttestation,
    RolloutAction,
    RolloutCapability,
    RolloutConflict,
    RolloutError,
    RolloutEvent,
    RolloutHead,
    RolloutLedger,
    RolloutTampered,
    classify_effect,
)
from terracompute_ops.tasks import TaskStore
from terracompute_ops.shadow_rollout import (
    CAPABILITY_COVERAGE,
    COVERAGE_ORDER,
    CanaryCriteria,
    ComparisonOrigin,
    PolicyDecision,
    PolicyOutcome,
    RouteKind,
    RoutingDecision,
    ShadowComparison,
    ShadowDecision,
    ShadowLedger,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CURRENT_ATTESTATIONS: dict[RolloutCapability, CommissioningAttestation] = {}
_SHADOW_TEMPLATE: bytes | None = None


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


def attestation(
    capability: RolloutCapability,
    *,
    version: int = 1,
    policy_revision: str = "policy-1",
    config_revision: str = "config-1",
    evidence_revision: str = "evidence-1",
    canary: dict | None = None,
    statement: str = "canary satisfied under shadow comparison",
) -> CommissioningAttestation:
    current = _CURRENT_ATTESTATIONS.get(capability)
    if canary is None and current is not None:
        canary = dict(current.canary_evidence)
    return CommissioningAttestation(
        capability=capability, version=version, policy_revision=policy_revision,
        config_revision=config_revision, evidence_revision=evidence_revision,
        canary_evidence=canary or {"shadow_matches": 100, "divergences": 0},
        statement=statement,
    )


class RolloutLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        global _SHADOW_TEMPLATE
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        if _SHADOW_TEMPLATE is not None:
            (self.root / "state.sqlite3").write_bytes(_SHADOW_TEMPLATE)
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        if _SHADOW_TEMPLATE is None:
            self._install_shadow_evidence()
            self.tasks.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _SHADOW_TEMPLATE = (self.root / "state.sqlite3").read_bytes()
        else:
            self.shadow = ShadowLedger(self.tasks, clock=self.clock)
            self.evidence_verifier = CommissioningEvidenceVerifier(self.shadow)
        # The deployment's injected pins: by default the version-1 attestation
        # of every capability.  Tests that commission anything else re-pin.
        self.pins = {item: attestation(item).attestation_id for item in ROLLOUT_ORDER}
        self.ledger = RolloutLedger(
            self.tasks, clock=self.clock, expected_attestations=self.pins,
            evidence_verifier=self.evidence_verifier,
        )
        self._nonce = 0

    @staticmethod
    def _policy(
        *, authority: Authority, outcome: PolicyOutcome = PolicyOutcome.PERMIT,
        approval: bool = False, **flags: bool,
    ) -> PolicyDecision:
        return PolicyDecision(
            effect=Effect(effect_id="coverage-effect", description="derived flags", **flags),
            outcome=outcome, authority=authority,
            approval_refs=("approval-1",) if approval else (),
        )

    @classmethod
    def _decision(
        cls, policy: PolicyDecision, route: RouteKind = RouteKind.NEW_TASK,
        task_id: str | None = None,
    ) -> ShadowDecision:
        return ShadowDecision(RoutingDecision(route, task_id), policy)

    def _install_shadow_evidence(self) -> None:
        """Create real, capability-specific durable evidence for isolated tests."""
        shadow_clock = Clock()
        shadow_clock.now = NOW - timedelta(hours=3)
        shadow = ShadowLedger(self.tasks, clock=shadow_clock)
        readonly = self._decision(self._policy(authority=Authority.AGENT))
        owned = self._decision(self._policy(
            authority=Authority.EXACT_HUMAN,
            outcome=PolicyOutcome.REQUIRE_APPROVAL, approval=True,
            owned_component=True,
        ))
        standing = self._decision(self._policy(
            authority=Authority.STANDING_CONSENT, owned_component=True,
        ))
        host = self._decision(self._policy(
            authority=Authority.EXACT_HUMAN,
            outcome=PolicyOutcome.REQUIRE_APPROVAL, approval=True, host=True,
        ))
        tenant = self._decision(self._policy(
            authority=Authority.ALWAYS_APPROVE_TENANT,
            outcome=PolicyOutcome.REQUIRE_APPROVAL, approval=True, tenant=True,
        ))
        external = self._decision(self._policy(
            authority=Authority.EXACT_HUMAN,
            outcome=PolicyOutcome.REQUIRE_APPROVAL, approval=True,
            external_commitment=True,
        ))
        handover_old = self._decision(
            self._policy(authority=Authority.EXACT_HUMAN,
                         outcome=PolicyOutcome.REQUIRE_APPROVAL,
                         approval=True, host=True),
            RouteKind.EXISTING_TASK, "task-handover",
        )
        handover_new = self._decision(
            self._policy(authority=Authority.EXACT_HUMAN,
                         outcome=PolicyOutcome.REQUIRE_APPROVAL,
                         approval=True, host=True),
            RouteKind.LEGACY_HANDOVER, "task-handover",
        )
        samples = (
            (readonly, readonly, ComparisonOrigin.OPERATOR),
            (owned, owned, ComparisonOrigin.OPERATOR),
            (standing, standing, ComparisonOrigin.OPERATOR),
            (host, host, ComparisonOrigin.OPERATOR),
            (tenant, tenant, ComparisonOrigin.OPERATOR),
            (external, external, ComparisonOrigin.OPERATOR),
            (readonly, readonly, ComparisonOrigin.DETECTOR),
            (handover_old, handover_new, ComparisonOrigin.DETECTOR),
        )
        nonce = 0
        for point in range(10):
            for old, new, origin in samples:
                nonce += 1
                shadow.record(ShadowComparison(
                    origin=origin,
                    operator_id=("operator:fixture" if origin is ComparisonOrigin.OPERATOR else None),
                    task_id=("task-handover" if new.routing.route is RouteKind.LEGACY_HANDOVER else None),
                    request_id=f"fixture-request-{nonce}", parent_hash="0" * 64,
                    policy_revision="policy-1",
                    config_revision="config-1", evidence_revision="evidence-1",
                    observed_at=shadow_clock.now, source_instance="fixture-source",
                    nonce=f"fixture-nonce-{nonce}", old=old, new=new,
                ))
            if point < 9:
                shadow_clock.advance(600)
        shadow_clock.now = NOW
        attestations: dict[RolloutCapability, CommissioningAttestation] = {}
        for capability in RolloutCapability:
            minimums = {kind: 0 for kind in COVERAGE_ORDER}
            for kind in CAPABILITY_COVERAGE[capability]:
                minimums[kind] = 10
            criteria = CanaryCriteria(
                capability=capability, coverage_minimums=minimums,
                policy_revision="policy-1", config_revision="config-1",
                evidence_revision="evidence-1", source_instances=("fixture-source",),
                window_start=NOW - timedelta(hours=3),
                window_end=NOW - timedelta(hours=1), minimum_samples=10,
                minimum_samples_per_source=1, minimum_span_seconds=5000,
                max_evidence_age_seconds=7 * 86400,
                max_safer_new=0, max_routing_mismatch=10,
            )
            evidence = shadow.attestation_evidence(criteria)
            attestations[capability] = CommissioningAttestation(
                capability=capability, version=1,
                policy_revision="policy-1", config_revision="config-1",
                evidence_revision="evidence-1", canary_evidence=evidence,
                statement="canary satisfied under shadow comparison",
            )
        _CURRENT_ATTESTATIONS.clear()
        _CURRENT_ATTESTATIONS.update(attestations)
        # Production verification is used unchanged; only the evidence producer
        # above is a test fixture.
        self.shadow = ShadowLedger(self.tasks, clock=self.clock)
        self.evidence_verifier = CommissioningEvidenceVerifier(self.shadow)

    def repin(self, *attestations: CommissioningAttestation, **kwargs) -> RolloutLedger:
        """Model a redeploy with new injected expected attestations."""
        for item in attestations:
            self.pins[item.capability] = item.attestation_id
        self.ledger = RolloutLedger(
            self.tasks, clock=self.clock, expected_attestations=self.pins,
            evidence_verifier=self.evidence_verifier, **kwargs)
        return self.ledger

    def reopen(self, **kwargs) -> RolloutLedger:
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        kwargs.setdefault("expected_attestations", self.pins)
        self.shadow = ShadowLedger(self.tasks, clock=self.clock)
        self.evidence_verifier = CommissioningEvidenceVerifier(self.shadow)
        kwargs.setdefault("evidence_verifier", self.evidence_verifier)
        self.ledger = RolloutLedger(self.tasks, clock=self.clock, **kwargs)
        return self.ledger

    def raw(self, *statements: str, parameters: tuple = ()) -> None:
        """Act as an attacker with raw database access, outside the library."""
        self.tasks.close()
        raw = sqlite3.connect(self.root / "state.sqlite3")
        try:
            for statement in statements:
                raw.execute(statement, parameters if "?" in statement else ())
            raw.commit()
        finally:
            raw.close()

    def nonce(self) -> str:
        self._nonce += 1
        return f"nonce-{self._nonce}"

    def activate(self, capability: RolloutCapability, **kwargs) -> RolloutEvent:
        att = kwargs.pop("attestation", None) or attestation(capability, **{
            key: kwargs.pop(key) for key in list(kwargs)
            if key in {"version", "policy_revision", "config_revision", "evidence_revision", "canary"}
        })
        return self.ledger.activate(
            capability, att, actor=kwargs.pop("actor", "operator:alice"),
            nonce=kwargs.pop("nonce", None) or self.nonce(),
            reason=kwargs.pop("reason", "commission next stage"),
            now=kwargs.pop("now", None),
        )

    def rollback(self, capability: RolloutCapability, **kwargs) -> RolloutEvent:
        return self.ledger.rollback(
            capability, actor=kwargs.pop("actor", "operator:alice"),
            nonce=kwargs.pop("nonce", None) or self.nonce(),
            reason=kwargs.pop("reason", "return to legacy routing"),
            policy_revision=kwargs.pop("policy_revision", "policy-1"),
            config_revision=kwargs.pop("config_revision", "config-1"),
            evidence_revision=kwargs.pop("evidence_revision", "evidence-1"),
            canary_evidence=kwargs.pop("canary_evidence", {"reason": "rollback"}),
            now=kwargs.pop("now", None),
        )

    def commission_through(self, capability: RolloutCapability) -> None:
        from terracompute_ops.rollout import ROLLOUT_ORDER, PREREQUISITES

        for item in ROLLOUT_ORDER:
            if item is capability or item in PREREQUISITES[capability]:
                self.activate(item)

    def commission_all(self) -> None:
        from terracompute_ops.rollout import ROLLOUT_ORDER

        for item in ROLLOUT_ORDER:
            self.activate(item)

    # -- defaults -------------------------------------------------------------

    def test_defaults_are_inert(self) -> None:
        self.assertEqual(self.ledger.events(), ())
        self.assertIsNone(self.ledger.head_hash())
        self.assertEqual(self.ledger.routing_target(), "legacy")
        for capability in RolloutCapability:
            self.assertFalse(self.ledger.is_active(capability))
            decision = self.ledger.evaluate(capability)
            self.assertFalse(decision.permitted)
        self.assertFalse(self.ledger.standing_consent_active())
        self.assertFalse(self.ledger.detector_tasks_active())

    def test_missing_verifier_denies_authority_but_preserves_history_and_rollback(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        self.activate(capability)
        historical = RolloutLedger(
            self.tasks, clock=self.clock, expected_attestations=self.pins)
        self.assertTrue(historical.is_active(capability))
        self.assertFalse(historical.evaluate(capability).permitted)
        self.assertFalse(historical.evaluate(capability).evidence_matches)
        self.assertFalse(historical.evaluate_effect(
            Effect(effect_id="read", description="read only")).permitted)
        historical.rollback(
            capability, actor="operator:alice", nonce="rollback-without-evidence",
            reason="retire safely", policy_revision="policy-1",
            config_revision="config-1", evidence_revision="evidence-1",
            canary_evidence={"reason": "evidence unavailable"},
        )
        self.assertFalse(historical.is_active(capability))

    def test_verifier_rejects_cross_capability_and_revision_substitution(self) -> None:
        operator = attestation(RolloutCapability.OPERATOR_READONLY)
        for changed in (
            CommissioningAttestation(
                capability=RolloutCapability.HOST_ACTIONS, version=1,
                policy_revision=operator.policy_revision,
                config_revision=operator.config_revision,
                evidence_revision=operator.evidence_revision,
                canary_evidence=operator.canary_evidence, statement="substitute capability"),
            dataclasses.replace(operator, policy_revision="policy-other"),
            dataclasses.replace(operator, config_revision="config-other"),
            dataclasses.replace(operator, evidence_revision="evidence-other"),
        ):
            with self.subTest(attestation=changed), self.assertRaises(CommissioningEvidenceError):
                self.evidence_verifier.verify(changed)

    def test_evidence_expiry_denies_reads_and_activation_replay_but_not_rollback(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        att = attestation(capability)
        self.ledger.activate(
            capability, att, actor="operator:alice", nonce="expiry-replay",
            reason="commission",
        )
        self.clock.advance(8 * 86400)
        decision = self.ledger.evaluate(capability)
        self.assertTrue(decision.active)
        self.assertFalse(decision.evidence_matches)
        self.assertFalse(decision.permitted)
        with self.assertRaisesRegex(RolloutError, "evidence"):
            self.ledger.activate(
                capability, att, actor="operator:alice", nonce="expiry-replay",
                reason="commission",
            )
        self.rollback(capability)
        self.assertFalse(self.ledger.is_active(capability))

    def test_shadow_tamper_denies_authority_but_not_rollback(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        self.activate(capability)
        self.tasks.db.execute("DROP TRIGGER tc_shadow_comparisons_immutable_delete")
        self.tasks.db.commit()
        self.assertFalse(self.ledger.evaluate(capability).permitted)
        self.assertFalse(self.ledger.evaluate_effect(
            Effect(effect_id="read", description="read only")).permitted)
        self.rollback(capability)
        self.assertFalse(self.ledger.is_active(capability))

    def test_verifier_must_be_concrete_and_share_the_rollout_store(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.evidence_verifier.shadow = self.shadow  # type: ignore[misc]
        with self.assertRaises(ContractError):
            RolloutLedger(
                self.tasks, clock=self.clock, expected_attestations=self.pins,
                evidence_verifier=lambda _attestation: True,
            )
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        other_tasks = TaskStore(Path(other_temp.name), clock=self.clock)
        self.addCleanup(other_tasks.close)
        other_verifier = CommissioningEvidenceVerifier(
            ShadowLedger(other_tasks, clock=self.clock))
        with self.assertRaises(ContractError):
            RolloutLedger(
                self.tasks, clock=self.clock, expected_attestations=self.pins,
                evidence_verifier=other_verifier,
            )

    # -- ordering / prerequisites --------------------------------------------

    def test_activation_requires_prerequisites(self) -> None:
        with self.assertRaises(RolloutError):
            self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.assertTrue(self.ledger.is_active(RolloutCapability.OWNED_COMPONENT_MUTATION))
        # tenant requires host, which is not yet active
        with self.assertRaises(RolloutError):
            self.activate(RolloutCapability.TENANT_ACTIONS)

    def test_full_ordered_commission_and_routing(self) -> None:
        self.commission_all()
        self.assertEqual(self.ledger.routing_target(), "phase7")
        self.assertEqual(self.ledger.verify_chain(), 8)
        for capability in RolloutCapability:
            self.assertTrue(self.ledger.is_active(capability))
        # events are totally ordered with a contiguous sequence and chained hashes
        events = self.ledger.events()
        self.assertEqual([e.sequence for e in events], list(range(1, 9)))
        self.assertIsNone(events[0].prior_event_hash)
        for earlier, later in zip(events, events[1:]):
            self.assertEqual(later.prior_event_hash, earlier.event_hash)

    def test_legacy_retirement_requires_only_detector_path(self) -> None:
        # Retirement follows the detector path being live; it does not force host,
        # tenant or external writes to be commissioned first.
        self.commission_through(RolloutCapability.LEGACY_LOOP_RETIREMENT)
        self.assertEqual(self.ledger.routing_target(), "phase7")
        self.assertFalse(self.ledger.is_active(RolloutCapability.HOST_ACTIONS))
        self.assertFalse(self.ledger.is_active(RolloutCapability.TENANT_ACTIONS))
        self.assertFalse(self.ledger.is_active(RolloutCapability.EXTERNAL_WRITES))

    def test_double_activation_conflicts(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        with self.assertRaises(RolloutConflict):
            self.activate(RolloutCapability.OPERATOR_READONLY)

    # -- restart --------------------------------------------------------------

    def test_state_survives_restart(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        head = self.ledger.head_hash()
        att_id = self.ledger.status()[1].attestation_id
        self.tasks.close()

        reopened_tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(reopened_tasks.close)
        reopened = RolloutLedger(reopened_tasks, clock=self.clock)
        self.assertTrue(reopened.is_active(RolloutCapability.OPERATOR_READONLY))
        self.assertTrue(reopened.is_active(RolloutCapability.OWNED_COMPONENT_MUTATION))
        self.assertEqual(reopened.head_hash(), head)
        self.assertEqual(reopened.status()[1].attestation_id, att_id)
        self.assertEqual(reopened.verify_chain(), 2)

    # -- nonce replay / conflict ---------------------------------------------

    def test_replayed_nonce_is_idempotent(self) -> None:
        first = self.activate(RolloutCapability.OPERATOR_READONLY, nonce="commission-1")
        att = attestation(RolloutCapability.OPERATOR_READONLY)
        again = self.ledger.activate(
            RolloutCapability.OPERATOR_READONLY, att, actor="operator:alice",
            nonce="commission-1", reason="commission next stage",
        )
        self.assertEqual(again.event_hash, first.event_hash)
        self.assertEqual(len(self.ledger.events()), 1)

    def test_replayed_nonce_with_different_content_conflicts(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY, nonce="commission-1")
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        with self.assertRaises(RolloutConflict):
            # same nonce, different capability
            self.ledger.activate(
                RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT,
                attestation(RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT),
                actor="operator:alice", nonce="commission-1", reason="x",
            )

    # -- rollback history -----------------------------------------------------

    def test_rollback_preserves_history_and_returns_to_legacy(self) -> None:
        self.commission_through(RolloutCapability.LEGACY_LOOP_RETIREMENT)
        self.assertEqual(self.ledger.routing_target(), "phase7")
        activation_count = len(self.ledger.events())
        self.rollback(RolloutCapability.LEGACY_LOOP_RETIREMENT)
        self.assertEqual(self.ledger.routing_target(), "legacy")
        self.assertFalse(self.ledger.is_active(RolloutCapability.LEGACY_LOOP_RETIREMENT))
        # history is preserved: the activation event still exists
        events = self.ledger.events(RolloutCapability.LEGACY_LOOP_RETIREMENT)
        self.assertEqual([e.action for e in events],
                         [RolloutAction.ACTIVATE, RolloutAction.ROLLBACK])
        self.assertEqual(len(self.ledger.events()), activation_count + 1)
        self.assertEqual(self.ledger.verify_chain(), activation_count + 1)

    def test_cannot_rollback_with_active_dependents(self) -> None:
        self.commission_through(RolloutCapability.HOST_ACTIONS)
        with self.assertRaises(RolloutError):
            self.rollback(RolloutCapability.OWNED_COMPONENT_MUTATION)
        # rolling back the dependent first is allowed
        self.rollback(RolloutCapability.HOST_ACTIONS)
        self.rollback(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.assertFalse(self.ledger.is_active(RolloutCapability.OWNED_COMPONENT_MUTATION))

    def test_rollback_of_inactive_capability_fails_closed(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollback(RolloutCapability.OPERATOR_READONLY)

    def test_reactivation_after_rollback(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.rollback(RolloutCapability.OPERATOR_READONLY)
        self.assertFalse(self.ledger.is_active(RolloutCapability.OPERATOR_READONLY))
        successor = attestation(RolloutCapability.OPERATOR_READONLY, version=2)
        self.repin(successor)
        self.activate(RolloutCapability.OPERATOR_READONLY, attestation=successor)
        self.assertTrue(self.ledger.is_active(RolloutCapability.OPERATOR_READONLY))
        self.assertEqual(self.ledger.verify_chain(), 3)

    # -- unknown / mismatched attestation and capability ----------------------

    def test_mismatched_attestation_capability_fails_closed(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        wrong = attestation(RolloutCapability.HOST_ACTIONS)
        with self.assertRaises(RolloutError):
            self.ledger.activate(
                RolloutCapability.OWNED_COMPONENT_MUTATION, wrong,
                actor="operator:alice", nonce=self.nonce(), reason="x",
            )

    def test_unknown_capability_document_rejected(self) -> None:
        document = attestation(RolloutCapability.OPERATOR_READONLY).to_document()
        document["capability"] = "invent-a-new-capability"
        with self.assertRaises(ContractError):
            CommissioningAttestation.from_document(document)

    def test_attestation_requires_machine_17049(self) -> None:
        with self.assertRaises(ContractError):
            CommissioningAttestation(
                capability=RolloutCapability.OPERATOR_READONLY, version=1,
                policy_revision="p", config_revision="c", evidence_revision="e",
                canary_evidence={"ok": 1}, statement="s", machine_id="9999",
            )

    def test_event_canary_binding_uses_canonical_json_types(self) -> None:
        att = attestation(
            RolloutCapability.OPERATOR_READONLY, canary={"observations": 1},
        )
        with self.assertRaisesRegex(ContractError, "bindings"):
            RolloutEvent(
                sequence=1, capability=att.capability,
                action=RolloutAction.ACTIVATE, actor="operator:alice",
                policy_revision=att.policy_revision,
                config_revision=att.config_revision,
                evidence_revision=att.evidence_revision,
                occurred_at=NOW, nonce="typed-canary",
                canary_evidence={"observations": 1.0}, reason="commission",
                attestation=att, prior_event_hash=None,
            )

    def test_evaluate_rejects_mismatched_attestation(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        # a differently-versioned attestation is a different exact identity
        other = attestation(RolloutCapability.OPERATOR_READONLY, version=2)
        decision = self.ledger.evaluate(RolloutCapability.OPERATOR_READONLY, attestation=other)
        self.assertFalse(decision.attestation_matches)
        self.assertFalse(decision.permitted)

    def test_evaluate_accepts_matching_attestation(self) -> None:
        att = attestation(RolloutCapability.OPERATOR_READONLY)
        self.ledger.activate(
            RolloutCapability.OPERATOR_READONLY, att, actor="operator:alice",
            nonce=self.nonce(), reason="commission",
        )
        decision = self.ledger.evaluate(RolloutCapability.OPERATOR_READONLY, attestation=att)
        self.assertTrue(decision.attestation_matches)
        self.assertTrue(decision.permitted)

    # -- tamper ---------------------------------------------------------------

    def test_rows_are_immutable_in_process(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks.db.execute("UPDATE tc_rollout_events SET occurred_utc='x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks.db.execute("DELETE FROM tc_rollout_events")

    def test_out_of_band_tamper_is_detected(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.assertEqual(self.ledger.verify_chain(), 2)

        # An attacker with raw database access drops the guard trigger, edits the
        # stored JSON, and restores the trigger so the schema looks intact.  The
        # hash chain must still catch it, already at open.
        self.raw(
            "DROP TRIGGER tc_rollout_events_immutable_update",
            "UPDATE tc_rollout_events SET event_json=CAST(REPLACE(event_json,"
            "'commission next stage','tampered reason') AS BLOB) WHERE sequence=1",
            rollout_module._UPDATE_TRIGGER_SQL,
        )
        with self.assertRaises(RolloutTampered):
            self.reopen()

    # -- effect-based classification, tenant/host/Vast separation ------------

    def test_effect_classification_is_by_effect_not_command(self) -> None:
        host = Effect(effect_id="e", description="d", host=True)
        tenant = Effect(effect_id="e", description="d", tenant=True)
        external = Effect(effect_id="e", description="d", external_commitment=True)
        owned = Effect(effect_id="e", description="d", owned_component=True)
        readonly = Effect(effect_id="e", description="d")
        self.assertEqual(classify_effect(host)[0], frozenset({RolloutCapability.HOST_ACTIONS}))
        self.assertEqual(classify_effect(tenant)[0], frozenset({RolloutCapability.TENANT_ACTIONS}))
        self.assertEqual(classify_effect(external)[0], frozenset({RolloutCapability.EXTERNAL_WRITES}))
        self.assertEqual(classify_effect(owned)[0], frozenset({RolloutCapability.OWNED_COMPONENT_MUTATION}))
        self.assertEqual(classify_effect(readonly)[0], frozenset({RolloutCapability.OPERATOR_READONLY}))

    def test_host_capability_does_not_permit_tenant_or_vast(self) -> None:
        self.commission_through(RolloutCapability.HOST_ACTIONS)
        host = Effect(effect_id="novel-host-op", description="a brand new host op", host=True)
        tenant = Effect(effect_id="novel-tenant-op", description="a brand new tenant op", tenant=True)
        external = Effect(effect_id="novel-vast-op", description="a brand new vast write", external_commitment=True)
        self.assertTrue(self.ledger.evaluate_effect(host).permitted)
        self.assertFalse(self.ledger.evaluate_effect(tenant).permitted)
        self.assertFalse(self.ledger.evaluate_effect(external).permitted)
        self.assertIn(RolloutCapability.TENANT_ACTIONS,
                      self.ledger.evaluate_effect(tenant).missing_capabilities)

    def test_novel_operation_needs_no_catalog_change(self) -> None:
        # An effect id that never appeared before is classified purely by effect.
        self.commission_through(RolloutCapability.EXTERNAL_WRITES)
        novel = Effect(
            effect_id="freshly-invented-2099-operation",
            description="an operation this module has never heard of",
            external_commitment=True,
        )
        decision = self.ledger.evaluate_effect(novel)
        self.assertTrue(decision.permitted)
        self.assertEqual(decision.required_capabilities, (RolloutCapability.EXTERNAL_WRITES,))

    def test_ungoverned_effects_never_permitted_by_rollout(self) -> None:
        self.commission_through(RolloutCapability.EXTERNAL_WRITES)
        for flag in ("reachability", "secrets", "irreversible"):
            effect = Effect(effect_id="e", description="d", **{flag: True})
            decision = self.ledger.evaluate_effect(effect)
            self.assertFalse(decision.permitted)
            self.assertIn(flag, decision.ungoverned_effects)

    def test_standing_consent_covers_only_owned_component(self) -> None:
        from terracompute_ops.rollout import STANDING_CONSENT_EFFECT_CLASSES

        self.assertEqual(STANDING_CONSENT_EFFECT_CLASSES, frozenset({"owned_component"}))
        self.assertNotIn("tenant", STANDING_CONSENT_EFFECT_CLASSES)
        self.assertNotIn("host", STANDING_CONSENT_EFFECT_CLASSES)
        self.assertNotIn("external_commitment", STANDING_CONSENT_EFFECT_CLASSES)

    def test_standing_consent_is_optional_not_a_host_prerequisite(self) -> None:
        # Host actions must be commissionable without ever enabling standing consent.
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.activate(RolloutCapability.HOST_ACTIONS)
        self.assertTrue(self.ledger.is_active(RolloutCapability.HOST_ACTIONS))
        self.assertFalse(self.ledger.standing_consent_active())

    # -- hardening: integrity before every decision and append (H1/H2) -------

    def insert_raw(self, event: RolloutEvent, **overrides) -> None:
        columns = {
            "sequence": event.sequence, "event_hash": event.event_hash,
            "prior_event_hash": event.prior_event_hash, "capability": event.capability.value,
            "action": event.action.value, "nonce": event.nonce,
            "occurred_utc": event.to_document()["occurred_at"],
            "recorded_utc": event.to_document()["occurred_at"],
            "event_json": event.canonical_json(),
        }
        columns.update(overrides)
        self.tasks.db.execute(
            f"INSERT INTO tc_rollout_events({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})", tuple(columns.values()),
        )
        self.tasks.db.commit()

    def tamper_in_session(self, statement: str, parameters: tuple = ()) -> None:
        db = self.tasks.db
        db.execute("DROP TRIGGER tc_rollout_events_immutable_update")
        db.execute("DROP TRIGGER tc_rollout_events_immutable_delete")
        db.execute(statement, parameters)
        db.execute(rollout_module._UPDATE_TRIGGER_SQL)
        db.execute(rollout_module._DELETE_TRIGGER_SQL)
        db.commit()

    def public_reads(self):
        ledger = self.ledger
        readonly = Effect(effect_id="e", description="d")
        return (
            ledger.status, ledger.events, ledger.head, ledger.head_hash,
            ledger.routing_target, ledger.standing_consent_active,
            ledger.detector_tasks_active, ledger.verify_chain,
            lambda: ledger.is_active(RolloutCapability.OPERATOR_READONLY),
            lambda: ledger.evaluate(RolloutCapability.OPERATOR_READONLY),
            lambda: ledger.evaluate_effect(readonly),
        )

    def assert_everything_fails_closed(self) -> None:
        for read in self.public_reads():
            with self.assertRaises(RolloutTampered):
                read()
        with self.assertRaises(RolloutError):  # tamper is catchable as RolloutError
            self.ledger.routing_target()
        count = self.tasks.db.execute("SELECT COUNT(*) FROM tc_rollout_events").fetchone()[0]
        with self.assertRaises(RolloutTampered):
            self.activate(RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT)
        with self.assertRaises(RolloutTampered):
            self.rollback(RolloutCapability.OWNED_COMPONENT_MUTATION)
        with self.assertRaises(RolloutTampered):
            # even an otherwise idempotent replay is refused over tampered state
            self.activate(RolloutCapability.OPERATOR_READONLY, nonce="nonce-1")
        self.assertEqual(
            self.tasks.db.execute("SELECT COUNT(*) FROM tc_rollout_events").fetchone()[0], count)

    def test_tampered_is_a_rollout_error(self) -> None:
        self.assertTrue(issubclass(RolloutTampered, RolloutError))

    def test_column_tamper_fails_every_decision_and_append(self) -> None:
        tampers = {
            "action": "UPDATE tc_rollout_events SET action='rollback' WHERE sequence=2",
            "capability": "UPDATE tc_rollout_events SET capability='host-actions' WHERE sequence=2",
            "nonce": "UPDATE tc_rollout_events SET nonce='forged-nonce' WHERE sequence=2",
            "occurred_utc": "UPDATE tc_rollout_events SET occurred_utc='2020-01-01T00:00:00.000000Z' WHERE sequence=2",
            "recorded_utc": "UPDATE tc_rollout_events SET recorded_utc='not a time' WHERE sequence=2",
            "event_hash": "UPDATE tc_rollout_events SET event_hash='" + "0" * 64 + "' WHERE sequence=2",
            "prior_event_hash": "UPDATE tc_rollout_events SET prior_event_hash=NULL WHERE sequence=2",
            "sequence": "UPDATE tc_rollout_events SET sequence=7 WHERE sequence=2",
            "event_hash type": "UPDATE tc_rollout_events SET event_hash=CAST(event_hash AS BLOB) WHERE sequence=2",
        }
        for name, statement in tampers.items():
            with self.subTest(column=name):
                self.setUp()
                self.activate(RolloutCapability.OPERATOR_READONLY)
                self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
                self.tamper_in_session(statement)
                self.assert_everything_fails_closed()

    def test_action_column_cannot_grant_authority(self) -> None:
        # A rolled-back capability whose index column is flipped to 'activate'
        # must never read as active: decisions come from the verified event.
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.rollback(RolloutCapability.OPERATOR_READONLY)
        self.tamper_in_session("UPDATE tc_rollout_events SET action='activate' WHERE sequence=2")
        with self.assertRaises(RolloutTampered):
            self.ledger.is_active(RolloutCapability.OPERATOR_READONLY)

    def test_corrupt_event_json_is_normalized_to_tampered(self) -> None:
        corruptions = {
            "truncated": (b"{",),
            "not json": (b"\xff\xfe",),
            "wrong shape": (b"[]",),
            "unknown field": (b'{"surprise":1}',),
            "text affinity": ("{}",),
        }
        for name, parameters in corruptions.items():
            with self.subTest(corruption=name):
                self.setUp()
                self.activate(RolloutCapability.OPERATOR_READONLY)
                self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
                self.tamper_in_session(
                    "UPDATE tc_rollout_events SET event_json=? WHERE sequence=1", parameters)
                self.assert_everything_fails_closed()

    def test_non_canonical_event_json_is_tampered(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.tamper_in_session(
            "UPDATE tc_rollout_events SET event_json=CAST(event_json || ' ' AS BLOB)")
        with self.assertRaises(RolloutTampered):
            self.ledger.verify_chain()

    def test_out_of_band_tamper_is_detected_at_open(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.raw(
            "DROP TRIGGER tc_rollout_events_immutable_update",
            "UPDATE tc_rollout_events SET action='rollback'",
            rollout_module._UPDATE_TRIGGER_SQL,
        )
        with self.assertRaises(RolloutTampered):
            self.reopen()

    def test_hash_consistent_but_impossible_history_is_tampered(self) -> None:
        # A forger who recomputes every hash still cannot record a transition the
        # writer would have refused: host actions without its prerequisites.
        att = attestation(RolloutCapability.HOST_ACTIONS)
        forged = RolloutEvent(
            sequence=1, capability=RolloutCapability.HOST_ACTIONS, action=RolloutAction.ACTIVATE,
            actor="operator:mallory", policy_revision=att.policy_revision,
            config_revision=att.config_revision, evidence_revision=att.evidence_revision,
            occurred_at=NOW, nonce="forged", canary_evidence=att.canary_evidence,
            reason="forged", attestation=att, prior_event_hash=None,
        )
        self.insert_raw(forged)
        with self.assertRaises(RolloutTampered):
            self.ledger.evaluate(RolloutCapability.HOST_ACTIONS)

    def test_interior_deletion_is_detected(self) -> None:
        self.commission_through(RolloutCapability.HOST_ACTIONS)
        self.tamper_in_session("DELETE FROM tc_rollout_events WHERE sequence=2")
        self.assert_everything_fails_closed()

    # -- hardening: replay binds every input (M1) -----------------------------

    def test_rollback_replay_binds_canary_evidence(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        first = self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1",
                              canary_evidence={"divergences": 3})
        again = self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1",
                              canary_evidence={"divergences": 3})
        self.assertEqual(again.event_hash, first.event_hash)
        for canary in ({"divergences": 4}, {"divergences": 3, "extra": 1},
                       {"divergences": 3.0}, {"divergences": True}):
            with self.subTest(canary=canary), self.assertRaises(RolloutConflict):
                self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1",
                              canary_evidence=canary)
        self.assertEqual(len(self.ledger.events()), 2)

    def test_replay_binds_every_meaningful_input(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1", now=NOW)
        changes = (
            {"actor": "operator:bob"}, {"reason": "other"}, {"policy_revision": "policy-2"},
            {"config_revision": "config-2"}, {"evidence_revision": "evidence-2"},
            {"now": NOW + timedelta(seconds=1)},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RolloutConflict):
                self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1", **change)
        # an identical replay, with or without restating the time, is idempotent
        self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1", now=NOW)
        self.clock.advance(3600)
        self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1")
        with self.assertRaises(RolloutConflict):
            # same nonce, different action
            self.activate(RolloutCapability.OPERATOR_READONLY, nonce="retire-1")

    def test_replay_inputs_are_validated_before_nonce_lookup(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY, nonce="commission-1")
        self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1")
        invalid = (
            {"actor": ""}, {"reason": ""}, {"policy_revision": ""}, {"config_revision": 7},
            {"evidence_revision": "x" * 300}, {"canary_evidence": {}},
            {"canary_evidence": {"value": float("nan")}}, {"canary_evidence": "nope"},
            {"now": datetime(2026, 9, 21, 12, 0)},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.rollback(RolloutCapability.OPERATOR_READONLY, nonce="retire-1", **change)
        with self.assertRaises(ContractError):
            self.activate(RolloutCapability.OPERATOR_READONLY, nonce="commission-1", actor="")
        with self.assertRaises(ContractError):
            self.ledger.rollback(
                RolloutCapability.OPERATOR_READONLY, actor="operator:alice", nonce="",
                reason="r", policy_revision="p", config_revision="c",
                evidence_revision="e", canary_evidence={"ok": 1},
            )

    # -- hardening: attestation versions and reuse (M2) -----------------------

    def test_attestation_version_must_strictly_increase(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        second = attestation(capability, version=2)
        self.repin(second)
        self.activate(capability, attestation=second)
        self.rollback(capability)
        downgrade = attestation(capability, version=1)
        same_version = attestation(capability, version=2, statement="a different statement")
        for candidate in (downgrade, same_version, second):
            with self.subTest(version=candidate.version):
                self.repin(candidate)  # even a matching pin cannot authorize it
                with self.assertRaises(RolloutError):
                    self.activate(capability, attestation=candidate)
        self.assertFalse(self.ledger.is_active(capability))
        third = attestation(capability, version=3)
        self.repin(third)
        self.activate(capability, attestation=third)
        self.assertTrue(self.ledger.evaluate(capability).permitted)

    def test_attestation_hash_cannot_be_reused_after_rollback(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        self.activate(capability)
        self.rollback(capability)
        with self.assertRaises(RolloutError):
            self.activate(capability)
        self.assertEqual(len(self.ledger.events()), 2)

    def test_versions_are_independent_per_capability(self) -> None:
        self.repin(attestation(RolloutCapability.OPERATOR_READONLY, version=5))
        self.activate(RolloutCapability.OPERATOR_READONLY, version=5)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)  # version 1
        self.assertTrue(self.ledger.evaluate(RolloutCapability.OWNED_COMPONENT_MUTATION).permitted)

    # -- hardening: injected expected attestation (M3) ------------------------

    def test_no_expected_attestation_fails_closed(self) -> None:
        unpinned = RolloutLedger(self.tasks, clock=self.clock)
        with self.assertRaises(RolloutError):
            unpinned.activate(
                RolloutCapability.OPERATOR_READONLY,
                attestation(RolloutCapability.OPERATOR_READONLY),
                actor="operator:alice", nonce=self.nonce(), reason="commission",
            )
        self.assertEqual(unpinned.events(), ())
        # durably active state is still not usable without the injected pin
        self.commission_all()
        self.assertTrue(unpinned.is_active(RolloutCapability.LEGACY_LOOP_RETIREMENT))
        for capability in RolloutCapability:
            decision = unpinned.evaluate(capability)
            self.assertFalse(decision.permitted)
            self.assertFalse(decision.expected_attestation_matches)
        self.assertEqual(unpinned.routing_target(), "legacy")
        self.assertFalse(unpinned.standing_consent_active())
        self.assertFalse(unpinned.detector_tasks_active())
        self.assertFalse(unpinned.evaluate_effect(Effect(effect_id="e", description="d")).permitted)
        self.assertEqual(self.ledger.routing_target(), "phase7")

    def test_wrong_expected_attestation_fails_activation(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        for change in ({"policy_revision": "policy-2"}, {"config_revision": "config-2"},
                       {"evidence_revision": "evidence-2"}, {"canary": {"shadow_matches": 99}},
                       {"version": 2}, {"statement": "other"}):
            with self.subTest(change=change), self.assertRaises(RolloutError):
                self.activate(capability, attestation=attestation(capability, **change))
        self.assertEqual(self.ledger.events(), ())

    def test_expected_attestation_mismatch_fails_evaluation(self) -> None:
        self.commission_all()
        self.assertTrue(self.ledger.evaluate(RolloutCapability.TENANT_ACTIONS).permitted)
        # A redeploy now expects a different owned-mutation commissioning.
        self.repin(attestation(RolloutCapability.OWNED_COMPONENT_MUTATION, policy_revision="policy-2"))
        own = self.ledger.evaluate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.assertTrue(own.active)
        self.assertFalse(own.expected_attestation_matches)
        self.assertFalse(own.permitted)
        # ...and everything depending on it stops being usable too.
        tenant = self.ledger.evaluate(RolloutCapability.TENANT_ACTIONS)
        self.assertFalse(tenant.prerequisites_satisfied)
        self.assertFalse(tenant.permitted)
        self.assertFalse(self.ledger.evaluate_effect(
            Effect(effect_id="e", description="d", host=True)).permitted)
        self.assertEqual(self.ledger.routing_target(), "legacy")
        self.assertTrue(self.ledger.evaluate(RolloutCapability.OPERATOR_READONLY).permitted)
        status = {item.capability: item for item in self.ledger.status()}
        self.assertFalse(status[RolloutCapability.OWNED_COMPONENT_MUTATION].expected_attestation_matches)
        self.assertFalse(status[RolloutCapability.HOST_ACTIONS].prerequisites_satisfied)
        # a dependent cannot be commissioned over a mismatched prerequisite
        self.rollback(RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT)
        consent = attestation(RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT, version=2)
        self.repin(consent)
        with self.assertRaises(RolloutError):
            self.activate(RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT, attestation=consent)
        # rollback never needs a pin
        self.rollback(RolloutCapability.LEGACY_LOOP_RETIREMENT)

    def test_authority_read_verifies_prerequisite_evidence_in_one_shadow_scan(self) -> None:
        self.commission_all()
        with mock.patch.object(self.shadow, "_scan", wraps=self.shadow._scan) as scan:
            self.assertEqual(self.ledger.routing_target(), "phase7")
        self.assertEqual(scan.call_count, 1)

    def test_presented_revision_mismatch_fails_evaluation(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        self.activate(capability)
        matching = self.ledger.evaluate(
            capability, policy_revision="policy-1", config_revision="config-1",
            evidence_revision="evidence-1")
        self.assertTrue(matching.revisions_match)
        self.assertTrue(matching.permitted)
        for change in ({"policy_revision": "policy-2"}, {"config_revision": "config-2"},
                       {"evidence_revision": "evidence-2"}):
            with self.subTest(change=change):
                decision = self.ledger.evaluate(capability, **change)
                self.assertFalse(decision.revisions_match)
                self.assertFalse(decision.permitted)
        with self.assertRaises(ContractError):
            self.ledger.evaluate(capability, policy_revision="")
        inactive = self.ledger.evaluate(
            RolloutCapability.HOST_ACTIONS, policy_revision="policy-1")
        self.assertFalse(inactive.revisions_match)

    def test_expected_attestation_configuration_is_validated(self) -> None:
        for pins in ({"operator-readonly-tasks": "0" * 64},
                     {RolloutCapability.OPERATOR_READONLY: "ABC"},
                     {RolloutCapability.OPERATOR_READONLY: "0" * 63},
                     {RolloutCapability.OPERATOR_READONLY: None}, ["x"]):
            with self.subTest(pins=pins), self.assertRaises(ContractError):
                RolloutLedger(self.tasks, clock=self.clock, expected_attestations=pins)

    # -- hardening: schema validation on open (M4) ----------------------------

    def test_missing_or_malformed_schema_object_fails_closed_on_open(self) -> None:
        breakages = {
            "update trigger": ("DROP TRIGGER tc_rollout_events_immutable_update",),
            "delete trigger": ("DROP TRIGGER tc_rollout_events_immutable_delete",),
            "index": ("DROP INDEX tc_rollout_events_capability",),
            "events table": ("DROP TABLE tc_rollout_events",),
            "schema table": ("DROP TABLE tc_rollout_schema",),
            "version row": ("DELETE FROM tc_rollout_schema",),
            "version value": ("UPDATE tc_rollout_schema SET version=0",),
            "version type": ("UPDATE tc_rollout_schema SET version='one'",),
            "neutered trigger": (
                "DROP TRIGGER tc_rollout_events_immutable_delete",
                "CREATE TRIGGER tc_rollout_events_immutable_delete BEFORE DELETE ON "
                "tc_rollout_events WHEN 0 BEGIN SELECT RAISE(ABORT, 'immutable rollout event'); END",
            ),
            "wrong index": (
                "DROP INDEX tc_rollout_events_capability",
                "CREATE INDEX tc_rollout_events_capability ON tc_rollout_events(nonce)",
            ),
            "extra trigger": (
                "CREATE TRIGGER sneaky AFTER INSERT ON tc_rollout_events "
                "BEGIN DELETE FROM tc_rollout_schema; END",
            ),
            "rebuilt table": (
                "DROP TABLE tc_rollout_events",
                "CREATE TABLE tc_rollout_events (sequence INTEGER PRIMARY KEY, event_hash TEXT, "
                "prior_event_hash TEXT, capability TEXT, action TEXT, nonce TEXT, "
                "occurred_utc TEXT, recorded_utc TEXT, event_json BLOB)",
                rollout_module._EVENTS_INDEX_SQL,
                rollout_module._UPDATE_TRIGGER_SQL,
                rollout_module._DELETE_TRIGGER_SQL,
            ),
        }
        for name, statements in breakages.items():
            with self.subTest(breakage=name):
                self.setUp()
                self.activate(RolloutCapability.OPERATOR_READONLY)
                self.raw(*statements)
                with self.assertRaises(RolloutTampered):
                    self.reopen()
                # nothing was silently repaired: a second open still fails closed
                self.tasks.close()
                with self.assertRaises(RolloutError):
                    self.reopen()

    def test_schema_damage_after_open_fails_every_decision_and_append(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.tasks.db.execute("DROP TRIGGER tc_rollout_events_immutable_delete")
        self.tasks.db.commit()
        self.assert_everything_fails_closed()

    def test_newer_schema_version_is_refused(self) -> None:
        self.raw("UPDATE tc_rollout_schema SET version=2")
        with self.assertRaises(RolloutError) as caught:
            self.reopen()
        self.assertNotIsInstance(caught.exception, RolloutTampered)

    def test_intact_schema_reopens(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.tasks.close()
        self.assertEqual(self.reopen().verify_chain(), 1)

    # -- hardening: effect coverage (L1) --------------------------------------

    def test_every_effect_flag_is_governed_or_explicitly_ungoverned(self) -> None:
        from terracompute_ops.plan_authorization import EFFECT_FLAG_NAMES

        governed = set(rollout_module._EFFECT_CLASS_CAPABILITY)
        ungoverned = set(rollout_module._ROLLOUT_UNGOVERNED_EFFECTS)
        self.assertEqual(governed | ungoverned, set(EFFECT_FLAG_NAMES))
        self.assertFalse(governed & ungoverned)
        self.assertEqual(
            rollout_module._effect_coverage_gaps(EFFECT_FLAG_NAMES, governed, ungoverned), ())

    def test_latent_uncovered_effect_flag_fails_closed(self) -> None:
        from terracompute_ops.plan_authorization import EFFECT_FLAG_NAMES

        gaps = rollout_module._effect_coverage_gaps(
            (*EFFECT_FLAG_NAMES, "future_flag"),
            rollout_module._EFFECT_CLASS_CAPABILITY, rollout_module._ROLLOUT_UNGOVERNED_EFFECTS)
        self.assertEqual(len(gaps), 1)
        self.assertIn("future_flag", gaps[0])
        self.assertTrue(rollout_module._effect_coverage_gaps(
            EFFECT_FLAG_NAMES, {"host": 1, "secrets": 1}, EFFECT_FLAG_NAMES))
        original = rollout_module.EFFECT_FLAG_NAMES
        rollout_module.EFFECT_FLAG_NAMES = (*original, "future_flag")
        try:
            with self.assertRaises(RuntimeError):
                rollout_module._assert_effect_coverage()
        finally:
            rollout_module.EFFECT_FLAG_NAMES = original
        rollout_module._assert_effect_coverage()

    def test_unknown_effect_flag_on_an_effect_is_never_permitted(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class FutureEffect(Effect):
            future_flag: bool = False

        self.commission_all()
        quiet = FutureEffect(effect_id="e", description="d", host=True)
        self.assertTrue(self.ledger.evaluate_effect(quiet).permitted)
        for value in (True, 1, "yes"):
            with self.subTest(value=value):
                loud = dataclasses.replace(quiet, future_flag=value)
                required, ungoverned = classify_effect(loud)
                self.assertEqual(ungoverned, frozenset({"future_flag"}))
                self.assertNotIn(RolloutCapability.OPERATOR_READONLY, required)
                decision = self.ledger.evaluate_effect(loud)
                self.assertFalse(decision.permitted)
                self.assertIn("future_flag", decision.ungoverned_effects)

    # -- hardening: time policy (L2) ------------------------------------------

    def test_event_time_outside_clock_skew_is_refused(self) -> None:
        capability = RolloutCapability.OPERATOR_READONLY
        for offset in (timedelta(minutes=5, seconds=1), timedelta(days=365),
                       -timedelta(minutes=5, seconds=1)):
            with self.subTest(offset=offset), self.assertRaises(RolloutError):
                self.activate(capability, now=NOW + offset)
        self.assertEqual(self.ledger.events(), ())
        self.activate(capability, now=NOW + timedelta(minutes=5))
        self.assertTrue(self.ledger.is_active(capability))

    def test_explicit_event_time_must_be_monotonic(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY, now=NOW + timedelta(minutes=2))
        with self.assertRaises(RolloutError):
            self.activate(
                RolloutCapability.OWNED_COMPONENT_MUTATION,
                now=NOW + timedelta(minutes=1),
            )
        self.assertEqual(len(self.ledger.events()), 1)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION, now=NOW + timedelta(minutes=2))
        self.clock.advance(600)
        self.activate(RolloutCapability.HOST_ACTIONS)
        self.assertEqual(self.ledger.verify_chain(), 3)

    def test_implicit_rollback_clamps_forward_skew_to_head(self) -> None:
        activated = self.activate(
            RolloutCapability.OPERATOR_READONLY, now=NOW + timedelta(minutes=5),
        )
        rolled_back = self.rollback(RolloutCapability.OPERATOR_READONLY)
        self.assertEqual(rolled_back.occurred_at, activated.occurred_at)
        self.assertFalse(self.ledger.is_active(RolloutCapability.OPERATOR_READONLY))

    def test_stored_future_timestamp_fails_closed(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.clock.now = NOW - timedelta(hours=2)  # the trusted clock disagrees with history
        for read in self.public_reads():
            with self.assertRaises(RolloutError):
                read()
        with self.assertRaises(RolloutError):
            self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        with self.assertRaises(RolloutError):
            RolloutLedger(self.tasks, clock=self.clock, expected_attestations=self.pins)

    def test_stored_non_monotonic_history_is_tampered(self) -> None:
        first = self.activate(RolloutCapability.OPERATOR_READONLY)
        att = attestation(RolloutCapability.OWNED_COMPONENT_MUTATION)
        backdated = RolloutEvent(
            sequence=2, capability=att.capability, action=RolloutAction.ACTIVATE,
            actor="operator:mallory", policy_revision=att.policy_revision,
            config_revision=att.config_revision, evidence_revision=att.evidence_revision,
            occurred_at=NOW - timedelta(days=1), nonce="backdated",
            canary_evidence=att.canary_evidence, reason="backdated", attestation=att,
            prior_event_hash=first.event_hash,
        )
        self.insert_raw(backdated)
        with self.assertRaises(RolloutTampered):
            self.ledger.status()

    def test_clock_skew_policy_is_bounded(self) -> None:
        for skew in (timedelta(hours=1, seconds=1), -timedelta(seconds=1), 300, None):
            with self.subTest(skew=skew), self.assertRaises(ContractError):
                RolloutLedger(self.tasks, clock=self.clock, max_clock_skew=skew)
        strict = self.repin(max_clock_skew=timedelta(0))
        with self.assertRaises(RolloutError):
            self.activate(RolloutCapability.OPERATOR_READONLY, now=NOW + timedelta(microseconds=1))
        self.activate(RolloutCapability.OPERATOR_READONLY, now=NOW)
        self.assertTrue(strict.is_active(RolloutCapability.OPERATOR_READONLY))

    # -- hardening: external expected-head anchor (H3) ------------------------

    def truncate_newest(self) -> None:
        self.tamper_in_session(
            "DELETE FROM tc_rollout_events WHERE sequence="
            "(SELECT MAX(sequence) FROM tc_rollout_events)")

    def test_suffix_truncation_needs_an_external_anchor(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        middle = self.ledger.head()
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.rollback(RolloutCapability.OWNED_COMPONENT_MUTATION)
        anchor = self.ledger.head()
        self.assertEqual(anchor, RolloutHead(3, self.ledger.head_hash()))
        self.assertEqual(self.ledger.verify_chain(expected_head=anchor), 3)
        self.assertEqual(self.ledger.verify_chain(expected_head=middle), 3)  # chain grew past it

        self.truncate_newest()
        # Documented limitation: a truncated chain is a valid chain, and here the
        # deletion even resurrects a rolled-back capability.
        self.assertEqual(self.ledger.verify_chain(), 2)
        self.assertTrue(self.ledger.is_active(RolloutCapability.OWNED_COMPONENT_MUTATION))
        # Only the trusted external anchor detects it.
        with self.assertRaises(RolloutTampered):
            self.ledger.verify_chain(expected_head=anchor)
        with self.assertRaises(RolloutTampered):
            RolloutLedger(self.tasks, clock=self.clock, expected_attestations=self.pins,
                          expected_head=anchor)

    def test_configured_anchor_guards_every_decision_and_append(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        self.repin(expected_head=self.ledger.head())
        self.activate(RolloutCapability.HOST_ACTIONS)  # growing past the anchor is fine
        self.assertTrue(self.ledger.evaluate(RolloutCapability.HOST_ACTIONS).permitted)
        self.truncate_newest()
        self.assertEqual(self.ledger.verify_chain(), 2)  # still at the anchor
        self.truncate_newest()
        self.assert_everything_fails_closed()

    def test_exact_head_mode_rejects_suffixes_and_is_read_only(self) -> None:
        self.activate(RolloutCapability.OPERATOR_READONLY)
        anchor = self.ledger.head()
        exact = RolloutLedger(
            self.tasks, clock=self.clock, expected_attestations=self.pins,
            expected_head=anchor, require_exact_head=True,
            evidence_verifier=self.evidence_verifier,
        )
        self.assertTrue(exact.evaluate(RolloutCapability.OPERATOR_READONLY).permitted)
        with self.assertRaisesRegex(RolloutError, "read-only"):
            exact.activate(
                RolloutCapability.OWNED_COMPONENT_MUTATION,
                attestation(RolloutCapability.OWNED_COMPONENT_MUTATION),
                actor="operator:alice", nonce="exact-write", reason="not through runtime",
            )

        self.activate(RolloutCapability.OWNED_COMPONENT_MUTATION)
        with self.assertRaisesRegex(RolloutTampered, "exact head"):
            RolloutLedger(
                self.tasks, clock=self.clock, expected_attestations=self.pins,
                expected_head=anchor, require_exact_head=True,
            )

    def test_exact_head_requires_an_anchor(self) -> None:
        with self.assertRaises(ContractError):
            RolloutLedger(
                self.tasks, clock=self.clock, expected_attestations=self.pins,
                require_exact_head=True,
            )

    def test_anchor_rejects_foreign_or_emptied_chain(self) -> None:
        foreign = RolloutHead(1, "f" * 64)
        with self.assertRaises(RolloutTampered):
            RolloutLedger(self.tasks, clock=self.clock, expected_head=foreign)  # emptied ledger
        self.activate(RolloutCapability.OPERATOR_READONLY)
        with self.assertRaises(RolloutTampered):
            self.ledger.verify_chain(expected_head=foreign)
        for bad in ((0, "f" * 64), (1, "F" * 64), (1, "short"), (True, "f" * 64)):
            with self.subTest(head=bad), self.assertRaises(ContractError):
                RolloutHead(*bad)
        with self.assertRaises(ContractError):
            self.ledger.verify_chain(expected_head=("1", "f" * 64))
        with self.assertRaises(ContractError):
            RolloutLedger(self.tasks, clock=self.clock, expected_head="f" * 64)


if __name__ == "__main__":
    unittest.main()
