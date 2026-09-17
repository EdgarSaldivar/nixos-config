from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.action_service import (
    BACKUP_RETRIGGER,
    BACKUP_WAIT,
    EPISODE_CLOSED,
    MAX_RESTARTS_PER_EPISODE,
    TARGET_WAIT,
    UNKNOWN_REMINDER,
    MAX_UNEXECUTED_CYCLES,
    PROPOSAL_INTERVAL,
    RECONCILE_INTERVAL,
    ActionService,
    Cycle,
    CycleStore,
    InboxApprovalAuthenticator,
    SystemdBackupProbe,
)
from terracompute_ops.actions import ActionBroker, ApprovalKind, HumanApprovalEvent, MembershipDecision
from terracompute_ops.monitor_restart import (
    ActorError,
    EvidenceStore,
    MonitorRestartAdapter,
    handover_incident_signature,
)
from terracompute_ops.policy import REPEAT_COOLDOWN, ActionClass, ActionPolicy, Mode
from terracompute_ops.telegram import (
    AuthenticatedInput,
    InputKind,
    NotificationMetadata,
    SendReceipt,
    SQLiteUpdateBackend,
    TelegramError,
)

try:
    from test_monitor_restart import BDF, FakeActor
except ImportError:
    from tests.test_monitor_restart import BDF, FakeActor

START = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
GROUP = -1004484415005
NAMESPACE = "terracompute-actions-telegram-v1"
INCIDENT_KEY = f"key-{BDF}"


class Crash(BaseException):
    """Ends the process mid-operation, past every ``except Exception``."""


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple[str, str] | None]] = []
        self.fail_next = 0

    def send_message(self, chat_id, message, *, approve_callback=None, **_kwargs):
        if self.fail_next:
            self.fail_next -= 1
            raise TelegramError("send failed")
        self.sent.append((chat_id, message, approve_callback))
        return SendReceipt(len(self.sent), NotificationMetadata())


class FakeConsumer:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def poll_once(self, *, poll_timeout: int = 25):
        if self.error is not None:
            raise self.error
        return ()


class FakeBackup:
    def __init__(self, clock: Clock, state_path: Path) -> None:
        self.clock = clock
        self.state_path = state_path
        self.triggers: list[datetime] = []
        self.evidence_at_trigger: list[int] = []
        self.completed: datetime | None = None

    def trigger(self) -> None:
        # A separate connection sees only committed evidence, as the backup would.
        reader = sqlite3.connect(self.state_path)
        try:
            count = reader.execute("SELECT COUNT(*) FROM tc_action_evidence").fetchone()[0]
        finally:
            reader.close()
        self.evidence_at_trigger.append(count)
        self.triggers.append(self.clock())

    def completed_after(self, since: datetime) -> str | None:
        if self.completed is not None and self.completed > since:
            return f"terracompute-backup.service@{int(self.completed.timestamp())}"
        return None


class Membership:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.members = {4242}
        self.error: Exception | None = None

    def verify(self, group_id: int, user_id: int) -> MembershipDecision:
        if self.error is not None:
            raise self.error
        return MembershipDecision(group_id, user_id, user_id in self.members, True, True, self.clock())


class ActionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.clock = Clock()
        self.state_path = root / "state.sqlite3"
        self.actions_path = root / "actions.sqlite3"
        self.state_db = sqlite3.connect(self.state_path)
        self.state_db.execute(
            """CREATE TABLE incidents (dedup_key TEXT PRIMARY KEY, source TEXT, fault_family TEXT,
                 stable_signature TEXT, status TEXT, notification_episode INTEGER NOT NULL DEFAULT 1)"""
        )
        self.state_db.commit()
        self.actions_db = sqlite3.connect(self.actions_path)
        self.actor = FakeActor(self.clock)
        self.backend = SQLiteUpdateBackend(root / "inbox.sqlite3")
        self.telegram = FakeTelegram()
        self.consumer = FakeConsumer()
        self.backup = FakeBackup(self.clock, self.state_path)
        self.membership = Membership(self.clock)
        self.reports: list[str] = []
        self.update_id = 100
        self.service = self.build_service()

    def build_service(self) -> ActionService:
        """A fresh process over the same databases, as after a service restart."""
        holder: dict[str, ActionService] = {}
        evidence = EvidenceStore(self.state_db, self.clock)
        adapter = MonitorRestartAdapter(
            self.actor, evidence,
            backup_ref=lambda proposal: holder["service"].backup_ref(proposal),
            preflight_ref=lambda proposal: holder["service"].preflight_ref(proposal),
            clock=self.clock,
        )
        self.broker = ActionBroker(
            self.actions_db,
            policy=ActionPolicy(
                mode=Mode.APPROVE, revision="monitor-restart-r1",
                enabled_actions=frozenset({ActionClass.MONITOR_COMPONENT_RESTART}),
                approval_group_id=GROUP,
            ),
            membership=self.membership,
            authenticator=InboxApprovalAuthenticator(self.backend, NAMESPACE),
            adapter=adapter,
            clock=self.clock,
        )
        self.cycles = CycleStore(self.actions_db)
        holder["service"] = ActionService(
            actions_db=self.actions_db, state_db=self.state_db, broker=self.broker,
            adapter=adapter, evidence=evidence, cycles=self.cycles, backup=self.backup,
            telegram=self.telegram, consumer=self.consumer, backend=self.backend,
            namespace=NAMESPACE, group_id=GROUP, policy_revision="monitor-restart-r1",
            clock=self.clock, report=self.reports.append,
        )
        return holder["service"]

    def tearDown(self) -> None:
        self.backend.close()
        self.state_db.close()
        self.actions_db.close()
        self.temp.cleanup()

    # -- helpers --------------------------------------------------------------------

    def open_incident(self, bdf: str = BDF, episode: int = 1) -> None:
        self.state_db.execute(
            """INSERT INTO incidents VALUES (?,?,?,?,?,?) ON CONFLICT(dedup_key) DO UPDATE
               SET status='open', notification_episode=excluded.notification_episode""",
            (f"key-{bdf}", "ssh", "gpu", handover_incident_signature(bdf), "open", episode),
        )
        self.state_db.commit()

    def store_input(self, kind: InputKind, subject: str, nonce: str | None, sender: int = 4242) -> None:
        self.update_id += 1
        self.backend.store_accepted(NAMESPACE, AuthenticatedInput(
            update_id=self.update_id, group_id=GROUP, sender_id=sender, message_id=7,
            callback_id="cb", kind=kind, subject_id=subject, nonce=nonce,
            text=f"{kind.value}:{subject}",
        ))

    def approval_input(self, proposal_id: str, nonce: str, sender: int = 4242) -> None:
        self.store_input(InputKind.APPROVAL_COMMAND, proposal_id, nonce, sender)

    def pending_proposal(self) -> tuple[str, str]:
        self.open_incident()
        self.service.tick()
        self.clock.advance(minutes=2)
        self.backup.completed = self.clock()
        self.clock.advance(seconds=5)
        self.service.tick()
        _chat, _text, button = self.telegram.sent[-1]
        label, data = button
        match = re.fullmatch(r"approve:(mr-[0-9a-f]{12}):([A-Za-z0-9_-]{24})", data)
        self.assertIsNotNone(match, data)
        self.assertLessEqual(len(data.encode()), 64)
        return match.group(1), match.group(2)

    def cycle_rows(self) -> list[tuple[str, str | None]]:
        return [
            tuple(row) for row in self.actions_db.execute(
                "SELECT stage, result FROM tc_action_cycles ORDER BY created_utc, rowid"
            )
        ]

    def stages(self) -> list[str]:
        return [stage for stage, _result in self.cycle_rows()]

    def texts(self) -> str:
        return "\n".join(text for _chat, text, _button in self.telegram.sent)

    def ended(self, index: int = -1) -> datetime:
        rows = self.actions_db.execute(
            "SELECT finished_utc FROM tc_action_cycles ORDER BY created_utc, rowid"
        ).fetchall()
        return datetime.fromisoformat(rows[index][0].replace("Z", "+00:00"))

    def restarts(self) -> int:
        return [call[0] for call in self.actor.calls].count("restart")

    def history(self, result: str, created: datetime, episode: int = 1, detail: str = "") -> None:
        cycle_id = f"history-{len(self.cycle_rows())}"
        self.cycles.create(Cycle(
            cycle_id=cycle_id, bdf=BDF, incident_key=INCIDENT_KEY, episode=episode, stage="done",
            evidence_revision="r", evidence_ref="e", trigger_utc="t", retrigger_utc="t",
            backup_ref=None, proposal_id=None, nonce=None, digest=None, expires_utc=None,
            created_utc=created.isoformat().replace("+00:00", "Z"),
        ))
        self.cycles.update(
            cycle_id, created, result=result, detail=detail,
            finished_utc=created.isoformat().replace("+00:00", "Z"),
        )

    # -- proposal ---------------------------------------------------------------------

    def test_no_incident_means_no_target_call_and_no_proposal(self) -> None:
        self.service.tick()
        self.assertEqual(self.actor.calls, [])
        self.assertEqual(self.telegram.sent, [])

    def test_evidence_is_committed_before_the_backup_and_backed_up_before_approval(self) -> None:
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])
        self.assertEqual(self.backup.evidence_at_trigger, [1])
        self.assertEqual(self.telegram.sent, [])
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.telegram.sent, [])
        self.backup.completed = self.clock()
        self.clock.advance(seconds=5)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_approval"])
        _chat, text, button = self.telegram.sent[-1]
        self.assertIn("No tenant container is touched", text)
        self.assertEqual(button[0], "Approve restart")

    def test_a_backup_that_does_not_start_is_requested_again(self) -> None:
        self.open_incident()
        self.service.tick()
        self.clock.advance(seconds=BACKUP_RETRIGGER.total_seconds() - 1)
        self.service.tick()
        self.assertEqual(len(self.backup.triggers), 1)
        self.clock.advance(seconds=1)
        self.service.tick()
        self.assertEqual(len(self.backup.triggers), 2)
        # The original trigger still defines which backup counts.
        self.backup.completed = self.backup.triggers[0] + timedelta(seconds=1)
        self.clock.advance(seconds=5)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_approval"])

    def test_no_proposal_unless_the_target_is_verified_with_the_exporter_running(self) -> None:
        self.open_incident()
        for changes in (
            {"hostname": "somewhere-else"},
            {"container": {"present": True, "running": False, "started_at": None,
                           "image": "jjziets/dcgm-exporter:latest", "runtime": "nvidia"}},
        ):
            with self.subTest(changes=changes):
                self.actor.status_changes = changes
                self.clock.advance(minutes=6)
                self.service.tick()
                self.assertEqual(self.cycle_rows(), [])
                self.assertEqual(self.backup.triggers, [])
                count = self.state_db.execute("SELECT COUNT(*) FROM tc_action_evidence").fetchone()[0]
                self.assertEqual(count, 0)

    def test_state_change_during_backup_supersedes_the_cycle(self) -> None:
        self.open_incident()
        self.service.tick()
        self.actor.status_changes = {"vm_containers": []}
        self.clock.advance(minutes=1)
        self.backup.completed = self.clock()
        self.clock.advance(seconds=5)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "superseded")])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("withdrawn: the target changed", self.texts())
        self.assertIsNone(self.telegram.sent[0][2])

    def test_backup_timeout_ends_the_cycle_without_proposal(self) -> None:
        self.open_incident()
        self.service.tick()
        self.clock.advance(seconds=BACKUP_WAIT.total_seconds() + 1)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "backup_failed")])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("backup did not complete", self.texts())

    def test_undeliverable_approval_request_ends_the_cycle(self) -> None:
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.telegram.fail_next = 1
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "notify_failed")])
        self.assertEqual(self.reports, [])
        self.assertIn("could not be delivered", self.texts())

    # -- approval and execution ---------------------------------------------------------

    def test_matching_approval_executes_once_and_reports(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.clock.advance(minutes=1)
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.assertIn("Restart succeeded", self.telegram.sent[-1][1])
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())
        self.assertEqual(self.stages(), ["done"])
        # A replayed approval cannot run it again.
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)

    def test_authority_state_is_private_and_the_outcome_is_audited_in_backed_up_state(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        shared = {row[0] for row in self.state_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(shared, {"incidents", "tc_action_evidence"})
        private = {row[0] for row in self.actions_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tc_action_approvals", "tc_action_attempts", "tc_action_cycles"} <= private)
        audit = self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='restart-result'"
        ).fetchall()
        self.assertEqual(len(audit), 1)
        record = json.loads(audit[0][0])
        self.assertEqual((record["approver_telegram_user_id"], record["result"]), (4242, "succeeded"))

    def test_wrong_nonce_or_non_member_does_not_execute(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, "x" * 24)
        self.service.tick()
        self.assertIn("does not match", self.telegram.sent[-1][1])
        self.approval_input(proposal_id, nonce, sender=9999)
        self.service.tick()
        self.assertIn("not accepted", self.telegram.sent[-1][1])
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.stages(), ["awaiting_approval"])

    def test_membership_lookup_failure_asks_for_another_tap_that_then_works(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.membership.error = TelegramError("getChatMember failed")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertIn("could not be verified", self.telegram.sent[-1][1])
        self.assertEqual((self.restarts(), self.stages()), (0, ["awaiting_approval"]))
        self.membership.error = None
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)

    def test_other_inputs_cannot_starve_an_approval(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        for index in range(100):
            self.store_input(InputKind.ACKNOWLEDGEMENT, f"incident-{index}", None)
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())

    def test_authenticator_requires_the_stored_input(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        authenticator = InboxApprovalAuthenticator(self.backend, NAMESPACE)

        def event(**changes):
            values = dict(
                event_id=f"telegram:{NAMESPACE}:{self.update_id}", kind=ApprovalKind.APPROVE,
                group_id=GROUP, user_id=4242, display_name="x", proposal_id=proposal_id,
                proposal_digest="d" * 64, nonce=f"{proposal_id}:{nonce}", occurred_at=self.clock(),
            )
            values.update(changes)
            return HumanApprovalEvent(**values)

        self.assertTrue(authenticator.authenticate(event()).authenticated)
        for changes in (
            {"user_id": 1}, {"group_id": -1}, {"nonce": f"{proposal_id}:other-nonce-000"},
            {"event_id": f"telegram:{NAMESPACE}:99999"}, {"event_id": "forged"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(authenticator.authenticate(event(**changes)).authenticated)

    # -- proposal policy ------------------------------------------------------------------

    def test_unexecuted_proposals_back_off_from_when_they_ended(self) -> None:
        self.pending_proposal()
        self.clock.advance(minutes=6)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "expired")])
        self.assertIn("expired", self.telegram.sent[-1][1])
        calls = len(self.actor.calls)
        # Inside the first backoff no target call is made at all.
        self.clock.value = self.ended() + PROPOSAL_INTERVAL - timedelta(seconds=1)
        self.service.tick()
        self.assertEqual(len(self.actor.calls), calls)
        self.clock.value = self.ended() + PROPOSAL_INTERVAL
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])
        self.clock.advance(minutes=21)
        self.service.tick()
        self.assertEqual(self.cycle_rows()[-1], ("done", "backup_failed"))
        # The second unexecuted cycle doubles the wait, counted from its end.
        self.clock.value = self.ended() + 2 * PROPOSAL_INTERVAL - timedelta(seconds=1)
        self.service.tick()
        self.assertEqual(len(self.stages()), 2)
        self.clock.value = self.ended() + 2 * PROPOSAL_INTERVAL
        self.service.tick()
        self.assertEqual(len(self.stages()), 3)

    def test_the_last_allowed_cycle_says_proposals_have_stopped(self) -> None:
        self.open_incident()
        for index in range(MAX_UNEXECUTED_CYCLES - 1):
            self.history("expired", START - timedelta(days=2, minutes=index))
        self.service.tick()
        self.clock.advance(seconds=BACKUP_WAIT.total_seconds() + 1)
        self.service.tick()
        self.assertEqual(self.cycle_rows()[-1], ("done", "backup_failed"))
        self.assertTrue(self.telegram.sent[-1][1].endswith(EPISODE_CLOSED))

    def test_proposals_stop_after_the_cycle_cap(self) -> None:
        self.open_incident()
        for index in range(MAX_UNEXECUTED_CYCLES):
            self.history("expired", START - timedelta(days=2, minutes=index))
        self.clock.advance(days=1)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), MAX_UNEXECUTED_CYCLES)
        self.assertEqual(self.actor.calls, [])

    def test_an_executed_restart_ends_proposals_for_the_incident_episode(self) -> None:
        for result, detail in (
            ("succeeded", "restart completed; handover still blocked"),
            # Only a success counts as cleared, whatever the detail says.
            ("failed", "restart_nonzero_exit; handover cleared"),
            ("postcondition-failed", "restart completed; handover cleared"),
            ("unknown", "restart_timeout; handover cleared"),
        ):
            with self.subTest(result=result):
                self.actions_db.execute("DELETE FROM tc_action_cycles")
                self.actions_db.commit()
                self.state_db.execute("DELETE FROM incidents")
                self.state_db.commit()
                self.open_incident()
                self.history(result, self.clock() - timedelta(hours=1), detail=detail)
                self.clock.advance(hours=6)
                self.service.tick()
                self.assertEqual(len(self.cycle_rows()), 1)
                self.assertEqual(self.backup.triggers, [])
        # A new episode, after the incident recovered and reopened, may be proposed again.
        self.open_incident(episode=2)
        self.clock.advance(hours=1)
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])

    # -- failures and restarts ------------------------------------------------------------

    def test_an_unknown_restart_blocks_new_proposals_until_reconciled(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.actor.run
        ledger = {"available": False, "reads": 0}

        def run(operation: str, request_id: str) -> dict:
            if operation == "restart":
                original(operation, request_id)
                raise ActorError("channel lost after dispatch")
            if operation == "result":
                ledger["reads"] += 1
            if operation == "result" and not ledger["available"]:
                raise ActorError("target unreachable")
            return original(operation, request_id)

        self.actor.run = run
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "unknown")])
        self.assertIn("unknown", self.telegram.sent[-1][1])
        # Even a new incident episode on a still-blocked GPU gets no proposal while the
        # earlier result is unknown.
        self.actor.status_changes = {"handover_blocked": [BDF]}
        self.open_incident(episode=2)
        # Longer than the 30-minute cooldown, so only the unknown result holds it back.
        for _ in range(8):
            self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
            self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(ledger["reads"], 8)
        ledger["available"] = True
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        self.assertIn("Settled the earlier restart. Restart succeeded", self.texts())
        self.assertNotEqual(self.cycle_rows()[0][1], "unknown")
        self.assertEqual(self.restarts(), 1)

    def test_a_failing_phase_never_ends_the_loop(self) -> None:
        self.open_incident()
        self.consumer.error = TelegramError("getUpdates failed")
        original = self.actor.run

        def unreachable(operation: str, request_id: str) -> dict:
            raise ActorError("target unreachable")

        self.actor.run = unreachable
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [])
        self.assertEqual(len(self.reports), 2)
        self.assertIn('"phase":"_poll"', self.reports[0])
        self.assertIn('"phase":"_advance"', self.reports[1])
        self.consumer.error = None
        self.actor.run = original
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])

    def test_a_recorded_approval_interrupted_before_execution_executes_after_restart(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)

        def crash(_proposal_id: str):
            raise Crash()

        self.broker.execute = crash
        with self.assertRaises(Crash):
            self.service.tick()
        self.service = self.build_service()
        self.clock.advance(seconds=30)
        self.service.recover()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.cycle_rows()[0][0], "done")

    def test_a_recorded_approval_that_expired_during_the_outage_is_reported_not_run(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)

        def crash(_proposal_id: str):
            raise Crash()

        self.broker.execute = crash
        with self.assertRaises(Crash):
            self.service.tick()
        self.service = self.build_service()
        self.clock.advance(minutes=10)
        self.service.recover()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.cycle_rows(), [("done", "not_executed")])
        self.assertIn("No restart was attempted", self.telegram.sent[-1][1])

    def test_restart_mid_dispatch_is_reconciled_and_never_replayed(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.actor.run

        def crash_in_dispatch(operation: str, request_id: str) -> dict:
            if operation == "restart":
                raise Crash()
            return original(operation, request_id)

        self.actor.run = crash_in_dispatch
        self.approval_input(proposal_id, nonce)
        with self.assertRaises(Crash):
            self.service.tick()
        self.actor.run = original
        self.service = self.build_service()
        self.service.recover()
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        # The helper has no record, so nothing ran: the attempt closes as refused and the
        # incident may be proposed again after the backoff.
        self.assertEqual(self.cycle_rows(), [("done", "refused")])
        self.assertIn("No restart was performed: execution_not_started", self.texts())
        self.assertEqual(
            self.actions_db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0
        )

    def test_the_result_is_reported_even_when_the_audit_copy_cannot_be_written(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        original = self.service.evidence.record

        def record(kind, subject, document):
            if kind == "restart-result":
                raise sqlite3.OperationalError("database is locked")
            return original(kind, subject, document)

        self.service.evidence.record = record
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.assertIn("Restart succeeded", self.telegram.sent[-1][1])
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertEqual(len(self.reports), 1)
        # The audit copy is retried until it is written, without repeating the message.
        sent = len(self.telegram.sent)
        self.service.evidence.record = original
        self.clock.advance(minutes=1)
        self.service.tick()
        count = self.state_db.execute(
            "SELECT COUNT(*) FROM tc_action_evidence WHERE kind='restart-result'"
        ).fetchone()[0]
        self.assertEqual((count, len(self.telegram.sent)), (1, sent))

    def test_approval_without_a_broker_record_is_not_replayed_after_restart(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        cycle = self.cycles.by_proposal(proposal_id)
        self.cycles.update(cycle.cycle_id, self.clock(), stage="executing")
        self.service.recover()
        self.assertEqual(self.cycle_rows(), [("done", "not_executed")])
        self.service.tick()
        self.assertEqual(self.restarts(), 0)

    # -- review round 2 ---------------------------------------------------------------------

    def restart_with(self, document: dict) -> tuple[str, str]:
        proposal_id, nonce = self.pending_proposal()
        self.actor.restart_document = document
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        return proposal_id, nonce

    def test_a_timed_out_restart_that_took_effect_is_settled_and_releases_the_lock(self) -> None:
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed"})
        self.assertEqual(self.cycle_rows(), [("done", "unknown")])
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertIn("restart took effect after restart_timeout", self.texts())
        self.assertEqual(self.actions_db.execute("SELECT COUNT(*) FROM tc_action_locks").fetchone()[0], 0)
        self.assertEqual(self.restarts(), 1)

    def test_a_timed_out_restart_without_effect_fails_and_says_proposals_stopped(self) -> None:
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed",
                           "restart_ran": False})
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "failed")])
        self.assertIn("restart did not take effect", self.telegram.sent[-1][1])
        self.assertTrue(self.telegram.sent[-1][1].endswith(EPISODE_CLOSED))
        self.assertFalse(self.service._open_attempt_exists())

    def test_a_result_that_stays_unknown_is_reminded_not_silent(self) -> None:
        original = self.actor.run

        def unreachable_ledger(operation: str, request_id: str) -> dict:
            if operation == "result":
                raise ActorError("target unreachable")
            return original(operation, request_id)

        self.actor.run = unreachable_ledger
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed"})
        sent = len(self.telegram.sent)
        elapsed = timedelta()
        while elapsed < UNKNOWN_REMINDER + RECONCILE_INTERVAL:
            self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
            elapsed += RECONCILE_INTERVAL
            self.service.tick()
        reminders = [text for _chat, text, _button in self.telegram.sent[sent:]]
        self.assertEqual(len(reminders), 1)
        self.assertIn("still unknown", reminders[0])

    def test_a_refused_restart_is_not_an_executed_one(self) -> None:
        self.restart_with({"ok": False, "reason": "status_before_unavailable", "state": "refused"})
        self.assertEqual(self.cycle_rows(), [("done", "refused")])
        self.assertIn("No restart was performed: status_before_unavailable", self.texts())
        self.assertNotIn("not running with a new start time", self.texts())
        self.assertFalse(self.actor.restarted)
        # The blocked GPU gets another proposal, but not before the broker's cooldown
        # from the refused attempt, so the next approval is not denied.
        started = datetime.fromisoformat(self.actions_db.execute(
            "SELECT started_utc FROM tc_action_attempts").fetchone()[0].replace("Z", "+00:00"))
        self.clock.value = self.ended() + PROPOSAL_INTERVAL
        self.service.tick()
        self.assertEqual(self.stages(), ["done"])
        self.clock.value = started + REPEAT_COOLDOWN
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])
        self.actor.restart_document = None
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.service.tick()
        _chat, _text, button = self.telegram.sent[-1]
        _prefix, proposal_id, nonce = button[1].split(":")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "refused"), ("done", "succeeded")])

    def test_a_failed_result_commit_after_a_real_restart_is_settled_not_stuck(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.broker._finish_known
        calls = {"count": 0}

        def failing_commit(*arguments):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return original(*arguments)

        self.broker._finish_known = failing_commit
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "unknown")])
        self.assertEqual(
            self.actions_db.execute("SELECT state FROM tc_action_attempts").fetchone()[0], "unknown"
        )
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        self.assertEqual(self.cycle_rows()[0][1], "succeeded")
        self.assertEqual(self.restarts(), 1)

    def test_a_failed_cycle_write_after_execution_is_finished_on_the_next_tick(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.cycles.update
        failures = {"left": 1}

        def update(cycle_id, now, **values):
            if values.get("stage") == "done" and failures["left"]:
                failures["left"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return original(cycle_id, now, **values)

        self.cycles.update = update
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        # The lifecycle phase of the same tick finishes the executing cycle.
        self.assertEqual(failures["left"], 0)
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertIn("Restart succeeded", self.texts())
        self.assertEqual(self.restarts(), 1)

    def test_no_phase_leaves_a_transaction_open(self) -> None:
        def leaves_transaction(namespace):
            self.actions_db.execute("BEGIN IMMEDIATE")
            self.state_db.execute("INSERT INTO incidents(dedup_key) VALUES ('stray')")
            return ()

        self.backend.pending_inputs = leaves_transaction
        self.service.tick()
        self.assertFalse(self.actions_db.in_transaction)
        self.assertFalse(self.state_db.in_transaction)
        self.assertEqual(self.state_db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)

    def test_outcome_messages_are_retried_until_delivered(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        original = self.telegram.send_message

        def fail_outcomes(chat_id, message, **kwargs):
            if "Restart succeeded" in message:
                raise TelegramError("send failed")
            return original(chat_id, message, **kwargs)

        self.telegram.send_message = fail_outcomes
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertNotIn("Restart succeeded", self.texts())
        self.telegram.send_message = original
        self.service.tick()
        self.assertNotIn("Restart succeeded", self.texts())  # Waits for the retry interval.
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.texts().count("Restart succeeded"), 1)

    def test_a_new_blockage_after_a_restart_that_cleared_it_can_be_proposed_again(self) -> None:
        self.open_incident()
        self.history("succeeded", START - timedelta(minutes=20),
                     detail="restart completed; handover cleared")
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)  # Inside the 30-minute cooldown.
        self.clock.advance(minutes=10)
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])

    def test_restarts_per_episode_are_capped(self) -> None:
        self.open_incident()
        for index in range(MAX_RESTARTS_PER_EPISODE):
            self.history("succeeded", START - timedelta(hours=3 - index),
                         detail="restart completed; handover cleared")
        self.clock.advance(hours=6)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), MAX_RESTARTS_PER_EPISODE)
        self.assertEqual(self.actor.calls, [])

    def test_other_gpu_incidents_cause_no_target_calls(self) -> None:
        self.state_db.execute(
            "INSERT INTO incidents VALUES ('driver', 'ssh', 'gpu', ?, 'open', 1)", ("a" * 64,)
        )
        self.state_db.commit()
        for _ in range(4):
            self.clock.advance(minutes=6)
            self.service.tick()
        self.assertEqual(self.actor.calls, [])

    def test_backup_probe_errors_are_bounded_by_the_wait(self) -> None:
        self.open_incident()
        self.service.tick()

        def broken(_since):
            raise subprocess.TimeoutExpired("systemctl", 10)

        self.backup.completed_after = broken
        self.clock.advance(seconds=BACKUP_WAIT.total_seconds() + 1)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "backup_failed")])

    def test_an_unwritable_backup_trigger_ends_a_counted_cycle(self) -> None:
        self.open_incident()

        def unwritable():
            raise PermissionError("trigger file")

        self.backup.trigger = unwritable
        for _ in range(3):
            self.clock.advance(hours=5)
            self.service.tick()
        rows = self.cycle_rows()
        self.assertEqual(rows, [("done", "backup_failed")] * 3)
        evidence = self.state_db.execute("SELECT COUNT(*) FROM tc_action_evidence").fetchone()[0]
        self.assertEqual(evidence, 3)
        self.assertIn("could not be requested", self.texts())

    def test_a_membership_error_at_execution_is_retried_within_the_proposal(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        original = self.membership.verify
        calls = {"count": 0}

        def flaky(group_id, user_id):
            calls["count"] += 1
            if calls["count"] == 2:  # The execution-time check, after approval.
                raise TelegramError("getChatMember failed")
            return original(group_id, user_id)

        self.membership.verify = flaky
        self.service.tick()
        self.assertEqual((self.stages(), self.restarts()), (["executing"], 0))
        self.assertIn("Retrying until the proposal expires", self.texts())
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertEqual(self.restarts(), 1)

    def test_a_membership_error_that_outlasts_the_proposal_is_reported(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        original = self.membership.verify
        calls = {"count": 0}

        def broken_after_approval(group_id, user_id):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise TelegramError("getChatMember failed")
            return original(group_id, user_id)

        self.membership.verify = broken_after_approval
        for _ in range(8):
            self.service.tick()
            self.clock.advance(minutes=1)
        self.assertEqual(self.cycle_rows(), [("done", "not_executed")])
        self.assertIn("could not run before its proposal expired", self.texts())
        self.assertEqual(self.restarts(), 0)

    def test_an_attempt_left_at_the_dispatch_boundary_is_swept_and_settled(self) -> None:
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed"})
        # A failed write left the broker attempt at the dispatch boundary.
        self.actions_db.execute("UPDATE tc_action_attempts SET state='dispatching'")
        self.actions_db.commit()
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        self.assertEqual(
            self.actions_db.execute("SELECT state FROM tc_action_attempts").fetchone()[0], "succeeded"
        )
        self.assertFalse(self.service._open_attempt_exists())

    def test_an_unreachable_target_after_the_backup_is_retried_slowly_then_reported(self) -> None:
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        original = self.actor.run
        status_calls = {"count": 0}

        def unreachable(operation: str, request_id: str) -> dict:
            if operation == "status":
                status_calls["count"] += 1
                raise ActorError("target unreachable")
            return original(operation, request_id)

        self.actor.run = unreachable
        for _ in range(20):  # Five minutes of 15-second ticks.
            self.clock.advance(seconds=15)
            self.service.tick()
        self.assertLessEqual(status_calls["count"], 6)
        self.clock.value = START + BACKUP_WAIT + TARGET_WAIT + timedelta(minutes=2)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "target_unavailable")])
        self.assertIn("the target did not answer", self.texts())

    def test_a_crash_after_a_real_restart_is_judged_from_preserved_preflight_evidence(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.actor.run

        def crash_after_restart(operation: str, request_id: str) -> dict:
            document = original(operation, request_id)
            if operation == "restart":
                raise Crash()
            return document

        self.actor.run = crash_after_restart
        self.approval_input(proposal_id, nonce)
        with self.assertRaises(Crash):
            self.service.tick()
        self.actor.run = original
        self.service = self.build_service()
        self.clock.advance(seconds=30)
        self.service.recover()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertIn("Resumed after an interruption. Restart succeeded", self.texts())
        self.assertEqual(self.restarts(), 1)

    def test_a_second_blocked_gpu_waits_for_the_cooldown_of_the_first_restart(self) -> None:
        other = "0000:c1:00.0"
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        restarted_at = self.clock()
        self.open_incident(other)
        self.actor.status_changes = {"handover_blocked": [other]}
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)
        self.clock.value = restarted_at + REPEAT_COOLDOWN
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])

    def test_a_failing_delivery_backs_off_and_does_not_block_other_cycles(self) -> None:
        self.open_incident()
        for index in range(2):
            self.history("expired", START - timedelta(days=1, minutes=index))
        first, second = [cycle.cycle_id for cycle in self.cycles.episode(INCIDENT_KEY, 1)]
        self.cycles.update(first, START, notice="first notice")
        self.cycles.update(second, START, notice="second notice")
        original = self.cycles.update

        def clear_fails_for_first(cycle_id, now, **values):
            if cycle_id == first and "notice" in values:
                raise sqlite3.OperationalError("disk full")
            return original(cycle_id, now, **values)

        self.cycles.update = clear_fails_for_first
        self.service._deliver()
        # The other cycle's message goes out in the same pass.
        self.assertEqual([text for _chat, text, _button in self.telegram.sent], ["first notice", "second notice"])
        for _ in range(60):  # One hour of one-minute ticks.
            self.service._deliver()
            self.clock.advance(minutes=1)
        texts = [text for _chat, text, _button in self.telegram.sent]
        self.assertEqual(texts.count("second notice"), 1)
        self.assertLessEqual(texts.count("first notice"), 7)

    def test_an_older_cycles_table_gains_the_new_columns(self) -> None:
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            """CREATE TABLE tc_action_cycles (cycle_id TEXT PRIMARY KEY, bdf TEXT NOT NULL,
                 incident_key TEXT NOT NULL, episode INTEGER NOT NULL, stage TEXT NOT NULL,
                 evidence_revision TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                 trigger_utc TEXT NOT NULL, retrigger_utc TEXT NOT NULL, backup_ref TEXT,
                 proposal_id TEXT UNIQUE, nonce TEXT, digest TEXT, expires_utc TEXT,
                 message_id INTEGER, result TEXT, detail TEXT, created_utc TEXT NOT NULL,
                 updated_utc TEXT NOT NULL)"""
        )
        store = CycleStore(db)
        self.assertIsNone(store.active())
        self.assertEqual(store.undelivered(), [])

    def test_delivery_backoff_survives_days_of_failure(self) -> None:
        self.open_incident()
        self.history("expired", START - timedelta(days=1))
        (first,) = [cycle.cycle_id for cycle in self.cycles.episode(INCIDENT_KEY, 1)]
        self.cycles.update(first, START, notice="first notice")
        original = self.telegram.send_message

        def down_for_first(chat_id, message, **kwargs):
            if message == "first notice":
                raise TelegramError("bot removed from group")
            return original(chat_id, message, **kwargs)

        self.telegram.send_message = down_for_first
        attempts = 0
        for minute in range(60 * 60):  # Sixty hours of one-minute passes.
            if minute == 40 * 60:
                self.history("expired", START - timedelta(hours=20))
                later = self.cycles.episode(INCIDENT_KEY, 1)[-1].cycle_id
                self.cycles.update(later, self.clock(), notice="second notice")
            before = len(self.reports)
            self.service._deliver()
            attempts += len(self.reports) - before
            self.clock.advance(minutes=1)
        self.assertEqual(self.texts().count("second notice"), 1)
        # Hourly at most after the first few doublings.
        self.assertLessEqual(attempts, 70)

    def test_audit_copies_do_not_wait_for_telegram(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)
        self.telegram.fail_next = 10**6
        self.service.tick()
        rows = self.state_db.execute(
            "SELECT COUNT(*) FROM tc_action_evidence WHERE kind='restart-result'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])

    def test_settling_an_unknown_result_keeps_the_earlier_audit_copy(self) -> None:
        original_record = self.service.evidence.record
        audit_down = {"value": True}

        def record(kind, subject, document):
            if kind == "restart-result" and audit_down["value"]:
                raise sqlite3.OperationalError("database is locked")
            return original_record(kind, subject, document)

        self.service.evidence.record = record
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed"})
        self.assertEqual(self.cycle_rows(), [("done", "unknown")])
        audit_down["value"] = False
        # The next pass that settles the result comes before the audit retry is due.
        self.service._delivery_backoff[(self.cycles.episode(INCIDENT_KEY, 1)[0].cycle_id, "audit")] = (
            5, self.clock() + timedelta(hours=1)
        )
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        results = sorted(
            json.loads(row[0])["result"] for row in self.state_db.execute(
                "SELECT document_json FROM tc_action_evidence WHERE kind='restart-result'"
            )
        )
        self.assertEqual(results, ["succeeded", "unknown"])

    # -- backup probe -----------------------------------------------------------------------

    def test_systemd_backup_probe_requires_a_later_successful_run(self) -> None:
        outputs = {}

        def runner(argv, **kwargs):
            class Result:
                returncode = 0
                stdout = outputs["value"]
            return Result()

        probe = SystemdBackupProbe(
            trigger_file=Path(self.temp.name) / "trigger", systemctl="/usr/bin/systemctl", runner=runner
        )
        since = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
        later = int(since.timestamp()) + 30
        good = (f"ActiveState=inactive\nResult=success\nExecMainStatus=0\n"
                f"ExecMainStartTimestamp=@{later}\nExecMainExitTimestamp=@{later + 40}\n")
        outputs["value"] = good
        self.assertEqual(probe.completed_after(since), f"terracompute-backup.service@{later}")
        for bad in (
            good.replace("Result=success", "Result=exit-code"),
            good.replace("ActiveState=inactive", "ActiveState=activating"),
            good.replace(f"@{later}\nExecMainExit", f"@{int(since.timestamp()) - 5}\nExecMainExit"),
            good.replace("ExecMainStatus=0", "ExecMainStatus=1"),
        ):
            with self.subTest(bad=bad):
                outputs["value"] = bad
                self.assertIsNone(probe.completed_after(since))
        probe.trigger()
        self.assertTrue((Path(self.temp.name) / "trigger").read_text().startswith("monitor-restart "))


if __name__ == "__main__":
    unittest.main()
