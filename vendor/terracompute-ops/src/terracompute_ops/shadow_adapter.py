"""Pure, disabled Phase 7 adapters for already-captured decision values.

This module does not call a gateway, task service, legacy loop, policy service,
model, executor, or ledger.  It only maps a ``TaskRoutingResult`` that a caller
already obtained into the strict shadow routing vocabulary.  Runtime capture and
the remaining policy/legacy/correlation adapters are separate work.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .plans import ContractError
from .shadow_rollout import RouteKind, RoutingDecision
from .task_service import TaskDisposition, TaskRoutingResult


TASK_DISPOSITION_ROUTES: Mapping[TaskDisposition, RouteKind] = MappingProxyType({
    TaskDisposition.TASK_CREATED: RouteKind.NEW_TASK,
    TaskDisposition.FOLLOW_UP_RECORDED: RouteKind.EXISTING_TASK,
    TaskDisposition.AMBIGUOUS_ROUTING: RouteKind.AMBIGUOUS,
    TaskDisposition.COMMAND_RESUMED: RouteKind.EXISTING_TASK,
    TaskDisposition.COMMAND_AMBIGUOUS: RouteKind.AMBIGUOUS,
    TaskDisposition.COMMAND_STATUS: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_SELECTED: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_PAUSED: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_CANCELLED: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_ALREADY_PAUSED: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_LIST: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_LIST_EMPTY: RouteKind.NO_ROUTE,
    TaskDisposition.COMMAND_NEW_USAGE: RouteKind.REJECTED,
    TaskDisposition.COMMAND_NO_MATCH: RouteKind.REJECTED,
    TaskDisposition.COMMAND_TERMINAL_SELECT: RouteKind.REJECTED,
    TaskDisposition.COMMAND_TERMINAL_PAUSE: RouteKind.REJECTED,
    TaskDisposition.COMMAND_TERMINAL_CANCEL: RouteKind.REJECTED,
    TaskDisposition.COMMAND_NOT_PAUSED: RouteKind.REJECTED,
    TaskDisposition.COMMAND_UNRECOGNISED: RouteKind.REJECTED,
})

if set(TASK_DISPOSITION_ROUTES) != set(TaskDisposition):
    raise RuntimeError("every task disposition needs an explicit shadow route")


def map_task_routing(result: TaskRoutingResult) -> RoutingDecision:
    """Map one already-persisted task result without invoking either path.

    Contradictory flags, missing task bindings, or an unrepresentable taskless
    duplicate become ``UNKNOWN``.  No identifier is fabricated.  A caller that
    is replaying a previously captured request should reuse the original shadow
    comparison rather than record another sample; this duplicate mapping exists
    only for a genuine observed suppression decision.
    """
    if type(result) is not TaskRoutingResult:
        raise ContractError("task routing mapping requires a typed result")
    disposition = result.disposition
    if type(disposition) is not TaskDisposition:
        return RoutingDecision(RouteKind.UNKNOWN)
    if result.created and result.ambiguous:
        return RoutingDecision(RouteKind.UNKNOWN)
    if result.created and disposition is not TaskDisposition.TASK_CREATED:
        return RoutingDecision(RouteKind.UNKNOWN)
    if disposition is TaskDisposition.TASK_CREATED and not result.created:
        return RoutingDecision(RouteKind.UNKNOWN)
    if result.ambiguous and disposition not in {
        TaskDisposition.AMBIGUOUS_ROUTING, TaskDisposition.COMMAND_AMBIGUOUS,
    }:
        return RoutingDecision(RouteKind.UNKNOWN)
    if disposition in {
        TaskDisposition.AMBIGUOUS_ROUTING, TaskDisposition.COMMAND_AMBIGUOUS,
    } and not result.ambiguous:
        return RoutingDecision(RouteKind.UNKNOWN)
    if result.duplicate:
        if result.task_id is None:
            return RoutingDecision(RouteKind.UNKNOWN)
        return RoutingDecision(RouteKind.DUPLICATE, result.task_id)

    route = TASK_DISPOSITION_ROUTES[disposition]
    if route is RouteKind.NEW_TASK:
        if result.task_id is None:
            return RoutingDecision(RouteKind.UNKNOWN)
        return RoutingDecision(route)
    if route is RouteKind.EXISTING_TASK:
        if result.task_id is None:
            return RoutingDecision(RouteKind.UNKNOWN)
        return RoutingDecision(route, result.task_id)
    if route is RouteKind.AMBIGUOUS:
        if result.task_id is not None:
            return RoutingDecision(RouteKind.UNKNOWN)
        return RoutingDecision(route)
    return RoutingDecision(route)


__all__ = ["TASK_DISPOSITION_ROUTES", "map_task_routing"]
