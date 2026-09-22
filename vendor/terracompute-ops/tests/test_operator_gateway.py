from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.plans import ContractError
from terracompute_ops.action_service import ActionService
from terracompute_ops.operator_gateway import OperatorGateway
from terracompute_ops.task_service import (
    TaskDisposition,
    TaskRoutingError,
    TaskRoutingResult,
    TaskService,
)
from terracompute_ops.tasks import Task, TaskEvent, TaskState, TaskStore, TaskStoreError
from terracompute_ops.telegram import AuthenticatedInput, InputKind


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
GROUP = -10017049
NAMESPACE = "operator-phase1-test"


def message(
    update_id: int,
    text: str,
    *,
    sender_id: int = 42,
    group_id: int = GROUP,
    kind: InputKind = InputKind.QUESTION,
) -> AuthenticatedInput:
    return AuthenticatedInput(
        update_id=update_id,
        group_id=group_id,
        sender_id=sender_id,
        message_id=1000 + update_id,
        callback_id=None if kind is InputKind.QUESTION else f"callback-{update_id}",
        kind=kind,
        subject_id="proposal-1" if kind is InputKind.APPROVAL_COMMAND else None,
        nonce=("nonce-value" if kind is InputKind.APPROVAL_COMMAND else " ".join(text.split())),
        text=text,
    )


class OperatorGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        self.gateway = OperatorGateway(self.tasks)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_task_dispositions_are_closed_typed_and_wire_stable(self) -> None:
        self.assertEqual(
            {item.value for item in TaskDisposition},
            {
                "task-created", "follow-up-recorded", "ambiguous-routing",
                "command-status", "command-selected", "command-paused",
                "command-resumed", "command-cancelled", "command-list",
                "command-list-empty", "command-new-usage", "command-no-match",
                "command-ambiguous", "command-terminal-select",
                "command-terminal-pause", "command-terminal-cancel",
                "command-already-paused", "command-not-paused",
                "command-unrecognised",
            },
        )
        for disposition in TaskDisposition:
            result = TaskRoutingResult("response", disposition=disposition)
            self.assertIs(result.disposition, disposition)
            self.assertEqual(result.disposition, disposition.value)
        for invalid in (None, "task-created", "", "future-disposition"):
            with self.subTest(invalid=invalid), self.assertRaises(TaskRoutingError):
                TaskRoutingResult("response", disposition=invalid)

    def test_unknown_text_creates_task_without_an_incident_or_intent_whitelist(self) -> None:
        envelope = message(1, "florp the quux and make it durable")
        result = self.gateway.handle(envelope)
        task = self.store.get_task(result.task_id)
        self.assertTrue(result.created)
        self.assertEqual(task.state, TaskState.INVESTIGATING)
        self.assertEqual(task.objective, envelope.text)
        self.assertEqual(task.incident_ids, ())
        event = self.store.events(task.task_id)[0]
        provenance = event.payload["operator_input"]
        self.assertEqual(json.loads(provenance["text_json"]), envelope.text)
        self.assertEqual(provenance["update_id"], envelope.update_id)
        self.assertEqual(provenance["message_id"], envelope.message_id)
        self.assertEqual(provenance["sender_id"], envelope.sender_id)

    def test_unknown_task_shaped_text_is_still_accepted_as_an_objective(self) -> None:
        result = self.gateway.handle(message(40, "/task frobnicate the novel subsystem"))
        task = self.store.get_task(result.task_id)
        self.assertTrue(result.created)
        self.assertEqual(task.objective, "/task frobnicate the novel subsystem")

    def test_multiline_text_is_accepted_and_preserved_exactly(self) -> None:
        envelope = message(41, "inspect this\nthen check the repo")
        result = self.gateway.handle(envelope)
        task = self.store.get_task(result.task_id)
        event = self.store.events(task.task_id)[0]
        self.assertEqual(task.objective, "inspect this then check the repo")
        self.assertEqual(
            json.loads(event.payload["operator_input"]["text_json"]), envelope.text
        )

    def test_task_and_follow_up_survive_restart_with_context(self) -> None:
        created = self.gateway.handle(message(2, "investigate and fix the machine"))
        followed = self.gateway.handle(message(3, "also check whether the repo supports it"))
        self.assertEqual(followed.task_id, created.task_id)
        self.store.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        recovered = self.tasks.recover(42)
        self.assertEqual(recovered.current_task.task_id, created.task_id)
        events = self.store.events(created.task_id)
        self.assertEqual([event.event_type for event in events], [
            "operator-task-created", "operator-message",
        ])
        self.assertEqual(
            json.loads(events[1].payload["operator_input"]["text_json"]),
            "also check whether the repo supports it",
        )

    def test_duplicate_telegram_delivery_is_idempotent(self) -> None:
        envelope = message(4, "look into this")
        first = self.gateway.handle(envelope)
        second = self.gateway.handle(envelope)
        self.assertEqual(second.task_id, first.task_id)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.tasks.list_tasks(42)), 1)
        self.assertEqual(len(self.store.events(first.task_id)), 1)
        changed = message(4, "different replay")
        with self.assertRaisesRegex(TaskRoutingError, "different content"):
            self.gateway.handle(changed)

    def test_v2_event_only_retry_is_promoted_to_the_intake_ledger(self) -> None:
        envelope = message(49, "survive the v2 acknowledgement gap")
        first = self.gateway.handle(envelope)
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("DROP TABLE tc_task_operator_inputs")
        database.execute("DROP TABLE tc_task_creation_contracts")
        database.execute(
            "UPDATE tc_task_schema SET version=2 WHERE namespace='tasks'"
        )
        database.commit()
        database.close()

        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        self.gateway = OperatorGateway(self.tasks)
        replayed = self.gateway.handle(envelope)
        self.assertEqual(replayed.response, first.response)
        self.assertTrue(replayed.duplicate)
        self.assertEqual(len(self.store.events(first.task_id)), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            1,
        )

    def test_explicit_separate_task_and_selection_keep_streams_apart(self) -> None:
        first = self.gateway.handle(message(5, "investigate GPU inventory"))
        second = self.gateway.handle(message(6, "/task new inspect the controller repo"))
        self.assertNotEqual(first.task_id, second.task_id)
        follow_second = self.gateway.handle(message(7, "include its tests"))
        self.assertEqual(follow_second.task_id, second.task_id)
        self.gateway.handle(message(8, f"/task select {first.task_id}"))
        follow_first = self.gateway.handle(message(9, "compare with fresh machine evidence"))
        self.assertEqual(follow_first.task_id, first.task_id)
        self.assertEqual(
            [json.loads(event.payload["operator_input"]["text_json"])
             for event in self.store.events(first.task_id)],
            ["investigate GPU inventory", f"/task select {first.task_id}",
             "compare with fresh machine evidence"],
        )
        self.assertEqual(
            [json.loads(event.payload["operator_input"]["text_json"])
             for event in self.store.events(second.task_id)],
            ["/task new inspect the controller repo", "include its tests"],
        )

    def test_ambiguous_attachment_asks_for_selection_and_appends_nothing(self) -> None:
        requester = self.tasks.requester_id(42)
        for index in (1, 2):
            created_at = NOW - timedelta(minutes=3 - index)
            task = Task(
                task_id=f"external-task-{index}", requester_id=requester,
                requester_group_id=GROUP, origin_message_id=f"external-{index}",
                created_at=created_at, objective=f"detector work {index}", constraints=(),
                state=TaskState.INVESTIGATING, evidence_revision=f"external-evidence-{index}",
                model_thread=None, attempt_count=0, deadline=None, budgets={}, incident_ids=(),
            )
            self.store.create_task(task)
        result = self.gateway.handle(message(10, "this belongs with one of those"))
        self.assertTrue(result.ambiguous)
        self.assertIn("did not attach", result.response)
        self.assertEqual([len(self.store.events(task.task_id)) for task in self.tasks.list_tasks(42)],
                         [1, 1])
        row = self.store.db.execute(
            "SELECT input_id,disposition,task_id FROM tc_task_operator_inputs"
        ).fetchone()
        self.assertEqual((row["disposition"], row["task_id"]), ("ambiguous-routing", None))
        intake = self.store.get_operator_input(row["input_id"])
        self.assertEqual(intake.response, result.response)
        self.assertEqual(
            json.loads(intake.provenance["text_json"]),
            "this belongs with one of those",
        )

        self.store.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        self.gateway = OperatorGateway(self.tasks)
        replayed = self.gateway.handle(message(10, "this belongs with one of those"))
        self.assertEqual(replayed.response, result.response)
        self.assertTrue(replayed.duplicate)
        self.assertTrue(replayed.ambiguous)
        with self.assertRaisesRegex(TaskRoutingError, "different content"):
            self.gateway.handle(message(10, "changed ambiguous replay"))

    def test_no_target_and_list_outcomes_replay_exactly_across_restart(self) -> None:
        no_target = self.gateway.handle(message(42, "/task status missing-task"))
        listed = self.gateway.handle(message(43, "/task list"))
        self.assertEqual(no_target.disposition, "command-no-match")
        self.assertEqual(listed.disposition, "command-list-empty")
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            2,
        )

        self.store.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        self.gateway = OperatorGateway(self.tasks)
        no_target_replay = self.gateway.handle(message(42, "/task status missing-task"))
        list_replay = self.gateway.handle(message(43, "/task list"))
        self.assertEqual(no_target_replay.response, no_target.response)
        self.assertEqual(list_replay.response, listed.response)
        self.assertTrue(no_target_replay.duplicate)
        self.assertTrue(list_replay.duplicate)

    def test_terminal_command_outcome_replays_without_a_new_event(self) -> None:
        created = self.gateway.handle(message(44, "investigate terminal replay"))
        self.gateway.handle(message(45, f"/task cancel {created.task_id}"))
        terminal = self.gateway.handle(message(46, f"/task pause {created.task_id}"))
        event_count = len(self.store.events(created.task_id))
        self.assertEqual(terminal.disposition, "command-terminal-pause")

        self.store.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.tasks = TaskService(
            self.store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
        )
        self.gateway = OperatorGateway(self.tasks)
        replayed = self.gateway.handle(message(46, f"/task pause {created.task_id}"))
        self.assertEqual(replayed.response, terminal.response)
        self.assertTrue(replayed.duplicate)
        self.assertEqual(len(self.store.events(created.task_id)), event_count)

    def test_credential_shaped_input_is_rejected_and_never_persisted(self) -> None:
        with self.assertRaisesRegex(ContractError, "credential material"):
            self.gateway.handle(message(47, "/task list password=<redacted>"))
        self.assertEqual(self.tasks.list_tasks(42), ())
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            0,
        )

    def test_json_shaped_credential_keys_are_rejected_atomically(self) -> None:
        unsafe_inputs = (
            '{"metadata":{"api_key":"<redacted>"}}',
            '[{"nested":{"token":"<redacted>"}}]',
        )
        for update_id, text in enumerate(unsafe_inputs, start=60):
            with self.subTest(text=text), self.assertRaisesRegex(
                ContractError, "credential-shaped"
            ):
                self.gateway.handle(message(update_id, text))
        self.assertEqual(self.tasks.list_tasks(42), ())
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_events"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            0,
        )

    def test_task_ownership_is_isolated_by_service_namespace(self) -> None:
        other_namespace = "operator-phase1-other"
        other = TaskService(
            self.store, group_id=GROUP, namespace=other_namespace, clock=lambda: NOW
        )
        # Identical Telegram group, sender and update identities are valid independent
        # inputs in different service namespaces.
        first = self.tasks.handle(message(70, "namespace one objective"))
        second = other.handle(message(70, "namespace two objective"))
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(
            [task.task_id for task in self.tasks.list_tasks(42)], [first.task_id]
        )
        self.assertEqual(
            [task.task_id for task in other.list_tasks(42)], [second.task_id]
        )
        self.assertNotEqual(
            self.tasks.requester_id(42), other.requester_id(42)
        )
        self.assertEqual(self.tasks.current_task(42).task_id, first.task_id)
        self.assertEqual(other.current_task(42).task_id, second.task_id)

        listed = other.handle(message(71, "/task list"))
        self.assertIn(second.task_id, listed.response)
        self.assertNotIn(first.task_id, listed.response)
        foreign_selection = other.handle(
            message(72, f"/task select {first.task_id}")
        )
        self.assertEqual(foreign_selection.disposition, "command-no-match")

        foreign = other.handle(message(73, f"/task status {first.task_id}"))
        self.assertEqual(foreign.disposition, "command-no-match")
        followed_first = self.tasks.handle(message(74, "namespace one follow-up"))
        followed_second = other.handle(message(74, "namespace two follow-up"))
        self.assertEqual(followed_first.task_id, first.task_id)
        self.assertEqual(followed_second.task_id, second.task_id)
        self.assertEqual(self.tasks.recover(42).current_task.task_id, first.task_id)
        self.assertEqual(other.recover(42).current_task.task_id, second.task_id)
        self.assertEqual(len(self.store.events(first.task_id)), 2)
        self.assertEqual(len(self.store.events(second.task_id)), 2)

    def test_list_and_ambiguity_responses_are_utf8_bounded_with_omission_count(self) -> None:
        requester = self.tasks.requester_id(42)
        long_objective = "🧪" * 400
        for index in range(80):
            task = Task(
                task_id=f"bulk-task-{index:04d}", requester_id=requester,
                requester_group_id=GROUP, origin_message_id=f"bulk-origin-{index:04d}",
                created_at=NOW, objective=f"{long_objective}-{index}", constraints=(),
                state=TaskState.INVESTIGATING,
                evidence_revision=f"bulk-evidence-{index:04d}", model_thread=None,
                attempt_count=0, deadline=None, budgets={}, incident_ids=(),
            )
            self.store.create_task(task)

        listed = self.gateway.handle(message(80, "/task list"))
        ambiguous = self.gateway.handle(message(81, "attach this safely"))
        for result in (listed, ambiguous):
            with self.subTest(disposition=result.disposition):
                encoded = result.response.encode("utf-8")
                self.assertLess(len(encoded), 16 * 1024)
                self.assertLess(
                    len(json.dumps(result.response, ensure_ascii=False)), 16 * 1024
                )
                self.assertEqual(encoded.decode("utf-8"), result.response)
                self.assertRegex(result.response, r"… \d+ task\(s\) omitted\.$")
                intake = self.store.get_operator_input(
                    self.tasks._event_id(message(
                        80 if result is listed else 81,
                        "/task list" if result is listed else "attach this safely",
                    ))
                )
                self.assertEqual(intake.response, result.response)

    def test_intake_digest_is_verified_during_recovery(self) -> None:
        self.gateway.handle(message(50, "/task list"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable task record"):
            self.store.db.execute(
                "UPDATE tc_task_operator_inputs SET disposition='changed'"
            )
        self.store.db.rollback()
        self.store.db.execute(
            "DROP TRIGGER tc_task_operator_inputs_immutable_update"
        )
        self.store.db.execute(
            "UPDATE tc_task_operator_inputs SET input_hash=?", ("0" * 64,)
        )
        self.store.db.commit()
        self.store.close()
        with self.assertRaisesRegex(TaskStoreError, "canonical JSON/hash mismatch"):
            TaskStore(self.root, clock=lambda: NOW)

    def test_foreign_group_and_approval_callbacks_are_rejected(self) -> None:
        with self.assertRaisesRegex(TaskRoutingError, "foreign"):
            self.gateway.handle(message(11, "do work", group_id=-999))
        with self.assertRaisesRegex(ValueError, "ordinary messages"):
            self.gateway.handle(message(
                12, "/approve proposal-1 nonce-value",
                kind=InputKind.APPROVAL_COMMAND,
            ))
        self.assertEqual(self.tasks.list_tasks(42), ())

    def test_free_text_approval_language_is_only_task_input(self) -> None:
        result = self.gateway.handle(message(13, "approve whatever repair is needed"))
        events = self.store.events(result.task_id)
        self.assertEqual(events[0].event_type, "operator-task-created")
        self.assertEqual(events[0].to_state, TaskState.INVESTIGATING)
        self.assertNotIn("approval", events[0].event_type)

    def test_pause_resume_cancel_commands_are_explicit_and_do_not_approve(self) -> None:
        created = self.gateway.handle(message(14, "investigate"))
        paused = self.gateway.handle(message(15, f"/task pause {created.task_id}"))
        self.assertIn("No task mutation is authorised", paused.response)
        self.assertEqual(self.store.get_task(created.task_id).state, TaskState.PAUSED)
        self.gateway.handle(message(16, f"/task resume {created.task_id}"))
        self.assertEqual(self.store.get_task(created.task_id).state, TaskState.INVESTIGATING)
        self.gateway.handle(message(17, f"/task cancel {created.task_id}"))
        self.assertEqual(self.store.get_task(created.task_id).state, TaskState.CANCELLED)
        self.assertFalse(any("approv" in event.event_type for event in self.store.events(created.task_id)))

    def test_follow_up_does_not_withdraw_an_awaiting_approval_state(self) -> None:
        created = self.gateway.handle(message(18, "prepare a repair"))
        task = self.store.get_task(created.task_id)
        self.store.append_event(TaskEvent(
            event_id="coordinator-planning", task_id=task.task_id, sequence=2,
            event_type="planning", actor_id="coordinator", occurred_at=NOW,
            from_state=TaskState.INVESTIGATING, to_state=TaskState.PLANNING, payload={},
        ))
        self.store.append_event(TaskEvent(
            event_id="coordinator-awaiting", task_id=task.task_id, sequence=3,
            event_type="awaiting-approval", actor_id="coordinator", occurred_at=NOW,
            from_state=TaskState.PLANNING, to_state=TaskState.AWAITING_APPROVAL,
            payload={"approval_scope": "unchanged"},
        ))
        result = self.gateway.handle(message(19, "also preserve the rollback evidence"))
        current = self.store.get_task(task.task_id)
        self.assertEqual(result.task_id, task.task_id)
        self.assertEqual(current.state, TaskState.AWAITING_APPROVAL)
        self.assertIsNone(current.current_plan_version)
        self.assertEqual(self.store.events(task.task_id)[-1].event_type, "operator-message")

    def test_concurrent_first_inputs_form_one_stream(self) -> None:
        self.store.close()
        barrier = threading.Barrier(2)
        results: list[str] = []
        failures: list[BaseException] = []

        def route(update_id: int) -> None:
            try:
                with TaskStore(self.root, clock=lambda: NOW) as store:
                    service = TaskService(
                        store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
                    )
                    barrier.wait()
                    results.append(service.handle(message(update_id, f"concurrent {update_id}")).task_id)
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=route, args=(value,)) for value in (20, 21)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)
        self.store = TaskStore(self.root, clock=lambda: NOW)
        tasks = self.store.list_tasks(
            requester_group_id=GROUP, requester_id=self.tasks.requester_id(42)
        )
        self.assertEqual(len(tasks), 1)
        texts = {
            json.loads(event.payload["operator_input"]["text_json"])
            for event in self.store.events(tasks[0].task_id)
        }
        self.assertEqual(texts, {"concurrent 20", "concurrent 21"})

    def test_concurrent_duplicate_delivery_creates_one_task_event_and_intake(self) -> None:
        self.store.close()
        barrier = threading.Barrier(2)
        results = []
        failures: list[BaseException] = []
        envelope = message(48, "one concurrently delivered objective")

        def route() -> None:
            try:
                with TaskStore(self.root, clock=lambda: NOW) as store:
                    service = TaskService(
                        store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW
                    )
                    barrier.wait()
                    results.append(service.handle(envelope))
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=route) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        self.assertEqual({result.task_id for result in results}, {results[0].task_id})
        self.assertEqual(sum(result.duplicate for result in results), 1)
        self.store = TaskStore(self.root, clock=lambda: NOW)
        task_id = results[0].task_id
        self.assertEqual(len(self.store.events(task_id)), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            1,
        )


