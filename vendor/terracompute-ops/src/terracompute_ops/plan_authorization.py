"""Phase 3 plan/effect authorization: immutable plans are the authorization subject.

This module is the local, testable core of the rearchitecture's Phase 3
(docs/OPERATOR-AGENT-REARCHITECTURE.md).  It replaces the single
``ProposedAction.command`` authority model with deterministic decisions over
immutable multi-step :class:`~terracompute_ops.plans.Plan` documents.  Nothing
constructs it in the deployed runtime: it is inert until the feature flag and
its injected dependencies are wired in a later commissioning step, and the
existing production action loop keeps its current behaviour meanwhile.

What it decides, and what it deliberately does not do:

- Effect derivation is deterministic and covers the ENTIRE step document:
  operation, arguments, affected resources, preconditions, postconditions,
  checkpoint, rollback, and artifacts.  A rental named anywhere in that
  document is a tenant effect whether or not the plan admits it, and a trusted
  resolver-backed classifier additionally recognises exact aliases, container
  names, and numeric rental identities.  Dynamic resource selection -- globs,
  shell substitution, or runtime discovery expressions -- can name tenants
  that no static analysis can bind, so it BLOCKS toward an exact
  resource-bound replan instead of flowing to any authority.  ``run_shell``
  stays intent-neutral: a never-before-coded literal mutation is expressible
  and lands on exact human approval by DEFAULT.  Permanent refusal is reserved
  for the unrepresentable: credential-bearing input.
- Tenant work is a risk class, not a wall.  A tenant step is proposable, but
  its approval must bind the exact rentals resolved through trusted metadata
  -- never a model-provided name alone -- and a generic exact approval never
  substitutes for a missing tenant binding: a tenant-affecting group that
  cannot bind its rentals blocks toward replanning before any card exists.
  Reading tenant payload data is a separate, purpose-limited approval that
  must itself bind at least one exact rental.
- Standing consent fails closed.  A grant is structurally unable to name
  tenant, money, reachability, secret, host, or irreversible effect classes,
  never covers ``run_shell`` or opaque argument shapes, and additionally
  requires deterministic preconditions, a deterministic checkpoint, postcondition
  verification, a rollback, a blast-radius bound over exactly named
  resources, and a rate budget backed by durable accounting.  Without a
  durable rate accountant and a trusted classifier, standing consent simply
  does not apply.
- Approval authority is authenticated-callback-only for every requirement,
  not just tenant ones.  The compact callback protocol fits Telegram's
  64-byte ``callback_data`` bound and parses deterministically; the card
  bytes bind the plan hash and version, policy revision, evidence revision,
  the exact steps, requester and approval-group identity, machine ``17049``,
  the resolved tenant identities, an expiry, and a single-use nonce.  Cards
  are bounded to the transport: they split on section boundaries when that
  is safe and otherwise block for a smaller exact plan before anything is
  persisted.  An explicit denial durably consumes the requirement's nonce
  and can never become an approval; issued grants and standing grants have
  durable revocation paths that preflight honours.
- Requirements for one plan version are issued atomically, and issuance
  rederives the :class:`PlanDecision` internally, requiring exact equality
  with the caller's copy: no caller-supplied decision is trusted.  Preflight
  re-authorizes the exact plan -- including standing grant validity and
  predicates -- immediately before a (future) execution and refuses on any
  change.  It performs no execution and consumes nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .actions import MembershipVerifier
from .plans import (
    _CREDENTIAL_FLAG,
    _CREDENTIAL_VALUE,
    ApprovalGrant,
    ApprovalKind,
    MACHINE_ID,
    Plan,
    PlanStep,
    canonical_json,
    parse_utc,
    stable_hash,
    strict_json_loads,
    utc_text,
)

FEATURE_FLAG_ENV = "TERRACOMPUTE_PLAN_AUTHORIZATION"
AUTHZ_SCHEMA_VERSION = 3
APPROVAL_LIFETIME = timedelta(minutes=5)
MAX_MEMBERSHIP_AGE = timedelta(seconds=60)
MAX_TENANT_REFERENCES = 64
MAX_CLASSIFICATION_TOKENS = 8192
MAX_PROGRAM_NESTING = 8
MAX_ANALYSIS_CHARACTERS = 1024 * 1024

# Read primitives have agent authority. Phase 4 additionally recognizes an isolated
# credential-free workspace effect boundary below; arbitrary programs outside that
# boundary remain proposable with exact approval.
READ_ONLY_OPERATIONS = frozenset({"observe_target", "controller_state", "research"})

# Purpose-limited tenant payload inspection. Never covered by an ordinary tenant
# mutation approval; it gets its own card with its own scope and expiry.
TENANT_PAYLOAD_OPERATION = "read_tenant_payload"

# The one operation whose arguments are an opaque program by construction.
RUN_SHELL_OPERATION = "run_shell"

# Vast names every rental this way and a tenant cannot choose the name. Same
# anchoring as the docker proxy and authorization.TENANT, restated at this third
# doorway to the same machine.
TENANT_REFERENCE = re.compile(r"(?<![A-Za-z0-9._-])C\.[0-9]{1,20}(?![0-9A-Za-z._-])")

# Dynamic resource selection: globs, variable or command substitution, process
# substitution, or brace alternation.  Any of these can select a tenant at
# runtime, which no static binding can cover. Apply this only to executable or
# resource-bearing fields; descriptive prose remains literal evidence.
_DYNAMIC_SELECTION = re.compile(
    r"\$|`|\*|\?|\[[^\]]*\]|<\(|>\(|\{[^{}]*[,\.][^{}]*\}|(?:^|\s)~"
)

# Shell composition operators inside a read-only operation's arguments turn a
# named read into an unconstrained effectful program; those escalate to a person.
_EFFECT_CAPABLE_ARGUMENT = re.compile(r"[|;&<>]")

# Argument keys that carry an opaque program rather than structured intent.
# Standing consent fails closed on these; exact human approval still applies.
_OPAQUE_ARGUMENT_KEYS = frozenset(
    {"argv", "command", "cmd", "script", "shell", "exec", "eval", "code", "run", "operation"}
)

EFFECT_FLAG_NAMES = (
    "owned_component", "host", "reachability", "tenant", "external_commitment",
    "secrets", "irreversible",
)

# Effect classes that may never appear in a standing grant under the initial
# policy: anything a tenant would feel, money, reachability, secrets, host
# state, or a change that cannot be undone.
_STANDING_FORBIDDEN = frozenset(EFFECT_FLAG_NAMES) - {"owned_component"}

# Only rentals in a state the operator can act on may be bound; anything else
# means the evidence is stale or the resolver disagrees with reality.
ACTIONABLE_RENTAL_STATUSES = frozenset({"running", "stopped"})

# Compact callback protocol: "<prefix>:<nonce>".  The nonce alone identifies
# the requirement (it is unique and unguessable), keeping the payload inside
# Telegram's 64-byte callback_data bound with deterministic parsing.
APPROVE_CALLBACK_PREFIX = "pa1"
DENY_CALLBACK_PREFIX = "pd1"
CALLBACK_DATA_MAX_BYTES = 64

# Telegram delivers at most 4096 characters of message text; bounding the
# UTF-8 byte length is strictly tighter and transport-safe.
CARD_MESSAGE_MAX_BYTES = 4096

_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,60}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuthorizationError(ValueError):
    """A Phase 3 authorization contract or decision failure."""


class Unrepresentable(AuthorizationError):
    """Credential-bearing input: the one permanent refusal, never approvable."""


class AuthorizationBlocked(AuthorizationError):
    """Not a refusal: a corrected plan can be proposed and approved again."""


class ApprovalRejected(AuthorizationError):
    """The decision event carries no authority over this exact requirement."""


class Authority(str, Enum):
    AGENT = "agent"
    STANDING_CONSENT = "standing-consent"
    EXACT_HUMAN = "exact-human"
    ALWAYS_APPROVE_TENANT = "always-approve-tenant"


_AUTHORITY_RANK = {
    Authority.AGENT: 0,
    Authority.STANDING_CONSENT: 1,
    Authority.EXACT_HUMAN: 2,
    Authority.ALWAYS_APPROVE_TENANT: 3,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AuthorizationError(f"{name} must be timezone-aware UTC")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise AuthorizationError(f"{name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AuthorizationError(f"{name} contains control characters")
    if _CREDENTIAL_VALUE.search(value):
        raise Unrepresentable(f"{name} appears to contain credential material")
    return value


def _scalar_leaves(value: Any) -> list[str]:
    """Every string and integer reachable in a JSON value, keys included.

    Integers are rendered as text so numeric rental identities are visible to
    the trusted classifier exactly like their string forms.
    """
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return []
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        leaves: list[str] = []
        for key, item in value.items():
            leaves.append(str(key))
            leaves.extend(_scalar_leaves(item))
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = []
        for item in value:
            leaves.extend(_scalar_leaves(item))
        return leaves
    return []


def _mapping_has_opaque_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _OPAQUE_ARGUMENT_KEYS:
                return True
            if _mapping_has_opaque_key(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_mapping_has_opaque_key(item) for item in value)
    return False


def _step_is_opaque(step: PlanStep) -> bool:
    """An opaque step carries a program, not structured intent."""
    document = _execution_document(step)
    return (
        step.operation == RUN_SHELL_OPERATION
        or _mapping_has_opaque_key({key: value for key, value in document.items() if key != "operation"})
        or any(
            _EFFECT_CAPABLE_ARGUMENT.search(token) or _DYNAMIC_SELECTION.search(token)
            or token == RUN_SHELL_OPERATION
            for token in _scalar_leaves(document)
        )
    )


def _execution_document(step: PlanStep) -> dict[str, Any]:
    """The executable/resource-bearing portion of the full step document.

    Entire subtrees are included, including unknown keys: a nested field named
    ``description`` inside a program is not an escape hatch. Only schema-defined
    evidence fields (effects, interruption/rollback prose, identifiers and
    numeric metadata) are literal. All fields still receive credential and
    literal tenant-reference analysis and remain bound into approval evidence.
    """
    document = step.to_document()
    return {key: document[key] for key in (
        "operation", "arguments", "affected_resources", "preconditions",
        "postconditions", "checkpoint", "rollback", "artifacts",
    )}


def _classification_tokens(leaves: list[str]) -> tuple[str, ...]:
    """Bounded literal parsing, independent of the command vocabulary.

    Keep whole values (including aliases containing spaces), shell words, and
    resource/option components such as ``container:alias`` or ``--name=alias``.
    Recursively decode literal words containing nested programs (including
    sh/bash -c and quoted/concatenated aliases), without a command whitelist.
    Malformed, dynamic, or excessively nested syntax blocks before issuance.
    No shell or other program is ever evaluated. Re-parsing may conservatively
    reject literal arguments that resemble executable syntax.
    """
    candidates: dict[str, None] = {}
    pending = deque((leaf, 0) for leaf in leaves)
    parsed: set[str] = set()
    characters = 0
    while pending:
        leaf, depth = pending.popleft()
        if leaf in parsed:
            continue
        parsed.add(leaf)
        characters += len(leaf)
        if depth > MAX_PROGRAM_NESTING or characters > MAX_ANALYSIS_CHARACTERS:
            raise AuthorizationBlocked("nested program exceeds static analysis bounds")
        candidates[leaf] = None
        if len(leaf) > 16384:
            raise AuthorizationBlocked("resource string exceeds static analysis bounds")
        if _DYNAMIC_SELECTION.search(leaf):
            raise AuthorizationBlocked(
                "dynamic resource selection cannot be bound exactly; "
                "name each exact resource and propose again"
            )
        try:
            parser = shlex.shlex(leaf, posix=True, punctuation_chars="();|&<>")
            parser.whitespace_split = True
            parser.commenters = ""
            words = list(parser)
        except ValueError:
            raise AuthorizationBlocked("ambiguous resource syntax; propose exact literal resources") from None
        for word in words:
            candidates[word] = None
            if word != leaf and re.search(r"[\s\"'\\]", word):
                pending.append((word, depth + 1))
            # Also inspect resource paths and option components.
            for component in re.findall(r"[A-Za-z0-9_.-]+", word):
                candidates[component] = None
        if len(candidates) > MAX_CLASSIFICATION_TOKENS:
            raise AuthorizationBlocked("step exceeds static classification bounds")
    return tuple(candidates)


def _standing_contracts_valid(step: PlanStep) -> bool:
    """Recognized data contracts, never prose or an executable check.

    identity checks the fixed machine; resource_fields_equal compares literal
    pre-state or post-state values. resource_snapshot captures named fields to
    a declared artifact; restore_checkpoint restores that snapshot. Unknown
    shapes remain eligible for exact approval.
    """
    checkpoint, rollback = step.checkpoint, step.rollback
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "kind", "resources", "fields", "artifact"
    }:
        return False
    if checkpoint["kind"] != "resource_snapshot":
        return False
    resources, fields, artifact = (
        checkpoint["resources"], checkpoint["fields"], checkpoint["artifact"]
    )
    if (
        not isinstance(resources, (list, tuple)) or not resources
        or any(not isinstance(item, str) or not item.strip() for item in resources)
        or set(resources) != set(step.affected_resources)
        or not isinstance(fields, (list, tuple)) or not fields
        or any(not isinstance(item, str) or not item.strip() for item in fields)
        or not isinstance(artifact, str) or not artifact.strip()
        or artifact not in step.artifacts
    ):
        return False
    if not isinstance(rollback, Mapping) or set(rollback) != {"kind", "artifact", "resources"}:
        return False
    if (
        rollback["kind"] != "restore_checkpoint" or rollback["artifact"] != artifact
        or not isinstance(rollback["resources"], (list, tuple))
        or tuple(rollback["resources"]) != tuple(resources)
    ):
        return False

    def fields_equal_valid(check: Mapping[str, Any]) -> bool:
        if set(check) != {"kind", "resource", "expected"}:
            return False
        expected = check["expected"]
        if (
            check["kind"] != "resource_fields_equal"
            or not isinstance(check["resource"], str)
            or check["resource"] not in resources
            or not isinstance(expected, Mapping) or not expected
            or any(key not in fields for key in expected)
            or any(
                not isinstance(value, (str, bool, int))
                or (isinstance(value, str) and not value.strip())
                for value in expected.values()
            )
        ):
            return False
        return True

    if not step.preconditions:
        return False
    for check in step.preconditions:
        if set(check) == {"check", "machine_id"} and check["check"] == "identity":
            if check["machine_id"] != MACHINE_ID:
                return False
        elif not fields_equal_valid(check):
            return False
    verified: set[str] = set()
    for check in step.postconditions:
        if not fields_equal_valid(check):
            return False
        verified.add(check["resource"])
    return verified == set(resources)


# A trusted classifier answers: does this exact token name a rental (alias,
# container name, or numeric identity) according to live trusted metadata?
# It must return False for unknown tokens and may raise on infrastructure
# failure, which blocks the plan rather than guessing.
TenantClassifier = Callable[[str], bool]


@dataclass(frozen=True)
class StepRisk:
    """One step's deterministically derived authority and effect facts."""

    step_id: str
    authority: Authority
    flags: frozenset[str]
    tenant_references: tuple[str, ...]
    payload_read: bool
    reasons: tuple[str, ...]


