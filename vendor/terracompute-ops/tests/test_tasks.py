from __future__ import annotations

import sqlite3
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.plans import (
    ApprovalGrant,
    ApprovalKind,
    ContractError,
    Effect,
    ExecutionLease,
    Plan,
    PlanStep,
    Verification,
    VerificationStatus,
)
from terracompute_ops.tasks import (
    IllegalTransition,
    Task,
    TaskConflict,
    TaskEvent,
    TaskState,
    TaskStore,
    TaskStoreError,
)
from terracompute_ops.state import CURRENT_SCHEMA_VERSION, StateStore


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def replace_authorization_tables_with_legacy_v1(database: sqlite3.Connection) -> None:
    database.execute("DROP TABLE IF EXISTS tc_task_creation_contracts")
    database.execute("DROP TABLE IF EXISTS tc_task_operator_inputs")
    database.execute("DROP TABLE tc_task_verifications")
    database.execute("DROP TABLE tc_task_leases")
    database.execute(
        """CREATE TABLE tc_task_leases (
             lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
             plan_hash TEXT NOT NULL, step_id TEXT NOT NULL, holder TEXT NOT NULL,
             idempotency_key TEXT NOT NULL UNIQUE, lease_hash TEXT NOT NULL UNIQUE,
             lease_json BLOB NOT NULL, acquired_utc TEXT NOT NULL, expires_utc TEXT NOT NULL,
             FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id)
           )"""
    )
    database.execute(
        """CREATE TABLE tc_task_verifications (
             verification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL,
             plan_hash TEXT NOT NULL, step_id TEXT, verification_hash TEXT NOT NULL UNIQUE,
             verification_json BLOB NOT NULL, status TEXT NOT NULL, performed_utc TEXT NOT NULL,
             FOREIGN KEY(task_id,plan_id) REFERENCES tc_task_plans(task_id,plan_id)
           )"""
    )
    database.execute("UPDATE tc_task_schema SET version=1 WHERE namespace='tasks'")


def task() -> Task:
    return Task(
        task_id="task-1", requester_id="user-1", requester_group_id=-1001,
        origin_message_id="message-1", created_at=NOW, objective="repair monitoring",
        constraints=("preserve tenant workloads",), state=TaskState.INVESTIGATING,
        evidence_revision="evidence-r1", model_thread=None, attempt_count=0,
        deadline=NOW + timedelta(hours=1), budgets={"model_units": 100}, incident_ids=(),
    )


def event(
    sequence: int, from_state: TaskState, to_state: TaskState, *, event_id: str | None = None,
    at: datetime | None = None, evidence_revision: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        event_id=event_id or f"event-{sequence}", task_id="task-1", sequence=sequence,
        event_type="state-transition", actor_id="coordinator",
        occurred_at=at or NOW + timedelta(seconds=sequence), from_state=from_state,
        to_state=to_state, payload={"reason": "test"},
        evidence_revision=evidence_revision,
    )


def plan(version: int = 1, *, evidence_revision: str = "evidence-r1", steps=None) -> Plan:
    return Plan(
        plan_id=f"plan-{version}", task_id="task-1", version=version,
        objective="repair monitoring", evidence_revision=evidence_revision, assumptions=(),
        freshness_requirements=({"source": "target", "max_age_seconds": 60},),
        steps=steps or (PlanStep(
            step_id="step-1", operation="run_shell", arguments={"argv": ["true"]},
            effects=(Effect("effect-1", "owned monitor", owned_component=True),),
            affected_resources=("monitor.service",), preconditions=({"check": "identity"},),
            postconditions=({"check": "active"},), checkpoint={"kind": "state"},
            rollback={"operation": "restore"}, rollback_impossible_reason=None,
            expected_interruption="none", max_execution_seconds=30,
        ),),
        created_at=NOW + timedelta(seconds=2 + version),
        expires_at=NOW + timedelta(minutes=10 + version),
    )


