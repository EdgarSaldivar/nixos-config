"""Disabled-by-default Phase 7 durable rollout state foundation.

This module is a library only.  It records, in the shared Phase 0 task database,
which Phase 7 capabilities have been commissioned for Vast machine ``17049`` and
provides read-only status/evaluation surfaces for a later runtime to consult.
Nothing here executes an action, flips a runtime feature flag, calls a model, or
wires itself into a service.  With no commissioning events recorded every
capability is inert and routing stays on the legacy loop.

Capabilities are classified by *effect*, never by an operation, command, unit,
or API name.  A novel operation that carries the ``host`` effect is a host action
because of its effect, not because it appears in a catalog; adding it therefore
needs no source change here.  Each capability has an independent activation flag
(derived from its own append-only event stream) and requires an exact, versioned
commissioning attestation.  Activation fails closed on a missing, unknown, or
mismatched attestation and on any unsatisfied prerequisite.  Rollback returns
routing to the legacy loop and never deletes history.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .plan_authorization import EFFECT_FLAG_NAMES, _STANDING_FORBIDDEN
from .plans import (
    MACHINE_ID,
    Contract,
    ContractError,
    Effect,
    _fields,
    _freeze,
    _identifier,
    _safe_json,
    _string,
    _thaw,
    _utc,
    canonical_json,
    parse_utc,
    utc_text,
)
from .tasks import TaskStore

ROLLOUT_SCHEMA_VERSION = 1
SCHEMA_VERSION = 1


class RolloutError(RuntimeError):
    """A rollout activation or rollback is unsafe and is refused fail-closed."""


class RolloutConflict(RolloutError):
    """A commissioning nonce was replayed with different content, or a capability
    was activated/rolled back in a way that conflicts with durable truth."""


class RolloutTampered(RolloutError):
    """Durable rollout state failed verification: a schema object is missing or
    malformed, a row is corrupt, an authority column disagrees with its hash-bound
    event, the chain is broken, or history replays to an impossible state.

    It is a :class:`RolloutError`, so a caller that fails closed on
    ``RolloutError`` also fails closed on tamper."""


class RolloutCapability(str, Enum):
    """The eight Phase 7 capabilities, classified by effect, not by command."""

    OPERATOR_READONLY = "operator-readonly-tasks"
    OWNED_COMPONENT_MUTATION = "owned-component-mutation"
    OWNED_COMPONENT_STANDING_CONSENT = "owned-component-standing-consent"
    HOST_ACTIONS = "host-actions"
    TENANT_ACTIONS = "tenant-actions"
    EXTERNAL_WRITES = "external-vast-writes"
    DETECTOR_CREATED_TASKS = "detector-created-tasks"
    LEGACY_LOOP_RETIREMENT = "legacy-loop-retirement"


class RolloutAction(str, Enum):
    ACTIVATE = "activate"
    ROLLBACK = "rollback"


# The commissioning sequence.  It is used for stable ordering only; safe order is
# enforced through the explicit prerequisite graph below, not this list.
ROLLOUT_ORDER: tuple[RolloutCapability, ...] = (
    RolloutCapability.OPERATOR_READONLY,
    RolloutCapability.OWNED_COMPONENT_MUTATION,
    RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT,
    RolloutCapability.HOST_ACTIONS,
    RolloutCapability.TENANT_ACTIONS,
    RolloutCapability.EXTERNAL_WRITES,
    RolloutCapability.DETECTOR_CREATED_TASKS,
    RolloutCapability.LEGACY_LOOP_RETIREMENT,
)

# Direct prerequisites.  Standing consent is an optional convenience over owned
# mutation and is deliberately NOT a prerequisite for host actions.  Host, then
# tenant, then external writes commission as an escalating chain.  Detector tasks
# require owned mutation so the new path can act, and only once detector tasks are
# live may the legacy handover loop be retired.
_DIRECT_PREREQUISITES: Mapping[RolloutCapability, tuple[RolloutCapability, ...]] = MappingProxyType({
    RolloutCapability.OPERATOR_READONLY: (),
    RolloutCapability.OWNED_COMPONENT_MUTATION: (RolloutCapability.OPERATOR_READONLY,),
    RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT: (RolloutCapability.OWNED_COMPONENT_MUTATION,),
    RolloutCapability.HOST_ACTIONS: (RolloutCapability.OWNED_COMPONENT_MUTATION,),
    RolloutCapability.TENANT_ACTIONS: (RolloutCapability.HOST_ACTIONS,),
    RolloutCapability.EXTERNAL_WRITES: (RolloutCapability.TENANT_ACTIONS,),
    RolloutCapability.DETECTOR_CREATED_TASKS: (RolloutCapability.OWNED_COMPONENT_MUTATION,),
    RolloutCapability.LEGACY_LOOP_RETIREMENT: (RolloutCapability.DETECTOR_CREATED_TASKS,),
})


def _closure(capability: RolloutCapability) -> frozenset[RolloutCapability]:
    seen: set[RolloutCapability] = set()
    frontier = list(_DIRECT_PREREQUISITES[capability])
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(_DIRECT_PREREQUISITES[current])
    return frozenset(seen)


PREREQUISITES: Mapping[RolloutCapability, frozenset[RolloutCapability]] = MappingProxyType(
    {capability: _closure(capability) for capability in RolloutCapability}
)
DEPENDENTS: Mapping[RolloutCapability, frozenset[RolloutCapability]] = MappingProxyType({
    capability: frozenset(
        other for other in RolloutCapability if capability in PREREQUISITES[other]
    )
    for capability in RolloutCapability
})


# Effect-based, open classification.  A capability governs an effect *class*, so
# any operation carrying that effect is covered without naming the operation.
_EFFECT_CLASS_CAPABILITY: Mapping[str, RolloutCapability] = MappingProxyType({
    "owned_component": RolloutCapability.OWNED_COMPONENT_MUTATION,
    "host": RolloutCapability.HOST_ACTIONS,
    "tenant": RolloutCapability.TENANT_ACTIONS,
    "external_commitment": RolloutCapability.EXTERNAL_WRITES,
})
# Reachability, secrets and irreversibility are never commissioned as a standing
# Phase 7 capability; they keep requiring explicit human approval every time.
_ROLLOUT_UNGOVERNED_EFFECTS = frozenset({"reachability", "secrets", "irreversible"})

# Standing consent may only ever cover the reversible owned-component class.  This
# reuses the authorization module's forbidden set so tenant, host, reachability,
# money/external, secret and irreversible standing consent is impossible here too.
STANDING_CONSENT_EFFECT_CLASSES = frozenset({"owned_component"})
assert STANDING_CONSENT_EFFECT_CLASSES <= frozenset(EFFECT_FLAG_NAMES)
assert not (STANDING_CONSENT_EFFECT_CLASSES & _STANDING_FORBIDDEN), (
    "standing consent must never cover a forbidden effect class"
)
assert "tenant" not in STANDING_CONSENT_EFFECT_CLASSES, "tenant standing consent is prohibited"

# Effect fields that are identity/serialization, not effect flags.
_EFFECT_NON_FLAG_FIELDS = frozenset({"effect_id", "description", "schema_version"})


def _effect_coverage_gaps(
    flag_names: Any, governed: Any, ungoverned: Any,
) -> tuple[str, ...]:
    """Describe every way the effect-flag coverage is incomplete or ambiguous.

    Every effect flag must be either mapped to a capability or explicitly
    ungoverned, never both and never neither, so a flag added later cannot be
    silently treated as harmless.
    """
    flags, mapped, unmapped = frozenset(flag_names), frozenset(governed), frozenset(ungoverned)
    problems = [f"effect flag {flag} is neither governed nor explicitly ungoverned"
                for flag in sorted(flags - mapped - unmapped)]
    problems += [f"effect flag {flag} is both governed and ungoverned"
                 for flag in sorted(mapped & unmapped)]
    problems += [f"coverage names unknown effect flag {flag}"
                 for flag in sorted((mapped | unmapped) - flags)]
    return tuple(problems)


def _assert_effect_coverage() -> None:
    # Raised explicitly (not ``assert``) so ``python -O`` cannot strip the check.
    problems = list(_effect_coverage_gaps(
        EFFECT_FLAG_NAMES, _EFFECT_CLASS_CAPABILITY, _ROLLOUT_UNGOVERNED_EFFECTS,
    ))
    declared = {item.name for item in dataclasses.fields(Effect)} - _EFFECT_NON_FLAG_FIELDS
    problems += [f"Effect field {name} is not a known effect flag"
                 for name in sorted(declared - frozenset(EFFECT_FLAG_NAMES))]
    if problems:
        raise RuntimeError("rollout effect coverage is incomplete: " + "; ".join(problems))


_assert_effect_coverage()


def _bounded_evidence(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    if not value:
        raise ContractError(f"{name} must record at least one canary observation")
    return _freeze(_safe_json(dict(value), name))


@dataclass(frozen=True)
class CommissioningAttestation(Contract):
    """An exact, versioned commissioning attestation for one capability.

    Its :attr:`attestation_id` (the content hash) is the exact identity an
    activation binds and a later evaluation checks against.  Any change to the
    capability, version, machine, bound revisions, canary evidence, or statement
    produces a different attestation, so a stale or foreign attestation cannot
    silently authorize a capability.
    """

    capability: RolloutCapability
    version: int
    policy_revision: str
    config_revision: str
    evidence_revision: str
    canary_evidence: Mapping[str, Any]
    statement: str
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "capability", "version", "policy_revision",
               "config_revision", "evidence_revision", "canary_evidence",
               "statement", "machine_id"}

    def __post_init__(self) -> None:
        if not isinstance(self.capability, RolloutCapability):
            raise ContractError("attestation capability must be a typed capability")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ContractError("attestation version must be a positive integer")
        _identifier(self.policy_revision, "policy_revision")
        _identifier(self.config_revision, "config_revision")
        _identifier(self.evidence_revision, "evidence_revision")
        object.__setattr__(self, "canary_evidence", _bounded_evidence(self.canary_evidence, "canary_evidence"))
        _string(self.statement, "statement")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"attestations are restricted to machine {MACHINE_ID}")

    @property
    def attestation_id(self) -> str:
        return self.content_hash

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "capability": self.capability.value,
            "version": self.version, "policy_revision": self.policy_revision,
            "config_revision": self.config_revision, "evidence_revision": self.evidence_revision,
            "canary_evidence": _thaw(self.canary_evidence), "statement": self.statement,
            "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "CommissioningAttestation":
        value = _fields(document, cls._FIELDS, "CommissioningAttestation")
        try:
            capability = RolloutCapability(value["capability"])
        except (TypeError, ValueError) as error:
            raise ContractError("unknown rollout capability") from error
        return cls(
            capability=capability, version=value["version"],
            policy_revision=value["policy_revision"], config_revision=value["config_revision"],
            evidence_revision=value["evidence_revision"], canary_evidence=value["canary_evidence"],
            statement=value["statement"], machine_id=value["machine_id"],
        )


@dataclass(frozen=True)
class RolloutEvent(Contract):
    """One durable, hash-bound activation or rollback in the append-only chain."""

    sequence: int
    capability: RolloutCapability
    action: RolloutAction
    actor: str
    policy_revision: str
    config_revision: str
    evidence_revision: str
    occurred_at: datetime
    nonce: str
    canary_evidence: Mapping[str, Any]
    reason: str
    attestation: CommissioningAttestation | None
    prior_event_hash: str | None
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "sequence", "capability", "action", "actor",
               "policy_revision", "config_revision", "evidence_revision", "occurred_at",
               "nonce", "canary_evidence", "reason", "attestation", "prior_event_hash",
               "machine_id"}

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ContractError("sequence must be a positive integer")
        if not isinstance(self.capability, RolloutCapability):
            raise ContractError("event capability must be a typed capability")
        if not isinstance(self.action, RolloutAction):
            raise ContractError("event action must be a typed action")
        _identifier(self.actor, "actor")
        _identifier(self.policy_revision, "policy_revision")
        _identifier(self.config_revision, "config_revision")
        _identifier(self.evidence_revision, "evidence_revision")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        _identifier(self.nonce, "nonce")
        object.__setattr__(self, "canary_evidence", _bounded_evidence(self.canary_evidence, "canary_evidence"))
        _string(self.reason, "reason")
        if self.prior_event_hash is not None:
            _identifier(self.prior_event_hash, "prior_event_hash")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"rollout events are restricted to machine {MACHINE_ID}")
        if self.action is RolloutAction.ACTIVATE:
            attestation = self.attestation
            if not isinstance(attestation, CommissioningAttestation):
                # Fail closed: an activation without a commissioning attestation.
                raise ContractError("activation requires a commissioning attestation")
            if attestation.capability is not self.capability:
                raise ContractError("attestation does not commission this capability")
            if attestation.machine_id != self.machine_id:
                raise ContractError("attestation machine does not match the event")
            if (
                attestation.policy_revision != self.policy_revision
                or attestation.config_revision != self.config_revision
                or attestation.evidence_revision != self.evidence_revision
                or canonical_json(attestation.canary_evidence)
                != canonical_json(self.canary_evidence)
            ):
                raise ContractError("attestation bindings do not match the activation event")
        elif self.attestation is not None:
            raise ContractError("a rollback event must not carry a commissioning attestation")

    @property
    def event_hash(self) -> str:
        return self.content_hash

    @property
    def attestation_id(self) -> str | None:
        return None if self.attestation is None else self.attestation.attestation_id

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "sequence": self.sequence,
            "capability": self.capability.value, "action": self.action.value,
            "actor": self.actor, "policy_revision": self.policy_revision,
            "config_revision": self.config_revision, "evidence_revision": self.evidence_revision,
            "occurred_at": utc_text(self.occurred_at), "nonce": self.nonce,
            "canary_evidence": _thaw(self.canary_evidence), "reason": self.reason,
            "attestation": None if self.attestation is None else self.attestation.to_document(),
            "prior_event_hash": self.prior_event_hash, "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "RolloutEvent":
        value = _fields(document, cls._FIELDS, "RolloutEvent")
        try:
            capability = RolloutCapability(value["capability"])
            action = RolloutAction(value["action"])
        except (TypeError, ValueError) as error:
            raise ContractError("unknown rollout capability or action") from error
        raw_attestation = value["attestation"]
        attestation = None if raw_attestation is None else CommissioningAttestation.from_document(raw_attestation)
        return cls(
            sequence=value["sequence"], capability=capability, action=action,
            actor=value["actor"], policy_revision=value["policy_revision"],
            config_revision=value["config_revision"], evidence_revision=value["evidence_revision"],
            occurred_at=parse_utc(value["occurred_at"], "occurred_at"), nonce=value["nonce"],
            canary_evidence=value["canary_evidence"], reason=value["reason"],
            attestation=attestation, prior_event_hash=value["prior_event_hash"],
            machine_id=value["machine_id"],
        )


@dataclass(frozen=True)
class CapabilityStatus:
    capability: RolloutCapability
    active: bool
    attestation_id: str | None
    sequence: int | None
    prerequisites: tuple[RolloutCapability, ...]
    prerequisites_satisfied: bool
    active_dependents: tuple[RolloutCapability, ...]
    expected_attestation_id: str | None
    expected_attestation_matches: bool


@dataclass(frozen=True)
class RolloutDecision:
    capability: RolloutCapability
    active: bool
    prerequisites_satisfied: bool
    attestation_matches: bool | None
    permitted: bool
    reasons: tuple[str, ...]
    expected_attestation_matches: bool
    revisions_match: bool | None
    evidence_matches: bool
    prerequisite_evidence_matches: bool


@dataclass(frozen=True)
class EffectDecision:
    permitted: bool
    required_capabilities: tuple[RolloutCapability, ...]
    missing_capabilities: tuple[RolloutCapability, ...]
    ungoverned_effects: tuple[str, ...]
    reasons: tuple[str, ...]


def classify_effect(effect: Effect) -> tuple[frozenset[RolloutCapability], frozenset[str]]:
    """Map an effect to the capabilities it needs, purely from its effect flags.

    Returns ``(required capabilities, ungoverned effect classes)``.  A read-only
    effect (no flags) requires only operator read-only tasks.  A set flag with no
    capability mapping, including one this module has never heard of, is reported
    as ungoverned and is therefore never permitted.  Classification
    never inspects ``effect_id`` or ``description``, so a brand-new operation is
    classified by effect alone and needs no change to this module.
    """
    if not isinstance(effect, Effect):
        raise ContractError("classification requires a typed Effect")
    required: set[RolloutCapability] = set()
    ungoverned: set[str] = set()
    any_flag = False
    # Besides the known flags, consider every other field the effect carries, so a
    # flag added to Effect (or a subclass) without coverage here fails closed.
    extra = tuple(
        item.name for item in dataclasses.fields(effect)
        if item.name not in _EFFECT_NON_FLAG_FIELDS and item.name not in EFFECT_FLAG_NAMES
    )
    for flag in (*EFFECT_FLAG_NAMES, *extra):
        value = getattr(effect, flag, False)
        if value is False:
            continue
        any_flag = True
        capability = _EFFECT_CLASS_CAPABILITY.get(flag) if value is True else None
        if capability is not None:
            required.add(capability)
        else:
            # Explicitly ungoverned, uncovered, or not a plain boolean: never
            # permitted by rollout state.
            ungoverned.add(flag)
    if not any_flag:
        required.add(RolloutCapability.OPERATOR_READONLY)
    return frozenset(required), frozenset(ungoverned)


_HASH = re.compile(r"[0-9a-f]{64}")

# Clock-skew policy.  An event's time must sit within ``max_clock_skew`` of the
# ledger's trusted clock, and the configured skew itself is bounded so a caller
# cannot configure the policy away.
DEFAULT_MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_CLOCK_SKEW_LIMIT = timedelta(hours=1)

# Exact DDL.  Opening a ledger compares these (whitespace-normalized) with what
# SQLite recorded, so a missing, replaced, or weakened object fails closed.
_SCHEMA_TABLE_SQL = (
    "CREATE TABLE tc_rollout_schema ("
    "namespace TEXT PRIMARY KEY CHECK(namespace='rollout'), version INTEGER NOT NULL)"
)
_EVENTS_TABLE_SQL = """CREATE TABLE tc_rollout_events (
                         sequence INTEGER PRIMARY KEY,
                         event_hash TEXT NOT NULL UNIQUE,
                         prior_event_hash TEXT UNIQUE,
                         capability TEXT NOT NULL,
                         action TEXT NOT NULL CHECK(action IN ('activate','rollback')),
                         nonce TEXT NOT NULL UNIQUE,
                         occurred_utc TEXT NOT NULL,
                         recorded_utc TEXT NOT NULL,
                         event_json BLOB NOT NULL)"""
_EVENTS_INDEX_SQL = (
    "CREATE INDEX tc_rollout_events_capability "
    "ON tc_rollout_events(capability,sequence)"
)
_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_rollout_events_immutable_update "
    "BEFORE UPDATE ON tc_rollout_events "
    "BEGIN SELECT RAISE(ABORT, 'immutable rollout event'); END"
)
_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER tc_rollout_events_immutable_delete "
    "BEFORE DELETE ON tc_rollout_events "
    "BEGIN SELECT RAISE(ABORT, 'immutable rollout event'); END"
)


def _normalized_sql(sql: Any) -> str | None:
    return None if not isinstance(sql, str) else " ".join(sql.split())


_EXPECTED_SCHEMA_OBJECTS: Mapping[tuple[str, str], tuple[str, str]] = MappingProxyType({
    ("table", "tc_rollout_schema"): ("tc_rollout_schema", _normalized_sql(_SCHEMA_TABLE_SQL)),
    ("table", "tc_rollout_events"): ("tc_rollout_events", _normalized_sql(_EVENTS_TABLE_SQL)),
    ("index", "tc_rollout_events_capability"): ("tc_rollout_events", _normalized_sql(_EVENTS_INDEX_SQL)),
    ("trigger", "tc_rollout_events_immutable_update"): ("tc_rollout_events", _normalized_sql(_UPDATE_TRIGGER_SQL)),
    ("trigger", "tc_rollout_events_immutable_delete"): ("tc_rollout_events", _normalized_sql(_DELETE_TRIGGER_SQL)),
})


@dataclass(frozen=True)
class RolloutHead:
    """The newest event's position and hash: the value an external anchor keeps."""

    sequence: int
    event_hash: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ContractError("head sequence must be a positive integer")
        if not isinstance(self.event_hash, str) or _HASH.fullmatch(self.event_hash) is None:
            raise ContractError("head event_hash must be a lowercase sha256 hex digest")


