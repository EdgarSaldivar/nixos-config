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

import hashlib
import json
from types import SimpleNamespace
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .authorization import Risk
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
    fault_revision,
    handover_incident_signature,
    proposal_shape,
)
from .diagnosing import (
    MODEL,
    Review,
    parse_review,
    review_prompt,
    Diagnoser,
    Diagnosis,
    DiagnosisRequest,
    RuleDiagnoser,
    conversation_followup_prompt,
    describe,
)
from .diagnosis import (MAX_READ_SCRIPT_CHARS, SUSPENDS_A_REQUEST, ObserveRound,
                        ProposedAction, operator_prose_problem)
from .console import CONSOLE_FORBIDDEN_STEERS, CONSOLE_SENDER, OutboxRecorder
from .inspection import answered, summarize
from .policy import REPEAT_COOLDOWN, ActionClass, PolicyDenied
from .telegram import InputKind
from .secrets_scrub import scrub

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
# How long one investigation may spend looking before the rule answers instead, and how
# many rounds of looking it gets. Six rounds is up to seven investigator turns counting
# the first ask, against the twelve one investigation may spend.
OBSERVE_LOOP_DEADLINE = timedelta(minutes=90)
MAX_OBSERVE_ROUNDS = 6
# After giving up on looking at a fault, how long before it is worth looking again.
OBSERVE_LOOP_COOLDOWN = timedelta(hours=6)
# One look per fault, not per symptom. A single GPU falling off the bus on 2026-09-30
# raised xid, capacity, probe and BMC incidents with one identical fault_revision, and
# each started its own investigation and Telegram thread. Once any incident has been
# looked at under a machine state, other incidents in that same state wait for it to
# change (a reboot, a GPU appearing or disappearing) or for a person to ask.
SAME_FAULT_WINDOW = timedelta(hours=24)
# Only these are held back: they report what some other fault did to capacity or to
# collection. A hardware fault (xid, aer, bmc, gpu, ...) is always looked at, because
# fault_revision does not move for it -- the Xid on 2026-09-30 left it unchanged.
SYMPTOM_FAMILIES = frozenset({"capacity", "probe", "source"})
# Investigations this service starts on its own in any 24 hours. Past it, incidents are
# still recorded and one notice a day says so; a person asking is never counted out.
MAX_UNREQUESTED_LOOKS = 4
LOOK_BUDGET_WINDOW = timedelta(hours=24)
# A proposal a person let expire or turned down is not put to them again for this long.
# On 2026-09-30 the same reboot was asked for twice, forty minutes after the first
# request expired unanswered, because a second incident about the same fault reached it.
REPEAT_PROPOSAL_WINDOW = timedelta(hours=24)
# One go at a given monitoring container, then a wait. Acting because something looks
# unhealthy, finding it still looks unhealthy and acting again is a loop that looks
# like work while nothing improves.
MONITORING_ACTION_COOLDOWN = timedelta(hours=1)
MAX_OBSERVE_STORED = 8000
# How much of one pass the reads may take before the rest of the service gets it back.
# A read that overruns it still finishes -- the budget is checked after, so the worst
# any other work waits is one read, exactly as when it was one read per pass.
OBSERVE_TICK_BUDGET = 20.0
# How long to wait for an answer to something the operator said before admitting that
# none is coming. Shorter than a diagnosis: somebody is watching the chat.
# Ten, not five: a turn that searches the web and reasons over what it read runs longer
# than one that restates a diagnosis, and the chat is the path that now does that.
CONVERSATION_WAIT = timedelta(minutes=10)
# A person is waiting on a conversation's reads, so they may hold the pass longer than an
# incident's looking does.
CONVERSATION_TICK_BUDGET = 60.0
# How often to say that the investigator is not answering. Falling back to the rule
# keeps the fault attended, but it must never be the only sign that the model path is
# broken: five unrelated faults in one evening all surfaced as an ordinary proposal.
INVESTIGATOR_SILENT_REMINDER = timedelta(hours=4)
# How a phase that keeps failing reports itself: once when it starts, then at widening
# intervals with a count, then once when it recovers. Never the same line forever.
PHASE_FAILURE_REMINDER = timedelta(minutes=15)
PHASE_FAILURE_RUN = 20
# How many questions are answered per pass, and how long the evidence behind an answer
# is reused. Group chat must not crowd out the incident loop or the model's allowance.
MAX_QUESTIONS_PER_TICK = 2
QUESTION_CONTEXT_LIFETIME = timedelta(minutes=5)
# A direct status answer is about what the machine is reporting now, not every
# incident that has ever failed to recover cleanly. All relevant collectors run at
# least this often; older open rows are called historical rather than current.
CURRENT_FAULT_MAX_AGE = timedelta(minutes=30)
MAX_CURRENT_FAULTS = 8
DELIVERY_RETRY = timedelta(minutes=1)
MAX_DELIVERY_RETRY = timedelta(hours=1)
BACKUP_UNIT = "terracompute-backup.service"
# A restart ran (or may have run) for these results.
EXECUTED_RESULTS = frozenset({"succeeded", "failed", "postcondition-failed", "unknown"})
# What a conversation is about when no incident is open: the machine itself.
MACHINE_SUBJECT = "machine:17049"
# What the operator said is kept whole: it is the question, and cutting it to fit a
# flag column is how "check the monitoring repo" became "look the machine over".
MAX_CONVERSATION_QUESTION_CHARS = 4000
# Room for a pending review: a plan of up to 8,000 characters and its context, as JSON.
MAX_NOTE_CHARS = 64_000
# How many rounds of reads one message may run before it must answer. Each round is a
# turn in the same thread, so this is also a bound on what one question can cost.
# Ten, not six: on 2026-09-25 it spent six rounds recovering from limits it did not know
# about and ended one read short of a plan.
MAX_CHAT_READ_ROUNDS = 10
# Turns beyond the reads in which it may correct something refused -- a plan in the wrong
# block, an oversized read -- so a fixable mistake in its last word is not the end.
MAX_CHAT_CORRECTIONS = 3
# How many times a reviewer's critique goes back to the agent before the plan goes to a
# person anyway, and how long to wait for a review.
MAX_PLAN_REVISIONS = 2
REVIEW_WAIT = timedelta(minutes=20)
# A conversation nobody has touched for this long is over, whatever state it was in.
CONVERSATION_LIFETIME = timedelta(hours=2)
# How long a model-proposed request stays approvable. Five minutes suited one restart;
# a plan has to be read first, and most taps on this machine arrived late.
GENERIC_APPROVAL_LIFETIME = timedelta(minutes=30)
# What one Telegram message may hold. A request that does not fit is not delivered at all.
MAX_TELEGRAM_TEXT = 4096
# After which endings of a handover restart the durable fix is worth putting to a person:
# it ran (the stopgap bought time), it failed (the stopgap is not enough), or they said no
# to the stopgap (they may well want the cure instead).
DURABLE_AFTER_RESULTS = frozenset({
    "succeeded", "failed", "postcondition-failed", "refused_by_operator",
})
# The one fault this service was taught by hand, and the only one with an adapter.
HANDOVER_CODE = "gpu_vfio_handover_blocked"
# A look a person asked for, with no incident behind it. Kept apart from an incident's
# key so nothing about it can be mistaken for a fault this service detected.
REVIEW_KEY = "request:machine"
REVIEW_REQUEST = "review-requested"


def _same_observation_subject(key: str, episode: int, other_key: str,
                              other_episode: int) -> bool:
    if key in (MACHINE_SUBJECT, REVIEW_KEY) and other_key in (MACHINE_SUBJECT, REVIEW_KEY):
        return True
    return key == other_key and episode == other_episode


def _request_episode(value: str) -> int:
    """One review told apart from the next by when it was asked for.

    Stable for as long as the request stands, and different for the next one, which is
    exactly what an investigation's identity has to be.
    """
    try:
        return int(_parse(value.rpartition("@")[2]).timestamp())
    except ValueError:
        return 1
# The few things that must still work with no model to read them. Every entry is an
# unambiguous whole message; nothing here is matched inside a longer sentence, because
# the sentence is usually the part that reverses it.
_PLAIN_STEER = {
    "pause": "pause", "stop": "pause", "stop acting": "pause", "halt": "pause",
    "pause acting": "pause", "freeze": "pause",
    "resume": "resume", "continue": "resume", "carry on": "resume",
    "unpause": "resume", "resume acting": "resume",
}
_CURRENT_STATE_QUESTIONS = frozenset({
    "status", "machine status", "current status", "whats the status",
    "what is the status", "whats wrong", "what is wrong", "whats wrong with it",
    "what is wrong with it", "whats wrong with the machine",
    "what is wrong with the machine", "whats happening", "what is happening",
    "whats happening with the machine", "what is happening with the machine",
    "is it working", "is the machine working",
})
OPEN_ATTEMPT_STATES = ("reserved", "dispatching", "unknown")
_BDF_ARGUMENT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
EPISODE_CLOSED = " No further restart proposals for this incident until it recovers."
GENERIC_ACTION_CLOSED = (
    " Ask me to investigate again before another action is proposed."
)
# A person said no. That answer holds for this incident episode.
REFUSED_BY_OPERATOR = "refused_by_operator"
# A person asked for a fresh look. Unlike a refusal this says nothing about the fault,
# so it neither closes the episode nor counts against its patience.
WITHDRAWN_BY_OPERATOR = "withdrawn_by_operator"
# Outcomes that say nothing about whether a person is being pestered. The waiting
# between proposals exists so the same question is not put over and over; a request
# that was withdrawn, or that could not be delivered at all, was never put. Charging
# for those doubled the wait after every Telegram hiccup, on the way to an unanswered
# incident no longer being raised.
#
# A failed backup or an unreachable target is deliberately not here: those say the
# machine is unwell, retrying them costs real work, and backing off is right.
UNASKED_RESULTS = frozenset({WITHDRAWN_BY_OPERATOR, "notify_failed"})
_UNIX_TIMESTAMP = re.compile(r"^@([0-9]{1,12})$")


def _text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _asks_current_state(question: str) -> bool:
    """Recognise present-state questions without requiring one canned sentence.

    The exact-message list is the conservative fast path. Operators naturally join
    questions (``is it healthy now, and what are the stats?``), though, and sending
    that to an incident thread loses the full-machine evidence and can resurrect stale
    conclusions. The second path therefore requires both a machine subject and a
    state/statistics term; it is deliberately not a general keyword matcher.
    """
    normalized = re.sub(r"[^a-z0-9 ]", "", question.lower())
    normalized = " ".join(normalized.split())
    if normalized in _CURRENT_STATE_QUESTIONS:
        return True
    words = frozenset(normalized.split())
    subject = bool(words & {"machine", "host", "node", "gpu", "gpus", "it"})
    state = bool(
        words
        & {
            "health", "healthy", "status", "stats", "statistics", "working",
            "wrong", "happening", "responsive", "online",
        }
    )
    return subject and state


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
    # The Telegram message carrying this request's buttons. Written since the request
    # was first sent and never read until the buttons needed taking off again.
    message_id: int | None = None
    # The command a person is being asked to approve, when this cycle is asking about
    # one the model proposed rather than the one fixed adapter's restart.
    command: str | None = None
    delivered_utc: str | None = None

    @property
    def ended(self) -> datetime:
        return _parse(self.finished_utc or self.created_utc)