def is_local_workspace_step(step: PlanStep) -> bool:
    """Credential-free, confined Phase 4 work; never a deployment authority.

    The executor additionally requires the workspace isolation attestation. This
    describes an effect boundary, not a catalogue of shell programs.
    """
    execution = step.arguments.get("execution", {})
    return (
        isinstance(execution, Mapping)
        and execution.get("domain") == "workspace"
        and execution.get("effect_scope") == "workspace"
        and all(not getattr(effect, flag) for effect in step.effects for flag in EFFECT_FLAG_NAMES)
        and bool(step.affected_resources)
        and all(resource.startswith("workspace:") for resource in step.affected_resources)
    )


def derive_step(step: PlanStep, *, classifier: TenantClassifier | None = None) -> StepRisk:
    """Who decides this step. Derived from its exact content, nothing else.

    The analysis covers the entire step document -- arguments, resources,
    preconditions, postconditions, checkpoint, rollback, and artifacts -- so
    an under-declared plan cannot shed its risk class by hiding a target in a
    rollback or precondition.  A trusted ``classifier`` additionally resolves
    exact aliases, container names, and numeric rental identities. Shell syntax
    is parsed only in executable/resource-bearing subtrees; schema-defined
    descriptive evidence stays literal. Dynamic
    selection blocks toward an exact resource-bound replan.  An operation this
    code has never seen defaults to exact human approval; it is not rejected
    merely because the implementation has not seen it before.
    """
    if not isinstance(step, PlanStep):
        raise AuthorizationError("derive_step requires a PlanStep")
    document = step.to_document()
    tokens = _scalar_leaves(document)
    for token in tokens:
        # The plan contract already refuses credential values; this restates the
        # boundary for shapes it admits, such as a bare secret-taking flag.
        if _CREDENTIAL_FLAG.fullmatch(token) or _CREDENTIAL_VALUE.search(token):
            raise Unrepresentable(
                "step input is credential-bearing; secrets never enter "
                "model-visible contracts and cannot be approved"
            )
    execution_tokens = _classification_tokens(_scalar_leaves(_execution_document(step)))
    references: list[str] = []
    for token in (*tokens, *execution_tokens):
        for match in TENANT_REFERENCE.findall(token):
            if match not in references:
                references.append(match)
    payload_read = step.operation == TENANT_PAYLOAD_OPERATION
    tenant_operation = payload_read or step.operation.startswith("tenant_")
    if tenant_operation:
        rental = dict(step.arguments).get("rental_id")
        if isinstance(rental, (str, int)) and not isinstance(rental, bool):
            name = rental if isinstance(rental, str) else f"C.{rental}"
            if name not in references:
                references.append(name)
    if classifier is not None:
        seen: set[str] = set()
        for token in (*tokens, *execution_tokens):
            if token in seen:
                continue
            seen.add(token)
            try:
                named = bool(classifier(token))
            except Exception as error:
                raise AuthorizationBlocked(
                    f"trusted resource classification failed: {type(error).__name__}; "
                    "a potential tenant target stays unresolved, propose again"
                ) from None
            if named and token not in references:
                references.append(token)
    if len(references) > MAX_TENANT_REFERENCES:
        raise AuthorizationBlocked("step names too many rentals to bind exactly")
    flags = {name for name in EFFECT_FLAG_NAMES if any(getattr(effect, name) for effect in step.effects)}
    reasons: list[str] = []
    if references or tenant_operation:
        if "tenant" not in flags:
            reasons.append("tenant impact derived from the step's own document")
        flags.add("tenant")
    if "tenant" in flags:
        authority = Authority.ALWAYS_APPROVE_TENANT
        reasons.append("a tenant would feel this; a human approves it, always")
    elif flags:
        authority = Authority.EXACT_HUMAN
        reasons.append("declared effects require exact human approval")
    elif is_local_workspace_step(step):
        authority = Authority.AGENT
        reasons.append("local workspace only; requires an attested credential-free sandbox")
    elif step.operation in READ_ONLY_OPERATIONS:
        if any(
            _EFFECT_CAPABLE_ARGUMENT.search(leaf)
            for leaf in _scalar_leaves(document["arguments"])
        ):
            authority = Authority.EXACT_HUMAN
            reasons.append(
                "a read operation with shell composition in its arguments is "
                "not a read; a human approves the exact program"
            )
        else:
            authority = Authority.AGENT
            reasons.append("bounded read-only observation")
    else:
        authority = Authority.EXACT_HUMAN
        reasons.append(
            "unrecognized operation defaults to exact human approval, not refusal"
        )
    return StepRisk(
        step_id=step.step_id,
        authority=authority,
        flags=frozenset(flags),
        tenant_references=tuple(sorted(references)),
        payload_read=payload_read,
        reasons=tuple(reasons),
    )


