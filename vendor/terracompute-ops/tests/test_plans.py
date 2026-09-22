from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

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
    canonical_json,
    strict_json_loads,
    utc_text,
)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def effect() -> Effect:
    return Effect("effect-1", "restart an owned monitor", owned_component=True)


def step(arguments=None, rollback=None) -> PlanStep:
    return PlanStep(
        step_id="step-1",
        operation="run_shell",
        arguments=arguments or {"argv": ["systemctl", "restart", "monitor.service"]},
        effects=(effect(),),
        affected_resources=("monitor.service",),
        preconditions=({"check": "identity", "machine_id": "17049"},),
        postconditions=({"check": "service-active", "unit": "monitor.service"},),
        checkpoint={"kind": "unit-state"},
        rollback=rollback or {"operation": "restore-unit-state"},
        rollback_impossible_reason=None,
        expected_interruption="monitoring gap under 10 seconds",
        max_execution_seconds=30,
        artifacts=("sha256:" + "a" * 64,),
    )


def plan(**changes) -> Plan:
    values = dict(
        plan_id="plan-1", task_id="task-1", version=1,
        objective="restore durable monitoring", evidence_revision="evidence-r1",
        assumptions=("the unit is controller-owned",),
        freshness_requirements=({"source": "target", "max_age_seconds": 60},),
        steps=(step(),), created_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    values.update(changes)
    return Plan(**values)


class ContractTests(unittest.TestCase):
    def test_canonical_json_and_hash_ignore_input_field_order(self) -> None:
        value = plan()
        reversed_document = dict(reversed(list(value.to_document().items())))
        loaded = Plan.from_json(json.dumps(reversed_document))
        self.assertEqual(loaded, value)
        self.assertEqual(loaded.content_hash, value.content_hash)
        self.assertEqual(value.canonical_json(), canonical_json(reversed_document))

    def test_duplicate_keys_unknown_fields_and_schema_versions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "duplicate JSON field"):
            strict_json_loads('{"schema_version":1,"schema_version":1}')
        document = plan().to_document()
        document["surprise"] = True
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            Plan.from_document(document)
        document.pop("surprise")
        document["schema_version"] = 2
        with self.assertRaisesRegex(ContractError, "unsupported"):
            Plan.from_document(document)

    def test_controls_credentials_and_nonfinite_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "control character"):
            step(arguments={"path": "bad\npath"})
        for arguments in (
            {"access_token": "not-stored"},
            {"nested": {"password": "not-stored"}},
            {"header": "Authorization: value"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ContractError, "credential"):
                    step(arguments=arguments)
        with self.assertRaisesRegex(ContractError, "non-finite"):
            step(arguments={"ratio": float("nan")})

    def test_credential_filter_rejects_argv_variants_urls_headers_and_keys(self) -> None:
        unsafe = (
            {"argv": ["client", "--token", "value"]},
            {"argv": ["client", "--api-key=value"]},
            {"parameters": ["client", "--token", "value"]},
            {"parameters": [{"name": "token", "value": "private"}]},
            {"parameters": [{"Name": "Authorization", "Value": "private"}]},
            {"parameters": [{"name": "-u", "value": "operator:private"}]},
            {"nested": {"values": [["client", "--passwd=private"]]}},
            {"parameters": ["client", "--passphrase", "private"]},
            {"parameters": ["client", "--pwd=private"]},
            {"parameters": ["client", "-u", "operator:private"]},
            {"parameters": ["client", "-uoperator:private"]},
            {"parameters": ["client", "--user=operator:private"]},
            {"parameters": ["client", "-H", "Authorization: Bearer private"]},
            {"parameters": ["client", "--header=Cookie: session=private"]},
            {"parameters": ["authorization", "private"]},
            {"token_value": "value"},
            {"passwd": "value"},
            {"passphrase": "value"},
            {"pass": "value"},
            {"passcode": "value"},
            {"pwd": "value"},
            {"nested": {"request_headers": {"X-Api-Key": "value"}}},
            {"url": "https://example.invalid/run?access_token=value"},
            {"url": "ssh://operator:private@example.invalid/host"},
            {"url": "https://private@example.invalid/host"},
            {"material": "-----BEGIN OPENSSH PRIVATE KEY-----"},
        )
        for arguments in unsafe:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ContractError, "credential"):
                    step(arguments=arguments)
        # Typed effect metadata declares secret use; it is not secret material.
        self.assertTrue(Effect("secret-effect", "worker uses an injected key", secrets=True).secrets)
        self.assertEqual(
            step(arguments={"parameters": ["client", "--user", "operator"]})
            .arguments["parameters"][2],
            "operator",
        )

    def test_artifacts_require_immutable_content_digests(self) -> None:
        for artifact in ("image:latest", "registry/image@latest", "sha256:not-a-digest"):
            with self.subTest(artifact=artifact):
                with self.assertRaisesRegex(ContractError, "content digests"):
                    replace(step(), artifacts=(artifact,))

    def test_from_document_rejects_non_arrays_bool_numbers_and_surrogates(self) -> None:
        document = plan().to_document()
        document["steps"] = {"0": document["steps"][0]}
        with self.assertRaisesRegex(ContractError, "JSON array"):
            Plan.from_document(document)
        document = plan().to_document()
        document["version"] = True
        with self.assertRaisesRegex(ContractError, "positive integer"):
            Plan.from_document(document)
        document = plan().to_document()
        document["schema_version"] = True
        with self.assertRaisesRegex(ContractError, "schema version"):
            Plan.from_document(document)
        with self.assertRaisesRegex(ContractError, "surrogate"):
            canonical_json({"value": "\ud800"})

    def test_timestamps_have_one_fixed_width_canonical_encoding(self) -> None:
        whole = NOW
        fractional = NOW.replace(microsecond=1)
        self.assertEqual(utc_text(whole), "2026-09-20T12:00:00.000000Z")
        self.assertEqual(len(utc_text(whole)), len(utc_text(fractional)))
        self.assertLess(utc_text(whole), utc_text(fractional))
        document = plan().to_document()
        document["created_at"] = "2026-09-20T12:00:00Z"
        with self.assertRaisesRegex(ContractError, "canonical timestamp"):
            Plan.from_document(document)

    def test_nested_values_cannot_mutate_after_hashing_and_documents_are_detached(self) -> None:
        arguments = {"items": [{"resource": "one"}]}
        value = plan(steps=(step(arguments=arguments),))
        digest = value.content_hash
        arguments["items"][0]["resource"] = "two"
        self.assertEqual(value.steps[0].arguments["items"][0]["resource"], "one")
        with self.assertRaises(TypeError):
            value.steps[0].arguments["items"][0]["resource"] = "three"
        detached = value.to_document()
        detached["steps"][0]["arguments"]["items"][0]["resource"] = "four"
        self.assertEqual(value.content_hash, digest)
        with self.assertRaises(FrozenInstanceError):
            value.objective = "changed"

    def test_executable_artifact_and_rollback_changes_change_the_hash(self) -> None:
        original = plan()
        changed_arguments = plan(steps=(step(arguments={"argv": ["false"]}),))
        changed_rollback = plan(steps=(step(rollback={"operation": "different"}),))
        changed_artifact = plan(steps=(replace(
            step(), artifacts=("sha256:" + "b" * 64,)
        ),))
        self.assertNotEqual(original.content_hash, changed_arguments.content_hash)
        self.assertNotEqual(original.content_hash, changed_rollback.content_hash)
        self.assertNotEqual(original.content_hash, changed_artifact.content_hash)

    def test_all_authorization_contracts_round_trip_strictly(self) -> None:
        value = plan()
        grant = ApprovalGrant(
            "grant-1", value.task_id, value.plan_id, value.content_hash, ("step-1",),
            ApprovalKind.EXACT_HUMAN, "operator-1", "policy-r1", value.evidence_revision,
            "nonce-1", NOW, NOW + timedelta(minutes=2),
        )
        lease = ExecutionLease(
            "lease-1", value.task_id, value.plan_id, value.content_hash, "step-1",
            grant.grant_id, grant.content_hash, grant.policy_revision, grant.evidence_revision,
            "worker-1", "execute-1", NOW, NOW + timedelta(seconds=30),
        )
        verification = Verification(
            "verification-1", value.task_id, value.plan_id, value.content_hash, "step-1",
            lease.lease_id, lease.content_hash,
            VerificationStatus.SUCCEEDED, ({"check": "service-active", "ok": True},),
            "evidence-r2", NOW,
        )
        self.assertEqual(lease.schema_version, 2)
        self.assertEqual(verification.schema_version, 2)
        for contract in (lease, verification):
            legacy = contract.to_document()
            legacy["schema_version"] = 1
            with self.assertRaisesRegex(ContractError, "unsupported"):
                type(contract).from_document(legacy)
        for contract in (effect(), step(), value, grant, lease, verification):
            with self.subTest(contract=type(contract).__name__):
                self.assertEqual(type(contract).from_json(contract.to_json()), contract)
                self.assertRegex(contract.content_hash, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