class _ChainState:
    """Commissioning state replayed from events, and the one transition rule set.

    The same :meth:`check` guards a new append and re-validates every stored
    event during verification, so durable history can never hold a transition
    the writer would have refused.
    """

    def __init__(self) -> None:
        self.events: list[RolloutEvent] = []
        self.latest: dict[RolloutCapability, RolloutEvent] = {}
        self.by_nonce: dict[str, RolloutEvent] = {}
        self.max_version: dict[RolloutCapability, int] = {}
        self.attestation_ids: set[str] = set()

    @property
    def head(self) -> RolloutEvent | None:
        return self.events[-1] if self.events else None

    def active(self, capability: RolloutCapability) -> bool:
        event = self.latest.get(capability)
        return event is not None and event.action is RolloutAction.ACTIVATE

    def active_attestation(self, capability: RolloutCapability) -> CommissioningAttestation | None:
        return self.latest[capability].attestation if self.active(capability) else None

    def check(self, event: RolloutEvent) -> None:
        head = self.head
        if event.sequence != len(self.events) + 1:
            raise RolloutError("rollout event sequence is not contiguous")
        if event.prior_event_hash != (None if head is None else head.event_hash):
            raise RolloutError("rollout event does not extend the chain head")
        if event.nonce in self.by_nonce:
            raise RolloutConflict("commissioning nonce is already recorded")
        if head is not None and event.occurred_at < head.occurred_at:
            raise RolloutError("rollout event time precedes the prior event")
        capability = event.capability
        if event.action is RolloutAction.ACTIVATE:
            attestation = event.attestation
            if attestation is None:  # unreachable through the RolloutEvent contract
                raise RolloutError("activation requires a commissioning attestation")
            if self.active(capability):
                raise RolloutConflict(f"{capability.value} is already active")
            missing = tuple(
                item for item in ROLLOUT_ORDER
                if item in PREREQUISITES[capability] and not self.active(item)
            )
            if missing:
                raise RolloutError(
                    f"{capability.value} requires active prerequisites: "
                    + ", ".join(item.value for item in missing)
                )
            if attestation.version <= self.max_version.get(capability, 0):
                raise RolloutError(
                    f"{capability.value} attestation version must exceed every "
                    "previously commissioned version"
                )
            if attestation.attestation_id in self.attestation_ids:
                raise RolloutError("a commissioning attestation can never be reused")
        else:
            if not self.active(capability):
                raise RolloutError(f"{capability.value} is not active")
            dependents = tuple(
                item for item in ROLLOUT_ORDER
                if item in DEPENDENTS[capability] and self.active(item)
            )
            if dependents:
                raise RolloutError(
                    f"{capability.value} cannot be rolled back while dependents are active: "
                    + ", ".join(item.value for item in dependents)
                )

    def apply(self, event: RolloutEvent) -> None:
        self.events.append(event)
        self.latest[event.capability] = event
        self.by_nonce[event.nonce] = event
        if event.attestation is not None:
            self.max_version[event.capability] = event.attestation.version
            self.attestation_ids.add(event.attestation.attestation_id)