@runtime_checkable
class StandingRateAccountant(Protocol):
    """Durable accounting for standing-consent authorizations.

    Without one, standing consent cannot authorize anything: rate predicates
    would be unenforceable, so the whole class fails closed.
    """

    def uses_since(self, grant_id: str, since: datetime) -> int: ...

    def record_use(
        self, grant_id: str, plan_hash: str, step_id: str, at: datetime
    ) -> None: ...


@dataclass(frozen=True)
class StandingEffectGrant:
    """A revocable, versioned grant over effects and invariants, not commands.

    Structurally unable to cover tenant, money, reachability, secret, host, or
    irreversible effects: naming one of those classes is a construction error,
    so no configuration mistake can quietly widen standing authority past the
    initial policy.  Coverage additionally demands a deterministic checkpoint,
    deterministic postcondition verification, a rollback, exactly named
    resources within the blast-radius bound, a non-opaque operation, and an
    unexhausted durable rate budget.
    """

    grant_id: str
    policy_revision: str
    description: str
    effect_classes: frozenset[str]
    max_step_seconds: int
    max_affected_resources: int
    max_uses: int
    rate_window_seconds: int
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.grant_id, "grant_id")
        _identifier(self.policy_revision, "policy_revision")
        _identifier(self.description, "description")
        classes = frozenset(self.effect_classes)
        unknown = classes - frozenset(EFFECT_FLAG_NAMES)
        if unknown:
            raise AuthorizationError(f"unknown standing effect classes: {sorted(unknown)}")
        forbidden = classes & _STANDING_FORBIDDEN
        if forbidden:
            raise AuthorizationError(
                "standing consent can never include these effect classes: "
                + ", ".join(sorted(forbidden))
            )
        if not classes:
            raise AuthorizationError("a standing grant must name an effect class")
        object.__setattr__(self, "effect_classes", classes)
        bounds = (
            ("max_step_seconds", self.max_step_seconds, 1, 86400),
            ("max_affected_resources", self.max_affected_resources, 1, 64),
            ("max_uses", self.max_uses, 1, 100000),
            ("rate_window_seconds", self.rate_window_seconds, 1, 30 * 86400),
        )
        for name, value, lower, upper in bounds:
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise AuthorizationError(f"{name} is outside its bounds")
        issued = _require_utc(self.issued_at, "issued_at")
        expires = _require_utc(self.expires_at, "expires_at")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if expires <= issued:
            raise AuthorizationError("grant expiry must follow issuance")

    def covers(
        self,
        step: PlanStep,
        risk: StepRisk,
        *,
        policy_revision: str,
        now: datetime,
        uses: int,
    ) -> bool:
        now = _require_utc(now, "clock")
        if isinstance(uses, bool) or not isinstance(uses, int) or uses < 0:
            return False
        return (
            policy_revision == self.policy_revision
            and self.issued_at <= now < self.expires_at
            and bool(risk.flags)
            and risk.flags <= self.effect_classes
            and not risk.tenant_references
            and not risk.payload_read
            and not _step_is_opaque(step)
            and _standing_contracts_valid(step)
            and 1 <= len(step.affected_resources) <= self.max_affected_resources
            and step.max_execution_seconds <= self.max_step_seconds
            and uses < self.max_uses
        )


@dataclass(frozen=True)
class PlanDecision:
    """The deterministic authority decision for one exact plan version."""

    plan_hash: str
    step_risks: tuple[StepRisk, ...]
    plan_authority: Authority
    approval_step_ids: tuple[str, ...]
    payload_step_ids: tuple[str, ...]

    @property
    def requires_approval(self) -> bool:
        return bool(self.approval_step_ids) or bool(self.payload_step_ids)

    @property
    def tenant(self) -> bool:
        return self.plan_authority is Authority.ALWAYS_APPROVE_TENANT


def authorize_plan(
    plan: Plan,
    *,
    grants: tuple[StandingEffectGrant, ...] = (),
    policy_revision: str,
    now: datetime | None = None,
    classifier: TenantClassifier | None = None,
    rate_accountant: StandingRateAccountant | None = None,
) -> PlanDecision:
    """Derive per-step and plan-level authority. Pure; nothing is persisted.

    Risk rolls upward: one tenant step makes the whole atomic approval group
    tenant-affecting.  A standing grant may only quiet a reversible
    owned-component step, and never one with tenant references, an opaque
    argument shape, or ``run_shell``.  Standing consent fails closed entirely
    when no trusted classifier or no durable rate accountant is supplied:
    without them a potential tenant target or the rate budget would go
    unchecked.
    """
    if not isinstance(plan, Plan):
        raise AuthorizationError("authorize_plan requires a Plan")
    _identifier(policy_revision, "policy_revision")
    now = _require_utc(now if now is not None else _utc_now(), "clock")
    usable_grants: tuple[StandingEffectGrant, ...] = ()
    grant_uses: dict[str, int] = {}
    if grants and classifier is not None and rate_accountant is not None:
        usable: list[StandingEffectGrant] = []
        for grant in grants:
            try:
                uses = rate_accountant.uses_since(
                    grant.grant_id, now - timedelta(seconds=grant.rate_window_seconds)
                )
            except Exception:
                continue  # accounting failure means this grant cannot authorize
            if isinstance(uses, bool) or not isinstance(uses, int) or uses < 0:
                continue
            grant_uses[grant.grant_id] = uses
            usable.append(grant)
        usable_grants = tuple(usable)
    risks: list[StepRisk] = []
    for step in plan.steps:
        risk = derive_step(step, classifier=classifier)
        if risk.authority is Authority.EXACT_HUMAN and risk.flags == {"owned_component"}:
            if any(
                grant.covers(
                    step, risk,
                    policy_revision=policy_revision, now=now,
                    uses=grant_uses[grant.grant_id],
                )
                for grant in usable_grants
            ):
                risk = StepRisk(
                    step_id=risk.step_id,
                    authority=Authority.STANDING_CONSENT,
                    flags=risk.flags,
                    tenant_references=risk.tenant_references,
                    payload_read=risk.payload_read,
                    reasons=risk.reasons + ("covered by a versioned standing effect grant",),
                )
        risks.append(risk)
    plan_authority = max(
        (risk.authority for risk in risks), key=_AUTHORITY_RANK.__getitem__,
    )
    payload_ids = tuple(risk.step_id for risk in risks if risk.payload_read)
    approval_ids = tuple(
        risk.step_id
        for risk in risks
        if not risk.payload_read
        and risk.authority in {Authority.EXACT_HUMAN, Authority.ALWAYS_APPROVE_TENANT}
    )
    return PlanDecision(
        plan_hash=plan.content_hash,
        step_risks=tuple(risks),
        plan_authority=plan_authority,
        approval_step_ids=approval_ids,
        payload_step_ids=payload_ids,
    )


