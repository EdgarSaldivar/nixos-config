"""Pure Phase 7 adapter for an already-observed legacy decision.

This disabled, unwired library accepts frozen facts captured after the legacy
path made its one real decision.  It does not run that path, classify a command,
read a store or clock, call a model or callback, or execute anything.  Missing
or contradictory facts fail closed into the shadow ``UNKNOWN`` vocabulary.

The legacy :class:`authorization.Risk` value is retained only as a consistency
check.  It does not contain effects, authority, approval bindings, or task
routing and can therefore never become a complete shadow decision by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .authorization import Risk
from .plan_authorization import EFFECT_FLAG_NAMES, _AUTHORITY_RANK, Authority
from .plans import ContractError, Effect, _identifier
from .shadow_rollout import (
    MAX_APPROVAL_REFS,
    PolicyDecision,
    PolicyOutcome,
    RouteKind,
    RoutingDecision,
    ShadowDecision,
    required_authority,
)


class LegacyRoute(str, Enum):
    """Closed outcomes an already-run legacy path may report."""

    NEW_TASK = "new-task"
    EXISTING_TASK = "existing-task"
    DUPLICATE = "duplicate"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    NO_ROUTE = "no-route"
    UNKNOWN = "unknown"


LEGACY_ROUTES: Mapping[LegacyRoute, RouteKind] = MappingProxyType({
    LegacyRoute.NEW_TASK: RouteKind.NEW_TASK,
    LegacyRoute.EXISTING_TASK: RouteKind.EXISTING_TASK,
    LegacyRoute.DUPLICATE: RouteKind.DUPLICATE,
    LegacyRoute.AMBIGUOUS: RouteKind.AMBIGUOUS,
    LegacyRoute.REJECTED: RouteKind.REJECTED,
    LegacyRoute.NO_ROUTE: RouteKind.NO_ROUTE,
    LegacyRoute.UNKNOWN: RouteKind.UNKNOWN,
})
if set(LEGACY_ROUTES) != set(LegacyRoute):
    raise RuntimeError("every legacy route needs an explicit shadow route")

_TASK_BOUND_ROUTES = frozenset({LegacyRoute.EXISTING_TASK, LegacyRoute.DUPLICATE})


@dataclass(frozen=True)
class LegacyRoutingFacts:
    """The legacy route and its exact task binding, if observation completed."""

    route: LegacyRoute | None
    task_id: str | None = None

    def __post_init__(self) -> None:
        if self.route is not None and type(self.route) is not LegacyRoute:
            raise ContractError("legacy route must be typed or missing")
        if self.task_id is not None:
            _identifier(self.task_id, "task_id")


@dataclass(frozen=True)
class LegacyEffectFacts:
    """All seven already-derived effect flags; ``None`` means capture failed."""

    owned_component: bool | None = None
    host: bool | None = None
    reachability: bool | None = None
    tenant: bool | None = None
    external_commitment: bool | None = None
    secrets: bool | None = None
    irreversible: bool | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and type(value) is not bool:
                raise ContractError(f"legacy effect fact {item.name} must be boolean or missing")

    @property
    def complete(self) -> bool:
        return all(type(getattr(self, name)) is bool for name in EFFECT_FLAG_NAMES)

    def values(self) -> dict[str, bool]:
        if not self.complete:
            raise ContractError("legacy effect facts are incomplete")
        return {name: getattr(self, name) for name in EFFECT_FLAG_NAMES}


if {item.name for item in fields(LegacyEffectFacts)} != set(EFFECT_FLAG_NAMES):
    raise RuntimeError("legacy observation must explicitly cover every effect flag")


@dataclass(frozen=True)
class LegacyPolicyFacts:
    """Complete effect/authority facts captured from one legacy policy result.

    Optional values represent an observation that did not capture that fact;
    they are not defaults.  ``approval_refs=None`` means missing, while ``()``
    is a complete observation that bound no approval.  ``risk`` is the coarse
    legacy classification and is never used to fill any other field.
    """

    effects: LegacyEffectFacts | None = None
    outcome: PolicyOutcome | None = None
    authority: Authority | None = None
    approval_refs: tuple[str, ...] | None = None
    risk: Risk | None = None

    def __post_init__(self) -> None:
        if self.effects is not None and type(self.effects) is not LegacyEffectFacts:
            raise ContractError("legacy effects must be typed or missing")
        if self.outcome is not None and type(self.outcome) is not PolicyOutcome:
            raise ContractError("legacy policy outcome must be typed or missing")
        if self.authority is not None and type(self.authority) is not Authority:
            raise ContractError("legacy policy authority must be typed or missing")
        if self.risk is not None and type(self.risk) is not Risk:
            raise ContractError("legacy risk must be typed or missing")
        if self.approval_refs is not None:
            if type(self.approval_refs) is not tuple:
                raise ContractError("legacy approval references must be a tuple or missing")
            if len(self.approval_refs) > MAX_APPROVAL_REFS:
                raise ContractError("legacy approval references exceed the shadow bound")
            for index, reference in enumerate(self.approval_refs):
                _identifier(reference, f"approval_refs[{index}]")


@dataclass(frozen=True)
class LegacyObservation:
    """One already-observed legacy route and its policy facts."""

    routing: LegacyRoutingFacts
    policy: LegacyPolicyFacts | None

    def __post_init__(self) -> None:
        if type(self.routing) is not LegacyRoutingFacts:
            raise ContractError("legacy routing must be typed")
        if self.policy is not None and type(self.policy) is not LegacyPolicyFacts:
            raise ContractError("legacy policy must be typed or missing")


_UNKNOWN_EFFECT = Effect(
    effect_id="legacy-observation-unknown",
    description="Legacy effect observation was incomplete or contradictory",
)


def _unknown_policy() -> PolicyDecision:
    return PolicyDecision(
        effect=_UNKNOWN_EFFECT,
        outcome=PolicyOutcome.UNKNOWN,
        authority=None,
    )


def map_legacy_routing(facts: LegacyRoutingFacts) -> RoutingDecision:
    """Map captured routing without invoking or retrying the legacy path."""
    if type(facts) is not LegacyRoutingFacts:
        raise ContractError("legacy routing mapping requires typed facts")
    route = facts.route
    if route is None or route is LegacyRoute.UNKNOWN:
        return RoutingDecision(RouteKind.UNKNOWN)
    if route in _TASK_BOUND_ROUTES:
        if facts.task_id is None:
            return RoutingDecision(RouteKind.UNKNOWN)
        return RoutingDecision(LEGACY_ROUTES[route], facts.task_id)
    if facts.task_id is not None:
        return RoutingDecision(RouteKind.UNKNOWN)
    return RoutingDecision(LEGACY_ROUTES[route])


def _risk_agrees(facts: LegacyPolicyFacts) -> bool:
    """The old coarse label may detect conflict but never supply missing facts."""
    if facts.risk is None:
        return True
    if facts.risk is Risk.REFUSED:
        return facts.outcome is PolicyOutcome.DENY
    if facts.risk is Risk.APPROVAL:
        return facts.outcome is PolicyOutcome.REQUIRE_APPROVAL
    return (
        facts.risk is Risk.SELF
        and facts.outcome is PolicyOutcome.PERMIT
        and facts.authority in {Authority.AGENT, Authority.STANDING_CONSENT}
    )


def map_legacy_policy(facts: LegacyPolicyFacts | None) -> PolicyDecision:
    """Map complete captured policy facts; anything less is shadow ``UNKNOWN``."""
    if facts is not None and type(facts) is not LegacyPolicyFacts:
        raise ContractError("legacy policy mapping requires typed facts or missing")
    if (
        facts is None
        or facts.effects is None
        or not facts.effects.complete
        or facts.outcome is None
        or facts.outcome is PolicyOutcome.UNKNOWN
        or facts.authority is None
        or facts.approval_refs is None
        or not _risk_agrees(facts)
    ):
        return _unknown_policy()

    approvals = facts.approval_refs
    if len(set(approvals)) != len(approvals):
        return _unknown_policy()
    effect = Effect(
        effect_id="legacy-observed-union",
        description="Union of complete already-derived legacy effect facts",
        **facts.effects.values(),
    )
    if _AUTHORITY_RANK[facts.authority] < _AUTHORITY_RANK[required_authority(effect)]:
        return _unknown_policy()

    human = facts.authority in {
        Authority.EXACT_HUMAN,
        Authority.ALWAYS_APPROVE_TENANT,
    }
    if facts.outcome is PolicyOutcome.PERMIT:
        if human or approvals:
            return _unknown_policy()
    elif facts.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        if not human or not approvals:
            return _unknown_policy()
    elif facts.outcome is PolicyOutcome.DENY:
        if approvals:
            return _unknown_policy()
    else:  # Closed today; keeps a future enum member fail-closed until reviewed.
        return _unknown_policy()

    # A tenant fact can never be weakened to ordinary exact-human authority.
    if effect.tenant and facts.authority is not Authority.ALWAYS_APPROVE_TENANT:
        return _unknown_policy()
    return PolicyDecision(
        effect=effect,
        outcome=facts.outcome,
        authority=facts.authority,
        approval_refs=approvals,
    )


def map_legacy_observation(observation: LegacyObservation) -> ShadowDecision:
    """Map one frozen observation to the non-authoritative shadow contract."""
    if type(observation) is not LegacyObservation:
        raise ContractError("legacy observation mapping requires a typed observation")
    return ShadowDecision(
        routing=map_legacy_routing(observation.routing),
        policy=map_legacy_policy(observation.policy),
    )


__all__ = [
    "LEGACY_ROUTES",
    "LegacyEffectFacts",
    "LegacyObservation",
    "LegacyPolicyFacts",
    "LegacyRoute",
    "LegacyRoutingFacts",
    "map_legacy_observation",
    "map_legacy_policy",
    "map_legacy_routing",
]
