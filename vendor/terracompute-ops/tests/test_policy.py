from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from terracompute_ops.policy import (
    MACHINE_ID,
    ActionClass,
    ActionPolicy,
    ActionProposal,
    Mode,
    Ownership,
    PolicyDenied,
    PreActionEvidence,
    RentalImpact,
    SourceBinding,
    SourceState,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def proposal(
    action_class: ActionClass = ActionClass.GPU_RESET_REBIND,
    *,
    rentals: tuple[RentalImpact, ...] = (),
    mappings_known: bool = True,
    power_domain_proven: bool = False,
    claims_cold_gpu_power: bool = False,
    parameters: dict | None = None,
) -> ActionProposal:
    source_names = {
        ActionClass.GPU_RESET_REBIND: ("host", "inventory", "rentals"),
        ActionClass.BMC_POWER: ("bmc", "rentals"),
        ActionClass.HOST_REBOOT: ("host", "bmc", "rentals"),
    }.get(action_class, ("host", "rentals"))
    return ActionProposal.create(
        proposal_id="proposal-1",
        action_class=action_class,
        parameters=parameters or {"gpu_uuid": "GPU-1"},
        resource_ids=("GPU-1",),
        rental_impacts=rentals,
        affected_domains=("reset-domain:slot-1",),
        source_bindings=tuple(SourceBinding(name, f"{name}-r1") for name in source_names),
        evidence_revision="evidence-r1",
        policy_revision="policy-r1",
        stop_condition="stop unless the exact GPU postcondition is verified",
        clock=lambda: NOW,
        mappings_known=mappings_known,
        power_domain_proven=power_domain_proven,
        claims_cold_gpu_power=claims_cold_gpu_power,
    )


def evidence(value: ActionProposal, *, age: int = 0, **changes) -> PreActionEvidence:
    defaults = dict(
        machine_id=MACHINE_ID,
        target_identity_verified=True,
        evidence_revision=value.evidence_revision,
        sources=tuple(
            SourceState(item.source, item.revision, NOW - timedelta(seconds=age))
            for item in value.source_bindings
        ),
        resource_ids=value.resource_ids,
        rental_impacts=value.rental_impacts,
        affected_domains=value.affected_domains,
        mappings_known=value.mappings_known,
        power_domain_proven=value.power_domain_proven,
        evidence_ref="evidence:before",
        backup_ref="backup:snapshot",
        backup_succeeded=True,
    )
    defaults.update(changes)
    return PreActionEvidence(**defaults)


def commissioned(*actions: ActionClass) -> ActionPolicy:
    return ActionPolicy(
        mode=Mode.APPROVE,
        revision="policy-r1",
        enabled_actions=frozenset(actions),
        approval_group_id=-42,
    )


class ActionPolicyTests(unittest.TestCase):
    def test_defaults_are_observe_only_with_no_enabled_actions(self) -> None:
        configured = ActionPolicy()
        self.assertEqual(configured.mode, Mode.OBSERVE)
        self.assertEqual(configured.enabled_actions, frozenset())
        with self.assertRaisesRegex(PolicyDenied, "observe-only"):
            configured.validate_proposal(proposal(), NOW)

    def test_proposal_is_pinned_to_machine_17049_and_five_minutes(self) -> None:
        value = proposal()
        self.assertEqual(value.machine_id, 17049)
        self.assertEqual(value.expires_at, NOW + timedelta(minutes=5))
        with self.assertRaises(ValueError):
            ActionProposal(**{**value.__dict__, "machine_id": 17050})
        with self.assertRaises(ValueError):
            ActionProposal(**{**value.__dict__, "expires_at": NOW + timedelta(minutes=6)})

    def test_digest_binds_exact_parameters_resources_evidence_and_policy(self) -> None:
        first = proposal(parameters={"gpu_uuid": "GPU-1"})
        second = proposal(parameters={"gpu_uuid": "GPU-2"})
        self.assertNotEqual(first.digest, second.digest)
        self.assertIn("evidence_revision", first.exact_document())
        self.assertIn("policy_revision", first.exact_document())

    def test_parameters_are_deeply_immutable_and_exact_document_is_detached(self) -> None:
        source = {"plan": [{"target": "GPU-1"}], "options": {"force": False}}
        value = proposal(parameters=source)
        digest = value.digest

        source["plan"][0]["target"] = "GPU-2"
        with self.assertRaises(TypeError):
            value.parameters["plan"][0]["target"] = "GPU-3"
        with self.assertRaises(TypeError):
            value.parameters["plan"][0] = {"target": "GPU-4"}
        detached = value.exact_document()
        detached["parameters"]["plan"][0]["target"] = "GPU-5"

        self.assertEqual(value.parameters["plan"][0]["target"], "GPU-1")
        self.assertEqual(value.digest, digest)

    def test_arbitrary_shell_and_credentials_are_not_parameters(self) -> None:
        for parameters in (
            {"command": "reboot"},
            {"nested": {"token": "sensitive-marker"}},
            {"api_key": "sensitive-marker"},
            {"APIKey": "sensitive-marker"},
            {"nested": [{"access-key": "sensitive-marker"}]},
            {"authorization": "sensitive-marker"},
            {"auth URLs": ["sensitive-marker"]},
            {"note": "https://fixture.invalid/?api_key=sensitive-marker"},
            {"note": "https://user:sensitive-marker@fixture.invalid/"},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                proposal(parameters=parameters)

    def test_unknown_rental_ownership_is_never_eligible(self) -> None:
        value = proposal(
            rentals=(RentalImpact("contract-1", True, Ownership.UNKNOWN, True),)
        )
        with self.assertRaisesRegex(PolicyDenied, "ownership is unknown"):
            commissioned(ActionClass.GPU_RESET_REBIND).validate_proposal(value, NOW)

    def test_active_rental_impact_must_be_explicit_in_exact_proposal(self) -> None:
        value = proposal(
            rentals=(RentalImpact("contract-1", True, Ownership.TENANT, False),)
        )
        with self.assertRaisesRegex(PolicyDenied, "not explicitly approved"):
            commissioned(ActionClass.GPU_RESET_REBIND).validate_proposal(value, NOW)

    def test_changed_and_stale_live_evidence_fail(self) -> None:
        value = proposal()
        configured = commissioned(ActionClass.GPU_RESET_REBIND)
        stale = evidence(value, age=61)
        with self.assertRaisesRegex(PolicyDenied, "stale"):
            configured.validate_preconditions(value, stale, NOW)
        changed_sources = list(evidence(value).sources)
        changed_sources[0] = SourceState(changed_sources[0].source, "changed", NOW)
        with self.assertRaisesRegex(PolicyDenied, "changed"):
            configured.validate_preconditions(
                value, evidence(value, sources=tuple(changed_sources)), NOW
            )

    def test_changed_resources_rentals_and_evidence_revision_fail(self) -> None:
        value = proposal()
        configured = commissioned(ActionClass.GPU_RESET_REBIND)
        for changed, message in (
            ({"resource_ids": ("GPU-2",)}, "resources changed"),
            ({"evidence_revision": "new"}, "revision changed"),
            ({"rental_impacts": (RentalImpact("new", False, Ownership.NONE),)}, "rental"),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(PolicyDenied, message):
                configured.validate_preconditions(value, evidence(value, **changed), NOW)

    def test_backup_exception_only_bypasses_backup_not_identity_or_ownership(self) -> None:
        value = proposal()
        configured = commissioned(ActionClass.GPU_RESET_REBIND)
        no_backup = evidence(value, backup_succeeded=False, backup_ref=None)
        configured.validate_preconditions(value, no_backup, NOW, backup_exception=True)
        bad_identity = evidence(
            value, backup_succeeded=False, backup_ref=None, target_identity_verified=False
        )
        with self.assertRaisesRegex(PolicyDenied, "identity"):
            configured.validate_preconditions(
                value, bad_identity, NOW, backup_exception=True
            )

    def test_bmc_host_down_rule_does_not_require_ssh_but_requires_fresh_bmc(self) -> None:
        value = proposal(
            ActionClass.BMC_POWER,
            mappings_known=True,
            power_domain_proven=True,
            parameters={"operation": "power-on"},
        )
        configured = commissioned(ActionClass.BMC_POWER)
        configured.validate_preconditions(value, evidence(value), NOW)
        self.assertNotIn("host", {item.source for item in value.source_bindings})

    def test_no_cold_gpu_power_claim_without_power_domain_proof(self) -> None:
        with self.assertRaisesRegex(ValueError, "cold GPU power"):
            proposal(
                ActionClass.BMC_POWER,
                mappings_known=True,
                power_domain_proven=False,
                claims_cold_gpu_power=True,
            )


if __name__ == "__main__":
    unittest.main()
