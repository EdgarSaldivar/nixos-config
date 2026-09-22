"""Phase 3: immutable plans and derived effects are the authorization boundary.

The point of these tests is the DEFAULT and the bindings.  A never-before-coded
mutation must reach a person rather than a wall; a tenant action must reach a
person rather than run or be refused; and the person's tap must bind the exact
plan hash, evidence, policy revision, effects, resources, artifacts, rollback,
identities, machine, expiry, nonce, and current rental identity -- so that
changing any of them withdraws the authority.  Standing consent, dynamic
resource selection, denials, revocations, transport bounds, atomicity, and
schema migration all fail closed.
"""

from __future__ import annotations

import dataclasses
import random
import shlex
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.actions import MembershipDecision
from terracompute_ops.authorization import Risk, classify
from terracompute_ops.plan_authorization import (
    AUTHZ_SCHEMA_VERSION,
    CALLBACK_DATA_MAX_BYTES,
    CARD_MESSAGE_MAX_BYTES,
    ApprovalDecision,
    ApprovalRejected,
    ApprovalRequirement,
    Authority,
    AuthorizationBlocked,
    AuthorizationError,
    CallbackAction,
    PlanAuthorizationService,
    RentalRecord,
    StandingEffectGrant,
    Unrepresentable,
    authorize_plan,
    build_plan_authorization,
    derive_step,
    parse_callback,
    plan_authorization_enabled,
    render_approval_card,
    render_approval_card_messages,
)
from terracompute_ops.plans import ApprovalKind, ContractError, Effect, Plan, PlanStep, canonical_json

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
GROUP = -1004484415005
OPERATOR = 777
POLICY = "policy-r1"
EVIDENCE = "ev-1"


def effect(effect_id: str = "effect-1", **flags: bool) -> Effect:
    return Effect(effect_id, "declared effect", **flags)


def step(
    step_id: str = "step-1",
    operation: str = "run_shell",
    arguments: dict | None = None,
    effects: tuple[Effect, ...] | None = None,
    resources: tuple[str, ...] = ("host:17049",),
    preconditions: tuple[dict, ...] = ({"check": "identity", "machine_id": "17049"},),
    postconditions: tuple[dict, ...] = ({"check": "verified"},),
    checkpoint: dict | None = None,
    rollback: dict | None = None,
    rollback_reason: str | None = None,
    artifacts: tuple[str, ...] = (),
    max_seconds: int = 60,
) -> PlanStep:
    if rollback is None and rollback_reason is None:
        rollback = {"kind": "undo", "argv": ["true"]}
    return PlanStep(
        step_id=step_id,
        operation=operation,
        arguments=arguments if arguments is not None else {"argv": ["uptime"]},
        effects=effects if effects is not None else (effect(),),
        affected_resources=resources,
        preconditions=preconditions,
        postconditions=postconditions,
        checkpoint=checkpoint,
        rollback=rollback,
        rollback_impossible_reason=rollback_reason,
        expected_interruption="none",
        max_execution_seconds=max_seconds,
        artifacts=artifacts,
    )


def owned_step(step_id: str = "step-1", **overrides) -> PlanStep:
    """A step shaped so a standing grant's predicates can all hold."""
    defaults = dict(
        operation="restart_owned_unit",
        arguments={"unit": "prometheus-exporter.service"},
        effects=(effect(owned_component=True),),
        resources=("service:prometheus-exporter",),
        checkpoint={
            "kind": "resource_snapshot", "resources": ["service:prometheus-exporter"],
            "fields": ["active"], "artifact": "sha256:" + "ab" * 32,
        },
        postconditions=({
            "kind": "resource_fields_equal", "resource": "service:prometheus-exporter",
            "expected": {"active": True},
        },),
        rollback={
            "kind": "restore_checkpoint", "artifact": "sha256:" + "ab" * 32,
            "resources": ["service:prometheus-exporter"],
        },
        artifacts=("sha256:" + "ab" * 32,),
    )
    if overrides.get("rollback_reason") is not None:
        defaults["rollback"] = None
    defaults.update(overrides)
    return step(step_id=step_id, **defaults)


