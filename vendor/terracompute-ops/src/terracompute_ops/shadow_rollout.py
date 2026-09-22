"""Disabled-by-default Phase 7 shadow comparison and canary-evidence foundation.

This module is a library only.  It accepts *already-derived* old-path and
new-path gateway routing and policy decisions as strict immutable contracts,
classifies how they diverge, and records each comparison append-only in the
shared Phase 0 task database for Vast machine ``17049``.  From those records it
derives a bounded, deterministic canary summary suitable as the
``canary_evidence`` of a :class:`~terracompute_ops.rollout.CommissioningAttestation`.

Nothing here invokes the old or the new path, accepts a callback, executes an
action, calls a model, flips a feature flag, or wires itself into a service.  A
shadow record or summary is evidence about two decisions; it is never an
approval, a lease, or any other execution authority.

Divergence is classified from *effect and authority semantics*, never from an
operation, command, unit, or API name: the effect flags each path derived, the
policy outcome, the authority it demands, and the approvals it binds.  A novel
operation is therefore classified by what it does with no change here.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .plan_authorization import _AUTHORITY_RANK, EFFECT_FLAG_NAMES, Authority
from .plans import (
    MACHINE_ID,
    Contract,
    ContractError,
    Effect,
    _fields,
    _identifier,
    _utc,
    parse_utc,
    utc_text,
)
from .rollout import RolloutCapability
from .tasks import TaskStore

# Version 5 additionally hash-chains durable per-parent capture accounting.
# Earlier evidence would be interpreted differently; no shadow ledger has been
# deployed, so old contracts and databases are refused, never migrated.
SHADOW_SCHEMA_VERSION = 5
SCHEMA_VERSION = 5

# Hard bounds.  Configuration may tighten these but can never configure them away.
DEFAULT_MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_CLOCK_SKEW_LIMIT = timedelta(hours=1)
DEFAULT_MAX_OBSERVATION_AGE = timedelta(minutes=10)
MAX_OBSERVATION_AGE_LIMIT = timedelta(hours=1)
MAX_LEDGER_RECORDS = 100_000
MAX_PAGE_RECORDS = 500
MAX_APPROVAL_REFS = 256
MAX_SOURCE_INSTANCES = 16
MINIMUM_SAMPLE_FLOOR = 10
MIN_OBSERVATION_WINDOW = timedelta(hours=1)
MAX_OBSERVATION_WINDOW = timedelta(days=30)
MAX_EVIDENCE_AGE_LIMIT = timedelta(days=7)

_HASH = re.compile(r"[0-9a-f]{64}")


class ShadowError(RuntimeError):
    """Shadow evidence is unsafe or unusable and is refused fail-closed."""


class ShadowConflict(ShadowError):
    """A nonce or a source/request pair was replayed with different content."""


class ShadowTampered(ShadowError):
    """Durable shadow state failed verification (schema, row, column, or chain)."""


class RouteKind(str, Enum):
    NEW_TASK = "new-task"
    EXISTING_TASK = "existing-task"
    LEGACY_HANDOVER = "legacy-handover"
    DUPLICATE = "duplicate"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    NO_ROUTE = "no-route"
    UNKNOWN = "unknown"


class PolicyOutcome(str, Enum):
    PERMIT = "permit"
    REQUIRE_APPROVAL = "require-approval"
    DENY = "deny"
    UNKNOWN = "unknown"


class ComparisonOrigin(str, Enum):
    OPERATOR = "operator"
    DETECTOR = "detector"


class CaptureOutcome(str, Enum):
    CAPTURED = "captured"
    FAILED = "failed"


class CaptureEventKind(str, Enum):
    EXPECTATION = "expectation"
    RESULT = "result"


class CoverageClass(str, Enum):
    OPERATOR_READONLY = "operator-readonly"
    OWNED_COMPONENT_MUTATION = "owned-component-mutation"
    OWNED_COMPONENT_STANDING_CONSENT = "owned-component-standing-consent"
    HOST_EFFECT = "host-effect"
    TENANT_EFFECT = "tenant-effect"
    EXTERNAL_COMMITMENT = "external-commitment"
    DETECTOR_ROUTING = "detector-routing"
    LEGACY_HANDOVER = "legacy-handover"


COVERAGE_ORDER: tuple[CoverageClass, ...] = tuple(CoverageClass)
# Every capability has an explicit rule, including routing retirement. Each
# required class needs at least the sample floor; criteria may only tighten it.
CAPABILITY_COVERAGE: Mapping[RolloutCapability, frozenset[CoverageClass]] = MappingProxyType({
    RolloutCapability.OPERATOR_READONLY: frozenset({CoverageClass.OPERATOR_READONLY}),
    RolloutCapability.OWNED_COMPONENT_MUTATION: frozenset({CoverageClass.OWNED_COMPONENT_MUTATION}),
    RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT: frozenset({
        CoverageClass.OWNED_COMPONENT_STANDING_CONSENT,
    }),
    RolloutCapability.HOST_ACTIONS: frozenset({CoverageClass.HOST_EFFECT}),
    RolloutCapability.TENANT_ACTIONS: frozenset({CoverageClass.TENANT_EFFECT}),
    RolloutCapability.EXTERNAL_WRITES: frozenset({CoverageClass.EXTERNAL_COMMITMENT}),
    RolloutCapability.DETECTOR_CREATED_TASKS: frozenset({CoverageClass.DETECTOR_ROUTING}),
    RolloutCapability.LEGACY_LOOP_RETIREMENT: frozenset({
        CoverageClass.DETECTOR_ROUTING, CoverageClass.LEGACY_HANDOVER,
    }),
})
assert set(CAPABILITY_COVERAGE) == set(RolloutCapability), "every capability needs a coverage rule"
assert all(CAPABILITY_COVERAGE.values()), "coverage rules must not be empty"
assert set().union(*CAPABILITY_COVERAGE.values()) == set(CoverageClass)
# A new effect flag must be reviewed here before it can silently earn coverage.
assert set(EFFECT_FLAG_NAMES) == {
    "owned_component", "host", "reachability", "tenant", "external_commitment",
    "secrets", "irreversible",
}


class DivergenceClass(str, Enum):
    EQUIVALENT = "equivalent"
    SAFER_NEW = "safer-new"
    MORE_PERMISSIVE_NEW = "more-permissive-new"
    ROUTING_MISMATCH = "routing-mismatch"
    MALFORMED_UNKNOWN = "malformed-unknown"


DIVERGENCE_ORDER: tuple[DivergenceClass, ...] = tuple(DivergenceClass)
# Any one of these in a window prevents canary acceptance, whatever the thresholds.
BLOCKING_CLASSES = frozenset({
    DivergenceClass.MORE_PERMISSIVE_NEW, DivergenceClass.MALFORMED_UNKNOWN,
})
# Benign divergence is tolerated only up to an explicit per-class threshold.
BENIGN_CLASSES: tuple[DivergenceClass, ...] = (
    DivergenceClass.SAFER_NEW, DivergenceClass.ROUTING_MISMATCH,
)
assert set(DIVERGENCE_ORDER) == BLOCKING_CLASSES | set(BENIGN_CLASSES) | {DivergenceClass.EQUIVALENT}

_OUTCOME_RANK = MappingProxyType({
    PolicyOutcome.PERMIT: 0, PolicyOutcome.REQUIRE_APPROVAL: 1, PolicyOutcome.DENY: 2,
})
_TASK_BOUND_ROUTES = frozenset({RouteKind.EXISTING_TASK, RouteKind.DUPLICATE, RouteKind.LEGACY_HANDOVER})
# A route either lets the request proceed toward execution or suppresses it.
_EXECUTABLE_ROUTES = frozenset({RouteKind.NEW_TASK, RouteKind.EXISTING_TASK, RouteKind.LEGACY_HANDOVER})
_SUPPRESSING_ROUTES = frozenset({
    RouteKind.DUPLICATE, RouteKind.AMBIGUOUS, RouteKind.REJECTED, RouteKind.NO_ROUTE,
})
assert set(RouteKind) == _EXECUTABLE_ROUTES | _SUPPRESSING_ROUTES | {RouteKind.UNKNOWN}


def _hash_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _positive(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name} must be an integer of at least {minimum}")
    return value


def _sorted_identifiers(value: Any, name: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ContractError(f"{name} must be a bounded array")
    result = tuple(sorted(_identifier(item, f"{name}[{index}]") for index, item in enumerate(value)))
    if len(set(result)) != len(result):
        raise ContractError(f"{name} contains duplicates")
    return result


def _enum(kind: type[Enum], value: Any, name: str) -> Any:
    try:
        return kind(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"unknown {name}") from error


@dataclass(frozen=True)
class RoutingDecision(Contract):
    """One path's already-derived gateway routing.  Plain data; nothing callable."""

    route: RouteKind
    task_id: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "route", "task_id"}

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteKind):
            raise ContractError("route must be a typed route kind")
        if self.route in _TASK_BOUND_ROUTES:
            _identifier(self.task_id, "task_id")
        elif self.task_id is not None:
            raise ContractError(f"a {self.route.value} route must not name a task")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "route": self.route.value,
                "task_id": self.task_id}

    @classmethod
    def from_document(cls, document: Any) -> "RoutingDecision":
        value = _fields(document, cls._FIELDS, "RoutingDecision", schema_version=SCHEMA_VERSION)
        return cls(route=_enum(RouteKind, value["route"], "route kind"), task_id=value["task_id"])


@dataclass(frozen=True)
class PolicyDecision(Contract):
    """One path's already-derived policy decision, in effect/authority terms.

    ``effect`` is the union effect the path derived, ``authority`` the authority
    it demands, and ``approval_refs`` the opaque identifiers of the approvals it
    would bind.  No operation, command, or argument is carried or inspected.
    """

    effect: Effect
    outcome: PolicyOutcome
    authority: Authority | None
    approval_refs: tuple[str, ...] = ()
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "effect", "outcome", "authority", "approval_refs"}

    def __post_init__(self) -> None:
        if type(self.effect) is not Effect:
            raise ContractError("policy decision effect must be an exact typed Effect")
        if not isinstance(self.outcome, PolicyOutcome):
            raise ContractError("policy outcome must be typed")
        object.__setattr__(
            self, "approval_refs",
            _sorted_identifiers(self.approval_refs, "approval_refs", MAX_APPROVAL_REFS),
        )
        if self.outcome is PolicyOutcome.UNKNOWN:
            if self.authority is not None or self.approval_refs:
                raise ContractError("an unknown policy outcome carries no authority or approvals")
            return
        if not isinstance(self.authority, Authority):
            raise ContractError("policy authority must be typed")
        human = self.authority in (Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT)
        if self.outcome is PolicyOutcome.PERMIT and human:
            raise ContractError("a human-authority decision cannot be an unapproved permit")
        if self.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            if not human:
                raise ContractError("an approval requirement needs a human authority")
            if not self.approval_refs:
                raise ContractError("an approval requirement must bind at least one approval")
        elif self.approval_refs:
            raise ContractError("only an approval requirement may bind approvals")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "effect": self.effect.to_document(),
            "outcome": self.outcome.value,
            "authority": None if self.authority is None else self.authority.value,
            "approval_refs": list(self.approval_refs),
        }

    @classmethod
    def from_document(cls, document: Any) -> "PolicyDecision":
        value = _fields(document, cls._FIELDS, "PolicyDecision", schema_version=SCHEMA_VERSION)
        raw_authority = value["authority"]
        return cls(
            effect=Effect.from_document(value["effect"]),
            outcome=_enum(PolicyOutcome, value["outcome"], "policy outcome"),
            authority=None if raw_authority is None else _enum(Authority, raw_authority, "authority"),
            approval_refs=value["approval_refs"],
        )


