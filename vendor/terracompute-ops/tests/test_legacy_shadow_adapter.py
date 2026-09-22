from __future__ import annotations

import dataclasses
import itertools
import unittest
from unittest import mock

from terracompute_ops.authorization import Risk
from terracompute_ops.legacy_shadow_adapter import (
    LEGACY_ROUTES,
    LegacyEffectFacts,
    LegacyObservation,
    LegacyPolicyFacts,
    LegacyRoute,
    LegacyRoutingFacts,
    map_legacy_observation,
    map_legacy_policy,
    map_legacy_routing,
)
from terracompute_ops.plan_authorization import EFFECT_FLAG_NAMES, Authority
from terracompute_ops.plans import ContractError
from terracompute_ops.shadow_rollout import (
    PolicyOutcome,
    RouteKind,
    RoutingDecision,
    ShadowDecision,
    effect_flags,
)


def effects(**overrides: bool | None) -> LegacyEffectFacts:
    values = dict.fromkeys(EFFECT_FLAG_NAMES, False)
    values.update(overrides)
    return LegacyEffectFacts(**values)


def policy(**overrides) -> LegacyPolicyFacts:
    values = dict(
        effects=effects(),
        outcome=PolicyOutcome.PERMIT,
        authority=Authority.AGENT,
        approval_refs=(),
        risk=None,
    )
    values.update(overrides)
    return LegacyPolicyFacts(**values)


class LegacyRoutingAdapterTest(unittest.TestCase):
    def test_route_mapping_is_exhaustive_and_immutable(self) -> None:
        self.assertEqual(set(LEGACY_ROUTES), set(LegacyRoute))
        self.assertEqual(set(LEGACY_ROUTES.values()), set(RouteKind) - {RouteKind.LEGACY_HANDOVER})
        with self.assertRaises(TypeError):
            LEGACY_ROUTES[LegacyRoute.REJECTED] = RouteKind.NEW_TASK

    def test_every_complete_route_maps_exactly(self) -> None:
        for route in LegacyRoute:
            with self.subTest(route=route):
                task_id = "task-7" if route in {
                    LegacyRoute.EXISTING_TASK, LegacyRoute.DUPLICATE,
                } else None
                actual = map_legacy_routing(LegacyRoutingFacts(route, task_id))
                expected = LEGACY_ROUTES[route]
                self.assertIs(actual.route, expected)
                self.assertEqual(actual.task_id, task_id)

    def test_missing_or_contradictory_task_binding_is_unknown(self) -> None:
        cases = (
            LegacyRoutingFacts(None),
            LegacyRoutingFacts(LegacyRoute.EXISTING_TASK),
            LegacyRoutingFacts(LegacyRoute.DUPLICATE),
            LegacyRoutingFacts(LegacyRoute.NEW_TASK, "invented-task"),
            LegacyRoutingFacts(LegacyRoute.AMBIGUOUS, "invented-task"),
            LegacyRoutingFacts(LegacyRoute.REJECTED, "invented-task"),
            LegacyRoutingFacts(LegacyRoute.NO_ROUTE, "invented-task"),
            LegacyRoutingFacts(LegacyRoute.UNKNOWN, "invented-task"),
        )
        for facts in cases:
            with self.subTest(facts=facts):
                self.assertEqual(map_legacy_routing(facts), RoutingDecision(RouteKind.UNKNOWN))

    def test_mapper_never_fabricates_a_task_identity(self) -> None:
        for route in LegacyRoute:
            result = map_legacy_routing(LegacyRoutingFacts(route))
            self.assertIsNone(result.task_id)
        bound = map_legacy_routing(
            LegacyRoutingFacts(LegacyRoute.EXISTING_TASK, "task-from-observation"))
        self.assertEqual(bound.task_id, "task-from-observation")


