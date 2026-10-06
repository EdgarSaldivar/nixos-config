from __future__ import annotations

import copy
import json
import hashlib
import sqlite3
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

from terracompute_ops.actions import (
    ActionBroker,
    ApprovalKind,
    EventAuthentication,
    HumanApprovalEvent,
    MembershipDecision,
)
from terracompute_ops.monitor_restart import (
    COMPONENT,
    ActorError,
    SSHActorClient,
    EvidenceStore,
    MonitorRestartAdapter,
    build_proposal,
    evidence_revision,
    fault_revision,
    handover_incident_signature,
    parse_status,
    postcondition,
)
from terracompute_ops.incidents import stable_signature
from terracompute_ops.policy import ActionClass, ActionPolicy, Mode, PolicyDenied

START = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
GROUP = -1004484415005
BDF = "0000:a1:00.0"
IDS = iter(f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 10_000))
TENANT_DIGEST = "a" * 64


class BoundSessionTransportTests(unittest.TestCase):
    def client(self):
        return SSHActorClient(ssh_binary="/usr/bin/ssh", target="actor@example.com",
            identity_file=Path("/run/identity"), known_hosts_file=Path("/run/known-hosts"))

    def test_old_helper_refusal_is_never_retried_as_unbound_writable_session(self):
        calls = []
        def old_helper(argv, timeout, stdin_bytes=None):
            calls.append((argv[-1], stdin_bytes))
            return {"schema_version": 1, "ok": False, "reason": "invalid_request"}
        with mock.patch("terracompute_ops.monitor_restart._run_bounded_json", old_helper):
            with self.assertRaises(ActorError):
                self.client().session("echo ${x}",
                    "00000000-0000-4000-8000-000000000001", writable=True,
                    expected_boot_id="11111111-2222-4333-8444-555555555555")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0],
            "session-v2 host 00000000-0000-4000-8000-000000000001 "
            "11111111-2222-4333-8444-555555555555")
        self.assertEqual(calls[0][1], b"echo $${x}")

    def test_restart_requires_boot_and_never_downgrades_for_an_old_helper(self):
        ticket = '00000000-0000-4000-8000-000000000001'
        boot = '11111111-2222-4333-8444-555555555555'
        calls = []
        def peer(argv, timeout):
            calls.append(argv[-1])
            return {'schema_version': 1, 'ok': False, 'reason': 'invalid_request'}
        with mock.patch('terracompute_ops.monitor_restart._run_bounded_json', peer):
            with self.assertRaises(ValueError):
                self.client().run('restart', ticket)
            self.assertEqual(calls, [])
            with self.assertRaises(ActorError):
                self.client().run('restart', ticket, expected_boot_id=boot)
        self.assertEqual(calls, [f'restart-v2 dcgm-exporter {ticket} {boot}'])

    def test_restart_accepts_only_boot_bound_helper_capability(self):
        ticket = '00000000-0000-4000-8000-000000000001'
        boot = '11111111-2222-4333-8444-555555555555'
        response = {'schema_version': 1, 'operation': 'restart', 'ok': True,
                    'restart_capability': 'boot-bound-v2'}
        with mock.patch('terracompute_ops.monitor_restart._run_bounded_json', return_value=response):
            self.assertEqual(self.client().run('restart', ticket, expected_boot_id=boot), response)

    def test_read_only_observe_keeps_three_token_legacy_grammar(self):
        calls = []
        def old_helper(argv, timeout, stdin_bytes=None):
            calls.append(argv[-1])
            return {"schema_version": 1, "operation": "observe", "ok": True}
        with mock.patch("terracompute_ops.monitor_restart._run_bounded_json", old_helper):
            self.assertTrue(self.client().session("true",
                "00000000-0000-4000-8000-000000000001")["ok"])
        self.assertEqual(calls, ["observe host 00000000-0000-4000-8000-000000000001"])