@dataclass(frozen=True)
class ShadowDecision(Contract):
    """Everything one path decided for one request: routing, and policy if any."""

    routing: RoutingDecision
    policy: PolicyDecision | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "routing", "policy"}

    def __post_init__(self) -> None:
        if type(self.routing) is not RoutingDecision:
            raise ContractError("routing must be a typed RoutingDecision")
        if self.policy is not None and type(self.policy) is not PolicyDecision:
            raise ContractError("policy must be a typed PolicyDecision or absent")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "routing": self.routing.to_document(),
            "policy": None if self.policy is None else self.policy.to_document(),
        }

    @classmethod
    def from_document(cls, document: Any) -> "ShadowDecision":
        value = _fields(document, cls._FIELDS, "ShadowDecision", schema_version=SCHEMA_VERSION)
        policy = value["policy"]
        return cls(
            routing=RoutingDecision.from_document(value["routing"]),
            policy=None if policy is None else PolicyDecision.from_document(policy),
        )


def effect_flags(effect: Effect) -> frozenset[str]:
    return frozenset(name for name in EFFECT_FLAG_NAMES if getattr(effect, name) is True)


def required_authority(effect: Effect) -> Authority:
    """The least authority an effect may proceed under, from its flags alone."""
    flags = effect_flags(effect)
    if "tenant" in flags:
        return Authority.ALWAYS_APPROVE_TENANT
    if flags - {"owned_component"}:
        return Authority.EXACT_HUMAN
    if flags:
        return Authority.STANDING_CONSENT
    return Authority.AGENT


def classify_divergence(
    old: ShadowDecision, new: ShadowDecision,
) -> tuple[DivergenceClass, tuple[str, ...]]:
    """Deterministically classify how the new path diverges from the old one.

    Precedence is malformed/unknown, then more-permissive-new, then routing
    mismatch, safer-new, and finally equivalent.  The reasons are deterministic
    and name only routes, effect classes, outcomes, authorities, and opaque
    approval identifiers.

    A new path that denies is never more permissive on that account: a
    suppressed request the new path routes but denies, or a policy-free request
    it denies, stays a routing mismatch or safer-new.
    """
    if type(old) is not ShadowDecision or type(new) is not ShadowDecision:
        raise ContractError("classification requires typed ShadowDecision contracts")
    malformed: list[str] = []
    for label, side in (("old", old), ("new", new)):
        if side.routing.route is RouteKind.UNKNOWN:
            malformed.append(f"{label} path routing is unknown")
        if side.policy is not None and side.policy.outcome is PolicyOutcome.UNKNOWN:
            malformed.append(f"{label} path policy outcome is unknown")
    routes_equal = old.routing == new.routing
    if routes_equal and (old.policy is None) != (new.policy is None):
        malformed.append("only one path derived a policy decision for the same route")
    if malformed:
        return DivergenceClass.MALFORMED_UNKNOWN, tuple(malformed)

    unsafe: list[str] = []
    stricter: list[str] = []
    fresh, prior = new.policy, old.policy
    new_denies = fresh is not None and fresh.outcome is PolicyOutcome.DENY
    if (
        old.routing.route in _SUPPRESSING_ROUTES
        and new.routing.route in _EXECUTABLE_ROUTES and not new_denies
    ):
        unsafe.append(
            f"new path routes {new.routing.route.value} without denying a request the "
            f"old path suppressed as {old.routing.route.value}"
        )
    if prior is None and fresh is not None and not new_denies:
        unsafe.append(
            f"new path derives a {fresh.outcome.value} policy decision where the old "
            "path derived none"
        )
    if (
        prior is not None and fresh is None
        and new.routing.route in _EXECUTABLE_ROUTES
    ):
        unsafe.append(
            f"new executable route {new.routing.route.value} drops the policy decision "
            "the old path derived"
        )
    if fresh is not None and fresh.outcome is not PolicyOutcome.DENY:
        floor = required_authority(fresh.effect)
        if _AUTHORITY_RANK[fresh.authority] < _AUTHORITY_RANK[floor]:
            unsafe.append(
                f"new path authority {fresh.authority.value} is below the "
                f"{floor.value} its own effect requires"
            )
    if fresh is not None and prior is not None:
        old_flags, new_flags = effect_flags(prior.effect), effect_flags(fresh.effect)
        if old_flags - new_flags:
            unsafe.append("new path drops effect classes: " + ", ".join(sorted(old_flags - new_flags)))
        if new_flags - old_flags:
            stricter.append("new path adds effect classes: " + ", ".join(sorted(new_flags - old_flags)))
        for name, old_rank, new_rank, old_value, new_value in (
            ("outcome", _OUTCOME_RANK[prior.outcome], _OUTCOME_RANK[fresh.outcome],
             prior.outcome.value, fresh.outcome.value),
            ("authority", _AUTHORITY_RANK[prior.authority], _AUTHORITY_RANK[fresh.authority],
             prior.authority.value, fresh.authority.value),
        ):
            if new_rank < old_rank:
                unsafe.append(f"new path {name} {new_value} is more permissive than {old_value}")
            elif new_rank > old_rank:
                stricter.append(f"new path {name} {new_value} is stricter than {old_value}")
        if (
            prior.outcome is PolicyOutcome.REQUIRE_APPROVAL
            and fresh.outcome is PolicyOutcome.REQUIRE_APPROVAL
        ):
            missing = sorted(set(prior.approval_refs) - set(fresh.approval_refs))
            extra = sorted(set(fresh.approval_refs) - set(prior.approval_refs))
            if missing:
                # A different approval never substitutes for a dropped one.
                unsafe.append(
                    "new path omits approvals the old path binds: " + ", ".join(missing)
                    + ("" if not extra else
                       "; its new-only approvals do not substitute: " + ", ".join(extra))
                )
            elif extra:
                stricter.append("new path binds additional approvals: " + ", ".join(extra))
    if unsafe:
        return DivergenceClass.MORE_PERMISSIVE_NEW, tuple(unsafe)
    if not routes_equal:
        return DivergenceClass.ROUTING_MISMATCH, (
            f"old path routes {old.routing.route.value}, new path routes "
            f"{new.routing.route.value}"
            + ("" if old.routing.route is not new.routing.route else " to a different task"),
        )
    if stricter:
        return DivergenceClass.SAFER_NEW, tuple(stricter)
    return DivergenceClass.EQUIVALENT, ()


@dataclass(frozen=True)
class ShadowComparison(Contract):
    """The caller's complete, hash-bound input for one shadow comparison."""

    origin: ComparisonOrigin
    operator_id: str | None
    task_id: str | None
    request_id: str
    parent_hash: str
    policy_revision: str
    config_revision: str
    evidence_revision: str
    observed_at: datetime
    source_instance: str
    nonce: str
    old: ShadowDecision
    new: ShadowDecision
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "origin", "operator_id", "task_id", "request_id", "parent_hash",
               "policy_revision", "config_revision", "evidence_revision", "observed_at",
               "source_instance", "nonce", "old", "new", "old_hash", "new_hash", "machine_id"}

    def __post_init__(self) -> None:
        if not isinstance(self.origin, ComparisonOrigin):
            raise ContractError("comparison origin must be typed")
        if self.origin is ComparisonOrigin.OPERATOR:
            _identifier(self.operator_id, "operator_id")
        elif self.operator_id is not None:
            raise ContractError("a detector comparison must not claim an operator identity")
        if self.task_id is not None:
            _identifier(self.task_id, "task_id")
        _hash_text(self.parent_hash, "parent_hash")
        for name in ("request_id", "policy_revision", "config_revision",
                     "evidence_revision", "source_instance", "nonce"):
            _identifier(getattr(self, name), name)
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if type(self.old) is not ShadowDecision or type(self.new) is not ShadowDecision:
            raise ContractError("old and new must be typed ShadowDecision contracts")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"shadow comparisons are restricted to machine {MACHINE_ID}")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "origin": self.origin.value,
            "operator_id": self.operator_id, "task_id": self.task_id,
            "request_id": self.request_id, "parent_hash": self.parent_hash,
            "policy_revision": self.policy_revision,
            "config_revision": self.config_revision,
            "evidence_revision": self.evidence_revision,
            "observed_at": utc_text(self.observed_at),
            "source_instance": self.source_instance, "nonce": self.nonce,
            "old": self.old.to_document(), "new": self.new.to_document(),
            "old_hash": self.old.content_hash, "new_hash": self.new.content_hash,
            "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "ShadowComparison":
        value = _fields(document, cls._FIELDS, "ShadowComparison", schema_version=SCHEMA_VERSION)
        result = cls(
            origin=_enum(ComparisonOrigin, value["origin"], "comparison origin"),
            operator_id=value["operator_id"], task_id=value["task_id"],
            request_id=value["request_id"], parent_hash=value["parent_hash"],
            policy_revision=value["policy_revision"],
            config_revision=value["config_revision"],
            evidence_revision=value["evidence_revision"],
            observed_at=parse_utc(value["observed_at"], "observed_at"),
            source_instance=value["source_instance"], nonce=value["nonce"],
            old=ShadowDecision.from_document(value["old"]),
            new=ShadowDecision.from_document(value["new"]),
            machine_id=value["machine_id"],
        )
        if value["old_hash"] != result.old.content_hash or value["new_hash"] != result.new.content_hash:
            raise ContractError("decision hashes do not match the decisions")
        return result


@dataclass(frozen=True)
class CaptureExpectation(Contract):
    """One authenticated parent input that must yield exactly one comparison.

    ``request_id`` is the parent identity.  Detector batches with several
    correlation domains still use one request ID and therefore can contribute
    at most one canary sample.
    """

    origin: ComparisonOrigin
    request_id: str
    parent_hash: str
    source_instance: str
    policy_revision: str
    config_revision: str
    evidence_revision: str
    observed_at: datetime
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "origin", "request_id", "parent_hash", "source_instance",
        "policy_revision", "config_revision", "evidence_revision",
        "observed_at", "machine_id",
    }

    def __post_init__(self) -> None:
        if type(self.origin) is not ComparisonOrigin:
            raise ContractError("capture origin must be typed")
        _hash_text(self.parent_hash, "parent_hash")
        for name in (
            "request_id", "source_instance", "policy_revision",
            "config_revision", "evidence_revision",
        ):
            _identifier(getattr(self, name), name)
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"capture expectations are restricted to machine {MACHINE_ID}")

    @classmethod
    def from_comparison(cls, comparison: ShadowComparison) -> "CaptureExpectation":
        if type(comparison) is not ShadowComparison:
            raise ContractError("capture expectation requires a typed comparison")
        return cls(
            origin=comparison.origin,
            request_id=comparison.request_id,
            parent_hash=comparison.parent_hash,
            source_instance=comparison.source_instance,
            policy_revision=comparison.policy_revision,
            config_revision=comparison.config_revision,
            evidence_revision=comparison.evidence_revision,
            observed_at=comparison.observed_at,
            machine_id=comparison.machine_id,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "origin": self.origin.value,
            "request_id": self.request_id,
            "parent_hash": self.parent_hash,
            "source_instance": self.source_instance,
            "policy_revision": self.policy_revision,
            "config_revision": self.config_revision,
            "evidence_revision": self.evidence_revision,
            "observed_at": utc_text(self.observed_at),
            "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "CaptureExpectation":
        value = _fields(
            document, cls._FIELDS, "CaptureExpectation", schema_version=SCHEMA_VERSION
        )
        return cls(
            origin=_enum(ComparisonOrigin, value["origin"], "capture origin"),
            request_id=value["request_id"],
            parent_hash=value["parent_hash"],
            source_instance=value["source_instance"],
            policy_revision=value["policy_revision"],
            config_revision=value["config_revision"],
            evidence_revision=value["evidence_revision"],
            observed_at=parse_utc(value["observed_at"], "observed_at"),
            machine_id=value["machine_id"],
        )


@dataclass(frozen=True)
class CaptureResult(Contract):
    """Immutable terminal result for one expected parent capture."""

    source_instance: str
    request_id: str
    expectation_hash: str
    outcome: CaptureOutcome
    recorded_at: datetime
    comparison_hash: str | None = None
    failure_code: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "source_instance", "request_id", "expectation_hash",
        "outcome", "recorded_at", "comparison_hash", "failure_code",
        "execution_authority",
    }
    execution_authority = False

    def __post_init__(self) -> None:
        _identifier(self.source_instance, "source_instance")
        _identifier(self.request_id, "request_id")
        _hash_text(self.expectation_hash, "expectation_hash")
        if type(self.outcome) is not CaptureOutcome:
            raise ContractError("capture outcome must be typed")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if self.outcome is CaptureOutcome.CAPTURED:
            _hash_text(self.comparison_hash, "comparison_hash")
            if self.failure_code is not None:
                raise ContractError("a captured result cannot carry a failure code")
        else:
            _identifier(self.failure_code, "failure_code")
            if self.comparison_hash is not None:
                raise ContractError("a failed result cannot bind a comparison")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_instance": self.source_instance,
            "request_id": self.request_id,
            "expectation_hash": self.expectation_hash,
            "outcome": self.outcome.value,
            "recorded_at": utc_text(self.recorded_at),
            "comparison_hash": self.comparison_hash,
            "failure_code": self.failure_code,
            "execution_authority": False,
        }

    @classmethod
    def from_document(cls, document: Any) -> "CaptureResult":
        value = _fields(
            document, cls._FIELDS, "CaptureResult", schema_version=SCHEMA_VERSION
        )
        if value["execution_authority"] is not False:
            raise ContractError("capture accounting never carries execution authority")
        return cls(
            source_instance=value["source_instance"],
            request_id=value["request_id"],
            expectation_hash=value["expectation_hash"],
            outcome=_enum(CaptureOutcome, value["outcome"], "capture outcome"),
            recorded_at=parse_utc(value["recorded_at"], "recorded_at"),
            comparison_hash=value["comparison_hash"],
            failure_code=value["failure_code"],
        )


