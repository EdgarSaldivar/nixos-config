from __future__ import annotations

import dataclasses
import unittest

from terracompute_ops.plans import ContractError
from terracompute_ops.shadow_adapter import TASK_DISPOSITION_ROUTES, map_task_routing
from terracompute_ops.shadow_rollout import RouteKind, RoutingDecision
from terracompute_ops.task_service import TaskDisposition, TaskRoutingResult


class TaskRoutingAdapterTest(unittest.TestCase):
    def result(self, disposition: TaskDisposition, **overrides) -> TaskRoutingResult:
        values = dict(
            response="captured response", task_id=None, created=False,
            duplicate=False, ambiguous=False, disposition=disposition,
        )
        values.update(overrides)
        return TaskRoutingResult(**values)

    def test_mapping_is_exhaustive_and_immutable(self) -> None:
        self.assertEqual(set(TASK_DISPOSITION_ROUTES), set(TaskDisposition))
        with self.assertRaises(TypeError):
            TASK_DISPOSITION_ROUTES[TaskDisposition.TASK_CREATED] = RouteKind.UNKNOWN

    def test_all_nineteen_dispositions_have_conservative_routes(self) -> None:
        cases = {
            TaskDisposition.TASK_CREATED: self.result(
                TaskDisposition.TASK_CREATED, task_id="task-1", created=True),
            TaskDisposition.FOLLOW_UP_RECORDED: self.result(
                TaskDisposition.FOLLOW_UP_RECORDED, task_id="task-1"),
            TaskDisposition.AMBIGUOUS_ROUTING: self.result(
                TaskDisposition.AMBIGUOUS_ROUTING, ambiguous=True),
            TaskDisposition.COMMAND_AMBIGUOUS: self.result(
                TaskDisposition.COMMAND_AMBIGUOUS, ambiguous=True),
            TaskDisposition.COMMAND_RESUMED: self.result(
                TaskDisposition.COMMAND_RESUMED, task_id="task-1"),
        }
        for disposition in TaskDisposition:
            result = cases.get(disposition, self.result(disposition, task_id="task-1"))
            decision = map_task_routing(result)
            self.assertIs(decision.route, TASK_DISPOSITION_ROUTES[disposition])
            if decision.route is RouteKind.EXISTING_TASK:
                self.assertEqual(decision.task_id, "task-1")
            else:
                self.assertIsNone(decision.task_id)

    def test_duplicate_requires_a_real_task_binding(self) -> None:
        duplicate = self.result(
            TaskDisposition.FOLLOW_UP_RECORDED, task_id="task-1", duplicate=True)
        self.assertEqual(
            map_task_routing(duplicate), RoutingDecision(RouteKind.DUPLICATE, "task-1"))
        taskless = dataclasses.replace(duplicate, task_id=None)
        self.assertIs(map_task_routing(taskless).route, RouteKind.UNKNOWN)

    def test_contradictory_or_incomplete_results_are_unknown(self) -> None:
        cases = (
            self.result(TaskDisposition.TASK_CREATED, created=True),
            self.result(TaskDisposition.TASK_CREATED, task_id="task-1"),
            self.result(TaskDisposition.FOLLOW_UP_RECORDED),
            self.result(TaskDisposition.COMMAND_RESUMED),
            self.result(TaskDisposition.COMMAND_STATUS, created=True, task_id="task-1"),
            self.result(TaskDisposition.COMMAND_AMBIGUOUS),
            self.result(TaskDisposition.COMMAND_AMBIGUOUS, ambiguous=True, task_id="task-1"),
            self.result(TaskDisposition.COMMAND_NO_MATCH, ambiguous=True),
        )
        for result in cases:
            with self.subTest(result=result):
                decision = map_task_routing(result)
                self.assertIs(decision.route, RouteKind.UNKNOWN)
                self.assertIsNone(decision.task_id)

    def test_mapper_rejects_foreign_objects_and_performs_no_mutation(self) -> None:
        with self.assertRaises(ContractError):
            map_task_routing({"disposition": "task-created"})
        result = self.result(
            TaskDisposition.TASK_CREATED, task_id="task-1", created=True)
        before = dataclasses.asdict(result)
        self.assertEqual(map_task_routing(result), map_task_routing(result))
        self.assertEqual(dataclasses.asdict(result), before)


if __name__ == "__main__":
    unittest.main()