def plan(
    steps: tuple[PlanStep, ...],
    plan_id: str = "plan-1",
    version: int = 1,
    evidence: str = EVIDENCE,
) -> Plan:
    return Plan(
        plan_id=plan_id,
        task_id="task-1",
        version=version,
        objective="repair the machine",
        evidence_revision=evidence,
        assumptions=(),
        freshness_requirements=({"source": "host", "max_age_seconds": 60},),
        steps=steps,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def rental(
    rental_id: str = "51217040",
    container: str = "C.51217040",
    generation: str = "gen-1",
    owner: str = "tenant",
    status: str = "running",
    gpus: tuple[str, ...] = ("GPU-0",),
) -> RentalRecord:
    return RentalRecord(
        rental_id=rental_id, container_name=container, machine_id="17049",
        generation=generation, owner=owner, status=status, gpu_allocation=gpus,
    )


def standing_grant(
    grant_id: str = "standing-1",
    policy: str = POLICY,
    classes: tuple[str, ...] = ("owned_component",),
    max_step_seconds: int = 300,
    max_affected_resources: int = 4,
    max_uses: int = 5,
    rate_window_seconds: int = 3600,
) -> StandingEffectGrant:
    return StandingEffectGrant(
        grant_id=grant_id, policy_revision=policy,
        description="reversible owned monitoring repair",
        effect_classes=frozenset(classes),
        max_step_seconds=max_step_seconds,
        max_affected_resources=max_affected_resources,
        max_uses=max_uses,
        rate_window_seconds=rate_window_seconds,
        issued_at=NOW - timedelta(days=1), expires_at=NOW + timedelta(days=1),
    )


class FakeResolver:
    """Trusted metadata stub: references (ids, names, aliases) -> records."""

    def __init__(self, records: dict[str, tuple[RentalRecord, ...]]):
        self.records = dict(records)

    def resolve(self, reference: str) -> tuple[RentalRecord, ...]:
        return self.records.get(reference, ())


class FakeMembership:
    def __init__(self, *, member: bool = True, human: bool = True, verified: bool = True):
        self.member = member
        self.human = human
        self.verified = verified
        self.verified_at = NOW

    def verify(self, group_id: int, user_id: int) -> MembershipDecision:
        return MembershipDecision(
            group_id=group_id, user_id=user_id, current_member=self.member,
            human=self.human, independently_verified=self.verified,
            verified_at=self.verified_at,
        )


class MemoryRateAccountant:
    """In-memory accounting for pure authorize_plan tests."""

    def __init__(self):
        self.rows: list[tuple[str, datetime]] = []

    def uses_since(self, grant_id: str, since: datetime) -> int:
        return sum(1 for grant, at in self.rows if grant == grant_id and at >= since)

    def record_use(self, grant_id: str, plan_hash: str, step_id: str, at: datetime) -> None:
        self.rows.append((grant_id, at))


def tenant_records() -> dict[str, tuple[RentalRecord, ...]]:
    record = rental()
    return {"C.51217040": (record,), "51217040": (record,), "the-lonely-tenant": (record,)}


def classifier_from(records: dict[str, tuple[RentalRecord, ...]]):
    return lambda token: bool(records.get(token))


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.membership = FakeMembership()
        self.resolver = FakeResolver(tenant_records())
        self.counter = 0
        self.service = self.make_service()

    def make_service(self, connection: sqlite3.Connection | None = None, **overrides) -> PlanAuthorizationService:
        def next_id() -> str:
            self.counter += 1
            return f"id-{self.counter:04d}"

        def next_nonce() -> str:
            self.counter += 1
            return f"nonce-{self.counter:010d}"

        options = dict(
            membership=self.membership,
            resolver=self.resolver,
            policy_revision=POLICY,
            approval_group_id=GROUP,
            clock=lambda: self.now,
            nonce_factory=next_nonce,
            id_factory=next_id,
        )
        options.update(overrides)
        service = PlanAuthorizationService(
            connection if connection is not None else sqlite3.connect(":memory:"),
            **options,
        )
        self.addCleanup(service.db.close)
        return service

    def temp_db_path(self) -> str:
        directory = tempfile.mkdtemp(prefix="tc-plan-authz-")
        self.addCleanup(lambda: None)
        return str(Path(directory) / "authz.sqlite")

    def issue(self, document: Plan, service: PlanAuthorizationService | None = None):
        service = service or self.service
        decision = service.authorize(document)
        return service.issue_requirements(
            document, decision,
            requester_id="operator:1", current_evidence_revision=document.evidence_revision,
        )

    def decision_for(
        self,
        requirement: ApprovalRequirement,
        *,
        group: int = GROUP,
        user: int | None = OPERATOR,
        callback: bool = True,
        card_hash: str | None = None,
        nonce: str | None = None,
        occurred: datetime | None = None,
        bot: bool = False,
        anonymous: bool = False,
        display_name: str = "Edgar",
    ) -> ApprovalDecision:
        return ApprovalDecision(
            requirement_id=requirement.requirement_id,
            card_hash=card_hash if card_hash is not None else requirement.card_hash,
            nonce=nonce if nonce is not None else requirement.nonce,
            group_id=group,
            user_id=user,
            display_name=display_name,
            via_callback=callback,
            occurred_at=occurred if occurred is not None else self.now,
            sender_is_bot=bot,
            sender_is_anonymous=anonymous,
        )

    def approve(
        self,
        requirement: ApprovalRequirement,
        *,
        evidence: str = EVIDENCE,
        service: PlanAuthorizationService | None = None,
        **overrides,
    ):
        service = service or self.service
        return service.record_decision(
            self.decision_for(requirement, **overrides),
            current_evidence_revision=evidence,
        )

    def deny(
        self,
        requirement: ApprovalRequirement,
        *,
        service: PlanAuthorizationService | None = None,
        **overrides,
    ) -> None:
        service = service or self.service
        service.record_denial(self.decision_for(requirement, **overrides))


class DerivationTests(unittest.TestCase):
    """Unknown stays proposable and lands on a person; only credentials refuse."""

    def test_a_novel_never_coded_mutation_defaults_to_exact_approval(self) -> None:
        novel = step(arguments={"argv": ["ethtool", "-s", "enp1s0", "wol", "g"]})
        risk = derive_step(novel)
        self.assertIs(risk.authority, Authority.EXACT_HUMAN)
        self.assertIn("defaults to exact human approval", " ".join(risk.reasons))

    def test_unrecognized_operations_are_never_refused_property(self) -> None:
        """No source whitelist: arbitrary safe operations always reach approval."""
        rng = random.Random(2026)
        alphabet = "bcdfghjklm"
        for _ in range(50):
            tokens = []
            for _ in range(rng.randint(1, 6)):
                word = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 8)))
                tokens.append(rng.choice([word, f"--{word}", f"/{word}/{word}"]))
            operation = "".join(rng.choice(alphabet) for _ in range(rng.randint(4, 12)))
            risk = derive_step(step(operation=operation, arguments={"argv": tokens}))
            self.assertIn(
                risk.authority, {Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT}
            )

    def test_read_only_observation_is_agent_authority(self) -> None:
        observe = step(
            operation="observe_target",
            arguments={"argv": ["dmesg", "-T"]},
            effects=(effect(),),
        )
        self.assertIs(derive_step(observe).authority, Authority.AGENT)

    def test_run_shell_is_never_agent_authority(self) -> None:
        self.assertIs(
            derive_step(step(arguments={"argv": ["cat", "/etc/passwd"]})).authority,
            Authority.EXACT_HUMAN,
        )

    def test_read_operation_with_shell_composition_escalates(self) -> None:
        """Finding 9: an operation name grants no read authority over an
        unconstrained effect-capable argument program."""
        for argv in (["dmesg | tee /etc/motd"], ["cat", "/proc/loadavg", ";", "reboot"],
                     ["journalctl", "-u", "x", ">", "/etc/passwd"]):
            with self.subTest(argv):
                observe = step(operation="observe_target", arguments={"argv": argv})
                risk = derive_step(observe)
                self.assertIs(risk.authority, Authority.EXACT_HUMAN)

    def test_declared_effects_require_a_person(self) -> None:
        for flags in ({"host": True}, {"irreversible": True}, {"external_commitment": True},
                      {"reachability": True}, {"secrets": True}):
            with self.subTest(flags):
                risk = derive_step(step(effects=(effect(**flags),)))
                self.assertIs(risk.authority, Authority.EXACT_HUMAN)

    def test_tenant_reference_is_derived_from_arguments_not_declaration(self) -> None:
        undeclared = step(arguments={"argv": ["docker", "restart", "C.51217040"]})
        risk = derive_step(undeclared)
        self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)
        self.assertEqual(risk.tenant_references, ("C.51217040",))
        self.assertIn("tenant", risk.flags)

    def test_tenant_reference_anywhere_in_the_step_document_counts(self) -> None:
        """Finding 1: the analysis covers rollback, checkpoint, preconditions,
        and postconditions -- not just the arguments."""
        variants = {
            "rollback": dict(rollback={"kind": "undo", "argv": ["docker", "start", "C.51217040"]}),
            "checkpoint": dict(checkpoint={"capture": "state of C.51217040"}),
            "preconditions": dict(preconditions=({"check": "exists", "target": "C.51217040"},)),
            "postconditions": dict(postconditions=({"check": "healthy", "target": "C.51217040"},)),
        }
        for name, overrides in variants.items():
            with self.subTest(name):
                risk = derive_step(step(**overrides))
                self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)
                self.assertEqual(risk.tenant_references, ("C.51217040",))

    def test_dynamic_selection_blocks_for_an_exact_resource_bound_replan(self) -> None:
        """Finding 1: globs, substitution, and runtime discovery cannot be
        bound to exact resources, so they block instead of reaching any
        authority -- including exact human approval."""
        variants = {
            "glob in argv": dict(arguments={"argv": ["docker", "rm", "C.5121704*"]}),
            "question glob": dict(arguments={"argv": ["docker", "rm", "C.5121704?"]}),
            "command substitution": dict(arguments={"argv": ["docker", "restart", "$(docker ps -q)"]}),
            "variable expansion": dict(arguments={"argv": ["systemctl", "restart", "$UNIT"]}),
            "backtick discovery": dict(arguments={"argv": ["kill", "`pgrep miner`"]}),
            "glob in rollback": dict(rollback={"kind": "undo", "argv": ["rm", "/tmp/x-*"]}),
            "expansion in checkpoint": dict(checkpoint={"capture": "${STATE_DIR}/dump"}),
            "glob in resources": dict(resources=("container:C.*",)),
            "glob in preconditions": dict(preconditions=({"check": "ls /var/lib/*.lock"},)),
        }
        for name, overrides in variants.items():
            with self.subTest(name):
                with self.assertRaisesRegex(AuthorizationBlocked, "exact resource"):
                    derive_step(step(**overrides))

    def test_trusted_classifier_recognises_aliases_and_numeric_identities(self) -> None:
        """Finding 1/6: exact aliases, container names, and numeric rental
        identities are tenant references when trusted metadata says so."""
        classifier = classifier_from(tenant_records())
        cases = {
            "alias": ({"argv": ["docker", "restart", "the-lonely-tenant"]}, "the-lonely-tenant"),
            "numeric string": ({"argv": ["docker", "restart", "51217040"]}, "51217040"),
            "numeric integer": ({"rental": 51217040}, "51217040"),
        }
        for name, (arguments, reference) in cases.items():
            with self.subTest(name):
                risk = derive_step(step(arguments=arguments), classifier=classifier)
                self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)
                self.assertIn(reference, risk.tenant_references)

    def test_classifier_failure_blocks_toward_replanning(self) -> None:
        def broken(token: str) -> bool:
            raise OSError("metadata unavailable")

        with self.assertRaisesRegex(AuthorizationBlocked, "classification failed"):
            derive_step(step(), classifier=broken)

    def test_tenant_moves_from_hard_refusal_to_mandatory_approval(self) -> None:
        """The legacy classifier refuses what Phase 3 sends to a person."""
        command = "docker restart C.51217040"
        self.assertIs(classify(command)[0], Risk.REFUSED)
        risk = derive_step(step(arguments={"argv": command.split()}))
        self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)

    def test_a_lookalike_is_not_a_rental(self) -> None:
        risk = derive_step(step(arguments={"argv": ["docker", "restart", "notC.51217040"]}))
        self.assertEqual(risk.tenant_references, ())
        self.assertNotIn("tenant", risk.flags)

    def test_credential_bearing_input_is_the_one_refusal(self) -> None:
        with self.assertRaises(Unrepresentable):
            derive_step(step(arguments={"argv": ["mysql", "--password"]}))

    def test_credential_values_cannot_even_be_planned(self) -> None:
        with self.assertRaises(ContractError):
            step(arguments={"argv": ["curl", "-u", "user:hunter2", "https://x"]})

    def test_payload_read_is_tenant_and_purpose_limited(self) -> None:
        payload = step(
            operation="read_tenant_payload",
            arguments={"rental_id": "51217040", "purpose": "inspect OOM logs",
                       "source": "container stdout logs"},
        )
        risk = derive_step(payload)
        self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)
        self.assertTrue(risk.payload_read)
        self.assertEqual(risk.tenant_references, ("51217040",))


class StandingConsentTests(unittest.TestCase):
    """Finding 1/8: standing consent fails closed on everything it cannot prove."""

    def setUp(self) -> None:
        self.accountant = MemoryRateAccountant()
        self.classifier = classifier_from(tenant_records())

    def decide(self, document: Plan, grants=(standing_grant(),), **overrides):
        options = dict(
            grants=grants, policy_revision=POLICY, now=NOW,
            classifier=self.classifier, rate_accountant=self.accountant,
        )
        options.update(overrides)
        return authorize_plan(document, **options)

    def test_reversible_owned_component_step_may_run_under_a_grant(self) -> None:
        decision = self.decide(plan((owned_step(),)))
        self.assertIs(decision.plan_authority, Authority.STANDING_CONSENT)
        self.assertFalse(decision.requires_approval)

    def test_standing_consent_can_never_name_tenant_or_irreversible_effects(self) -> None:
        for forbidden in ("tenant", "irreversible", "reachability", "external_commitment",
                          "secrets", "host"):
            with self.subTest(forbidden):
                with self.assertRaises(AuthorizationError):
                    standing_grant(grant_id="standing-x", classes=(forbidden,))

    def test_a_grant_never_quiets_a_step_that_names_a_rental(self) -> None:
        touching = owned_step(arguments={"unit": "x", "container": "C.51217040"})
        decision = self.decide(plan((touching,)))
        self.assertIs(decision.plan_authority, Authority.ALWAYS_APPROVE_TENANT)

    def test_risk_rolls_upward_to_the_whole_approval_group(self) -> None:
        tenant = step(
            step_id="step-2", arguments={"argv": ["docker", "restart", "C.51217040"]}
        )
        decision = self.decide(plan((owned_step(), tenant)))
        self.assertIs(decision.plan_authority, Authority.ALWAYS_APPROVE_TENANT)

    def test_no_rollback_means_no_standing_consent(self) -> None:
        one_way = owned_step(rollback_reason="state cannot be restored")
        decision = self.decide(plan((one_way,)))
        self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)

    def test_run_shell_and_opaque_arguments_fail_closed(self) -> None:
        """Finding 1: standing consent never covers an opaque program."""
        opaque_shell = owned_step(operation="run_shell")
        opaque_arguments = owned_step(
            arguments={"unit": "x", "command": "systemctl restart prometheus"}
        )
        nested_opaque = owned_step(
            arguments={"unit": "x", "steps": [{"script": "restart.sh"}]}
        )
        for document in (opaque_shell, opaque_arguments, nested_opaque):
            with self.subTest(document.operation):
                decision = self.decide(plan((document,)))
                self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)

    def test_checkpoint_and_verification_predicates_fail_closed(self) -> None:
        """Finding 8: no deterministic checkpoint or verification, no grant."""
        without_checkpoint = owned_step(checkpoint=None)
        unverifiable = owned_step(postconditions=({"note": "seems fine"},))
        for document in (without_checkpoint, unverifiable):
            with self.subTest(document.checkpoint):
                decision = self.decide(plan((document,)))
                self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)

    def test_blast_radius_bound_over_exactly_named_resources(self) -> None:
        too_wide = owned_step(
            resources=tuple(f"service:unit-{index}" for index in range(5)),
        )
        unnamed = owned_step(resources=())
        for document, label in ((too_wide, "too wide"), (unnamed, "unnamed")):
            with self.subTest(label):
                decision = self.decide(plan((document,)))
                self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)

    def test_rate_budget_is_enforced_from_durable_accounting(self) -> None:
        grant = standing_grant()
        for _ in range(grant.max_uses):
            self.accountant.record_use(grant.grant_id, "0" * 64, "step-1", NOW - timedelta(minutes=5))
        decision = self.decide(plan((owned_step(),)), grants=(grant,))
        self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)

    def test_uses_outside_the_window_do_not_count(self) -> None:
        grant = standing_grant()
        for _ in range(grant.max_uses):
            self.accountant.record_use(grant.grant_id, "0" * 64, "step-1", NOW - timedelta(hours=2))
        decision = self.decide(plan((owned_step(),)), grants=(grant,))
        self.assertIs(decision.plan_authority, Authority.STANDING_CONSENT)

    def test_no_accounting_or_no_classifier_fails_closed_entirely(self) -> None:
        """Finding 8: standing consent cannot authorize without durable rate
        accounting; finding 1: nor without a trusted classifier."""
        without_accounting = self.decide(plan((owned_step(),)), rate_accountant=None)
        self.assertIs(without_accounting.plan_authority, Authority.EXACT_HUMAN)
        without_classifier = self.decide(plan((owned_step(),)), classifier=None)
        self.assertIs(without_classifier.plan_authority, Authority.EXACT_HUMAN)

    def test_accounting_failure_disqualifies_the_grant(self) -> None:
        class BrokenAccountant:
            def uses_since(self, grant_id: str, since: datetime) -> int:
                raise OSError("disk gone")

            def record_use(self, *args) -> None:
                raise OSError("disk gone")

        decision = self.decide(plan((owned_step(),)), rate_accountant=BrokenAccountant())
        self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)


