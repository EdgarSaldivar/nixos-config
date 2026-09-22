from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from terracompute_ops.plans import ContractError
from terracompute_ops.shadow_adapter import map_task_routing
from terracompute_ops.shadow_rollout import RouteKind
from terracompute_ops.task_routing_preview import (
    TaskRoutingCapture,
    TaskRoutingPreview,
    TaskRoutingPreviewInput,
    TaskRoutingPreviewKind,
    capture_task_routing,
    preview_task_routing,
)
from terracompute_ops.task_service import (
    TaskDisposition,
    TaskRoutingError,
    TaskService,
)
from terracompute_ops.tasks import TaskStore
from terracompute_ops.telegram import AuthenticatedInput, InputKind


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
GROUP = -10017049
NAMESPACE = "operator-preview-test"


def message(
    update_id: int,
    text: str,
    *,
    sender_id: int = 42,
    group_id: int = GROUP,
    message_id: int | None = None,
    callback_id: str | None = None,
) -> AuthenticatedInput:
    return AuthenticatedInput(
        update_id=update_id,
        group_id=group_id,
        sender_id=sender_id,
        message_id=(1000 + update_id) if message_id is None else message_id,
        callback_id=callback_id,
        kind=InputKind.QUESTION,
        subject_id=None,
        nonce=" ".join(text.split()) or None,
        text=text,
    )


def preview_of(envelope: AuthenticatedInput) -> TaskRoutingPreview:
    return preview_task_routing(
        TaskRoutingPreviewInput.from_envelope(envelope, group_id=GROUP)
    )


