from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timezone
from unittest import mock

from terracompute_ops.incident_correlation import (
    CorrelationDomain,
    CorrelationOutcome,
    CorrelationResult,
    DetectorBatch,
    FactRejection,
)
from terracompute_ops.incident_shadow_adapter import (
    ProjectionObservation,
    ProjectionOutcome,
    ProjectionSnapshot,
    map_correlation_observation,
)
from terracompute_ops.plans import ContractError
from terracompute_ops.shadow_rollout import RouteKind


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def batch() -> DetectorBatch:
    return DetectorBatch("batch-1", "evidence-1", NOW, ())


def outcome(
    domain: CorrelationDomain = CorrelationDomain.CONTROLLER,
    *,
    status: str = "active",
    task_id: str | None = "task-1",
    created: bool = True,
    material_changed: bool = False,
) -> CorrelationOutcome:
    return CorrelationOutcome(
        domain, status, task_id, created, material_changed,
        "correlation-revision", ("incident-1",),
    )


def projections(*states: ProjectionOutcome) -> tuple[ProjectionObservation, ...]:
    return tuple(
        ProjectionObservation(f"projection-{index}", "batch-1", state)
        for index, state in enumerate(states, 1)
    )


def snapshot(*states: ProjectionOutcome, batch_id: str = "batch-1") -> ProjectionSnapshot:
    statuses = projections(*states)
    if batch_id != "batch-1":
        statuses = tuple(dataclasses.replace(item, batch_id=batch_id) for item in statuses)
    return ProjectionSnapshot(
        batch_id,
        tuple(sorted(item.projection_id for item in statuses)),
        statuses,
    )


class IncidentShadowAdapterTest(unittest.TestCase):
    def map(self, result: CorrelationResult, observed=None):
        return map_correlation_observation(
            batch(), result, source_instance="collector-a",
            projection_snapshot=(snapshot(ProjectionOutcome.APPLIED)
                                 if observed is None else observed),
        )

    def test_new_existing_and_no_route_mapping(self) -> None:
        new = self.map(CorrelationResult(True, False, (outcome(),)))
        self.assertIs(new.decision.route, RouteKind.NEW_TASK)
        self.assertEqual(new.task_ids, ("task-1",))
        existing = self.map(CorrelationResult(True, False, (
            outcome(created=False),
        )))
        self.assertIs(existing.decision.route, RouteKind.EXISTING_TASK)
        self.assertEqual(existing.decision.task_id, "task-1")
        quiet = self.map(CorrelationResult(True, False, (
            outcome(status="quiet", task_id=None, created=False),
        )))
        self.assertIs(quiet.decision.route, RouteKind.NO_ROUTE)

    def test_replay_instructs_producer_to_reuse_original(self) -> None:
        replay = self.map(CorrelationResult(True, True, (outcome(),)))
        self.assertTrue(replay.reuse_original)
        self.assertIsNone(replay.decision)
        self.assertEqual(replay.parent_request_id, "batch-1")

    def test_multi_domain_batch_is_one_unknown_parent_not_multiple_samples(self) -> None:
        observed = (
            outcome(CorrelationDomain.CONTROLLER, task_id="task-1"),
            outcome(CorrelationDomain.NETWORK, task_id="task-2"),
        )
        mapped = self.map(CorrelationResult(True, False, observed))
        self.assertIs(mapped.decision.route, RouteKind.UNKNOWN)
        self.assertEqual(mapped.task_ids, ())

    def test_incomplete_rejected_or_projection_failed_is_unknown(self) -> None:
        valid = CorrelationResult(True, False, (outcome(),))
        cases = (
            (CorrelationResult(False, False, ()), snapshot(ProjectionOutcome.APPLIED)),
            (CorrelationResult(True, False, ()), snapshot(ProjectionOutcome.APPLIED)),
            (CorrelationResult(True, False, (outcome(),), (FactRejection("fact-1", "bad"),)),
             snapshot(ProjectionOutcome.APPLIED)),
            (valid, ProjectionSnapshot("batch-1", ("projection-1",), ())),
            (valid, snapshot(ProjectionOutcome.FAILED)),
            (valid, snapshot(ProjectionOutcome.PENDING)),
            (valid, snapshot(ProjectionOutcome.APPLIED, batch_id="other-batch")),
        )
        for result, observed in cases:
            with self.subTest(result=result, projections=observed):
                self.assertIs(self.map(result, observed).decision.route, RouteKind.UNKNOWN)

    def test_contradictory_outcomes_and_order_are_unknown(self) -> None:
        cases = (
            outcome(task_id=None),
            outcome(created=False, material_changed=True),
            outcome(status="quiet", created=True),
            dataclasses.replace(outcome(), status="future"),
        )
        for item in cases:
            self.assertIs(
                self.map(CorrelationResult(True, False, (item,))).decision.route,
                RouteKind.UNKNOWN,
            )
        reversed_domains = (
            outcome(CorrelationDomain.NETWORK, status="quiet", task_id=None, created=False),
            outcome(CorrelationDomain.CONTROLLER, status="quiet", task_id=None, created=False),
        )
        self.assertIs(
            self.map(CorrelationResult(True, False, reversed_domains)).decision.route,
            RouteKind.UNKNOWN,
        )

    def test_foreign_values_are_rejected_and_inputs_are_frozen(self) -> None:
        with self.assertRaises(ContractError):
            map_correlation_observation(
                {}, CorrelationResult(True, False, ()),  # type: ignore[arg-type]
                source_instance="collector-a", projection_snapshot=snapshot(ProjectionOutcome.APPLIED),
            )
        with self.assertRaises(ContractError):
            ProjectionObservation("projection-1", "batch-1", "applied")  # type: ignore[arg-type]
        observed = ProjectionObservation("projection-1", "batch-1", ProjectionOutcome.APPLIED)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            observed.batch_id = "other"  # type: ignore[misc]

    def test_mapper_does_not_invoke_correlation_or_projection_replay(self) -> None:
        forbidden = mock.Mock(side_effect=AssertionError("correlation was invoked"))
        with mock.patch(
            "terracompute_ops.incident_correlation.IncidentCorrelator.process", forbidden,
        ), mock.patch(
            "terracompute_ops.incident_correlation.IncidentCorrelator.replay_state_projections",
            forbidden,
        ):
            mapped = self.map(CorrelationResult(True, False, (outcome(),)))
        self.assertIs(mapped.decision.route, RouteKind.NEW_TASK)
        forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