class CycleStore:
    _COLUMNS = (
        "cycle_id,bdf,incident_key,episode,stage,evidence_revision,evidence_ref,trigger_utc,"
        "retrigger_utc,backup_ref,proposal_id,nonce,digest,shape,created_utc,result,"
        "detail,finished_utc,notice,audit_pending,execution_id,override_by,message_id,"
        "command,delivered_utc"
    )
    _UPDATABLE = frozenset({
        "stage", "retrigger_utc", "backup_ref", "proposal_id", "nonce", "digest",
        "shape", "message_id", "result", "detail", "finished_utc", "notice",
        "audit_pending", "execution_id", "override_by", "command", "delivered_utc",
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
                 command TEXT,
                 delivered_utc TEXT,
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
            "execution_id TEXT", "shape TEXT", "override_by TEXT", "message_id INTEGER",
            "command TEXT", "delivered_utc TEXT",
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
            "stage IN ('awaiting_backup','awaiting_delivery','awaiting_answer','executing','reporting')"
        )
        return cycles[-1] if cycles else None

    def resumable(self) -> list[Cycle]:
        return self._many("stage IN ('awaiting_delivery','awaiting_answer','executing','reporting')")

    def by_proposal(self, proposal_id: str | None) -> Cycle | None:
        if not proposal_id:
            return None
        cycles = self._many("proposal_id=?", (proposal_id,))
        return cycles[-1] if cycles else None

    def episode(self, incident_key: str, episode: int) -> list[Cycle]:
        return self._many("incident_key=? AND episode=?", (incident_key, episode))

    def recently_unwanted(self, command: str, since: datetime) -> bool:
        """Whether this exact command was let expire or turned down since ``since``."""
        return self.db.execute(
            """SELECT 1 FROM tc_action_cycles
               WHERE command=? AND result IN ('expired', ?) AND finished_utc>=? LIMIT 1""",
            (command, REFUSED_BY_OPERATOR, _text(since)),
        ).fetchone() is not None

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

    def reviews(self) -> tuple[str, ...]:
        """Incident or GPU subjects with a requested fresh look."""
        rows = self.db.execute(
            "SELECT name FROM tc_action_controls WHERE name LIKE 'review:%' ORDER BY name"
        ).fetchall()
        return tuple(str(row[0])[len("review:"):] for row in rows)


# Every state a loop can be in. OPEN is being worked on, FINAL has stopped looking and
# is asking its last question, SPENT gave up looking, CLOSED reached a finding.
OPEN, FINAL, SPENT, CLOSED = "open", "final", "spent", "closed"
LOOP_STATES = (OPEN, FINAL, SPENT, CLOSED)
LIVE_STATES = (OPEN, FINAL)


def _live(alias: str = "") -> str:
    """The one definition of a loop that work may still reach.

    The lookup, the read queue, the reaper and the uniqueness the schema enforces must
    all use exactly this set. When one of them disagrees the disagreement is silent,
    and what it produces is either two loops on one fault or a finished loop answering
    as the current one -- both of which happened here.

    Written positively on purpose. As `state <> 'closed'` it said "every state I have
    not thought of yet is live", so the day SPENT was added it became live in four
    places at once and the only symptom was a finished loop being treated as current.
    A state nobody has considered yet has to be excluded by default.
    """
    at = f"{alias}." if alias else ""
    return f"{at}state IN ({', '.join(repr(state) for state in LIVE_STATES)})"


class Observations:
    """One read loop per fault, and the reads it is part way through.

    A loop is a durable object with an identity of its own, not something derived from
    its rows: it has to be able to say "started, nothing asked yet", to survive a
    restart without its identity moving, and to be told apart from the next loop on the
    same incident episode. The question it is asking is frozen into it at the start,
    because every round must ask about one machine state -- `subject_hash` folds in the
    evidence revision and which reads answered, and both of those move under it
    otherwise, opening a new episode instead of replaying the answer just collected.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tc_action_observe_loops (
              loop_id           TEXT PRIMARY KEY,
              incident_key      TEXT NOT NULL,
              episode           INTEGER NOT NULL,
              bdf               TEXT NOT NULL,
              state             TEXT NOT NULL,
              started_utc       TEXT NOT NULL,
              updated_utc       TEXT NOT NULL,
              rounds            INTEGER NOT NULL DEFAULT 0,
              severity          TEXT NOT NULL,
              code              TEXT NOT NULL DEFAULT 'gpu_vfio_handover_blocked',
              evidence_revision TEXT NOT NULL,
              reads_available   TEXT NOT NULL,
              reads_text        TEXT NOT NULL,
              status_json       TEXT NOT NULL,
              facts_json        TEXT NOT NULL,
              vast_text         TEXT NOT NULL DEFAULT '',
              vast_reports      INTEGER NOT NULL DEFAULT 0,
              attempts          INTEGER NOT NULL DEFAULT 0,
              observed_utc      TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tc_action_observe_reads (
              loop_id      TEXT NOT NULL,
              round        INTEGER NOT NULL,
              seq          INTEGER NOT NULL,
              command      TEXT NOT NULL,
              note         TEXT NOT NULL DEFAULT '',
              output       TEXT,
              asked_utc    TEXT NOT NULL,
              ran_utc      TEXT,
              PRIMARY KEY (loop_id, round, seq)
            );
            """
        )
        self._add_missing_columns()
        self._rebuild_live_index()
        self.db.commit()

    # Columns added to the loop header after the table first shipped, with the default
    # a pre-existing row should carry. `CREATE TABLE IF NOT EXISTS` does nothing to a
    # table that already exists, so without this the first deploy after a new column
    # meets a live database that has not got it and every insert fails -- which is
    # exactly what happened, as an OperationalError caught by the phase guard and
    # logged as a category with no hint of which column was missing.
    _ADDED_COLUMNS = (
        ("code", "TEXT NOT NULL DEFAULT 'gpu_vfio_handover_blocked'"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("vast_text", "TEXT NOT NULL DEFAULT ''"),
        ("vast_reports", "INTEGER NOT NULL DEFAULT 0"),
        ("observed_utc", "TEXT NOT NULL DEFAULT ''"),
        # Added after loops already existed on the live machine, so it migrates in
        # with a default rather than arriving with the table.
        ("fault_revision", "TEXT NOT NULL DEFAULT ''"),
    )

    def _add_missing_columns(self) -> None:
        have = {
            row[1] for row in self.db.execute("PRAGMA table_info(tc_action_observe_loops)")
        }
        for name, definition in self._ADDED_COLUMNS:
            if name not in have:
                self.db.execute(
                    f"ALTER TABLE tc_action_observe_loops ADD COLUMN {name} {definition}"
                )

    def _rebuild_live_index(self) -> None:
        """Keep the schema's idea of a live loop in step with this module's.

        A partial index bakes its predicate at creation, and `IF NOT EXISTS` then makes
        a later change to `_live` a no-op against a database that already has one: the
        constraint goes on enforcing the old predicate with nothing to say about it,
        which is how two live loops on one fault would arrive. Fingerprinting it turns
        an edit somebody made without knowing the index was baked into a rebuild.
        """
        fingerprint = hashlib.sha256(_live().encode()).hexdigest()[:16]
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_observe_schema (
                 name TEXT PRIMARY KEY, value TEXT NOT NULL)"""
        )
        row = self.db.execute(
            "SELECT value FROM tc_action_observe_schema WHERE name='live_index'"
        ).fetchone()
        if row is None or str(row[0]) != fingerprint:
            self.db.execute("DROP INDEX IF EXISTS tc_action_observe_live")
        self.db.execute("DROP INDEX IF EXISTS tc_action_observe_open")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS tc_action_observe_live"
            f" ON tc_action_observe_loops(incident_key, episode) WHERE {_live()}"
        )
        self.db.execute(
            """INSERT INTO tc_action_observe_schema(name, value) VALUES('live_index', ?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
            (fingerprint,),
        )

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Read rows by name without touching how the rest of the service reads them.

        The connection is shared, so its row factory is not ours to change; a cursor
        carries its own.
        """
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters)

    def open(self, incident_key: str, episode: int) -> Any | None:
        """The loop still being worked on, if there is one. Over is over either way."""
        return self._query(
            f"""SELECT * FROM tc_action_observe_loops
                WHERE incident_key=? AND episode=? AND {_live()}""",
            (incident_key, episode),
        ).fetchone()

    def start(self, loop: Mapping[str, Any]) -> Any:
        # fault_revision arrived after loops already existed, and a caller that has not
        # been taught the difference should still start one. Defaulted rather than
        # required: subject_hash falls back to the strict revision when it is empty,
        # which is exactly the old behaviour.
        loop = {"fault_revision": "", **loop}
        self.db.execute(
            """INSERT INTO tc_action_observe_loops(
                 loop_id,incident_key,episode,bdf,state,started_utc,updated_utc,rounds,
                 severity,evidence_revision,fault_revision,reads_available,reads_text,
                 status_json,facts_json,vast_text,vast_reports,attempts,observed_utc,code)
               VALUES(:loop_id,:incident_key,:episode,:bdf,'open',:now,:now,0,:severity,
                 :evidence_revision,:fault_revision,:reads_available,:reads_text,
                 :status_json,:facts_json,:vast_text,:vast_reports,:attempts,:observed_utc,:code)""",
            dict(loop),
        )
        self.db.commit()
        return self.open(str(loop["incident_key"]), int(loop["episode"]))

    def ask(self, loop_id: str, round: int, commands: tuple[str, ...], note: str, now: datetime) -> None:
        """Record a whole round at once; half a round would be asked about as if whole.

        Each read is stored exactly as asked. A read the parser admitted may be a whole
        script (up to MAX_READ_SCRIPT_CHARS); cutting it here would run a fragment
        nobody wrote on the host. Anything over the limit is refused, never shortened.
        """
        oversized = [c for c in commands if len(c) > MAX_READ_SCRIPT_CHARS]
        if oversized:
            raise ValueError(f"read of {len(oversized[0])} chars exceeds {MAX_READ_SCRIPT_CHARS}")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany(
                """INSERT OR IGNORE INTO tc_action_observe_reads(
                     loop_id,round,seq,command,note,asked_utc) VALUES(?,?,?,?,?,?)""",
                [
                    (loop_id, round, seq, command, note[:512], _text(now))
                    for seq, command in enumerate(commands, start=1)
                ],
            )
            self.db.execute(
                "UPDATE tc_action_observe_loops SET rounds=?,updated_utc=? WHERE loop_id=?",
                (round, _text(now), loop_id),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def queued(self, loop_id: str) -> Any | None:
        return self._query(
            """SELECT round, seq, command FROM tc_action_observe_reads
               WHERE loop_id=? AND output IS NULL ORDER BY round, seq LIMIT 1""",
            (loop_id,),
        ).fetchone()

    def waiting(self) -> list[tuple[str, str, int, str, int, int, str]]:
        """Every loop with a read still to run, the longest-waiting read first.

        Ordered by when each read was asked for rather than by when its loop began.
        A loop that keeps asking for more can then never starve another: its new
        rounds are asked for after the other fault's read, so the wait is bounded by
        the round already in flight rather than by how long the loop goes on.
        """
        return [
            (str(row[0]), str(row[1]), int(row[2]), str(row[3]), int(row[4]), int(row[5]), str(row[6]))
            for row in self._query(
                # One row per loop, chosen explicitly. Picking it with MIN() over two
                # columns leaves which row the other columns come from up to SQLite,
                # and the wrong one here runs a later read before an earlier one --
                # which nothing would notice except the transcript being wrong.
                f"""SELECT l.loop_id, l.incident_key, l.episode, l.bdf, r.round, r.seq,
                           r.command
                      FROM tc_action_observe_loops l
                     JOIN tc_action_observe_reads r ON r.loop_id=l.loop_id
                    WHERE {_live('l')} AND r.output IS NULL
                      AND r.rowid = (SELECT r2.rowid FROM tc_action_observe_reads r2
                                      WHERE r2.loop_id=l.loop_id AND r2.output IS NULL
                                      ORDER BY r2.round, r2.seq LIMIT 1)
                    ORDER BY r.asked_utc, r.round, r.seq""",
            ).fetchall()
        ]

    def answer(self, loop_id: str, round: int, seq: int, output: str, now: datetime) -> None:
        self.db.execute(
            """UPDATE tc_action_observe_reads SET output=?, ran_utc=?
               WHERE loop_id=? AND round=? AND seq=?""",
            (output, _text(now), loop_id, round, seq),
        )
        # This loop has had its turn; the next pass goes to whoever has waited longest.
        self.db.execute(
            "UPDATE tc_action_observe_loops SET updated_utc=? WHERE loop_id=?",
            (_text(now), loop_id),
        )
        self.db.commit()

    def abandon(self, loop_id: str, reason: str, now: datetime) -> None:
        """Terminalize whatever is still queued. A queued read must never outlive its loop."""
        self.db.execute(
            """UPDATE tc_action_observe_reads SET output=?, ran_utc=?
               WHERE loop_id=? AND output IS NULL""",
            (f"(not run: {reason})", _text(now), loop_id),
        )
        self.db.commit()

    def conclude(self, loop_id: str, now: datetime) -> None:
        """No more looking; the next ask is the last one."""
        self.db.execute(
            f"UPDATE tc_action_observe_loops SET state='{FINAL}',updated_utc=? WHERE loop_id=?",
            (_text(now), loop_id),
        )
        self.db.commit()

    def _mark(self, loop_id: str, now: datetime) -> None:
        self.db.execute(
            "UPDATE tc_action_observe_loops SET updated_utc=? WHERE loop_id=?",
            (_text(now), loop_id),
        )
        self.db.commit()

    def close(self, loop_id: str, state: str = CLOSED, now: datetime | None = None) -> None:
        """End a loop. ``spent`` means it looked all it was allowed and concluded nothing.

        The row stays. Without it, giving up would be indistinguishable from never
        having looked, and the next pass five minutes later would open another loop and
        spend another six rounds on the same fault, for as long as it stayed open.
        """
        self.db.execute("DELETE FROM tc_action_observe_reads WHERE loop_id=?", (loop_id,))
        # `updated_utc` alongside the state, so the column means one thing in every row
        # rather than two things depending on which state the row is in. `spent_since`
        # reads it on a loop that has given up, and without this the cooldown ran from
        # the last thing that happened BEFORE it gave up -- short by however long the
        # final ask took.
        self.db.execute(
            "UPDATE tc_action_observe_loops SET state=?, updated_utc=? WHERE loop_id=?",
            (state, _text(now) if now is not None else _text(datetime.now(timezone.utc)), loop_id),
        )
        self.db.commit()

    def abandoned(self, untouched_since: datetime) -> list[tuple[str, str, int]]:
        """Loops nothing has come back to for a long time.

        Measured from the last thing that happened to the loop, not from when it
        started, and the difference is the whole point. The investigation's own
        deadline is enforced where the question is asked: it ends the looking and puts
        the loop into its final ask. Sweeping on age instead deleted the loop in the
        very pass that concluded it -- the concluding answer was never collected, and
        the next pass opened a fresh loop with six new rounds, so the deadline started
        the next investigation rather than ending this one.

        What is left for this to collect is a loop nothing will ever return to: its
        fault recovered, so the ask is never reached again and nothing is queued for
        the read phase to notice.
        """
        return [
            (str(row[0]), str(row[1]), int(row[2]))
            for row in self._query(
                f"""SELECT loop_id, incident_key, episode FROM tc_action_observe_loops
                    WHERE {_live()} AND updated_utc<?""",
                (_text(untouched_since),),
            ).fetchall()
        ]

    def live_loop(self) -> Any | None:
        """The one loop being worked on, if there is one."""
        return self._query(
            f"SELECT * FROM tc_action_observe_loops WHERE {_live()}"
            " ORDER BY started_utc LIMIT 1"
        ).fetchone()

    def live(self) -> bool:
        """Whether any loop is being worked on. One investigation at a time.

        There are dozens of open incidents on this machine and one budget. Without
        this, widening what may be investigated would start an investigation for every
        one of them at once and spend the day's allowance before any of them finished.
        """
        return self._query(
            f"SELECT 1 FROM tc_action_observe_loops WHERE {_live()} LIMIT 1"
        ).fetchone() is not None

    def seen(self, incident_key: str, episode: int) -> bool:
        """Whether this fault has already had its look, however that look ended."""
        return self._query(
            """SELECT 1 FROM tc_action_observe_loops
               WHERE incident_key=? AND episode=? LIMIT 1""",
            (incident_key, episode),
        ).fetchone() is not None

    def looked_at_under(self, revision: str, since: datetime) -> set[str]:
        """Incidents investigated under this machine fault state since ``since``."""
        if not revision:
            return set()
        return {
            str(row[0]) for row in self._query(
                """SELECT DISTINCT incident_key FROM tc_action_observe_loops
                   WHERE fault_revision=? AND started_utc>=?""",
                (revision, _text(since)),
            ).fetchall()
        }

    def started_since(self, since: datetime) -> int:
        """How many investigations were started since ``since``."""
        return int(self._query(
            "SELECT count(*) FROM tc_action_observe_loops WHERE started_utc>=?",
            (_text(since),),
        ).fetchone()[0])

    def spent_since(self, incident_key: str, episode: int, since: datetime) -> bool:
        """Whether looking at this fault was already given up on, recently."""
        row = self._query(
            # The complement of `_live`, deliberately: this asks the opposite question
            # -- was looking at this given up on, recently.
            """SELECT 1 FROM tc_action_observe_loops
               WHERE incident_key=? AND episode=? AND state='spent' AND updated_utc>=?
               LIMIT 1""",
            (incident_key, episode, _text(since)),
        ).fetchone()
        return row is not None

    def transcript(self, loop_id: str) -> tuple[ObserveRound, ...]:
        rows = self._query(
            """SELECT round, note, command, output FROM tc_action_observe_reads
               WHERE loop_id=? AND output IS NOT NULL ORDER BY round, seq""",
            (loop_id,),
        ).fetchall()
        rounds: dict[int, list[Any]] = {}
        notes: dict[int, str] = {}
        for number, note, command, output in rows:
            rounds.setdefault(int(number), []).append((str(command), str(output)))
            notes.setdefault(int(number), str(note or ""))
        return tuple(
            ObserveRound(note=notes[number], results=tuple(results))
            for number, results in sorted(rounds.items())
        )


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

    def pending(self, prefix: str) -> list[tuple[str, datetime]]:
        """Everything scheduled under one prefix, oldest first."""
        rows = self.db.execute(
            "SELECT name, due_utc FROM tc_action_schedule WHERE name LIKE ? ESCAPE '\\'"
            " ORDER BY due_utc",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
        outstanding = []
        for name, due in rows:
            try:
                outstanding.append((str(name), _parse(str(due))))
            except ValueError:
                self.clear(str(name))
        return outstanding

    def forget(self, prefix: str) -> None:
        self.db.execute(
            "DELETE FROM tc_action_schedule WHERE name LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )
        self.db.commit()


class Conversations:
    """A conversation with the operator that outlives one model turn.

    The chat could only answer in one turn, so "can't you find it on the machine?" got
    "yes, I need the compose config" -- and then nothing, because nothing ran what it
    named. Now a reply may ask for reads; they run under the read-only profile, and
    their output goes back into the same thread as the next turn. This keeps where
    that exchange is, so a restart between rounds resumes it rather than dropping the
    operator's question on the floor.

    `root` is the first ticket of the exchange and names it for its whole life;
    `ticket` is the turn currently outstanding.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_conversations (
                 root TEXT PRIMARY KEY,
                 ticket TEXT NOT NULL UNIQUE,
                 incident_key TEXT NOT NULL,
                 episode INTEGER NOT NULL,
                 bdf TEXT NOT NULL,
                 subject_hash TEXT NOT NULL,
                 investigation_id TEXT NOT NULL,
                 sender_id INTEGER NOT NULL,
                 question TEXT NOT NULL,
                 round INTEGER NOT NULL DEFAULT 0,
                 created_utc TEXT NOT NULL,
                 updated_utc TEXT NOT NULL
               )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_conversation_reads (
                 root TEXT NOT NULL,
                 round INTEGER NOT NULL,
                 seq INTEGER NOT NULL,
                 command TEXT NOT NULL,
                 output TEXT,
                 asked_utc TEXT NOT NULL,
                 ran_utc TEXT,
                 PRIMARY KEY (root, round, seq)
               )"""
        )
        self.db.commit()

    def start(
        self, ticket: str, *, incident_key: str, episode: int, bdf: str,
        subject_hash: str, investigation_id: str, sender_id: int, question: str,
        now: datetime,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO tc_action_conversations(
                 root, ticket, incident_key, episode, bdf, subject_hash, investigation_id,
                 sender_id, question, round, created_utc, updated_utc)
               VALUES(?,?,?,?,?,?,?,?,?,0,?,?)""",
            (ticket, ticket, incident_key, int(episode), bdf, subject_hash,
             investigation_id, int(sender_id), question[:MAX_CONVERSATION_QUESTION_CHARS],
             _text(now), _text(now)),
        )
        self.db.commit()

    def by_ticket(self, ticket: str) -> Any | None:
        cursor = self.db.execute(
            "SELECT * FROM tc_action_conversations WHERE ticket=?", (ticket,)
        )
        row = cursor.fetchone()
        return None if row is None else dict(zip([d[0] for d in cursor.description], row))

    def by_root(self, root: str) -> Any | None:
        cursor = self.db.execute("SELECT * FROM tc_action_conversations WHERE root=?", (root,))
        row = cursor.fetchone()
        return None if row is None else dict(zip([d[0] for d in cursor.description], row))

    def ask(self, root: str, round: int, commands: tuple[str, ...], now: datetime) -> None:
        """Queue one round of reads, all of it in one write."""
        self.db.executemany(
            """INSERT OR IGNORE INTO tc_action_conversation_reads(
                 root, round, seq, command, asked_utc) VALUES(?,?,?,?,?)""",
            [(root, round, seq, command, _text(now))
             for seq, command in enumerate(commands, start=1)],
        )
        self.db.execute(
            "UPDATE tc_action_conversations SET round=?, updated_utc=? WHERE root=?",
            (round, _text(now), root),
        )
        self.db.commit()

    def waiting(self) -> list[tuple[str, int, int, str]]:
        """Reads not yet run, oldest conversation first."""
        rows = self.db.execute(
            """SELECT r.root, r.round, r.seq, r.command
                 FROM tc_action_conversation_reads r
                 JOIN tc_action_conversations c ON c.root = r.root
                WHERE r.ran_utc IS NULL
             ORDER BY c.created_utc, r.round, r.seq"""
        ).fetchall()
        return [(str(a), int(b), int(c), str(d)) for a, b, c, d in rows]

    def answer(self, root: str, round: int, seq: int, output: str, now: datetime) -> None:
        self.db.execute(
            """UPDATE tc_action_conversation_reads SET output=?, ran_utc=?
                WHERE root=? AND round=? AND seq=?""",
            (output, _text(now), root, round, seq),
        )
        self.db.commit()

    def round_done(self, root: str, round: int) -> bool:
        row = self.db.execute(
            """SELECT COUNT(*) FROM tc_action_conversation_reads
                WHERE root=? AND round=? AND ran_utc IS NULL""",
            (root, round),
        ).fetchone()
        return int(row[0]) == 0

    def results(self, root: str, round: int) -> tuple[tuple[str, str], ...]:
        rows = self.db.execute(
            """SELECT command, COALESCE(output, '') FROM tc_action_conversation_reads
                WHERE root=? AND round=? ORDER BY seq""",
            (root, round),
        ).fetchall()
        return tuple((str(command), str(output)) for command, output in rows)

    def advance(self, root: str, ticket: str, now: datetime) -> None:
        """The next turn of this exchange is now the one outstanding."""
        self.db.execute(
            "UPDATE tc_action_conversations SET ticket=?, updated_utc=? WHERE root=?",
            (ticket, _text(now), root),
        )
        self.db.commit()

    def end(self, root: str) -> None:
        row = self.by_root(root)
        if row is not None:
            # Keep one small generation marker after the live row disappears. Late
            # model results and a replayed inbox input must not reopen this request.
            self.db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
                (event_id,root,ticket,subject,episode,kind,text,created_utc)
                VALUES(?,?,?,?,?,?,?,?)""", (f"ended:{root}", root,
                str(row["ticket"]), str(row["incident_key"]), int(row["episode"]),
                "request-end", "completed", str(row["updated_utc"])))
        self.db.execute("DELETE FROM tc_action_conversation_reads WHERE root=?", (root,))
        self.db.execute("DELETE FROM tc_action_conversations WHERE root=?", (root,))
        if row is not None:
            self.db.executemany("DELETE FROM tc_action_notes WHERE name=?", [
                (f"{prefix}:{root}",) for prefix in
                ("defer", "published", "input-nonce", "finding", "conclusion",
                 "progress", "revisions", "reviewed", "corrections")
            ])
            # Input-to-root maps protect retries whose subject changed after
            # acceptance. Keep them for the same bounded late-reply window.
            self.db.execute("DELETE FROM tc_action_dialogue WHERE kind='request-end' AND created_utc<?",
                            (_text(_parse(str(row["updated_utc"])) - timedelta(days=7)),))
            self.db.execute("DELETE FROM tc_action_notes WHERE name LIKE 'input-root:%' "
                            "AND set_utc<?",
                            (_text(_parse(str(row["updated_utc"])) - timedelta(days=7)),))
        self.db.commit()

    def stale(self, before: datetime) -> list[str]:
        rows = self.db.execute(
            "SELECT root FROM tc_action_conversations WHERE updated_utc < ?", (_text(before),)
        ).fetchall()
        return [str(row[0]) for row in rows]


class Notes:
    """Text a person or a plan left behind that is too long for `Controls`.

    `Controls` keeps 128 characters, which is right for a flag and wrong for what the
    operator actually asked, or for the way to undo a plan and the checks that follow it.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_notes (
                 name TEXT PRIMARY KEY,
                 value TEXT NOT NULL,
                 set_utc TEXT NOT NULL
               )"""
        )
        self.db.commit()

    def set(self, name: str, value: str, now: datetime) -> None:
        self.db.execute(
            """INSERT INTO tc_action_notes(name, value, set_utc) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc""",
            (name[:160], value[:MAX_NOTE_CHARS], _text(now)),
        )
        self.db.commit()

    def get(self, name: str) -> str:
        row = self.db.execute(
            "SELECT value FROM tc_action_notes WHERE name=?", (name[:160],)
        ).fetchone()
        return "" if row is None else str(row[0])

    def clear(self, name: str) -> None:
        self.db.execute("DELETE FROM tc_action_notes WHERE name=?", (name[:160],))
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
        observer: Any | None = None,
        actor: Any | None = None,
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
        # Everything said to the group is written down: the Bot API cannot read back
        # what the bot said, and "what did the agent tell them?" needs an answer.
        self.telegram = OutboxRecorder(telegram, actions_db, clock)
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
        # How the operator's own words reach the incident's thread, when one exists.
        self.conversation: Any | None = None
        # Who reviews a plan before a person is asked to approve it.
        self.reviewer: Any | None = None
        self.reader = reader
        # How a model-authored read reaches the host: the read-only profile, and the
        # only thing between the text it wrote and this machine.
        self.observer = observer
        # How the service carries out the monitoring work that is its own to do.
        self.actor = actor
        self.observations = Observations(actions_db)
        self.conversations = Conversations(actions_db)
        self.notes = Notes(actions_db)
        self.actions_db.execute("""CREATE TABLE IF NOT EXISTS tc_action_dialogue (
            event_id TEXT PRIMARY KEY, root TEXT NOT NULL, ticket TEXT NOT NULL,
            subject TEXT NOT NULL, episode INTEGER NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, created_utc TEXT NOT NULL, delivered_utc TEXT)""")
        self.actions_db.commit()
        self.poll_timeout = poll_timeout
        self.report = report or (lambda line: print(line, file=sys.stderr, flush=True))
        self.signatures = _gpu_signatures()
        self.schedule = Schedule(actions_db)
        self._next_target_check_at: datetime | None = None
        self._next_reconcile_at: datetime | None = None
        self._retry_warned: set[str] = set()
        self._cached_question_context = ""
        # Who asked, per outstanding conversation. Only used to attribute a steer; a
        # restart loses it, and a steer then carries out under nobody, which is the
        # right way round -- what it does is recorded either way.
        self._conversation_sender: dict[str, int] = {}
        # Which phases are currently failing, with what, since when, and how often.
        self._failing: dict[str, tuple[str, datetime, int]] = {}
        # The fault most recently put in front of them, so that "do it" said
        # straight after a report has something to refer to.
        self._last_reported: str = ""
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
        self._guard(self._reconcile_legacy_plans)
        # Conversation rows are the durable acceptance record. A crash can occur
        # between publishing a spool request and installing its in-memory timer.
        for root, ticket, updated in self.actions_db.execute(
            "SELECT root,ticket,updated_utc FROM tc_action_conversations"
        ).fetchall():
            if (self.notes.get(f"defer:{root}")
                    or self.notes.get(f"review:{root}")
                    or (root == ticket and not self.notes.get(f"published:{root}")
                        and self.notes.get(f"input-nonce:{root}") is not None)):
                continue
            if self.schedule.get(f"conversation:{ticket}")[0] is None:
                self.schedule.set(f"conversation:{ticket}",
                                  _parse(str(updated)) + CONVERSATION_WAIT)
        for cycle in self.cycles.resumable():
            self._guard(self._resume, cycle)
        self._guard(self._reconcile_unknown, True)
        self._guard(self._deliver)

    def tick(self) -> None:
        self._guard(self._poll)
        self._guard(self._reconcile_legacy_plans)
        self._guard(self._handle_inputs)
        self._guard(self._resume_deferred_conversations)
        self._guard(self._collect_conversations)
        self._guard(self._collect_reviews)
        self._guard(self._reconcile_unknown, False)
        self._guard(self._advance)
        self._guard(self._expire_reviews)
        self._guard(self._investigate_open)
        self._guard(self._review)
        self._guard(self._deliver)
        # Last: a read can wait a minute on a busy host, and nothing above it should.
        self._guard(self._observe)

    def _report_failure(self, phase: str, category: str) -> None:
        """Say a failure once, then say how it is going -- never the same line forever.

        Some failures are a blip and some are an outage, and they look identical one
        line at a time. Polling Telegram fails on one or two per cent of its long polls
        and loses nothing when it does -- the cursor only advances after an input is
        stored, so the update comes back on the next pass -- but at a tick every
        fifteen seconds that is scores of identical lines a day. The cost is not the
        noise, it is that a real outage looks exactly like the noise it is buried in.
        """
        streak = self._failing.get(phase)
        now = self.clock()
        if streak is None or streak[0] != category:
            self._failing[phase] = (category, now, 1)
            self.report(
                f'{{"operation":"actions","phase":"{phase}",'
                f'"status":"failed","category":"{category}"}}'
            )
            return
        _category, since, count = streak
        self._failing[phase] = (category, since, count + 1)
        if now - since >= PHASE_FAILURE_REMINDER * (1 + (count // PHASE_FAILURE_RUN)):
            self.report(
                f'{{"operation":"actions","phase":"{phase}","status":"failing",'
                f'"category":"{category}","count":{count + 1},'
                f'"since":"{_text(since)}"}}'
            )

    def _report_recovery(self, phase: str) -> None:
        """A phase that was failing and is not any more is worth exactly one line."""
        streak = self._failing.pop(phase, None)
        if streak is None:
            return
        category, since, count = streak
        self.report(
            f'{{"operation":"actions","phase":"{phase}","status":"recovered",'
            f'"category":"{category}","count":{count},"since":"{_text(since)}"}}'
        )

    def _observe(self) -> None:
        """Run the reads the models asked for, one per loop, and keep what came back.

        Last in the tick, deliberately. A read waits up to a minute on a host that may
        be busy, and everything else this service does -- answering a person, taking an
        approval, delivering an outcome -- is behind it in the queue. At the end of the
        pass, that costs a loop some seconds and costs nobody else anything.
        """
        now = self.clock()
        started = time.monotonic()
        # A person is waiting on these, so they go before any incident's looking.
        if self._observe_for_conversations(now, started):
            return
        # Swept here rather than in the ask, because the ask is only reached while the
        # fault is still eligible for a proposal, and one that recovers mid-loop is
        # exactly the case that leaves a loop behind.
        for loop_id, incident_key, episode in self.observations.abandoned(
            now - OBSERVE_LOOP_DEADLINE
        ):
            self.observations.abandon(loop_id, "nothing came back to this", now)
            # Closed, not spent: nothing was given up on here, so looking again when
            # this fault next opens must not be held back.
            self.observations.close(loop_id, now=now)
        if self.observer is None:
            # Reads were queued and then the way to run them went away -- a
            # configuration change, or a service that came back without one. Waiting
            # for the abandonment sweep would leave the fault unattended for ninety
            # minutes over something that will not resolve itself.
            for loop_id, _key, _episode, _bdf, _round, _seq, _command in self.observations.waiting():
                self.observations.abandon(loop_id, "I have no way to look at the host", now)
                self.observations.conclude(loop_id, now)
            return
        for loop_id, incident_key, episode, bdf, round, seq, command in self.observations.waiting():
            if not self._incident_open(incident_key):
                # Whatever it wanted to know, it is about a fault that is over.
                self.observations.abandon(loop_id, "the fault recovered", now)
                self.observations.close(loop_id, now=now)
                continue
            if incident_key == REVIEW_KEY and episode != self._review_episode():
                # A look from an earlier request, left behind by one that replaced it.
                self.observations.abandon(loop_id, "a later look replaced this one", now)
                self.observations.close(loop_id, now=now)
                continue
            # Checked here as well as in the ask, because a queued read must never
            # outlive the reason it was queued: the deadline, a person taking over, or
            # the fault simply being gone.
            loop = self.observations.open(incident_key, episode)
            if loop is None:
                continue
            if now - _parse(str(loop["started_utc"])) > OBSERVE_LOOP_DEADLINE:
                self.observations.abandon(loop_id, "the loop ran out of time", now)
                self.observations.conclude(loop_id, now)
                continue
            if self._override_for(bdf, now) is not None:
                self.observations.abandon(loop_id, "you asked me to get on with it", now)
                self.observations.conclude(loop_id, now)
                continue
            if self.controls.held(bdf):
                # A hold says to leave this one alone, and running commands on it is
                # not leaving it alone. Not abandoned: a hold is temporary. Pausing is
                # different -- it stops this service acting, and looking is not acting.
                continue
            result = self.observer.observe(command, subject=f"incident:{incident_key}")
            # The tail, because that is the end the prompt keeps too. Storing the head
            # and rendering the tail showed the model the middle of a long read and
            # told it the earlier part was dropped -- so it asked again for an end it
            # could never be given, and spent a round doing it.
            self.observations.answer(
                loop_id, round, seq, result.text()[-MAX_OBSERVE_STORED:], now
            )
            # One read per loop, and as many loops as the slice allows. One read per
            # PASS meant nine faults being looked at at once each progressed nine times
            # slower, while every loop's deadline stayed the same ninety minutes -- so
            # under load a loop ran out of time having done a fraction of its looking.
            # A slow read still ends the pass on its own, so the longest anything else
            # waits is one read, exactly as before.
            if time.monotonic() - started > OBSERVE_TICK_BUDGET:
                return

    def _observe_for_conversations(self, now: datetime, started: float) -> bool:
        """Run the reads a conversation asked for. True when the pass's budget is spent."""
        waiting = self.conversations.waiting()
        if not waiting:
            return False
        if self.observer is None:
            for root in {item[0] for item in waiting}:
                self._queue_terminal(root,
                    "I cannot finish the requested host reads because the read channel is unavailable. Ask for a fresh look when it is restored.", now)
                self.conversations.end(root)
            return False
        for root, round, seq, command in waiting:
            result = self.observer.observe(command, subject=f"conversation:{root[:24]}")
            self.conversations.answer(
                root, round, seq, result.text()[-MAX_OBSERVE_STORED:], self.clock()
            )
            if self.conversations.round_done(root, round):
                self._continue_conversation(root, round, self.clock())
            if time.monotonic() - started > CONVERSATION_TICK_BUDGET:
                return True
        return False

    def _review(self) -> None:
        """Look the machine over because a person asked, and say what came of it.

        Driven through exactly the same loop as a diagnosis -- the same freezing, the
        same rounds, the same deadline -- because a look somebody asked for deserves
        the same care as one a check asked for. It cannot act on its own, but a concrete
        command that needs a person must become an approval request rather than prose
        that merely says what somebody could run.
        """
        requested = self.controls.get(REVIEW_REQUEST)
        if requested is None:
            return
        # Not gated on `paused`. Pausing says "keep watching and reporting, act on
        # nothing", and a review is watching and reporting: it has no path to an
        # action at all. Refusing it while paused left the request sitting silently
        # until somebody resumed, with nothing said about why.
        now = self.clock()
        self.notes.set(f"last-request:{REVIEW_REQUEST}", str(requested), now)
        if self._chat_owns(MACHINE_SUBJECT, 1):
            return
        if not self.schedule.due("review", now):
            return
        self.schedule.set("review", now + STATUS_RETRY_INTERVAL)
        try:
            status = self.adapter.status()
        except Exception:
            # The target is unreachable; say so rather than leaving them waiting.
            self.controls.clear(REVIEW_REQUEST)
            self._send(
                "I could not look the machine over: the target did not answer. "
                "Everything I last knew is in /status."
            )
            return
        # Each request is its own investigation. Keyed on a constant, every review this
        # machine is ever asked for shared one budget -- and that budget is a lifetime
        # count with no window, so the second or third would have exhausted it and
        # every review after that would have been refused for good, with nothing an
        # operator could do about it. The moment it was asked for is what distinguishes
        # them, and it holds still for as long as the request does.
        diagnosis = self._diagnose(
            "", REVIEW_KEY, _request_episode(str(requested)), status, now, requested=True
        )
        if diagnosis.pending:
            return  # Still looking; it comes back on a later pass.
        self.controls.clear(REVIEW_REQUEST)
        self.notes.clear(f"request:{REVIEW_REQUEST}")
        self.notes.set(f"review-generation:{REVIEW_KEY}", uuid.uuid4().hex, now)
        self.schedule.clear("review")
        if diagnosis.finding is None:
            reason = diagnosis.reason or "no answer"
            # A budget that is spent is not the same as a look that found nothing, and
            # reporting it as one leaves a person waiting for an answer that is never
            # coming. Nothing here can fall back to the rule: it knows one fault, and
            # this is a look at the whole machine.
            if any(word in reason for word in ("-cap", "backstop")):
                self._send(
                    "I could not look the machine over: the investigator's allowance "
                    f"for this is spent ({reason}). Everything else still works, and "
                    "it will answer again once that clears."
                )
            else:
                self._send(f"I looked the machine over and reached no conclusion ({reason}).")
            return
        if self._ask_about(
            diagnosis, "", REVIEW_KEY, _request_episode(str(requested)), now
        ):
            return
        self._send("You asked me to look the machine over.\n" + describe(diagnosis))

    def _loop_ready(self, incident_key: str, episode: int) -> bool:
        """A loop that has looked at everything it asked for and wants to ask again."""
        loop = self.observations.open(incident_key, episode)
        if loop is None or int(loop["rounds"]) == 0:
            return False
        return self.observations.queued(str(loop["loop_id"])) is None

    def _review_episode(self) -> int | None:
        """Which requested look is the current one, if any."""
        requested = self.controls.get(REVIEW_REQUEST)
        return None if requested is None else _request_episode(str(requested))

    def _incident_open(self, incident_key: str) -> bool:
        if incident_key == REVIEW_KEY:
            # A review is not an incident and has no lifecycle of its own; its loop is
            # bounded by the same deadline as any other and ends when it concludes.
            return self.controls.get(REVIEW_REQUEST) is not None
        try:
            row = self.state_db.execute(
                "SELECT status FROM incidents WHERE dedup_key=?", (incident_key,)
            ).fetchone()
        except sqlite3.Error:
            return True  # Unreadable state is not evidence that a fault is over.
        return row is not None and str(row[0]) in ("open", "recovery_pending")

    def _guard(self, phase: Callable[..., Any], *arguments: Any) -> None:
        # One phase's failure must not skip the others or end the service. Every
        # authority check raises before its side effect, so continuing grants nothing.
        try:
            phase(*arguments)
        except Exception as error:
            self.phase_failures += 1
            self._report_failure(phase.__name__, type(error).__name__)
        else:
            self._report_recovery(phase.__name__)
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
                       "the detail of it. Just ask.",
            )
            return
        if cycle.stage == "executing" and cycle.command and cycle.result in ("succeeded", "failed"):
            # The command returned and its outcome was recorded; only the checks
            # afterwards were interrupted. Report what is known, not a guess.
            ran = cycle.result == "succeeded"
            unfinished = "I was interrupted during the checks afterwards, so they did not complete."
            self._finish(
                cycle, cycle.result, cycle.detail or "",
                notice=(f"Request {cycle.proposal_id}: execution completed. " if ran
                        else f"Request {cycle.proposal_id}: execution failed and may have changed something. ")
                        + unfinished,
                audit=True,
            )
            # The thread that proposed it still needs to hear how it went.
            self._tell_conversation_about(
                cycle, SimpleNamespace(ok=ran, detail=cycle.detail or ""), unfinished)
            return
        if cycle.stage == "executing" and cycle.command:
            # A model-proposed command has no broker attempt to consult. The stage is
            # written before the command is dispatched, so an interruption here may have
            # come after it ran: saying it never did would be a guess, and a wrong one
            # sends a person to re-run a change that already happened.
            self._finish(
                cycle, "unknown", "interrupted after approval; the command may have run",
                notice=f"I was interrupted while running approved request {cycle.proposal_id}, so I "
                       f"cannot say whether it ran. I will not run "
                       "it again from that approval. Ask me to check the machine.",
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
        elif cycle.stage == "awaiting_answer":
            self._recheck_request(cycle, now)
        elif cycle.stage in ("executing", "reporting"):
            if self.schedule.due("execute", now):
                self._resume(cycle)

    def _recheck_request(self, cycle: Cycle, now: datetime) -> None:
        """A waiting request is withdrawn only when the machine stops matching it."""
        if cycle.command:
            # A model-proposed command is not a dcgm-exporter handover proposal, so
            # build_proposal() cannot revalidate it. Treating it as one withdrew a
            # perfectly valid generic button on the next recheck. Its boundary is the
            # short, exact approval lifetime instead.
            if cycle.delivered_utc and now >= _parse(cycle.delivered_utc) + GENERIC_APPROVAL_LIFETIME:
                self._finish(
                    cycle, "expired", "the generic approval request expired",
                    notice="That approval request expired after thirty minutes, so nothing "
                           "ran. Ask me again if you still want it.",
                )
            return
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

    def _expired(self, cycle: Cycle, now: datetime) -> None:
        """The press was late. That is not the machine changing, and must not say so.

        An approval is deliberately short-lived and single-use; that property is worth
        keeping. What is not worth keeping is answering a late tap with "the machine
        has changed", which is false, unactionable, and points at hardware.
        """
        self._finish(
            cycle, "expired", "the approval arrived after the proposal expired",
            notice="That tap arrived after the request had expired -- approvals are "
                   "good for five minutes and single-use, so nothing ran. Nothing is "
                   "wrong with the machine beyond the fault I already described. I "
                   "will ask again shortly; the next button will work.",
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

    def _loop_for(
        self, bdf: str, incident_key: str, episode: int, status: Any, now: datetime,
        requested: bool = False, code: str = HANDOVER_CODE,
    ) -> Any:
        """The open read loop for this fault, started if there is none.

        Everything the question rests on is frozen in here at the start. `subject_hash`
        folds in the evidence revision and which diagnostics answered, and `_diagnose`
        recomputes both from fresh reads on every pass -- so without freezing, a
        container restarting between collecting an answer and persisting its round
        moves the hash, and the loop opens a new episode instead of being given back
        the answer it just collected.
        """
        existing = self.observations.open(incident_key, episode)
        if existing is not None:
            return existing
        # Looking at this was given up on recently. Open the next one already finished
        # looking, so the fault is still diagnosed and the rounds are not spent again.
        # A person asking is never held back by it: the cooldown exists to stop this
        # service spending its rounds over and over on a fault it could not work out,
        # and answering somebody with a look that did not look is not that.
        exhausted = not requested and self.observations.spent_since(
            incident_key, episode, now - OBSERVE_LOOP_COOLDOWN
        )
        try:
            facts = self.state_db.execute(
                """SELECT severity, first_occurrence_utc, last_occurrence_utc, occurrence_count
                   FROM incidents WHERE dedup_key=?""",
                (incident_key,),
            ).fetchone()
        except sqlite3.Error:
            facts = None
        reads, available = "", ()
        if self.reader is not None and getattr(self.diagnoser, "uses_reads", True):
            answers = self.reader.read_all(subject=f"incident:{incident_key}")
            reads, available = summarize(answers), answered(answers)
        vast, vast_reports = "", 0
        latest = _latest_vast(self.state_db)
        if latest is not None:
            vast = _vast_text(*latest, now=now)
            reports = latest[1].get("reports")
            vast_reports = len(reports) if isinstance(reports, list) else 0
        started = self.observations.start({
            "loop_id": str(uuid.uuid4()),
            "incident_key": incident_key,
            "episode": episode,
            "bdf": bdf,
            "now": _text(now),
            "severity": str(facts[0]) if facts else "error",
            "evidence_revision": evidence_revision(status, bdf),
            # Narrower, and what the question is named by. See fault_revision.
            "fault_revision": fault_revision(status, bdf),
            "reads_available": json.dumps(list(available)),
            "reads_text": reads,
            "status_json": json.dumps(_status_document(status), default=str),
            "facts_json": json.dumps({
                "first_occurrence_utc": facts[1] if facts else None,
                "last_occurrence_utc": facts[2] if facts else None,
                "occurrence_count": facts[3] if facts else None,
            }, default=str),
            "code": code,
            "vast_text": vast,
            "vast_reports": vast_reports,
            # Frozen with the rest of it. Read live, this moved when a cycle from
            # before the loop finished -- and it feeds both the subject hash and the
            # investigation id, so the loop would lose its collected answer to a new
            # episode and hand itself a fresh budget at the same moment.
            "attempts": sum(
                1 for cycle in self.cycles.episode(incident_key, episode)
                if cycle.result in EXECUTED_RESULTS
            ),
            "observed_utc": _text(status.observed_at),
        })
        if exhausted:
            self.observations.conclude(str(started["loop_id"]), now)
            return self.observations.open(incident_key, episode)
        return started

    def _request_for(
        self, loop: Any, incident_key: str, final: bool, requested: bool = False
    ) -> DiagnosisRequest:
        """The question, rebuilt from what was frozen into the loop."""
        return DiagnosisRequest(
            incident_key=incident_key,
            episode=int(loop["episode"]),
            severity=str(loop["severity"]),
            code=str(loop["code"]),
            bdf=str(loop["bdf"]) or None,
            observed_at=_parse(str(loop["observed_utc"])),
            status_document=json.loads(str(loop["status_json"])),
            reads=str(loop["reads_text"]),
            incident_facts=json.loads(str(loop["facts_json"])),
            evidence_revision=str(loop["evidence_revision"]),
            fault_revision=str(loop["fault_revision"] or ""),
            reads_available=tuple(json.loads(str(loop["reads_available"]))),
            attempts=int(loop["attempts"]),
            vast=str(loop["vast_text"]),
            vast_reports=int(loop["vast_reports"]),
            loop_id=str(loop["loop_id"]),
            observe_rounds=self.observations.transcript(str(loop["loop_id"])),
            # Never offer a read that will not be granted: on the last ask this is
            # false, so the contract does not invite one at all.
            observation_available=self.observer is not None and not final,
            final_round=final,
            requested=requested,
            operator_request=self._operator_request(incident_key) if requested else "",
        )

    def _operator_request(self, incident_key: str) -> str:
        """What the operator said when they asked for this look, if they said anything."""
        if incident_key == REVIEW_KEY:
            return self.notes.get(f"request:{REVIEW_REQUEST}")
        return self.notes.get(f"request:review:{incident_key}")

    def _diagnose(
        self, bdf: str, incident_key: str, episode: int, status: Any, now: datetime,
        requested: bool = False, code: str = HANDOVER_CODE,
    ) -> Diagnosis:
        """Ask what is wrong, letting it look at the host first if it needs to.

        The order below is the design. Every early return sits underneath the deadline,
        because a read left queued by a failure that repeats would otherwise mean an
        open incident with neither the model's answer nor the rule's, for ever.
        """
        loop = self._loop_for(
            bdf, incident_key, episode, status, now,
            requested or self._review_for(bdf, now) is not None, code,
        )
        loop_id = str(loop["loop_id"])
        final = str(loop["state"]) == "final"
        # 1. Out of time or out of rounds: stop looking, whatever is still queued.
        expired = now - _parse(str(loop["started_utc"])) > OBSERVE_LOOP_DEADLINE
        if not final and (expired or int(loop["rounds"]) >= MAX_OBSERVE_ROUNDS):
            self.observations.abandon(
                loop_id, "the loop ran out of time" if expired else "no reads left", now
            )
            self.observations.conclude(loop_id, now)
            final = True
        # 2. A person asking for this to be acted on ends the looking now: an override
        #    lapses in an hour and a loop may run for most of one, so waiting it out
        #    would swallow their request entirely.
        elif self._override_for(bdf, now) is not None:
            # Whether or not a read happens to be queued this instant. Between rounds
            # there is nothing queued, and requiring one meant a person saying "get on
            # with it" at that moment was simply ignored and the looking carried on.
            self.observations.abandon(loop_id, "you asked me to get on with it", now)
            self.observations.conclude(loop_id, now)
            final = True
        # 3. Anything still to look at: look first, conclude later.
        elif self.observations.queued(loop_id) is not None:
            return Diagnosis(None, MODEL, reason="looking at the host", pending=True)
        request = self._request_for(loop, incident_key, final, requested)
        diagnosis = self.diagnoser.diagnose(request)
        waited = f"diagnosis:{request.subject_hash()[:32]}"
        if diagnosis.reads is not None:
            # It wants to look. Persist the whole round before anything runs, so a
            # crash cannot leave it asked about half of what it wanted to see.
            self.schedule.clear(waited)
            if final or self.observer is None:
                # Its last word was a request we cannot grant. The rule answers, and
                # what it wanted is kept so a person can read it.
                self.observations.close(loop_id, SPENT, now)
                return replace(
                    self.fallback.diagnose(request),
                    reason="it kept asking to look after I ran out of reads",
                    raw_text=diagnosis.raw_text,
                )
            round = int(loop["rounds"]) + 1
            self.observations.ask(
                loop_id, round, diagnosis.reads.commands, diagnosis.reads.note, now,
            )
            if round == 1:
                # Once, when it starts looking. A loop may run for an hour and a half
                # and said nothing at all until it concluded, so an agent that was
                # working looked exactly like an agent that had died -- and the person
                # watching has no way to tell those apart from the outside.
                what = str(loop["code"]).replace("_", " ")
                self._send(
                    f"I am looking into {what} on {incident_key}. It asked to run "
                    f"{len(diagnosis.reads.commands)} reads on the host first"
                    + (f": {diagnosis.reads.note}" if diagnosis.reads.note else "")
                    + "\nI will come back with what it concludes."
                )
            return Diagnosis(None, MODEL, reason="it asked to look at the host", pending=True)
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
        # Over either way, but not the same either way: a loop that reached a finding
        # may be followed by another when the fault comes back, and one that ran out of
        # things to say must not simply start again on the next pass.
        self.observations.close(
            loop_id, CLOSED if diagnosis.finding is not None else SPENT, now
        )
        self._note_investigator_health(diagnosis, now)
        # Guarded, and last. By the time this runs the loop has been closed and the
        # conclusion reached; letting a failed write throw would discard a diagnosis
        # that already cost its rounds, and the next pass would start the whole loop
        # again -- for exactly as long as whatever is wrong with the store lasts,
        # which on a full disk is precisely when the diagnosis matters most.
        self._guard(self._keep_diagnosis, diagnosis, request, incident_key, episode)
        return diagnosis

    def _keep_diagnosis(
        self, diagnosis: Diagnosis, request: DiagnosisRequest, incident_key: str, episode: int
    ) -> None:
        self.evidence.record("diagnosis", f"incident:{incident_key}", {
            "incident_key": incident_key,
            "episode": episode,
            "evidence_hash": request.evidence_hash(),
            # Which investigation this belongs to. A conversation about this incident
            # has to reach the same episode, or it opens a second one with an empty
            # thread and the model is asked about work it has never seen.
            "subject_hash": request.subject_hash(),
            "observe_rounds": len(request.observe_rounds),
            # What this fault's turns are charged against. A conversation about it has
            # to join the same investigation, or talking is metered as if it were the
            # machine investigating itself.
            "investigation_id": request.investigation_id,
            "source": diagnosis.source,
            "reason": diagnosis.reason,
            "summary": None if diagnosis.finding is None else diagnosis.finding.summary,
            "mechanism": None if diagnosis.finding is None else diagnosis.finding.mechanism,
            "action": None if diagnosis.action is None else diagnosis.action.describe(),
            "action_summary": None if diagnosis.action is None else diagnosis.action.summary,
            "action_impact": None if diagnosis.action is None else diagnosis.action.impact,
            "durable_summary": (None if diagnosis.finding is None or
                diagnosis.finding.durable_action is None else
                diagnosis.finding.durable_action.summary),
            "durable_impact": (None if diagnosis.finding is None or
                diagnosis.finding.durable_action is None else
                diagnosis.finding.durable_action.impact),
            # The half of the answer that matters most to a person, and it was the half
            # not kept: a conversation about this incident was briefed with the stopgap
            # and never told what the cure was.
            "durable": None if diagnosis.finding is None else " ".join(part for part in (
                diagnosis.finding.durable_action.describe()
                if diagnosis.finding.durable_action else "",
                diagnosis.finding.durable_recommendation,
            ) if part) or None,
            "recurs": None if diagnosis.finding is None or diagnosis.finding.recurrence is None
            else diagnosis.finding.recurrence.mechanism or bool(diagnosis.finding.recurrence.expected),
            "confidence": None if diagnosis.finding is None else diagnosis.finding.confidence,
            "unsupported_request": None if diagnosis.finding is None else diagnosis.finding.unsupported_request,
            "answer": diagnosis.raw_text,
            "recorded_at": _text(self.clock()),
        })

    def _note_investigator_health(self, diagnosis: Diagnosis, now: datetime) -> None:
        """Say plainly when the model is not the one answering, and say it once.

        A fallback diagnosis is a reasonable proposal from the rule, and it looks
        exactly like a healthy one. Without this, the only difference between "the
        model agreed" and "the model has been unreachable for a day" is a phrase in
        the middle of a message nobody reads twice.
        """
        reason = diagnosis.reason or ""
        # Not the same as an investigator we cannot reach, and it must not be reported
        # as one: this is our own spending, stopped on purpose, and it stays stopped
        # until the day rolls. Saying it plainly is the whole point of a backstop that
        # is meant to be noticed rather than to quietly halve what the machine can do.
        if diagnosis.source != MODEL and "daily-spend-backstop" in reason:
            if self.schedule.due("investigation-backstop", now):
                self.schedule.set("investigation-backstop", now + INVESTIGATOR_SILENT_REMINDER)
                self._send(
                    "I have stopped investigating on my own: today's investigation "
                    "spend passed the ceiling I keep against a runaway. I am "
                    "diagnosing with the one rule I was taught by hand until it "
                    "clears. Ask me anything and I will still answer."
                )
            return
        broken = diagnosis.source != MODEL and reason.startswith(
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

    def _other_open_incidents(self) -> list[tuple[str, int, str, str]]:
        """Every open fault that is not the one the handover path already owns.

        There were forty of these on this machine and the investigator looked at two:
        eighteen BMC faults, nine GPU Xid errors, eight capacity, three probe. All of
        them raised, notified and then seen by nobody who could work out what they
        meant. Ordered worst first, because if only one gets looked at it should be
        that one.
        """
        try:
            rows = self.state_db.execute(
                """SELECT dedup_key, notification_episode, fault_family, severity,
                          source, stable_signature
                     FROM incidents
                    WHERE status IN ('open','recovery_pending')
                 ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                          WHEN 'warning' THEN 2 ELSE 3 END, last_occurrence_utc DESC"""
            ).fetchall()
        except sqlite3.Error:
            return []
        return [
            (str(row[0]), int(row[1]), str(row[2]), str(row[3]))
            for row in rows
            # SSH/GPU is broader than the one handover signature this service has a
            # dedicated driver for. Excluding the whole class left every Xid and
            # other non-handover GPU incident with no driver at all.
            if not (
                str(row[4]) == "ssh"
                and str(row[2]) == "gpu"
                and str(row[5]) in self.signatures
            )
        ]

    def _why(self, what: str, **fields: Any) -> None:
        """Say why a pass did nothing, once, in the journal.

        Every early return between an open incident and a button was silent. When no
        button appeared there was nothing to read -- the loops table showed `spent` at
        nought rounds and no reason anywhere -- so working out which of a dozen guards
        had fired meant querying the database from another machine and inferring it.
        A line here is cheaper than that, every time.
        """
        extra = "".join(f',"{key}":"{str(value)[:80]}"' for key, value in fields.items())
        self.report(f'{{"operation":"actions","phase":"investigate","why":"{what}"{extra}}}')

    def _investigate_open(self) -> None:
        """Work out what one other open fault is, and deal with it if it is ours.

        One at a time, and once per fault episode. There is one budget and dozens of
        open incidents, so investigating them all at once would spend the day before
        any of them finished; and a fault that has had its look does not need another
        until it recurs, which gives it a new episode.
        """
        if self.diagnoser is None or self.controls.paused:
            return
        now = self.clock()
        if not self.schedule.due("investigate", now):
            return
        status: Any = None
        others = self._other_open_incidents()
        if not others:
            self._why("no-open-incidents")
            return
        live = self.observations.live_loop()
        if live is not None and not self._live_loop_still_owned(live, others, now):
            live = None
        if live is not None:
            # Carry on with the one already under way. Refusing to act while anything
            # was live stopped a NEW investigation starting, which is what it was for,
            # and also stopped this one ever coming back to the loop it had just
            # opened -- so its first question was asked and never followed up, and it
            # sat at nought rounds until the reaper took it an hour and a half later.
            key, episode = str(live["incident_key"]), int(live["episode"])
            carrying = [item for item in others if item[0] == key and item[1] == episode]
            if not carrying:
                # A handover or a requested look; each has its own driver.
                self._why("another-look-under-way", incident=key)
                return
            chosen = carrying[0]
        else:
            # A fault that has had its look does not get another until it recurs --
            # unless a person asks for one. `look-again` is keyed by GPU address, so
            # for an incident with no GPU there was no way to ask at all; it could only
            # be looked at once, ever, per episode. An operator asking is the other
            # half of "re-fire when the fault changes", and it has to reach every fault.
            pending = [
                incident for incident in others
                if ((not self.observations.seen(incident[0], incident[1])
                     and not self._chat_concluded_same_evidence(incident[0], incident[1]))
                    or self._review_for(incident[0], now) is not None)
                and not self._chat_owns(incident[0], incident[1])
            ]
            if not pending:
                self._why("every-open-incident-already-looked-at", count=len(others))
                return
            # Something a person asked about goes FIRST, not into the queue behind
            # whatever is most severe. Asking and then waiting through three unrelated
            # investigations is indistinguishable from being ignored -- and it was:
            # a fault diagnosed before a deploy is never revisited on its own, because
            # it has already been seen, so the operator asking is the ONLY way it gets
            # looked at again. Making them wait their turn for that is the whole gap.
            asked = [item for item in pending if self._review_for(item[0], now) is not None]
            if asked:
                chosen = asked[0]
            else:
                try:
                    status = self.adapter.status()
                except Exception:
                    return  # No view of the machine is no time to reason about it.
                eligible = self._unrequested_eligible(status, others, pending, now)
                if not eligible:
                    return
                chosen = eligible[0]
        self.schedule.set("investigate", now + STATUS_RETRY_INTERVAL)
        key, episode, family, severity = chosen
        if self._chat_owns(key, episode):
            return
        if status is None:
            try:
                status = self.adapter.status()
            except Exception:
                return  # No view of the machine is no time to reason about it.
        requested = self._review_for(key, now) is not None
        diagnosis = self._diagnose(
            "", key, episode, status, now,
            requested=requested, code=f"{family}_fault",
        )
        if diagnosis.pending:
            return
        if requested:
            # One asking buys exactly one completed look. Leaving this standing made
            # the same incident start a fresh investigation every five minutes until
            # the request happened to expire.
            self._spend_review(key, now)
        if self._carried_out(diagnosis, key, now):
            # The stopgap ran on its own; the cure still needs a person.
            self._ask_about(diagnosis, "", key, episode, now, durable_only=True)
            return
        if diagnosis.finding is None:
            if requested:
                self._send(
                    f"I looked at {key} again and reached no conclusion "
                    f"({diagnosis.reason or 'no answer'})."
                )
            self._why("no-finding", incident=key, reason=diagnosis.reason or "")
            return  # The reason is in the evidence; nothing else to say.
        # A command it wants run is a question for somebody, not a paragraph about one.
        if self._ask_about(diagnosis, "", key, episode, now):
            return
        self._last_reported = key
        self._send(
            (f"You asked me to look at {key} again.\n" if requested else
             f"{key} ({severity}) is open and nobody had looked at it.\n") +
            f"{describe(diagnosis)}"
        )

    def _unrequested_eligible(
        self, status: Any, others: list[tuple[str, int, str, str]],
        pending: list[tuple[str, int, str, str]], now: datetime,
    ) -> list[tuple[str, int, str, str]]:
        """The pending incidents an investigation nobody asked for may start on now.

        Both limits are deterministic and cost nothing to check; a withheld pass waits
        one retry interval, and says why in the journal rather than to a person. A
        symptom is held back only while an incident already looked at under the same
        machine state is still open: once that clears, it is a different problem.
        """
        still_open = {item[0] for item in others}
        covering = self.observations.looked_at_under(
            fault_revision(status, ""), now - SAME_FAULT_WINDOW
        ) & still_open
        eligible = [
            item for item in pending
            if not (covering and item[2] in SYMPTOM_FAMILIES)
        ]
        if not eligible:
            self.schedule.set("investigate", now + STATUS_RETRY_INTERVAL)
            self._why("same-fault-already-looked-at", pending=len(pending),
                      covered_by=sorted(covering)[0])
            return []
        started = self.observations.started_since(now - LOOK_BUDGET_WINDOW)
        if started >= MAX_UNREQUESTED_LOOKS:
            self.schedule.set("investigate", now + STATUS_RETRY_INTERVAL)
            self._why("daily-look-budget-spent", started=started, pending=len(pending))
            if self.schedule.due("look-budget-notice", now):
                self.schedule.set("look-budget-notice", now + LOOK_BUDGET_WINDOW)
                self._send(
                    f"I have started {started} investigations in the last 24 hours, "
                    "which is my limit, so I am not starting more on my own today. "
                    "New faults are still recorded. Ask me to look at one and I will."
                )
            return []
        return eligible

    def _live_loop_still_owned(
        self, live: Any, others: list[tuple[str, int, str, str]], now: datetime
    ) -> bool:
        """Whether the loop under way is still about something that is wrong.

        A loop for a fault that recovered -- or recurred, which gives it a new episode --
        has nothing left to find. Leaving it live made every pass return here silently,
        so nothing else on the machine was investigated until the ninety-minute reaper
        took it: on 2026-09-24 a capacity fault that recovered mid-deploy blinded the
        service for an hour and a half, with not one line in the journal to say why.
        """
        key, episode = str(live["incident_key"]), int(live["episode"])
        if any(item[0] == key and item[1] == episode for item in others):
            return True
        if key == REVIEW_KEY or str(live["code"]) == HANDOVER_CODE:
            # These have their own drivers, which end their loops themselves.
            return self._incident_open(key)
        loop_id = str(live["loop_id"])
        self.observations.abandon(loop_id, "the fault it was about is over", now)
        self.observations.close(loop_id, now=now)
        self._why("closed-loop-for-a-fault-that-is-over", incident=key)
        return False

    def _carried_out(self, diagnosis: Diagnosis, incident_key: str, now: datetime) -> bool:
        """Do it ourselves when it is ours to do. True when it was handled here.

        The charter's line: managing the monitoring we installed is the agent's own
        work, because it is reversible and touches nobody who is paying us. That is
        every container in the catalogue except the one the adapter owns, which keeps
        its evidence backup and its button because acting on it perturbs the very GPU
        state being diagnosed.
        """
        action = diagnosis.action
        if self.actor is None or action is None or action.risk is not Risk.SELF:
            return False
        container = _container_named(action.command)
        # The one exception, and it is about this container rather than about the
        # vocabulary: restarting dcgm-exporter perturbs the very GPU state being
        # diagnosed, so it keeps its evidence backup and its button.
        if container == COMPONENT:
            return False
        if not container:
            container = action.command
        if self.controls.paused:
            self._send(
                f"I would deal with {container} myself, and I am paused. Tell me to "
                "resume and I will."
            )
            return True
        # Its own cooldown, per container. Acting because something looks unhealthy,
        # finding it still looks unhealthy, and acting again is a loop that looks like
        # work while nothing improves.
        waited = f"acted:{container}"
        if not self.schedule.due(waited, now):
            return False
        self.schedule.set(waited, now + MONITORING_ACTION_COOLDOWN)
        result = self.actor.run(action.command, subject=f"incident:{incident_key}")
        if result.ok:
            self._send(
                f"I restarted {container}. I will check whether that restores service."
            )
        else:
            self._send(
                f"I tried to restart {container}, but it failed. "
                "The fault is not confirmed fixed; the technical result is in the audit record."
            )
        return True

    def _ask_about(
        self, diagnosis: Diagnosis, bdf: str, incident_key: str, episode: int, now: datetime,
        *, durable_only: bool = False,
    ) -> bool:
        """Put a proposed command to a person, with buttons. True when it was asked.

        Findings used to be narrated and nothing else: the model could say `systemctl
        reboot` and there was no way to answer it, because the only path that built a
        proposal was the one fixed handover restart. So a correct diagnosis of a dead
        GPU arrived with a proposed fix and no way to say yes -- which is the whole
        product, missing.

        The durable fix gets the same button. It used to be shown as "needs your
        decision" with nothing to decide with, so the stopgap was the only thing that
        could ever run and the fault it papered over came back on schedule.

        The command is what is shown and what is bound. The approval stays a single-use
        tap on one proposal, and the classifier has already decided this needs a person.
        """
        finding = diagnosis.finding
        if finding is None:
            self._why("no-action-to-ask-about")
            return False
        candidates = (
            (finding.durable_action,) if durable_only
            else (finding.action, finding.durable_action)
        )
        action = next(
            (item for item in candidates
             if item is not None and item.risk is Risk.APPROVAL),
            None,
        )
        if action is None:
            self._why("not-for-a-person")
            return False
        attempt_key = f"proposal-attempt:{incident_key}:{episode}:{int(durable_only)}"
        fingerprint = hashlib.sha256(json.dumps((
            self.notes.get(f"review-generation:{incident_key}") or "automatic",
            self.notes.get(f"review-generation:{bdf}") or "automatic",
            describe(diagnosis), action.command,
            action.rollback, action.verify, action.summary, action.impact)).encode()).hexdigest()
        if self.notes.get(attempt_key) == fingerprint:
            return True  # The same evidence has already had its bounded review attempt.
        queued = self._queue_review(
            action, headline=finding.summary, question=self._operator_request(incident_key),
            bdf=bdf, incident_key=incident_key, episode=episode, now=now,
            durable=action is finding.durable_action, reasoning=describe(diagnosis),
        )
        active = self.cycles.active()
        if queued or active is None or active.stage != "executing":
            self.notes.set(attempt_key, fingerprint, now)
        return queued

    @staticmethod
    def _action_binding(action: ProposedAction, bdf: str, incident_key: str,
                        episode: int) -> str:
        payload = (action.command, action.rollback, action.verify, action.summary,
                   action.impact, bdf, incident_key, episode)
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def _queue_review(self, action: ProposedAction, *, headline: str, question: str,
                      bdf: str, incident_key: str, episode: int, now: datetime,
                      durable: bool = False, exchange: Mapping[str, Any] | None = None,
                      reasoning: str = "") -> bool:
        active = self.cycles.active()
        if active is not None and active.stage == "executing":
            if exchange is not None:
                self._queue_terminal(str(exchange["root"]),
                    headline.strip() + "\n\nA change is still running. Its outcome must be checked before another action is proposed.",
                    now, incident_key, episode)
            return False
        if exchange is None and self.conversation is not None:
            root = f"auto-{uuid.uuid4().hex}"
            self.conversations.start(root, incident_key=incident_key, episode=episode,
                bdf=bdf, subject_hash=hashlib.sha256(
                    f"{incident_key}:{episode}".encode()).hexdigest(),
                investigation_id=root, sender_id=CONSOLE_SENDER,
                question=question or headline, now=now)
            exchange = self.conversations.by_root(root)
        root = str(exchange["root"]) if exchange is not None else f"auto-{uuid.uuid4().hex}"
        if exchange is not None and self.conversations.by_root(root) is None:
            return False
        if (not action.summary or not action.impact or not action.rollback
                or not action.verify or len(action.summary) + len(action.impact) > 1000
                or any("\n" in value or "```" in value or scrub(value) != value
                       or operator_prose_problem(value)
                       for value in (action.summary, action.impact))):
            prose_problem = (operator_prose_problem(action.summary) or
                             operator_prose_problem(action.impact))
            reason = (f"The action or impact summary contains {prose_problem}. "
                      "Describe the action and impact in plain prose."
                      if prose_problem else
                      "The proposed action needs a complete action, impact, recovery, and verification description.")
            corrections = int(self.notes.get(f"corrections:{root}") or 0)
            if (exchange is not None and self.conversation is not None
                    and corrections < MAX_CHAT_CORRECTIONS):
                self.notes.set(f"corrections:{root}", str(corrections + 1), now)
                if not self.notes.get(f"finding:{root}"):
                    self.notes.set(f"finding:{root}", headline[:1200], now)
                self.conversations.ask(root, int(exchange["round"]) + 1, (), now)
                self._put_to_conversation(exchange, f"{root}:repair",
                    f"Established finding: {self.notes.get(f'finding:{root}')}\n"
                    f"Internal reasoning: {reasoning[:3000]}\n"
                    f"Current proposed command: {action.command[:8000]}\n"
                    f"Rollback: {action.rollback[:2000]}\n"
                    f"Verification: {list(action.verify)[:4]}\n"
                    f"Action summary: {action.summary}\nImpact: {action.impact}\n\n" + reason
                    + " Correct the proposal and return a complete ```plan block, or "
                    "conclude with the established finding and the unresolved blocker. "
                    "Do not claim the change was made.", now)
                return True
            self._queue_terminal(root,
                (self.notes.get(f"finding:{root}") or headline.strip()) + "\n\n" +
                reason + " The action is withheld until a corrected plan is available.",
                now, incident_key, episode)
            if exchange is not None:
                self.conversations.end(root)
            return True  # A terminal finding was queued; do not narrate it again.
        revisions = int(self.notes.get(f"revisions:{root}") or 0)
        finding = self.notes.get(f"finding:{root}") or headline.strip()
        if exchange is not None and finding:
            self.notes.set(f"finding:{root}", finding[:1200], now)
        binding = self._action_binding(action, bdf, incident_key, episode)
        pending = {
            "root": root, "turn_ticket": str(exchange["ticket"]) if exchange else "",
            "ticket": "", "asked_utc": _text(now), "revisions": revisions,
            "headline": headline[:200], "finding": finding[:1200],
            "question": question[:2000],
            "explicit_request": bool(question),
            "command": action.command, "intent": action.intent,
            "rollback": action.rollback, "verify": list(action.verify),
            "summary": action.summary, "impact": action.impact,
            "bdf": bdf, "incident_key": incident_key, "episode": episode,
            "durable": durable, "binding": binding,
            "diagnosis_revision": self._diagnosis_revision(incident_key),
            "incident_signature": self._incident_signature(incident_key),
        }
        if incident_key in (REVIEW_KEY, MACHINE_SUBJECT):
            current = (self.controls.get(REVIEW_REQUEST) or
                       self.notes.get(f"last-request:{REVIEW_REQUEST}") or "")
            previous = self.notes.get(f"reviewed:{root}")
            if previous:
                # A correction belongs to the original request, even if a new
                # whole-machine look was requested while the model was revising it.
                try:
                    pending["machine_request"] = json.loads(previous).get("machine_request", "")
                except (ValueError, AttributeError):
                    pending["machine_request"] = ""
            else:
                pending["machine_request"] = current
            if pending["machine_request"] != current:
                self._queue_terminal(root, finding + "\n\nThe proposal is withheld because a fresh look was requested.",
                                     now, incident_key, episode)
                if exchange is not None:
                    self.conversations.end(root)
                return True
        if self.reviewer is not None:
            try:
                pending["ticket"] = self.reviewer.ask(
                    review_id=f"{root[:24]}-{revisions}-{int(now.timestamp())}",
                    prompt=review_prompt(question, reasoning or headline, action),
                )
            except Exception as error:
                self.report(f"review request failed: {type(error).__name__}")
        self.notes.set(f"review:{root}", json.dumps(pending), now)
        return True

    def _diagnosis_revision(self, incident_key: str) -> str:
        row = self.state_db.execute(
            "SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis' "
            "AND subject=? ORDER BY recorded_utc DESC,rowid DESC LIMIT 1",
            (f"incident:{incident_key}",),
        ).fetchone()
        if row is None:
            return ""
        try:
            raw = row[0]
            document = json.loads(bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw)
            return str(document.get("evidence_hash") or "")
        except (TypeError, ValueError):
            return ""

    def _incident_signature(self, incident_key: str) -> str:
        row = self.state_db.execute(
            "SELECT stable_signature FROM incidents WHERE dedup_key=?", (incident_key,)
        ).fetchone()
        return "" if row is None else str(row[0])

    def _autonomous_review_stale(self, pending: Mapping[str, Any]) -> bool:
        key = str(pending.get("incident_key") or "")
        if key in (REVIEW_KEY, MACHINE_SUBJECT):
            current = (self.controls.get(REVIEW_REQUEST) or
                       self.notes.get(f"last-request:{REVIEW_REQUEST}") or "")
            return str(pending.get("machine_request") or "") != current
        row = self.state_db.execute(
            "SELECT notification_episode,status FROM incidents WHERE dedup_key=?", (key,)
        ).fetchone()
        if row is None or int(row[0]) != int(pending.get("episode") or 0):
            return True
        if not pending.get("durable") and str(row[1]) not in ("open", "recovery_pending"):
            return True
        signature = str(pending.get("incident_signature") or "")
        if signature and self._incident_signature(key) != signature:
            return True
        revision = str(pending.get("diagnosis_revision") or "")
        return bool(revision and self._diagnosis_revision(key) != revision)

    def _reconcile_legacy_plans(self) -> None:
        """Withdraw old generic cards before an input can spend their nonce."""
        for cycle in self.cycles._many("command IS NOT NULL AND stage IN ('awaiting_answer','awaiting_delivery')"):
            if (self._valid_review_binding(cycle)
                    and (cycle.stage != "awaiting_answer" or cycle.delivered_utc)):
                continue
            self._finish(cycle, "withdrawn", "legacy generic request lacked review binding",
                         notice=f"Request {cycle.proposal_id} was withdrawn because its review could not be verified. Ask for a fresh proposal.")

    def _record_internal(self, exchange: Mapping[str, Any], ticket: str,
                         kind: str, detail: str, now: datetime) -> None:
        event_id = hashlib.sha256(f"{exchange['root']}:{ticket}:{kind}".encode()).hexdigest()
        self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
            (event_id,root,ticket,subject,episode,kind,text,created_utc)
            VALUES(?,?,?,?,?,?,?,?)""", (event_id, str(exchange["root"]), ticket,
            str(exchange["incident_key"]), int(exchange["episode"]), kind,
            scrub(detail)[:MAX_NOTE_CHARS], _text(now)))
        self.actions_db.commit()

    def _record_input_start(self, envelope: Any) -> None:
        event_id = f"input:{envelope.update_id}"
        detail = (str(envelope.nonce or "") if envelope.kind is InputKind.QUESTION
                  else str(envelope.kind.value))
        self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
            (event_id,root,ticket,subject,episode,kind,text,created_utc)
            VALUES(?,?,?,?,?,?,?,?)""", (
            event_id, event_id, str(envelope.update_id), str(envelope.subject_id or ""),
            0, "input-start", scrub(detail)[:4000],
            _text(self.clock()),
        ))
        self.actions_db.commit()

    def _queue_terminal(self, root: str, prose: str, now: datetime,
                        subject: str = "", episode: int = 0) -> None:
        # A final answer survives the deletion of its conversation row and a restart.
        if len(prose) > MAX_TELEGRAM_TEXT - 96:
            self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
                (event_id,root,ticket,subject,episode,kind,text,created_utc)
                VALUES(?,?,?,?,?,?,?,?)""", (f"source:{root}", root, root, subject,
                episode, "terminal-source", scrub(prose)[:MAX_NOTE_CHARS], _text(now)))
        text = self._operator_prose(prose)
        exchange = self.conversations.by_root(root)
        if exchange is not None:
            subject = str(exchange["incident_key"])
            episode = int(exchange["episode"])
        for index, part in enumerate(_message_parts(text)):
            event_id = f"final:{root}" if index == 0 else f"final:{root}:{index + 1}"
            self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
                (event_id,root,ticket,subject,episode,kind,text,created_utc)
                VALUES(?,?,?,?,?,?,?,?)""", (event_id, root,
                str(exchange["ticket"]) if exchange else root, subject, episode,
                "terminal", part, _text(now)))
        self.actions_db.commit()

    def _progress(self, root: str, message: str, now: datetime) -> None:
        if not message:
            return
        message = self._operator_prose(message)
        if message == self.notes.get(f"progress:{root}"):
            return
        self.notes.set(f"progress:{root}", message, now)
        exchange = self.conversations.by_root(root)
        event_id = f"progress:{hashlib.sha256(f'{root}:{message}'.encode()).hexdigest()}"
        self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
            (event_id,root,ticket,subject,episode,kind,text,created_utc)
            VALUES(?,?,?,?,?,?,?,?)""", (event_id, root,
            str(exchange["ticket"]) if exchange else root,
            str(exchange["incident_key"]) if exchange else "",
            int(exchange["episode"]) if exchange else 0,
            "progress", message, _text(now)))
        self.actions_db.commit()

    @staticmethod
    def _operator_prose(prose: str) -> str:
        # Keep fenced technical detail internal and reject command syntax that
        # remains in normal operator prose.
        clean = re.sub(r"```[\s\S]*?```", "", scrub(prose or ""))
        clean = re.sub(r"```[\s\S]*$", "", clean)
        if (re.search(r"```operator[^\n]*\n[\s\S]*?```(?:plan|reads|read-script)",
                      prose or "") or operator_prose_problem(clean)):
            return "I could not provide a safe summary yet. A corrected plain-language answer is needed."
        paragraphs = []
        for block in re.split(r"\n\s*\n", clean):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if lines:
                paragraphs.append(" ".join(lines))
        return "\n\n".join(paragraphs) or "I could not establish a safe conclusion yet."

    def _request_approval(
        self, action: ProposedAction, *, headline: str, body: str, bdf: str,
        incident_key: str, episode: int, now: datetime,
        conversation: Mapping[str, Any] | None = None, durable: bool = False,
        reviewed_binding: str = "", review_event: str = "",
        explicit_request: bool = False,
    ) -> bool:
        """Create one approval request for an exact command and send it with buttons."""
        if self.actor is None:
            self._why("no-actor-configured")
            if conversation is not None:
                self._progress(str(conversation["root"]),
                    "The reviewed change is ready, but the action channel is unavailable. I will retry the offer.", now)
            return False
        binding = self._action_binding(action, bdf, incident_key, episode)
        try:
            grant = json.loads(self.notes.get(f"grant:{review_event}") or "{}")
        except ValueError:
            grant = {}
        if (not reviewed_binding or not secrets.compare_digest(binding, reviewed_binding)
                or grant.get("binding") != binding or not grant.get("ticket")
                or not action.summary or not action.impact or not action.rollback
                or not action.verify
                or any("\n" in value or "```" in value or scrub(value) != value
                       or operator_prose_problem(value)
                       for value in (action.summary, action.impact))):
            self._why("unreviewed-or-incomplete-generic-plan")
            return False
        card = (f"Action: {action.summary.strip()}\n\n"
                f"Impact and limits: {action.impact.strip()}\n\n"
                f"Proposal: {{proposal_id}}")
        if len(card) > 1200:
            self._why("approval-card-too-long")
            return False
        if action.risk is Risk.REFUSED:
            self._send(f"I will not put that to you: {action.why}.")
            return False
        if conversation is None and not explicit_request and self.cycles.recently_unwanted(
            action.command, now - REPEAT_PROPOSAL_WINDOW
        ):
            # Asked already, and nobody wanted it. Asking again because a different
            # incident about the same fault reached the same answer is nagging.
            self._why("same-proposal-recently-unwanted")
            return False
        # One request at a time. active() only ever sees the newest cycle, so a second
        # one created beside a live one would leave the older orphaned: never advanced,
        # never expired, its buttons never cleared, and its episode closed for good.
        active = self.cycles.active()
        if active is not None:
            replaces = (active.stage in ("awaiting_answer", "awaiting_delivery")
                        and active.incident_key == incident_key and active.episode == episode
                        and self._plan_lineage(active) == _lineage(conversation))
            if not replaces:
                # Either something approved is running, or a request about a different
                # problem is waiting for an answer; withdrawing that one would drop a
                # question nobody has answered yet.
                self._why("cycle-in-flight")
                if conversation is not None:
                    self._progress(str(conversation["root"]),
                        "A reviewed change is ready. I will offer it after the current request finishes.", now)
                return False
            # A newer plan for the same problem replaces the waiting one, visibly.
            self._finish(
                active, "superseded", "a newer plan replaced this request",
                notice=(f"The request {active.proposal_id or ''} is withdrawn: a newer "
                        "plan replaces it."),
            )
        cycle_id, nonce = str(uuid.uuid4()), secrets.token_urlsafe(18)
        proposal_id = f"cmd-{secrets.token_hex(6)}"
        self.cycles.create(Cycle(
            cycle_id=cycle_id, bdf=bdf, incident_key=incident_key, episode=episode,
            stage="awaiting_delivery", evidence_revision="", evidence_ref="",
            trigger_utc=_text(now), retrigger_utc=_text(now), backup_ref=None,
            proposal_id=None, nonce=None, digest=None, shape=None,
            created_utc=_text(now),
        ))
        # create() writes a fixed subset of the row and drops the rest, so what makes
        # this cycle findable by its proposal has to be written here. Passing them to
        # Cycle() looked right and left the request unanswerable: the buttons carried a
        # proposal id that matched no row, so every tap was "that answer does not match
        # a waiting restart request".
        self.cycles.update(
            cycle_id, now, proposal_id=proposal_id, nonce=nonce, command=action.command
        )
        # What happens after the tap: the checks to run, and the conversation to tell.
        self.notes.set(f"plan:{proposal_id}", json.dumps({
            "verify": list(action.verify), "rollback": action.rollback,
            "conversation": dict(conversation) if conversation else None,
            "binding": binding, "summary": action.summary, "impact": action.impact,
            "card": card.format(proposal_id=proposal_id),
            "review_event": review_event,
        }), now)
        self._deliver_card(self.cycles.get(cycle_id), now)
        return True

    def _deliver_card(self, cycle: Cycle, now: datetime) -> None:
        if cycle.stage != "awaiting_delivery" or not self.schedule.due(f"card:{cycle.cycle_id}", now):
            return
        if not self._valid_review_binding(cycle):
            self._finish(cycle, "withdrawn", "review binding changed before delivery",
                         notice=f"Request {cycle.proposal_id} was withdrawn because its review could not be verified.")
            return
        note = self._plan_note(cycle)
        def deliver() -> None:
            receipt = self.telegram.send_message(
                self.group_id, note["card"],
                approve_callback=("Approve", f"approve:{cycle.proposal_id}:{cycle.nonce}"),
                deny_callback=("Leave it", f"deny:{cycle.proposal_id}:{cycle.nonce}"),
            )
            self.cycles.update(cycle.cycle_id, now, stage="awaiting_answer",
                               message_id=receipt.message_id, delivered_utc=_text(now))
        self._attempt_delivery(f"card:{cycle.cycle_id}", now, deliver)

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
        if not bdf or (
            self._override_for(bdf, now) is None and self._review_for(bdf, now) is None
        ):
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
            and not self._chat_owns(incident[1], incident[2])
            and (self._review_for(incident[0], now) is not None
                 or not self._chat_concluded_same_evidence(incident[1], incident[2], incident[0]))
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
                or self._review_for(incident[0], now) is not None
            ]
            if not incidents:
                return
        asked_for = any(
            self._override_for(incident[0], now) is not None
            or self._review_for(incident[0], now) is not None
            # A loop that has finished its reads is waiting on nothing but this gate,
            # and waiting five minutes to ask the next question -- once per round, six
            # rounds -- put half an hour of dead time into every investigation.
            or self._loop_ready(incident[1], incident[2])
            for incident in incidents
        )
        if not asked_for and not self.schedule.due("status", now):
            return
        self.schedule.set("status", now + STATUS_RETRY_INTERVAL)
        status = self.adapter.status()
        # Never ask a human to approve something the adapter would refuse to propose.
        if not status.identity_verified:
            self._end_unproposable_reviews(
                incidents, now, "target-identity-not-verified",
                "I checked {bdf}, but the target identity did not verify. I did not "
                "create or carry out a restart request.",
            )
            return
        if not status.container.present:
            self._end_unproposable_reviews(
                incidents, now, "exporter-not-present",
                "I checked {bdf}, but dcgm-exporter is not present. There is no safe "
                "restart request to put in front of you, and nothing was carried out.",
            )
            return
        if not status.container.running:
            self._end_unproposable_reviews(
                incidents, now, "exporter-not-running",
                "I checked {bdf}, but dcgm-exporter is not running. The restart proposal "
                "requires the running container I described, so I did not create or carry "
                "out a request.",
            )
            return
        for bdf, incident_key, episode in incidents:
            if bdf not in status.handover_blocked:
                if self._review_for(bdf, now) is not None:
                    self._spend_review(bdf, now)
                    self._why("requested-gpu-no-longer-blocked", gpu=bdf)
                    self._send(
                        f"I checked {bdf}. Fresh target status no longer reports that GPU "
                        "blocked in the NVIDIA-to-vfio handover, so the earlier diagnosis "
                        "does not support a dcgm-exporter restart now. I did not create or "
                        "carry out a restart request."
                    )
                continue
            diagnosis = self._diagnose(bdf, incident_key, episode, status, now)
            if diagnosis.pending:
                # The investigator is still thinking. Nothing is decided, the override
                # is untouched, and this pass has nothing else to do for this GPU.
                continue
            override = self._spend_override(bdf, now)
            # A look that was asked for is used up whatever it concludes, so asking
            # again is asking again rather than leaving the loop running hot.
            self._spend_review(bdf, now)
            action = diagnosis.action
            if (
                action is None
                # The adapter restarts one fixed container; a finding about another is
                # for a person, not authority to restart this one.
                or _container_named(action.command) != COMPONENT
            ):
                # The rest of the monitoring is the agent's own work per the charter,
                # and had no path at all: a finding asking for the node exporter was
                # validated, found to be something the adapter could not do, and handed
                # to a person who would then have typed the restart themselves.
                if self._carried_out(diagnosis, incident_key, now):
                    self._ask_about(diagnosis, bdf, incident_key, episode, now,
                                    durable_only=True)
                    return
                if self._ask_about(diagnosis, bdf, incident_key, episode, now):
                    return
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
            self._remember_durable(cycle_id, diagnosis, now)
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

    def _remember_durable(self, cycle_id: str, diagnosis: Diagnosis, now: datetime) -> None:
        """Keep the durable fix behind a handover restart, to offer once it is over.

        The handover restart has its own cycle, backup and button, and only one of those
        may be in flight. The cure it papers over -- often replacing the very exporter
        being restarted -- was otherwise lost the moment the restart was proposed.
        """
        finding = diagnosis.finding
        durable = finding.durable_action if finding is not None else None
        if durable is None or durable.risk is not Risk.APPROVAL:
            return
        self.notes.set(f"durable:{cycle_id}", json.dumps({
            "headline": finding.summary, "command": durable.command,
            "intent": durable.intent, "rollback": durable.rollback,
            "verify": list(durable.verify), "summary": durable.summary,
            "impact": durable.impact,
        }), now)

    def _offer_durable(self, cycle: Cycle, result: str) -> None:
        """Once the stopgap is settled, put the cure to a person."""
        name = f"durable:{cycle.cycle_id}"
        raw = self.notes.get(name)
        if not raw:
            return
        self.notes.clear(name)
        if result not in DURABLE_AFTER_RESULTS:
            return
        try:
            data = json.loads(raw)
            action = ProposedAction(
                str(data["command"]), str(data.get("intent") or ""),
                str(data.get("rollback") or ""),
                tuple(str(item) for item in data.get("verify") or ()),
                str(data.get("summary") or ""), str(data.get("impact") or ""),
            )
        except (ValueError, KeyError, TypeError):
            return
        self._queue_review(
            action, headline=str(data.get("headline") or "The durable fix"), question="",
            bdf=cycle.bdf, incident_key=cycle.incident_key, episode=cycle.episode,
            now=self.clock(), durable=True,
        )

    def _end_unproposable_reviews(
        self,
        incidents: list[tuple[str, str, int]],
        now: datetime,
        reason: str,
        message: str,
    ) -> None:
        """Answer requested looks when fresh status cannot become a proposal.

        Ordinary monitoring may try again on its own cadence. A person who was just
        told "looking now" needs a terminal answer instead: leaving the review control
        behind made a rejected live status look like an investigation still in progress.
        """
        for bdf, _incident_key, _episode in incidents:
            if self._review_for(bdf, now) is None:
                continue
            self._spend_review(bdf, now)
            self._why(reason, gpu=bdf)
            self._send(message.format(bdf=bdf))

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

    def _review_for(self, subject: str, now: datetime) -> str | None:
        """Who asked for this GPU or incident to be looked at again.

        Deliberately not an override. An override lets the service act without asking;
        this only lets it investigate sooner than its own waiting periods would allow,
        because a person saying "look at it again" is asking for an opinion, not
        handing over the button.
        """
        value = self.controls.get(f"review:{subject}")
        if value is None:
            return None
        who, _, when = value.rpartition("@")
        try:
            set_at = _parse(when)
        except ValueError:
            self.controls.clear(f"review:{subject}")
            return None
        if now - set_at > OVERRIDE_LIFETIME:
            live = self.observations.live_loop()
            if live is not None and subject in (
                str(live["incident_key"]), str(live["bdf"])
            ):
                # The lifetime bounds how long a request may wait to be picked up,
                # not how long the investigation it started is allowed to take.
                return who
            self.controls.clear(f"review:{subject}")
            self._send(
                f"The request to look at {subject} again lapsed before I could take "
                "it up. Ask me again if you still want a fresh look."
            )
            return None
        return who

    def _expire_reviews(self) -> None:
        """Revisit requests whose incident may have vanished before pickup.

        The normal drivers check a review while selecting its open incident. A fault
        that recovered first is no longer selectable, so without this independent
        sweep its control remained forever and its expiry could never be announced.
        """
        now = self.clock()
        for subject in self.controls.reviews():
            self._review_for(subject, now)

    def _spend_review(self, subject: str, _now: datetime) -> str | None:
        """Take up a request to look again; one asking buys one look."""
        value = self.controls.get(f"review:{subject}")
        if value is None:
            return None
        who, _, when = value.rpartition("@")
        try:
            _parse(when)
        except ValueError:
            who = ""
        # Do not re-run the pickup expiry here. The caller has just completed the
        # investigation, whose own deadline may legitimately be longer than the time
        # allowed for an unclaimed request to wait.
        self.controls.clear(f"review:{subject}")
        self.notes.clear(f"request:review:{subject}")
        self.notes.set(f"review-generation:{subject}", uuid.uuid4().hex, _now)
        return who or None

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
        self._remember_durable(cycle_id, diagnosis, now)
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

    def _steer(self, name: str, argument: str, sender_id: int, question: str = "") -> str:
        """Carry out one thing a person asked for, and say what was done.

        Reached from their own words by way of the model, which names it from a fixed
        list this service checks. None of it can act on the machine, and every one of
        them is recoverable: the worst a misreading costs is a look nobody wanted or a
        pause you undo. What a look proposes still comes back as a button bound to that
        one proposal.

        That argument is why a model may read these out of someone's sentence at all,
        so it has to hold for every entry. It did not hold for `withdraw`, which
        destroys a pending approval -- see :meth:`_offer_withdrawal`, which now asks.
        """
        now = self.clock()
        if name == "pause":
            self.controls.set("paused", f"telegram:{sender_id}", sender_id, now)
            return "Paused: I will keep watching and reporting, and act on nothing."
        if name == "resume":
            self.controls.clear("paused")
            return "Acting again as usual."
        if name == "hold":
            self.controls.set(f"hold:{argument}", f"telegram:{sender_id}", sender_id, now)
            return f"Leaving {argument} alone until you say otherwise."
        if name == "release":
            self.controls.clear(f"hold:{argument}")
            self.controls.clear(f"override:{argument}")
            self.controls.clear(f"review:{argument}")
            return f"No longer holding {argument}."
        if name == "look-again":
            # A fresh look, not a fresh action: this lifts my own waiting periods for
            # investigating and nothing else. `_may_act_alone` never reads it, so
            # whatever it concludes still comes back to be approved.
            self.controls.set(
                f"review:{argument}", f"telegram:{sender_id}@{_text(now)}", sender_id, now
            )
            self._remember_request(f"review:{argument}", question, now)
            return (
                f"Looking at {argument} again from the beginning. If I find something "
                "worth doing I will come back and ask."
            )
        if name == "investigate":
            if self.controls.get(REVIEW_REQUEST) is not None:
                # Its identity is the moment it was asked for, so overwriting it here
                # would leave the look already running orphaned under the old one --
                # still holding its rounds and still taking its turn at the reads.
                return "I am already looking the machine over; I will come back with it."
            self.controls.set(
                REVIEW_REQUEST, f"telegram:{sender_id}@{_text(now)}", sender_id, now
            )
            self._remember_request(REVIEW_REQUEST, question, now)
            return (
                "Looking the machine over now. I will come back with what I find, "
                "whether or not it is anything."
            )
        if name == "ask-me":
            return self._ask_again(argument, now)
        if name == "withdraw":
            return self._offer_withdrawal(argument)
        return ""

    def _remember_request(self, control: str, question: str, now: datetime) -> None:
        """Keep what they actually asked, for the look this steer starts."""
        if question:
            self.notes.set(f"request:{control}", question, now)
        else:
            self.notes.clear(f"request:{control}")

    def _ask_again(self, subject: str, now: datetime) -> str:
        """Put the request in front of them, or arrange for there to be one.

        The vocabulary had a verb for pausing, holding, releasing, looking and
        withdrawing, and none for "go ahead". So "okay so do it" had nowhere to land:
        the model could only answer in prose, and it described a pending request that
        did not exist. The only way to say act-on-this was `/now <bdf>`, which is a
        command to remember -- the thing this vocabulary exists to avoid.

        It does not authorise anything. It produces the button and nothing else: the
        approval stays a deliberate tap bound to one proposal, because a model reading
        "do it" out of a sentence must not be what carries a change to this machine.
        """
        cycle = self.cycles.active()
        if cycle is not None and cycle.stage == "awaiting_answer" and cycle.proposal_id:
            if self._resend(cycle):
                return ""  # The request itself is the answer, and it carries buttons.
            return "I could not send that request again just now; I will keep trying."
        if cycle is not None:
            return (
                f"I am still working on {cycle.bdf} and will ask as soon as I can. "
                "Nothing is waiting on you yet."
            )
        # No subject named means the thing just discussed. Falling back to a whole-
        # machine review sounds helpful and is not: "do it" said after a report about
        # one fault has to reach THAT fault, and naming a stale GPU instead sets a
        # review on something that is no longer wrong, where it quietly does nothing.
        subject = subject or self._last_reported or ""
        if subject:
            self.controls.set(
                f"review:{subject}", f"telegram:asked@{_text(now)}", 0, now
            )
            return (
                f"Right. Looking at {subject[:16]} now, and I will put the request to "
                "you as soon as I have one."
            )
        self.controls.set(REVIEW_REQUEST, f"telegram:asked@{_text(now)}", 0, now)
        return "Right. I will look now and put the request to you as soon as I have one."

    def _resend(self, cycle: Cycle) -> bool:
        """Send the waiting request again, with its buttons, as a fresh message.

        A request that scrolled away may as well not exist -- which is what "I don't
        see it, send again" meant, and there was no way to answer it. The proposal and
        its nonce are unchanged, so the new buttons are the same single-use approval
        bound to the same proposal; only the message is new.
        """
        # The command this request will actually run. It said `docker restart
        # dcgm-exporter` for every request, so a person re-sent a plan was shown a
        # restart and approved something other than what they read.
        if cycle.command:
            if not self._valid_review_binding(cycle) or cycle.stage != "awaiting_answer":
                return False
            text = self._plan_note(cycle)["card"]
            approve = "Approve"
        else:
            text = _catalogue_card(cycle.bdf, str(cycle.proposal_id))
            approve = "Approve restart"
        try:
            receipt = self.telegram.send_message(
                self.group_id, text,
                approve_callback=(approve, f"approve:{cycle.proposal_id}:{cycle.nonce}"),
                deny_callback=("Leave it", f"deny:{cycle.proposal_id}:{cycle.nonce}"),
            )
        except Exception:
            return False
        # The newest message is the one carrying live buttons, so it is the one whose
        # buttons get cleared when this ends.
        self.cycles.update(cycle.cycle_id, self.clock(), message_id=receipt.message_id)
        return True

    def _offer_withdrawal(self, bdf: str) -> str:
        """Ask before taking a request back, because a question is not an instruction.

        Every other steer is recoverable -- a pause is undone by resuming, a hold by
        releasing, a look changes nothing -- which is what made it safe to read them
        out of someone's words with a model. Withdrawal is not. It destroys a pending
        approval, and the only way back is a whole new cycle.

        The model got it wrong FOURTEEN times: 14 of 19 cycles on this machine ended
        `withdrawn_by_operator`, against zero executions ever. Asking "do we actually
        need a restart though?" reads a great deal like wanting it cancelled, and the
        cost of that reading was the restart this service exists to perform never once
        running. The docstring above claimed the worst a misreading costs is "a look
        nobody wanted or a pause you undo". For this one entry that was never true.

        So the model no longer carries it out. It says what it thinks was meant, and a
        deliberate answer bound to the one proposal decides -- which the request is
        already carrying, as the button beside Approve.
        """
        cycle = self.cycles.active()
        if cycle is None or (bdf and cycle.bdf != bdf):
            return f"Nothing is waiting on {bdf}." if bdf else "Nothing is waiting."
        if cycle.stage not in ("awaiting_backup", "awaiting_answer"):
            return f"{cycle.bdf} is past the point where I can take that back."
        return (
            f"It sounds like you may want the {cycle.bdf} restart request taken back. "
            "I have not: it is still waiting for you, and doing nothing costs nothing. "
            "Tap Leave it on that request to drop it, or Approve to go ahead."
        )

    def _instruct(self, envelope: Any) -> None:
        """Carry out one operator instruction. None of them can cause an action."""
        self._record_input_start(envelope)
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
            waiting = self.cycles.active()
            if waiting is not None:
                # Saying "right away" while something is already in flight is a lie:
                # an override never cuts in front of a cycle under way, so nothing
                # would happen and the reason would be invisible.
                self._send(
                    f"Noted for {argument}, but I am already waiting on {waiting.bdf} "
                    f"({waiting.stage.replace('_', ' ')}). Nothing moves until that "
                    f"ends; tell me to take back the one on {waiting.bdf} and this will "
                    "then go without waiting."
                )
                return
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

    def _suspend_for_conversation(self) -> None:
        """Take back a waiting request, because the conversation may change it."""
        cycle = self.cycles.active()
        if cycle is None or cycle.stage not in ("awaiting_backup", "awaiting_answer"):
            return
        self._finish(
            cycle, WITHDRAWN_BY_OPERATOR, "withdrawn: the operator is still talking",
            notice=f"I have taken back the request for {cycle.bdf} while we talk, so "
                   "nothing is waiting on a button that might no longer mean what it "
                   "said. I will ask again when we are done.",
        )

    def _converse(self, question: str, envelope: Any) -> bool:
        """Put the operator's own words to the investigator.

        In the incident's thread when there is one, and in the machine's own otherwise.
        Requiring an incident meant a person could only be heard about faults this
        service had already found for itself -- and the whole reason a person is the
        most reliable trigger there is, is that they notice what we did not.
        """
        # An open database row is not enough to make every conversation about that
        # incident. The live target may already have recovered while lifecycle
        # confirmation is pending. Binding arbitrary questions to that old thread was
        # the reason the same handover explanation kept swallowing unrelated faults.
        try:
            current_blocked = frozenset(self.adapter.status().handover_blocked)
        except Exception:
            current_blocked = frozenset()
        # Only an explicit GPU/handover reference, or a short follow-up to a recent
        # conversation about it, inherits that incident's thread. A general question
        # about the machine must be allowed to find a different fault.
        lowered = question.lower()
        referential = (len(lowered) <= 80 and bool(re.search(r"\b(?:it|that|this)\b", lowered))
                      and not re.search(r"\b(?:backup|monitoring|exporter|service|network|disk)\b", lowered))
        mentioned = bool(re.search(r"\b(?:gpu|vfio|handover|passthrough)\b", lowered))
        live_handover = [item for item in self._open_handover_incidents()
                         if item[0] in current_blocked]
        incident = next((item for item in live_handover if item[0].lower() in lowered), None)
        if incident is None and mentioned and len(live_handover) == 1:
            incident = live_handover[0]
        if incident is None and referential:
            # Pronouns refer to this sender's last subject, not to whichever fault
            # happens to be the only open handover incident today.
            try:
                recent = json.loads(self.notes.get(f"recent-subject:{envelope.sender_id}") or "{}")
                if self.clock() - _parse(recent["at"]) <= timedelta(hours=2):
                    incident = next((item for item in live_handover
                                     if item[1] == recent.get("key")), None)
            except (KeyError, TypeError, ValueError):
                pass
        if incident is None:
            # A named fault family can bind a question to its own active episode.
            # This also covers non-GPU investigations without making an unrelated
            # machine-status question inherit whichever loop happens to be open.
            rows = self.state_db.execute(
                "SELECT dedup_key,notification_episode,fault_family FROM incidents "
                "WHERE status IN ('open','recovery_pending')"
            ).fetchall()
            matches = []
            for fault_key, fault_episode, family in rows:
                words = {word for word in re.findall(r"[a-z0-9]+", str(family).lower())
                         if len(word) >= 5 and word not in ("fault", "service", "error")}
                if words and any(re.search(rf"\b{re.escape(word)}\b", lowered)
                                 for word in words):
                    matches.append(("", str(fault_key), int(fault_episode)))
            if len(matches) == 1:
                incident = matches[0]
        if incident is not None:
            key, episode, bdf = incident[1], incident[2], incident[0]
            subject, investigation, briefing = self._last_investigation(key)
        else:
            key, episode, bdf = MACHINE_SUBJECT, 1, ""
            # One thread a day about the machine itself. A single permanent thread
            # would grow without bound; a new one for every message would forget
            # what was said a minute ago.
            subject = hashlib.sha256(
                f"{MACHINE_SUBJECT}:{self.clock().date().isoformat()}".encode()
            ).hexdigest()
            investigation = f"{MACHINE_SUBJECT}#{self.clock().date().isoformat()}"
            briefing = self._last_diagnosis_text()
        briefing = self._fresh_conversation_briefing(bdf, briefing)
        nonce = str(envelope.update_id)
        stable_ticket = self.notes.get(f"input-root:{nonce}") or None
        ticket_fn = getattr(self.conversation, "ticket", None)
        if callable(ticket_fn):
            stable_ticket = stable_ticket or ticket_fn(key, int(envelope.sender_id), question, nonce)
            if self.actions_db.execute(
                "SELECT 1 FROM tc_action_dialogue WHERE event_id=?",
                (f"ended:{stable_ticket}",),
            ).fetchone():
                return True
            self.notes.set(f"input-root:{nonce}", stable_ticket, self.clock())
            self.notes.set(f"input-nonce:{stable_ticket}", nonce, self.clock())
            if self.conversations.by_root(stable_ticket) is None:
                # Accept before publishing. The inbox remains pending until ask()
                # succeeds, and SpoolConversation's stable ticket makes retries safe.
                self.conversations.start(
                    stable_ticket, incident_key=key, episode=episode, bdf=bdf,
                    subject_hash=subject, investigation_id=investigation,
                    sender_id=int(envelope.sender_id), question=question, now=self.clock(),
                )
            else:
                # A retry must keep the accepted subject even if target status changed.
                accepted = self.conversations.by_root(stable_ticket)
                key, episode, bdf = (str(accepted["incident_key"]),
                                     int(accepted["episode"]), str(accepted["bdf"]))
                subject, investigation = (str(accepted["subject_hash"]),
                                          str(accepted["investigation_id"]))
            if self.notes.get(f"published:{stable_ticket}"):
                return True
            if self.notes.get(f"defer:{stable_ticket}"):
                return True
            live = self.observations.live_loop()
            if (live is not None and _same_observation_subject(
                    key, episode, str(live["incident_key"]), int(live["episode"]))):
                # Keep the accepted request pending until the existing investigation
                # concludes. The next tick will publish its findings into this chat.
                self.notes.set(f"defer:{stable_ticket}", str(live["loop_id"]), self.clock())
                self.notes.set(f"recent-subject:{envelope.sender_id}", json.dumps({
                    "key": key, "at": _text(self.clock()),
                }), self.clock())
                self._progress(stable_ticket, "I am looking into that.", self.clock())
                return True
        try:
            ticket = self.conversation.ask(
                incident_key=key, episode=episode, bdf=bdf,
                message=question, sender_id=envelope.sender_id,
                subject_hash=subject, briefing=briefing,
                investigation_id=investigation, nonce=nonce,
            )
        except Exception as error:  # A conversation is never worth crashing the loop.
            self.report(
                '{"operation":"actions","phase":"_converse","status":"failed",'
                f'"category":"{type(error).__name__}"}}'
            )
            return False
        if not ticket:
            return False
        if stable_ticket is not None and ticket != stable_ticket:
            self.report("conversation ticket changed after durable acceptance")
            return False
        if stable_ticket is not None:
            self.notes.set(f"published:{stable_ticket}", ticket, self.clock())
        # The answer arrives on a later pass. Waiting for it here would stop the loop
        # answering anybody else, finishing executions or delivering outcomes for as
        # long as the model thinks -- the same mistake diagnosis already made once.
        self.schedule.set(f"conversation:{ticket}", self.clock() + CONVERSATION_WAIT)
        self._conversation_sender[ticket] = int(envelope.sender_id)
        # Where this exchange is, so its reads can run and come back to the same thread.
        if stable_ticket is None:
            self.conversations.start(
                ticket, incident_key=key, episode=episode, bdf=bdf,
                subject_hash=subject, investigation_id=investigation,
                sender_id=int(envelope.sender_id), question=question, now=self.clock(),
            )
            self.notes.set(f"published:{ticket}", ticket, self.clock())
        self._progress(ticket, "I am looking into that.", self.clock())
        self.notes.set(f"recent-subject:{envelope.sender_id}", json.dumps({
            "key": key, "at": _text(self.clock()),
        }), self.clock())
        return True

    def _chat_owns(self, incident_key: str, episode: int) -> bool:
        """A published operator exchange owns its incident until it concludes.

        A question deferred behind a live observation loop cannot own that loop.
        Auto-generated and outcome threads also do not represent a new operator ask.
        """
        if incident_key in (MACHINE_SUBJECT, REVIEW_KEY):
            rows = self.actions_db.execute(
                "SELECT root FROM tc_action_conversations WHERE incident_key IN (?,?)",
                (MACHINE_SUBJECT, REVIEW_KEY),
            ).fetchall()
        else:
            rows = self.actions_db.execute(
                "SELECT root FROM tc_action_conversations WHERE incident_key=? AND episode=?",
                (incident_key, episode),
            ).fetchall()
        for (root,) in rows:
            root = str(root)
            if (not root.startswith(("auto-", "outcome:"))
                    and self.notes.get(f"published:{root}")
                    and not self.notes.get(f"defer:{root}")):
                return True
        return False

    def _chat_evidence_revision(self, incident_key: str, bdf: str) -> str:
        row = self.state_db.execute(
            "SELECT stable_signature,status FROM incidents WHERE dedup_key=?",
            (incident_key,),
        ).fetchone()
        if row is None or str(row[1]) not in ("open", "recovery_pending"):
            return ""
        try:
            current = fault_revision(self.adapter.status(), bdf)
        except Exception:
            return ""
        return hashlib.sha256(json.dumps((str(row[0]), current)).encode()).hexdigest()

    def _chat_concluded_same_evidence(self, incident_key: str, episode: int,
                                      bdf: str = "") -> bool:
        saved = self.notes.get(f"chat-concluded:{incident_key}:{episode}")
        if not saved:
            return False
        try:
            marker = json.loads(saved)
        except ValueError:
            return False
        revision = self._chat_evidence_revision(incident_key, str(marker.get("bdf") or bdf))
        return bool(revision and revision == marker.get("revision"))

    def _keep_chat_finding(self, exchange: Mapping[str, Any], finding: str,
                           now: datetime) -> None:
        key, episode = str(exchange["incident_key"]), int(exchange["episode"])
        if key == MACHINE_SUBJECT or not finding.strip():
            return
        bdf = str(exchange["bdf"])
        revision = self._chat_evidence_revision(key, bdf)
        diagnosis_revision = self._diagnosis_revision(key) or revision
        self.evidence.record("diagnosis", f"incident:{key}", {
            "incident_key": key, "episode": episode, "summary": scrub(finding)[:1200],
            "source": "operator-chat", "subject_hash": str(exchange["subject_hash"]),
            "investigation_id": str(exchange["investigation_id"]),
            "conversation_root": str(exchange["root"]), "evidence_hash": diagnosis_revision,
            "recorded_at": _text(now),
        })
        if revision:
            self.notes.set(f"chat-concluded:{key}:{episode}",
                           json.dumps({"revision": revision, "bdf": bdf}), now)

    def _resume_deferred_conversations(self) -> None:
        """Publish accepted questions after their same-subject investigation settles."""
        now = self.clock()
        for name, loop_id in self.actions_db.execute(
            "SELECT name,value FROM tc_action_notes WHERE name LIKE 'defer:%'"
        ).fetchall():
            root = str(name)[len("defer:"):]
            exchange = self.conversations.by_root(root)
            if exchange is None:
                self.notes.clear(str(name))
                continue
            live = self.observations.live_loop()
            if live is not None and str(live["loop_id"]) == loop_id:
                continue
            if self.conversation is None:
                continue
            key = str(exchange["incident_key"])
            background_key = (REVIEW_KEY if key == MACHINE_SUBJECT else key)
            _subject, _investigation, finding = self._last_investigation(background_key)
            briefing = self._fresh_conversation_briefing(str(exchange["bdf"]), finding)
            try:
                ticket = self.conversation.ask(
                    incident_key=key, episode=int(exchange["episode"]),
                    bdf=str(exchange["bdf"]), message=str(exchange["question"]),
                    sender_id=int(exchange["sender_id"]),
                    subject_hash=str(exchange["subject_hash"]), briefing=briefing,
                    investigation_id=str(exchange["investigation_id"]),
                    nonce=self.notes.get(f"input-nonce:{root}") or "",
                )
            except Exception as error:
                self.report(f"deferred conversation publication failed: {type(error).__name__}")
                continue
            if ticket != root:
                self.report("deferred conversation ticket changed")
                continue
            self.notes.set(f"published:{root}", root, now)
            self.notes.clear(str(name))
            self.schedule.set(f"conversation:{root}", now + CONVERSATION_WAIT)

    def _fresh_conversation_briefing(self, bdf: str, historical: str) -> str:
        """Put present state ahead of the incident thread's historical conclusion."""
        try:
            status = self.adapter.status()
        except Exception as error:
            current = (
                "CURRENT TARGET STATUS: unavailable "
                f"({type(error).__name__}). Do not claim the historical diagnosis is "
                "still true without current evidence."
            )
        else:
            blocked = ", ".join(status.handover_blocked) or "none"
            lines = [
                f"CURRENT TARGET STATUS (authoritative for present-tense claims) at "
                f"{_text(status.observed_at)}:",
                f"identity_verified: {status.identity_verified}",
                f"dcgm-exporter present: {status.container.present}; running: "
                f"{status.container.running}",
                f"handover_blocked: {blocked}",
            ]
            if bdf:
                lines.append(
                    (f"The current status confirms {bdf} is handover-blocked."
                     if bdf in status.handover_blocked else
                     f"The current status does NOT confirm the historical handover "
                     f"fault on {bdf}.")
                )
            full_status = self._latest_full_status_text(self.clock())
            if full_status:
                lines.append(full_status)
            metrics = self._latest_prometheus_stats_text(self.clock())
            if metrics:
                lines.append(metrics)
            current = "\n".join(lines)
        if not historical:
            return current
        return (
            current
            + "\n\nHISTORICAL DIAGNOSIS (context only; not proof it is still true):\n"
            + historical
        )

    def _collect_conversations(self) -> None:
        """Say what came back, run what it asked to look at, and admit it when nothing did."""
        now = self.clock()
        if self.conversation is None:
            return
        for root in self.conversations.stale(now - CONVERSATION_LIFETIME):
            try:
                pending_review = json.loads(self.notes.get(f"review:{root}") or "{}")
            except ValueError:
                pending_review = {}
            if pending_review.get("ready"):
                continue
            self._queue_terminal(root,
                "I could not finish that investigation within its time limit. Ask for a fresh look.", now)
            self.conversations.end(root)
        for name, due in self.schedule.pending("conversation:"):
            ticket = name[len("conversation:"):]
            try:
                answer = self.conversation.collect(ticket)
            except Exception as error:
                self.report(
                    '{"operation":"actions","phase":"_collect_conversations",'
                    f'"status":"failed","category":"{type(error).__name__}"}}'
                )
                continue
            if answer is not None and (
                answer.text or answer.steer is not None
                or getattr(answer, "reads", ()) or getattr(answer, "plan", None) is not None
                or getattr(answer, "plan_problem", "") or getattr(answer, "progress", "")
                or getattr(answer, "operator_text", None) is not None
            ):
                self.schedule.clear(name)
                self._answered(ticket, answer, now)
            elif now >= due:
                self.schedule.clear(name)
                exchange = self.conversations.by_ticket(ticket)
                if exchange is not None:
                    self._queue_terminal(str(exchange["root"]),
                        self.notes.get(f"conclusion:{exchange['root']}") or
                        "I could not get an answer in time. Ask me for a fresh look.", now)
                    self.conversations.end(str(exchange["root"]))

    def _answered(self, ticket: str, answer: Any, now: datetime) -> None:
        """Deliver one conversational turn, and carry out what it asked for."""
        exchange = self.conversations.by_ticket(ticket)
        if exchange is None:
            return
        concluding = self.notes.get(f"conclusion:{exchange['root']}")
        if concluding:
            self._record_internal(exchange, ticket, "conclusion-source", str(answer.text), now)
            source = str(answer.text).strip()
            conclusion = (source.split("\n", 1)[1].strip()
                          if source.startswith("OPERATOR CONCLUSION:\n") else "")
            if len(conclusion) > 1200 or "```" in conclusion:
                conclusion = ""
            if conclusion and operator_prose_problem(conclusion):
                conclusion = ""
            established = self.notes.get(f"conclusion-finding:{exchange['root']}") or ""
            if conclusion and established and established not in conclusion:
                conclusion = established + "\n\n" + conclusion
            self._queue_terminal(str(exchange["root"]),
                conclusion or concluding, now)
            self._guard(self._keep_chat_finding, exchange, conclusion or concluding, now)
            self.notes.clear(f"conclusion:{exchange['root']}")
            self.notes.clear(f"conclusion-finding:{exchange['root']}")
            self.conversations.end(str(exchange["root"]))
            return
        sender = self._conversation_sender.pop(ticket, None)
        if sender is None:
            sender = int(exchange["sender_id"]) if exchange is not None else 0
        reads = tuple(getattr(answer, "reads", ()) or ())
        plan = getattr(answer, "plan", None)
        problem = str(getattr(answer, "plan_problem", "") or "")
        done = ""
        live = self.observations.live_loop()
        same_background = (live is not None and _same_observation_subject(
            str(exchange["incident_key"]), int(exchange["episode"]),
            str(live["incident_key"]), int(live["episode"])))
        if (answer.steer is not None and answer.steer.name == "investigate"
                and (reads or same_background)):
            # This turn already owns the question, or an existing healthy loop is
            # gathering the same evidence. A second machine review adds no work.
            self._record_internal(exchange, ticket, "coordination",
                                  "investigate steer joined with active reads/loop", now)
        elif answer.steer is not None and sender == CONSOLE_SENDER \
                and answer.steer.name in CONSOLE_FORBIDDEN_STEERS:
            # Lifting a pause or a hold is a person's decision, made in Telegram.
            done = (f"It asked to {answer.steer.name}; that is for the operator to do in "
                    "Telegram, not the console, so I did not.")
        elif answer.steer is not None:
            try:
                # Here, not when they spoke: what they asked for may change what a
                # waiting button would mean, and a question does not. Nor does every
                # steer -- resuming or releasing enables acting, so taking back the
                # request that was waiting to be enabled is backwards, and
                # `investigate` is about a different subject.
                if answer.steer.name in SUSPENDS_A_REQUEST:
                    self._suspend_for_conversation()
                done = self._steer(
                    answer.steer.name, answer.steer.argument, sender,
                    question=str(exchange["question"]) if exchange is not None else "",
                )
            except Exception as error:
                self.report(
                    '{"operation":"actions","phase":"_steer","status":"failed",'
                    f'"category":"{type(error).__name__}"}}'
                )
                done = "I could not do that just now."
        looking = ""
        blocker = ""
        continues = False
        if reads:
            if exchange is None or self.observer is None:
                blocker = "The host read channel is unavailable, so I cannot complete this look."
            elif int(exchange["round"]) >= MAX_CHAT_READ_ROUNDS:
                blocker = "The read budget is exhausted; I cannot verify the remaining question."
            else:
                round = int(exchange["round"]) + 1
                self.conversations.ask(str(exchange["root"]), round, reads, now)
                continues = True
                # Said, so the operator can see what is being run on their machine and
                # that the silence that follows is work rather than nothing.
                looking = ""
        if problem:
            looking = "\n".join(part for part in (
                looking, f"It wrote a plan I could not take: {problem}"
            ) if part)
        # Nothing it asked for is dropped without a word -- to it or to you. A read or a
        # plan refused here goes back into the same conversation with the reason, so it
        # can fix it and carry on, instead of the exchange ending as if it had finished.
        refused = [str(item) for item in (getattr(answer, "read_problems", ()) or ())]
        if problem:
            refused.append(f"your plan was not accepted: {problem}")
        if refused:
            looking = "\n".join(part for part in (
                looking, "Not run: " + "; ".join(refused)
            ) if part)
            if exchange is not None and continues:
                self.notes.set(f"refused:{exchange['root']}", "\n".join(refused), now)
            elif (
                exchange is not None and self.conversation is not None
                and int(exchange["round"]) < MAX_CHAT_READ_ROUNDS + MAX_CHAT_CORRECTIONS
            ):
                round = int(exchange["round"]) + 1
                self.conversations.ask(str(exchange["root"]), round, (), now)
                self._put_to_conversation(
                    exchange, f"{exchange['root']}:refused{round}",
                    "Some of what you asked for was not accepted, so nothing from it "
                    "ran:\n- " + "\n- ".join(refused)
                    + "\n\nFix it and carry on: ask again in a ```reads, ```read-script "
                    "or ```plan block within the limits, or answer the operator.",
                    now,
                )
                continues = True
        # Their answer first, then what actually happened: the words are the model's
        # and the doing is mine, and a person should be able to tell which is which.
        self._record_internal(exchange, ticket, "reply", json.dumps({
            "text": answer.text, "reads": reads, "plan_problem": problem,
            "refused": refused, "steer": str(answer.steer),
            "operator_text": getattr(answer, "operator_text", None),
        }), now)
        if (not continues and plan is None and not problem and answer.steer is None
                and getattr(answer, "contract_required", False)
                and (getattr(answer, "operator_text", None) is None or
                     operator_prose_problem(str(answer.operator_text)))):
            corrections = int(self.notes.get(f"corrections:{exchange['root']}") or 0)
            if corrections < MAX_CHAT_CORRECTIONS and self.conversation is not None:
                self.notes.set(f"corrections:{exchange['root']}", str(corrections + 1), now)
                self.conversations.ask(str(exchange["root"]), int(exchange["round"]) + 1,
                                       (), now)
                self._put_to_conversation(exchange,
                    f"{exchange['root']}:operator{corrections}",
                    "Your internal answer did not include one complete bounded "
                    "```operator block. Preserve the established findings and answer "
                    "the operator's question in that block, at most 1200 characters. "
                    "State the finding, blocker and next requirement in short "
                    "paragraphs. No commands, code, read output or review discussion. "
                    f"Correction reason: {getattr(answer, 'operator_problem', '') or operator_prose_problem(str(getattr(answer, 'operator_text', '') or '')) or 'missing operator block'}. "
                    f"Their question: {exchange['question']}\n\n"
                    f"Your internal answer: {str(answer.text)[:4000]}", now)
                continues = True
            else:
                self._queue_terminal(str(exchange["root"]),
                    (self.notes.get(f"finding:{exchange['root']}") or
                     "I could not establish a concise conclusion from this look.") +
                    "\n\nA further look is needed before I can answer safely.", now)
                self.conversations.end(str(exchange["root"]))
                return
        if continues:
            update = ("I need to correct part of the plan before I can answer." if refused
                      else str(getattr(answer, "progress", "") or ""))
            progress_problem = operator_prose_problem(update)
            if progress_problem:
                self.notes.set(f"refused:{exchange['root']}",
                    "Your progress message contains " + progress_problem
                    + ". Give the operator a short action-and-impact update without commands.", now)
                update = ""
            self._progress(str(exchange["root"]), update, now)
        elif not plan and not problem and not self.notes.get(f"reviewed:{exchange['root']}"):
            finding = (done or getattr(answer, "operator_text", None)
                       or ("" if getattr(answer, "contract_required", False) else answer.text)
                       or str(getattr(answer, "progress", "") or ""))
            prior = self.notes.get(f"finding:{exchange['root']}")
            if str(exchange["root"]).startswith("auto-") and prior and prior not in finding:
                finding = prior + "\n\n" + finding
            self._queue_terminal(str(exchange["root"]),
                "\n\n".join(part for part in (finding, blocker) if part) or
                "I could not reach a conclusion from the available evidence.", now)
            if finding and not blocker:
                self._guard(self._keep_chat_finding, exchange, finding, now)
        elif problem and not continues:
            self._queue_terminal(str(exchange["root"]),
                                 "\n\n".join(part for part in (
                                     (getattr(answer, "operator_text", None) or
                                      self.notes.get(f"finding:{exchange['root']}") or
                                      "The proposed change is incomplete."),
                                     "The change is withheld because the plan is incomplete."
                                 ) if part), now)
        if plan is not None and continues:
            # It asked to look and proposed at once. Reviewing a plan before the reads it
            # just asked for come back reviews a plan it may be about to change.
            self.notes.set(
                f"refused:{exchange['root']}",
                "\n".join(filter(None, (
                    self.notes.get(f"refused:{exchange['root']}"),
                    "your plan was not sent for review, because you also asked for "
                    "reads; send it again once you have their output, if it still stands",
                ))), now,
            )
        elif plan is not None and exchange is not None:
            # Reviewed before it reaches a person. The conversation stays open so the
            # review can come back into it.
            public_finding = (getattr(answer, "operator_text", None) or
                              plan.summary if getattr(answer, "contract_required", False)
                              else answer.text)
            continues = self._send_for_review(exchange, plan, answer.text, now,
                                              public_finding=public_finding)
        if exchange is not None and not continues and plan is None:
            kept = self.notes.get(f"reviewed:{exchange['root']}")
            if kept:
                # No plan block is not an affirmative decision to keep the old plan.
                # A disavowal or an exhausted read budget must never resurrect it.
                self.notes.clear(f"reviewed:{exchange['root']}")
                self.notes.clear(f"revisions:{exchange['root']}")
                self._queue_terminal(str(exchange["root"]),
                                     "The earlier change is withheld because no reviewed revision was submitted.", now)
        if exchange is not None and not continues:
            self.conversations.end(str(exchange["root"]))

    def _send_for_review(
        self, exchange: Mapping[str, Any], plan: ProposedAction, answer: str, now: datetime,
        *, public_finding: str = "",
    ) -> bool:
        """Ask the escalation model to review a plan before a person is asked."""
        return self._queue_review(
            plan, headline=((public_finding or answer).strip().splitlines() or ["A plan"])[0],
            question=str(exchange["question"]), bdf=str(exchange["bdf"]),
            incident_key=str(exchange["incident_key"]), episode=int(exchange["episode"]),
            now=now, exchange=exchange, reasoning=answer,
        )

    def _collect_reviews(self) -> None:
        """Revise internally within a bound; offer only an affirmatively reviewed plan."""
        now = self.clock()
        rows = self.actions_db.execute(
            "SELECT name, value FROM tc_action_notes WHERE name LIKE 'review:%'"
        ).fetchall()
        for name, value in rows:
            root = str(name)[len("review:"):]
            try:
                pending = json.loads(value)
            except ValueError:
                self.notes.clear(str(name))
                continue
            exchange = self.conversations.by_root(root)
            if pending.get("turn_ticket") and (
                exchange is None or str(exchange["ticket"]) != pending["turn_ticket"]
            ):
                self.notes.clear(str(name))
                continue
            incident_key = str(pending.get("incident_key") or "")
            if self._autonomous_review_stale(pending):
                self.notes.clear(str(name))
                if pending.get("review_event"):
                    self.notes.clear(f"grant:{pending['review_event']}")
                self.notes.clear(f"revisions:{root}")
                self.notes.clear(f"reviewed:{root}")
                self.notes.clear(f"finding:{root}")
                self._queue_terminal(root,
                    str(pending.get("finding") or "The earlier finding has changed.")
                    + "\n\nThe proposal is withheld because the incident or its evidence changed. "
                    "A fresh look is needed before another action.", now,
                    incident_key, int(pending.get("episode") or 0))
                if exchange is not None:
                    self.conversations.end(root)
                continue
            if pending.get("ready"):
                self._offer_ready_review(root, pending, exchange, now)
                continue
            review = None
            late = False
            if pending.get("ticket") and self.reviewer is not None:
                try:
                    review = self.reviewer.collect(str(pending["ticket"]))
                except Exception:
                    review = None
            if review is None:
                late = now - _parse(str(pending["asked_utc"])) > REVIEW_WAIT
                if pending.get("ticket") and not late:
                    continue
                review = Review("", "The reviewer did not return an affirmative verdict in time.")
            parsed_verdict = parse_review(review.text).verdict
            if parsed_verdict != review.verdict:
                review = Review("", review.text)
            revisions = int(pending.get("revisions") or 0)
            review_event = hashlib.sha256(
                f"{root}:{pending.get('ticket')}:{revisions}:review".encode()).hexdigest()
            self.actions_db.execute("""INSERT OR IGNORE INTO tc_action_dialogue
                (event_id,root,ticket,subject,episode,kind,text,created_utc)
                VALUES(?,?,?,?,?,?,?,?)""", (
                review_event, root, str(pending.get("turn_ticket") or ""),
                str(pending.get("incident_key") or ""), int(pending.get("episode") or 0),
                "review", scrub(review.text)[:MAX_NOTE_CHARS], _text(now),
            ))
            self.actions_db.commit()
            if (
                review.verdict == "revise" and revisions < MAX_PLAN_REVISIONS
                and exchange is not None and self.conversation is not None
            ):
                self.notes.clear(str(name))
                self.notes.set(f"revisions:{root}", str(revisions + 1), now)
                # Keep the original internally for the correction turn. Prose alone
                # does not resubmit it for approval.
                self.notes.set(f"reviewed:{root}", json.dumps(dict(
                    pending, review=review.text[:3000])), now)
                self.conversations.ask(root, int(exchange["round"]) + 1, (), now)
                self._put_to_conversation(
                    exchange, f"{root}:review{revisions}",
                    "An independent reviewer (a different model, Astra) examined your "
                    "plan before it goes to the operator. Established finding: "
                    f"{pending.get('finding') or pending.get('headline')}\n"
                    f"Current proposal: {pending.get('command', '')[:8000]}\n"
                    f"Rollback: {pending.get('rollback', '')[:2000]}\n"
                    f"Verification: {pending.get('verify', [])[:4]}\n"
                    f"Action: {pending.get('summary', '')}\n"
                    f"Impact: {pending.get('impact', '')}\n\nIts review:\n\n"
                    f"{review.text[:8000]}\n\nWeigh it on the evidence. Fix what it is "
                    "right about -- look again with reads if you need to -- and push "
                    "back where it is wrong. Then send the revised plan in a ```plan "
                    "block. If you defend the original plan, explain why and resend "
                    "that exact plan in a ```plan block. Prose alone never resubmits "
                    "an earlier plan for approval.",
                    now,
                )
                continue
            if review.verdict != "approve":
                self.notes.clear(str(name))
                self._conclude_review(root, pending, review.text,
                                      None if late or not pending.get("ticket") else exchange, now)
                self.notes.clear(f"revisions:{root}")
                self.notes.clear(f"reviewed:{root}")
                self.notes.clear(f"finding:{root}")
                if (late or not pending.get("ticket")) and exchange is not None:
                    self.conversations.end(root)
                continue
            plan = ProposedAction(
                str(pending["command"]), str(pending.get("intent") or ""),
                str(pending.get("rollback") or ""),
                tuple(str(item) for item in pending.get("verify") or ()),
                str(pending.get("summary") or ""), str(pending.get("impact") or ""),
            )
            bdf = str(pending.get("bdf") or "")
            incident_key = str(pending.get("incident_key") or MACHINE_SUBJECT)
            episode = int(pending.get("episode") or 1)
            if self._action_binding(plan, bdf, incident_key, episode) != pending.get("binding"):
                self.notes.clear(str(name))
                self.notes.clear(f"revisions:{root}")
                self.notes.clear(f"reviewed:{root}")
                self.notes.clear(f"finding:{root}")
                self._queue_terminal(root, "The proposal changed during review, so the action is withheld.",
                                     now, incident_key, episode)
                if exchange is not None:
                    self.conversations.end(root)
                continue
            self.notes.set(f"grant:{review_event}", json.dumps({
                "binding": pending["binding"], "ticket": pending.get("ticket"),
                "root": root, "approved_utc": _text(now),
            }), now)
            pending["ready"] = True
            pending["review_event"] = review_event
            self.notes.set(str(name), json.dumps(pending), now)
            self._offer_ready_review(root, pending, exchange, now)

    def _offer_ready_review(self, root: str, pending: Mapping[str, Any],
                            exchange: Mapping[str, Any] | None, now: datetime) -> None:
        """Retry an exact reviewed plan after an unrelated card has finished."""
        plan = ProposedAction(
            str(pending["command"]), str(pending.get("intent") or ""),
            str(pending.get("rollback") or ""),
            tuple(str(item) for item in pending.get("verify") or ()),
            str(pending.get("summary") or ""), str(pending.get("impact") or ""),
        )
        bdf = str(pending.get("bdf") or "")
        incident_key = str(pending.get("incident_key") or MACHINE_SUBJECT)
        episode = int(pending.get("episode") or 1)
        review_event = str(pending["review_event"])
        if self._action_binding(plan, bdf, incident_key, episode) != pending.get("binding"):
            self.notes.clear(f"review:{root}")
            self.notes.clear(f"grant:{review_event}")
            self.notes.clear(f"revisions:{root}")
            self.notes.clear(f"reviewed:{root}")
            self.notes.clear(f"finding:{root}")
            self._queue_terminal(root,
                str(pending.get("finding") or pending.get("headline") or "A change was identified.")
                + "\n\nThe reviewed proposal changed, so it is withheld. A fresh plan is needed.",
                now, incident_key, episode)
            if exchange is not None:
                self.conversations.end(root)
            return
        offered = self._request_approval(
            plan, headline=str(pending.get("headline") or "A plan"), body="",
            bdf=bdf, incident_key=incident_key, episode=episode, now=now,
            reviewed_binding=str(pending["binding"]), review_event=review_event,
            explicit_request=bool(pending.get("explicit_request")),
            conversation={key: exchange[key] for key in (
                "root", "incident_key", "episode", "bdf", "subject_hash",
                "investigation_id", "sender_id",
            )} if exchange is not None else None,
        )
        if not offered and self.actor is not None and self.cycles.active() is None:
            self.notes.clear(f"review:{root}")
            self.notes.clear(f"grant:{review_event}")
            self.notes.clear(f"revisions:{root}")
            self.notes.clear(f"reviewed:{root}")
            self.notes.clear(f"finding:{root}")
            self._queue_terminal(root,
                str(pending.get("finding") or pending.get("headline") or "A change was identified.")
                + "\n\nThe reviewed change could not be offered. Ask for a fresh review before acting.",
                now, incident_key, episode)
            if exchange is not None:
                self.conversations.end(root)
            return
        if not offered:
            return
        self.notes.clear(f"review:{root}")
        self.notes.clear(f"revisions:{root}")
        self.notes.clear(f"reviewed:{root}")
        self.notes.clear(f"finding:{root}")
        if exchange is not None:
            self._guard(self._keep_chat_finding, exchange,
                        str(pending.get("finding") or ""), now)
            self.conversations.end(root)

    def _conclude_review(self, root: str, pending: Mapping[str, Any],
                         critique: str, exchange: Mapping[str, Any] | None,
                         now: datetime) -> None:
        finding = str(pending.get("finding") or pending.get("headline") or
                      "A possible change was identified.").strip()
        fallback = (finding + "\n\nThe proposed action is withheld because its effect or "
                    "recovery is not established. A corrected plan with supporting "
                    "evidence is needed before approval.")
        if exchange is not None and self.conversation is not None:
            self.notes.set(f"conclusion:{root}", fallback, now)
            self.notes.set(f"conclusion-finding:{root}", finding[:1200], now)
            self.conversations.ask(root, int(exchange["round"]) + 1, (), now)
            self._put_to_conversation(exchange, f"{root}:conclusion", (
                "Your proposal cannot be offered for approval. Preserve the established "
                f"finding: {finding[:1200]}\n\nIn at most three short plain paragraphs, tell the operator "
                "what you found, the actual operational blocker, and what evidence or "
                "changed plan is needed next. Do not include a plan, commands, code, "
                "review process, or critique quotation. Begin with exactly "
                "OPERATOR CONCLUSION: on its own line.\n\nInternal review:\n" +
                critique[:6000]), now)
            return
        self._queue_terminal(root, fallback, now,
            str(pending.get("incident_key") or ""), int(pending.get("episode") or 0))

    def _continue_conversation(self, root: str, round: int, now: datetime) -> None:
        """Hand a finished round of reads back into the thread that asked for them."""
        exchange = self.conversations.by_root(root)
        if exchange is None or self.conversation is None:
            return
        prompt = conversation_followup_prompt(
            self.conversations.results(root, round),
            last_round=round >= MAX_CHAT_READ_ROUNDS,
            question=str(exchange["question"]),
        )
        refused = self.notes.get(f"refused:{root}")
        if refused:
            prompt += ("\n\nNot run, because it was not accepted:\n- "
                       + refused.replace("\n", "\n- "))
            self.notes.clear(f"refused:{root}")
        self._put_to_conversation(exchange, f"{root}:r{round}", prompt, now)

    def _put_to_conversation(
        self, exchange: Mapping[str, Any], seed: str, prompt: str, now: datetime
    ) -> None:
        """One more turn in an exchange's thread; its answer is collected like any other."""
        try:
            ticket = self.conversation.ask(
                incident_key=str(exchange["incident_key"]), episode=int(exchange["episode"]),
                bdf=str(exchange["bdf"]), message=seed,
                sender_id=int(exchange["sender_id"]),
                subject_hash=str(exchange["subject_hash"]),
                investigation_id=str(exchange["investigation_id"]), prompt=prompt,
            )
        except Exception as error:
            self.report(
                '{"operation":"actions","phase":"_continue_conversation","status":"failed",'
                f'"category":"{type(error).__name__}"}}'
            )
            fallback = self.notes.get(f"conclusion:{exchange['root']}")
            if fallback:
                self._queue_terminal(str(exchange["root"]), fallback, now)
            self.conversations.end(str(exchange["root"]))
            if not fallback:
                self._queue_terminal(str(exchange["root"]),
                    (self.notes.get(f"finding:{exchange['root']}") or
                     "I found a possible change.") +
                    "\n\nThe action is withheld while the investigator is unavailable. "
                    "A complete, supported proposal is needed.", now)
            return
        self.conversations.advance(str(exchange["root"]), ticket, now)
        self.schedule.set(f"conversation:{ticket}", now + CONVERSATION_WAIT)

    # A conversation answers; it never authorises. Approval stays a button bound to an
    # exact proposal and nonce, because tenant-controlled text shares this channel and
    # must never be able to imitate the operator.

    def _last_investigation(self, incident_key: str) -> tuple[str, str, str]:
        """Which investigation this incident is on, what it is, and what it concluded."""
        row = self.state_db.execute(
            """SELECT document_json FROM tc_action_evidence
               WHERE kind='diagnosis' AND subject=?
               ORDER BY recorded_utc DESC, rowid DESC LIMIT 1""",
            (f"incident:{incident_key}",),
        ).fetchone()
        if row is None:
            return "", "", ""
        document = json.loads(bytes(row[0]) if isinstance(row[0], (bytes, memoryview)) else row[0])
        briefing = "\n".join(
            line for line in (
                document.get("summary") or "",
                document.get("mechanism") or "",
                f"It wanted: {document['action']}" if document.get("action") else "",
                f"Durable fix: {document['durable']}" if document.get("durable") else "",
            ) if line
        )
        return (
            str(document.get("subject_hash") or ""),
            str(document.get("investigation_id") or ""),
            briefing,
        )

    def _last_diagnosis_text(self) -> str:
        """What it last concluded, straight from the evidence it kept.

        It says what the conclusion was about. A look somebody asked for is recorded
        the same way a fault's diagnosis is, so the newest one is often about the whole
        machine rather than the incident being asked about -- and "reached no
        conclusion", handed back without saying what it was looking at, reads as the
        current word on a fault it never examined.
        """
        row = self.state_db.execute(
            """SELECT document_json FROM tc_action_evidence WHERE kind='diagnosis'
               ORDER BY recorded_utc DESC, rowid DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return "I have not diagnosed anything yet."
        document = json.loads(bytes(row[0]) if isinstance(row[0], (bytes, memoryview)) else row[0])
        key = str(document.get("incident_key") or "")
        about = ("the machine, because you asked me to look" if key == REVIEW_KEY
                 else "the machine" if key == MACHINE_SUBJECT else
                 "the most recently investigated fault")
        if not document.get("summary"):
            return f"My last look at {about} reached no conclusion."
        lines = [
            f"About {about}:",
            document["summary"],
            document.get("mechanism") or "",
            (f"Wanted: {document['action_summary']}" if document.get("action_summary")
             else ""),
            document.get("action_impact") or "",
            (f"Durable option: {document['durable_summary']}" if document.get("durable_summary")
             else ""),
            document.get("durable_impact") or "",
        ]
        return "\n".join(line for line in lines if line)

    def _answer_question(self, envelope: Any) -> None:
        """Answer one question from the group. Nothing it says can cause an action."""
        question = str(envelope.nonce or "").strip()
        self._record_input_start(envelope)
        if not question:
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        # A present-tense question goes to the model like any other. It used to get a
        # canned status dump, so "what's wrong with the machine?" -- the question asked
        # most -- never reached anything that could look. The stale-diagnosis problem
        # that shortcut guarded against is handled in the briefing, which puts current
        # status first and marks earlier conclusions as history, and by the model now
        # being able to read the machine for itself.
        if self.conversation is None and _asks_current_state(question):
            self._reply_terminal(envelope, self._current_machine_text())
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        # Only a conversation that actually reaches the investigator can change what a
        # request means, so only that withdraws one. A question answered from evidence
        # already gathered changes nothing and should cost nothing.
        if self.conversation is not None and self._converse(question, envelope):
            self.backend.mark_handled(self.namespace, envelope.update_id)
            # Deliberately not withdrawing a waiting request here. Taking it back
            # whenever anybody spoke meant asking about a proposal cancelled it, so a
            # request could never survive being enquired about: three in a row were
            # withdrawn by somebody asking what was going on. A question does not
            # change what the button means. Something that does -- a hold, a pause, a
            # fresh look -- withdraws it when the answer comes back carrying it.
            return
        # Once a durable root exists, a failed publication is a retryable input.
        # Falling through to a canned answer would consume it while its chat row
        # remained live, leaving the actual operator request stranded.
        if self.notes.get(f"input-root:{envelope.update_id}"):
            return
        # The model is how words become instructions, so when it cannot be reached the
        # machine would stop being steerable by anything -- exactly when somebody is
        # most likely to be telling it to stop. This understands almost nothing on
        # purpose: whole-message matches only, so "don't pause" is not a pause.
        plain = _PLAIN_STEER.get(" ".join(question.lower().split()).strip(" .!"))
        if plain is not None and not (
            int(envelope.sender_id) == CONSOLE_SENDER and plain in CONSOLE_FORBIDDEN_STEERS
        ):
            done = self._steer(plain, "", envelope.sender_id)
            self._reply_terminal(envelope,
                f"{done}\n\n(I could not reach the investigator, so I took that "
                "plainly rather than thinking about it.)"
            )
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        if self.assistant is None:
            self._reply_terminal(envelope,
                "I have no model to think with right now. Here is what I last concluded:\n"
                + self._last_diagnosis_text()
            )
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        context = self._question_context()
        answer = self.assistant.answer(question, context, subject=str(envelope.sender_id))
        if answer is None:
            self._reply_terminal(envelope,
                "I could not reach the model to answer that. Here is what I last "
                "concluded:\n" + self._last_diagnosis_text()
            )
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        self._reply_terminal(envelope, answer)
        self.backend.mark_handled(self.namespace, envelope.update_id)

    def _reply_terminal(self, envelope: Any, message: str) -> None:
        self._queue_terminal(f"input:{envelope.update_id}", message, self.clock(),
                             str(envelope.subject_id or ""))

    def _current_machine_text(self) -> str:
        """A deterministic live answer, kept separate from historical diagnosis prose."""
        try:
            status = self.adapter.status()
        except Exception as error:
            return (
                "I could not read the target live, so I cannot honestly say what is "
                f"wrong right now ({type(error).__name__}). My earlier diagnoses are "
                "historical until a fresh status check succeeds."
            )
        lines = [f"Fresh target status at {_text(status.observed_at)}:"]
        if not status.identity_verified:
            lines.append("The target identity did not verify, so I trust no machine-state claim.")
            return "\n".join(lines)
        faults = self._current_faults(status.observed_at)
        if faults:
            lines.append("Current problems from fresh monitoring:")
            lines.extend(f"- {fault}" for fault in faults)
        else:
            lines.append("No other fresh open fault is currently recorded by monitoring.")
        full_status = self._latest_full_status_text(status.observed_at)
        if full_status:
            lines.append(full_status)
        metrics = self._latest_prometheus_stats_text(status.observed_at)
        if metrics:
            lines.append(metrics)
        if not status.container.present:
            lines.append("The dcgm-exporter container is not present.")
        elif not status.container.running:
            lines.append("The dcgm-exporter container is present but not running.")
        else:
            lines.append("The dcgm-exporter container is present and running.")
        if status.handover_blocked:
            lines.append(
                "Currently handover-blocked GPU(s): "
                + ", ".join(status.handover_blocked)
                + "."
            )
            lines.append(
                "That confirms the current symptom, but not the mechanism from an older "
                "diagnosis; I need fresh investigation evidence before repeating why."
            )
        else:
            lines.append("No GPU is currently reported as blocked in NVIDIA-to-vfio handover.")
            stale = [
                bdf for bdf, _key, _episode in self._open_handover_incidents()
                if bdf not in status.handover_blocked
            ]
            if stale:
                lines.append(
                    "The incident database still has an open or recovery-pending record "
                    f"for {', '.join(stale)}, but fresh target status does not confirm it. "
                    "I will not present that record's old diagnosis as current."
                )
        investigator = self._investigator_limit_text(status.observed_at)
        if investigator:
            lines.append(investigator)
        return "\n".join(lines)

    def _latest_prometheus_stats_text(self, now: datetime) -> str:
        """Return bounded live utilization, framebuffer, and marketplace statistics."""
        try:
            row = self.state_db.execute(
                """SELECT observed_utc,document_json
                     FROM terracompute_observation_artifacts
                    WHERE machine_id='17049' AND source='prometheus'
                 ORDER BY artifact_id DESC LIMIT 1"""
            ).fetchone()
        except sqlite3.Error:
            return ""
        if row is None:
            return ""
        try:
            observed = _parse(str(row[0]))
            raw = row[1]
            document = json.loads(
                bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw
            )
        except (TypeError, ValueError):
            return ""
        if not isinstance(document, dict):
            return ""
        age = now - observed
        if (
            age > CURRENT_FAULT_MAX_AGE
            or age < -timedelta(seconds=30)
            or document.get("freshness") != "fresh"
        ):
            return ""
        snapshot = document.get("snapshot")
        metrics = snapshot.get("metrics") if isinstance(snapshot, dict) else None
        if not isinstance(metrics, dict):
            return ""

        by_gpu: dict[int, dict[str, float]] = {}
        dcgm = metrics.get("dcgm")
        if isinstance(dcgm, list):
            for sample in dcgm[:128]:
                if not isinstance(sample, dict) or not isinstance(sample.get("labels"), dict):
                    continue
                labels = sample["labels"]
                gpu = str(labels.get("gpu", ""))
                name = str(labels.get("__name__", ""))
                value = sample.get("value")
                if (
                    not gpu.isdigit()
                    or not 0 <= int(gpu) <= 63
                    or not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or name not in {"DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED"}
                ):
                    continue
                by_gpu.setdefault(int(gpu), {})[name] = float(value)
        gpu_stats = []
        for gpu, values in sorted(by_gpu.items())[:16]:
            pieces = []
            if "DCGM_FI_DEV_GPU_UTIL" in values:
                pieces.append(f"{values['DCGM_FI_DEV_GPU_UTIL']:.0f}% util")
            if "DCGM_FI_DEV_FB_USED" in values:
                pieces.append(f"{values['DCGM_FI_DEV_FB_USED']:.0f} MiB FB")
            if pieces:
                gpu_stats.append(f"GPU {gpu} " + ", ".join(pieces))

        vast_values: dict[str, float] = {}
        vast = metrics.get("vast")
        wanted = {
            "vast_machine_Listed", "vast_machine_Verification",
            "vastai_machine_gpu_idle", "vastai_machine_gpu_rented_bid_demand",
            "vastai_machine_gpu_rented_on_demand", "vastai_machine_gpu_rented_on_reserved",
        }
        if isinstance(vast, list):
            for sample in vast[:128]:
                if not isinstance(sample, dict) or not isinstance(sample.get("labels"), dict):
                    continue
                name = str(sample["labels"].get("__name__", ""))
                value = sample.get("value")
                if (
                    name in wanted
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    vast_values[name] = float(value)
        lines = []
        if gpu_stats:
            lines.append(
                f"DCGM stats at {_text(observed)}: " + "; ".join(gpu_stats) + "."
            )
        if vast_values:
            rental_names = (
                "vastai_machine_gpu_rented_bid_demand",
                "vastai_machine_gpu_rented_on_demand",
                "vastai_machine_gpu_rented_on_reserved",
            )
            rented = sum(
                vast_values.get(name, 0.0)
                for name in rental_names
            )
            fields = []
            if "vast_machine_Listed" in vast_values:
                fields.append("listed" if vast_values["vast_machine_Listed"] == 1 else "unlisted")
            if "vast_machine_Verification" in vast_values:
                fields.append(
                    "verified" if vast_values["vast_machine_Verification"] == 1
                    else "not verified"
                )
            if "vastai_machine_gpu_idle" in vast_values:
                fields.append(f"{vast_values['vastai_machine_gpu_idle']:.0f} idle GPUs")
            if any(name in vast_values for name in rental_names):
                fields.append(f"{rented:.0f} rented GPUs")
            lines.append("Vast stats: " + ", ".join(fields) + ".")
        return "\n".join(lines)

    def _latest_full_status_text(self, now: datetime) -> str:
        """Summarise the newest complete SSH collector artifact for an operator.

        The handover adapter intentionally has a tiny contract and cannot establish
        whole-machine health. The collector already stores the broader evidence; not
        using it made the Telegram bot ask the operator to provide data it had collected
        minutes earlier. A stale or incomplete artifact is named but never promoted to
        a current health verdict.
        """
        try:
            row = self.state_db.execute(
                """SELECT observed_utc,document_json
                     FROM terracompute_observation_artifacts
                    WHERE machine_id='17049' AND source='ssh'
                 ORDER BY artifact_id DESC LIMIT 1"""
            ).fetchone()
        except sqlite3.Error:
            # Older/test stores may not have the bounded artifact archive yet.
            return ""
        if row is None:
            return "No full-machine SSH collector snapshot is available."
        try:
            observed = _parse(str(row[0]))
            raw = row[1]
            document = json.loads(
                bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw
            )
        except (TypeError, ValueError):
            return "The newest full-machine SSH collector snapshot is malformed."
        if not isinstance(document, dict):
            return "The newest full-machine SSH collector snapshot is malformed."
        age = now - observed
        if (
            age > CURRENT_FAULT_MAX_AGE
            or age < -timedelta(seconds=30)
            or document.get("complete") is not True
            or document.get("freshness") != "fresh"
        ):
            return (
                f"The latest full-machine collector snapshot at {_text(observed)} is "
                "stale or incomplete, so it is not used for a health verdict."
            )

        events = document.get("events")
        clean_events = isinstance(events, list) and not events
        healthy = (
            document.get("healthy") is True
            and document.get("status") == "healthy"
            and clean_events
        )
        lines = [
            f"Host hardware/service collector snapshot at {_text(observed)}: "
            + ("healthy." if healthy else "not healthy or not fully clean."),
        ]
        snapshot = document.get("snapshot")
        if not isinstance(snapshot, dict):
            return "\n".join(lines)
        gpu = snapshot.get("gpu")
        if isinstance(gpu, dict):
            counts = []
            for label, key in (
                ("expected", "expected_count"), ("PCI", "pci_count"),
                ("NVIDIA", "nvidia_count"), ("VFIO", "vfio_count"),
            ):
                value = gpu.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 64:
                    counts.append(f"{label} {value}")
            if counts:
                lines.append("GPU inventory: " + ", ".join(counts) + ".")
            temperatures = []
            devices = gpu.get("gpus")
            if isinstance(devices, list):
                for device in devices[:16]:
                    if not isinstance(device, dict):
                        continue
                    bdf = _bounded_status_value(device.get("pci_bdf"), 16)
                    temperature = device.get("temperature_c")
                    pstate = _bounded_status_value(device.get("pstate"), 8)
                    if (
                        bdf
                        and isinstance(temperature, int)
                        and not isinstance(temperature, bool)
                        and -100 <= temperature <= 200
                    ):
                        temperatures.append(
                            f"{bdf} {temperature}C" + (f" {pstate}" if pstate else "")
                        )
            if temperatures:
                lines.append("GPU temperatures: " + "; ".join(temperatures) + ".")
        docker = snapshot.get("docker")
        if isinstance(docker, dict) and isinstance(docker.get("containers"), list):
            exporter = next(
                (
                    item for item in docker["containers"]
                    if isinstance(item, dict) and item.get("name") == COMPONENT
                ),
                None,
            )
            if exporter is not None:
                exporter_status = _bounded_status_value(exporter.get("status"), 80)
                if exporter_status:
                    lines.append(f"dcgm-exporter container status: {exporter_status}.")
        services = snapshot.get("services")
        if isinstance(services, dict):
            known = []
            for name in ("docker", "nvidia-persistenced", "vastai"):
                value = _bounded_status_value(services.get(name), 24)
                if value:
                    known.append(f"{name} {value}")
            if known:
                lines.append("Host services: " + ", ".join(known) + ".")
        if isinstance(events, list):
            lines.append(
                "Collector events: none." if not events else
                f"Collector events: {min(len(events), 999)} reported."
            )
        return "\n".join(lines)

    def _current_faults(self, now: datetime) -> list[str]:
        """Human-readable current incidents, backed by their newest fresh sample."""
        try:
            rows = self.state_db.execute(
                """SELECT i.dedup_key,i.source,i.fault_family,i.severity,
                          i.last_occurrence_utc,o.source_utc,o.status,o.freshness,
                          o.evidence_json
                     FROM incidents i JOIN observations o ON o.id=(
                          SELECT MAX(recent.id) FROM observations recent
                           WHERE recent.incident_key=i.dedup_key)
                    WHERE i.status IN ('open','recovery_pending')
                 ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                          WHEN 'warning' THEN 2 ELSE 3 END,
                          i.last_occurrence_utc DESC
                    LIMIT 32"""
            ).fetchall()
        except sqlite3.Error:
            return []
        faults: list[str] = []
        for row in rows:
            try:
                observed = _parse(str(row[5]))
            except ValueError:
                continue
            if (
                now - observed > CURRENT_FAULT_MAX_AGE
                or observed - now > timedelta(seconds=30)
                or str(row[6]) not in {"unhealthy", "unknown"}
                or str(row[7]) != "fresh"
            ):
                continue
            try:
                raw = row[8]
                event = json.loads(
                    bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw
                )
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            message = _bounded_status_value(event.get("message"), 280)
            code = _bounded_status_value(event.get("code"), 96)
            if code == HANDOVER_CODE:
                continue
            if not message:
                message = code.replace("_", " ") if code else "Unspecified monitoring fault"
            detail = _current_fault_detail(code, event.get("evidence"))
            source = _bounded_status_value(row[1], 80) or "monitoring"
            faults.append(
                f"{message}{detail} (source {source}, observed {_text(observed)})."
            )
            if len(faults) >= MAX_CURRENT_FAULTS:
                break
        return faults

    def _investigator_limit_text(self, now: datetime) -> str:
        """Expose a recent investigation backstop instead of silently looking stuck."""
        try:
            row = self.state_db.execute(
                """SELECT recorded_utc,document_json FROM tc_action_evidence
                    WHERE kind='diagnosis'
                 ORDER BY recorded_utc DESC,rowid DESC LIMIT 1"""
            ).fetchone()
        except sqlite3.Error:
            return ""
        if row is None:
            return ""
        try:
            recorded = _parse(str(row[0]))
            raw = row[1]
            document = json.loads(
                bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw
            )
        except (TypeError, ValueError):
            return ""
        if (
            now - recorded <= CURRENT_FAULT_MAX_AGE
            and isinstance(document, dict)
            and "daily-spend-backstop" in str(document.get("reason") or "")
        ):
            return (
                "Automated model investigation has hit its daily safety spending "
                "ceiling; deterministic monitoring is still collecting and reporting faults."
            )
        return ""

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
        latest = _latest_vast(self.state_db)
        if latest is not None:
            parts.append("## vast\n" + _vast_text(*latest, now=self.clock()))
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
            stage = {
                "awaiting_backup": "Preserving evidence before a proposed change",
                "awaiting_delivery": "Preparing a request for your decision",
                "awaiting_answer": "Waiting for your decision",
                "executing": "An approved change is running",
            }.get(active.stage, "A change is in progress")
            lines.append(f"{stage}{f' for GPU {active.bdf}' if active.bdf else ''}.")
            if active.command:
                note = self._plan_note(active)
                lines.extend(str(note.get(name)) for name in ("summary", "impact")
                             if note.get(name))
        lines.append(self._last_diagnosis_text())
        recent = self.actions_db.execute(
            "SELECT COUNT(*) FROM tc_action_attempts WHERE started_utc > ?",
            (_text(now - timedelta(days=1)),),
        ).fetchone()[0]
        lines.append(f"{recent} restart(s) in the last day.")
        return "\n".join(lines)

    def _handle_inputs(self) -> None:
        answered = 0
        for envelope in self.backend.pending_inputs(self.namespace):
            self._record_input_start(envelope)
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
                or (cycle.command and (not cycle.message_id or not cycle.delivered_utc))
                or cycle.nonce is None
                or not secrets.compare_digest(str(envelope.nonce), cycle.nonce)
            ):
                self.backend.mark_handled(self.namespace, envelope.update_id)
                self._send("That answer does not match a waiting approval request.")
                continue
            if envelope.kind is InputKind.DENIAL_COMMAND:
                self._deny(cycle, envelope)
            else:
                self._approve_and_execute(cycle, envelope)

    def _deny(self, cycle: Cycle, envelope: Any) -> None:
        """A refusal ends the request and is remembered for this incident episode."""
        self._record_input_start(envelope)
        self.backend.mark_handled(self.namespace, envelope.update_id)
        self._finish(
            cycle, REFUSED_BY_OPERATOR, f"telegram:{envelope.sender_id} left it alone",
            notice=(f"Understood. Request {cycle.proposal_id} will not run."
                    if cycle.command else "Understood, leaving dcgm-exporter alone."),
        )

    def _run_approved(self, cycle: Cycle, envelope: Any, now: datetime) -> None:
        """Carry out the command a person just approved.

        Through the general executor and the management session, which is how every
        change to this machine is made. There is no per-action adapter to write first,
        which is what used to make a proposal like `systemctl reboot` unanswerable.

        The approval is spent on this attempt whatever it returns: it authorised one
        run of one command, and a failure does not hand back permission for another.
        """
        self.cycles.update(
            cycle.cycle_id, now, stage="executing", override_by=str(envelope.sender_id)
        )
        self._send(f"Approved request {cycle.proposal_id}. The action is starting; I will check the result.")
        result = self.actor.run(
            str(cycle.command), subject=f"incident:{cycle.incident_key}", approved=True
        )
        if result.uncertain:
            self._finish(
                self.cycles.get(cycle.cycle_id), "unknown", result.detail,
                notice=(f"The target connection closed before it reported the result of "
                        f"request {cycle.proposal_id}. The action may have run; I will not run it "
                        "again from this approval. Check fresh target status."),
                audit=True,
            )
            return
        # The outcome is known now; record it before the checks, which can take minutes.
        # An interruption during them then resumes with what actually happened instead
        # of reporting that the command "may have run".
        outcome = "succeeded" if result.ok else "failed"
        self.cycles.update(cycle.cycle_id, self.clock(), result=outcome, detail=result.detail[:512])
        verified = False
        try:
            checked, verified = self._verify_plan(cycle) if result.ok else ("", False)
        except Exception as error:  # The checks must never cost the outcome its record.
            checked = f"The checks afterwards could not run ({type(error).__name__})."
        self._finish(
            self.cycles.get(cycle.cycle_id),
            outcome, result.detail,
            notice=(f"Request {cycle.proposal_id}: execution completed. "
                    + ("Verification checks ran successfully; review the target status before treating the fault as fixed."
                       if verified else
                       "Verification is incomplete; the fault is not confirmed fixed.")
                    if result.ok else
                    f"Request {cycle.proposal_id}: execution failed and may have changed something. "
                    "Check the target before retrying."),
            audit=True,
        )
        self._tell_conversation_about(cycle, result, checked)

    def _plan_lineage(self, cycle: Cycle) -> str:
        """Which conversation a waiting request came from ("" for none)."""
        return _lineage(self._plan_note(cycle).get("conversation"))

    def _plan_note(self, cycle: Cycle) -> dict[str, Any]:
        try:
            note = json.loads(self.notes.get(f"plan:{cycle.proposal_id}") or "{}")
        except ValueError:
            return {}
        return note if isinstance(note, dict) else {}

    def _valid_review_binding(self, cycle: Cycle) -> bool:
        if not cycle.command or not cycle.proposal_id:
            return False
        note = self._plan_note(cycle)
        try:
            action = ProposedAction(cycle.command, "", str(note["rollback"]),
                                    tuple(str(item) for item in note["verify"]),
                                    str(note["summary"]), str(note["impact"]))
            expected = self._action_binding(action, cycle.bdf,
                                            cycle.incident_key, cycle.episode)
            card = (f"Action: {action.summary.strip()}\n\n"
                    f"Impact and limits: {action.impact.strip()}\n\n"
                    f"Proposal: {cycle.proposal_id}")
            return (note.get("card") == card and secrets.compare_digest(
                expected, str(note["binding"])) and
                json.loads(self.notes.get(f"grant:{note['review_event']}") or "{}").get("binding") == expected)
        except (KeyError, TypeError, ValueError):
            return False

    def _verify_plan(self, cycle: Cycle) -> tuple[str, bool]:
        """Keep diagnostic output and its execution status separate."""
        checks = [str(item) for item in self._plan_note(cycle).get("verify", [])
                  if isinstance(item, str)][:4]
        if not checks or self.observer is None:
            return "", False
        lines = ["Checks afterwards:"]
        complete = True
        for command in checks:
            observed = self.observer.observe(command, subject=f"verify:{cycle.proposal_id}")
            text = observed.text().strip()
            lines.append(f"$ {command}\n{text[-600:] if text else '(no output)'}")
            complete = complete and (getattr(observed, "ok", False)
                and getattr(observed, "exit_code", None) == 0
                and not getattr(observed, "truncated", True))
        return "\n".join(lines), complete

    def _tell_conversation_about(self, cycle: Cycle, result: Any, checked: str) -> None:
        """Give the outcome back to the agent that proposed it, so it can judge it.

        Without this the plan's author never learns whether its plan worked: the
        operator sees "Done" and the thread that proposed it is left believing the
        machine is still as it was.
        """
        thread = self._plan_note(cycle).get("conversation")
        if not isinstance(thread, dict) or self.conversation is None:
            return
        now = self.clock()
        seed = f"outcome:{cycle.proposal_id}"
        try:
            exchange = {
                "root": seed, "incident_key": str(thread["incident_key"]),
                "episode": int(thread["episode"]), "bdf": str(thread["bdf"]),
                "subject_hash": str(thread["subject_hash"]),
                "investigation_id": str(thread["investigation_id"]),
                "sender_id": int(thread["sender_id"]),
            }
        except (KeyError, TypeError, ValueError):
            return
        self.conversations.start(
            seed, incident_key=exchange["incident_key"], episode=exchange["episode"],
            bdf=exchange["bdf"], subject_hash=exchange["subject_hash"],
            investigation_id=exchange["investigation_id"], sender_id=exchange["sender_id"],
            question="(the outcome of the plan it proposed)", now=now,
        )
        prompt = (
            f"The operator approved your plan and it ran.\n$ {cycle.command}\n"
            f"Result: {'it completed' if result.ok else 'it failed'} ({result.detail}).\n"
            + (f"{checked}\n" if checked else "")
            + "Tell the operator plainly whether it worked. Look further if you need to; "
            "if it did not work, say why and what you would do next."
        )
        self._put_to_conversation(exchange, seed, prompt, now)

    def _approve_and_execute(self, cycle: Cycle, envelope: Any) -> None:
        self._record_input_start(envelope)
        if cycle.command and not self._valid_review_binding(cycle):
            self._finish(cycle, "withdrawn", "review binding missing or changed",
                         notice=f"Request {cycle.proposal_id} was withdrawn because its reviewed plan changed.")
            self.backend.mark_handled(self.namespace, envelope.update_id)
            return
        # Persisted above before consuming the input.
        self.backend.mark_handled(self.namespace, envelope.update_id)
        now = self.clock()
        if self.controls.paused:
            self._send("I am paused, so I did not act on that. Send /resume and approve "
                       "again if you want it to run.")
            return
        if cycle.command:
            if not cycle.delivered_utc or now >= _parse(cycle.delivered_utc) + GENERIC_APPROVAL_LIFETIME:
                self._finish(
                    cycle, "expired", "the generic approval request expired",
                    notice="That approval request expired after thirty minutes, so nothing "
                           "ran. Ask me again if you still want it.",
                )
                return
            # The machine is named again immediately before anything runs on it, as
            # for every write here: a request can wait half an hour, and the host behind
            # the connection is not something a button press can vouch for.
            try:
                verified = bool(self.adapter.status().identity_verified)
            except Exception as error:
                self._send(
                    f"Could not re-read the target ({type(error).__name__}), so nothing "
                    "ran. Tap Approve again."
                )
                return
            if not verified:
                self._finish(
                    cycle, "denied", "target identity did not verify before execution",
                    notice="The target's identity did not verify just before running, so "
                           "nothing ran.",
                    audit=True,
                )
                return
            self._run_approved(cycle, envelope, now)
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
            # Two different things, and they had one message between them. A press
            # that arrived late is not the machine having changed, and saying so sent
            # a person hunting a fault that was not there. It is also the likelier of
            # the two: a late press is exactly what a slow callback produces, and the
            # callback was slow for months because the poll was trying to reach
            # Telegram over an IPv6 address with no route to it.
            if existing.expires_at <= now:
                self._expired(cycle, now)
                return
            if proposal_shape(existing) != cycle.shape:
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
            # Read after the proposal exists, not before. This is the moment the
            # service acted on the tap, not the moment the finger touched the glass,
            # and the broker requires it to fall inside the proposal's lifetime. Taken
            # at the top of this method it was always a few seconds EARLIER than the
            # proposal built from `adapter.status()` -- an SSH round trip to the target
            # -- so every approval was refused as "outside the proposal lifetime". A
            # frozen test clock made the two identical and hid it completely: on this
            # machine no approval was ever recorded and no restart ever ran.
            occurred_at=self.clock(),
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
        # However this ended -- approved, refused, withdrawn, lapsed, superseded -- the
        # request is spent, so its buttons stop being an invitation. An approval is
        # single-use and bound to this one proposal, so a later press could only ever
        # fail, and after a week where every press failed for a different reason the
        # last thing wanted is an affordance that does nothing. The message itself
        # stays: what was asked and how it ended is the record.
        if current.message_id:
            try:
                self.telegram.clear_buttons(self.group_id, int(current.message_id))
            except Exception:
                # The database withdrawal is the authority; stale visual buttons
                # cannot make an ended cycle executable.
                pass
        finished = {"result": result[:64], "detail": detail[:512], "finished_utc": _text(now)}
        if notice is not None and result != "unknown":
            # Say so when this outcome ends proposals for the incident episode.
            projected = [
                replace(other, **finished) if other.cycle_id == cycle.cycle_id else other
                for other in self.cycles.episode(cycle.incident_key, cycle.episode)
            ]
            if not episode_outlook(projected)[0]:
                notice += GENERIC_ACTION_CLOSED if cycle.command else EPISODE_CLOSED
        # One write marks the cycle done and queues its message and audit copy.
        self.cycles.update(
            cycle.cycle_id, now, stage="done", audit_pending=int(audit),
            execution_id=execution_id or cycle.execution_id, notice=notice, **finished,
        )
        if result != "unknown":
            try:
                self._offer_durable(cycle, result)
            except Exception as error:  # Offering the cure must not undo the outcome.
                self.report(
                    '{"operation":"actions","phase":"_offer_durable","status":"failed",'
                    f'"category":"{type(error).__name__}"}}'
                )

    def _deliver(self) -> None:
        """Write pending audit copies and send pending outcome messages until both succeed.

        The audit copy is a local write and never waits on Telegram. Each cycle and each
        kind backs off on its own, doubling up to an hour, so one failure neither blocks
        other cycles nor repeats a message every minute. An ambiguous Telegram transport
        failure may have delivered the message before the retry; exactly once delivery
        cannot be guaranteed by this API.
        """
        now = self.clock()
        for waiting in self.cycles._many("stage='awaiting_delivery' AND command IS NOT NULL"):
            self._deliver_card(waiting, now)
        blocked_roots: set[str] = set()
        for event_id, root, message in self.actions_db.execute(
            "SELECT event_id,root,text FROM tc_action_dialogue WHERE kind IN ('terminal','progress') "
            "AND delivered_utc IS NULL ORDER BY created_utc,rowid").fetchall():
            if str(event_id).startswith("final:") and root in blocked_roots:
                continue
            if not self.schedule.due(f"dialogue:{event_id}", now):
                if str(event_id).startswith("final:"):
                    blocked_roots.add(str(root))
                continue
            def send_terminal(event_id: str = event_id, message: str = message) -> None:
                self.telegram.send_message(self.group_id, message)
                self.actions_db.execute("UPDATE tc_action_dialogue SET delivered_utc=? WHERE event_id=?",
                                        (_text(now), event_id))
                self.actions_db.commit()
            self._attempt_delivery(f"dialogue:{event_id}", now, send_terminal)
            if str(event_id).startswith("final:") and self.actions_db.execute(
                "SELECT delivered_utc FROM tc_action_dialogue WHERE event_id=?",
                (event_id,),
            ).fetchone()[0] is None:
                blocked_roots.add(str(root))
        for cycle in self.cycles.undelivered():
            if cycle.audit_pending and self.schedule.due(f"deliver:{cycle.cycle_id}:audit", now):
                self._attempt_delivery(f"deliver:{cycle.cycle_id}:audit", now, lambda cycle=cycle: (
                    self._audit(cycle), self.cycles.update(cycle.cycle_id, now, audit_pending=0)
                ))
            if cycle.notice is not None and self.schedule.due(f"deliver:{cycle.cycle_id}:notice", now):
                self._attempt_delivery(f"deliver:{cycle.cycle_id}:notice", now, lambda cycle=cycle: (
                    self.telegram.send_message(self.group_id, self._operator_prose(cycle.notice)),
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
        approver = self._approver(cycle.proposal_id)
        if approver is None and cycle.command and cycle.override_by:
            try:
                approver = int(cycle.override_by)
            except ValueError:
                pass
        self.evidence.record("restart-result", str(cycle.proposal_id), {
            "proposal_id": cycle.proposal_id,
            "digest": cycle.digest,
            "bdf": cycle.bdf,
            "incident_key": cycle.incident_key,
            "episode": cycle.episode,
            "execution_id": cycle.execution_id,
            "approver_telegram_user_id": approver,
            "result": cycle.result,
            "detail": cycle.detail,
            "recorded_at": cycle.finished_utc,
        })

    def _send(self, text: str) -> None:
        # Split, never cut: one Telegram message holds 4,096 characters, and a reply
        # that ran past it lost its last steps.
        for part in _message_parts(self._operator_prose(text)):
            try:
                self.telegram.send_message(self.group_id, part)
            except Exception:
                pass


_DOCKER_VERB = re.compile(
    r"^docker (?:restart|start|stop) ([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$"
)


def _message_parts(text: str, limit: int = MAX_TELEGRAM_TEXT - 96) -> list[str]:
    """Text in pieces that each fit one message, broken between paragraphs if possible."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    total = len(parts)
    return [f"({index}/{total}) {part}" for index, part in enumerate(parts, start=1)]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _container_named(command: str) -> str:
    """The container a simple docker command acts on, or "" if it is not one.

    Reading the command rather than a parameter dictionary: the command IS the action
    now, so anything that used to branch on an action name and its parameters has to
    ask the command the same question instead.
    """
    match = _DOCKER_VERB.fullmatch(command.strip())
    return match.group(1) if match else ""


def _lineage(conversation: Any) -> str:
    """A conversation's identity for replacing its own earlier plan, never another's.

    Several operator questions can share one incident thread. Only the same request
    root can replace its waiting card; older notes use investigation_id as a fallback.
    """
    if not isinstance(conversation, Mapping):
        return ""
    return str(conversation.get("root") or conversation.get("investigation_id") or "")


def episode_outlook(cycles: list[Cycle]) -> tuple[bool, datetime | None]:
    """Whether an incident episode with these cycles may get another proposal, and when."""
    if any(cycle.result is None for cycle in cycles):
        return False, None
    if any(cycle.result == REFUSED_BY_OPERATOR for cycle in cycles):
        return False, None
    executed = [index for index, cycle in enumerate(cycles) if cycle.result in EXECUTED_RESULTS]
    # Only cycles that actually reached a person spend the episode's patience.
    cycles = [cycle for cycle in cycles if cycle.result not in UNASKED_RESULTS]
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


# What Vast says about this machine, as the model sees it. The scheduler already
# collects this every pass; until now it reached incidents and nobody else, so the thing
# whose job is working out what is wrong could not see that a renter had filed a report
# an hour ago saying exactly what was wrong.
MAX_VAST_REPORTS = 6
MAX_VAST_REPORT_CHARS = 600
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _bounded_status_value(value: object, limit: int) -> str:
    """Bound stored evidence before it is copied into a Telegram status message."""
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


def _current_fault_detail(code: str, evidence: object) -> str:
    """Render only the small, useful fields of known deterministic findings."""
    if not isinstance(evidence, dict):
        return ""
    if code == "physical_free_vast_market_unavailable":
        values = tuple(evidence.get(name) for name in ("idle", "rented", "total"))
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return f" ({values[0]} idle, {values[1]} rented, {values[2]} total)"
    if code == "vast_machine_error":
        description = _bounded_status_value(evidence.get("error_description"), 300)
        if description:
            return f": {description}"
    return ""


def _latest_vast(state_db: sqlite3.Connection) -> tuple[str, Mapping[str, Any]] | None:
    """The most recent Vast observation the scheduler retained, and when it was taken."""
    try:
        row = state_db.execute(
            """SELECT source_utc, evidence_json FROM observations
               WHERE source='vast' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        raw = row[1]
        document = json.loads(bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw)
    except (ValueError, TypeError):
        return None
    snapshot = document.get("snapshot") if isinstance(document, dict) else None
    return (str(row[0]), snapshot) if isinstance(snapshot, dict) else None


# A Vast reading older than this is worth saying out loud. The scheduler takes one
# every pass, so an old one means the collector has been failing -- and a stale reading
# presented as the current state is the kind of wrong answer nobody catches.
VAST_STALE_AFTER = timedelta(minutes=30)


def _vast_text(
    observed_at: str, snapshot: Mapping[str, Any], now: datetime | None = None
) -> str:
    """One compact block: what Vast believes, and what renters have complained about."""
    lines = [f"As of {observed_at}, from the Vast.ai API:"]
    if now is not None:
        try:
            age = now - _parse(observed_at)
        except ValueError:
            lines.append("(this reading is not dated; treat it as of unknown age)")
        else:
            if age > VAST_STALE_AFTER:
                hours = age.total_seconds() / 3600
                lines.append(
                    f"(this reading is {hours:.1f} hours old -- the marketplace is "
                    "polled every pass, so something is wrong with the collection "
                    "itself; do not read it as the current state)"
                )
    machine = snapshot.get("machine")
    if isinstance(machine, dict):
        lines.append(
            "machine: "
            + " ".join(
                f"{key}={machine.get(key)}"
                for key in ("listed", "rentable", "rented", "total_gpus", "rented_gpus")
            )
        )
    else:
        lines.append("machine: unavailable this pass")
    market = snapshot.get("market")
    if isinstance(market, dict):
        lines.append(
            "market: "
            + " ".join(
                f"{key}={market.get(key)}"
                for key in ("search_complete", "advertised", "rentable", "launch_proven")
            )
        )
    reports = snapshot.get("reports")
    if reports is None:
        lines.append("renter reports: unavailable this pass")
    elif not reports:
        lines.append("renter reports: none")
    else:
        # A renter writes this text. It is the most direct account of the fault there
        # is and the least trustworthy string in the prompt, which is why it is bounded
        # here and named as theirs where it is rendered.
        lines.append(f"renter reports ({len(reports)}), newest last:")
        for report in list(reports)[-MAX_VAST_REPORTS:]:
            if not isinstance(report, dict):
                continue
            body = _CONTROL.sub(" ", str(report.get("message") or ""))[:MAX_VAST_REPORT_CHARS]
            problem = _CONTROL.sub(" ", str(report.get("problem") or "unstated"))[:128]
            when = _CONTROL.sub(" ", str(report.get("created_at") or "unknown"))[:64]
            lines.append(f"- {when} [{problem}] {body}".rstrip())
    errors = snapshot.get("errors")
    if errors:
        lines.append(f"parts of this reading failed: {', '.join(str(e) for e in errors[:8])}")
    return "\n".join(lines)


def _request_text(request: Any, status: Any, bdf: str, reasoning: str = "") -> str:
    """The deterministic restart has a fixed, short action and impact card."""
    return _catalogue_card(bdf, str(request.proposal_id))


def _catalogue_card(bdf: str, proposal_id: str) -> str:
    return (f"Action: Restart the monitoring exporter to release GPU {bdf} for VM handover.\n\n"
            "Impact and limits: Monitoring pauses briefly. No tenant container is restarted. "
            "The VM handover still needs a fresh check; this restart may be a stopgap.\n\n"
            f"Proposal: {proposal_id}")


def _result_text(state: str, detail: str) -> str:
    outcome = {
        "succeeded": "Restart succeeded",
        "failed": "Restart failed",
        "postcondition-failed": "Restart ran but its checks failed; stopped for review",
        "refused": "No restart was performed",
        "unknown": "Restart result is unknown; it is checked against the target every 5 "
                   "minutes and nothing else is proposed until it is settled",
    }.get(state, f"Restart ended as {state}")
    # Detailed actor output stays in the audit record and explicit console details.
    return outcome