@dataclass(frozen=True)
class RentalRecord:
    """One rental's identity from trusted target/Vast metadata, never model text."""

    rental_id: str
    container_name: str
    machine_id: str
    generation: str
    owner: str
    status: str
    gpu_allocation: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("rental_id", "container_name", "machine_id", "generation", "owner", "status"):
            _identifier(getattr(self, name), name)
        allocation = tuple(self.gpu_allocation)
        for item in allocation:
            _identifier(item, "gpu_allocation")
        object.__setattr__(self, "gpu_allocation", allocation)

    def to_document(self) -> dict[str, Any]:
        return {
            "rental_id": self.rental_id,
            "container_name": self.container_name,
            "machine_id": self.machine_id,
            "generation": self.generation,
            "owner": self.owner,
            "status": self.status,
            "gpu_allocation": list(self.gpu_allocation),
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "RentalRecord":
        if not isinstance(value, Mapping) or set(value) != {
            "rental_id", "container_name", "machine_id", "generation", "owner",
            "status", "gpu_allocation",
        }:
            raise AuthorizationError("invalid rental binding document")
        allocation = value["gpu_allocation"]
        if not isinstance(allocation, list):
            raise AuthorizationError("invalid rental binding document")
        return cls(
            rental_id=value["rental_id"], container_name=value["container_name"],
            machine_id=value["machine_id"], generation=value["generation"],
            owner=value["owner"], status=value["status"],
            gpu_allocation=tuple(allocation),
        )


@runtime_checkable
class RentalResolver(Protocol):
    """Trusted rental lookup. Must query live target/Vast metadata per call.

    ``resolve`` must return an empty tuple for tokens that name no rental; an
    exception is treated as an unresolved potential tenant target and blocks.
    """

    def resolve(self, reference: str) -> tuple[RentalRecord, ...]: ...


def _resolve_binding(resolver: RentalResolver, reference: str) -> RentalRecord:
    try:
        records = tuple(resolver.resolve(reference))
    except Exception as error:
        raise AuthorizationBlocked(
            f"trusted rental resolution failed for {reference!r}: {type(error).__name__}"
        ) from None
    if any(not isinstance(record, RentalRecord) for record in records):
        raise AuthorizationBlocked("rental resolver returned an untrusted record shape")
    if not records:
        raise AuthorizationBlocked(
            f"no trusted rental matches {reference!r}; correct the plan and propose again"
        )
    # Ambiguity is judged over the complete record: two records that agree on
    # id and generation but differ in owner, status, or GPU allocation are
    # still two different claims about reality.
    if len(set(records)) > 1:
        raise AuthorizationBlocked(
            f"rental reference {reference!r} is ambiguous; bind one exact rental and propose again"
        )
    record = records[0]
    if record.machine_id != MACHINE_ID:
        raise AuthorizationBlocked(
            f"rental {record.rental_id} is not on machine {MACHINE_ID}"
        )
    if record.status not in ACTIONABLE_RENTAL_STATUSES:
        raise AuthorizationBlocked(
            f"rental {record.rental_id} status {record.status!r} is not "
            "actionable; refresh evidence and propose again"
        )
    return record


@dataclass(frozen=True)
class CallbackAction:
    """The deterministic meaning of one Telegram callback payload."""

    approve: bool
    nonce: str


def parse_callback(data: object) -> CallbackAction:
    """Parse ``callback_data`` deterministically; reject everything else.

    The grammar is exactly ``pa1:<nonce>`` or ``pd1:<nonce>`` within 64 UTF-8
    bytes.  Anything outside it carries no authority in either direction.
    """
    if not isinstance(data, str):
        raise ApprovalRejected("callback data must be text")
    try:
        encoded = data.encode("utf-8")
    except UnicodeEncodeError:
        raise ApprovalRejected("callback data is not valid UTF-8") from None
    if len(encoded) > CALLBACK_DATA_MAX_BYTES:
        raise ApprovalRejected("callback data exceeds the 64-byte transport bound")
    prefix, separator, nonce = data.partition(":")
    if separator != ":" or prefix not in {APPROVE_CALLBACK_PREFIX, DENY_CALLBACK_PREFIX}:
        raise ApprovalRejected("callback data does not match the approval grammar")
    if not _NONCE_PATTERN.fullmatch(nonce):
        raise ApprovalRejected("callback nonce does not match the approval grammar")
    return CallbackAction(approve=prefix == APPROVE_CALLBACK_PREFIX, nonce=nonce)


_REQUIREMENT_FIELDS = {
    "schema_version", "requirement_id", "task_id", "plan_id", "plan_version",
    "plan_hash", "machine_id", "evidence_revision", "policy_revision",
    "requester_id", "approval_group_id", "steps", "tenant_bindings",
    "payload_purpose", "payload_source", "issued_at", "expires_at", "nonce",
}


@dataclass(frozen=True)
class ApprovalRequirement:
    """Everything an approval binds, rendered to the human byte-for-byte.

    ``steps`` embeds the exact plan-step documents (arguments, effects,
    resources, artifacts, rollback) together with the derived effect flags and
    tenant references. Each step's ``reference_bindings`` maps every original
    reference to an entry in ``tenant_bindings``, preserving aliases even when
    several aliases identify the same rental. Both mappings and full records
    are covered by the card hash and revalidated before authority is used.
    The policy revision under which the requirement was derived is part of the
    document and therefore of the card bytes and hash.
    """

    requirement_id: str
    task_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    evidence_revision: str
    policy_revision: str
    requester_id: str
    approval_group_id: int
    steps: tuple[Mapping[str, Any], ...]
    tenant_bindings: tuple[RentalRecord, ...]
    payload_purpose: str | None
    payload_source: str | None
    issued_at: datetime
    expires_at: datetime
    nonce: str
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=AUTHZ_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        for name in (
            "requirement_id", "task_id", "plan_id", "evidence_revision",
            "policy_revision", "requester_id", "nonce",
        ):
            _identifier(getattr(self, name), name)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.requirement_id):
            raise AuthorizationError("requirement_id is not callback-safe")
        if not _NONCE_PATTERN.fullmatch(self.nonce):
            raise AuthorizationError("nonce is not callback-safe within the transport bound")
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version < 1:
            raise AuthorizationError("plan_version must be a positive integer")
        if not _SHA256.fullmatch(self.plan_hash):
            raise AuthorizationError("plan_hash must be a SHA-256 digest")
        if isinstance(self.approval_group_id, bool) or not isinstance(self.approval_group_id, int):
            raise AuthorizationError("approval_group_id must be an integer")
        steps = tuple(self.steps)
        if not steps:
            raise AuthorizationError("a requirement must cover at least one step")
        for item in steps:
            if not isinstance(item, Mapping) or set(item) != {
                "step", "flags", "tenant_references", "reference_bindings"
            }:
                raise AuthorizationError("invalid requirement step summary")
            if any(flag not in EFFECT_FLAG_NAMES for flag in item["flags"]):
                raise AuthorizationError("unknown derived effect flag")
            for reference in item["tenant_references"]:
                _identifier(reference, "tenant_references")
            # Reconstructing proves the embedded document is an exact PlanStep.
            PlanStep.from_document(item["step"])
        object.__setattr__(self, "steps", steps)
        bindings = tuple(self.tenant_bindings)
        if any(not isinstance(item, RentalRecord) for item in bindings):
            raise AuthorizationError("tenant_bindings must contain RentalRecord values")
        if len({item.rental_id for item in bindings}) != len(bindings):
            raise AuthorizationError("tenant_bindings must bind each rental once")
        object.__setattr__(self, "tenant_bindings", bindings)
        by_id = {binding.rental_id: binding for binding in bindings}
        bound_ids: set[str] = set()
        for item in steps:
            mapping = item["reference_bindings"]
            if not isinstance(mapping, Mapping) or set(mapping) != set(item["tenant_references"]):
                raise AuthorizationError("every original tenant reference needs an exact rental binding")
            if ("tenant" in item["flags"] or item["step"]["operation"] == TENANT_PAYLOAD_OPERATION) and not mapping:
                raise AuthorizationError("every tenant step needs an exact rental binding")
            for reference, rental_id in mapping.items():
                _identifier(reference, "tenant reference")
                _identifier(rental_id, "rental_id")
                if rental_id not in by_id:
                    raise AuthorizationError("step reference lacks an exact rental binding")
                bound_ids.add(rental_id)
        if bound_ids != set(by_id):
            raise AuthorizationError("rental bindings must match the exact step references")
        if (self.payload_purpose is None) != (self.payload_source is None):
            raise AuthorizationError("payload purpose and source must be set together")
        if self.payload_purpose is not None:
            _identifier(self.payload_purpose, "payload_purpose")
            _identifier(self.payload_source, "payload_source")
        tenant_touching = any(
            "tenant" in item["flags"] or item["tenant_references"] for item in steps
        )
        if (tenant_touching or self.payload_purpose is not None) and not bindings:
            raise AuthorizationError(
                "a tenant-affecting requirement must bind at least one exact rental; "
                "a generic approval never substitutes for the missing binding"
            )
        issued = _require_utc(self.issued_at, "issued_at")
        expires = _require_utc(self.expires_at, "expires_at")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if expires <= issued:
            raise AuthorizationError("requirement expiry must follow issuance")
        if self.machine_id != MACHINE_ID:
            raise AuthorizationError(f"requirements are restricted to machine {MACHINE_ID}")
        for data in (self.approve_callback_data, self.deny_callback_data):
            if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
                raise AuthorizationError("callback data exceeds the 64-byte transport bound")

    @property
    def tenant(self) -> bool:
        return bool(self.tenant_bindings) or self.payload_purpose is not None

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(item["step"]["step_id"] for item in self.steps)

    @property
    def approve_callback_data(self) -> str:
        return f"{APPROVE_CALLBACK_PREFIX}:{self.nonce}"

    @property
    def deny_callback_data(self) -> str:
        return f"{DENY_CALLBACK_PREFIX}:{self.nonce}"

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_hash": self.plan_hash,
            "machine_id": self.machine_id,
            "evidence_revision": self.evidence_revision,
            "policy_revision": self.policy_revision,
            "requester_id": self.requester_id,
            "approval_group_id": self.approval_group_id,
            "steps": [
                {
                    "step": dict(item["step"]),
                    "flags": list(item["flags"]),
                    "tenant_references": list(item["tenant_references"]),
                    "reference_bindings": dict(item["reference_bindings"]),
                }
                for item in self.steps
            ],
            "tenant_bindings": [item.to_document() for item in self.tenant_bindings],
            "payload_purpose": self.payload_purpose,
            "payload_source": self.payload_source,
            "issued_at": utc_text(self.issued_at),
            "expires_at": utc_text(self.expires_at),
            "nonce": self.nonce,
        }

    @classmethod
    def from_document(cls, value: Any) -> "ApprovalRequirement":
        if not isinstance(value, Mapping) or set(value) != _REQUIREMENT_FIELDS:
            raise AuthorizationError("invalid ApprovalRequirement document")
        if value["schema_version"] != AUTHZ_SCHEMA_VERSION:
            raise AuthorizationError("unsupported ApprovalRequirement schema version")
        if not isinstance(value["steps"], list) or not isinstance(value["tenant_bindings"], list):
            raise AuthorizationError("invalid ApprovalRequirement document")
        return cls(
            requirement_id=value["requirement_id"], task_id=value["task_id"],
            plan_id=value["plan_id"], plan_version=value["plan_version"],
            plan_hash=value["plan_hash"], evidence_revision=value["evidence_revision"],
            policy_revision=value["policy_revision"],
            requester_id=value["requester_id"], approval_group_id=value["approval_group_id"],
            steps=tuple(
                {
                    "step": item["step"],
                    "flags": tuple(item["flags"]),
                    "tenant_references": tuple(item["tenant_references"]),
                    "reference_bindings": dict(item["reference_bindings"]),
                }
                for item in value["steps"]
            ),
            tenant_bindings=tuple(
                RentalRecord.from_document(item) for item in value["tenant_bindings"]
            ),
            payload_purpose=value["payload_purpose"], payload_source=value["payload_source"],
            issued_at=parse_utc(value["issued_at"], "issued_at"),
            expires_at=parse_utc(value["expires_at"], "expires_at"),
            nonce=value["nonce"],
            machine_id=value["machine_id"],
        )

    @property
    def card_hash(self) -> str:
        return hashlib.sha256(render_approval_card(self)).hexdigest()


