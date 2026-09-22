"""Disabled Phase 4 foundation. No transport, subprocess, or credential implementation.

Trust roots are injected by a controller, never by a plan. Worker attestation strings
are type contracts only, not evidence of isolation or an implemented sandbox. See
``docs/STEP-EXECUTOR.md`` for the wire shape and crash/reconciliation contract.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from .plan_authorization import PlanAuthorizationService, RentalRecord
from .plans import (
    MACHINE_ID, ApprovalGrant, ApprovalKind, ExecutionLease, Plan, PlanStep,
    Verification, VerificationStatus, canonical_json, parse_utc, stable_hash,
    strict_json_loads, utc_text,
)
from .tasks import TaskEvent, TaskState, TaskStore, TaskStoreError

FEATURE_FLAG_ENV = "TERRACOMPUTE_STEP_EXECUTOR"
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ATTEMPTS = 3
WORKER_CONTRACT = (
    "step-worker-v1:external-isolation,single-credential-domain,no-credential-return,"
    "bounded-calls,hard-deadline,output-bounded,no-tenant-payload,"
    "validate-effects-and-purpose,reverify-machine-17049-and-rental-before-effect,"
    "durable-idempotency,fenced-read-only-reconcile,read-only-inspect"
)
WORKSPACE_CONTRACT = (
    WORKER_CONTRACT + ",no-credentials,no-network,task-worktree-only,no-production-effects"
)
VERIFIER_CONTRACT = (
    "step-verifier-v1:independent-read-only,bounded,no-tenant-payload,"
    "exact-request-and-postconditions,authenticated-evidence"
)


class ExecutionBlocked(ValueError):
    """A missing trust root or changed authority requires approval/replanning."""


class Domain(str, Enum):
    TARGET_HOST = "target_host"
    VAST_WRITE = "vast_write"
    BMC = "bmc"
    CONTROLLER_DEPLOYMENT = "controller_deployment"
    WORKSPACE = "workspace"


class Outcome(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied_fenced"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerResult:
    idempotency_key: str
    outcome: Outcome
    # Arbitrary stdout and error text never enter the durable journal.
    output: bytes = b""


@dataclass(frozen=True)
class Inspection:
    machine_id: str
    preconditions_passed: bool
    rentals: tuple[RentalRecord, ...] = ()
    checkpoint_digest: str | None = None


@dataclass(frozen=True)
class ExecutionSpec:
    domain: Domain
    effect_scope: str
    purpose: str
    safe_boundary_after: bool
    max_output_bytes: int

    @classmethod
    def from_step(cls, step: PlanStep) -> ExecutionSpec:
        value = step.arguments.get("execution")
        if not isinstance(value, Mapping) or set(value) != {
            "domain", "effect_scope", "purpose", "safe_boundary_after", "max_output_bytes"
        }:
            raise ExecutionBlocked("missing typed execution envelope; replan")
        try:
            domain = Domain(value["domain"])
        except (ValueError, TypeError):
            raise ExecutionBlocked("unknown credential domain; replan") from None
        scope = value["effect_scope"]
        required = {
            Domain.TARGET_HOST: {"host": "host", "owned_component": "owned_component", "tenant": "tenant"},
            Domain.VAST_WRITE: {"vast": "external_commitment", "tenant": "tenant"},
            Domain.BMC: {"bmc": "reachability"},
            Domain.CONTROLLER_DEPLOYMENT: {"deployment": "host"},
            Domain.WORKSPACE: {"workspace": None},
        }[domain]
        if not isinstance(scope, str) or scope not in required:
            raise ExecutionBlocked("unknown domain/effect binding; replan")
        flag = required[scope]
        if flag and not any(getattr(effect, flag) for effect in step.effects):
            raise ExecutionBlocked("domain effect is undeclared; replan for approval")
        purpose = value["purpose"]
        bound = value["max_output_bytes"]
        safe = value["safe_boundary_after"]
        if not isinstance(purpose, str) or not purpose.strip():
            raise ExecutionBlocked("an exact approved purpose is required")
        if type(safe) is not bool or type(bound) is not int or not 1 <= bound <= MAX_OUTPUT_BYTES:
            raise ExecutionBlocked("invalid execution bounds or cancellation boundary")
        return cls(domain, scope, purpose, safe, bound)


@dataclass(frozen=True)
class StepRequest:
    """Only one domain's exact step, never other workers or raw credentials."""
    plan_hash: str
    step: PlanStep
    phase: str
    lease: ExecutionLease
    spec: ExecutionSpec
    rentals: tuple[RentalRecord, ...]
    checkpoint_digest: str | None
    result_at: datetime | None = None

    @property
    def idempotency_key(self) -> str:
        return self.lease.idempotency_key


class DomainWorker(Protocol):
    contract: str
    domain: Domain
    credential_domains: frozenset[Domain]

    def inspect(self, request: StepRequest) -> Inspection:
        """Bounded read-only identity/precondition/checkpoint collection."""
        ...

    def dispatch(self, request: StepRequest) -> WorkerResult:
        """Enforce deadline, exact effects, identity and idempotency at effect time."""
        ...

    def reconcile(self, request: StepRequest) -> WorkerResult:
        """Read-only. NOT_APPLIED proves this key cannot cause a future effect."""
        ...


