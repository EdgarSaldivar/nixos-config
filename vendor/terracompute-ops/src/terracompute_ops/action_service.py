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
from .diagnosis import MAX_READ_COMMAND_CHARS, ObserveRound, Tier
from .inspection import answered, summarize
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
# How long one investigation may spend looking before the rule answers instead, and how
# many rounds of looking it gets. Six rounds is up to seven investigator turns counting
# the first ask, against the twelve one investigation may spend.
OBSERVE_LOOP_DEADLINE = timedelta(minutes=90)
MAX_OBSERVE_ROUNDS = 6
# After giving up on looking at a fault, how long before it is worth looking again.
OBSERVE_LOOP_COOLDOWN = timedelta(hours=6)
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
CONVERSATION_WAIT = timedelta(minutes=5)
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
DELIVERY_RETRY = timedelta(minutes=1)
MAX_DELIVERY_RETRY = timedelta(hours=1)
BACKUP_UNIT = "terracompute-backup.service"
# A restart ran (or may have run) for these results.
EXECUTED_RESULTS = frozenset({"succeeded", "failed", "postcondition-failed", "unknown"})
# What a conversation is about when no incident is open: the machine itself.
MACHINE_SUBJECT = "machine:17049"
# The one fault this service was taught by hand, and the only one with an adapter.
HANDOVER_CODE = "gpu_vfio_handover_blocked"
# A look a person asked for, with no incident behind it. Kept apart from an incident's
# key so nothing about it can be mistaken for a fault this service detected.
REVIEW_KEY = "request:machine"
REVIEW_REQUEST = "review-requested"


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
OPEN_ATTEMPT_STATES = ("reserved", "dispatching", "unknown")
_BDF_ARGUMENT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
EPISODE_CLOSED = " No further restart proposals for this incident until it recovers."
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
        self.db.execute(
            """INSERT INTO tc_action_observe_loops(
                 loop_id,incident_key,episode,bdf,state,started_utc,updated_utc,rounds,
                 severity,evidence_revision,reads_available,reads_text,status_json,
                 facts_json,vast_text,vast_reports,attempts,observed_utc,code)
               VALUES(:loop_id,:incident_key,:episode,:bdf,'open',:now,:now,0,:severity,
                 :evidence_revision,:reads_available,:reads_text,:status_json,
                 :facts_json,:vast_text,:vast_reports,:attempts,:observed_utc,:code)""",
            dict(loop),
        )
        self.db.commit()
        return self.open(str(loop["incident_key"]), int(loop["episode"]))

    def ask(self, loop_id: str, round: int, commands: tuple[str, ...], note: str, now: datetime) -> None:
        """Record a whole round at once; half a round would be asked about as if whole."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany(
                """INSERT OR IGNORE INTO tc_action_observe_reads(
                     loop_id,round,seq,command,note,asked_utc) VALUES(?,?,?,?,?,?)""",
                [
                    (loop_id, round, seq, command[:MAX_READ_COMMAND_CHARS], note[:512], _text(now))
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
        # How the operator's own words reach the incident's thread, when one exists.
        self.conversation: Any | None = None
        self.reader = reader
        # How a model-authored read reaches the host: the read-only profile, and the
        # only thing between the text it wrote and this machine.
        self.observer = observer
        # How the service carries out the monitoring work that is its own to do.
        self.actor = actor
        self.observations = Observations(actions_db)
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
        self._guard(self._collect_conversations)
        self._guard(self._reconcile_unknown, False)
        self._guard(self._advance)
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

    def _review(self) -> None:
        """Look the machine over because a person asked, and say what came of it.

        Driven through exactly the same loop as a diagnosis -- the same freezing, the
        same rounds, the same deadline -- because a look somebody asked for deserves
        the same care as one a check asked for. What it cannot do is act: it reports,
        and anything worth doing comes back through the ordinary catalogue for a person
        to approve.
        """
        requested = self.controls.get(REVIEW_REQUEST)
        if requested is None:
            return
        # Not gated on `paused`. Pausing says "keep watching and reporting, act on
        # nothing", and a review is watching and reporting: it has no path to an
        # action at all. Refusing it while paused left the request sitting silently
        # until somebody resumed, with nothing said about why.
        now = self.clock()
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
        )

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
                """SELECT dedup_key, notification_episode, fault_family, severity
                     FROM incidents
                    WHERE status IN ('open','recovery_pending')
                      AND NOT (source='ssh' AND fault_family='gpu')
                 ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                          WHEN 'warning' THEN 2 ELSE 3 END, last_occurrence_utc DESC"""
            ).fetchall()
        except sqlite3.Error:
            return []
        return [(str(row[0]), int(row[1]), str(row[2]), str(row[3])) for row in rows]

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
        others = self._other_open_incidents()
        live = self.observations.live_loop()
        if live is not None:
            # Carry on with the one already under way. Refusing to act while anything
            # was live stopped a NEW investigation starting, which is what it was for,
            # and also stopped this one ever coming back to the loop it had just
            # opened -- so its first question was asked and never followed up, and it
            # sat at nought rounds until the reaper took it an hour and a half later.
            key, episode = str(live["incident_key"]), int(live["episode"])
            carrying = [item for item in others if item[0] == key and item[1] == episode]
            if not carrying:
                return  # A handover or a requested look; each has its own driver.
            chosen = carrying[0]
        else:
            pending = [
                incident for incident in others
                if not self.observations.seen(incident[0], incident[1])
            ]
            if not pending:
                return
            chosen = pending[0]
        self.schedule.set("investigate", now + STATUS_RETRY_INTERVAL)
        key, episode, family, severity = chosen
        try:
            status = self.adapter.status()
        except Exception:
            return  # No view of the machine is no time to reason about it.
        diagnosis = self._diagnose("", key, episode, status, now, code=f"{family}_fault")
        if diagnosis.pending:
            return
        if self._carried_out(diagnosis, key, now):
            return
        if diagnosis.finding is None:
            return  # The reason is in the evidence; nothing to say to the group.
        self._send(
            f"{key} ({severity}) is open and nobody had looked at it.\n"
            f"{describe(diagnosis)}"
        )

    def _carried_out(self, diagnosis: Diagnosis, incident_key: str, now: datetime) -> bool:
        """Do it ourselves when it is ours to do. True when it was handled here.

        The charter's line: managing the monitoring we installed is the agent's own
        work, because it is reversible and touches nobody who is paying us. That is
        every container in the catalogue except the one the adapter owns, which keeps
        its evidence backup and its button because acting on it perturbs the very GPU
        state being diagnosed.
        """
        action = diagnosis.action
        if self.actor is None or action is None or action.tier is not Tier.REPAIR:
            return False
        container = action.parameters.get("container", "")
        if action.name != "restart-monitoring-container" or container == COMPONENT:
            return False
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
        result = self.actor.restart(container, subject=f"incident:{incident_key}")
        if result.ok:
            self._send(
                f"I dealt with {container} myself.\n{describe(diagnosis)}\n"
                "That is monitoring we installed, so it did not need your approval."
            )
        else:
            self._send(
                f"I tried to deal with {container} and could not: {result.detail}\n"
                f"{describe(diagnosis)}"
            )
        return True

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
            # A look that was asked for is used up whatever it concludes, so asking
            # again is asking again rather than leaving the loop running hot.
            self._spend_review(bdf, now)
            action = diagnosis.action
            if (
                action is None
                or action.name not in self.ACTIONS_WE_CAN_TAKE
                # The adapter restarts one fixed container; a finding about another is
                # for a person, not authority to restart this one.
                or action.parameters.get("container") != COMPONENT
            ):
                # The rest of the monitoring is the agent's own work per the charter,
                # and had no path at all: a finding asking for the node exporter was
                # validated, found to be something the adapter could not do, and handed
                # to a person who would then have typed the restart themselves.
                if self._carried_out(diagnosis, incident_key, now):
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

    def _review_for(self, bdf: str, now: datetime) -> str | None:
        """Who asked for this GPU to be looked at again.

        Deliberately not an override. An override lets the service act without asking;
        this only lets it investigate sooner than its own waiting periods would allow,
        because a person saying "look at it again" is asking for an opinion, not
        handing over the button.
        """
        value = self.controls.get(f"review:{bdf}")
        if value is None:
            return None
        who, _, when = value.rpartition("@")
        try:
            set_at = _parse(when)
        except ValueError:
            self.controls.clear(f"review:{bdf}")
            return None
        if now - set_at > OVERRIDE_LIFETIME:
            self.controls.clear(f"review:{bdf}")
            return None
        return who

    def _spend_review(self, bdf: str, now: datetime) -> str | None:
        """Take up a request to look again; one asking buys one look."""
        who = self._review_for(bdf, now)
        if who is not None:
            self.controls.clear(f"review:{bdf}")
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

    def _steer(self, name: str, argument: str, sender_id: int) -> str:
        """Carry out one thing a person asked for, and say what was done.

        Reached from their own words by way of the model, which names it from a fixed
        list this service checks. None of it can act on the machine: the worst a
        misreading costs is a look nobody wanted or a pause you undo. What a look
        proposes still comes back as a button bound to that one proposal.
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
            return (
                "Looking the machine over now. I will come back with what I find, "
                "whether or not it is anything."
            )
        if name == "withdraw":
            self._withdraw_for_operator(argument, sender_id)
            return ""  # It says its own piece, with the reason.
        return ""

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
        incident = next(iter(self._open_handover_incidents()), None)
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
        try:
            ticket = self.conversation.ask(
                incident_key=key, episode=episode, bdf=bdf,
                message=question, sender_id=envelope.sender_id,
                subject_hash=subject, briefing=briefing,
                investigation_id=investigation,
            )
        except Exception as error:  # A conversation is never worth crashing the loop.
            self.report(
                '{"operation":"actions","phase":"_converse","status":"failed",'
                f'"category":"{type(error).__name__}"}}'
            )
            return False
        if not ticket:
            return False
        # The answer arrives on a later pass. Waiting for it here would stop the loop
        # answering anybody else, finishing executions or delivering outcomes for as
        # long as the model thinks -- the same mistake diagnosis already made once.
        self.schedule.set(f"conversation:{ticket}", self.clock() + CONVERSATION_WAIT)
        self._conversation_sender[ticket] = int(envelope.sender_id)
        return True

    def _collect_conversations(self) -> None:
        """Say what came back, and admit it when nothing did."""
        now = self.clock()
        if self.conversation is None:
            return
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
            if answer is not None and (answer.text or answer.steer is not None):
                self.schedule.clear(name)
                done = ""
                if answer.steer is not None:
                    sender = self._conversation_sender.get(ticket, 0)
                    try:
                        # Here, not when they spoke: what they asked for may change
                        # what a waiting button would mean, and a question does not.
                        self._suspend_for_conversation()
                        done = self._steer(
                            answer.steer.name, answer.steer.argument, sender
                        )
                    except Exception as error:
                        self.report(
                            '{"operation":"actions","phase":"_steer","status":"failed",'
                            f'"category":"{type(error).__name__}"}}'
                        )
                        done = "I could not do that just now."
                self._conversation_sender.pop(ticket, None)
                # Their answer first, then what actually happened: the words are the
                # model's and the doing is mine, and a person should be able to tell
                # which is which.
                self._send("\n\n".join(part for part in (answer.text, done) if part))
            elif now >= due:
                self.schedule.clear(name)
                self._send(
                    "I could not get an answer to that in time. Ask me again, or "
                    "ask me what I last concluded."
                )

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
        key = str(document.get("incident_key") or "the machine")
        about = "the machine, because you asked me to look" if key == REVIEW_KEY else key
        if not document.get("summary"):
            return f"My last look at {about} reached no conclusion ({document.get('reason') or 'no answer'})."
        lines = [
            f"About {about}:",
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
        # Only a conversation that actually reaches the investigator can change what a
        # request means, so only that withdraws one. A question answered from evidence
        # already gathered changes nothing and should cost nothing.
        if self.conversation is not None and self._converse(question, envelope):
            # Deliberately not withdrawing a waiting request here. Taking it back
            # whenever anybody spoke meant asking about a proposal cancelled it, so a
            # request could never survive being enquired about: three in a row were
            # withdrawn by somebody asking what was going on. A question does not
            # change what the button means. Something that does -- a hold, a pause, a
            # fresh look -- withdraws it when the answer comes back carrying it.
            return
        # The model is how words become instructions, so when it cannot be reached the
        # machine would stop being steerable by anything -- exactly when somebody is
        # most likely to be telling it to stop. This understands almost nothing on
        # purpose: whole-message matches only, so "don't pause" is not a pause.
        plain = _PLAIN_STEER.get(" ".join(question.lower().split()).strip(" .!"))
        if plain is not None:
            done = self._steer(plain, "", envelope.sender_id)
            self._send(
                f"{done}\n\n(I could not reach the investigator, so I took that "
                "plainly rather than thinking about it.)"
            )
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
