from __future__ import annotations

import dataclasses
import itertools
import unittest

from terracompute_ops.plan_authorization import (
    EFFECT_FLAG_NAMES,
    Authority,
    PlanDecision,
    StepRisk,
)
from terracompute_ops.plans import ContractError, stable_hash
from terracompute_ops.shadow_policy_adapter import ApprovalReference, map_plan_policy
from terracompute_ops.shadow_rollout import PolicyOutcome, effect_flags


PLAN_HASH = "a" * 64


def risk(
    step_id: str = "step-1",
    *,
    authority: Authority = Authority.AGENT,
    flags: frozenset[str] = frozenset(),
    tenant_references: tuple[str, ...] = (),
    payload_read: bool = False,
) -> StepRisk:
    return StepRisk(
        step_id, authority, flags, tenant_references, payload_read, ("derived",)
    )


def decision(*risks: StepRisk) -> PlanDecision:
    authority = max(
        (item.authority for item in risks),
        key={
            Authority.AGENT: 0,
            Authority.STANDING_CONSENT: 1,
            Authority.EXACT_HUMAN: 2,
            Authority.ALWAYS_APPROVE_TENANT: 3,
        }.__getitem__,
    )
    return PlanDecision(
        PLAN_HASH,
        tuple(risks),
        authority,
        tuple(
            item.step_id
            for item in risks
            if not item.payload_read
            and item.authority
            in {Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT}
        ),
        tuple(item.step_id for item in risks if item.payload_read),
    )