def grant_for(value: Plan, *, grant_id: str = "grant-1", issued_at=None, expires_at=None) -> ApprovalGrant:
    return ApprovalGrant(
        grant_id, value.task_id, value.plan_id, value.content_hash,
        tuple(item.step_id for item in value.steps), ApprovalKind.EXACT_HUMAN,
        "operator-1", "policy-r1", value.evidence_revision, f"nonce-{grant_id}",
        issued_at or NOW + timedelta(seconds=5),
        expires_at or NOW + timedelta(minutes=5),
    )


def lease_for(
    value: Plan, grant: ApprovalGrant, *, lease_id: str = "lease-1",
    step_id: str = "step-1", acquired_at=None, expires_at=None,
) -> ExecutionLease:
    return ExecutionLease(
        lease_id, value.task_id, value.plan_id, value.content_hash, step_id,
        grant.grant_id, grant.content_hash, grant.policy_revision, grant.evidence_revision,
        "worker-1", f"idempotency-{lease_id}",
        acquired_at or NOW + timedelta(seconds=10),
        expires_at or NOW + timedelta(seconds=40),
    )


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.current_time = NOW + timedelta(seconds=30)
        self.policy_revision = "policy-r1"
        self.store = TaskStore(
            self.root, clock=lambda: self.current_time,
            policy_revision=lambda: self.policy_revision,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_new_database_uses_namespaced_migration_without_global_user_version(self) -> None:
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(
            self.store.db.execute(
                "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
            ).fetchone()[0],
            4,
        )
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("tc_tasks", tables)
        self.assertIn("tc_task_verifications", tables)
        self.assertIn("tc_task_operator_inputs", tables)
        self.assertIn("tc_task_creation_contracts", tables)

    def test_existing_incident_and_action_tables_remain_readable(self) -> None:
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("CREATE TABLE incidents(dedup_key TEXT PRIMARY KEY)")
        database.execute("CREATE TABLE tc_action_audit(id INTEGER PRIMARY KEY, detail TEXT)")
        database.execute("INSERT INTO incidents VALUES('incident-1')")
        database.execute("INSERT INTO tc_action_audit VALUES(1,'preserved')")
        database.commit()
        database.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.assertEqual(self.store.db.execute("SELECT * FROM incidents").fetchone()[0], "incident-1")
        self.assertEqual(self.store.db.execute("SELECT detail FROM tc_action_audit").fetchone()[0], "preserved")

    def test_task_tables_coexist_with_the_real_state_schema_in_both_startup_orders(self) -> None:
        self.store.close()
        state = StateStore(self.root)
        state.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.store.create_task(task())
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION)
        self.store.close()
        state = StateStore(self.root)
        try:
            self.assertEqual(state.db.execute("SELECT count(*) FROM tc_tasks").fetchone()[0], 1)
            self.assertTrue(state.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'"
            ).fetchone())
        finally:
            state.close()
        self.store = TaskStore(self.root, clock=lambda: NOW)

    def test_lifecycle_replay_is_idempotent_and_illegal_transition_rolls_back(self) -> None:
        value = task()
        self.assertTrue(self.store.create_task(value))
        self.assertFalse(self.store.create_task(value))
        with self.assertRaises(TaskConflict):
            self.store.create_task(replace(value, incident_ids=("incident-forged",)))
        planning = event(2, TaskState.INVESTIGATING, TaskState.PLANNING)
        self.assertEqual(planning.schema_version, 2)
        legacy_event = planning.to_document()
        legacy_event["schema_version"] = 1
        with self.assertRaisesRegex(ContractError, "unsupported"):
            TaskEvent.from_document(legacy_event)
        self.assertTrue(self.store.append_event(planning))
        self.assertFalse(self.store.append_event(planning))
        self.assertFalse(self.store.create_task(value))
        with self.assertRaises(IllegalTransition):
            self.store.append_event(event(3, TaskState.PLANNING, TaskState.SUCCEEDED))
        self.assertEqual(self.store.get_task("task-1").state, TaskState.PLANNING)
        self.assertEqual(len(self.store.events("task-1")), 2)
        with self.assertRaises(TaskConflict):
            self.store.append_event(replace(planning, payload={"reason": "different"}))

    def test_v3_migration_backfills_immutable_creation_contract(self) -> None:
        value = task()
        self.store.create_task(value)
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("DROP TABLE tc_task_creation_contracts")
        database.execute("UPDATE tc_task_schema SET version=3 WHERE namespace='tasks'")
        database.commit()
        database.close()

        self.store = TaskStore(
            self.root, clock=lambda: self.current_time,
            policy_revision=lambda: self.policy_revision,
        )
        self.assertFalse(self.store.create_task(value))
        with self.assertRaises(TaskConflict):
            self.store.create_task(replace(value, incident_ids=("incident-forged",)))
        self.assertEqual(self.store.get_task(value.task_id).state, TaskState.PLANNING)

    def test_plan_approval_lease_verification_survive_restart(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.assertTrue(self.store.add_plan(value))
        self.assertFalse(self.store.add_plan(value))
        grant = grant_for(value)
        self.assertTrue(self.store.add_approval(grant))
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.assertTrue(self.store.acquire_lease(lease))
        self.assertFalse(self.store.acquire_lease(lease))
        self.current_time = NOW + timedelta(seconds=50)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.VERIFYING,
            at=NOW + timedelta(seconds=45), evidence_revision="evidence-r2",
        ))
        verification = Verification(
            "verification-1", "task-1", "plan-1", value.content_hash, "step-1",
            lease.lease_id, lease.content_hash,
            VerificationStatus.SUCCEEDED, ({"check": "active", "ok": True},),
            "evidence-r2", NOW + timedelta(seconds=50),
        )
        self.store.add_verification(verification)
        self.store.close()
        self.store = TaskStore(self.root, clock=lambda: NOW + timedelta(seconds=20))
        self.assertEqual(self.store.get_task("task-1").state, TaskState.VERIFYING)
        self.assertEqual(self.store.get_plan("task-1"), value)
        self.assertEqual(self.store.approvals("task-1"), (grant,))
        self.assertEqual(self.store.leases("task-1"), (lease,))
        self.assertEqual(self.store.verifications("task-1"), (verification,))
        self.assertEqual(self.store.recovery_report.active_task_ids, ("task-1",))
        self.assertEqual(self.store.recovery_report.active_lease_ids, ("lease-1",))

    def test_active_lease_conflicts_expired_lease_allows_next_attempt(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        first = lease_for(value, grant)
        self.store.acquire_lease(first)
        self.current_time = NOW + timedelta(seconds=20)
        with self.assertRaisesRegex(TaskConflict, "active execution lease"):
            self.store.acquire_lease(lease_for(
                value, grant, lease_id="lease-2",
                acquired_at=self.current_time, expires_at=NOW + timedelta(seconds=50),
            ))
        self.current_time = NOW + timedelta(seconds=41)
        second = lease_for(
            value, grant, lease_id="lease-2", acquired_at=self.current_time,
            expires_at=NOW + timedelta(seconds=60),
        )
        self.assertTrue(self.store.acquire_lease(second))

    def test_execution_lease_requires_the_executing_lifecycle_state(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        with self.assertRaisesRegex(IllegalTransition, "executing task"):
            self.store.acquire_lease(lease_for(
                value, grant, acquired_at=self.current_time,
                expires_at=self.current_time + timedelta(seconds=30),
            ))

    def test_crash_injection_rolls_back_event_and_plan_transitions(self) -> None:
        self.store.create_task(task())
        with mock.patch.object(self.store, "_update_task", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        self.assertEqual(len(self.store.events("task-1")), 1)
        self.assertEqual(self.store.get_task("task-1").state, TaskState.INVESTIGATING)

        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        with mock.patch.object(self.store, "_update_task", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.store.add_plan(plan())
        self.assertIsNone(self.store.get_plan("task-1"))
        self.assertEqual(len(self.store.events("task-1")), 2)

    def test_multi_step_execution_leases_survive_restart(self) -> None:
        second_step = replace(
            plan().steps[0], step_id="step-2",
            arguments={"argv": ["systemctl", "is-active", "monitor.service"]},
        )
        value = plan(steps=(plan().steps[0], second_step))
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        first = lease_for(value, grant)
        self.store.acquire_lease(first)
        self.store.close()
        self.current_time = NOW + timedelta(seconds=20)
        self.store = TaskStore(
            self.root, clock=lambda: self.current_time,
            policy_revision=lambda: self.policy_revision,
        )
        second = lease_for(
            value, grant, lease_id="lease-2", step_id="step-2",
            acquired_at=self.current_time, expires_at=NOW + timedelta(seconds=50),
        )
        self.store.acquire_lease(second)
        self.assertEqual(self.store.leases("task-1"), (first, second))

    def test_nested_transaction_is_composed_with_caller_savepoint(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.root / "state.sqlite3")
        connection.execute("BEGIN")
        nested = TaskStore(connection, clock=lambda: self.current_time)
        nested.create_task(task())
        self.assertTrue(connection.in_transaction)
        connection.rollback()
        self.assertEqual(connection.execute("SELECT count(*) FROM tc_tasks").fetchone()[0], 0)
        nested.close()
        connection.close()
        self.store = TaskStore(self.root, clock=lambda: self.current_time)

    def test_concurrent_first_start_serializes_namespaced_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            barrier = threading.Barrier(2)
            failures: list[BaseException] = []

            def start() -> None:
                try:
                    barrier.wait()
                    store = TaskStore(root, clock=lambda: self.current_time)
                    store.close()
                except BaseException as error:  # assertion reports both thread failures
                    failures.append(error)

            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            database = sqlite3.connect(root / "state.sqlite3")
            try:
                self.assertEqual(database.execute(
                    "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
                ).fetchone()[0], 4)
            finally:
                database.close()

    def test_shared_database_mode_and_hardlink_safety_match_state_store(self) -> None:
        database_path = self.root / "state.sqlite3"
        self.assertEqual(stat.S_IMODE(database_path.stat().st_mode), 0o660)
        self.store.close()
        os.link(database_path, self.root / "linked.sqlite3")
        with self.assertRaisesRegex(RuntimeError, "unsafe shared SQLite file"):
            TaskStore(self.root, clock=lambda: self.current_time)
        os.unlink(self.root / "linked.sqlite3")
        self.store = TaskStore(self.root, clock=lambda: self.current_time)

    def test_plan_change_is_audited_and_active_lease_blocks_replan(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        first_plan = plan()
        self.store.add_plan(first_plan)
        plan_event = self.store.events("task-1")[-1]
        self.assertEqual(plan_event.event_type, "plan-added")
        self.assertEqual(plan_event.payload["plan_hash"], first_plan.content_hash)
        grant = grant_for(first_plan)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        self.store.acquire_lease(lease_for(first_plan, grant))
        self.current_time = NOW + timedelta(seconds=20)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.PLANNING,
            at=NOW + timedelta(seconds=15), evidence_revision="evidence-r2",
        ))
        second_plan = replace(
            plan(2, evidence_revision="evidence-r2"),
            created_at=self.current_time,
        )
        with self.assertRaisesRegex(TaskConflict, "execution lease is active"):
            self.store.add_plan(second_plan)
        self.current_time = NOW + timedelta(seconds=41)
        second_plan = replace(second_plan, created_at=self.current_time)
        self.assertTrue(self.store.add_plan(second_plan))
        self.current_time = NOW + timedelta(seconds=42)
        self.store.append_event(event(
            7, TaskState.PLANNING, TaskState.EXECUTING,
            at=NOW + timedelta(seconds=42), evidence_revision="evidence-r2",
        ))
        with self.assertRaisesRegex(TaskConflict, "current plan"):
            self.store.acquire_lease(lease_for(
                first_plan, grant, lease_id="stale-plan-lease",
                acquired_at=self.current_time, expires_at=NOW + timedelta(seconds=50),
            ))

    def test_plan_requires_current_evidence_valid_state_and_sensible_time(self) -> None:
        self.store.create_task(task())
        with self.assertRaisesRegex(TaskConflict, "evidence revision"):
            self.store.add_plan(plan(evidence_revision="stale-evidence"))
        with self.assertRaisesRegex(TaskConflict, "future-dated"):
            self.store.add_plan(replace(
                plan(), created_at=self.current_time + timedelta(seconds=1),
                expires_at=self.current_time + timedelta(minutes=1),
            ))
        self.store.append_event(event(
            2, TaskState.INVESTIGATING, TaskState.PAUSED,
            at=NOW + timedelta(seconds=2),
        ))
        with self.assertRaisesRegex(IllegalTransition, "investigating or planning"):
            self.store.add_plan(plan())

    def test_task_deadline_bounds_plan_and_blocks_each_authorization_stage(self) -> None:
        deadline = NOW + timedelta(seconds=50)
        self.store.create_task(replace(task(), deadline=deadline))
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        with self.assertRaisesRegex(TaskConflict, "plan expiry exceeds the task deadline"):
            self.store.add_plan(plan())

        value = replace(plan(), expires_at=deadline)
        self.store.add_plan(value)
        self.current_time = deadline
        grant = grant_for(
            value, issued_at=NOW + timedelta(seconds=5),
            expires_at=NOW + timedelta(seconds=45),
        )
        with self.assertRaisesRegex(TaskConflict, "task deadline has expired"):
            self.store.add_approval(grant)

        self.current_time = NOW + timedelta(seconds=30)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = deadline
        with self.assertRaisesRegex(TaskConflict, "task deadline has expired"):
            self.store.acquire_lease(lease_for(
                value, grant, acquired_at=deadline,
                expires_at=deadline + timedelta(seconds=1),
            ))

    def test_expired_task_cannot_add_verification(self) -> None:
        deadline = NOW + timedelta(seconds=50)
        self.store.create_task(replace(task(), deadline=deadline))
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = replace(plan(), expires_at=deadline)
        self.store.add_plan(value)
        grant = grant_for(value, expires_at=NOW + timedelta(seconds=45))
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.store.acquire_lease(lease)
        self.current_time = NOW + timedelta(seconds=45)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.VERIFYING,
            at=self.current_time, evidence_revision="verify-r1",
        ))
        self.current_time = deadline
        verification = Verification(
            "verification-deadline", value.task_id, value.plan_id, value.content_hash,
            "step-1", lease.lease_id, lease.content_hash,
            VerificationStatus.SUCCEEDED, ({"check": "active", "ok": True},),
            "verify-r1", deadline - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(TaskConflict, "task deadline has expired"):
            self.store.add_verification(verification)

    def test_store_clock_rejects_missing_expired_and_stale_policy_approval(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        expired = grant_for(
            value, grant_id="expired", expires_at=NOW + timedelta(seconds=20)
        )
        with self.assertRaisesRegex(TaskConflict, "not live"):
            self.store.add_approval(expired)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=40)
        missing = replace(
            lease_for(
                value, grant, lease_id="missing", acquired_at=self.current_time,
                expires_at=NOW + timedelta(seconds=50),
            ),
            grant_id="not-a-grant", grant_hash="0" * 64,
        )
        with self.assertRaisesRegex(TaskConflict, "no approval grant"):
            self.store.acquire_lease(missing)
        self.store.policy_revision = None
        with self.assertRaisesRegex(TaskStoreError, "current policy revision provider"):
            self.store.acquire_lease(lease_for(
                value, grant, lease_id="no-policy", acquired_at=self.current_time,
                expires_at=NOW + timedelta(seconds=50),
            ))
        self.store.policy_revision = lambda: self.policy_revision
        self.policy_revision = "policy-r2"
        with self.assertRaisesRegex(TaskConflict, "policy revision"):
            self.store.acquire_lease(lease_for(
                value, grant, lease_id="stale-policy", acquired_at=self.current_time,
                expires_at=NOW + timedelta(seconds=50),
            ))
        self.policy_revision = "policy-r1"
        self.current_time = NOW + timedelta(minutes=6)
        with self.assertRaisesRegex(TaskConflict, "not live"):
            self.store.acquire_lease(lease_for(
                value, grant, lease_id="expired-grant", acquired_at=self.current_time,
                expires_at=self.current_time + timedelta(seconds=10),
            ))

    def test_fractional_timestamp_boundaries_detect_overlapping_leases(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        first_at = NOW + timedelta(seconds=10, microseconds=100_000)
        first_expiry = NOW + timedelta(seconds=10, microseconds=900_000)
        self.current_time = first_at
        self.store.acquire_lease(lease_for(
            value, grant, acquired_at=first_at, expires_at=first_expiry,
        ))
        overlap_at = NOW + timedelta(seconds=10, microseconds=200_000)
        self.current_time = overlap_at
        with self.assertRaisesRegex(TaskConflict, "active execution lease"):
            self.store.acquire_lease(lease_for(
                value, grant, lease_id="lease-2", acquired_at=overlap_at,
                expires_at=NOW + timedelta(seconds=11),
            ))
        self.current_time = first_expiry
        self.assertTrue(self.store.acquire_lease(lease_for(
            value, grant, lease_id="lease-2", acquired_at=first_expiry,
            expires_at=NOW + timedelta(seconds=11),
        )))

    def test_exact_lease_replay_revalidates_expiry_and_policy(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.assertTrue(self.store.acquire_lease(lease))
        self.assertFalse(self.store.acquire_lease(lease))

        self.policy_revision = "policy-r2"
        self.current_time = NOW + timedelta(seconds=20)
        with self.assertRaisesRegex(TaskConflict, "policy revision"):
            self.store.acquire_lease(lease)
        self.policy_revision = "policy-r1"
        self.current_time = lease.expires_at
        with self.assertRaisesRegex(TaskConflict, "not live"):
            self.store.acquire_lease(lease)

    def test_numeric_replay_ambiguity_uses_canonical_bytes(self) -> None:
        self.store.create_task(task())
        original = replace(
            event(2, TaskState.INVESTIGATING, TaskState.INVESTIGATING),
            payload={"value": 1},
        )
        self.store.append_event(original)
        with self.assertRaisesRegex(TaskConflict, "different content"):
            self.store.append_event(replace(original, payload={"value": 1.0}))

    def test_step_verification_requires_current_evidence_and_real_lease(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.store.acquire_lease(lease)
        self.current_time = NOW + timedelta(seconds=20)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.VERIFYING,
            at=self.current_time, evidence_revision="verify-r1",
        ))
        unbound = Verification(
            "verification-unbound", value.task_id, value.plan_id, value.content_hash,
            "step-1", None, None, VerificationStatus.SUCCEEDED,
            ({"check": "active", "ok": True},), "verify-r1", self.current_time,
        )
        with self.assertRaisesRegex(TaskConflict, "bind an execution lease"):
            self.store.add_verification(unbound)
        stale = replace(
            unbound, verification_id="verification-stale",
            lease_id=lease.lease_id, lease_hash=lease.content_hash,
            evidence_revision="old-evidence",
        )
        with self.assertRaisesRegex(TaskConflict, "not current"):
            self.store.add_verification(stale)

    def test_verification_cannot_predate_verifying_transition_or_execution(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.store.acquire_lease(lease)
        self.current_time = NOW + timedelta(seconds=30)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.VERIFYING,
            at=NOW + timedelta(seconds=20), evidence_revision="verify-r1",
        ))
        before_transition = Verification(
            "verification-before-transition", value.task_id, value.plan_id,
            value.content_hash, "step-1", lease.lease_id, lease.content_hash,
            VerificationStatus.SUCCEEDED, ({"check": "active", "ok": True},),
            "verify-r1", NOW + timedelta(seconds=19),
        )
        with self.assertRaisesRegex(TaskConflict, "predates plan creation, execution, or verifying"):
            self.store.add_verification(before_transition)
        plan_level = replace(
            before_transition, verification_id="verification-plan-before-transition",
            step_id=None, lease_id=None, lease_hash=None,
        )
        with self.assertRaisesRegex(TaskConflict, "predates plan creation, execution, or verifying"):
            self.store.add_verification(plan_level)

    def test_plan_verification_cannot_predate_any_relevant_lease(self) -> None:
        self.store.create_task(task())
        self.store.append_event(event(2, TaskState.INVESTIGATING, TaskState.PLANNING))
        value = plan()
        self.store.add_plan(value)
        grant = grant_for(value)
        self.store.add_approval(grant)
        self.store.append_event(event(4, TaskState.PLANNING, TaskState.EXECUTING))
        self.current_time = NOW + timedelta(seconds=10)
        lease = lease_for(value, grant)
        self.store.acquire_lease(lease)
        # A malformed lifecycle transition time cannot make an earlier verification
        # appear causal; the durable lease acquisition remains an independent floor.
        self.current_time = NOW + timedelta(seconds=20)
        self.store.append_event(event(
            5, TaskState.EXECUTING, TaskState.VERIFYING,
            at=NOW + timedelta(seconds=9), evidence_revision="verify-r1",
        ))
        verification = Verification(
            "verification-before-lease", value.task_id, value.plan_id,
            value.content_hash, None, None, None, VerificationStatus.SUCCEEDED,
            ({"check": "active", "ok": True},), "verify-r1",
            NOW + timedelta(seconds=9, microseconds=500_000),
        )
        with self.assertRaisesRegex(TaskConflict, "predates plan creation, execution, or verifying"):
            self.store.add_verification(verification)

    def test_recovery_rejects_foreign_key_or_indexed_column_corruption(self) -> None:
        for corruption in ("orphan", "mismatch"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = TaskStore(root, clock=lambda: self.current_time)
                store.create_task(task())
                store.close()
                database = sqlite3.connect(root / "state.sqlite3")
                database.execute("PRAGMA foreign_keys=OFF")
                database.execute("DROP TRIGGER tc_task_events_immutable_update")
                if corruption == "orphan":
                    database.execute(
                        "UPDATE tc_task_events SET task_id='missing' WHERE event_id='task-created:task-1'"
                    )
                else:
                    database.execute(
                        "UPDATE tc_task_events SET to_state='failed' WHERE event_id='task-created:task-1'"
                    )
                database.commit()
                database.close()
                with self.assertRaisesRegex(
                    TaskStoreError, "foreign key check|indexed columns"
                ):
                    TaskStore(root, clock=lambda: self.current_time)

    def test_uncommitted_crash_write_is_rolled_back_on_restart(self) -> None:
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """INSERT INTO tc_tasks(task_id,task_hash,task_json,state,current_plan_version,
                   latest_event_sequence,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?)""",
            ("partial", "0" * 64, b"{}", "investigating", None, 1,
             "2026-09-20T12:00:00Z", "2026-09-20T12:00:00Z"),
        )
        database.close()  # SQLite rolls back the interrupted transaction.
        self.store = TaskStore(self.root, clock=lambda: NOW)
        self.assertIsNone(self.store.get_task("partial"))

    def test_stored_contract_rows_are_append_only(self) -> None:
        self.store.create_task(task())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable task record"):
            self.store.db.execute("UPDATE tc_task_events SET event_id='changed'")
        self.store.db.rollback()

    def test_empty_legacy_v1_database_upgrades_transactionally_to_v4(self) -> None:
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        replace_authorization_tables_with_legacy_v1(database)
        database.commit()
        database.close()

        self.store = TaskStore(self.root, clock=lambda: self.current_time)
        self.assertEqual(self.store.db.execute(
            "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
        ).fetchone()[0], 4)
        self.assertEqual(
            {row[1] for row in self.store.db.execute("PRAGMA table_info(tc_task_leases)")},
            {
                "lease_id", "task_id", "plan_id", "plan_hash", "step_id", "grant_id",
                "grant_hash", "policy_revision", "evidence_revision", "holder",
                "idempotency_key", "lease_hash", "lease_json", "acquired_utc",
                "expires_utc",
            },
        )
        self.assertIn(
            "lease_id",
            {row[1] for row in self.store.db.execute(
                "PRAGMA table_info(tc_task_verifications)"
            )},
        )

    def test_populated_v2_database_gets_additive_operator_intake_ledger(self) -> None:
        self.store.create_task(task())
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("DROP TABLE tc_task_operator_inputs")
        database.execute("DROP TABLE tc_task_creation_contracts")
        database.execute(
            "UPDATE tc_task_schema SET version=2 WHERE namespace='tasks'"
        )
        database.commit()
        database.close()

        self.store = TaskStore(self.root, clock=lambda: self.current_time)
        self.assertEqual(
            self.store.db.execute(
                "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(self.store.get_task("task-1"), task())
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM tc_task_operator_inputs"
            ).fetchone()[0],
            0,
        )

    def test_populated_legacy_authorization_rows_refuse_v1_upgrade(self) -> None:
        for table, insert in (
            (
                "tc_task_leases",
                """INSERT INTO tc_task_leases VALUES(
                     'lease-legacy','task-legacy','plan-legacy','hash','step-1','worker',
                     'key','lease-hash',X'7B7D','2026-09-20','2026-09-21')""",
            ),
            (
                "tc_task_verifications",
                """INSERT INTO tc_task_verifications VALUES(
                     'verification-legacy','task-legacy','plan-legacy','hash','step-1',
                     'verification-hash',X'7B7D','succeeded','2026-09-20')""",
            ),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = TaskStore(root)
                store.close()
                database = sqlite3.connect(root / "state.sqlite3")
                replace_authorization_tables_with_legacy_v1(database)
                database.execute("PRAGMA foreign_keys=OFF")
                database.execute(insert)
                database.commit()
                database.close()
                with self.assertRaisesRegex(
                    TaskStoreError,
                    "cannot safely migrate populated task schema v1 authorization records",
                ):
                    TaskStore(root)
                database = sqlite3.connect(root / "state.sqlite3")
                try:
                    self.assertEqual(database.execute(
                        "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
                    ).fetchone()[0], 1)
                    self.assertEqual(database.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0], 1)
                finally:
                    database.close()

    def test_future_task_schema_is_refused(self) -> None:
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("UPDATE tc_task_schema SET version=5 WHERE namespace='tasks'")
        database.commit()
        database.close()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            TaskStore(self.root)
        database = sqlite3.connect(self.root / "state.sqlite3")
        database.execute("UPDATE tc_task_schema SET version=4 WHERE namespace='tasks'")
        database.commit()
        database.close()
        self.store = TaskStore(self.root)

    def test_failed_migration_rolls_back_all_partial_schema_changes(self) -> None:
        self.store.close()
        database = sqlite3.connect(self.root / "state.sqlite3")
        for name in (
            "tc_task_events", "tc_task_plans", "tc_task_approvals", "tc_task_leases",
            "tc_task_verifications", "tc_task_operator_inputs", "tc_tasks",
            "tc_task_creation_contracts", "tc_task_schema",
        ):
            database.execute(f"DROP TABLE IF EXISTS {name}")
        database.execute(
            "CREATE TABLE tc_task_schema(namespace TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        database.execute("INSERT INTO tc_task_schema VALUES('tasks',0)")
        database.execute("CREATE TABLE tc_task_plans(incompatible TEXT)")
        database.commit()
        database.close()
        with self.assertRaises(sqlite3.OperationalError):
            TaskStore(self.root)
        database = sqlite3.connect(self.root / "state.sqlite3")
        try:
            self.assertEqual(database.execute(
                "SELECT version FROM tc_task_schema WHERE namespace='tasks'"
            ).fetchone()[0], 0)
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertNotIn("tc_tasks", tables)
            self.assertNotIn("tc_task_events", tables)
            self.assertEqual(
                [row[1] for row in database.execute("PRAGMA table_info(tc_task_plans)")],
                ["incompatible"],
            )
            database.execute("DROP TABLE tc_task_plans")
            database.commit()
        finally:
            database.close()
        self.store = TaskStore(self.root)


if __name__ == "__main__":
    unittest.main()