class CardTests(ServiceCase):
    def tenant_plan(self, **step_overrides) -> Plan:
        arguments = step_overrides.pop(
            "arguments", {"argv": ["docker", "restart", "C.51217040"]}
        )
        return plan((step(arguments=arguments, **step_overrides),))

    def test_card_bytes_are_deterministic_across_equal_plans(self) -> None:
        forward = plan((step(arguments={"argv": ["x", "y"], "cwd": "/", "env_name": "a"}),))
        reordered = plan((step(arguments={"env_name": "a", "cwd": "/", "argv": ["x", "y"]}),))
        self.assertEqual(forward.content_hash, reordered.content_hash)
        first = self.issue(forward)[0]
        self.counter = 0  # replay the same id/nonce sequence in a fresh service
        self.service = self.make_service()
        second = self.issue(reordered)[0]
        self.assertEqual(render_approval_card(first), render_approval_card(second))
        self.assertEqual(first.card_hash, second.card_hash)

    def test_card_binds_every_authorization_fact(self) -> None:
        document = self.tenant_plan(artifacts=("sha256:" + "ab" * 32,))
        requirement = self.issue(document)[0]
        text = render_approval_card(requirement).decode("utf-8")
        for fragment in (
            "TENANT APPROVAL REQUIRED — machine 17049",
            f"plan sha256 {document.content_hash}",
            "task task-1 plan plan-1 v1",
            f"evidence {EVIDENCE}",
            f"policy {POLICY}",
            f"requested by operator:1 for group {GROUP}",
            "sha256:" + "ab" * 32,
            "rental 51217040 container C.51217040 owner tenant status running "
            "generation gen-1 gpus GPU-0",
            "rollback",
            f"nonce {requirement.nonce}",
            f"expires {requirement.expires_at.isoformat(timespec='microseconds').replace('+00:00', 'Z')}",
            f"approve pa1:{requirement.nonce}",
            f"deny pd1:{requirement.nonce}",
        ):
            self.assertIn(fragment, text)

    def test_policy_revision_is_bound_into_document_and_hash(self) -> None:
        """Finding 5: a different policy revision is a different card."""
        document = self.tenant_plan()
        requirement = self.issue(document)[0]
        self.assertEqual(requirement.policy_revision, POLICY)
        self.assertEqual(requirement.to_document()["policy_revision"], POLICY)
        self.counter = 0
        other = self.make_service(policy_revision="policy-r2")
        other_requirement = self.issue(document, service=other)[0]
        self.assertNotEqual(requirement.card_hash, other_requirement.card_hash)

    def test_changed_rollback_or_artifacts_change_the_plan_hash_and_card(self) -> None:
        base = self.tenant_plan()
        changed_rollback = self.tenant_plan(rollback={"kind": "undo", "argv": ["false"]})
        changed_artifacts = self.tenant_plan(artifacts=("sha256:" + "cd" * 32,))
        self.assertNotEqual(base.content_hash, changed_rollback.content_hash)
        self.assertNotEqual(base.content_hash, changed_artifacts.content_hash)
        cards = set()
        for document in (base, changed_rollback, changed_artifacts):
            self.counter = 0
            self.service = self.make_service()
            cards.add(self.issue(document)[0].card_hash)
        self.assertEqual(len(cards), 3)

    def test_secrets_never_enter_the_model_visible_contract(self) -> None:
        with self.assertRaises(Unrepresentable):
            ApprovalRequirement(
                requirement_id="req-1", task_id="task-1", plan_id="plan-1",
                plan_version=1, plan_hash="0" * 64, evidence_revision=EVIDENCE,
                policy_revision=POLICY,
                requester_id="operator:1", approval_group_id=GROUP,
                steps=({"step": step().to_document(), "flags": (), "tenant_references": (),
                        "reference_bindings": {}},),
                tenant_bindings=(), payload_purpose="token=abc123secretvalue",
                payload_source="container logs", issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5), nonce="nonce-1234567890",
            )

    def test_a_generic_requirement_cannot_stand_in_for_a_tenant_binding(self) -> None:
        """Finding 1: a tenant-flagged step without a binding is unrepresentable
        as a requirement; the binding is not optional."""
        tenant_summary = {
            "step": step(arguments={"argv": ["docker", "restart", "C.51217040"]}).to_document(),
            "flags": ("tenant",),
            "tenant_references": ("C.51217040",),
            "reference_bindings": {},
        }
        with self.assertRaisesRegex(AuthorizationError, "exact rental"):
            ApprovalRequirement(
                requirement_id="req-1", task_id="task-1", plan_id="plan-1",
                plan_version=1, plan_hash="0" * 64, evidence_revision=EVIDENCE,
                policy_revision=POLICY,
                requester_id="operator:1", approval_group_id=GROUP,
                steps=(tenant_summary,),
                tenant_bindings=(), payload_purpose=None, payload_source=None,
                issued_at=NOW, expires_at=NOW + timedelta(minutes=5),
                nonce="nonce-1234567890",
            )


class CallbackProtocolTests(ServiceCase):
    """Finding 2: compact, deterministic, authenticated callback protocol."""

    def requirement(self) -> ApprovalRequirement:
        return self.issue(plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),)))[0]

    def test_callback_data_fits_the_64_byte_transport_bound(self) -> None:
        # Default factories: an unguessable urlsafe nonce, not test counters.
        service = PlanAuthorizationService(
            sqlite3.connect(":memory:"), membership=self.membership,
            resolver=self.resolver, policy_revision=POLICY,
            approval_group_id=GROUP, clock=lambda: self.now,
        )
        decision = service.authorize(plan((step(),)))
        requirement = service.issue_requirements(
            plan((step(),)), decision,
            requester_id="operator:1", current_evidence_revision=EVIDENCE,
        )[0]
        for data in (requirement.approve_callback_data, requirement.deny_callback_data):
            self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_DATA_MAX_BYTES)

    def test_parsing_is_deterministic_and_total(self) -> None:
        requirement = self.requirement()
        approve = parse_callback(requirement.approve_callback_data)
        deny = parse_callback(requirement.deny_callback_data)
        self.assertEqual(approve, CallbackAction(approve=True, nonce=requirement.nonce))
        self.assertEqual(deny, CallbackAction(approve=False, nonce=requirement.nonce))
        for garbage in (
            None, 7, b"pa1:x", "", "pa1", "pa1:", "approve:" + requirement.nonce,
            "pa2:" + requirement.nonce, "pa1:short", "pa1:" + "x" * 61,
            "pa1:" + requirement.nonce + ":extra!",
            "pa1:" + "n" * 100,
        ):
            with self.subTest(repr(garbage)):
                with self.assertRaises(ApprovalRejected):
                    parse_callback(garbage)

    def test_resolve_callback_binds_the_stored_requirement(self) -> None:
        requirement = self.requirement()
        action, resolved = self.service.resolve_callback(requirement.approve_callback_data)
        self.assertTrue(action.approve)
        self.assertEqual(resolved.requirement_id, requirement.requirement_id)
        self.assertEqual(resolved.card_hash, requirement.card_hash)
        with self.assertRaisesRegex(ApprovalRejected, "known requirement"):
            self.service.resolve_callback("pa1:nonce-9999999999")

    def test_a_long_nonce_cannot_produce_an_oversized_callback(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "callback-safe"):
            self.issue(
                plan((step(),)),
                service=self.make_service(nonce_factory=lambda: "n" * 61),
            )


class CardTransportTests(ServiceCase):
    """Finding 7: cards are bounded to the transport before persistence."""

    def wide_plan(self, count: int = 20) -> Plan:
        steps = tuple(
            step(
                step_id=f"step-{index:02d}",
                arguments={"argv": ["systemctl", "restart", f"unit-{index:02d}"],
                           "detail": "d" * 150},
            )
            for index in range(count)
        )
        return plan(steps)

    def test_large_cards_split_on_section_boundaries_without_byte_changes(self) -> None:
        requirement = self.issue(self.wide_plan())[0]
        messages = render_approval_card_messages(requirement)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), CARD_MESSAGE_MAX_BYTES)
        self.assertEqual(b"\n\n".join(messages), render_approval_card(requirement))

    def test_an_unsplittable_card_blocks_before_anything_is_persisted(self) -> None:
        huge = plan((step(arguments={"argv": ["write"], "payload_text": "a" * 5000}),))
        with self.assertRaisesRegex(AuthorizationBlocked, "smaller exact plan"):
            self.issue(huge)
        count = self.service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements"
        ).fetchone()[0]
        self.assertEqual(count, 0)