def tenant_doc(names, digest=TENANT_DIGEST, started=None) -> dict:
    """Tenants with stable per-name container IDs; ``started`` overrides start times."""
    started = started or {}
    members = [
        {
            "name": name,
            "id": hashlib.sha256(name.encode()).hexdigest(),
            "started_at": started.get(name, "2026-09-15T02:47:34Z"),
        }
        for name in sorted(names)
    ]
    return {"count": len(members), "digest": digest, "names": sorted(names), "members": members}


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def status_document(request_id: str, **changes) -> dict:
    document = {
        "schema_version": 1,
        "operation": "status",
        "id": request_id,
        "component": COMPONENT,
        "machine_id": 17049,
        "ok": True,
        "observed_at": "2026-09-17T06:00:00Z",
        "hostname": "terracompute",
        "board": "ROME2D32GM-2T",
        "boot_id": "f183fc28-44d0-4e80-a294-1da917fd1a76",
        "container": {
            "present": True, "running": True, "started_at": "2026-09-15T02:47:18Z",
            "image": "jjziets/dcgm-exporter:latest", "runtime": "nvidia",
        },
        "handover_blocked": [BDF],
        "nvidia_visible_count": 7,
        "pci_gpu_count": 8,
        "tenants": tenant_doc(["C.50352859", "C.51137407", "C.51217040", "C.51265315"], TENANT_DIGEST),
        "vm_containers": ["C.51217040"],
    }
    for key, value in changes.items():
        document[key] = value
    return document


BOOT_ID = "f183fc28-44d0-4e80-a294-1da917fd1a76"
EXPORTER_STARTED = "2026-09-15T02:47:18Z"


def utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class FakeActor:
    """The helper protocol as the real target helper speaks it, with a ledger."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.status_changes: dict = {}
        self.restarted = False
        self.restart_document: dict | None = None
        self.restarted_at: str | None = None
        self.ledger: dict[str, dict] = {}

    def envelope(self, operation: str, request_id: str, *, expected_boot_id=None) -> dict:
        return {
            "schema_version": 1, "operation": operation, "id": request_id,
            "component": COMPONENT, "machine_id": 17049, "observed_at": utc_text(self.clock()),
            "hostname": "terracompute", "board": "ROME2D32GM-2T", "boot_id": BOOT_ID,
        }

    def run(self, operation: str, request_id: str, *, expected_boot_id=None) -> dict:
        self.calls.append((operation, request_id))
        if operation == "status":
            observed = utc_text(self.clock())
            changes = dict(self.status_changes, observed_at=observed)
            if self.restarted:
                changes.setdefault("container", {
                    "present": True, "running": True, "started_at": self.restarted_at,
                    "image": "jjziets/dcgm-exporter:latest", "runtime": "nvidia",
                })
                changes.setdefault("handover_blocked", [])
                changes.setdefault("tenants", tenant_doc(["C.50352859", "C.51137407", "C.51265315"], "b" * 64))
                changes.setdefault("vm_containers", [])
            return status_document(request_id, **changes)
        if operation == "restart":
            now = utc_text(self.clock())
            record = {
                "state": "executed", "ok": True, "requested_at": now, "completed_at": now,
                "execution_boot_id": BOOT_ID, "started_at_before": EXPORTER_STARTED,
                "started_at_after": now,
                **(self.restart_document or {}),
            }
            if record.pop("restart_ran", record["state"] == "executed"):
                self.restarted = True
                self.restarted_at = now
            self.ledger[request_id] = record
            return {**self.envelope("restart", request_id), **record}
        if request_id not in self.ledger:
            # Like the helper: an unclaimed ID is recorded as never started.
            now = utc_text(self.clock())
            self.ledger[request_id] = {
                "state": "refused", "ok": False, "reason": "execution_not_started",
                "requested_at": None, "completed_at": now, "execution_boot_id": BOOT_ID,
            }
        return {**self.envelope("result", request_id), **self.ledger[request_id]}


class Membership:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def verify(self, group_id: int, user_id: int) -> MembershipDecision:
        return MembershipDecision(group_id, user_id, True, True, True, self.clock())


class Authenticator:
    def authenticate(self, event: HumanApprovalEvent) -> EventAuthentication:
        return EventAuthentication(event.event_id, event.group_id, event.user_id, True)


class MonitorRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.db = sqlite3.connect(":memory:")
        self.actor = FakeActor(self.clock)
        self.evidence = EvidenceStore(self.db, self.clock)
        self.backup_refs: dict[str, str] = {}
        self.adapter = MonitorRestartAdapter(
            self.actor,
            self.evidence,
            backup_ref=lambda proposal: self.backup_refs.get(proposal.proposal_id),
            request_id_factory=lambda: next(IDS),
            clock=self.clock,
        )
        self.policy = ActionPolicy(
            mode=Mode.APPROVE,
            revision="monitor-restart-r1",
            enabled_actions=frozenset({ActionClass.MONITOR_COMPONENT_RESTART}),
            approval_group_id=GROUP,
        )
        self.broker = ActionBroker(
            self.db,
            policy=self.policy,
            membership=Membership(self.clock),
            authenticator=Authenticator(),
            adapter=self.adapter,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.db.close()

    def proposal(self):
        status = self.adapter.status()
        proposal = build_proposal(
            status, BDF, policy_revision=self.policy.revision, clock=self.clock
        )
        self.broker.submit_proposal(proposal)
        self.backup_refs[proposal.proposal_id] = "terracompute-backup@1789620000"
        return proposal

    def approve(self, proposal) -> None:
        self.broker.record_human_event(HumanApprovalEvent(
            event_id=f"telegram:test:{proposal.proposal_id}",
            kind=ApprovalKind.APPROVE,
            group_id=GROUP,
            user_id=4242,
            display_name="telegram:4242",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            nonce=f"nonce-{proposal.proposal_id}",
            occurred_at=self.clock(),
        ))

    def test_a_failed_evidence_write_does_not_leave_a_stale_transaction(self) -> None:
        class BusyOnce(sqlite3.Connection):
            busy = False

            def commit(self) -> None:
                if self.busy:
                    self.busy = False
                    raise sqlite3.OperationalError("database is locked")
                super().commit()

        db = sqlite3.connect(":memory:", factory=BusyOnce)
        self.addCleanup(db.close)
        store = EvidenceStore(db, self.clock)
        db.busy = True
        with self.assertRaises(sqlite3.OperationalError):
            store.record("proposal-status", "cycle:1", {"a": 1})
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM tc_action_evidence").fetchone()[0], 0)

    def test_preserved_evidence_is_used_only_when_it_still_matches_its_reference(self) -> None:
        status = self.adapter.status()
        from terracompute_ops.monitor_restart import _status_document, status_from_evidence
        ref = self.evidence.record("preflight-status", "mr-000000000001", _status_document(status))
        document = self.evidence.verified(ref, "preflight-status", "mr-000000000001")
        self.assertEqual(status_from_evidence(document), status)
        self.assertIsNone(self.evidence.verified(ref, "preflight-status", "mr-000000000002"))
        self.assertIsNone(self.evidence.verified(ref, "postflight-status", "mr-000000000001"))
        # Another role rewriting the shared row cannot change what the reference proves.
        forged = dict(_status_document(status), vm_containers=[])
        self.db.execute(
            "UPDATE tc_action_evidence SET document_json=? WHERE ref=?",
            (json.dumps(forged, sort_keys=True, separators=(",", ":")).encode(), ref),
        )
        self.db.commit()
        self.assertIsNone(self.evidence.verified(ref, "preflight-status", "mr-000000000001"))

    def test_status_parsing_is_strict(self) -> None:
        good = status_document("00000000-0000-4000-8000-000000000001")
        self.assertEqual(parse_status(good, good["id"]).handover_blocked, (BDF,))
        for mutate in (
            lambda d: d.update(id="00000000-0000-4000-8000-000000000002"),
            lambda d: d.update(component="cadvisor"),
            lambda d: d.update(machine_id=17050),
            lambda d: d.update(ok=False),
            lambda d: d.update(handover_blocked=["0000:e1:00.0", BDF]),
            lambda d: d["tenants"].update(count=3),
            lambda d: d["tenants"].update(names=["C.1;rm"]),
            lambda d: d.update(observed_at="2026-09-17 06:00:00"),
            lambda d: d.update(nvidia_visible_count=True),
        ):
            with self.subTest(mutate=mutate):
                document = copy.deepcopy(good)
                mutate(document)
                with self.assertRaises(ActorError):
                    parse_status(document, good["id"])

    def test_evidence_revision_binds_every_approved_fact(self) -> None:
        base = parse_status(status_document("00000000-0000-4000-8000-000000000001"),
                            "00000000-0000-4000-8000-000000000001")
        revision = evidence_revision(base, BDF)
        same_later = parse_status(
            status_document("00000000-0000-4000-8000-000000000003",
                            observed_at="2026-09-17T06:04:00Z"),
            "00000000-0000-4000-8000-000000000003",
        )
        self.assertEqual(evidence_revision(same_later, BDF), revision)
        for changes in (
            {"handover_blocked": []},
            {"tenants": tenant_doc(["C.50352859", "C.51137407", "C.51265315"], "c" * 64)},
            {"container": {"present": True, "running": True,
                           "started_at": "2026-09-17T06:01:00Z"}},
            {"boot_id": "00000000-0000-4000-8000-00000000abcd"},
            # The request quotes how many GPUs are visible, so it is bound too.
            {"nvidia_visible_count": 2},
            {"pci_gpu_count": 7},
        ):
            with self.subTest(changes=changes):
                changed = parse_status(
                    status_document("00000000-0000-4000-8000-000000000004", **changes),
                    "00000000-0000-4000-8000-000000000004",
                )
                self.assertNotEqual(evidence_revision(changed, BDF), revision)

    def test_the_fault_revision_ignores_who_happens_to_be_renting(self) -> None:
        """Re-asking has to be earned by something that could change the answer.

        This and evidence_revision are two jobs that want opposite things, and they
        shared one hash. Every rental starting or stopping moved it, so it became a
        new question, so the investigator ran again from the top at full price and
        reached the same conclusion -- because somebody renting a GPU elsewhere on the
        box has nothing to do with whether this one can be handed to its VM. On a
        marketplace host that is constant.
        """
        base = parse_status(status_document("00000000-0000-4000-8000-000000000001"),
                            "00000000-0000-4000-8000-000000000001")
        revision = fault_revision(base, BDF)
        for irrelevant in (
            # Rentals coming and going, which is the whole churn.
            {"tenants": tenant_doc(["C.50352859", "C.51137407", "C.51265315"], "c" * 64)},
            # Moves every few seconds while a container is in a crash loop, and says
            # nothing the present/running pair does not.
            {"container": {"present": True, "running": True,
                           "started_at": "2026-09-19T06:01:00Z"}},
        ):
            with self.subTest(irrelevant=irrelevant):
                changed = parse_status(
                    status_document("00000000-0000-4000-8000-000000000004", **irrelevant),
                    "00000000-0000-4000-8000-000000000004",
                )
                self.assertEqual(fault_revision(changed, BDF), revision,
                                 "the same question was asked again at full price")

    def test_the_fault_revision_still_moves_when_the_answer_might(self) -> None:
        base = parse_status(status_document("00000000-0000-4000-8000-000000000001"),
                            "00000000-0000-4000-8000-000000000001")
        revision = fault_revision(base, BDF)
        for news in (
            {"handover_blocked": []},          # it is no longer blocked
            {"boot_id": "00000000-0000-4000-8000-00000000abcd"},  # rebooted
            {"nvidia_visible_count": 2},       # a GPU fell off the bus
            {"pci_gpu_count": 7},
            {"container": {"present": True, "running": False, "started_at": None}},
        ):
            with self.subTest(news=news):
                changed = parse_status(
                    status_document("00000000-0000-4000-8000-000000000004", **news),
                    "00000000-0000-4000-8000-000000000004",
                )
                self.assertNotEqual(fault_revision(changed, BDF), revision,
                                    "the machine changed and it did not look again")

    def test_binding_an_approval_stays_as_strict_as_it_was(self) -> None:
        """Narrowing the question must not narrow what a person's yes was bound to."""
        base = parse_status(status_document("00000000-0000-4000-8000-000000000001"),
                            "00000000-0000-4000-8000-000000000001")
        strict = evidence_revision(base, BDF)
        loose = fault_revision(base, BDF)
        self.assertNotEqual(strict, loose, "one hash is still doing both jobs")
        tenants_moved = parse_status(
            status_document("00000000-0000-4000-8000-000000000004",
                            tenants=tenant_doc(["C.50352859"], "d" * 64)),
            "00000000-0000-4000-8000-000000000004",
        )
        self.assertNotEqual(evidence_revision(tenants_moved, BDF), strict,
                            "a proposal outlived a change the approver was shown")

    def test_proposal_requires_blocked_gpu_verified_target_and_running_exporter(self) -> None:
        proposal = self.proposal()
        self.assertEqual(dict(proposal.parameters), {"component": COMPONENT})
        self.assertEqual(set(proposal.resource_ids), {"container:dcgm-exporter", f"gpu:{BDF}"})
        self.assertEqual(proposal.rental_impacts, ())
        self.assertLessEqual(len(f"approve:{proposal.proposal_id}:" + "n" * 24), 64)
        for changes, bdf in (
            ({}, "0000:e1:00.0"),
            ({"hostname": "other"}, BDF),
            ({"container": {"present": True, "running": False, "started_at": None}}, BDF),
        ):
            with self.subTest(changes=changes, bdf=bdf):
                status = parse_status(
                    status_document("00000000-0000-4000-8000-000000000005", **changes),
                    "00000000-0000-4000-8000-000000000005",
                )
                with self.assertRaises(ActorError):
                    build_proposal(status, bdf, policy_revision="r", clock=self.clock)

    def test_approved_restart_succeeds_when_only_the_stuck_vm_rental_leaves(self) -> None:
        proposal = self.proposal()
        self.clock.value += timedelta(minutes=2)
        self.approve(proposal)
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "succeeded", attempt.result_detail)
        self.assertIn("handover cleared", attempt.result_detail)
        self.assertEqual([call[0] for call in self.actor.calls], ["status", "status", "restart", "status"])
        self.assertTrue(self.evidence.has(attempt.pre_evidence_ref))
        self.assertTrue(self.evidence.has(attempt.post_evidence_ref))

    def test_changed_state_between_proposal_and_approval_denies_dispatch(self) -> None:
        proposal = self.proposal()
        self.approve(proposal)
        self.actor.status_changes = {"tenants": tenant_doc(["C.50352859", "C.51137407", "C.51217040", "C.51265315", "C.51300000"], "d" * 64)}
        with self.assertRaises(PolicyDenied):
            self.broker.execute(proposal.proposal_id)
        self.assertNotIn("restart", [call[0] for call in self.actor.calls])

    def test_exporter_restarted_by_someone_else_needs_a_new_proposal(self) -> None:
        # Only the evidence revision binds the exporter start time.
        proposal = self.proposal()
        self.approve(proposal)
        self.actor.status_changes = {"container": {
            "present": True, "running": True, "started_at": "2026-09-17T06:01:00Z",
            "image": "jjziets/dcgm-exporter:latest", "runtime": "nvidia",
        }}
        with self.assertRaisesRegex(PolicyDenied, "evidence revision changed"):
            self.broker.execute(proposal.proposal_id)
        self.assertNotIn("restart", [call[0] for call in self.actor.calls])

    def test_claimed_success_without_new_exporter_start_is_postcondition_failure(self) -> None:
        proposal = self.proposal()
        self.approve(proposal)
        self.actor.status_changes = {}
        original_run = self.actor.run

        def restart_without_effect(operation: str, request_id: str, *, expected_boot_id=None) -> dict:
            document = original_run(operation, request_id)
            if operation == "restart":
                self.actor.restarted = False
            return document

        self.actor.run = restart_without_effect
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "postcondition-failed", attempt.result_detail)
        self.assertIn("not running with a new start time", attempt.result_detail)

    def test_missing_backup_denies_dispatch(self) -> None:
        proposal = self.proposal()
        del self.backup_refs[proposal.proposal_id]
        self.approve(proposal)
        with self.assertRaises(PolicyDenied):
            self.broker.execute(proposal.proposal_id)
        self.assertNotIn("restart", [call[0] for call in self.actor.calls])

    def test_postcondition_stops_when_a_surviving_tenant_was_recreated_or_restarted(self) -> None:
        names = ["C.50352859", "C.51137407", "C.51217040", "C.51265315"]
        before = parse_status(status_document("00000000-0000-4000-8000-000000000009"),
                              "00000000-0000-4000-8000-000000000009")
        new_exporter = {"present": True, "running": True, "started_at": "2026-09-17T06:02:00Z"}
        survivors = [name for name in names if name != "C.51217040"]
        restarted = parse_status(status_document(
            "00000000-0000-4000-8000-000000000010", container=new_exporter,
            tenants=tenant_doc(survivors, "f" * 64, started={"C.51137407": "2026-09-17T06:02:01Z"}),
            vm_containers=[],
        ), "00000000-0000-4000-8000-000000000010")
        self.assertEqual(postcondition(before, restarted),
                         (False, "a tenant container was recreated or restarted"))
        clean = parse_status(status_document(
            "00000000-0000-4000-8000-000000000011", container=new_exporter,
            tenants=tenant_doc(survivors, "f" * 64), vm_containers=[], handover_blocked=[],
        ), "00000000-0000-4000-8000-000000000011")
        self.assertEqual(postcondition(before, clean), (True, "handover cleared"))

    def test_postcondition_stops_on_non_vm_tenant_loss_or_stale_exporter(self) -> None:
        before = parse_status(status_document("00000000-0000-4000-8000-000000000006"),
                              "00000000-0000-4000-8000-000000000006")
        lost_tenant = parse_status(status_document(
            "00000000-0000-4000-8000-000000000007",
            container={"present": True, "running": True, "started_at": "2026-09-17T06:02:00Z"},
            tenants=tenant_doc(["C.50352859", "C.51217040", "C.51265315"], "e" * 64),
        ), "00000000-0000-4000-8000-000000000007")
        self.assertEqual(postcondition(before, lost_tenant)[0], False)
        stale = parse_status(status_document("00000000-0000-4000-8000-000000000008"),
                             "00000000-0000-4000-8000-000000000008")
        self.assertEqual(postcondition(before, stale)[0], False)
        self.assertEqual(postcondition(None, stale)[0], False)

    def test_a_proposal_shape_ignores_time_and_nothing_else(self) -> None:
        from terracompute_ops.monitor_restart import proposal_shape

        status = self.adapter.status()
        first = build_proposal(status, BDF, policy_revision="monitor-restart-r1", clock=self.clock)
        self.clock.value += timedelta(hours=9)
        later = build_proposal(
            status, BDF, policy_revision="monitor-restart-r1", clock=self.clock,
            proposal_id=first.proposal_id,
        )
        # Same facts, hours apart: the digests differ, the shape does not.
        self.assertNotEqual(first.digest, later.digest)
        self.assertEqual(proposal_shape(first), proposal_shape(later))
        # Anything the operator was shown changes the shape.
        other_id = build_proposal(status, BDF, policy_revision="monitor-restart-r1", clock=self.clock)
        self.assertNotEqual(proposal_shape(first), proposal_shape(other_id))
        other_gpu = build_proposal(
            parse_status(status_document("00000000-0000-4000-8000-000000000021",
                                         handover_blocked=[BDF, "0000:c1:00.0"]),
                         "00000000-0000-4000-8000-000000000021"),
            "0000:c1:00.0", policy_revision="monitor-restart-r1", clock=self.clock,
            proposal_id=first.proposal_id,
        )
        self.assertNotEqual(proposal_shape(first), proposal_shape(other_gpu))
        moved_tenants = build_proposal(
            parse_status(status_document("00000000-0000-4000-8000-000000000022",
                                         tenants=tenant_doc(["C.50352859"], "f" * 64)),
                         "00000000-0000-4000-8000-000000000022"),
            BDF, policy_revision="monitor-restart-r1", clock=self.clock,
            proposal_id=first.proposal_id,
        )
        self.assertNotEqual(proposal_shape(first), proposal_shape(moved_tenants))

    def test_handover_cleared_is_judged_for_the_proposed_gpu(self) -> None:
        other = "0000:c1:00.0"
        before = parse_status(status_document(
            "00000000-0000-4000-8000-000000000011", handover_blocked=[BDF, other],
        ), "00000000-0000-4000-8000-000000000011")
        after = parse_status(status_document(
            "00000000-0000-4000-8000-000000000012", handover_blocked=[other],
            container={"present": True, "running": True, "started_at": "2026-09-17T06:02:00Z"},
        ), "00000000-0000-4000-8000-000000000012")
        self.assertEqual(postcondition(before, after, BDF), (True, "handover cleared"))
        self.assertEqual(postcondition(before, after, other), (True, "handover still blocked"))

    def test_helper_failure_or_lost_channel_is_not_success(self) -> None:
        proposal = self.proposal()
        self.approve(proposal)
        self.actor.restart_document = {
            "ok": False, "reason": "started_at_unchanged", "state": "executed",
            "started_at_after": EXPORTER_STARTED, "restart_ran": False,
        }
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "failed")

    def test_dispatch_outcome_follows_what_the_helper_proves(self) -> None:
        for document, expected in (
            ({"ok": False, "reason": "restart_timeout", "state": "executed"}, "unknown"),
            ({"ok": False, "reason": "restart_failed", "state": "executed"}, "unknown"),
            ({"ok": False, "reason": "status_after_unavailable", "state": "executed"}, "unknown"),
            ({"ok": False, "reason": "ledger_write_failed", "state": "executed"}, "unknown"),
            ({"ok": False, "reason": "internal_error", "state": "executed"}, "unknown"),
            ({"ok": False, "reason": "restart_nonzero_exit", "state": "executed"}, "failed"),
            # Refusals and a docker CLI that never launched prove nothing ran.
            ({"ok": False, "reason": "identity_mismatch", "state": "refused"}, "refused"),
            ({"ok": False, "reason": "status_before_unavailable", "state": "refused"}, "refused"),
            ({"ok": False, "reason": "restart_launch_failed", "state": "executed"}, "refused"),
            ({"ok": False, "reason": "ledger_write_failed", "state": "refused"}, "refused"),
            ({"ok": True, "state": "executed", "truncated": True}, "succeeded"),
        ):
            with self.subTest(document=document):
                self.setUp()
                proposal = self.proposal()
                self.approve(proposal)
                self.actor.restart_document = document
                attempt = self.broker.execute(proposal.proposal_id)
                self.assertEqual(attempt.state, expected, attempt.result_detail)
                self.tearDown()
        for reason, expected in (("actor_busy", "refused"), ("ledger_unavailable", "refused"),
                                 ("internal_error", "unknown")):
            with self.subTest(envelope=reason):
                self.setUp()
                proposal = self.proposal()
                self.approve(proposal)
                self.actor.run = lambda operation, request_id, reason=reason, original=self.actor.run, expected_boot_id=None: (
                    {**self.actor.envelope(operation, request_id), "ok": False, "reason": reason}
                    if operation == "restart" else original(operation, request_id)
                )
                attempt = self.broker.execute(proposal.proposal_id)
                self.assertEqual(attempt.state, expected, attempt.result_detail)
                self.tearDown()
        self.setUp()

    def test_target_clock_skew_does_not_make_fresh_evidence_stale(self) -> None:
        proposal = self.proposal()
        self.approve(proposal)
        # The target reports a time two minutes ahead of Imladris.
        self.actor.status_changes = {"observed_at": "2026-09-17T06:02:00Z"}
        original_run = self.actor.run

        def skewed(operation: str, request_id: str, *, expected_boot_id=None) -> dict:
            document = original_run(operation, request_id)
            if operation == "status":
                document["observed_at"] = (self.clock() + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
            return document

        self.actor.run = skewed
        attempt = self.broker.execute(proposal.proposal_id)
        self.assertEqual(attempt.state, "succeeded", attempt.result_detail)

    def reconcile_with(self, proposal, record: dict | None, *, restarted: bool = False,
                       status_error: bool = False, baseline: bool | str = False, **envelope) -> str:
        execution_id = "00000000-0000-4000-8000-00000000beef"
        original = self.actor.run
        self.adapter._before.clear()
        self.adapter.preflight_ref = lambda _proposal: None
        if baseline:
            from terracompute_ops.monitor_restart import _status_document
            self.actor.restarted = False
            ref = self.evidence.record("preflight-status", proposal.proposal_id,
                                       _status_document(self.adapter.status()))
            if baseline == "forged":
                self.db.execute("UPDATE tc_action_evidence SET document_json=? WHERE ref=?",
                                (b'{"forged":true}', ref))
                self.db.commit()
            self.adapter.preflight_ref = lambda _proposal: ref
        self.actor.restarted = restarted
        self.actor.restarted_at = utc_text(self.clock() - timedelta(minutes=4))

        def run(operation: str, request_id: str, *, expected_boot_id=None) -> dict:
            if operation == "status" and status_error:
                raise ActorError("actor_timeout")
            if operation != "result":
                return original(operation, request_id)
            base = {**self.actor.envelope("result", request_id), **envelope}
            if record is None:
                return {**base, "ok": False, "reason": "unknown_execution"}
            return {**base, **record}

        self.actor.run = run
        try:
            return self.adapter.reconcile(proposal, execution_id).status.value
        finally:
            self.actor.run = original

    def test_reconcile_settles_final_and_abandoned_records(self) -> None:
        proposal = self.proposal()
        now = self.clock()
        executed = {"state": "executed", "ok": False, "reason": "restart_timeout",
                    "execution_boot_id": BOOT_ID, "started_at_before": EXPORTER_STARTED,
                    "requested_at": utc_text(now - timedelta(minutes=5)),
                    "completed_at": utc_text(now - timedelta(minutes=4))}
        pending = {"state": "unknown", "ok": False, "reason": "execution_state_unknown",
                   "execution_boot_id": BOOT_ID, "requested_at": utc_text(now - timedelta(minutes=5))}
        interrupted = {"state": "interrupted", "ok": False, "reason": "execution_interrupted",
                       "execution_boot_id": BOOT_ID, "requested_at": utc_text(now - timedelta(minutes=9)),
                       "completed_at": utc_text(now - timedelta(minutes=3)), "started_at_before": None}
        cases = (
            # Only the helper's own record of a never-started ID proves nothing ran.
            ("never started", dict(record={"state": "refused", "ok": False, "reason": "execution_not_started"}),
             "refused"),
            ("envelope without a record", dict(record=None), "unknown"),
            ("run in progress", dict(record={"ok": False, "reason": "execution_in_progress"}), "unknown"),
            ("ledger unavailable", dict(record={"ok": False, "reason": "ledger_unavailable"}), "unknown"),
            ("refused record", dict(record={"state": "refused", "ok": False, "reason": "identity_mismatch"}), "refused"),
            ("succeeded record", dict(record={"state": "executed", "ok": True}), "succeeded"),
            ("nonzero exit", dict(record={**executed, "reason": "restart_nonzero_exit"}), "failed"),
            # A timed-out restart is judged from a fresh status once docker had time.
            ("timeout took effect", dict(record=executed, restarted=True), "succeeded"),
            ("timeout had no effect", dict(record=executed), "failed"),
            ("timeout, target unreachable", dict(record=executed, status_error=True), "unknown"),
            ("timeout, too recent",
             dict(record={**executed, "completed_at": utc_text(now - timedelta(seconds=30))}, restarted=True),
             "unknown"),
            # A claim whose run may still be alive stays unknown however old it is.
            ("pending, in progress",
             dict(record={**pending, "reason": "execution_in_progress",
                          "requested_at": utc_text(now - timedelta(hours=2))}), "unknown"),
            ("pending, old", dict(record={**pending, "requested_at": utc_text(now - timedelta(hours=2))}),
             "unknown"),
            # A dead run's claim, finalized by the helper, is judged against the preflight.
            ("interrupted, took effect", dict(record=interrupted, restarted=True, baseline=True), "succeeded"),
            ("interrupted, no effect", dict(record=interrupted, baseline=True), "failed"),
            ("interrupted, no baseline", dict(record=interrupted, restarted=True), "unknown"),
            ("interrupted, forged baseline", dict(record=interrupted, restarted=True, baseline="forged"),
             "unknown"),
            ("invalid record", dict(record={"state": "unknown", "ok": False, "reason": "ledger_record_invalid"}),
             "unknown"),
        )
        for name, arguments, expected in cases:
            with self.subTest(name):
                arguments = dict(arguments)
                record = arguments.pop("record")
                self.assertEqual(self.reconcile_with(proposal, record, **arguments), expected)
        # A fresh status from an unverified target settles nothing.
        self.actor.status_changes = {"hostname": "somewhere-else"}
        self.assertEqual(self.reconcile_with(proposal, executed, restarted=True), "unknown")
        self.actor.status_changes = {}
        # Without a baseline there is nothing to compare, so the target is not asked.
        calls = len(self.actor.calls)
        self.reconcile_with(proposal, interrupted, restarted=True)
        self.assertNotIn("status", [call[0] for call in self.actor.calls[calls:]])

    def test_incident_signature_matches_probe_event_identity(self) -> None:
        event = {
            "fault_family": "gpu",
            "code": "gpu_vfio_handover_blocked",
            "severity": "critical",
            "message": "GPU handover to a VM is blocked while the NVIDIA driver still holds it",
            "evidence": {"pci_bdf": BDF, "audio_driver": "vfio-pci", "nvrm_registered": True},
        }
        self.assertEqual(handover_incident_signature(BDF), stable_signature(event))


if __name__ == "__main__":
    unittest.main()
