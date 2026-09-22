"""Strict, immutable Phase 0 plan and authorization contracts.

These types describe data only.  They deliberately do not authorize or execute
anything.  Every contract has an explicit schema version, a canonical JSON
representation, and a SHA-256 digest over that representation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, TypeVar


SCHEMA_VERSION = 1
AUTHORIZATION_SCHEMA_VERSION = 2
MACHINE_ID = "17049"
MAX_STRING_LENGTH = 16 * 1024
MAX_COLLECTION_LENGTH = 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:\b(?:authorization|proxy-authorization|cookie|set-cookie|credential|"
    r"password|passwd|passphrase|passcode|pass|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|session[_-]?key)\b\s*[:=]\s*\S+|"
    r"\b(?:bearer|basic)\s+\S+|[a-z][a-z0-9+.-]*://[^/\s@]+:[^/\s@]+@|"
    r"[a-z][a-z0-9+.-]*://[^/\s@]+@|"
    r"(?:[?&](?:[^&=]*(?:token|secret|password|passwd|passphrase|passcode|pass|pwd|api[_-]?key|access[_-]?key)"
    r"[^&=]*)=)[^&#\s]*|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)"
)
_CREDENTIAL_FLAG = re.compile(
    r"^--?(?:[a-z0-9]+[-_])*(?:auth(?:orization|entication)?|credential|"
    r"password|passwd|passphrase|passcode|pass|pwd|secret|token|api[-_]?key|access[-_]?key|"
    r"client[-_]?secret|private[-_]?key|session[-_]?key)"
    r"(?:[-_][a-z0-9]+)*(?:=.*)?$",
    re.IGNORECASE,
)
_USER_FLAG = re.compile(r"^(?:-u|--user(?:name)?)(?:=(.*))?$", re.IGNORECASE)
_HEADER_FLAG = re.compile(r"^(?:-H|--header)(?:=(.*))?$", re.IGNORECASE)
_CREDENTIAL_KEY_PARTS = frozenset(
    {
        "authorization", "cookie", "credential", "credentials", "password",
        "passwd", "passphrase", "pwd", "privatekey", "apikey", "accesskey",
        "secret", "secrets", "token", "clientsecret", "sessionkey", "header",
        "headers",
    }
)
_CREDENTIAL_KEY_EXACT = frozenset({"pass", "passcode"})


class ContractError(ValueError):
    """A serialized contract is malformed, ambiguous, or unsafe."""


def _reject_control_characters(value: str, path: str) -> None:
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ContractError(f"{path} contains a control character or Unicode surrogate")


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path} must be a string")
    if (not allow_empty and not value) or len(value) > MAX_STRING_LENGTH:
        raise ContractError(f"{path} is empty or too long")
    _reject_control_characters(value, path)
    if _CREDENTIAL_VALUE.search(value):
        raise ContractError(f"{path} appears to contain credential material")
    return value


def _identifier(value: Any, path: str) -> str:
    result = _string(value, path)
    if len(result) > 256:
        raise ContractError(f"{path} is too long")
    return result


def _utc(value: datetime, path: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ContractError(f"{path} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ContractError(f"{path} must be UTC")
    return value.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    # Fixed-width text is safe for SQLite ordering as well as human inspection.
    return _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: Any, path: str) -> datetime:
    text = _string(value, path)
    if not text.endswith("Z"):
        raise ContractError(f"{path} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ContractError(f"{path} is not a valid timestamp") from error
    parsed = _utc(parsed, path)
    if utc_text(parsed) != text:
        raise ContractError(f"{path} is not a canonical timestamp")
    return parsed


def _credential_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _CREDENTIAL_KEY_EXACT or any(
        part in normalized for part in _CREDENTIAL_KEY_PARTS
    )


def _reject_credential_sequence(value: list[Any], path: str) -> None:
    """Reject secret-bearing flag/value forms in any free-form JSON sequence."""
    for index, item in enumerate(value):
        if not isinstance(item, str):
            continue
        following = value[index + 1] if index + 1 < len(value) else None
        if _CREDENTIAL_FLAG.fullmatch(item) and ("=" in item or following is not None):
            raise ContractError(f"{path}[{index}] is a credential-bearing argv flag")
        user_flag = _USER_FLAG.fullmatch(item)
        if user_flag is not None:
            userinfo = user_flag.group(1) if "=" in item else following
            if isinstance(userinfo, str) and ":" in userinfo:
                raise ContractError(f"{path}[{index}] is a credential-bearing user flag")
        elif item.casefold().startswith("-u") and not item.casefold().startswith("--"):
            userinfo = item[2:].removeprefix("=")
            if ":" in userinfo:
                raise ContractError(f"{path}[{index}] is a credential-bearing user flag")
        header_flag = _HEADER_FLAG.fullmatch(item)
        if header_flag is not None:
            header = header_flag.group(1) if "=" in item else following
            if isinstance(header, str) and _CREDENTIAL_VALUE.search(header):
                raise ContractError(f"{path}[{index}] is a credential-bearing header flag")
        normalized = re.sub(r"[^a-z0-9]", "", item.casefold())
        if (
            following is not None
            and normalized in _CREDENTIAL_KEY_PARTS
            and normalized not in {"header", "headers"}
        ):
            raise ContractError(f"{path}[{index}] names credential material")


def _safe_json(
    value: Any, path: str = "value", *, forbid_credential_keys: bool = True,
) -> Any:
    """Detach and validate a JSON value before it crosses a hash boundary."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, str):
        result = _string(value, path, allow_empty=True)
        if _CREDENTIAL_VALUE.search(result):
            raise ContractError(f"{path} appears to contain credential material")
        return result
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_LENGTH:
            raise ContractError(f"{path} contains too many fields")
        copied: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = _string(raw_key, f"{path} key")
            if len(key) > 128:
                raise ContractError(f"{path}.{key} key is too long")
            if forbid_credential_keys and _credential_key(key):
                raise ContractError(f"{path}.{key} is credential-shaped")
            copied[key] = _safe_json(
                item, f"{path}.{key}",
                forbid_credential_keys=forbid_credential_keys,
            )
        if forbid_credential_keys:
            folded = {key.casefold(): item for key, item in copied.items()}
            name = next(
                (
                    folded[key] for key in ("name", "key", "parameter")
                    if isinstance(folded.get(key), str)
                ),
                None,
            )
            if (
                isinstance(name, str)
                and _credential_key(name)
                and any(key in folded for key in ("value", "values", "content"))
            ):
                raise ContractError(f"{path} contains a named credential value")
            named_value = next(
                (folded[key] for key in ("value", "content") if key in folded),
                None,
            )
            if (
                isinstance(name, str)
                and _USER_FLAG.fullmatch(name) is not None
                and isinstance(named_value, str)
                and ":" in named_value
            ):
                raise ContractError(f"{path} contains credential-bearing userinfo")
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_LENGTH:
            raise ContractError(f"{path} contains too many values")
        copied = [
            _safe_json(
                item, f"{path}[{index}]",
                forbid_credential_keys=forbid_credential_keys,
            )
            for index, item in enumerate(value)
        ]
        if forbid_credential_keys:
            _reject_credential_sequence(copied, path)
        return copied
    raise ContractError(f"{path} must contain JSON values only")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def strict_json_loads(value: str | bytes) -> Any:
    """Load JSON while rejecting duplicate keys, constants, and unsafe strings."""
    try:
        document = json.loads(
            value,
            object_pairs_hook=_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ContractError(f"invalid JSON constant: {constant}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ContractError("invalid JSON") from error
    # Schema constructors reject unknown fields and validate free-form mappings.
    # Known contract fields such as Effect.secrets are not credential material.
    return _safe_json(document, "document", forbid_credential_keys=False)


def canonical_json(value: Any) -> bytes:
    """Return the one stable UTF-8 encoding accepted for contract hashing."""
    if isinstance(value, Contract):
        value = value.to_document()
    safe = _safe_json(
        _thaw(value), "document", forbid_credential_keys=False,
    )
    try:
        return json.dumps(
            safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContractError("document contains a Unicode surrogate") from error


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _fields(
    document: Any, expected: set[str], contract: str, *, schema_version: int = SCHEMA_VERSION,
) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise ContractError(f"{contract} must be an object")
    if type(document.get("schema_version")) is not int or document["schema_version"] != schema_version:
        raise ContractError(f"unsupported {contract} schema version")
    actual = set(document)
    if actual != expected:
        unknown = sorted(str(name) for name in actual - expected)
        missing = sorted(str(name) for name in expected - actual)
        detail = []
        if unknown:
            detail.append(f"unknown fields {unknown}")
        if missing:
            detail.append(f"missing fields {missing}")
        raise ContractError(f"invalid {contract}: " + "; ".join(detail))
    return document


def _document_array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_LENGTH:
        raise ContractError(f"{path} must be a bounded JSON array")
    return value


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_COLLECTION_LENGTH:
        raise ContractError(f"{path} must be a bounded array")
    result = tuple(_string(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise ContractError(f"{path} contains duplicates")
    return result


T = TypeVar("T", bound="Contract")


class Contract:
    """Shared serialization surface for immutable contracts."""

    schema_version: int
    SCHEMA_VERSION: ClassVar[int] = SCHEMA_VERSION

    def to_document(self) -> dict[str, Any]:
        raise NotImplementedError

    def canonical_json(self) -> bytes:
        return canonical_json(self.to_document())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()

    @property
    def digest(self) -> str:
        return self.content_hash

    def to_json(self) -> bytes:
        return self.canonical_json()

    @classmethod
    def from_json(cls: type[T], value: str | bytes) -> T:
        return cls.from_document(strict_json_loads(value))


@dataclass(frozen=True)
class Effect(Contract):
    effect_id: str
    description: str
    owned_component: bool = False
    host: bool = False
    reachability: bool = False
    tenant: bool = False
    external_commitment: bool = False
    secrets: bool = False
    irreversible: bool = False
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "effect_id", "description", "owned_component", "host",
        "reachability", "tenant", "external_commitment", "secrets", "irreversible",
    }

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _string(self.description, "description")
        for name in self._FIELDS - {"schema_version", "effect_id", "description"}:
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} must be a boolean")

    def to_document(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self._FIELDS)}

    @classmethod
    def from_document(cls, document: Any) -> "Effect":
        value = _fields(document, cls._FIELDS, "Effect")
        return cls(**{name: value[name] for name in cls._FIELDS if name != "schema_version"})


@dataclass(frozen=True)
class PlanStep(Contract):
    step_id: str
    operation: str
    arguments: Mapping[str, Any]
    effects: tuple[Effect, ...]
    affected_resources: tuple[str, ...]
    preconditions: tuple[Mapping[str, Any], ...]
    postconditions: tuple[Mapping[str, Any], ...]
    checkpoint: Mapping[str, Any] | None
    rollback: Mapping[str, Any] | None
    rollback_impossible_reason: str | None
    expected_interruption: str
    max_execution_seconds: int
    artifacts: tuple[str, ...] = ()
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "step_id", "operation", "arguments", "effects",
        "affected_resources", "preconditions", "postconditions", "checkpoint",
        "rollback", "rollback_impossible_reason", "expected_interruption",
        "max_execution_seconds", "artifacts",
    }

    def __post_init__(self) -> None:
        _identifier(self.step_id, "step_id")
        _string(self.operation, "operation")
        if not isinstance(self.arguments, Mapping):
            raise ContractError("arguments must be an object")
        object.__setattr__(self, "arguments", _freeze(_safe_json(self.arguments, "arguments")))
        effects = tuple(self.effects)
        if not effects or any(not isinstance(effect, Effect) for effect in effects):
            raise ContractError("effects must contain at least one Effect")
        if len({effect.effect_id for effect in effects}) != len(effects):
            raise ContractError("effects contain duplicate identifiers")
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "affected_resources", _strings(self.affected_resources, "affected_resources"))
        for name in ("preconditions", "postconditions"):
            values = tuple(getattr(self, name))
            if not values or any(not isinstance(item, Mapping) for item in values):
                raise ContractError(f"{name} must contain deterministic check objects")
            object.__setattr__(self, name, tuple(_freeze(_safe_json(item, name)) for item in values))
        for name in ("checkpoint", "rollback"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ContractError(f"{name} must be an object or null")
                object.__setattr__(self, name, _freeze(_safe_json(value, name)))
        if (self.rollback is None) == (self.rollback_impossible_reason is None):
            raise ContractError("provide exactly one rollback or rollback_impossible_reason")
        if self.rollback_impossible_reason is not None:
            _string(self.rollback_impossible_reason, "rollback_impossible_reason")
        _string(self.expected_interruption, "expected_interruption", allow_empty=True)
        if isinstance(self.max_execution_seconds, bool) or not isinstance(self.max_execution_seconds, int):
            raise ContractError("max_execution_seconds must be an integer")
        if self.max_execution_seconds < 1 or self.max_execution_seconds > 86400:
            raise ContractError("max_execution_seconds is outside its bounds")
        artifacts = _strings(self.artifacts, "artifacts")
        if any(not _ARTIFACT_DIGEST.fullmatch(artifact) for artifact in artifacts):
            raise ContractError("artifacts must be immutable sha256 content digests")
        object.__setattr__(self, "artifacts", artifacts)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "step_id": self.step_id,
            "operation": self.operation,
            "arguments": _thaw(self.arguments),
            "effects": [item.to_document() for item in self.effects],
            "affected_resources": list(self.affected_resources),
            "preconditions": [_thaw(item) for item in self.preconditions],
            "postconditions": [_thaw(item) for item in self.postconditions],
            "checkpoint": _thaw(self.checkpoint),
            "rollback": _thaw(self.rollback),
            "rollback_impossible_reason": self.rollback_impossible_reason,
            "expected_interruption": self.expected_interruption,
            "max_execution_seconds": self.max_execution_seconds,
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_document(cls, document: Any) -> "PlanStep":
        value = _fields(document, cls._FIELDS, "PlanStep")
        effects = _document_array(value["effects"], "effects")
        affected_resources = _document_array(value["affected_resources"], "affected_resources")
        preconditions = _document_array(value["preconditions"], "preconditions")
        postconditions = _document_array(value["postconditions"], "postconditions")
        artifacts = _document_array(value["artifacts"], "artifacts")
        return cls(
            step_id=value["step_id"], operation=value["operation"],
            arguments=value["arguments"],
            effects=tuple(Effect.from_document(item) for item in effects),
            affected_resources=tuple(affected_resources),
            preconditions=tuple(preconditions), postconditions=tuple(postconditions),
            checkpoint=value["checkpoint"], rollback=value["rollback"],
            rollback_impossible_reason=value["rollback_impossible_reason"],
            expected_interruption=value["expected_interruption"],
            max_execution_seconds=value["max_execution_seconds"], artifacts=tuple(artifacts),
        )


@dataclass(frozen=True)
class Plan(Contract):
    plan_id: str
    task_id: str
    version: int
    objective: str
    evidence_revision: str
    assumptions: tuple[str, ...]
    freshness_requirements: tuple[Mapping[str, Any], ...]
    steps: tuple[PlanStep, ...]
    created_at: datetime
    expires_at: datetime
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "plan_id", "task_id", "version", "objective",
        "evidence_revision", "assumptions", "freshness_requirements", "steps",
        "created_at", "expires_at", "machine_id",
    }

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan_id")
        _identifier(self.task_id, "task_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ContractError("plan version must be a positive integer")
        _string(self.objective, "objective")
        _identifier(self.evidence_revision, "evidence_revision")
        object.__setattr__(self, "assumptions", _strings(self.assumptions, "assumptions"))
        freshness = tuple(self.freshness_requirements)
        if not freshness or any(not isinstance(item, Mapping) for item in freshness):
            raise ContractError("freshness_requirements must contain check objects")
        object.__setattr__(self, "freshness_requirements", tuple(
            _freeze(_safe_json(item, "freshness_requirements")) for item in freshness
        ))
        steps = tuple(self.steps)
        if not steps or any(not isinstance(step, PlanStep) for step in steps):
            raise ContractError("steps must contain at least one PlanStep")
        if len({step.step_id for step in steps}) != len(steps):
            raise ContractError("steps contain duplicate identifiers")
        object.__setattr__(self, "steps", steps)
        created = _utc(self.created_at, "created_at")
        expires = _utc(self.expires_at, "expires_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        if expires <= created:
            raise ContractError("plan expiry must follow creation")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"plans are restricted to machine {MACHINE_ID}")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "plan_id": self.plan_id,
            "task_id": self.task_id, "version": self.version, "objective": self.objective,
            "evidence_revision": self.evidence_revision, "assumptions": list(self.assumptions),
            "freshness_requirements": [_thaw(item) for item in self.freshness_requirements],
            "steps": [step.to_document() for step in self.steps],
            "created_at": utc_text(self.created_at), "expires_at": utc_text(self.expires_at),
            "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "Plan":
        value = _fields(document, cls._FIELDS, "Plan")
        assumptions = _document_array(value["assumptions"], "assumptions")
        freshness = _document_array(value["freshness_requirements"], "freshness_requirements")
        steps = _document_array(value["steps"], "steps")
        return cls(
            plan_id=value["plan_id"], task_id=value["task_id"], version=value["version"],
            objective=value["objective"], evidence_revision=value["evidence_revision"],
            assumptions=tuple(assumptions),
            freshness_requirements=tuple(freshness),
            steps=tuple(PlanStep.from_document(item) for item in steps),
            created_at=parse_utc(value["created_at"], "created_at"),
            expires_at=parse_utc(value["expires_at"], "expires_at"),
            machine_id=value["machine_id"],
        )


class ApprovalKind(str, Enum):
    EXACT_HUMAN = "exact-human"
    STANDING_CONSENT = "standing-consent"


@dataclass(frozen=True)
class ApprovalGrant(Contract):
    grant_id: str
    task_id: str
    plan_id: str
    plan_hash: str
    step_ids: tuple[str, ...]
    kind: ApprovalKind
    approver_id: str
    policy_revision: str
    evidence_revision: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "grant_id", "task_id", "plan_id", "plan_hash", "step_ids",
        "kind", "approver_id", "policy_revision", "evidence_revision", "nonce",
        "issued_at", "expires_at",
    }

    def __post_init__(self) -> None:
        for name in ("grant_id", "task_id", "plan_id", "approver_id", "policy_revision", "evidence_revision", "nonce"):
            _identifier(getattr(self, name), name)
        if not _SHA256.fullmatch(self.plan_hash):
            raise ContractError("plan_hash must be a SHA-256 digest")
        object.__setattr__(self, "step_ids", _strings(self.step_ids, "step_ids"))
        if not self.step_ids:
            raise ContractError("step_ids must not be empty")
        if not isinstance(self.kind, ApprovalKind):
            raise ContractError("kind must be an ApprovalKind")
        issued, expires = _utc(self.issued_at, "issued_at"), _utc(self.expires_at, "expires_at")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if expires <= issued:
            raise ContractError("approval expiry must follow issuance")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "grant_id": self.grant_id,
            "task_id": self.task_id, "plan_id": self.plan_id, "plan_hash": self.plan_hash,
            "step_ids": list(self.step_ids), "kind": self.kind.value,
            "approver_id": self.approver_id, "policy_revision": self.policy_revision,
            "evidence_revision": self.evidence_revision, "nonce": self.nonce,
            "issued_at": utc_text(self.issued_at), "expires_at": utc_text(self.expires_at),
        }

    @classmethod
    def from_document(cls, document: Any) -> "ApprovalGrant":
        value = _fields(document, cls._FIELDS, "ApprovalGrant")
        step_ids = _document_array(value["step_ids"], "step_ids")
        try:
            kind = ApprovalKind(value["kind"])
        except (ValueError, TypeError) as error:
            raise ContractError("unknown approval kind") from error
        return cls(
            grant_id=value["grant_id"], task_id=value["task_id"], plan_id=value["plan_id"],
            plan_hash=value["plan_hash"], step_ids=tuple(step_ids), kind=kind,
            approver_id=value["approver_id"], policy_revision=value["policy_revision"],
            evidence_revision=value["evidence_revision"], nonce=value["nonce"],
            issued_at=parse_utc(value["issued_at"], "issued_at"),
            expires_at=parse_utc(value["expires_at"], "expires_at"),
        )