class AtomicIssuanceTests(ServiceCase):
    """Finding 4: requirements are issued atomically from a rederived decision."""

    def mixed_plan(self, detail: str | None = None) -> Plan:
        payload_arguments = {
            "rental_id": "51217040", "purpose": "inspect OOM logs",
            "source": "container stdout logs",
        }
        if detail is not None:
            payload_arguments["detail"] = detail
        return plan((
            step(step_id="step-1", arguments={"argv": ["docker", "restart", "C.51217040"]}),
            step(step_id="step-2", operation="read_tenant_payload",
                 arguments=payload_arguments),
        ))

    def requirement_count(self) -> int:
        return self.service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements"
        ).fetchone()[0]

    def test_a_tampered_caller_decision_is_rejected(self) -> None:
        document = plan((step(),))
        decision = self.service.authorize(document)
        forged = dataclasses.replace(decision, approval_step_ids=())
        with self.assertRaisesRegex(AuthorizationError, "rederived"):
            self.service.issue_requirements(
                document, forged,
                requester_id="operator:1", current_evidence_revision=EVIDENCE,
            )
        promoted = dataclasses.replace(decision, plan_authority=Authority.AGENT)
        with self.assertRaisesRegex(AuthorizationError, "rederived"):
            self.service.issue_requirements(
                document, promoted,
                requester_id="operator:1", current_evidence_revision=EVIDENCE,
            )
        self.assertEqual(self.requirement_count(), 0)

    def test_a_decision_for_a_different_plan_is_rejected(self) -> None:
        document = plan((step(),))
        other = plan((step(arguments={"argv": ["uptime", "-p"]}),))
        decision = self.service.authorize(other)
        with self.assertRaises(AuthorizationError):
            self.service.issue_requirements(
                document, decision,
                requester_id="operator:1", current_evidence_revision=EVIDENCE,
            )
        self.assertEqual(self.requirement_count(), 0)

    def test_an_oversized_second_card_persists_nothing(self) -> None:
        document = self.mixed_plan(detail="a" * 5000)
        with self.assertRaisesRegex(AuthorizationBlocked, "smaller exact plan"):
            self.issue(document)
        self.assertEqual(self.requirement_count(), 0)

    def test_a_nonce_collision_rolls_back_every_requirement(self) -> None:
        service = self.make_service(nonce_factory=lambda: "nonce-collision0")
        decision = service.authorize(self.mixed_plan())
        with self.assertRaisesRegex(AuthorizationError, "already used"):
            service.issue_requirements(
                self.mixed_plan(), decision,
                requester_id="operator:1", current_evidence_revision=EVIDENCE,
            )
        count = service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements"
        ).fetchone()[0]
        self.assertEqual(count, 0)


class ApprovalTests(ServiceCase):
    def tenant_requirement(self) -> tuple[Plan, ApprovalRequirement]:
        document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        return document, self.issue(document)[0]

    def test_exact_callback_approval_yields_a_bound_grant(self) -> None:
        document, requirement = self.tenant_requirement()
        grant = self.approve(requirement)
        self.assertIs(grant.kind, ApprovalKind.EXACT_HUMAN)
        self.assertEqual(grant.plan_hash, document.content_hash)
        self.assertEqual(grant.step_ids, ("step-1",))
        self.assertEqual(grant.approver_id, str(OPERATOR))
        self.assertEqual(grant.nonce, requirement.nonce)
        self.assertEqual(grant.evidence_revision, EVIDENCE)
        self.assertEqual(grant.policy_revision, POLICY)

    def test_foreign_group_and_foreign_operator_have_no_authority(self) -> None:
        _, requirement = self.tenant_requirement()
        with self.assertRaisesRegex(ApprovalRejected, "foreign group"):
            self.approve(requirement, group=GROUP + 1)
        self.membership.member = False
        with self.assertRaisesRegex(ApprovalRejected, "membership"):
            self.approve(requirement)
        self.membership.member = True
        with self.assertRaisesRegex(ApprovalRejected, "human"):
            self.approve(requirement, bot=True)
        with self.assertRaisesRegex(ApprovalRejected, "human"):
            self.approve(requirement, user=None, anonymous=True)

    def test_a_replayed_callback_is_rejected(self) -> None:
        _, requirement = self.tenant_requirement()
        self.approve(requirement)
        with self.assertRaisesRegex(ApprovalRejected, "already consumed"):
            self.approve(requirement)

    def test_a_stale_or_tampered_card_is_rejected(self) -> None:
        _, requirement = self.tenant_requirement()
        with self.assertRaisesRegex(ApprovalRejected, "exact card"):
            self.approve(requirement, card_hash="1" * 64)
        with self.assertRaisesRegex(ApprovalRejected, "nonce"):
            self.approve(requirement, nonce="nonce-forged00")

    def test_expiry_and_stale_evidence_reject_the_decision(self) -> None:
        _, requirement = self.tenant_requirement()
        with self.assertRaisesRegex(ApprovalRejected, "stale"):
            self.approve(requirement, evidence="ev-2")
        self.now = NOW + timedelta(minutes=6)
        with self.assertRaisesRegex(ApprovalRejected, "expired"):
            self.approve(requirement, occurred=NOW)

    def test_text_confirmation_carries_no_authority_for_any_mutation(self) -> None:
        """Finding 2: callback-only authority applies to every approval,
        tenant or not."""
        _, tenant_requirement = self.tenant_requirement()
        with self.assertRaisesRegex(ApprovalRejected, "text confirmation"):
            self.approve(tenant_requirement, callback=False)
        host_document = plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),))
        host_requirement = self.issue(host_document)[0]
        with self.assertRaisesRegex(ApprovalRejected, "text confirmation"):
            self.approve(host_requirement, callback=False)
        grant = self.approve(host_requirement, callback=True)
        self.assertEqual(grant.plan_hash, host_document.content_hash)

    def test_a_requirement_is_bound_to_its_issuing_group(self) -> None:
        """Finding 9: a requirement issued for one group cannot be approved
        through a service commissioned for another."""
        path = self.temp_db_path()
        issuing = self.make_service(connection=sqlite3.connect(path))
        requirement = self.issue(
            plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),)),
            service=issuing,
        )[0]
        other_group = self.make_service(
            connection=sqlite3.connect(path), approval_group_id=GROUP + 1
        )
        with self.assertRaisesRegex(ApprovalRejected, "different approval group"):
            self.approve(requirement, group=GROUP + 1, service=other_group)

    def test_display_name_is_validated(self) -> None:
        """Finding 9: display names are bounded, printable, and secret-free."""
        _, requirement = self.tenant_requirement()
        for name, error in (
            ("", AuthorizationError),
            ("Edgar\x00", AuthorizationError),
            ("x" * 129, AuthorizationError),
            ("token=abc123secretvalue", Unrepresentable),
        ):
            with self.subTest(repr(name[:16])):
                with self.assertRaises(error):
                    self.decision_for(requirement, display_name=name)

    def test_a_rental_changed_before_approval_is_rejected(self) -> None:
        _, requirement = self.tenant_requirement()
        replaced = rental(generation="gen-2")
        self.resolver.records = {
            "C.51217040": (replaced,), "51217040": (replaced,),
        }
        with self.assertRaisesRegex(ApprovalRejected, "identity changed"):
            self.approve(requirement)


class PolicyRevisionTests(ServiceCase):
    """Finding 5: the policy revision is bound end to end."""

    def test_approval_under_a_changed_policy_revision_is_rejected(self) -> None:
        path = self.temp_db_path()
        issuing = self.make_service(connection=sqlite3.connect(path))
        requirement = self.issue(plan((step(),)), service=issuing)[0]
        revised = self.make_service(
            connection=sqlite3.connect(path), policy_revision="policy-r2"
        )
        with self.assertRaisesRegex(ApprovalRejected, "policy revision"):
            self.approve(requirement, service=revised)

    def test_preflight_refuses_after_a_policy_revision_change(self) -> None:
        path = self.temp_db_path()
        issuing = self.make_service(connection=sqlite3.connect(path))
        document = plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),))
        requirement = self.issue(document, service=issuing)[0]
        self.approve(requirement, service=issuing)
        revised = self.make_service(
            connection=sqlite3.connect(path), policy_revision="policy-r2"
        )
        verdict = revised.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertFalse(verdict.admit)
        self.assertIn("policy revision", verdict.reason)


