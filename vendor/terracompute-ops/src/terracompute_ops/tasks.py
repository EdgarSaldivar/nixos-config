"""Immutable task contracts and the transactional Phase 0 task store.

The store is intentionally transport- and executor-neutral.  It can share the
existing ``state.sqlite3`` without changing its global ``user_version`` or any
incident/action table, and all new objects use a ``tc_task_`` namespace.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Iterator, Mapping

from .plans import (
    AUTHORIZATION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    MACHINE_ID,
    ApprovalGrant,
    Contract,
    ContractError,
    ExecutionLease,
    Plan,
    Verification,
    _fields,
    _document_array,
    _freeze,
    _identifier,
    _safe_json,
    _string,
    _strings,
    _thaw,
    _utc,
    parse_utc,
    stable_hash,
    strict_json_loads,
    utc_text,
)
from .state import _ensure_shared_sqlite_mode


TASK_STORE_SCHEMA_VERSION = 4


class TaskState(str, Enum):
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})
LEGAL_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType({
    TaskState.INVESTIGATING: frozenset({
        TaskState.PLANNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED,
    }),
    TaskState.PLANNING: frozenset({
        TaskState.INVESTIGATING, TaskState.AWAITING_APPROVAL, TaskState.EXECUTING,
        TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED,
    }),
    TaskState.AWAITING_APPROVAL: frozenset({
        TaskState.INVESTIGATING, TaskState.PLANNING, TaskState.EXECUTING,
        TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED,
    }),
    TaskState.EXECUTING: frozenset({
        TaskState.INVESTIGATING, TaskState.PLANNING, TaskState.AWAITING_APPROVAL,
        TaskState.VERIFYING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED,
    }),
    TaskState.VERIFYING: frozenset({
        TaskState.INVESTIGATING, TaskState.PLANNING, TaskState.EXECUTING,
        TaskState.SUCCEEDED, TaskState.FAILED, TaskState.PAUSED, TaskState.CANCELLED,
    }),
    # A paused task may resume at the coordinator-selected safe lifecycle boundary.
    TaskState.PAUSED: frozenset({
        TaskState.INVESTIGATING, TaskState.PLANNING, TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING, TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED,
    }),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
})


class TaskStoreError(RuntimeError):
    """Base class for deterministic persistence failures."""


class TaskConflict(TaskStoreError):
    """An identifier/idempotency key was replayed with different content."""


class IllegalTransition(TaskStoreError):
    """A task event does not follow the durable lifecycle."""


@dataclass(frozen=True)
class Task(Contract):
    task_id: str
    requester_id: str
    requester_group_id: int
    origin_message_id: str
    created_at: datetime
    objective: str
    constraints: tuple[str, ...]
    state: TaskState
    evidence_revision: str
    model_thread: str | None
    attempt_count: int
    deadline: datetime | None
    budgets: Mapping[str, int]
    incident_ids: tuple[str, ...]
    current_plan_version: int | None = None
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "task_id", "requester_id", "requester_group_id",
        "origin_message_id", "created_at", "objective", "constraints", "state",
        "evidence_revision", "model_thread", "attempt_count", "deadline", "budgets",
        "incident_ids", "current_plan_version", "machine_id",
    }

    def __post_init__(self) -> None:
        for name in ("task_id", "requester_id", "origin_message_id", "evidence_revision"):
            _identifier(getattr(self, name), name)
        if isinstance(self.requester_group_id, bool) or not isinstance(self.requester_group_id, int):
            raise ContractError("requester_group_id must be an integer")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        _string(self.objective, "objective")
        object.__setattr__(self, "constraints", _strings(self.constraints, "constraints"))
        if not isinstance(self.state, TaskState):
            raise ContractError("state must be a TaskState")
        if self.model_thread is not None:
            _identifier(self.model_thread, "model_thread")
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise ContractError("attempt_count must be a non-negative integer")
        if self.deadline is not None:
            deadline = _utc(self.deadline, "deadline")
            if deadline <= self.created_at:
                raise ContractError("deadline must follow task creation")
            object.__setattr__(self, "deadline", deadline)
        if not isinstance(self.budgets, Mapping):
            raise ContractError("budgets must be an object")
        safe_budgets = _safe_json(self.budgets, "budgets")
        budgets: dict[str, int] = {}
        for raw_name, raw_value in safe_budgets.items():
            name = _identifier(raw_name, "budget name")
            if (
                isinstance(raw_value, bool) or not isinstance(raw_value, int)
                or raw_value < 0
            ):
                raise ContractError(f"budget {name} must be a non-negative integer")
            budgets[name] = raw_value
        object.__setattr__(self, "budgets", MappingProxyType(budgets))
        object.__setattr__(self, "incident_ids", _strings(self.incident_ids, "incident_ids"))
        if self.current_plan_version is not None and (
            isinstance(self.current_plan_version, bool)
            or not isinstance(self.current_plan_version, int)
            or self.current_plan_version < 1
        ):
            raise ContractError("current_plan_version must be null or positive")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"tasks are restricted to machine {MACHINE_ID}")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "task_id": self.task_id,
            "requester_id": self.requester_id, "requester_group_id": self.requester_group_id,
            "origin_message_id": self.origin_message_id, "created_at": utc_text(self.created_at),
            "objective": self.objective, "constraints": list(self.constraints),
            "state": self.state.value, "evidence_revision": self.evidence_revision,
            "model_thread": self.model_thread, "attempt_count": self.attempt_count,
            "deadline": None if self.deadline is None else utc_text(self.deadline),
            "budgets": dict(self.budgets), "incident_ids": list(self.incident_ids),
            "current_plan_version": self.current_plan_version, "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "Task":
        value = _fields(document, cls._FIELDS, "Task")
        constraints = _document_array(value["constraints"], "constraints")
        incident_ids = _document_array(value["incident_ids"], "incident_ids")
        try:
            state = TaskState(value["state"])
        except (ValueError, TypeError) as error:
            raise ContractError("unknown task state") from error
        return cls(
            task_id=value["task_id"], requester_id=value["requester_id"],
            requester_group_id=value["requester_group_id"],
            origin_message_id=value["origin_message_id"],
            created_at=parse_utc(value["created_at"], "created_at"),
            objective=value["objective"], constraints=tuple(constraints), state=state,
            evidence_revision=value["evidence_revision"], model_thread=value["model_thread"],
            attempt_count=value["attempt_count"],
            deadline=None if value["deadline"] is None else parse_utc(value["deadline"], "deadline"),
            budgets=value["budgets"], incident_ids=tuple(incident_ids),
            current_plan_version=value["current_plan_version"], machine_id=value["machine_id"],
        )


@dataclass(frozen=True)
class TaskEvent(Contract):
    SCHEMA_VERSION: ClassVar[int] = AUTHORIZATION_SCHEMA_VERSION
    event_id: str
    task_id: str
    sequence: int
    event_type: str
    actor_id: str
    occurred_at: datetime
    from_state: TaskState | None
    to_state: TaskState
    payload: Mapping[str, Any]
    evidence_revision: str | None = None
    incident_id: str | None = None
    action_id: str | None = None
    schema_version: int = field(default=AUTHORIZATION_SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "event_id", "task_id", "sequence", "event_type", "actor_id",
        "occurred_at", "from_state", "to_state", "payload", "incident_id", "action_id",
        "evidence_revision",
    }

    def __post_init__(self) -> None:
        for name in ("event_id", "task_id", "event_type", "actor_id"):
            _identifier(getattr(self, name), name)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ContractError("event sequence must be positive")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if self.from_state is not None and not isinstance(self.from_state, TaskState):
            raise ContractError("from_state must be a TaskState or null")
        if not isinstance(self.to_state, TaskState):
            raise ContractError("to_state must be a TaskState")
        if not isinstance(self.payload, Mapping):
            raise ContractError("payload must be an object")
        object.__setattr__(self, "payload", _freeze(_safe_json(self.payload, "payload")))
        if self.evidence_revision is not None:
            _identifier(self.evidence_revision, "evidence_revision")
        for name in ("incident_id", "action_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "event_id": self.event_id,
            "task_id": self.task_id, "sequence": self.sequence, "event_type": self.event_type,
            "actor_id": self.actor_id, "occurred_at": utc_text(self.occurred_at),
            "from_state": None if self.from_state is None else self.from_state.value,
            "to_state": self.to_state.value, "payload": _thaw(self.payload),
            "evidence_revision": self.evidence_revision,
            "incident_id": self.incident_id, "action_id": self.action_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "TaskEvent":
        value = _fields(
            document, cls._FIELDS, "TaskEvent",
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
        )
        try:
            from_state = None if value["from_state"] is None else TaskState(value["from_state"])
            to_state = TaskState(value["to_state"])
        except (ValueError, TypeError) as error:
            raise ContractError("unknown task event state") from error
        return cls(
            event_id=value["event_id"], task_id=value["task_id"], sequence=value["sequence"],
            event_type=value["event_type"], actor_id=value["actor_id"],
            occurred_at=parse_utc(value["occurred_at"], "occurred_at"),
            from_state=from_state, to_state=to_state, payload=value["payload"],
            evidence_revision=value["evidence_revision"],
            incident_id=value["incident_id"], action_id=value["action_id"],
        )


@dataclass(frozen=True)
class OperatorInputRecord(Contract):
    """Append-only result of routing one exact authenticated operator input."""

    SCHEMA_VERSION: ClassVar[int] = 1
    input_id: str
    recorded_at: datetime
    provenance: Mapping[str, Any]
    input_digest: str
    disposition: str
    task_id: str | None
    response_json: str
    created: bool = False
    ambiguous: bool = False
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "input_id", "recorded_at", "provenance", "input_digest",
        "disposition", "task_id", "response_json", "created", "ambiguous",
    }
    _PROVENANCE_FIELDS = {
        "transport", "namespace", "update_id", "group_id", "sender_id",
        "message_id", "callback_id", "input_kind", "subject_id", "nonce",
        "text_json",
    }

    def __post_init__(self) -> None:
        _identifier(self.input_id, "input_id")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if not isinstance(self.provenance, Mapping):
            raise ContractError("provenance must be an object")
        safe = _safe_json(self.provenance, "provenance")
        if set(safe) != self._PROVENANCE_FIELDS:
            raise ContractError("provenance has unsupported fields")
        for name in ("transport", "namespace", "input_kind"):
            _identifier(safe[name], f"provenance.{name}")
        for name in ("update_id", "group_id", "sender_id", "message_id"):
            value = safe[name]
            if value is None and name == "message_id":
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"provenance.{name} must be an integer")
        for name in ("callback_id", "subject_id", "nonce"):
            value = safe[name]
            if value is not None:
                _string(value, f"provenance.{name}")
        text_json = _string(safe["text_json"], "provenance.text_json")
        try:
            exact_text = json.loads(text_json)
        except (TypeError, ValueError) as error:
            raise ContractError("provenance.text_json must be JSON string text") from error
        if not isinstance(exact_text, str):
            raise ContractError("provenance.text_json must encode a string")
        # Validate what the operator actually sent, not only its JSON-escaped durable
        # spelling.  In particular, quotes around a JSON key hide ``"token":`` from
        # the string-level credential pattern used while validating ``text_json``.
        # If the exact text is itself JSON, recursively apply the normal credential-key
        # and value checks to it as well.  This happens while TaskService's surrounding
        # transaction is still open, so rejection rolls back any task/event mutation.
        # Telegram conversational text may contain newlines.  They are deliberately
        # escaped in the durable spelling; normalize non-printing characters only for
        # this credential scan so multiline input remains supported without letting a
        # newline split a credential marker from its separator.
        credential_scan_text = "".join(
            character if character.isprintable() else " " for character in exact_text
        )
        _safe_json(credential_scan_text, "provenance exact text")
        try:
            shaped_text = json.loads(exact_text)
        except (TypeError, ValueError):
            pass
        else:
            _safe_json(shaped_text, "provenance exact text JSON")
        canonical = json.dumps(
            safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        expected_digest = hashlib.sha256(canonical).hexdigest()
        if self.input_digest != expected_digest:
            raise ContractError("input_digest does not match provenance")
        object.__setattr__(self, "provenance", _freeze(safe))
        _identifier(self.disposition, "disposition")
        if self.task_id is not None:
            _identifier(self.task_id, "task_id")
        response_json = _string(self.response_json, "response_json", allow_empty=True)
        try:
            response = json.loads(response_json)
        except (TypeError, ValueError) as error:
            raise ContractError("response_json must be JSON string text") from error
        if not isinstance(response, str):
            raise ContractError("response_json must encode a string")
        if len(response) > 16 * 1024:
            raise ContractError("response is too long")
        if not isinstance(self.created, bool) or not isinstance(self.ambiguous, bool):
            raise ContractError("created and ambiguous must be booleans")

    @property
    def response(self) -> str:
        value = json.loads(self.response_json)
        assert isinstance(value, str)
        return value

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_id": self.input_id,
            "recorded_at": utc_text(self.recorded_at),
            "provenance": _thaw(self.provenance),
            "input_digest": self.input_digest,
            "disposition": self.disposition,
            "task_id": self.task_id,
            "response_json": self.response_json,
            "created": self.created,
            "ambiguous": self.ambiguous,
        }

    @classmethod
    def from_document(cls, document: Any) -> "OperatorInputRecord":
        value = _fields(document, cls._FIELDS, "OperatorInputRecord")
        return cls(
            input_id=value["input_id"],
            recorded_at=parse_utc(value["recorded_at"], "recorded_at"),
            provenance=value["provenance"],
            input_digest=value["input_digest"],
            disposition=value["disposition"],
            task_id=value["task_id"],
            response_json=value["response_json"],
            created=value["created"],
            ambiguous=value["ambiguous"],
        )


@dataclass(frozen=True)
class RecoveryReport:
    active_task_ids: tuple[str, ...]
    active_lease_ids: tuple[str, ...]
    verified_records: int


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    """Transactionally durable storage for task/plan contract documents."""

    def __init__(
        self,
        location: Path | sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = _default_clock,
        policy_revision: Callable[[], str] | None = None,
        permit_post_deadline_observation: bool = False,
    ):
        self.clock = clock
        self.policy_revision = policy_revision
        # Controller-injected policy, never plan/task input. Default fail closed.
        self.permit_post_deadline_observation = permit_post_deadline_observation is True
        self._savepoints = itertools.count()
        self._owns_connection = not isinstance(location, sqlite3.Connection)
        if isinstance(location, sqlite3.Connection):
            self.db = location
            self.db_path: Path | None = None
        else:
            path = Path(location)
            if path.suffix in {".sqlite", ".sqlite3", ".db"}:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.db_path = path
            else:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.db_path = path / "state.sqlite3"
            if self.db_path.exists() or self.db_path.is_symlink():
                _ensure_shared_sqlite_mode(self.db_path)
            self.db = sqlite3.connect(self.db_path, timeout=30)
            _ensure_shared_sqlite_mode(self.db_path)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=30000")
            if self._owns_connection:
                self._enable_wal()
                for suffix in ("-wal", "-shm"):
                    shared_file = Path(f"{self.db_path}{suffix}")
                    if shared_file.exists():
                        _ensure_shared_sqlite_mode(shared_file)
                self.db.execute("PRAGMA synchronous=FULL")
            self._migrate()
            self.recovery_report = self.recover()
        except BaseException:
            if self._owns_connection:
                self.db.close()
            raise

    def _enable_wal(self) -> None:
        """Tolerate the short journal-mode race between concurrent first starts."""
        for attempt in range(6):
            try:
                mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).casefold() != "wal":
                    raise TaskStoreError("task database did not enter WAL mode")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold() or attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def close(self) -> None:
        if self._owns_connection:
            self.db.close()

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold one durable write boundary across a higher-level task decision.

        Phase 1 routing must choose an active task and append its input atomically;
        otherwise two controller processes can both observe an empty task list and
        create two tasks for two adjacent messages.  Nested store operations use a
        savepoint, so callers do not need access to the private transaction helper.
        """
        with self._transaction():
            yield

    @contextmanager
    def _transaction(self, conflict_message: str | None = None) -> Iterator[None]:
        nested = self.db.in_transaction
        savepoint = f"tc_task_store_{next(self._savepoints)}"
        try:
            if nested:
                self.db.execute(f"SAVEPOINT {savepoint}")
            else:
                self.db.execute("BEGIN IMMEDIATE")
            yield
            if nested:
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.commit()
        except sqlite3.IntegrityError as error:
            if nested:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.rollback()
            if conflict_message is not None:
                raise TaskConflict(conflict_message) from error
            raise
        except BaseException:
            if nested:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.rollback()
            raise

    def _migrate(self) -> None:
        # Read and advance the namespace version under one write lock so two
        # first-starting processes cannot both decide to run migration zero.
        with self._transaction():
            exists = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tc_task_schema'"
            ).fetchone()
            if exists is None:
                version = 0
            else:
                row = self.db.execute(
                    "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
                ).fetchone()
                if row is None:
                    raise RuntimeError("task schema table has no tasks namespace")
                version = int(row[0])
            if version > TASK_STORE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"task schema {version} is newer than supported schema {TASK_STORE_SCHEMA_VERSION}"
                )
            migrations = (
                self._migrate_0_to_1,
                self._migrate_1_to_2,
                self._migrate_2_to_3,
                self._migrate_3_to_4,
            )
            while version < TASK_STORE_SCHEMA_VERSION:
                migrations[version]()
                version += 1
                self.db.execute(
                    "INSERT INTO tc_task_schema(namespace,version) VALUES('tasks',?) "
                    "ON CONFLICT(namespace) DO UPDATE SET version=excluded.version",
                    (version,),
                )

    def _migrate_0_to_1(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS tc_task_schema (
                 namespace TEXT PRIMARY KEY CHECK(namespace='tasks'), version INTEGER NOT NULL
               )""",
            """CREATE TABLE tc_tasks (
                 task_id TEXT PRIMARY KEY, task_hash TEXT NOT NULL, task_json BLOB NOT NULL,
                 state TEXT NOT NULL, current_plan_version INTEGER, latest_event_sequence INTEGER NOT NULL,
                 created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
               )""",
            """CREATE TABLE tc_task_events (
                 event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                 event_hash TEXT NOT NULL UNIQUE, event_json BLOB NOT NULL,
                 from_state TEXT, to_state TEXT NOT NULL, occurred_utc TEXT NOT NULL,
                 incident_id TEXT, action_id TEXT,
                 FOREIGN KEY(task_id) REFERENCES tc_tasks(task_id), UNIQUE(task_id,sequence)
               )""",
            """CREATE TABLE tc_task_plans (
                 task_id TEXT NOT NULL, version INTEGER NOT NULL, plan_id TEXT NOT NULL UNIQUE,
                 plan_hash TEXT NOT NULL UNIQUE, plan_json BLOB NOT NULL, created_utc TEXT NOT NULL,
                 PRIMARY KEY(task_id,version), UNIQUE(task_id,plan_id),
                 FOREIGN KEY(task_id) REFERENCES tc_tasks(task_id)
               )""",
            """CREATE TABLE tc_task_approvals (
                 grant_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                 plan_hash TEXT NOT NULL, grant_hash TEXT NOT NULL UNIQUE, grant_json BLOB NOT NULL,
                 nonce TEXT NOT NULL UNIQUE, issued_utc TEXT NOT NULL,
                 FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id)
               )""",
            """CREATE TABLE tc_task_leases (
                 lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                 plan_hash TEXT NOT NULL, step_id TEXT NOT NULL, holder TEXT NOT NULL,
                 idempotency_key TEXT NOT NULL UNIQUE, lease_hash TEXT NOT NULL UNIQUE,
                 lease_json BLOB NOT NULL, acquired_utc TEXT NOT NULL, expires_utc TEXT NOT NULL,
                 FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id)
               )""",
            """CREATE TABLE tc_task_verifications (
                 verification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                 plan_hash TEXT NOT NULL, step_id TEXT, verification_hash TEXT NOT NULL UNIQUE,
                 verification_json BLOB NOT NULL, status TEXT NOT NULL, performed_utc TEXT NOT NULL,
                 FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id)
               )""",
            "CREATE INDEX tc_task_events_order ON tc_task_events(task_id,sequence)",
            "CREATE INDEX tc_task_leases_active ON tc_task_leases(task_id,plan_id,step_id,expires_utc)",
            "CREATE INDEX tc_task_verifications_plan ON tc_task_verifications(task_id,plan_id,performed_utc)",
        )
        for statement in statements:
            self.db.execute(statement)
        for table in (
            "tc_task_events", "tc_task_plans", "tc_task_approvals",
            "tc_task_leases", "tc_task_verifications",
        ):
            self.db.execute(
                f"""CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
            )
            self.db.execute(
                f"""CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
            )

    def _migrate_1_to_2(self) -> None:
        legacy_counts = {
            table: int(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("tc_task_leases", "tc_task_verifications")
        }
        populated = [table for table, count in legacy_counts.items() if count]
        if populated:
            names = ", ".join(populated)
            raise TaskStoreError(
                "cannot safely migrate populated task schema v1 authorization records "
                f"({names}): legacy leases/verifications do not bind the approval and "
                "execution authority required by schema v2"
            )
        other_legacy_counts = {
            table: int(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in (
                "tc_tasks", "tc_task_events", "tc_task_plans", "tc_task_approvals",
            )
        }
        other_populated = [
            table for table, count in other_legacy_counts.items() if count
        ]
        if other_populated:
            names = ", ".join(other_populated)
            raise TaskStoreError(
                "cannot safely migrate populated task schema v1 records "
                f"({names}): schema v1 task events lack the durable evidence binding "
                "required by schema v2"
            )

        # Empty legacy authorization tables carry no authority to preserve. Rebuild
        # them transactionally so v2 has strict columns and foreign keys rather than
        # nullable ALTER TABLE compatibility columns.
        self.db.execute("DROP TABLE tc_task_verifications")
        self.db.execute("DROP TABLE tc_task_leases")
        self.db.execute(
            """CREATE TABLE tc_task_leases (
                 lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                 plan_hash TEXT NOT NULL, step_id TEXT NOT NULL,
                 grant_id TEXT NOT NULL, grant_hash TEXT NOT NULL,
                 policy_revision TEXT NOT NULL, evidence_revision TEXT NOT NULL,
                 holder TEXT NOT NULL,
                 idempotency_key TEXT NOT NULL UNIQUE, lease_hash TEXT NOT NULL UNIQUE,
                 lease_json BLOB NOT NULL, acquired_utc TEXT NOT NULL, expires_utc TEXT NOT NULL,
                 FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id),
                 FOREIGN KEY(grant_id) REFERENCES tc_task_approvals(grant_id)
               )"""
        )
        self.db.execute(
            """CREATE TABLE tc_task_verifications (
                 verification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                 plan_hash TEXT NOT NULL, step_id TEXT, lease_id TEXT, lease_hash TEXT,
                 verification_hash TEXT NOT NULL UNIQUE,
                 verification_json BLOB NOT NULL, status TEXT NOT NULL, performed_utc TEXT NOT NULL,
                 FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id),
                 FOREIGN KEY(lease_id) REFERENCES tc_task_leases(lease_id)
               )"""
        )
        self.db.execute(
            "CREATE INDEX tc_task_leases_active "
            "ON tc_task_leases(task_id,plan_id,step_id,expires_utc)"
        )
        self.db.execute(
            "CREATE INDEX tc_task_verifications_plan "
            "ON tc_task_verifications(task_id,plan_id,performed_utc)"
        )
        for table in ("tc_task_leases", "tc_task_verifications"):
            self.db.execute(
                f"""CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
            )
            self.db.execute(
                f"""CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
            )

    def _migrate_2_to_3(self) -> None:
        """Add the operator intake ledger without rewriting any v2 record."""
        self.db.execute(
            """CREATE TABLE tc_task_operator_inputs (
                 input_id TEXT PRIMARY KEY,
                 input_hash TEXT NOT NULL UNIQUE,
                 input_json BLOB NOT NULL,
                 transport TEXT NOT NULL,
                 namespace TEXT NOT NULL,
                 group_id INTEGER NOT NULL,
                 update_id INTEGER NOT NULL,
                 input_digest TEXT NOT NULL,
                 disposition TEXT NOT NULL,
                 task_id TEXT,
                 recorded_utc TEXT NOT NULL,
                 UNIQUE(transport,namespace,group_id,update_id),
                 FOREIGN KEY(task_id) REFERENCES tc_tasks(task_id)
               )"""
        )
        self.db.execute(
            "CREATE INDEX tc_task_operator_inputs_task "
            "ON tc_task_operator_inputs(task_id,recorded_utc)"
        )
        self.db.execute(
            """CREATE TRIGGER tc_task_operator_inputs_immutable_update
                BEFORE UPDATE ON tc_task_operator_inputs
                BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
        )
        self.db.execute(
            """CREATE TRIGGER tc_task_operator_inputs_immutable_delete
                BEFORE DELETE ON tc_task_operator_inputs
                BEGIN SELECT RAISE(ABORT, 'immutable task record'); END"""
        )

    def _migrate_3_to_4(self) -> None:
        """Persist the exact creation contract separately from its live snapshot."""
        self.db.execute(
            """CREATE TABLE tc_task_creation_contracts (
                 task_id TEXT PRIMARY KEY, task_hash TEXT NOT NULL,
                 task_json BLOB NOT NULL,
                 FOREIGN KEY(task_id) REFERENCES tc_tasks(task_id)
               )"""
        )
        self.db.execute(
            """CREATE TRIGGER tc_task_creation_contracts_immutable_update
                BEFORE UPDATE ON tc_task_creation_contracts
                BEGIN SELECT RAISE(ABORT, 'immutable task creation contract'); END"""
        )
        self.db.execute(
            """CREATE TRIGGER tc_task_creation_contracts_immutable_delete
                BEFORE DELETE ON tc_task_creation_contracts
                BEGIN SELECT RAISE(ABORT, 'immutable task creation contract'); END"""
        )
        rows = self.db.execute("SELECT * FROM tc_tasks ORDER BY created_utc,task_id").fetchall()
        for row in rows:
            current = self._stored_contract(row, "task_json", "task_hash", Task)
            event_row = self.db.execute(
                "SELECT * FROM tc_task_events WHERE task_id=? AND sequence=1",
                (current.task_id,),
            ).fetchone()
            if event_row is None:
                raise TaskStoreError("task is missing its immutable creation event")
            event = self._stored_contract(event_row, "event_json", "event_hash", TaskEvent)
            incident_ids = current.incident_ids
            payload_incidents = event.payload.get("incident_ids")
            if (
                event.event_type == "correlation-created"
                and event.actor_id == "phase6-correlator"
                and isinstance(payload_incidents, (list, tuple))
                and all(isinstance(item, str) for item in payload_incidents)
                and set(payload_incidents) <= set(current.incident_ids)
            ):
                incident_ids = tuple(payload_incidents)
            creation = replace(
                current, state=TaskState.INVESTIGATING, current_plan_version=None,
                evidence_revision=event.evidence_revision,
                incident_ids=incident_ids,
            )
            self.db.execute(
                "INSERT INTO tc_task_creation_contracts(task_id,task_hash,task_json) VALUES(?,?,?)",
                (creation.task_id, creation.content_hash, creation.canonical_json()),
            )

    @staticmethod
    def _stored_contract(row: sqlite3.Row, json_column: str, hash_column: str, cls: type[Any]) -> Any:
        raw = bytes(row[json_column]) if not isinstance(row[json_column], str) else row[json_column].encode()
        value = cls.from_json(raw)
        if raw != value.canonical_json() or value.content_hash != row[hash_column]:
            raise TaskStoreError(f"stored {cls.__name__} canonical JSON/hash mismatch")
        return value

    @staticmethod
    def _same_contract(left: Contract, right: Contract) -> bool:
        """Compare exact canonical bytes, preserving JSON numeric distinctions."""
        return type(left) is type(right) and left.canonical_json() == right.canonical_json()

    def _now(self) -> datetime:
        return _utc(self.clock(), "clock")

    def _current_policy_revision(self) -> str | None:
        if self.policy_revision is None:
            return None
        return _identifier(self.policy_revision(), "current policy revision")

    def _has_active_lease(self, task_id: str, now: datetime) -> bool:
        rows = self.db.execute(
            "SELECT expires_utc FROM tc_task_leases WHERE task_id=?", (task_id,)
        ).fetchall()
        return any(parse_utc(row[0], "lease expires_utc") > now for row in rows)

    @staticmethod
    def _enforce_task_deadline(
        task: Task, now: datetime, *bounded_times: tuple[str, datetime],
    ) -> None:
        if task.deadline is None:
            return
        if now >= task.deadline:
            raise TaskConflict("task deadline has expired")
        for name, value in bounded_times:
            if value > task.deadline:
                raise TaskConflict(f"{name} exceeds the task deadline")

    def _task(self, task_id: str) -> Task:
        row = self.db.execute("SELECT * FROM tc_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskStoreError("unknown task")
        return self._stored_contract(row, "task_json", "task_hash", Task)

    def get_task(self, task_id: str) -> Task | None:
        row = self.db.execute("SELECT * FROM tc_tasks WHERE task_id=?", (task_id,)).fetchone()
        return None if row is None else self._stored_contract(row, "task_json", "task_hash", Task)

    def list_tasks(
        self,
        *,
        requester_group_id: int | None = None,
        requester_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[Task, ...]:
        """Return task snapshots in creation order for coordinator recovery/UI work."""
        rows = self.db.execute(
            "SELECT * FROM tc_tasks ORDER BY created_utc, task_id"
        ).fetchall()
        tasks = tuple(
            self._stored_contract(row, "task_json", "task_hash", Task) for row in rows
        )
        return tuple(
            task for task in tasks
            if (requester_group_id is None or task.requester_group_id == requester_group_id)
            and (requester_id is None or task.requester_id == requester_id)
            and (include_terminal or task.state not in TERMINAL_STATES)
        )

    def get_event(self, event_id: str) -> TaskEvent | None:
        """Look up an immutable event by its transport idempotency identity."""
        row = self.db.execute(
            "SELECT * FROM tc_task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return None if row is None else self._stored_contract(
            row, "event_json", "event_hash", TaskEvent
        )

    def get_operator_input(self, input_id: str) -> OperatorInputRecord | None:
        """Look up the durable result for one transport update identity."""
        row = self.db.execute(
            "SELECT * FROM tc_task_operator_inputs WHERE input_id=?", (input_id,)
        ).fetchone()
        return None if row is None else self._stored_contract(
            row, "input_json", "input_hash", OperatorInputRecord
        )

    def record_operator_input(self, record: OperatorInputRecord) -> bool:
        """Append one authenticated input/result pair, or verify its exact replay."""
        if record.recorded_at > self._now():
            raise TaskConflict("operator input is future-dated")
        provenance = record.provenance
        key = (
            provenance["transport"], provenance["namespace"],
            provenance["group_id"], provenance["update_id"],
        )
        existing = self.db.execute(
            """SELECT * FROM tc_task_operator_inputs
               WHERE input_id=? OR
                     (transport=? AND namespace=? AND group_id=? AND update_id=?)""",
            (record.input_id, *key),
        ).fetchone()
        if existing is not None:
            stored = self._stored_contract(
                existing, "input_json", "input_hash", OperatorInputRecord
            )
            if self._same_contract(stored, record):
                return False
            raise TaskConflict("operator input identity was replayed with different content")
        with self._transaction("operator input identity already exists"):
            self.db.execute(
                """INSERT INTO tc_task_operator_inputs(
                       input_id,input_hash,input_json,transport,namespace,group_id,
                       update_id,input_digest,disposition,task_id,recorded_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.input_id, record.content_hash, record.canonical_json(),
                    provenance["transport"], provenance["namespace"],
                    provenance["group_id"], provenance["update_id"],
                    record.input_digest, record.disposition, record.task_id,
                    utc_text(record.recorded_at),
                ),
            )
        return True

    def _plan_row(self, task_id: str, plan_id: str) -> tuple[sqlite3.Row, Plan]:
        row = self.db.execute(
            "SELECT * FROM tc_task_plans WHERE task_id=? AND plan_id=?", (task_id, plan_id)
        ).fetchone()
        if row is None:
            raise TaskStoreError("unknown task plan")
        return row, self._stored_contract(row, "plan_json", "plan_hash", Plan)

    def get_plan(self, task_id: str, version: int | None = None) -> Plan | None:
        if version is None:
            task = self.get_task(task_id)
            if task is None or task.current_plan_version is None:
                return None
            version = task.current_plan_version
        row = self.db.execute(
            "SELECT * FROM tc_task_plans WHERE task_id=? AND version=?", (task_id, version)
        ).fetchone()
        return None if row is None else self._stored_contract(row, "plan_json", "plan_hash", Plan)

    def _update_task(self, task: Task, updated_at: datetime, latest_sequence: int) -> None:
        self.db.execute(
            """UPDATE tc_tasks SET task_hash=?,task_json=?,state=?,current_plan_version=?,
                   latest_event_sequence=?,updated_utc=? WHERE task_id=?""",
            (
                task.content_hash, task.canonical_json(), task.state.value,
                task.current_plan_version, latest_sequence, utc_text(updated_at), task.task_id,
            ),
        )
        if self.db.execute("SELECT changes()").fetchone()[0] != 1:
            raise TaskStoreError("task update lost")

    def create_task(self, task: Task, event: TaskEvent | None = None) -> bool:
        if task.created_at > self._now():
            raise IllegalTransition("task creation is future-dated")
        if task.state is not TaskState.INVESTIGATING or task.current_plan_version is not None:
            raise IllegalTransition("new tasks must start investigating without a current plan")
        if event is None:
            event = TaskEvent(
                event_id=f"task-created:{task.task_id}", task_id=task.task_id, sequence=1,
                event_type="task-created", actor_id=task.requester_id,
                occurred_at=task.created_at, from_state=None, to_state=task.state, payload={},
                evidence_revision=task.evidence_revision,
            )
        if (
            event.task_id != task.task_id or event.sequence != 1 or event.from_state is not None
            or event.to_state is not task.state or event.evidence_revision != task.evidence_revision
            or event.occurred_at != task.created_at
        ):
            raise IllegalTransition("creation event does not create the exact task state")
        existing = self.db.execute("SELECT * FROM tc_tasks WHERE task_id=?", (task.task_id,)).fetchone()
        if existing is not None:
            creation_row = self.db.execute(
                "SELECT * FROM tc_task_creation_contracts WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if creation_row is None:
                raise TaskStoreError("task is missing its immutable creation contract")
            creation = self._stored_contract(
                creation_row, "task_json", "task_hash", Task,
            )
            event_row = self.db.execute(
                "SELECT * FROM tc_task_events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if self._same_contract(creation, task) and event_row is not None:
                stored_event = self._stored_contract(event_row, "event_json", "event_hash", TaskEvent)
                if self._same_contract(stored_event, event):
                    return False
            raise TaskConflict("task identifier was replayed with different content")
        with self._transaction("task or creation event already exists"):
            self.db.execute(
                """INSERT INTO tc_tasks(task_id,task_hash,task_json,state,current_plan_version,
                       latest_event_sequence,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    task.task_id, task.content_hash, task.canonical_json(), task.state.value,
                    task.current_plan_version, 1, utc_text(task.created_at), utc_text(event.occurred_at),
                ),
            )
            self.db.execute(
                """INSERT INTO tc_task_creation_contracts(task_id,task_hash,task_json)
                   VALUES(?,?,?)""",
                (task.task_id, task.content_hash, task.canonical_json()),
            )
            self._insert_event(event)
        return True

    def _insert_event(self, event: TaskEvent) -> None:
        self.db.execute(
            """INSERT INTO tc_task_events(event_id,task_id,sequence,event_hash,event_json,
                   from_state,to_state,occurred_utc,incident_id,action_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, event.task_id, event.sequence, event.content_hash,
                event.canonical_json(),
                None if event.from_state is None else event.from_state.value,
                event.to_state.value, utc_text(event.occurred_at), event.incident_id, event.action_id,
            ),
        )

    def append_event(self, event: TaskEvent) -> bool:
        if event.event_type == "plan-added":
            raise IllegalTransition("plan-added events are written atomically with plans")
        if event.occurred_at > self._now():
            raise IllegalTransition("task event is future-dated")
        existing = self.db.execute(
            "SELECT * FROM tc_task_events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        if existing is not None:
            stored = self._stored_contract(existing, "event_json", "event_hash", TaskEvent)
            if self._same_contract(stored, event):
                return False
            raise TaskConflict("event identifier was replayed with different content")
        with self._transaction("task event already exists"):
            row = self.db.execute(
                "SELECT * FROM tc_tasks WHERE task_id=?", (event.task_id,)
            ).fetchone()
            if row is None:
                raise TaskStoreError("unknown task")
            task = self._stored_contract(row, "task_json", "task_hash", Task)
            expected_sequence = int(row["latest_event_sequence"]) + 1
            if event.sequence != expected_sequence:
                raise IllegalTransition(f"expected event sequence {expected_sequence}")
            prior_time = parse_utc(
                self.db.execute(
                    "SELECT occurred_utc FROM tc_task_events WHERE task_id=? AND sequence=?",
                    (event.task_id, expected_sequence - 1),
                ).fetchone()[0],
                "prior event occurred_utc",
            )
            if event.occurred_at < prior_time:
                raise IllegalTransition("task event time precedes the prior event")
            if event.from_state is not task.state:
                raise IllegalTransition("event does not start at the current task state")
            if not self._legal_event_transition(event):
                raise IllegalTransition(
                    f"illegal task transition {task.state.value} -> {event.to_state.value}"
                )
            adds_incident = (
                event.incident_id is not None and event.incident_id not in task.incident_ids
            )
            if adds_incident and not self._validated_correlation_incident_link(event):
                raise IllegalTransition(
                    "incident_ids may grow only through a validated correlator incident link"
                )
            revised = replace(
                task,
                state=event.to_state,
                evidence_revision=(event.evidence_revision or task.evidence_revision),
                incident_ids=(
                    task.incident_ids
                    if event.incident_id is None or event.incident_id in task.incident_ids
                    else task.incident_ids + (event.incident_id,)
                ),
            )
            self._insert_event(event)
            self._update_task(revised, event.occurred_at, event.sequence)
        return True

    def _validated_correlation_incident_link(self, event: TaskEvent) -> bool:
        if (
            event.event_type != "incident-linked"
            or event.actor_id != "phase6-correlator"
            or event.incident_id is None
        ):
            return False
        # The correlator must first bind the incident to this task in its
        # authoritative evaluation, in the same BEGIN IMMEDIATE transaction.
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tc_correlation_evaluations'"
        ).fetchone()
        if exists is None:
            return False
        rows = self.db.execute(
            """SELECT incident_ids_json FROM tc_correlation_evaluations
               WHERE task_id=? ORDER BY id DESC""", (event.task_id,)
        ).fetchall()
        for row in rows:
            try:
                if event.incident_id in json.loads(row["incident_ids_json"]):
                    return True
            except (TypeError, ValueError):
                raise TaskStoreError("correlation incident link record is invalid")
        return False

    def events(self, task_id: str) -> tuple[TaskEvent, ...]:
        rows = self.db.execute(
            "SELECT * FROM tc_task_events WHERE task_id=? ORDER BY sequence", (task_id,)
        ).fetchall()
        return tuple(
            self._stored_contract(row, "event_json", "event_hash", TaskEvent) for row in rows
        )

    def _executor_document(self, table: str, task_id: str, plan_hash: str | None = None) -> dict | None:
        if table not in {"tc_step_runs", "tc_step_run_history"}:
            raise TaskStoreError("unknown executor table")
        # Optional executor tables are absent on observation-only installations.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return None
        query, args = f"SELECT * FROM {table} WHERE task_id=?", (task_id,)
        if plan_hash is not None:
            query += " AND plan_hash=?"
            args += (plan_hash,)
        row = self.db.execute(query, args).fetchone()
        if row is None:
            return None
        doc = strict_json_loads(row["document"])
        if stable_hash(doc) != row["digest"] or doc["task_id"] != task_id:
            raise TaskStoreError("execution journal integrity failure")
        return doc

    def _legal_event_transition(self, event: TaskEvent) -> bool:
        if event.to_state is event.from_state or event.to_state in LEGAL_TRANSITIONS[event.from_state]:
            return True
        # Only an execution whose durable pre-supersede status was exactly
        # rolled_back can be reopened. An arbitrary FAILED task, even with a safe
        # journal, does not acquire an outgoing transition.
        if (event.from_state is TaskState.FAILED and event.to_state is TaskState.PLANNING
                and event.event_type == "executor-replan" and event.actor_id == "step-executor"
                and isinstance(event.payload.get("plan_hash"), str)):
            doc = self._executor_document("tc_step_run_history", event.task_id, event.payload["plan_hash"])
            preceding = self.db.execute(
                "SELECT event_json FROM tc_task_events WHERE task_id=? AND sequence=?",
                (event.task_id, event.sequence - 1),
            ).fetchone()
            prior = TaskEvent.from_document(strict_json_loads(preceding[0])) if preceding else None
            return bool(prior and prior.event_type == "executor-superseded"
                        and prior.payload == event.payload
                        and doc and doc["status"] == "superseded"
                        and doc.get("superseded_from") == "rolled_back"
                        and doc["plan_hash"] == event.payload["plan_hash"]
                        and stable_hash(doc) == event.payload.get("journal_hash")
                        and not doc["rollback_queue"]
                        and all(e["state"] == "resolved" for e in doc["effects"])
                        and not any(a["state"] in {"intent", "dispatched", "uncertain", "applied"}
                                    for a in doc["attempts"]))
        return False

    def _execution_verification_time(self, value: Verification) -> datetime | None:
        doc = self._executor_document("tc_step_runs", value.task_id)
        if doc is None or doc["plan_hash"] != value.plan_hash:
            return None
        for attempt in doc["attempts"]:
            if (attempt["lease_id"] == value.lease_id
                    and attempt.get("pending_verification") == value.to_document()):
                return parse_utc(attempt["result_at"], "execution result time")
        return None

    def add_plan(self, plan: Plan) -> bool:
        existing = self.db.execute(
            "SELECT * FROM tc_task_plans WHERE plan_id=?", (plan.plan_id,)
        ).fetchone()
        if existing is not None:
            stored = self._stored_contract(existing, "plan_json", "plan_hash", Plan)
            if self._same_contract(stored, plan):
                return False
            raise TaskConflict("plan identifier was replayed with different content")
        with self._transaction("plan identifier, version, or hash already exists"):
            row = self.db.execute("SELECT * FROM tc_tasks WHERE task_id=?", (plan.task_id,)).fetchone()
            if row is None:
                raise TaskStoreError("unknown task")
            task = self._stored_contract(row, "task_json", "task_hash", Task)
            if task.state not in {TaskState.INVESTIGATING, TaskState.PLANNING}:
                raise IllegalTransition("plans may only be added while investigating or planning")
            if plan.evidence_revision != task.evidence_revision:
                raise TaskConflict("plan evidence revision does not match the task")
            now = self._now()
            self._enforce_task_deadline(
                task, now,
                ("plan creation", plan.created_at),
                ("plan expiry", plan.expires_at),
            )
            if plan.created_at > now or plan.expires_at <= now:
                raise TaskConflict("plan is future-dated or already expired")
            prior_event = self.db.execute(
                "SELECT occurred_utc FROM tc_task_events WHERE task_id=? AND sequence=?",
                (plan.task_id, int(row["latest_event_sequence"])),
            ).fetchone()
            if prior_event is None or plan.created_at < parse_utc(prior_event[0], "prior event time"):
                raise TaskConflict("plan creation precedes current task history")
            if task.current_plan_version is not None and self._has_active_lease(plan.task_id, now):
                raise TaskConflict("cannot replace a plan while an execution lease is active")
            if task.current_plan_version is not None and self._executor_document("tc_step_runs", plan.task_id):
                raise TaskConflict("registered execution must be safely superseded before replacing its plan")
            expected = 1 if task.current_plan_version is None else task.current_plan_version + 1
            if plan.version != expected:
                raise TaskConflict(f"expected plan version {expected}")
            self.db.execute(
                """INSERT INTO tc_task_plans(task_id,version,plan_id,plan_hash,plan_json,created_utc)
                   VALUES(?,?,?,?,?,?)""",
                (
                    plan.task_id, plan.version, plan.plan_id, plan.content_hash,
                    plan.canonical_json(), utc_text(plan.created_at),
                ),
            )
            sequence = int(row["latest_event_sequence"]) + 1
            audit_event = TaskEvent(
                event_id=f"plan-added:{plan.plan_id}", task_id=plan.task_id,
                sequence=sequence, event_type="plan-added", actor_id="coordinator",
                occurred_at=plan.created_at, from_state=task.state, to_state=task.state,
                payload={
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "plan_hash": plan.content_hash,
                    "evidence_revision": plan.evidence_revision,
                },
                evidence_revision=task.evidence_revision,
            )
            self._insert_event(audit_event)
            self._update_task(
                replace(task, current_plan_version=plan.version), plan.created_at,
                sequence,
            )
        return True

    def add_approval(self, grant: ApprovalGrant) -> bool:
        return self._add_plan_record(
            table="tc_task_approvals", id_column="grant_id", identifier=grant.grant_id,
            hash_column="grant_hash", json_column="grant_json", value=grant,
            extra_columns=("nonce", "issued_utc"),
            extra_values=(grant.nonce, utc_text(grant.issued_at)),
        )

    def add_verification(self, verification: Verification) -> bool:
        return self._add_plan_record(
            table="tc_task_verifications", id_column="verification_id",
            identifier=verification.verification_id, hash_column="verification_hash",
            json_column="verification_json", value=verification,
            extra_columns=("step_id", "lease_id", "lease_hash", "status", "performed_utc"),
            extra_values=(
                verification.step_id, verification.lease_id, verification.lease_hash,
                verification.status.value, utc_text(verification.performed_at),
            ),
        )

    def _add_plan_record(
        self, *, table: str, id_column: str, identifier: str, hash_column: str,
        json_column: str, value: Any, extra_columns: tuple[str, ...], extra_values: tuple[Any, ...],
    ) -> bool:
        existing = self.db.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?", (identifier,)
        ).fetchone()
        if existing is not None:
            stored = self._stored_contract(existing, json_column, hash_column, type(value))
            if self._same_contract(stored, value):
                return False
            raise TaskConflict(f"{id_column} was replayed with different content")
        with self._transaction(f"{id_column}, hash, or nonce already exists"):
            _row, plan = self._plan_row(value.task_id, value.plan_id)
            if value.plan_hash != plan.content_hash:
                raise TaskConflict("record does not bind the stored plan hash")
            if isinstance(value, ApprovalGrant):
                task = self._task(value.task_id)
                if task.current_plan_version != plan.version:
                    raise TaskConflict("approval does not bind the current plan")
                if task.state not in {TaskState.PLANNING, TaskState.AWAITING_APPROVAL}:
                    raise IllegalTransition("approvals require a planning or awaiting-approval task")
                plan_steps = {step.step_id for step in plan.steps}
                if not set(value.step_ids).issubset(plan_steps):
                    raise TaskConflict("approval names a step outside the plan")
                if value.evidence_revision != plan.evidence_revision or value.evidence_revision != task.evidence_revision:
                    raise TaskConflict("approval evidence revision is not current")
                if value.issued_at < plan.created_at or value.expires_at > plan.expires_at:
                    raise TaskConflict("approval lifetime is outside the plan lifetime")
                now = self._now()
                self._enforce_task_deadline(
                    task, now,
                    ("plan expiry", plan.expires_at),
                    ("approval issuance", value.issued_at),
                    ("approval expiry", value.expires_at),
                )
                if value.issued_at > now or value.expires_at <= now or plan.expires_at <= now:
                    raise TaskConflict("approval or plan is not live at the store clock")
                current_policy = self._current_policy_revision()
                if current_policy is not None and value.policy_revision != current_policy:
                    raise TaskConflict("approval policy revision is not current")
            if isinstance(value, Verification):
                task = self._task(value.task_id)
                if task.current_plan_version != plan.version:
                    raise TaskConflict("verification does not bind the current plan")
                if task.state is not TaskState.VERIFYING:
                    raise IllegalTransition("verification records require a verifying task")
                if value.evidence_revision != task.evidence_revision:
                    raise TaskConflict("verification evidence revision is not current")
                now = self._now()
                if value.performed_at > now:
                    raise TaskConflict("verification is future-dated")
                deadline_times: list[tuple[str, datetime]] = [
                    ("plan expiry", plan.expires_at),
                    ("verification time", value.performed_at),
                ]
                transition_row = self.db.execute(
                    """SELECT occurred_utc FROM tc_task_events
                       WHERE task_id=? AND to_state=? ORDER BY sequence DESC LIMIT 1""",
                    (value.task_id, TaskState.VERIFYING.value),
                ).fetchone()
                if transition_row is None:
                    raise TaskStoreError("verifying task has no durable verifying transition")
                verifying_at = parse_utc(transition_row[0], "verifying transition time")
                execution_result_at = self._execution_verification_time(value)
                # An authenticated receipt may arrive during pause, before the
                # resumed verifying event. Its durable result time is causal.
                causal_floor = max(plan.created_at, execution_result_at or verifying_at)
                if value.step_id is not None:
                    steps_by_id = {step.step_id: step for step in plan.steps}
                    if value.step_id not in steps_by_id:
                        raise TaskConflict("verification names a step outside the plan")
                    if value.lease_id is None:
                        raise TaskConflict("step verification must bind an execution lease")
                    lease_row = self.db.execute(
                        "SELECT * FROM tc_task_leases WHERE lease_id=?", (value.lease_id,)
                    ).fetchone()
                    if lease_row is None:
                        raise TaskConflict("verification execution lease does not exist")
                    lease = self._stored_contract(
                        lease_row, "lease_json", "lease_hash", ExecutionLease
                    )
                    if (
                        value.lease_hash != lease.content_hash
                        or lease.task_id != value.task_id
                        or lease.plan_id != value.plan_id
                        or lease.plan_hash != value.plan_hash
                        or lease.step_id != value.step_id
                    ):
                        raise TaskConflict("verification execution lease binding does not match")
                    if value.performed_at < lease.acquired_at:
                        raise TaskConflict("verification predates execution")
                    causal_floor = max(causal_floor, lease.acquired_at)
                    deadline_times.append(("execution lease expiry", lease.expires_at))
                    grant_row = self.db.execute(
                        "SELECT * FROM tc_task_approvals WHERE grant_id=?", (lease.grant_id,)
                    ).fetchone()
                    if grant_row is None:
                        raise TaskStoreError("verification execution lease approval is missing")
                    grant = self._stored_contract(
                        grant_row, "grant_json", "grant_hash", ApprovalGrant
                    )
                    deadline_times.append(("approval expiry", grant.expires_at))
                    expected_checks = {
                        item.get("check") for item in steps_by_id[value.step_id].postconditions
                        if isinstance(item.get("check"), str)
                    }
                else:
                    lease_rows = self.db.execute(
                        "SELECT * FROM tc_task_leases WHERE task_id=? AND plan_id=?",
                        (value.task_id, value.plan_id),
                    ).fetchall()
                    leases = [
                        self._stored_contract(row, "lease_json", "lease_hash", ExecutionLease)
                        for row in lease_rows
                    ]
                    leased_steps = {lease.step_id for lease in leases}
                    if leased_steps != {step.step_id for step in plan.steps}:
                        raise TaskConflict("plan verification requires leases for every plan step")
                    if leases:
                        causal_floor = max(causal_floor, *(lease.acquired_at for lease in leases))
                    for lease in leases:
                        deadline_times.append(("execution lease expiry", lease.expires_at))
                        grant_row = self.db.execute(
                            "SELECT * FROM tc_task_approvals WHERE grant_id=?", (lease.grant_id,)
                        ).fetchone()
                        if grant_row is None:
                            raise TaskStoreError("verification execution lease approval is missing")
                        grant = self._stored_contract(
                            grant_row, "grant_json", "grant_hash", ApprovalGrant
                        )
                        deadline_times.append(("approval expiry", grant.expires_at))
                    expected_checks = {
                        item.get("check") for step in plan.steps for item in step.postconditions
                        if isinstance(item.get("check"), str)
                    }
                if value.performed_at < causal_floor:
                    raise TaskConflict(
                        "verification predates plan creation, execution, or verifying transition"
                    )
                if (
                    execution_result_at is not None and task.deadline is not None
                    and self.permit_post_deadline_observation
                    and execution_result_at < task.deadline
                ):
                    # The durable executor result time causally proves the effect
                    # and its result predate the deadline; explicit policy admits
                    # only the read-only observation afterwards. Authority bounds
                    # (plan, lease, approval) remain subject to the deadline.
                    for name, bounded in deadline_times:
                        if name != "verification time" and bounded > task.deadline:
                            raise TaskConflict(f"{name} exceeds the task deadline")
                else:
                    self._enforce_task_deadline(task, now, *deadline_times)
                actual_checks = {
                    item.get("check") for item in value.checks
                    if isinstance(item.get("check"), str)
                }
                if not expected_checks.issubset(actual_checks):
                    raise TaskConflict("verification omits a declared postcondition")
            columns = (id_column, "task_id", "plan_id", "plan_hash", hash_column, json_column) + extra_columns
            placeholders = ",".join("?" for _ in columns)
            self.db.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
                (
                    identifier, value.task_id, value.plan_id, value.plan_hash,
                    value.content_hash, value.canonical_json(), *extra_values,
                ),
            )
        return True

    def _validate_lease_authority(
        self, lease: ExecutionLease, *, replay: bool, checked_at: datetime | None = None,
    ) -> tuple[Plan, datetime]:
        _row, plan = self._plan_row(lease.task_id, lease.plan_id)
        task = self._task(lease.task_id)
        if task.current_plan_version != plan.version or lease.plan_hash != plan.content_hash:
            raise TaskConflict("lease does not bind the current plan")
        if plan.evidence_revision != task.evidence_revision or lease.evidence_revision != task.evidence_revision:
            raise TaskConflict("lease evidence revision is not current")
        if task.state is not TaskState.EXECUTING:
            raise IllegalTransition("execution leases require an executing task")
        if lease.step_id not in {step.step_id for step in plan.steps}:
            raise TaskConflict("lease names a step outside the plan")
        step = next(step for step in plan.steps if step.step_id == lease.step_id)
        grant_row = self.db.execute(
            "SELECT * FROM tc_task_approvals WHERE grant_id=?", (lease.grant_id,)
        ).fetchone()
        if grant_row is None:
            raise TaskConflict("execution lease has no approval grant")
        grant = self._stored_contract(
            grant_row, "grant_json", "grant_hash", ApprovalGrant
        )
        if (
            lease.grant_hash != grant.content_hash
            or grant.task_id != lease.task_id
            or grant.plan_id != lease.plan_id
            or grant.plan_hash != lease.plan_hash
            or lease.step_id not in grant.step_ids
            or lease.policy_revision != grant.policy_revision
            or lease.evidence_revision != grant.evidence_revision
        ):
            raise TaskConflict("execution lease approval binding does not match")
        current_policy = self._current_policy_revision()
        if current_policy is None:
            raise TaskStoreError("execution leasing requires a current policy revision provider")
        if lease.policy_revision != current_policy:
            raise TaskConflict("execution lease policy revision is not current")
        now = self._now() if checked_at is None else checked_at
        if (not replay and lease.acquired_at != now) or (replay and lease.acquired_at > now):
            raise TaskConflict("lease acquisition must use the store clock")
        self._enforce_task_deadline(
            task, now,
            ("plan expiry", plan.expires_at),
            ("approval expiry", grant.expires_at),
            ("execution lease expiry", lease.expires_at),
        )
        if (
            plan.expires_at <= now
            or grant.issued_at > now
            or grant.expires_at <= now
            or lease.expires_at <= now
            or lease.acquired_at < grant.issued_at
            or lease.expires_at > grant.expires_at
            or lease.expires_at > plan.expires_at
            or lease.expires_at > lease.acquired_at + timedelta(seconds=step.max_execution_seconds)
        ):
            raise TaskConflict("plan, approval, or lease is not live at the store clock")
        return plan, now

    def acquire_lease(self, lease: ExecutionLease) -> bool:
        return self._acquire_lease(lease)

    def acquire_current_lease(self, draft: ExecutionLease) -> ExecutionLease:
        """Stamp and acquire a new lease at one store-owned clock instant.

        Caller-created timestamps cannot equal a live wall clock on the next
        function call. Preserve acquire_lease's strict replay/clock checks while
        giving executors a transactional construction path. The draft's expiry
        remains an upper bound; all grant, deadline and duration checks still run.
        """
        with self._transaction():
            now = self._now()
            lease = replace(draft, acquired_at=now)
            self._acquire_lease(lease, checked_at=now)
            return lease

    def _acquire_lease(self, lease: ExecutionLease, *, checked_at: datetime | None = None) -> bool:
        with self._transaction("lease identifier, idempotency key, or hash already exists"):
            existing = self.db.execute(
                "SELECT * FROM tc_task_leases WHERE lease_id=? OR idempotency_key=?",
                (lease.lease_id, lease.idempotency_key),
            ).fetchone()
            if existing is not None:
                stored = self._stored_contract(
                    existing, "lease_json", "lease_hash", ExecutionLease
                )
                if not self._same_contract(stored, lease):
                    raise TaskConflict("lease identifier or idempotency key has different content")
                self._validate_lease_authority(lease, replay=True, checked_at=checked_at)
                return False
            _plan, now = self._validate_lease_authority(lease, replay=False, checked_at=checked_at)
            active_rows = self.db.execute(
                """SELECT expires_utc FROM tc_task_leases
                   WHERE task_id=? AND plan_id=? AND step_id=?""",
                (lease.task_id, lease.plan_id, lease.step_id),
            ).fetchall()
            if any(parse_utc(row[0], "lease expires_utc") > now for row in active_rows):
                raise TaskConflict("plan step already has an active execution lease")
            self.db.execute(
                """INSERT INTO tc_task_leases(lease_id,task_id,plan_id,plan_hash,step_id,
                   grant_id,grant_hash,policy_revision,evidence_revision,holder,idempotency_key,
                   lease_hash,lease_json,acquired_utc,expires_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lease.lease_id, lease.task_id, lease.plan_id, lease.plan_hash,
                    lease.step_id, lease.grant_id, lease.grant_hash, lease.policy_revision,
                    lease.evidence_revision, lease.holder, lease.idempotency_key,
                    lease.content_hash, lease.canonical_json(), utc_text(lease.acquired_at),
                    utc_text(lease.expires_at),
                ),
            )
        return True

    def approvals(self, task_id: str) -> tuple[ApprovalGrant, ...]:
        rows = self.db.execute(
            "SELECT * FROM tc_task_approvals WHERE task_id=? ORDER BY issued_utc,grant_id", (task_id,)
        ).fetchall()
        return tuple(self._stored_contract(row, "grant_json", "grant_hash", ApprovalGrant) for row in rows)

    def leases(self, task_id: str) -> tuple[ExecutionLease, ...]:
        rows = self.db.execute(
            "SELECT * FROM tc_task_leases WHERE task_id=? ORDER BY acquired_utc,lease_id", (task_id,)
        ).fetchall()
        return tuple(self._stored_contract(row, "lease_json", "lease_hash", ExecutionLease) for row in rows)

    def verifications(self, task_id: str) -> tuple[Verification, ...]:
        rows = self.db.execute(
            "SELECT * FROM tc_task_verifications WHERE task_id=? ORDER BY performed_utc,verification_id",
            (task_id,),
        ).fetchall()
        return tuple(
            self._stored_contract(row, "verification_json", "verification_hash", Verification)
            for row in rows
        )

    def recover(self) -> RecoveryReport:
        """Verify durable hashes and expose resumable work after a process restart."""
        integrity = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise TaskStoreError("SQLite task store integrity check failed")
        foreign_key_failures = self.db.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_failures:
            raise TaskStoreError("SQLite task store foreign key check failed")
        verified = 0
        active_tasks: list[str] = []
        task_rows = self.db.execute("SELECT * FROM tc_tasks ORDER BY task_id").fetchall()
        for row in task_rows:
            task = self._stored_contract(row, "task_json", "task_hash", Task)
            verified += 1
            if (
                row["task_id"] != task.task_id
                or row["state"] != task.state.value
                or row["current_plan_version"] != task.current_plan_version
                or row["created_utc"] != utc_text(task.created_at)
            ):
                raise TaskStoreError("task indexed columns disagree with its contract")
            event_rows = self.db.execute(
                "SELECT * FROM tc_task_events WHERE task_id=? ORDER BY sequence", (task.task_id,)
            ).fetchall()
            events: list[TaskEvent] = []
            for event_row in event_rows:
                event = self._stored_contract(event_row, "event_json", "event_hash", TaskEvent)
                if (
                    event_row["event_id"] != event.event_id
                    or event_row["task_id"] != event.task_id
                    or event_row["sequence"] != event.sequence
                    or event_row["from_state"] != (
                        None if event.from_state is None else event.from_state.value
                    )
                    or event_row["to_state"] != event.to_state.value
                    or event_row["occurred_utc"] != utc_text(event.occurred_at)
                    or event_row["incident_id"] != event.incident_id
                    or event_row["action_id"] != event.action_id
                ):
                    raise TaskStoreError("task event indexed columns disagree with its contract")
                events.append(event)
            verified += len(events)
            if not events or events[0].from_state is not None or events[0].sequence != 1:
                raise TaskStoreError("task event stream has no valid creation event")
            if (
                events[0].occurred_at != task.created_at
                or events[0].to_state is not TaskState.INVESTIGATING
            ):
                raise TaskStoreError("task event stream has an invalid creation event")
            if events[0].evidence_revision != task.to_document()["evidence_revision"] and len(events) == 1:
                raise TaskStoreError("task creation event has the wrong evidence revision")
            replay_state = events[0].to_state
            replay_evidence = events[0].evidence_revision
            replay_time = events[0].occurred_at
            if replay_evidence is None:
                raise TaskStoreError("task creation event has no evidence revision")
            for expected, event in enumerate(events[1:], 2):
                if event.sequence != expected or event.from_state is not replay_state:
                    raise TaskStoreError("task event stream is not replayable")
                if not self._legal_event_transition(event):
                    raise TaskStoreError("task event stream contains an illegal transition")
                if event.occurred_at < replay_time:
                    raise TaskStoreError("task event stream time moves backwards")
                replay_state = event.to_state
                replay_time = event.occurred_at
                if event.evidence_revision is not None:
                    replay_evidence = event.evidence_revision
            if (
                replay_state is not task.state
                or replay_evidence != task.evidence_revision
                or int(row["latest_event_sequence"]) != len(events)
                or row["updated_utc"] != utc_text(events[-1].occurred_at)
            ):
                raise TaskStoreError("task snapshot disagrees with its event stream")
            plans = self.db.execute(
                "SELECT * FROM tc_task_plans WHERE task_id=? ORDER BY version", (task.task_id,)
            ).fetchall()
            for expected, plan_row in enumerate(plans, 1):
                plan = self._stored_contract(plan_row, "plan_json", "plan_hash", Plan)
                verified += 1
                if (
                    plan.version != expected
                    or plan.task_id != task.task_id
                    or plan_row["task_id"] != plan.task_id
                    or plan_row["version"] != plan.version
                    or plan_row["plan_id"] != plan.plan_id
                    or plan_row["created_utc"] != utc_text(plan.created_at)
                ):
                    raise TaskStoreError("task plan history is not contiguous")
                matching_events = [
                    event for event in events
                    if event.event_type == "plan-added"
                    and event.payload.get("plan_id") == plan.plan_id
                ]
                expected_payload = {
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "plan_hash": plan.content_hash,
                    "evidence_revision": plan.evidence_revision,
                }
                if (
                    len(matching_events) != 1
                    or dict(matching_events[0].payload) != expected_payload
                    or matching_events[0].occurred_at != plan.created_at
                    or matching_events[0].evidence_revision != plan.evidence_revision
                ):
                    raise TaskStoreError("plan has no matching durable audit event")
            expected_current = None if not plans else len(plans)
            if task.current_plan_version != expected_current:
                raise TaskStoreError("task snapshot disagrees with current plan version")
            if task.state not in TERMINAL_STATES:
                active_tasks.append(task.task_id)
        for row in self.db.execute(
            "SELECT * FROM tc_task_operator_inputs ORDER BY recorded_utc,input_id"
        ):
            record = self._stored_contract(
                row, "input_json", "input_hash", OperatorInputRecord
            )
            provenance = record.provenance
            if any((
                row["input_id"] != record.input_id,
                row["transport"] != provenance["transport"],
                row["namespace"] != provenance["namespace"],
                row["group_id"] != provenance["group_id"],
                row["update_id"] != provenance["update_id"],
                row["input_digest"] != record.input_digest,
                row["disposition"] != record.disposition,
                row["task_id"] != record.task_id,
                row["recorded_utc"] != utc_text(record.recorded_at),
            )):
                raise TaskStoreError(
                    "operator input indexed columns disagree with its contract"
                )
            verified += 1
        for row in self.db.execute("SELECT * FROM tc_task_approvals"):
            grant = self._stored_contract(row, "grant_json", "grant_hash", ApprovalGrant)
            if (
                row["grant_id"] != grant.grant_id or row["task_id"] != grant.task_id
                or row["plan_id"] != grant.plan_id or row["plan_hash"] != grant.plan_hash
                or row["nonce"] != grant.nonce or row["issued_utc"] != utc_text(grant.issued_at)
            ):
                raise TaskStoreError("approval indexed columns disagree with its contract")
            plan_row, plan = self._plan_row(grant.task_id, grant.plan_id)
            if (
                plan_row["plan_hash"] != grant.plan_hash
                or grant.plan_hash != plan.content_hash
                or not set(grant.step_ids).issubset({step.step_id for step in plan.steps})
                or grant.evidence_revision != plan.evidence_revision
                or grant.issued_at < plan.created_at
                or grant.expires_at > plan.expires_at
            ):
                raise TaskStoreError("approval plan binding is corrupt")
            verified += 1
        lease_intervals: dict[tuple[str, str, str], list[tuple[datetime, datetime]]] = {}
        for row in self.db.execute("SELECT * FROM tc_task_leases"):
            lease = self._stored_contract(row, "lease_json", "lease_hash", ExecutionLease)
            if any((
                row["lease_id"] != lease.lease_id,
                row["task_id"] != lease.task_id,
                row["plan_id"] != lease.plan_id,
                row["plan_hash"] != lease.plan_hash,
                row["step_id"] != lease.step_id,
                row["grant_id"] != lease.grant_id,
                row["grant_hash"] != lease.grant_hash,
                row["policy_revision"] != lease.policy_revision,
                row["evidence_revision"] != lease.evidence_revision,
                row["holder"] != lease.holder,
                row["idempotency_key"] != lease.idempotency_key,
                row["acquired_utc"] != utc_text(lease.acquired_at),
                row["expires_utc"] != utc_text(lease.expires_at),
            )):
                raise TaskStoreError("lease indexed columns disagree with its contract")
            grant_row = self.db.execute(
                "SELECT * FROM tc_task_approvals WHERE grant_id=?", (lease.grant_id,)
            ).fetchone()
            if grant_row is None:
                raise TaskStoreError("lease approval binding is orphaned")
            grant = self._stored_contract(grant_row, "grant_json", "grant_hash", ApprovalGrant)
            _plan_row, plan = self._plan_row(lease.task_id, lease.plan_id)
            if (
                lease.grant_hash != grant.content_hash or lease.task_id != grant.task_id
                or lease.plan_id != grant.plan_id or lease.plan_hash != grant.plan_hash
                or lease.step_id not in grant.step_ids
                or lease.policy_revision != grant.policy_revision
                or lease.evidence_revision != grant.evidence_revision
                or lease.plan_hash != plan.content_hash
                or lease.step_id not in {step.step_id for step in plan.steps}
                or lease.acquired_at < grant.issued_at
                or lease.expires_at > grant.expires_at
                or lease.expires_at > plan.expires_at
            ):
                raise TaskStoreError("lease approval binding is corrupt")
            step = next(step for step in plan.steps if step.step_id == lease.step_id)
            if lease.expires_at > lease.acquired_at + timedelta(seconds=step.max_execution_seconds):
                raise TaskStoreError("lease duration exceeds its plan step")
            lease_intervals.setdefault(
                (lease.task_id, lease.plan_id, lease.step_id), []
            ).append((lease.acquired_at, lease.expires_at))
            verified += 1
        for intervals in lease_intervals.values():
            intervals.sort()
            if any(current[0] < prior[1] for prior, current in zip(intervals, intervals[1:])):
                raise TaskStoreError("stored execution leases overlap")
        for row in self.db.execute("SELECT * FROM tc_task_verifications"):
            verification = self._stored_contract(
                row, "verification_json", "verification_hash", Verification
            )
            if any((
                row["verification_id"] != verification.verification_id,
                row["task_id"] != verification.task_id,
                row["plan_id"] != verification.plan_id,
                row["plan_hash"] != verification.plan_hash,
                row["step_id"] != verification.step_id,
                row["lease_id"] != verification.lease_id,
                row["lease_hash"] != verification.lease_hash,
                row["status"] != verification.status.value,
                row["performed_utc"] != utc_text(verification.performed_at),
            )):
                raise TaskStoreError("verification indexed columns disagree with its contract")
            if verification.lease_id is not None:
                lease_row = self.db.execute(
                    "SELECT * FROM tc_task_leases WHERE lease_id=?", (verification.lease_id,)
                ).fetchone()
                if lease_row is None:
                    raise TaskStoreError("verification lease binding is orphaned")
                lease = self._stored_contract(
                    lease_row, "lease_json", "lease_hash", ExecutionLease
                )
                if (
                    verification.lease_hash != lease.content_hash
                    or verification.task_id != lease.task_id
                    or verification.plan_id != lease.plan_id
                    or verification.plan_hash != lease.plan_hash
                    or verification.step_id != lease.step_id
                ):
                    raise TaskStoreError("verification lease binding is corrupt")
            _plan_row, plan = self._plan_row(verification.task_id, verification.plan_id)
            if (
                verification.plan_hash != plan.content_hash
                or (
                    verification.step_id is not None
                    and verification.step_id not in {step.step_id for step in plan.steps}
                )
            ):
                raise TaskStoreError("verification plan binding is corrupt")
            verified += 1
        now = self._now()
        active_leases = tuple(sorted(
            row["lease_id"] for row in self.db.execute(
                "SELECT lease_id,expires_utc FROM tc_task_leases"
            ) if parse_utc(row["expires_utc"], "lease expires_utc") > now
        ))
        return RecoveryReport(tuple(active_tasks), active_leases, verified)


__all__ = [
    "IllegalTransition", "LEGAL_TRANSITIONS", "OperatorInputRecord", "RecoveryReport",
    "Task", "TaskConflict", "TaskEvent", "TaskState", "TaskStore", "TaskStoreError",
    "TERMINAL_STATES",
]