@dataclass(frozen=True)
class CaptureEvent(Contract):
    """One hash-chained append to the capture-accounting history."""

    sequence: int
    kind: CaptureEventKind
    source_instance: str
    request_id: str
    payload_hash: str
    recorded_at: datetime
    prior_event_hash: str | None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "sequence", "kind", "source_instance", "request_id",
        "payload_hash", "recorded_at", "prior_event_hash", "execution_authority",
    }
    execution_authority = False

    def __post_init__(self) -> None:
        _positive(self.sequence, "capture event sequence")
        if type(self.kind) is not CaptureEventKind:
            raise ContractError("capture event kind must be typed")
        _identifier(self.source_instance, "source_instance")
        _identifier(self.request_id, "request_id")
        _hash_text(self.payload_hash, "payload_hash")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if self.prior_event_hash is not None:
            _hash_text(self.prior_event_hash, "prior_event_hash")
        if (self.sequence == 1) != (self.prior_event_hash is None):
            raise ContractError("only the first capture event may lack a prior hash")

    @property
    def event_hash(self) -> str:
        return self.content_hash

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "source_instance": self.source_instance,
            "request_id": self.request_id,
            "payload_hash": self.payload_hash,
            "recorded_at": utc_text(self.recorded_at),
            "prior_event_hash": self.prior_event_hash,
            "execution_authority": False,
        }

    @classmethod
    def from_document(cls, document: Any) -> "CaptureEvent":
        value = _fields(document, cls._FIELDS, "CaptureEvent", schema_version=SCHEMA_VERSION)
        if value["execution_authority"] is not False:
            raise ContractError("capture events never carry execution authority")
        return cls(
            sequence=value["sequence"],
            kind=_enum(CaptureEventKind, value["kind"], "capture event kind"),
            source_instance=value["source_instance"],
            request_id=value["request_id"],
            payload_hash=value["payload_hash"],
            recorded_at=parse_utc(value["recorded_at"], "recorded_at"),
            prior_event_hash=value["prior_event_hash"],
        )


def derive_coverage(comparison: ShadowComparison) -> frozenset[CoverageClass]:
    """Coverage exercised by a usable new decision, never by names or prose.

    Approval references describe the decision's requirement, not proof that a
    human approved execution. Unsafe/unknown comparisons earn no coverage.
    Handover specifically witnesses an existing detector task moving to the
    explicit new handover route with the same task binding and a mutation policy.
    """
    if type(comparison) is not ShadowComparison:
        raise ContractError("coverage requires a typed ShadowComparison")
    new = comparison.new
    policy = new.policy
    if (classify_divergence(comparison.old, new)[0] in BLOCKING_CLASSES
            or new.routing.route not in _EXECUTABLE_ROUTES or policy is None
            or policy.outcome not in {PolicyOutcome.PERMIT, PolicyOutcome.REQUIRE_APPROVAL}):
        return frozenset()
    flags = effect_flags(policy.effect)
    if _AUTHORITY_RANK[policy.authority] < _AUTHORITY_RANK[required_authority(policy.effect)]:
        return frozenset()
    coverage: set[CoverageClass] = set()
    if (comparison.origin is ComparisonOrigin.OPERATOR and not flags
            and policy.outcome is PolicyOutcome.PERMIT and policy.authority is Authority.AGENT):
        coverage.add(CoverageClass.OPERATOR_READONLY)
    if flags == {"owned_component"}:
        if policy.outcome is PolicyOutcome.REQUIRE_APPROVAL and policy.authority is Authority.EXACT_HUMAN:
            coverage.add(CoverageClass.OWNED_COMPONENT_MUTATION)
        if policy.outcome is PolicyOutcome.PERMIT and policy.authority is Authority.STANDING_CONSENT:
            coverage.add(CoverageClass.OWNED_COMPONENT_STANDING_CONSENT)
    if policy.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        for flag, kind in (
            ("host", CoverageClass.HOST_EFFECT),
            ("tenant", CoverageClass.TENANT_EFFECT),
            ("external_commitment", CoverageClass.EXTERNAL_COMMITMENT),
        ):
            if flag in flags:
                coverage.add(kind)
    if comparison.origin is ComparisonOrigin.DETECTOR:
        coverage.add(CoverageClass.DETECTOR_ROUTING)
        if (flags and new.routing.route is RouteKind.LEGACY_HANDOVER
                and comparison.old.routing.route is RouteKind.EXISTING_TASK
                and comparison.old.routing.task_id == new.routing.task_id):
            coverage.add(CoverageClass.LEGACY_HANDOVER)
    return frozenset(coverage)


def _coverage_counts(value: Any, name: str) -> Mapping[CoverageClass, int]:
    if (not isinstance(value, Mapping) or set(value) != set(COVERAGE_ORDER)
            or any(type(key) is not CoverageClass for key in value)):
        raise ContractError(f"{name} must cover every typed coverage class exactly")
    return MappingProxyType({
        kind: _positive(value[kind], name, minimum=0) for kind in COVERAGE_ORDER
    })


def _parse_coverage_counts(value: Any, name: str) -> Mapping[CoverageClass, int]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return {_enum(CoverageClass, key, "coverage class"): count for key, count in value.items()}


@dataclass(frozen=True)
class ShadowRecord(Contract):
    """One durable, hash-chained comparison with its derived divergence class."""

    sequence: int
    comparison: ShadowComparison
    divergence: DivergenceClass
    reasons: tuple[str, ...]
    recorded_at: datetime
    prior_record_hash: str | None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    # A record is evidence about two decisions and nothing more.
    execution_authority = False

    _FIELDS = {"schema_version", "sequence", "comparison", "comparison_hash", "divergence",
               "reasons", "recorded_at", "prior_record_hash", "execution_authority"}

    def __post_init__(self) -> None:
        _positive(self.sequence, "sequence")
        if type(self.comparison) is not ShadowComparison:
            raise ContractError("record comparison must be typed")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if self.prior_record_hash is not None:
            _hash_text(self.prior_record_hash, "prior_record_hash")
        if (self.sequence == 1) != (self.prior_record_hash is None):
            raise ContractError("only the first record may lack a prior record hash")
        if not isinstance(self.reasons, (list, tuple)):
            raise ContractError("reasons must be an array")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        # The class is derived, never asserted: a stored or presented class that
        # differs from the deterministic classification is a forgery.
        derived = classify_divergence(self.comparison.old, self.comparison.new)
        if (self.divergence, self.reasons) != derived:
            raise ContractError("divergence does not match the deterministic classification")

    @property
    def record_hash(self) -> str:
        return self.content_hash

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "sequence": self.sequence,
            "comparison": self.comparison.to_document(),
            "comparison_hash": self.comparison.content_hash,
            "divergence": self.divergence.value, "reasons": list(self.reasons),
            "recorded_at": utc_text(self.recorded_at),
            "prior_record_hash": self.prior_record_hash, "execution_authority": False,
        }

    @classmethod
    def from_document(cls, document: Any) -> "ShadowRecord":
        value = _fields(document, cls._FIELDS, "ShadowRecord", schema_version=SCHEMA_VERSION)
        if value["execution_authority"] is not False:
            raise ContractError("a shadow record can never carry execution authority")
        result = cls(
            sequence=value["sequence"],
            comparison=ShadowComparison.from_document(value["comparison"]),
            divergence=_enum(DivergenceClass, value["divergence"], "divergence class"),
            reasons=value["reasons"],
            recorded_at=parse_utc(value["recorded_at"], "recorded_at"),
            prior_record_hash=value["prior_record_hash"],
        )
        if value["comparison_hash"] != result.comparison.content_hash:
            raise ContractError("comparison hash does not match the comparison")
        return result


@dataclass(frozen=True)
class CanaryCriteria(Contract):
    """Explicit acceptance criteria for one canary window.  Nothing defaults."""

    capability: RolloutCapability
    coverage_minimums: Mapping[CoverageClass, int]
    policy_revision: str
    config_revision: str
    evidence_revision: str
    source_instances: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    minimum_samples: int
    minimum_samples_per_source: int
    minimum_span_seconds: int
    max_evidence_age_seconds: int
    max_safer_new: int
    max_routing_mismatch: int
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "capability", "coverage_minimums",
               "policy_revision", "config_revision", "evidence_revision",
               "source_instances", "window_start", "window_end", "minimum_samples",
               "minimum_samples_per_source", "minimum_span_seconds",
               "max_evidence_age_seconds", "max_safer_new", "max_routing_mismatch",
               "machine_id"}

    def __post_init__(self) -> None:
        if type(self.capability) is not RolloutCapability:
            raise ContractError("criteria capability must be typed")
        minimums = _coverage_counts(self.coverage_minimums, "coverage_minimums")
        for kind, count in minimums.items():
            if count > MAX_LEDGER_RECORDS:
                raise ContractError("coverage minimum exceeds the ledger bound")
            if kind in CAPABILITY_COVERAGE[self.capability] and count < MINIMUM_SAMPLE_FLOOR:
                raise ContractError(f"{kind.value} coverage minimum must be at least {MINIMUM_SAMPLE_FLOOR}")
        object.__setattr__(self, "coverage_minimums", minimums)
        for name in ("policy_revision", "config_revision", "evidence_revision"):
            _identifier(getattr(self, name), name)
        sources = _sorted_identifiers(self.source_instances, "source_instances", MAX_SOURCE_INSTANCES)
        if not sources:
            raise ContractError("criteria must name at least one source instance")
        object.__setattr__(self, "source_instances", sources)
        object.__setattr__(self, "window_start", _utc(self.window_start, "window_start"))
        object.__setattr__(self, "window_end", _utc(self.window_end, "window_end"))
        if not MIN_OBSERVATION_WINDOW <= self.window <= MAX_OBSERVATION_WINDOW:
            raise ContractError(
                f"observation window must be between {MIN_OBSERVATION_WINDOW} "
                f"and {MAX_OBSERVATION_WINDOW}"
            )
        _positive(self.minimum_samples, "minimum_samples", minimum=MINIMUM_SAMPLE_FLOOR)
        if self.minimum_samples > MAX_LEDGER_RECORDS:
            raise ContractError("minimum_samples exceeds the ledger bound")
        # Every named source instance must contribute; one silent instance
        # cannot hide behind the others' samples.
        _positive(self.minimum_samples_per_source, "minimum_samples_per_source")
        if self.minimum_samples_per_source * len(sources) > MAX_LEDGER_RECORDS:
            raise ContractError("minimum_samples_per_source exceeds the ledger bound")
        _positive(self.minimum_span_seconds, "minimum_span_seconds")
        if self.minimum_span_seconds > int(self.window.total_seconds()):
            raise ContractError("minimum_span_seconds exceeds the observation window")
        _positive(self.max_evidence_age_seconds, "max_evidence_age_seconds")
        if self.max_evidence_age_seconds > int(MAX_EVIDENCE_AGE_LIMIT.total_seconds()):
            raise ContractError(f"max_evidence_age_seconds exceeds {MAX_EVIDENCE_AGE_LIMIT}")
        for name in ("max_safer_new", "max_routing_mismatch"):
            _positive(getattr(self, name), name, minimum=0)
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"canary criteria are restricted to machine {MACHINE_ID}")

    @property
    def window(self) -> timedelta:
        return self.window_end - self.window_start

    def contains(self, moment: datetime) -> bool:
        """Membership in the half-open observation window ``[start, end)``."""
        return self.window_start <= moment < self.window_end

    def threshold(self, divergence: DivergenceClass) -> int:
        return {
            DivergenceClass.SAFER_NEW: self.max_safer_new,
            DivergenceClass.ROUTING_MISMATCH: self.max_routing_mismatch,
        }[divergence]

    def to_document(self) -> dict[str, Any]:
        document = {name: getattr(self, name) for name in self._FIELDS}
        document["capability"] = self.capability.value
        document["coverage_minimums"] = {kind.value: self.coverage_minimums[kind] for kind in COVERAGE_ORDER}
        document["source_instances"] = list(self.source_instances)
        document["window_start"] = utc_text(self.window_start)
        document["window_end"] = utc_text(self.window_end)
        return document

    @classmethod
    def from_document(cls, document: Any) -> "CanaryCriteria":
        value = dict(_fields(document, cls._FIELDS, "CanaryCriteria", schema_version=SCHEMA_VERSION))
        del value["schema_version"]
        value["capability"] = _enum(RolloutCapability, value["capability"], "capability")
        value["coverage_minimums"] = _parse_coverage_counts(value["coverage_minimums"], "coverage_minimums")
        value["window_start"] = parse_utc(value["window_start"], "window_start")
        value["window_end"] = parse_utc(value["window_end"], "window_end")
        return cls(**value)


