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
    HANDOVER_CLEARED,
    EvidenceStore,
    MonitorRestartAdapter,
    _status_document,
    build_proposal,
    evidence_revision,
    handover_incident_signature,
)
from .policy import REPEAT_COOLDOWN, ActionClass, PolicyDenied
from .telegram import InputKind

PROPOSAL_INTERVAL = timedelta(minutes=15)
MAX_PROPOSAL_INTERVAL = timedelta(hours=4)
MAX_UNEXECUTED_CYCLES = 8
MAX_RESTARTS_PER_EPISODE = 3
STATUS_RETRY_INTERVAL = timedelta(minutes=5)
BACKUP_WAIT = timedelta(minutes=20)
BACKUP_RETRIGGER = timedelta(minutes=5)
TARGET_WAIT = timedelta(minutes=10)
TARGET_RETRY = timedelta(minutes=1)
EXECUTE_RETRY = timedelta(minutes=1)
RECONCILE_INTERVAL = timedelta(minutes=5)
UNKNOWN_REMINDER = timedelta(hours=6)
DELIVERY_RETRY = timedelta(minutes=1)
MAX_DELIVERY_RETRY = timedelta(hours=1)
BACKUP_UNIT = "terracompute-backup.service"
# A restart ran (or may have run) for these results.
EXECUTED_RESULTS = frozenset({"succeeded", "failed", "postcondition-failed", "unknown"})
OPEN_ATTEMPT_STATES = ("reserved", "dispatching", "unknown")
EPISODE_CLOSED = " No further restart proposals for this incident until it recovers."
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
    expires_utc: str | None
    created_utc: str
    result: str | None = None
    detail: str | None = None
    finished_utc: str | None = None
    notice: str | None = None
    audit_pending: int = 0
    execution_id: str | None = None

    @property
    def ended(self) -> datetime:
        return _parse(self.finished_utc or self.created_utc)


