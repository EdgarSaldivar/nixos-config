from __future__ import annotations

import json
from dataclasses import replace
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
    CONVERSATION_WAIT,
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
    MAX_OBSERVE_ROUNDS,
    LIVE_STATES,
    LOOP_STATES,
    OBSERVE_LOOP_DEADLINE,
    REVIEW_KEY,
    ActionService,
    Cycle,
    CycleStore,
    InboxApprovalAuthenticator,
    Observations,
    SystemdBackupProbe,
    _text,
)
from terracompute_ops.actions import ActionBroker, ApprovalKind, HumanApprovalEvent, MembershipDecision
from terracompute_ops.monitor_restart import (
    HANDOVER_CLEARED,
    ActorError,
    EvidenceStore,
    MonitorRestartAdapter,
    handover_incident_signature,
)
from terracompute_ops.diagnosing import Diagnosis, Reply
from terracompute_ops.diagnosis import ReadRequest, Steer
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


class _FixedDiagnoser:
    """A diagnoser that answers exactly this, whatever it is asked."""

    uses_reads = True

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def diagnose(self, request):
        self.requests.append(request)
        return self.answer


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
        self.cleared: list[tuple[int, int]] = []
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

    def clear_buttons(self, chat_id, message_id):
        """Take the buttons off a spent request. Best effort, like the real one."""
        self.cleared.append((chat_id, message_id))


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
        # The shared store's observation table, as the scheduler fills it. The service
        # reads the newest Vast reading out of it rather than holding a Vast key.
        self.state_db.execute(
            """CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 target TEXT NOT NULL, machine_id TEXT NOT NULL, source TEXT NOT NULL,
                 source_utc TEXT NOT NULL, receipt_utc TEXT NOT NULL, boot_id TEXT NOT NULL,
                 status TEXT NOT NULL, freshness TEXT NOT NULL,
                 evidence_sha256 TEXT NOT NULL, evidence_json BLOB NOT NULL)"""
        )
        self.state_db.commit()
        self.actions_db = sqlite3.connect(self.actions_path)
        self.actor = FakeActor(self.clock)
        self.backend = SQLiteUpdateBackend(root / "inbox.sqlite3")
        self.telegram = FakeTelegram()
        self.consumer = FakeConsumer()
        self.backup = FakeBackup(self.clock, self.state_path)
        self.membership = Membership(self.clock)
        self._all_reports: list[str] = []
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
            clock=self.clock, report=self._all_reports.append,
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

    @property
    def reports(self) -> list[str]:
        """Failure and recovery signalling, without the informational why-lines."""
        return [line for line in self._all_reports if '"why"' not in line]

    @property
    def why(self) -> list[str]:
        return [line for line in self._all_reports if '"why"' in line]

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
        # The shared store holds what other services wrote and what this one must back
        # up. What it must never hold is anything this service decides with: an
        # approval, an attempt or a cycle there would be authority outside the boundary.
        self.assertEqual(
            shared - {"sqlite_sequence"},
            {"incidents", "observations", "tc_action_evidence"},
        )
        self.assertFalse(
            {name for name in shared if name.startswith("tc_action_")} - {"tc_action_evidence"}
        )
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

    def test_other_gpu_incidents_are_diagnosed_without_entering_the_handover_path(self) -> None:
        service = self.diagnosing_service(Diagnosis(None, "model", reason="no answer"))
        self.state_db.execute(
            """INSERT INTO incidents(dedup_key, source, fault_family, stable_signature, status,
                 notification_episode) VALUES ('driver', 'ssh', 'gpu', ?, 'open', 1)""",
            ("a" * 64,),
        )
        self.state_db.commit()
        for _ in range(4):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(len(self.diagnoser.requests), 1)
        self.assertEqual(self.diagnoser.requests[0].incident_key, "driver")
        self.assertEqual(self.cycle_rows(), [], "a non-handover signature made a proposal")
        self.assertEqual(self.restarts(), 0)

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

    def test_an_approval_survives_the_time_the_target_call_takes(self) -> None:
        """Reading the target is an SSH round trip, and the clock moves during it.

        The approval event was timestamped before that call and the proposal after it,
        so the event was always earlier than the proposal it approved and the broker
        refused every one as "outside the proposal lifetime". A frozen clock made the
        two identical, so the suite was green while no approval had ever been recorded
        and no restart had ever run on the machine.
        """
        proposal_id, nonce = self.pending_proposal()
        real_status = self.service.adapter.status

        def slow_status():
            self.clock.advance(seconds=3)   # as a real call to the target does
            return real_status()

        self.service.adapter.status = slow_status
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(self.restarts(), 1, "the approval was refused")
        self.assertNotIn("was not accepted", self.texts())

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
        # Recorded as expired, NOT superseded. They are different things and they
        # shared one outcome: a late tap was reported as the machine having changed.
        self.assertEqual(self.cycle_rows(), [("done", "expired")])
        self.assertEqual(self.restarts(), 0)
        self.assertIn("I will ask again", self.texts())
        said = self.texts()
        self.assertIn("expired", said)
        self.assertNotIn(
            "the machine has changed", said,
            "a late tap was blamed on the hardware, which sends a person hunting a "
            "fault that is not there",
        )

    def test_a_denial_that_does_not_match_a_waiting_request_does_nothing(self) -> None:
        proposal_id, nonce = self.pending_proposal()
        self.store_input(InputKind.DENIAL_COMMAND, proposal_id, "x" * 24)
        self.store_input(InputKind.DENIAL_COMMAND, "mr-000000000000", nonce)
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.assertIn("does not match a waiting restart request", self.texts())

    def test_the_request_shows_the_command_that_will_run(self) -> None:
        """What is approved is a command, so the command is what a person is shown.

        A catalogue name told them the shape of the thing -- and could only name things
        somebody had thought of first. The command says exactly what will happen, with
        nothing between the sentence and the machine.
        """
        self.pending_proposal()
        text = [entry[1] for entry in self.telegram.sent if entry[2]][-1]
        self.assertIn("docker restart dcgm-exporter", text)
        self.assertIn("I want to run", text)

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
        self.assertIn("Waiting for you", text)
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
        self.assertIn("docker restart dcgm-exporter", stored["action"])
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

    def test_the_spend_backstop_is_said_plainly_and_not_as_a_broken_investigator(self) -> None:
        """A ceiling we chose is not the same as a model we cannot reach.

        Reporting it as unreachable would send somebody looking for a fault in the
        investigator, when what happened is that this system spent ten times a heavy
        day on itself and stopped -- which is exactly the thing worth noticing.
        """
        service = self.diagnosing_service(
            Diagnosis(None, "rule", reason="model unavailable (daily-spend-backstop)")
        )
        self.open_incident()
        service.tick()
        said = self.texts()
        self.assertIn("stopped investigating on my own", said)
        self.assertIn("Ask me anything and I will still answer", said)
        self.assertNotIn("cannot reach the investigator", said)

    def test_a_second_attempt_is_a_new_investigation_and_a_new_question(self) -> None:
        """A fix that did not work must not be investigated on its predecessor's change.

        The budget belongs to the investigation, so a retry that inherited the first
        attempt's investigation would inherit what it spent -- and, because the subject
        would be unchanged too, would be answered by replaying the conclusion reached
        before we touched the machine.
        """
        service = self.diagnosing_service(Diagnosis(None, "model", reason="no answer"))
        self.open_incident()
        service.tick()
        first = self.diagnoser.requests[0]
        self.assertEqual(first.attempts, 0)
        self.assertEqual(first.investigation_id, f"{INCIDENT_KEY}#1#0")

        # The service carries something out; the machine is no longer what it was.
        self.service.cycles.create(Cycle(
            cycle_id="c-executed", bdf=BDF, incident_key=INCIDENT_KEY, episode=1,
            stage="done", evidence_revision="", evidence_ref="", trigger_utc=_text(self.clock()),
            retrigger_utc=_text(self.clock()), backup_ref=None, proposal_id=None,
            nonce=None, digest=None, shape=None, created_utc=_text(self.clock()),
        ))
        self.service.cycles.update(
            "c-executed", self.clock(), result="succeeded",
            detail=f"restart completed; {HANDOVER_CLEARED}",
            finished_utc=_text(self.clock()),
        )
        self.clock.advance(hours=4)
        self.service.schedule.clear("status")
        service.tick()

        second = self.diagnoser.requests[-1]
        self.assertEqual(second.attempts, 1)
        self.assertEqual(second.investigation_id, f"{INCIDENT_KEY}#1#1")
        self.assertNotEqual(
            first.subject_hash(), second.subject_hash(),
            "the retry asked the same question and would have replayed the old answer",
        )

    def test_what_a_renter_reported_reaches_the_diagnosis(self) -> None:
        """The customer's own account of the fault was collected and shown to nobody.

        The scheduler has polled Vast every pass all along; it reached incidents and
        stopped there, so the one thing that says what the renter actually experienced
        never got to the thing whose job is working out what is wrong.
        """
        self.state_db.execute(
            """INSERT INTO observations(
                 target,machine_id,source,source_utc,receipt_utc,boot_id,status,
                 freshness,evidence_sha256,evidence_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "vast:17049", "17049", "vast", "2026-09-17T05:50:00Z",
                "2026-09-17T05:50:01Z", "boot", "unhealthy", "fresh", "a" * 64,
                json.dumps({"snapshot": {
                    "machine": {"listed": True, "rentable": False, "rented": True,
                                "total_gpus": 8, "rented_gpus": 4},
                    "market": {"search_complete": True, "advertised": True,
                               "rentable": False, "launch_proven": False},
                    "reports": [{
                        "problem": "gpu_unavailable",
                        "message": "gpu 3 keeps dropping off the bus mid-job\x07",
                        "created_at": "2026-09-17T05:31:00Z",
                    }],
                }}),
            ),
        )
        self.state_db.commit()
        service = self.diagnosing_service(Diagnosis(None, "model", reason="no answer"))
        self.open_incident()
        service.tick()
        request = self.diagnoser.requests[0]
        self.assertIn("gpu 3 keeps dropping off the bus", request.vast)
        self.assertNotIn("\x07", request.vast, "a renter's control characters went through")
        self.assertEqual(request.vast_reports, 1)
        prompt = request.prompt()
        self.assertIn("gpu 3 keeps dropping off the bus", prompt)
        self.assertIn("written by a customer", prompt, "it was not framed as their claim")
        self.assertIn("rented_gpus=4", prompt)

    def test_a_stale_vast_reading_says_so_rather_than_passing_as_current(self) -> None:
        """It is polled every pass, so an old one means collection is broken."""
        self.state_db.execute(
            """INSERT INTO observations(
                 target,machine_id,source,source_utc,receipt_utc,boot_id,status,
                 freshness,evidence_sha256,evidence_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "vast:17049", "17049", "vast", "2026-09-16T06:00:00Z",
                "2026-09-16T06:00:01Z", "boot", "healthy", "fresh", "b" * 64,
                json.dumps({"snapshot": {"machine": {"listed": True}, "reports": []}}),
            ),
        )
        self.state_db.commit()
        service = self.diagnosing_service(Diagnosis(None, "model", reason="x"))
        self.open_incident()
        service.tick()
        vast = self.diagnoser.requests[0].vast
        self.assertIn("hours old", vast)
        self.assertIn("do not read it as the current state", vast)

    def test_a_renter_report_makes_it_a_different_question(self) -> None:
        """A complaint arriving is materially new, the way a new read is."""
        base = self.diagnosing_service(Diagnosis(None, "model", reason="x"))
        self.open_incident()
        base.tick()
        quiet = self.diagnoser.requests[0]
        self.assertNotEqual(
            quiet.subject_hash(),
            replace(quiet, vast_reports=1).subject_hash(),
            "a renter filing a report replayed the answer given before it arrived",
        )

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
            "action": {"command": "docker exec C.51217040 sh", "intent": "look inside"},
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
        self.assertIn("customer", text)
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
            "action": {"command": "docker exec C.51217040 sh", "intent": "look inside"},
            "expected_effect": "the module reloads cleanly",
            "alternatives": [],
            "prevention": "",
            "confidence": "medium",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIn("customer", self.texts())
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
        self.assertIn("Wanted: docker restart dcgm-exporter", latest)
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
            "action": {"command": "docker exec C.51217040 sh", "intent": "look inside"},
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
            "action": {"command": "docker exec C.51217040 sh", "intent": "look inside"},
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
            "action": {"command": "docker restart vast-gddr6-metrics-exporter-1", "intent": "release its GPU handles"},
            "expected_effect": "metrics resume", "alternatives": [], "prevention": "",
            "confidence": "high",
        }))
        service = self.diagnosing_service(Diagnosis(finding, "model"))
        self.open_incident()
        service.tick()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.cycle_rows(), [("done", "referred_to_operator")])
        self.assertIn("vast-gddr6-metrics-exporter-1", self.texts())

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
        self.assertIn("take back the one on", said)
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
            "action": {"command": "docker exec C.51217040 sh", "intent": "look inside"},
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
            "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
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
            "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
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

    def test_failing_to_ask_does_not_spend_the_waiting(self) -> None:
        """A proposal nobody ever saw has not worn out anybody's patience.

        Telegram failing doubled the wait between proposals, and eight such failures
        would have stopped an unresolved incident being raised at all.
        """
        self.open_incident()
        self.service.tick()
        self.backup.completed = self.clock() + timedelta(seconds=1)
        self.clock.advance(minutes=1)
        self.telegram.fail_next = 99
        self.service.tick()
        self.assertEqual(self.cycle_rows()[-1][1], "notify_failed")
        self.telegram.fail_next = 0
        # The next look follows on the first interval, not a doubled one.
        self.clock.advance(seconds=PROPOSAL_INTERVAL.total_seconds() + 60)
        self.open_incident()
        self.service.tick()
        self.assertEqual(len(self.cycle_rows()), 2)

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

    # -- talking to it --------------------------------------------------------------

    def talking_service(self, answer="I would look at replacing it."):
        """A conversation that publishes now and answers when the test says so."""
        class Conversation:
            def __init__(self) -> None:
                self.asked = []
                self.ready = answer

            def ask(self, *, incident_key, episode, bdf, message, sender_id,
                    subject_hash="", briefing="", investigation_id=""):
                self.asked.append(
                    (message, sender_id, bdf, subject_hash, briefing, investigation_id)
                )
                return f"c{len(self.asked):048d}"

            def collect(self, ticket):
                if self.ready is None:
                    return None
                return self.ready if isinstance(self.ready, Reply) else Reply(self.ready)

        self.conversation = Conversation()
        self.service.conversation = self.conversation
        return self.service

    def test_what_the_operator_says_reaches_the_incident_and_comes_back(self) -> None:
        service = self.talking_service()
        self.open_incident()
        self.ask("dont restart it, look at replacing it")
        service.tick()
        self.assertEqual(self.conversation.asked[0][0], "dont restart it, look at replacing it")
        self.assertEqual(self.conversation.asked[0][2], BDF)
        # The answer arrives on a later pass; the loop never waits for the model.
        service.tick()
        self.assertIn("I would look at replacing it.", self.texts())

    def test_talking_reaches_the_investigation_already_under_way(self) -> None:
        """An episode is keyed by the subject hash, so the wrong one is not merely lost.

        It opens a second episode on the same incident, with its own empty thread, and
        the model is asked about an investigation it has never seen. In production that
        looked like the model trying to run `pwd` on a machine it has no shell on.
        """
        service = self.talking_service()
        self.open_incident()
        service.evidence.record("diagnosis", f"incident:{INCIDENT_KEY}", {
            "incident_key": INCIDENT_KEY,
            "subject_hash": "1f" * 32,
            "investigation_id": f"{INCIDENT_KEY}#1#0",
            "summary": "dcgm-exporter is holding the GPU open",
            "mechanism": "it reopens every device node on start",
            "action": "docker restart dcgm-exporter",
        })
        self.ask("dont restart it, look at replacing it")
        service.tick()
        _message, _sender, _bdf, subject, briefing, investigation = self.conversation.asked[0]
        self.assertEqual(subject, "1f" * 32, "a conversation must join the open episode")
        self.assertIn("dcgm-exporter is holding the GPU open", briefing)
        self.assertIn("docker restart dcgm-exporter", briefing)
        # And the investigation it belongs to, so talking is recorded against the work
        # it is about rather than metered as the machine investigating itself.
        self.assertEqual(investigation, f"{INCIDENT_KEY}#1#0")

    def test_a_conversation_before_any_diagnosis_still_goes_through(self) -> None:
        """Nothing concluded yet is not a reason to refuse to talk."""
        service = self.talking_service()
        self.open_incident()
        self.ask("whats wrong with it")
        service.tick()
        _message, _sender, _bdf, subject, briefing, _investigation = self.conversation.asked[0]
        self.assertEqual((subject, briefing), ("", ""))

    def test_saying_it_in_words_is_enough_to_steer_it(self) -> None:
        """Nobody should have to remember a command to pause a machine.

        The model reads what was said and names one thing from a fixed list; this
        service checks it against that list and carries it out. The words are the
        model's, the doing is the service's, and the operator is told both.
        """
        service = self.talking_service(
            Reply("Alright, I will leave it alone.", Steer("hold", BDF))
        )
        self.open_incident()
        self.ask("leave that gpu alone for now please")
        service.tick()
        service.tick()
        self.assertTrue(self.service.controls.held(BDF), "the words did nothing")
        said = self.texts()
        self.assertIn("Alright, I will leave it alone.", said)
        self.assertIn(f"Leaving {BDF} alone", said)

    def test_words_cannot_destroy_a_waiting_request(self) -> None:
        """The model may say it thinks you want it dropped. It may not drop it.

        Every other steer is recoverable -- pause/resume, hold/release, and a look
        changes nothing -- which is the whole reason a model is allowed to read them
        out of someone's sentence. Withdrawal is not recoverable, and on the live
        machine the model read it wrong fourteen times: 14 of 19 cycles ended
        `withdrawn_by_operator` against zero executions ever. "Do we actually need a
        restart though?" is a question, and it was costing the request every time.
        """
        self.pending_proposal()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        said = self.service._steer("withdraw", BDF, 4242)
        # Still waiting. The request is the only thing that can drop itself.
        self.assertEqual(self.stages(), ["awaiting_answer"], "words threw the request away")
        self.assertNotIn(
            "withdrawn_by_operator", [result for _stage, result in self.cycle_rows()]
        )
        self.assertIn("still waiting", said)
        self.assertIn("Leave it", said, "it did not say how to actually drop it")

    def test_an_explicit_instruction_still_withdraws_at_once(self) -> None:
        """Asking is for the inferred path only. A command is already unambiguous."""
        self.pending_proposal()
        self.instruct("again", BDF)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "withdrawn_by_operator")])

    def test_a_spent_request_stops_offering_its_buttons(self) -> None:
        """A single-use button that has been used can only fail if pressed again.

        Approval is bound to one proposal and spent on one restart, so a later press
        is never going to do anything. Leaving it there invites exactly the press that
        cannot work -- and the message itself stays, so what was asked and how it ended
        is still the record.
        """
        proposal_id, nonce = self.pending_proposal()
        sent = [entry for entry in self.telegram.sent if entry[2]]
        self.assertTrue(sent, "the request never carried buttons")
        self.assertEqual(self.telegram.cleared, [], "cleared before it was answered")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertTrue(self.telegram.cleared, "the spent button was left pressable")

    def test_a_request_that_lapses_also_stops_offering_them(self) -> None:
        """However it ends -- answered, withdrawn, lapsed -- the invitation goes."""
        self.pending_proposal()
        self.instruct("again", BDF)
        self.service.tick()
        self.assertEqual(self.cycle_rows(), [("done", "withdrawn_by_operator")])
        self.assertTrue(self.telegram.cleared, "a withdrawn request kept its buttons")

    def test_the_fake_actor_matches_the_real_one(self) -> None:
        """A fake that has drifted hides the bug it was standing in for.

        `_guard` swallows exceptions, so calling the real actor's `approved=` keyword
        against a fake without it looked exactly like an interrupted execution: the
        approval was recorded, "Approved. Running:" was sent, and nothing ran.
        """
        import inspect
        from terracompute_ops.acting import MonitoringActor
        real = inspect.signature(MonitoringActor.run).parameters
        fake = inspect.signature(self.Actor.run).parameters
        self.assertEqual(set(real) - {"self"}, set(fake) - {"self"})

    def test_a_proposed_command_arrives_with_a_button_and_runs_when_tapped(self) -> None:
        """The whole product, end to end, for a command nobody wrote an adapter for.

        A correct diagnosis of a dead GPU proposed `systemctl reboot` and arrived with
        no way to say yes: the only path that built a proposal was the one fixed
        handover restart, so everything else was narrated at somebody. Observed live on
        2026-09-19 after the catalogue was already gone -- the model could propose it,
        and there was still no button.
        """
        actor = self.Actor()
        self.service.actor = actor
        finding = parse_finding(json.dumps({
            "summary": "GPU 0000:61:00.0 has fallen off its PCIe bus",
            "mechanism": "Xid 79 then Xid 154, node reboot required",
            "evidence": ["target-read@kernel-gpu-log"],
            "action": {"command": "systemctl reboot",
                       "intent": "reinitialise the GPU the driver cannot reach"},
            "expected_effect": "eight GPUs enumerate again", "confidence": "high",
        }))
        asked = self.service._ask_about(
            Diagnosis(finding, "model"), BDF, INCIDENT_KEY, 1, self.clock()
        )
        self.assertTrue(asked, "a command needing approval was narrated, not asked")
        sent = [entry for entry in self.telegram.sent if entry[2]][-1]
        self.assertIn("systemctl reboot", sent[1], "the command was not shown")
        self.assertEqual(actor.done, [], "it acted before anybody answered")

        proposal_id, nonce = None, None
        for _label, data in sent[2]:
            if data.startswith("approve:"):
                _, proposal_id, nonce = data.split(":")
        self.approval_input(proposal_id, nonce)
        self.service.tick()
        self.assertEqual(actor.done, ["reboot"], "approving it did not run it")
        self.assertEqual(self.cycle_rows()[-1], ("done", "succeeded"))

    def test_a_command_it_can_do_alone_is_not_put_to_a_person(self) -> None:
        """Only what needs somebody gets a button; the rest would just be noise."""
        finding = self.monitoring_finding("node-exporter")
        self.assertFalse(self.service._ask_about(
            Diagnosis(finding, "model"), BDF, INCIDENT_KEY, 1, self.clock()
        ))

    def test_saying_do_it_sends_the_request_again_with_its_buttons(self) -> None:
        """"I don't see it, send again" had nowhere to land, and neither did "do it".

        The vocabulary could pause, hold, release, look, investigate and withdraw --
        every verb except the one that matters. The only way to say act-on-this was
        `/now <bdf>`, a command to remember, which is the thing this vocabulary exists
        to avoid. So the model answered in prose and described a pending request that
        did not exist, and the operator kept looking for a button nobody had sent.
        """
        proposal_id, nonce = self.pending_proposal()
        before = len([entry for entry in self.telegram.sent if entry[2]])
        self.service._steer("ask-me", BDF, 4242)
        with_buttons = [entry for entry in self.telegram.sent if entry[2]]
        self.assertEqual(len(with_buttons), before + 1, "no button was sent")
        # The same single-use approval, bound to the same proposal. Only the message
        # is new; re-sending must not mint a second way to approve one restart.
        labels = dict((label, data) for label, data in with_buttons[-1][2])
        self.assertIn(f"approve:{proposal_id}:{nonce}", labels.values())
        self.assertIn(f"deny:{proposal_id}:{nonce}", labels.values())

    def test_asking_for_it_does_not_approve_it(self) -> None:
        """Natural language carries the request; the tap is still what authorises it.

        A model reading "do it" out of a sentence must not be what carries a change to
        this machine, or prompt injection becomes an approval bypass.
        """
        self.pending_proposal()
        self.service._steer("ask-me", BDF, 4242)
        self.assertEqual(self.restarts(), 0, "words authorised a restart")
        self.assertEqual(self.stages(), ["awaiting_answer"], "it acted instead of asking")

    def test_a_pass_that_does_nothing_says_why(self) -> None:
        """Silence here cost an evening.

        Between an open incident and a button there were a dozen early returns and not
        one of them said anything. When no button appeared there was nothing to read,
        so working out which guard had fired meant querying the database from another
        machine and inferring it -- six times, wrongly more than once.
        """
        actor = self.Actor()
        self.service.actor = actor
        # A finding it can do alone is not put to a person, and says so.
        self.service._ask_about(
            Diagnosis(self.monitoring_finding("node-exporter"), "model"),
            BDF, INCIDENT_KEY, 1, self.clock(),
        )
        self.assertTrue(any("not-for-a-person" in line for line in self.why), self.why)
        # A finding with no action at all, likewise.
        self.service._ask_about(
            Diagnosis(None, "model"), BDF, INCIDENT_KEY, 1, self.clock()
        )
        self.assertTrue(any("no-action-to-ask-about" in line for line in self.why))

    def test_a_bare_ask_means_the_thing_just_reported(self) -> None:
        """"do it" straight after a report has to reach the fault that was reported.

        Live on 2026-09-19: a bare ask-me set a review on a GPU nobody had mentioned,
        which was no longer faulty, so it quietly did nothing and no request appeared.
        """
        self.service._last_reported = "abc123def456"
        said = self.service._steer("ask-me", "", 4242)
        self.assertIsNotNone(self.service._review_for("abc123def456", self.clock()))
        self.assertIn("abc123def456", said)

    def test_what_a_person_asked_about_is_looked_at_first(self) -> None:
        """Waiting behind three unrelated investigations is being ignored, slowly.

        A fault already investigated is never revisited on its own, so an operator
        asking is the only way it gets looked at again.
        """
        now = self.clock()
        others = [("aaa", 1, "gpu", "critical"), ("bbb", 1, "xid", "error")]
        self.service.controls.set(
            "review:bbb", f"telegram:asked@{_text(now)}", 0, now
        )
        chosen = next(
            (item for item in others if self.service._review_for(item[0], now) is not None),
            others[0],
        )
        self.assertEqual(chosen[0], "bbb", "the asked-for fault queued behind another")

    def test_asking_when_nothing_is_waiting_arranges_one(self) -> None:
        said = self.service._steer("ask-me", BDF, 4242)
        self.assertIn("put the request to you", said)
        self.assertIsNotNone(
            self.service._review_for(BDF, self.clock()), "nothing was set in motion"
        )
        self.assertIsNone(
            self.service._override_for(BDF, self.clock()),
            "asking to be asked handed over the button",
        )

    def test_a_look_asked_for_in_words_cannot_authorise_acting(self) -> None:
        """Looking is read-only, so asking for it in words is safe. Acting is not.

        A misread "look at it again" costs a wasted investigation. A misread that could
        act would cost a restart nobody asked for, so what the look proposes still
        comes back as a button.
        """
        said = self.service._steer("look-again", BDF, 4242)
        self.assertIn("again", said)
        self.assertIsNotNone(
            self.service._review_for(BDF, self.clock()), "the words bought no look"
        )
        self.assertIsNone(
            self.service._override_for(BDF, self.clock()),
            "asking for a look handed over the button",
        )
        self.assertFalse(
            self.service._may_act_alone(self.clock(), BDF, None),
            "a look asked for in words let it act without asking",
        )
        # And the look it bought is used up by the look, not left standing.
        self.assertEqual(self.service._spend_review(BDF, self.clock()), "telegram:4242")
        self.assertIsNone(self.service._review_for(BDF, self.clock()))

    def test_with_no_model_it_can_still_be_told_to_stop(self) -> None:
        """The model is how words become instructions, so its outage must not mute you.

        This understands almost nothing on purpose: whole messages only, so the reverse
        of an instruction is never mistaken for it.
        """
        self.service.conversation = None
        self.service.assistant = None
        self.open_incident()
        self.ask("stop")
        self.service.tick()
        self.assertTrue(self.service.controls.paused)
        self.assertIn("Paused", self.texts())

        self.service.controls.clear("paused")
        self.ask("please don't stop what you are doing")
        self.service.tick()
        self.assertFalse(
            self.service.controls.paused, "a sentence about stopping was read as stop"
        )

    def test_the_loop_keeps_running_while_it_is_being_talked_to(self) -> None:
        service = self.talking_service()
        self.conversation.ready = None  # Still thinking.
        self.open_incident()
        self.ask("what is holding it?")
        for _ in range(4):
            self.clock.advance(seconds=30)
            service.tick()
        self.assertEqual(len(self.conversation.asked), 1, "asked again while thinking")
        self.conversation.ready = "dcgm-exporter is."
        service.tick()
        self.assertIn("dcgm-exporter is.", self.texts())

    def test_an_answer_that_never_comes_is_admitted(self) -> None:
        service = self.talking_service()
        self.conversation.ready = None
        self.open_incident()
        self.ask("what is holding it?")
        service.tick()
        self.clock.advance(seconds=CONVERSATION_WAIT.total_seconds() + 60)
        service.tick()
        self.assertIn("could not get an answer to that in time", self.texts())
        # And it stops trying rather than saying so on every pass.
        said = self.texts().count("could not get an answer")
        self.clock.advance(minutes=10)
        service.tick()
        self.assertEqual(self.texts().count("could not get an answer"), said)

    def test_asking_about_a_request_does_not_cancel_it(self) -> None:
        """Taking it back whenever anybody spoke meant a request could never survive
        being enquired about.

        Three in a row were withdrawn on this machine by somebody asking what was
        going on, so the button vanished every time it was questioned and nothing
        could ever be approved.
        """
        service = self.talking_service("The handle is held by dcgm-exporter, yes.")
        self.pending_proposal()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.ask("wait, is the exporter even the holder?")
        service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"], "the question cancelled it")
        self.assertIn("The handle is held by dcgm-exporter", self.texts())

    def test_asking_for_something_that_changes_it_does_take_it_back(self) -> None:
        """A button must never authorise something the conversation has moved on from."""
        service = self.talking_service(
            Reply("Alright, leaving it alone.", Steer("hold", BDF))
        )
        self.pending_proposal()
        self.assertEqual(self.stages(), ["awaiting_answer"])
        self.ask("leave that gpu alone for now")
        service.tick()
        self.assertEqual(self.cycle_rows()[-1], ("done", "withdrawn_by_operator"))
        self.assertIn("taken back the request", self.texts())

    def test_talking_never_authorises_anything(self) -> None:
        """"Do it" asks; only a button bound to a proposal may act."""
        service = self.talking_service(answer="Yes, restarting is the right call.")
        proposal_id, nonce = self.pending_proposal()
        for words in ("do it", "yes", "approve", "go ahead and restart it"):
            self.ask(words)
        service.tick()
        self.assertEqual(self.restarts(), 0, "words authorised an action")
        # The button still does, on a fresh request.
        self.clock.advance(minutes=1)
        self.open_incident()
        service.conversation = None
        service.tick()

    def test_a_question_answered_from_evidence_costs_the_request_nothing(self) -> None:
        """Only a conversation that can change the investigation withdraws a request."""
        self.pending_proposal()
        self.ask("what did you conclude?")
        self.service.tick()
        self.assertEqual(self.stages(), ["awaiting_answer"], "withdrawn for nothing")

    def test_an_investigator_that_cannot_talk_falls_back_rather_than_going_quiet(self) -> None:
        class Silent:
            def ask(self, **_kwargs):
                raise OSError("spool unavailable")

        self.service.conversation = Silent()
        self.open_incident()
        self.ask("what is going on?")
        self.service.tick()
        self.assertIn("no model to think with", self.texts())
        self.assertTrue(any("_converse" in line for line in self.reports))

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

    class Observer:
        """Answers each read from a script, and records what it was asked."""

        def __init__(self, outputs=None):
            self.asked: list[str] = []
            self.outputs = dict(outputs or {})

        def observe(self, command, subject=None):
            self.asked.append(command)
            text = self.outputs.get(command, f"output of {command}")

            class Result:
                def text(self_inner):
                    return text

            return Result()

    class Asking:
        """A diagnoser that asks for reads until the test lets it conclude."""

        uses_reads = True

        def __init__(self, rounds, finding=None):
            self.rounds = list(rounds)
            self.finding = finding
            self.requests: list = []

        def diagnose(self, request):
            self.requests.append(request)
            if self.rounds:
                commands = self.rounds.pop(0)
                return Diagnosis(
                    None, "model", reason="it asked to look", pending=True,
                    raw_text='{"reads_requested": []}',
                    reads=ReadRequest(commands=tuple(commands), note="because"),
                )
            if self.finding is None:
                return Diagnosis(None, "model", reason="no answer")
            return Diagnosis(self.finding, "model")

    def looking_service(self, rounds, finding=None, outputs=None):
        self.observer = self.Observer(outputs)
        self.diagnoser = self.Asking(rounds, finding)
        self.service.observer = self.observer
        self.service.diagnoser = self.diagnoser
        return self.service

    def finding(self):
        return parse_finding(json.dumps({
            "summary": "the exporter holds the GPU",
            "mechanism": "it keeps handles open",
            "evidence": ["observe@r1c1"],
            "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
            "expected_effect": "the handover proceeds",
            "confidence": "high",
        }))

    def test_it_looks_then_asks_again_with_what_it_saw(self) -> None:
        service = self.looking_service(
            [["ls -l /proc/1/fd"]], self.finding(),
            outputs={"ls -l /proc/1/fd": "nvidia6 -> held by dcgm-exporter"},
        )
        self.open_incident()
        service.tick()  # asks, and is told to look
        self.assertEqual(self.stages(), [], "it acted before it had looked")
        service.tick()  # runs the read
        self.assertEqual(self.observer.asked, ["ls -l /proc/1/fd"])
        self.clock.advance(minutes=6)
        service.tick()  # asks again, now with the output
        second = self.diagnoser.requests[-1]
        self.assertEqual(len(second.observe_rounds), 1)
        self.assertIn("nvidia6 -> held by dcgm-exporter", second.prompt())
        self.assertIn("observe@r1c1", second.prompt())
        self.assertNotEqual(
            self.diagnoser.requests[0].subject_hash(), second.subject_hash(),
            "the second round asked the same question and would replay the first answer",
        )

    def test_the_reads_are_run_one_a_pass_and_survive_a_restart(self) -> None:
        service = self.looking_service([["a", "b", "c"]], self.finding())
        self.open_incident()
        service.tick()
        self.assertEqual(self.observer.asked, ["a"], "a pass ran more than one read")
        restarted = self.build_service()
        restarted.observer = self.observer
        restarted.diagnoser = self.diagnoser
        restarted.tick()
        restarted.tick()
        self.assertEqual(self.observer.asked, ["a", "b", "c"], "a restart lost the round")

    def test_it_stops_looking_after_its_last_round(self) -> None:
        service = self.looking_service([["x"]] * (MAX_OBSERVE_ROUNDS + 2))
        self.open_incident()
        for _ in range(MAX_OBSERVE_ROUNDS * 3 + 6):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertTrue(
            [request for request in self.diagnoser.requests if request.final_round],
            "it was never told to conclude",
        )
        self.assertLessEqual(
            len(self.observer.asked), MAX_OBSERVE_ROUNDS,
            "it looked more times than it was allowed",
        )
        # And having given up, it does not start the whole thing again next pass.
        before = len(self.observer.asked)
        for _ in range(4):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(len(self.observer.asked), before, "it started looking all over again")

    def test_a_deadline_ends_the_looking_even_with_reads_queued(self) -> None:
        """A queued read must never outlive the loop that wanted it."""
        service = self.looking_service([["slow"]], self.finding())
        self.open_incident()
        service.tick()
        self.clock.advance(minutes=91)
        service.tick()
        left = self.service.observations.waiting()
        self.assertEqual(left, [], "a read outlived its deadline")

    def test_a_finding_clears_the_loop(self) -> None:
        service = self.looking_service([["x"]], self.finding())
        self.open_incident()
        service.tick()
        service.tick()
        self.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"], "the finding did not land")
        self.assertIsNone(
            self.service.observations.open(INCIDENT_KEY, 1), "the loop was left open"
        )

    def test_a_failed_read_is_evidence_and_does_not_stop_the_round(self) -> None:
        class Failing:
            def __init__(self):
                self.asked = []

            def observe(self, command, subject=None):
                self.asked.append(command)

                class Result:
                    def text(self_inner):
                        return "(this read did not run: observe ActorError)"

                return Result()

        service = self.looking_service([["x"]], self.finding())
        self.service.observer = Failing()
        self.open_incident()
        service.tick()
        service.tick()
        self.clock.advance(minutes=6)
        service.tick()
        second = self.diagnoser.requests[-1]
        self.assertIn("did not run", second.prompt())

    def test_with_no_observer_it_is_never_offered_a_read(self) -> None:
        service = self.looking_service([["x"]], self.finding())
        self.service.observer = None
        self.open_incident()
        service.tick()
        request = self.diagnoser.requests[0]
        self.assertFalse(request.observation_available)
        self.assertNotIn("reads_requested", request.prompt())
        # And a request made anyway is refused rather than looped on.
        self.assertEqual(self.stages(), ["awaiting_backup"], "the rule did not answer")

    def test_a_fault_that_recovers_stops_being_looked_at(self) -> None:
        """Reads outlived the reason for them, and the loop outlived the fault.

        The ask is only reached while the fault can still get a proposal, and the read
        phase only sees loops with something queued -- so a fault that recovers between
        the two was seen by neither: its remaining reads ran anyway, and the loop stayed
        open for ever, ready to hand its hours-old frozen question to the next blockage
        in the same episode.
        """
        service = self.looking_service([["a", "b", "c"]], self.finding())
        self.open_incident()
        service.tick()
        self.assertEqual(self.observer.asked, ["a"])
        self.state_db.execute(
            "UPDATE incidents SET status='resolved' WHERE dedup_key=?", (INCIDENT_KEY,)
        )
        self.state_db.commit()
        for _ in range(3):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(self.observer.asked, ["a"], "it kept reading for a fault that was over")
        self.assertIsNone(
            self.service.observations.open(INCIDENT_KEY, 1), "the loop outlived the fault"
        )

    def test_the_deadline_ends_the_investigation_rather_than_starting_the_next(self) -> None:
        """The sweep once deleted the loop in the very pass that concluded it.

        Reaching the deadline puts a loop into its final ask, which is answered on a
        later pass. A sweep measured from when the loop STARTED matches that loop too,
        so it was deleted before the concluding answer could be collected -- and the
        next pass opened a fresh loop with six new rounds. The deadline did not end the
        investigation; it started the next one.
        """
        service = self.looking_service([["x"]] * 20, None)
        self.open_incident()
        service.tick()
        loop = self.service.observations.open(INCIDENT_KEY, 1)
        self.clock.advance(minutes=91)
        self.service.observations.conclude(loop["loop_id"], self.clock())
        self.service._observe()
        surviving = self.service.observations.open(INCIDENT_KEY, 1)
        self.assertIsNotNone(surviving, "the sweep deleted a loop that was concluding")
        self.assertEqual(str(surviving["state"]), "final")
        self.assertEqual(str(surviving["loop_id"]), str(loop["loop_id"]), "a new loop began")

    def test_the_question_does_not_move_when_a_cycle_finishes_under_it(self) -> None:
        """`attempts` was the one thing still read from live state, and it feeds both
        the subject hash and the investigation id.

        A cycle created before the loop finishing mid-loop moved it, which would lose
        the loop's collected answer to a new episode and hand it a fresh budget in the
        same moment.
        """
        service = self.looking_service([["x"], ["y"]], self.finding())
        self.open_incident()
        service.tick()
        before = self.diagnoser.requests[0]
        # A restart approved earlier finishes while the loop is between rounds.
        self.service.cycles.create(Cycle(
            cycle_id="c-earlier", bdf=BDF, incident_key=INCIDENT_KEY, episode=1,
            stage="done", evidence_revision="", evidence_ref="",
            trigger_utc=_text(self.clock()), retrigger_utc=_text(self.clock()),
            backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
            created_utc=_text(self.clock()),
        ))
        self.service.cycles.update("c-earlier", self.clock(), result="succeeded")
        self.clock.advance(minutes=6)
        service.tick()
        after = self.diagnoser.requests[-1]
        self.assertEqual(after.attempts, before.attempts, "the frozen question moved")
        self.assertEqual(
            after.investigation_id, before.investigation_id,
            "the loop handed itself a fresh budget mid-investigation",
        )

    def test_losing_the_way_to_look_is_answered_not_waited_out(self) -> None:
        """Reads queued, then no observer: the fault must not sit for ninety minutes."""
        service = self.looking_service([["x", "y"]], self.finding())
        self.open_incident()
        service.tick()  # runs 'x'; 'y' is still queued
        self.assertTrue(self.service.observations.waiting())
        self.service.observer = None
        service.tick()
        self.assertEqual(self.service.observations.waiting(), [], "the reads were left queued")
        loop = self.service.observations.open(INCIDENT_KEY, 1)
        self.assertEqual(str(loop["state"]), "final", "it was left waiting on a look it cannot do")

    def test_a_loop_nothing_returns_to_is_still_ended(self) -> None:
        """Neither phase would ever look at it again, so nothing would close it."""
        service = self.looking_service([["x"]], self.finding())
        self.open_incident()
        service.tick()
        # The fault stops being eligible without recovering: no ask, nothing queued.
        while self.service.observations.waiting():
            service.tick()
        self.service.controls.set("paused", "telegram:1", 1, self.clock())
        self.clock.advance(minutes=95)
        service.tick()
        self.assertIsNone(
            self.service.observations.open(INCIDENT_KEY, 1),
            "a loop outlived its deadline because nothing came back to it",
        )

    def test_a_person_is_heard_even_when_nothing_is_wrong(self) -> None:
        """A person is the one trigger that does not depend on our detection working.

        Requiring an open incident meant they could only be heard about faults this
        service had already found for itself -- and the whole reason a person is the
        most reliable trigger is that they notice what we did not.
        """
        service = self.talking_service("Nothing looks wrong to me either.")
        self.ask("is the machine alright?")   # no incident open at all
        service.tick()
        self.assertTrue(self.conversation.asked, "the operator was not heard")
        _message, _sender, bdf, subject, _briefing, investigation = self.conversation.asked[0]
        self.assertEqual(bdf, "")
        self.assertTrue(subject, "a conversation with no subject opens an empty episode")
        self.assertIn("machine:17049", investigation)
        self.assertIn("Nothing looks wrong to me either.", self.texts())

    def test_asking_it_to_look_starts_a_real_investigation(self) -> None:
        """Not an answer from evidence already gathered -- a look, with a finding."""
        service = self.talking_service(
            Reply("I will take a look now.", Steer("investigate", ""))
        )
        self.diagnoser = self.Asking([], self.finding())
        self.service.diagnoser = self.diagnoser
        self.ask("something feels off, can you check the machine")
        service.tick()  # heard, looked and answered, in one pass
        self.assertTrue(self.diagnoser.requests, "nothing was investigated")
        request = self.diagnoser.requests[0]
        self.assertTrue(request.requested, "it was framed as an incident nobody reported")
        self.assertIn("asked you to look the machine over", request.prompt())
        self.assertIn("I found nothing", request.prompt())
        said = self.texts()
        self.assertIn("You asked me to look the machine over", said)
        self.assertIn("the exporter holds the GPU", said)
        # It reports; it does not act. Nothing was proposed and nothing is waiting.
        self.assertEqual(self.cycle_rows(), [], "a requested look created a request")
        self.assertIsNone(self.service.controls.get("review-requested"), "it never ended")

    def test_each_requested_look_is_its_own_investigation(self) -> None:
        """Keyed on a constant, every review this machine is ever asked for shared one
        budget -- and that budget is a lifetime count with no window.

        The second or third review would have exhausted it and every one after that
        would have been refused for good, with nothing an operator could do about it.
        """
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([], self.finding())
        self.service.diagnoser = self.diagnoser
        seen = []
        for number in range(3):
            self.ask(f"have a look #{number}")
            self.clock.advance(minutes=6)
            service.tick()
            seen.append(self.diagnoser.requests[-1].investigation_id)
        self.assertEqual(len(set(seen)), 3, "every review spent the same budget")

    def test_a_spent_allowance_is_not_reported_as_finding_nothing(self) -> None:
        """Nothing here can fall back to the rule, so the words are all there is."""
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.service.diagnoser = _FixedDiagnoser(
            Diagnosis(None, "model", reason="investigation-turn-cap")
        )
        self.ask("check it over")
        service.tick()
        said = self.texts()
        self.assertIn("allowance for this is spent", said)
        self.assertNotIn("reached no conclusion", said)

    def test_what_it_last_concluded_says_what_it_was_about(self) -> None:
        """A look at the whole machine is recorded like a fault's diagnosis is."""
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([], None)
        self.service.diagnoser = self.diagnoser
        self.ask("check it over")
        service.tick()
        self.assertIn("because you asked me to look", self.service._last_diagnosis_text())

    def test_a_requested_look_that_concludes_nothing_still_comes_back(self) -> None:
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([], None)
        self.service.diagnoser = self.diagnoser
        self.ask("check it over please")
        service.tick()
        self.assertIn("reached no conclusion", self.texts())
        self.assertIsNone(self.service.controls.get("review-requested"))

    def test_a_requested_look_may_look_at_the_host_too(self) -> None:
        """The same loop, so a requested look can go and read like any other."""
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([["dmesg | tail"]], self.finding())
        self.service.diagnoser = self.diagnoser
        self.service.observer = self.Observer({"dmesg | tail": "NVRM Xid 79"})
        self.ask("check it over please")
        service.tick()
        self.assertEqual(self.service.observer.asked, ["dmesg | tail"])
        self.clock.advance(minutes=6)
        service.tick()
        self.assertIn("NVRM Xid 79", self.diagnoser.requests[-1].prompt())
        self.assertIn("You asked me to look the machine over", self.texts())

    def test_asking_again_gets_a_look_that_actually_looks(self) -> None:
        """A finished loop must not stand as the current one.

        Only 'closed' counted as over, so a loop that gave up stayed standing as the
        open one -- and the next look reused its exhausted rounds and answered without
        looking at anything. To a person who had just asked twice, that reads as being
        ignored.
        """
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([["x"]] * 20, None)
        self.service.diagnoser = self.diagnoser
        self.service.observer = self.Observer()
        self.ask("check it")
        for _ in range(MAX_OBSERVE_ROUNDS * 4):
            self.clock.advance(minutes=6)
            service.tick()
        first = len(self.service.observer.asked)
        self.assertEqual(first, MAX_OBSERVE_ROUNDS)
        self.ask("no really, look again")
        for _ in range(MAX_OBSERVE_ROUNDS * 3):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(
            len(self.service.observer.asked) - first, MAX_OBSERVE_ROUNDS,
            "asking again got an answer from a loop that had already given up",
        )

    def test_asking_again_while_it_is_looking_does_not_start_a_second_look(self) -> None:
        """A review's identity is the moment it was asked for.

        Overwriting that mid-look orphaned the one already running: it kept its rounds
        and kept taking its turn at the reads, with nothing left that would ever
        conclude it.
        """
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.diagnoser = self.Asking([["a", "b", "c"]] * 4, self.finding())
        self.service.diagnoser = self.diagnoser
        self.service.observer = self.Observer()
        self.ask("look at it")
        service.tick()
        self.clock.advance(minutes=6)
        self.ask("any news?")
        service.tick()
        self.clock.advance(minutes=6)
        service.tick()
        loops = self.service.observations._query(
            "SELECT state FROM tc_action_observe_loops WHERE incident_key=?", (REVIEW_KEY,)
        ).fetchall()
        self.assertEqual(len(loops), 1, "asking again started a second look")
        self.assertIn("already looking", self.texts())

    def test_many_faults_being_looked_at_all_make_progress(self) -> None:
        """One read per PASS made nine loops each progress nine times slower, while
        every loop's deadline stayed the same ninety minutes."""
        self.looking_service([["x"]], self.finding())
        observations = self.service.observations
        for number in range(9):
            observations.start({
                "loop_id": f"L{number}", "incident_key": f"inc{number}", "episode": 1,
                "bdf": BDF, "now": _text(self.clock()), "severity": "error",
                "evidence_revision": "r", "reads_available": "[]", "reads_text": "",
                "status_json": "{}", "facts_json": "{}", "vast_text": "",
                "vast_reports": 0, "attempts": 0, "code": "gpu_vfio_handover_blocked", "observed_utc": _text(self.clock()),
            })
            observations.ask(f"L{number}", 1, (f"read-{number}",), "", self.clock())
            self.state_db.execute(
                "INSERT INTO incidents(dedup_key,source,fault_family,stable_signature,status)"
                " VALUES(?,'ssh','gpu','sig','open')", (f"inc{number}",))
        self.state_db.commit()
        self.service._observe()
        self.assertEqual(
            len(self.service.observer.asked), 9, "only some of the faults made progress"
        )

    def test_a_conclusion_is_not_lost_because_its_copy_could_not_be_kept(self) -> None:
        """The loop is already closed and the rounds already spent by this point.

        Letting the write throw discarded the diagnosis and started the whole loop
        again on the next pass -- for as long as whatever is wrong with the store
        lasts, which on a full disk is exactly when the diagnosis matters most.
        """
        service = self.diagnosing_service(Diagnosis(self.finding(), "model"))
        self.open_incident()
        original = self.service.evidence.record

        def refuse(kind, subject, document):
            if kind == "diagnosis":
                raise sqlite3.OperationalError("database or disk is full")
            return original(kind, subject, document)

        self.service.evidence.record = refuse
        service.tick()
        self.assertEqual(self.stages(), ["awaiting_backup"], "the finding was thrown away")

    def test_every_view_of_a_loop_agrees_on_whether_it_is_still_live(self) -> None:
        """`open`, `waiting`, `abandoned` and the unique index must not drift apart.

        Two defects in this loop's life were one of them disagreeing with the others.
        Nothing about writing the predicate out five times made the coupling visible,
        so this checks the agreement rather than any one of them.
        """
        observations = self.service.observations
        for state in LOOP_STATES:
            observations.start({
                "loop_id": state, "incident_key": f"inc-{state}", "episode": 1, "bdf": BDF,
                "now": _text(self.clock()), "severity": "error", "evidence_revision": "r",
                "reads_available": "[]", "reads_text": "", "status_json": "{}",
                "facts_json": "{}", "vast_text": "", "vast_reports": 0, "attempts": 0, "code": "gpu_vfio_handover_blocked",
                "observed_utc": _text(self.clock()),
            })
            observations.ask(state, 1, ("read",), "", self.clock())
            if state in ("spent", "closed"):
                observations.close(state, state)
            elif state == "final":
                observations.conclude(state, self.clock())
        live = set(LIVE_STATES)
        for state in LOOP_STATES:
            self.assertEqual(
                observations.open(f"inc-{state}", 1) is not None, state in live,
                f"`open` disagrees about a {state} loop",
            )
        self.assertEqual(
            {loop for loop, *_rest in observations.waiting()}, live,
            "`waiting` disagrees about which loops are live",
        )
        self.clock.advance(minutes=91)
        self.assertEqual(
            {loop for loop, *_rest in observations.abandoned(self.clock() - OBSERVE_LOOP_DEADLINE)},
            live,
            "`abandoned` disagrees about which loops are live",
        )
        # And a live loop for the same fault cannot be opened twice.
        with self.assertRaises(sqlite3.IntegrityError):
            observations.start({
                "loop_id": "second", "incident_key": "inc-open", "episode": 1, "bdf": BDF,
                "now": _text(self.clock()), "severity": "error", "evidence_revision": "r",
                "reads_available": "[]", "reads_text": "", "status_json": "{}",
                "facts_json": "{}", "vast_text": "", "vast_reports": 0, "attempts": 0, "code": "gpu_vfio_handover_blocked",
                "observed_utc": _text(self.clock()),
            })

    def test_a_loop_being_worked_on_is_never_reaped(self) -> None:
        """The reaper and the deadline read two different clocks on purpose.

        The deadline runs from when the question was frozen, because a loop must not
        outlive the machine state it froze. The reaper runs from when anything last
        happened, because its job is finding loops nothing will return to -- and a loop
        slowly draining reads is being returned to. `answer()` bumping `updated_utc` is
        the whole of what keeps those apart, and nothing asserted it: without it, the
        reaper deletes a loop mid-round, which is the first defect this loop ever had,
        pointed the other way.
        """
        observations = self.service.observations
        for state in LIVE_STATES:
            loop_id = f"busy-{state}"
            observations.start({
                "loop_id": loop_id, "incident_key": f"inc-{state}", "episode": 1,
                "bdf": BDF, "now": _text(self.clock()), "severity": "error",
                "evidence_revision": "r", "reads_available": "[]", "reads_text": "",
                "status_json": "{}", "facts_json": "{}", "vast_text": "",
                "vast_reports": 0, "attempts": 0, "code": "gpu_vfio_handover_blocked", "observed_utc": _text(self.clock()),
            })
            observations.ask(loop_id, 1, tuple(f"r{n}" for n in range(6)), "", self.clock())
            if state == "final":
                observations.conclude(loop_id, self.clock())
            for number in range(6):
                # Long enough between reads that a reaper watching the wrong clock
                # would have taken it, several times over.
                self.clock.advance(seconds=int(OBSERVE_LOOP_DEADLINE.total_seconds() * 0.75))
                self.assertNotIn(
                    loop_id,
                    {loop for loop, *_rest in observations.abandoned(
                        self.clock() - OBSERVE_LOOP_DEADLINE)},
                    f"a {state} loop was reaped while it was still being worked on",
                )
                observations.answer(loop_id, 1, number + 1, "out", self.clock())
            # And once nothing comes back to it, it is reaped as it should be.
            self.clock.advance(seconds=int(OBSERVE_LOOP_DEADLINE.total_seconds() * 1.5))
            self.assertIn(
                loop_id,
                {loop for loop, *_rest in observations.abandoned(
                    self.clock() - OBSERVE_LOOP_DEADLINE)},
                f"a {state} loop nothing returns to was left standing",
            )

    def test_the_cooldown_runs_from_when_it_gave_up(self) -> None:
        """`spent_since` reads `updated_utc` on a loop that has given up.

        Without `close()` stamping it, the six hours ran from the last thing that
        happened BEFORE it gave up -- short by however long the final ask took.
        """
        observations = self.service.observations
        observations.start({
            "loop_id": "gave-up", "incident_key": "inc-x", "episode": 1, "bdf": BDF,
            "now": _text(self.clock()), "severity": "error", "evidence_revision": "r",
            "reads_available": "[]", "reads_text": "", "status_json": "{}",
            "facts_json": "{}", "vast_text": "", "vast_reports": 0, "attempts": 0, "code": "gpu_vfio_handover_blocked",
            "observed_utc": _text(self.clock()),
        })
        self.clock.advance(minutes=40)   # the final ask takes its time
        observations.close("gave-up", "spent", self.clock())
        self.clock.advance(hours=5, minutes=50)
        self.assertTrue(
            observations.spent_since("inc-x", 1, self.clock() - timedelta(hours=6)),
            "the cooldown ran from before it gave up, so it expired early",
        )

    def test_a_changed_live_predicate_rebuilds_the_index_it_was_baked_into(self) -> None:
        """A partial index stores its predicate, and IF NOT EXISTS then ignores edits.

        The constraint would go on enforcing the old definition with nothing to say
        about it, which is how two live loops on one fault arrive.
        """
        database = sqlite3.connect(":memory:")
        Observations(database)
        database.execute("DROP INDEX tc_action_observe_live")
        database.execute(
            "CREATE UNIQUE INDEX tc_action_observe_live"
            " ON tc_action_observe_loops(incident_key, episode) WHERE state<>'closed'"
        )
        database.execute("UPDATE tc_action_observe_schema SET value='stale'")
        database.commit()
        Observations(database)   # a fresh process over the same database
        baked = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='tc_action_observe_live'"
        ).fetchone()[0]
        self.assertIn("state IN ('open', 'final')", baked, "the old predicate survived")

    def test_a_blip_is_said_once_and_an_outage_says_it_is_still_going(self) -> None:
        """A blip and an outage look identical one line at a time.

        Polling Telegram fails on one or two per cent of its long polls and loses
        nothing when it does -- the cursor only advances once an input is stored -- but
        at a tick every fifteen seconds that is scores of identical lines a day, and a
        real outage looks exactly like the noise it is buried in.
        """
        def failing():
            raise TelegramError("getUpdates failed")

        failing.__name__ = "_poll"
        self.service._guard(failing)
        self.assertEqual(len(self.reports), 1, "the first failure was not reported")
        self.assertIn('"status":"failed"', self.reports[0])

        for _ in range(8):   # it keeps failing, within the reminder window
            self.clock.advance(seconds=15)
            self.service._guard(failing)
        self.assertEqual(len(self.reports), 1, "it repeated itself while nothing changed")

        self.clock.advance(minutes=16)
        self.service._guard(failing)
        self.assertEqual(len(self.reports), 2, "an outage went quiet instead of saying so")
        self.assertIn('"status":"failing"', self.reports[1])
        self.assertIn('"count":10', self.reports[1])

        def recovered():
            return None

        recovered.__name__ = "_poll"
        self.service._guard(recovered)
        self.assertEqual(len(self.reports), 3, "recovery was not worth a line")
        self.assertIn('"status":"recovered"', self.reports[2])
        self.assertIn('"count":10', self.reports[2])

    def test_a_different_failure_in_the_same_phase_is_news(self) -> None:
        def broken(error):
            def phase():
                raise error
            phase.__name__ = "_poll"
            return phase

        self.service._guard(broken(TelegramError("transport")))
        self.service._guard(broken(ValueError("something else entirely")))
        self.assertEqual(len(self.reports), 2, "a new kind of failure was swallowed")
        self.assertIn("ValueError", self.reports[1])

    def monitoring_finding(self, container):
        return parse_finding(json.dumps({
            "summary": f"{container} stopped reporting",
            "mechanism": "it is up but scraping nothing",
            "evidence": ["target-read@containers"],
            "action": {"command": f"docker restart {container}",
                       "intent": "make it scrape again"},
            "expected_effect": "metrics resume", "confidence": "high",
        }))

    class Actor:
        """Records what it was asked to carry out, and whether it worked."""

        def __init__(self, ok=True, detail="restarted"):
            self.done = []
            self.ok = ok
            self.detail = detail

        def run(self, command, subject=None, *, approved=False):
            # What it was asked to do is the command itself now; tests that assert on
            # a container name read it back out of the command they wrote.
            self.done.append(command.rsplit(" ", 1)[-1])
            from terracompute_ops.acting import Carried
            return Carried(command, command, self.ok, self.detail)

    def test_the_monitoring_that_is_ours_is_dealt_with_without_asking(self) -> None:
        """Seven containers are the agent's own work and it could act on one.

        A finding about any of the others was validated, found to be something the
        adapter could not do, and handed to a person who would then have typed the
        restart themselves.
        """
        actor = self.Actor()
        self.service.actor = actor
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("node-exporter"), "model")
        )
        self.open_incident()
        service.tick()
        self.assertEqual(actor.done, ["node-exporter"])
        self.assertIn("docker restart node-exporter", self.texts())
        self.assertIn("did not need your approval", self.texts())
        self.assertEqual(self.cycle_rows(), [], "it opened a request for its own work")

    def test_the_one_with_the_blast_radius_still_goes_through_the_button(self) -> None:
        """dcgm-exporter keeps its evidence backup and its approval: acting on it
        perturbs the very GPU state being diagnosed."""
        actor = self.Actor()
        self.service.actor = actor
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("dcgm-exporter"), "model")
        )
        self.open_incident()
        service.tick()
        self.assertEqual(actor.done, [], "it took the one action that needs a person")
        self.assertEqual(self.stages(), ["awaiting_backup"])

    def test_a_tenant_container_never_reaches_the_actor(self) -> None:
        """It is reported rather than raised, and it is still never carried out.

        Throwing the whole finding away over its last field lost the reasoning too.
        The model may have good grounds for wanting to look inside a rental; it does
        not get to, and a person should read why it asked.
        """
        actor = self.Actor()
        self.service.actor = actor
        finding = self.monitoring_finding("C.51217040")
        self.assertIsNone(finding.action, "a tenant's container reached the actor")
        self.assertIn("customer", finding.unsupported_request)
        self.assertIn("stopped reporting", finding.summary, "the reasoning was discarded")
        self.assertEqual(actor.done, [])

    def test_it_does_not_restart_the_same_thing_in_a_loop(self) -> None:
        actor = self.Actor()
        self.service.actor = actor
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("cadvisor"), "model")
        )
        self.open_incident()
        service.tick()
        for _ in range(4):
            self.clock.advance(minutes=6)
            self.service.schedule.clear("status")
            service.tick()
        self.assertEqual(actor.done, ["cadvisor"], "it restarted it again while waiting")

    def test_a_failure_to_carry_it_out_is_reported_with_what_went_wrong(self) -> None:
        actor = self.Actor(ok=False, detail="docker exited 1: No such container")
        self.service.actor = actor
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("vast-grafana-1"), "model")
        )
        self.open_incident()
        service.tick()
        said = self.texts()
        self.assertIn("docker exited 1", said)
        self.assertIn("No such container", said)
        # The command it actually ran, so a failure can be read without guessing.
        self.assertIn("docker restart vast-grafana-1", said)

    def test_paused_means_it_says_so_rather_than_acting(self) -> None:
        actor = self.Actor()
        self.service.actor = actor
        self.service.controls.set("paused", "telegram:1", 1, self.clock())
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("node-exporter"), "model")
        )
        self.open_incident()
        service.tick()
        self.assertEqual(actor.done, [])

    def open_other_incident(
        self, key, family="bmc", severity="error", episode=1,
        source=None, signature=None,
    ):
        self.state_db.execute(
            """INSERT INTO incidents(dedup_key,source,fault_family,stable_signature,
                 status,notification_episode,severity,first_occurrence_utc,
                 last_occurrence_utc) VALUES(?,?,?,?,'open',?,?,?,?)""",
            (key, source or family, family, signature or f"sig-{key}", episode, severity,
             "2026-09-17T00:00:00Z", "2026-09-17T01:00:00Z"),
        )
        self.state_db.commit()

    def test_a_fault_nobody_was_looking_at_is_looked_at(self) -> None:
        """Forty open incidents on this machine and the investigator saw two.

        Eighteen BMC faults, nine GPU Xid errors, eight capacity, three probe -- all
        raised, notified, and then seen by nobody who could work out what they meant.
        """
        service = self.diagnosing_service(Diagnosis(self.finding(), "model"))
        self.open_other_incident("bmc-psu-redundancy-lost", severity="critical")
        service.tick()
        self.assertTrue(self.diagnoser.requests, "it was still not looked at")
        request = self.diagnoser.requests[0]
        self.assertEqual(request.incident_key, "bmc-psu-redundancy-lost")
        self.assertIsNone(request.bdf, "a BMC fault is not about a GPU")
        self.assertIn("bmc", request.code)
        self.assertIn("nobody had looked at it", self.texts())

    def test_a_requested_non_handover_ssh_gpu_incident_gets_one_fresh_look(self) -> None:
        """SSH/GPU is not synonymous with the recognized handover signature.

        The generic driver excluded the whole class while the handover driver selected
        only its own signatures. After the first acknowledgement there was therefore
        nobody to take up ``review:<incident-key>``; if reached, that driver also left
        the review standing and would have repeated it until expiry.
        """
        key = "54c708b83c880d7e31fd307812354b43d53df991515b81e1645e50d722ccafe8"
        service = self.diagnosing_service(Diagnosis(None, "model", reason="no answer"))
        self.open_other_incident(
            key, family="gpu", severity="critical", source="ssh",
            signature="gpu:xid:79:0000:c1:00.0",
        )
        service.tick()  # Its ordinary first look, before the operator asks again.
        first = len(self.diagnoser.requests)
        self.assertEqual(first, 1, "the non-handover SSH/GPU fault had no driver")

        self.service._steer("look-again", key, 4242)
        self.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(len(self.diagnoser.requests), first + 1)
        self.assertTrue(self.diagnoser.requests[-1].requested)
        self.assertIsNone(self.service.controls.get(f"review:{key}"))
        self.assertIn(f"looked at {key} again and reached no conclusion", self.texts())

        self.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(
            len(self.diagnoser.requests), first + 1,
            "the completed review started another investigation",
        )

    def test_a_requested_incident_review_does_not_expire_while_it_is_under_way(self) -> None:
        key = "slow-gpu-fault"
        self.open_other_incident(key, family="gpu", source="ssh", severity="critical")
        self.service._steer("look-again", key, 4242)
        service = self.looking_service([["a"], ["b"]], None)
        service.tick()
        self.assertIsNotNone(self.service.controls.get(f"review:{key}"))

        self.clock.advance(minutes=61)
        service.tick()
        self.assertIsNotNone(
            self.service.controls.get(f"review:{key}"),
            "the pickup lifetime cancelled a look already in progress",
        )
        self.clock.advance(minutes=6)
        service.tick()
        self.assertIsNone(self.service.controls.get(f"review:{key}"))
        self.assertIn(f"looked at {key} again and reached no conclusion", self.texts())
        self.assertNotIn("lapsed before I could take it up", self.texts())

    def test_a_requested_incident_review_that_never_starts_lapses_out_loud(self) -> None:
        key = "missing-fault"
        self.service._steer("look-again", key, 4242)
        self.clock.advance(seconds=OVERRIDE_LIFETIME.total_seconds() + 1)
        self.service.tick()
        self.assertIsNone(self.service.controls.get(f"review:{key}"))
        self.assertIn("lapsed before I could take it up", self.texts())

    def test_the_worst_one_goes_first(self) -> None:
        service = self.diagnosing_service(Diagnosis(self.finding(), "model"))
        self.open_other_incident("a-warning", severity="warning")
        self.open_other_incident("b-critical", severity="critical")
        service.tick()
        self.assertEqual(self.diagnoser.requests[0].incident_key, "b-critical")

    def test_only_one_fault_is_investigated_at_a_time(self) -> None:
        """One budget, dozens of open faults: all at once spends the day before any
        of them finishes."""
        service = self.looking_service([["x"], ["y"]], self.finding())
        for number in range(5):
            self.open_other_incident(f"fault-{number}")
        service.tick()
        self.clock.advance(minutes=6)
        service.tick()
        started = {request.incident_key for request in self.diagnoser.requests}
        self.assertEqual(len(started), 1, f"it began looking at {len(started)} at once")

    def test_the_one_under_way_is_carried_on_with(self) -> None:
        """Refusing to act while anything was live also stopped it returning to the
        loop it had just opened, so the first question was asked and never followed
        up and the loop sat at nought rounds until the reaper took it."""
        service = self.looking_service([["a"], ["b"]], self.finding())
        self.open_other_incident("lonely-fault")
        service.tick()
        loop = self.service.observations.open("lonely-fault", 1)
        self.assertIsNotNone(loop, "it never started")
        for _ in range(8):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertGreater(
            len(self.diagnoser.requests), 1, "it asked once and never came back to it"
        )
        self.assertTrue(self.service.observer.asked, "it never ran the reads it asked for")

    def test_a_fault_that_had_its_look_is_not_looked_at_again(self) -> None:
        service = self.diagnosing_service(Diagnosis(self.finding(), "model"))
        self.open_other_incident("settled")
        service.tick()
        first = len(self.diagnoser.requests)
        for _ in range(3):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(len(self.diagnoser.requests), first, "it kept re-diagnosing it")

    def test_monitoring_work_found_on_another_fault_is_still_carried_out(self) -> None:
        actor = self.Actor()
        self.service.actor = actor
        service = self.diagnosing_service(
            Diagnosis(self.monitoring_finding("node-exporter"), "model")
        )
        self.open_other_incident("xid-79-gpu3", family="xid", severity="critical")
        service.tick()
        self.assertEqual(actor.done, ["node-exporter"])

    def test_a_database_from_before_a_column_existed_gains_it(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists.

        The first deploy after a new column met a live database without it, and every
        insert failed -- an OperationalError caught by the phase guard and logged as a
        category with no hint of which column was missing.
        """
        database = sqlite3.connect(":memory:")
        Observations(database)
        for name, _definition in Observations._ADDED_COLUMNS:
            database.execute(f"ALTER TABLE tc_action_observe_loops DROP COLUMN {name}")
        database.commit()
        Observations(database)   # a fresh process over the same database
        have = {row[1] for row in database.execute("PRAGMA table_info(tc_action_observe_loops)")}
        for name, _definition in Observations._ADDED_COLUMNS:
            self.assertIn(name, have, f"{name} was not restored")
        # And it can still be written to.
        Observations(database).start({
            "loop_id": "after", "incident_key": "inc", "episode": 1, "bdf": BDF,
            "now": _text(self.clock()), "severity": "error", "evidence_revision": "r",
            "reads_available": "[]", "reads_text": "", "status_json": "{}",
            "facts_json": "{}", "vast_text": "", "vast_reports": 0, "attempts": 0,
            "code": "gpu_vfio_handover_blocked", "observed_utc": _text(self.clock()),
        })

    def test_it_says_when_it_starts_looking_and_only_once(self) -> None:
        """A loop may run ninety minutes. Saying nothing for that long makes an agent
        that is working look exactly like one that has died, and from the outside
        there is no way to tell those apart."""
        service = self.looking_service([["a"], ["b"]], self.finding())
        self.open_incident()
        service.tick()
        said = self.texts()
        self.assertIn("I am looking into", said)
        self.assertIn("1 reads on the host", said)
        self.assertIn("because", said)   # the model's own note for asking
        # A second round is not a second announcement.
        for _ in range(6):
            self.clock.advance(minutes=6)
            service.tick()
        self.assertEqual(self.texts().count("I am looking into"), 1, "it repeated itself")

    def test_pausing_stops_it_acting_and_not_watching(self) -> None:
        """Pause says "keep watching and reporting, act on nothing".

        A look has no path to an action, so refusing one while paused left the request
        sitting silently until somebody resumed, with nothing said about why.
        """
        service = self.talking_service(Reply("Looking.", Steer("investigate", "")))
        self.service.diagnoser = self.Asking([], self.finding())
        self.service.controls.set("paused", "telegram:1", 1, self.clock())
        self.ask("check it over")
        service.tick()
        self.assertIn("You asked me to look the machine over", self.texts())

    def test_being_told_to_get_on_with_it_between_rounds_still_ends_it(self) -> None:
        """Between rounds nothing is queued, and requiring a queued read meant a person
        saying so at that moment was simply ignored."""
        service = self.looking_service([["a"], ["b"]], self.finding())
        self.open_incident()
        service.tick()
        while self.service.observations.waiting():   # drain the round; now between rounds
            service.tick()
        self.assertIsNone(self.service.observations.queued(
            str(self.service.observations.open(INCIDENT_KEY, 1)["loop_id"])
        ))
        self.service.controls.set(
            f"override:{BDF}", f"telegram:7@{_text(self.clock())}", 7, self.clock()
        )
        self.clock.advance(minutes=6)
        service.tick()
        loop = self.service.observations.open(INCIDENT_KEY, 1)
        self.assertTrue(
            loop is None or str(loop["state"]) == "final",
            "it carried on looking after being told to get on with it",
        )

    def test_one_fault_looking_hard_cannot_starve_another(self) -> None:
        """Reads are served oldest-asked first, not oldest-loop first.

        By loop, a fault that keeps asking for more rounds would hold another fault's
        first read behind every round it ever asks for.
        """
        observations = self.service.observations
        for name, minutes in (("busy", 0), ("other", 10)):
            observations.start({
                "loop_id": name, "incident_key": f"inc-{name}", "episode": 1, "bdf": BDF,
                "now": _text(self.clock() + timedelta(minutes=minutes)), "severity": "error",
                "evidence_revision": "r", "reads_available": "[]", "reads_text": "",
                "status_json": "{}", "facts_json": "{}", "vast_text": "", "vast_reports": 0,
                "attempts": 0, "code": "gpu_vfio_handover_blocked", "observed_utc": _text(self.clock()),
            })
        observations.ask("busy", 1, ("a", "b"), "", self.clock())
        observations.ask("other", 1, ("waiting",), "", self.clock() + timedelta(minutes=10))
        order = []
        now = self.clock() + timedelta(minutes=10)
        for _ in range(3):
            now += timedelta(seconds=30)
            loop_id, _key, _episode, _bdf, round, seq, command = observations.waiting()[0]
            order.append(command)
            observations.answer(loop_id, round, seq, "done", now)
            if command == "b":  # the busy loop immediately wants another round
                observations.ask("busy", 2, ("c",), "", now)
        self.assertEqual(order, ["a", "b", "waiting"], "a busy loop jumped the queue")

    def test_being_told_to_get_on_with_it_ends_the_looking(self) -> None:
        service = self.looking_service([["slow"], ["slower"]], self.finding())
        self.open_incident()
        service.tick()
        self.service.controls.set(
            f"override:{BDF}", f"telegram:7@{_text(self.clock())}", 7, self.clock()
        )
        self.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(
            self.service.observations.waiting(), [], "it kept looking after being told not to"
        )


if __name__ == "__main__":
    unittest.main()
