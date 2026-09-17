"""Deterministic policy types and checks for disruptive actions.

This module deliberately contains no transport, shell, or credential handling.  It
turns typed, bounded facts into an eligibility decision; :mod:`actions` owns the
durable approval and dispatch protocol.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


MACHINE_ID = 17049
DEPLOYMENT_APPROVAL_GROUP_ID = -1004484415005
APPROVAL_LIFETIME = timedelta(minutes=5)
MAX_SOURCE_AGE = timedelta(seconds=60)
REPEAT_COOLDOWN = timedelta(minutes=30)
POWER_COOLDOWN = timedelta(minutes=60)
POWER_WINDOW = timedelta(hours=24)
POWER_WINDOW_LIMIT = 2

_SECRET_MATERIAL = re.compile(
    r"(?i)(?:\b(?:authorization|credential|password|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]|"
    r"\b(?:bearer|basic)\s+\S+|https?://[^/\s@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

_FORBIDDEN_PARAMETER_KEYS = frozenset({
    "accesskey",
    "apikey",
    "argv",
    "authorization",
    "authorizationurl",
    "authorizationurls",
    "authurl",
    "authurls",
    "command",
    "credential",
    "credentials",
    "password",
    "privatekey",
    "script",
    "secret",
    "shell",
    "token",
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def utc_text(value: datetime) -> str:
    return require_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return require_utc(parsed, "timestamp")


class PolicyDenied(RuntimeError):
    """A deterministic policy gate rejected an operation."""


class Mode(str, Enum):
    OBSERVE = "observe"
    APPROVE = "approve"


class ActionClass(str, Enum):
    MONITOR_COMPONENT_RESTART = "monitor-component-restart"
    VAST_HOST_SERVICE_RESTART = "vast-host-service-restart"
    REMOVE_SELF_TEST_RESOURCE = "remove-self-test-resource"
    GPU_RESET_REBIND = "gpu-reset-rebind"
    HOST_REBOOT = "host-reboot"
    BMC_POWER = "bmc-power"
    SELF_TEST_RENTAL = "self-test-rental"
    LISTING_CHANGE = "listing-change"
    RENTAL_TERMINATION = "rental-termination"


POWER_ACTIONS = frozenset({ActionClass.HOST_REBOOT, ActionClass.BMC_POWER})


class Ownership(str, Enum):
    CONTROLLER = "controller"
    TENANT = "tenant"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceBinding:
    """The exact source revision shown in a proposal."""

    source: str
    revision: str

    def __post_init__(self) -> None:
        if not self.source or not self.revision:
            raise ValueError("source and revision are required")


@dataclass(frozen=True)
class SourceState:
    """A live source value collected immediately before an action."""

    source: str
    revision: str
    observed_at: datetime
    available: bool = True

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "observed_at")
        if not self.source or not self.revision:
            raise ValueError("source and revision are required")


@dataclass(frozen=True)
class RentalImpact:
    rental_id: str
    active: bool
    ownership: Ownership
    interruption_approved: bool = False

    def __post_init__(self) -> None:
        if not self.rental_id:
            raise ValueError("rental_id is required")


def _json_value(value: Any, path: str = "parameters") -> Any:
    """Copy a JSON value while denying command/secret-shaped capabilities."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError(f"{path} string is too long")
        if isinstance(value, str) and _SECRET_MATERIAL.search(value):
            raise ValueError(f"{path} appears to contain credential material")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _FORBIDDEN_PARAMETER_KEYS:
                raise ValueError(f"{path}.{key} is not an allowed action parameter")
            if not key or len(key) > 128:
                raise ValueError(f"{path} contains an invalid key")
            copied[key] = _json_value(item, f"{path}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ValueError(f"{path} contains too many values")
        return [_json_value(item, path) for item in value]
    raise ValueError(f"{path} must contain JSON values only")


def validated_action_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached JSON document after action-boundary validation."""

    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be an object")
    value = _json_value(parameters)
    if not isinstance(value, dict):  # Kept explicit for type checkers and subclasses.
        raise ValueError("parameters must be an object")
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class ActionProposal:
    """An immutable exact action request; the digest is what a human approves."""

    proposal_id: str
    action_class: ActionClass
    parameters: Mapping[str, Any]
    resource_ids: tuple[str, ...]
    rental_impacts: tuple[RentalImpact, ...]
    affected_domains: tuple[str, ...]
    source_bindings: tuple[SourceBinding, ...]
    evidence_revision: str
    policy_revision: str
    stop_condition: str
    created_at: datetime
    expires_at: datetime
    machine_id: int = MACHINE_ID
    mappings_known: bool = False
    power_domain_proven: bool = False
    claims_cold_gpu_power: bool = False

    def __post_init__(self) -> None:
        require_utc(self.created_at, "created_at")
        require_utc(self.expires_at, "expires_at")
        if not isinstance(self.action_class, ActionClass):
            raise ValueError("action_class must be an ActionClass")
        if self.machine_id != MACHINE_ID:
            raise ValueError(f"actions are restricted to machine {MACHINE_ID}")
        if (
            not self.proposal_id
            or not self.evidence_revision
            or not self.policy_revision
            or not self.stop_condition.strip()
        ):
            raise ValueError("proposal, evidence, policy, and stop condition are required")
        if len(self.stop_condition) > 1024:
            raise ValueError("stop_condition is too long")
        if self.expires_at <= self.created_at:
            raise ValueError("proposal expiry must follow creation")
        if self.expires_at - self.created_at > APPROVAL_LIFETIME:
            raise ValueError("proposal lifetime may not exceed five minutes")
        safe_parameters = validated_action_parameters(self.parameters)
        if not safe_parameters:
            raise ValueError("exact action parameters are required")
        object.__setattr__(self, "parameters", _freeze_json(safe_parameters))
        for name, values in (
            ("resource_ids", self.resource_ids),
            ("affected_domains", self.affected_domains),
        ):
            if not values or any(not value for value in values):
                raise ValueError(f"{name} must be non-empty")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicates")
        if any(not isinstance(item, SourceBinding) for item in self.source_bindings):
            raise ValueError("source_bindings must contain SourceBinding values")
        source_names = [item.source for item in self.source_bindings]
        if len(set(source_names)) != len(source_names):
            raise ValueError("source bindings must be unique")
        if any(not isinstance(item, RentalImpact) for item in self.rental_impacts):
            raise ValueError("rental_impacts must contain RentalImpact values")
        rental_ids = [item.rental_id for item in self.rental_impacts]
        if len(set(rental_ids)) != len(rental_ids):
            raise ValueError("rental impacts must be unique")
        if self.claims_cold_gpu_power and not self.power_domain_proven:
            raise ValueError("cold GPU power requires proven power-domain mapping")

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        action_class: ActionClass,
        parameters: Mapping[str, Any],
        resource_ids: Sequence[str],
        rental_impacts: Sequence[RentalImpact],
        affected_domains: Sequence[str],
        source_bindings: Sequence[SourceBinding],
        evidence_revision: str,
        policy_revision: str,
        stop_condition: str,
        clock: Callable[[], datetime] = utc_now,
        **mapping_flags: Any,
    ) -> "ActionProposal":
        created = require_utc(clock(), "clock")
        return cls(
            proposal_id=proposal_id,
            action_class=action_class,
            parameters=parameters,
            resource_ids=tuple(resource_ids),
            rental_impacts=tuple(rental_impacts),
            affected_domains=tuple(affected_domains),
            source_bindings=tuple(source_bindings),
            evidence_revision=evidence_revision,
            policy_revision=policy_revision,
            stop_condition=stop_condition,
            created_at=created,
            expires_at=created + APPROVAL_LIFETIME,
            **mapping_flags,
        )

    def exact_document(self) -> dict[str, Any]:
        return {
            "action_class": self.action_class.value,
            "affected_domains": list(self.affected_domains),
            "claims_cold_gpu_power": self.claims_cold_gpu_power,
            "created_at": utc_text(self.created_at),
            "evidence_revision": self.evidence_revision,
            "expires_at": utc_text(self.expires_at),
            "machine_id": self.machine_id,
            "mappings_known": self.mappings_known,
            # Always return a detached mutable JSON document. Mutating this view cannot
            # alter the deeply frozen proposal supplied to adapters or its digest.
            "parameters": _thaw_json(self.parameters),
            "policy_revision": self.policy_revision,
            "power_domain_proven": self.power_domain_proven,
            "proposal_id": self.proposal_id,
            "rental_impacts": [
                {
                    "active": item.active,
                    "interruption_approved": item.interruption_approved,
                    "ownership": item.ownership.value,
                    "rental_id": item.rental_id,
                }
                for item in self.rental_impacts
            ],
            "resource_ids": list(self.resource_ids),
            "source_bindings": [
                {"revision": item.revision, "source": item.source}
                for item in self.source_bindings
            ],
            "stop_condition": self.stop_condition,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.exact_document()).encode()).hexdigest()


@dataclass(frozen=True)
class PreActionEvidence:
    """Facts independently recaptured directly before reservation/dispatch."""

    machine_id: int
    target_identity_verified: bool
    evidence_revision: str
    sources: tuple[SourceState, ...]
    resource_ids: tuple[str, ...]
    rental_impacts: tuple[RentalImpact, ...]
    affected_domains: tuple[str, ...]
    mappings_known: bool
    power_domain_proven: bool
    evidence_ref: str | None
    backup_ref: str | None
    backup_succeeded: bool


@dataclass(frozen=True)
class ActionRule:
    required_sources: frozenset[str]
    mapping_required: bool = False
    power_domain_required: bool = False


DEFAULT_RULES: Mapping[ActionClass, ActionRule] = MappingProxyType({
    ActionClass.MONITOR_COMPONENT_RESTART: ActionRule(frozenset({"host", "rentals"})),
    ActionClass.VAST_HOST_SERVICE_RESTART: ActionRule(frozenset({"host", "rentals"})),
    ActionClass.REMOVE_SELF_TEST_RESOURCE: ActionRule(frozenset({"host", "rentals"})),
    ActionClass.GPU_RESET_REBIND: ActionRule(
        frozenset({"host", "inventory", "rentals"}), mapping_required=True
    ),
    ActionClass.HOST_REBOOT: ActionRule(frozenset({"host", "bmc", "rentals"})),
    # SSH/host evidence is intentionally not required for a host-down BMC action.
    ActionClass.BMC_POWER: ActionRule(
        frozenset({"bmc", "rentals"}), mapping_required=True, power_domain_required=True
    ),
    ActionClass.SELF_TEST_RENTAL: ActionRule(frozenset({"inventory", "rentals"})),
    ActionClass.LISTING_CHANGE: ActionRule(frozenset({"rentals", "vast"})),
    ActionClass.RENTAL_TERMINATION: ActionRule(frozenset({"rentals", "vast"})),
})


@dataclass(frozen=True)
class ActionPolicy:
    """Commissioned policy configuration. Defaults cannot execute an action."""

    mode: Mode = Mode.OBSERVE
    revision: str = "uncommissioned"
    enabled_actions: frozenset[ActionClass] = field(default_factory=frozenset)
    # Classes the controller may carry out on its own: reversible work on monitoring it
    # installed, which touches no tenant. Everything else needs an exact human approval.
    self_service_actions: frozenset[ActionClass] = field(default_factory=frozenset)
    approval_group_id: int = DEPLOYMENT_APPROVAL_GROUP_ID
    rules: Mapping[ActionClass, ActionRule] = field(default_factory=lambda: DEFAULT_RULES)
    max_source_age: timedelta = MAX_SOURCE_AGE

    def self_service(self, action_class: ActionClass) -> bool:
        """Whether this class may run without a human approval."""
        return action_class in self.self_service_actions and action_class in self.enabled_actions

    def validate_proposal(self, proposal: ActionProposal, now: datetime) -> None:
        now = require_utc(now, "clock")
        if self.mode is not Mode.APPROVE:
            raise PolicyDenied("action policy is observe-only")
        if self.self_service_actions - self.enabled_actions:
            raise PolicyDenied("a self-service class must also be commissioned")
        if proposal.action_class not in self.enabled_actions:
            raise PolicyDenied("action class is not commissioned")
        if proposal.action_class not in self.rules:
            raise PolicyDenied("action class has no configured policy rule")
        if proposal.machine_id != MACHINE_ID:
            raise PolicyDenied("target machine identity mismatch")
        if proposal.policy_revision != self.revision:
            raise PolicyDenied("policy revision changed")
        if now < proposal.created_at:
            raise PolicyDenied("proposal is not yet current")
        if now >= proposal.expires_at:
            raise PolicyDenied("proposal expired")
        if any(item.ownership is Ownership.UNKNOWN for item in proposal.rental_impacts):
            raise PolicyDenied("rental ownership is unknown")
        if any(item.active and not item.interruption_approved for item in proposal.rental_impacts):
            raise PolicyDenied("active rental impact was not explicitly approved")
        rule = self.rules[proposal.action_class]
        bound_sources = {item.source for item in proposal.source_bindings}
        if not rule.required_sources.issubset(bound_sources):
            raise PolicyDenied("proposal lacks a required evidence source")
        if rule.mapping_required and not proposal.mappings_known:
            raise PolicyDenied("required affected-domain mapping is unknown")
        if rule.power_domain_required and not proposal.power_domain_proven:
            raise PolicyDenied("power-domain scope is not proven")
        if proposal.claims_cold_gpu_power and not proposal.power_domain_proven:
            raise PolicyDenied("cold GPU power is not proven")

    def validate_preconditions(
        self,
        proposal: ActionProposal,
        evidence: PreActionEvidence,
        now: datetime,
        *,
        backup_exception: bool = False,
    ) -> None:
        self.validate_proposal(proposal, now)
        if evidence.machine_id != MACHINE_ID or not evidence.target_identity_verified:
            raise PolicyDenied("immediate target identity verification failed")
        if evidence.evidence_revision != proposal.evidence_revision:
            raise PolicyDenied("evidence revision changed")
        if set(evidence.resource_ids) != set(proposal.resource_ids):
            raise PolicyDenied("affected resources changed")
        if set(evidence.affected_domains) != set(proposal.affected_domains):
            raise PolicyDenied("affected domains changed")
        if evidence.rental_impacts != proposal.rental_impacts:
            raise PolicyDenied("rental identity, ownership, or state changed")
        if any(item.ownership is Ownership.UNKNOWN for item in evidence.rental_impacts):
            raise PolicyDenied("current rental ownership is unknown")
        if not evidence.evidence_ref:
            raise PolicyDenied("pre-action evidence was not preserved")
        if (
            not evidence.backup_succeeded
            and not backup_exception
            # A reversible restart of our own monitoring is not worth a 20-minute wait
            # for a backup; its evidence is still preserved by the ordinary cycle.
            and not self.self_service(proposal.action_class)
        ):
            raise PolicyDenied("pre-action evidence backup did not succeed")
        if evidence.backup_succeeded and not evidence.backup_ref:
            raise PolicyDenied("successful backup lacks an evidence reference")
        rule = self.rules[proposal.action_class]
        if rule.mapping_required and not evidence.mappings_known:
            raise PolicyDenied("current affected-domain mapping is unknown")
        if rule.power_domain_required and not evidence.power_domain_proven:
            raise PolicyDenied("current power-domain scope is not proven")

        by_source = {item.source: item for item in evidence.sources}
        proposed = {item.source: item.revision for item in proposal.source_bindings}
        for source in rule.required_sources:
            state = by_source.get(source)
            if state is None or not state.available:
                raise PolicyDenied(f"required source unavailable: {source}")
            age = require_utc(now, "clock") - state.observed_at
            if age < timedelta(0) or age > self.max_source_age:
                raise PolicyDenied(f"required source stale: {source}")
            if proposed.get(source) != state.revision:
                raise PolicyDenied(f"required source changed: {source}")