def _replay_view(event: RolloutEvent, *, include_time: bool) -> bytes:
    """Canonical bytes of every caller-controlled input an event binds."""
    document = event.to_document()
    del document["sequence"], document["prior_event_hash"]
    if not include_time:
        del document["occurred_at"]
    return canonical_json(document)


class RolloutLedger:
    """Durable, append-only Phase 7 commissioning state in the shared task DB.

    All defaults are inert: an empty ledger reports every capability inactive and
    routes to the legacy loop, and with no ``expected_attestations`` configured
    nothing can be activated or evaluated as permitted.  The ledger executes
    nothing; it only records and reads commissioning truth for a later runtime to
    consult.

    Every public read and every append first re-verifies the schema objects and
    the whole event chain and then derives its answer from the verified, parsed
    events, never from the unauthenticated SQL index columns.  Appends verify
    inside the same write transaction that inserts.  Any failure raises
    :class:`RolloutTampered` (a :class:`RolloutError`); callers must treat any
    ``RolloutError`` as "not permitted / legacy routing".

    ``expected_attestations`` is the deployment's trusted, injected pin: the exact
    attestation hash expected per capability.  ``expected_head`` is an optional
    trusted external anchor; when given, the verified chain must contain it.
    ``require_exact_head`` additionally rejects any suffix and makes the ledger
    instance read-only for runtime authority decisions.
    """

    def __init__(
        self,
        tasks: TaskStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        expected_attestations: Mapping[RolloutCapability, str] | None = None,
        expected_head: RolloutHead | None = None,
        require_exact_head: bool = False,
        max_clock_skew: timedelta = DEFAULT_MAX_CLOCK_SKEW,
        evidence_verifier: Any = None,
    ):
        pins: dict[RolloutCapability, str] = {}
        if expected_attestations is not None:
            if not isinstance(expected_attestations, Mapping):
                raise ContractError("expected_attestations must map capabilities to hashes")
            for capability, pin in expected_attestations.items():
                if not isinstance(capability, RolloutCapability):
                    raise ContractError("expected attestation keys must be typed capabilities")
                if not isinstance(pin, str) or _HASH.fullmatch(pin) is None:
                    raise ContractError(
                        "an expected attestation must be a lowercase sha256 hex digest"
                    )
                pins[capability] = pin
        if expected_head is not None and not isinstance(expected_head, RolloutHead):
            raise ContractError("expected_head must be a typed RolloutHead")
        if type(require_exact_head) is not bool:
            raise ContractError("require_exact_head must be a boolean")
        if require_exact_head and expected_head is None:
            raise ContractError("require_exact_head needs an expected_head anchor")
        if (
            not isinstance(max_clock_skew, timedelta)
            or not timedelta(0) <= max_clock_skew <= MAX_CLOCK_SKEW_LIMIT
        ):
            raise ContractError(
                f"max_clock_skew must be between zero and {MAX_CLOCK_SKEW_LIMIT}"
            )
        self.tasks = tasks
        self.clock = clock
        self.expected_attestations: Mapping[RolloutCapability, str] = MappingProxyType(pins)
        self.expected_head = expected_head
        self.require_exact_head = require_exact_head
        self.max_clock_skew = max_clock_skew
        if evidence_verifier is not None:
            # Delayed import avoids the intentional one-way bridge import:
            # shadow contracts depend on RolloutCapability, while this ledger
            # accepts only the concrete verifier that imports both foundations.
            from .commissioning_evidence import CommissioningEvidenceVerifier
            if type(evidence_verifier) is not CommissioningEvidenceVerifier:
                raise ContractError(
                    "evidence_verifier must be a concrete CommissioningEvidenceVerifier"
                )
            if evidence_verifier.shadow.tasks is not tasks:
                raise ContractError(
                    "the evidence verifier must share this rollout TaskStore"
                )
        self.evidence_verifier = evidence_verifier
        self._migrate()

    # -- schema ---------------------------------------------------------------

    def _schema_objects(self) -> dict[tuple[str, str], tuple[str, str | None]]:
        rows = self.tasks.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE tbl_name IN ('tc_rollout_schema','tc_rollout_events') "
            "OR name IN ('tc_rollout_schema','tc_rollout_events',"
            "'tc_rollout_events_capability','tc_rollout_events_immutable_update',"
            "'tc_rollout_events_immutable_delete')"
        ).fetchall()
        return {
            (row["type"], row["name"]): (row["tbl_name"], _normalized_sql(row["sql"]))
            for row in rows
            # The implicit indexes behind PRIMARY KEY/UNIQUE are bound by the table SQL.
            if not str(row["name"]).startswith("sqlite_autoindex_")
        }

    def _verify_schema(self) -> None:
        objects = self._schema_objects()
        if ("table", "tc_rollout_schema") in objects:
            rows = self.tasks.db.execute("SELECT namespace,version FROM tc_rollout_schema").fetchall()
            versions = [row["version"] for row in rows]
            if len(rows) == 1 and type(versions[0]) is int and versions[0] > ROLLOUT_SCHEMA_VERSION:
                raise RolloutError(
                    f"rollout schema {versions[0]} is newer than supported schema "
                    f"{ROLLOUT_SCHEMA_VERSION}"
                )
            if (
                len(rows) != 1 or rows[0]["namespace"] != "rollout"
                or type(versions[0]) is not int or versions[0] != ROLLOUT_SCHEMA_VERSION
            ):
                raise RolloutTampered("rollout schema version record is missing or malformed")
        for key, expected in _EXPECTED_SCHEMA_OBJECTS.items():
            if key not in objects:
                raise RolloutTampered(f"rollout schema {key[0]} {key[1]} is missing")
            if objects[key] != expected:
                raise RolloutTampered(f"rollout schema {key[0]} {key[1]} is malformed")
        unexpected = sorted(name for _, name in set(objects) - set(_EXPECTED_SCHEMA_OBJECTS))
        if unexpected:
            raise RolloutTampered(
                "unexpected schema objects on rollout tables: " + ", ".join(unexpected)
            )

    def _migrate(self) -> None:
        with self.tasks.transaction():
            # Only a database with no rollout object at all is fresh.  Anything
            # partial (a dropped trigger, table, index, or version row) is never
            # repaired or trusted; it fails closed below.
            if not self._schema_objects():
                for statement in (
                    _SCHEMA_TABLE_SQL, _EVENTS_TABLE_SQL, _EVENTS_INDEX_SQL,
                    _UPDATE_TRIGGER_SQL, _DELETE_TRIGGER_SQL,
                ):
                    self.tasks.db.execute(statement)
                self.tasks.db.execute(
                    "INSERT INTO tc_rollout_schema(namespace,version) VALUES('rollout',?)",
                    (ROLLOUT_SCHEMA_VERSION,),
                )
            self._load_verified()

    # -- verification ---------------------------------------------------------

    def _now(self) -> datetime:
        return _utc(self.clock(), "rollout clock")

    def _load_verified(self, expected_head: RolloutHead | None = None) -> _ChainState:
        """Verify schema and chain fail-closed; return state built from events."""
        if expected_head is not None and not isinstance(expected_head, RolloutHead):
            raise ContractError("expected_head must be a typed RolloutHead")
        try:
            self._verify_schema()
            rows = self.tasks.db.execute(
                "SELECT sequence,event_hash,prior_event_hash,capability,action,nonce,"
                "occurred_utc,recorded_utc,event_json FROM tc_rollout_events ORDER BY sequence"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise RolloutError("durable rollout state could not be read") from error
        state = _ChainState()
        for row in rows:
            raw = row["event_json"]
            try:
                if not isinstance(raw, bytes):
                    raise ContractError("event_json must be stored as bytes")
                event = RolloutEvent.from_json(raw)
                parse_utc(row["recorded_utc"], "recorded_utc")
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                # ContractError is a ValueError; every corrupt-row shape lands here.
                raise RolloutTampered("a stored rollout event is corrupt") from error
            if raw != event.canonical_json():
                raise RolloutTampered("stored event JSON is not canonical")
            # Every duplicated SQL column must equal the hash-bound content, with
            # exact types, so a column edit cannot change authority or lookups.
            columns = {
                "sequence": event.sequence, "event_hash": event.event_hash,
                "prior_event_hash": event.prior_event_hash,
                "capability": event.capability.value, "action": event.action.value,
                "nonce": event.nonce, "occurred_utc": utc_text(event.occurred_at),
            }
            for column, bound in columns.items():
                stored = row[column]
                if type(stored) is not type(bound) or stored != bound:
                    raise RolloutTampered(f"stored {column} disagrees with event content")
            try:
                state.check(event)
            except RolloutError as error:
                raise RolloutTampered(f"rollout history is invalid: {error}") from error
            state.apply(event)
        for anchor in (self.expected_head, expected_head):
            if anchor is None:
                continue
            if (
                anchor.sequence > len(state.events)
                or state.events[anchor.sequence - 1].event_hash != anchor.event_hash
            ):
                raise RolloutTampered(
                    "rollout chain does not contain the trusted expected head"
                )
        if self.require_exact_head and (
            self.expected_head is None
            or len(state.events) != self.expected_head.sequence
            or state.events[-1].event_hash != self.expected_head.event_hash
        ):
            raise RolloutTampered("rollout chain grew beyond the trusted exact head")
        head = state.head
        if head is not None and head.occurred_at > self._now() + self.max_clock_skew:
            raise RolloutError(
                "rollout history is ahead of the trusted clock beyond the permitted skew"
            )
        return state

    def _pin_matches(self, state: _ChainState, capability: RolloutCapability) -> bool:
        attestation = state.active_attestation(capability)
        pin = self.expected_attestations.get(capability)
        return attestation is not None and pin is not None and attestation.attestation_id == pin

    def _prerequisites_satisfied(self, state: _ChainState, capability: RolloutCapability) -> bool:
        return all(self._pin_matches(state, item) for item in PREREQUISITES[capability])

    def _evidence_result(
        self, attestation: CommissioningAttestation | None,
        verified: Mapping[str, Any] | None = None,
        batch_error: str | None = None,
    ) -> tuple[bool, str | None]:
        if attestation is None:
            return False, "capability has no active commissioning attestation"
        if self.evidence_verifier is None:
            return False, "no commissioning evidence verifier is configured"
        from .commissioning_evidence import (
            CommissioningEvidenceError,
            VerifiedCommissioningEvidence,
        )
        if verified is None:
            try:
                result = self.evidence_verifier.verify(attestation)
            except (CommissioningEvidenceError, ContractError) as error:
                return False, str(error)
        else:
            result = verified.get(attestation.attestation_id)
            if result is None:
                return False, batch_error or "commissioning evidence batch is incomplete"
        if type(result) is not VerifiedCommissioningEvidence:
            return False, "commissioning evidence verifier returned an invalid result"
        if (
            result.attestation_id != attestation.attestation_id
            or result.capability is not attestation.capability
            or result.machine_id != attestation.machine_id
            or result.policy_revision != attestation.policy_revision
            or result.config_revision != attestation.config_revision
            or result.evidence_revision != attestation.evidence_revision
        ):
            return False, "verified commissioning evidence bindings do not match"
        return True, None

    def _evidence_batch(
        self, attestations: tuple[CommissioningAttestation | None, ...],
    ) -> tuple[Mapping[str, Any], str | None]:
        """Verify unique active attestations with one shadow-ledger scan."""
        if self.evidence_verifier is None:
            return MappingProxyType({}), "no commissioning evidence verifier is configured"
        unique: list[CommissioningAttestation] = []
        seen: set[str] = set()
        for attestation in attestations:
            if attestation is None or attestation.attestation_id in seen:
                continue
            seen.add(attestation.attestation_id)
            unique.append(attestation)
        if not unique:
            return MappingProxyType({}), "capability has no active commissioning attestation"
        from .commissioning_evidence import CommissioningEvidenceError
        try:
            results = self.evidence_verifier.verify_many(tuple(unique))
        except (CommissioningEvidenceError, ContractError) as error:
            return MappingProxyType({}), str(error)
        return MappingProxyType({item.attestation_id: item for item in results}), None

    def _prerequisite_evidence_satisfied(
        self, state: _ChainState, capability: RolloutCapability,
        verified: Mapping[str, Any] | None = None,
        batch_error: str | None = None,
    ) -> bool:
        return all(
            self._pin_matches(state, item)
            and self._evidence_result(
                state.active_attestation(item), verified, batch_error,
            )[0]
            for item in PREREQUISITES[capability]
        )

    def _usable(
        self, state: _ChainState, capability: RolloutCapability,
        verified: Mapping[str, Any] | None = None,
        batch_error: str | None = None,
    ) -> bool:
        return (
            self._pin_matches(state, capability)
            and self._evidence_result(
                state.active_attestation(capability), verified, batch_error,
            )[0]
            and self._prerequisite_evidence_satisfied(
                state, capability, verified, batch_error,
            )
        )

    @contextmanager
    def _authority_state(self) -> Iterator[_ChainState]:
        """Hold one SQLite read snapshot across history and evidence checks."""
        if self.tasks.db.in_transaction:
            raise RolloutError("authority evaluation requires a fresh database snapshot")
        try:
            self.tasks.db.execute("BEGIN")
            yield self._load_verified()
        finally:
            if self.tasks.db.in_transaction:
                self.tasks.db.rollback()

    # -- mutation -------------------------------------------------------------

    def _commit(
        self, draft: RolloutEvent, *, explicit_time: bool,
        guard: Callable[[_ChainState], None],
    ) -> RolloutEvent:
        if self.require_exact_head:
            raise RolloutError(
                "exact-head mode is read-only; use a commissioning writer and publish a new anchor"
            )
        with self.tasks.transaction():
            state = self._load_verified()
            existing = state.by_nonce.get(draft.nonce)
            if existing is not None:
                if _replay_view(existing, include_time=explicit_time) == _replay_view(
                    draft, include_time=explicit_time
                ):
                    guard(state)
                    return existing
                raise RolloutConflict("commissioning nonce was replayed with different content")
            trusted_now = self._now()
            if not explicit_time:
                head_time = None if state.head is None else state.head.occurred_at
                draft = dataclasses.replace(
                    draft,
                    occurred_at=(
                        trusted_now
                        if head_time is None or trusted_now >= head_time
                        else head_time
                    ),
                )
            if abs(draft.occurred_at - trusted_now) > self.max_clock_skew:
                raise RolloutError("event time is outside the permitted clock skew")
            head = state.head
            event = dataclasses.replace(
                draft, sequence=len(state.events) + 1,
                prior_event_hash=None if head is None else head.event_hash,
            )
            state.check(event)
            guard(state)
            self.tasks.db.execute(
                """INSERT INTO tc_rollout_events(
                     sequence,event_hash,prior_event_hash,capability,action,nonce,
                     occurred_utc,recorded_utc,event_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (event.sequence, event.event_hash, event.prior_event_hash,
                 event.capability.value, event.action.value, event.nonce,
                 utc_text(event.occurred_at), utc_text(trusted_now), event.canonical_json()),
            )
            return event

    def activate(
        self,
        capability: RolloutCapability,
        attestation: CommissioningAttestation,
        *,
        actor: str,
        nonce: str,
        reason: str,
        now: datetime | None = None,
    ) -> RolloutEvent:
        """Commission one capability, failing closed on any unsafe condition."""
        if not isinstance(capability, RolloutCapability):
            raise ContractError("capability must be a typed rollout capability")
        if not isinstance(attestation, CommissioningAttestation):
            raise ContractError("activation requires a commissioning attestation")
        if attestation.capability is not capability:
            raise RolloutError("attestation does not commission this capability")
        # Building the draft validates every caller input before any nonce lookup.
        draft = RolloutEvent(
            sequence=1, capability=capability, action=RolloutAction.ACTIVATE, actor=actor,
            policy_revision=attestation.policy_revision,
            config_revision=attestation.config_revision,
            evidence_revision=attestation.evidence_revision,
            occurred_at=_utc(now if now is not None else self.clock(), "activation time"),
            nonce=nonce, canary_evidence=attestation.canary_evidence, reason=reason,
            attestation=attestation, prior_event_hash=None,
        )

        def guard(state: _ChainState) -> None:
            pin = self.expected_attestations.get(capability)
            if pin is None:
                raise RolloutError(
                    f"no expected attestation is configured for {capability.value}"
                )
            if attestation.attestation_id != pin:
                raise RolloutError(
                    "attestation does not match the configured expected attestation"
                )
            prerequisite_attestations = tuple(
                state.active_attestation(item)
                for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]
            )
            verified, batch_error = self._evidence_batch(
                (attestation, *prerequisite_attestations)
            )
            evidence_ok, evidence_reason = self._evidence_result(
                attestation, verified, batch_error,
            )
            if not evidence_ok:
                raise RolloutError(
                    "commissioning evidence is not valid: " + str(evidence_reason)
                )
            unpinned = tuple(
                item for item in ROLLOUT_ORDER
                if item in PREREQUISITES[capability] and not self._pin_matches(state, item)
            )
            if unpinned:
                raise RolloutError(
                    f"{capability.value} prerequisites do not match their expected "
                    "attestations: " + ", ".join(item.value for item in unpinned)
                )
            invalid_evidence = tuple(
                item for item in ROLLOUT_ORDER
                if item in PREREQUISITES[capability]
                and not self._evidence_result(
                    state.active_attestation(item), verified, batch_error,
                )[0]
            )
            if invalid_evidence:
                raise RolloutError(
                    f"{capability.value} prerequisites lack valid commissioning evidence: "
                    + ", ".join(item.value for item in invalid_evidence)
                )

        return self._commit(draft, explicit_time=now is not None, guard=guard)

    def rollback(
        self,
        capability: RolloutCapability,
        *,
        actor: str,
        nonce: str,
        reason: str,
        policy_revision: str,
        config_revision: str,
        evidence_revision: str,
        canary_evidence: Mapping[str, Any],
        now: datetime | None = None,
    ) -> RolloutEvent:
        """Retire one capability back toward legacy routing, preserving history.

        Rollback never requires an expected attestation: retiring a capability
        must stay possible whatever the configured pins say.
        """
        if not isinstance(capability, RolloutCapability):
            raise ContractError("capability must be a typed rollout capability")
        draft = RolloutEvent(
            sequence=1, capability=capability, action=RolloutAction.ROLLBACK, actor=actor,
            policy_revision=policy_revision, config_revision=config_revision,
            evidence_revision=evidence_revision,
            occurred_at=_utc(now if now is not None else self.clock(), "rollback time"),
            nonce=nonce, canary_evidence=canary_evidence, reason=reason,
            attestation=None, prior_event_hash=None,
        )
        return self._commit(draft, explicit_time=now is not None, guard=lambda state: None)

    # -- read-only status / evaluation ---------------------------------------

    def is_active(self, capability: RolloutCapability) -> bool:
        """Whether verified history leaves the capability commissioned.

        This is durable truth only.  Authority decisions must use
        :meth:`evaluate` or :meth:`evaluate_effect`, which also require the
        configured expected attestations.
        """
        if not isinstance(capability, RolloutCapability):
            raise ContractError("capability must be a typed rollout capability")
        return self._load_verified().active(capability)

    def head(self) -> RolloutHead | None:
        """The verified head, for a trusted external anchor to record."""
        head = self._load_verified().head
        return None if head is None else RolloutHead(head.sequence, head.event_hash)

    def head_hash(self) -> str | None:
        head = self.head()
        return None if head is None else head.event_hash

    def events(self, capability: RolloutCapability | None = None) -> tuple[RolloutEvent, ...]:
        if capability is not None and not isinstance(capability, RolloutCapability):
            raise ContractError("capability must be a typed rollout capability")
        return tuple(
            event for event in self._load_verified().events
            if capability is None or event.capability is capability
        )

    def status(self) -> tuple[CapabilityStatus, ...]:
        state = self._load_verified()
        statuses = []
        for capability in ROLLOUT_ORDER:
            latest = state.latest.get(capability)
            attestation = state.active_attestation(capability)
            statuses.append(CapabilityStatus(
                capability=capability, active=state.active(capability),
                attestation_id=None if attestation is None else attestation.attestation_id,
                sequence=None if latest is None else latest.sequence,
                prerequisites=tuple(item for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]),
                prerequisites_satisfied=self._prerequisites_satisfied(state, capability),
                active_dependents=tuple(
                    item for item in ROLLOUT_ORDER
                    if item in DEPENDENTS[capability] and state.active(item)
                ),
                expected_attestation_id=self.expected_attestations.get(capability),
                expected_attestation_matches=self._pin_matches(state, capability),
            ))
        return tuple(statuses)

    def evaluate(
        self,
        capability: RolloutCapability,
        *,
        attestation: CommissioningAttestation | None = None,
        policy_revision: str | None = None,
        config_revision: str | None = None,
        evidence_revision: str | None = None,
    ) -> RolloutDecision:
        """Report, read-only, whether a capability is commissioned and usable.

        Fails closed.  The active commissioning must equal the configured
        expected attestation, and so must every prerequisite's.  If a caller
        presents an attestation, or the policy/config/evidence revision it is
        running under, each must match the active commissioning exactly.
        """
        if not isinstance(capability, RolloutCapability):
            raise ContractError("capability must be a typed rollout capability")
        if attestation is not None and not isinstance(attestation, CommissioningAttestation):
            raise ContractError("evaluation attestation must be typed")
        presented = {
            "policy_revision": policy_revision, "config_revision": config_revision,
            "evidence_revision": evidence_revision,
        }
        for name, value in presented.items():
            if value is not None:
                _identifier(value, name)
        with self._authority_state() as state:
            active_attestation = state.active_attestation(capability)
            relevant = (capability, *tuple(
                item for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]
            ))
            verified, batch_error = self._evidence_batch(tuple(
                state.active_attestation(item) for item in relevant
            ))
            active = active_attestation is not None
            prerequisites_satisfied = self._prerequisites_satisfied(state, capability)
            prerequisite_evidence = self._prerequisite_evidence_satisfied(
                state, capability, verified, batch_error,
            )
            expected_matches = self._pin_matches(state, capability)
            evidence_matches, evidence_reason = self._evidence_result(
                active_attestation, verified, batch_error,
            )
            reasons: list[str] = []
            if not active:
                reasons.append("capability is not commissioned")
            if not prerequisites_satisfied:
                reasons.append("a prerequisite capability is not active under its expected attestation")
            if not prerequisite_evidence:
                reasons.append("a prerequisite capability lacks valid commissioning evidence")
            if capability not in self.expected_attestations:
                reasons.append("no expected attestation is configured")
            elif not expected_matches:
                reasons.append("active commissioning does not match the configured expected attestation")
            if not evidence_matches:
                reasons.append("commissioning evidence is not valid: " + str(evidence_reason))
            attestation_matches: bool | None = None
            if attestation is not None:
                attestation_matches = (
                    attestation.capability is capability
                    and active_attestation is not None
                    and attestation.attestation_id == active_attestation.attestation_id
                )
                if not attestation_matches:
                    reasons.append("presented attestation does not match the active commissioning")
            revisions_match: bool | None = None
            if any(value is not None for value in presented.values()):
                revisions_match = active_attestation is not None and all(
                    value is None or value == getattr(active_attestation, name)
                    for name, value in presented.items()
                )
                if not revisions_match:
                    reasons.append("presented revisions do not match the active commissioning")
            permitted = (
                active and prerequisites_satisfied and prerequisite_evidence
                and expected_matches and evidence_matches
                and attestation_matches is not False and revisions_match is not False
            )
            return RolloutDecision(
                capability=capability, active=active,
                prerequisites_satisfied=prerequisites_satisfied,
                attestation_matches=attestation_matches, permitted=permitted,
                reasons=tuple(reasons), expected_attestation_matches=expected_matches,
                revisions_match=revisions_match, evidence_matches=evidence_matches,
                prerequisite_evidence_matches=prerequisite_evidence,
            )

    def evaluate_effect(self, effect: Effect) -> EffectDecision:
        """Report, read-only, whether every capability an effect needs is usable."""
        required, ungoverned = classify_effect(effect)
        with self._authority_state() as state:
            ordered_required = tuple(item for item in ROLLOUT_ORDER if item in required)
            relevant = tuple(
                item for item in ROLLOUT_ORDER
                if item in required or any(item in PREREQUISITES[target] for target in required)
            )
            verified, batch_error = self._evidence_batch(tuple(
                state.active_attestation(item) for item in relevant
            ))
            missing = tuple(
                item for item in ordered_required
                if not self._usable(state, item, verified, batch_error)
            )
            reasons: list[str] = []
            if ungoverned:
                reasons.append(
                    "effect carries classes Phase 7 never commissions as a capability: "
                    + ", ".join(sorted(ungoverned))
                )
            if missing:
                reasons.append(
                    "capabilities not commissioned with current pinned semantic evidence: "
                    + ", ".join(item.value for item in missing)
                )
            permitted = not ungoverned and not missing
            return EffectDecision(
                permitted=permitted, required_capabilities=ordered_required,
                missing_capabilities=missing, ungoverned_effects=tuple(sorted(ungoverned)),
                reasons=tuple(reasons),
            )

    def standing_consent_active(self) -> bool:
        """Standing consent covers only the reversible owned-component class."""
        with self._authority_state() as state:
            capability = RolloutCapability.OWNED_COMPONENT_STANDING_CONSENT
            relevant = (capability, *tuple(
                item for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]
            ))
            verified, batch_error = self._evidence_batch(tuple(
                state.active_attestation(item) for item in relevant
            ))
            return self._usable(state, capability, verified, batch_error)

    def detector_tasks_active(self) -> bool:
        with self._authority_state() as state:
            capability = RolloutCapability.DETECTOR_CREATED_TASKS
            relevant = (capability, *tuple(
                item for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]
            ))
            verified, batch_error = self._evidence_batch(tuple(
                state.active_attestation(item) for item in relevant
            ))
            return self._usable(state, capability, verified, batch_error)

    def routing_target(self) -> str:
        """``'phase7'`` once legacy retirement is usable, otherwise ``'legacy'``.

        Raises :class:`RolloutError` when durable state cannot be verified; the
        caller must route to the legacy loop in that case.
        """
        with self._authority_state() as state:
            capability = RolloutCapability.LEGACY_LOOP_RETIREMENT
            relevant = (capability, *tuple(
                item for item in ROLLOUT_ORDER if item in PREREQUISITES[capability]
            ))
            verified, batch_error = self._evidence_batch(tuple(
                state.active_attestation(item) for item in relevant
            ))
            usable = self._usable(state, capability, verified, batch_error)
            return "phase7" if usable else "legacy"

    def verify_chain(self, *, expected_head: RolloutHead | None = None) -> int:
        """Re-verify schema and chain end to end; raise on any tamper or gap.

        Returns the number of verified events.  Verification detects any edit,
        column/JSON disagreement, gap, fork, reorder, interior deletion, or
        impossible transition.  It can NOT by itself detect an attacker with raw
        database access deleting the newest events (or the whole ledger): a
        truncated chain is still a valid chain.  Only a trusted external anchor
        closes that gap; pass the externally recorded head as ``expected_head``
        (or configure it on the ledger) and the verified chain must contain it.
        """
        return len(self._load_verified(expected_head).events)