class DenialRevocationTests(ServiceCase):
    """Finding 3: durable denial and revocation; denial can never approve."""

    def host_requirement(self) -> ApprovalRequirement:
        return self.issue(plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),)))[0]

    def test_denial_consumes_the_nonce_and_cannot_become_approval(self) -> None:
        requirement = self.host_requirement()
        self.deny(requirement)
        self.assertIsNotNone(self.service.get_denial(requirement.requirement_id))
        with self.assertRaisesRegex(ApprovalRejected, "denied"):
            self.approve(requirement)
        self.assertIsNone(self.service.get_grant(requirement.requirement_id))

    def test_denial_is_replay_protected_and_post_approval_denial_fails(self) -> None:
        requirement = self.host_requirement()
        self.deny(requirement)
        with self.assertRaisesRegex(ApprovalRejected, "already denied"):
            self.deny(requirement)
        approved = self.host_requirement()
        self.approve(approved)
        with self.assertRaisesRegex(ApprovalRejected, "already approved"):
            self.deny(approved)

    def test_denial_requires_the_same_authenticated_callback_authority(self) -> None:
        requirement = self.host_requirement()
        with self.assertRaisesRegex(ApprovalRejected, "text confirmation"):
            self.deny(requirement, callback=False)
        with self.assertRaisesRegex(ApprovalRejected, "foreign group"):
            self.deny(requirement, group=GROUP + 1)
        with self.assertRaisesRegex(ApprovalRejected, "human"):
            self.deny(requirement, bot=True)
        with self.assertRaisesRegex(ApprovalRejected, "exact card"):
            self.deny(requirement, card_hash="1" * 64)

    def test_denial_is_accepted_even_after_expiry(self) -> None:
        requirement = self.host_requirement()
        self.now = NOW + timedelta(minutes=10)
        self.membership.verified_at = self.now
        self.deny(requirement, occurred=NOW + timedelta(minutes=10))
        self.assertIsNotNone(self.service.get_denial(requirement.requirement_id))

    def test_grant_revocation_is_durable_and_preflight_honours_it(self) -> None:
        document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        requirement = self.issue(document)[0]
        self.approve(requirement)
        admitted = self.service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertTrue(admitted.admit)
        self.service.revoke_grant(
            requirement.requirement_id, revoked_by="operator:1", reason="changed my mind"
        )
        refused = self.service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertFalse(refused.admit)
        self.assertIn("revoked", refused.reason)

    def test_revoking_an_unknown_grant_fails(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "no approval grant"):
            self.service.revoke_grant("missing", revoked_by="operator:1", reason="x")

    def test_standing_grant_revocation_withdraws_standing_authority(self) -> None:
        service = self.make_service(standing_grants=(standing_grant(),))
        document = plan((owned_step(),))
        self.assertIs(
            service.authorize(document).plan_authority, Authority.STANDING_CONSENT
        )
        service.revoke_standing_grant(
            "standing-1", revoked_by="operator:1", reason="commissioning rollback"
        )
        self.assertIs(
            service.authorize(document).plan_authority, Authority.EXACT_HUMAN
        )


class TenantBindingTests(ServiceCase):
    def test_trusted_resolution_binds_the_exact_rental_not_the_alias(self) -> None:
        document = plan((
            step(
                operation="tenant_restart",
                arguments={"rental_id": "the-lonely-tenant"},
            ),
        ))
        requirement = self.issue(document)[0]
        self.assertEqual(len(requirement.tenant_bindings), 1)
        binding = requirement.tenant_bindings[0]
        self.assertEqual(binding.rental_id, "51217040")
        self.assertEqual(binding.container_name, "C.51217040")
        self.assertEqual(binding.generation, "gen-1")

    def test_aliases_and_ids_deduplicate_to_one_binding(self) -> None:
        document = plan((
            step(step_id="step-1", arguments={"argv": ["docker", "restart", "C.51217040"]}),
            step(step_id="step-2", operation="tenant_verify",
                 arguments={"rental_id": "51217040"}),
        ))
        requirement = self.issue(document)[0]
        self.assertEqual(len(requirement.tenant_bindings), 1)

    def test_multiple_rentals_are_each_bound_exactly(self) -> None:
        second = rental(rental_id="60000001", container="C.60000001", gpus=("GPU-1",))
        self.resolver.records["C.60000001"] = (second,)
        document = plan((
            step(step_id="step-1", arguments={"argv": ["docker", "restart", "C.51217040"]}),
            step(step_id="step-2", arguments={"argv": ["docker", "restart", "C.60000001"]}),
        ))
        requirement = self.issue(document)[0]
        self.assertEqual(
            tuple(binding.rental_id for binding in requirement.tenant_bindings),
            ("51217040", "60000001"),
        )

    def test_unknown_or_ambiguous_rentals_block_toward_replanning(self) -> None:
        stranger = plan((step(arguments={"argv": ["docker", "restart", "C.99999999"]}),))
        with self.assertRaisesRegex(AuthorizationBlocked, "propose again"):
            self.issue(stranger)
        self.resolver.records["C.51217040"] = (
            rental(), rental(rental_id="51217041", container="C.51217041"),
        )
        ambiguous = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        with self.assertRaisesRegex(AuthorizationBlocked, "ambiguous"):
            self.issue(ambiguous)

    def test_ambiguity_is_judged_over_the_complete_record(self) -> None:
        """Finding 6: same id and generation but a different owner, status, or
        GPU allocation is still ambiguous."""
        for variant in (rental(owner="controller"), rental(gpus=("GPU-9",))):
            with self.subTest(variant.owner):
                self.resolver.records["C.51217040"] = (rental(), variant)
                document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
                with self.assertRaisesRegex(AuthorizationBlocked, "ambiguous"):
                    self.issue(document)

    def test_identical_duplicate_records_are_not_ambiguous(self) -> None:
        self.resolver.records["C.51217040"] = (rental(), rental())
        document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        requirement = self.issue(document)[0]
        self.assertEqual(len(requirement.tenant_bindings), 1)

    def test_non_actionable_rental_status_blocks(self) -> None:
        """Finding 6: only rentals in an actionable state can be bound."""
        for status in ("exited", "destroyed", "creating"):
            with self.subTest(status):
                sick = rental(status=status)
                self.resolver.records["C.51217040"] = (sick,)
                self.resolver.records["51217040"] = (sick,)
                document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
                with self.assertRaisesRegex(AuthorizationBlocked, "not\\s+actionable"):
                    self.issue(document)

    def test_a_declared_tenant_effect_without_a_binding_blocks(self) -> None:
        """Finding 1: a generic exact approval never substitutes for a missing
        tenant binding."""
        declared = plan((step(effects=(effect(tenant=True),), arguments={"argv": ["true"]}),))
        with self.assertRaisesRegex(AuthorizationBlocked, "exact rental binding"):
            self.issue(declared)


class PreflightTests(ServiceCase):
    def approved(self) -> tuple[Plan, ApprovalRequirement]:
        document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        requirement = self.issue(document)[0]
        self.approve(requirement)
        return document, requirement

    def preflight(self, requirement: ApprovalRequirement, document: Plan, **overrides):
        options = dict(
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now
        )
        options.update(overrides)
        service = overrides.pop("service", None) or self.service
        options.pop("service", None)
        return service.preflight(requirement.requirement_id, document, **options)

    def test_an_intact_approval_admits_and_consumes_nothing(self) -> None:
        document, requirement = self.approved()
        first = self.preflight(requirement, document)
        second = self.preflight(requirement, document)
        self.assertTrue(first.admit)
        self.assertTrue(second.admit)
        self.assertIn("unconsumed", first.reason)

    def test_any_plan_change_invalidates_the_approval(self) -> None:
        _, requirement = self.approved()
        for changed in (
            plan((step(arguments={"argv": ["docker", "restart", "C.51217040"], "extra": 1}),)),
            plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]},
                       rollback={"kind": "undo", "argv": ["false"]}),)),
            plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]},
                       artifacts=("sha256:" + "ef" * 32,)),)),
        ):
            with self.subTest(changed.content_hash):
                verdict = self.preflight(requirement, changed)
                self.assertFalse(verdict.admit)
                self.assertIn("plan hash changed", verdict.reason)

    def test_replaced_rental_refuses_immediately_before_execution(self) -> None:
        document, requirement = self.approved()
        for replacement in (
            rental(generation="gen-2"),
            rental(owner="controller"),
            rental(status="stopped"),
            rental(gpus=("GPU-7",)),
        ):
            with self.subTest(replacement):
                self.resolver.records["51217040"] = (replacement,)
                verdict = self.preflight(requirement, document)
                self.assertFalse(verdict.admit)
                self.assertIn("changed after approval", verdict.reason)

    def test_a_rental_that_left_an_actionable_state_refuses(self) -> None:
        document, requirement = self.approved()
        self.resolver.records["51217040"] = (rental(status="exited"),)
        verdict = self.preflight(requirement, document)
        self.assertFalse(verdict.admit)
        self.assertIn("not actionable", verdict.reason)

    def test_a_vanished_rental_refuses_toward_replanning(self) -> None:
        document, requirement = self.approved()
        self.resolver.records["51217040"] = ()
        verdict = self.preflight(requirement, document)
        self.assertFalse(verdict.admit)
        self.assertIn("propose again", verdict.reason)

    def test_stale_evidence_wrong_machine_and_expiry_refuse(self) -> None:
        document, requirement = self.approved()
        self.assertFalse(
            self.preflight(requirement, document, current_evidence_revision="ev-2").admit
        )
        self.assertFalse(
            self.preflight(requirement, document, current_machine_id="17050").admit
        )
        self.assertFalse(
            self.preflight(requirement, document, now=NOW + timedelta(minutes=6)).admit
        )

    def test_without_a_recorded_decision_nothing_is_admitted(self) -> None:
        document = plan((step(arguments={"argv": ["docker", "restart", "C.51217040"]}),))
        requirement = self.issue(document)[0]
        verdict = self.preflight(requirement, document)
        self.assertFalse(verdict.admit)
        self.assertIn("no exact human approval", verdict.reason)

    def test_a_lapsed_standing_grant_widens_the_approval_set_and_refuses(self) -> None:
        """Finding 4: preflight revalidates standing grant validity; the human
        approved a card that assumed the grant quieted a step."""
        service = self.make_service(standing_grants=(standing_grant(),))
        document = plan((
            owned_step(step_id="step-1"),
            step(step_id="step-2", operation="host_fix", arguments={"unit": "nic"},
                 effects=(effect(host=True),)),
        ))
        decision = service.authorize(document)
        self.assertEqual(decision.approval_step_ids, ("step-2",))
        requirement = service.issue_requirements(
            document, decision,
            requester_id="operator:1", current_evidence_revision=EVIDENCE,
        )[0]
        self.approve(requirement, service=service)
        admitted = service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertTrue(admitted.admit)
        service.revoke_standing_grant(
            "standing-1", revoked_by="operator:1", reason="rollback"
        )
        refused = service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertFalse(refused.admit)
        self.assertIn("exact approval changed", refused.reason)

    def test_an_exhausted_rate_budget_refuses_at_preflight(self) -> None:
        """Finding 8: durable rate accounting; the budget consumed elsewhere
        withdraws standing coverage here."""
        service = self.make_service(
            standing_grants=(standing_grant(max_uses=1),)
        )
        document = plan((
            owned_step(step_id="step-1"),
            step(step_id="step-2", operation="host_fix", arguments={"unit": "nic"},
                 effects=(effect(host=True),)),
        ))
        decision = service.authorize(document)
        requirement = service.issue_requirements(
            document, decision,
            requester_id="operator:1", current_evidence_revision=EVIDENCE,
        )[0]
        self.approve(requirement, service=service)
        standing_only = plan((owned_step(step_id="step-1"),), plan_id="plan-2")
        charged = service.commit_standing_uses(
            standing_only, service.authorize(standing_only)
        )
        self.assertEqual(charged, (("standing-1", "step-1"),))
        refused = service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertFalse(refused.admit)
        self.assertIn("exact approval changed", refused.reason)

    def test_changed_effect_derivation_refuses(self) -> None:
        """Finding 4: preflight re-analyses the exact plan; drifted trusted
        metadata that reclassifies a token withdraws the approval."""
        document = plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),))
        requirement = self.issue(document)[0]
        self.approve(requirement)
        self.resolver.records["daemon-reload"] = (rental(),)
        verdict = self.preflight(requirement, document)
        self.assertFalse(verdict.admit)
        self.assertIn("changed after approval", verdict.reason)