class LegacyPolicyAdapterTest(unittest.TestCase):
    def assertUnknown(self, facts: LegacyPolicyFacts | None) -> None:
        result = map_legacy_policy(facts)
        self.assertIs(result.outcome, PolicyOutcome.UNKNOWN)
        self.assertIsNone(result.authority)
        self.assertEqual(result.approval_refs, ())
        self.assertEqual(effect_flags(result.effect), frozenset())

    def test_complete_read_owned_host_and_tenant_facts_map(self) -> None:
        cases = (
            policy(),
            policy(
                effects=effects(owned_component=True),
                authority=Authority.STANDING_CONSENT,
                risk=Risk.SELF,
            ),
            policy(
                effects=effects(host=True),
                outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN,
                approval_refs=("approval:host:task-7",),
                risk=Risk.APPROVAL,
            ),
            policy(
                effects=effects(tenant=True),
                outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.ALWAYS_APPROVE_TENANT,
                approval_refs=("approval:tenant:C.42:task-7",),
                risk=Risk.APPROVAL,
            ),
            policy(
                effects=effects(tenant=True),
                outcome=PolicyOutcome.DENY,
                authority=Authority.ALWAYS_APPROVE_TENANT,
                risk=Risk.REFUSED,
            ),
        )
        for facts in cases:
            with self.subTest(facts=facts):
                mapped = map_legacy_policy(facts)
                self.assertIs(mapped.outcome, facts.outcome)
                self.assertIs(mapped.authority, facts.authority)
                self.assertEqual(mapped.approval_refs, tuple(sorted(facts.approval_refs)))
                self.assertEqual(effect_flags(mapped.effect), frozenset(
                    name for name in EFFECT_FLAG_NAMES if getattr(facts.effects, name)
                ))

    def test_every_missing_policy_fact_is_unknown(self) -> None:
        self.assertUnknown(None)
        self.assertUnknown(LegacyPolicyFacts())
        complete = policy()
        self.assertUnknown(dataclasses.replace(complete, effects=None))
        self.assertUnknown(dataclasses.replace(complete, outcome=None))
        self.assertUnknown(dataclasses.replace(complete, authority=None))
        self.assertUnknown(dataclasses.replace(complete, approval_refs=None))
        for name in EFFECT_FLAG_NAMES:
            incomplete = dataclasses.replace(complete.effects, **{name: None})
            self.assertUnknown(dataclasses.replace(complete, effects=incomplete))

    def test_risk_alone_never_becomes_a_policy_decision(self) -> None:
        for risk in Risk:
            with self.subTest(risk=risk):
                self.assertUnknown(LegacyPolicyFacts(risk=risk))

    def test_refusal_and_unknown_never_become_permit(self) -> None:
        for risk in (Risk.REFUSED, Risk.APPROVAL):
            with self.subTest(risk=risk):
                self.assertUnknown(policy(risk=risk))
        explicit = policy(outcome=PolicyOutcome.UNKNOWN, authority=None, approval_refs=())
        self.assertUnknown(explicit)
        refused = map_legacy_policy(policy(
            effects=effects(tenant=True), outcome=PolicyOutcome.DENY,
            authority=Authority.ALWAYS_APPROVE_TENANT, risk=Risk.REFUSED,
        ))
        self.assertIs(refused.outcome, PolicyOutcome.DENY)

    def test_all_effect_combinations_preserve_flags_and_authority_floor(self) -> None:
        for bits in itertools.product((False, True), repeat=len(EFFECT_FLAG_NAMES)):
            flags = dict(zip(EFFECT_FLAG_NAMES, bits))
            fact_effects = LegacyEffectFacts(**flags)
            probe = map_legacy_policy(policy(
                effects=fact_effects,
                outcome=PolicyOutcome.DENY,
                authority=Authority.AGENT,
            ))
            required = (
                Authority.ALWAYS_APPROVE_TENANT if flags["tenant"]
                else Authority.EXACT_HUMAN if any(
                    flags[name] for name in EFFECT_FLAG_NAMES if name != "owned_component"
                )
                else Authority.STANDING_CONSENT if flags["owned_component"]
                else Authority.AGENT
            )
            mapped = map_legacy_policy(policy(
                effects=fact_effects,
                outcome=(PolicyOutcome.REQUIRE_APPROVAL
                         if required in {Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT}
                         else PolicyOutcome.PERMIT),
                authority=required,
                approval_refs=("approval:bound",)
                if required in {Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT} else (),
            ))
            with self.subTest(flags=flags):
                self.assertIsNot(mapped.outcome, PolicyOutcome.UNKNOWN)
                self.assertEqual(effect_flags(mapped.effect), frozenset(
                    name for name, value in flags.items() if value))
                self.assertIs(mapped.authority, required)
                if any(flags.values()):
                    self.assertIs(probe.outcome, PolicyOutcome.UNKNOWN)

    def test_tenant_and_human_approval_boundaries_fail_closed(self) -> None:
        cases = (
            policy(effects=effects(tenant=True), authority=Authority.AGENT),
            policy(
                effects=effects(tenant=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN, approval_refs=("approval:tenant",)),
            policy(
                effects=effects(tenant=True), outcome=PolicyOutcome.PERMIT,
                authority=Authority.ALWAYS_APPROVE_TENANT),
            policy(
                effects=effects(host=True), outcome=PolicyOutcome.PERMIT,
                authority=Authority.EXACT_HUMAN),
            policy(
                effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN, approval_refs=()),
            policy(
                effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN, approval_refs=None),
            policy(
                effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN,
                approval_refs=("approval:a", "approval:a")),
            policy(outcome=PolicyOutcome.PERMIT, approval_refs=("approval:unexpected",)),
            policy(outcome=PolicyOutcome.DENY, approval_refs=("approval:unexpected",)),
        )
        for facts in cases:
            with self.subTest(facts=facts):
                self.assertUnknown(facts)

    def test_bound_approval_references_are_preserved_not_invented(self) -> None:
        supplied = ("approval:z", "approval:a")
        mapped = map_legacy_policy(policy(
            effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
            authority=Authority.EXACT_HUMAN, approval_refs=supplied,
        ))
        self.assertEqual(mapped.approval_refs, tuple(sorted(supplied)))
        self.assertUnknown(policy(
            effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
            authority=Authority.EXACT_HUMAN, approval_refs=(),
        ))


class LegacyObservationTest(unittest.TestCase):
    def test_combined_mapping_is_frozen_pure_and_deterministic(self) -> None:
        observation = LegacyObservation(
            routing=LegacyRoutingFacts(LegacyRoute.EXISTING_TASK, "task-7"),
            policy=policy(
                effects=effects(host=True), outcome=PolicyOutcome.REQUIRE_APPROVAL,
                authority=Authority.EXACT_HUMAN, approval_refs=("approval:task-7",),
            ),
        )
        expected = ShadowDecision(
            routing=RoutingDecision(RouteKind.EXISTING_TASK, "task-7"),
            policy=map_legacy_policy(observation.policy),
        )
        self.assertEqual(map_legacy_observation(observation), expected)
        self.assertEqual(map_legacy_observation(observation), expected)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            observation.policy = None

    def test_missing_policy_is_an_explicit_unknown_not_an_absent_decision(self) -> None:
        mapped = map_legacy_observation(LegacyObservation(
            routing=LegacyRoutingFacts(LegacyRoute.REJECTED), policy=None,
        ))
        self.assertIsNotNone(mapped.policy)
        self.assertIs(mapped.policy.outcome, PolicyOutcome.UNKNOWN)

    def test_no_legacy_classifier_callback_store_clock_or_executor_is_called(self) -> None:
        observation = LegacyObservation(
            routing=LegacyRoutingFacts(LegacyRoute.REJECTED),
            policy=policy(
                effects=effects(tenant=True), outcome=PolicyOutcome.DENY,
                authority=Authority.ALWAYS_APPROVE_TENANT, risk=Risk.REFUSED,
            ),
        )
        forbidden = mock.Mock(side_effect=AssertionError("decision path was invoked"))
        with mock.patch("terracompute_ops.authorization.classify", forbidden), \
                mock.patch("terracompute_ops.acting.MonitoringActor.run", forbidden), \
                mock.patch("terracompute_ops.task_service.TaskService.handle", forbidden), \
                mock.patch("terracompute_ops.tasks.TaskStore.create_task", forbidden):
            mapped = map_legacy_observation(observation)
        self.assertIs(mapped.routing.route, RouteKind.REJECTED)
        self.assertIs(mapped.policy.outcome, PolicyOutcome.DENY)
        forbidden.assert_not_called()

    def test_foreign_objects_and_mutable_collections_are_rejected(self) -> None:
        for call in (
            lambda: LegacyRoutingFacts("rejected"),
            lambda: LegacyEffectFacts(host=1),
            lambda: LegacyPolicyFacts(effects={}),
            lambda: LegacyPolicyFacts(outcome="deny"),
            lambda: LegacyPolicyFacts(authority="agent"),
            lambda: LegacyPolicyFacts(risk="self"),
            lambda: LegacyPolicyFacts(approval_refs=[]),
            lambda: LegacyObservation(routing={"route": "rejected"}, policy=None),
            lambda: map_legacy_routing({"route": "rejected"}),
            lambda: map_legacy_policy({"outcome": "deny"}),
            lambda: map_legacy_observation({"routing": "rejected"}),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ContractError):
                    call()


if __name__ == "__main__":
    unittest.main()
