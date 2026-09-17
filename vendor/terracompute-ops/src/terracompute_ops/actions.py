"""Durable, crash-safe broker for exact human-approved actions.

The broker has no network or command implementation.  Commissioning supplies a
fixed action adapter, a membership verifier, and an already-owned SQLite
connection.  Adapters are addressed only with typed proposals and opaque
execution IDs, which makes an arbitrary shell impossible through this interface.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from .policy import (
    MACHINE_ID,
    POWER_ACTIONS,
    POWER_COOLDOWN,
    POWER_WINDOW,
    POWER_WINDOW_LIMIT,
    REPEAT_COOLDOWN,
    ActionClass,
    ActionPolicy,
    ActionProposal,
    Ownership,
    PolicyDenied,
    PreActionEvidence,
    RentalImpact,
    SourceBinding,
    canonical_json,
    parse_utc,
    require_utc,
    utc_now,
    utc_text,
)


_SENSITIVE_DETAIL = re.compile(
    r"(?i)(\b(?:authorization|credential|password|secret|token)\b\s*[:=]\s*)\S+|"
    r"\b(?:bearer|basic)\s+\S+|https://api\.telegram\.org/bot[^/\s]+"
)
ACTION_SCHEMA_VERSION = 1


def _safe_detail(value: str) -> str:
    return _SENSITIVE_DETAIL.sub(r"\1[REDACTED]", value)[:512]


class ApprovalKind(str, Enum):
    APPROVE = "approve"
    ACKNOWLEDGE = "acknowledge"
    BACKUP_EXCEPTION = "backup-exception"


@dataclass(frozen=True)
class HumanApprovalEvent:
    """A bounded event from the trusted, authenticated approval ingress."""

    event_id: str
    kind: ApprovalKind
    group_id: int
    user_id: int | None
    display_name: str
    proposal_id: str
    proposal_digest: str
    nonce: str
    occurred_at: datetime
    sender_is_bot: bool = False
    sender_is_anonymous: bool = False
    chat_migrated: bool = False

    def __post_init__(self) -> None:
        require_utc(self.occurred_at, "occurred_at")
        if not self.event_id or not self.proposal_id or not self.proposal_digest:
            raise ValueError("event, proposal, and digest are required")
        if not self.nonce or len(self.nonce) > 256:
            raise ValueError("a bounded nonce is required")
        if len(self.display_name) > 256:
            raise ValueError("display_name is too long")


@dataclass(frozen=True)
class EventAuthentication:
    """Identity returned by the trusted ingress authenticator."""

    event_id: str
    group_id: int
    user_id: int | None
    authenticated: bool


@runtime_checkable
class ApprovalAuthenticator(Protocol):
    """Authenticates event origin and sender independently of membership."""

    def authenticate(self, event: HumanApprovalEvent) -> EventAuthentication: ...


class DenyAllApprovalAuthenticator:
    """Default trust boundary: raw/model-created values have no authority."""

    def authenticate(self, event: HumanApprovalEvent) -> EventAuthentication:
        return EventAuthentication(event.event_id, event.group_id, event.user_id, False)


@dataclass(frozen=True)
class MembershipDecision:
    group_id: int
    user_id: int
    current_member: bool
    human: bool
    independently_verified: bool
    verified_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.verified_at, "verified_at")


@runtime_checkable
class MembershipVerifier(Protocol):
    """Must perform a current, independent group membership lookup per call."""

    def verify(self, group_id: int, user_id: int) -> MembershipDecision: ...


class DispatchStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DRY_RUN = "dry-run"
    # The adapter proved the action never ran (for example, the target refused before
    # acting). The approval is still consumed and the cooldown still applies.
    REFUSED = "refused"


@dataclass(frozen=True)
class DispatchResult:
    execution_id: str
    status: DispatchStatus
    detail: str = ""


@dataclass(frozen=True)
class PostActionEvidence:
    execution_id: str
    evidence_ref: str | None
    postcondition_ok: bool
    detail: str = ""


@runtime_checkable
class ActionAdapter(Protocol):
    """Fixed action integration. ``reconcile`` must be read-only."""

    def supports(self, action_class: ActionClass) -> bool: ...

    def validate(self, proposal: ActionProposal) -> None: ...

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence: ...

    def dispatch(self, proposal: ActionProposal, execution_id: str) -> DispatchResult: ...

    def postflight(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> PostActionEvidence: ...

    def reconcile(self, proposal: ActionProposal, execution_id: str) -> DispatchResult: ...


class UnconfiguredActionAdapter:
    """Safe default: it never captures, dispatches, or claims success."""

    def supports(self, action_class: ActionClass) -> bool:
        return False

    def validate(self, proposal: ActionProposal) -> None:
        self._fail()

    def _fail(self) -> None:
        raise PolicyDenied("action adapter is not configured")

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence:
        self._fail()

    def dispatch(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        self._fail()

    def postflight(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> PostActionEvidence:
        self._fail()

    def reconcile(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        self._fail()


class DryRunActionAdapter:
    """Non-actuating adapter that explicitly reports ``dry-run``, never success."""

    def __init__(self, preflight: Callable[[ActionProposal], PreActionEvidence]):
        self._preflight = preflight

    def supports(self, action_class: ActionClass) -> bool:
        return True

    def validate(self, proposal: ActionProposal) -> None:
        return None

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence:
        return self._preflight(proposal)

    def dispatch(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        return DispatchResult(execution_id, DispatchStatus.DRY_RUN, "no action performed")

    def postflight(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> PostActionEvidence:
        return PostActionEvidence(execution_id, None, True, "dry-run has no postcondition")

    def reconcile(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        return DispatchResult(execution_id, DispatchStatus.DRY_RUN, "no action performed")


class FakeActionAdapter:
    """Deterministic test adapter with no external effects."""

    def __init__(
        self,
        preflight: Callable[[ActionProposal], PreActionEvidence],
        *,
        dispatch_status: DispatchStatus = DispatchStatus.SUCCEEDED,
        postcondition_ok: bool = True,
        reconciled_status: DispatchStatus = DispatchStatus.UNKNOWN,
        fail_dispatch: BaseException | None = None,
        fail_postflight: BaseException | None = None,
        supported: frozenset[ActionClass] | None = None,
    ):
        self._preflight = preflight
        self.dispatch_status = dispatch_status
        self.postcondition_ok = postcondition_ok
        self.reconciled_status = reconciled_status
        self.fail_dispatch = fail_dispatch
        self.fail_postflight = fail_postflight
        self.supported = supported
        self.dispatches: list[tuple[str, str]] = []
        self.reconciliations: list[str] = []

    def supports(self, action_class: ActionClass) -> bool:
        return self.supported is None or action_class in self.supported

    def validate(self, proposal: ActionProposal) -> None:
        return None

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence:
        return self._preflight(proposal)

    def dispatch(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        self.dispatches.append((proposal.proposal_id, execution_id))
        if self.fail_dispatch is not None:
            raise self.fail_dispatch
        return DispatchResult(execution_id, self.dispatch_status, "fake dispatch")

    def postflight(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> PostActionEvidence:
        if self.fail_postflight is not None:
            raise self.fail_postflight
        return PostActionEvidence(
            execution_id, f"post:{execution_id}", self.postcondition_ok, "fake postflight"
        )

    def reconcile(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        self.reconciliations.append(execution_id)
        return DispatchResult(execution_id, self.reconciled_status, "fake reconciliation")


@dataclass(frozen=True)
class Attempt:
    execution_id: str
    proposal_id: str
    state: str
    started_at: datetime
    dispatched_at: datetime | None
    finished_at: datetime | None
    pre_evidence_ref: str | None
    post_evidence_ref: str | None
    result_detail: str | None


class ActionBroker:
    """SQLite-backed exact-approval broker.

    Construction creates only ``tc_action_*`` tables.  In particular, it never
    reads or writes ``PRAGMA user_version``; the repository state owner controls
    global schema versioning.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        policy: ActionPolicy | None = None,
        membership: MembershipVerifier,
        authenticator: ApprovalAuthenticator | None = None,
        adapter: ActionAdapter | None = None,
        clock: Callable[[], datetime] = utc_now,
        execution_id_factory: Callable[[], str] | None = None,
    ):
        self.db = connection
        self.policy = policy or ActionPolicy()
        self.membership = membership
        self.authenticator = authenticator or DenyAllApprovalAuthenticator()
        self.adapter = adapter or UnconfiguredActionAdapter()
        self.clock = clock
        self.execution_id_factory = execution_id_factory or (lambda: str(uuid.uuid4()))
        self._create_schema()

    def _create_schema(self) -> None:
        schema_exists = self.db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'tc_action_schema'"""
        ).fetchone()
        if schema_exists is not None:
            existing = self.db.execute(
                "SELECT version FROM tc_action_schema WHERE namespace = 'actions'"
            ).fetchone()
            if existing is None or existing[0] != ACTION_SCHEMA_VERSION:
                raise RuntimeError("incompatible action table schema version")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tc_action_schema (
              namespace TEXT PRIMARY KEY CHECK(namespace = 'actions'),
              version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tc_action_proposals (
              proposal_id TEXT PRIMARY KEY,
              digest TEXT NOT NULL UNIQUE,
              document_json TEXT NOT NULL,
              created_utc TEXT NOT NULL,
              expires_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tc_action_nonces (
              nonce TEXT PRIMARY KEY,
              event_id TEXT NOT NULL UNIQUE,
              purpose TEXT NOT NULL,
              proposal_id TEXT NOT NULL,
              recorded_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tc_action_approvals (
              proposal_id TEXT PRIMARY KEY,
              nonce TEXT NOT NULL UNIQUE,
              user_id INTEGER NOT NULL,
              group_id INTEGER NOT NULL,
              display_name TEXT NOT NULL,
              approved_utc TEXT NOT NULL,
              verified_utc TEXT NOT NULL,
              consumed_execution_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS tc_action_backup_exceptions (
              proposal_id TEXT PRIMARY KEY,
              nonce TEXT NOT NULL UNIQUE,
              user_id INTEGER NOT NULL,
              group_id INTEGER NOT NULL,
              display_name TEXT NOT NULL,
              approved_utc TEXT NOT NULL,
              verified_utc TEXT NOT NULL,
              consumed_execution_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS tc_action_attempts (
              execution_id TEXT PRIMARY KEY,
              proposal_id TEXT NOT NULL UNIQUE,
              approval_nonce TEXT NOT NULL UNIQUE,
              action_class TEXT NOT NULL,
              resource_ids_json TEXT NOT NULL,
              started_utc TEXT NOT NULL,
              dispatched_utc TEXT,
              finished_utc TEXT,
              state TEXT NOT NULL,
              pre_evidence_ref TEXT NOT NULL,
              backup_ref TEXT,
              post_evidence_ref TEXT,
              result_detail TEXT
            );
            CREATE TABLE IF NOT EXISTS tc_action_locks (
              domain TEXT PRIMARY KEY,
              execution_id TEXT NOT NULL,
              acquired_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tc_action_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_utc TEXT NOT NULL,
              event TEXT NOT NULL,
              proposal_id TEXT,
              execution_id TEXT,
              detail TEXT NOT NULL
            );
            INSERT OR IGNORE INTO tc_action_schema(namespace, version)
              VALUES ('actions', 1);
            """
        )
        version = self.db.execute(
            "SELECT version FROM tc_action_schema WHERE namespace = 'actions'"
        ).fetchone()
        if version is None or version[0] != ACTION_SCHEMA_VERSION:
            raise RuntimeError("incompatible action table schema version")
        self.db.commit()

    def _now(self) -> datetime:
        return require_utc(self.clock(), "clock")

    def _audit(
        self, event: str, detail: str, proposal_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO tc_action_audit
               (recorded_utc, event, proposal_id, execution_id, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (utc_text(self._now()), event, proposal_id, execution_id, _safe_detail(detail)),
        )
        self.db.commit()

    def submit_proposal(self, proposal: ActionProposal) -> str:
        """Persist a proposal without granting eligibility or authority."""
        try:
            self.db.execute(
                """INSERT INTO tc_action_proposals
                   (proposal_id, digest, document_json, created_utc, expires_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    proposal.proposal_id,
                    proposal.digest,
                    canonical_json(proposal.exact_document()),
                    utc_text(proposal.created_at),
                    utc_text(proposal.expires_at),
                ),
            )
            self.db.execute(
                """INSERT INTO tc_action_audit
                   (recorded_utc, event, proposal_id, detail) VALUES (?, ?, ?, ?)""",
                (utc_text(self._now()), "proposal-recorded", proposal.proposal_id, proposal.digest),
            )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise PolicyDenied("proposal identifier or exact document was already used") from error
        return proposal.digest

    def find_proposal(self, proposal_id: str) -> ActionProposal | None:
        """The stored proposal for this identifier, or None when it was never submitted."""
        try:
            return self._proposal(proposal_id)
        except PolicyDenied:
            return None

    def _proposal(self, proposal_id: str) -> ActionProposal:
        row = self.db.execute(
            "SELECT digest, document_json FROM tc_action_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise PolicyDenied("unknown proposal")
        value = json.loads(row[1])
        proposal = ActionProposal(
            proposal_id=value["proposal_id"],
            action_class=ActionClass(value["action_class"]),
            parameters=value["parameters"],
            resource_ids=tuple(value["resource_ids"]),
            rental_impacts=tuple(
                RentalImpact(
                    item["rental_id"], item["active"], Ownership(item["ownership"]),
                    item["interruption_approved"],
                )
                for item in value["rental_impacts"]
            ),
            affected_domains=tuple(value["affected_domains"]),
            source_bindings=tuple(
                SourceBinding(item["source"], item["revision"])
                for item in value["source_bindings"]
            ),
            evidence_revision=value["evidence_revision"],
            policy_revision=value["policy_revision"],
            stop_condition=value["stop_condition"],
            created_at=parse_utc(value["created_at"]),
            expires_at=parse_utc(value["expires_at"]),
            machine_id=value["machine_id"],
            mappings_known=value["mappings_known"],
            power_domain_proven=value["power_domain_proven"],
            claims_cold_gpu_power=value["claims_cold_gpu_power"],
        )
        if proposal.digest != row[0]:
            raise PolicyDenied("stored proposal digest does not match its exact document")
        return proposal

    def _verify_event_identity(self, event: HumanApprovalEvent) -> MembershipDecision:
        authentication = self.authenticator.authenticate(event)
        if (
            not authentication.authenticated
            or authentication.event_id != event.event_id
            or authentication.group_id != event.group_id
            or authentication.user_id != event.user_id
        ):
            raise PolicyDenied("approval ingress did not authenticate the exact sender event")
        if event.kind is ApprovalKind.ACKNOWLEDGE:
            raise PolicyDenied("acknowledgment is not approval")
        if event.group_id != self.policy.approval_group_id:
            raise PolicyDenied("approval came from the wrong group")
        if event.chat_migrated:
            raise PolicyDenied("migrated chat events cannot approve actions")
        if event.user_id is None or event.sender_is_anonymous or event.sender_is_bot:
            raise PolicyDenied("approval requires an identifiable human sender")
        decision = self.membership.verify(event.group_id, event.user_id)
        verified_age = self._now() - decision.verified_at
        if (
            decision.group_id != event.group_id
            or decision.user_id != event.user_id
            or not decision.current_member
            or not decision.human
            or not decision.independently_verified
            or verified_age < timedelta(0)
            or verified_age > self.policy.max_source_age
        ):
            raise PolicyDenied("current human group membership was not verified")
        return decision

    def record_human_event(self, event: HumanApprovalEvent) -> None:
        """Record one exact approval or separately audited backup exception."""
        proposal = self._proposal(event.proposal_id)
        now = self._now()
        self.policy.validate_proposal(proposal, now)
        if event.proposal_digest != proposal.digest:
            raise PolicyDenied("approval does not bind the exact proposal")
        if event.occurred_at < proposal.created_at or event.occurred_at > proposal.expires_at:
            raise PolicyDenied("approval event is outside the proposal lifetime")
        if event.occurred_at > now:
            raise PolicyDenied("approval event timestamp is in the future")
        decision = self._verify_event_identity(event)
        table = (
            "tc_action_approvals"
            if event.kind is ApprovalKind.APPROVE
            else "tc_action_backup_exceptions"
        )
        if event.kind not in {ApprovalKind.APPROVE, ApprovalKind.BACKUP_EXCEPTION}:
            raise PolicyDenied("event kind cannot authorize an action")
        now_text = utc_text(self._now())
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT INTO tc_action_nonces
                   (nonce, event_id, purpose, proposal_id, recorded_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (event.nonce, event.event_id, event.kind.value, event.proposal_id, now_text),
            )
            self.db.execute(
                f"""INSERT INTO {table}
                    (proposal_id, nonce, user_id, group_id, display_name,
                     approved_utc, verified_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.proposal_id, event.nonce, event.user_id, event.group_id,
                    event.display_name, utc_text(event.occurred_at),
                    utc_text(decision.verified_at),
                ),
            )
            self.db.execute(
                """INSERT INTO tc_action_audit
                   (recorded_utc, event, proposal_id, detail) VALUES (?, ?, ?, ?)""",
                (now_text, f"human-{event.kind.value}", event.proposal_id,
                 f"group={event.group_id};user={event.user_id}"),
            )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise PolicyDenied("nonce, event, or proposal approval was already used") from error
        except BaseException:
            # A busy or failed write must not leave BEGIN IMMEDIATE open on the connection.
            self.db.rollback()
            raise

    def _approval(
        self, proposal_id: str, table: str = "tc_action_approvals"
    ) -> tuple[Any, ...] | None:
        return self.db.execute(
            f"""SELECT nonce, user_id, group_id, consumed_execution_id
                FROM {table} WHERE proposal_id = ?""",
            (proposal_id,),
        ).fetchone()

    def _verify_execution_member(self, approval: tuple[Any, ...]) -> None:
        _nonce, user_id, group_id, _consumed = approval
        decision = self.membership.verify(group_id, user_id)
        verified_age = self._now() - decision.verified_at
        if (
            decision.group_id != group_id
            or decision.user_id != user_id
            or not decision.current_member
            or not decision.human
            or not decision.independently_verified
            or verified_age < timedelta(0)
            or verified_age > self.policy.max_source_age
        ):
            raise PolicyDenied("approver is not a currently verified human member")

    def _lock_domains(self, proposal: ActionProposal) -> tuple[str, ...]:
        domains = {f"machine:{MACHINE_ID}", *proposal.affected_domains}
        if proposal.action_class in POWER_ACTIONS:
            domains.add(f"global-power:{MACHINE_ID}")
        return tuple(sorted(domains))

    def _check_limits(
        self, proposal: ActionProposal, now: datetime, operator_override: str | None = None
    ) -> None:
        # A named person asking for it again is authority, so it lifts the repeat
        # cooldown. The power ceilings below are not lifted by anyone.
        repeat_after = utc_text(now - (timedelta(0) if operator_override else REPEAT_COOLDOWN))
        rows = self.db.execute(
            """SELECT action_class, resource_ids_json FROM tc_action_attempts
               WHERE started_utc > ?""",
            (repeat_after,),
        ).fetchall()
        resources = set(proposal.resource_ids)
        for action_class, resource_json in rows:
            repeated_resource = resources.intersection(json.loads(resource_json))
            if action_class == proposal.action_class.value and repeated_resource:
                raise PolicyDenied("repeated action class/resource is in its 30-minute cooldown")
        if proposal.action_class in POWER_ACTIONS:
            power_after = utc_text(now - POWER_COOLDOWN)
            recent_power = self.db.execute(
                """SELECT 1 FROM tc_action_attempts
                   WHERE action_class IN (?, ?) AND started_utc > ? LIMIT 1""",
                (ActionClass.HOST_REBOOT.value, ActionClass.BMC_POWER.value, power_after),
            ).fetchone()
            if recent_power is not None:
                raise PolicyDenied("reboot/power action is in its 60-minute cooldown")
            window_after = utc_text(now - POWER_WINDOW)
            count = self.db.execute(
                """SELECT COUNT(*) FROM tc_action_attempts
                   WHERE action_class IN (?, ?) AND started_utc > ?""",
                (ActionClass.HOST_REBOOT.value, ActionClass.BMC_POWER.value, window_after),
            ).fetchone()[0]
            if count >= POWER_WINDOW_LIMIT:
                raise PolicyDenied("combined reboot/power limit of two per 24 hours reached")

    def execute(self, proposal_id: str, *, operator_override: str | None = None) -> Attempt:
        """Consume approval, lock, dispatch once, and verify postconditions.

        ``operator_override`` names the person who asked for this despite the repeat
        cooldown. It lifts that limit and nothing else, and it is written to the audit.
        """
        proposal = self._proposal(proposal_id)
        now = self._now()
        self.policy.validate_proposal(proposal, now)
        if not self.adapter.supports(proposal.action_class):
            raise PolicyDenied("action class has no configured fixed adapter")
        self.adapter.validate(proposal)
        # A self-service class is carried out on the controller's own authority. Every
        # other gate stays: adapter validation, fresh evidence, locks, limits, one
        # dispatch, and verification afterwards.
        self_service = self.policy.self_service(proposal.action_class)
        approval = self._approval(proposal_id)
        if approval is None and not self_service:
            raise PolicyDenied("proposal has no exact human approval")
        if approval is not None and approval[3] is not None:
            raise PolicyDenied("approval was already consumed")
        exception = self._approval(proposal_id, "tc_action_backup_exceptions")
        if exception is not None:
            if exception[3] is not None:
                raise PolicyDenied("backup exception was already consumed")
        preflight = self.adapter.preflight(proposal)
        # These are fresh independent lookups after evidence collection, directly
        # before the atomic reservation and adapter boundary.
        if approval is not None:
            self._verify_execution_member(approval)
        if exception is not None:
            self._verify_execution_member(exception)
        self.policy.validate_preconditions(
            proposal, preflight, self._now(), backup_exception=exception is not None
        )

        execution_id = self.execution_id_factory()
        if not execution_id:
            raise RuntimeError("execution ID factory returned an empty ID")
        started = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._approval(proposal_id)
            if current is None and not self_service:
                raise PolicyDenied("approval was concurrently consumed")
            if current is not None and current[3] is not None:
                raise PolicyDenied("approval was concurrently consumed")
            # Without an approval the attempt still needs its own unique mark.
            nonce = current[0] if current is not None else f"self-service:{execution_id}"
            self.policy.validate_proposal(proposal, started)
            self._check_limits(proposal, started, operator_override)
            for domain in self._lock_domains(proposal):
                self.db.execute(
                    """INSERT INTO tc_action_locks(domain, execution_id, acquired_utc)
                       VALUES (?, ?, ?)""",
                    (domain, execution_id, utc_text(started)),
                )
            self.db.execute(
                """INSERT INTO tc_action_attempts
                   (execution_id, proposal_id, approval_nonce, action_class,
                    resource_ids_json, started_utc, state, pre_evidence_ref, backup_ref)
                   VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (
                    execution_id, proposal_id, nonce, proposal.action_class.value,
                    canonical_json(list(proposal.resource_ids)), utc_text(started),
                    preflight.evidence_ref, preflight.backup_ref,
                ),
            )
            if current is not None:
                self.db.execute(
                    """UPDATE tc_action_approvals SET consumed_execution_id = ?
                       WHERE proposal_id = ? AND consumed_execution_id IS NULL""",
                    (execution_id, proposal_id),
                )
                if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PolicyDenied("approval was concurrently consumed")
            if exception is not None:
                self.db.execute(
                    """UPDATE tc_action_backup_exceptions SET consumed_execution_id = ?
                       WHERE proposal_id = ? AND consumed_execution_id IS NULL""",
                    (execution_id, proposal_id),
                )
                if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PolicyDenied("backup exception was concurrently consumed")
            self.db.execute(
                """INSERT INTO tc_action_audit
                   (recorded_utc, event, proposal_id, execution_id, detail)
                   VALUES (?, 'attempt-reserved', ?, ?, ?)""",
                (
                    utc_text(started), proposal_id, execution_id,
                    "locks acquired; "
                    + ("self-service, no human approval" if current is None else "approval consumed")
                    + (f"; cooldown lifted by {operator_override}" if operator_override else ""),
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise PolicyDenied("machine or affected domain is locked") from error
        except sqlite3.OperationalError as error:
            self.db.rollback()
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise PolicyDenied("machine action reservation is concurrently locked") from error
            raise
        except Exception:
            self.db.rollback()
            raise

        # Persist the dispatch boundary before crossing into the adapter.  A crash
        # from here onward is reconciled read-only and is never replayed.
        dispatched = self._now()
        self.db.execute(
            """UPDATE tc_action_attempts SET state = 'dispatching', dispatched_utc = ?
               WHERE execution_id = ? AND state = 'reserved'""",
            (utc_text(dispatched), execution_id),
        )
        self.db.commit()
        try:
            result = self.adapter.dispatch(proposal, execution_id)
            self._check_execution_id(execution_id, result.execution_id)
        except Exception as error:
            self._mark_unknown(execution_id, f"dispatch raised {type(error).__name__}")
            return self.get_attempt(execution_id)
        if result.status is DispatchStatus.UNKNOWN:
            self._mark_unknown(execution_id, result.detail or "adapter result unknown")
            return self.get_attempt(execution_id)
        return self._finish_known(proposal, execution_id, result)

    @staticmethod
    def _check_execution_id(expected: str, actual: str) -> None:
        if actual != expected:
            raise RuntimeError("adapter returned a mismatched execution ID")

    def _mark_unknown(self, execution_id: str, detail: str) -> None:
        self.db.execute(
            """UPDATE tc_action_attempts
               SET state = 'unknown', result_detail = ?
               WHERE execution_id = ? AND state IN ('reserved', 'dispatching', 'unknown')""",
            (_safe_detail(detail), execution_id),
        )
        self.db.execute(
            """INSERT INTO tc_action_audit
               (recorded_utc, event, execution_id, detail)
               VALUES (?, 'result-unknown', ?, ?)""",
            (utc_text(self._now()), execution_id, _safe_detail(detail)),
        )
        self.db.commit()

    def _postflight_state(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> tuple[str, PostActionEvidence]:
        try:
            post = self.adapter.postflight(proposal, execution_id, result)
            self._check_execution_id(execution_id, post.execution_id)
        except Exception as error:
            return "postcondition-failed", PostActionEvidence(
                execution_id, None, False, f"postflight raised {type(error).__name__}"
            )
        if result.status is DispatchStatus.DRY_RUN:
            return "dry-run", post
        if not post.evidence_ref:
            return "postcondition-failed", post
        if result.status is DispatchStatus.FAILED:
            return "failed", post
        if result.status is DispatchStatus.SUCCEEDED and post.postcondition_ok:
            return "succeeded", post
        return "postcondition-failed", post

    def _finish_known(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> Attempt:
        if result.status is DispatchStatus.REFUSED:
            # Nothing ran, so there is no postcondition to verify.
            state = "refused"
            post = PostActionEvidence(execution_id, None, False, "")
        else:
            state, post = self._postflight_state(proposal, execution_id, result)
        detail = _safe_detail("; ".join(part for part in (result.detail, post.detail) if part))
        finished = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """UPDATE tc_action_attempts
                   SET state = ?, finished_utc = ?, post_evidence_ref = ?, result_detail = ?
                   WHERE execution_id = ? AND state IN ('reserved', 'dispatching', 'unknown')""",
                (state, utc_text(finished), post.evidence_ref, detail, execution_id),
            )
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise PolicyDenied("attempt is already in a terminal state")
            self.db.execute("DELETE FROM tc_action_locks WHERE execution_id = ?", (execution_id,))
            self.db.execute(
                """INSERT INTO tc_action_audit
                   (recorded_utc, event, proposal_id, execution_id, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (utc_text(finished), f"result-{state}", proposal.proposal_id, execution_id, detail),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_attempt(execution_id)

    def recover_interrupted_attempts(self) -> tuple[str, ...]:
        """On startup, make dispatch-boundary attempts unknown and retain locks."""
        rows = self.db.execute(
            """SELECT execution_id FROM tc_action_attempts
               WHERE state IN ('reserved', 'dispatching') ORDER BY started_utc"""
        ).fetchall()
        recovered = tuple(row[0] for row in rows)
        if not recovered:
            return ()
        now = utc_text(self._now())
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for execution_id in recovered:
                self.db.execute(
                    """UPDATE tc_action_attempts SET state = 'unknown',
                       result_detail = 'recovered after interrupted dispatch'
                       WHERE execution_id = ? AND state IN ('reserved', 'dispatching')""",
                    (execution_id,),
                )
                self.db.execute(
                    """INSERT INTO tc_action_audit
                       (recorded_utc, event, execution_id, detail)
                       VALUES (?, 'result-unknown', ?, 'startup recovery; lock retained')""",
                    (now, execution_id),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return recovered

    def reconcile(self, execution_id: str) -> Attempt:
        """Resolve an unknown attempt through the adapter's read-only lookup."""
        attempt = self.get_attempt(execution_id)
        if attempt.state != "unknown":
            raise PolicyDenied("only an unknown attempt may be reconciled")
        proposal = self._proposal(attempt.proposal_id)
        if not self.adapter.supports(proposal.action_class):
            raise PolicyDenied("action class has no configured reconciliation adapter")
        try:
            result = self.adapter.reconcile(proposal, execution_id)
            self._check_execution_id(execution_id, result.execution_id)
        except Exception as error:
            self._mark_unknown(execution_id, f"reconciliation raised {type(error).__name__}")
            return self.get_attempt(execution_id)
        if result.status is DispatchStatus.UNKNOWN:
            self._mark_unknown(execution_id, result.detail or "reconciliation inconclusive")
            return self.get_attempt(execution_id)
        return self._finish_known(proposal, execution_id, result)

    def get_attempt(self, execution_id: str) -> Attempt:
        row = self.db.execute(
            """SELECT execution_id, proposal_id, state, started_utc, dispatched_utc,
                      finished_utc, pre_evidence_ref, post_evidence_ref, result_detail
               FROM tc_action_attempts WHERE execution_id = ?""",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return Attempt(
            execution_id=row[0], proposal_id=row[1], state=row[2],
            started_at=parse_utc(row[3]),
            dispatched_at=parse_utc(row[4]) if row[4] else None,
            finished_at=parse_utc(row[5]) if row[5] else None,
            pre_evidence_ref=row[6], post_evidence_ref=row[7], result_detail=row[8],
        )