class StandingUseAccountingTests(ServiceCase):
    """Finding 8: rate accounting is durable and race-safe at commit time."""

    def service_with_grant(self, connection=None, max_uses: int = 2):
        return self.make_service(
            connection=connection,
            standing_grants=(standing_grant(max_uses=max_uses),),
        )

    def test_commit_charges_the_budget_and_then_fails_closed(self) -> None:
        service = self.service_with_grant(max_uses=2)
        document = plan((owned_step(),))
        for _ in range(2):
            decision = service.authorize(document)
            self.assertIs(decision.plan_authority, Authority.STANDING_CONSENT)
            service.commit_standing_uses(document, decision)
        exhausted = service.authorize(document)
        self.assertIs(exhausted.plan_authority, Authority.EXACT_HUMAN)
        with self.assertRaises(AuthorizationError):
            service.commit_standing_uses(document, decision)

    def test_commit_rederives_and_rejects_a_tampered_decision(self) -> None:
        service = self.service_with_grant()
        document = plan((owned_step(),))
        decision = service.authorize(document)
        forged = dataclasses.replace(decision, plan_hash="0" * 64)
        with self.assertRaisesRegex(AuthorizationError, "rederived"):
            service.commit_standing_uses(document, forged)

    def test_accounting_survives_restart(self) -> None:
        path = self.temp_db_path()
        service = self.service_with_grant(connection=sqlite3.connect(path), max_uses=1)
        document = plan((owned_step(),))
        service.commit_standing_uses(document, service.authorize(document))
        restarted = self.service_with_grant(connection=sqlite3.connect(path), max_uses=1)
        self.assertIs(
            restarted.authorize(document).plan_authority, Authority.EXACT_HUMAN
        )


class PayloadApprovalTests(ServiceCase):
    def mixed_plan(self) -> Plan:
        return plan((
            step(step_id="step-1", arguments={"argv": ["docker", "restart", "C.51217040"]}),
            step(
                step_id="step-2", operation="read_tenant_payload",
                arguments={"rental_id": "51217040", "purpose": "inspect OOM logs",
                           "source": "container stdout logs"},
            ),
        ))

    def test_payload_access_needs_its_own_purpose_limited_card(self) -> None:
        requirements = self.issue(self.mixed_plan())
        self.assertEqual(len(requirements), 2)
        mutation, payload = requirements
        self.assertEqual(mutation.step_ids, ("step-1",))
        self.assertIsNone(mutation.payload_purpose)
        self.assertEqual(payload.step_ids, ("step-2",))
        self.assertEqual(payload.payload_purpose, "inspect OOM logs")
        self.assertEqual(payload.payload_source, "container stdout logs")
        self.assertEqual(len(payload.tenant_bindings), 1)
        self.assertIn(
            "purpose-limited tenant payload access",
            render_approval_card(payload).decode("utf-8"),
        )

    def test_a_mutation_grant_does_not_cover_the_payload_step(self) -> None:
        mutation, payload = self.issue(self.mixed_plan())
        grant = self.approve(mutation)
        self.assertNotIn("step-2", grant.step_ids)
        self.assertIsNone(self.service.get_grant(payload.requirement_id))

    def test_payload_without_purpose_or_source_blocks_toward_replanning(self) -> None:
        vague = plan((
            step(operation="read_tenant_payload", arguments={"rental_id": "51217040"}),
        ))
        with self.assertRaisesRegex(AuthorizationBlocked, "purpose"):
            self.issue(vague)

    def test_payload_without_a_resolvable_rental_blocks(self) -> None:
        """Finding 6: a payload approval must bind at least one exact rental."""
        unbound = plan((
            step(operation="read_tenant_payload",
                 arguments={"purpose": "inspect OOM logs",
                            "source": "container stdout logs"}),
        ))
        with self.assertRaisesRegex(AuthorizationBlocked, "exact rental binding"):
            self.issue(unbound)


class ConcurrencyRestartTests(ServiceCase):
    """Approvals, denials, and accounting hold across connections and restarts."""

    def two_services(self):
        path = self.temp_db_path()
        first = self.make_service(connection=sqlite3.connect(path))
        second = self.make_service(connection=sqlite3.connect(path))
        return path, first, second

    def host_plan(self) -> Plan:
        return plan((step(arguments={"argv": ["systemctl", "daemon-reload"]}),))

    def test_two_connections_cannot_both_approve_one_requirement(self) -> None:
        _, first, second = self.two_services()
        requirement = self.issue(self.host_plan(), service=first)[0]
        self.approve(requirement, service=second)
        with self.assertRaisesRegex(ApprovalRejected, "already consumed"):
            self.approve(requirement, service=first)

    def test_a_denial_on_one_connection_blocks_approval_on_another(self) -> None:
        _, first, second = self.two_services()
        requirement = self.issue(self.host_plan(), service=first)[0]
        self.deny(requirement, service=first)
        with self.assertRaisesRegex(ApprovalRejected, "denied"):
            self.approve(requirement, service=second)

    def test_denial_and_grant_survive_restart(self) -> None:
        path = self.temp_db_path()
        service = self.make_service(connection=sqlite3.connect(path))
        denied = self.issue(self.host_plan(), service=service)[0]
        self.deny(denied, service=service)
        approved_plan = plan(
            (step(arguments={"argv": ["systemctl", "restart", "chronyd"]}),),
            plan_id="plan-2",
        )
        approved = self.issue(approved_plan, service=service)[0]
        self.approve(approved, service=service)
        restarted = self.make_service(connection=sqlite3.connect(path))
        self.assertIsNotNone(restarted.get_denial(denied.requirement_id))
        with self.assertRaisesRegex(ApprovalRejected, "denied"):
            self.approve(denied, service=restarted)
        self.assertIsNotNone(restarted.get_grant(approved.requirement_id))
        verdict = restarted.preflight(
            approved.requirement_id, approved_plan,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertTrue(verdict.admit)


class SchemaMigrationTests(ServiceCase):
    """Finding 9: schema versions migrate deterministically or fail closed."""

    V1_SCHEMA = """
        CREATE TABLE tc_plan_authz_schema (
          namespace TEXT PRIMARY KEY CHECK(namespace = 'plan-authz'),
          version INTEGER NOT NULL
        );
        CREATE TABLE tc_plan_authz_requirements (
          requirement_id TEXT PRIMARY KEY,
          plan_hash TEXT NOT NULL,
          card_hash TEXT NOT NULL UNIQUE,
          nonce TEXT NOT NULL UNIQUE,
          document_json TEXT NOT NULL,
          issued_utc TEXT NOT NULL,
          expires_utc TEXT NOT NULL
        );
        CREATE TABLE tc_plan_authz_nonces (
          nonce TEXT PRIMARY KEY,
          requirement_id TEXT NOT NULL UNIQUE,
          recorded_utc TEXT NOT NULL
        );
        CREATE TABLE tc_plan_authz_decisions (
          requirement_id TEXT PRIMARY KEY,
          nonce TEXT NOT NULL UNIQUE,
          card_hash TEXT NOT NULL,
          group_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          display_name TEXT NOT NULL,
          via_callback INTEGER NOT NULL,
          occurred_utc TEXT NOT NULL,
          verified_utc TEXT NOT NULL,
          grant_id TEXT NOT NULL UNIQUE,
          grant_json TEXT NOT NULL
        );
        INSERT INTO tc_plan_authz_schema(namespace, version) VALUES ('plan-authz', 1);
        INSERT INTO tc_plan_authz_requirements VALUES
          ('req-v1', 'aa', 'bb', 'nonce-v1-0000001', '{}', '2026-01-01T00:00:00.000000Z',
           '2026-01-01T00:05:00.000000Z');
        INSERT INTO tc_plan_authz_nonces VALUES
          ('nonce-consumed-1', 'req-old', '2026-01-01T00:01:00.000000Z');
    """

    def test_v1_databases_migrate_and_v1_authority_fails_closed(self) -> None:
        path = self.temp_db_path()
        seed = sqlite3.connect(path)
        seed.executescript(self.V1_SCHEMA)
        seed.commit()
        seed.close()
        service = self.make_service(connection=sqlite3.connect(path))
        version = service.db.execute(
            "SELECT version FROM tc_plan_authz_schema WHERE namespace = 'plan-authz'"
        ).fetchone()[0]
        self.assertEqual(version, AUTHZ_SCHEMA_VERSION)
        # v1 requirements are archived: they can authorize nothing under v2.
        self.assertIsNone(service.get_requirement("req-v1"))
        archived = service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements_v1"
        ).fetchone()[0]
        self.assertEqual(archived, 1)
        # Consumed nonces are preserved so migration never revives a replay.
        preserved = service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_nonces WHERE nonce = 'nonce-consumed-1'"
        ).fetchone()[0]
        self.assertEqual(preserved, 1)
        # The migrated database serves fresh v2 requirements normally.
        requirement = self.issue(plan((step(),)), service=service)[0]
        self.assertIsNotNone(service.get_requirement(requirement.requirement_id))

    def test_an_unknown_newer_schema_version_fails_closed(self) -> None:
        path = self.temp_db_path()
        seed = sqlite3.connect(path)
        seed.executescript(
            """CREATE TABLE tc_plan_authz_schema (
                 namespace TEXT PRIMARY KEY CHECK(namespace = 'plan-authz'),
                 version INTEGER NOT NULL
               );
               INSERT INTO tc_plan_authz_schema(namespace, version)
                 VALUES ('plan-authz', 99);"""
        )
        seed.commit()
        seed.close()
        with self.assertRaisesRegex(AuthorizationError, "not supported"):
            self.make_service(connection=sqlite3.connect(path))


