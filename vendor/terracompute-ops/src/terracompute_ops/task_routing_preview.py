"""Pure, disabled Phase 7 preview/capture seam for task routing.

``TaskService.handle`` combines the routing *decision* with the durable commit in
one transaction.  This module extracts the part of that decision that is a pure
function of the authenticated envelope, so a later shadow producer can observe it
without a store, clock, executor, model, network, or callback — and without ever
invoking ``handle`` a second time.

Only three routing outcomes are store-independent, and each is invariant across a
fresh input and its durable replay (the idempotent-input and legacy-event gates in
``handle`` preserve the recorded disposition for identical content):

* an envelope that ``handle`` would reject before routing (``INVALID``);
* ``/task new`` with no objective, which is always ``COMMAND_NEW_USAGE``;
* ``/task new <objective>``, which always routes to task creation
  (``_route_message(separate=True)`` unconditionally creates), so its disposition
  is always ``TASK_CREATED``.

Every other disposition depends on durable state — the active-task set, the
selected task, task lifecycle states, or the duplicate/replay gate — so it cannot
be previewed.  For those paths the caller performs exactly one real ``handle``
commit and wraps the already-derived ``TaskRoutingResult`` in
:class:`TaskRoutingCapture`.  This module never calls a gateway, task service,
store, legacy loop, policy service, model, executor, or ledger, and it fabricates
no identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .plans import ContractError, _string
from .shadow_rollout import RoutingDecision
from .shadow_adapter import map_task_routing
# Reuse the exact command grammar and disposition types the commit path uses, so
# the preview can never drift from the behaviour it previews.
from .task_service import _TASK_COMMAND, TaskDisposition, TaskRoutingResult


class TaskRoutingPreviewKind(str, Enum):
    """Whether a routing decision can be derived without durable store state."""

    INVALID = "invalid"
    DETERMINISTIC = "deterministic"
    CAPTURE_REQUIRED = "capture-required"


@dataclass(frozen=True)
class TaskRoutingPreviewInput:
    """Frozen, envelope-only inputs a pure routing preview may depend on.

    Deliberately excludes the store, clock, executor, model, network, and any
    callback.  ``from_envelope`` copies scalar fields out of the authenticated
    envelope; it reads no durable state and mutates nothing.
    """

    expected_group_id: int
    group_id: int
    message_id: int | None
    callback_id: str | None
    text: str

    @classmethod
    def from_envelope(
        cls, envelope, *, group_id: int
    ) -> "TaskRoutingPreviewInput":
        return cls(
            expected_group_id=int(group_id),
            group_id=int(envelope.group_id),
            message_id=envelope.message_id,
            callback_id=envelope.callback_id,
            text=envelope.text,
        )


@dataclass(frozen=True)
class TaskRoutingPreview:
    """The pure, deterministic portion of a routing decision.

    ``DETERMINISTIC`` carries the disposition ``handle`` will record for this
    envelope regardless of durable state.  ``INVALID`` carries the reason
    ``handle`` would raise.  ``CAPTURE_REQUIRED`` carries neither: the routing
    decision depends on durable state and must come from a single real commit.
    No identifier is ever fabricated, so ``task_id`` is always ``None`` here — a
    concrete binding lives only on the captured result.
    """

    kind: TaskRoutingPreviewKind
    disposition: TaskDisposition | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not TaskRoutingPreviewKind:
            raise ContractError("routing preview kind must be typed")
        if self.kind is TaskRoutingPreviewKind.DETERMINISTIC:
            if type(self.disposition) is not TaskDisposition:
                raise ContractError("a deterministic preview needs a typed disposition")
            if self.reason is not None:
                raise ContractError("a deterministic preview carries no reason")
        elif self.kind is TaskRoutingPreviewKind.INVALID:
            if not self.reason:
                raise ContractError("an invalid preview needs a reason")
            if self.disposition is not None:
                raise ContractError("an invalid preview has no disposition")
        else:  # CAPTURE_REQUIRED
            if self.disposition is not None or self.reason is not None:
                raise ContractError(
                    "a capture-required preview carries no disposition or reason"
                )


def preview_task_routing(preview_input: TaskRoutingPreviewInput) -> TaskRoutingPreview:
    """Derive the store-independent routing decision, else demand a capture.

    Mirrors ``TaskService._validate_envelope`` and the ``/task new`` branch of the
    commit path without touching any durable state.  Any input whose disposition
    depends on the active-task set, the selected task, task lifecycle states, or
    the duplicate/replay gate returns ``CAPTURE_REQUIRED`` rather than a guess.
    """
    if type(preview_input) is not TaskRoutingPreviewInput:
        raise ContractError("routing preview requires a typed preview input")

    # Envelope validation, in the same order as the commit path.
    if preview_input.group_id != preview_input.expected_group_id:
        return TaskRoutingPreview(
            TaskRoutingPreviewKind.INVALID,
            reason="authenticated input is from a foreign operator group",
        )
    if preview_input.message_id is None or preview_input.callback_id is not None:
        return TaskRoutingPreview(
            TaskRoutingPreviewKind.INVALID,
            reason="task input must be an ordinary Telegram message",
        )
    if not preview_input.text:
        return TaskRoutingPreview(
            TaskRoutingPreviewKind.INVALID, reason="task input is empty"
        )

    command = _TASK_COMMAND.fullmatch(preview_input.text)
    if command is not None and (command.group(1) or "status").casefold() == "new":
        argument = (command.group(2) or "").strip()
        if not argument:
            # /task new with no objective is a fixed usage reply.
            return TaskRoutingPreview(
                TaskRoutingPreviewKind.DETERMINISTIC,
                disposition=TaskDisposition.COMMAND_NEW_USAGE,
            )
        try:
            # The commit path constructs a Task whose contract applies this
            # exact objective validation before anything is persisted.  Keep
            # credential-shaped/control-character input from being previewed
            # as executable when the real path will reject it.
            _string(argument, "objective")
        except ContractError as error:
            return TaskRoutingPreview(
                TaskRoutingPreviewKind.INVALID,
                reason=str(error),
            )
        # /task new <objective> always routes to a separate task creation.
        return TaskRoutingPreview(
            TaskRoutingPreviewKind.DETERMINISTIC,
            disposition=TaskDisposition.TASK_CREATED,
        )

    # Plain messages, bare/other /task verbs, and non-verb "/task ..." text all
    # depend on durable state and must be captured from one real commit.
    return TaskRoutingPreview(TaskRoutingPreviewKind.CAPTURE_REQUIRED)


@dataclass(frozen=True)
class TaskRoutingCapture:
    """Typed, immutable capture of an already-derived ``TaskRoutingResult``.

    Used wherever a pure preview is impossible.  The caller obtains ``result``
    from exactly one real ``TaskService.handle`` commit and wraps it here; the
    shadow layer maps it through the existing adapter without re-invoking any
    decision path.  This preserves durable dispositions and replay semantics —
    a replayed input keeps ``duplicate=True`` on its original disposition.
    """

    result: TaskRoutingResult

    def __post_init__(self) -> None:
        if type(self.result) is not TaskRoutingResult:
            raise ContractError("routing capture requires a typed TaskRoutingResult")

    def route(self) -> RoutingDecision:
        """Map the captured result into the strict shadow routing vocabulary."""
        return map_task_routing(self.result)


def capture_task_routing(result: TaskRoutingResult) -> TaskRoutingCapture:
    """Wrap an already-derived routing result; ``handle`` is never called here."""
    return TaskRoutingCapture(result)


__all__ = [
    "TaskRoutingPreviewKind",
    "TaskRoutingPreviewInput",
    "TaskRoutingPreview",
    "preview_task_routing",
    "TaskRoutingCapture",
    "capture_task_routing",
]