def _card_sections(requirement: ApprovalRequirement) -> tuple[str, ...]:
    """The card as ordered sections; the full card joins them with blank lines."""
    if not isinstance(requirement, ApprovalRequirement):
        raise AuthorizationError("render_approval_card requires an ApprovalRequirement")
    header = (
        "TENANT APPROVAL REQUIRED" if requirement.tenant else "APPROVAL REQUIRED"
    )
    sections: list[str] = [
        "\n".join(
            [
                f"{header} — machine {requirement.machine_id}",
                f"task {requirement.task_id} plan {requirement.plan_id} v{requirement.plan_version}",
                f"plan sha256 {requirement.plan_hash}",
                f"evidence {requirement.evidence_revision}",
                f"policy {requirement.policy_revision}",
                f"requested by {requirement.requester_id} for group {requirement.approval_group_id}",
            ]
        )
    ]
    for item in requirement.steps:
        step = item["step"]
        lines = [f"step {step['step_id']}: {step['operation']}"]
        lines.append("  arguments " + canonical_json(step["arguments"]).decode("utf-8"))
        lines.append("  reference bindings " + canonical_json(item["reference_bindings"]).decode("utf-8"))
        lines.append("  preconditions " + canonical_json(step["preconditions"]).decode("utf-8"))
        lines.append("  postconditions " + canonical_json(step["postconditions"]).decode("utf-8"))
        lines.append("  checkpoint " + canonical_json(step["checkpoint"]).decode("utf-8"))
        flags = ", ".join(sorted(item["flags"]))
        lines.append(f"  effects {flags or 'none declared; defaults to human approval'}")
        lines.append("  resources " + (", ".join(step["affected_resources"]) or "none"))
        lines.append("  artifacts " + (", ".join(step["artifacts"]) or "none"))
        if step["rollback"] is not None:
            lines.append("  rollback " + canonical_json(step["rollback"]).decode("utf-8"))
        else:
            lines.append(f"  rollback impossible: {step['rollback_impossible_reason']}")
        lines.append(f"  interruption {step['expected_interruption'] or 'none stated'}")
        lines.append(f"  max seconds {step['max_execution_seconds']}")
        sections.append("\n".join(lines))
    for binding in requirement.tenant_bindings:
        sections.append(
            f"rental {binding.rental_id} container {binding.container_name} "
            f"owner {binding.owner} status {binding.status} "
            f"generation {binding.generation} "
            f"gpus {', '.join(binding.gpu_allocation) or 'none'}"
        )
    if requirement.payload_purpose is not None:
        sections.append(
            "purpose-limited tenant payload access: "
            f"{requirement.payload_purpose} (source {requirement.payload_source})"
        )
    sections.append(
        "\n".join(
            [
                f"expires {utc_text(requirement.expires_at)}",
                f"nonce {requirement.nonce}",
                f"approve {requirement.approve_callback_data}",
                f"deny {requirement.deny_callback_data}",
            ]
        )
    )
    text = "\n\n".join(sections)
    if _CREDENTIAL_VALUE.search(text):
        raise Unrepresentable("approval card would carry credential material")
    return tuple(sections)


def render_approval_card(requirement: ApprovalRequirement) -> bytes:
    """The exact bytes a human approves, rendered only from plan fields.

    Model prose may explain the reasoning elsewhere; it is not part of this
    card and not part of the authorization decision.  The same requirement
    always renders the same bytes, and any change to an executable field,
    binding, artifact, rollback, or the policy revision changes them.
    """
    return "\n\n".join(_card_sections(requirement)).encode("utf-8")


def render_approval_card_messages(
    requirement: ApprovalRequirement,
) -> tuple[bytes, ...]:
    """Split the card into transport-sized messages without changing a byte.

    Splits only on section boundaries, so a step, binding, or the footer is
    never torn apart; joining the messages with a blank line reproduces the
    exact card bytes the hash binds.  A single section that exceeds the
    transport bound cannot be split safely and blocks toward a smaller exact
    plan instead.
    """
    sections = _card_sections(requirement)
    messages: list[str] = []
    current: list[str] = []
    for section in sections:
        if len(section.encode("utf-8")) > CARD_MESSAGE_MAX_BYTES:
            raise AuthorizationBlocked(
                "an approval card section exceeds the Telegram transport bound "
                "and cannot be split safely; propose a smaller exact plan"
            )
        if not current:
            current = [section]
            continue
        candidate = "\n\n".join(current + [section])
        if len(candidate.encode("utf-8")) <= CARD_MESSAGE_MAX_BYTES:
            current.append(section)
        else:
            messages.append("\n\n".join(current))
            current = [section]
    if current:
        messages.append("\n\n".join(current))
    return tuple(message.encode("utf-8") for message in messages)


@dataclass(frozen=True)
class ApprovalDecision:
    """A bounded event from the trusted, authenticated approval ingress."""

    requirement_id: str
    card_hash: str
    nonce: str
    group_id: int
    user_id: int | None
    display_name: str
    via_callback: bool
    occurred_at: datetime
    sender_is_bot: bool = False
    sender_is_anonymous: bool = False

    def __post_init__(self) -> None:
        _identifier(self.requirement_id, "requirement_id")
        _identifier(self.nonce, "nonce")
        _identifier(self.display_name, "display_name")
        if len(self.display_name) > 128:
            raise AuthorizationError("display_name is too long")
        if not _SHA256.fullmatch(self.card_hash):
            raise AuthorizationError("card_hash must be a SHA-256 digest")
        object.__setattr__(self, "occurred_at", _require_utc(self.occurred_at, "occurred_at"))


@dataclass(frozen=True)
class PreflightDecision:
    """Read-only admission verdict directly before a (future) execution."""

    admit: bool
    reason: str


class _SqliteStandingRateAccountant:
    """Durable standing-consent rate accounting on the service's connection."""

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection

    def uses_since(self, grant_id: str, since: datetime) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM tc_plan_authz_standing_uses"
            " WHERE grant_id = ? AND used_utc >= ?",
            (grant_id, utc_text(since)),
        ).fetchone()
        return int(row[0])

    def record_use(self, grant_id: str, plan_hash: str, step_id: str, at: datetime) -> None:
        self.db.execute(
            "INSERT INTO tc_plan_authz_standing_uses"
            " (grant_id, plan_hash, step_id, used_utc) VALUES (?, ?, ?, ?)",
            (grant_id, plan_hash, step_id, utc_text(at)),
        )


