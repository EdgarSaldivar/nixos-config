"""Disabled Phase 5 typed verification and recovery foundation.

This module has no probes, transports, subprocesses, credentials, scheduler, or
runtime wiring.  A commissioned controller must inject an isolated read-only
source and authenticator.  The source receives verification declarations and
immutable execution bindings, never worker output or a worker success value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence

from .plans import (
    MACHINE_ID, ContractError, Plan, PlanStep, Verification,
    VerificationStatus, canonical_json, parse_utc, stable_hash, strict_json_loads,
    utc_text,
)
from .step_executor import (
    MAX_OUTPUT_BYTES, VERIFIER_CONTRACT, ExecutionBlocked, StepExecutor, StepRequest,
)
from .tasks import TaskStore

FEATURE_FLAG_ENV = "TERRACOMPUTE_INDEPENDENT_VERIFICATION"
SOURCE_CONTRACT = (
    "postcondition-source-v2:exact-attempt-binding,external-process,independent-of-step-worker,read-only,"
    "authenticated-evidence,bounded,hard-deadline,no-worker-results,no-tenant-payload,"
    "no-write-credentials,machine-17049"
)
AUTHENTICATOR_CONTRACT = (
    "postcondition-authenticator-v2:controller-trust-root,pinned-source-identity,"
    "exact-canonical-result-digest,task-plan-step-lease-phase-result-time,"
    "evidence-integrity,no-worker-success"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BREAKER_FIELDS = {
    "schema_version", "event_number", "machine_id", "state", "task_id",
    "plan_hash", "lease_hash", "reason", "occurred_at", "approval_identity",
}


class VerificationFoundationError(ExecutionBlocked):
    """Typed verification input or independent recovery policy failed closed."""


class PostconditionKind(str, Enum):
    MACHINE_REACHABILITY = "machine_reachability"
    NETWORK = "network"
    GPU_INVENTORY = "gpu_inventory"
    EXPORTER_HEALTH = "exporter_health"
    METRICS_CARDINALITY = "metrics_cardinality"
    VAST_AVAILABILITY = "vast_availability"
    RENTAL_CONTINUITY = "rental_continuity"
    CONTROLLER_HEALTH = "controller_health"


class EvidenceBasis(str, Enum):
    OBSERVATION = "observation"
    DEADLINE = "deadline"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise VerificationFoundationError(f"{name} must be a nonempty bounded string")
    if any(ord(character) < 32 for character in value):
        raise VerificationFoundationError(f"{name} contains a control character")
    return value


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VerificationFoundationError(f"{name} is outside its integer bounds")
    return value


_CONDITION_FIELDS = MappingProxyType({
    PostconditionKind.MACHINE_REACHABILITY: frozenset({"vantage", "protocol", "endpoint"}),
    PostconditionKind.NETWORK: frozenset({"interface", "expected_address", "expected_route"}),
    PostconditionKind.GPU_INVENTORY: frozenset({"expected_count", "expected_pci_ids"}),
    PostconditionKind.EXPORTER_HEALTH: frozenset({"exporter", "endpoint"}),
    PostconditionKind.METRICS_CARDINALITY: frozenset(
        {"metric", "label", "minimum", "maximum"}
    ),
    PostconditionKind.VAST_AVAILABILITY: frozenset({"expected_available"}),
    PostconditionKind.RENTAL_CONTINUITY: frozenset(
        {"rental_id", "generation", "expected_state"}
    ),
    PostconditionKind.CONTROLLER_HEALTH: frozenset({"service", "expected_revision"}),
})


@dataclass(frozen=True)
class Postcondition:
    """One strict desired-state declaration; it contains no executable operation."""

    kind: PostconditionKind
    max_age_seconds: int
    parameters: Mapping[str, object]
    machine_id: str = MACHINE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PostconditionKind):
            raise VerificationFoundationError("postcondition kind is unknown")
        if self.machine_id != MACHINE_ID:
            raise VerificationFoundationError("postcondition must bind machine 17049")
        _integer(self.max_age_seconds, "max_age_seconds", minimum=1, maximum=3600)
        if not isinstance(self.parameters, Mapping):
            raise VerificationFoundationError("postcondition parameters must be an object")
        values = dict(self.parameters)
        expected = _CONDITION_FIELDS[self.kind]
        if set(values) != expected:
            raise VerificationFoundationError("postcondition has missing or unknown fields")
        for name, value in values.items():
            if name in {"expected_count", "minimum", "maximum"}:
                _integer(value, name)
            elif name == "expected_available":
                if type(value) is not bool:
                    raise VerificationFoundationError("expected_available must be boolean")
            elif name == "expected_pci_ids":
                if (not isinstance(value, (list, tuple)) or len(value) > 64
                        or any(not isinstance(item, str) or not item for item in value)
                        or len(set(value)) != len(value)):
                    raise VerificationFoundationError("expected_pci_ids must be unique strings")
                values[name] = tuple(value)
            else:
                _text(value, name)
        if self.kind is PostconditionKind.GPU_INVENTORY:
            if values["expected_count"] != len(values["expected_pci_ids"]):
                raise VerificationFoundationError("GPU count and PCI inventory disagree")
        if self.kind is PostconditionKind.METRICS_CARDINALITY:
            if values["minimum"] > values["maximum"]:
                raise VerificationFoundationError("metrics cardinality bounds are inverted")
        object.__setattr__(self, "parameters", MappingProxyType(values))

    @property
    def content_hash(self) -> str:
        return stable_hash(self.to_document())

    def to_document(self) -> dict:
        values = {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in self.parameters.items()
        }
        return {
            "check": self.kind.value, "machine_id": self.machine_id,
            "max_age_seconds": self.max_age_seconds, **values,
        }

    @classmethod
    def from_document(cls, document: object) -> "Postcondition":
        if not isinstance(document, Mapping):
            raise VerificationFoundationError("postcondition must be an object")
        value = dict(document)
        try:
            kind = PostconditionKind(value.get("check"))
        except (TypeError, ValueError):
            raise VerificationFoundationError("postcondition kind is unknown") from None
        common = {"check", "machine_id", "max_age_seconds"}
        if set(value) != common | _CONDITION_FIELDS[kind]:
            raise VerificationFoundationError("postcondition has missing or unknown fields")
        return cls(
            kind=kind, machine_id=value["machine_id"],
            max_age_seconds=value["max_age_seconds"],
            parameters={name: value[name] for name in _CONDITION_FIELDS[kind]},
        )


@dataclass(frozen=True)
class PostconditionResult:
    """Authenticated digest-only evidence for one exact declaration."""

    task_id: str
    plan_id: str
    plan_hash: str
    step_id: str
    lease_id: str
    lease_hash: str
    phase: str
    result_at: datetime
    check: PostconditionKind
    condition_hash: str
    outcome: VerificationStatus
    evidence_revision: str
    source_id: str
    observed_at: datetime
    evidence_digest: str
    basis: EvidenceBasis = EvidenceBasis.OBSERVATION
    machine_id: str = MACHINE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.check, PostconditionKind):
            raise VerificationFoundationError("result check is unknown")
        if not isinstance(self.outcome, VerificationStatus):
            raise VerificationFoundationError("result outcome is unknown")
        if not isinstance(self.basis, EvidenceBasis):
            raise VerificationFoundationError("result basis is unknown")
        if self.machine_id != MACHINE_ID:
            raise VerificationFoundationError("result must bind machine 17049")
        for value, name in (
            (self.plan_hash, "plan_hash"), (self.lease_hash, "lease_hash"),
            (self.condition_hash, "condition_hash"),
            (self.evidence_digest, "evidence_digest"),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise VerificationFoundationError(f"{name} must be a SHA-256 digest")
        for name in ("task_id", "plan_id", "step_id", "lease_id"):
            _text(getattr(self, name), name)
        if self.phase not in {"forward", "rollback"}:
            raise VerificationFoundationError("result phase is invalid")
        try:
            object.__setattr__(self, "result_at", parse_utc(utc_text(self.result_at), "result_at"))
        except (ContractError, ValueError, TypeError, AttributeError):
            raise VerificationFoundationError("result_at must be UTC") from None
        _text(self.evidence_revision, "evidence_revision")
        _text(self.source_id, "source_id")
        try:
            canonical = parse_utc(utc_text(self.observed_at), "observed_at")
        except (ContractError, ValueError, TypeError, AttributeError):
            raise VerificationFoundationError("observed_at must be UTC") from None
        object.__setattr__(self, "observed_at", canonical)
        if self.basis is EvidenceBasis.DEADLINE and self.outcome is not VerificationStatus.FAILED:
            raise VerificationFoundationError("deadline evidence can only fail closed")

    @property
    def binding(self) -> dict:
        return {name: getattr(self, name) for name in (
            "task_id", "plan_id", "plan_hash", "step_id", "lease_id", "lease_hash",
            "phase", "result_at",
        )}

    @property
    def authentication_digest(self) -> str:
        """Trust-root authentication must cover this entire canonical envelope."""
        return stable_hash(self.to_document())

    def deadline_digest(self, deadline: datetime) -> str:
        document = self.to_document()
        document.pop("evidence_digest")
        return stable_hash({"result": document, "deadline": utc_text(deadline)})

    def to_document(self) -> dict:
        return {
            **self.binding, "result_at": utc_text(self.result_at),
            "check": self.check.value, "machine_id": self.machine_id,
            "condition_hash": self.condition_hash, "outcome": self.outcome.value,
            "evidence_revision": self.evidence_revision, "source_id": self.source_id,
            "observed_at": utc_text(self.observed_at),
            "evidence_digest": self.evidence_digest, "basis": self.basis.value,
        }

    @classmethod
    def from_document(cls, document: object) -> "PostconditionResult":
        fields = {
            "check", "machine_id", "condition_hash", "outcome", "evidence_revision",
            "source_id", "observed_at", "evidence_digest", "basis",
            "task_id", "plan_id", "plan_hash", "step_id", "lease_id", "lease_hash",
            "phase", "result_at",
        }
        if not isinstance(document, Mapping) or set(document) != fields:
            raise VerificationFoundationError("postcondition result shape is invalid")
        try:
            return cls(
                **{name: document[name] for name in (
                    "task_id", "plan_id", "plan_hash", "step_id", "lease_id", "lease_hash", "phase",
                )},
                result_at=parse_utc(document["result_at"], "result_at"),
                check=PostconditionKind(document["check"]),
                machine_id=document["machine_id"],
                condition_hash=document["condition_hash"],
                outcome=VerificationStatus(document["outcome"]),
                evidence_revision=document["evidence_revision"],
                source_id=document["source_id"],
                observed_at=parse_utc(document["observed_at"], "observed_at"),
                evidence_digest=document["evidence_digest"],
                basis=EvidenceBasis(document["basis"]),
            )
        except (KeyError, TypeError, ValueError, ContractError):
            raise VerificationFoundationError("postcondition result is invalid") from None


@dataclass(frozen=True)
class VerificationRequest:
    """Sanitized verifier input.  It intentionally has no command-result field."""

    task_id: str
    plan_id: str
    plan_hash: str
    step_id: str
    lease_id: str
    lease_hash: str
    phase: str
    evidence_revision: str
    result_at: datetime
    conditions: tuple[Postcondition, ...]
    max_verification_seconds: int
    recovery_deadline: datetime | None = None

    @property
    def binding(self) -> dict:
        return {name: getattr(self, name) for name in (
            "task_id", "plan_id", "plan_hash", "step_id", "lease_id", "lease_hash",
            "phase", "result_at",
        )}


def _binding(request: StepRequest) -> dict:
    lease = request.lease
    return dict(task_id=lease.task_id, plan_id=lease.plan_id, plan_hash=request.plan_hash,
                step_id=request.step.step_id, lease_id=lease.lease_id,
                lease_hash=lease.content_hash, phase=request.phase, result_at=request.result_at)


class PostconditionSource(Protocol):
    contract: str
    source_id: str
    credential_domains: frozenset[str]

    def observe(self, request: VerificationRequest) -> Sequence[PostconditionResult]: ...


class EvidenceAuthenticator(Protocol):
    contract: str

    def authenticate(self, result: PostconditionResult, authenticated_digest: str) -> bool:
        """Verify source authentication over the supplied canonical envelope digest."""
        ...


def _typed_conditions(step: PlanStep) -> tuple[Postcondition, ...]:
    return tuple(Postcondition.from_document(item) for item in step.postconditions)


def _recovery(step: PlanStep, conditions: tuple[Postcondition, ...]) -> datetime | None:
    if not any(effect.reachability for effect in step.effects):
        return None
    value = step.arguments.get("reachability_recovery")
    fields = {"staged_apply", "deadline", "health_confirmation", "out_of_band"}
    if not isinstance(value, Mapping) or set(value) != fields or value["staged_apply"] is not True:
        raise VerificationFoundationError("reachability change requires an exact staged apply")
    out_of_band = value["out_of_band"]
    if (not isinstance(out_of_band, Mapping)
            or set(out_of_band) != {"available", "method", "target"}
            or out_of_band["available"] is not True):
        raise VerificationFoundationError("reachability change requires out-of-band recovery")
    _text(out_of_band["method"], "out_of_band.method")
    target = _text(out_of_band["target"], "out_of_band.target")
    if re.fullmatch(r"[a-z][a-z0-9+.-]*:17049", target) is None:
        raise VerificationFoundationError("out-of-band recovery must bind machine 17049")
    try:
        deadline = parse_utc(value["deadline"], "reachability deadline")
    except (ContractError, TypeError, ValueError):
        raise VerificationFoundationError("reachability deadline is invalid") from None
    confirmation = value["health_confirmation"]
    expected = [condition.content_hash for condition in conditions]
    if not isinstance(confirmation, (list, tuple)) or list(confirmation) != expected:
        raise VerificationFoundationError("health confirmation must bind every typed postcondition")
    required = {PostconditionKind.MACHINE_REACHABILITY, PostconditionKind.NETWORK}
    if not required <= {condition.kind for condition in conditions}:
        raise VerificationFoundationError("reachability change needs reachability and network health checks")
    return deadline


class TypedIndependentVerifier:
    """Phase 4 verifier adapter that authenticates typed result documents."""

    contract = VERIFIER_CONTRACT

    def __init__(self, authenticator: EvidenceAuthenticator, *, source_id: str,
                 breaker: DurableVerificationBreaker):
        if getattr(authenticator, "contract", None) != AUTHENTICATOR_CONTRACT:
            raise VerificationFoundationError("independent evidence authenticator is required")
        self.authenticator = authenticator
        self.source_id = _text(source_id, "source_id")
        if source_id == "phase5-deadline":
            raise VerificationFoundationError("deadline identity is controller-reserved")
        self.breaker = breaker

    def check_executor(self, executor: StepExecutor) -> None:
        if executor.store is not self.breaker.store:
            raise VerificationFoundationError("verification and executor require one store")
        for gate, function in (
            (executor.verification_allowed, DurableVerificationBreaker.mutation_allowed),
            (executor.rollback_allowed, DurableVerificationBreaker.rollback_allowed),
        ):
            if (getattr(gate, "__self__", None) is not self.breaker
                    or getattr(gate, "__func__", None) is not function):
                raise VerificationFoundationError("executor gates must bind exact breaker functions")

    def dispatch_precondition(self, plan: Plan, step: PlanStep, phase: str) -> None:
        conditions = _typed_conditions(step)
        if phase != "forward":
            return
        deadline = _recovery(step, conditions)
        if deadline is not None:
            task = self.breaker.store.get_task(plan.task_id)
            bounds = [plan.expires_at]
            if task is not None and task.deadline is not None:
                bounds.append(task.deadline)
            if deadline > min(bounds) or self.breaker.store.clock() >= deadline:
                raise VerificationFoundationError("reachability recovery deadline is not live and bounded")

    def reconcile_state(self, plan: Plan, document: dict) -> bool:
        """Called under the executor write transaction, including direct recovery."""
        if not document["attempts"]:
            return False
        attempt = document["attempts"][-1]
        if attempt["phase"] != "forward" or attempt["state"] not in {
            "dispatched", "uncertain", "not_applied",
        }:
            return False
        step = next(step for step in plan.steps if step.step_id == attempt["step_id"])
        deadline = _recovery(step, _typed_conditions(step))
        if deadline is None or self.breaker.store.clock() < deadline:
            return False
        existing = document.get("investigation", {})
        if (attempt["state"] != "not_applied"
                and existing.get("lease_id") == attempt["lease_id"]):
            return False
        lease = next(item for item in self.breaker.store.leases(plan.task_id)
                     if item.lease_id == attempt["lease_id"])
        if attempt["state"] == "not_applied":
            reason = "reachability-deadline-not-applied-fenced"
            document["status"] = "needs_replan"
            document["blocked_reason"] = reason
            document["investigation"] = {
                **existing,
                "task_id": plan.task_id, "plan_hash": plan.content_hash,
                "step_id": step.step_id, "lease_id": lease.lease_id,
                "lease_hash": lease.content_hash, "phase": attempt["phase"],
                "deadline": utc_text(deadline),
                "recorded_at": existing.get("recorded_at", utc_text(self.breaker.store.clock())),
                "resolved_at": utc_text(self.breaker.store.clock()),
                "resolution": "not_applied_fenced",
                "effect_application": "not_applied_fenced",
                "out_of_band_needed": False,
                "out_of_band": dict(step.arguments["reachability_recovery"]["out_of_band"]),
            }
            return True
        reason = "reachability-deadline-effect-unproven"
        self.breaker.trip(task_id=plan.task_id, plan_hash=plan.content_hash,
                          lease_hash=lease.content_hash, reason=reason)
        document["status"] = "needs_investigation"
        document["blocked_reason"] = reason
        document["investigation"] = dict(
            task_id=plan.task_id, plan_hash=plan.content_hash, step_id=step.step_id,
            lease_id=lease.lease_id, lease_hash=lease.content_hash, phase=attempt["phase"],
            deadline=utc_text(deadline), recorded_at=utc_text(self.breaker.store.clock()),
            effect_application="unproven", out_of_band_needed=True,
            out_of_band=dict(step.arguments["reachability_recovery"]["out_of_band"]),
        )
        return True

    def verification_received(self, request: StepRequest, verification: Verification) -> None:
        """Atomic with accepted evidence, before any continuation or rollback."""
        if verification.status is VerificationStatus.FAILED:
            self.breaker.trip(task_id=request.lease.task_id, plan_hash=request.plan_hash,
                              lease_hash=request.lease.content_hash,
                              reason="independent-verification-failed")

    def validate(self, request: StepRequest, verification: Verification) -> bool:
        try:
            conditions = _typed_conditions(request.step)
            if len(verification.checks) != len(conditions):
                return False
            results = tuple(PostconditionResult.from_document(item) for item in verification.checks)
            if len({result.condition_hash for result in results}) != len(results):
                return False
            by_hash = {condition.content_hash: condition for condition in conditions}
            deadline = _recovery(request.step, conditions) if request.phase == "forward" else None
            for result in results:
                condition = by_hash.get(result.condition_hash)
                if (condition is None or result.check is not condition.kind
                        or result.binding != _binding(request)):
                    return False
                if (result.evidence_revision != verification.evidence_revision
                        or result.observed_at > verification.performed_at
                        or request.result_at is None or result.observed_at < request.result_at
                        or verification.performed_at - result.observed_at
                        > timedelta(seconds=condition.max_age_seconds)):
                    return False
                if result.basis is EvidenceBasis.OBSERVATION:
                    if (result.source_id != self.source_id
                            or self.authenticator.authenticate(result, result.authentication_digest) is not True):
                        return False
                else:
                    if (deadline is None or result.source_id != "phase5-deadline"
                            or result.observed_at < deadline
                            or result.evidence_digest != result.deadline_digest(deadline)):
                        return False
            expected_status = _aggregate(results)
            if deadline is not None and verification.performed_at >= deadline:
                expected_status = (
                    VerificationStatus.SUCCEEDED
                    if all(result.outcome is VerificationStatus.SUCCEEDED for result in results)
                    and all(result.observed_at <= deadline for result in results)
                    else VerificationStatus.FAILED
                )
            return verification.status is expected_status
        except (VerificationFoundationError, ContractError, ValueError, TypeError):
            return False


def _aggregate(results: Sequence[PostconditionResult]) -> VerificationStatus:
    if any(result.outcome is VerificationStatus.FAILED for result in results):
        return VerificationStatus.FAILED
    if results and all(result.outcome is VerificationStatus.SUCCEEDED for result in results):
        return VerificationStatus.SUCCEEDED
    return VerificationStatus.UNCERTAIN


class DurableVerificationBreaker:
    """Machine-scoped append-only breaker state in the shared TaskStore database."""

    def __init__(self, store: TaskStore):
        self.store = store
        with store.transaction():
            store.db.execute("""CREATE TABLE IF NOT EXISTS tc_verification_breaker_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT NOT NULL,
                event_json BLOB NOT NULL, event_hash TEXT NOT NULL UNIQUE)""")

    def _events(self) -> list[dict]:
        result = []
        for row in self.store.db.execute(
            "SELECT sequence,machine_id,event_json,event_hash "
            "FROM tc_verification_breaker_events ORDER BY sequence",
        ):
            try:
                document = strict_json_loads(row["event_json"])
                valid = (
                    isinstance(document, Mapping) and set(document) == _BREAKER_FIELDS
                    and document["schema_version"] == 1
                    and document["event_number"] == len(result) + 1
                    and row["sequence"] == document["event_number"]
                    and row["machine_id"] == document["machine_id"] == MACHINE_ID
                    and document["state"] in {"open", "closed"}
                    and stable_hash(document) == row["event_hash"]
                )
                parse_utc(document["occurred_at"], "breaker event time")
                if document["state"] == "open":
                    valid = valid and all(
                        isinstance(document[name], str) and document[name]
                        for name in ("task_id", "plan_hash", "lease_hash")
                    ) and document["approval_identity"] is None
                else:
                    valid = valid and all(
                        document[name] is None for name in ("task_id", "plan_hash", "lease_hash")
                    ) and isinstance(document["approval_identity"], str)
            except (KeyError, TypeError, ValueError, ContractError):
                valid = False
            if not valid:
                raise VerificationFoundationError("verification breaker journal integrity failure")
            result.append(document)
        return result

    def is_open(self) -> bool:
        events = self._events()
        return bool(events and events[-1]["state"] == "open")

    def mutation_allowed(self) -> bool:
        return not self.is_open()

    def rollback_allowed(self) -> bool:
        return self.is_open()

    def trip(self, *, task_id: str, plan_hash: str, lease_hash: str, reason: str) -> bool:
        _text(reason, "breaker reason")
        if (not isinstance(plan_hash, str) or not _SHA256.fullmatch(plan_hash)
                or not isinstance(lease_hash, str) or not _SHA256.fullmatch(lease_hash)):
            raise VerificationFoundationError("breaker trip requires exact hash bindings")
        with self.store.transaction():
            events = self._events()
            # Keep distinct concurrent failures even while already open. Repeated
            # ingestion of the same failure since the last reset is idempotent.
            for event in reversed(events):
                if event["state"] == "closed":
                    break
                if all(event[name] == value for name, value in (
                    ("task_id", task_id), ("plan_hash", plan_hash),
                    ("lease_hash", lease_hash), ("reason", reason),
                )):
                    return False
            event_number = len(events) + 1
            document = {
                "schema_version": 1, "event_number": event_number,
                "machine_id": MACHINE_ID, "state": "open",
                "task_id": _text(task_id, "task_id"), "plan_hash": plan_hash,
                "lease_hash": lease_hash, "reason": reason,
                "occurred_at": utc_text(self.store.clock()), "approval_identity": None,
            }
            self.store.db.execute(
                "INSERT INTO tc_verification_breaker_events(machine_id,event_json,event_hash) "
                "VALUES(?,?,?)", (MACHINE_ID, canonical_json(document), stable_hash(document)),
            )
            return True

    def reset(self, *, approval_identity: str, reason: str) -> bool:
        """Close only through an explicit controller-supplied operator identity."""
        _text(approval_identity, "approval_identity")
        _text(reason, "breaker reason")
        with self.store.transaction():
            if not self.is_open():
                return False
            event_number = len(self._events()) + 1
            document = {
                "schema_version": 1, "event_number": event_number,
                "machine_id": MACHINE_ID, "state": "closed",
                "task_id": None, "plan_hash": None, "lease_hash": None,
                "reason": reason, "occurred_at": utc_text(self.store.clock()),
                "approval_identity": approval_identity,
            }
            self.store.db.execute(
                "INSERT INTO tc_verification_breaker_events(machine_id,event_json,event_hash) "
                "VALUES(?,?,?)", (MACHINE_ID, canonical_json(document), stable_hash(document)),
            )
            return True


class IndependentVerificationCoordinator:
    """Completion-driven Phase 5 wrapper; performs at most one executor wake."""

    def __init__(
        self, store: TaskStore, executor: StepExecutor, source: PostconditionSource,
        breaker: DurableVerificationBreaker, *, id_factory: Callable[[], str],
    ):
        if executor.store is not store or breaker.store is not store:
            raise VerificationFoundationError("verification, execution, and breaker need one store")
        if not isinstance(executor.verifier, TypedIndependentVerifier):
            raise VerificationFoundationError("executor must use the typed independent verifier")
        if executor.verifier.breaker is not breaker:
            raise VerificationFoundationError("verifier must bind the same breaker")
        executor.verifier.check_executor(executor)
        if getattr(source, "contract", None) != SOURCE_CONTRACT:
            raise VerificationFoundationError("independent postcondition source is required")
        if source in executor.workers.values():
            raise VerificationFoundationError("a step worker cannot be its own verifier")
        credential_domains = getattr(source, "credential_domains", None)
        write_domains = {domain.value for domain in executor.workers}
        if (not isinstance(credential_domains, frozenset)
                or any(not isinstance(item, str) or not item for item in credential_domains)
                or credential_domains & write_domains
                or any(item.endswith("_write") for item in credential_domains)):
            raise VerificationFoundationError("verification source cannot receive a write credential")
        if getattr(source, "source_id", None) != executor.verifier.source_id:
            raise VerificationFoundationError("source identity must match the pinned verifier identity")
        self.store, self.executor, self.source, self.breaker = store, executor, source, breaker
        self.id_factory = id_factory

    def _request(self, task_id: str) -> tuple[VerificationRequest, StepRequest]:
        document = self.executor.snapshot(task_id)
        plan = self.store.get_plan(task_id)
        if plan is None or plan.content_hash != document["plan_hash"]:
            raise VerificationFoundationError("current plan binding changed")
        internal = self.executor.verification_request(task_id)
        step = internal.step
        conditions = _typed_conditions(step)
        deadline = _recovery(step, conditions) if internal.phase == "forward" else None
        task = self.store.get_task(task_id)
        assert task is not None
        if deadline is not None:
            bounds = [plan.expires_at]
            if task.deadline is not None:
                bounds.append(task.deadline)
            if deadline > min(bounds):
                raise VerificationFoundationError("reachability recovery deadline exceeds authority")
        lease = internal.lease
        assert internal.result_at is not None
        request = VerificationRequest(
            task_id=task_id, plan_id=plan.plan_id, plan_hash=plan.content_hash,
            step_id=step.step_id, lease_id=lease.lease_id, lease_hash=lease.content_hash,
            phase=internal.phase, evidence_revision=plan.evidence_revision,
            result_at=internal.result_at, conditions=conditions,
            max_verification_seconds=step.max_execution_seconds,
            recovery_deadline=deadline,
        )
        return request, internal

    def validate_plan(self, task_id: str) -> None:
        """Optional structural audit; dispatch liveness belongs to StepExecutor."""
        plan = self.store.get_plan(task_id)
        task = self.store.get_task(task_id)
        if plan is None or task is None:
            raise VerificationFoundationError("task plan is missing")
        for step in plan.steps:
            # Structure only: future deadlines do not gate observation or rollback.
            _recovery(step, _typed_conditions(step))

    def advance(self, task_id: str) -> str:
        state = self.executor.advance(task_id)
        if state != "verifying":
            return state
        return self.verify(task_id)

    def verify(self, task_id: str) -> str:
        request, internal = self._request(task_id)
        now = self.store.clock()
        if request.recovery_deadline is not None and now >= request.recovery_deadline:
            results = tuple(PostconditionResult(
                **request.binding,
                check=condition.kind, condition_hash=condition.content_hash,
                outcome=VerificationStatus.FAILED,
                evidence_revision=request.evidence_revision, source_id="phase5-deadline",
                observed_at=now,
                evidence_digest="0" * 64,
                basis=EvidenceBasis.DEADLINE,
            ) for condition in request.conditions)
            results = tuple(replace(result, evidence_digest=result.deadline_digest(
                request.recovery_deadline)) for result in results)
        else:
            started = self.store.clock()
            try:
                results = tuple(self.source.observe(request))
            except Exception:
                raise VerificationFoundationError("independent postcondition collection failed") from None
            if self.store.clock() - started >= timedelta(seconds=request.max_verification_seconds):
                raise VerificationFoundationError("independent postcondition collection exceeded its bound")
        if len(results) != len(request.conditions) or any(
            not isinstance(result, PostconditionResult) for result in results
        ):
            raise VerificationFoundationError("source returned incomplete or malformed evidence")
        status = _aggregate(results)
        performed_at = self.store.clock()
        if request.recovery_deadline is not None and performed_at >= request.recovery_deadline:
            status = (
                VerificationStatus.SUCCEEDED
                if all(result.outcome is VerificationStatus.SUCCEEDED for result in results)
                and all(result.observed_at <= request.recovery_deadline for result in results)
                else VerificationStatus.FAILED
            )
        verification = Verification(
            verification_id=_text(self.id_factory(), "verification_id"),
            task_id=request.task_id, plan_id=request.plan_id, plan_hash=request.plan_hash,
            step_id=request.step_id, lease_id=request.lease_id, lease_hash=request.lease_hash,
            status=status, checks=tuple(result.to_document() for result in results),
            evidence_revision=request.evidence_revision, performed_at=performed_at,
        )
        if len(verification.canonical_json()) > MAX_OUTPUT_BYTES:
            raise VerificationFoundationError("typed verification evidence exceeds its bound")
        # Do not trip the breaker for malformed, unauthenticated, stale, or
        # wrong-attempt material.  StepExecutor repeats this validation at the
        # durable ingestion boundary to fence races.
        if self.executor.verifier.validate(internal, verification) is not True:
            raise VerificationFoundationError("independent verification failed authentication")
        return self.executor.accept_verification(verification)


def independent_verification_enabled(environment: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environment is None else environment).get(FEATURE_FLAG_ENV, "") == "1"


def build_independent_verification(
    environment: Mapping[str, str] | None = None, **interfaces,
) -> IndependentVerificationCoordinator | None:
    if not independent_verification_enabled(environment):
        return None
    required = {"store", "executor", "source", "breaker", "id_factory"}
    if set(interfaces) != required or any(value is None for value in interfaces.values()):
        raise VerificationFoundationError("enabled independent verification needs all interfaces")
    return IndependentVerificationCoordinator(**interfaces)


__all__ = [
    "AUTHENTICATOR_CONTRACT", "EvidenceBasis", "FEATURE_FLAG_ENV",
    "IndependentVerificationCoordinator", "Postcondition", "PostconditionKind",
    "PostconditionResult", "SOURCE_CONTRACT", "TypedIndependentVerifier",
    "VerificationFoundationError", "VerificationRequest", "DurableVerificationBreaker",
    "build_independent_verification", "independent_verification_enabled",
]
