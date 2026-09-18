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
    DELIVERY_RETRY,
    DIAGNOSIS_WAIT,
    MAX_QUESTIONS_PER_TICK,
    OVERRIDE_LIFETIME,
    SELF_SERVICE_DAILY_CAP,
    BACKUP_WAIT,
    EPISODE_CLOSED,
    MAX_RESTARTS_PER_EPISODE,
    TARGET_WAIT,
    UNKNOWN_REMINDER,
    MAX_UNEXECUTED_CYCLES,
    PROPOSAL_INTERVAL,
    RECONCILE_INTERVAL,
    STATUS_RETRY_INTERVAL,
    ActionService,
    Cycle,
    CycleStore,
    InboxApprovalAuthenticator,
    SystemdBackupProbe,
    _text,
)
from terracompute_ops.actions import ActionBroker, ApprovalKind, HumanApprovalEvent, MembershipDecision
from terracompute_ops.monitor_restart import (
    ActorError,
    EvidenceStore,
    MonitorRestartAdapter,
    handover_incident_signature,
)
from terracompute_ops.diagnosing import Diagnosis
from terracompute_ops.diagnosis import parse_finding
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
        self.attempts = 0

    def send_message(self, chat_id, message, *, approve_callback=None, deny_callback=None,
                     **_kwargs):
        self.attempts += 1
        if self.fail_next:
            self.fail_next -= 1
            raise TelegramError("send failed")
        buttons = [button for button in (approve_callback, deny_callback) if button]
        self.sent.append((chat_id, message, buttons or None))
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
            count = reader.execute(
                "SELECT COUNT(*) FROM tc_action_evidence WHERE kind='proposal-status'"
            ).fetchone()[0]
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
                 stable_signature TEXT, status TEXT, notification_episode INTEGER NOT NULL DEFAULT 1,
                 severity TEXT DEFAULT 'critical', first_occurrence_utc TEXT,
                 last_occurrence_utc TEXT, occurrence_count INTEGER DEFAULT 1)"""
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

    def policy(self) -> ActionPolicy:
        classes = frozenset({ActionClass.MONITOR_COMPONENT_RESTART})
        return ActionPolicy(
            mode=Mode.APPROVE, revision="monitor-restart-r1", enabled_actions=classes,
            self_service_actions=classes if getattr(self, "commissioned_self_service", False)
            else frozenset(),
            approval_group_id=GROUP,
        )

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
            policy=self.policy(),
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

    def status_reads(self) -> int:
        return [operation for operation, _request in self.actor.calls].count("status")

    def open_incident(self, bdf: str = BDF, episode: int = 1) -> None:
        self.state_db.execute(
            """INSERT INTO incidents(dedup_key, source, fault_family, stable_signature, status,
                 notification_episode, severity, first_occurrence_utc, last_occurrence_utc)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dedup_key) DO UPDATE
               SET status='open', notification_episode=excluded.notification_episode""",
            (f"key-{bdf}", "ssh", "gpu", handover_incident_signature(bdf), "open", episode,
             "critical", "2026-09-16T13:39:34Z", "2026-09-17T06:00:00Z"),
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
        _chat, _text, buttons = self.telegram.sent[-1]
        _label, data = buttons[0]
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
            backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
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
        self.assertEqual(self.stages(), ["awaiting_answer"])
        _chat, text, buttons = self.telegram.sent[-1]
        self.assertIn("No tenant container is touched", text)
        self.assertEqual(buttons[0][0], "Approve restart")

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
        self.assertEqual(self.stages(), ["awaiting_answer"])

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
        self.assertEqual(self.stages(), ["awaiting_answer"])

    def test_membership_lookup_failure_asks_for_another_tap_that_then_works(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.membership.error = TelegramError("getChatMember failed")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertIn("could not be verified", self.telegram.sent[-1][1])
        self.assertEqual((self.restarts(), self.stages()), (0, ["awaiting_answer"]))
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

    def test_unexecuted_requests_back_off_from_when_they_ended(self) -> None:
        self.open_incident()
        self.service.tick()
        self.clock.advance(seconds=BACKUP_WAIT.total_seconds() + 1)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "backup_failed")])
        calls = len(self.actor.calls)
        # Inside the first backoff no target call is made at all.
        self.clock.value = self.ended() + PROPOSAL_INTERVAL - timedelta(seconds=1)
        self.service.tick()
        self.assertEqual(len(self.actor.calls), calls)
        self.clock.value = self.ended() + PROPOSAL_INTERVAL
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])
        self.clock.advance(seconds=BACKUP_WAIT.total_seconds() + 1)
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

        def crash(_proposal_id: str, **_kwargs):
            raise Crash()

        self.broker.execute = crash
        with self.assertRaises(Crash):
            self.service.tick()
        self.service = self.build_service()
        self.clock.advance(seconds=30)
        self.service.recover()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.cycle_rows()[0][0], "done")

    def test_an_approval_the_broker_will_not_accept_after_an_outage_is_reported(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.approval_input(proposal_id, nonce)

        def crash(_proposal_id: str, **_kwargs):
            raise Crash()

        self.broker.execute = crash
        with self.assertRaises(Crash):
            self.service.tick()
        self.service = self.build_service()
        self.clock.advance(minutes=10)
        self.service.recover()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.cycle_rows(), [("done", "denied")])
        self.assertIn("Restart was not performed", self.texts())

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
        _chat, _text, buttons = self.telegram.sent[-1]
        _prefix, proposal_id, nonce = buttons[0][1].split(":")
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
            """INSERT INTO incidents(dedup_key, source, fault_family, stable_signature, status,
                 notification_episode) VALUES ('driver', 'ssh', 'gpu', ?, 'open', 1)""",
            ("a" * 64,),
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
        evidence = self.state_db.execute(
            "SELECT COUNT(*) FROM tc_action_evidence WHERE kind='proposal-status'"
        ).fetchone()[0]
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
        self.assertIn("Retrying while the approval is still good", self.texts())
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        self.assertEqual(self.restarts(), 1)

    def test_a_membership_error_that_outlasts_the_approval_is_reported(self) -> None:
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
        # The approval was spent on a proposal the broker would no longer accept.
        self.assertEqual(self.cycle_rows(), [("done", "denied")])
        self.assertIn("Restart was not performed", self.texts())
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
        self.service.schedule.set(
            f"deliver:{self.cycles.episode(INCIDENT_KEY, 1)[0].cycle_id}:audit",
            self.clock() + timedelta(hours=1), 5,
        )
        self.clock.advance(seconds=RECONCILE_INTERVAL.total_seconds())
        self.service.tick()
        results = sorted(
            json.loads(row[0])["result"] for row in self.state_db.execute(
                "SELECT document_json FROM tc_action_evidence WHERE kind='restart-result'"
            )
        )
        self.assertEqual(results, ["succeeded", "unknown"])

    # -- requests that wait -------------------------------------------------------------

    def test_a_request_waits_indefinitely_and_costs_nothing(self) -> None:
        proposal_id, _nonce = self.pending_proposal()
        sent = len(self.telegram.sent)
        calls = len(self.actor.calls)
        for _ in range(60):  # An hour of one-minute ticks.
            self.clock.advance(minutes=1)
            self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.assertEqual(len(self.telegram.sent), sent)  # It does not nag.
        # It re-reads the target while waiting, but on its own slow cadence, not per tick.
        self.assertLessEqual(len(self.actor.calls) - calls, 13)
        self.assertEqual(self.restarts(), 0)

    def test_an_approval_hours_later_still_restarts(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.clock.advance(hours=9)
        self.service.tick()
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        # The proposal the broker executed was built when the answer arrived.
        digest, started = self.actions_db.execute(
            "SELECT digest, created_utc FROM tc_action_proposals"
        ).fetchone()
        self.assertEqual(digest, self.actions_db.execute(
            "SELECT digest FROM tc_action_cycles"
        ).fetchone()[0])
        self.assertGreater(started, (START + timedelta(hours=9)).isoformat().replace("+00:00", "Z"))

    def test_a_request_is_withdrawn_when_the_machine_stops_matching_it(self) -> None:
        self.pending_proposal()
        # The exporter was replaced while the request waited: different start time.
        self.actor.status_changes = {"container": {
            "present": True, "running": True, "started_at": "2026-09-17T09:00:00Z",
            "image": "jjziets/dcgm-exporter:latest", "runtime": "nvidia",
        }}
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "superseded")])
        self.assertIn("withdrawn: the machine has changed", self.texts())
        self.assertEqual(self.restarts(), 0)

    def test_an_approval_after_the_machine_changed_does_not_act(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.actor.status_changes = {"handover_blocked": []}
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "superseded")])
        self.assertIn("withdrawn: the machine has changed", self.texts())
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(
            self.actions_db.execute("SELECT COUNT(*) FROM tc_action_approvals").fetchone()[0], 0
        )

    def test_denying_ends_the_request_and_stops_asking_for_this_incident(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.store_input(InputKind.DENIAL_COMMAND, proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "refused_by_operator")])
        self.assertIn("leaving dcgm-exporter alone", self.texts())
        self.assertTrue(self.telegram.sent[-1][1].endswith(EPISODE_CLOSED))
        self.assertEqual(self.restarts(), 0)
        # No further request for this episode, however long it stays broken.
        self.clock.advance(days=1)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)
        # A new episode may be asked about again.
        self.open_incident(episode=2)
        self.clock.advance(hours=1)
        self.service.tick()
        self.assertEqual(self.stages(), ["done", "awaiting_backup"])

    def test_a_late_tap_on_an_expired_proposal_asks_again_rather_than_acting(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        original = self.membership.verify
        self.membership.verify = lambda *_args: (_ for _ in ()).throw(TelegramError("down"))
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertIn("Tap Approve again", self.texts())
        self.assertEqual(self.stages(), ["awaiting_answer"])
        # The proposal submitted by that tap has since expired.
        self.membership.verify = original
        self.clock.advance(minutes=10)
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "superseded")])
        self.assertEqual(self.restarts(), 0)
        self.assertIn("I will ask again", self.texts())

    def test_a_denial_that_does_not_match_a_waiting_request_does_nothing(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.store_input(InputKind.DENIAL_COMMAND, proposal_id, "x" * 24)
        self.store_input(InputKind.DENIAL_COMMAND, "mr-000000000000", nonce)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.assertIn("does not match a waiting restart request", self.texts())

    def test_the_request_message_offers_both_answers(self) -> None:
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.service.tick()
        _chat, text, buttons = self.telegram.sent[-1]
        self.assertEqual([label for label, _data in buttons], ["Approve restart", "Leave it"])
        self.assertTrue(buttons[0][1].startswith("approve:"))
        self.assertTrue(buttons[1][1].startswith("deny:"))
        self.assertIn("It waits for your answer", text)
        self.assertNotIn("expires", text)

    # -- the diagnosis decides ----------------------------------------------------------

    def diagnosing_service(self, diagnosis):
        """A service whose diagnoser returns exactly this."""
        class Fixed:
            def __init__(self, answer):
                self.answer = answer
                self.requests = []

            def diagnose(self, request):
                self.requests.append(request)
                return self.answer

        self.diagnoser = Fixed(diagnosis)
        self.service.diagnoser = self.diagnoser
        return self.service

    def test_the_finding_is_what_starts_a_request(self) -> None:
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])
        stored = json.loads(self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis'"
        ).fetchone()[0])
        self.assertEqual(stored["source"], "rule")
        self.assertEqual(stored["action"], "restart-monitoring-container(container=dcgm-exporter)")
        self.assertEqual(stored["incident_key"], INCIDENT_KEY)

    def test_the_diagnosis_sees_the_incident_and_the_target_reads(self) -> None:
        class FakeReader:
            def read_all(self, subject=None):
                self.subject = subject
                return {"gpu-handles": "pid=101 comm=dcgm-exporter container=abc devices=nvidia5"}

        self.service.reader = FakeReader()
        service = self.diagnosing_service(Diagnosis(None, "model", reason="no answer"))
        self.open_incident()
        service.tick()
        request = self.diagnoser.requests[0]
        self.assertEqual((request.incident_key, request.episode, request.bdf),
                         (INCIDENT_KEY, 1, BDF))
        self.assertEqual(request.severity, "critical")
        self.assertEqual(request.incident_facts["first_occurrence_utc"], "2026-09-16T13:39:34Z")
        self.assertIn("comm=dcgm-exporter", request.reads)
        self.assertEqual(request.status_document["handover_blocked"], [BDF])

    def test_no_diagnosis_means_no_request_and_no_message(self) -> None:
        service = self.diagnosing_service(Diagnosis(None, "model", reason="model-unavailable"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [])
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.backup.triggers, [])
        stored = json.loads(self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis'"
        ).fetchone()[0])
        self.assertEqual(stored["reason"], "model-unavailable")

    def test_an_action_this_service_cannot_take_goes_to_the_operator(self) -> None:
        finding = parse_finding(json.dumps({
            "summary": "the exporter holds the GPU and will do so again",
            "mechanism": "its image keeps NVML handles open across handovers",
            "evidence": ["target-read@gpu-handles"],
            "action": {"name": "replace-monitoring-container",
                       "parameters": {"container": "dcgm-exporter", "image": "cryptolabsza/dc-exporter-rs:0.2.8"}},
            "expected_effect": "handovers stop being blocked",
            "alternatives": ["restarting only clears it until the next handover"],
            "prevention": "keep the replacement",
            "confidence": "high",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        text = self.texts()
        self.assertIn("I cannot carry this out myself", text)
        self.assertIn("replace-monitoring-container", text)
        self.assertIn("Confidence high, from the model.", text)
        self.assertEqual(self.backup.triggers, [])
        self.assertEqual(self.restarts(), 0)
        # It counts as an unexecuted cycle, so it backs off rather than repeating.
        self.clock.advance(minutes=10)
        service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)

    def test_a_finding_asking_for_something_uncatalogued_is_reported(self) -> None:
        finding = parse_finding(json.dumps({
            "summary": "the driver module is wedged",
            "mechanism": "removal fails with a non-zero usage count",
            "evidence": ["target-read@kernel-gpu-log"],
            "action": {"name": "reload-nvidia-module", "parameters": {}},
            "expected_effect": "the module reloads cleanly",
            "alternatives": [],
            "prevention": "",
            "confidence": "medium",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIn("reload-nvidia-module", self.texts())
        self.assertEqual(self.restarts(), 0)

    # -- acting alone -------------------------------------------------------------------

    def self_service(self) -> None:
        """Commission the restart as something the controller may do by itself."""
        self.commissioned_self_service = True
        self.broker.policy = self.policy()

    def test_a_repair_runs_without_asking_and_says_so_both_times(self) -> None:
        self.self_service()
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        text = self.texts()
        self.assertIn("restarting dcgm-exporter now, without asking", text)
        self.assertIn("touches no tenant", text)
        self.assertIn("Restart succeeded", text)
        # No approval was involved, and no backup was waited for.
        self.assertEqual(
            self.actions_db.execute("SELECT COUNT(*) FROM tc_action_approvals").fetchone()[0], 0
        )
        self.assertEqual(self.backup.triggers, [])
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())

    def test_acting_alone_still_records_evidence_and_an_audit_copy(self) -> None:
        self.self_service()
        self.open_incident()
        self.service.tick()
        kinds = [row[0] for row in self.state_db.execute(
            "SELECT kind FROM tc_action_evidence ORDER BY rowid"
        )]
        for kind in ("diagnosis", "proposal-status", "preflight-status", "postflight-status",
                     "restart-result"):
            self.assertIn(kind, kinds)
        audit = json.loads(self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='restart-result'"
        ).fetchone()[0])
        self.assertEqual(audit["result"], "succeeded")
        self.assertIsNone(audit["approver_telegram_user_id"])

    def test_past_its_daily_allowance_it_asks_instead_of_acting(self) -> None:
        self.self_service()
        self.open_incident()
        for index in range(SELF_SERVICE_DAILY_CAP):
            self.actions_db.execute(
                """INSERT INTO tc_action_attempts(execution_id, proposal_id, approval_nonce,
                     action_class, resource_ids_json, started_utc, state, pre_evidence_ref)
                   VALUES (?,?,?,?,?,?,'succeeded','ref')""",
                (f"e{index}", f"p{index}", f"n{index}", ActionClass.MONITOR_COMPONENT_RESTART.value,
                 '["container:dcgm-exporter"]',
                 (START - timedelta(hours=index + 2)).isoformat().replace("+00:00", "Z")),
            )
        self.actions_db.commit()
        self.clock.advance(hours=1)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])
        self.assertEqual(self.restarts(), 0)
        # A day later the allowance has rolled off and it acts again.
        self.actions_db.execute("DELETE FROM tc_action_cycles")
        self.actions_db.commit()
        self.clock.advance(days=1)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)

    def test_without_the_commission_it_still_asks(self) -> None:
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])
        self.assertEqual(self.restarts(), 0)

    def test_a_repair_that_fails_stops_and_does_not_try_again(self) -> None:
        self.self_service()
        self.actor.restart_document = {
            "ok": False, "reason": "restart_nonzero_exit", "state": "executed",
            "started_at_after": "2026-09-15T02:47:18Z", "restart_ran": False,
        }
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "failed")])
        self.assertIn("Restart failed", self.texts())
        self.assertTrue(self.telegram.sent[-1][1].endswith(EPISODE_CLOSED))
        self.clock.advance(hours=6)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)

    # -- being told what to do ----------------------------------------------------------

    def instruct(self, verb: str, argument: str | None = None, sender: int = 4242) -> None:
        self.store_input(InputKind.INSTRUCTION, verb, argument, sender)

    def test_pause_stops_it_acting_and_resume_restores_it(self) -> None:
        self.self_service()
        self.instruct("pause")
        self.open_incident()
        self.service.tick()
        self.assertIn("Paused.", self.texts())
        self.assertEqual((self.cycle_rows(), self.restarts()), ([], 0))
        self.assertEqual(self.actor.calls, [])  # It does not even read the target.
        # A restart of the service does not forget a pause.
        self.service = self.build_service()
        self.clock.advance(hours=2)
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        self.instruct("resume")
        self.service.tick()
        self.assertIn("Resumed.", self.texts())
        self.assertEqual(self.restarts(), 1)

    def test_a_hold_protects_one_gpu_and_leaves_the_others(self) -> None:
        self.self_service()
        other = "0000:c1:00.0"
        self.instruct("hold", BDF)
        self.open_incident()
        self.open_incident(other)
        self.actor.status_changes = {"handover_blocked": [BDF, other]}
        self.service.tick()
        self.assertIn(f"Holding {BDF}", self.texts())
        # It acted on the GPU that is not held.
        self.assertEqual(self.restarts(), 1)
        self.assertEqual([row[0] for row in self.actions_db.execute(
            "SELECT bdf FROM tc_action_cycles"
        )], [other])
        self.instruct("release", BDF)
        self.clock.advance(hours=1)
        self.service.tick()
        self.assertIn(f"Released {BDF}", self.texts())
        self.assertEqual(sorted(row[0] for row in self.actions_db.execute(
            "SELECT bdf FROM tc_action_cycles"
        )), [BDF, other])

    def test_a_malformed_hold_is_refused_and_holds_nothing(self) -> None:
        self.self_service()
        for argument in (None, "a1", "0000:a1:00.0 extra", "../etc"):
            with self.subTest(argument=argument):
                self.instruct("hold", argument)
                self.service.tick()
                self.assertIn("Name the GPU to hold", self.telegram.sent[-1][1])
        self.assertEqual(self.service.controls.holds(), ())

    def test_status_says_what_it_is_doing(self) -> None:
        self.self_service()
        self.instruct("status")
        self.service.tick()
        self.assertIn("Running.", self.texts())
        self.assertIn("No holds.", self.texts())
        self.assertIn("Nothing in flight.", self.texts())
        self.open_incident()
        self.service.tick()
        self.instruct("pause")
        self.instruct("hold", BDF)
        self.instruct("status")
        self.service.tick()
        latest = self.telegram.sent[-1][1]
        self.assertIn("Paused.", latest)
        self.assertIn(f"Holding: {BDF}.", latest)
        self.assertIn("1 restart(s) in the last day.", latest)

    def test_an_instruction_never_approves_anything(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        for verb in ("pause", "resume", "status"):
            self.instruct(verb)
        self.instruct("hold", BDF)
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.assertEqual(
            self.actions_db.execute("SELECT COUNT(*) FROM tc_action_approvals").fetchone()[0], 0
        )
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())

    def test_now_overrides_the_waiting_period_and_the_allowance(self) -> None:
        self.self_service()
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        # The restart did not help: the GPU is still blocked.
        self.actor.status_changes = {"handover_blocked": [BDF]}
        # Inside the cooldown it would normally do nothing at all.
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.instruct("now", BDF)
        self.service.tick()
        self.assertIn("Right away", self.texts())
        self.assertEqual(self.restarts(), 2)
        detail = self.actions_db.execute(
            "SELECT detail FROM tc_action_audit WHERE event='attempt-reserved' ORDER BY id DESC"
        ).fetchone()[0]
        self.assertIn("cooldown lifted by telegram:4242", detail)
        # It is spent: the next tick inside the cooldown does nothing.
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(self.restarts(), 2)
        self.assertIsNone(self.service.controls.get(f"override:{BDF}"))

    def test_now_reopens_an_episode_a_failure_had_closed(self) -> None:
        self.self_service()
        self.actor.restart_document = {
            "ok": False, "reason": "restart_nonzero_exit", "state": "executed",
            "started_at_after": "2026-09-15T02:47:18Z", "restart_ran": False,
        }
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "failed")])
        self.clock.advance(hours=2)
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)  # Closed for this episode.
        self.actor.restart_document = None
        self.instruct("now", BDF)
        self.service.tick()
        self.assertEqual(self.cycle_rows()[-1], ("done", "succeeded"))
        self.assertEqual(len(self.cycle_rows()), 2)

    def test_now_does_not_override_a_pause_a_hold_or_the_machine_checks(self) -> None:
        self.self_service()
        self.open_incident()
        self.instruct("pause")
        self.instruct("now", BDF)
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        self.instruct("resume")
        self.instruct("hold", BDF)
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        self.instruct("release", BDF)
        # The machine itself still has to agree: no blocked handover, no action.
        self.actor.status_changes = {"handover_blocked": []}
        self.service.tick()
        self.assertEqual(self.restarts(), 0)

    def test_a_malformed_now_is_refused(self) -> None:
        self.self_service()
        self.instruct("now", "everything")
        self.service.tick()
        self.assertIn("Name the GPU to act on", self.telegram.sent[-1][1])
        self.assertEqual(self.service.controls.get("override:everything"), None)

    # -- answering questions ------------------------------------------------------------

    def ask(self, question: str, sender: int = 4242) -> None:
        self.store_input(InputKind.QUESTION, None, question, sender)

    def test_why_answers_from_the_diagnosis_it_kept(self) -> None:
        self.instruct("why")
        self.service.tick()
        self.assertIn("not diagnosed anything yet", self.telegram.sent[-1][1])
        self.open_incident()
        self.service.tick()
        self.instruct("why")
        self.service.tick()
        latest = self.telegram.sent[-1][1]
        self.assertIn("cannot be handed to its VM rental", latest)
        self.assertIn("Wanted: restart-monitoring-container(container=dcgm-exporter)", latest)
        self.assertIn("from the rule", latest)

    def test_a_question_is_answered_from_evidence_and_changes_nothing(self) -> None:
        class FakeAssistant:
            def __init__(self):
                self.calls = []

            def answer(self, question, context, subject):
                self.calls.append((question, context, subject))
                return "The exporter holds it open; restarting it clears the handover."

        assistant = FakeAssistant()
        self.service.assistant = assistant
        self.open_incident()
        self.service.tick()
        self.ask("what is holding a1?")
        self.service.tick()
        self.assertEqual(self.telegram.sent[-1][1],
                         "The exporter holds it open; restarting it clears the handover.")
        question, context, subject = assistant.calls[0]
        self.assertEqual((question, subject), ("what is holding a1?", "4242"))
        self.assertIn("## open incidents", context)
        self.assertIn(INCIDENT_KEY, context)
        self.assertIn("## last diagnosis", context)
        # Answering is not acting.
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())

    def test_a_question_without_a_model_still_gets_an_honest_reply(self) -> None:
        self.open_incident()
        self.service.tick()
        self.ask("why is this taking so long?")
        self.service.tick()
        latest = self.telegram.sent[-1][1]
        self.assertIn("no model to think with", latest)
        self.assertIn("cannot be handed to its VM rental", latest)

    def test_a_model_that_cannot_answer_says_so(self) -> None:
        class Broken:
            def answer(self, question, context, subject):
                return None

        self.service.assistant = Broken()
        self.open_incident()
        self.service.tick()
        self.ask("what now?")
        self.service.tick()
        self.assertIn("could not reach the model", self.telegram.sent[-1][1])

    def test_an_empty_question_is_ignored(self) -> None:
        sent = len(self.telegram.sent)
        self.ask("   ")
        self.service.tick()
        self.assertEqual(len(self.telegram.sent), sent)
        self.assertEqual(self.backend.pending_inputs(NAMESPACE), ())

    # -- what the fifth review found ----------------------------------------------------

    def test_an_override_is_spent_even_when_nothing_can_be_done(self) -> None:
        """A referral, not a restart: the override is gone and the loop stays cold."""
        finding = parse_finding(json.dumps({
            "summary": "the host needs a reboot", "mechanism": "the driver is wedged",
            "evidence": ["target-read@kernel-gpu-log"],
            "action": {"name": "reboot-host", "parameters": {}},
            "expected_effect": "the module reloads", "alternatives": [], "prevention": "",
            "confidence": "medium",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        self.instruct("now", BDF)
        service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIsNone(service.controls.get(f"override:{BDF}"))
        messages, calls = len(self.telegram.sent), len(self.actor.calls)
        for _ in range(40):  # Ten minutes of ticks.
            self.clock.advance(seconds=15)
            service.tick()
        self.assertEqual(len(self.telegram.sent), messages)
        self.assertEqual(len(self.actor.calls), calls)

    def test_an_override_nobody_takes_up_lapses(self) -> None:
        self.self_service()
        self.instruct("now", BDF)
        self.service.tick()
        self.assertIsNotNone(self.service.controls.get(f"override:{BDF}"))
        self.clock.advance(seconds=OVERRIDE_LIFETIME.total_seconds() + 60)
        self.open_incident()
        self.service.tick()
        self.assertIn("has lapsed", self.texts())
        self.assertIsNone(self.service.controls.get(f"override:{BDF}"))

    def test_an_override_cannot_overrule_a_refusal(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.store_input(InputKind.DENIAL_COMMAND, proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "refused_by_operator")])
        self.instruct("now", BDF)
        for _ in range(4):
            self.clock.advance(hours=1)
            self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1)
        self.assertEqual(self.restarts(), 0)
        # And /release takes the standing override away as well as the hold.
        self.instruct("release", BDF)
        self.service.tick()
        self.assertIsNone(self.service.controls.get(f"override:{BDF}"))

    def test_an_override_on_one_gpu_does_not_speak_for_another(self) -> None:
        self.self_service()
        other = "0000:c1:00.0"
        self.open_incident()
        self.actor.status_changes = {"handover_blocked": [BDF, other]}
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.open_incident(other)
        self.clock.advance(minutes=5)
        self.instruct("now", BDF)
        self.service.tick()
        acted = [row[0] for row in self.actions_db.execute(
            "SELECT bdf FROM tc_action_cycles ORDER BY created_utc, rowid"
        )]
        self.assertEqual(acted, [BDF, BDF])
        self.assertNotIn(f"GPU {other}: restarting", self.texts())

    def test_a_referral_that_cannot_be_written_is_finished_later(self) -> None:
        finding = parse_finding(json.dumps({
            "summary": "replace the exporter", "mechanism": "it keeps handles open",
            "evidence": ["target-read@gpu-handles"],
            "action": {"name": "replace-monitoring-container",
                       "parameters": {"container": "dcgm-exporter", "image": "acme/exporter:1.0"}},
            "expected_effect": "handovers stop failing", "alternatives": [], "prevention": "",
            "confidence": "high",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        original = self.cycles.update
        failures = {"left": 1}

        def refuse_first_finish(cycle_id, now, **values):
            if values.get("stage") == "done" and failures["left"]:
                failures["left"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return original(cycle_id, now, **values)

        self.cycles.update = refuse_first_finish
        self.open_incident()
        service.tick()
        # The write failed, so the referral is unfinished rather than lost.
        self.assertEqual(self.cycle_rows(), [("reporting", None)])
        self.clock.advance(minutes=1)
        service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIn("could not carry out what I concluded", self.texts())
        # The episode is not silently stuck: a later cycle can still happen.
        self.clock.advance(hours=5)
        service.tick()
        self.assertEqual(len(self.cycle_rows()), 2)

    def test_a_pause_stops_a_cycle_already_under_way(self) -> None:
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])
        self.instruct("pause")
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])  # No request posted.
        self.instruct("resume")
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        # An approval while paused does not act either.
        _chat, _text, buttons = self.telegram.sent[-1]
        _prefix, proposal_id, nonce = buttons[0][1].split(":")
        self.instruct("pause")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 0)
        self.assertIn("I am paused, so I did not act on that", self.texts())

    def test_a_finding_about_another_container_is_not_authority_to_restart_this_one(self) -> None:
        self.self_service()
        finding = parse_finding(json.dumps({
            "summary": "the gddr6 exporter is stuck", "mechanism": "it stopped reporting",
            "evidence": ["target-read@containers"],
            "action": {"name": "restart-monitoring-container",
                       "parameters": {"container": "gddr6-exporter"}},
            "expected_effect": "metrics resume", "alternatives": [], "prevention": "",
            "confidence": "high",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIn("gddr6-exporter", self.texts())

    def test_questions_do_not_crowd_out_the_incident_loop(self) -> None:
        class Counting:
            def __init__(self):
                self.calls = 0

            def answer(self, question, context, subject):
                self.calls += 1
                return f"answer {self.calls}"

        class CountingReader:
            def __init__(self):
                self.calls = 0

            def read_all(self, subject=None):
                self.calls += 1
                return {"gpu-handles": "pid=1 comm=dcgm-exporter container=abc devices=nvidia5"}

        assistant, reader = Counting(), CountingReader()
        self.service.assistant = assistant
        self.service.reader = reader
        self.open_incident()
        for index in range(20):
            self.ask(f"question {index}")
        self.service.tick()
        self.assertEqual(assistant.calls, MAX_QUESTIONS_PER_TICK)
        self.assertEqual(self.stages(), ["awaiting_backup"])  # The incident still moved.
        # The evidence behind answers is reused rather than re-read per question.
        self.clock.advance(seconds=30)
        self.service.tick()
        self.assertEqual(assistant.calls, 2 * MAX_QUESTIONS_PER_TICK)
        self.assertEqual(reader.calls, 1)

    def test_the_request_says_why_and_quotes_only_what_it_binds(self) -> None:
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.service.tick()
        _chat, text, _buttons = self.telegram.sent[-1]
        self.assertIn("Why: ", text)
        self.assertIn("cannot be handed to its VM rental", text)
        # A change in visible GPUs now withdraws the request rather than being ignored.
        self.actor.status_changes = {"nvidia_visible_count": 2}
        self.clock.advance(minutes=5)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "superseded")])

    def test_a_restart_of_the_service_does_not_lose_its_own_waiting(self) -> None:
        """Waiting periods and the unknown-result reminder survive the process."""
        original = self.actor.run

        def unreachable_ledger(operation: str, request_id: str) -> dict:
            if operation == "result":
                raise ActorError("target unreachable")
            return original(operation, request_id)

        self.actor.run = unreachable_ledger
        self.restart_with({"ok": False, "reason": "restart_timeout", "state": "executed"})
        self.assertEqual(self.cycle_rows(), [("done", "unknown")])
        said = len(self.telegram.sent)
        for _hour in range(5):  # Five hours, a fresh process every time.
            self.clock.advance(hours=1)
            self.build_service().tick()
        self.assertEqual([t for _c, t, _b in self.telegram.sent[said:]], [], "reminded early")
        self.clock.advance(hours=2)
        self.build_service().tick()
        self.assertIn("restart result is still unknown", self.texts())
        # And having said it, a fresh process does not say it again straight away.
        said = len(self.telegram.sent)
        self.clock.advance(minutes=30)
        self.build_service().tick()
        self.assertEqual(len(self.telegram.sent), said)

    def test_a_restart_of_the_service_does_not_reset_delivery_backoff(self) -> None:
        self.restart_with({"ok": True, "state": "executed"})
        self.assertEqual(self.cycle_rows(), [("done", "succeeded")])
        # The outcome message cannot be delivered, so it backs off.
        self.telegram.fail_next = 99
        self.cycles.update(
            self.cycles.episode(INCIDENT_KEY, 1)[0].cycle_id, self.clock(),
            notice="the outcome, undelivered",
        )
        self.service.tick()
        attempts = self.telegram.attempts
        for _ in range(2):  # Restarting does not shorten the wait.
            self.clock.advance(seconds=20)
            self.build_service().tick()
        self.assertEqual(self.telegram.attempts, attempts, "retried before the backoff was up")
        self.clock.advance(seconds=DELIVERY_RETRY.total_seconds())
        self.build_service().tick()
        self.assertEqual(self.telegram.attempts, attempts + 1)

    def test_an_override_does_not_cut_in_front_of_a_cycle_under_way(self) -> None:
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.instruct("now", BDF)
        for _ in range(4):
            self.clock.advance(minutes=1)
            self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 1, "started a second cycle alongside the first")
        self.assertEqual(self.restarts(), 0)

    def test_now_does_not_promise_what_a_waiting_cycle_prevents(self) -> None:
        """An override cannot cut in front, so "right away" would be a falsehood."""
        self.pending_proposal()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.instruct("now", BDF)
        self.service.tick()
        said = self.texts()
        self.assertNotIn("Right away", said)
        self.assertIn("I am already waiting on", said)
        self.assertIn("/again", said)
        # It is still recorded, so taking the cycle back lets it go at once.
        self.assertIsNotNone(self.service.controls.get(f"override:{BDF}"))

    def test_release_takes_back_a_standing_override(self) -> None:
        self.instruct("now", BDF)
        self.service.tick()
        self.assertIsNotNone(self.service.controls.get(f"override:{BDF}"))
        self.instruct("release", BDF)
        self.service.tick()
        self.assertIsNone(self.service.controls.get(f"override:{BDF}"),
                          "release left the override standing")
        # And with it gone the waiting period applies again.
        self.self_service()
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.restarts(), 1)
        self.clock.advance(minutes=1)
        self.open_incident()
        self.service.tick()
        self.assertEqual(self.restarts(), 1)

    def test_eligibility_reads_one_gpus_override_and_never_precedes_a_live_cycle(self) -> None:
        """Both guards on the override branch, at a boundary the loop itself cannot reach.

        The loop starts nothing while a cycle is active and asks about one GPU at a time,
        so these are checked where they are decided rather than through a whole tick.
        """
        other = "0000:c1:00.0"
        self.open_incident()
        self.open_incident(other)
        # Set where /now sets it, so no tick can take it up before it is examined.
        self.service.controls.set(
            f"override:{BDF}", f"telegram:4242@{_text(self.clock())}", 4242, self.clock()
        )
        now = self.clock() + timedelta(minutes=1)
        # An override standing on one GPU says nothing about another GPU's episode.
        self.cycles.create(Cycle(
            cycle_id="finished", bdf=other, incident_key=f"key-{other}", episode=1,
            stage="done", evidence_revision="", evidence_ref="", trigger_utc=_text(self.clock()),
            retrigger_utc=_text(self.clock()), backup_ref=None, proposal_id=None, nonce=None,
            digest=None, shape=None, created_utc=_text(self.clock()),
        ))
        self.cycles.update(
            "finished", self.clock(), stage="done", result="succeeded",
            detail="restarted", finished_utc=_text(self.clock()),
        )
        self.assertFalse(
            self.service._eligible(f"key-{other}", 1, now, other),
            "one GPU's override made another GPU's episode eligible",
        )
        # And an override never starts a second cycle beside one still under way.
        self.cycles.create(Cycle(
            cycle_id="live", bdf=BDF, incident_key=INCIDENT_KEY, episode=1,
            stage="awaiting_answer", evidence_revision="", evidence_ref="",
            trigger_utc=_text(self.clock()), retrigger_utc=_text(self.clock()),
            backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
            created_utc=_text(self.clock()),
        ))
        self.assertFalse(
            self.service._eligible(INCIDENT_KEY, 1, now, BDF),
            "an override cut in front of a cycle that had not finished",
        )

    def test_an_override_does_not_make_another_gpus_episode_eligible(self) -> None:
        self.self_service()
        other = "0000:c1:00.0"
        self.actor.status_changes = {"handover_blocked": [other]}
        self.open_incident(other)
        self.service.tick()
        self.assertEqual(self.restarts(), 1)  # Now that GPU is inside its waiting period.
        self.clock.advance(minutes=1)
        self.open_incident(other)
        self.instruct("now", BDF)  # An override for a different GPU.
        self.service.tick()
        self.assertEqual(self.restarts(), 1, "one GPU's override released another's waiting")

    def test_an_unfinished_referral_is_picked_up_by_a_fresh_process(self) -> None:
        finding = parse_finding(json.dumps({
            "summary": "the host needs a reboot", "mechanism": "the driver is wedged",
            "evidence": ["target-read@kernel-gpu-log"],
            "action": {"name": "reboot-host", "parameters": {}},
            "expected_effect": "the module reloads", "alternatives": [], "prevention": "",
            "confidence": "medium",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        original = self.cycles.update

        def refuse_finish(cycle_id, now, **values):
            if values.get("stage") == "done":
                raise sqlite3.OperationalError("database is locked")
            return original(cycle_id, now, **values)

        self.cycles.update = refuse_finish
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [("reporting", None)])
        # A restart of the service: recover() must finish what was left unsaid.
        self.clock.advance(minutes=1)
        self.build_service().recover()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])

    def test_the_target_is_not_read_more_often_than_the_status_interval(self) -> None:
        # An open incident the target does not confirm: every tick reaches the read and
        # none of them starts a cycle, so the cadence is all that holds them back.
        self.actor.status_changes = {"handover_blocked": []}
        self.open_incident()
        self.service.tick()
        reads = self.status_reads()
        self.assertEqual(reads, 1)
        for _ in range(8):  # Two minutes of ticks, a fresh process every time.
            self.clock.advance(seconds=15)
            self.build_service().tick()
        self.assertEqual(self.status_reads(), reads,
                         "read the target again inside its own interval")
        self.clock.advance(seconds=STATUS_RETRY_INTERVAL.total_seconds())
        self.build_service().tick()
        self.assertEqual(self.status_reads(), reads + 1)

    # -- asking an investigator that answers on its own schedule --------------------

    def waiting_service(self):
        """A diagnoser that answers only when the test says so."""
        class Waiting:
            def __init__(self) -> None:
                self.answer = None
                self.asked = 0
                self.uses_reads = True

            def diagnose(self, request):
                self.asked += 1
                if self.answer is None:
                    return Diagnosis(None, "model", reason="waiting", pending=True)
                return self.answer

        self.diagnoser = Waiting()
        self.service.diagnoser = self.diagnoser
        return self.service

    def test_the_loop_keeps_working_while_the_investigator_thinks(self) -> None:
        service = self.waiting_service()
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [], "acted before it had been told anything")
        # Instructions, questions and answers all still get through.
        self.instruct("status")
        service.tick()
        self.assertIn("Running.", self.texts())
        for _ in range(8):
            self.clock.advance(minutes=2)
            service.tick()
        self.assertEqual(self.cycle_rows(), [])
        self.assertEqual(self.restarts(), 0)
        # When the answer lands, the cycle starts from it.
        self.diagnoser.answer = Diagnosis(parse_finding(json.dumps({
            "summary": "the exporter holds the GPU", "mechanism": "open handles",
            "evidence": ["target-read@gpu-handles"],
            "action": {"name": "restart-monitoring-container",
                       "parameters": {"container": "dcgm-exporter"}},
            "expected_effect": "the handover proceeds", "alternatives": [], "prevention": "",
            "confidence": "high",
        })), "model")
        self.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"])

    def test_nothing_is_recorded_as_a_diagnosis_until_one_is_reached(self) -> None:
        service = self.waiting_service()
        self.open_incident()
        for _ in range(4):
            self.clock.advance(minutes=5)
            service.tick()
        recorded = self.state_db.execute(
            "SELECT COUNT(*) FROM tc_action_evidence WHERE kind='diagnosis'"
        ).fetchone()[0]
        self.assertEqual(recorded, 0, "a question was filed as a conclusion")

    def test_a_silent_investigator_is_announced_and_not_merely_implied(self) -> None:
        """A fallback proposal looks healthy; the broken model path must say so."""
        service = self.waiting_service()
        self.open_incident()
        service.tick()
        self.clock.advance(seconds=DIAGNOSIS_WAIT.total_seconds() + 60)
        service.tick()
        self.assertIn("I cannot reach the investigator", self.texts())
        said = len([t for t in self.texts().splitlines() if "cannot reach" in t])
        # Said once, not with every fault it attends while the model is down.
        for _ in range(6):
            self.clock.advance(minutes=20)
            self.open_incident()
            service.tick()
        self.assertEqual(
            len([t for t in self.texts().splitlines() if "cannot reach" in t]), said
        )
        # And it says when the model comes back, so silence is never the all-clear.
        self.diagnoser.answer = Diagnosis(parse_finding(json.dumps({
            "summary": "the exporter holds the GPU", "mechanism": "open handles",
            "evidence": ["target-read@gpu-handles"],
            "action": {"name": "restart-monitoring-container",
                       "parameters": {"container": "dcgm-exporter"}},
            "expected_effect": "the handover proceeds", "alternatives": [], "prevention": "",
            "confidence": "high",
        })), "model")
        self.clock.advance(hours=5)
        self.open_incident()
        service.tick()
        self.assertIn("The investigator is answering again", self.texts())

    def test_an_investigator_that_never_answers_does_not_strand_the_fault(self) -> None:
        service = self.waiting_service()
        self.open_incident()
        service.tick()
        self.clock.advance(seconds=DIAGNOSIS_WAIT.total_seconds() + 60)
        service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"], "the rule never took over")
        recorded = json.loads(self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis'"
        ).fetchone()[0])
        self.assertEqual(recorded["source"], "rule")
        self.assertEqual(recorded["reason"], "the investigator did not answer in time")

    def test_waiting_for_an_answer_survives_a_restart_of_the_service(self) -> None:
        service = self.waiting_service()
        self.open_incident()
        service.tick()
        for _ in range(6):  # A fresh process every few minutes, for half an hour.
            self.clock.advance(minutes=5)
            restarted = self.build_service()
            restarted.diagnoser = self.diagnoser
            restarted.tick()
        # The deadline was set once and kept, so the rule took over on time rather
        # than the wait starting again with every process.
        self.assertEqual(self.stages(), ["awaiting_backup"])

    def test_a_pending_answer_does_not_spend_an_override(self) -> None:
        service = self.waiting_service()
        self.open_incident()
        self.instruct("now", BDF)
        service.tick()
        self.assertIsNotNone(service.controls.get(f"override:{BDF}"), "spent on a non-answer")

    # -- asking for a fresh look ----------------------------------------------------

    def test_a_withdrawal_frees_the_fault_without_condemning_it(self) -> None:
        proposal_id, _nonce = self.pending_proposal()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.instruct("again", BDF)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "withdrawn_by_operator")])
        self.assertIn("look at 0000:a1:00.0 again", self.texts())
        # The episode is neither closed nor made to wait: a withdrawal is not a cycle
        # that failed, so the fresh look follows as soon as the target is read again.
        self.clock.advance(seconds=STATUS_RETRY_INTERVAL.total_seconds() + 10)
        self.open_incident()
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 2)
        self.assertEqual(self.stages()[-1], "awaiting_backup")

    def test_a_withdrawal_is_not_a_refusal(self) -> None:
        """A refusal closes the episode; withdrawing must not be mistaken for one."""
        self.pending_proposal()
        self.instruct("again", BDF)
        self.service.tick()
        for _ in range(6):
            self.clock.advance(hours=1)
            self.open_incident()
            self.service.tick()
        self.assertGreater(len(self.cycle_rows()), 1, "the episode was closed by a withdrawal")

    def test_a_restart_already_under_way_cannot_be_taken_back(self) -> None:
        """Once an execution is in flight, taking the request back would be a lie."""
        self.cycles.create(Cycle(
            cycle_id="running", bdf=BDF, incident_key=INCIDENT_KEY, episode=1,
            stage="executing", evidence_revision="", evidence_ref="",
            trigger_utc=_text(self.clock()), retrigger_utc=_text(self.clock()),
            backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
            created_utc=_text(self.clock()),
        ))
        self.instruct("again", BDF)
        self.service.tick()
        self.assertIn("past the point where I can take that back", self.texts())
        # However the cycle later settles, it was never taken back by the instruction.
        self.assertNotIn("withdrawn_by_operator", [result for _stage, result in self.cycle_rows()])

    def test_a_withdrawal_names_its_gpu(self) -> None:
        self.pending_proposal()
        self.instruct("again")
        self.service.tick()
        self.assertIn("Name the GPU to look at again", self.texts())
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.instruct("again", "0000:c1:00.0")
        self.service.tick()
        self.assertIn("Nothing is waiting on 0000:c1:00.0", self.texts())
        self.assertEqual(self.stages(), ["awaiting_answer"])

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
