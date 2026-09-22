from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.incident_correlation import (
    BackupPayload,
    CollectorPayload,
    ControllerServicePayload,
    CorrelationDomain,
    DetectorBatch,
    DetectorFact,
    DetectorKind,
    DiskPayload,
    ExporterPayload,
    FactFreshness,
    FactStatus,
    GpuInventoryPayload,
    IncidentCorrelator,
    MarketCapacityPayload,
    MemoryOomPayload,
    NetworkPayload,
    NixGenerationPayload,
    NvidiaXidPayload,
    ResourceOwnership,
    Severity,
    authorize_correlation_plan,
    derive_repair_eligibility,
)
from terracompute_ops.plan_authorization import Authority, StandingEffectGrant
from terracompute_ops.plans import ContractError, Effect, Plan, PlanStep
from terracompute_ops.state import StateStore
from terracompute_ops.tasks import (
    IllegalTransition,
    TaskConflict,
    TaskEvent,
    TaskState,
    TaskStore,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


def fact(
    fact_id: str,
    revision: str,
    kind: DetectorKind,
    domain: CorrelationDomain,
    resource: str,
    failure: str,
    payload,
    *,
    status: FactStatus = FactStatus.FAULT,
    freshness: FactFreshness = FactFreshness.CURRENT,
    observed_at: datetime = NOW,
    related: tuple[str, ...] = (),
    ownership: ResourceOwnership = ResourceOwnership.HOST,
    routine: bool = False,
    severity: Severity = Severity.CRITICAL,
    source_instance: str = "boot-1",
) -> DetectorFact:
    return DetectorFact(
        fact_id=fact_id, evidence_revision=revision, kind=kind, domain=domain,
        resource_id=resource, failure_id=failure, status=status, freshness=freshness,
        severity=severity, observed_at=observed_at, source_instance=source_instance,
        summary=f"{kind.value} {failure}", payload=payload,
        related_resources=related, ownership=ownership, routine_reversible=routine,
    )


def batch(
    batch_id: str,
    revision: str,
    facts: tuple[DetectorFact, ...],
    *,
    complete: tuple[DetectorKind, ...] = (),
    collected_at: datetime = NOW,
) -> DetectorBatch:
    return DetectorBatch(batch_id, revision, collected_at, facts, complete)


class CorrelationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.state = StateStore(self.root, clock=self.clock)
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        self.addCleanup(self.state.close)
        self.service = IncidentCorrelator(
            self.state, self.tasks, enabled=True, clock=self.clock,
        )

    def gpu_facts(self, revision: str, suffix: str = "1") -> tuple[DetectorFact, ...]:
        return (
            fact(
                f"collector-{suffix}", revision, DetectorKind.COLLECTOR,
                CorrelationDomain.GPU_CAPACITY, "collector:target-probe", "stale",
                CollectorPayload(900, 725, None),
            ),
            fact(
                f"gpu-{suffix}", revision, DetectorKind.GPU_INVENTORY,
                CorrelationDomain.GPU_CAPACITY, "machine:17049", "gpu-count",
                GpuInventoryPayload(8, 7, ("GPU-8",)),
            ),
            fact(
                f"market-{suffix}", revision, DetectorKind.MARKET_CAPACITY,
                CorrelationDomain.GPU_CAPACITY, "market:machine-17049", "capacity-mismatch",
                MarketCapacityPayload(1, 0, 7, 8, True),
            ),
        )

    def test_concurrent_gpu_collector_market_faults_create_one_task(self) -> None:
        # An unrelated already-open incident must not suppress this correlation.
        unrelated = "a" * 64
        self.state.create_incident(
            unrelated,
            {"target": "terracompute", "machine_id": "17049"},
            {"kind": "old"}, "old incident", None,
        )
        result = self.service.process(batch("batch-1", "revision-1", self.gpu_facts("revision-1")))
        active = [item for item in result.outcomes if item.status == "active"]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].created)
        task = self.tasks.get_task(active[0].task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.machine_id, "17049")
        self.assertEqual(task.evidence_revision, active[0].evidence_revision)
        self.assertEqual(len(task.incident_ids), 3)
        self.assertEqual(len(self.tasks.list_tasks()), 1)
        self.assertEqual(len(self.state.current_snapshot()["incidents"]), 4)

    def test_unchanged_repeat_appends_evidence_and_material_change_creates_task(self) -> None:
        first = self.service.process(batch("batch-1", "revision-1", self.gpu_facts("revision-1")))
        first_task = first.outcomes[0].task_id
        self.clock.advance()
        repeated_facts = tuple(
            replace(item, fact_id=item.fact_id.replace("-1", "-2"),
                    evidence_revision="revision-2", observed_at=self.clock.now)
            for item in self.gpu_facts("revision-1")
        )
        repeated = self.service.process(batch(
            "batch-2", "revision-2", repeated_facts, collected_at=self.clock.now,
        ))
        self.assertFalse(repeated.outcomes[0].created)
        self.assertEqual(repeated.outcomes[0].task_id, first_task)
        self.assertEqual(len(self.tasks.events(first_task)), 2)
        self.assertEqual(self.tasks.get_task(first_task).evidence_revision,
                         repeated.outcomes[0].evidence_revision)

        self.clock.advance()
        changed = list(repeated_facts)
        changed[1] = replace(
            changed[1], fact_id="gpu-3", evidence_revision="revision-3",
            observed_at=self.clock.now,
            payload=GpuInventoryPayload(8, 6, ("GPU-7", "GPU-8")),
        )
        changed[0] = replace(changed[0], fact_id="collector-3",
                             evidence_revision="revision-3", observed_at=self.clock.now)
        changed[2] = replace(changed[2], fact_id="market-3",
                             evidence_revision="revision-3", observed_at=self.clock.now)
        result = self.service.process(batch(
            "batch-3", "revision-3", tuple(changed), collected_at=self.clock.now,
        ))
        self.assertTrue(result.outcomes[0].created)
        self.assertTrue(result.outcomes[0].material_changed)
        self.assertNotEqual(result.outcomes[0].task_id, first_task)
        self.assertIn(first_task, self.service.linked_tasks(result.outcomes[0].task_id))
        self.assertEqual(len(self.tasks.list_tasks()), 2)

    def test_stale_and_incomplete_facts_do_not_clear_fresh_fault(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        opened = self.service.process(batch("batch-1", "revision-1", (disk,)))
        task_id = opened.outcomes[0].task_id
        self.clock.advance()
        stale_healthy = replace(
            disk, fact_id="disk-stale", evidence_revision="revision-2",
            status=FactStatus.HEALTHY, freshness=FactFreshness.STALE,
            observed_at=self.clock.now,
        )
        stale = self.service.process(batch(
            "batch-2", "revision-2", (stale_healthy,), collected_at=self.clock.now,
        ))
        self.assertEqual(stale.outcomes, ())
        self.assertEqual(self.tasks.get_task(task_id).evidence_revision,
                         opened.outcomes[0].evidence_revision)

        self.clock.advance()
        incomplete = self.service.process(batch(
            "batch-3", "revision-3", (), collected_at=self.clock.now,
        ))
        self.assertEqual(incomplete.outcomes, ())
        self.assertEqual(len(self.tasks.list_tasks()), 1)

        self.clock.advance()
        healthy = replace(
            disk, fact_id="disk-healthy", evidence_revision="revision-4",
            status=FactStatus.HEALTHY, observed_at=self.clock.now,
            payload=DiskPayload(1000, 500, 0),
        )
        recovered = self.service.process(batch(
            "batch-4", "revision-4", (healthy,), complete=(DetectorKind.DISK,),
            collected_at=self.clock.now,
        ))
        storage = next(item for item in recovered.outcomes if item.domain is CorrelationDomain.STORAGE)
        self.assertEqual(storage.status, "recovered")
        # StateStore intentionally retains its recovery window; fresh facts still win.
        incident = next(item for item in self.state.current_snapshot()["incidents"]
                        if item["dedup_key"] == disk.incident_id)
        self.assertEqual(incident["status"], "recovery_pending")

    def test_out_of_order_current_recovery_cannot_override_newer_fault(self) -> None:
        self.clock.advance(120)
        disk = fact(
            "disk-new", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
            observed_at=self.clock.now,
        )
        opened = self.service.process(batch(
            "batch-new", "revision-1", (disk,), collected_at=self.clock.now,
        ))
        task_id = opened.outcomes[0].task_id
        self.clock.advance(30)
        older_healthy = replace(
            disk, fact_id="disk-old", evidence_revision="revision-2",
            status=FactStatus.HEALTHY, observed_at=disk.observed_at - timedelta(seconds=60),
            payload=DiskPayload(1000, 500, 0),
        )
        result = self.service.process(batch(
            "batch-old", "revision-2", (older_healthy,), collected_at=self.clock.now,
        ))
        self.assertEqual(result.outcomes, ())
        self.assertEqual(len(self.tasks.list_tasks()), 1)
        self.assertEqual(self.tasks.get_task(task_id).evidence_revision,
                         opened.outcomes[0].evidence_revision)
        incident = next(item for item in self.state.current_snapshot()["incidents"]
                        if item["dedup_key"] == disk.incident_id)
        self.assertEqual(incident["status"], "open")

    def test_complete_source_removes_omitted_fault_but_incomplete_source_does_not(self) -> None:
        first = fact(
            "gpu-a1", "revision-1", DetectorKind.GPU_INVENTORY,
            CorrelationDomain.GPU_CAPACITY, "gpu:GPU-A", "missing",
            GpuInventoryPayload(1, 0, ("GPU-A",)),
        )
        second = replace(first, fact_id="gpu-b1", resource_id="gpu:GPU-B",
                         payload=GpuInventoryPayload(1, 0, ("GPU-B",)))
        opened = self.service.process(batch("batch-1", "revision-1", (first, second)))
        original_task = opened.outcomes[0].task_id
        self.clock.advance()
        first_repeat = replace(first, fact_id="gpu-a2", evidence_revision="revision-2",
                               observed_at=self.clock.now)
        incomplete = self.service.process(batch(
            "batch-2", "revision-2", (first_repeat,), collected_at=self.clock.now,
        ))
        self.assertEqual(incomplete.outcomes[0].task_id, original_task)
        self.assertFalse(incomplete.outcomes[0].created)
        self.clock.advance()
        first_repeat = replace(first_repeat, fact_id="gpu-a3", evidence_revision="revision-3",
                               observed_at=self.clock.now)
        complete = self.service.process(batch(
            "batch-3", "revision-3", (first_repeat,),
            complete=(DetectorKind.GPU_INVENTORY,), collected_at=self.clock.now,
        ))
        self.assertTrue(complete.outcomes[0].created)
        self.assertTrue(complete.outcomes[0].material_changed)
        self.assertNotEqual(complete.outcomes[0].task_id, original_task)
        omitted_incident = second.incident_id
        pending = next(item for item in self.state.current_snapshot()["incidents"]
                       if item["dedup_key"] == omitted_incident)
        self.assertEqual(pending["status"], "recovery_pending")
        followup = None
        for number in range(4, 8):
            self.clock.advance(75)
            surviving = replace(
                first_repeat, fact_id=f"gpu-a{number}",
                evidence_revision=f"revision-{number}", observed_at=self.clock.now,
            )
            followup = self.service.process(batch(
                f"batch-{number}", f"revision-{number}", (surviving,),
                complete=(DetectorKind.GPU_INVENTORY,), collected_at=self.clock.now,
            ))
        self.assertIsNotNone(followup)
        self.assertFalse(followup.outcomes[0].created)
        recovered = next(item for item in self.state.current_snapshot()["incidents"]
                         if item["dedup_key"] == omitted_incident)
        self.assertEqual(recovered["status"], "recovered")

    def test_cross_domain_resources_are_separate_and_explicitly_linked(self) -> None:
        gpu = fact(
            "gpu-1", "revision-1", DetectorKind.GPU_INVENTORY,
            CorrelationDomain.GPU_CAPACITY, "gpu:GPU-A", "missing",
            GpuInventoryPayload(8, 7, ("GPU-A",)), related=("machine:17049",),
        )
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
            related=("machine:17049",),
        )
        result = self.service.process(batch("batch-1", "revision-1", (gpu, disk)))
        active = [item for item in result.outcomes if item.status == "active"]
        self.assertEqual(len(active), 2)
        self.assertNotEqual(active[0].task_id, active[1].task_id)
        self.assertEqual(active[0].linked_task_ids, (active[1].task_id,))
        self.assertEqual(active[1].linked_task_ids, (active[0].task_id,))

    def test_new_domain_links_to_unchanged_active_related_domain(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
            related=("machine:17049",),
        )
        first = self.service.process(batch("batch-1", "revision-1", (disk,)))
        storage_task = first.outcomes[0].task_id
        self.clock.advance()
        gpu = fact(
            "gpu-1", "revision-2", DetectorKind.GPU_INVENTORY,
            CorrelationDomain.GPU_CAPACITY, "gpu:GPU-A", "missing",
            GpuInventoryPayload(8, 7, ("GPU-A",)), related=("machine:17049",),
            observed_at=self.clock.now,
        )
        second = self.service.process(batch(
            "batch-2", "revision-2", (gpu,), collected_at=self.clock.now,
        ))
        gpu_task = second.outcomes[0].task_id
        self.assertIn(storage_task, second.outcomes[0].linked_task_ids)
        self.assertEqual(self.service.linked_tasks(storage_task), (gpu_task,))

    def test_restart_durability_and_exact_batch_replay(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        first_batch = batch("batch-1", "revision-1", (disk,))
        first = self.service.process(first_batch)
        task_id = first.outcomes[0].task_id
        replay = self.service.process(first_batch)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.outcomes[0].task_id, task_id)

        self.tasks.close()
        self.state.close()
        self.state = StateStore(self.root, clock=self.clock)
        self.tasks = TaskStore(self.root, clock=self.clock)
        self.addCleanup(self.tasks.close)
        self.addCleanup(self.state.close)
        self.service = IncidentCorrelator(self.state, self.tasks, enabled=True, clock=self.clock)
        self.clock.advance()
        repeated = replace(disk, fact_id="disk-2", evidence_revision="revision-2",
                           observed_at=self.clock.now)
        result = self.service.process(batch(
            "batch-2", "revision-2", (repeated,), collected_at=self.clock.now,
        ))
        self.assertEqual(result.outcomes[0].task_id, task_id)
        self.assertFalse(result.outcomes[0].created)
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_v1_active_heads_migrate_to_durable_accepted_observations(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        self.service.process(batch("batch-1", "revision-1", (disk,)))
        with self.tasks.transaction():
            for table in (
                "tc_state_projections", "tc_detector_fact_rejections",
                "tc_detector_complete_watermarks", "tc_detector_acceptances",
                "tc_detector_observations",
            ):
                self.tasks.db.execute(f"DROP TABLE {table}")
            self.tasks.db.execute(
                "UPDATE tc_correlation_schema SET version=1 WHERE namespace='correlation'"
            )
        self.service = IncidentCorrelator(
            self.state, self.tasks, enabled=True, clock=self.clock,
        )
        row = self.tasks.db.execute(
            """SELECT o.status,o.source_instance,a.fact_id
               FROM tc_detector_observations o
               JOIN tc_detector_acceptances a ON a.fact_id=o.fact_id
               WHERE o.identity_key=?""", (disk.failure_identity,),
        ).fetchone()
        self.assertEqual(dict(row), {
            "status": "fault", "source_instance": "boot-1", "fact_id": "disk-1",
        })

    def test_incident_link_event_updates_task_snapshot_idempotently(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        opened = self.service.process(batch("batch-1", "revision-1", (disk,)))
        task_id = opened.outcomes[0].task_id
        self.clock.advance()
        original_task = self.tasks.get_task(task_id)
        creation = self.tasks.events(task_id)[0]
        linked_incident = "linked-incident"
        event = TaskEvent(
            event_id="incident-linked:test", task_id=task_id, sequence=2,
            event_type="incident-linked", actor_id="phase6-correlator",
            occurred_at=self.clock.now, from_state=self.tasks.get_task(task_id).state,
            to_state=self.tasks.get_task(task_id).state,
            payload={"machine_id": "17049"},
            evidence_revision=opened.outcomes[0].evidence_revision,
            incident_id=linked_incident,
        )
        with self.assertRaises(IllegalTransition):
            self.tasks.append_event(event)
        # The correlator must durably validate the incident-task relationship
        # before TaskStore will permit snapshot growth.
        with self.tasks.transaction():
            self.tasks.db.execute(
                """INSERT INTO tc_correlation_evaluations(
                     batch_id,domain,status,task_created,material_changed,material_digest,
                     evidence_revision,task_id,fact_ids_json,incident_ids_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("batch-1", CorrelationDomain.NETWORK.value, "active", 0, 0,
                 "validated-link", opened.outcomes[0].evidence_revision, task_id,
                 "[]", f'["{linked_incident}"]'),
            )
        self.assertTrue(self.tasks.append_event(event))
        self.assertFalse(self.tasks.append_event(event))
        self.assertEqual(self.tasks.get_task(task_id).incident_ids.count(linked_incident), 1)
        self.assertFalse(self.tasks.create_task(original_task, creation))
        with self.assertRaises(TaskConflict):
            self.tasks.create_task(
                replace(original_task, incident_ids=("arbitrary-incident",)), creation,
            )

    def test_stale_or_unknown_complete_batch_cannot_clear_or_recover(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        self.service.process(batch("batch-1", "revision-1", (disk,)))
        self.clock.advance()
        stale = replace(
            disk, fact_id="disk-stale", evidence_revision="revision-2",
            freshness=FactFreshness.STALE, status=FactStatus.HEALTHY,
            observed_at=self.clock.now,
        )
        self.service.process(batch(
            "batch-2", "revision-2", (stale,), complete=(DetectorKind.DISK,),
            collected_at=self.clock.now,
        ))
        self.assertEqual(
            self.tasks.db.execute("SELECT count(*) FROM tc_detector_heads").fetchone()[0], 1
        )
        self.assertIsNone(self.tasks.db.execute(
            "SELECT 1 FROM tc_detector_complete_watermarks WHERE kind='disk'"
        ).fetchone())
        self.clock.advance()
        unknown = replace(
            disk, fact_id="disk-unknown", evidence_revision="revision-3",
            freshness=FactFreshness.CURRENT, status=FactStatus.UNKNOWN,
            observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-3", "revision-3", (unknown,), complete=(DetectorKind.DISK,),
            collected_at=self.clock.now,
        ))
        self.assertEqual(result.outcomes[0].status, "active")
        self.assertEqual(
            self.tasks.db.execute("SELECT count(*) FROM tc_detector_heads").fetchone()[0], 1
        )
        incident = next(item for item in self.state.current_snapshot()["incidents"]
                        if item["dedup_key"] == disk.incident_id)
        self.assertEqual(incident["status"], "open")

    def test_dynamic_domain_changes_re_evaluate_old_and_new_domains(self) -> None:
        collector = fact(
            "collector-1", "revision-1", DetectorKind.COLLECTOR,
            CorrelationDomain.GPU_CAPACITY, "collector:probe", "stale",
            CollectorPayload(100, 60, None),
        )
        self.service.process(batch("batch-1", "revision-1", (collector,)))
        self.clock.advance()
        moved = replace(
            collector, fact_id="collector-2", evidence_revision="revision-2",
            domain=CorrelationDomain.CONTROLLER, observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-2", "revision-2", (moved,), collected_at=self.clock.now,
        ))
        self.assertEqual(
            {(item.domain, item.status) for item in result.outcomes},
            {(CorrelationDomain.GPU_CAPACITY, "recovered"),
             (CorrelationDomain.CONTROLLER, "active")},
        )
        self.clock.advance()
        unknown = replace(
            moved, fact_id="collector-3", evidence_revision="revision-3",
            domain=CorrelationDomain.NETWORK, status=FactStatus.UNKNOWN,
            observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-3", "revision-3", (unknown,), collected_at=self.clock.now,
        ))
        self.assertEqual(
            {(item.domain, item.status) for item in result.outcomes},
            {(CorrelationDomain.CONTROLLER, "active"),
             (CorrelationDomain.NETWORK, "quiet")},
        )
        self.clock.advance()
        healthy = replace(
            unknown, fact_id="collector-4", evidence_revision="revision-4",
            domain=CorrelationDomain.BACKUP, status=FactStatus.HEALTHY,
            observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-4", "revision-4", (healthy,), collected_at=self.clock.now,
        ))
        self.assertEqual(
            {(item.domain, item.status) for item in result.outcomes},
            {(CorrelationDomain.NETWORK, "quiet"),
             (CorrelationDomain.CONTROLLER, "recovered"),
             (CorrelationDomain.BACKUP, "quiet")},
        )

    def test_accepted_healthy_blocks_late_and_equal_time_fault_resurrection(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        self.service.process(batch("batch-1", "revision-1", (disk,)))
        self.clock.advance(10)
        healthy = replace(
            disk, fact_id="disk-healthy", evidence_revision="revision-2",
            status=FactStatus.HEALTHY, observed_at=self.clock.now,
            payload=DiskPayload(1000, 500, 0),
        )
        self.service.process(batch(
            "batch-2", "revision-2", (healthy,), collected_at=self.clock.now,
        ))
        self.clock.advance()
        equal_fault = replace(
            disk, fact_id="disk-equal", evidence_revision="revision-3",
            observed_at=healthy.observed_at,
        )
        result = self.service.process(batch(
            "batch-3", "revision-3", (equal_fault,), collected_at=self.clock.now,
        ))
        self.assertEqual(result.outcomes, ())
        self.assertEqual(tuple(item.fact_id for item in result.rejections), ("disk-equal",))
        row = self.tasks.db.execute(
            "SELECT status FROM tc_detector_observations WHERE identity_key=?",
            (disk.failure_identity,),
        ).fetchone()
        self.assertEqual(row["status"], "healthy")
        self.assertEqual(
            self.tasks.db.execute("SELECT count(*) FROM tc_detector_heads").fetchone()[0], 0
        )

    def test_source_instance_clock_reset_is_material_and_retired_instance_cannot_return(self) -> None:
        self.clock.advance(100)
        xid = fact(
            "xid-1", "revision-1", DetectorKind.NVIDIA_XID,
            CorrelationDomain.GPU_CAPACITY, "gpu:GPU-A", "xid-79",
            NvidiaXidPayload(79, "GPU-A"), observed_at=self.clock.now,
            source_instance="driver-1",
        )
        first = self.service.process(batch(
            "batch-1", "revision-1", (xid,), collected_at=self.clock.now,
        ))
        self.clock.advance()
        restarted = replace(
            xid, fact_id="xid-2", evidence_revision="revision-2",
            source_instance="driver-2", observed_at=NOW,
        )
        second = self.service.process(batch(
            "batch-2", "revision-2", (restarted,), collected_at=self.clock.now,
        ))
        self.assertTrue(second.outcomes[0].created)
        self.assertTrue(second.outcomes[0].material_changed)
        self.assertNotEqual(first.outcomes[0].task_id, second.outcomes[0].task_id)
        self.clock.advance()
        late_old = replace(
            xid, fact_id="xid-3", evidence_revision="revision-3",
            observed_at=self.clock.now,
        )
        rejected = self.service.process(batch(
            "batch-3", "revision-3", (late_old,), collected_at=self.clock.now,
        ))
        self.assertEqual(tuple(item.fact_id for item in rejected.rejections), ("xid-3",))
        head = self.service._head_fact(xid.failure_identity)
        self.assertEqual(head.source_instance, "driver-2")

    def test_older_complete_batch_cannot_clear_newer_head_or_watermark(self) -> None:
        self.clock.advance(30)
        disk = fact(
            "disk-new", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
            observed_at=self.clock.now,
        )
        self.service.process(batch(
            "batch-new", "revision-1", (disk,), collected_at=self.clock.now,
        ))
        older_time = self.clock.now - timedelta(seconds=10)
        self.service.process(batch(
            "batch-old-complete", "revision-2", (), complete=(DetectorKind.DISK,),
            collected_at=older_time,
        ))
        self.assertIsNotNone(self.service._head_fact(disk.failure_identity))
        self.clock.advance()
        self.service.process(batch(
            "batch-new-complete", "revision-3", (), complete=(DetectorKind.DISK,),
            collected_at=self.clock.now,
        ))
        watermark = self.tasks.db.execute(
            "SELECT batch_id FROM tc_detector_complete_watermarks WHERE kind='disk'"
        ).fetchone()
        self.assertEqual(watermark["batch_id"], "batch-new-complete")
        self.service.process(batch(
            "batch-late-old", "revision-4", (), complete=(DetectorKind.DISK,),
            collected_at=older_time + timedelta(seconds=1),
        ))
        watermark = self.tasks.db.execute(
            "SELECT batch_id FROM tc_detector_complete_watermarks WHERE kind='disk'"
        ).fetchone()
        self.assertEqual(watermark["batch_id"], "batch-new-complete")

    def test_complete_absence_watermark_blocks_late_fault_resurrection(self) -> None:
        self.clock.advance(100)
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
            observed_at=self.clock.now,
        )
        self.service.process(batch(
            "batch-1", "revision-1", (disk,), collected_at=self.clock.now,
        ))
        self.clock.advance(10)
        cleared_at = self.clock.now
        self.service.process(batch(
            "batch-2", "revision-2", (), complete=(DetectorKind.DISK,),
            collected_at=cleared_at,
        ))
        self.assertIsNone(self.service._head_fact(disk.failure_identity))

        self.clock.advance()
        late = replace(
            disk, fact_id="disk-late", evidence_revision="revision-3",
            observed_at=cleared_at - timedelta(seconds=5),
        )
        result = self.service.process(batch(
            "batch-3", "revision-3", (late,), collected_at=self.clock.now,
        ))
        self.assertEqual(tuple(item.fact_id for item in result.rejections), ("disk-late",))
        self.assertIsNone(self.service._head_fact(disk.failure_identity))
        observation = self.tasks.db.execute(
            "SELECT status,observed_utc FROM tc_detector_observations WHERE identity_key=?",
            (disk.failure_identity,),
        ).fetchone()
        self.assertEqual(observation["status"], "healthy")
        self.assertEqual(
            observation["observed_utc"],
            cleared_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

    def test_temporal_conflict_isolated_from_unrelated_fact(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        self.service.process(batch("batch-1", "revision-1", (disk,)))
        self.clock.advance()
        conflict = replace(
            disk, fact_id="disk-conflict", evidence_revision="revision-2",
            status=FactStatus.HEALTHY, payload=DiskPayload(1000, 500, 0),
        )
        network = fact(
            "network-1", "revision-2", DetectorKind.NETWORK,
            CorrelationDomain.NETWORK, "network:uplink", "unreachable",
            NetworkPayload(False, "enp1s0", True, True), observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-2", "revision-2", (conflict, network), collected_at=self.clock.now,
        ))
        self.assertEqual(tuple(item.fact_id for item in result.rejections), ("disk-conflict",))
        self.assertTrue(any(item.domain is CorrelationDomain.NETWORK and item.created
                            for item in result.outcomes))
        self.assertIsNotNone(self.service._head_fact(disk.failure_identity))

    def test_state_projection_failure_is_visible_and_retryable_without_task_rollback(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        original = self.state.record_observation

        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic projection failure")

        self.state.record_observation = fail
        first_batch = batch("batch-1", "revision-1", (disk,))
        result = self.service.process(first_batch)
        self.assertTrue(result.outcomes[0].created)
        self.assertEqual(len(self.tasks.list_tasks()), 1)
        self.assertEqual(self.state.current_snapshot()["incidents"], [])
        self.assertEqual(self.service.projection_statuses()[0]["status"], "failed")
        self.state.record_observation = original
        replay = self.service.process(first_batch)
        self.assertTrue(replay.replay)
        self.assertEqual(self.service.projection_statuses()[0]["status"], "applied")
        self.assertEqual(len(self.state.current_snapshot()["incidents"]), 1)
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_superseded_failed_fault_projection_cannot_reopen_healthy_truth(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        original = self.state.record_observation

        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic projection failure")

        self.state.record_observation = fail
        self.service.process(batch("batch-1", "revision-1", (disk,)))
        self.state.record_observation = original
        self.clock.advance()
        healthy = replace(
            disk, fact_id="disk-healthy", evidence_revision="revision-2",
            status=FactStatus.HEALTHY, observed_at=self.clock.now,
            payload=DiskPayload(1000, 500, 0),
        )
        self.service.process(batch(
            "batch-2", "revision-2", (healthy,), collected_at=self.clock.now,
        ))
        self.service.replay_state_projections("batch-1")
        self.assertEqual(self.state.current_snapshot()["incidents"], [])
        statuses = {item["batch_id"]: item["status"]
                    for item in self.service.projection_statuses()}
        self.assertEqual(statuses, {"batch-1": "applied", "batch-2": "applied"})

    def test_concurrent_disjoint_batches_preserve_both_authoritative_heads(self) -> None:
        disk = fact(
            "disk-concurrent", "revision-disk", DetectorKind.DISK,
            CorrelationDomain.STORAGE, "disk:root", "pressure",
            DiskPayload(1000, 40, 0),
        )
        network = fact(
            "network-concurrent", "revision-network", DetectorKind.NETWORK,
            CorrelationDomain.NETWORK, "network:uplink", "unreachable",
            NetworkPayload(False, "enp1s0", True, True),
        )
        work = (
            batch("batch-disk", "revision-disk", (disk,)),
            batch("batch-network", "revision-network", (network,)),
        )
        gate = threading.Barrier(2)
        errors: list[BaseException] = []

        def run(item: DetectorBatch) -> None:
            state = StateStore(self.root, clock=self.clock)
            tasks = TaskStore(self.root, clock=self.clock)
            try:
                service = IncidentCorrelator(state, tasks, enabled=True, clock=self.clock)
                gate.wait()
                service.process(item)
            except BaseException as error:
                errors.append(error)
            finally:
                tasks.close()
                state.close()

        threads = [threading.Thread(target=run, args=(item,)) for item in work]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            self.tasks.db.execute("SELECT count(*) FROM tc_detector_heads").fetchone()[0], 2
        )
        self.assertEqual(len(self.tasks.list_tasks()), 2)

    def test_terminal_task_does_not_suppress_unchanged_active_fault(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        opened = self.service.process(batch("batch-1", "revision-1", (disk,)))
        old_task_id = opened.outcomes[0].task_id
        self.clock.advance()
        old_task = self.tasks.get_task(old_task_id)
        self.tasks.append_event(TaskEvent(
            event_id="task-failed:test", task_id=old_task_id, sequence=2,
            event_type="task-failed", actor_id="test", occurred_at=self.clock.now,
            from_state=old_task.state, to_state=TaskState.FAILED, payload={},
            evidence_revision=old_task.evidence_revision,
        ))
        self.clock.advance()
        repeated = replace(
            disk, fact_id="disk-2", evidence_revision="revision-2",
            observed_at=self.clock.now,
        )
        result = self.service.process(batch(
            "batch-2", "revision-2", (repeated,), collected_at=self.clock.now,
        ))
        self.assertTrue(result.outcomes[0].created)
        self.assertNotEqual(result.outcomes[0].task_id, old_task_id)
        self.assertIn(old_task_id, result.outcomes[0].linked_task_ids)

    def test_disabled_by_default_is_a_noop(self) -> None:
        disk = fact(
            "disk-1", "revision-1", DetectorKind.DISK, CorrelationDomain.STORAGE,
            "disk:root", "pressure", DiskPayload(1000, 40, 0),
        )
        disabled = IncidentCorrelator(self.state, self.tasks, clock=self.clock)
        result = disabled.process(batch("batch-disabled", "revision-1", (disk,)))
        self.assertFalse(result.enabled)
        self.assertEqual(self.tasks.list_tasks(), ())
        self.assertEqual(self.state.current_snapshot()["incidents"], [])


class ContractAndPolicyCase(unittest.TestCase):
    def test_every_required_detector_has_a_strict_round_trip_payload(self) -> None:
        samples = {
            DetectorKind.COLLECTOR: (CorrelationDomain.CONTROLLER, CollectorPayload(5, 60, None)),
            DetectorKind.GPU_INVENTORY: (CorrelationDomain.GPU_CAPACITY, GpuInventoryPayload(8, 8)),
            DetectorKind.NVIDIA_XID: (CorrelationDomain.GPU_CAPACITY, NvidiaXidPayload(79, "GPU-A")),
            DetectorKind.EXPORTER: (CorrelationDomain.GPU_CAPACITY, ExporterPayload(True, 0, 300, 3)),
            DetectorKind.MARKET_CAPACITY: (CorrelationDomain.GPU_CAPACITY, MarketCapacityPayload(1, 1, 7, 8, True)),
            DetectorKind.DISK: (CorrelationDomain.STORAGE, DiskPayload(1000, 500, 0)),
            DetectorKind.MEMORY_OOM: (CorrelationDomain.MEMORY, MemoryOomPayload(1000, 500, 0)),
            DetectorKind.NETWORK: (CorrelationDomain.NETWORK, NetworkPayload(True, "enp1s0", True, True)),
            DetectorKind.BACKUP: (CorrelationDomain.BACKUP, BackupPayload(60, 3600, True)),
            DetectorKind.CONTROLLER_SERVICE: (CorrelationDomain.CONTROLLER, ControllerServicePayload(True, 0, 3)),
            DetectorKind.NIX_GENERATION: (CorrelationDomain.SYSTEM_GENERATION, NixGenerationPayload("42", "42")),
        }
        self.assertEqual(set(samples), set(DetectorKind))
        for index, (kind, (domain, payload)) in enumerate(samples.items()):
            item = fact(f"fact-{index}", "revision-1", kind, domain,
                        f"component:item-{index}", "health", payload,
                        status=FactStatus.HEALTHY)
            self.assertEqual(DetectorFact.from_json(item.canonical_json()), item)
            document = item.to_document()
            document["unexpected"] = True
            with self.assertRaises(ContractError):
                DetectorFact.from_document(document)
        with self.assertRaises(ContractError):
            replace(next(iter([fact("bad", "revision-1", DetectorKind.DISK,
                                    CorrelationDomain.STORAGE, "disk:root", "pressure",
                                    DiskPayload(1000, 500, 0))])), machine_id="999")

    def test_standing_consent_requires_owned_routine_metadata_and_phase3_predicates(self) -> None:
        owned_fact = fact(
            "controller-1", "revision-1", DetectorKind.CONTROLLER_SERVICE,
            CorrelationDomain.CONTROLLER, "controller:collector", "restart-loop",
            ControllerServicePayload(False, 4, 3),
            ownership=ResourceOwnership.OWNED_COMPONENT, routine=True,
        )
        artifact = "sha256:" + "ab" * 32

        def make_plan(task_id: str, evidence_revision: str, **effect_flags: bool) -> Plan:
            resource = "service:collector"
            step = PlanStep(
                step_id="step-1", operation="restart_owned_unit",
                arguments={"unit": "collector.service"},
                effects=(Effect("effect-1", "repair", **effect_flags),),
                affected_resources=(resource,),
                preconditions=({"check": "identity", "machine_id": "17049"},),
                postconditions=({"kind": "resource_fields_equal", "resource": resource,
                                 "expected": {"active": True}},),
                checkpoint={"kind": "resource_snapshot", "resources": [resource],
                            "fields": ["active"], "artifact": artifact},
                rollback={"kind": "restore_checkpoint", "artifact": artifact,
                          "resources": [resource]},
                rollback_impossible_reason=None, expected_interruption="none",
                max_execution_seconds=60, artifacts=(artifact,),
            )
            return Plan(
                plan_id="plan-1", task_id=task_id, version=1, objective="repair",
                evidence_revision=evidence_revision, assumptions=(),
                freshness_requirements=({"source": "detectors", "max_age_seconds": 60},),
                steps=(step,), created_at=NOW, expires_at=NOW + timedelta(hours=1),
            )

        grant = StandingEffectGrant(
            grant_id="standing-1", policy_revision="policy-1", description="owned repairs",
            effect_classes=frozenset({"owned_component"}), max_step_seconds=300,
            max_affected_resources=2, max_uses=10, rate_window_seconds=3600,
            issued_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(hours=1),
        )

        class Rate:
            def uses_since(self, _grant_id, _since):
                return 0

            def record_use(self, _grant_id, _plan_hash, _step_id, _at):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local_clock = Clock()
            state = StateStore(root, clock=local_clock)
            tasks = TaskStore(root, clock=local_clock)
            try:
                service = IncidentCorrelator(
                    state, tasks, enabled=True, clock=local_clock,
                )
                opened = service.process(batch("batch-1", "revision-1", (owned_fact,)))
                outcome = opened.outcomes[0]
                options = dict(
                    correlator=service, grants=(grant,), policy_revision="policy-1", now=NOW,
                    classifier=lambda _token: False, rate_accountant=Rate(),
                )
                decision = authorize_correlation_plan(
                    make_plan(outcome.task_id, outcome.evidence_revision,
                              owned_component=True), **options,
                )
                self.assertEqual(decision.plan_authority, Authority.STANDING_CONSENT)

                for forbidden in (
                    "host", "reachability", "tenant", "external_commitment",
                    "secrets", "irreversible",
                ):
                    flags = {"owned_component": True, forbidden: True}
                    decision = authorize_correlation_plan(
                        make_plan(outcome.task_id, outcome.evidence_revision, **flags),
                        **options,
                    )
                    expected = (Authority.ALWAYS_APPROVE_TENANT if forbidden == "tenant"
                                else Authority.EXACT_HUMAN)
                    self.assertEqual(decision.plan_authority, expected, forbidden)

                non_owned = replace(
                    owned_fact, fact_id="host-1", resource_id="controller:host-path",
                    failure_id="host-failure", ownership=ResourceOwnership.HOST,
                    routine_reversible=False,
                )
                changed = service.process(batch("batch-2", "revision-1", (non_owned,)))
                current = changed.outcomes[0]
                decision = authorize_correlation_plan(
                    make_plan(current.task_id, current.evidence_revision,
                              owned_component=True), **options,
                )
                self.assertEqual(decision.plan_authority, Authority.EXACT_HUMAN)
                with self.assertRaises(ContractError):
                    authorize_correlation_plan(
                        make_plan(outcome.task_id, outcome.evidence_revision,
                                  owned_component=True), **options,
                    )

                local_clock.advance()
                unknown = replace(
                    owned_fact, fact_id="controller-unknown",
                    evidence_revision="revision-2", status=FactStatus.UNKNOWN,
                    observed_at=local_clock.now,
                )
                unknown_result = service.process(batch(
                    "batch-3", "revision-2", (unknown,), collected_at=local_clock.now,
                ))
                unknown_outcome = next(
                    item for item in unknown_result.outcomes
                    if item.domain is CorrelationDomain.CONTROLLER
                )
                with self.assertRaisesRegex(ContractError, "newer non-fault"):
                    authorize_correlation_plan(
                        make_plan(unknown_outcome.task_id, unknown_outcome.evidence_revision,
                                  owned_component=True),
                        **{**options, "now": local_clock.now},
                    )

                local_clock.advance()
                current_fault = replace(
                    owned_fact, fact_id="controller-current",
                    evidence_revision="revision-3", observed_at=local_clock.now,
                )
                current_result = service.process(batch(
                    "batch-4", "revision-3", (current_fault,),
                    collected_at=local_clock.now,
                ))
                current_outcome = next(
                    item for item in current_result.outcomes
                    if item.domain is CorrelationDomain.CONTROLLER
                )
                local_clock.advance(301)
                with self.assertRaisesRegex(ContractError, "freshness bound"):
                    authorize_correlation_plan(
                        make_plan(current_outcome.task_id, current_outcome.evidence_revision,
                                  owned_component=True),
                        **{**options, "now": local_clock.now},
                    )
            finally:
                tasks.close()
                state.close()

        self.assertFalse(derive_repair_eligibility((
            replace(owned_fact, status=FactStatus.UNKNOWN),
        )).eligible)
        with self.assertRaises(ContractError):
            fact(
                "owned-disk", "revision-1", DetectorKind.DISK,
                CorrelationDomain.STORAGE, "disk:root", "pressure",
                DiskPayload(1000, 40, 0),
                ownership=ResourceOwnership.OWNED_COMPONENT, routine=True,
            )
        with self.assertRaises(ContractError):
            fact(
                "owned-host-path", "revision-1", DetectorKind.CONTROLLER_SERVICE,
                CorrelationDomain.CONTROLLER, "disk:root", "restart-loop",
                ControllerServicePayload(False, 4, 3),
                ownership=ResourceOwnership.OWNED_COMPONENT, routine=True,
            )


if __name__ == "__main__":
    unittest.main()
