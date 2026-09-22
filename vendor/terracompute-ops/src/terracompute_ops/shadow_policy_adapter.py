"""Pure, disabled Phase 7 adapter for already-derived Phase 3 policy values.

This module maps one complete :class:`PlanDecision` into the strict shadow
policy vocabulary.  It has no store, clock, classifier, accountant, callback,
model, executor, or network access and never calls ``authorize_plan``.  Any
partial or contradictory decision becomes an explicit ``UNKNOWN`` decision so
the authenticated producer can account it as a blocking capture failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .plan_authorization import (
    EFFECT_FLAG_NAMES,
    _AUTHORITY_RANK,
    Authority,
    PlanDecision,
    StepRisk,
)
from .plans import ContractError, Effect, _identifier, stable_hash
from .shadow_rollout import (
    MAX_APPROVAL_REFS,
    PolicyDecision,
    PolicyOutcome,
    required_authority,
)


_HASH = re.compile(r"[0-9a-f]{64}")
_HUMAN = frozenset({Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT})
_UNKNOWN_EFFECT = Effect(
    effect_id="phase3-policy-unknown",
    description="Phase 3 policy observation was incomplete or contradictory",
)


@dataclass(frozen=True)
class ApprovalReference:
    """One opaque approval identity bound to one exact plan step."""

    step_id: str
    reference: str

    def __post_init__(self) -> None:
        _identifier(self.step_id, "approval step_id")
        _identifier(self.reference, "approval reference")


def _unknown() -> PolicyDecision:
    return PolicyDecision(_UNKNOWN_EFFECT, PolicyOutcome.UNKNOWN, None)


def _risk_is_complete(risk: StepRisk) -> bool:
    if type(risk) is not StepRisk or not isinstance(risk.authority, Authority):
        return False
    try:
        _identifier(risk.step_id, "step_id")
    except ContractError:
        return False
    if type(risk.flags) is not frozenset or not all(
        type(item) is str for item in risk.flags
    ):
        return False
    if not risk.flags <= frozenset(EFFECT_FLAG_NAMES):
        return False
    if type(risk.tenant_references) is not tuple or type(risk.reasons) is not tuple:
        return False
    if type(risk.payload_read) is not bool:
        return False
    try:
        for item in risk.tenant_references:
            _identifier(item, "tenant reference")
        for item in risk.reasons:
            _identifier(item, "risk reason")
    except ContractError:
        return False

    tenant = "tenant" in risk.flags
    if risk.payload_read or risk.tenant_references:
        if not tenant:
            return False
    if tenant != (risk.authority is Authority.ALWAYS_APPROVE_TENANT):
        return False
    if risk.authority is Authority.AGENT:
        return not risk.flags and not risk.payload_read and not risk.tenant_references
    if risk.authority is Authority.STANDING_CONSENT:
        return risk.flags == frozenset({"owned_component"})
    if risk.authority is Authority.EXACT_HUMAN:
        return not tenant and not risk.payload_read and not risk.tenant_references
    return tenant


def _approval_map(
    bindings: tuple[ApprovalReference, ...],
) -> dict[str, str] | None:
    if type(bindings) is not tuple or len(bindings) > MAX_APPROVAL_REFS:
        return None
    result: dict[str, str] = {}
    references: set[str] = set()
    for binding in bindings:
        if type(binding) is not ApprovalReference:
            return None
        if binding.step_id in result or binding.reference in references:
            return None
        result[binding.step_id] = binding.reference
        references.add(binding.reference)
    return result


def map_plan_policy(
    decision: PlanDecision,
    *,
    approval_references: tuple[ApprovalReference, ...] = (),
) -> PolicyDecision:
    """Map a complete captured Phase 3 decision without re-authorizing it.

    Human-authority decisions always remain ``REQUIRE_APPROVAL`` and must carry
    exactly one caller-captured opaque reference for every approval or payload
    step.  Extra, missing, duplicated, or cross-step references fail closed.
    """
    if type(decision) is not PlanDecision:
        raise ContractError("policy mapping requires a typed PlanDecision")
    bindings = _approval_map(approval_references)
    if bindings is None:
        return _unknown()
    if (
        type(decision.plan_hash) is not str
        or _HASH.fullmatch(decision.plan_hash) is None
        or type(decision.step_risks) is not tuple
        or not decision.step_risks
        or not isinstance(decision.plan_authority, Authority)
        or type(decision.approval_step_ids) is not tuple
        or type(decision.payload_step_ids) is not tuple
    ):
        return _unknown()
    risks = decision.step_risks
    if any(not _risk_is_complete(risk) for risk in risks):
        return _unknown()
    step_ids = tuple(risk.step_id for risk in risks)
    if len(set(step_ids)) != len(step_ids):
        return _unknown()

    derived_authority = max(
        (risk.authority for risk in risks), key=_AUTHORITY_RANK.__getitem__
    )
    derived_payload = tuple(risk.step_id for risk in risks if risk.payload_read)
    derived_approval = tuple(
        risk.step_id
        for risk in risks
        if not risk.payload_read and risk.authority in _HUMAN
    )
    if (
        decision.plan_authority is not derived_authority
        or decision.approval_step_ids != derived_approval
        or decision.payload_step_ids != derived_payload
        or len(set(decision.approval_step_ids)) != len(decision.approval_step_ids)
        or len(set(decision.payload_step_ids)) != len(decision.payload_step_ids)
        or set(decision.approval_step_ids) & set(decision.payload_step_ids)
    ):
        return _unknown()

    required_steps = set(derived_approval) | set(derived_payload)
    if set(bindings) != required_steps:
        return _unknown()

    flags = frozenset().union(*(risk.flags for risk in risks))
    effect = Effect(
        effect_id="phase3-plan-union-" + decision.plan_hash[:32],
        description="Union of complete already-derived Phase 3 step effects",
        **{name: name in flags for name in EFFECT_FLAG_NAMES},
    )
    if _AUTHORITY_RANK[decision.plan_authority] < _AUTHORITY_RANK[
        required_authority(effect)
    ]:
        return _unknown()

    # PolicyDecision intentionally carries opaque identifiers, not action data.
    # Hash the exact step/reference pair so divergence compares the authorization
    # binding rather than an unordered bag of approval identifiers.  Sorting in
    # PolicyDecision remains safe because the step identity is inside each hash.
    references = tuple(
        "phase3-approval-binding-" + stable_hash({
            "step_id": step_id,
            "reference": bindings[step_id],
        })
        for step_id in (*derived_approval, *derived_payload)
    )
    if decision.plan_authority in _HUMAN:
        if not references:
            return _unknown()
        return PolicyDecision(
            effect,
            PolicyOutcome.REQUIRE_APPROVAL,
            decision.plan_authority,
            references,
        )
    if references:
        return _unknown()
    return PolicyDecision(effect, PolicyOutcome.PERMIT, decision.plan_authority)


__all__ = ["ApprovalReference", "map_plan_policy"]