class PreviewPurityTests(unittest.TestCase):
    def test_preview_input_and_result_are_frozen(self) -> None:
        preview_input = TaskRoutingPreviewInput.from_envelope(
            message(1, "/task new deploy"), group_id=GROUP
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            preview_input.text = "mutated"  # type: ignore[misc]
        preview = preview_task_routing(preview_input)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            preview.kind = TaskRoutingPreviewKind.INVALID  # type: ignore[misc]

    def test_preview_is_deterministic_and_side_effect_free(self) -> None:
        preview_input = TaskRoutingPreviewInput.from_envelope(
            message(1, "look at the gpu"), group_id=GROUP
        )
        self.assertEqual(
            preview_task_routing(preview_input), preview_task_routing(preview_input)
        )

    def test_preview_rejects_foreign_input_object(self) -> None:
        with self.assertRaises(ContractError):
            preview_task_routing({"text": "/task new x"})  # type: ignore[arg-type]

    def test_preview_result_invariants_are_enforced(self) -> None:
        with self.assertRaises(ContractError):
            TaskRoutingPreview(TaskRoutingPreviewKind.DETERMINISTIC)
        with self.assertRaises(ContractError):
            TaskRoutingPreview(
                TaskRoutingPreviewKind.DETERMINISTIC,
                disposition=TaskDisposition.TASK_CREATED,
                reason="unexpected",
            )
        with self.assertRaises(ContractError):
            TaskRoutingPreview(TaskRoutingPreviewKind.INVALID)
        with self.assertRaises(ContractError):
            TaskRoutingPreview(
                TaskRoutingPreviewKind.CAPTURE_REQUIRED,
                disposition=TaskDisposition.FOLLOW_UP_RECORDED,
            )
        with self.assertRaises(ContractError):
            TaskRoutingPreview("deterministic")  # type: ignore[arg-type]

    def test_capture_required_never_fabricates_a_disposition(self) -> None:
        preview = preview_of(message(1, "please investigate"))
        self.assertIs(preview.kind, TaskRoutingPreviewKind.CAPTURE_REQUIRED)
        self.assertIsNone(preview.disposition)
        self.assertIsNone(preview.reason)


class CountingStore:
    """Transparent TaskStore proxy that fails the test if handle re-enters it."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return self._store.transaction()

    def __getattr__(self, name):
        return getattr(self._store, name)


class PreviewCommitParityTests(unittest.TestCase):
    """Every preview verdict must agree with a single real handle commit."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.service = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_invalid_previews_match_handle_rejections(self) -> None:
        no_message_id = AuthenticatedInput(
            update_id=3, group_id=GROUP, sender_id=42, message_id=None,
            callback_id="cb-3", kind=InputKind.APPROVAL_COMMAND,
            subject_id="proposal-1", nonce="n", text="hello",
        )
        cases = (
            message(1, "hello", group_id=GROUP + 1),
            message(2, "hello", callback_id="cb-2"),
            no_message_id,
            message(4, ""),
        )
        for envelope in cases:
            with self.subTest(text=envelope.text, group=envelope.group_id):
                self.assertIs(preview_of(envelope).kind, TaskRoutingPreviewKind.INVALID)
                with self.assertRaises(TaskRoutingError):
                    self.service.handle(envelope)

    def test_deterministic_previews_match_the_committed_disposition(self) -> None:
        deterministic = {
            "/task new deploy the probe": TaskDisposition.TASK_CREATED,
            "/task new": TaskDisposition.COMMAND_NEW_USAGE,
            "/task new   ": TaskDisposition.COMMAND_NEW_USAGE,
        }
        for offset, (text, expected) in enumerate(deterministic.items()):
            with self.subTest(text=text):
                envelope = message(100 + offset, text)
                preview = preview_of(envelope)
                self.assertIs(preview.kind, TaskRoutingPreviewKind.DETERMINISTIC)
                self.assertIs(preview.disposition, expected)
                # Preview never fabricates a task identifier even when one exists.
                result = self.service.handle(envelope)
                self.assertIs(result.disposition, expected)

    def test_new_objective_contract_rejections_are_not_previewed_as_tasks(self) -> None:
        for offset, text in enumerate((
            "/task new first line\nsecond line",
            "/task new password=hunter2",
        )):
            envelope = message(150 + offset, text)
            preview = preview_of(envelope)
            self.assertIs(preview.kind, TaskRoutingPreviewKind.INVALID)
            self.assertIsNone(preview.disposition)
            with self.assertRaises(ContractError):
                self.service.handle(envelope)

    def test_deterministic_disposition_is_invariant_on_replay(self) -> None:
        envelope = message(200, "/task new inspect the fans")
        preview = preview_of(envelope)
        first = self.service.handle(envelope)
        second = self.service.handle(envelope)
        self.assertIs(preview.disposition, TaskDisposition.TASK_CREATED)
        self.assertIs(first.disposition, TaskDisposition.TASK_CREATED)
        self.assertIs(second.disposition, TaskDisposition.TASK_CREATED)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        # Fresh creation and its durable replay diverge only in the shadow route,
        # which the capture — not the preview — resolves.
        self.assertIs(map_task_routing(first).route, RouteKind.NEW_TASK)
        self.assertIs(map_task_routing(second).route, RouteKind.DUPLICATE)

    def test_capture_required_paths_are_never_marked_deterministic(self) -> None:
        # Seed an active task so status/list/plain routing have durable state.
        self.service.handle(message(300, "/task new watch temps"))
        capture_inputs = (
            message(301, "another observation"),
            message(302, "/task list"),
            message(303, "/task"),
            message(304, "/task status"),
            message(305, "/task select task-telegram-abc"),
            message(306, "/task pause"),
            message(307, "/task resume"),
            message(308, "/task cancel"),
            message(309, "/task frobnicate now"),
        )
        for envelope in capture_inputs:
            with self.subTest(text=envelope.text):
                preview = preview_of(envelope)
                self.assertIs(
                    preview.kind, TaskRoutingPreviewKind.CAPTURE_REQUIRED
                )
                # A real commit still succeeds and yields a typed disposition that
                # the capture — not the preview — is responsible for.
                result = self.service.handle(envelope)
                self.assertIsInstance(result.disposition, TaskDisposition)


class CaptureSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.counting = CountingStore(self.store)
        self.service = TaskService(
            self.counting, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_capture_wraps_one_commit_and_maps_it(self) -> None:
        envelope = message(400, "keep an eye on the disk")
        self.assertIs(preview_of(envelope).kind, TaskRoutingPreviewKind.CAPTURE_REQUIRED)
        result = self.service.handle(envelope)
        # Exactly one commit produced the value we capture; handle is not re-run.
        self.assertEqual(self.counting.transactions, 1)
        capture = capture_task_routing(result)
        self.assertEqual(self.counting.transactions, 1)
        self.assertIs(capture.result, result)
        self.assertEqual(capture.route(), map_task_routing(result))
        self.assertIs(capture.route().route, RouteKind.NEW_TASK)

    def test_capture_rejects_untyped_results(self) -> None:
        with self.assertRaises(ContractError):
            capture_task_routing({"disposition": "task-created"})  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            capture_task_routing(None)  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            TaskRoutingCapture("not-a-result")  # type: ignore[arg-type]

    def test_capture_preserves_duplicate_replay_semantics(self) -> None:
        envelope = message(401, "watch the psu")
        first = self.service.handle(envelope)
        replay = self.service.handle(envelope)
        self.assertTrue(replay.duplicate)
        self.assertIs(
            capture_task_routing(first).route().route, RouteKind.NEW_TASK
        )
        self.assertIs(
            capture_task_routing(replay).route().route, RouteKind.DUPLICATE
        )
        self.assertIs(replay.disposition, first.disposition)


if __name__ == "__main__":
    unittest.main()