class Review344508Regressions(ServiceCase):
    def test_nested_literal_programs_bind_the_exact_alias(self) -> None:
        programs = (
            'new-tool "the-lonely-"tenant',
            "new-tool the-'lonely'-tenant",
            r"new-tool the-lonely\-tenant",
        )
        for program in programs:
            for shell in ("sh", "bash", "unrecognized-wrapper"):
                command = f"{shell} -c {shlex.quote(program)}"
                nested = f"sh -c {shlex.quote(command)}"
                for arguments in ({"command": command}, {"command": nested},
                                  {"argv": [shell, "-c", program]}):
                    with self.subTest(arguments):
                        requirement = self.issue(plan((step(arguments=arguments),)))[0]
                        self.assertTrue(requirement.tenant)
                        self.assertEqual(requirement.steps[0]["reference_bindings"],
                                         {"the-lonely-tenant": "51217040"})
                        self.assertEqual(requirement.tenant_bindings, (rental(),))

    def test_nested_programs_in_every_executable_subtree_bind_aliases(self) -> None:
        command = 'bash -c \'new-tool "the-lonely-"tenant\''
        for change in (
            {"arguments": {"nested": {"description": command}}},
            {"rollback": {"command": command}},
            {"checkpoint": {"capture": {"command": command}}},
            {"preconditions": ({"command": command},)},
            {"postconditions": ({"command": command},)},
            {"resources": (command,)},
        ):
            with self.subTest(change):
                requirement = self.issue(plan((step(**change),)))[0]
                self.assertTrue(requirement.tenant)
                self.assertEqual(requirement.steps[0]["reference_bindings"],
                                 {"the-lonely-tenant": "51217040"})
        with self.assertRaises(ContractError):
            step(artifacts=(command,))

    def test_unresolved_nested_programs_never_issue_generic_approval(self) -> None:
        command = 'new-tool "the-lonely-"tenant'
        for _ in range(10):
            command = 'sh -c "' + command.replace('\\', '\\\\').replace('"', '\\"') + '"'
        for index, program in enumerate((
            "new-tool 'the-lonely-tenant", "new-tool ${TARGET}",
            "new-tool C.*", "new-tool $(discover)", command,
        )):
            with self.subTest(index=index), self.assertRaises(AuthorizationBlocked):
                self.issue(plan((step(arguments={
                    "command": f"bash -c {shlex.quote(program)}",
                }),)))
        self.assertEqual(self.service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements"
        ).fetchone()[0], 0)

    def test_nested_literal_container_binding_without_classifier(self) -> None:
        risk = derive_step(step(arguments={
            "command": 'sh -c \'new-tool "C.51217"040\'',
        }))
        self.assertIs(risk.authority, Authority.ALWAYS_APPROVE_TENANT)
        self.assertIn("C.51217040", risk.tenant_references)

    def test_preconditions_require_recognized_deterministic_contracts(self) -> None:
        service = self.make_service(standing_grants=(standing_grant(),))
        good = owned_step()
        for check in (
            {}, {"check": None}, {"check": ""}, {"check": {}},
            {"check": "looks good"}, {"check": "identity"},
            {"check": "identity", "machine_id": "17050"},
            {"check": "identity", "machine_id": None},
            {"check": "identity", "machine_id": "17049", "extra": "opaque"},
            {"command": "true"}, {"check": "sh -c true"},
            {**dict(good.postconditions[0]), "expected": {}},
            {**dict(good.postconditions[0]), "expected": {"active": None}},
            {**dict(good.postconditions[0]), "resource": "other-resource"},
        ):
            with self.subTest(check):
                document = plan((owned_step(preconditions=(check,)),))
                self.assertIs(service.authorize(document).plan_authority,
                              Authority.EXACT_HUMAN)
                self.assertEqual(len(self.issue(document, service=service)), 1)
        for checks in (good.preconditions, good.postconditions,
                       (*good.preconditions, *good.postconditions)):
            with self.subTest(checks):
                self.assertIs(service.authorize(plan((owned_step(preconditions=checks),))).plan_authority,
                              Authority.STANDING_CONSENT)
        # The outer plan contract already rejects absent/non-object checks.
        for checks in ((), (None,)):
            with self.subTest(checks), self.assertRaises(ContractError):
                owned_step(preconditions=checks)

    def test_descriptive_prose_is_literal_evidence(self) -> None:
        service = self.make_service(standing_grants=(standing_grant(),))
        for prose in ("restart the operator's exporter", "ready?", "capture *.log",
                      "cost $5", "safe & reversible", 'unmatched "quote',
                      "run_shell", "explain $(discovery) and `substitution`"):
            with self.subTest(prose):
                document = plan((owned_step(effects=(Effect(
                    "effect-1", prose, owned_component=True,
                ),)),))
                self.assertIs(service.authorize(document).plan_authority,
                              Authority.STANDING_CONSENT)
                exact = plan((dataclasses.replace(
                    step(rollback_reason=prose),
                    effects=(Effect("effect-1", prose),), expected_interruption=prose,
                ),))
                requirement = self.issue(exact)[0]
                restored = requirement.steps[0]["step"]
                self.assertEqual(restored["effects"][0]["description"], prose)
                self.assertEqual(restored["rollback_impossible_reason"], prose)
                self.assertEqual(restored["expected_interruption"], prose)
                self.assertEqual(requirement.plan_hash, exact.content_hash)

    def test_literal_tenant_evidence_in_prose_remains_covered(self) -> None:
        document = plan((step(effects=(Effect(
            "effect-1", "the operator's C.51217040 might be interrupted?",
        ),)),))
        requirement = self.issue(document)[0]
        self.assertTrue(requirement.tenant)
        self.assertEqual(requirement.steps[0]["reference_bindings"],
                         {"C.51217040": "51217040"})


