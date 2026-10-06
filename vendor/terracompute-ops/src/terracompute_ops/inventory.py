"""Append-only hardware identity and relationship history for machine 17049.

The inventory deliberately owns namespaced tables in a caller supplied SQLite
connection.  It does not alter SQLite's global ``user_version`` or connection
PRAGMAs; those remain the responsibility of the state-store owner.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Iterable, Mapping, Sequence


MACHINE_ID = "17049"
DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 256
HARD_MAX_DEPTH = 32
HARD_MAX_NODES = 2_048
MAX_PROBE_PAYLOAD_BYTES = 512 * 1024

# These names make the expected physical model discoverable without making the
# store unable to represent a newly observed component or relationship.
COMPONENT_KINDS = frozenset(
    {
        "machine",
        "gpu",
        "riser",
        "slimsas_cable",
        "motherboard",
        "pcie_root",
        "cpu",
        "numa_domain",
        "power_lead",
        "adapter_leg",
        "breakout_output",
        "breakout_board",
        "gpu_psu",
        "atx_psu",
        "control_signal",
        "ac_lead",
        "outlet",
        "pdu",
        "circuit",
        "fan",
        "cooling_zone",
        "nic",
        "network_cable",
        "switch_port",
        "unknown",
    }
)
RELATIONSHIP_KINDS = frozenset(
    {
        "data_path",
        "power_path",
        "control_path",
        "ac_path",
        "cooling_path",
        "network_path",
        "mounted_on",
        "topology",
        "depends_on",
    }
)
IDENTITY_ALIAS_KINDS = frozenset({"serial", "uuid"})
LOCATION_ATTRIBUTE_KINDS = frozenset(
    {"pci_bdf", "linux_index", "physical_position", "numa_node", "root_port"}
)
REMOTE_AGENT_OWNED_FIELDS = frozenset(
    {
        "gpu_model",
        "gpu_uuid",
        "exported_serial",
        "pci_bdf",
        "linux_index",
        "numa_node",
        "root_port",
        "firmware",
        "driver",
        "vbios",
        "board_identity",
        "memory_identity",
        "storage_identity",
        "nic_identity",
        "sensor_name",
        "switch_port",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class InventoryError(ValueError):
    """Base class for rejected inventory input."""


class WrongTargetError(InventoryError):
    """Raised whenever data is not explicitly scoped to machine 17049."""


class InventoryCycleError(InventoryError):
    """Raised when a connection would create a dependency cycle."""


class InventoryLimitError(InventoryError):
    """Raised when a requested traversal exceeds a hard safety bound."""


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: int
    inserted: bool
    contradictions: tuple[int, ...] = ()


@dataclass(frozen=True)
class ConnectionRecord:
    assertion_id: int
    source_asset_id: str
    source_port: str | None
    destination_asset_id: str | None
    destination_port: str | None
    relationship: str
    valid_from: str
    valid_to: str | None
    observed_at: str
    provenance: str
    evidence_ref: str | None
    confidence: float
    unknown: bool
    unknown_detail: str | None
    corrects_assertion_id: int | None


@dataclass(frozen=True)
class DependencyResult:
    root_asset_id: str
    at: str
    asset_ids: tuple[str, ...]
    connections: tuple[ConnectionRecord, ...]
    unknowns: tuple[ConnectionRecord, ...]
    uncertain: bool
    cycle_detected: bool
    truncated: bool


@dataclass(frozen=True)
class SharingResult:
    gpu_asset_id: str
    at: str
    shared_dependencies: Mapping[str, tuple[str, ...]]
    uncertain: bool
    unknown_gpu_paths: tuple[str, ...]
    cycle_detected: bool
    truncated: bool


@dataclass(frozen=True)
class ProbeCaptureResult:
    """Summary of one idempotent target-probe inventory ingestion."""

    machine_id: str
    observed_at: str
    payload_hash: str
    asset_ids: tuple[str, ...]
    unresolved_gpu_asset_ids: tuple[str, ...]
    inserted_assets: int
    inserted_aliases: int
    inserted_attributes: int
    partial: bool


def _utc_text(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InventoryError("timestamps must be timezone-aware")
        parsed = value.astimezone(timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise InventoryError("timestamp is required")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InventoryError("timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise InventoryError("timestamps must include a UTC offset")
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


_GPU_UUID = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_PCI_BDF = re.compile(r"(?:[0-9a-fA-F]{4,8}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]\Z")
_UNKNOWN_PROBE_VALUES = frozenset(
    {
        "",
        "n/a",
        "[n/a]",
        "none",
        "not available",
        "not specified",
        "to be filled by o.e.m.",
        "to be filled by oem",
        "default string",
        "system serial number",
        "not supported",
        "[not supported]",
        "no asset tag",
        "invalid",
        "unknown",
    }
)
_UNKNOWN_COMPACT_PROBE_VALUES = frozenset(
    {
        "na",
        "none",
        "notavailable",
        "notspecified",
        "tobefilledbyoem",
        "defaultstring",
        "systemserialnumber",
        "notsupported",
        "noassettag",
        "invalid",
        "unknown",
    }
)


def _bounded_text(value: object, field: str, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = str(value).strip()
    if not text:
        if optional:
            return None
        raise InventoryError(f"{field} is required")
    if len(text.encode("utf-8")) > limit:
        raise InventoryError(f"{field} exceeds {limit} bytes")
    return text


def _confidence(value: float) -> float:
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise InventoryError("confidence must be between 0 and 1")
    return result


class Inventory:
    """Hardware history stored in tables private to ``namespace``.

    Source components are upstream of destination components.  For example, a
    PSU/breakout/lead/GPU path is stored as a sequence of connections directed
    toward the GPU.  Unknown upstream endpoints use ``destination_asset_id=None``
    only through :meth:`record_unknown`; they are never guessed from GPU order.
    """

    def __init__(self, connection: sqlite3.Connection, namespace: str = "inventory"):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not _IDENTIFIER.fullmatch(namespace):
            raise InventoryError("namespace must be a short SQL identifier")
        self.connection = connection
        self.namespace = namespace
        self._meta = f"{namespace}_schema"
        self._assets = f"{namespace}_assets"
        self._aliases = f"{namespace}_alias_assertions"
        self._attributes = f"{namespace}_attribute_assertions"
        self._connections = f"{namespace}_connection_assertions"
        self._corrections = f"{namespace}_corrections"
        self._probe_captures = f"{namespace}_probe_captures"
        self._transaction_depth = 0
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {self._meta} (
              schema_version INTEGER NOT NULL CHECK(schema_version = 1)
            );
            INSERT INTO {self._meta}(schema_version)
              SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM {self._meta});
            CREATE TABLE IF NOT EXISTS {self._assets} (
              asset_id TEXT PRIMARY KEY,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              component_kind TEXT NOT NULL,
              label TEXT,
              first_observed_at TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref TEXT,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS {self._aliases} (
              assertion_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              asset_id TEXT NOT NULL REFERENCES {self._assets}(asset_id),
              alias_kind TEXT NOT NULL,
              alias_value TEXT NOT NULL,
              valid_from TEXT NOT NULL,
              valid_to TEXT,
              observed_at TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref TEXT,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              fingerprint TEXT NOT NULL UNIQUE,
              CHECK(valid_to IS NULL OR valid_to > valid_from)
            );
            CREATE INDEX IF NOT EXISTS {self.namespace}_alias_lookup
              ON {self._aliases}(alias_kind, alias_value, valid_from, valid_to);
            CREATE TABLE IF NOT EXISTS {self._attributes} (
              assertion_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              asset_id TEXT NOT NULL REFERENCES {self._assets}(asset_id),
              attribute_kind TEXT NOT NULL,
              attribute_value TEXT,
              explicit_unknown INTEGER NOT NULL CHECK(explicit_unknown IN (0, 1)),
              valid_from TEXT NOT NULL,
              valid_to TEXT,
              observed_at TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref TEXT,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              fingerprint TEXT NOT NULL UNIQUE,
              CHECK((explicit_unknown = 1 AND attribute_value IS NULL) OR
                    (explicit_unknown = 0 AND attribute_value IS NOT NULL)),
              CHECK(valid_to IS NULL OR valid_to > valid_from)
            );
            CREATE INDEX IF NOT EXISTS {self.namespace}_attribute_lookup
              ON {self._attributes}(asset_id, attribute_kind, valid_from, valid_to);
            CREATE TABLE IF NOT EXISTS {self._connections} (
              assertion_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              source_asset_id TEXT NOT NULL REFERENCES {self._assets}(asset_id),
              source_port TEXT,
              destination_asset_id TEXT REFERENCES {self._assets}(asset_id),
              destination_port TEXT,
              relationship TEXT NOT NULL,
              valid_from TEXT NOT NULL,
              valid_to TEXT,
              observed_at TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref TEXT,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              unknown INTEGER NOT NULL CHECK(unknown IN (0, 1)),
              unknown_detail TEXT,
              corrects_assertion_id INTEGER REFERENCES {self._connections}(assertion_id),
              fingerprint TEXT NOT NULL UNIQUE,
              CHECK(valid_to IS NULL OR valid_to > valid_from),
              CHECK((unknown = 0 AND destination_asset_id IS NOT NULL AND unknown_detail IS NULL) OR
                    (unknown = 1 AND unknown_detail IS NOT NULL))
            );
            CREATE INDEX IF NOT EXISTS {self.namespace}_connection_source
              ON {self._connections}(source_asset_id, valid_from, valid_to);
            CREATE INDEX IF NOT EXISTS {self.namespace}_connection_destination
              ON {self._connections}(destination_asset_id, valid_from, valid_to);
            CREATE TABLE IF NOT EXISTS {self._corrections} (
              correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              assertion_table TEXT NOT NULL CHECK(assertion_table IN ('alias', 'attribute', 'connection')),
              assertion_id INTEGER NOT NULL,
              effective_at TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref TEXT,
              reason TEXT NOT NULL,
              fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS {self.namespace}_correction_lookup
              ON {self._corrections}(assertion_table, assertion_id, effective_at);
            CREATE TABLE IF NOT EXISTS {self._probe_captures} (
              payload_hash TEXT PRIMARY KEY,
              machine_id TEXT NOT NULL CHECK(machine_id = '{MACHINE_ID}'),
              observed_at TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('pending', 'complete')),
              asset_ids TEXT,
              unresolved_gpu_asset_ids TEXT,
              partial INTEGER CHECK(partial IN (0, 1)),
              CHECK(
                (state = 'pending' AND asset_ids IS NULL
                   AND unresolved_gpu_asset_ids IS NULL AND partial IS NULL)
                OR
                (state = 'complete' AND asset_ids IS NOT NULL
                   AND unresolved_gpu_asset_ids IS NOT NULL AND partial IS NOT NULL)
              )
            );
            CREATE INDEX IF NOT EXISTS {self.namespace}_probe_capture_order
              ON {self._probe_captures}(state, observed_at);
            """
        )
        version = self.connection.execute(
            f"SELECT schema_version FROM {self._meta}"
        ).fetchone()
        if version is None or version[0] != 1:
            raise InventoryError("unsupported inventory schema version")
        columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({self._probe_captures})")}
        for name, kind in (("accepted_batch_id", "INTEGER"), ("host_epoch", "INTEGER"), ("source_observed_at", "TEXT")):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE {self._probe_captures} ADD COLUMN {name} {kind}")
        self.connection.commit()

    def _commit(self) -> None:
        """Commit standalone API writes, but defer commits inside owned work."""
        if self._transaction_depth == 0:
            self.connection.commit()

    @contextmanager
    def _transaction(self) -> Iterable[None]:
        """Own an atomic unit while respecting a caller-owned transaction."""
        owns_transaction = not self.connection.in_transaction
        savepoint = f"{self.namespace}_capture_{self._transaction_depth}"
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if owns_transaction:
                self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            self._transaction_depth -= 1
            if owns_transaction:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE {savepoint}")

    @staticmethod
    def _target(machine_id: str) -> str:
        if str(machine_id) != MACHINE_ID:
            raise WrongTargetError(f"inventory target must be Vast machine {MACHINE_ID}")
        return MACHINE_ID

    @staticmethod
    def _asset(value: str) -> str:
        asset_id = _bounded_text(value, "asset_id", 128)
        assert asset_id is not None
        if not _ASSET_ID.fullmatch(asset_id):
            raise InventoryError("asset_id contains unsupported characters")
        return asset_id

    def _require_asset(self, asset_id: str) -> None:
        if self.connection.execute(
            f"SELECT 1 FROM {self._assets} WHERE asset_id = ?", (asset_id,)
        ).fetchone() is None:
            raise InventoryError(f"unknown asset_id: {asset_id}")

    def add_asset(
        self,
        asset_id: str,
        component_kind: str,
        *,
        observed_at: str | datetime,
        provenance: str,
        machine_id: str = MACHINE_ID,
        label: str | None = None,
        evidence_ref: str | None = None,
        confidence: float = 1.0,
    ) -> AssertionResult:
        """Add an explicitly assigned stable asset ID, idempotently.

        PCI BDFs and Linux indexes are deliberately rejected as stable asset IDs
        when their conventional forms are used.
        """
        self._target(machine_id)
        asset_id = self._asset(asset_id)
        lowered = asset_id.lower()
        if re.fullmatch(r"(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", lowered):
            raise InventoryError("a PCI BDF is a time-scoped attribute, not an asset_id")
        kind = _bounded_text(component_kind, "component_kind", 64)
        provenance = _bounded_text(provenance, "provenance", 256)
        assert kind is not None and provenance is not None
        stamp = _utc_text(observed_at)
        label = _bounded_text(label, "label", 256, optional=True)
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 512, optional=True)
        certainty = _confidence(confidence)
        payload = {
            "asset_id": asset_id,
            "machine_id": MACHINE_ID,
            "component_kind": kind,
            "label": label,
            "first_observed_at": stamp,
            "provenance": provenance,
            "evidence_ref": evidence_ref,
            "confidence": certainty,
        }
        fingerprint = _fingerprint(payload)
        row = self.connection.execute(
            f"SELECT rowid, component_kind FROM {self._assets} WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if row is not None:
            if row[1] != kind:
                raise InventoryError(
                    f"asset {asset_id} already exists as component kind {row[1]}"
                )
            return AssertionResult(int(row[0]), False)
        cursor = self.connection.execute(
            f"""INSERT INTO {self._assets}
               (asset_id, machine_id, component_kind, label, first_observed_at,
                provenance, evidence_ref, confidence, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                asset_id,
                MACHINE_ID,
                kind,
                label,
                stamp,
                provenance,
                evidence_ref,
                certainty,
                fingerprint,
            ),
        )
        self._commit()
        return AssertionResult(int(cursor.lastrowid), True)

    def assert_alias(
        self,
        asset_id: str,
        alias_kind: str,
        alias_value: str,
        *,
        valid_from: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        valid_to: str | datetime | None = None,
        machine_id: str = MACHINE_ID,
        evidence_ref: str | None = None,
        confidence: float = 1.0,
    ) -> AssertionResult:
        """Append a serial/UUID alias while retaining conflicting assertions."""
        self._target(machine_id)
        asset_id = self._asset(asset_id)
        self._require_asset(asset_id)
        kind = _bounded_text(alias_kind, "alias_kind", 64)
        value = _bounded_text(alias_value, "alias_value", 256)
        provenance = _bounded_text(provenance, "provenance", 256)
        assert kind is not None and value is not None and provenance is not None
        if kind not in IDENTITY_ALIAS_KINDS:
            raise InventoryError("only serial and uuid are identity aliases")
        start = _utc_text(valid_from)
        end = _utc_text(valid_to) if valid_to is not None else None
        stamp = _utc_text(observed_at)
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 512, optional=True)
        certainty = _confidence(confidence)
        payload = {
            "asset_id": asset_id,
            "alias_kind": kind,
            "alias_value": value,
            "valid_from": start,
            "valid_to": end,
            "observed_at": stamp,
            "provenance": provenance,
            "evidence_ref": evidence_ref,
            "confidence": certainty,
        }
        fingerprint = _fingerprint(payload)
        existing = self.connection.execute(
            f"SELECT assertion_id FROM {self._aliases} WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            return AssertionResult(int(existing[0]), False, self.alias_conflicts(int(existing[0])))
        cursor = self.connection.execute(
            f"""INSERT INTO {self._aliases}
               (machine_id, asset_id, alias_kind, alias_value, valid_from, valid_to,
                observed_at, provenance, evidence_ref, confidence, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MACHINE_ID,
                asset_id,
                kind,
                value,
                start,
                end,
                stamp,
                provenance,
                evidence_ref,
                certainty,
                fingerprint,
            ),
        )
        assertion_id = int(cursor.lastrowid)
        self._commit()
        return AssertionResult(assertion_id, True, self.alias_conflicts(assertion_id))

    def alias_conflicts(self, assertion_id: int) -> tuple[int, ...]:
        row = self.connection.execute(
            f"""SELECT asset_id, alias_kind, alias_value, valid_from, valid_to
                FROM {self._aliases} WHERE assertion_id = ?""",
            (int(assertion_id),),
        ).fetchone()
        if row is None:
            raise InventoryError("unknown alias assertion")
        found = self.connection.execute(
            f"""SELECT assertion_id FROM {self._aliases}
                WHERE assertion_id <> ? AND asset_id <> ?
                  AND alias_kind = ? AND alias_value = ?
                  AND (valid_to IS NULL OR valid_to > ?)
                  AND (? IS NULL OR valid_from < ?)
                ORDER BY assertion_id""",
            (assertion_id, row[0], row[1], row[2], row[3], row[4], row[4]),
        ).fetchall()
        return tuple(int(item[0]) for item in found)

    def assert_attribute(
        self,
        asset_id: str,
        attribute_kind: str,
        value: str | None,
        *,
        valid_from: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        valid_to: str | datetime | None = None,
        explicit_unknown: bool = False,
        machine_id: str = MACHINE_ID,
        evidence_ref: str | None = None,
        confidence: float = 1.0,
    ) -> AssertionResult:
        """Append a time-scoped fact such as BDF, index, position, or model."""
        self._target(machine_id)
        asset_id = self._asset(asset_id)
        self._require_asset(asset_id)
        kind = _bounded_text(attribute_kind, "attribute_kind", 64)
        provenance = _bounded_text(provenance, "provenance", 256)
        assert kind is not None and provenance is not None
        if kind in IDENTITY_ALIAS_KINDS:
            raise InventoryError("serial and uuid must be recorded as aliases")
        if explicit_unknown:
            if value is not None:
                raise InventoryError("an explicit unknown cannot also have a value")
            normalized = None
        else:
            normalized = _bounded_text(value, "attribute value", 1024)
        start = _utc_text(valid_from)
        end = _utc_text(valid_to) if valid_to is not None else None
        stamp = _utc_text(observed_at)
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 512, optional=True)
        certainty = _confidence(confidence)
        payload = {
            "asset_id": asset_id,
            "attribute_kind": kind,
            "attribute_value": normalized,
            "explicit_unknown": bool(explicit_unknown),
            "valid_from": start,
            "valid_to": end,
            "observed_at": stamp,
            "provenance": provenance,
            "evidence_ref": evidence_ref,
            "confidence": certainty,
        }
        fingerprint = _fingerprint(payload)
        existing = self.connection.execute(
            f"SELECT assertion_id FROM {self._attributes} WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            return AssertionResult(int(existing[0]), False)
        cursor = self.connection.execute(
            f"""INSERT INTO {self._attributes}
               (machine_id, asset_id, attribute_kind, attribute_value,
                explicit_unknown, valid_from, valid_to, observed_at, provenance,
                evidence_ref, confidence, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MACHINE_ID,
                asset_id,
                kind,
                normalized,
                int(explicit_unknown),
                start,
                end,
                stamp,
                provenance,
                evidence_ref,
                certainty,
                fingerprint,
            ),
        )
        self._commit()
        return AssertionResult(int(cursor.lastrowid), True)

    @staticmethod
    def _connection_columns() -> str:
        return """c.assertion_id, c.source_asset_id, c.source_port,
                  c.destination_asset_id, c.destination_port, c.relationship,
                  c.valid_from, c.valid_to, c.observed_at, c.provenance,
                  c.evidence_ref, c.confidence, c.unknown, c.unknown_detail,
                  c.corrects_assertion_id"""

    def _active_connection_rows(
        self,
        at: str,
        *,
        max_rows: int,
        asset_id: str | None = None,
        relationship_kinds: frozenset[str] = frozenset(),
        traversal_asset_id: str | None = None,
    ) -> tuple[list[sqlite3.Row | tuple[Any, ...]], bool]:
        """Fetch at most ``max_rows`` active edges and report query truncation.

        ``traversal_asset_id`` selects known incoming edges and unknown assertions
        attached to that asset.  Keeping this predicate in SQL is important: a
        small traversal must never materialize an unrelated large active graph.
        """
        predicates = [
            "c.valid_from <= ?",
            "(c.valid_to IS NULL OR c.valid_to > ?)",
            f"""NOT EXISTS (
                SELECT 1 FROM {self._corrections} x
                WHERE x.assertion_table = 'connection'
                  AND x.assertion_id = c.assertion_id
                  AND x.effective_at <= ?)""",
        ]
        parameters: list[Any] = [at, at, at]
        if asset_id is not None:
            predicates.append("(c.source_asset_id = ? OR c.destination_asset_id = ?)")
            parameters.extend((asset_id, asset_id))
        if traversal_asset_id is not None:
            predicates.append(
                "((c.unknown = 0 AND c.destination_asset_id = ?) OR "
                "(c.unknown = 1 AND c.source_asset_id = ?))"
            )
            parameters.extend((traversal_asset_id, traversal_asset_id))
        if relationship_kinds:
            placeholders = ",".join("?" for _ in relationship_kinds)
            predicates.append(f"c.relationship IN ({placeholders})")
            parameters.extend(sorted(relationship_kinds))
        parameters.append(max_rows + 1)
        cursor = self.connection.execute(
            f"""SELECT {self._connection_columns()}
                FROM {self._connections} c
                WHERE {' AND '.join(predicates)}
                ORDER BY c.assertion_id
                LIMIT ?""",
            parameters,
        )
        rows = cursor.fetchmany(max_rows + 1)
        return rows[:max_rows], len(rows) > max_rows

    @staticmethod
    def _record(row: Sequence[Any]) -> ConnectionRecord:
        return ConnectionRecord(
            assertion_id=int(row[0]),
            source_asset_id=str(row[1]),
            source_port=row[2],
            destination_asset_id=row[3],
            destination_port=row[4],
            relationship=str(row[5]),
            valid_from=str(row[6]),
            valid_to=row[7],
            observed_at=str(row[8]),
            provenance=str(row[9]),
            evidence_ref=row[10],
            confidence=float(row[11]),
            unknown=bool(row[12]),
            unknown_detail=row[13],
            corrects_assertion_id=row[14],
        )

    def _overlapping_outgoing_rows(
        self, asset_id: str, lower: str, upper: str | None, max_rows: int
    ) -> tuple[list[tuple[Any, ...]], bool]:
        predicates = [
            "c.source_asset_id = ?",
            "c.unknown = 0",
            "c.destination_asset_id IS NOT NULL",
            "(c.valid_to IS NULL OR c.valid_to > ?)",
            f"""NOT EXISTS (
                SELECT 1 FROM {self._corrections} x
                WHERE x.assertion_table = 'connection'
                  AND x.assertion_id = c.assertion_id
                  AND x.effective_at <= ?)""",
        ]
        parameters: list[Any] = [asset_id, lower, lower]
        if upper is not None:
            predicates.append("c.valid_from < ?")
            parameters.append(upper)
        parameters.append(max_rows + 1)
        rows = self.connection.execute(
            f"""SELECT {self._connection_columns()},
                       (SELECT MIN(x.effective_at)
                        FROM {self._corrections} x
                        WHERE x.assertion_table = 'connection'
                          AND x.assertion_id = c.assertion_id) AS correction_at
                FROM {self._connections} c
                WHERE {' AND '.join(predicates)}
                ORDER BY c.assertion_id
                LIMIT ?""",
            parameters,
        ).fetchmany(max_rows + 1)
        return [tuple(row) for row in rows[:max_rows]], len(rows) > max_rows

    @staticmethod
    def _earliest_end(*values: str | None) -> str | None:
        finite = [value for value in values if value is not None]
        return min(finite) if finite else None

    def _would_cycle(
        self, source: str, destination: str, lower: str, upper: str | None
    ) -> bool:
        """Check reachability throughout the new edge's complete interval.

        Each queued state carries the common validity intersection of its path.
        Thus future edges are considered, but edges which never coexist cannot
        create a false cycle.  SQL predicates and the global edge/state budget
        keep the check bounded without first loading the graph.
        """
        pending: list[tuple[str, str, str | None]] = [(destination, lower, upper)]
        seen: set[tuple[str, str, str | None]] = set()
        examined = 0
        while pending:
            current, interval_start, interval_end = pending.pop()
            if current == source:
                return True
            state = (current, interval_start, interval_end)
            if state in seen:
                continue
            seen.add(state)
            if len(seen) > HARD_MAX_NODES:
                raise InventoryLimitError("cycle check exceeded its state bound")
            remaining = HARD_MAX_NODES - examined
            if remaining <= 0:
                raise InventoryLimitError("cycle check exceeded its edge bound")
            rows, truncated = self._overlapping_outgoing_rows(
                current, interval_start, interval_end, remaining
            )
            if truncated:
                raise InventoryLimitError("cycle check exceeded its edge bound")
            examined += len(rows)
            for row in rows:
                record = self._record(row)
                next_start = max(interval_start, record.valid_from)
                next_end = self._earliest_end(
                    interval_end, record.valid_to, row[15]
                )
                if next_end is not None and next_start >= next_end:
                    continue
                assert record.destination_asset_id is not None
                pending.append((record.destination_asset_id, next_start, next_end))
        return False

    def assert_connection(
        self,
        source_asset_id: str,
        source_port: str | None,
        destination_asset_id: str,
        destination_port: str | None,
        relationship: str,
        *,
        valid_from: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        valid_to: str | datetime | None = None,
        machine_id: str = MACHINE_ID,
        evidence_ref: str | None = None,
        confidence: float = 1.0,
        corrects_assertion_id: int | None = None,
    ) -> AssertionResult:
        """Append one fully described directed component/port connection."""
        return self._assert_connection(
            source_asset_id,
            source_port,
            destination_asset_id,
            destination_port,
            relationship,
            valid_from=valid_from,
            observed_at=observed_at,
            provenance=provenance,
            valid_to=valid_to,
            machine_id=machine_id,
            evidence_ref=evidence_ref,
            confidence=confidence,
            unknown=False,
            unknown_detail=None,
            corrects_assertion_id=corrects_assertion_id,
        )

    def record_unknown(
        self,
        asset_id: str,
        asset_port: str | None,
        relationship: str,
        detail: str,
        *,
        valid_from: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        machine_id: str = MACHINE_ID,
        evidence_ref: str | None = None,
        confidence: float = 0.0,
    ) -> AssertionResult:
        """Append an explicit unknown upstream relation for ``asset_id``."""
        return self._assert_connection(
            asset_id,
            asset_port,
            None,
            None,
            relationship,
            valid_from=valid_from,
            observed_at=observed_at,
            provenance=provenance,
            valid_to=None,
            machine_id=machine_id,
            evidence_ref=evidence_ref,
            confidence=confidence,
            unknown=True,
            unknown_detail=detail,
            corrects_assertion_id=None,
        )

    def _assert_connection(
        self,
        source_asset_id: str,
        source_port: str | None,
        destination_asset_id: str | None,
        destination_port: str | None,
        relationship: str,
        *,
        valid_from: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        valid_to: str | datetime | None,
        machine_id: str,
        evidence_ref: str | None,
        confidence: float,
        unknown: bool,
        unknown_detail: str | None,
        corrects_assertion_id: int | None,
    ) -> AssertionResult:
        self._target(machine_id)
        source = self._asset(source_asset_id)
        self._require_asset(source)
        destination = self._asset(destination_asset_id) if destination_asset_id else None
        if destination:
            self._require_asset(destination)
        if not unknown and destination is None:
            raise InventoryError("a known connection requires both components")
        if destination == source:
            raise InventoryCycleError("self-connections are not allowed")
        source_port = _bounded_text(source_port, "source_port", 128, optional=True)
        destination_port = _bounded_text(
            destination_port, "destination_port", 128, optional=True
        )
        relation = _bounded_text(relationship, "relationship", 64)
        provenance = _bounded_text(provenance, "provenance", 256)
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 512, optional=True)
        detail = _bounded_text(unknown_detail, "unknown_detail", 512, optional=not unknown)
        assert relation is not None and provenance is not None
        if unknown and detail is None:
            raise InventoryError("an unknown relationship requires an explanation")
        start = _utc_text(valid_from)
        end = _utc_text(valid_to) if valid_to is not None else None
        stamp = _utc_text(observed_at)
        certainty = _confidence(confidence)
        if destination and self._would_cycle(source, destination, start, end):
            raise InventoryCycleError("connection would create a dependency cycle")
        if corrects_assertion_id is not None:
            self._require_connection(int(corrects_assertion_id))
        payload = {
            "source_asset_id": source,
            "source_port": source_port,
            "destination_asset_id": destination,
            "destination_port": destination_port,
            "relationship": relation,
            "valid_from": start,
            "valid_to": end,
            "observed_at": stamp,
            "provenance": provenance,
            "evidence_ref": evidence_ref,
            "confidence": certainty,
            "unknown": unknown,
            "unknown_detail": detail,
            "corrects_assertion_id": corrects_assertion_id,
        }
        fingerprint = _fingerprint(payload)
        existing = self.connection.execute(
            f"SELECT assertion_id FROM {self._connections} WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            return AssertionResult(int(existing[0]), False)
        cursor = self.connection.execute(
            f"""INSERT INTO {self._connections}
               (machine_id, source_asset_id, source_port, destination_asset_id,
                destination_port, relationship, valid_from, valid_to, observed_at,
                provenance, evidence_ref, confidence, unknown, unknown_detail,
                corrects_assertion_id, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MACHINE_ID,
                source,
                source_port,
                destination,
                destination_port,
                relation,
                start,
                end,
                stamp,
                provenance,
                evidence_ref,
                certainty,
                int(unknown),
                detail,
                corrects_assertion_id,
                fingerprint,
            ),
        )
        self._commit()
        return AssertionResult(int(cursor.lastrowid), True)

    def _require_connection(self, assertion_id: int) -> None:
        if self.connection.execute(
            f"SELECT 1 FROM {self._connections} WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone() is None:
            raise InventoryError("unknown connection assertion")

    def correct(
        self,
        assertion_table: str,
        assertion_id: int,
        *,
        effective_at: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        reason: str,
        evidence_ref: str | None = None,
        machine_id: str = MACHINE_ID,
    ) -> AssertionResult:
        """Append a correction/retraction without modifying the original row."""
        self._target(machine_id)
        if assertion_table not in {"alias", "attribute", "connection"}:
            raise InventoryError("unsupported assertion table")
        table = {
            "alias": self._aliases,
            "attribute": self._attributes,
            "connection": self._connections,
        }[assertion_table]
        if self.connection.execute(
            f"SELECT 1 FROM {table} WHERE assertion_id = ?", (int(assertion_id),)
        ).fetchone() is None:
            raise InventoryError("unknown assertion")
        effective = _utc_text(effective_at)
        stamp = _utc_text(observed_at)
        provenance = _bounded_text(provenance, "provenance", 256)
        reason = _bounded_text(reason, "reason", 512)
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 512, optional=True)
        assert provenance is not None and reason is not None
        payload = {
            "assertion_table": assertion_table,
            "assertion_id": int(assertion_id),
            "effective_at": effective,
            "observed_at": stamp,
            "provenance": provenance,
            "evidence_ref": evidence_ref,
            "reason": reason,
        }
        fingerprint = _fingerprint(payload)
        existing = self.connection.execute(
            f"SELECT correction_id FROM {self._corrections} WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            return AssertionResult(int(existing[0]), False)
        cursor = self.connection.execute(
            f"""INSERT INTO {self._corrections}
               (machine_id, assertion_table, assertion_id, effective_at,
                observed_at, provenance, evidence_ref, reason, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MACHINE_ID,
                assertion_table,
                int(assertion_id),
                effective,
                stamp,
                provenance,
                evidence_ref,
                reason,
                fingerprint,
            ),
        )
        self._commit()
        return AssertionResult(int(cursor.lastrowid), True)

    def collapse_duplicate_attributes(
        self, *, at: str | datetime, provenance: str
    ) -> int:
        """Retract attribute assertions that repeat an earlier active one exactly.

        Captures before 2026-09-30 re-asserted unchanged values every time, until
        reconciliation exceeded its row bound. The earliest assertion of each
        active (asset, kind, value) stays. Each repeat gets a correction effective
        from its own start, so no point-in-time view changes, and nothing is
        deleted. Idempotent: a clean inventory yields zero.
        """
        stamp = _utc_text(at)
        rows = self.connection.execute(
            f"""SELECT a.assertion_id, a.asset_id, a.attribute_kind,
                       a.attribute_value, a.explicit_unknown, a.valid_from
                FROM {self._attributes} a
                WHERE a.valid_from <= ? AND (a.valid_to IS NULL OR a.valid_to > ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM {self._corrections} x
                    WHERE x.assertion_table = 'attribute'
                      AND x.assertion_id = a.assertion_id
                      AND x.effective_at <= ?)
                ORDER BY a.asset_id, a.attribute_kind, a.explicit_unknown,
                         a.attribute_value, a.assertion_id""",
            (stamp, stamp, stamp),
        ).fetchall()
        kept: dict[tuple[Any, ...], int] = {}
        repeats: list[tuple[int, int, str]] = []
        for assertion_id, asset_id, kind, value, unknown, valid_from in rows:
            key = (asset_id, kind, value, unknown)
            if key in kept:
                repeats.append((int(assertion_id), kept[key], valid_from))
            else:
                kept[key] = int(assertion_id)
        with self._transaction():
            for assertion_id, original, valid_from in repeats:
                self.correct(
                    "attribute",
                    assertion_id,
                    effective_at=valid_from,
                    observed_at=stamp,
                    provenance=provenance,
                    reason=f"repeats active assertion {original}",
                )
        return len(repeats)

    def move_attribute(
        self,
        asset_id: str,
        attribute_kind: str,
        value: str,
        *,
        effective_at: str | datetime,
        observed_at: str | datetime,
        provenance: str,
        evidence_ref: str | None = None,
        machine_id: str = MACHINE_ID,
        confidence: float = 1.0,
    ) -> AssertionResult:
        """Append a new location assertion and corrections for prior active values."""
        self._target(machine_id)
        asset_id = self._asset(asset_id)
        effective = _utc_text(effective_at)
        prior = self.connection.execute(
            f"""SELECT a.assertion_id FROM {self._attributes} a
                WHERE a.asset_id = ? AND a.attribute_kind = ?
                  AND a.valid_from <= ? AND (a.valid_to IS NULL OR a.valid_to > ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM {self._corrections} x
                    WHERE x.assertion_table = 'attribute'
                      AND x.assertion_id = a.assertion_id
                      AND x.effective_at <= ?)
                ORDER BY a.assertion_id""",
            (asset_id, attribute_kind, effective, effective, effective),
        ).fetchall()
        for row in prior:
            self.correct(
                "attribute",
                int(row[0]),
                effective_at=effective,
                observed_at=observed_at,
                provenance=provenance,
                reason=f"{attribute_kind} moved to {value}",
                evidence_ref=evidence_ref,
                machine_id=machine_id,
            )
        return self.assert_attribute(
            asset_id,
            attribute_kind,
            value,
            valid_from=effective,
            observed_at=observed_at,
            provenance=provenance,
            evidence_ref=evidence_ref,
            machine_id=machine_id,
            confidence=confidence,
        )

    def point_in_time(
        self,
        at: str | datetime,
        *,
        asset_id: str | None = None,
        relationship_kinds: Iterable[str] | None = None,
        max_rows: int = DEFAULT_MAX_NODES,
        machine_id: str = MACHINE_ID,
    ) -> tuple[ConnectionRecord, ...]:
        """Return a bounded effective topology view at one instant."""
        self._target(machine_id)
        if not 1 <= int(max_rows) <= HARD_MAX_NODES:
            raise InventoryLimitError(f"max_rows must be 1..{HARD_MAX_NODES}")
        stamp = _utc_text(at)
        selected = self._asset(asset_id) if asset_id is not None else None
        kinds = frozenset(str(item) for item in relationship_kinds or ())
        raw_rows, truncated = self._active_connection_rows(
            stamp,
            max_rows=int(max_rows),
            asset_id=selected,
            relationship_kinds=kinds,
        )
        if truncated:
            raise InventoryLimitError("point-in-time result exceeds max_rows")
        return tuple(self._record(row) for row in raw_rows)

    def upstream_dependencies(
        self,
        asset_id: str,
        at: str | datetime,
        *,
        relationship_kinds: Iterable[str] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        machine_id: str = MACHINE_ID,
    ) -> DependencyResult:
        """Traverse upstream with explicit uncertainty and hard safety bounds."""
        self._target(machine_id)
        root = self._asset(asset_id)
        self._require_asset(root)
        if not 1 <= int(max_depth) <= HARD_MAX_DEPTH:
            raise InventoryLimitError(f"max_depth must be 1..{HARD_MAX_DEPTH}")
        if not 1 <= int(max_nodes) <= HARD_MAX_NODES:
            raise InventoryLimitError(f"max_nodes must be 1..{HARD_MAX_NODES}")
        stamp = _utc_text(at)
        kinds = frozenset(str(item) for item in relationship_kinds or ())
        discovered: set[str] = set()
        used: dict[int, ConnectionRecord] = {}
        unknowns: dict[int, ConnectionRecord] = {}
        queue: list[tuple[str, int, frozenset[str]]] = [(root, 0, frozenset({root}))]
        cycle = False
        truncated = False
        while queue:
            current, depth, path = queue.pop(0)
            remaining_edges = max_nodes - len(used) - len(unknowns)
            raw_rows, query_truncated = self._active_connection_rows(
                stamp,
                max_rows=max(0, remaining_edges),
                relationship_kinds=kinds,
                traversal_asset_id=current,
            )
            truncated = truncated or query_truncated
            active = [self._record(raw) for raw in raw_rows]
            for unknown in (row for row in active if row.unknown):
                unknowns[unknown.assertion_id] = unknown
            if depth >= max_depth:
                if any(not row.unknown for row in active):
                    truncated = True
                continue
            for edge in (row for row in active if not row.unknown):
                used[edge.assertion_id] = edge
                upstream = edge.source_asset_id
                if upstream in path:
                    cycle = True
                    continue
                if upstream not in discovered:
                    discovered.add(upstream)
                    if len(discovered) > max_nodes:
                        truncated = True
                        queue.clear()
                        break
                    queue.append((upstream, depth + 1, path | {upstream}))
        connections = tuple(used[key] for key in sorted(used))
        unresolved = tuple(unknowns[key] for key in sorted(unknowns))
        uncertain = bool(unresolved or cycle or truncated) or any(
            row.confidence < 1.0 for row in connections
        )
        return DependencyResult(
            root,
            stamp,
            tuple(sorted(discovered)),
            connections,
            unresolved,
            uncertain,
            cycle,
            truncated,
        )

    def gpu_sharing(
        self,
        gpu_asset_id: str,
        at: str | datetime,
        *,
        relationship_kinds: Iterable[str] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        machine_id: str = MACHINE_ID,
    ) -> SharingResult:
        """Report upstream components shared with other GPUs.

        Results are conservative: an unknown path on any considered GPU marks the
        answer uncertain, so policy gates cannot interpret absence as isolation.
        """
        self._target(machine_id)
        gpu = self._asset(gpu_asset_id)
        row = self.connection.execute(
            f"SELECT component_kind FROM {self._assets} WHERE asset_id = ?", (gpu,)
        ).fetchone()
        if row is None or row[0] != "gpu":
            raise InventoryError("gpu_sharing requires a known gpu asset")
        stamp = _utc_text(at)
        gpu_cursor = self.connection.execute(
            f"""SELECT asset_id FROM {self._assets}
                WHERE component_kind = 'gpu'
                ORDER BY asset_id = ? DESC, asset_id
                LIMIT ?""",
            (gpu, HARD_MAX_NODES + 1),
        )
        gpu_rows = gpu_cursor.fetchmany(HARD_MAX_NODES + 1)
        dependency_sets: dict[str, set[str]] = {}
        unknown_paths: list[str] = []
        cycle = False
        truncated = len(gpu_rows) > HARD_MAX_NODES
        gpu_rows = gpu_rows[:HARD_MAX_NODES]
        uncertain = False
        total_seen = 0
        for gpu_row in gpu_rows:
            gpu_id = str(gpu_row[0])
            result = self.upstream_dependencies(
                gpu_id,
                stamp,
                relationship_kinds=relationship_kinds,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
            dependency_sets[gpu_id] = set(result.asset_ids)
            total_seen += len(result.asset_ids)
            if total_seen > HARD_MAX_NODES:
                truncated = True
                uncertain = True
                break
            if result.unknowns:
                unknown_paths.append(gpu_id)
            cycle = cycle or result.cycle_detected
            truncated = truncated or result.truncated
            uncertain = uncertain or result.uncertain
        target_dependencies = dependency_sets.get(gpu, set())
        shared: dict[str, tuple[str, ...]] = {}
        for dependency in sorted(target_dependencies):
            peers = tuple(
                sorted(
                    gpu_id
                    for gpu_id, dependencies in dependency_sets.items()
                    if gpu_id != gpu and dependency in dependencies
                )
            )
            if peers:
                shared[dependency] = peers
        return SharingResult(
            gpu,
            stamp,
            shared,
            uncertain or cycle or truncated,
            tuple(sorted(unknown_paths)),
            cycle,
            truncated,
        )

    def assertion_history(
        self, assertion_table: str, *, machine_id: str = MACHINE_ID
    ) -> tuple[tuple[Any, ...], ...]:
        """Return bounded raw assertion history for audit and restore tests."""
        self._target(machine_id)
        table = {
            "alias": self._aliases,
            "attribute": self._attributes,
            "connection": self._connections,
            "correction": self._corrections,
        }.get(assertion_table)
        if table is None:
            raise InventoryError("unsupported assertion table")
        rows = self.connection.execute(
            f"SELECT * FROM {table} ORDER BY 1 LIMIT ?", (HARD_MAX_NODES,)
        ).fetchall()
        return tuple(tuple(row) for row in rows)


def _probe_text(value: object, *, limit: int = 1024) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    compact = re.sub(r"[^a-z0-9]", "", text.casefold())
    if (
        not text
        or len(text.encode("utf-8")) > limit
        or not text.isprintable()
        or text.casefold() in _UNKNOWN_PROBE_VALUES
        or compact in _UNKNOWN_COMPACT_PROBE_VALUES
    ):
        return None
    return text


def _valid_probe_serial(value: object) -> str | None:
    serial = _probe_text(value, limit=256)
    if serial is None or not any(character.isalnum() for character in serial):
        return None
    compact = re.sub(r"[^0-9a-f]", "", serial.casefold())
    if compact and set(compact) <= {"0", "f"}:
        return None
    return serial


def _probe_bdf(value: object) -> str | None:
    if not isinstance(value, str) or not _PCI_BDF.fullmatch(value.strip()):
        return None
    parts = value.strip().lower().split(":")
    if len(parts) == 2:
        parts.insert(0, "0000")
    return f"{int(parts[0], 16):04x}:{parts[1]}:{parts[2]}"


def _active_attributes(
    inventory: Inventory, asset_id: str, kind: str, at: str
) -> tuple[tuple[Any, ...], ...]:
    rows = inventory.connection.execute(
        f"""SELECT a.assertion_id, a.attribute_value, a.explicit_unknown
            FROM {inventory._attributes} a
            WHERE a.asset_id = ? AND a.attribute_kind = ?
              AND a.valid_from <= ? AND (a.valid_to IS NULL OR a.valid_to > ?)
              AND NOT EXISTS (
                SELECT 1 FROM {inventory._corrections} x
                WHERE x.assertion_table = 'attribute'
                  AND x.assertion_id = a.assertion_id
                  AND x.effective_at <= ?)
            ORDER BY a.assertion_id
            LIMIT ?""",
        (asset_id, kind, at, at, at, HARD_MAX_NODES + 1),
    ).fetchmany(HARD_MAX_NODES + 1)
    if len(rows) > HARD_MAX_NODES:
        raise InventoryLimitError("probe attribute reconciliation exceeded its row bound")
    return tuple(tuple(row) for row in rows)


def _capture_attribute(
    inventory: Inventory,
    asset_id: str,
    kind: str,
    value: object,
    *,
    observed_at: str,
    provenance: str,
    evidence_ref: str,
) -> AssertionResult:
    normalized = _probe_text(value)
    active = _active_attributes(inventory, asset_id, kind, observed_at)
    if normalized is None:
        # An unavailable field is evidence in its own right, but must not retract
        # a last-known value from a partial capture. An unknown that is already
        # active still holds; asserting it again every capture grew without bound.
        for assertion_id, _old_value, explicit_unknown in active:
            if explicit_unknown:
                return AssertionResult(int(assertion_id), False)
        return inventory.assert_attribute(
            asset_id,
            kind,
            None,
            explicit_unknown=True,
            valid_from=observed_at,
            observed_at=observed_at,
            provenance=provenance,
            evidence_ref=evidence_ref,
            confidence=0.0,
        )

    unchanged: int | None = None
    for assertion_id, old_value, explicit_unknown in active:
        if not explicit_unknown and old_value == normalized:
            # The fact already holds from its first observation. Its fingerprint
            # includes the capture time, so asserting it again would add a row
            # every capture until reconciliation exceeds its row bound.
            if unchanged is None:
                unchanged = int(assertion_id)
            continue
        inventory.correct(
            "attribute",
            int(assertion_id),
            effective_at=observed_at,
            observed_at=observed_at,
            provenance=provenance,
            reason=f"target probe observed a changed {kind}",
            evidence_ref=evidence_ref,
        )
    if unchanged is not None:
        return AssertionResult(unchanged, False)
    return inventory.assert_attribute(
        asset_id,
        kind,
        normalized,
        valid_from=observed_at,
        observed_at=observed_at,
        provenance=provenance,
        evidence_ref=evidence_ref,
    )


def _atomic_probe_capture(function: Any) -> Any:
    """Keep capture metadata and all resulting assertions in one transaction."""

    @wraps(function)
    def wrapped(inventory: Inventory, probe: Mapping[str, Any], **kwargs: Any) -> ProbeCaptureResult:
        if not isinstance(inventory, Inventory):
            return function(inventory, probe, **kwargs)
        with inventory._transaction():
            return function(inventory, probe, **kwargs)

    return wrapped


def _retained_probe_result(
    observed_at: str,
    payload_hash: str,
    asset_ids_json: str,
    unresolved_json: str,
    partial: int,
) -> ProbeCaptureResult:
    """Rebuild the no-op result for an already completed capture."""
    try:
        asset_ids = json.loads(asset_ids_json)
        unresolved = json.loads(unresolved_json)
    except (TypeError, ValueError) as exc:
        raise InventoryError("stored probe capture metadata is invalid") from exc
    if (
        not isinstance(asset_ids, list)
        or not all(isinstance(item, str) for item in asset_ids)
        or not isinstance(unresolved, list)
        or not all(isinstance(item, str) for item in unresolved)
    ):
        raise InventoryError("stored probe capture metadata is invalid")
    return ProbeCaptureResult(
        MACHINE_ID,
        observed_at,
        payload_hash,
        tuple(asset_ids),
        tuple(unresolved),
        0,
        0,
        0,
        bool(partial),
    )


@_atomic_probe_capture
def capture_probe(inventory: Inventory, probe: Mapping[str, Any], *, accepted_batch_id: int | None = None) -> ProbeCaptureResult:
    """Append one machine-17049 target-probe snapshot to ``inventory``.

    UUID is the only cross-capture GPU fusion key.  PCI-only observations receive
    capture-local unresolved IDs, so a device at the same BDF in a later capture
    is never silently treated as the same physical GPU.  Missing sections and
    unknown fields append uncertainty and never retract absent components.
    """
    if not isinstance(inventory, Inventory):
        raise TypeError("inventory must be Inventory")
    if not isinstance(probe, Mapping):
        raise InventoryError("probe must be a mapping")
    inventory._target(str(probe.get("machine_id", "")))
    observed_at = _utc_text(probe.get("observed_at"))
    source_observed_at = observed_at
    epoch = None
    if accepted_batch_id is not None:
        accepted = inventory.connection.execute(
            """SELECT b.measured_utc,b.epoch,b.boot_id,b.ordering,b.source,b.source_utc,b.evidence_json FROM observation_batches b
               WHERE b.id=? AND b.target=?""", (accepted_batch_id, probe.get("target"))).fetchone()
        current = inventory.connection.execute(
            "SELECT MAX(epoch) FROM host_epochs WHERE target=?", (probe.get("target"),)).fetchone()[0]
        if (not accepted or accepted[3] != "current" or accepted[4] not in {"ssh", "target-probe"}
                or accepted[1] != current or accepted[2] != probe.get("boot_id")):
            raise InventoryError("inventory requires current accepted host evidence")
        retained_evidence = json.loads(accepted[6])
        if (_utc_text(accepted[5]) != _utc_text(probe.get("source_timestamp", probe.get("observed_at")))
                or retained_evidence.get("snapshot") != probe.get("snapshot")):
            raise InventoryError("inventory payload does not match accepted measurement")
        observed_at, epoch = _utc_text(accepted[0]), accepted[1]
    try:
        encoded = json.dumps(
            probe,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise InventoryError("probe payload must be canonical JSON") from exc
    if len(encoded) > MAX_PROBE_PAYLOAD_BYTES:
        raise InventoryLimitError("probe payload exceeds its byte bound")
    payload_hash = hashlib.sha256(encoded).hexdigest()
    provenance = f"target-probe:sha256:{payload_hash}"
    evidence_ref = f"sha256:{payload_hash}"

    retained = inventory.connection.execute(
        f"""SELECT observed_at, state, asset_ids, unresolved_gpu_asset_ids, partial
            FROM {inventory._probe_captures} WHERE payload_hash = ?""",
        (payload_hash,),
    ).fetchone()
    if retained is not None:
        if retained[1] != "complete":
            raise InventoryError("probe capture is already pending and not complete")
        return _retained_probe_result(
            str(retained[0]), payload_hash, retained[2], retained[3], int(retained[4])
        )

    latest = inventory.connection.execute(
        f"SELECT MAX(observed_at) FROM {inventory._probe_captures} WHERE state = 'complete'"
    ).fetchone()[0]
    if latest is not None and observed_at < str(latest):
        raise InventoryError(
            f"probe capture observed_at {observed_at} is older than "
            f"latest completed capture {latest}"
        )
    inventory.connection.execute(
        f"""INSERT INTO {inventory._probe_captures}
            (payload_hash, machine_id, observed_at, state, accepted_batch_id,host_epoch,source_observed_at)
            VALUES (?, ?, ?, 'pending',?,?,?)""",
        (payload_hash, MACHINE_ID, observed_at, accepted_batch_id, epoch, source_observed_at),
    )

    snapshot = probe.get("snapshot")
    if snapshot is None:
        snapshot = {}
    if not isinstance(snapshot, Mapping):
        raise InventoryError("probe snapshot must be a mapping")
    gpu_snapshot = snapshot.get("gpu")
    system_identity = snapshot.get("system_identity")
    partial = not isinstance(gpu_snapshot, Mapping) or not isinstance(
        system_identity, Mapping
    )
    gpu_snapshot = gpu_snapshot if isinstance(gpu_snapshot, Mapping) else {}
    visible_value = gpu_snapshot.get("gpus")
    pci_value = gpu_snapshot.get("pci_devices")
    if visible_value is None:
        visible_value = []
        partial = True
    if pci_value is None:
        pci_value = []
        partial = True
    if not isinstance(visible_value, list) or not isinstance(pci_value, list):
        raise InventoryError("probe GPU inventories must be lists")
    if len(visible_value) > 32 or len(pci_value) > 32:
        raise InventoryLimitError("probe GPU inventory exceeds its row bound")
    if gpu_snapshot.get("pci_count") is None or gpu_snapshot.get("nvidia_count") is None:
        partial = True

    visible: list[dict[str, Any]] = []
    pci_by_bdf: dict[str, dict[str, Any]] = {}
    seen_uuids: set[str] = set()
    for raw in visible_value:
        if not isinstance(raw, Mapping):
            raise InventoryError("probe GPU record must be a mapping")
        record = dict(raw)
        uuid = record.get("uuid")
        if isinstance(uuid, str) and _GPU_UUID.fullmatch(uuid):
            canonical_uuid = f"GPU-{uuid[4:].lower()}"
            if canonical_uuid in seen_uuids:
                raise InventoryError("probe contains a duplicate GPU UUID")
            seen_uuids.add(canonical_uuid)
            record["_uuid"] = canonical_uuid
        else:
            record["_uuid"] = None
        record["_bdf"] = _probe_bdf(record.get("pci_bdf"))
        visible.append(record)
    for raw in pci_value:
        if not isinstance(raw, Mapping):
            raise InventoryError("probe PCI GPU record must be a mapping")
        record = dict(raw)
        bdf = _probe_bdf(record.get("pci_bdf"))
        if bdf is None:
            raise InventoryError("probe contains an invalid PCI BDF")
        if bdf in pci_by_bdf:
            raise InventoryError("probe contains a duplicate PCI BDF")
        record["_bdf"] = bdf
        pci_by_bdf[bdf] = record

    identity_sections: dict[str, Mapping[str, Any]] = {}
    if isinstance(system_identity, Mapping):
        expected_identity_fields = {
            "motherboard": frozenset({"vendor", "name", "version", "serial"}),
            "bios": frozenset({"vendor", "version", "date"}),
        }
        extra_categories = set(system_identity) - set(expected_identity_fields)
        if extra_categories:
            raise InventoryError("probe system identity category is invalid")
        for category, expected_fields in expected_identity_fields.items():
            values = system_identity.get(category)
            if values is None:
                partial = True
                continue
            if not isinstance(values, Mapping) or not set(values) <= expected_fields:
                raise InventoryError(f"probe {category} identity must use fixed fields")
            identity_sections[category] = values

    assets: list[str] = []
    unresolved: list[str] = []
    inserted_assets = 0
    inserted_aliases = 0
    inserted_attributes = 0
    unresolved_index = 0

    def add_gpu(record: Mapping[str, Any], pci_record: Mapping[str, Any] | None) -> None:
        nonlocal inserted_assets, inserted_aliases, inserted_attributes, unresolved_index
        uuid = record.get("_uuid")
        known_uuid = isinstance(uuid, str)
        if known_uuid:
            asset_id = f"gpu-{uuid[4:]}"
        else:
            asset_id = f"unresolved-gpu-{payload_hash}-{unresolved_index}"
            unresolved_index += 1
            unresolved.append(asset_id)
        assets.append(asset_id)
        added = inventory.add_asset(
            asset_id,
            "gpu",
            observed_at=observed_at,
            provenance=provenance,
            label=None if known_uuid else "Unresolved target-probe GPU observation",
            evidence_ref=evidence_ref,
            confidence=1.0 if known_uuid else 0.0,
        )
        inserted_assets += int(added.inserted)
        if known_uuid:
            alias = inventory.assert_alias(
                asset_id,
                "uuid",
                str(uuid),
                valid_from=observed_at,
                observed_at=observed_at,
                provenance=provenance,
                evidence_ref=evidence_ref,
            )
            inserted_aliases += int(alias.inserted)
        else:
            identity_unknown = _capture_attribute(
                inventory,
                asset_id,
                "gpu_identity",
                None,
                observed_at=observed_at,
                provenance=provenance,
                evidence_ref=evidence_ref,
            )
            inserted_attributes += int(identity_unknown.inserted)
        serial = _valid_probe_serial(record.get("serial"))
        if serial is not None:
            alias = inventory.assert_alias(
                asset_id,
                "serial",
                serial,
                valid_from=observed_at,
                observed_at=observed_at,
                provenance=provenance,
                evidence_ref=evidence_ref,
            )
            inserted_aliases += int(alias.inserted)
        elif "serial" in record:
            result = _capture_attribute(
                inventory,
                asset_id,
                "serial_status",
                None,
                observed_at=observed_at,
                provenance=provenance,
                evidence_ref=evidence_ref,
            )
            inserted_attributes += int(result.inserted)

        fields: dict[str, object] = {}
        for source_name, kind in (
            ("name", "gpu_model"),
            ("driver_version", "driver_version"),
            ("vbios_version", "vbios_version"),
        ):
            if source_name in record:
                fields[kind] = record[source_name]
        if record.get("_bdf") is not None:
            fields["pci_bdf"] = record["_bdf"]
        if pci_record is not None:
            for source_name, kind in (
                ("pci_bdf", "pci_bdf"),
                ("pci_root_path", "pci_root_path"),
                ("numa_node", "numa_node"),
                ("current_link_speed", "current_link_speed"),
                ("current_link_width", "current_link_width"),
                ("max_link_speed", "max_link_speed"),
                ("max_link_width", "max_link_width"),
                ("driver", "driver"),
            ):
                if source_name in pci_record:
                    fields[kind] = pci_record[source_name]
            fields["pci_bdf"] = pci_record["_bdf"]
        for kind, value in fields.items():
            result = _capture_attribute(
                inventory,
                asset_id,
                kind,
                str(value) if value is not None else None,
                observed_at=observed_at,
                provenance=provenance,
                evidence_ref=evidence_ref,
            )
            inserted_attributes += int(result.inserted)

    for record in visible:
        bdf = record.get("_bdf")
        known_uuid = isinstance(record.get("_uuid"), str)
        correlated = (
            pci_by_bdf.pop(str(bdf), None)
            if known_uuid and bdf is not None
            else None
        )
        add_gpu(record, correlated)
    for pci_record in pci_by_bdf.values():
        add_gpu({}, pci_record)

    if isinstance(system_identity, Mapping):
        machine_asset = f"machine-{MACHINE_ID}"
        assets.append(machine_asset)
        added = inventory.add_asset(
            machine_asset,
            "machine",
            observed_at=observed_at,
            provenance=provenance,
            evidence_ref=evidence_ref,
        )
        inserted_assets += int(added.inserted)
        for category, prefix in (("motherboard", "board"), ("bios", "bios")):
            values = identity_sections.get(category)
            if values is None:
                continue
            for field, value in values.items():
                result = _capture_attribute(
                    inventory,
                    machine_asset,
                    f"{prefix}_{field}",
                    value,
                    observed_at=observed_at,
                    provenance=provenance,
                    evidence_ref=evidence_ref,
                )
                inserted_attributes += int(result.inserted)

    result = ProbeCaptureResult(
        MACHINE_ID,
        observed_at,
        payload_hash,
        tuple(assets),
        tuple(unresolved),
        inserted_assets,
        inserted_aliases,
        inserted_attributes,
        partial or bool(unresolved),
    )
    completed = inventory.connection.execute(
        f"""UPDATE {inventory._probe_captures}
            SET state = 'complete', asset_ids = ?, unresolved_gpu_asset_ids = ?,
                partial = ?
            WHERE payload_hash = ? AND state = 'pending'""",
        (
            json.dumps(result.asset_ids, separators=(",", ":")),
            json.dumps(result.unresolved_gpu_asset_ids, separators=(",", ":")),
            int(result.partial),
            payload_hash,
        ),
    )
    if completed.rowcount != 1:
        raise InventoryError("probe capture could not be marked complete")
    return result