class ActionServiceGatewayIntegrationTests(unittest.TestCase):
    class Backend:
        def __init__(self, envelope: AuthenticatedInput):
            self.envelope = envelope
            self.handled: list[int] = []

        def pending_inputs(self, _namespace: str):
            return () if self.handled else (self.envelope,)

        def mark_handled(self, _namespace: str, update_id: int):
            self.handled.append(update_id)

    class Telegram:
        def __init__(self):
            self.sent: list[str] = []
            self.attempted: list[str] = []
            self.failures_remaining = 0

        def send_message(self, _group_id: int, text: str):
            self.attempted.append(text)
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise RuntimeError("delivery failed")
            self.sent.append(text)

    @staticmethod
    def service(envelope: AuthenticatedInput, gateway: object):
        service = object.__new__(ActionService)
        service.backend = ActionServiceGatewayIntegrationTests.Backend(envelope)
        service.telegram = ActionServiceGatewayIntegrationTests.Telegram()
        service.namespace = NAMESPACE
        service.group_id = GROUP
        service.task_gateway = gateway
        return service

    def test_current_status_keeps_the_deterministic_fast_path(self) -> None:
        class MustNotRun:
            def handle(self, _envelope):
                raise AssertionError("gateway received current-status question")

        service = self.service(message(30, "what is the machine status?"), MustNotRun())
        service._current_machine_text = lambda: "fresh deterministic status"
        service._handle_inputs()
        self.assertEqual(service.backend.handled, [30])
        self.assertEqual(service.telegram.sent, ["fresh deterministic status"])

    def test_persistence_failure_leaves_input_pending_and_sends_no_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory), clock=lambda: NOW)
            tasks = TaskService(store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW)
            service = self.service(message(31, "investigate this"), OperatorGateway(tasks))
            with mock.patch.object(store, "create_task", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    service._handle_inputs()
            self.assertEqual(service.backend.handled, [])
            self.assertEqual(service.telegram.sent, [])
            self.assertEqual(tasks.list_tasks(42), ())
            store.close()

    def test_intake_failure_rolls_back_then_prevents_mark_handled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory), clock=lambda: NOW)
            tasks = TaskService(store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW)
            service = self.service(message(33, "investigate atomically"), OperatorGateway(tasks))
            with mock.patch.object(
                store, "record_operator_input", side_effect=OSError("ledger unavailable")
            ):
                with self.assertRaisesRegex(OSError, "ledger unavailable"):
                    service._handle_inputs()
            self.assertEqual(service.backend.handled, [])
            self.assertEqual(service.telegram.sent, [])
            self.assertEqual(tasks.list_tasks(42), ())
            self.assertEqual(
                store.db.execute(
                    "SELECT count(*) FROM tc_task_operator_inputs"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_non_event_outcome_is_persisted_before_mark_handled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory), clock=lambda: NOW)
            tasks = TaskService(store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW)
            service = self.service(message(34, "/task list"), OperatorGateway(tasks))
            with mock.patch.object(
                store, "record_operator_input", side_effect=OSError("ledger unavailable")
            ):
                with self.assertRaisesRegex(OSError, "ledger unavailable"):
                    service._handle_inputs()
            self.assertEqual(service.backend.handled, [])
            self.assertEqual(service.telegram.sent, [])
            store.close()

    def test_gateway_success_is_persisted_then_marked_and_acknowledged_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory), clock=lambda: NOW)
            tasks = TaskService(store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW)
            service = self.service(message(32, "investigate this"), OperatorGateway(tasks))
            service._handle_inputs()
            service._handle_inputs()
            self.assertEqual(service.backend.handled, [32])
            self.assertEqual(len(service.telegram.sent), 1)
            self.assertEqual(len(tasks.list_tasks(42)), 1)
            store.close()

    def test_delivery_failure_leaves_pending_then_replays_exact_stored_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory), clock=lambda: NOW)
            tasks = TaskService(store, group_id=GROUP, namespace=NAMESPACE, clock=lambda: NOW)
            envelope = message(35, "persist this acknowledgement")
            service = self.service(envelope, OperatorGateway(tasks))
            service.telegram.failures_remaining = 1

            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                service._handle_inputs()
            self.assertEqual(service.backend.handled, [])
            self.assertEqual(len(tasks.list_tasks(42)), 1)
            intake = store.get_operator_input(tasks._event_id(envelope))
            self.assertIsNotNone(intake)
            self.assertEqual(service.telegram.attempted, [intake.response])

            service._handle_inputs()
            self.assertEqual(service.backend.handled, [35])
            self.assertEqual(service.telegram.attempted, [intake.response, intake.response])
            self.assertEqual(service.telegram.sent, [intake.response])
            self.assertEqual(len(store.events(intake.task_id)), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