class Review021Regressions(ServiceCase):
    def alias_plan(self) -> Plan:
        return plan((step(arguments={"command": "docker stop the-lonely-tenant"}),))

    def test_embedded_aliases_are_classified_without_a_command_whitelist(self) -> None:
        for command in (
            "docker stop the-lonely-tenant",
            "new-unrecognized-tool --target=the-lonely-tenant",
            "new-tool 'the-lonely-tenant'",
            'new-tool "the-lonely-"tenant',
            "new-tool container:the-lonely-tenant/state",
        ):
            with self.subTest(command):
                requirement = self.issue(plan((step(arguments={"command": command}),)))[0]
                self.assertEqual(requirement.steps[0]["reference_bindings"],
                                 {"the-lonely-tenant": "51217040"})
                self.assertEqual(requirement.tenant_bindings, (rental(),))

    def test_dynamic_or_ambiguous_command_syntax_blocks(self) -> None:
        for command in (
            "docker stop $1", "docker stop ${TARGET}", "docker stop C.[12]",
            "docker stop $(discover)", "docker stop 'the-lonely-tenant",
        ):
            with self.subTest(command), self.assertRaises(AuthorizationBlocked):
                self.issue(plan((step(arguments={"command": command}),)))

    def test_static_analysis_has_a_total_candidate_bound(self) -> None:
        document = plan((step(arguments={
            "values": [f"literal-{index}" for index in range(1024)],
            "more": [f"component-{index}-a:b:c:d:e:f:g:h:i:j" for index in range(1024)],
            "large": [" ".join(f"resource-{i}-{j}" for j in range(10)) for i in range(800)],
        }),))
        with self.assertRaisesRegex(AuthorizationBlocked, "bounds"):
            self.service.authorize(document)

    def test_alias_repointing_before_approval_rejects_even_with_old_rental_present(self) -> None:
        requirement = self.issue(self.alias_plan())[0]
        self.resolver.records["the-lonely-tenant"] = (rental("60000001", "C.60000001"),)
        self.assertEqual(self.resolver.resolve("51217040"), (rental(),))
        with self.assertRaisesRegex(ApprovalRejected, "identity changed"):
            self.approve(requirement)
        self.assertIsNone(self.service.get_grant(requirement.requirement_id))

    def test_original_alias_and_complete_record_revalidated_at_preflight(self) -> None:
        document = self.alias_plan()
        requirement = self.issue(document)[0]
        self.approve(requirement)
        for records in (
            (rental("60000001", "C.60000001"),), (rental(owner="another-owner"),),
            (rental(generation="gen-2"),), (rental(gpus=("GPU-4",)),), (),
        ):
            with self.subTest(records):
                self.resolver.records["the-lonely-tenant"] = records
                verdict = self.service.preflight(
                    requirement.requirement_id, document,
                    current_evidence_revision=EVIDENCE, current_machine_id="17049",
                )
                self.assertFalse(verdict.admit)

    def test_one_bound_step_cannot_cover_an_unresolved_tenant_step(self) -> None:
        for unresolved in (
            step(step_id="step-2", effects=(effect(tenant=True),)),
            step(step_id="step-2", operation="tenant_restart", arguments={}),
        ):
            with self.subTest(unresolved.operation), self.assertRaisesRegex(
                AuthorizationBlocked, "step-2.*exact rental binding"
            ):
                self.issue(plan((self.alias_plan().steps[0], unresolved)))
        self.assertEqual(self.service.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_requirements"
        ).fetchone()[0], 0)

    def test_binding_mapping_roundtrips_and_changes_the_card_hash(self) -> None:
        other = rental("60000001", "C.60000001")
        self.resolver.records["second-alias"] = (other,)
        document = plan((self.alias_plan().steps[0], step(
            step_id="step-2", arguments={"command": "novel-tool second-alias"}
        )))
        requirement = self.issue(document)[0]
        restored = ApprovalRequirement.from_document(requirement.to_document())
        self.assertEqual(restored, requirement)
        changed = requirement.to_document()
        changed["steps"][0]["reference_bindings"]["the-lonely-tenant"] = "60000001"
        changed["steps"][1]["reference_bindings"]["second-alias"] = "51217040"
        swapped = ApprovalRequirement.from_document(changed)
        self.assertNotEqual(swapped.card_hash, requirement.card_hash)
        changed["steps"][1]["reference_bindings"] = {}
        with self.assertRaisesRegex(AuthorizationError, "exact rental binding"):
            ApprovalRequirement.from_document(changed)

    def test_standing_checks_entire_step_for_opaque_or_dynamic_content(self) -> None:
        service = self.make_service(standing_grants=(standing_grant(),))
        changes = (
            {"rollback": {"operation": "run_shell", "arguments": {"argv": ["reboot"]}}},
            {"rollback": {"operation": "reboot"}},
            {"preconditions": ({"check": "identity", "nested": {"command": "reboot"}},)},
            {"preconditions": ({"operation": "reboot"},)},
            {"checkpoint": {"capture": {"script": "reboot"}}},
            {"postconditions": ({"check": "verified", "command": "reboot"},)},
        )
        for change in changes:
            with self.subTest(change):
                decision = service.authorize(plan((owned_step(**change),)))
                self.assertIs(decision.plan_authority, Authority.EXACT_HUMAN)
        for change in (
            {"rollback": {"restore": "$SNAPSHOT"}},
            {"checkpoint": {"capture": "$(reboot)"}},
            {"preconditions": ({"check": "${STATE}"},)},
            {"postconditions": ({"check": "unit-*"},)},
        ):
            with self.subTest(change), self.assertRaises(AuthorizationBlocked):
                service.authorize(plan((owned_step(**change),)))
        # Artifacts have an even stricter plan-level digest contract.
        with self.assertRaises(ContractError):
            owned_step(artifacts=("$(reboot)",))

    def test_unknown_or_empty_checkpoint_and_verification_shapes_fail_closed(self) -> None:
        service = self.make_service(standing_grants=(standing_grant(),))
        good = owned_step()
        invalid = [
            {"checkpoint": {"capture": "take care"}},
            {"checkpoint": {"capture": None}},
            {"checkpoint": {**dict(good.checkpoint), "fields": []}},
            {"checkpoint": {**dict(good.checkpoint), "resources": []}},
            {"checkpoint": {**dict(good.checkpoint), "artifact": ""}},
        ]
        invalid.extend({"postconditions": (check,)} for check in (
            {"check": None}, {"check": "looks good"}, {"check": {}},
            {"kind": "unknown", "resource": "service:prometheus-exporter", "expected": {"active": True}},
            {**dict(good.postconditions[0]), "expected": {}},
            {**dict(good.postconditions[0]), "expected": {"active": None}},
            {**dict(good.postconditions[0]), "expected": {"active": ""}},
            {**dict(good.postconditions[0]), "expected": {"active": {"anything": True}}},
            {**dict(good.postconditions[0]), "extra": "unrecognized"},
        ))
        for change in invalid:
            with self.subTest(change):
                self.assertIs(service.authorize(plan((owned_step(**change),))).plan_authority,
                              Authority.EXACT_HUMAN)
        self.assertIs(service.authorize(plan((owned_step(operation="novel_owned_operation"),))).plan_authority,
                      Authority.STANDING_CONSENT)

    def test_commit_reads_revocations_and_budget_after_writer_lock_two_connections(self) -> None:
        for mutation in ("revocation", "last-budget-slot"):
            with self.subTest(mutation):
                path = self.temp_db_path()
                writer = self.make_service(sqlite3.connect(path), standing_grants=(standing_grant(max_uses=1),))
                runner = self.make_service(
                    sqlite3.connect(path, timeout=5, check_same_thread=False),
                    standing_grants=(standing_grant(max_uses=1),),
                )
                document = plan((owned_step(),))
                decision = runner.authorize(document)
                writer.db.execute("BEGIN IMMEDIATE")
                if mutation == "revocation":
                    writer.db.execute(
                        "INSERT INTO tc_plan_authz_standing_revocations VALUES (?, ?, ?, ?)",
                        ("standing-1", "operator:1", "withdrawn", NOW.isoformat()),
                    )
                else:
                    writer.rate_accountant.record_use("standing-1", document.content_hash, "step-1", NOW)
                begin_attempted = threading.Event()
                statements: list[str] = []
                errors: list[Exception] = []

                def trace(sql):
                    statements.append(sql)
                    if sql == "BEGIN IMMEDIATE":
                        begin_attempted.set()

                def commit():
                    try:
                        runner.commit_standing_uses(document, decision)
                    except Exception as error:
                        errors.append(error)

                runner.db.set_trace_callback(trace)
                thread = threading.Thread(target=commit)
                thread.start()
                try:
                    self.assertTrue(begin_attempted.wait(3), "runner must attempt its write lock")
                finally:
                    writer.db.commit()
                    thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(statements[0], "BEGIN IMMEDIATE")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], AuthorizationError)
                self.assertIn("rederived", str(errors[0]))
                self.assertEqual(writer.rate_accountant.uses_since("standing-1", NOW),
                                 0 if mutation == "revocation" else 1)

    def test_v2_requirements_are_archived_without_reviving_authority_or_budget(self) -> None:
        path = self.temp_db_path()
        original = self.make_service(sqlite3.connect(path))
        requirement = self.issue(self.alias_plan(), service=original)[0]
        self.approve(requirement, service=original)
        legacy = requirement.to_document()
        legacy["schema_version"] = 2
        for item in legacy["steps"]:
            del item["reference_bindings"]
        original.db.execute("UPDATE tc_plan_authz_requirements SET document_json = ?",
                            (canonical_json(legacy).decode(),))
        original.db.execute("UPDATE tc_plan_authz_schema SET version = 2")
        original.rate_accountant.record_use("standing-1", requirement.plan_hash, "step-1", NOW)
        original.db.commit()
        original.revoke_standing_grant("standing-1", revoked_by="operator:1", reason="withdrawn")
        migrated = self.make_service(sqlite3.connect(path), standing_grants=(standing_grant(),))
        self.assertIsNone(migrated.get_requirement(requirement.requirement_id))
        self.assertIsNone(migrated.get_grant(requirement.requirement_id))
        self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM tc_plan_authz_requirements_v2").fetchone()[0], 1)
        self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM tc_plan_authz_decisions_v2").fetchone()[0], 1)
        self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM tc_plan_authz_nonces").fetchone()[0], 1)
        self.assertEqual(migrated.rate_accountant.uses_since("standing-1", NOW), 1)
        self.assertEqual(migrated.active_standing_grants(), ())
        with self.assertRaises(ApprovalRejected):
            self.approve(requirement, service=migrated)


class FeatureFlagTests(unittest.TestCase):
    def test_disabled_by_default_and_off_without_the_flag(self) -> None:
        self.assertFalse(plan_authorization_enabled({}))
        self.assertIsNone(build_plan_authorization({}))

    def test_enabled_service_fails_closed_without_injected_trust_roots(self) -> None:
        with self.assertRaises(AuthorizationError):
            build_plan_authorization({"TERRACOMPUTE_PLAN_AUTHORIZATION": "1"})

    def test_enabled_service_constructs_with_full_injection(self) -> None:
        service = build_plan_authorization(
            {"TERRACOMPUTE_PLAN_AUTHORIZATION": "1"},
            connection=sqlite3.connect(":memory:"),
            membership=FakeMembership(),
            resolver=FakeResolver({}),
            policy_revision=POLICY,
            approval_group_id=GROUP,
        )
        self.assertIsInstance(service, PlanAuthorizationService)

    def test_production_runtime_does_not_reference_phase3(self) -> None:
        """The existing action loop stays unchanged until explicit wiring."""
        source_root = Path(__file__).resolve().parent.parent / "src" / "terracompute_ops"
        for name in ("cli.py", "runtime_entrypoints.py", "action_service.py",
                     "telegram.py", "acting.py", "diagnosing.py"):
            self.assertNotIn(
                "plan_authorization", (source_root / name).read_text(encoding="utf-8"),
                name,
            )


class IntegrationTests(ServiceCase):
    def test_novel_host_action_and_tenant_restart_full_lifecycle(self) -> None:
        document = plan((
            step(step_id="step-1",
                 arguments={"argv": ["ethtool", "-s", "enp1s0", "wol", "g"]}),
            step(step_id="step-2",
                 arguments={"argv": ["docker", "restart", "C.51217040"]}),
        ))
        decision = self.service.authorize(document)
        self.assertIs(decision.plan_authority, Authority.ALWAYS_APPROVE_TENANT)
        self.assertEqual(decision.approval_step_ids, ("step-1", "step-2"))

        requirement = self.service.issue_requirements(
            document, decision, requester_id="operator:1",
            current_evidence_revision=EVIDENCE,
        )[0]
        stored = self.service.get_requirement(requirement.requirement_id)
        self.assertEqual(render_approval_card(stored), render_approval_card(requirement))
        action, resolved = self.service.resolve_callback(requirement.approve_callback_data)
        self.assertTrue(action.approve)
        self.assertEqual(resolved.requirement_id, requirement.requirement_id)

        grant = self.approve(requirement)
        self.assertEqual(grant.plan_hash, document.content_hash)
        verdict = self.service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertTrue(verdict.admit)

        with self.assertRaisesRegex(ApprovalRejected, "already consumed"):
            self.approve(requirement)

        replaced = rental(generation="gen-2", status="stopped")
        self.resolver.records["51217040"] = (replaced,)
        refused = self.service.preflight(
            requirement.requirement_id, document,
            current_evidence_revision=EVIDENCE, current_machine_id="17049", now=self.now,
        )
        self.assertFalse(refused.admit)
        self.assertIn("changed after approval", refused.reason)

    def test_stale_evidence_blocks_issuance_before_a_card_exists(self) -> None:
        document = plan((step(),))
        decision = self.service.authorize(document)
        with self.assertRaisesRegex(AuthorizationBlocked, "replan"):
            self.service.issue_requirements(
                document, decision, requester_id="operator:1",
                current_evidence_revision="ev-2",
            )


if __name__ == "__main__":
    unittest.main()
