"""Disabled-by-default deterministic Phase 6 incident correlation.

Detector facts are immutable evidence, not instructions or authority.  Fresh fact
heads drive correlation; the older incident lifecycle remains the durable incident
history and is deliberately not consulted to decide whether a current fault exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping

from .incidents import delivery_identity, evidence_digest, stable_incident_id
from .plan_authorization import (
    PlanDecision,
    StandingEffectGrant,
    StandingRateAccountant,
    TenantClassifier,
    authorize_plan,
)
from .plans import (
    MACHINE_ID,
    Contract,
    ContractError,
    Effect,
    Plan,
    _document_array,
    _fields,
    _identifier,
    _string,
    _strings,
    _utc,
    parse_utc,
    stable_hash,
    utc_text,
)
from .state import StateStore, TARGET
from .tasks import TERMINAL_STATES, Task, TaskConflict, TaskEvent, TaskState, TaskStore


SCHEMA_VERSION = 1
CORRELATION_SCHEMA_VERSION = 2
MAX_FACTS_PER_BATCH = 256
MAX_RELATED_RESOURCES = 32
MAX_REPAIR_EVIDENCE_AGE_SECONDS = 300
_RESOURCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
_FAILURE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


class DetectorKind(str, Enum):
    COLLECTOR = "collector"
    GPU_INVENTORY = "gpu-inventory"
    NVIDIA_XID = "nvidia-xid"
    EXPORTER = "exporter"
    MARKET_CAPACITY = "market-capacity"
    DISK = "disk"
    MEMORY_OOM = "memory-oom"
    NETWORK = "network"
    BACKUP = "backup"
    CONTROLLER_SERVICE = "controller-service"
    NIX_GENERATION = "nix-generation"


class CorrelationDomain(str, Enum):
    GPU_CAPACITY = "gpu-capacity"
    STORAGE = "storage"
    MEMORY = "memory"
    NETWORK = "network"
    BACKUP = "backup"
    CONTROLLER = "controller"
    SYSTEM_GENERATION = "system-generation"


class FactStatus(str, Enum):
    FAULT = "fault"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"


class FactFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ResourceOwnership(str, Enum):
    OWNED_COMPONENT = "owned-component"
    HOST = "host"
    TENANT = "tenant"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


def _bounded_int(value: Any, name: str, *, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ContractError(f"{name} must be a bounded non-negative integer")
    return value


def _bounded_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a boolean")
    return value


def _bounded_identifier(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    result = _identifier(value, name)
    if pattern.fullmatch(result) is None:
        raise ContractError(f"{name} has an invalid stable identity")
    return result


@dataclass(frozen=True)
class FactPayload(Contract):
    """Base for strict detector-specific payloads."""

    SCHEMA_VERSION: ClassVar[int] = SCHEMA_VERSION
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def material_document(self) -> Mapping[str, Any]:
        return self.to_document()


@dataclass(frozen=True)
class CollectorPayload(FactPayload):
    age_seconds: int
    deadline_seconds: int
    failure_code: str | None

    _FIELDS = {"schema_version", "age_seconds", "deadline_seconds", "failure_code"}

    def __post_init__(self) -> None:
        _bounded_int(self.age_seconds, "age_seconds", maximum=30 * 86400)
        _bounded_int(self.deadline_seconds, "deadline_seconds", maximum=30 * 86400)
        if self.failure_code is not None:
            _bounded_identifier(self.failure_code, "failure_code", _FAILURE)

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "age_seconds": self.age_seconds,
                "deadline_seconds": self.deadline_seconds, "failure_code": self.failure_code}

    def material_document(self) -> Mapping[str, Any]:
        return {"overdue": self.age_seconds > self.deadline_seconds,
                "failure_code": self.failure_code}

    @classmethod
    def from_document(cls, document: Any) -> "CollectorPayload":
        value = _fields(document, cls._FIELDS, "CollectorPayload")
        return cls(value["age_seconds"], value["deadline_seconds"], value["failure_code"])


@dataclass(frozen=True)
class GpuInventoryPayload(FactPayload):
    expected_count: int
    observed_count: int
    missing_gpu_ids: tuple[str, ...] = ()

    _FIELDS = {"schema_version", "expected_count", "observed_count", "missing_gpu_ids"}

    def __post_init__(self) -> None:
        _bounded_int(self.expected_count, "expected_count", maximum=64)
        _bounded_int(self.observed_count, "observed_count", maximum=64)
        ids = _strings(self.missing_gpu_ids, "missing_gpu_ids")
        if len(ids) > 64:
            raise ContractError("missing_gpu_ids is too large")
        object.__setattr__(self, "missing_gpu_ids", tuple(sorted(ids)))

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "expected_count": self.expected_count,
                "observed_count": self.observed_count,
                "missing_gpu_ids": list(self.missing_gpu_ids)}

    @classmethod
    def from_document(cls, document: Any) -> "GpuInventoryPayload":
        value = _fields(document, cls._FIELDS, "GpuInventoryPayload")
        return cls(value["expected_count"], value["observed_count"],
                   tuple(_document_array(value["missing_gpu_ids"], "missing_gpu_ids")))


@dataclass(frozen=True)
class NvidiaXidPayload(FactPayload):
    code: int
    gpu_uuid: str | None = None
    pci_bdf: str | None = None

    _FIELDS = {"schema_version", "code", "gpu_uuid", "pci_bdf"}

    def __post_init__(self) -> None:
        _bounded_int(self.code, "code", maximum=9999)
        if (self.gpu_uuid is None) == (self.pci_bdf is None):
            raise ContractError("provide exactly one stable GPU UUID or PCI location")
        for name in ("gpu_uuid", "pci_bdf"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "code": self.code,
                "gpu_uuid": self.gpu_uuid, "pci_bdf": self.pci_bdf}

    @classmethod
    def from_document(cls, document: Any) -> "NvidiaXidPayload":
        value = _fields(document, cls._FIELDS, "NvidiaXidPayload")
        return cls(value["code"], value["gpu_uuid"], value["pci_bdf"])


@dataclass(frozen=True)
class ExporterPayload(FactPayload):
    healthy: bool
    restart_count: int
    restart_window_seconds: int
    loop_threshold: int

    _FIELDS = {"schema_version", "healthy", "restart_count", "restart_window_seconds", "loop_threshold"}

    def __post_init__(self) -> None:
        _bounded_bool(self.healthy, "healthy")
        _bounded_int(self.restart_count, "restart_count", maximum=100000)
        _bounded_int(self.restart_window_seconds, "restart_window_seconds", maximum=86400)
        if not 1 <= _bounded_int(self.loop_threshold, "loop_threshold", maximum=100000):
            raise ContractError("loop_threshold must be positive")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "healthy": self.healthy,
                "restart_count": self.restart_count,
                "restart_window_seconds": self.restart_window_seconds,
                "loop_threshold": self.loop_threshold}

    def material_document(self) -> Mapping[str, Any]:
        return {"healthy": self.healthy,
                "restart_loop": self.restart_count >= self.loop_threshold}

    @classmethod
    def from_document(cls, document: Any) -> "ExporterPayload":
        value = _fields(document, cls._FIELDS, "ExporterPayload")
        return cls(value["healthy"], value["restart_count"],
                   value["restart_window_seconds"], value["loop_threshold"])


@dataclass(frozen=True)
class MarketCapacityPayload(FactPayload):
    physical_free: int
    market_available: int
    rented: int
    total: int
    conclusive: bool

    _FIELDS = {"schema_version", "physical_free", "market_available", "rented", "total", "conclusive"}

    def __post_init__(self) -> None:
        for name in ("physical_free", "market_available", "rented", "total"):
            _bounded_int(getattr(self, name), name, maximum=64)
        _bounded_bool(self.conclusive, "conclusive")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "physical_free": self.physical_free,
                "market_available": self.market_available, "rented": self.rented,
                "total": self.total, "conclusive": self.conclusive}

    @classmethod
    def from_document(cls, document: Any) -> "MarketCapacityPayload":
        value = _fields(document, cls._FIELDS, "MarketCapacityPayload")
        return cls(value["physical_free"], value["market_available"], value["rented"],
                   value["total"], value["conclusive"])


@dataclass(frozen=True)
class DiskPayload(FactPayload):
    total_bytes: int
    available_bytes: int
    io_error_count: int

    _FIELDS = {"schema_version", "total_bytes", "available_bytes", "io_error_count"}

    def __post_init__(self) -> None:
        if not 1 <= _bounded_int(self.total_bytes, "total_bytes"):
            raise ContractError("total_bytes must be positive")
        _bounded_int(self.available_bytes, "available_bytes")
        _bounded_int(self.io_error_count, "io_error_count")
        if self.available_bytes > self.total_bytes:
            raise ContractError("available_bytes exceeds total_bytes")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "total_bytes": self.total_bytes,
                "available_bytes": self.available_bytes, "io_error_count": self.io_error_count}

    def material_document(self) -> Mapping[str, Any]:
        percent = (100 * self.available_bytes) // self.total_bytes
        pressure = "critical" if percent < 5 else "warning" if percent < 15 else "normal"
        return {"pressure": pressure, "io_failure": self.io_error_count > 0}

    @classmethod
    def from_document(cls, document: Any) -> "DiskPayload":
        value = _fields(document, cls._FIELDS, "DiskPayload")
        return cls(value["total_bytes"], value["available_bytes"], value["io_error_count"])


@dataclass(frozen=True)
class MemoryOomPayload(FactPayload):
    total_bytes: int
    available_bytes: int
    oom_kill_count: int

    _FIELDS = {"schema_version", "total_bytes", "available_bytes", "oom_kill_count"}

    def __post_init__(self) -> None:
        if not 1 <= _bounded_int(self.total_bytes, "total_bytes"):
            raise ContractError("total_bytes must be positive")
        _bounded_int(self.available_bytes, "available_bytes")
        _bounded_int(self.oom_kill_count, "oom_kill_count")
        if self.available_bytes > self.total_bytes:
            raise ContractError("available_bytes exceeds total_bytes")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "total_bytes": self.total_bytes,
                "available_bytes": self.available_bytes, "oom_kill_count": self.oom_kill_count}

    def material_document(self) -> Mapping[str, Any]:
        percent = (100 * self.available_bytes) // self.total_bytes
        pressure = "critical" if percent < 5 else "warning" if percent < 15 else "normal"
        return {"pressure": pressure, "oom": self.oom_kill_count > 0}

    @classmethod
    def from_document(cls, document: Any) -> "MemoryOomPayload":
        value = _fields(document, cls._FIELDS, "MemoryOomPayload")
        return cls(value["total_bytes"], value["available_bytes"], value["oom_kill_count"])


@dataclass(frozen=True)
class NetworkPayload(FactPayload):
    reachable: bool
    interface: str
    link_up: bool
    address_present: bool

    _FIELDS = {"schema_version", "reachable", "interface", "link_up", "address_present"}

    def __post_init__(self) -> None:
        _bounded_bool(self.reachable, "reachable")
        _bounded_identifier(self.interface, "interface", _FAILURE)
        _bounded_bool(self.link_up, "link_up")
        _bounded_bool(self.address_present, "address_present")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "reachable": self.reachable,
                "interface": self.interface, "link_up": self.link_up,
                "address_present": self.address_present}

    @classmethod
    def from_document(cls, document: Any) -> "NetworkPayload":
        value = _fields(document, cls._FIELDS, "NetworkPayload")
        return cls(value["reachable"], value["interface"], value["link_up"],
                   value["address_present"])


@dataclass(frozen=True)
class BackupPayload(FactPayload):
    age_seconds: int
    maximum_age_seconds: int
    last_run_succeeded: bool

    _FIELDS = {"schema_version", "age_seconds", "maximum_age_seconds", "last_run_succeeded"}

    def __post_init__(self) -> None:
        _bounded_int(self.age_seconds, "age_seconds", maximum=365 * 86400)
        _bounded_int(self.maximum_age_seconds, "maximum_age_seconds", maximum=365 * 86400)
        _bounded_bool(self.last_run_succeeded, "last_run_succeeded")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "age_seconds": self.age_seconds,
                "maximum_age_seconds": self.maximum_age_seconds,
                "last_run_succeeded": self.last_run_succeeded}

    def material_document(self) -> Mapping[str, Any]:
        return {"overdue": self.age_seconds > self.maximum_age_seconds,
                "last_run_succeeded": self.last_run_succeeded}

    @classmethod
    def from_document(cls, document: Any) -> "BackupPayload":
        value = _fields(document, cls._FIELDS, "BackupPayload")
        return cls(value["age_seconds"], value["maximum_age_seconds"],
                   value["last_run_succeeded"])


@dataclass(frozen=True)
class ControllerServicePayload(FactPayload):
    active: bool
    restart_count: int
    loop_threshold: int

    _FIELDS = {"schema_version", "active", "restart_count", "loop_threshold"}

    def __post_init__(self) -> None:
        _bounded_bool(self.active, "active")
        _bounded_int(self.restart_count, "restart_count", maximum=100000)
        if not 1 <= _bounded_int(self.loop_threshold, "loop_threshold", maximum=100000):
            raise ContractError("loop_threshold must be positive")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "active": self.active,
                "restart_count": self.restart_count, "loop_threshold": self.loop_threshold}

    def material_document(self) -> Mapping[str, Any]:
        return {"active": self.active, "restart_loop": self.restart_count >= self.loop_threshold}

    @classmethod
    def from_document(cls, document: Any) -> "ControllerServicePayload":
        value = _fields(document, cls._FIELDS, "ControllerServicePayload")
        return cls(value["active"], value["restart_count"], value["loop_threshold"])


@dataclass(frozen=True)
class NixGenerationPayload(FactPayload):
    expected_generation: str
    observed_generation: str

    _FIELDS = {"schema_version", "expected_generation", "observed_generation"}

    def __post_init__(self) -> None:
        _identifier(self.expected_generation, "expected_generation")
        _identifier(self.observed_generation, "observed_generation")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "expected_generation": self.expected_generation,
                "observed_generation": self.observed_generation}

    @classmethod
    def from_document(cls, document: Any) -> "NixGenerationPayload":
        value = _fields(document, cls._FIELDS, "NixGenerationPayload")
        return cls(value["expected_generation"], value["observed_generation"])


_PAYLOAD_TYPES: Mapping[DetectorKind, type[FactPayload]] = MappingProxyType({
    DetectorKind.COLLECTOR: CollectorPayload,
    DetectorKind.GPU_INVENTORY: GpuInventoryPayload,
    DetectorKind.NVIDIA_XID: NvidiaXidPayload,
    DetectorKind.EXPORTER: ExporterPayload,
    DetectorKind.MARKET_CAPACITY: MarketCapacityPayload,
    DetectorKind.DISK: DiskPayload,
    DetectorKind.MEMORY_OOM: MemoryOomPayload,
    DetectorKind.NETWORK: NetworkPayload,
    DetectorKind.BACKUP: BackupPayload,
    DetectorKind.CONTROLLER_SERVICE: ControllerServicePayload,
    DetectorKind.NIX_GENERATION: NixGenerationPayload,
})

_FIXED_DOMAINS: Mapping[DetectorKind, CorrelationDomain] = MappingProxyType({
    DetectorKind.GPU_INVENTORY: CorrelationDomain.GPU_CAPACITY,
    DetectorKind.NVIDIA_XID: CorrelationDomain.GPU_CAPACITY,
    DetectorKind.MARKET_CAPACITY: CorrelationDomain.GPU_CAPACITY,
    DetectorKind.DISK: CorrelationDomain.STORAGE,
    DetectorKind.MEMORY_OOM: CorrelationDomain.MEMORY,
    DetectorKind.NETWORK: CorrelationDomain.NETWORK,
    DetectorKind.BACKUP: CorrelationDomain.BACKUP,
    DetectorKind.CONTROLLER_SERVICE: CorrelationDomain.CONTROLLER,
    DetectorKind.NIX_GENERATION: CorrelationDomain.SYSTEM_GENERATION,
})

# Only software components controlled by this repository can ever be described
# as owned components.  Hardware, host paths, networking, capacity and system
# generation remain host/external authority even if an untrusted producer marks
# them otherwise.
_OWNED_COMPONENT_KINDS = frozenset({
    DetectorKind.COLLECTOR,
    DetectorKind.EXPORTER,
    DetectorKind.CONTROLLER_SERVICE,
})
_OWNED_RESOURCE_PREFIXES: Mapping[DetectorKind, tuple[str, ...]] = MappingProxyType({
    DetectorKind.COLLECTOR: ("collector:", "service:"),
    DetectorKind.EXPORTER: ("exporter:", "service:"),
    DetectorKind.CONTROLLER_SERVICE: ("controller:", "service:"),
})

_INSTANCE_MATERIAL_KINDS = frozenset({
    DetectorKind.NVIDIA_XID,
    DetectorKind.MEMORY_OOM,
    DetectorKind.EXPORTER,
    DetectorKind.CONTROLLER_SERVICE,
})


@dataclass(frozen=True)
class DetectorFact(Contract):
    fact_id: str
    evidence_revision: str
    kind: DetectorKind
    domain: CorrelationDomain
    resource_id: str
    failure_id: str
    status: FactStatus
    freshness: FactFreshness
    severity: Severity
    observed_at: datetime
    source_instance: str
    summary: str
    payload: FactPayload
    related_resources: tuple[str, ...] = ()
    ownership: ResourceOwnership = ResourceOwnership.UNKNOWN
    routine_reversible: bool = False
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "fact_id", "evidence_revision", "kind", "domain",
               "resource_id", "failure_id", "status", "freshness", "severity",
               "observed_at", "source_instance", "summary", "payload",
               "related_resources", "ownership", "routine_reversible", "machine_id"}

    def __post_init__(self) -> None:
        _identifier(self.fact_id, "fact_id")
        _identifier(self.evidence_revision, "evidence_revision")
        if not isinstance(self.kind, DetectorKind) or not isinstance(self.domain, CorrelationDomain):
            raise ContractError("fact kind and domain must be typed values")
        fixed = _FIXED_DOMAINS.get(self.kind)
        if fixed is not None and self.domain is not fixed:
            raise ContractError(f"{self.kind.value} facts belong to {fixed.value}")
        _bounded_identifier(self.resource_id, "resource_id", _RESOURCE)
        _bounded_identifier(self.failure_id, "failure_id", _FAILURE)
        if not isinstance(self.status, FactStatus) or not isinstance(self.freshness, FactFreshness):
            raise ContractError("fact status and freshness must be typed values")
        if not isinstance(self.severity, Severity):
            raise ContractError("severity must be typed")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        _identifier(self.source_instance, "source_instance")
        if len(_string(self.summary, "summary")) > 512:
            raise ContractError("summary is too long")
        expected = _PAYLOAD_TYPES[self.kind]
        if type(self.payload) is not expected:
            raise ContractError(f"{self.kind.value} requires {expected.__name__}")
        related = _strings(self.related_resources, "related_resources")
        if len(related) > MAX_RELATED_RESOURCES:
            raise ContractError("related_resources is too large")
        for resource in related:
            _bounded_identifier(resource, "related_resource", _RESOURCE)
        object.__setattr__(self, "related_resources", tuple(sorted(related)))
        if not isinstance(self.ownership, ResourceOwnership):
            raise ContractError("ownership must be typed")
        if (
            self.ownership is ResourceOwnership.OWNED_COMPONENT
            and self.kind not in _OWNED_COMPONENT_KINDS
        ):
            raise ContractError(f"{self.kind.value} facts cannot describe an owned component")
        if (
            self.ownership is ResourceOwnership.OWNED_COMPONENT
            and not self.resource_id.startswith(_OWNED_RESOURCE_PREFIXES[self.kind])
        ):
            raise ContractError("host paths cannot be classified as owned components")
        if not isinstance(self.routine_reversible, bool):
            raise ContractError("routine_reversible must be boolean")
        if self.routine_reversible and self.ownership is not ResourceOwnership.OWNED_COMPONENT:
            raise ContractError("routine reversible repair is limited to owned components")
        if self.machine_id != MACHINE_ID:
            raise ContractError(f"detector facts are restricted to machine {MACHINE_ID}")

    @property
    def failure_identity(self) -> str:
        return stable_hash([self.machine_id, self.kind.value, self.resource_id, self.failure_id])

    @property
    def source(self) -> str:
        return f"phase6:{self.kind.value}:{self.failure_identity}"

    @property
    def incident_id(self) -> str:
        return stable_incident_id(TARGET, self.source, self.kind.value, self.failure_identity)

    def material_document(self) -> Mapping[str, Any]:
        material = {
            "kind": self.kind.value, "domain": self.domain.value,
            "resource_id": self.resource_id, "failure_id": self.failure_id,
            "severity": self.severity.value, "payload": dict(self.payload.material_document()),
            "related_resources": list(self.related_resources),
            "ownership": self.ownership.value,
            "routine_reversible": self.routine_reversible,
        }
        if self.kind in _INSTANCE_MATERIAL_KINDS:
            material["source_instance"] = self.source_instance
        return material

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "fact_id": self.fact_id,
            "evidence_revision": self.evidence_revision, "kind": self.kind.value,
            "domain": self.domain.value, "resource_id": self.resource_id,
            "failure_id": self.failure_id, "status": self.status.value,
            "freshness": self.freshness.value, "severity": self.severity.value,
            "observed_at": utc_text(self.observed_at), "source_instance": self.source_instance,
            "summary": self.summary, "payload": self.payload.to_document(),
            "related_resources": list(self.related_resources),
            "ownership": self.ownership.value,
            "routine_reversible": self.routine_reversible, "machine_id": self.machine_id,
        }

    @classmethod
    def from_document(cls, document: Any) -> "DetectorFact":
        value = _fields(document, cls._FIELDS, "DetectorFact")
        try:
            kind = DetectorKind(value["kind"])
            domain = CorrelationDomain(value["domain"])
            status = FactStatus(value["status"])
            freshness = FactFreshness(value["freshness"])
            severity = Severity(value["severity"])
            ownership = ResourceOwnership(value["ownership"])
        except (TypeError, ValueError) as error:
            raise ContractError("unknown detector fact enum value") from error
        payload = _PAYLOAD_TYPES[kind].from_document(value["payload"])
        related = tuple(_document_array(value["related_resources"], "related_resources"))
        return cls(
            fact_id=value["fact_id"], evidence_revision=value["evidence_revision"],
            kind=kind, domain=domain, resource_id=value["resource_id"],
            failure_id=value["failure_id"], status=status, freshness=freshness,
            severity=severity, observed_at=parse_utc(value["observed_at"], "observed_at"),
            source_instance=value["source_instance"], summary=value["summary"],
            payload=payload, related_resources=related, ownership=ownership,
            routine_reversible=value["routine_reversible"], machine_id=value["machine_id"],
        )


@dataclass(frozen=True)
class DetectorBatch(Contract):
    batch_id: str
    evidence_revision: str
    collected_at: datetime
    facts: tuple[DetectorFact, ...]
    complete_detectors: tuple[DetectorKind, ...] = ()
    machine_id: str = MACHINE_ID
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    _FIELDS = {"schema_version", "batch_id", "evidence_revision", "collected_at", "facts",
               "complete_detectors", "machine_id"}

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        _identifier(self.evidence_revision, "evidence_revision")
        object.__setattr__(self, "collected_at", _utc(self.collected_at, "collected_at"))
        facts = tuple(self.facts)
        if len(facts) > MAX_FACTS_PER_BATCH or any(not isinstance(item, DetectorFact) for item in facts):
            raise ContractError("facts must be a bounded DetectorFact array")
        if len({item.fact_id for item in facts}) != len(facts):
            raise ContractError("facts contain duplicate identifiers")
        if len({item.failure_identity for item in facts}) != len(facts):
            raise ContractError("facts contain duplicate stable failure identities")
        if any(item.evidence_revision != self.evidence_revision for item in facts):
            raise ContractError("every fact must bind the batch evidence revision")
        if any(item.observed_at > self.collected_at for item in facts):
            raise ContractError("fact observation cannot follow batch collection")
        object.__setattr__(self, "facts", facts)
        complete = tuple(self.complete_detectors)
        if any(not isinstance(item, DetectorKind) for item in complete) or len(set(complete)) != len(complete):
            raise ContractError("complete_detectors must contain unique detector kinds")
        object.__setattr__(self, "complete_detectors", tuple(sorted(complete, key=lambda item: item.value)))
        if self.machine_id != MACHINE_ID or any(item.machine_id != MACHINE_ID for item in facts):
            raise ContractError(f"detector batches are restricted to machine {MACHINE_ID}")

    def to_document(self) -> dict[str, Any]:
        return {"schema_version": 1, "batch_id": self.batch_id,
                "evidence_revision": self.evidence_revision,
                "collected_at": utc_text(self.collected_at),
                "facts": [item.to_document() for item in self.facts],
                "complete_detectors": [item.value for item in self.complete_detectors],
                "machine_id": self.machine_id}

    @classmethod
    def from_document(cls, document: Any) -> "DetectorBatch":
        value = _fields(document, cls._FIELDS, "DetectorBatch")
        facts = tuple(DetectorFact.from_document(item) for item in _document_array(value["facts"], "facts"))
        try:
            complete = tuple(DetectorKind(item) for item in _document_array(
                value["complete_detectors"], "complete_detectors"))
        except (TypeError, ValueError) as error:
            raise ContractError("unknown complete detector") from error
        return cls(value["batch_id"], value["evidence_revision"],
                   parse_utc(value["collected_at"], "collected_at"), facts,
                   complete, value["machine_id"])


@dataclass(frozen=True)
class RepairEligibility:
    eligible: bool
    effect: Effect | None
    reasons: tuple[str, ...]


def derive_repair_eligibility(facts: tuple[DetectorFact, ...]) -> RepairEligibility:
    """Derive non-authoritative effect metadata from current detector facts."""
    if not facts:
        return RepairEligibility(False, None, ("no active fault facts",))
    reasons: list[str] = []
    if any(
        item.freshness is not FactFreshness.CURRENT or item.status is not FactStatus.FAULT
        for item in facts
    ):
        reasons.append("repair eligibility requires current fault facts")
    if any(item.ownership is not ResourceOwnership.OWNED_COMPONENT for item in facts):
        reasons.append("correlation includes a non-owned resource")
    if any(not item.routine_reversible for item in facts):
        reasons.append("correlation is not classified as routine and reversible")
    if reasons:
        return RepairEligibility(False, None, tuple(reasons))
    return RepairEligibility(
        True,
        Effect(
            effect_id="detector-derived-owned-component",
            description="routine reversible repair of detector-owned components",
            owned_component=True,
        ),
        ("candidate only; Phase 3 plan predicates still decide authority",),
    )


def authorize_correlation_plan(
    plan: Plan,
    correlator: "IncidentCorrelator",
    *,
    grants: tuple[StandingEffectGrant, ...] = (),
    policy_revision: str,
    now: datetime | None = None,
    classifier: TenantClassifier | None = None,
    rate_accountant: StandingRateAccountant | None = None,
) -> PlanDecision:
    """Route candidate repair authority through the existing Phase 3 predicates.

    Detector metadata can only remove standing grants from consideration.  It
    cannot authorize a plan, operation, or effect on its own.
    """
    if not isinstance(correlator, IncidentCorrelator):
        raise ContractError("authorization requires the durable incident correlator")
    effective_now = _utc(now if now is not None else correlator.clock(), "authorization time")
    facts = correlator.current_task_facts(plan.task_id, now=effective_now)
    task = correlator.tasks.get_task(plan.task_id)
    if task is None or plan.evidence_revision != task.evidence_revision:
        raise ContractError("plan does not bind the current correlation evidence revision")
    eligibility = derive_repair_eligibility(facts)
    return authorize_plan(
        plan, grants=grants if eligibility.eligible else (),
        policy_revision=policy_revision, now=effective_now, classifier=classifier,
        rate_accountant=rate_accountant,
    )


@dataclass(frozen=True)
class CorrelationOutcome:
    domain: CorrelationDomain
    status: str
    task_id: str | None
    created: bool
    material_changed: bool
    evidence_revision: str
    incident_ids: tuple[str, ...]
    linked_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrelationResult:
    enabled: bool
    replay: bool
    outcomes: tuple[CorrelationOutcome, ...]
    rejections: tuple["FactRejection", ...] = ()


@dataclass(frozen=True)
class FactRejection:
    fact_id: str
    reason: str


class IncidentCorrelator:
    """Persist facts and trigger machine-scoped tasks without model calls."""

    def __init__(
        self,
        state: StateStore,
        tasks: TaskStore,
        *,
        enabled: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.state = state
        self.tasks = tasks
        self.enabled = enabled is True
        self.clock = clock
        self._migrate()

    def _migrate(self) -> None:
        with self.tasks.transaction():
            self.tasks.db.execute(
                "CREATE TABLE IF NOT EXISTS tc_correlation_schema ("
                "namespace TEXT PRIMARY KEY CHECK(namespace='correlation'), version INTEGER NOT NULL)"
            )
            row = self.tasks.db.execute(
                "SELECT version FROM tc_correlation_schema WHERE namespace='correlation'"
            ).fetchone()
            version = 0 if row is None else int(row[0])
            if version > CORRELATION_SCHEMA_VERSION:
                raise RuntimeError(
                    f"correlation schema {version} is newer than supported schema "
                    f"{CORRELATION_SCHEMA_VERSION}"
                )
            if version == 0:
                statements = (
                    """CREATE TABLE tc_detector_batches (
                         batch_id TEXT PRIMARY KEY, batch_hash TEXT NOT NULL UNIQUE,
                         batch_json BLOB NOT NULL, recorded_utc TEXT NOT NULL)""",
                    """CREATE TABLE tc_detector_facts (
                         fact_id TEXT PRIMARY KEY, fact_hash TEXT NOT NULL UNIQUE,
                         fact_json BLOB NOT NULL, batch_id TEXT NOT NULL,
                         identity_key TEXT NOT NULL, domain TEXT NOT NULL,
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))""",
                    """CREATE TABLE tc_detector_heads (
                         identity_key TEXT PRIMARY KEY, fact_id TEXT NOT NULL UNIQUE,
                         kind TEXT NOT NULL, domain TEXT NOT NULL,
                         FOREIGN KEY(fact_id) REFERENCES tc_detector_facts(fact_id))""",
                    """CREATE TABLE tc_correlation_evaluations (
                         id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
                         domain TEXT NOT NULL, status TEXT NOT NULL,
                         task_created INTEGER NOT NULL CHECK(task_created IN (0,1)),
                         material_changed INTEGER NOT NULL CHECK(material_changed IN (0,1)),
                         material_digest TEXT, evidence_revision TEXT NOT NULL,
                         task_id TEXT, fact_ids_json BLOB NOT NULL,
                         incident_ids_json BLOB NOT NULL,
                         UNIQUE(batch_id,domain),
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id),
                         FOREIGN KEY(task_id) REFERENCES tc_tasks(task_id))""",
                    """CREATE TABLE tc_correlation_task_links (
                         task_a TEXT NOT NULL, task_b TEXT NOT NULL, reason TEXT NOT NULL,
                         batch_id TEXT NOT NULL, PRIMARY KEY(task_a,task_b,reason),
                         CHECK(task_a < task_b),
                         FOREIGN KEY(task_a) REFERENCES tc_tasks(task_id),
                         FOREIGN KEY(task_b) REFERENCES tc_tasks(task_id),
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))""",
                    "CREATE INDEX tc_detector_heads_domain ON tc_detector_heads(domain,identity_key)",
                    "CREATE INDEX tc_correlation_eval_domain ON tc_correlation_evaluations(domain,id)",
                )
                for statement in statements:
                    self.tasks.db.execute(statement)
                for table in ("tc_detector_batches", "tc_detector_facts",
                              "tc_correlation_evaluations", "tc_correlation_task_links"):
                    self.tasks.db.execute(
                        f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable correlation record'); END"
                    )
                    self.tasks.db.execute(
                        f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable correlation record'); END"
                    )
                self.tasks.db.execute(
                    "INSERT INTO tc_correlation_schema(namespace,version) VALUES('correlation',1)"
                )
                version = 1
            if version == 1:
                self.tasks.db.execute(
                    """CREATE TABLE tc_detector_observations (
                         identity_key TEXT PRIMARY KEY, fact_id TEXT NOT NULL UNIQUE,
                         kind TEXT NOT NULL, domain TEXT NOT NULL, status TEXT NOT NULL,
                         source_instance TEXT NOT NULL, observed_utc TEXT NOT NULL,
                         collected_utc TEXT NOT NULL, batch_id TEXT NOT NULL,
                         FOREIGN KEY(fact_id) REFERENCES tc_detector_facts(fact_id),
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))"""
                )
                self.tasks.db.execute(
                    """CREATE TABLE tc_detector_acceptances (
                         fact_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
                         source_instance TEXT NOT NULL, batch_id TEXT NOT NULL,
                         FOREIGN KEY(fact_id) REFERENCES tc_detector_facts(fact_id),
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))"""
                )
                self.tasks.db.execute(
                    """CREATE TABLE tc_detector_complete_watermarks (
                         kind TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                         collected_utc TEXT NOT NULL,
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))"""
                )
                self.tasks.db.execute(
                    """CREATE TABLE tc_detector_fact_rejections (
                         batch_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                         fact_hash TEXT NOT NULL, fact_json BLOB NOT NULL,
                         reason TEXT NOT NULL, recorded_utc TEXT NOT NULL,
                         PRIMARY KEY(batch_id,fact_id),
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))"""
                )
                self.tasks.db.execute(
                    """CREATE TABLE tc_state_projections (
                         projection_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                         operation_hash TEXT NOT NULL, operation_json BLOB NOT NULL,
                         status TEXT NOT NULL CHECK(status IN ('pending','applied','failed')),
                         attempts INTEGER NOT NULL DEFAULT 0,
                         last_error TEXT, updated_utc TEXT NOT NULL,
                         FOREIGN KEY(batch_id) REFERENCES tc_detector_batches(batch_id))"""
                )
                self.tasks.db.execute(
                    "CREATE INDEX tc_state_projections_status ON tc_state_projections(status,batch_id)"
                )
                self.tasks.db.execute(
                    """CREATE TRIGGER tc_state_projections_operation_immutable
                       BEFORE UPDATE ON tc_state_projections WHEN
                         NEW.projection_id!=OLD.projection_id OR NEW.batch_id!=OLD.batch_id OR
                         NEW.operation_hash!=OLD.operation_hash OR
                         NEW.operation_json!=OLD.operation_json
                       BEGIN SELECT RAISE(ABORT, 'immutable correlation projection'); END"""
                )
                self.tasks.db.execute(
                    """CREATE TRIGGER tc_state_projections_no_delete
                       BEFORE DELETE ON tc_state_projections
                       BEGIN SELECT RAISE(ABORT, 'immutable correlation projection'); END"""
                )
                for table in ("tc_detector_acceptances", "tc_detector_fact_rejections"):
                    self.tasks.db.execute(
                        f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable correlation record'); END"
                    )
                    self.tasks.db.execute(
                        f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable correlation record'); END"
                    )
                # A v1 database knows only active fault heads.  Seed accepted
                # observations from them without inventing healthy/unknown history.
                rows = self.tasks.db.execute(
                    """SELECT h.identity_key,h.kind,h.domain,h.fact_id,f.fact_json,
                              f.batch_id,b.batch_json
                       FROM tc_detector_heads h
                       JOIN tc_detector_facts f ON f.fact_id=h.fact_id
                       JOIN tc_detector_batches b ON b.batch_id=f.batch_id"""
                ).fetchall()
                for old in rows:
                    fact = DetectorFact.from_json(old["fact_json"])
                    old_batch = DetectorBatch.from_json(old["batch_json"])
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_observations(
                             identity_key,fact_id,kind,domain,status,source_instance,
                             observed_utc,collected_utc,batch_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (old["identity_key"], old["fact_id"], old["kind"], old["domain"],
                         FactStatus.FAULT.value, fact.source_instance,
                         utc_text(fact.observed_at), utc_text(old_batch.collected_at),
                         old["batch_id"]),
                    )
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_acceptances(
                             fact_id,identity_key,source_instance,batch_id) VALUES(?,?,?,?)""",
                        (old["fact_id"], old["identity_key"], fact.source_instance,
                         old["batch_id"]),
                    )
                self.tasks.db.execute(
                    "UPDATE tc_correlation_schema SET version=2 WHERE namespace='correlation'"
                )

    def _existing_batch(self, batch: DetectorBatch) -> bool:
        row = self.tasks.db.execute(
            "SELECT batch_hash,batch_json FROM tc_detector_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
        if row is None:
            return False
        raw = bytes(row["batch_json"]) if not isinstance(row["batch_json"], str) else row["batch_json"].encode()
        if raw != batch.canonical_json() or row["batch_hash"] != batch.content_hash:
            raise TaskConflict("detector batch identifier was replayed with different content")
        return True

    @staticmethod
    def _fact_projection(fact: DetectorFact, batch: DetectorBatch) -> dict[str, Any]:
        evidence = fact.to_document()
        digest, _ = evidence_digest(evidence)
        incident = None
        if fact.status is FactStatus.FAULT:
            incident = {
                "dedup_key": fact.incident_id,
                "target": TARGET,
                "machine_id": MACHINE_ID,
                "fault_family": fact.kind.value,
                "stable_signature": fact.failure_identity,
            }
        return {
            "kind": "fact", "fact_id": fact.fact_id,
            "observation": {
                "target": TARGET, "machine_id": MACHINE_ID, "source": fact.source,
                "source_event_id": fact.fact_id,
                "delivery_key": delivery_identity(TARGET, fact.source, fact.fact_id),
                # Correlation orders source-instance transitions by collection.
                # Project the same ordering clock so StateStore cannot disagree.
                "source_utc": utc_text(batch.collected_at),
                "receipt_utc": utc_text(batch.collected_at),
                "boot_id": fact.source_instance,
                "status": {
                    FactStatus.FAULT: "unhealthy", FactStatus.HEALTHY: "healthy",
                    FactStatus.UNKNOWN: "unknown",
                }[fact.status],
                "freshness": "fresh", "evidence_sha256": digest,
            },
            "incident": incident, "evidence": evidence,
            "notification": (f"{fact.kind.value} fault on {fact.resource_id}: {fact.summary}"
                             if incident is not None else ""),
            "severity": fact.severity.value,
            "apply_healthy_recovery": fact.status is FactStatus.HEALTHY,
        }

    @staticmethod
    def _absence_projection(
        fact: DetectorFact, batch: DetectorBatch,
    ) -> dict[str, Any]:
        evidence = {
            "schema_version": 1, "kind": "complete-detector-absence",
            "batch_id": batch.batch_id, "evidence_revision": batch.evidence_revision,
            "detector": fact.kind.value, "identity_key": fact.failure_identity,
            "machine_id": MACHINE_ID,
        }
        digest, _ = evidence_digest(evidence)
        event_id = f"absent:{batch.batch_id}:{fact.failure_identity}"
        return {
            "kind": "absence", "fact_id": fact.fact_id,
            "observation": {
                "target": TARGET, "machine_id": MACHINE_ID, "source": fact.source,
                "source_event_id": event_id,
                "delivery_key": delivery_identity(TARGET, fact.source, event_id),
                "source_utc": utc_text(batch.collected_at),
                "receipt_utc": utc_text(batch.collected_at),
                "boot_id": "phase6-correlator", "status": "healthy",
                "freshness": "fresh", "evidence_sha256": digest,
            },
            "incident": None, "evidence": evidence, "notification": "",
            "severity": "warning", "apply_healthy_recovery": True,
        }

    def _queue_projection(self, batch: DetectorBatch, operation: Mapping[str, Any]) -> None:
        projection_id = stable_hash({"batch_id": batch.batch_id, "operation": operation})
        raw = json.dumps(operation, sort_keys=True, separators=(",", ":")).encode()
        self.tasks.db.execute(
            """INSERT OR IGNORE INTO tc_state_projections(
                 projection_id,batch_id,operation_hash,operation_json,status,updated_utc)
               VALUES(?,?,?,?, 'pending',?)""",
            (projection_id, batch.batch_id, stable_hash(operation), raw, utc_text(self.clock())),
        )

    def _apply_projection(self, operation: Mapping[str, Any]) -> None:
        self.state.record_observation(
            dict(operation["observation"]),
            incident=operation["incident"], evidence=operation["evidence"],
            notification=str(operation["notification"]),
            severity=str(operation["severity"]),
            apply_healthy_recovery=bool(operation["apply_healthy_recovery"]),
        )

    def _projection_is_relevant(self, operation: Mapping[str, Any]) -> bool:
        """Suppress a delayed projection that authoritative truth has superseded."""
        fact_id = str(operation["fact_id"])
        accepted = self.tasks.db.execute(
            "SELECT identity_key FROM tc_detector_acceptances WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if accepted is None:
            return False
        identity = accepted["identity_key"]
        observation = self._observation_row(identity)
        if observation is None:
            return False
        if operation["kind"] == "absence":
            evidence = operation["evidence"]
            return (
                observation["status"] == FactStatus.HEALTHY.value
                and observation["batch_id"] == evidence["batch_id"]
            )
        status = operation["observation"]["status"]
        if status == "unhealthy":
            # UNKNOWN advances the accepted observation but intentionally leaves
            # the preceding fault authoritative.
            head = self.tasks.db.execute(
                "SELECT fact_id FROM tc_detector_heads WHERE identity_key=?", (identity,)
            ).fetchone()
            return head is not None and head["fact_id"] == fact_id
        return observation["fact_id"] == fact_id

    def replay_state_projections(self, batch_id: str | None = None) -> int:
        """Retry unapplied StateStore projections without changing task truth."""
        parameters: tuple[Any, ...] = () if batch_id is None else (batch_id,)
        where = "status!='applied'" if batch_id is None else "batch_id=? AND status!='applied'"
        rows = self.tasks.db.execute(
            f"SELECT * FROM tc_state_projections WHERE {where} ORDER BY rowid", parameters
        ).fetchall()
        applied = 0
        for row in rows:
            try:
                operation = json.loads(row["operation_json"])
                if stable_hash(operation) != row["operation_hash"]:
                    raise RuntimeError("stored StateStore projection hash mismatch")
                if self._projection_is_relevant(operation):
                    self._apply_projection(operation)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"[:1000]
                with self.tasks.transaction():
                    self.tasks.db.execute(
                        """UPDATE tc_state_projections SET status='failed',attempts=attempts+1,
                             last_error=?,updated_utc=? WHERE projection_id=?""",
                        (message, utc_text(self.clock()), row["projection_id"]),
                    )
                continue
            with self.tasks.transaction():
                self.tasks.db.execute(
                    """UPDATE tc_state_projections SET status='applied',attempts=attempts+1,
                         last_error=NULL,updated_utc=? WHERE projection_id=?""",
                    (utc_text(self.clock()), row["projection_id"]),
                )
            applied += 1
        return applied

    def projection_statuses(self, batch_id: str | None = None) -> tuple[dict[str, Any], ...]:
        parameters: tuple[Any, ...] = () if batch_id is None else (batch_id,)
        where = "1=1" if batch_id is None else "batch_id=?"
        rows = self.tasks.db.execute(
            f"""SELECT projection_id,batch_id,status,attempts,last_error
                FROM tc_state_projections WHERE {where} ORDER BY rowid""", parameters
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def _active_facts(self) -> dict[CorrelationDomain, tuple[DetectorFact, ...]]:
        rows = self.tasks.db.execute(
            """SELECT f.fact_json FROM tc_detector_heads h
               JOIN tc_detector_facts f ON f.fact_id=h.fact_id
               ORDER BY h.domain,h.identity_key"""
        ).fetchall()
        result: dict[CorrelationDomain, list[DetectorFact]] = {}
        for row in rows:
            fact = DetectorFact.from_json(row[0])
            result.setdefault(fact.domain, []).append(fact)
        return {domain: tuple(facts) for domain, facts in result.items()}

    def _head_fact(self, identity: str) -> DetectorFact | None:
        row = self.tasks.db.execute(
            """SELECT f.fact_json FROM tc_detector_heads h
               JOIN tc_detector_facts f ON f.fact_id=h.fact_id
               WHERE h.identity_key=?""",
            (identity,),
        ).fetchone()
        return None if row is None else DetectorFact.from_json(row[0])

    def _observation_row(self, identity: str) -> sqlite3.Row | None:
        return self.tasks.db.execute(
            "SELECT * FROM tc_detector_observations WHERE identity_key=?", (identity,)
        ).fetchone()

    def _acceptance_reason(self, fact: DetectorFact, batch: DetectorBatch) -> str | None:
        """Return a durable rejection reason, or None when current truth may advance."""
        prior_row = self._observation_row(fact.failure_identity)
        if prior_row is None:
            return None
        prior = self._head_or_observation_fact(fact.failure_identity)
        assert prior is not None
        prior_collected = parse_utc(prior_row["collected_utc"], "collected_utc")
        if batch.collected_at < prior_collected:
            return "batch collection precedes the last accepted observation"
        if fact.source_instance != prior_row["source_instance"]:
            seen = self.tasks.db.execute(
                """SELECT 1 FROM tc_detector_acceptances
                   WHERE identity_key=? AND source_instance=? AND fact_id!=? LIMIT 1""",
                (fact.failure_identity, fact.source_instance, fact.fact_id),
            ).fetchone()
            if seen is not None:
                return "retired source instance cannot become current again"
            if batch.collected_at <= prior_collected:
                return "source instance change does not follow the accepted batch"
            return None
        prior_observed = parse_utc(prior_row["observed_utc"], "observed_utc")
        if fact.observed_at < prior_observed:
            return "observation precedes the last accepted observation"
        if fact.observed_at == prior_observed and (
            fact.status.value != prior_row["status"]
            or fact.domain.value != prior_row["domain"]
            or fact.material_document() != prior.material_document()
        ):
            return "equal-time detector fact conflicts with accepted truth"
        return None

    def _head_or_observation_fact(self, identity: str) -> DetectorFact | None:
        row = self.tasks.db.execute(
            """SELECT f.fact_json FROM tc_detector_observations o
               JOIN tc_detector_facts f ON f.fact_id=o.fact_id WHERE o.identity_key=?""",
            (identity,),
        ).fetchone()
        return None if row is None else DetectorFact.from_json(row[0])

    def _reject_fact(self, batch: DetectorBatch, fact: DetectorFact, reason: str) -> None:
        self.tasks.db.execute(
            """INSERT INTO tc_detector_fact_rejections(
                 batch_id,fact_id,fact_hash,fact_json,reason,recorded_utc)
               VALUES(?,?,?,?,?,?)""",
            (batch.batch_id, fact.fact_id, fact.content_hash, fact.canonical_json(),
             reason, utc_text(self.clock())),
        )

    @staticmethod
    def _correlation_revision(facts: tuple[DetectorFact, ...], batch_revision: str) -> str:
        return stable_hash({"batch_revision": batch_revision,
                            "fact_revisions": sorted(item.evidence_revision for item in facts),
                            "fact_hashes": sorted(item.content_hash for item in facts)})

    @staticmethod
    def _material_digest(facts: tuple[DetectorFact, ...]) -> str:
        return stable_hash(sorted((dict(item.material_document()) for item in facts),
                                  key=lambda item: (item["kind"], item["resource_id"], item["failure_id"])))

    def _latest(self, domain: CorrelationDomain) -> sqlite3.Row | None:
        return self.tasks.db.execute(
            "SELECT * FROM tc_correlation_evaluations WHERE domain=? ORDER BY id DESC LIMIT 1",
            (domain.value,),
        ).fetchone()

    def _event_payload(self, facts: tuple[DetectorFact, ...], material: str) -> dict[str, Any]:
        eligibility = derive_repair_eligibility(facts)
        derived = None
        if eligibility.effect is not None:
            derived = {
                "effect_id": eligibility.effect.effect_id,
                "description": eligibility.effect.description,
                "owned_component": eligibility.effect.owned_component,
            }
        return {
            "machine_id": MACHINE_ID, "material_digest": material,
            "fact_ids": [item.fact_id for item in facts],
            "evidence_revisions": sorted({item.evidence_revision for item in facts}),
            "incident_ids": sorted(item.incident_id for item in facts),
            "repair_eligibility": {
                "eligible": eligibility.eligible,
                "derived_effect": derived,
                "reasons": list(eligibility.reasons),
                "authorizes_execution": False,
            },
        }

    def _new_task(
        self, batch: DetectorBatch, domain: CorrelationDomain,
        facts: tuple[DetectorFact, ...], material: str, evidence_revision: str,
        previous_task: str | None,
    ) -> str:
        identity = stable_hash([batch.batch_id, domain.value, material])
        task_id = f"detector-{identity[:40]}"
        incidents = tuple(sorted(item.incident_id for item in facts))
        summaries = "; ".join(item.summary for item in facts)
        objective = f"Investigate {domain.value} faults on machine {MACHINE_ID}: {summaries}"
        if len(objective) > 4096:
            objective = objective[:4095] + "…"
        task = Task(
            task_id=task_id, requester_id="phase6-detector", requester_group_id=0,
            origin_message_id=batch.batch_id, created_at=self.clock(), objective=objective,
            constraints=("detector-triggered", "no mutation authority", "machine:17049"),
            state=TaskState.INVESTIGATING, evidence_revision=evidence_revision,
            model_thread=None, attempt_count=0, deadline=None,
            budgets={"detector_facts": len(facts)}, incident_ids=incidents,
        )
        payload = self._event_payload(facts, material)
        if previous_task is not None:
            payload["previous_task_id"] = previous_task
            payload["change"] = "material"
        event = TaskEvent(
            event_id=f"correlation-created:{identity}", task_id=task_id, sequence=1,
            event_type="correlation-created", actor_id="phase6-correlator",
            occurred_at=task.created_at, from_state=None, to_state=TaskState.INVESTIGATING,
            payload=payload, evidence_revision=evidence_revision,
            incident_id=incidents[0] if len(incidents) == 1 else None,
        )
        self.tasks.create_task(task, event)
        return task_id

    def _append_event(
        self, task_id: str, batch: DetectorBatch, domain: CorrelationDomain,
        event_type: str, facts: tuple[DetectorFact, ...], material: str,
        evidence_revision: str,
    ) -> None:
        task = self.tasks.get_task(task_id)
        if task is None:
            raise TaskConflict("correlation references a missing task")
        sequence = len(self.tasks.events(task_id)) + 1
        self.tasks.append_event(TaskEvent(
            event_id=f"{event_type}:{batch.batch_id}:{domain.value}", task_id=task_id,
            sequence=sequence, event_type=event_type, actor_id="phase6-correlator",
            occurred_at=self.clock(), from_state=task.state, to_state=task.state,
            payload=self._event_payload(facts, material),
            evidence_revision=evidence_revision,
        ))

    def process(self, batch: DetectorBatch) -> CorrelationResult:
        if not isinstance(batch, DetectorBatch):
            raise ContractError("process requires a DetectorBatch")
        if not self.enabled:
            return CorrelationResult(False, False, ())
        now = _utc(self.clock(), "clock")
        if batch.collected_at > now or any(item.observed_at > now for item in batch.facts):
            raise ContractError("detector evidence is future-dated")
        if self._existing_batch(batch):
            self.replay_state_projections(batch.batch_id)
            return CorrelationResult(
                True, True, self._outcomes_for_batch(batch.batch_id),
                self.rejections_for_batch(batch.batch_id),
            )

        outcomes: list[CorrelationOutcome] = []
        with self.tasks.transaction():
            if self._existing_batch(batch):
                replay = True
            else:
                replay = False
                self.tasks.db.execute(
                    "INSERT INTO tc_detector_batches(batch_id,batch_hash,batch_json,recorded_utc) VALUES(?,?,?,?)",
                    (batch.batch_id, batch.content_hash, batch.canonical_json(), utc_text(now)),
                )
            if replay:
                # The enclosing transaction must finish before cross-database
                # projections are retried.
                pass
            else:
                recorded: list[DetectorFact] = []
                rejected_ids: set[str] = set()
                for fact in batch.facts:
                    by_id = self.tasks.db.execute(
                        "SELECT fact_hash,fact_json FROM tc_detector_facts WHERE fact_id=?",
                        (fact.fact_id,),
                    ).fetchone()
                    by_hash = self.tasks.db.execute(
                        "SELECT fact_id FROM tc_detector_facts WHERE fact_hash=?",
                        (fact.content_hash,),
                    ).fetchone()
                    if by_id is not None or by_hash is not None:
                        self._reject_fact(
                            batch, fact,
                            "fact identifier or content was already recorded in another batch",
                        )
                        rejected_ids.add(fact.fact_id)
                        continue
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_facts(
                             fact_id,fact_hash,fact_json,batch_id,identity_key,domain)
                           VALUES(?,?,?,?,?,?)""",
                        (fact.fact_id, fact.content_hash, fact.canonical_json(), batch.batch_id,
                         fact.failure_identity, fact.domain.value),
                    )
                    recorded.append(fact)

                affected_domains: set[CorrelationDomain] = set()
                accepted_ids: set[str] = set()
                for fact in recorded:
                    if fact.freshness is not FactFreshness.CURRENT:
                        continue
                    prior_observation = self._head_or_observation_fact(fact.failure_identity)
                    prior_head = self._head_fact(fact.failure_identity)
                    reason = self._acceptance_reason(fact, batch)
                    if reason is not None:
                        self._reject_fact(batch, fact, reason)
                        rejected_ids.add(fact.fact_id)
                        continue
                    accepted_ids.add(fact.fact_id)
                    if prior_observation is not None:
                        affected_domains.add(prior_observation.domain)
                    if prior_head is not None:
                        affected_domains.add(prior_head.domain)
                    if prior_observation is not None or fact.status is FactStatus.FAULT:
                        affected_domains.add(fact.domain)
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_observations(
                             identity_key,fact_id,kind,domain,status,source_instance,
                             observed_utc,collected_utc,batch_id) VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(identity_key) DO UPDATE SET
                             fact_id=excluded.fact_id,kind=excluded.kind,domain=excluded.domain,
                             status=excluded.status,source_instance=excluded.source_instance,
                             observed_utc=excluded.observed_utc,collected_utc=excluded.collected_utc,
                             batch_id=excluded.batch_id""",
                        (fact.failure_identity, fact.fact_id, fact.kind.value,
                         fact.domain.value, fact.status.value, fact.source_instance,
                         utc_text(fact.observed_at), utc_text(batch.collected_at), batch.batch_id),
                    )
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_acceptances(
                             fact_id,identity_key,source_instance,batch_id) VALUES(?,?,?,?)""",
                        (fact.fact_id, fact.failure_identity, fact.source_instance, batch.batch_id),
                    )
                    if fact.status is FactStatus.FAULT:
                        self.tasks.db.execute(
                            """INSERT INTO tc_detector_heads(identity_key,fact_id,kind,domain)
                               VALUES(?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET
                               fact_id=excluded.fact_id,kind=excluded.kind,domain=excluded.domain""",
                            (fact.failure_identity, fact.fact_id, fact.kind.value, fact.domain.value),
                        )
                    elif fact.status is FactStatus.HEALTHY:
                        self.tasks.db.execute(
                            "DELETE FROM tc_detector_heads WHERE identity_key=?",
                            (fact.failure_identity,),
                        )
                    # CURRENT UNKNOWN advances the anti-resurrection observation
                    # watermark but deliberately preserves an existing fault head.
                    self._queue_projection(batch, self._fact_projection(fact, batch))

                for kind in batch.complete_detectors:
                    kind_facts = [item for item in batch.facts if item.kind is kind]
                    conclusive = all(
                        item.freshness is FactFreshness.CURRENT
                        and item.status is not FactStatus.UNKNOWN
                        and item.fact_id in accepted_ids
                        for item in kind_facts
                    ) and not any(item.fact_id in rejected_ids for item in kind_facts)
                    if not conclusive:
                        continue
                    watermark = self.tasks.db.execute(
                        "SELECT collected_utc FROM tc_detector_complete_watermarks WHERE kind=?",
                        (kind.value,),
                    ).fetchone()
                    if watermark is not None and batch.collected_at <= parse_utc(
                        watermark["collected_utc"], "complete watermark"
                    ):
                        continue
                    present = {item.failure_identity for item in kind_facts}
                    rows = self.tasks.db.execute(
                        """SELECT o.identity_key,o.domain,o.collected_utc,o.fact_id,
                                  f.fact_json,h.fact_id AS head_fact_id
                           FROM tc_detector_observations o
                           JOIN tc_detector_facts f ON f.fact_id=o.fact_id
                           LEFT JOIN tc_detector_heads h ON h.identity_key=o.identity_key
                           WHERE o.kind=?""",
                        (kind.value,),
                    ).fetchall()
                    for row in rows:
                        if row["identity_key"] in present:
                            continue
                        if parse_utc(row["collected_utc"], "observation collection") >= batch.collected_at:
                            continue
                        prior = DetectorFact.from_json(row["fact_json"])
                        if row["head_fact_id"] is not None:
                            self.tasks.db.execute(
                                "DELETE FROM tc_detector_heads WHERE identity_key=?",
                                (row["identity_key"],),
                            )
                            affected_domains.add(CorrelationDomain(row["domain"]))
                        self.tasks.db.execute(
                            """UPDATE tc_detector_observations SET status='healthy',
                                 observed_utc=?,collected_utc=?,batch_id=?
                               WHERE identity_key=?""",
                            (utc_text(batch.collected_at), utc_text(batch.collected_at),
                             batch.batch_id, row["identity_key"]),
                        )
                        self._queue_projection(batch, self._absence_projection(prior, batch))
                    self.tasks.db.execute(
                        """INSERT INTO tc_detector_complete_watermarks(kind,batch_id,collected_utc)
                           VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET
                           batch_id=excluded.batch_id,collected_utc=excluded.collected_utc""",
                        (kind.value, batch.batch_id, utc_text(batch.collected_at)),
                    )

                active = self._active_facts()
                for domain in sorted(affected_domains, key=lambda item: item.value):
                    facts = active.get(domain, ())
                    latest = self._latest(domain)
                    previous_active = latest is not None and latest["status"] == "active"
                    previous_task = str(latest["task_id"]) if previous_active else None
                    if facts:
                        material = self._material_digest(facts)
                        revision = self._correlation_revision(facts, batch.evidence_revision)
                        unchanged = bool(previous_active and latest["material_digest"] == material)
                        previous_snapshot = (
                            self.tasks.get_task(previous_task) if previous_task is not None else None
                        )
                        replace_terminal = bool(
                            unchanged and previous_snapshot is not None
                            and previous_snapshot.state in TERMINAL_STATES
                        )
                        if unchanged and not replace_terminal:
                            task_id = str(previous_task)
                            if latest["evidence_revision"] != revision:
                                self._append_event(task_id, batch, domain, "correlation-evidence",
                                                   facts, material, revision)
                            created = False
                            material_changed = False
                        else:
                            task_id = self._new_task(
                                batch, domain, facts, material, revision, previous_task,
                            )
                            created = True
                            material_changed = bool(previous_active and not replace_terminal)
                            if previous_task:
                                pair = tuple(sorted((previous_task, task_id)))
                                self.tasks.db.execute(
                                    """INSERT OR IGNORE INTO tc_correlation_task_links(
                                         task_a,task_b,reason,batch_id) VALUES(?,?,?,?)""",
                                    (*pair, ("terminal-replacement" if replace_terminal
                                             else "material-change"), batch.batch_id),
                                )
                        incidents = tuple(sorted(item.incident_id for item in facts))
                        fact_ids = tuple(sorted(item.fact_id for item in facts))
                        status = "active"
                    else:
                        material = None
                        revision = stable_hash({"batch_revision": batch.evidence_revision,
                                                "domain": domain.value, "status": "clear"})
                        task_id = previous_task
                        created = material_changed = False
                        incidents = fact_ids = ()
                        status = "recovered" if previous_active else "quiet"
                        if previous_active and task_id:
                            prior_task = self.tasks.get_task(task_id)
                            if prior_task is not None and prior_task.state not in TERMINAL_STATES:
                                self._append_event(task_id, batch, domain, "correlation-recovered",
                                                   (), "clear", revision)
                    self.tasks.db.execute(
                        """INSERT INTO tc_correlation_evaluations(
                             batch_id,domain,status,task_created,material_changed,material_digest,
                             evidence_revision,task_id,fact_ids_json,incident_ids_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (batch.batch_id, domain.value, status, int(created), int(material_changed),
                         material, revision, task_id,
                         json.dumps(fact_ids, separators=(",", ":")),
                         json.dumps(incidents, separators=(",", ":"))),
                    )
                    outcomes.append(CorrelationOutcome(
                        domain, status, task_id, created, material_changed, revision, incidents,
                    ))

                self._link_related_tasks(batch, active)

        self.replay_state_projections(batch.batch_id)
        final = self._outcomes_for_batch(batch.batch_id)
        return CorrelationResult(
            True, replay, final, self.rejections_for_batch(batch.batch_id),
        )

    def _link_related_tasks(
        self, batch: DetectorBatch,
        active: Mapping[CorrelationDomain, tuple[DetectorFact, ...]],
    ) -> None:
        by_domain: dict[CorrelationDomain, str] = {}
        for domain in active:
            latest = self._latest(domain)
            if latest is None or latest["status"] != "active" or latest["task_id"] is None:
                continue
            task_id = str(latest["task_id"])
            task = self.tasks.get_task(task_id)
            if task is not None and task.state not in TERMINAL_STATES:
                by_domain[domain] = task_id
        domains = sorted(by_domain, key=lambda item: item.value)
        for index, left in enumerate(domains):
            left_resources = {resource for fact in active.get(left, ())
                              for resource in (fact.resource_id, *fact.related_resources)}
            for right in domains[index + 1:]:
                right_resources = {resource for fact in active.get(right, ())
                                   for resource in (fact.resource_id, *fact.related_resources)}
                if not left_resources.intersection(right_resources):
                    continue
                pair = tuple(sorted((str(by_domain[left]), str(by_domain[right]))))
                if pair[0] == pair[1]:
                    continue
                self.tasks.db.execute(
                    """INSERT OR IGNORE INTO tc_correlation_task_links(
                         task_a,task_b,reason,batch_id) VALUES(?,?,?,?)""",
                    (*pair, "shared-resource", batch.batch_id),
                )

    def linked_tasks(self, task_id: str) -> tuple[str, ...]:
        _identifier(task_id, "task_id")
        rows = self.tasks.db.execute(
            """SELECT task_a,task_b FROM tc_correlation_task_links
               WHERE task_a=? OR task_b=? ORDER BY task_a,task_b""", (task_id, task_id)
        ).fetchall()
        return tuple(row["task_b"] if row["task_a"] == task_id else row["task_a"] for row in rows)

    def current_task_facts(
        self, task_id: str, *, now: datetime | None = None,
    ) -> tuple[DetectorFact, ...]:
        """Return facts only when task, latest evaluation and durable heads agree."""
        _identifier(task_id, "task_id")
        task = self.tasks.get_task(task_id)
        if task is None:
            raise ContractError("correlation authorization references an unknown task")
        if task.state in TERMINAL_STATES:
            raise ContractError("terminal correlation tasks cannot authorize repair")
        rows = self.tasks.db.execute(
            """SELECT e.* FROM tc_correlation_evaluations e
               WHERE e.task_id=? AND e.status='active' ORDER BY e.id DESC""",
            (task_id,),
        ).fetchall()
        if not rows:
            raise ContractError("task is not backed by an active correlation")
        row = rows[0]
        latest = self._latest(CorrelationDomain(row["domain"]))
        if latest is None or int(latest["id"]) != int(row["id"]):
            raise ContractError("task is no longer the current task for its domain")
        checked_at = _utc(now if now is not None else self.clock(), "fact freshness time")
        fact_ids = tuple(json.loads(row["fact_ids_json"]))
        facts: list[DetectorFact] = []
        for fact_id in fact_ids:
            head = self.tasks.db.execute(
                """SELECT f.fact_json FROM tc_detector_heads h
                   JOIN tc_detector_facts f ON f.fact_id=h.fact_id WHERE h.fact_id=?""",
                (fact_id,),
            ).fetchone()
            if head is None:
                raise ContractError("task facts do not match current durable heads")
            fact = DetectorFact.from_json(head["fact_json"])
            if fact.status is not FactStatus.FAULT or fact.freshness is not FactFreshness.CURRENT:
                raise ContractError("task is not backed by current fault facts")
            observation = self._observation_row(fact.failure_identity)
            if (
                observation is None
                or observation["fact_id"] != fact_id
                or observation["status"] != FactStatus.FAULT.value
            ):
                raise ContractError("a newer non-fault observation blocks repair authority")
            collected_at = parse_utc(observation["collected_utc"], "fact collection time")
            age = checked_at - collected_at
            if age < timedelta(0) or age > timedelta(seconds=MAX_REPAIR_EVIDENCE_AGE_SECONDS):
                raise ContractError("correlation evidence is outside the repair freshness bound")
            facts.append(fact)
        current_ids = {
            row["fact_id"] for row in self.tasks.db.execute(
                "SELECT fact_id FROM tc_detector_heads WHERE domain=?", (row["domain"],)
            )
        }
        if current_ids != set(fact_ids):
            raise ContractError("task fact set does not match current durable heads")
        if tuple(sorted(task.incident_ids)) != tuple(sorted(fact.incident_id for fact in facts)):
            raise ContractError("task incident links do not match current durable heads")
        return tuple(facts)

    def rejections_for_batch(self, batch_id: str) -> tuple[FactRejection, ...]:
        rows = self.tasks.db.execute(
            """SELECT fact_id,reason FROM tc_detector_fact_rejections
               WHERE batch_id=? ORDER BY fact_id""", (batch_id,)
        ).fetchall()
        return tuple(FactRejection(row["fact_id"], row["reason"]) for row in rows)

    def _outcomes_for_batch(self, batch_id: str) -> tuple[CorrelationOutcome, ...]:
        rows = self.tasks.db.execute(
            "SELECT * FROM tc_correlation_evaluations WHERE batch_id=? ORDER BY domain",
            (batch_id,),
        ).fetchall()
        outcomes = []
        for row in rows:
            task_id = row["task_id"]
            linked = self.linked_tasks(task_id) if task_id else ()
            outcomes.append(CorrelationOutcome(
                domain=CorrelationDomain(row["domain"]), status=row["status"], task_id=task_id,
                created=bool(row["task_created"]),
                material_changed=bool(row["material_changed"]),
                evidence_revision=row["evidence_revision"],
                incident_ids=tuple(json.loads(row["incident_ids_json"])),
                linked_task_ids=linked,
            ))
        return tuple(outcomes)