@dataclass(frozen=True)
class CanarySummary(Contract):
    """Bounded, deterministic canary evidence over one closed observation window.

    The document is a pure function of the criteria and the verified in-window
    records, so its hash is stable across reads and restarts.  ``evidence_root``
    folds every counted record hash in ledger order, binding the exact evidence.
    A sample is counted only when both its observation and its record time lie
    inside the closed window; ``source_counts`` names every criteria source
    instance, including one that contributed nothing.
    """

    criteria: CanaryCriteria
    counts: Mapping[DivergenceClass, int]
    coverage_counts: Mapping[CoverageClass, int]
    source_counts: tuple[tuple[str, int], ...]
    expected_captures: int
    captured_captures: int
    failed_captures: int
    pending_captures: int
    unaccounted_samples: int
    cross_revision_samples: int
    foreign_source_samples: int
    out_of_window_observations: int
    first_sequence: int | None
    last_sequence: int | None
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    evidence_root: str
    schema_version: int = field(default=SCHEMA_VERSION, init=False)
    # Derived from the counts and criteria, never asserted by a caller.
    reasons: tuple[str, ...] = field(default=(), init=False)

    execution_authority = False

    _FIELDS = {"schema_version", "machine_id", "capability", "coverage_counts",
               "criteria", "criteria_hash", "sample_count",
               "counts", "source_counts", "expected_captures", "captured_captures",
               "failed_captures", "pending_captures", "unaccounted_samples",
               "cross_revision_samples", "foreign_source_samples",
               "out_of_window_observations", "first_sequence", "last_sequence",
               "first_recorded_at", "last_recorded_at", "first_observed_at",
               "last_observed_at", "evidence_root", "accepted", "reasons",
               "execution_authority"}

    def __post_init__(self) -> None:
        if type(self.criteria) is not CanaryCriteria:
            raise ContractError("summary criteria must be typed")
        if not isinstance(self.counts, Mapping) or set(self.counts) != set(DIVERGENCE_ORDER):
            raise ContractError("summary counts must cover every divergence class exactly")
        for value in self.counts.values():
            _positive(value, "count", minimum=0)
        object.__setattr__(self, "counts", MappingProxyType(
            {item: self.counts[item] for item in DIVERGENCE_ORDER}))
        coverage = _coverage_counts(self.coverage_counts, "coverage_counts")
        if any(count > self.sample_count for count in coverage.values()):
            raise ContractError("coverage count exceeds the sample count")
        object.__setattr__(self, "coverage_counts", coverage)
        sources = tuple((_identifier(name, "source"), _positive(count, "source count", minimum=0))
                        for name, count in self.source_counts)
        # Exactly the criteria sources, in order, so a silent instance is
        # reported as zero rather than omitted.
        if tuple(name for name, _ in sources) != self.criteria.source_instances:
            raise ContractError("source counts must cover every criteria source exactly")
        if sum(count for _, count in sources) != self.sample_count:
            raise ContractError("source counts do not add up to the sample count")
        object.__setattr__(self, "source_counts", sources)
        for name in (
            "expected_captures", "captured_captures", "failed_captures",
            "pending_captures", "unaccounted_samples",
        ):
            _positive(getattr(self, name), name, minimum=0)
        if self.captured_captures + self.failed_captures + self.pending_captures != self.expected_captures:
            raise ContractError("capture outcome counts do not add up to expected captures")
        _positive(self.cross_revision_samples, "cross_revision_samples", minimum=0)
        _positive(self.foreign_source_samples, "foreign_source_samples", minimum=0)
        _positive(self.out_of_window_observations, "out_of_window_observations", minimum=0)
        times = ("first_recorded_at", "last_recorded_at", "first_observed_at", "last_observed_at")
        bounds = (self.first_sequence, self.last_sequence, *(getattr(self, name) for name in times))
        if self.sample_count == 0:
            if any(item is not None for item in bounds):
                raise ContractError("an empty summary has no evidence bounds")
        else:
            _positive(self.first_sequence, "first_sequence")
            _positive(self.last_sequence, "last_sequence")
            for name in times:
                object.__setattr__(self, name, _utc(getattr(self, name), name))
                if not self.criteria.contains(getattr(self, name)):
                    raise ContractError(f"summary {name} is outside the observation window")
            if (self.last_sequence - self.first_sequence + 1 < self.sample_count
                    or self.last_recorded_at < self.first_recorded_at
                    or self.last_observed_at < self.first_observed_at):
                raise ContractError("summary evidence bounds are inconsistent")
        _hash_text(self.evidence_root, "evidence_root")
        object.__setattr__(self, "reasons", self._derive_reasons())

    @property
    def capability(self) -> RolloutCapability:
        return self.criteria.capability

    @property
    def sample_count(self) -> int:
        return sum(self.counts.values())

    @property
    def accepted(self) -> bool:
        return not self.reasons

    def _derive_reasons(self) -> tuple[str, ...]:
        criteria, reasons = self.criteria, []
        if self.failed_captures:
            reasons.append(f"{self.failed_captures} expected captures failed")
        if self.pending_captures:
            reasons.append(f"{self.pending_captures} expected captures remain unresolved")
        if self.unaccounted_samples:
            reasons.append(
                f"{self.unaccounted_samples} comparisons lack exact capture accounting"
            )
        if self.captured_captures != self.sample_count:
            reasons.append(
                f"{self.captured_captures} completed captures produced "
                f"{self.sample_count} in-window samples"
            )
        if self.cross_revision_samples:
            reasons.append(
                f"{self.cross_revision_samples} comparisons under other revisions were "
                "recorded inside the observation window"
            )
        if self.foreign_source_samples:
            reasons.append(
                f"{self.foreign_source_samples} comparisons from unexpected source "
                "instances were recorded inside the observation window"
            )
        if self.out_of_window_observations:
            reasons.append(
                f"{self.out_of_window_observations} comparisons recorded inside the "
                "observation window were observed outside it"
            )
        if self.sample_count < criteria.minimum_samples:
            reasons.append(
                f"{self.sample_count} samples is below the required minimum of "
                f"{criteria.minimum_samples}"
            )
        for name, count in self.source_counts:
            if count < criteria.minimum_samples_per_source:
                reasons.append(
                    f"source instance {name} contributed {count} samples, below the "
                    f"required per-source minimum of {criteria.minimum_samples_per_source}"
                )
        # Both clocks must support the span: neither a burst of observations
        # recorded slowly nor spread observations recorded in a burst counts.
        for label, first, last in (
            ("recorded", self.first_recorded_at, self.last_recorded_at),
            ("observed", self.first_observed_at, self.last_observed_at),
        ):
            span = timedelta(0) if self.sample_count == 0 else last - first
            if span < timedelta(seconds=criteria.minimum_span_seconds):
                reasons.append(
                    f"{label} sample times do not span the required "
                    f"{criteria.minimum_span_seconds} seconds of the observation window"
                )
        for divergence in DIVERGENCE_ORDER:
            count = self.counts[divergence]
            if divergence in BLOCKING_CLASSES and count:
                reasons.append(f"{count} {divergence.value} comparisons; none are tolerated")
            elif divergence in BENIGN_CLASSES and count > criteria.threshold(divergence):
                reasons.append(
                    f"{count} {divergence.value} comparisons exceed the threshold of "
                    f"{criteria.threshold(divergence)}"
                )
        for kind in COVERAGE_ORDER:
            count, minimum = self.coverage_counts[kind], criteria.coverage_minimums[kind]
            if count < minimum:
                reasons.append(
                    f"{kind.value} coverage has {count} samples, "
                    f"below the required minimum of {minimum}")
        return tuple(reasons)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "machine_id": self.criteria.machine_id,
            "capability": self.capability.value,
            "coverage_counts": {kind.value: self.coverage_counts[kind] for kind in COVERAGE_ORDER},
            "criteria": self.criteria.to_document(),
            "criteria_hash": self.criteria.content_hash, "sample_count": self.sample_count,
            "counts": {item.value: self.counts[item] for item in DIVERGENCE_ORDER},
            "source_counts": [
                {"source_instance": name, "samples": count} for name, count in self.source_counts
            ],
            "expected_captures": self.expected_captures,
            "captured_captures": self.captured_captures,
            "failed_captures": self.failed_captures,
            "pending_captures": self.pending_captures,
            "unaccounted_samples": self.unaccounted_samples,
            "cross_revision_samples": self.cross_revision_samples,
            "foreign_source_samples": self.foreign_source_samples,
            "out_of_window_observations": self.out_of_window_observations,
            "first_sequence": self.first_sequence, "last_sequence": self.last_sequence,
            **{name: None if getattr(self, name) is None else utc_text(getattr(self, name))
               for name in ("first_recorded_at", "last_recorded_at",
                            "first_observed_at", "last_observed_at")},
            "evidence_root": self.evidence_root, "accepted": self.accepted,
            "reasons": list(self.reasons), "execution_authority": False,
        }

    @classmethod
    def from_document(cls, document: Any) -> "CanarySummary":
        value = _fields(document, cls._FIELDS, "CanarySummary", schema_version=SCHEMA_VERSION)
        if value["execution_authority"] is not False:
            raise ContractError("a canary summary can never carry execution authority")
        counts, sources = value["counts"], value["source_counts"]
        if not isinstance(counts, Mapping) or not isinstance(sources, (list, tuple)):
            raise ContractError("summary counts are malformed")
        if not isinstance(value["reasons"], (list, tuple)):
            raise ContractError("summary reasons must be an array")
        for item in sources:
            if not isinstance(item, Mapping) or set(item) != {"source_instance", "samples"}:
                raise ContractError("summary source counts are malformed")
        times = {
            name: None if value[name] is None else parse_utc(value[name], name)
            for name in ("first_recorded_at", "last_recorded_at",
                         "first_observed_at", "last_observed_at")
        }
        result = cls(
            criteria=CanaryCriteria.from_document(value["criteria"]),
            coverage_counts=_parse_coverage_counts(value["coverage_counts"], "coverage_counts"),
            counts={_enum(DivergenceClass, name, "divergence class"): count
                    for name, count in counts.items()},
            source_counts=tuple((item["source_instance"], item["samples"]) for item in sources),
            expected_captures=value["expected_captures"],
            captured_captures=value["captured_captures"],
            failed_captures=value["failed_captures"],
            pending_captures=value["pending_captures"],
            unaccounted_samples=value["unaccounted_samples"],
            cross_revision_samples=value["cross_revision_samples"],
            foreign_source_samples=value["foreign_source_samples"],
            out_of_window_observations=value["out_of_window_observations"],
            first_sequence=value["first_sequence"], last_sequence=value["last_sequence"],
            evidence_root=value["evidence_root"], **times,
        )
        if (
            tuple(value["reasons"]) != result.reasons
            or value["capability"] != result.capability.value
            or value["machine_id"] != result.criteria.machine_id
            or value["criteria_hash"] != result.criteria.content_hash
            or type(value["sample_count"]) is not int
            or value["sample_count"] != result.sample_count
            or value["accepted"] is not result.accepted
        ):
            raise ContractError("summary derived fields do not match its content")
        return result


