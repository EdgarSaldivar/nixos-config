"""Typed bridge from durable shadow evidence to rollout commissioning.

This module grants no authority.  It rederives one attestation's exact canary
document from a :class:`ShadowLedger` and returns a typed, non-authoritative
receipt.  ``RolloutLedger`` remains responsible for pins, prerequisite order,
and the final authority decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .plans import MACHINE_ID, ContractError
from .rollout import CommissioningAttestation, RolloutCapability
from .shadow_rollout import (
    CAPABILITY_COVERAGE,
    COVERAGE_ORDER,
    MINIMUM_SAMPLE_FLOOR,
    CanarySummary,
    CoverageClass,
    ShadowError,
    ShadowLedger,
)


class CommissioningEvidenceError(RuntimeError):
    """The presented commissioning evidence cannot authorize rollout."""


@dataclass(frozen=True)
class VerifiedCommissioningEvidence:
    """Freshly rederived evidence bindings; never an authority token."""

    attestation_id: str
    capability: RolloutCapability
    machine_id: str
    policy_revision: str
    config_revision: str
    evidence_revision: str
    summary_hash: str
    criteria_hash: str
    evidence_root: str
    coverage_counts: Mapping[CoverageClass, int]

    execution_authority = False

    def __post_init__(self) -> None:
        if type(self.capability) is not RolloutCapability:
            raise ContractError("verified evidence capability must be typed")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"verified evidence is restricted to machine {MACHINE_ID}")
        counts = dict(self.coverage_counts)
        if set(counts) != set(COVERAGE_ORDER):
            raise ContractError("verified evidence must cover every coverage class")
        if any(type(key) is not CoverageClass or type(value) is not int or value < 0
               for key, value in counts.items()):
            raise ContractError("verified evidence coverage counts are malformed")
        object.__setattr__(self, "coverage_counts", MappingProxyType(
            {kind: counts[kind] for kind in COVERAGE_ORDER}
        ))


@dataclass(frozen=True)
class CommissioningEvidenceVerifier:
    """Concrete production verifier backed by one durable ``ShadowLedger``."""

    shadow: ShadowLedger

    def __post_init__(self) -> None:
        if type(self.shadow) is not ShadowLedger:
            raise ContractError("commissioning evidence requires a concrete ShadowLedger")

    def verify(
        self, attestation: CommissioningAttestation,
    ) -> VerifiedCommissioningEvidence:
        return self.verify_many((attestation,))[0]

    def verify_many(
        self, attestations: tuple[CommissioningAttestation, ...],
    ) -> tuple[VerifiedCommissioningEvidence, ...]:
        """Verify a bounded attestation set with one durable shadow scan."""
        if (
            type(attestations) is not tuple
            or not attestations
            or len(attestations) > len(RolloutCapability)
            or any(type(item) is not CommissioningAttestation for item in attestations)
            or len({item.attestation_id for item in attestations}) != len(attestations)
        ):
            raise ContractError("evidence verification requires unique typed attestations")
        try:
            summaries = self.shadow.verify_evidence_many(tuple(
                item.canary_evidence for item in attestations
            ))
        except ShadowError as error:
            raise CommissioningEvidenceError(
                "durable shadow evidence could not be verified"
            ) from error
        verified: list[VerifiedCommissioningEvidence] = []
        for attestation, summary in zip(attestations, summaries):
            self._verify_bindings(attestation, summary)
            verified.append(VerifiedCommissioningEvidence(
                attestation_id=attestation.attestation_id,
                capability=summary.capability,
                machine_id=summary.criteria.machine_id,
                policy_revision=summary.criteria.policy_revision,
                config_revision=summary.criteria.config_revision,
                evidence_revision=summary.criteria.evidence_revision,
                summary_hash=summary.content_hash,
                criteria_hash=summary.criteria.content_hash,
                evidence_root=summary.evidence_root,
                coverage_counts=summary.coverage_counts,
            ))
        return tuple(verified)

    @staticmethod
    def _verify_bindings(
        attestation: CommissioningAttestation, summary: CanarySummary,
    ) -> None:
        criteria = summary.criteria
        if summary.capability is not attestation.capability:
            raise CommissioningEvidenceError(
                "canary evidence is bound to a different rollout capability"
            )
        if criteria.machine_id != attestation.machine_id:
            raise CommissioningEvidenceError("canary evidence is bound to another machine")
        for name in ("policy_revision", "config_revision", "evidence_revision"):
            if getattr(criteria, name) != getattr(attestation, name):
                raise CommissioningEvidenceError(
                    f"canary evidence {name} does not match the attestation"
                )
        for coverage in CAPABILITY_COVERAGE[attestation.capability]:
            if (
                criteria.coverage_minimums[coverage] < MINIMUM_SAMPLE_FLOOR
                or summary.coverage_counts[coverage]
                < criteria.coverage_minimums[coverage]
            ):
                raise CommissioningEvidenceError(
                    f"canary evidence lacks required {coverage.value} coverage"
                )


__all__ = [
    "CommissioningEvidenceError", "CommissioningEvidenceVerifier",
    "VerifiedCommissioningEvidence",
]
