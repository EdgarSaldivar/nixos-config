"""Authenticated transport boundary for durable operator tasks."""

from __future__ import annotations

from .task_service import TaskRoutingResult, TaskService
from .telegram import AuthenticatedInput, InputKind


class OperatorGateway:
    """Admit ordinary authenticated Telegram messages, never callbacks/authority."""

    def __init__(self, tasks: TaskService):
        self.tasks = tasks

    def handle(self, envelope: AuthenticatedInput) -> TaskRoutingResult:
        if envelope.kind is not InputKind.QUESTION:
            raise ValueError("operator gateway accepts ordinary messages only")
        return self.tasks.handle(envelope)


__all__ = ["OperatorGateway"]
