"""Deterministic approval loop for the blocked-handover monitoring restart.

The service never acts on its own authority. It proposes the one allowlisted
restart only while the controller holds an open ``gpu_vfio_handover_blocked``
incident and the target helper independently shows the same blocked GPU on a
verified target with the exporter running. A proposal is sent for approval only
after its evidence is preserved by a completed backup, and it is executed at most
once, through ActionBroker, after an exact approval from a verified member of the
configured Telegram group.

Authority state (proposals, approvals, nonces, locks, attempts, cycles) lives in a
private database that no other controller role can write. Evidence and an audit
copy of every approved outcome are also written to the shared state database,
which the backup role preserves. Every cycle outcome is told to the group, and the
message and audit copy are retried until they are delivered.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .actions import ActionBroker, ApprovalKind, EventAuthentication, HumanApprovalEvent
from .monitor_restart import (
    COMPONENT,
    HANDOVER_CLEARED,
    ActorError,
    EvidenceStore,
    MonitorRestartAdapter,
    _status_document,
    build_proposal,
    evidence_revision,
    handover_incident_signature,
    proposal_shape,
)
from .diagnosing import MODEL, Diagnoser, Diagnosis, DiagnosisRequest, RuleDiagnoser, describe
from .inspection import summarize
from .policy import REPEAT_COOLDOWN, ActionClass, PolicyDenied
from .telegram import InputKind

PROPOSAL_INTERVAL = timedelta(minutes=15)
MAX_PROPOSAL_INTERVAL = timedelta(hours=4)
MAX_UNEXECUTED_CYCLES = 8
MAX_RESTARTS_PER_EPISODE = 3
# How often the controller may act on its own authority in a day before it starts
# asking again instead.
SELF_SERVICE_DAILY_CAP = 4
# An override that is never taken up lapses rather than arming the loop indefinitely.
OVERRIDE_LIFETIME = timedelta(hours=1)
STATUS_RETRY_INTERVAL = timedelta(minutes=5)
BACKUP_WAIT = timedelta(minutes=20)
BACKUP_RETRIGGER = timedelta(minutes=5)
TARGET_WAIT = timedelta(minutes=10)
TARGET_RETRY = timedelta(minutes=1)
# How often a request waiting for an answer re-reads the target.
REQUEST_RECHECK = timedelta(minutes=5)
EXECUTE_RETRY = timedelta(minutes=1)
RECONCILE_INTERVAL = timedelta(minutes=5)
UNKNOWN_REMINDER = timedelta(hours=6)
# How long the loop waits for an investigator that answers on its own schedule before
# falling back to the one rule it was taught by hand. Nothing blocks while it waits.
DIAGNOSIS_WAIT = timedelta(minutes=20)
# How often to say that the investigator is not answering. Falling back to the rule
# keeps the fault attended, but it must never be the only sign that the model path is
# broken: five unrelated faults in one evening all surfaced as an ordinary proposal.
INVESTIGATOR_SILENT_REMINDER = timedelta(hours=4)
# How many questions are answered per pass, and how long the evidence behind an answer
# is reused. Group chat must not crowd out the incident loop or the model's allowance.
MAX_QUESTIONS_PER_TICK = 2
QUESTION_CONTEXT_LIFETIME = timedelta(minutes=5)
DELIVERY_RETRY = timedelta(minutes=1)
MAX_DELIVERY_RETRY = timedelta(hours=1)
BACKUP_UNIT = "terracompute-backup.service"
# A restart ran (or may have run) for these results.
EXECUTED_RESULTS = frozenset({"succeeded", "failed", "postcondition-failed", "unknown"})
OPEN_ATTEMPT_STATES = ("reserved", "dispatching", "unknown")
_BDF_ARGUMENT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
EPISODE_CLOSED = " No further restart proposals for this incident until it recovers."
# A person said no. That answer holds for this incident episode.
REFUSED_BY_OPERATOR = "refused_by_operator"
# A person asked for a fresh look. Unlike a refusal this says nothing about the fault,
# so it neither closes the episode nor counts against its patience.
WITHDRAWN_BY_OPERATOR = "withdrawn_by_operator"
_UNIX_TIMESTAMP = re.compile(r"^@([0-9]{1,12})$")


def _text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _gpu_signatures() -> dict[str, str]:
    """Map every possible GPU function address to its handover incident signature."""
    addresses = (f"0000:{bus:02x}:{device:02x}.0" for bus in range(256) for device in range(32))
    return {handover_incident_signature(bdf): bdf for bdf in addresses}


class BackupProbe(Protocol):
    def trigger(self) -> None: ...

    def completed_after(self, since: datetime) -> str | None: ...


class SystemdBackupProbe:
    """Request the expedited backup and confirm a later successful run."""

    def __init__(
        self,
        *,
        trigger_file: Path,
        systemctl: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        if not Path(trigger_file).is_absolute() or not Path(systemctl).is_absolute():
            raise ValueError("backup trigger and systemctl paths must be absolute")
        self.trigger_file = Path(trigger_file)
        self.systemctl = systemctl
        self.runner = runner

    def trigger(self) -> None:
        # The backup path unit watches this file; the content is only a local marker.
        with open(self.trigger_file, "w", encoding="ascii") as handle:
            handle.write(f"monitor-restart {_text(datetime.now(timezone.utc))}\n")

    def completed_after(self, since: datetime) -> str | None:
        result = self.runner(
            [
                self.systemctl, "show", BACKUP_UNIT,
                "--property=ActiveState,Result,ExecMainStatus,"
                "ExecMainStartTimestamp,ExecMainExitTimestamp",
                "--timestamp=unix",
            ],
            capture_output=True, text=True, timeout=10, check=False,
            env={"PATH": "/run/current-system/sw/bin", "LC_ALL": "C"},
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            return None
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        start = _UNIX_TIMESTAMP.fullmatch(values.get("ExecMainStartTimestamp", ""))
        finish = _UNIX_TIMESTAMP.fullmatch(values.get("ExecMainExitTimestamp", ""))
        if (
            values.get("ActiveState") != "inactive"
            or values.get("Result") != "success"
            or values.get("ExecMainStatus") != "0"
            or start is None
            or finish is None
            or int(start.group(1)) <= int(since.timestamp())
            or int(finish.group(1)) < int(start.group(1))
        ):
            return None
        return f"{BACKUP_UNIT}@{start.group(1)}"


@dataclass(frozen=True)
class Cycle:
    cycle_id: str
    bdf: str
    incident_key: str
    episode: int
    stage: str
    evidence_revision: str
    evidence_ref: str
    trigger_utc: str
    retrigger_utc: str
    backup_ref: str | None
    proposal_id: str | None
    nonce: str | None
    digest: str | None
    shape: str | None
    created_utc: str
    result: str | None = None
    detail: str | None = None
    finished_utc: str | None = None
    notice: str | None = None
    audit_pending: int = 0
    execution_id: str | None = None
    # Who asked for this one to go before the usual waiting periods.
    override_by: str | None = None

    @property
    def ended(self) -> datetime:
        return _parse(self.finished_utc or self.created_utc)


class CycleStore:
    _COLUMNS = (
        "cycle_id,bdf,incident_key,episode,stage,evidence_revision,evidence_ref,trigger_utc,"
        "retrigger_utc,backup_ref,proposal_id,nonce,digest,shape,created_utc,result,"
        "detail,finished_utc,notice,audit_pending,execution_id,override_by"
    )
    _UPDATABLE = frozenset({
        "stage", "retrigger_utc", "backup_ref", "proposal_id", "nonce", "digest",
        "shape", "message_id", "result", "detail", "finished_utc", "notice",
        "audit_pending", "execution_id", "override_by",
    })

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_cycles (
                 cycle_id TEXT PRIMARY KEY,
                 bdf TEXT NOT NULL,
                 incident_key TEXT NOT NULL,
                 episode INTEGER NOT NULL,
                 stage TEXT NOT NULL,
                 evidence_revision TEXT NOT NULL,
                 evidence_ref TEXT NOT NULL,
                 trigger_utc TEXT NOT NULL,
                 retrigger_utc TEXT NOT NULL,
                 backup_ref TEXT,
                 proposal_id TEXT UNIQUE,
                 nonce TEXT,
                 digest TEXT,
                 shape TEXT,
                 message_id INTEGER,
                 result TEXT,
                 detail TEXT,
                 finished_utc TEXT,
                 notice TEXT,
                 audit_pending INTEGER NOT NULL DEFAULT 0,
                 execution_id TEXT,
                 override_by TEXT,
                 created_utc TEXT NOT NULL,
                 updated_utc TEXT NOT NULL
               )"""
        )
        # Columns added after the table first shipped.
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(tc_action_cycles)")}
        for definition in (
            "finished_utc TEXT", "notice TEXT", "audit_pending INTEGER NOT NULL DEFAULT 0",
            "execution_id TEXT", "shape TEXT", "override_by TEXT",
        ):
            if definition.split()[0] not in existing:
                self.db.execute(f"ALTER TABLE tc_action_cycles ADD COLUMN {definition}")
        self.db.commit()

    def _many(self, where: str, parameters: tuple[Any, ...] = ()) -> list[Cycle]:
        rows = self.db.execute(
            f"SELECT {self._COLUMNS} FROM tc_action_cycles WHERE {where} "
            "ORDER BY created_utc, rowid",
            parameters,
        ).fetchall()
        return [Cycle(*row) for row in rows]

    def active(self) -> Cycle | None:
        cycles = self._many(
            "stage IN ('awaiting_backup','awaiting_answer','executing','reporting')"
        )
        return cycles[-1] if cycles else None

    def resumable(self) -> list[Cycle]:
        return self._many("stage IN ('awaiting_answer','executing','reporting')")

    def by_proposal(self, proposal_id: str | None) -> Cycle | None:
        if not proposal_id:
            return None
        cycles = self._many("proposal_id=?", (proposal_id,))
        return cycles[-1] if cycles else None

    def episode(self, incident_key: str, episode: int) -> list[Cycle]:
        return self._many("incident_key=? AND episode=?", (incident_key, episode))

    def undelivered(self) -> list[Cycle]:
        return self._many("notice IS NOT NULL OR audit_pending=1")

    def get(self, cycle_id: str) -> Cycle:
        return self._many("cycle_id=?", (cycle_id,))[0]

    def create(self, cycle: Cycle) -> None:
        self.db.execute(
            """INSERT INTO tc_action_cycles(cycle_id,bdf,incident_key,episode,stage,
                 evidence_revision,evidence_ref,trigger_utc,retrigger_utc,created_utc,updated_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle.cycle_id, cycle.bdf, cycle.incident_key, cycle.episode, cycle.stage,
             cycle.evidence_revision, cycle.evidence_ref, cycle.trigger_utc,
             cycle.retrigger_utc, cycle.created_utc, cycle.created_utc),
        )
        self.db.commit()

    def update(self, cycle_id: str, now: datetime, **values: Any) -> None:
        if not values or set(values) - self._UPDATABLE:
            raise ValueError("unsupported cycle update")
        keys = sorted(values)
        self.db.execute(
            f"UPDATE tc_action_cycles SET {','.join(f'{key}=?' for key in keys)},updated_utc=? "
            "WHERE cycle_id=?",
            (*[values[key] for key in keys], _text(now), cycle_id),
        )
        self.db.commit()


class Controls:
    """What a person has told the service to do, kept in its private database.

    Instructions steer the service and never approve an action: pausing stops it
    acting, a hold protects one resource, and neither can cause anything to run.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_controls (
                 name TEXT PRIMARY KEY,
                 value TEXT NOT NULL,
                 set_by INTEGER NOT NULL,
                 set_utc TEXT NOT NULL
               )"""
        )
        self.db.commit()

    def set(self, name: str, value: str, user_id: int, now: datetime) -> None:
        self.db.execute(
            """INSERT INTO tc_action_controls(name, value, set_by, set_utc) VALUES(?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value, set_by=excluded.set_by,
                 set_utc=excluded.set_utc""",
            (name[:128], value[:128], int(user_id), _text(now)),
        )
        self.db.commit()

    def clear(self, name: str) -> None:
        self.db.execute("DELETE FROM tc_action_controls WHERE name=?", (name[:128],))
        self.db.commit()

    def get(self, name: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM tc_action_controls WHERE name=?", (name[:128],)
        ).fetchone()
        return None if row is None else str(row[0])

    @property
    def paused(self) -> bool:
        return self.get("paused") is not None

    def held(self, resource: str) -> bool:
        return self.get(f"hold:{resource}") is not None

    def holds(self) -> tuple[str, ...]:
        rows = self.db.execute(
            "SELECT name FROM tc_action_controls WHERE name LIKE 'hold:%' ORDER BY name"
        ).fetchall()
        return tuple(str(row[0])[len("hold:"):] for row in rows)


class Schedule:
    """When the service next means to do something, remembered across restarts.

    Waiting periods that live only in memory are lost on every restart, so a service
    that restarts often would retry immediately and would never reach a reminder it
    only ever schedules for later. These are the service's own cadences and never a
    person's instruction, so they are kept apart from `Controls`.

    Cadences that a restart may safely shorten -- reconciling, re-checking the target --
    stay in memory on purpose: doing those sooner after a restart is what we want.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_schedule (
                 name TEXT PRIMARY KEY,
                 due_utc TEXT NOT NULL,
                 count INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self.db.commit()

    def due(self, name: str, now: datetime) -> bool:
        """True when nothing is scheduled, or the time has come."""
        when, _count = self.get(name)
        return when is None or now >= when

    def get(self, name: str) -> tuple[datetime | None, int]:
        row = self.db.execute(
            "SELECT due_utc, count FROM tc_action_schedule WHERE name=?", (name[:160],)
        ).fetchone()
        if row is None:
            return None, 0
        try:
            return _parse(str(row[0])), int(row[1])
        except ValueError:  # An unreadable time must not stop the service forever.
            return None, int(row[1])

    def set(self, name: str, when: datetime, count: int = 0) -> None:
        self.db.execute(
            """INSERT INTO tc_action_schedule(name, due_utc, count) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET due_utc=excluded.due_utc, count=excluded.count""",
            (name[:160], _text(when), int(count)),
        )
        self.db.commit()

    def clear(self, name: str) -> None:
        self.db.execute("DELETE FROM tc_action_schedule WHERE name=?", (name[:160],))
        self.db.commit()

    def forget(self, prefix: str) -> None:
        self.db.execute(
            "DELETE FROM tc_action_schedule WHERE name LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )
        self.db.commit()


class InboxApprovalAuthenticator:
    """Authenticate an approval only against its stored, authenticated Telegram input."""

    def __init__(self, backend: Any, namespace: str):
        self.backend = backend
        self.namespace = namespace

    def authenticate(self, event: HumanApprovalEvent) -> EventAuthentication:
        prefix = f"telegram:{self.namespace}:"
        stored = None
        if event.event_id.startswith(prefix) and event.event_id[len(prefix):].isdigit():
            stored = self.backend.get_input(self.namespace, int(event.event_id[len(prefix):]))
        authenticated = (
            stored is not None
            and stored.kind is InputKind.APPROVAL_COMMAND
            and stored.group_id == event.group_id
            and stored.sender_id == event.user_id
            and stored.subject_id == event.proposal_id
            and f"{event.proposal_id}:{stored.nonce}" == event.nonce
        )
        return EventAuthentication(event.event_id, event.group_id, event.user_id, authenticated)


class ActionService:
    def __init__(
        self,
        *,
        actions_db: sqlite3.Connection,
        state_db: sqlite3.Connection,
        broker: ActionBroker,
        adapter: MonitorRestartAdapter,
        evidence: EvidenceStore,
        cycles: CycleStore,
        backup: BackupProbe,
        telegram: Any,
        consumer: Any,
        backend: Any,
        namespace: str,
        group_id: int,
        policy_revision: str,
        clock: Callable[[], datetime],
        diagnoser: Diagnoser | None = None,
        assistant: Any | None = None,
        reader: Any | None = None,
        poll_timeout: int = 10,
        report: Callable[[str], None] | None = None,
    ):
        self.actions_db = actions_db
        self.state_db = state_db
        self.broker = broker
        self.adapter = adapter
        self.evidence = evidence
        self.cycles = cycles
        self.controls = Controls(actions_db)
        self.backup = backup
        self.telegram = telegram
        self.consumer = consumer
        self.backend = backend
        self.namespace = namespace
        self.group_id = group_id
        self.policy_revision = policy_revision
        self.clock = clock
        # Without a diagnoser the service keeps the one rule it was taught by hand,
        # which is also what it falls back to when the investigator does not answer.
        self.diagnoser = diagnoser or RuleDiagnoser()
        self.fallback: Diagnoser = RuleDiagnoser()
        self.assistant = assistant
        self.reader = reader
        self.poll_timeout = poll_timeout
        self.report = report or (lambda line: print(line, file=sys.stderr, flush=True))
        self.signatures = _gpu_signatures()
        self.schedule = Schedule(actions_db)
        self._next_target_check_at: datetime | None = None
        self._next_reconcile_at: datetime | None = None
        self._retry_warned: set[str] = set()
        self._cached_question_context = ""
        self._question_context_at: datetime | None = None
        self.phase_failures = 0

    def backup_ref(self, proposal: Any) -> str | None:
        cycle = self.cycles.by_proposal(proposal.proposal_id)
        return None if cycle is None else cycle.backup_ref

    def preflight_ref(self, proposal: Any) -> str | None:
        """The evidence reference the broker privately recorded before dispatch."""
        row = self.actions_db.execute(
            "SELECT pre_evidence_ref FROM tc_action_attempts WHERE proposal_id=?",
            (proposal.proposal_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    # -- loop -----------------------------------------------------------------------

    def recover(self) -> None:
        """Resolve anything a restart of this service interrupted, truthfully and once."""
        self._guard(self.broker.recover_interrupted_attempts)
        for cycle in self.cycles.resumable():
            self._guard(self._resume, cycle)
        self._guard(self._reconcile_unknown, True)
        self._guard(self._deliver)

    def tick(self) -> None:
        self._guard(self._poll)
        self._guard(self._handle_inputs)
        self._guard(self._reconcile_unknown, False)
        self._guard(self._advance)
        self._guard(self._deliver)

    def _guard(self, phase: Callable[..., Any], *arguments: Any) -> None:
        # One phase's failure must not skip the others or end the service. Every
        # authority check raises before its side effect, so continuing grants nothing.
        try:
            phase(*arguments)
        except Exception as error:
            self.phase_failures += 1
            self.report(
                f'{{"operation":"actions","phase":"{phase.__name__}",'
                f'"status":"failed","category":"{type(error).__name__}"}}'
            )
        finally:
            # Nothing is meant to stay open between phases. A transaction left by a
            # swallowed error would hold a stale snapshot and block WAL checkpoints.
            for connection in (self.actions_db, self.state_db):
                if connection.in_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass

    def _poll(self) -> None:
        self.consumer.poll_once(poll_timeout=self.poll_timeout)

    # -- broker state ---------------------------------------------------------------

    def _open_attempt_exists(self) -> bool:
        placeholders = ",".join("?" for _ in OPEN_ATTEMPT_STATES)
        attempt = self.actions_db.execute(
            f"SELECT 1 FROM tc_action_attempts WHERE state IN ({placeholders}) LIMIT 1",
            OPEN_ATTEMPT_STATES,
        ).fetchone()
        lock = self.actions_db.execute("SELECT 1 FROM tc_action_locks LIMIT 1").fetchone()
        return attempt is not None or lock is not None

    def _attempt_for(self, proposal_id: str | None) -> Any | None:
        if not proposal_id:
            return None
        row = self.actions_db.execute(
            "SELECT execution_id FROM tc_action_attempts WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return None if row is None else self.broker.get_attempt(row[0])

    def _unconsumed_approval(self, proposal_id: str | None) -> bool:
        return proposal_id is not None and self.actions_db.execute(
            """SELECT 1 FROM tc_action_approvals
               WHERE proposal_id=? AND consumed_execution_id IS NULL""",
            (proposal_id,),
        ).fetchone() is not None

    def _approver(self, proposal_id: str | None) -> int | None:
        row = self.actions_db.execute(
            "SELECT user_id FROM tc_action_approvals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def _reconcile_unknown(self, force: bool) -> None:
        now = self.clock()
        if not force and self._next_reconcile_at is not None and now < self._next_reconcile_at:
            return
        self._next_reconcile_at = now + RECONCILE_INTERVAL
        # This phase never runs while an execution is in flight, so any attempt still at
        # the dispatch boundary was interrupted (for example by a failed commit).
        self.broker.recover_interrupted_attempts()
        rows = self.actions_db.execute(
            """SELECT execution_id, proposal_id FROM tc_action_attempts
               WHERE state='unknown' ORDER BY started_utc"""
        ).fetchall()
        for execution_id, proposal_id in rows:
            attempt = self.broker.reconcile(execution_id)
            if attempt.state == "unknown":
                key = f"unknown:{execution_id}"
                when, _count = self.schedule.get(key)
                if when is None:
                    # The unknown outcome was announced when it happened; from here the
                    # reminder only nags, and it survives a restart of this service.
                    self.schedule.set(key, now + UNKNOWN_REMINDER)
                elif now >= when:
                    # Scheduled before it is said, so a crash mid-send does not repeat it.
                    self.schedule.set(key, now + UNKNOWN_REMINDER)
                    self._send(
                        "A dcgm-exporter restart result is still unknown because the target "
                        "cannot confirm it. No restart will be proposed until it is settled; "
                        "check the target by hand."
                    )
                continue
            self.schedule.clear(f"unknown:{execution_id}")
            cycle = self.cycles.by_proposal(proposal_id)
            if cycle is not None:
                self._finish_attempt(cycle, attempt, "Settled the earlier restart. ")

    def _resume(self, cycle: Cycle) -> None:
        attempt = self._attempt_for(cycle.proposal_id)
        if attempt is not None:
            if attempt.state in ("reserved", "dispatching"):
                self.broker.recover_interrupted_attempts()
                attempt = self.broker.get_attempt(attempt.execution_id)
            if attempt.state == "unknown":
                attempt = self.broker.reconcile(attempt.execution_id)
            self._finish_attempt(cycle, attempt, "Resumed after an interruption. ")
            return
        if self._unconsumed_approval(cycle.proposal_id):
            # The approval was recorded against a proposal the broker already holds, so
            # it can still be spent; the proposal's own window governs whether it may.
            self._execute(cycle)
            return
        if cycle.stage == "reporting":
            # The referral was written but never finished; say it now.
            self._finish(
                cycle, "referred_to_operator", "referral interrupted",
                notice=f"GPU {cycle.bdf}: I could not carry out what I concluded, and lost "
                       "the detail of it. Ask me with /why.",
            )
            return
        if cycle.stage == "executing":
            self._finish(
                cycle, "not_executed", "execution stopped before the approval was used",
                notice="The approved restart was interrupted before it started. "
                       "No restart was attempted.",
                audit=True,
            )

    # -- proposal lifecycle ---------------------------------------------------------

    def _advance(self) -> None:
        now = self.clock()
        cycle = self.cycles.active()
        if cycle is None:
            if not self._open_attempt_exists():
                self._maybe_start(now)
        elif cycle.stage == "awaiting_backup":
            self._after_backup(cycle, now)
        elif cycle.stage == "awaiting_answer":
            self._recheck_request(cycle, now)
        elif cycle.stage in ("executing", "reporting"):
            if self.schedule.due("execute", now):
                self._resume(cycle)

    def _recheck_request(self, cycle: Cycle, now: datetime) -> None:
        """A waiting request is withdrawn only when the machine stops matching it."""
        if not self.schedule.due("request-check", now):
            return
        self.schedule.set("request-check", now + REQUEST_RECHECK)
        status = self.adapter.status()
        if self._still_matches(cycle, status) is None:
            self._withdraw(cycle, now)

    def _still_matches(self, cycle: Cycle, status: Any) -> Any | None:
        """The proposal this request stands for, or None if the machine moved on.

        ``build_proposal`` refuses an unverified target, a GPU that is no longer blocked
        and a stopped exporter, so anything it accepts only has to match the shape the
        person was shown.
        """
        try:
            proposal = build_proposal(
                status, cycle.bdf, policy_revision=self.policy_revision, clock=self.clock,
                proposal_id=str(cycle.proposal_id),
            )
        except ActorError:
            return None
        return proposal if proposal_shape(proposal) == cycle.shape else None

    def _withdraw(self, cycle: Cycle, now: datetime) -> None:
        self._finish(
            cycle, "superseded", "the target no longer matches the request",
            notice="That dcgm-exporter restart request is withdrawn: the machine has "
                   "changed since I asked. I will ask again if the fault is still there.",
        )

    def _open_handover_incidents(self) -> list[tuple[str, str, int]]:
        rows = self.state_db.execute(
            """SELECT stable_signature, dedup_key, notification_episode FROM incidents
               WHERE source='ssh' AND fault_family='gpu' AND status IN ('open','recovery_pending')
               ORDER BY dedup_key"""
        ).fetchall()
        return [
            (self.signatures[str(row[0])], str(row[1]), int(row[2]))
            for row in rows if str(row[0]) in self.signatures
        ]

    # What the service can carry out today; the catalogue names more than this.
    ACTIONS_WE_CAN_TAKE = ("restart-monitoring-container",)

    def _diagnose(
        self, bdf: str, incident_key: str, episode: int, status: Any, now: datetime
    ) -> Diagnosis:
        """Ask what is wrong, with the read-only diagnostics in hand."""
        try:
            facts = self.state_db.execute(
                """SELECT severity, first_occurrence_utc, last_occurrence_utc, occurrence_count
                   FROM incidents WHERE dedup_key=?""",
                (incident_key,),
            ).fetchone()
        except sqlite3.Error:  # Diagnosis continues on what the target itself reports.
            facts = None
        severity = str(facts[0]) if facts else "error"
        reads = ""
        if self.reader is not None and getattr(self.diagnoser, "uses_reads", True):
            reads = summarize(self.reader.read_all(subject=f"incident:{incident_key}"))
        request = DiagnosisRequest(
            incident_key=incident_key,
            episode=episode,
            severity=severity,
            code="gpu_vfio_handover_blocked",
            bdf=bdf,
            observed_at=status.observed_at,
            status_document=_status_document(status),
            reads=reads,
            incident_facts={
                "first_occurrence_utc": facts[1] if facts else None,
                "last_occurrence_utc": facts[2] if facts else None,
                "occurrence_count": facts[3] if facts else None,
            },
            evidence_revision=evidence_revision(status, bdf),
        )
        diagnosis = self.diagnoser.diagnose(request)
        waited = f"diagnosis:{request.subject_hash()[:32]}"
        if diagnosis.pending:
            due, _count = self.schedule.get(waited)
            if due is None:
                self.schedule.set(waited, now + DIAGNOSIS_WAIT)
                return diagnosis
            if now < due:
                return diagnosis
            # It has had its time. Fall back rather than leave the fault unattended,
            # and say in the evidence that this is not what the investigator said.
            diagnosis = replace(
                self.fallback.diagnose(request),
                reason="the investigator did not answer in time",
            )
        self.schedule.clear(waited)
        self._note_investigator_health(diagnosis, now)
        self.evidence.record("diagnosis", f"incident:{incident_key}", {
            "incident_key": incident_key,
            "episode": episode,
            "evidence_hash": request.evidence_hash(),
            "source": diagnosis.source,
            "reason": diagnosis.reason,
            "summary": None if diagnosis.finding is None else diagnosis.finding.summary,
            "mechanism": None if diagnosis.finding is None else diagnosis.finding.mechanism,
            "action": None if diagnosis.action is None else diagnosis.action.describe(),
            "confidence": None if diagnosis.finding is None else diagnosis.finding.confidence,
            "unsupported_request": None if diagnosis.finding is None else diagnosis.finding.unsupported_request,
            "answer": diagnosis.raw_text,
            "recorded_at": _text(self.clock()),
        })
        return diagnosis

    def _note_investigator_health(self, diagnosis: Diagnosis, now: datetime) -> None:
        """Say plainly when the model is not the one answering, and say it once.

        A fallback diagnosis is a reasonable proposal from the rule, and it looks
        exactly like a healthy one. Without this, the only difference between "the
        model agreed" and "the model has been unreachable for a day" is a phrase in
        the middle of a message nobody reads twice.
        """
        broken = diagnosis.source != MODEL and (diagnosis.reason or "").startswith(
            ("model unavailable", "the investigator did not answer")
        )
        if not broken:
            if self.schedule.get("investigator-silent")[0] is not None:
                self.schedule.clear("investigator-silent")
                self._send("The investigator is answering again.")
            return
        if not self.schedule.due("investigator-silent", now):
            return
        self.schedule.set("investigator-silent", now + INVESTIGATOR_SILENT_REMINDER)
        self._send(
            "I cannot reach the investigator, so I am diagnosing with the one rule I "
            f"was taught by hand ({diagnosis.reason}). Anything it cannot recognise "
            "will go unnoticed until this is fixed."
        )

    def _report_finding(
        self, diagnosis: Diagnosis, bdf: str, incident_key: str, episode: int, now: datetime
    ) -> None:
        """A finding this service cannot carry out is for a person to read."""
        if diagnosis.finding is None:
            return  # Nothing was concluded; the reason is in the evidence.
        cycle_id = str(uuid.uuid4())
        self.cycles.create(Cycle(
            cycle_id=cycle_id, bdf=bdf, incident_key=incident_key, episode=episode,
            stage="reporting", evidence_revision="", evidence_ref="", trigger_utc=_text(now),
            retrigger_utc=_text(now), backup_ref=None, proposal_id=None, nonce=None,
            digest=None, shape=None, created_utc=_text(now),
        ))
        self._finish(
            self.cycles.get(cycle_id), "referred_to_operator",
            (diagnosis.finding.action.describe() if diagnosis.finding.action
             else diagnosis.finding.unsupported_request or "no action"),
            notice=f"GPU {bdf}: I cannot carry this out myself.\n{describe(diagnosis)}",
        )

    def _eligible(self, incident_key: str, episode: int, now: datetime, bdf: str = "") -> bool:
        cycles = self.cycles.episode(incident_key, episode)
        allowed, earliest = episode_outlook(cycles)
        if allowed and (earliest is None or now >= earliest):
            return True
        if not bdf or self._override_for(bdf, now) is None:
            return False
        # An override lifts what this service decided: its waiting, its caps, an episode
        # its own failure closed. It never lifts a person's refusal, and never cuts in
        # front of a cycle already under way.
        if any(cycle.result is None for cycle in cycles):
            return False
        return not any(cycle.result == REFUSED_BY_OPERATOR for cycle in cycles)

    def _maybe_start(self, now: datetime) -> None:
        if self.controls.paused:
            return
        # Only an open handover incident that could still get a proposal justifies a
        # target call; other GPU incidents never do.
        incidents = [
            incident for incident in self._open_handover_incidents()
            if self._eligible(incident[1], incident[2], now, incident[0])
            and not self.controls.held(incident[0])
        ]
        if not incidents:
            return
        # Every restart shares the exporter resource, so the broker would deny an
        # execution inside the cooldown of any earlier attempt; don't ask for one.
        latest = self.actions_db.execute(
            "SELECT MAX(started_utc) FROM tc_action_attempts WHERE action_class=?",
            (ActionClass.MONITOR_COMPONENT_RESTART.value,),
        ).fetchone()[0]
        in_cooldown = latest is not None and now < _parse(latest) + REPEAT_COOLDOWN
        if in_cooldown:
            # Only a GPU somebody asked about may go before the cooldown is up.
            incidents = [
                incident for incident in incidents
                if self._override_for(incident[0], now) is not None
            ]
            if not incidents:
                return
        overridden = any(self._override_for(incident[0], now) is not None for incident in incidents)
        if not overridden and not self.schedule.due("status", now):
            return
        self.schedule.set("status", now + STATUS_RETRY_INTERVAL)
        status = self.adapter.status()
        # Never ask a human to approve something the adapter would refuse to propose.
        if not status.identity_verified or not status.container.present or not status.container.running:
            return
        for bdf, incident_key, episode in incidents:
            if bdf not in status.handover_blocked:
                continue
            diagnosis = self._diagnose(bdf, incident_key, episode, status, now)
            if diagnosis.pending:
                # The investigator is still thinking. Nothing is decided, the override
                # is untouched, and this pass has nothing else to do for this GPU.
                continue
            override = self._spend_override(bdf, now)
            action = diagnosis.action
            if (
                action is None
                or action.name not in self.ACTIONS_WE_CAN_TAKE
                # The adapter restarts one fixed container; a finding about another is
                # for a person, not authority to restart this one.
                or action.parameters.get("container") != COMPONENT
            ):
                self._report_finding(diagnosis, bdf, incident_key, episode, now)
                return
            if self._may_act_alone(now, bdf, override):
                self._act_now(diagnosis, bdf, incident_key, episode, status, now, override)
                return
            cycle_id = str(uuid.uuid4())
            ref = self.evidence.record(
                "proposal-status", f"cycle:{cycle_id}", _status_document(status)
            )
            # Only a backup that starts after the committed evidence can preserve it.
            triggered = _text(self.clock())
            self.cycles.create(Cycle(
                cycle_id=cycle_id, bdf=bdf, incident_key=incident_key, episode=episode,
                stage="awaiting_backup", evidence_revision=evidence_revision(status, bdf),
                evidence_ref=ref, trigger_utc=triggered, retrigger_utc=triggered,
                backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
                created_utc=_text(now),
            ))
            if override is not None:
                self.cycles.update(cycle_id, now, override_by=override)
            try:
                self.backup.trigger()
            except Exception:
                self._finish(
                    self.cycles.get(cycle_id), "backup_failed",
                    "evidence backup could not be requested",
                    notice="A dcgm-exporter restart could not be proposed: the evidence backup "
                           "could not be requested.",
                )
                raise
            return

    def _override_for(self, bdf: str, now: datetime) -> str | None:
        """Who asked for this GPU to be acted on despite the waiting periods.

        An override is spent when the service takes it up, and lapses on its own if it
        never does, so a request that goes nowhere cannot leave the loop running hot.
        """
        value = self.controls.get(f"override:{bdf}")
        if value is None:
            return None
        who, _, when = value.rpartition("@")
        try:
            set_at = _parse(when)
        except ValueError:
            self.controls.clear(f"override:{bdf}")
            return None
        if now - set_at > OVERRIDE_LIFETIME:
            self.controls.clear(f"override:{bdf}")
            self._send(f"The request to act on {bdf} now has lapsed; I am back to my "
                       "usual waiting periods.")
            return None
        return who

    def _spend_override(self, bdf: str, now: datetime) -> str | None:
        """Take up an override, if there is one. It is gone whatever happens next."""
        who = self._override_for(bdf, now)
        if who is not None:
            self.controls.clear(f"override:{bdf}")
        return who

    def _may_act_alone(self, now: datetime, bdf: str = "", override: str | None = None) -> bool:
        """Self-service, and not more often than its daily allowance."""
        if not self.broker.policy.self_service(ActionClass.MONITOR_COMPONENT_RESTART):
            return False
        if override is not None:
            return True
        recent = self.actions_db.execute(
            "SELECT COUNT(*) FROM tc_action_attempts WHERE action_class=? AND started_utc > ?",
            (ActionClass.MONITOR_COMPONENT_RESTART.value, _text(now - timedelta(days=1))),
        ).fetchone()[0]
        # Past the allowance it keeps working, but asks first.
        return int(recent) < SELF_SERVICE_DAILY_CAP

    def _act_now(
        self, diagnosis: Diagnosis, bdf: str, incident_key: str, episode: int,
        status: Any, now: datetime, override: str | None = None,
    ) -> None:
        """Carry out a repair on the controller's own authority, saying so either way."""
        cycle_id = str(uuid.uuid4())
        ref = self.evidence.record(
            "proposal-status", f"cycle:{cycle_id}", _status_document(status)
        )
        self.cycles.create(Cycle(
            cycle_id=cycle_id, bdf=bdf, incident_key=incident_key, episode=episode,
            stage="executing", evidence_revision=evidence_revision(status, bdf),
            evidence_ref=ref, trigger_utc=_text(now), retrigger_utc=_text(now),
            backup_ref=None, proposal_id=None, nonce=None, digest=None, shape=None,
            created_utc=_text(now),
        ))
        proposal = build_proposal(
            status, bdf, policy_revision=self.policy_revision, clock=self.clock
        )
        self.cycles.update(
            cycle_id, now, proposal_id=proposal.proposal_id, digest=proposal.digest,
            shape=proposal_shape(proposal), override_by=override,
        )
        self._send(
            f"GPU {bdf}: restarting dcgm-exporter now, without asking, because this is "
            f"reversible and touches no tenant.\n{describe(diagnosis)}"
        )
        self.broker.submit_proposal(proposal)
        self._execute(self.cycles.get(cycle_id))

    def _after_backup(self, cycle: Cycle, now: datetime) -> None:
        if self.controls.paused:
            return  # Resuming picks this cycle up where it left off.
        waited = now - _parse(cycle.trigger_utc)
        try:
            backup_ref = self.backup.completed_after(_parse(cycle.trigger_utc))
        except Exception:
            backup_ref = None  # Bounded by the backup wait below.
        if backup_ref is None:
            if waited > BACKUP_WAIT:
                self._finish(
                    cycle, "backup_failed", "evidence backup did not complete",
                    notice="A dcgm-exporter restart could not be proposed: the evidence backup "
                           "did not complete within 20 minutes.",
                )
            elif now - _parse(cycle.retrigger_utc) >= BACKUP_RETRIGGER:
                # A path trigger during a running backup can be missed; ask again.
                self.cycles.update(cycle.cycle_id, now, retrigger_utc=_text(now))
                self.backup.trigger()
            return
        if self._next_target_check_at is not None and now < self._next_target_check_at:
            return
        try:
            status = self.adapter.status()
        except Exception:
            self._next_target_check_at = now + TARGET_RETRY
            if waited > BACKUP_WAIT + TARGET_WAIT:
                self._finish(
                    cycle, "target_unavailable", "target status failed after the backup",
                    notice="A dcgm-exporter restart could not be proposed: the target did not "
                           "answer after the evidence backup.",
                )
            raise
        if (
            evidence_revision(status, cycle.bdf) != cycle.evidence_revision
            or not status.identity_verified
            or not status.container.running
        ):
            self._finish(
                cycle, "superseded", "target state changed while evidence was backed up",
                notice="A dcgm-exporter restart proposal was withdrawn: the target changed "
                       "while its evidence was backed up.",
            )
            return
        # The request carries the identity the proposal will have. The proposal itself is
        # built when an answer arrives, so the request can wait for a person indefinitely
        # without holding a five-minute machine-side window open.
        request = build_proposal(
            status, cycle.bdf, policy_revision=self.policy_revision, clock=self.clock
        )
        # The cycle records the backup before the broker can ask for it at execution.
        nonce = secrets.token_urlsafe(18)
        self.cycles.update(
            cycle.cycle_id, now, backup_ref=backup_ref, stage="awaiting_answer",
            proposal_id=request.proposal_id, nonce=nonce, digest=request.digest,
            shape=proposal_shape(request),
        )
        try:
            receipt = self.telegram.send_message(
                self.group_id,
                _request_text(request, status, cycle.bdf, self._last_diagnosis_text()),
                approve_callback=("Approve restart", f"approve:{request.proposal_id}:{nonce}"),
                deny_callback=("Leave it", f"deny:{request.proposal_id}:{nonce}"),
            )
        except Exception:
            # Without a delivered request nobody can answer; end it and say so later.
            self._finish(
                self.cycles.get(cycle.cycle_id), "notify_failed",
                "restart request could not be delivered",
                notice="A dcgm-exporter restart request could not be delivered.",
            )
            return
        self.cycles.update(cycle.cycle_id, now, message_id=receipt.message_id)

    # -- approvals ------------------------------------------------------------------

    ANSWERS = (InputKind.APPROVAL_COMMAND, InputKind.DENIAL_COMMAND)

    def _instruct(self, envelope: Any) -> None:
        """Carry out one operator instruction. None of them can cause an action."""
        self.backend.mark_handled(self.namespace, envelope.update_id)
        now = self.clock()
        verb, argument = str(envelope.subject_id or ""), envelope.nonce
        if verb == "pause":
            self.controls.set("paused", f"telegram:{envelope.sender_id}", envelope.sender_id, now)
            self._send("Paused. I will keep watching and reporting, and act on nothing "
                       "until you resume.")
        elif verb == "resume":
            self.controls.clear("paused")
            self._send("Resumed.")
        elif verb in ("hold", "release"):
            if not argument or not _BDF_ARGUMENT.fullmatch(argument):
                self._send("Name the GPU to hold, like /hold 0000:a1:00.0.")
                return
            if verb == "hold":
                self.controls.set(
                    f"hold:{argument}", f"telegram:{envelope.sender_id}", envelope.sender_id, now
                )
                self._send(f"Holding {argument}: I will not act on it until you release it.")
            else:
                self.controls.clear(f"hold:{argument}")
                self.controls.clear(f"override:{argument}")
                self._send(f"Released {argument}.")
        elif verb == "now":
            if not argument or not _BDF_ARGUMENT.fullmatch(argument):
                self._send("Name the GPU to act on, like /now 0000:a1:00.0.")
                return
            self.controls.set(
                f"override:{argument}", f"telegram:{envelope.sender_id}@{_text(now)}",
                envelope.sender_id, now,
            )
            self._send(
                f"Right away: {argument} gets one more restart, ignoring my own waiting "
                "periods and daily allowance. Everything I check about the machine still "
                "applies."
            )
        elif verb == "again":
            if not argument or not _BDF_ARGUMENT.fullmatch(argument):
                self._send("Name the GPU to look at again, like /again 0000:a1:00.0.")
                return
            self._withdraw_for_operator(argument, envelope.sender_id)
        elif verb == "why":
            self._send(self._last_diagnosis_text())
        elif verb == "status":
            self._send(self._status_text(now))

    def _withdraw_for_operator(self, bdf: str, sender_id: int) -> None:
        """Take back a request that has not run, so the fault can be looked at afresh.

        A refusal would close the incident episode, because it is an answer about the
        fault. This is not an answer: it withdraws what was asked, says nothing about
        whether it was right, and leaves the episode exactly as patient as it was.
        """
        cycle = self.cycles.active()
        if cycle is None or cycle.bdf != bdf:
            self._send(f"Nothing is waiting on {bdf}.")
            return
        if cycle.stage not in ("awaiting_backup", "awaiting_answer"):
            # An execution is under way; taking it back now would be a lie.
            self._send(f"{bdf} is past the point where I can take that back.")
            return
        self._finish(
            cycle, WITHDRAWN_BY_OPERATOR, f"withdrawn by telegram:{sender_id}",
            notice=f"Withdrawn: I will look at {bdf} again from scratch, as soon as I "
                   "next read the target. This says nothing about whether the request "
                   "was right, so it costs the incident none of my patience.",
        )

    def _last_diagnosis_text(self) -> str:
        """What it last concluded, straight from the evidence it kept."""
        row = self.state_db.execute(
            """SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis'
               ORDER BY recorded_utc DESC, rowid DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return "I have not diagnosed anything yet."
        document = json.loads(bytes(row[0]) if isinstance(row[0], (bytes, memoryview)) else row[0])
        if not document.get("summary"):
            return (
                f"My last look at {document.get('incident_key', 'the machine')} reached no "
                f"conclusion ({document.get('reason') or 'no answer'})."
            )
        lines = [
            document["summary"],
            document.get("mechanism") or "",
            f"Wanted: {document['action']}" if document.get("action") else "No action proposed.",
            f"Confidence {document.get('confidence')}, from the {document.get('source')}, "
            f"at {document.get('recorded_at')}.",
        ]
        return "\n".join(line for line in lines if line)

    def _answer_question(self, envelope: Any) -> None:
        """Answer one question from the group. Nothing it says can cause an action."""
        self.backend.mark_handled(self.namespace, envelope.update_id)
        question = str(envelope.nonce or "").strip()
        if not question:
            return
        if self.assistant is None:
            self._send(
                "I have no model to think with right now. Here is what I last concluded:\n"
                + self._last_diagnosis_text()
            )
            return
        context = self._question_context()
        answer = self.assistant.answer(question, context, subject=str(envelope.sender_id))
        if answer is None:
            self._send(
                "I could not reach the model to answer that. Here is what I last "
                "concluded:\n" + self._last_diagnosis_text()
            )
            return
        self._send(answer)

    def _question_context(self) -> str:
        """Recent evidence for a question: open faults, the last diagnosis, the target.

        Reused for a few minutes: answering a run of questions must not mean a run of
        target reads.
        """
        now = self.clock()
        if self._question_context_at is not None and now - self._question_context_at < QUESTION_CONTEXT_LIFETIME:
            return self._cached_question_context
        context = self._build_question_context()
        self._cached_question_context = context
        self._question_context_at = now
        return context

    def _build_question_context(self) -> str:
        incidents = self.state_db.execute(
            """SELECT dedup_key, fault_family, severity, status, last_occurrence_utc
               FROM incidents WHERE status IN ('open','recovery_pending')
               ORDER BY last_occurrence_utc DESC LIMIT 12"""
        ).fetchall()
        parts = [
            "## open incidents\n"
            + ("\n".join(" | ".join(str(value) for value in row) for row in incidents)
               or "(none)"),
            "## last diagnosis\n" + self._last_diagnosis_text(),
        ]
        if self.reader is not None:
            try:
                parts.append(summarize(self.reader.read_all(subject="question")))
            except Exception as error:
                parts.append(f"## target reads\nunavailable: {type(error).__name__}")
        return "\n\n".join(parts)

    def _status_text(self, now: datetime) -> str:
        active = self.cycles.active()
        holds = self.controls.holds()
        lines = [
            "Paused." if self.controls.paused else "Running.",
            f"Holding: {', '.join(holds)}." if holds else "No holds.",
        ]
        if active is None:
            lines.append("Nothing in flight.")
        else:
            lines.append(f"In flight: {active.stage} for GPU {active.bdf}.")
        recent = self.actions_db.execute(
            "SELECT COUNT(*) FROM tc_action_attempts WHERE started_utc > ?",
            (_text(now - timedelta(days=1)),),
        ).fetchone()[0]
        lines.append(f"{recent} restart(s) in the last day.")
        return "\n".join(lines)

    def _handle_inputs(self) -> None:
        answered = 0
        for envelope in self.backend.pending_inputs(self.namespace):
            if envelope.kind is InputKind.INSTRUCTION:
                self._instruct(envelope)
                continue
            if envelope.kind is InputKind.QUESTION:
                if answered >= MAX_QUESTIONS_PER_TICK:
                    continue  # Left pending: the incident loop comes first.
                answered += 1
                self._answer_question(envelope)
                continue
            if envelope.kind not in self.ANSWERS:
                # This namespace belongs to the action service alone; other input would
                # otherwise fill the bounded pending window and hide later answers.
                self.backend.mark_handled(self.namespace, envelope.update_id)
                continue
            cycle = self.cycles.by_proposal(envelope.subject_id)
            if (
                cycle is None
                or cycle.stage != "awaiting_answer"
                or cycle.nonce is None
                or not secrets.compare_digest(str(envelope.nonce), cycle.nonce)
            ):
                self.backend.mark_handled(self.namespace, envelope.update_id)
                self._send("That answer does not match a waiting restart request.")
                continue
            if envelope.kind is InputKind.DENIAL_COMMAND:
                self._deny(cycle, envelope)
            else:
                self._approve_and_execute(cycle, envelope)

    def _deny(self, cycle: Cycle, envelope: Any) -> None:
        """A refusal ends the request and is remembered for this incident episode."""
        self.backend.mark_handled(self.namespace, envelope.update_id)
        self._finish(
            cycle, REFUSED_BY_OPERATOR, f"telegram:{envelope.sender_id} left it alone",
            notice="Understood, leaving dcgm-exporter alone.",
        )

    def _approve_and_execute(self, cycle: Cycle, envelope: Any) -> None:
        # Mark first: a crash can then only lose this tap (tap again), never replay it.
        self.backend.mark_handled(self.namespace, envelope.update_id)
        now = self.clock()
        if self.controls.paused:
            self._send("I am paused, so I did not act on that. Send /resume and approve "
                       "again if you want it to run.")
            return
        try:
            status = self.adapter.status()
        except Exception as error:
            self._send(
                f"Could not re-read the target ({type(error).__name__}). Tap Approve again."
            )
            return
        # The request may have waited for hours. The proposal is built from what the
        # machine says now, and it must still say exactly what you were shown.
        proposal = self._still_matches(cycle, status)
        if proposal is None:
            self._withdraw(cycle, now)
            return
        # A second tap after a failed verification reuses the proposal the first one
        # submitted; that identifier can only ever carry one proposal.
        existing = self.broker.find_proposal(str(cycle.proposal_id))
        if existing is not None:
            if existing.expires_at <= now or proposal_shape(existing) != cycle.shape:
                self._withdraw(cycle, now)
                return
            proposal = existing
        event = HumanApprovalEvent(
            event_id=f"telegram:{self.namespace}:{envelope.update_id}",
            kind=ApprovalKind.APPROVE,
            group_id=envelope.group_id,
            user_id=envelope.sender_id,
            display_name=f"telegram:{envelope.sender_id}",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            nonce=f"{cycle.proposal_id}:{envelope.nonce}",
            occurred_at=now,
        )
        try:
            self._submit_once(proposal, cycle, now)
            self.broker.record_human_event(event)
        except PolicyDenied as error:
            self._send(f"Approval was not accepted: {error}.")
            return
        except Exception:
            self._send("Approval could not be verified right now. Tap Approve again.")
            return
        self._send("Approved. Re-checking the target and restarting dcgm-exporter.")
        self._execute(self.cycles.get(cycle.cycle_id))

    def _submit_once(self, proposal: Any, cycle: Cycle, now: datetime) -> None:
        if self.broker.find_proposal(proposal.proposal_id) is None:
            self.broker.submit_proposal(proposal)
        self.cycles.update(cycle.cycle_id, now, digest=proposal.digest)

    def _execute(self, cycle: Cycle) -> None:
        now = self.clock()
        self.cycles.update(cycle.cycle_id, now, stage="executing")
        # An override is spent on the attempt it allows, whatever its outcome.
        try:
            attempt = self.broker.execute(
                str(cycle.proposal_id), operator_override=cycle.override_by
            )
        except PolicyDenied as error:
            self._finish(
                cycle, "denied", str(error),
                notice=f"Restart was not performed: {error}.", audit=True,
            )
            return
        except Exception as error:
            attempt = self._attempt_for(cycle.proposal_id)
            if attempt is None:
                # Nothing was reserved and the approval is unused: retry the pre-action
                # check while the proposal is valid (the resume path owns the expiry).
                self.schedule.set("execute", now + EXECUTE_RETRY)
                if cycle.proposal_id not in self._retry_warned:
                    self._retry_warned.add(str(cycle.proposal_id))
                    self._send(
                        f"The pre-action check could not complete ({type(error).__name__}). "
                        "Retrying while the approval is still good."
                    )
                raise
            if attempt.state in ("reserved", "dispatching"):
                self.broker.recover_interrupted_attempts()
                attempt = self.broker.get_attempt(attempt.execution_id)
        self._finish_attempt(cycle, attempt)

    # -- outcomes -------------------------------------------------------------------

    def _finish_attempt(self, cycle: Cycle, attempt: Any, prefix: str = "") -> None:
        state = "unknown" if attempt.state in OPEN_ATTEMPT_STATES else attempt.state
        detail = attempt.result_detail or ""
        self._finish(
            cycle, state, detail, notice=prefix + _result_text(state, detail),
            audit=True, execution_id=attempt.execution_id,
        )

    def _finish(
        self, cycle: Cycle, result: str, detail: str, *, notice: str | None = None,
        audit: bool = False, execution_id: str | None = None,
    ) -> None:
        now = self.clock()
        current = self.cycles.get(cycle.cycle_id)
        if current.audit_pending:
            # Re-finishing (an unknown result settling) would overwrite the earlier
            # outcome's audit copy before it was written; write it first if possible.
            try:
                self._audit(current)
            except Exception:
                pass
        self.schedule.forget(f"deliver:{cycle.cycle_id}:")
        finished = {"result": result[:64], "detail": detail[:512], "finished_utc": _text(now)}
        if notice is not None and result != "unknown":
            # Say so when this outcome ends proposals for the incident episode.
            projected = [
                replace(other, **finished) if other.cycle_id == cycle.cycle_id else other
                for other in self.cycles.episode(cycle.incident_key, cycle.episode)
            ]
            if not episode_outlook(projected)[0]:
                notice += EPISODE_CLOSED
        # One write marks the cycle done and queues its message and audit copy.
        self.cycles.update(
            cycle.cycle_id, now, stage="done", audit_pending=int(audit),
            execution_id=execution_id or cycle.execution_id, notice=notice, **finished,
        )

    def _deliver(self) -> None:
        """Write pending audit copies and send pending outcome messages until both succeed.

        The audit copy is a local write and never waits on Telegram. Each cycle and each
        kind backs off on its own, doubling up to an hour, so one failure neither blocks
        other cycles nor repeats a message every minute.
        """
        now = self.clock()
        for cycle in self.cycles.undelivered():
            if cycle.audit_pending and self.schedule.due(f"deliver:{cycle.cycle_id}:audit", now):
                self._attempt_delivery(f"deliver:{cycle.cycle_id}:audit", now, lambda cycle=cycle: (
                    self._audit(cycle), self.cycles.update(cycle.cycle_id, now, audit_pending=0)
                ))
            if cycle.notice is not None and self.schedule.due(f"deliver:{cycle.cycle_id}:notice", now):
                self._attempt_delivery(f"deliver:{cycle.cycle_id}:notice", now, lambda cycle=cycle: (
                    self.telegram.send_message(self.group_id, cycle.notice),
                    self.cycles.update(cycle.cycle_id, now, notice=None),
                ))

    def _attempt_delivery(self, key: str, now: datetime, deliver: Callable[[], Any]) -> None:
        try:
            deliver()
        except Exception as error:
            attempts = self.schedule.get(key)[1]
            # The exponent is capped before multiplying, so the delay can never overflow.
            delay = min(DELIVERY_RETRY * (2 ** min(attempts, 6)), MAX_DELIVERY_RETRY)
            self.schedule.set(key, now + delay, attempts + 1)
            self.phase_failures += 1
            self.report(
                '{"operation":"actions","phase":"_deliver","status":"failed",'
                f'"category":"{type(error).__name__}"}}'
            )
            return
        self.schedule.clear(key)

    def _audit(self, cycle: Cycle) -> None:
        # A backed-up copy of the outcome and approval identity, outside the private
        # store. Every field is fixed at finish time, so a retry records the same row.
        self.evidence.record("restart-result", str(cycle.proposal_id), {
            "proposal_id": cycle.proposal_id,
            "digest": cycle.digest,
            "bdf": cycle.bdf,
            "incident_key": cycle.incident_key,
            "episode": cycle.episode,
            "execution_id": cycle.execution_id,
            "approver_telegram_user_id": self._approver(cycle.proposal_id),
            "result": cycle.result,
            "detail": cycle.detail,
            "recorded_at": cycle.finished_utc,
        })

    def _send(self, text: str) -> None:
        try:
            self.telegram.send_message(self.group_id, text)
        except Exception:
            pass


def episode_outlook(cycles: list[Cycle]) -> tuple[bool, datetime | None]:
    """Whether an incident episode with these cycles may get another proposal, and when."""
    if any(cycle.result is None for cycle in cycles):
        return False, None
    if any(cycle.result == REFUSED_BY_OPERATOR for cycle in cycles):
        return False, None
    executed = [index for index, cycle in enumerate(cycles) if cycle.result in EXECUTED_RESULTS]
    # A person asking for a fresh look is not this service failing to get an answer.
    cycles = [cycle for cycle in cycles if cycle.result != WITHDRAWN_BY_OPERATOR]
    executed = [index for index, cycle in enumerate(cycles) if cycle.result in EXECUTED_RESULTS]
    since = cycles
    earliest: datetime | None = None
    if executed:
        last = cycles[executed[-1]]
        # Only a restart that verifiably cleared the handover allows another one, for a
        # new blockage in the same episode; anything else needs a human.
        if (
            last.result != "succeeded"
            or HANDOVER_CLEARED not in (last.detail or "")
            or len(executed) >= MAX_RESTARTS_PER_EPISODE
        ):
            return False, None
        since = cycles[executed[-1] + 1:]
        earliest = last.ended + REPEAT_COOLDOWN
    if len(since) >= MAX_UNEXECUTED_CYCLES:
        return False, None
    if since:
        backoff = min(PROPOSAL_INTERVAL * (2 ** (len(since) - 1)), MAX_PROPOSAL_INTERVAL)
        after_backoff = since[-1].ended + backoff
        earliest = after_backoff if earliest is None else max(earliest, after_backoff)
    return True, earliest


def _request_text(request: Any, status: Any, bdf: str, reasoning: str = "") -> str:
    return "\n".join(line for line in (
        "terracompute (machine 17049): GPU handover to a VM is blocked",
        f"GPU {bdf} is held by the NVIDIA driver while Vast hands it to a VM rental.",
        "Proposed fix: restart the dcgm-exporter monitoring container to release its GPU "
        "handles. No tenant container is touched.",
        f"Why: {reasoning}" if reasoning else "",
        f"Evidence at {_text(status.observed_at)}: {status.tenants.count} tenant "
        f"containers, VM rentals {', '.join(status.vm_containers) or 'none'}.",
        f"Request {request.proposal_id}. It waits for your answer: no answer changes "
        "nothing, and it is withdrawn only if the machine stops matching it. One "
        "approval allows exactly one restart.",
    ) if line)


def _result_text(state: str, detail: str) -> str:
    outcome = {
        "succeeded": "Restart succeeded",
        "failed": "Restart failed",
        "postcondition-failed": "Restart ran but its checks failed; stopped for review",
        "refused": "No restart was performed",
        "unknown": "Restart result is unknown; it is checked against the target every 5 "
                   "minutes and nothing else is proposed until it is settled",
    }.get(state, f"Restart ended as {state}")
    return f"{outcome}: {detail}" if detail else outcome
