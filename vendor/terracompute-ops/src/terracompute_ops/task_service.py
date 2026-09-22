"""Intent-neutral, durable operator task routing.

This module only records operator work and lifecycle commands.  It has no model,
planner, executor, credentials, or authority to approve an action.  Every accepted
message is bound to its exact Telegram provenance before the caller acknowledges it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .plans import MACHINE_ID
from .tasks import (
    OperatorInputRecord,
    Task,
    TaskEvent,
    TaskState,
    TaskStore,
    TERMINAL_STATES,
)
from .telegram import AuthenticatedInput


_TASK_COMMAND = re.compile(
    r"^/task(?:@[A-Za-z0-9_]{5,32})?"
    r"(?:\s+(new|select|status|list|pause|resume|cancel)(?:\s+([\s\S]*?))?)?\s*$",
    re.IGNORECASE,
)

# OperatorInputRecord permits 16 KiB for the JSON string spelling while Telegram
# permits 16 KiB of UTF-8.  Generated multi-task replies stay strictly below both.
# Building complete lines up to the bound avoids slicing through a Unicode code point.
_TASK_RESPONSE_LIMIT = 16 * 1024


class TaskRoutingError(RuntimeError):
    """The authenticated envelope cannot safely be attached to a task."""


class TaskDisposition(str, Enum):
    TASK_CREATED = "task-created"
    FOLLOW_UP_RECORDED = "follow-up-recorded"
    AMBIGUOUS_ROUTING = "ambiguous-routing"
    COMMAND_STATUS = "command-status"
    COMMAND_SELECTED = "command-selected"
    COMMAND_PAUSED = "command-paused"
    COMMAND_RESUMED = "command-resumed"
    COMMAND_CANCELLED = "command-cancelled"
    COMMAND_LIST = "command-list"
    COMMAND_LIST_EMPTY = "command-list-empty"
    COMMAND_NEW_USAGE = "command-new-usage"
    COMMAND_NO_MATCH = "command-no-match"
    COMMAND_AMBIGUOUS = "command-ambiguous"
    COMMAND_TERMINAL_SELECT = "command-terminal-select"
    COMMAND_TERMINAL_PAUSE = "command-terminal-pause"
    COMMAND_TERMINAL_CANCEL = "command-terminal-cancel"
    COMMAND_ALREADY_PAUSED = "command-already-paused"
    COMMAND_NOT_PAUSED = "command-not-paused"
    COMMAND_UNRECOGNISED = "command-unrecognised"


@dataclass(frozen=True)
class TaskRoutingResult:
    response: str
    task_id: str | None = None
    created: bool = False
    duplicate: bool = False
    ambiguous: bool = False
    disposition: TaskDisposition | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not TaskDisposition:
            raise TaskRoutingError("task routing disposition must be typed")


@dataclass(frozen=True)
class TaskRecovery:
    active_tasks: tuple[Task, ...]
    current_task: Task | None
    ambiguous: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskService:
    """Attach authenticated operator text to durable task event streams."""

    def __init__(
        self,
        store: TaskStore,
        *,
        group_id: int,
        namespace: str,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if not namespace or len(namespace) > 128:
            raise ValueError("task namespace must be non-empty and bounded")
        self.store = store
        self.group_id = int(group_id)
        self.namespace = namespace
        self.clock = clock

    def requester_id(self, sender_id: int) -> str:
        """Durably bind a Telegram requester to this service namespace.

        Task's existing requester field is the backwards-compatible ownership
        contract, so namespace isolation needs no table or serialized-schema change.
        There is no deployed Phase 1 data requiring a legacy identity alias.
        """
        return f"telegram:{self.namespace}:user:{int(sender_id)}"

    def list_tasks(
        self, sender_id: int, *, include_terminal: bool = True
    ) -> tuple[Task, ...]:
        return self.store.list_tasks(
            requester_group_id=self.group_id,
            requester_id=self.requester_id(sender_id),
            include_terminal=include_terminal,
        )

    def current_task(self, sender_id: int) -> Task | None:
        active = self.list_tasks(sender_id, include_terminal=False)
        if not active:
            return None
        selected = self._selected_task(active, sender_id)
        if selected is not None:
            return selected
        return active[0] if len(active) == 1 else None

    def recover(self, sender_id: int) -> TaskRecovery:
        active = self.list_tasks(sender_id, include_terminal=False)
        current = self._selected_task(active, sender_id)
        if current is None and len(active) == 1:
            current = active[0]
        return TaskRecovery(active, current, len(active) > 1 and current is None)

    def handle(self, envelope: AuthenticatedInput) -> TaskRoutingResult:
        """Persist one input, returning text that is safe to acknowledge afterward."""
        self._validate_envelope(envelope)
        identity = self._identity(envelope)
        digest = self._input_digest(envelope)
        input_id = self._event_id(envelope)
        with self.store.transaction():
            stored = self.store.get_operator_input(input_id)
            if stored is not None:
                if (
                    dict(stored.provenance) != identity
                    or stored.input_digest != digest
                ):
                    raise TaskRoutingError(
                        "Telegram update identity was replayed with different content"
                    )
                try:
                    disposition = TaskDisposition(stored.disposition)
                except ValueError as error:
                    raise TaskRoutingError(
                        "stored task routing disposition is unknown"
                    ) from error
                return TaskRoutingResult(
                    response=stored.response,
                    task_id=stored.task_id,
                    created=stored.created,
                    duplicate=True,
                    ambiguous=stored.ambiguous,
                    disposition=disposition,
                )

            result = self._legacy_event_replay(envelope)
            if result is None:
                command = _TASK_COMMAND.fullmatch(envelope.text)
                if command is not None:
                    verb = (command.group(1) or "status").casefold()
                    argument = (command.group(2) or "").strip()
                    result = self._command(envelope, verb, argument)
                else:
                    result = self._route_message(
                        envelope, objective=self._objective(envelope), separate=False
                    )
            record = OperatorInputRecord(
                input_id=input_id,
                recorded_at=self.clock(),
                provenance=identity,
                input_digest=digest,
                disposition=result.disposition.value,
                task_id=result.task_id,
                response_json=json.dumps(result.response, ensure_ascii=False),
                created=result.created,
                ambiguous=result.ambiguous,
            )
            self.store.record_operator_input(record)
            return result

    def _validate_envelope(self, envelope: AuthenticatedInput) -> None:
        if envelope.group_id != self.group_id:
            raise TaskRoutingError("authenticated input is from a foreign operator group")
        if envelope.message_id is None or envelope.callback_id is not None:
            raise TaskRoutingError("task input must be an ordinary Telegram message")
        if not envelope.text:
            raise TaskRoutingError("task input is empty")

    @staticmethod
    def _objective(envelope: AuthenticatedInput) -> str:
        # /ask remains a compatibility spelling; provenance retains the exact wrapper.
        if envelope.text.lstrip().casefold().startswith("/ask") and envelope.nonce:
            return str(envelope.nonce)
        # Telegram accepts multiline text. Phase 0 contracts deliberately reject
        # literal control characters, so use the parser's whitespace-normalized
        # conversational form as the objective while the reversible JSON spelling in
        # operator_input below retains the exact original input.
        if not all(character.isprintable() for character in envelope.text):
            return str(envelope.nonce or " ".join(envelope.text.split()))
        return envelope.text

    def _identity(self, envelope: AuthenticatedInput) -> Mapping[str, Any]:
        return {
            "transport": "telegram",
            "namespace": self.namespace,
            "update_id": envelope.update_id,
            "group_id": envelope.group_id,
            "sender_id": envelope.sender_id,
            "message_id": envelope.message_id,
            "callback_id": envelope.callback_id,
            "input_kind": envelope.kind.value,
            "subject_id": envelope.subject_id,
            "nonce": envelope.nonce,
            # Store the JSON string spelling, not a lossy cleaned form. It is exactly
            # reversible while respecting the task contract's ban on literal control
            # characters in durable values.
            "text_json": json.dumps(envelope.text, ensure_ascii=False),
        }

    def _input_digest(self, envelope: AuthenticatedInput) -> str:
        encoded = json.dumps(
            self._identity(envelope), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _event_id(self, envelope: AuthenticatedInput) -> str:
        key = f"{self.namespace}:{envelope.group_id}:{envelope.update_id}".encode()
        return "telegram-input:" + hashlib.sha256(key).hexdigest()

    def _task_id(self, envelope: AuthenticatedInput) -> str:
        return "task-telegram-" + self._input_digest(envelope)[:32]

    def _origin_id(self, envelope: AuthenticatedInput) -> str:
        return (
            f"telegram:{self.namespace}:{envelope.group_id}:"
            f"{envelope.update_id}:{envelope.message_id}"
        )

    def _legacy_event_replay(
        self, envelope: AuthenticatedInput
    ) -> TaskRoutingResult | None:
        """Upgrade an event-backed v2 retry into the intake ledger without mutation."""
        event = self.store.get_event(self._event_id(envelope))
        if event is None:
            return None
        if dict(event.payload).get("operator_input") != self._identity(envelope):
            raise TaskRoutingError(
                "Telegram update identity was replayed with different content"
            )
        task = self.store.get_task(event.task_id)
        if task is None:
            raise TaskRoutingError("Telegram input references a missing task")
        historical = replace(task, state=event.to_state)
        if event.event_type == "operator-task-created":
            return TaskRoutingResult(
                self._acknowledgement(historical, created=True), task.task_id,
                created=True, duplicate=True, disposition=TaskDisposition.TASK_CREATED,
            )
        if event.event_type == "operator-message":
            return TaskRoutingResult(
                self._acknowledgement(historical), task.task_id,
                duplicate=True, disposition=TaskDisposition.FOLLOW_UP_RECORDED,
            )
        if event.event_type == "task-status-requested":
            response = self._task_line(historical)
            disposition = TaskDisposition.COMMAND_STATUS
        elif event.event_type == "task-selected":
            response = f"Selected {task.task_id} ({event.to_state.value})."
            disposition = TaskDisposition.COMMAND_SELECTED
        elif event.event_type == "task-paused":
            response = f"Paused {task.task_id}. No task mutation is authorised."
            disposition = TaskDisposition.COMMAND_PAUSED
        elif event.event_type == "task-resumed":
            response = f"Resumed {task.task_id} in {event.to_state.value}."
            disposition = TaskDisposition.COMMAND_RESUMED
        elif event.event_type == "task-cancelled":
            response = f"Cancelled {task.task_id}. This did not approve any action."
            disposition = TaskDisposition.COMMAND_CANCELLED
        else:
            raise TaskRoutingError("Telegram input has an unsupported legacy task event")
        return TaskRoutingResult(
            response, task.task_id, duplicate=True, disposition=disposition
        )

    def _route_message(
        self, envelope: AuthenticatedInput, *, objective: str, separate: bool
    ) -> TaskRoutingResult:
        if not objective:
            raise TaskRoutingError("task objective is empty")
        active = self.list_tasks(envelope.sender_id, include_terminal=False)
        if separate or not active:
            return self._create(envelope, objective)
        task = self._selected_task(active, envelope.sender_id)
        if task is None:
            if len(active) == 1:
                task = active[0]
            else:
                return TaskRoutingResult(
                    self._ambiguity_text(active), ambiguous=True,
                    disposition=TaskDisposition.AMBIGUOUS_ROUTING,
                )
        event = self._event(
            envelope,
            task,
            "operator-message",
            task.state,
            extra={"message_role": "follow-up"},
        )
        self.store.append_event(event)
        refreshed = self.store.get_task(task.task_id) or task
        return TaskRoutingResult(
            self._acknowledgement(refreshed), task.task_id,
            disposition=TaskDisposition.FOLLOW_UP_RECORDED,
        )

    def _create(self, envelope: AuthenticatedInput, objective: str) -> TaskRoutingResult:
        now = self.clock()
        digest = self._input_digest(envelope)
        task = Task(
            task_id=self._task_id(envelope),
            requester_id=self.requester_id(envelope.sender_id),
            requester_group_id=envelope.group_id,
            origin_message_id=self._origin_id(envelope),
            created_at=now,
            objective=objective,
            constraints=(),
            state=TaskState.INVESTIGATING,
            evidence_revision=f"operator-input:{digest}",
            model_thread=None,
            attempt_count=0,
            deadline=None,
            budgets={},
            incident_ids=(),
            machine_id=MACHINE_ID,
        )
        event = TaskEvent(
            event_id=self._event_id(envelope),
            task_id=task.task_id,
            sequence=1,
            event_type="operator-task-created",
            actor_id=task.requester_id,
            occurred_at=now,
            from_state=None,
            to_state=TaskState.INVESTIGATING,
            payload={
                "operator_input": self._identity(envelope),
                "message_role": "objective",
                "selects_task": True,
            },
            evidence_revision=task.evidence_revision,
        )
        self.store.create_task(task, event)
        return TaskRoutingResult(
            self._acknowledgement(task, created=True), task.task_id, True,
            disposition=TaskDisposition.TASK_CREATED,
        )

    def _event(
        self,
        envelope: AuthenticatedInput,
        task: Task,
        event_type: str,
        to_state: TaskState,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> TaskEvent:
        payload: dict[str, Any] = {"operator_input": self._identity(envelope)}
        payload.update(extra or {})
        return TaskEvent(
            event_id=self._event_id(envelope),
            task_id=task.task_id,
            sequence=len(self.store.events(task.task_id)) + 1,
            event_type=event_type,
            actor_id=self.requester_id(envelope.sender_id),
            occurred_at=self.clock(),
            from_state=task.state,
            to_state=to_state,
            payload=payload,
        )

    def _selected_task(self, active: tuple[Task, ...], sender_id: int) -> Task | None:
        active_by_id = {task.task_id: task for task in active}
        requester = self.requester_id(sender_id)
        selections: list[tuple[int, str]] = []
        for task in active:
            for event in self.store.events(task.task_id):
                if event.actor_id != requester:
                    continue
                payload = dict(event.payload)
                identity = payload.get("operator_input")
                if not isinstance(identity, Mapping):
                    continue
                if (
                    identity.get("namespace") != self.namespace
                    or identity.get("group_id") != self.group_id
                ):
                    continue
                if event.event_type == "task-selected" or payload.get("selects_task") is True:
                    update_id = identity.get("update_id")
                    if isinstance(update_id, int) and not isinstance(update_id, bool):
                        selections.append((update_id, task.task_id))
        if not selections:
            return None
        selected_id = max(selections)[1]
        return active_by_id.get(selected_id)

    def _owned_task(
        self, sender_id: int, reference: str
    ) -> tuple[Task | None, bool]:
        matches = [
            task for task in self.list_tasks(sender_id)
            if task.task_id == reference or task.task_id.startswith(reference)
        ]
        return (matches[0], False) if len(matches) == 1 else (None, len(matches) > 1)

    def _command_target(self, sender_id: int, argument: str) -> tuple[Task | None, bool]:
        if argument:
            return self._owned_task(sender_id, argument)
        active = self.list_tasks(sender_id, include_terminal=False)
        selected = self._selected_task(active, sender_id)
        if selected is not None:
            return selected, False
        if len(active) == 1:
            return active[0], False
        return None, len(active) > 1

    def _command(
        self, envelope: AuthenticatedInput, verb: str, argument: str
    ) -> TaskRoutingResult:
        if verb == "new":
            if not argument:
                return TaskRoutingResult(
                    "Use /task new <objective> to start a separate task.",
                    disposition=TaskDisposition.COMMAND_NEW_USAGE,
                )
            return self._route_message(envelope, objective=argument, separate=True)
        if verb == "list":
            tasks = self.list_tasks(envelope.sender_id)
            if not tasks:
                return TaskRoutingResult(
                    "No tasks are recorded for you in this group.",
                    disposition=TaskDisposition.COMMAND_LIST_EMPTY,
                )
            return TaskRoutingResult(
                self._bounded_task_text("Tasks:", tasks),
                disposition=TaskDisposition.COMMAND_LIST,
            )

        task, ambiguous = self._command_target(envelope.sender_id, argument)
        if task is None:
            active = self.list_tasks(envelope.sender_id, include_terminal=False)
            if ambiguous:
                return TaskRoutingResult(
                    self._ambiguity_text(active), ambiguous=True,
                    disposition=TaskDisposition.COMMAND_AMBIGUOUS,
                )
            return TaskRoutingResult(
                "No matching task. Use /task list, then /task select <task-id>.",
                disposition=TaskDisposition.COMMAND_NO_MATCH,
            )
        if verb == "status":
            self.store.append_event(self._event(
                envelope, task, "task-status-requested", task.state
            ))
            return TaskRoutingResult(
                self._task_line(task), task.task_id,
                disposition=TaskDisposition.COMMAND_STATUS,
            )
        if verb == "select":
            if task.state in TERMINAL_STATES:
                return TaskRoutingResult(
                    f"{task.task_id} is {task.state.value} and cannot receive follow-ups.",
                    task.task_id,
                    disposition=TaskDisposition.COMMAND_TERMINAL_SELECT,
                )
            self.store.append_event(self._event(
                envelope, task, "task-selected", task.state,
                extra={"selects_task": True},
            ))
            return TaskRoutingResult(
                f"Selected {task.task_id} ({task.state.value}).", task.task_id,
                disposition=TaskDisposition.COMMAND_SELECTED,
            )
        if verb == "pause":
            if task.state in TERMINAL_STATES:
                return TaskRoutingResult(
                    f"{task.task_id} is already {task.state.value}.", task.task_id,
                    disposition=TaskDisposition.COMMAND_TERMINAL_PAUSE,
                )
            if task.state is TaskState.PAUSED:
                return TaskRoutingResult(
                    f"{task.task_id} is already paused.", task.task_id,
                    disposition=TaskDisposition.COMMAND_ALREADY_PAUSED,
                )
            self.store.append_event(self._event(
                envelope, task, "task-paused", TaskState.PAUSED,
                extra={"resume_state": task.state.value},
            ))
            return TaskRoutingResult(
                f"Paused {task.task_id}. No task mutation is authorised.", task.task_id,
                disposition=TaskDisposition.COMMAND_PAUSED,
            )
        if verb == "resume":
            if task.state is not TaskState.PAUSED:
                return TaskRoutingResult(
                    f"{task.task_id} is not paused.", task.task_id,
                    disposition=TaskDisposition.COMMAND_NOT_PAUSED,
                )
            resume_state = self._resume_state(task)
            self.store.append_event(self._event(
                envelope, task, "task-resumed", resume_state,
                extra={"resumed_to": resume_state.value},
            ))
            return TaskRoutingResult(
                f"Resumed {task.task_id} in {resume_state.value}.", task.task_id,
                disposition=TaskDisposition.COMMAND_RESUMED,
            )
        if verb == "cancel":
            if task.state in TERMINAL_STATES:
                return TaskRoutingResult(
                    f"{task.task_id} is already {task.state.value}.", task.task_id,
                    disposition=TaskDisposition.COMMAND_TERMINAL_CANCEL,
                )
            self.store.append_event(self._event(
                envelope, task, "task-cancelled", TaskState.CANCELLED
            ))
            return TaskRoutingResult(
                f"Cancelled {task.task_id}. This did not approve any action.", task.task_id,
                disposition=TaskDisposition.COMMAND_CANCELLED,
            )
        return TaskRoutingResult(
            "Task command not recognised. Use /task new, list, select, status, pause, resume, or cancel.",
            disposition=TaskDisposition.COMMAND_UNRECOGNISED,
        )

    def _resume_state(self, task: Task) -> TaskState:
        for event in reversed(self.store.events(task.task_id)):
            if event.event_type != "task-paused":
                continue
            value = dict(event.payload).get("resume_state")
            try:
                state = TaskState(str(value))
            except ValueError:
                return TaskState.INVESTIGATING
            return state if state is not TaskState.PAUSED and state not in TERMINAL_STATES else TaskState.INVESTIGATING
        return TaskState.INVESTIGATING

    @staticmethod
    def _task_line(task: Task) -> str:
        objective = " ".join(task.objective.split())
        if len(objective) > 96:
            objective = objective[:93] + "..."
        return f"{task.task_id} — {task.state.value} — {objective}"

    def _ambiguity_text(self, tasks: tuple[Task, ...]) -> str:
        return self._bounded_task_text(
            "More than one task is active; I did not attach this message. Select one with "
            "/task select <task-id>:",
            tasks,
        )

    def _bounded_task_text(self, heading: str, tasks: tuple[Task, ...]) -> str:
        """Render the largest deterministic prefix that fits the durable wire bound."""
        lines = tuple(self._task_line(task) for task in tasks)
        selected: list[str] = []
        for index, line in enumerate(lines):
            omitted = len(lines) - index - 1
            candidate_parts = [heading, *selected, line]
            if omitted:
                candidate_parts.append(f"… {omitted} task(s) omitted.")
            candidate = "\n".join(candidate_parts)
            if not self._task_response_fits(candidate):
                break
            selected.append(line)
        omitted = len(lines) - len(selected)
        parts = [heading, *selected]
        if omitted:
            parts.append(f"… {omitted} task(s) omitted.")
        response = "\n".join(parts)
        # The fixed headings and omission footer are tiny, so an overrun here would
        # mean a future task-line contract changed without updating this formatter.
        if not self._task_response_fits(response):
            raise TaskRoutingError("bounded task response cannot fit the wire contract")
        return response

    @staticmethod
    def _task_response_fits(response: str) -> bool:
        return (
            len(response.encode("utf-8")) < _TASK_RESPONSE_LIMIT
            and len(json.dumps(response, ensure_ascii=False)) < _TASK_RESPONSE_LIMIT
        )

    @staticmethod
    def _acknowledgement(task: Task, *, created: bool = False) -> str:
        verb = "Created" if created else "Recorded on"
        return f"{verb} task {task.task_id}; state: {task.state.value}."


__all__ = [
    "TaskRecovery", "TaskRoutingError", "TaskRoutingResult", "TaskService",
]