@dataclass(frozen=True)
class ExecutionLease(Contract):
    SCHEMA_VERSION: ClassVar[int] = AUTHORIZATION_SCHEMA_VERSION
    lease_id: str
    task_id: str
    plan_id: str
    plan_hash: str
    step_id: str
    grant_id: str
    grant_hash: str
    policy_revision: str
    evidence_revision: str
    holder: str
    idempotency_key: str
    acquired_at: datetime
    expires_at: datetime
    schema_version: int = field(default=AUTHORIZATION_SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "lease_id", "task_id", "plan_id", "plan_hash", "step_id",
        "grant_id", "grant_hash", "policy_revision", "evidence_revision", "holder",
        "idempotency_key", "acquired_at", "expires_at",
    }

    def __post_init__(self) -> None:
        for name in (
            "lease_id", "task_id", "plan_id", "step_id", "grant_id", "policy_revision",
            "evidence_revision", "holder", "idempotency_key",
        ):
            _identifier(getattr(self, name), name)
        for name in ("plan_hash", "grant_hash"):
            if not _SHA256.fullmatch(getattr(self, name)):
                raise ContractError(f"{name} must be a SHA-256 digest")
        acquired, expires = _utc(self.acquired_at, "acquired_at"), _utc(self.expires_at, "expires_at")
        object.__setattr__(self, "acquired_at", acquired)
        object.__setattr__(self, "expires_at", expires)
        if expires <= acquired:
            raise ContractError("lease expiry must follow acquisition")

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "lease_id": self.lease_id,
            "task_id": self.task_id, "plan_id": self.plan_id, "plan_hash": self.plan_hash,
            "step_id": self.step_id, "grant_id": self.grant_id, "grant_hash": self.grant_hash,
            "policy_revision": self.policy_revision, "evidence_revision": self.evidence_revision,
            "holder": self.holder,
            "idempotency_key": self.idempotency_key,
            "acquired_at": utc_text(self.acquired_at), "expires_at": utc_text(self.expires_at),
        }

    @classmethod
    def from_document(cls, document: Any) -> "ExecutionLease":
        value = _fields(
            document, cls._FIELDS, "ExecutionLease",
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
        )
        return cls(
            lease_id=value["lease_id"], task_id=value["task_id"], plan_id=value["plan_id"],
            plan_hash=value["plan_hash"], step_id=value["step_id"],
            grant_id=value["grant_id"], grant_hash=value["grant_hash"],
            policy_revision=value["policy_revision"], evidence_revision=value["evidence_revision"],
            holder=value["holder"],
            idempotency_key=value["idempotency_key"],
            acquired_at=parse_utc(value["acquired_at"], "acquired_at"),
            expires_at=parse_utc(value["expires_at"], "expires_at"),
        )


class VerificationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Verification(Contract):
    SCHEMA_VERSION: ClassVar[int] = AUTHORIZATION_SCHEMA_VERSION
    verification_id: str
    task_id: str
    plan_id: str
    plan_hash: str
    step_id: str | None
    lease_id: str | None
    lease_hash: str | None
    status: VerificationStatus
    checks: tuple[Mapping[str, Any], ...]
    evidence_revision: str
    performed_at: datetime
    schema_version: int = field(default=AUTHORIZATION_SCHEMA_VERSION, init=False)

    _FIELDS = {
        "schema_version", "verification_id", "task_id", "plan_id", "plan_hash",
        "step_id", "lease_id", "lease_hash", "status", "checks", "evidence_revision",
        "performed_at",
    }

    def __post_init__(self) -> None:
        for name in ("verification_id", "task_id", "plan_id", "evidence_revision"):
            _identifier(getattr(self, name), name)
        if self.step_id is not None:
            _identifier(self.step_id, "step_id")
        if (self.lease_id is None) != (self.lease_hash is None):
            raise ContractError("lease_id and lease_hash must both be set or both be null")
        if self.lease_id is not None:
            _identifier(self.lease_id, "lease_id")
            if not _SHA256.fullmatch(self.lease_hash or ""):
                raise ContractError("lease_hash must be a SHA-256 digest")
        if self.step_id is None and self.lease_id is not None:
            raise ContractError("plan-level verification cannot bind a step lease")
        if not _SHA256.fullmatch(self.plan_hash):
            raise ContractError("plan_hash must be a SHA-256 digest")
        if not isinstance(self.status, VerificationStatus):
            raise ContractError("status must be a VerificationStatus")
        checks = tuple(self.checks)
        if not checks or any(not isinstance(item, Mapping) for item in checks):
            raise ContractError("checks must contain deterministic check objects")
        object.__setattr__(self, "checks", tuple(_freeze(_safe_json(item, "checks")) for item in checks))
        object.__setattr__(self, "performed_at", _utc(self.performed_at, "performed_at"))

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "verification_id": self.verification_id,
            "task_id": self.task_id, "plan_id": self.plan_id, "plan_hash": self.plan_hash,
            "step_id": self.step_id, "lease_id": self.lease_id, "lease_hash": self.lease_hash,
            "status": self.status.value,
            "checks": [_thaw(item) for item in self.checks],
            "evidence_revision": self.evidence_revision,
            "performed_at": utc_text(self.performed_at),
        }

    @classmethod
    def from_document(cls, document: Any) -> "Verification":
        value = _fields(
            document, cls._FIELDS, "Verification",
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
        )
        checks = _document_array(value["checks"], "checks")
        try:
            status = VerificationStatus(value["status"])
        except (ValueError, TypeError) as error:
            raise ContractError("unknown verification status") from error
        return cls(
            verification_id=value["verification_id"], task_id=value["task_id"],
            plan_id=value["plan_id"], plan_hash=value["plan_hash"], step_id=value["step_id"],
            lease_id=value["lease_id"], lease_hash=value["lease_hash"],
            status=status, checks=tuple(checks), evidence_revision=value["evidence_revision"],
            performed_at=parse_utc(value["performed_at"], "performed_at"),
        )


__all__ = [
    "ApprovalGrant", "ApprovalKind", "ContractError", "Effect", "ExecutionLease",
    "MACHINE_ID", "Plan", "PlanStep", "Verification", "VerificationStatus",
    "canonical_json", "stable_hash", "strict_json_loads", "parse_utc", "utc_text",
]
