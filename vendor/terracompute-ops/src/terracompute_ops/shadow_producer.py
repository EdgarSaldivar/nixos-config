"""Authenticated, disabled Phase 7 shadow capture producer.

The producer accepts durable provenance and decisions that were already made.
It registers the parent capture before validating/mapping the decisions, then
records either exactly one comparison or one terminal failure.  It invokes no
gateway, legacy loop, correlator, classifier, model, executor, callback, or
network operation.  The only mutable dependency is the supplied ShadowLedger.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from .incident_correlation import DetectorBatch
from .plans import ContractError, _identifier
from .shadow_rollout import (
    CaptureExpectation,
    CaptureOutcome,
    CaptureResult,
    ComparisonOrigin,
    PolicyOutcome,
    RouteKind,
    ShadowComparison,
    ShadowDecision,
    ShadowLedger,
    ShadowRecord,
)
from .task_service import TaskDisposition
from .tasks import OperatorInputRecord


class CaptureFailure(str, Enum):
    MISSING_OLD_DECISION = "missing-old-decision"
    MISSING_NEW_DECISION = "missing-new-decision"
    MALFORMED_OLD_DECISION = "malformed-old-decision"
    MALFORMED_NEW_DECISION = "malformed-new-decision"
    UNKNOWN_OLD_DECISION = "unknown-old-decision"
    UNKNOWN_NEW_DECISION = "unknown-new-decision"
    INCOMPLETE_POLICY = "incomplete-policy"
    TASK_BINDING_CONFLICT = "task-binding-conflict"
    DETECTOR_REVISION_MISMATCH = "detector-revision-mismatch"
    DETECTOR_SOURCE_AMBIGUOUS = "detector-source-ambiguous"


@dataclass(frozen=True)
class ProducerRevisions:
    policy_revision: str
    config_revision: str
    evidence_revision: str

    def __post_init__(self) -> None:
        for name in ("policy_revision", "config_revision", "evidence_revision"):
            _identifier(getattr(self, name), name)


_EXECUTABLE = frozenset({
    RouteKind.NEW_TASK, RouteKind.EXISTING_TASK, RouteKind.LEGACY_HANDOVER,
})


def _failure(old: ShadowDecision | None, new: ShadowDecision | None) -> CaptureFailure | None:
    if old is None:
        return CaptureFailure.MISSING_OLD_DECISION
    if new is None:
        return CaptureFailure.MISSING_NEW_DECISION
    if type(old) is not ShadowDecision:
        return CaptureFailure.MALFORMED_OLD_DECISION
    if type(new) is not ShadowDecision:
        return CaptureFailure.MALFORMED_NEW_DECISION
    for side, value in (("old", old), ("new", new)):
        if value.routing.route is RouteKind.UNKNOWN:
            return (
                CaptureFailure.UNKNOWN_OLD_DECISION
                if side == "old" else CaptureFailure.UNKNOWN_NEW_DECISION
            )
        if value.policy is not None and value.policy.outcome is PolicyOutcome.UNKNOWN:
            return (
                CaptureFailure.UNKNOWN_OLD_DECISION
                if side == "old" else CaptureFailure.UNKNOWN_NEW_DECISION
            )
        if value.routing.route in _EXECUTABLE and value.policy is None:
            return CaptureFailure.INCOMPLETE_POLICY
    if (old.policy is None) != (new.policy is None):
        return CaptureFailure.INCOMPLETE_POLICY
    return None


def _task_binding(old: ShadowDecision, new: ShadowDecision) -> str | None | CaptureFailure:
    bindings = {
        decision.routing.task_id
        for decision in (old, new)
        if decision.routing.task_id is not None
    }
    if len(bindings) > 1:
        return CaptureFailure.TASK_BINDING_CONFLICT
    return next(iter(bindings), None)


def _capture(
    ledger: ShadowLedger,
    expectation: CaptureExpectation,
    *,
    operator_id: str | None,
    task_id: str | None,
    old: ShadowDecision | None,
    new: ShadowDecision | None,
) -> ShadowRecord | CaptureResult:
    if type(ledger) is not ShadowLedger:
        raise ContractError("shadow producer requires a concrete ShadowLedger")
    prior_expectation, prior_result, prior_record = ledger.capture_for(
        expectation.source_instance, expectation.request_id,
    )
    if prior_expectation is not None:
        # This comparison is deliberate: ``expect_capture`` then raises the
        # typed conflict with the durable row rather than letting a changed
        # replay reuse unrelated evidence.
        if prior_expectation != expectation:
            ledger.expect_capture(expectation)
        if prior_result is not None:
            if prior_result.outcome is CaptureOutcome.CAPTURED:
                if prior_record is None:
                    raise ContractError("captured parent is missing its comparison")
                return prior_record
            return prior_result
    ledger.expect_capture(expectation)
    problem = _failure(old, new)
    if problem is not None:
        return ledger.record_capture_failure(expectation, problem.value)
    assert old is not None and new is not None
    binding = _task_binding(old, new)
    if type(binding) is CaptureFailure:
        return ledger.record_capture_failure(expectation, binding.value)
    if task_id is not None and binding is not None and task_id != binding:
        return ledger.record_capture_failure(
            expectation, CaptureFailure.TASK_BINDING_CONFLICT.value,
        )
    comparison = ShadowComparison(
        origin=expectation.origin,
        operator_id=operator_id,
        task_id=task_id if task_id is not None else binding,
        request_id=expectation.request_id,
        parent_hash=expectation.parent_hash,
        policy_revision=expectation.policy_revision,
        config_revision=expectation.config_revision,
        evidence_revision=expectation.evidence_revision,
        observed_at=expectation.observed_at,
        source_instance=expectation.source_instance,
        nonce="capture-" + expectation.content_hash[:48],
        old=old,
        new=new,
        machine_id=expectation.machine_id,
    )
    return ledger.record(comparison)


def capture_operator(
    ledger: ShadowLedger,
    routed_input: OperatorInputRecord,
    revisions: ProducerRevisions,
    *,
    old: ShadowDecision | None,
    new: ShadowDecision | None,
) -> ShadowRecord | CaptureResult:
    """Capture one durable authenticated Telegram parent input."""
    if type(ledger) is not ShadowLedger:
        raise ContractError("shadow producer requires a concrete ShadowLedger")
    if type(routed_input) is not OperatorInputRecord or type(revisions) is not ProducerRevisions:
        raise ContractError("operator capture requires typed durable provenance and revisions")
    durable = ledger.tasks.get_operator_input(routed_input.input_id)
    if durable is None or durable.canonical_json() != routed_input.canonical_json():
        raise ContractError(
            "operator capture parent is not the exact durable TaskStore input"
        )
    provenance = routed_input.provenance
    if (
        provenance["transport"] != "telegram"
        or provenance["input_kind"] != "question"
        or provenance["message_id"] is None
        or provenance["callback_id"] is not None
    ):
        raise ContractError("operator capture provenance is not an ordinary authenticated message")
    try:
        TaskDisposition(routed_input.disposition)
    except ValueError as error:
        raise ContractError("operator capture has an unknown durable disposition") from error
    namespace = provenance["namespace"]
    source_instance = f"telegram:{namespace}"
    operator_id = f"telegram:{namespace}:user:{provenance['sender_id']}"
    expectation = CaptureExpectation(
        origin=ComparisonOrigin.OPERATOR,
        request_id=routed_input.input_id,
        parent_hash=routed_input.content_hash,
        source_instance=source_instance,
        policy_revision=revisions.policy_revision,
        config_revision=revisions.config_revision,
        evidence_revision=revisions.evidence_revision,
        observed_at=routed_input.recorded_at,
    )
    return _capture(
        ledger, expectation, operator_id=operator_id, task_id=routed_input.task_id,
        old=old, new=new,
    )


def capture_detector(
    ledger: ShadowLedger,
    batch: DetectorBatch,
    revisions: ProducerRevisions,
    *,
    old: ShadowDecision | None,
    new: ShadowDecision | None,
) -> ShadowRecord | CaptureResult:
    """Capture one detector batch as one parent, never one sample per domain."""
    if type(ledger) is not ShadowLedger:
        raise ContractError("shadow producer requires a concrete ShadowLedger")
    if type(batch) is not DetectorBatch or type(revisions) is not ProducerRevisions:
        raise ContractError("detector capture requires typed durable provenance and revisions")
    try:
        row = ledger.tasks.db.execute(
            "SELECT batch_hash,batch_json FROM tc_detector_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise ContractError("detector capture durable batch store is unavailable") from error
    if (
        row is None
        or type(row["batch_hash"]) is not str
        or type(row["batch_json"]) is not bytes
        or row["batch_hash"] != batch.content_hash
        or row["batch_json"] != batch.canonical_json()
    ):
        raise ContractError(
            "detector capture parent is not the exact durable detector batch"
        )
    sources = {fact.source_instance for fact in batch.facts}
    source_instance = (
        next(iter(sources)) if len(sources) == 1 else "detector:unattributed-parent"
    )
    expectation = CaptureExpectation(
        origin=ComparisonOrigin.DETECTOR,
        request_id=batch.batch_id,
        parent_hash=batch.content_hash,
        source_instance=source_instance,
        policy_revision=revisions.policy_revision,
        config_revision=revisions.config_revision,
        evidence_revision=revisions.evidence_revision,
        observed_at=batch.collected_at,
    )
    if revisions.evidence_revision != batch.evidence_revision:
        ledger.expect_capture(expectation)
        return ledger.record_capture_failure(
            expectation, CaptureFailure.DETECTOR_REVISION_MISMATCH.value,
        )
    if len(sources) != 1:
        ledger.expect_capture(expectation)
        return ledger.record_capture_failure(
            expectation, CaptureFailure.DETECTOR_SOURCE_AMBIGUOUS.value,
        )
    return _capture(
        ledger, expectation, operator_id=None, task_id=None, old=old, new=new,
    )


__all__ = [
    "CaptureFailure", "ProducerRevisions", "capture_detector", "capture_operator",
]