class IndependentVerifier(Protocol):
    contract: str

    def validate(self, request: StepRequest, verification: Verification) -> bool: ...


class StepExecutor:
    def __init__(
        self, store: TaskStore, authorization: PlanAuthorizationService,
        workers: Mapping[Domain, DomainWorker], verifier: IndependentVerifier, *,
        mutation_allowed: Callable[[], bool],
        rollback_allowed: Callable[[], bool] | None = None,
        verification_allowed: Callable[[], bool] | None = None,
    ):
        if store.db is not authorization.db:
            raise ExecutionBlocked("authorization and execution require one transactional database")
        if getattr(verifier, "contract", None) != VERIFIER_CONTRACT:
            raise ExecutionBlocked("an independent verifier with the expected type contract is required")
        self.store, self.authorization = store, authorization
        self.workers = MappingProxyType(dict(workers))
        self.verifier, self.mutation_allowed = verifier, mutation_allowed
        # General mutation authority is never bypassed. Recovery can exempt only
        # the separately injected verification breaker, never pause/other policy.
        self.verification_allowed = verification_allowed or (lambda: True)
        self.rollback_allowed = rollback_allowed or (lambda: False)
        if len({id(worker) for worker in workers.values()}) != len(workers):
            raise ExecutionBlocked("a worker cannot serve multiple domains")
        for domain, worker in workers.items():
            self._check_worker(domain, worker)
        with store.transaction():
            store.db.execute("""CREATE TABLE IF NOT EXISTS tc_step_runs (
                task_id TEXT PRIMARY KEY REFERENCES tc_tasks(task_id),
                document BLOB NOT NULL, digest TEXT NOT NULL)""")
            store.db.execute("""CREATE TABLE IF NOT EXISTS tc_step_run_history (
                task_id TEXT NOT NULL REFERENCES tc_tasks(task_id), plan_hash TEXT NOT NULL,
                document BLOB NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(task_id, plan_hash))""")

    @staticmethod
    def _check_worker(domain: Domain, worker: DomainWorker) -> None:
        expected = frozenset() if domain is Domain.WORKSPACE else frozenset({domain})
        contract = WORKSPACE_CONTRACT if domain is Domain.WORKSPACE else WORKER_CONTRACT
        if (
            not isinstance(domain, Domain) or getattr(worker, "domain", None) is not domain
            or getattr(worker, "credential_domains", None) != expected
            or getattr(worker, "contract", None) != contract
        ):
            raise ExecutionBlocked("worker domain/type contract mismatch")

    def _worker(self, spec: ExecutionSpec) -> DomainWorker:
        worker = self.workers.get(spec.domain)
        if worker is None:
            raise ExecutionBlocked("no worker with the expected type contract for the declared domain")
        self._check_worker(spec.domain, worker)
        return worker

    def _load(self, task_id: str) -> dict:
        row = self.store.db.execute("SELECT * FROM tc_step_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ExecutionBlocked("execution is not registered")
        try:
            doc = strict_json_loads(row["document"])
            intact = (stable_hash(doc) == row["digest"] and doc["schema_version"] == 2
                      and doc["task_id"] == task_id)
        except (KeyError, TypeError, ValueError):
            intact = False
        if not intact:
            raise ExecutionBlocked("execution journal integrity failure")
        return doc

    def snapshot(self, task_id: str, plan_hash: str | None = None) -> dict:
        """A detached snapshot; mutating it cannot mutate durable execution."""
        if plan_hash is None:
            return self._load(task_id)
        row = self.store.db.execute(
            "SELECT * FROM tc_step_run_history WHERE task_id=? AND plan_hash=?",
            (task_id, plan_hash),
        ).fetchone()
        doc = self._load(task_id) if row is None else self._history_document(row)
        if doc["task_id"] != task_id or doc["plan_hash"] != plan_hash:
            raise ExecutionBlocked("execution plan identity mismatch")
        return doc

    @staticmethod
    def _history_document(row) -> dict:
        doc = strict_json_loads(row["document"])
        if stable_hash(doc) != row["digest"] or doc["task_id"] != row["task_id"]:
            raise ExecutionBlocked("execution journal integrity failure")
        return doc

    def _outermost(self) -> None:
        if self.store.db.in_transaction:
            raise ExecutionBlocked("executor entrypoint requires no ambient transaction")

    def _save(self, doc: dict, event: str) -> None:
        self.store.db.execute(
            "INSERT INTO tc_step_runs VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE "
            "SET document=excluded.document,digest=excluded.digest",
            (doc["task_id"], canonical_json(doc), stable_hash(doc)),
        )
        task = self.store.get_task(doc["task_id"])
        assert task is not None
        sequence = len(self.store.events(task.task_id)) + 1
        self.store.append_event(TaskEvent(
            event_id=f"executor:{task.task_id}:{sequence}", task_id=task.task_id,
            sequence=sequence, event_type=event, actor_id="step-executor",
            occurred_at=self.store.clock(), from_state=task.state, to_state=task.state,
            payload={"journal_hash": stable_hash(doc), "plan_hash": doc["plan_hash"]},
        ))

    def _transition(self, task_id: str, state: TaskState, *,
                    event_type: str = "executor-state", payload: dict | None = None) -> None:
        task = self.store.get_task(task_id)
        assert task is not None
        if task.state is state:
            return
        sequence = len(self.store.events(task_id)) + 1
        self.store.append_event(TaskEvent(
            event_id=f"executor:{task_id}:{sequence}", task_id=task_id, sequence=sequence,
            event_type=event_type, actor_id="step-executor", occurred_at=self.store.clock(),
            from_state=task.state, to_state=state, payload=payload or {},
        ))

    def register(self, plan: Plan, requirements: Mapping[str, str]) -> None:
        """Bind the current immutable plan; later steps may still await approval."""
        with self.store.transaction():
            stored = self.store.get_plan(plan.task_id)
            if stored != plan or len(plan.steps) > 128:
                raise ExecutionBlocked("execution needs the current stored, bounded plan")
            if not set(requirements) <= {step.step_id for step in plan.steps}:
                raise ExecutionBlocked("approval binding names an unknown step")
            for step_id, requirement_id in requirements.items():
                requirement = self.authorization.get_requirement(requirement_id)
                if (requirement is None or requirement.plan_hash != plan.content_hash
                        or requirement.plan_id != plan.plan_id or requirement.task_id != plan.task_id
                        or step_id not in requirement.step_ids):
                    raise ExecutionBlocked("requirement must bind this exact fresh plan and step")
            if self.store.db.execute(
                "SELECT 1 FROM tc_step_run_history WHERE task_id=? AND plan_hash=?",
                (plan.task_id, plan.content_hash),
            ).fetchone():
                raise ExecutionBlocked("superseded execution cannot be registered again")
            for step in plan.steps:
                self._worker(ExecutionSpec.from_step(step))
                if step.rollback is not None:
                    self._effective_step(step, "rollback")
            if not ExecutionSpec.from_step(plan.steps[-1]).safe_boundary_after:
                raise ExecutionBlocked("a plan must end at a declared safe boundary")
            row = self.store.db.execute("SELECT 1 FROM tc_step_runs WHERE task_id=?", (plan.task_id,)).fetchone()
            if row:
                doc = self._load(plan.task_id)
                if doc["plan_hash"] == plan.content_hash and doc["requirements"] == dict(requirements):
                    return
                raise ExecutionBlocked("task already has an execution; resolve it before replanning")
            # Cancellation recorded against a superseded execution stays durable:
            # the replacement stops at its first (already safe) boundary.
            inherited = any(self._history_document(row)["cancel_requested"]
                            for row in self.store.db.execute(
                                "SELECT * FROM tc_step_run_history WHERE task_id=?", (plan.task_id,)))
            doc = dict(schema_version=2, task_id=plan.task_id, plan_hash=plan.content_hash,
                       requirements=dict(requirements), status="running", cancel_requested=inherited,
                       cursor=0, rollback_queue=[], attempts=[], effects=[])
            self._save(doc, "executor-registered")

    def _plan(self, doc: dict) -> Plan:
        plan = self.store.get_plan(doc["task_id"])
        if plan is None or plan.content_hash != doc["plan_hash"]:
            raise ExecutionBlocked("current plan changed; execution must be reconciled")
        return plan

    @staticmethod
    def _effective_step(step: PlanStep, phase: str) -> PlanStep:
        if phase == "forward":
            return step
        rollback = step.rollback
        if not isinstance(rollback, Mapping) or set(rollback) != {"operation", "arguments", "postconditions"}:
            raise ExecutionBlocked("rollback needs an exact operation, arguments and postconditions")
        effective = replace(step, operation=rollback["operation"], arguments=rollback["arguments"],
                            postconditions=tuple(rollback["postconditions"]), checkpoint=None)
        if ExecutionSpec.from_step(effective) != ExecutionSpec.from_step(step):
            raise ExecutionBlocked("rollback cannot broaden domain, purpose, bounds or effects")
        return effective

    def _grant(self, doc: dict, plan: Plan, step: PlanStep) -> tuple[ApprovalGrant, tuple[RentalRecord, ...]]:
        task = self.store.get_task(plan.task_id)
        assert task is not None
        spec = ExecutionSpec.from_step(step)
        if spec.domain is Domain.WORKSPACE:
            return self.authorization.workspace_preflight(
                plan, step, current_evidence_revision=task.evidence_revision,
                current_machine_id=MACHINE_ID,
            ), ()
        requirement_id = doc["requirements"].get(step.step_id)
        if not requirement_id:
            raise ExecutionBlocked("step awaits its exact Phase 3 approval")
        decision = self.authorization.preflight(
            requirement_id, plan, current_evidence_revision=task.evidence_revision,
            current_machine_id=MACHINE_ID,
        )
        if not decision.admit:
            raise ExecutionBlocked(decision.reason)
        requirement = self.authorization.get_requirement(requirement_id)
        grant = self.authorization.get_grant(requirement_id)
        assert requirement is not None and grant is not None
        if step.step_id not in grant.step_ids or grant.kind is not ApprovalKind.EXACT_HUMAN:
            raise ExecutionBlocked("step is outside the exact approval")
        if requirement.payload_purpose is not None:
            raise ExecutionBlocked("tenant payload access is unavailable to step workers")
        entry = next(item for item in requirement.steps if item["step"]["step_id"] == step.step_id)
        if entry["reference_bindings"]:
            ids = set(entry["reference_bindings"].values())
            rentals = tuple(record for record in requirement.tenant_bindings if record.rental_id in ids)
        else:
            rentals = ()
        if "tenant" in entry["flags"] and (spec.effect_scope != "tenant" or not rentals):
            raise ExecutionBlocked("tenant worker needs an exact rental and approved purpose")
        return grant, rentals

    def _request(self, plan: Plan, attempt: dict) -> StepRequest:
        step = next(step for step in plan.steps if step.step_id == attempt["step_id"])
        effective = self._effective_step(step, attempt["phase"])
        lease = next(item for item in self.store.leases(plan.task_id) if item.lease_id == attempt["lease_id"])
        return StepRequest(plan.content_hash, effective, attempt["phase"], lease,
                           ExecutionSpec.from_step(effective),
                           tuple(RentalRecord.from_document(item) for item in attempt["rentals"]),
                           attempt["checkpoint"],
                           parse_utc(attempt["result_at"], "result time")
                           if attempt.get("result_at") else None)

    def verification_request(self, task_id: str) -> StepRequest:
        """Return the exact applied-attempt binding, never a worker result."""
        doc = self._load(task_id)
        plan = self._plan(doc)
        if not doc["attempts"]:
            raise ExecutionBlocked("verification has no applied attempt")
        attempt = doc["attempts"][-1]
        if attempt["state"] not in {"applied", "verification_failed"}:
            raise ExecutionBlocked("latest attempt is not awaiting verification")
        request = self._request(plan, attempt)
        if request.result_at is None:
            raise ExecutionBlocked("applied attempt has no durable result time")
        return request

    def _enabled_task(self, plan: Plan, phase: str = "forward") -> None:
        task = self.store.get_task(plan.task_id)
        if task is None or task.state in {TaskState.PAUSED, TaskState.CANCELLED, TaskState.FAILED, TaskState.SUCCEEDED}:
            raise ExecutionBlocked("task breaker or terminal state prevents dispatch")
        check_executor = getattr(self.verifier, "check_executor", None)
        if check_executor is not None:
            check_executor(self)
        if self.mutation_allowed() is not True:
            raise ExecutionBlocked("independent mutation breaker is open (general mutation gate)")
        allowed = self.verification_allowed()
        if phase == "rollback" and allowed is not True:
            allowed = self.rollback_allowed()
        if allowed is not True:
            raise ExecutionBlocked("independent mutation breaker is open")

    def _dispatch_precondition(self, plan: Plan, step: PlanStep, phase: str) -> None:
        precondition = getattr(self.verifier, "dispatch_precondition", None)
        if precondition is not None:
            precondition(plan, self._effective_step(step, phase), phase)

    def _reconcile_policy_state(self, plan: Plan, doc: dict) -> bool:
        reconcile = getattr(self.verifier, "reconcile_state", None)
        return reconcile is not None and reconcile(plan, doc) is True

    def _safe(self, plan: Plan, doc: dict) -> bool:
        # Effects survive refused retries and failed verification. Only authenticated
        # postconditions can resolve them, never a later NOT_APPLIED result.
        return (not doc["rollback_queue"]
                and all(e["state"] == "resolved" for e in doc["effects"])
                and not any(a["state"] in {"intent", "dispatched", "uncertain", "applied"}
                            for a in doc["attempts"]))

    def prepare_replan(self, task_id: str) -> str:
        """Archive a safe execution before a new immutable plan is added/approved."""
        self._outermost()
        # Authority-free settlement first: an expired undispatched intent is
        # durably released and a recorded cancellation completes instead of
        # vanishing, even when the grant was revoked or has expired.
        self._settle(task_id)
        with self.store.transaction():
            doc = self._load(task_id)
            plan = self._plan(doc)
            task = self.store.get_task(task_id)
            if doc["status"] == "cancelled":
                return "cancelled"
            if (not self._safe(plan, doc) or task.state in {
                    TaskState.PAUSED, TaskState.CANCELLED, TaskState.SUCCEEDED}
                    or any(l.expires_at > self.store.clock() for l in self.store.leases(task_id))):
                raise ExecutionBlocked("replanning requires resolved effects and no live attempts or leases")
            if task.state is TaskState.FAILED and doc["status"] != "rolled_back":
                raise ExecutionBlocked("only a verified rolled-back execution can reopen a failed task")
            # Persist the terminal status before overwriting it; TaskStore checks it.
            doc["superseded_from"] = doc["status"]
            doc["status"] = "superseded"
            self._save(doc, "executor-superseded")
            self.store.db.execute("INSERT INTO tc_step_run_history VALUES(?,?,?,?)",
                                  (task_id, plan.content_hash, canonical_json(doc), stable_hash(doc)))
            self._transition(task_id, TaskState.PLANNING, event_type="executor-replan",
                             payload={"plan_hash": plan.content_hash, "journal_hash": stable_hash(doc)})
            self.store.db.execute("DELETE FROM tc_step_runs WHERE task_id=?", (task_id,))
            return "planning"

    def request_cancel(self, task_id: str) -> str:
        self._outermost()
        with self.store.transaction():
            doc = self._load(task_id)
            if doc["status"] in {"succeeded", "rolled_back", "cancelled"}:
                return doc["status"]
            doc["cancel_requested"] = True
            self._save(doc, "executor-cancel-requested")
        return self.advance(task_id)

    def advance(self, task_id: str) -> str:
        """One leased dispatch at most. Call again on a coordinator wake/recovery.

        A dispatched attempt is never submitted again. A live attempt is left alone;
        after its deadline only read-only reconciliation can change its outcome.
        """
        self._outermost()
        self._settle(task_id)
        reconcile = False
        with self.store.transaction():
            doc = self._load(task_id)
            plan = self._plan(doc)
            last = doc["attempts"][-1] if doc["attempts"] else None
            observing_investigation = doc["status"] == "needs_investigation"
            if doc["status"] not in {"running", "needs_investigation"}:
                return doc["status"]
            if last and last["state"] in {"dispatched", "uncertain"}:
                request = self._request(plan, last)
                if self.store.clock() < request.lease.expires_at:
                    return doc["status"] if observing_investigation else last["state"]
                reconcile = True
            elif last and last["state"] == "applied":
                return "verifying"
            elif observing_investigation:
                return doc["status"]
            if not reconcile:
                # _settle already committed any safe cancellation and released an
                # expired intent; a cancel racing in later is caught in _dispatch.
                phase = "rollback" if doc["rollback_queue"] else "forward"
                self._enabled_task(plan, phase)
                if last and last["state"] == "intent":
                    request = self._request(plan, last)
                    if self.store.clock() >= request.lease.expires_at:
                        last["state"] = "expired_intent"
                        self._save(doc, "executor-intent-expired")
                        last = None
                if not last or last["state"] != "intent":
                    step_id = doc["rollback_queue"][0] if phase == "rollback" else plan.steps[doc["cursor"]].step_id
                    step = next(step for step in plan.steps if step.step_id == step_id)
                    self._dispatch_precondition(plan, step, phase)
                    if any(lease.step_id == step_id and lease.expires_at > self.store.clock()
                           for lease in self.store.leases(task_id)):
                        return "lease_wait"
                    attempt_no = 1 + sum(a["step_id"] == step_id and a["phase"] == phase for a in doc["attempts"])
                    if attempt_no > MAX_ATTEMPTS:
                        doc["status"] = "needs_replan"
                        self._save(doc, "executor-attempt-budget-exhausted")
                        return doc["status"]
                    grant, rentals = self._grant(doc, plan, step)
                    if grant not in self.store.approvals(task_id):
                        if self.store.get_task(task_id).state in {TaskState.VERIFYING, TaskState.INVESTIGATING}:
                            self._transition(task_id, TaskState.PLANNING)
                        self._transition(task_id, TaskState.AWAITING_APPROVAL)
                        self.store.add_approval(grant)
                    self._transition(task_id, TaskState.EXECUTING)
                    now = self.store.clock()
                    key = stable_hash({"plan": plan.content_hash, "step": step_id,
                                       "phase": phase, "attempt": attempt_no})
                    lease = ExecutionLease(
                        lease_id="step-" + key, task_id=task_id, plan_id=plan.plan_id,
                        plan_hash=plan.content_hash, step_id=step_id, grant_id=grant.grant_id,
                        grant_hash=grant.content_hash, policy_revision=grant.policy_revision,
                        evidence_revision=grant.evidence_revision, holder="step-executor",
                        idempotency_key=key, acquired_at=now,
                        expires_at=min(now + timedelta(seconds=step.max_execution_seconds),
                                       grant.expires_at, plan.expires_at),
                    )
                    lease = self.store.acquire_current_lease(lease)
                    checkpoint = None
                    if phase == "rollback":
                        checkpoint = next(a["checkpoint"] for a in reversed(doc["attempts"])
                                          if a["step_id"] == step_id and a["phase"] == "forward")
                    last = dict(step_id=step_id, phase=phase, attempt=attempt_no, lease_id=lease.lease_id,
                                state="intent", checkpoint=checkpoint, rentals=[r.to_document() for r in rentals],
                                dispatched_at=None, result_at=None, verification=None)
                    doc["attempts"].append(last)
                    self._save(doc, "executor-intent")
        # Intent committed separately: a crash here is provably pre-dispatch.
        if reconcile:
            return self._reconcile(task_id, request)
        return self._dispatch(task_id)

    _TO_VERIFYING = MappingProxyType({
        TaskState.INVESTIGATING: (TaskState.PLANNING, TaskState.EXECUTING, TaskState.VERIFYING),
        TaskState.PLANNING: (TaskState.EXECUTING, TaskState.VERIFYING),
        TaskState.AWAITING_APPROVAL: (TaskState.EXECUTING, TaskState.VERIFYING),
        TaskState.EXECUTING: (TaskState.VERIFYING,),
        TaskState.VERIFYING: (),
    })

    def _settle(self, task_id: str) -> None:
        """Authority-free bookkeeping, committed before any dispatch gate.

        Observed progress, release of an expired undispatched intent, and a safe
        cancellation are recordings, not mutations. Neither the breaker, a pause,
        nor revoked/expired authority may roll them back or prevent them.
        """
        with self.store.transaction():
            doc = self._load(task_id)
            plan = self._plan(doc)
            last = doc["attempts"][-1] if doc["attempts"] else None
            task = self.store.get_task(task_id)
            if self._reconcile_policy_state(plan, doc):
                self._save(doc, "executor-investigation-needed")
            if (last and last["state"] in {"applied", "verification_failed"}
                    and task.state is not TaskState.PAUSED):
                path = self._TO_VERIFYING.get(task.state)
                if last.get("pending_verification"):
                    self._settle_verification(plan, doc, last, path)
                elif task.state is TaskState.EXECUTING:
                    self._transition(task_id, TaskState.VERIFYING)
            last = doc["attempts"][-1] if doc["attempts"] else None
            if last and last["state"] == "intent":
                lease = self._request(plan, last).lease
                if self.store.clock() >= lease.expires_at:
                    last["state"] = "expired_intent"
                    self._save(doc, "executor-intent-expired")
            if doc["cancel_requested"] and doc["status"] in {"running", "needs_replan"}:
                last = doc["attempts"][-1] if doc["attempts"] else None
                undispatched = bool(last and last["state"] == "intent")
                boundary = dict(doc, attempts=doc["attempts"][:-1]) if undispatched else doc
                if self._safe(plan, boundary):
                    if undispatched:
                        # Provably pre-dispatch; its lease simply lapses unused.
                        last["state"] = "cancelled_intent"
                    doc["status"] = "cancelled"
                    task = self.store.get_task(task_id)
                    if task.state not in {TaskState.FAILED, TaskState.SUCCEEDED}:
                        self._transition(task_id, TaskState.CANCELLED)
                    self._save(doc, "executor-cancelled")

    def _settle_verification(self, plan: Plan, doc: dict, attempt: dict, path) -> None:
        pending = attempt["pending_verification"]
        try:
            with self.store.transaction():  # Savepoint: a rejection leaves no partial lifecycle.
                candidate = strict_json_loads(canonical_json(doc))
                if path is None and attempt["phase"] == "forward":
                    raise TaskStoreError("lifecycle cannot legally reach verifying")
                for state in path or ():
                    self._transition(plan.task_id, state)
                self._apply_verification(plan, candidate, candidate["attempts"][-1])
        except TaskStoreError as error:
            # Durable, inspectable refusal instead of the same exception on every
            # wake. The effect stays unresolved until fresh evidence is accepted.
            attempt.pop("pending_verification")
            attempt.setdefault("rejected_verifications", []).append(
                {"verification_id": pending["verification_id"], "reason": type(error).__name__})
            doc["status"] = "needs_replan"
            doc["blocked_reason"] = "verification-rejected"
            self._save(doc, "executor-verification-rejected")
        else:
            doc.clear()
            doc.update(candidate)

    def _cancel_intent(self, plan: Plan, doc: dict, attempt: dict) -> bool:
        # No new work at a safe boundary, including an already leased intent.
        # Recording this is not a mutation, so it precedes the breaker/pause gates.
        if not doc["cancel_requested"] or not self._safe(plan, dict(doc, attempts=doc["attempts"][:-1])):
            return False
        attempt["state"] = "cancelled_intent"
        doc["status"] = "cancelled"
        if self.store.get_task(plan.task_id).state not in {TaskState.FAILED, TaskState.SUCCEEDED}:
            self._transition(plan.task_id, TaskState.CANCELLED)
        self._save(doc, "executor-cancelled")
        return True

    def _dispatch(self, task_id: str) -> str:
        self._outermost()
        with self.store.transaction():
            doc = self._load(task_id)
            plan = self._plan(doc)
            attempt = doc["attempts"][-1]
            if attempt["state"] != "intent":
                return attempt["state"]
            if self._cancel_intent(plan, doc, attempt):
                return "cancelled"
            self._enabled_task(plan, attempt["phase"])
            request = self._request(plan, attempt)
            original = next(step for step in plan.steps if step.step_id == attempt["step_id"])
            self._dispatch_precondition(plan, original, attempt["phase"])
            worker = self._worker(request.spec)
        # External read with no database write lock held. The worker contract
        # (bounded-calls, hard-deadline, read-only-inspect) bounds it; everything it
        # relied on is rechecked under the write lock below before the marker.
        started = self.store.clock()
        try:
            inspection = worker.inspect(request)
        except Exception:
            raise ExecutionBlocked("worker inspection failed") from None
        if self.store.clock() - started >= timedelta(seconds=request.step.max_execution_seconds):
            raise ExecutionBlocked("worker inspection exceeded its bound")
        if not isinstance(inspection, Inspection) or inspection.machine_id != MACHINE_ID:
            raise ExecutionBlocked("machine identity mismatch")
        if inspection.preconditions_passed is not True or inspection.rentals != request.rentals:
            raise ExecutionBlocked("preconditions or exact current RentalRecord mismatch")
        digest = None
        if request.step.checkpoint is not None:
            digest = inspection.checkpoint_digest
            if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
                raise ExecutionBlocked("checkpoint needs a durable content digest")
            if any(c not in "0123456789abcdef" for c in digest[7:]):
                raise ExecutionBlocked("invalid checkpoint digest")
        with self.store.transaction():
            doc = self._load(task_id)
            plan = self._plan(doc)
            attempt = doc["attempts"][-1]
            # Fence: another executor may have dispatched, cancelled or released
            # this intent while no lock was held.
            if attempt["lease_id"] != request.lease.lease_id or attempt["state"] != "intent":
                return attempt["state"]
            if self._cancel_intent(plan, doc, attempt):
                return "cancelled"
            self._enabled_task(plan, attempt["phase"])
            if self._request(plan, attempt) != request:
                raise ExecutionBlocked("execution request changed during inspection")
            if digest is not None:
                attempt["checkpoint"] = digest
                request = replace(request, checkpoint_digest=digest)
            original = next(s for s in plan.steps if s.step_id == attempt["step_id"])
            grant, rentals = self._grant(doc, plan, original)
            if grant.content_hash != request.lease.grant_hash or rentals != request.rentals:
                raise ExecutionBlocked("authority changed before dispatch")
            self._enabled_task(plan, attempt["phase"])
            self.store.acquire_lease(request.lease)  # Replay revalidates live authority.
            self._dispatch_precondition(plan, original, attempt["phase"])
            attempt["state"] = "dispatched"
            attempt["dispatched_at"] = utc_text(self.store.clock())
            self._save(doc, "executor-dispatched")
        # This marker is deliberately conservative: a crash before the call still
        # requires reconciliation because atomic commit+external effect is impossible.
        try:
            result = worker.dispatch(request)
        except Exception:
            result = WorkerResult(request.idempotency_key, Outcome.UNKNOWN)
        return self._result(task_id, request, result, reconciliation=False)

    def _reconcile(self, task_id: str, request: StepRequest) -> str:
        # Reconciliation is observation, permitted even after revocation/pause. It
        # cannot retry, mutate, read payload, or infer non-execution from absence.
        started = self.store.clock()
        try:
            result = self._worker(request.spec).reconcile(request)
        except Exception:
            result = WorkerResult(request.idempotency_key, Outcome.UNKNOWN)
        if self.store.clock() - started >= timedelta(seconds=request.step.max_execution_seconds):
            result = WorkerResult(request.idempotency_key, Outcome.UNKNOWN)
        return self._result(task_id, request, result, reconciliation=True)

    def _result(self, task_id: str, request: StepRequest, result: WorkerResult, *, reconciliation: bool) -> str:
        with self.store.transaction():
            doc = self._load(task_id)
            attempt = doc["attempts"][-1]
            if attempt["lease_id"] != request.lease.lease_id or attempt["state"] not in {"dispatched", "uncertain"}:
                return attempt["state"]
            valid = (
                isinstance(result, WorkerResult) and result.idempotency_key == request.idempotency_key
                and isinstance(result.outcome, Outcome) and isinstance(result.output, bytes)
                and len(result.output) <= request.spec.max_output_bytes
            )
            # A timeout/invalid response cannot prove execution or non-execution.
            if not reconciliation and self.store.clock() >= request.lease.expires_at:
                valid = False
            outcome = result.outcome if valid else Outcome.UNKNOWN
            was_investigation = doc["status"] == "needs_investigation"
            attempt["state"] = {Outcome.APPLIED: "applied", Outcome.NOT_APPLIED: "not_applied",
                                Outcome.UNKNOWN: "uncertain"}[outcome]
            attempt["result_at"] = utc_text(self.store.clock())
            if valid:
                attempt["output_digest"] = hashlib.sha256(result.output).hexdigest()
                attempt["output_bytes"] = len(result.output)
            if outcome is Outcome.APPLIED:
                doc["effects"].append(dict(lease_id=attempt["lease_id"], step_id=attempt["step_id"],
                                           phase=attempt["phase"], state="unverified"))
                if was_investigation:
                    doc["status"] = "running"
                    doc.pop("blocked_reason", None)
                    doc["investigation"].update(
                        resolution="applied", resolved_at=utc_text(self.store.clock()),
                        effect_application="applied", out_of_band_needed=False,
                    )
                task = self.store.get_task(task_id)
                if task and task.state is TaskState.EXECUTING:
                    self._transition(task_id, TaskState.VERIFYING)
            investigation = self._reconcile_policy_state(self._plan(doc), doc)
            self._save(doc, "executor-reconciled" if reconciliation else "executor-result")
            if investigation or doc["status"] == "needs_investigation":
                return doc["status"]
            return "verifying" if outcome is Outcome.APPLIED else attempt["state"]

    def accept_verification(self, verification: Verification) -> str:
        """Record independent observation even while dispatch is paused/broken.

        Validation's hard deadline/isolation is an external interface contract.
        Evidence commits before lifecycle reconciliation or any next dispatch.
        """
        self._outermost()
        with self.store.transaction():
            doc = self._load(verification.task_id)
            plan = self._plan(doc)
            prior = [v for a in doc["attempts"] for v in a.get("verifications", [])]
            if verification.to_document() in prior:
                return doc["status"]
            if any(v["verification_id"] == verification.verification_id for v in prior):
                raise ExecutionBlocked("verification identity was reused with different evidence")
            if not doc["attempts"]:
                raise ExecutionBlocked("verification has no applied attempt")
            attempt = doc["attempts"][-1]
            request = self._request(plan, attempt)
            if (
                attempt["state"] not in {"applied", "verification_failed"}
                or attempt.get("pending_verification")
                or any(verification.performed_at < parse_utc(v["performed_at"], "verification time")
                       for v in attempt.get("verifications", []))
                or verification.plan_hash != plan.content_hash
                or verification.plan_id != plan.plan_id or verification.step_id != request.step.step_id
                or verification.lease_id != request.lease.lease_id
                or verification.lease_hash != request.lease.content_hash
                or verification.evidence_revision != plan.evidence_revision
                or not parse_utc(attempt["result_at"], "result time") <= verification.performed_at <= self.store.clock()
                or getattr(self.verifier, "contract", None) != VERIFIER_CONTRACT
                or len(verification.canonical_json()) > MAX_OUTPUT_BYTES
            ):
                raise ExecutionBlocked("verification is not independent evidence for this exact attempt")
            started = self.store.clock()
            if (self.verifier.validate(request, verification) is not True
                    or self.store.clock() - started >= timedelta(seconds=request.step.max_execution_seconds)):
                raise ExecutionBlocked("independent verification failed validation or exceeded its deadline")
            attempt.setdefault("verifications", []).append(verification.to_document())
            if verification.status is not VerificationStatus.UNCERTAIN:
                attempt["pending_verification"] = verification.to_document()
            received = getattr(self.verifier, "verification_received", None)
            if received is not None:
                received(request, verification)
            self._save(doc, "executor-verification-received")
        if verification.status is VerificationStatus.UNCERTAIN:
            return "verifying"
        return self.advance(plan.task_id)

    def _apply_verification(self, plan: Plan, doc: dict, attempt: dict) -> None:
        verification = Verification.from_document(attempt["pending_verification"])
        if attempt["phase"] == "forward":
            self.store.add_verification(verification)
        attempt.pop("pending_verification")
        doc.pop("blocked_reason", None)
        attempt["verification"] = verification.to_document()
        effect = next(e for e in doc["effects"] if e["lease_id"] == attempt["lease_id"])
        if verification.status is VerificationStatus.FAILED:
            attempt["state"] = "verification_failed"
            effect["state"] = "failed"
            if attempt["phase"] == "rollback":
                doc["status"] = "needs_replan"
            else:
                affected = list(plan.steps[:doc["cursor"] + 1])
                if any(s.rollback is None for s in affected):
                    doc["status"] = "needs_replan"
                else:
                    doc["rollback_queue"] = [s.step_id for s in reversed(affected)]
        else:
            attempt["state"] = "verified"
            effect["state"] = "verified"
            doc["status"] = "running"
            if attempt["phase"] == "rollback":
                # Independent restored postconditions resolve this step's original
                # effect AND any applied rollback, including failed verification.
                for item in doc["effects"]:
                    if item["step_id"] == attempt["step_id"]:
                        item["state"] = "resolved"
                doc["rollback_queue"].pop(0)
                if not doc["rollback_queue"]:
                    doc["status"] = "rolled_back"
                    self._transition(plan.task_id, TaskState.FAILED)
            else:
                doc["rollback_queue"] = []
                if self._request(plan, attempt).spec.safe_boundary_after:
                    for item in doc["effects"]:
                        if item["phase"] == "forward" and item["state"] == "verified":
                            item["state"] = "resolved"
                doc["cursor"] += 1
                if doc["cursor"] == len(plan.steps):
                    doc["status"] = "succeeded"
                    self._transition(plan.task_id, TaskState.SUCCEEDED)
        self._save(doc, "executor-verified")

    def recover(self) -> dict[str, str]:
        """Completion-driven coordinator entry point after restart; no polling loop."""
        self._outermost()
        result = {}
        for row in self.store.db.execute("SELECT task_id FROM tc_step_runs ORDER BY task_id").fetchall():
            # One bad task (store conflict, illegal lifecycle, corrupt journal)
            # must never abort recovery of the tasks after it.
            try:
                result[row[0]] = self.advance(row[0])
            except (ExecutionBlocked, TaskStoreError):
                result[row[0]] = "blocked"
        return result


def step_executor_enabled(environment: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environment is None else environment).get(FEATURE_FLAG_ENV, "") == "1"


def build_step_executor(environment: Mapping[str, str] | None = None, **interfaces) -> StepExecutor | None:
    if not step_executor_enabled(environment):
        return None
    required = {"store", "authorization", "workers", "verifier", "mutation_allowed"}
    optional = {"rollback_allowed", "verification_allowed"}
    if (not required <= set(interfaces) or not set(interfaces) <= required | optional
            or any(interfaces[name] is None for name in required)):
        raise ExecutionBlocked("enabled executor requires all injected interfaces")
    return StepExecutor(**interfaces)