class CycleStore:
    _COLUMNS = (
        "cycle_id,bdf,incident_key,episode,stage,evidence_revision,evidence_ref,trigger_utc,"
        "retrigger_utc,backup_ref,proposal_id,nonce,digest,expires_utc,created_utc,result,"
        "detail,finished_utc,notice,audit_pending,execution_id"
    )
    _UPDATABLE = frozenset({
        "stage", "retrigger_utc", "backup_ref", "proposal_id", "nonce", "digest",
        "expires_utc", "message_id", "result", "detail", "finished_utc", "notice",
        "audit_pending", "execution_id",
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
                 expires_utc TEXT,
                 message_id INTEGER,
                 result TEXT,
                 detail TEXT,
                 finished_utc TEXT,
                 notice TEXT,
                 audit_pending INTEGER NOT NULL DEFAULT 0,
                 execution_id TEXT,
                 created_utc TEXT NOT NULL,
                 updated_utc TEXT NOT NULL
               )"""
        )
        # Columns added after the table first shipped.
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(tc_action_cycles)")}
        for definition in (
            "finished_utc TEXT", "notice TEXT", "audit_pending INTEGER NOT NULL DEFAULT 0",
            "execution_id TEXT",
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
        cycles = self._many("stage IN ('awaiting_backup','awaiting_approval','executing')")
        return cycles[-1] if cycles else None

    def resumable(self) -> list[Cycle]:
        return self._many("stage IN ('awaiting_approval','executing')")

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
        poll_timeout: int = 10,
        report: Callable[[str], None] | None = None,
    ):
        self.actions_db = actions_db
        self.state_db = state_db
        self.broker = broker
        self.adapter = adapter
        self.evidence = evidence
        self.cycles = cycles
        self.backup = backup
        self.telegram = telegram
        self.consumer = consumer
        self.backend = backend
        self.namespace = namespace
        self.group_id = group_id
        self.policy_revision = policy_revision
        self.clock = clock
        self.poll_timeout = poll_timeout
        self.report = report or (lambda line: print(line, file=sys.stderr, flush=True))
        self.signatures = _gpu_signatures()
        self._next_status_at: datetime | None = None
        self._next_target_check_at: datetime | None = None
        self._next_execute_at: datetime | None = None
        self._next_reconcile_at: datetime | None = None
        self._delivery_backoff: dict[tuple[str, str], tuple[int, datetime]] = {}
        self._unknown_reminded: dict[str, datetime] = {}
        self._retry_warned: set[str] = set()
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
                reminded = self._unknown_reminded.setdefault(execution_id, now)
                if now - reminded >= UNKNOWN_REMINDER:
                    self._unknown_reminded[execution_id] = now
                    self._send(
                        "A dcgm-exporter restart result is still unknown because the target "
                        "cannot confirm it. No restart will be proposed until it is settled; "
                        "check the target by hand."
                    )
                continue
            self._unknown_reminded.pop(execution_id, None)
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
        now = self.clock()
        if self._unconsumed_approval(cycle.proposal_id):
            if cycle.expires_utc is not None and now < _parse(cycle.expires_utc):
                self._execute(cycle)
                return
            self._finish(
                cycle, "not_executed", "approval recorded but the proposal expired first",
                notice="The approved restart could not run before its proposal expired. "
                       "No restart was attempted.",
                audit=True,
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
        elif cycle.stage == "awaiting_approval" and cycle.expires_utc is not None:
            if now >= _parse(cycle.expires_utc):
                self._finish(
                    cycle, "expired", "no approval within the proposal lifetime",
                    notice="Restart proposal expired without approval.",
                )
        elif cycle.stage == "executing":
            if self._next_execute_at is None or now >= self._next_execute_at:
                self._resume(cycle)

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

    def _eligible(self, incident_key: str, episode: int, now: datetime) -> bool:
        allowed, earliest = episode_outlook(self.cycles.episode(incident_key, episode))
        return allowed and (earliest is None or now >= earliest)

    def _maybe_start(self, now: datetime) -> None:
        # Only an open handover incident that could still get a proposal justifies a
        # target call; other GPU incidents never do.
        incidents = [
            incident for incident in self._open_handover_incidents()
            if self._eligible(incident[1], incident[2], now)
        ]
        if not incidents:
            return
        # Every restart shares the exporter resource, so the broker would deny an
        # execution inside the cooldown of any earlier attempt; don't ask for one.
        latest = self.actions_db.execute(
            "SELECT MAX(started_utc) FROM tc_action_attempts WHERE action_class=?",
            (ActionClass.MONITOR_COMPONENT_RESTART.value,),
        ).fetchone()[0]
        if latest is not None and now < _parse(latest) + REPEAT_COOLDOWN:
            return
        if self._next_status_at is not None and now < self._next_status_at:
            return
        self._next_status_at = now + STATUS_RETRY_INTERVAL
        status = self.adapter.status()
        # Never ask a human to approve something the adapter would refuse to propose.
        if not status.identity_verified or not status.container.present or not status.container.running:
            return
        for bdf, incident_key, episode in incidents:
            if bdf not in status.handover_blocked:
                continue
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
                backup_ref=None, proposal_id=None, nonce=None, digest=None, expires_utc=None,
                created_utc=_text(now),
            ))
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

    def _after_backup(self, cycle: Cycle, now: datetime) -> None:
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
        proposal = build_proposal(
            status, cycle.bdf, policy_revision=self.policy_revision, clock=self.clock
        )
        # The cycle records the backup before the broker can ask for it at execution.
        self.cycles.update(cycle.cycle_id, now, backup_ref=backup_ref)
        self.broker.submit_proposal(proposal)
        nonce = secrets.token_urlsafe(18)
        self.cycles.update(
            cycle.cycle_id, now, stage="awaiting_approval", proposal_id=proposal.proposal_id,
            nonce=nonce, digest=proposal.digest, expires_utc=_text(proposal.expires_at),
        )
        try:
            receipt = self.telegram.send_message(
                self.group_id,
                _approval_text(proposal, status, cycle.bdf),
                approve_callback=("Approve restart", f"approve:{proposal.proposal_id}:{nonce}"),
            )
        except Exception:
            # Without a delivered request nobody can approve; end it and say so later.
            self._finish(
                self.cycles.get(cycle.cycle_id), "notify_failed",
                "approval request could not be delivered",
                notice="A dcgm-exporter restart approval request could not be delivered.",
            )
            return
        self.cycles.update(cycle.cycle_id, now, message_id=receipt.message_id)

    # -- approvals ------------------------------------------------------------------

    def _handle_inputs(self) -> None:
        for envelope in self.backend.pending_inputs(self.namespace):
            if envelope.kind is not InputKind.APPROVAL_COMMAND:
                # This namespace belongs to the action service alone; other input would
                # otherwise fill the bounded pending window and hide later approvals.
                self.backend.mark_handled(self.namespace, envelope.update_id)
                continue
            cycle = self.cycles.by_proposal(envelope.subject_id)
            if (
                cycle is None
                or cycle.stage != "awaiting_approval"
                or cycle.nonce is None
                or not secrets.compare_digest(str(envelope.nonce), cycle.nonce)
            ):
                self.backend.mark_handled(self.namespace, envelope.update_id)
                self._send("That approval does not match a pending restart proposal.")
                continue
            self._approve_and_execute(cycle, envelope)

    def _approve_and_execute(self, cycle: Cycle, envelope: Any) -> None:
        # Mark first: a crash can then only lose this tap (tap again), never replay it.
        self.backend.mark_handled(self.namespace, envelope.update_id)
        event = HumanApprovalEvent(
            event_id=f"telegram:{self.namespace}:{envelope.update_id}",
            kind=ApprovalKind.APPROVE,
            group_id=envelope.group_id,
            user_id=envelope.sender_id,
            display_name=f"telegram:{envelope.sender_id}",
            proposal_id=str(cycle.proposal_id),
            proposal_digest=str(cycle.digest),
            nonce=f"{cycle.proposal_id}:{envelope.nonce}",
            occurred_at=self.clock(),
        )
        try:
            self.broker.record_human_event(event)
        except PolicyDenied as error:
            self._send(f"Approval was not accepted: {error}.")
            return
        except Exception:
            self._send("Approval could not be verified right now. Tap Approve again.")
            return
        self._send("Approved. Re-checking the target and restarting dcgm-exporter.")
        self._execute(cycle)

    def _execute(self, cycle: Cycle) -> None:
        now = self.clock()
        self.cycles.update(cycle.cycle_id, now, stage="executing")
        try:
            attempt = self.broker.execute(str(cycle.proposal_id))
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
                self._next_execute_at = now + EXECUTE_RETRY
                if cycle.proposal_id not in self._retry_warned:
                    self._retry_warned.add(str(cycle.proposal_id))
                    self._send(
                        f"The pre-action check could not complete ({type(error).__name__}). "
                        f"Retrying until the proposal expires at {cycle.expires_utc}."
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
        for kind in ("audit", "notice"):
            self._delivery_backoff.pop((cycle.cycle_id, kind), None)
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
            if cycle.audit_pending and self._delivery_due((cycle.cycle_id, "audit"), now):
                self._attempt_delivery((cycle.cycle_id, "audit"), now, lambda cycle=cycle: (
                    self._audit(cycle), self.cycles.update(cycle.cycle_id, now, audit_pending=0)
                ))
            if cycle.notice is not None and self._delivery_due((cycle.cycle_id, "notice"), now):
                self._attempt_delivery((cycle.cycle_id, "notice"), now, lambda cycle=cycle: (
                    self.telegram.send_message(self.group_id, cycle.notice),
                    self.cycles.update(cycle.cycle_id, now, notice=None),
                ))

    def _delivery_due(self, key: tuple[str, str], now: datetime) -> bool:
        return now >= self._delivery_backoff.get(key, (0, now))[1]

    def _attempt_delivery(self, key: tuple[str, str], now: datetime, deliver: Callable[[], Any]) -> None:
        try:
            deliver()
        except Exception as error:
            attempts = self._delivery_backoff.get(key, (0, now))[0]
            # The exponent is capped before multiplying, so the delay can never overflow.
            delay = min(DELIVERY_RETRY * (2 ** min(attempts, 6)), MAX_DELIVERY_RETRY)
            self._delivery_backoff[key] = (attempts + 1, now + delay)
            self.phase_failures += 1
            self.report(
                '{"operation":"actions","phase":"_deliver","status":"failed",'
                f'"category":"{type(error).__name__}"}}'
            )
            return
        self._delivery_backoff.pop(key, None)

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


def _approval_text(proposal: Any, status: Any, bdf: str) -> str:
    return "\n".join((
        "terracompute (machine 17049): GPU handover to a VM is blocked",
        f"GPU {bdf} is held by the NVIDIA driver while Vast hands it to a VM rental.",
        "Proposed fix: restart the dcgm-exporter monitoring container to release its GPU "
        "handles. No tenant container is touched.",
        f"Evidence at {_text(status.observed_at)}: {status.nvidia_visible_count}/"
        f"{status.pci_gpu_count} GPUs visible, {status.tenants.count} tenant containers, "
        f"VM rentals {', '.join(status.vm_containers) or 'none'}.",
        f"Proposal {proposal.proposal_id}, digest {proposal.digest[:16]}, "
        f"expires {_text(proposal.expires_at)}. One approval allows exactly one restart.",
    ))


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
