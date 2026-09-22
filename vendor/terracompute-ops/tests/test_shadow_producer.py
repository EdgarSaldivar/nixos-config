from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.incident_correlation import (
    ControllerServicePayload,
    CorrelationDomain,
    DetectorBatch,
    DetectorFact,
    DetectorKind,
    FactFreshness,
    FactStatus,
    ResourceOwnership,
    Severity,
    IncidentCorrelator,
)
from terracompute_ops.plan_authorization import Authority
from terracompute_ops.plans import ContractError, Effect
from terracompute_ops.shadow_producer import (
    CaptureFailure,
    ProducerRevisions,
    capture_detector,
    capture_operator,
)
from terracompute_ops.shadow_rollout import (
    CaptureOutcome,
    PolicyDecision,
    PolicyOutcome,
    RouteKind,
    RoutingDecision,
    ShadowDecision,
    ShadowLedger,
    ShadowRecord,
)
from terracompute_ops.task_service import TaskService
from terracompute_ops.tasks import OperatorInputRecord, TaskStore
from terracompute_ops.telegram import AuthenticatedInput, InputKind
from terracompute_ops.state import StateStore


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
GROUP = -10017049
NAMESPACE = "shadow-producer-test"


def decision(
    route: RouteKind = RouteKind.NEW_TASK,
    *,
    task_id: str | None = None,
    policy: PolicyDecision | None | object = Ellipsis,
) -> ShadowDecision:
    if policy is Ellipsis:
        policy = PolicyDecision(
            Effect("read", "read-only observation"),
            PolicyOutcome.PERMIT,
            Authority.AGENT,
        )
    return ShadowDecision(RoutingDecision(route, task_id), policy)  # type: ignore[arg-type]


def unknown() -> ShadowDecision:
    return ShadowDecision(RoutingDecision(RouteKind.UNKNOWN))


class ShadowProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = StateStore(self.root, clock=lambda: NOW)
        self.addCleanup(self.state.close)
        self.tasks = TaskStore(self.root, clock=lambda: NOW)
        self.addCleanup(self.tasks.close)
        self.correlator = IncidentCorrelator(
            self.state, self.tasks, enabled=True, clock=lambda: NOW,
        )
        self.ledger = ShadowLedger(self.tasks, clock=lambda: NOW)
        self.revisions = ProducerRevisions("policy-1", "config-1", "evidence-1")
        self.service = TaskService(
            self.tasks, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW,
        )

    def intake(self, update_id: int = 1) -> OperatorInputRecord:
        envelope = AuthenticatedInput(
            update_id=update_id,
            group_id=GROUP,
            sender_id=42,
            message_id=1000 + update_id,
            callback_id=None,
            kind=InputKind.QUESTION,
            subject_id=None,
            nonce="investigate the controller",
            text="/task new investigate the controller",
        )
        self.service.handle(envelope)
        stored = self.tasks.get_operator_input(self.service._event_id(envelope))
        assert stored is not None
        return stored

    def fact(
        self, fact_id: str = "fact-1", *, source: str = "boot-1",
        domain: CorrelationDomain = CorrelationDomain.CONTROLLER,
    ) -> DetectorFact:
        return DetectorFact(
            fact_id=fact_id,
            evidence_revision="evidence-1",
            kind=DetectorKind.CONTROLLER_SERVICE,
            domain=domain,
            resource_id=f"controller:{fact_id}",
            failure_id=f"failure-{fact_id}",
            status=FactStatus.FAULT,
            freshness=FactFreshness.CURRENT,
            severity=Severity.ERROR,
            observed_at=NOW,
            source_instance=source,
            summary="controller service fault",
            payload=ControllerServicePayload(False, 3, 3),
            ownership=ResourceOwnership.HOST,
        )

    def test_operator_capture_uses_durable_identity_and_replays_original(self) -> None:
        intake = self.intake()
        first = capture_operator(
            self.ledger, intake, self.revisions, old=decision(), new=decision(),
        )
        self.assertIsInstance(first, ShadowRecord)
        assert isinstance(first, ShadowRecord)
        self.assertEqual(first.comparison.request_id, intake.input_id)
        self.assertEqual(first.comparison.source_instance, f"telegram:{NAMESPACE}")
        self.assertEqual(first.comparison.operator_id, f"telegram:{NAMESPACE}:user:42")
        self.assertEqual(first.comparison.task_id, intake.task_id)

        replay = capture_operator(
            self.ledger, intake, self.revisions, old=unknown(), new=unknown(),
        )
        self.assertEqual(replay, first)
        expectation, result, record = self.ledger.capture_for(
            f"telegram:{NAMESPACE}", intake.input_id,
        )
        self.assertIsNotNone(expectation)
        self.assertIs(result.outcome, CaptureOutcome.CAPTURED)  # type: ignore[union-attr]
        self.assertEqual(record, first)

    def test_failure_is_terminal_and_reused_on_replay(self) -> None:
        intake = self.intake()
        first = capture_operator(
            self.ledger, intake, self.revisions, old=decision(), new=unknown(),
        )
        self.assertIs(first.outcome, CaptureOutcome.FAILED)  # type: ignore[union-attr]
        self.assertEqual(first.failure_code, CaptureFailure.UNKNOWN_NEW_DECISION.value)  # type: ignore[union-attr]
        replay = capture_operator(
            self.ledger, intake, self.revisions, old=decision(), new=decision(),
        )
        self.assertEqual(replay, first)
        self.assertEqual(self.ledger.records(), ())

    def test_missing_malformed_and_incomplete_decisions_are_accounted(self) -> None:
        cases = (
            (decision(), None, CaptureFailure.MISSING_NEW_DECISION),
            ({}, decision(), CaptureFailure.MALFORMED_OLD_DECISION),
            (decision(), decision(policy=None), CaptureFailure.INCOMPLETE_POLICY),
        )
        for offset, (old, new, expected) in enumerate(cases, 10):
            with self.subTest(expected=expected):
                result = capture_operator(
                    self.ledger, self.intake(offset), self.revisions,
                    old=old, new=new,  # type: ignore[arg-type]
                )
                self.assertIs(result.outcome, CaptureOutcome.FAILED)  # type: ignore[union-attr]
                self.assertEqual(result.failure_code, expected.value)  # type: ignore[union-attr]

    def test_durable_and_decision_task_bindings_must_agree(self) -> None:
        result = capture_operator(
            self.ledger,
            self.intake(),
            self.revisions,
            old=decision(RouteKind.EXISTING_TASK, task_id="task-other"),
            new=decision(RouteKind.EXISTING_TASK, task_id="task-other"),
        )
        self.assertIs(result.outcome, CaptureOutcome.FAILED)  # type: ignore[union-attr]
        self.assertEqual(result.failure_code, CaptureFailure.TASK_BINDING_CONFLICT.value)  # type: ignore[union-attr]

    def test_detector_batch_is_one_authenticated_parent(self) -> None:
        batch = DetectorBatch(
            "batch-1", "evidence-1", NOW,
            (self.fact("fact-1"), self.fact("fact-2")),
        )
        self.correlator.process(batch)
        result = capture_detector(
            self.ledger, batch, self.revisions, old=decision(), new=decision(),
        )
        self.assertIsInstance(result, ShadowRecord)
        assert isinstance(result, ShadowRecord)
        self.assertEqual(result.comparison.request_id, "batch-1")
        self.assertEqual(result.comparison.source_instance, "boot-1")
        self.assertIsNone(result.comparison.operator_id)
        self.assertEqual(len(self.ledger.records()), 1)

    def test_detector_failures_are_durably_accounted(self) -> None:
        empty = DetectorBatch("batch-empty", "evidence-1", NOW, ())
        self.correlator.process(empty)
        empty_result = capture_detector(
            self.ledger, empty, self.revisions, old=decision(), new=decision(),
        )
        self.assertIs(empty_result.outcome, CaptureOutcome.FAILED)  # type: ignore[union-attr]
        self.assertEqual(
            empty_result.failure_code,  # type: ignore[union-attr]
            CaptureFailure.DETECTOR_SOURCE_AMBIGUOUS.value,
        )
        mixed = DetectorBatch(
            "batch-mixed", "evidence-1", NOW,
            (self.fact("fact-1", source="boot-1"), self.fact("fact-2", source="boot-2")),
        )
        self.correlator.process(mixed)
        mixed_result = capture_detector(
            self.ledger, mixed, self.revisions, old=decision(), new=decision(),
        )
        self.assertEqual(
            mixed_result.failure_code,  # type: ignore[union-attr]
            CaptureFailure.DETECTOR_SOURCE_AMBIGUOUS.value,
        )
        mismatch = DetectorBatch("batch-revision", "evidence-2", NOW, ())
        self.correlator.process(mismatch)
        mismatch_result = capture_detector(
            self.ledger, mismatch, self.revisions, old=decision(), new=decision(),
        )
        self.assertEqual(
            mismatch_result.failure_code,  # type: ignore[union-attr]
            CaptureFailure.DETECTOR_REVISION_MISMATCH.value,
        )
        for item in (empty, mixed, mismatch):
            expectation, result, _ = self.ledger.capture_for(
                ("detector:unattributed-parent" if len({
                    fact.source_instance for fact in item.facts
                }) != 1 else item.facts[0].source_instance),
                item.batch_id,
            )
            self.assertIsNotNone(expectation)
            self.assertIs(result.outcome, CaptureOutcome.FAILED)  # type: ignore[union-attr]

    def test_capture_never_invokes_a_decision_path(self) -> None:
        intake = self.intake()
        forbidden = mock.Mock(side_effect=AssertionError("decision path was invoked"))
        with mock.patch(
            "terracompute_ops.task_service.TaskService.handle", forbidden,
        ), mock.patch(
            "terracompute_ops.incident_correlation.IncidentCorrelator.process", forbidden,
        ), mock.patch(
            "terracompute_ops.plan_authorization.authorize_plan", forbidden,
        ):
            result = capture_operator(
                self.ledger, intake, self.revisions, old=decision(), new=decision(),
            )
        self.assertIsInstance(result, ShadowRecord)
        forbidden.assert_not_called()

    def test_only_durable_typed_parents_are_accepted(self) -> None:
        with self.assertRaises(ContractError):
            capture_operator(
                self.ledger, {}, self.revisions,  # type: ignore[arg-type]
                old=decision(), new=decision(),
            )
        with self.assertRaises(ContractError):
            capture_operator(
                self.ledger, self.intake(), {},  # type: ignore[arg-type]
                old=decision(), new=decision(),
            )
        intake = self.intake(50)
        with self.assertRaises(ContractError):
            capture_operator(
                self.ledger,
                dataclasses.replace(intake, input_id="telegram-input:absent"),
                self.revisions,
                old=decision(), new=decision(),
            )
        with self.assertRaises(ContractError):
            capture_operator(
                self.ledger,
                dataclasses.replace(intake, task_id="task-changed"),
                self.revisions,
                old=decision(), new=decision(),
            )
        with self.assertRaises(ContractError):
            capture_detector(
                self.ledger,
                DetectorBatch("batch-absent", "evidence-1", NOW, (self.fact(),)),
                self.revisions,
                old=decision(), new=decision(),
            )


if __name__ == "__main__":
    unittest.main()