@dataclass(frozen=True)
class ShadowStatus:
    """Read-only ledger status.  Never an authority decision."""

    record_count: int
    head_sequence: int | None
    head_hash: str | None
    last_recorded_at: datetime | None
    counts: Mapping[DivergenceClass, int]
    execution_authority: bool = field(default=False, init=False)


# Exact DDL.  Opening or using a ledger compares these (whitespace-normalized)
# with what SQLite recorded, so a missing, replaced, or weakened object fails closed.
_SCHEMA_TABLE_SQL = (
    "CREATE TABLE tc_shadow_schema ("
    "namespace TEXT PRIMARY KEY CHECK(namespace='shadow'), version INTEGER NOT NULL)"
)
_COMPARISONS_TABLE_SQL = """CREATE TABLE tc_shadow_comparisons (
                              sequence INTEGER PRIMARY KEY,
                              record_hash TEXT NOT NULL UNIQUE,
                              prior_record_hash TEXT UNIQUE,
                              nonce TEXT NOT NULL UNIQUE,
                              comparison_hash TEXT NOT NULL,
                              old_hash TEXT NOT NULL,
                              new_hash TEXT NOT NULL,
                              divergence TEXT NOT NULL,
                              machine_id TEXT NOT NULL CHECK(machine_id='17049'),
                              policy_revision TEXT NOT NULL,
                              config_revision TEXT NOT NULL,
                              evidence_revision TEXT NOT NULL,
                              source_instance TEXT NOT NULL,
                              request_id TEXT NOT NULL,
                              observed_utc TEXT NOT NULL,
                              recorded_utc TEXT NOT NULL,
                              record_json BLOB NOT NULL,
                              UNIQUE(source_instance, request_id))"""
_RECORDED_INDEX_SQL = (
    "CREATE INDEX tc_shadow_comparisons_recorded "
    "ON tc_shadow_comparisons(recorded_utc,sequence)"
)
_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_comparisons_immutable_update "
    "BEFORE UPDATE ON tc_shadow_comparisons "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow comparison'); END"
)
_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_comparisons_immutable_delete "
    "BEFORE DELETE ON tc_shadow_comparisons "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow comparison'); END"
)
_CAPTURE_EXPECTATIONS_TABLE_SQL = """CREATE TABLE tc_shadow_capture_expectations (
                              source_instance TEXT NOT NULL,
                              request_id TEXT NOT NULL,
                              expectation_hash TEXT NOT NULL UNIQUE,
                              parent_hash TEXT NOT NULL,
                              origin TEXT NOT NULL,
                              machine_id TEXT NOT NULL CHECK(machine_id='17049'),
                              policy_revision TEXT NOT NULL,
                              config_revision TEXT NOT NULL,
                              evidence_revision TEXT NOT NULL,
                              observed_utc TEXT NOT NULL,
                              expectation_json BLOB NOT NULL,
                              PRIMARY KEY(source_instance,request_id))"""
_CAPTURE_RESULTS_TABLE_SQL = """CREATE TABLE tc_shadow_capture_results (
                              source_instance TEXT NOT NULL,
                              request_id TEXT NOT NULL,
                              result_hash TEXT NOT NULL UNIQUE,
                              expectation_hash TEXT NOT NULL UNIQUE,
                              outcome TEXT NOT NULL CHECK(outcome IN ('captured','failed')),
                              comparison_hash TEXT UNIQUE,
                              result_json BLOB NOT NULL,
                              PRIMARY KEY(source_instance,request_id))"""
_CAPTURE_EVENTS_TABLE_SQL = """CREATE TABLE tc_shadow_capture_events (
                              sequence INTEGER PRIMARY KEY,
                              event_hash TEXT NOT NULL UNIQUE,
                              prior_event_hash TEXT UNIQUE,
                              kind TEXT NOT NULL CHECK(kind IN ('expectation','result')),
                              source_instance TEXT NOT NULL,
                              request_id TEXT NOT NULL,
                              payload_hash TEXT NOT NULL UNIQUE,
                              recorded_utc TEXT NOT NULL,
                              event_json BLOB NOT NULL)"""
_CAPTURE_EXPECTATIONS_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_expectations_immutable_update "
    "BEFORE UPDATE ON tc_shadow_capture_expectations "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture expectation'); END"
)
_CAPTURE_EXPECTATIONS_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_expectations_immutable_delete "
    "BEFORE DELETE ON tc_shadow_capture_expectations "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture expectation'); END"
)
_CAPTURE_RESULTS_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_results_immutable_update "
    "BEFORE UPDATE ON tc_shadow_capture_results "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture result'); END"
)
_CAPTURE_RESULTS_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_results_immutable_delete "
    "BEFORE DELETE ON tc_shadow_capture_results "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture result'); END"
)
_CAPTURE_EVENTS_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_events_immutable_update "
    "BEFORE UPDATE ON tc_shadow_capture_events "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture event'); END"
)
_CAPTURE_EVENTS_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_shadow_capture_events_immutable_delete "
    "BEFORE DELETE ON tc_shadow_capture_events "
    "BEGIN SELECT RAISE(ABORT, 'immutable shadow capture event'); END"
)
assert MACHINE_ID == "17049", "the comparisons table CHECK pins the machine literally"

_COLUMNS = (
    "sequence,record_hash,prior_record_hash,nonce,comparison_hash,old_hash,new_hash,"
    "divergence,machine_id,policy_revision,config_revision,evidence_revision,"
    "source_instance,request_id,observed_utc,recorded_utc,record_json"
)


def _normalized_sql(sql: Any) -> str | None:
    return None if not isinstance(sql, str) else " ".join(sql.split())


