"""Pure, disabled Phase 7 adapter for an already-run incident correlation.

The adapter receives frozen values captured after the one real correlation and
projection attempt.  It never calls ``correlate`` or projection replay and has
no store, clock, model, executor, callback, or network access.  One detector
batch is one parent request even when it contains several domains.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .incident_correlation import (
    CorrelationDomain,
    CorrelationOutcome,
    CorrelationResult,
    DetectorBatch,
    FactRejection,
)
from .plans import ContractError, _identifier
from .shadow_rollout import RouteKind, RoutingDecision


class ProjectionOutcome(str, Enum):
    APPLIED = "applied"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True)
class ProjectionObservation:
    projection_id: str
    batch_id: str
    outcome: ProjectionOutcome

    def __post_init__(self) -> None:
        _identifier(self.projection_id, "projection_id")
        _identifier(self.batch_id, "projection batch_id")
        if type(self.outcome) is not ProjectionOutcome:
            raise ContractError("projection outcome must be typed")


@dataclass(frozen=True)
class ProjectionSnapshot:
    """Complete already-captured projection set for one detector batch."""

    batch_id: str
    expected_projection_ids: tuple[str, ...]
    statuses: tuple[ProjectionObservation, ...]

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "projection batch_id")
        if type(self.expected_projection_ids) is not tuple or type(self.statuses) is not tuple:
            raise ContractError("projection snapshot arrays must be tuples")
        for projection_id in self.expected_projection_ids:
            _identifier(projection_id, "expected projection_id")
        if any(type(item) is not ProjectionObservation for item in self.statuses):
            raise ContractError("projection statuses must be typed")
        if (
            not self.expected_projection_ids
            or len(set(self.expected_projection_ids)) != len(self.expected_projection_ids)
            or tuple(sorted(self.expected_projection_ids)) != self.expected_projection_ids
        ):
            raise ContractError("expected projection identities must be non-empty, unique and sorted")


@dataclass(frozen=True)
class CorrelationObservation:
    """One parent routing observation or an instruction to reuse its original."""

    parent_request_id: str
    source_instance: str
    evidence_revision: str
    decision: RoutingDecision | None
    task_ids: tuple[str, ...]
    reuse_original: bool

    def __post_init__(self) -> None:
        for name in ("parent_request_id", "source_instance", "evidence_revision"):
            _identifier(getattr(self, name), name)
        if self.decision is not None and type(self.decision) is not RoutingDecision:
            raise ContractError("correlation decision must be typed or absent")
        if type(self.task_ids) is not tuple:
            raise ContractError("correlation task_ids must be a tuple")
        for task_id in self.task_ids:
            _identifier(task_id, "task_id")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ContractError("correlation task_ids contain duplicates")
        if type(self.reuse_original) is not bool:
            raise ContractError("reuse_original must be boolean")
        if self.reuse_original != (self.decision is None):
            raise ContractError("only a replay may omit the captured decision")


def _unknown(batch: DetectorBatch, source_instance: str) -> CorrelationObservation:
    return CorrelationObservation(
        parent_request_id=batch.batch_id,
        source_instance=source_instance,
        evidence_revision=batch.evidence_revision,
        decision=RoutingDecision(RouteKind.UNKNOWN),
        task_ids=(),
        reuse_original=False,
    )


def _valid_outcome(outcome: CorrelationOutcome) -> bool:
    if type(outcome) is not CorrelationOutcome or type(outcome.domain) is not CorrelationDomain:
        return False
    if outcome.status not in {"active", "recovered", "quiet"}:
        return False
    if type(outcome.created) is not bool or type(outcome.material_changed) is not bool:
        return False
    try:
        _identifier(outcome.evidence_revision, "correlation evidence_revision")
        if outcome.task_id is not None:
            _identifier(outcome.task_id, "correlation task_id")
        if type(outcome.incident_ids) is not tuple or type(outcome.linked_task_ids) is not tuple:
            return False
        for value in (*outcome.incident_ids, *outcome.linked_task_ids):
            _identifier(value, "correlation identity")
    except ContractError:
        return False
    if outcome.status == "active":
        return outcome.task_id is not None and (outcome.created or not outcome.material_changed)
    return not outcome.created and not outcome.material_changed


def map_correlation_observation(
    batch: DetectorBatch,
    result: CorrelationResult,
    *,
    source_instance: str,
    projection_snapshot: ProjectionSnapshot,
) -> CorrelationObservation:
    """Map one already-derived batch result without invoking correlation again."""
    if type(batch) is not DetectorBatch or type(result) is not CorrelationResult:
        raise ContractError("correlation mapping requires typed batch and result")
    _identifier(source_instance, "source_instance")
    if type(projection_snapshot) is not ProjectionSnapshot:
        raise ContractError("projection snapshot must be typed")
    if type(result.enabled) is not bool or type(result.replay) is not bool:
        return _unknown(batch, source_instance)
    if type(result.outcomes) is not tuple or type(result.rejections) is not tuple:
        return _unknown(batch, source_instance)
    if any(type(item) is not FactRejection for item in result.rejections):
        return _unknown(batch, source_instance)
    if not result.enabled or result.rejections or not result.outcomes:
        return _unknown(batch, source_instance)
    if any(not _valid_outcome(item) for item in result.outcomes):
        return _unknown(batch, source_instance)
    domains = tuple(item.domain.value for item in result.outcomes)
    if len(set(domains)) != len(domains) or domains != tuple(sorted(domains)):
        return _unknown(batch, source_instance)
    if result.replay:
        return CorrelationObservation(
            parent_request_id=batch.batch_id,
            source_instance=source_instance,
            evidence_revision=batch.evidence_revision,
            decision=None,
            task_ids=(),
            reuse_original=True,
        )
    projections = projection_snapshot.statuses
    if (
        projection_snapshot.batch_id != batch.batch_id
        or tuple(sorted(item.projection_id for item in projections))
        != projection_snapshot.expected_projection_ids
        or len({item.projection_id for item in projections}) != len(projections)
        or any(item.batch_id != batch.batch_id for item in projections)
        or any(item.outcome is not ProjectionOutcome.APPLIED for item in projections)
    ):
        return _unknown(batch, source_instance)
    active = tuple(item for item in result.outcomes if item.status == "active")
    task_ids = tuple(sorted({item.task_id for item in active if item.task_id is not None}))
    if not active:
        decision = RoutingDecision(RouteKind.NO_ROUTE)
    elif len(active) != 1:
        # One parent cannot silently become several canary samples, and the
        # strict routing contract cannot represent several simultaneous tasks.
        return _unknown(batch, source_instance)
    elif active[0].created:
        decision = RoutingDecision(RouteKind.NEW_TASK)
    else:
        decision = RoutingDecision(RouteKind.EXISTING_TASK, active[0].task_id)
    return CorrelationObservation(
        parent_request_id=batch.batch_id,
        source_instance=source_instance,
        evidence_revision=batch.evidence_revision,
        decision=decision,
        task_ids=task_ids,
        reuse_original=False,
    )


__all__ = [
    "CorrelationObservation", "ProjectionObservation", "ProjectionOutcome",
    "ProjectionSnapshot",
    "map_correlation_observation",
]