_SCHEMA_V2_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_requirements (
         requirement_id TEXT PRIMARY KEY,
         plan_hash TEXT NOT NULL,
         card_hash TEXT NOT NULL UNIQUE,
         nonce TEXT NOT NULL UNIQUE,
         policy_revision TEXT NOT NULL,
         group_id INTEGER NOT NULL,
         document_json TEXT NOT NULL,
         issued_utc TEXT NOT NULL,
         expires_utc TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_nonces (
         nonce TEXT PRIMARY KEY,
         requirement_id TEXT NOT NULL UNIQUE,
         recorded_utc TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_decisions (
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
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_denials (
         requirement_id TEXT PRIMARY KEY,
         nonce TEXT NOT NULL UNIQUE,
         card_hash TEXT NOT NULL,
         group_id INTEGER NOT NULL,
         user_id INTEGER NOT NULL,
         display_name TEXT NOT NULL,
         occurred_utc TEXT NOT NULL,
         recorded_utc TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_revocations (
         requirement_id TEXT PRIMARY KEY,
         grant_id TEXT NOT NULL UNIQUE,
         revoked_by TEXT NOT NULL,
         reason TEXT NOT NULL,
         revoked_utc TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_standing_revocations (
         grant_id TEXT PRIMARY KEY,
         revoked_by TEXT NOT NULL,
         reason TEXT NOT NULL,
         revoked_utc TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS tc_plan_authz_standing_uses (
         use_id INTEGER PRIMARY KEY AUTOINCREMENT,
         grant_id TEXT NOT NULL,
         plan_hash TEXT NOT NULL,
         step_id TEXT NOT NULL,
         used_utc TEXT NOT NULL
       )""",
    """CREATE INDEX IF NOT EXISTS tc_plan_authz_standing_uses_by_grant
         ON tc_plan_authz_standing_uses (grant_id, used_utc)""",
)


class PlanAuthorizationService:
    """Deterministic Phase 3 authorization and preflight over immutable plans.

    Owns no transport, credential, or execution path.  Construction creates
    or migrates only ``tc_plan_authz_*`` tables on the supplied connection.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        membership: MembershipVerifier,
        resolver: RentalResolver,
        policy_revision: str,
        approval_group_id: int,
        standing_grants: tuple[StandingEffectGrant, ...] = (),
        clock: Callable[[], datetime] = _utc_now,
        nonce_factory: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self.db = connection
        self.membership = membership
        self.resolver = resolver
        self.policy_revision = _identifier(policy_revision, "policy_revision")
        if isinstance(approval_group_id, bool) or not isinstance(approval_group_id, int):
            raise AuthorizationError("approval_group_id must be an integer")
        self.approval_group_id = approval_group_id
        self.standing_grants = tuple(standing_grants)
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.rate_accountant: StandingRateAccountant = _SqliteStandingRateAccountant(connection)
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_plan_authz_schema (
                 namespace TEXT PRIMARY KEY CHECK(namespace = 'plan-authz'),
                 version INTEGER NOT NULL
               )"""
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT version FROM tc_plan_authz_schema WHERE namespace = 'plan-authz'"
        ).fetchone()
        version = None if row is None else row[0]
        if version is None:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA_V2_STATEMENTS:
                    self.db.execute(statement)
                self.db.execute(
                    "INSERT INTO tc_plan_authz_schema(namespace, version) VALUES ('plan-authz', ?)",
                    (AUTHZ_SCHEMA_VERSION,),
                )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        elif version in (1, 2):
            self._migrate_legacy_requirements(version)
        elif version == AUTHZ_SCHEMA_VERSION:
            # Idempotent: another connection may already hold the current layout.
            try:
                self.db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA_V2_STATEMENTS:
                    self.db.execute(statement)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        else:
            raise AuthorizationError(
                f"plan authorization schema version {version!r} is not supported "
                "by this code; refusing to run against it"
            )

    def _migrate_legacy_requirements(self, version: int) -> None:
        """Archive legacy authority and adopt the current layout atomically.

        v1 omitted the policy revision; v2 omitted original-reference and
        per-step bindings. Neither can safely acquire those facts retroactively.
        Preserve their evidence, consumed nonces, revocations and rate counts.
        """
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                f"ALTER TABLE tc_plan_authz_requirements RENAME TO tc_plan_authz_requirements_v{version}"
            )
            self.db.execute(
                f"ALTER TABLE tc_plan_authz_decisions RENAME TO tc_plan_authz_decisions_v{version}"
            )
            for statement in _SCHEMA_V2_STATEMENTS:
                self.db.execute(statement)
            self.db.execute(
                "UPDATE tc_plan_authz_schema SET version = ? WHERE namespace = 'plan-authz'",
                (AUTHZ_SCHEMA_VERSION,),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _now(self) -> datetime:
        return _require_utc(self.clock(), "clock")

    def _classify_reference(self, token: str) -> bool:
        return len(tuple(self.resolver.resolve(token))) > 0

    def active_standing_grants(self, now: datetime | None = None) -> tuple[StandingEffectGrant, ...]:
        """Configured grants minus the durably revoked ones."""
        revoked = {
            row[0]
            for row in self.db.execute(
                "SELECT grant_id FROM tc_plan_authz_standing_revocations"
            ).fetchall()
        }
        return tuple(
            grant for grant in self.standing_grants if grant.grant_id not in revoked
        )

    def _authorize_at(self, plan: Plan, now: datetime) -> PlanDecision:
        return authorize_plan(
            plan,
            grants=self.active_standing_grants(now),
            policy_revision=self.policy_revision,
            now=now,
            classifier=self._classify_reference,
            rate_accountant=self.rate_accountant,
        )

    def authorize(self, plan: Plan) -> PlanDecision:
        return self._authorize_at(plan, self._now())

    def _revalidate_bindings(self, requirement: ApprovalRequirement) -> None:
        bindings = {record.rental_id: record for record in requirement.tenant_bindings}
        references = [(record.rental_id, record) for record in requirement.tenant_bindings]
        references.extend(
            (reference, bindings[rental_id])
            for item in requirement.steps
            for reference, rental_id in item["reference_bindings"].items()
        )
        for reference, binding in references:
            if _resolve_binding(self.resolver, reference) != binding:
                raise AuthorizationBlocked(
                    f"rental reference {reference!r} identity changed (ownership or state may differ); "
                    "a new plan and approval are required"
                )

    def issue_requirements(
        self,
        plan: Plan,
        decision: PlanDecision,
        *,
        requester_id: str,
        current_evidence_revision: str,
    ) -> tuple[ApprovalRequirement, ...]:
        """Atomically persist the approval requirements this plan version needs.

        The caller's decision is not trusted: it is rederived here and must be
        exactly equal.  One atomic requirement covers every non-payload step
        needing approval (risk already rolled upward), plus one purpose-limited
        requirement per tenant payload step.  Every card is bound to the
        transport limit before anything is persisted, and either every
        requirement of the plan is durably recorded or none is.
        """
        if not isinstance(plan, Plan) or not isinstance(decision, PlanDecision):
            raise AuthorizationError("issue_requirements needs the plan and its decision")
        _identifier(requester_id, "requester_id")
        _identifier(current_evidence_revision, "current_evidence_revision")
        now = self._now()
        rederived = self._authorize_at(plan, now)
        if decision != rederived:
            raise AuthorizationError(
                "the supplied decision does not equal the internally rederived "
                "decision for this exact plan; re-authorize and try again"
            )
        if current_evidence_revision != plan.evidence_revision:
            raise AuthorizationBlocked(
                "evidence revision changed since planning; replan against fresh evidence"
            )
        if now >= plan.expires_at:
            raise AuthorizationBlocked("plan expired before approval was requested")
        risks = {risk.step_id: risk for risk in decision.step_risks}
        steps = {step.step_id: step for step in plan.steps}
        groups: list[tuple[tuple[str, ...], str | None, str | None]] = []
        if decision.approval_step_ids:
            groups.append((decision.approval_step_ids, None, None))
        for step_id in decision.payload_step_ids:
            arguments = dict(steps[step_id].arguments)
            purpose = arguments.get("purpose")
            source = arguments.get("source")
            if not isinstance(purpose, str) or not purpose or not isinstance(source, str) or not source:
                raise AuthorizationBlocked(
                    "tenant payload access requires an explicit purpose and scoped "
                    "source; state them in the step and propose again"
                )
            groups.append(((step_id,), purpose, source))
        requirements: list[ApprovalRequirement] = []
        for step_ids, purpose, source in groups:
            references: list[str] = []
            for step_id in step_ids:
                if "tenant" in risks[step_id].flags and not risks[step_id].tenant_references:
                    raise AuthorizationBlocked(
                        f"tenant step {step_id} lacks an exact rental binding; propose again"
                    )
                for reference in risks[step_id].tenant_references:
                    if reference not in references:
                        references.append(reference)
            bindings: dict[str, RentalRecord] = {}
            reference_bindings: dict[str, str] = {}
            for reference in references:
                record = _resolve_binding(self.resolver, reference)
                existing = bindings.get(record.rental_id)
                if existing is not None and existing != record:
                    raise AuthorizationBlocked(
                        f"rental {record.rental_id} resolved inconsistently; propose again"
                    )
                bindings[record.rental_id] = record
                reference_bindings[reference] = record.rental_id
            tenant_touching = purpose is not None or any(
                "tenant" in risks[step_id].flags for step_id in step_ids
            )
            if tenant_touching and not bindings:
                raise AuthorizationBlocked(
                    "a tenant-affecting step lacks an exact rental binding; a "
                    "generic approval never substitutes for it -- name the exact "
                    "rental and propose again"
                )
            requirement = ApprovalRequirement(
                requirement_id=self.id_factory(),
                task_id=plan.task_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                plan_hash=plan.content_hash,
                evidence_revision=plan.evidence_revision,
                policy_revision=self.policy_revision,
                requester_id=requester_id,
                approval_group_id=self.approval_group_id,
                steps=tuple(
                    {
                        "step": steps[step_id].to_document(),
                        "flags": tuple(sorted(risks[step_id].flags)),
                        "tenant_references": risks[step_id].tenant_references,
                        "reference_bindings": {
                            reference: reference_bindings[reference]
                            for reference in risks[step_id].tenant_references
                        },
                    }
                    for step_id in step_ids
                ),
                tenant_bindings=tuple(
                    bindings[rental_id] for rental_id in sorted(bindings)
                ),
                payload_purpose=purpose,
                payload_source=source,
                issued_at=now,
                expires_at=min(plan.expires_at, now + APPROVAL_LIFETIME),
                nonce=self.nonce_factory(),
            )
            # Transport bound is a pre-persistence gate: an unsendable card
            # must never exist durably.
            render_approval_card_messages(requirement)
            requirements.append(requirement)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for requirement in requirements:
                self.db.execute(
                    """INSERT INTO tc_plan_authz_requirements
                       (requirement_id, plan_hash, card_hash, nonce, policy_revision,
                        group_id, document_json, issued_utc, expires_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        requirement.requirement_id, requirement.plan_hash,
                        requirement.card_hash, requirement.nonce,
                        requirement.policy_revision, requirement.approval_group_id,
                        canonical_json(requirement.to_document()).decode("utf-8"),
                        utc_text(requirement.issued_at), utc_text(requirement.expires_at),
                    ),
                )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise AuthorizationError("requirement identity, card, or nonce was already used") from error
        except BaseException:
            self.db.rollback()
            raise
        return tuple(requirements)

    def get_requirement(self, requirement_id: str) -> ApprovalRequirement | None:
        row = self.db.execute(
            "SELECT document_json, card_hash FROM tc_plan_authz_requirements WHERE requirement_id = ?",
            (requirement_id,),
        ).fetchone()
        if row is None:
            return None
        requirement = ApprovalRequirement.from_document(strict_json_loads(row[0]))
        if requirement.card_hash != row[1]:
            raise AuthorizationError("stored requirement no longer matches its card hash")
        return requirement

    def resolve_callback(self, data: object) -> tuple[CallbackAction, ApprovalRequirement]:
        """Deterministically map callback bytes to a requirement, or reject."""
        action = parse_callback(data)
        row = self.db.execute(
            "SELECT requirement_id FROM tc_plan_authz_requirements WHERE nonce = ?",
            (action.nonce,),
        ).fetchone()
        if row is None:
            raise ApprovalRejected("callback does not reference a known requirement")
        requirement = self.get_requirement(row[0])
        if requirement is None:
            raise ApprovalRejected("callback does not reference a known requirement")
        return action, requirement

    def get_grant(self, requirement_id: str) -> ApprovalGrant | None:
        row = self.db.execute(
            "SELECT grant_json FROM tc_plan_authz_decisions WHERE requirement_id = ?",
            (requirement_id,),
        ).fetchone()
        if row is None:
            return None
        return ApprovalGrant.from_json(row[0])

    def get_denial(self, requirement_id: str) -> Mapping[str, Any] | None:
        row = self.db.execute(
            """SELECT nonce, card_hash, group_id, user_id, display_name,
                      occurred_utc, recorded_utc
               FROM tc_plan_authz_denials WHERE requirement_id = ?""",
            (requirement_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "nonce": row[0], "card_hash": row[1], "group_id": row[2],
            "user_id": row[3], "display_name": row[4],
            "occurred_utc": row[5], "recorded_utc": row[6],
        }

    def grant_revoked(self, requirement_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM tc_plan_authz_revocations WHERE requirement_id = ?",
                (requirement_id,),
            ).fetchone()
            is not None
        )

    def _verify_member(self, decision: ApprovalDecision, expected_group: int, now: datetime) -> datetime:
        if decision.group_id != expected_group:
            raise ApprovalRejected("approval came from a foreign group")
        if decision.user_id is None or decision.sender_is_anonymous or decision.sender_is_bot:
            raise ApprovalRejected("approval requires an identifiable human sender")
        verdict = self.membership.verify(decision.group_id, decision.user_id)
        verified_age = now - verdict.verified_at
        if (
            verdict.group_id != decision.group_id
            or verdict.user_id != decision.user_id
            or not verdict.current_member
            or not verdict.human
            or not verdict.independently_verified
            or verified_age < timedelta(0)
            or verified_age > MAX_MEMBERSHIP_AGE
        ):
            raise ApprovalRejected("current human group membership was not verified")
        return verdict.verified_at

    def _validate_decision_event(
        self, decision: ApprovalDecision, requirement: ApprovalRequirement, now: datetime
    ) -> None:
        """Shared authenticated-event checks for approvals and denials.

        Runs inside the caller's write transaction so validation and the nonce
        consumption it protects are one atomic step.  Membership verification
        is separate: callers apply their own lifetime checks first.
        """
        if requirement.approval_group_id != self.approval_group_id:
            raise ApprovalRejected("requirement is bound to a different approval group")
        if requirement.policy_revision != self.policy_revision:
            raise ApprovalRejected("policy revision changed since the card was issued")
        if decision.card_hash != requirement.card_hash:
            raise ApprovalRejected("decision does not bind the exact card bytes")
        if decision.nonce != requirement.nonce:
            raise ApprovalRejected("decision nonce does not match the requirement")
        if not decision.via_callback:
            raise ApprovalRejected(
                "text confirmation carries no authority; tap the exact button"
            )
        if decision.occurred_at > now:
            raise ApprovalRejected("decision event timestamp is in the future")
        if self.get_denial(decision.requirement_id) is not None:
            raise ApprovalRejected(
                "requirement was explicitly denied; a denial can never become an approval"
            )

    def record_decision(
        self, decision: ApprovalDecision, *, current_evidence_revision: str
    ) -> ApprovalGrant:
        """Validate one exact approval event and return its bound grant.

        Nothing here executes or consumes execution authority; it converts a
        verified human callback over exact card bytes into a durable
        :class:`~terracompute_ops.plans.ApprovalGrant` bound to the plan hash.
        Validation and nonce consumption happen in one write transaction, so a
        concurrent decision on the same requirement cannot slip between the
        check and the record.
        """
        if not isinstance(decision, ApprovalDecision):
            raise ApprovalRejected("decision must be an ApprovalDecision")
        _identifier(current_evidence_revision, "current_evidence_revision")
        now = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            requirement = self.get_requirement(decision.requirement_id)
            if requirement is None:
                raise ApprovalRejected("unknown approval requirement")
            self._validate_decision_event(decision, requirement, now)
            if not requirement.issued_at <= now < requirement.expires_at:
                raise ApprovalRejected("approval requirement expired")
            if not requirement.issued_at <= decision.occurred_at < requirement.expires_at:
                raise ApprovalRejected("approval event is outside the requirement lifetime")
            if current_evidence_revision != requirement.evidence_revision:
                raise ApprovalRejected("evidence revision changed; the card is stale")
            verified_at = self._verify_member(decision, requirement.approval_group_id, now)
            # Rental identity must still hold at the moment authority is granted.
            try:
                self._revalidate_bindings(requirement)
            except AuthorizationBlocked as error:
                raise ApprovalRejected(str(error)) from error
            grant = ApprovalGrant(
                grant_id=self.id_factory(),
                task_id=requirement.task_id,
                plan_id=requirement.plan_id,
                plan_hash=requirement.plan_hash,
                step_ids=requirement.step_ids,
                kind=ApprovalKind.EXACT_HUMAN,
                approver_id=str(decision.user_id),
                policy_revision=self.policy_revision,
                evidence_revision=requirement.evidence_revision,
                nonce=requirement.nonce,
                issued_at=now,
                expires_at=requirement.expires_at,
            )
            self.db.execute(
                """INSERT INTO tc_plan_authz_nonces (nonce, requirement_id, recorded_utc)
                   VALUES (?, ?, ?)""",
                (decision.nonce, decision.requirement_id, utc_text(now)),
            )
            self.db.execute(
                """INSERT INTO tc_plan_authz_decisions
                   (requirement_id, nonce, card_hash, group_id, user_id, display_name,
                    via_callback, occurred_utc, verified_utc, grant_id, grant_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.requirement_id, decision.nonce, decision.card_hash,
                    decision.group_id, decision.user_id, decision.display_name,
                    1 if decision.via_callback else 0, utc_text(decision.occurred_at),
                    utc_text(verified_at), grant.grant_id,
                    grant.canonical_json().decode("utf-8"),
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise ApprovalRejected("decision nonce was already consumed") from error
        except BaseException:
            self.db.rollback()
            raise
        return grant

    def record_denial(self, decision: ApprovalDecision) -> None:
        """Durably record an explicit denial, consuming the requirement's nonce.

        A denial is authenticated exactly like an approval -- exact card bytes,
        matching nonce, callback-only, current human group membership -- and is
        terminal: once recorded, no event can turn this requirement into an
        approval.  Denial is accepted even for an expired requirement; refusing
        authority is always safe.
        """
        if not isinstance(decision, ApprovalDecision):
            raise ApprovalRejected("decision must be an ApprovalDecision")
        now = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            requirement = self.get_requirement(decision.requirement_id)
            if requirement is None:
                raise ApprovalRejected("unknown approval requirement")
            if self.get_grant(decision.requirement_id) is not None:
                raise ApprovalRejected(
                    "requirement was already approved; revoke the grant instead"
                )
            if self.get_denial(decision.requirement_id) is not None:
                raise ApprovalRejected("requirement was already denied")
            self._validate_decision_event(decision, requirement, now)
            self._verify_member(decision, requirement.approval_group_id, now)
            self.db.execute(
                """INSERT INTO tc_plan_authz_nonces (nonce, requirement_id, recorded_utc)
                   VALUES (?, ?, ?)""",
                (decision.nonce, decision.requirement_id, utc_text(now)),
            )
            self.db.execute(
                """INSERT INTO tc_plan_authz_denials
                   (requirement_id, nonce, card_hash, group_id, user_id, display_name,
                    occurred_utc, recorded_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.requirement_id, decision.nonce, decision.card_hash,
                    decision.group_id, decision.user_id, decision.display_name,
                    utc_text(decision.occurred_at), utc_text(now),
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise ApprovalRejected("decision nonce was already consumed") from error
        except BaseException:
            self.db.rollback()
            raise

    def revoke_grant(self, requirement_id: str, *, revoked_by: str, reason: str) -> None:
        """Durably withdraw an issued approval grant; preflight honours it."""
        _identifier(requirement_id, "requirement_id")
        _identifier(revoked_by, "revoked_by")
        _identifier(reason, "reason")
        now = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            grant = self.get_grant(requirement_id)
            if grant is None:
                raise AuthorizationError("no approval grant is recorded for this requirement")
            self.db.execute(
                """INSERT OR IGNORE INTO tc_plan_authz_revocations
                   (requirement_id, grant_id, revoked_by, reason, revoked_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (requirement_id, grant.grant_id, revoked_by, reason, utc_text(now)),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def revoke_standing_grant(self, grant_id: str, *, revoked_by: str, reason: str) -> None:
        """Durably withdraw a standing grant from all future authorization."""
        _identifier(grant_id, "grant_id")
        _identifier(revoked_by, "revoked_by")
        _identifier(reason, "reason")
        now = self._now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT OR IGNORE INTO tc_plan_authz_standing_revocations
                   (grant_id, revoked_by, reason, revoked_utc)
                   VALUES (?, ?, ?, ?)""",
                (grant_id, revoked_by, reason, utc_text(now)),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def commit_standing_uses(
        self, plan: Plan, decision: PlanDecision
    ) -> tuple[tuple[str, str], ...]:
        """Durably charge the rate budget for every standing-consent step.

        A (future) executor must call this before acting under standing
        consent.  The decision is rederived and must be exactly equal, and the
        rate budget is re-checked inside the write transaction, so two racing
        runners cannot both fit into the last budget slot.
        """
        if not isinstance(plan, Plan) or not isinstance(decision, PlanDecision):
            raise AuthorizationError("commit_standing_uses needs the plan and its decision")
        charged: list[tuple[str, str]] = []
        try:
            self.db.execute("BEGIN IMMEDIATE")
            # All authority, revocation and accounting reads follow the lock.
            now = self._now()
            rederived = self._authorize_at(plan, now)
            if decision != rederived:
                raise AuthorizationError(
                    "the supplied decision does not equal the internally rederived "
                    "decision for this exact plan; re-authorize and try again"
                )
            standing_ids = tuple(
                risk.step_id for risk in rederived.step_risks
                if risk.authority is Authority.STANDING_CONSENT
            )
            steps = {step.step_id: step for step in plan.steps}
            risks = {risk.step_id: risk for risk in rederived.step_risks}
            grants = self.active_standing_grants(now)
            for step_id in standing_ids:
                covering: StandingEffectGrant | None = None
                for grant in grants:
                    uses = self.rate_accountant.uses_since(
                        grant.grant_id,
                        now - timedelta(seconds=grant.rate_window_seconds),
                    )
                    if grant.covers(
                        steps[step_id], risks[step_id],
                        policy_revision=self.policy_revision, now=now, uses=uses,
                    ):
                        covering = grant
                        break
                if covering is None:
                    raise AuthorizationBlocked(
                        "standing consent no longer covers this step (revoked, "
                        "expired, or rate budget exhausted); request exact human approval"
                    )
                self.rate_accountant.record_use(
                    covering.grant_id, plan.content_hash, step_id, now
                )
                charged.append((covering.grant_id, step_id))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return tuple(charged)

    def workspace_preflight(
        self, plan: Plan, step: PlanStep, *, current_evidence_revision: str,
        current_machine_id: str,
    ) -> ApprovalGrant:
        """Issue a local-only lease binding, without consuming production approval.

        This standing binding cannot authorize any other step or survive a policy,
        evidence, or plan change. Only the isolated workspace worker may use it.
        """
        now = self._now()
        if (
            step not in plan.steps or not is_local_workspace_step(step)
            or current_machine_id != MACHINE_ID or plan.machine_id != MACHINE_ID
            or current_evidence_revision != plan.evidence_revision
            or not plan.created_at <= now < plan.expires_at
            or derive_step(step, classifier=self._classify_reference).authority is not Authority.AGENT
        ):
            raise AuthorizationBlocked("workspace preflight failed; replan or request exact approval")
        binding = stable_hash({"plan": plan.content_hash, "step": step.step_id,
                               "policy": self.policy_revision})
        return ApprovalGrant(
            grant_id="workspace-" + binding, task_id=plan.task_id, plan_id=plan.plan_id,
            plan_hash=plan.content_hash, step_ids=(step.step_id,),
            kind=ApprovalKind.STANDING_CONSENT, approver_id="isolated-workspace-policy",
            policy_revision=self.policy_revision, evidence_revision=plan.evidence_revision,
            nonce="workspace-" + binding, issued_at=plan.created_at,
            expires_at=plan.expires_at,
        )

    def preflight(
        self,
        requirement_id: str,
        plan: Plan,
        *,
        current_evidence_revision: str,
        current_machine_id: str,
        now: datetime | None = None,
    ) -> PreflightDecision:
        """Revalidate everything an execution would rely on. Read-only.

        Re-authorizes the exact plan from scratch -- including standing grant
        validity, predicates, and rate budgets -- and refuses when the set of
        steps requiring exact approval no longer matches what the human saw.
        Refusal here is not permanent: a corrected plan can earn a fresh
        approval.  It consumes nothing; Phase 4's executor owns leases.
        """
        now = _require_utc(now if now is not None else self._now(), "clock")
        requirement = self.get_requirement(requirement_id)
        if requirement is None:
            return PreflightDecision(False, "unknown approval requirement")
        grant = self.get_grant(requirement_id)
        if grant is None:
            return PreflightDecision(False, "no exact human approval is recorded")
        if self.get_denial(requirement_id) is not None:
            return PreflightDecision(False, "requirement was explicitly denied")
        if self.grant_revoked(requirement_id):
            return PreflightDecision(False, "the approval grant was revoked; request a fresh one")
        if not isinstance(plan, Plan):
            return PreflightDecision(False, "preflight requires the exact plan")
        if plan.content_hash != requirement.plan_hash or grant.plan_hash != requirement.plan_hash:
            return PreflightDecision(
                False, "plan hash changed; the approval does not cover this plan"
            )
        if plan.version != requirement.plan_version:
            return PreflightDecision(False, "plan version changed after approval")
        if current_machine_id != MACHINE_ID or plan.machine_id != MACHINE_ID:
            return PreflightDecision(False, "target machine identity verification failed")
        if requirement.approval_group_id != self.approval_group_id:
            return PreflightDecision(False, "requirement is bound to a different approval group")
        if now >= requirement.expires_at or now >= grant.expires_at:
            return PreflightDecision(False, "approval expired; request a fresh one")
        if now >= plan.expires_at:
            return PreflightDecision(False, "plan expired; replan and reapprove")
        if current_evidence_revision != grant.evidence_revision:
            return PreflightDecision(False, "evidence revision changed after approval")
        if (
            grant.policy_revision != self.policy_revision
            or requirement.policy_revision != self.policy_revision
        ):
            return PreflightDecision(False, "policy revision changed after approval")
        try:
            current_decision = self._authorize_at(plan, now)
        except AuthorizationError as error:
            return PreflightDecision(
                False, f"the plan no longer authorizes deterministically: {error}"
            )
        if requirement.payload_purpose is not None:
            if not set(requirement.step_ids) <= set(current_decision.payload_step_ids):
                return PreflightDecision(
                    False, "payload step derivation changed after approval"
                )
        elif set(requirement.step_ids) != set(current_decision.approval_step_ids):
            return PreflightDecision(
                False,
                "the set of steps requiring exact approval changed after approval "
                "(a standing grant may have expired, been revoked, or run out of "
                "budget); a fresh approval is required",
            )
        current_risks = {risk.step_id: risk for risk in current_decision.step_risks}
        for item in requirement.steps:
            step_id = item["step"]["step_id"]
            risk = current_risks.get(step_id)
            if (
                risk is None
                or tuple(sorted(item["flags"])) != tuple(sorted(risk.flags))
                or tuple(item["tenant_references"]) != risk.tenant_references
            ):
                return PreflightDecision(
                    False, "derived effects or tenant references changed after approval"
                )
        try:
            self._revalidate_bindings(requirement)
        except AuthorizationBlocked as error:
            return PreflightDecision(False, f"rental binding changed after approval: {error}")
        return PreflightDecision(True, "authorized; the grant remains unconsumed")


def plan_authorization_enabled(environment: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    return source.get(FEATURE_FLAG_ENV, "") == "1"


def build_plan_authorization(
    environment: Mapping[str, str] | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    membership: MembershipVerifier | None = None,
    resolver: RentalResolver | None = None,
    policy_revision: str | None = None,
    approval_group_id: int | None = None,
    standing_grants: tuple[StandingEffectGrant, ...] = (),
    clock: Callable[[], datetime] = _utc_now,
) -> PlanAuthorizationService | None:
    """The deployment entry point: None until the feature flag turns it on.

    An enabled service still fails closed without its injected trust roots;
    there is no default membership verifier or rental resolver.
    """
    if not plan_authorization_enabled(environment):
        return None
    if connection is None or membership is None or resolver is None:
        raise AuthorizationError(
            "plan authorization requires an injected connection, membership "
            "verifier, and trusted rental resolver"
        )
    if policy_revision is None or approval_group_id is None:
        raise AuthorizationError(
            "plan authorization requires a commissioned policy revision and approval group"
        )
    return PlanAuthorizationService(
        connection,
        membership=membership,
        resolver=resolver,
        policy_revision=policy_revision,
        approval_group_id=approval_group_id,
        standing_grants=standing_grants,
        clock=clock,
    )


__all__ = [
    "ACTIONABLE_RENTAL_STATUSES", "APPROVAL_LIFETIME", "APPROVE_CALLBACK_PREFIX",
    "AUTHZ_SCHEMA_VERSION", "ApprovalDecision", "ApprovalRejected",
    "ApprovalRequirement", "Authority", "AuthorizationBlocked",
    "AuthorizationError", "CALLBACK_DATA_MAX_BYTES", "CARD_MESSAGE_MAX_BYTES",
    "CallbackAction", "DENY_CALLBACK_PREFIX", "FEATURE_FLAG_ENV",
    "PlanAuthorizationService", "PlanDecision", "PreflightDecision",
    "RentalRecord", "RentalResolver", "StandingEffectGrant",
    "StandingRateAccountant", "StepRisk", "TENANT_PAYLOAD_OPERATION",
    "Unrepresentable", "authorize_plan", "build_plan_authorization",
    "derive_step", "is_local_workspace_step", "parse_callback", "plan_authorization_enabled",
    "render_approval_card", "render_approval_card_messages",
]