_EXPECTED_SCHEMA_OBJECTS: Mapping[tuple[str, str], tuple[str, str | None]] = MappingProxyType({
    ("table", "tc_shadow_schema"): ("tc_shadow_schema", _normalized_sql(_SCHEMA_TABLE_SQL)),
    ("table", "tc_shadow_comparisons"): ("tc_shadow_comparisons", _normalized_sql(_COMPARISONS_TABLE_SQL)),
    ("index", "tc_shadow_comparisons_recorded"): ("tc_shadow_comparisons", _normalized_sql(_RECORDED_INDEX_SQL)),
    ("trigger", "tc_shadow_comparisons_immutable_update"): ("tc_shadow_comparisons", _normalized_sql(_UPDATE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_comparisons_immutable_delete"): ("tc_shadow_comparisons", _normalized_sql(_DELETE_TRIGGER_SQL)),
    ("table", "tc_shadow_capture_expectations"): (
        "tc_shadow_capture_expectations", _normalized_sql(_CAPTURE_EXPECTATIONS_TABLE_SQL)),
    ("table", "tc_shadow_capture_results"): (
        "tc_shadow_capture_results", _normalized_sql(_CAPTURE_RESULTS_TABLE_SQL)),
    ("table", "tc_shadow_capture_events"): (
        "tc_shadow_capture_events", _normalized_sql(_CAPTURE_EVENTS_TABLE_SQL)),
    ("trigger", "tc_shadow_capture_expectations_immutable_update"): (
        "tc_shadow_capture_expectations", _normalized_sql(_CAPTURE_EXPECTATIONS_UPDATE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_capture_expectations_immutable_delete"): (
        "tc_shadow_capture_expectations", _normalized_sql(_CAPTURE_EXPECTATIONS_DELETE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_capture_results_immutable_update"): (
        "tc_shadow_capture_results", _normalized_sql(_CAPTURE_RESULTS_UPDATE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_capture_results_immutable_delete"): (
        "tc_shadow_capture_results", _normalized_sql(_CAPTURE_RESULTS_DELETE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_capture_events_immutable_update"): (
        "tc_shadow_capture_events", _normalized_sql(_CAPTURE_EVENTS_UPDATE_TRIGGER_SQL)),
    ("trigger", "tc_shadow_capture_events_immutable_delete"): (
        "tc_shadow_capture_events", _normalized_sql(_CAPTURE_EVENTS_DELETE_TRIGGER_SQL)),
})


def _column_values(record: ShadowRecord) -> dict[str, Any]:
    comparison = record.comparison
    return {
        "sequence": record.sequence, "record_hash": record.record_hash,
        "prior_record_hash": record.prior_record_hash, "nonce": comparison.nonce,
        "comparison_hash": comparison.content_hash,
        "old_hash": comparison.old.content_hash, "new_hash": comparison.new.content_hash,
        "divergence": record.divergence.value, "machine_id": comparison.machine_id,
        "policy_revision": comparison.policy_revision,
        "config_revision": comparison.config_revision,
        "evidence_revision": comparison.evidence_revision,
        "source_instance": comparison.source_instance,
        "request_id": comparison.request_id,
        "observed_utc": utc_text(comparison.observed_at),
        "recorded_utc": utc_text(record.recorded_at),
        "record_json": record.canonical_json(),
    }


def _expectation_values(expectation: CaptureExpectation) -> dict[str, Any]:
    return {
        "source_instance": expectation.source_instance,
        "request_id": expectation.request_id,
        "expectation_hash": expectation.content_hash,
        "parent_hash": expectation.parent_hash,
        "origin": expectation.origin.value,
        "machine_id": expectation.machine_id,
        "policy_revision": expectation.policy_revision,
        "config_revision": expectation.config_revision,
        "evidence_revision": expectation.evidence_revision,
        "observed_utc": utc_text(expectation.observed_at),
        "expectation_json": expectation.canonical_json(),
    }


def _capture_result_values(result: CaptureResult) -> dict[str, Any]:
    return {
        "source_instance": result.source_instance,
        "request_id": result.request_id,
        "result_hash": result.content_hash,
        "expectation_hash": result.expectation_hash,
        "outcome": result.outcome.value,
        "comparison_hash": result.comparison_hash,
        "result_json": result.canonical_json(),
    }


def _capture_event_values(event: CaptureEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_hash": event.event_hash,
        "prior_event_hash": event.prior_event_hash,
        "kind": event.kind.value,
        "source_instance": event.source_instance,
        "request_id": event.request_id,
        "payload_hash": event.payload_hash,
        "recorded_utc": utc_text(event.recorded_at),
        "event_json": event.canonical_json(),
    }


def _bounded_delta(value: Any, limit: timedelta, name: str) -> timedelta:
    if not isinstance(value, timedelta) or not timedelta(0) <= value <= limit:
        raise ContractError(f"{name} must be between zero and {limit}")
    return value


class ShadowLedger:
    """Durable, append-only shadow comparisons in the shared task database.

    All defaults are inert: constructing a ledger only migrates and verifies its
    own namespaced tables, and nothing in the repository constructs one.  The
    ledger takes data and a clock; it holds no reference to either decision path
    and cannot invoke one.  Every read verifies the exact schema and the whole
    hash chain and answers from verified, parsed records, never from the
    unauthenticated SQL columns.  Any failure raises :class:`ShadowError`; a
    caller must treat that as "no usable shadow evidence".
    """

    def __init__(
        self,
        tasks: TaskStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_clock_skew: timedelta = DEFAULT_MAX_CLOCK_SKEW,
        max_observation_age: timedelta = DEFAULT_MAX_OBSERVATION_AGE,
    ):
        self.tasks = tasks
        self.clock = clock
        self.max_clock_skew = _bounded_delta(max_clock_skew, MAX_CLOCK_SKEW_LIMIT, "max_clock_skew")
        self.max_observation_age = _bounded_delta(
            max_observation_age, MAX_OBSERVATION_AGE_LIMIT, "max_observation_age")
        self._migrate()

    # -- schema ---------------------------------------------------------------

    def _schema_objects(self) -> dict[tuple[str, str], tuple[str, str | None]]:
        names = sorted({name for _, name in _EXPECTED_SCHEMA_OBJECTS})
        marks = ",".join("?" for _ in names)
        rows = self.tasks.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE tbl_name IN ('tc_shadow_schema','tc_shadow_comparisons',"
            "'tc_shadow_capture_expectations','tc_shadow_capture_results',"
            "'tc_shadow_capture_events') "
            f"OR name IN ({marks})", names,
        ).fetchall()
        return {
            (row["type"], row["name"]): (row["tbl_name"], _normalized_sql(row["sql"]))
            for row in rows
            # The implicit indexes behind PRIMARY KEY/UNIQUE are bound by the table SQL.
            if not str(row["name"]).startswith("sqlite_autoindex_")
        }

    def _verify_schema(self) -> None:
        objects = self._schema_objects()
        for key, expected in _EXPECTED_SCHEMA_OBJECTS.items():
            if key not in objects:
                raise ShadowTampered(f"shadow schema {key[0]} {key[1]} is missing")
            if objects[key] != expected:
                raise ShadowTampered(f"shadow schema {key[0]} {key[1]} is malformed")
        unexpected = sorted(name for _, name in set(objects) - set(_EXPECTED_SCHEMA_OBJECTS))
        if unexpected:
            raise ShadowTampered("unexpected schema objects on shadow tables: " + ", ".join(unexpected))
        rows = self.tasks.db.execute("SELECT namespace,version FROM tc_shadow_schema").fetchall()
        if len(rows) == 1 and type(rows[0]["version"]) is int and rows[0]["version"] > SHADOW_SCHEMA_VERSION:
            raise ShadowError(
                f"shadow schema {rows[0]['version']} is newer than supported schema "
                f"{SHADOW_SCHEMA_VERSION}"
            )
        if (
            len(rows) != 1 or rows[0]["namespace"] != "shadow"
            or type(rows[0]["version"]) is not int or rows[0]["version"] != SHADOW_SCHEMA_VERSION
        ):
            raise ShadowTampered("shadow schema version record is missing or malformed")

    def _migrate(self) -> None:
        try:
            with self.tasks.transaction():
                # Only a database with no shadow object at all is fresh.  Anything
                # partial is never repaired or trusted; it fails closed below.
                if not self._schema_objects():
                    for statement in (
                        _SCHEMA_TABLE_SQL, _COMPARISONS_TABLE_SQL, _RECORDED_INDEX_SQL,
                        _UPDATE_TRIGGER_SQL, _DELETE_TRIGGER_SQL,
                        _CAPTURE_EXPECTATIONS_TABLE_SQL, _CAPTURE_RESULTS_TABLE_SQL,
                        _CAPTURE_EVENTS_TABLE_SQL,
                        _CAPTURE_EXPECTATIONS_UPDATE_TRIGGER_SQL,
                        _CAPTURE_EXPECTATIONS_DELETE_TRIGGER_SQL,
                        _CAPTURE_RESULTS_UPDATE_TRIGGER_SQL,
                        _CAPTURE_RESULTS_DELETE_TRIGGER_SQL,
                        _CAPTURE_EVENTS_UPDATE_TRIGGER_SQL,
                        _CAPTURE_EVENTS_DELETE_TRIGGER_SQL,
                    ):
                        self.tasks.db.execute(statement)
                    self.tasks.db.execute(
                        "INSERT INTO tc_shadow_schema(namespace,version) VALUES('shadow',?)",
                        (SHADOW_SCHEMA_VERSION,),
                    )
                records = tuple(self._scan())
                self._capture_state(records)
        except sqlite3.DatabaseError as error:
            raise ShadowError("durable shadow state could not be read") from error

    # -- verification ---------------------------------------------------------

    def _now(self) -> datetime:
        return _utc(self.clock(), "shadow clock")

    @property
    def closure_delay(self) -> timedelta:
        """Post-window delay, which configuration may increase but not weaken."""
        return max(self.max_clock_skew, DEFAULT_MAX_CLOCK_SKEW)

    @staticmethod
    def _verify_row(row: sqlite3.Row) -> ShadowRecord:
        """Parse one row and require every SQL column to equal hash-bound content."""
        raw = row["record_json"]
        try:
            if not isinstance(raw, bytes):
                raise ContractError("record_json must be stored as bytes")
            record = ShadowRecord.from_json(raw)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            # ContractError is a ValueError; every corrupt-row shape lands here.
            raise ShadowTampered("a stored shadow comparison is corrupt") from error
        if raw != record.canonical_json():
            raise ShadowTampered("stored comparison JSON is not canonical")
        for column, bound in _column_values(record).items():
            stored = row[column]
            if type(stored) is not type(bound) or stored != bound:
                raise ShadowTampered(f"stored {column} disagrees with comparison content")
        return record

    @staticmethod
    def _check_link(record: ShadowRecord, prior: ShadowRecord | None) -> None:
        """The one chain/time rule set, shared by appends and verification."""
        if record.sequence != (1 if prior is None else prior.sequence + 1):
            raise ShadowError("shadow comparison sequence is not contiguous")
        if record.prior_record_hash != (None if prior is None else prior.record_hash):
            raise ShadowError("shadow comparison does not extend the chain head")
        if prior is not None and record.recorded_at < prior.recorded_at:
            raise ShadowError("shadow comparison record time precedes the prior record")
        observed = record.comparison.observed_at
        if observed > record.recorded_at + MAX_CLOCK_SKEW_LIMIT:
            raise ShadowError("shadow comparison was observed after it was recorded")
        if record.recorded_at - observed > MAX_OBSERVATION_AGE_LIMIT:
            raise ShadowError("shadow comparison was stale when it was recorded")

    def _scan(self) -> Iterator[ShadowRecord]:
        """Verify schema and chain fail-closed, yielding each verified record."""
        try:
            self._verify_schema()
            cursor = self.tasks.db.execute(
                f"SELECT {_COLUMNS} FROM tc_shadow_comparisons ORDER BY sequence")
            prior: ShadowRecord | None = None
            nonces: set[str] = set()
            requests: set[tuple[str, str]] = set()
            for row in cursor:
                record = self._verify_row(row)
                try:
                    self._check_link(record, prior)
                except ShadowError as error:
                    raise ShadowTampered(f"shadow history is invalid: {error}") from error
                if record.comparison.nonce in nonces:
                    raise ShadowTampered("shadow history repeats a comparison nonce")
                request = (record.comparison.source_instance, record.comparison.request_id)
                if request in requests:
                    raise ShadowTampered("shadow history repeats a source request")
                if record.sequence > MAX_LEDGER_RECORDS:
                    raise ShadowTampered("shadow history exceeds the ledger bound")
                nonces.add(record.comparison.nonce)
                requests.add(request)
                prior = record
                yield record
        except sqlite3.DatabaseError as error:
            raise ShadowError("durable shadow state could not be read") from error
        if prior is not None and prior.recorded_at > self._now() + self.max_clock_skew:
            raise ShadowError(
                "shadow history is ahead of the trusted clock beyond the permitted skew")

    @staticmethod
    def _verify_expectation_row(row: sqlite3.Row) -> CaptureExpectation:
        try:
            raw = row["expectation_json"]
            if not isinstance(raw, bytes):
                raise ContractError("expectation_json must be stored as bytes")
            expectation = CaptureExpectation.from_json(raw)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ShadowTampered("a stored capture expectation is corrupt") from error
        if raw != expectation.canonical_json():
            raise ShadowTampered("stored capture expectation JSON is not canonical")
        for column, bound in _expectation_values(expectation).items():
            stored = row[column]
            if type(stored) is not type(bound) or stored != bound:
                raise ShadowTampered(
                    f"stored capture expectation {column} disagrees with content"
                )
        return expectation

    @staticmethod
    def _verify_capture_result_row(row: sqlite3.Row) -> CaptureResult:
        try:
            raw = row["result_json"]
            if not isinstance(raw, bytes):
                raise ContractError("result_json must be stored as bytes")
            result = CaptureResult.from_json(raw)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ShadowTampered("a stored capture result is corrupt") from error
        if raw != result.canonical_json():
            raise ShadowTampered("stored capture result JSON is not canonical")
        for column, bound in _capture_result_values(result).items():
            stored = row[column]
            if type(stored) is not type(bound) or stored != bound:
                raise ShadowTampered(
                    f"stored capture result {column} disagrees with content"
                )
        return result

    @staticmethod
    def _verify_capture_event_row(row: sqlite3.Row) -> CaptureEvent:
        try:
            raw = row["event_json"]
            if not isinstance(raw, bytes):
                raise ContractError("event_json must be stored as bytes")
            event = CaptureEvent.from_json(raw)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ShadowTampered("a stored capture event is corrupt") from error
        if raw != event.canonical_json():
            raise ShadowTampered("stored capture event JSON is not canonical")
        for column, bound in _capture_event_values(event).items():
            stored = row[column]
            if type(stored) is not type(bound) or stored != bound:
                raise ShadowTampered(
                    f"stored capture event {column} disagrees with content"
                )
        return event

    def _capture_events(self) -> tuple[CaptureEvent, ...]:
        rows = self.tasks.db.execute(
            "SELECT * FROM tc_shadow_capture_events ORDER BY sequence"
        ).fetchall()
        if len(rows) > 2 * MAX_LEDGER_RECORDS:
            raise ShadowTampered("shadow capture event history exceeds its bound")
        events: list[CaptureEvent] = []
        prior: CaptureEvent | None = None
        payloads: set[str] = set()
        for row in rows:
            event = self._verify_capture_event_row(row)
            if event.sequence != (1 if prior is None else prior.sequence + 1):
                raise ShadowTampered("capture event sequence is not contiguous")
            if event.prior_event_hash != (
                None if prior is None else prior.event_hash
            ):
                raise ShadowTampered("capture event does not extend its chain head")
            if prior is not None and event.recorded_at < prior.recorded_at:
                raise ShadowTampered("capture event time precedes its chain head")
            if event.payload_hash in payloads:
                raise ShadowTampered("capture event history repeats a payload")
            payloads.add(event.payload_hash)
            events.append(event)
            prior = event
        if prior is not None and prior.recorded_at > self._now() + self.max_clock_skew:
            raise ShadowError("capture event history is ahead of the trusted clock")
        return tuple(events)

    def _capture_state(
        self, records: tuple[ShadowRecord, ...] | None = None,
    ) -> tuple[
        Mapping[tuple[str, str], CaptureExpectation],
        Mapping[tuple[str, str], CaptureResult],
    ]:
        """Verify all capture rows and their exact comparison bindings."""
        try:
            self._verify_schema()
            if records is None:
                records = tuple(self._scan())
            records_by_hash = {
                item.comparison.content_hash: item for item in records
            }
            expectation_rows = self.tasks.db.execute(
                "SELECT * FROM tc_shadow_capture_expectations "
                "ORDER BY source_instance,request_id"
            ).fetchall()
            result_rows = self.tasks.db.execute(
                "SELECT * FROM tc_shadow_capture_results "
                "ORDER BY source_instance,request_id"
            ).fetchall()
            if len(expectation_rows) > MAX_LEDGER_RECORDS or len(result_rows) > MAX_LEDGER_RECORDS:
                raise ShadowTampered("shadow capture accounting exceeds the ledger bound")
            expectations: dict[tuple[str, str], CaptureExpectation] = {}
            results: dict[tuple[str, str], CaptureResult] = {}
            for row in expectation_rows:
                expectation = self._verify_expectation_row(row)
                key = (expectation.source_instance, expectation.request_id)
                if key in expectations:
                    raise ShadowTampered("capture accounting repeats a parent request")
                expectations[key] = expectation
            for row in result_rows:
                result = self._verify_capture_result_row(row)
                key = (result.source_instance, result.request_id)
                expectation = expectations.get(key)
                if expectation is None or result.expectation_hash != expectation.content_hash:
                    raise ShadowTampered("capture result does not bind its expectation")
                if key in results:
                    raise ShadowTampered("capture accounting repeats a terminal result")
                if result.recorded_at < expectation.observed_at - MAX_CLOCK_SKEW_LIMIT:
                    raise ShadowTampered("capture result predates its observation")
                if result.outcome is CaptureOutcome.CAPTURED:
                    record = records_by_hash.get(result.comparison_hash)
                    if record is None:
                        raise ShadowTampered("captured result references a missing comparison")
                    if CaptureExpectation.from_comparison(record.comparison) != expectation:
                        raise ShadowTampered(
                            "captured comparison disagrees with its parent expectation"
                        )
                results[key] = result
            events = self._capture_events()
            event_by_payload = {event.payload_hash: event for event in events}
            if len(event_by_payload) != len(events):
                raise ShadowTampered("capture event history repeats a payload")
            expected_payloads = {
                item.content_hash for item in expectations.values()
            } | {item.content_hash for item in results.values()}
            if set(event_by_payload) != expected_payloads:
                raise ShadowTampered(
                    "capture event chain does not match capture accounting"
                )
            for key, expectation in expectations.items():
                event = event_by_payload[expectation.content_hash]
                if (
                    event.kind is not CaptureEventKind.EXPECTATION
                    or (event.source_instance, event.request_id) != key
                ):
                    raise ShadowTampered("capture expectation event is inconsistent")
                result = results.get(key)
                if result is not None:
                    result_event = event_by_payload[result.content_hash]
                    if (
                        result_event.kind is not CaptureEventKind.RESULT
                        or (result_event.source_instance, result_event.request_id) != key
                        or result_event.sequence <= event.sequence
                        or result_event.recorded_at < result.recorded_at
                    ):
                        raise ShadowTampered("capture result event is inconsistent")
            now = self._now()
            if any(result.recorded_at > now + self.max_clock_skew for result in results.values()):
                raise ShadowError("capture accounting is ahead of the trusted clock")
            return MappingProxyType(expectations), MappingProxyType(results)
        except sqlite3.DatabaseError as error:
            raise ShadowError("durable capture accounting could not be read") from error

    # -- append ---------------------------------------------------------------

    def _append_capture_event(
        self,
        kind: CaptureEventKind,
        *,
        source_instance: str,
        request_id: str,
        payload_hash: str,
        recorded_at: datetime,
    ) -> CaptureEvent:
        db = self.tasks.db
        head_row = db.execute(
            "SELECT * FROM tc_shadow_capture_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        head = None if head_row is None else self._verify_capture_event_row(head_row)
        count = db.execute("SELECT COUNT(*) FROM tc_shadow_capture_events").fetchone()[0]
        if count != (0 if head is None else head.sequence):
            raise ShadowTampered("capture event history has a gap")
        if count >= 2 * MAX_LEDGER_RECORDS:
            raise ShadowError("capture event history is full")
        event = CaptureEvent(
            sequence=count + 1,
            kind=kind,
            source_instance=source_instance,
            request_id=request_id,
            payload_hash=payload_hash,
            recorded_at=(
                recorded_at
                if head is None or recorded_at >= head.recorded_at
                else head.recorded_at
            ),
            prior_event_hash=None if head is None else head.event_hash,
        )
        values = _capture_event_values(event)
        db.execute(
            "INSERT INTO tc_shadow_capture_events("
            + ",".join(values)
            + ") VALUES("
            + ",".join("?" for _ in values)
            + ")",
            tuple(values.values()),
        )
        return event

    def expect_capture(self, expectation: CaptureExpectation) -> CaptureExpectation:
        """Durably register one authenticated parent before adapter work begins."""
        if type(expectation) is not CaptureExpectation:
            raise ContractError("expect_capture requires a typed CaptureExpectation")
        try:
            with self.tasks.transaction():
                return self._expect_capture(expectation)
        except sqlite3.DatabaseError as error:
            raise ShadowError("capture expectation could not be recorded") from error

    def _expect_capture(self, expectation: CaptureExpectation) -> CaptureExpectation:
        self._verify_schema()
        row = self.tasks.db.execute(
            "SELECT * FROM tc_shadow_capture_expectations "
            "WHERE source_instance=? AND request_id=?",
            (expectation.source_instance, expectation.request_id),
        ).fetchone()
        if row is not None:
            stored = self._verify_expectation_row(row)
            if stored.canonical_json() != expectation.canonical_json():
                raise ShadowConflict("parent request was replayed with different capture facts")
            return stored
        count = self.tasks.db.execute(
            "SELECT COUNT(*) FROM tc_shadow_capture_expectations"
        ).fetchone()[0]
        if count >= MAX_LEDGER_RECORDS:
            raise ShadowError("shadow capture ledger is full; refusing unbounded growth")
        now = self._now()
        if expectation.observed_at > now + self.max_clock_skew:
            raise ShadowError("capture expectation observation is in the future")
        if now - expectation.observed_at > MAX_OBSERVATION_WINDOW:
            raise ShadowError("capture expectation is too old to account safely")
        values = _expectation_values(expectation)
        self.tasks.db.execute(
            "INSERT INTO tc_shadow_capture_expectations("
            + ",".join(values)
            + ") VALUES("
            + ",".join("?" for _ in values)
            + ")",
            tuple(values.values()),
        )
        self._append_capture_event(
            CaptureEventKind.EXPECTATION,
            source_instance=expectation.source_instance,
            request_id=expectation.request_id,
            payload_hash=expectation.content_hash,
            recorded_at=now,
        )
        return expectation

    def record_capture_failure(
        self, expectation: CaptureExpectation, failure_code: str,
    ) -> CaptureResult:
        """Permanently account an adapter/append failure; it blocks the window."""
        _identifier(failure_code, "failure_code")
        if type(expectation) is not CaptureExpectation:
            raise ContractError("capture failure requires a typed expectation")
        try:
            with self.tasks.transaction():
                stored = self._expect_capture(expectation)
                row = self.tasks.db.execute(
                    "SELECT * FROM tc_shadow_capture_results "
                    "WHERE source_instance=? AND request_id=?",
                    (stored.source_instance, stored.request_id),
                ).fetchone()
                if row is not None:
                    prior = self._verify_capture_result_row(row)
                    if (
                        prior.expectation_hash == stored.content_hash
                        and prior.outcome is CaptureOutcome.FAILED
                        and prior.failure_code == failure_code
                    ):
                        return prior
                    raise ShadowConflict(
                        "parent request already has a different capture result"
                    )
                return self._record_capture_result(
                    CaptureResult(
                        source_instance=stored.source_instance,
                        request_id=stored.request_id,
                        expectation_hash=stored.content_hash,
                        outcome=CaptureOutcome.FAILED,
                        recorded_at=self._now(),
                        failure_code=failure_code,
                    )
                )
        except sqlite3.DatabaseError as error:
            raise ShadowError("capture failure could not be recorded") from error

    def _record_capture_result(self, result: CaptureResult) -> CaptureResult:
        row = self.tasks.db.execute(
            "SELECT * FROM tc_shadow_capture_results "
            "WHERE source_instance=? AND request_id=?",
            (result.source_instance, result.request_id),
        ).fetchone()
        if row is not None:
            stored = self._verify_capture_result_row(row)
            if stored.canonical_json() != result.canonical_json():
                raise ShadowConflict("parent request already has a different capture result")
            return stored
        values = _capture_result_values(result)
        self.tasks.db.execute(
            "INSERT INTO tc_shadow_capture_results("
            + ",".join(values)
            + ") VALUES("
            + ",".join("?" for _ in values)
            + ")",
            tuple(values.values()),
        )
        self._append_capture_event(
            CaptureEventKind.RESULT,
            source_instance=result.source_instance,
            request_id=result.request_id,
            payload_hash=result.content_hash,
            recorded_at=result.recorded_at,
        )
        return result

    def record(self, comparison: ShadowComparison) -> ShadowRecord:
        """Append one comparison.  An exact replay returns the stored record; a
        nonce replayed with any changed content, or a source/request pair
        presented again under a fresh nonce, is a :class:`ShadowConflict`.  One
        request from one source instance is therefore at most one sample.

        Appends verify the schema, the replayed row, and the chain head inside
        the inserting write transaction; whole-chain verification happens on
        every read, so an append stays cheap as the ledger grows.
        """
        if type(comparison) is not ShadowComparison:
            raise ContractError("record requires a typed ShadowComparison")
        expectation = CaptureExpectation.from_comparison(comparison)
        try:
            with self.tasks.transaction():
                self._expect_capture(expectation)
            # A crash here leaves a durable pending capture, which is exactly
            # the fail-closed accounting state the canary summary must see.
            with self.tasks.transaction():
                return self._record(comparison, expectation)
        except sqlite3.DatabaseError as error:
            raise ShadowError("shadow comparison could not be recorded") from error

    def _record(
        self, comparison: ShadowComparison, expectation: CaptureExpectation,
    ) -> ShadowRecord:
        db = self.tasks.db
        self._verify_schema()
        replayed = db.execute(
            f"SELECT {_COLUMNS} FROM tc_shadow_comparisons WHERE nonce=? "
            "OR (source_instance=? AND request_id=?) ORDER BY sequence",
            (comparison.nonce, comparison.source_instance, comparison.request_id),
        ).fetchall()
        if replayed:
            stored = [self._verify_row(row) for row in replayed]
            if len(stored) == 1 and stored[0].comparison.canonical_json() == comparison.canonical_json():
                result_row = db.execute(
                    "SELECT * FROM tc_shadow_capture_results "
                    "WHERE source_instance=? AND request_id=?",
                    (comparison.source_instance, comparison.request_id),
                ).fetchone()
                if result_row is None:
                    raise ShadowTampered("comparison exists without a capture result")
                result = self._verify_capture_result_row(result_row)
                if (
                    result.outcome is not CaptureOutcome.CAPTURED
                    or result.comparison_hash != comparison.content_hash
                    or result.expectation_hash != expectation.content_hash
                ):
                    raise ShadowTampered("comparison capture result is inconsistent")
                return stored[0]
            if any(item.comparison.nonce == comparison.nonce for item in stored):
                raise ShadowConflict("comparison nonce was replayed with different content")
            raise ShadowConflict(
                "source request was already recorded under a different nonce")
        head_row = db.execute(
            f"SELECT {_COLUMNS} FROM tc_shadow_comparisons ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        head = None if head_row is None else self._verify_row(head_row)
        count = db.execute("SELECT COUNT(*) FROM tc_shadow_comparisons").fetchone()[0]
        if count != (0 if head is None else head.sequence):
            raise ShadowTampered("shadow history has a gap")
        if count >= MAX_LEDGER_RECORDS:
            raise ShadowError("shadow ledger is full; refusing unbounded growth")
        now = self._now()
        if head is not None and head.recorded_at > now + self.max_clock_skew:
            raise ShadowError(
                "shadow history is ahead of the trusted clock beyond the permitted skew")
        if comparison.observed_at > now + self.max_clock_skew:
            raise ShadowError("comparison observation time is in the future")
        if now - comparison.observed_at > self.max_observation_age:
            raise ShadowError("comparison observation is stale")
        divergence, reasons = classify_divergence(comparison.old, comparison.new)
        record = ShadowRecord(
            sequence=count + 1, comparison=comparison, divergence=divergence, reasons=reasons,
            # Monotonic by construction, so a window over record time is a
            # contiguous run of the chain.
            recorded_at=now if head is None or now >= head.recorded_at else head.recorded_at,
            prior_record_hash=None if head is None else head.record_hash,
        )
        self._check_link(record, head)
        values = _column_values(record)
        db.execute(
            f"INSERT INTO tc_shadow_comparisons({_COLUMNS}) VALUES("
            + ",".join("?" for _ in values) + ")",
            tuple(values.values()),
        )
        self._record_capture_result(CaptureResult(
            source_instance=comparison.source_instance,
            request_id=comparison.request_id,
            expectation_hash=expectation.content_hash,
            outcome=CaptureOutcome.CAPTURED,
            recorded_at=record.recorded_at,
            comparison_hash=comparison.content_hash,
        ))
        return record

    # -- read-only surfaces ---------------------------------------------------

    def _verified_records(self) -> tuple[ShadowRecord, ...]:
        records = tuple(self._scan())
        self._capture_state(records)
        return records

    def get(self, nonce: str) -> ShadowRecord | None:
        """The verified record for a nonce, or ``None``."""
        _identifier(nonce, "nonce")
        found = None
        for record in self._verified_records():
            if record.comparison.nonce == nonce:
                found = record
        return found

    def get_by_hash(self, record_hash: str) -> ShadowRecord | None:
        _hash_text(record_hash, "record_hash")
        found = None
        for record in self._verified_records():
            if record.record_hash == record_hash:
                found = record
        return found

    def capture_for(
        self, source_instance: str, request_id: str,
    ) -> tuple[CaptureExpectation | None, CaptureResult | None, ShadowRecord | None]:
        """Return verified accounting for one parent request, if registered."""
        _identifier(source_instance, "source_instance")
        _identifier(request_id, "request_id")
        records = tuple(self._scan())
        expectations, results = self._capture_state(records)
        key = (source_instance, request_id)
        expectation = expectations.get(key)
        result = results.get(key)
        record = next((
            item for item in records
            if (
                item.comparison.source_instance,
                item.comparison.request_id,
            ) == key
        ), None)
        if expectation is None and (result is not None or record is not None):
            raise ShadowTampered("parent capture exists without an expectation")
        if result is not None and result.outcome is CaptureOutcome.CAPTURED:
            if record is None or result.comparison_hash != record.comparison.content_hash:
                raise ShadowTampered("captured parent does not bind its comparison")
        elif record is not None:
            raise ShadowTampered("comparison exists without a captured parent result")
        return expectation, result, record

    def records(self, *, after_sequence: int = 0, limit: int = MAX_PAGE_RECORDS) -> tuple[ShadowRecord, ...]:
        """One bounded page of verified records, in ledger order."""
        _positive(after_sequence, "after_sequence", minimum=0)
        if _positive(limit, "limit") > MAX_PAGE_RECORDS:
            raise ContractError(f"limit must not exceed {MAX_PAGE_RECORDS}")
        page = [
            record for record in self._verified_records()
            if record.sequence > after_sequence
        ]
        return tuple(page[:limit])

    def status(self) -> ShadowStatus:
        counts = dict.fromkeys(DIVERGENCE_ORDER, 0)
        head = None
        records = self._verified_records()
        for record in records:
            counts[record.divergence] += 1
            head = record
        return ShadowStatus(
            record_count=0 if head is None else head.sequence,
            head_sequence=None if head is None else head.sequence,
            head_hash=None if head is None else head.record_hash,
            last_recorded_at=None if head is None else head.recorded_at,
            counts=MappingProxyType(counts),
        )

    def verify_chain(self) -> int:
        """Re-verify schema and chain end to end; return the verified count.

        As with the rollout ledger, a raw-database attacker truncating the newest
        records leaves a valid shorter chain.  A canary summary binds the exact
        in-window records through ``evidence_root``, so truncation changes the
        summary hash and no longer matches a pinned attestation.
        """
        records = tuple(self._scan())
        count = len(records)
        self._capture_state(records)
        return count

    def summary(self, criteria: CanaryCriteria) -> CanarySummary:
        """Derive the canary summary for a closed, fresh observation window.

        Raises :class:`ShadowError` until the trusted clock is strictly past
        ``window_end`` plus the non-weakenable ``closure_delay`` (the evidence is
        incomplete, and a clock regression within that delay could
        still record inside the window) and once the window is older than the
        criteria's ``max_evidence_age_seconds``.  Otherwise returns a summary
        whose ``accepted``/``reasons`` follow deterministically from the evidence.

        A record is a sample only when both ``observed_at`` and ``recorded_at``
        lie inside the half-open window.  A record recorded inside the window but
        observed outside it is never silently dropped: it is counted in
        ``out_of_window_observations`` and blocks acceptance.
        """
        return self.summaries((criteria,))[0]

    def summaries(
        self, criteria_items: tuple[CanaryCriteria, ...],
    ) -> tuple[CanarySummary, ...]:
        """Derive several summaries from one verified SQLite snapshot scan."""
        if (
            type(criteria_items) is not tuple
            or not criteria_items
            or len(criteria_items) > len(RolloutCapability)
            or any(type(item) is not CanaryCriteria for item in criteria_items)
        ):
            raise ContractError("summaries requires a bounded typed criteria tuple")
        now = self._now()
        for criteria in criteria_items:
            if now <= criteria.window_end + self.closure_delay:
                raise ShadowError(
                    "the observation window has not closed beyond the permitted clock skew")
            if now - criteria.window_end > timedelta(
                seconds=criteria.max_evidence_age_seconds
            ):
                raise ShadowError("the observation window is stale")
        records = tuple(self._scan())
        expectations, capture_results = self._capture_state(records)
        return tuple(
            self._summary_from_verified(
                criteria, records, expectations, capture_results,
            )
            for criteria in criteria_items
        )

    @staticmethod
    def _summary_from_verified(
        criteria: CanaryCriteria,
        records: tuple[ShadowRecord, ...],
        expectations: Mapping[tuple[str, str], CaptureExpectation],
        capture_results: Mapping[tuple[str, str], CaptureResult],
    ) -> CanarySummary:
        counts = dict.fromkeys(DIVERGENCE_ORDER, 0)
        sources = dict.fromkeys(criteria.source_instances, 0)
        coverage = dict.fromkeys(COVERAGE_ORDER, 0)
        cross_revision = foreign = unobserved = 0
        expected = captured = failed = pending = unaccounted = 0
        first: ShadowRecord | None = None
        last: ShadowRecord | None = None
        # Observation time is not monotonic along the chain, so its bounds are
        # the extremes over the counted samples, not the first and last record.
        observed: list[datetime] = []
        root = hashlib.sha256(criteria.content_hash.encode("ascii"))
        capture_keys = {
            key for key, expectation in expectations.items()
            if criteria.contains(expectation.observed_at)
        } | {
            (record.comparison.source_instance, record.comparison.request_id)
            for record in records if criteria.contains(record.recorded_at)
        }
        for key in sorted(capture_keys):
            expectation = expectations.get(key)
            result = capture_results.get(key)
            root.update(
                b"missing-expectation" if expectation is None
                else expectation.content_hash.encode("ascii")
            )
            root.update(
                b"pending-capture" if result is None
                else result.content_hash.encode("ascii")
            )

        for key, expectation in expectations.items():
            if not criteria.contains(expectation.observed_at):
                continue
            if (
                expectation.policy_revision,
                expectation.config_revision,
                expectation.evidence_revision,
            ) != (
                criteria.policy_revision,
                criteria.config_revision,
                criteria.evidence_revision,
            ):
                cross_revision += 1
                continue
            if expectation.source_instance not in criteria.source_instances:
                foreign += 1
                continue
            expected += 1
            result = capture_results.get(key)
            if result is None:
                pending += 1
            elif result.outcome is CaptureOutcome.FAILED:
                failed += 1
            else:
                captured += 1

        for record in records:
            if not criteria.contains(record.recorded_at):
                continue
            comparison = record.comparison
            key = (comparison.source_instance, comparison.request_id)
            expectation = expectations.get(key)
            result = capture_results.get(key)
            exact_capture = (
                expectation is not None
                and expectation == CaptureExpectation.from_comparison(comparison)
                and result is not None
                and result.outcome is CaptureOutcome.CAPTURED
                and result.comparison_hash == comparison.content_hash
                and result.expectation_hash == expectation.content_hash
            )
            if not exact_capture:
                unaccounted += 1
                continue
            if not criteria.contains(comparison.observed_at):
                unobserved += 1
                continue
            if (comparison.policy_revision, comparison.config_revision,
                    comparison.evidence_revision) != (
                    criteria.policy_revision, criteria.config_revision,
                    criteria.evidence_revision):
                continue
            if comparison.source_instance not in criteria.source_instances:
                continue
            counts[record.divergence] += 1
            for kind in derive_coverage(comparison):
                coverage[kind] += 1
            sources[comparison.source_instance] += 1
            root.update(record.record_hash.encode("ascii"))
            first = first or record
            last = record
            observed.append(comparison.observed_at)
            observed = [min(observed), max(observed)]
        return CanarySummary(
            criteria=criteria, counts=counts, coverage_counts=coverage,
            source_counts=tuple(sorted(sources.items())),
            expected_captures=expected, captured_captures=captured,
            failed_captures=failed, pending_captures=pending,
            unaccounted_samples=unaccounted,
            cross_revision_samples=cross_revision, foreign_source_samples=foreign,
            out_of_window_observations=unobserved,
            first_sequence=None if first is None else first.sequence,
            last_sequence=None if last is None else last.sequence,
            first_recorded_at=None if first is None else first.recorded_at,
            last_recorded_at=None if last is None else last.recorded_at,
            first_observed_at=observed[0] if observed else None,
            last_observed_at=observed[-1] if observed else None,
            evidence_root=root.hexdigest(),
        )

    def attestation_evidence(self, criteria: CanaryCriteria) -> dict[str, Any]:
        """The accepted summary document, for ``CommissioningAttestation.canary_evidence``.

        Raises :class:`ShadowError` unless the canary is accepted.  The document
        is evidence for a human commissioning decision; it activates nothing.
        """
        summary = self.summary(criteria)
        if not summary.accepted:
            raise ShadowError("canary evidence is not acceptable: " + "; ".join(summary.reasons))
        return summary.to_document()

    def verify_evidence(self, document: Any) -> CanarySummary:
        """Re-derive a presented evidence document from durable truth, fail-closed.

        The document must parse strictly, be for machine ``17049``, be accepted,
        still be fresh, and equal the summary this ledger derives now, so a
        tampered, foreign, stale, or no-longer-supported document is refused.
        """
        return self.verify_evidence_many((document,))[0]

    def verify_evidence_many(self, documents: tuple[Any, ...]) -> tuple[CanarySummary, ...]:
        """Rederive several evidence documents with one full ledger scan."""
        if type(documents) is not tuple or not documents or len(documents) > len(RolloutCapability):
            raise ContractError("evidence batch must be a bounded non-empty tuple")
        presented: list[CanarySummary] = []
        try:
            for document in documents:
                presented.append(CanarySummary.from_document(document))
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ShadowError("presented canary evidence is malformed") from error
        current = self.summaries(tuple(item.criteria for item in presented))
        for offered, derived in zip(presented, current):
            if derived.content_hash != offered.content_hash:
                raise ShadowTampered(
                    "presented canary evidence does not match durable shadow records"
                )
            if not derived.accepted:
                raise ShadowError(
                    "canary evidence is not acceptable: " + "; ".join(derived.reasons)
                )
        return current


__all__ = [
    "BENIGN_CLASSES", "BLOCKING_CLASSES", "CanaryCriteria", "CanarySummary",
    "CaptureEvent", "CaptureEventKind", "CaptureExpectation", "CaptureOutcome",
    "CaptureResult", "ComparisonOrigin",
    "DivergenceClass", "PolicyDecision", "PolicyOutcome", "RouteKind",
    "RoutingDecision", "ShadowComparison", "ShadowConflict", "ShadowDecision", "ShadowError",
    "ShadowLedger", "ShadowRecord", "ShadowStatus", "ShadowTampered", "classify_divergence",
    "effect_flags", "required_authority", "CAPABILITY_COVERAGE", "COVERAGE_ORDER",
    "CoverageClass", "derive_coverage",
]