class ShadowPolicyAdapterTest(unittest.TestCase):
    def assert_unknown(self, value) -> None:
        self.assertIs(value.outcome, PolicyOutcome.UNKNOWN)
        self.assertIsNone(value.authority)
        self.assertEqual(value.approval_refs, ())
        self.assertEqual(effect_flags(value.effect), frozenset())

    def test_agent_and_standing_decisions_permit_without_approval(self) -> None:
        agent = map_plan_policy(decision(risk()))
        self.assertIs(agent.outcome, PolicyOutcome.PERMIT)
        self.assertIs(agent.authority, Authority.AGENT)
        standing = map_plan_policy(decision(risk(
            authority=Authority.STANDING_CONSENT,
            flags=frozenset({"owned_component"}),
        )))
        self.assertIs(standing.outcome, PolicyOutcome.PERMIT)
        self.assertIs(standing.authority, Authority.STANDING_CONSENT)

    def test_human_and_payload_steps_require_exact_bound_references(self) -> None:
        captured = decision(
            risk("host", authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})),
            risk(
                "payload",
                authority=Authority.ALWAYS_APPROVE_TENANT,
                flags=frozenset({"tenant"}),
                tenant_references=("C.42",),
                payload_read=True,
            ),
        )
        mapped = map_plan_policy(
            captured,
            approval_references=(
                ApprovalReference("host", "approval:host"),
                ApprovalReference("payload", "approval:payload:C.42"),
            ),
        )
        self.assertIs(mapped.outcome, PolicyOutcome.REQUIRE_APPROVAL)
        self.assertIs(mapped.authority, Authority.ALWAYS_APPROVE_TENANT)
        self.assertEqual(
            mapped.approval_refs,
            tuple(sorted((
                "phase3-approval-binding-" + stable_hash({
                    "step_id": "host", "reference": "approval:host",
                }),
                "phase3-approval-binding-" + stable_hash({
                    "step_id": "payload", "reference": "approval:payload:C.42",
                }),
            ))),
        )
        self.assertEqual(effect_flags(mapped.effect), frozenset({"host", "tenant"}))

    def test_missing_extra_duplicate_or_cross_step_references_are_unknown(self) -> None:
        captured = decision(risk(
            authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})
        ))
        cases = (
            (),
            (ApprovalReference("step-1", "approval:a"), ApprovalReference("extra", "approval:b")),
            (ApprovalReference("step-1", "approval:a"), ApprovalReference("step-1", "approval:b")),
            (ApprovalReference("step-1", "approval:a"), ApprovalReference("extra", "approval:a")),
        )
        for bindings in cases:
            with self.subTest(bindings=bindings):
                self.assert_unknown(map_plan_policy(captured, approval_references=bindings))

    def test_all_effect_combinations_preserve_the_exact_union(self) -> None:
        for bits in itertools.product((False, True), repeat=len(EFFECT_FLAG_NAMES)):
            flags = frozenset(
                name for name, enabled in zip(EFFECT_FLAG_NAMES, bits) if enabled
            )
            if "tenant" in flags:
                authority = Authority.ALWAYS_APPROVE_TENANT
                refs = (ApprovalReference("step-1", "approval:tenant"),)
            elif flags == {"owned_component"}:
                authority = Authority.STANDING_CONSENT
                refs = ()
            elif flags:
                authority = Authority.EXACT_HUMAN
                refs = (ApprovalReference("step-1", "approval:effect"),)
            else:
                authority = Authority.AGENT
                refs = ()
            mapped = map_plan_policy(
                decision(risk(authority=authority, flags=flags)),
                approval_references=refs,
            )
            with self.subTest(flags=flags):
                self.assertIsNot(mapped.outcome, PolicyOutcome.UNKNOWN)
                self.assertEqual(effect_flags(mapped.effect), flags)

    def test_human_authority_never_becomes_unapproved_permit(self) -> None:
        captured = decision(risk(authority=Authority.EXACT_HUMAN))
        self.assert_unknown(map_plan_policy(captured))
        mapped = map_plan_policy(
            captured,
            approval_references=(ApprovalReference("step-1", "approval:novel"),),
        )
        self.assertIs(mapped.outcome, PolicyOutcome.REQUIRE_APPROVAL)

    def test_swapping_approvals_between_steps_changes_policy_evidence(self) -> None:
        captured = decision(
            risk("host-a", authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})),
            risk("host-b", authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})),
        )
        original = map_plan_policy(captured, approval_references=(
            ApprovalReference("host-a", "approval:a"),
            ApprovalReference("host-b", "approval:b"),
        ))
        swapped = map_plan_policy(captured, approval_references=(
            ApprovalReference("host-a", "approval:b"),
            ApprovalReference("host-b", "approval:a"),
        ))
        self.assertNotEqual(original.approval_refs, swapped.approval_refs)
        self.assertNotEqual(original.content_hash, swapped.content_hash)

    def test_inconsistent_decision_fields_fail_closed(self) -> None:
        valid = decision(risk(
            authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})
        ))
        bindings = (ApprovalReference("step-1", "approval:host"),)
        variants = (
            dataclasses.replace(valid, plan_hash="bad"),
            dataclasses.replace(valid, plan_authority=Authority.AGENT),
            dataclasses.replace(valid, approval_step_ids=()),
            dataclasses.replace(valid, approval_step_ids=("step-1", "step-1")),
            dataclasses.replace(valid, payload_step_ids=("step-1",)),
            dataclasses.replace(valid, step_risks=()),
            dataclasses.replace(valid, step_risks=(valid.step_risks[0], valid.step_risks[0])),
            decision(risk(authority=Authority.AGENT, flags=frozenset({"host"}))),
            decision(risk(
                authority=Authority.EXACT_HUMAN, flags=frozenset({"tenant"})
            )),
            decision(risk(
                authority=Authority.EXACT_HUMAN,
                flags=frozenset(),
                tenant_references=("C.42",),
            )),
            decision(risk(
                authority=Authority.STANDING_CONSENT,
                flags=frozenset({"owned_component", "host"}),
            )),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_unknown(map_plan_policy(
                    variant, approval_references=bindings
                ))

    def test_unknown_flags_mutable_fields_and_foreign_values_fail_closed(self) -> None:
        base = decision(risk())
        malformed = (
            dataclasses.replace(base, step_risks=(dataclasses.replace(
                base.step_risks[0], flags=frozenset({"future-effect"})
            ),)),
            dataclasses.replace(base, step_risks=(dataclasses.replace(
                base.step_risks[0], flags={"host"}  # type: ignore[arg-type]
            ),)),
            dataclasses.replace(base, step_risks=(dataclasses.replace(
                base.step_risks[0], tenant_references=["C.42"]  # type: ignore[arg-type]
            ),)),
            dataclasses.replace(base, step_risks=(dataclasses.replace(
                base.step_risks[0], payload_read=1  # type: ignore[arg-type]
            ),)),
        )
        for variant in malformed:
            self.assert_unknown(map_plan_policy(variant))
        self.assert_unknown(map_plan_policy(base, approval_references=[]))  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            map_plan_policy({"plan_hash": PLAN_HASH})  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            ApprovalReference("", "approval:x")

    def test_inputs_and_bindings_are_not_mutated(self) -> None:
        captured = decision(risk(
            authority=Authority.EXACT_HUMAN, flags=frozenset({"host"})
        ))
        bindings = (ApprovalReference("step-1", "approval:host"),)
        before = dataclasses.asdict(captured)
        self.assertEqual(
            map_plan_policy(captured, approval_references=bindings),
            map_plan_policy(captured, approval_references=bindings),
        )
        self.assertEqual(dataclasses.asdict(captured), before)


if __name__ == "__main__":
    unittest.main()
