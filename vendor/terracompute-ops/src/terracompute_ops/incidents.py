"""Deterministic classification and bounded incident evidence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_MODEL_REQUEST_BYTES = 16 * 1024
MAX_STRING_CHARS = 4096
MAX_SOURCE_CHARS = 128
MAX_EVENT_ID_CHARS = 256

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private.?key|api.?key|access.?key|secret|token)", re.I
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]+")
_BOT_URL = re.compile(r"https://api\.telegram\.org/bot[^/\s]+", re.I)

XID_CLASSES = {
    "13": ("gpu-graphics-exception", "critical"),
    "31": ("gpu-memory-management-fault", "critical"),
    "43": ("gpu-reset-channel", "warning"),
    "45": ("gpu-preemptive-cleanup", "warning"),
    "48": ("gpu-double-bit-ecc", "critical"),
    "63": ("gpu-row-remap-pending", "warning"),
    "74": ("gpu-link-fault", "critical"),
    "79": ("gpu-fallen-off-bus", "critical"),
    "119": ("gpu-gsp-timeout", "critical"),
    "120": ("gpu-gsp-error", "critical"),
}
AER_CLASSES = {
    "correctable": ("pcie-aer-correctable", "warning"),
    "nonfatal": ("pcie-aer-nonfatal", "critical"),
    "fatal": ("pcie-aer-fatal", "critical"),
}
CDI_CLASSES = {
    "device-missing": ("cdi-device-missing", "critical"),
    "device-unavailable": ("cdi-device-unavailable", "critical"),
    "injection-failed": ("cdi-injection-failed", "critical"),
    "spec-invalid": ("cdi-spec-invalid", "warning"),
}
CAPACITY_CLASSES = {
    "dcgm-identity-mismatch": ("dcgm-identity-mismatch", "critical"),
    "dcgm-scrape-down": ("dcgm-scrape-down", "critical"),
    "dcgm-scrape-stale": ("dcgm-scrape-stale", "critical"),
    "physical-free-vast-unavailable": ("physical-free-vast-unavailable", "critical"),
    "prometheus-data-invalid": ("prometheus-capacity-data-invalid", "critical"),
    "prometheus-data-missing": ("prometheus-capacity-data-missing", "critical"),
    "prometheus-data-stale": ("prometheus-capacity-data-stale", "critical"),
    "prometheus-query-failed": ("prometheus-capacity-query-failed", "critical"),
    "prometheus-response-malformed": ("prometheus-response-malformed", "critical"),
    "prometheus-response-oversized": ("prometheus-response-oversized", "critical"),
    "target-capacity-invalid": ("target-capacity-invalid", "critical"),
    "target-probe-stale": ("target-probe-stale", "critical"),
    "vast-capacity-arithmetic-mismatch": ("vast-capacity-arithmetic-mismatch", "critical"),
    "vast-exporter-recent-errors": ("vast-exporter-recent-errors", "warning"),
    "vast-idle-exceeds-physical-free": ("vast-idle-exceeds-physical-free", "critical"),
    "vast-machine-unlisted": ("vast-machine-unlisted", "warning"),
    "vast-machine-unverified": ("vast-machine-unverified", "warning"),
    "vast-machine-error": ("vast-machine-error", "critical"),
    "vast-occupancy-mismatch": ("vast-occupancy-mismatch", "critical"),
    "vast-rented-exceeds-healthy": ("vast-rented-exceeds-healthy", "critical"),
    "vast-scrape-down": ("vast-scrape-down", "critical"),
    "vast-scrape-stale": ("vast-scrape-stale", "critical"),
    "vast-total-differs-physical": ("vast-total-differs-physical", "critical"),
    "vast-total-exceeds-physical": ("vast-total-exceeds-physical", "critical"),
}


def canonical_json(value: Any) -> bytes:
    """Return stable, compact UTF-8 JSON bytes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _redact(value: Any, key: str = "", *, preserve_fields: bool = False) -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            (str(k) if preserve_fields else str(k)[:128]): _redact(
                v, str(k), preserve_fields=preserve_fields
            )
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_redact(v, preserve_fields=preserve_fields)
                for v in (value if preserve_fields else value[:256])]
    if isinstance(value, str):
        value = _BEARER.sub("[REDACTED]", value)
        value = _BOT_URL.sub("https://api.telegram.org/bot[REDACTED]", value)
        if not preserve_fields and len(value) > MAX_STRING_CHARS:
            return value[:MAX_STRING_CHARS] + "…[truncated]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_CHARS]


def bounded_evidence(
    value: Any, limit: int = MAX_EVIDENCE_BYTES, *, preserve_fields: bool = False,
) -> Any:
    """Redact evidence, replacing oversized material with a safe digest/preview."""
    redacted = _redact(value, preserve_fields=preserve_fields)
    encoded = canonical_json(redacted)
    if len(encoded) <= limit:
        return redacted
    preview_limit = max(0, min(4096, limit // 2))
    return {
        "truncated": True,
        "original_redacted_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": encoded[:preview_limit].decode("utf-8", errors="replace"),
    }


def classify(event: dict[str, Any]) -> dict[str, Any]:
    """Classify known hardware and capacity events without heuristics."""
    family = str(event.get("fault_family", "")).strip().lower()
    if family == "xid":
        code = str(event.get("code", "")).strip()
        if code in XID_CLASSES:
            label, severity = XID_CLASSES[code]
            return {"known": True, "label": label, "severity": severity}
    elif family == "aer":
        severity_key = str(event.get("severity", "")).strip().lower().replace("-", "")
        if severity_key in AER_CLASSES:
            label, severity = AER_CLASSES[severity_key]
            return {"known": True, "label": label, "severity": severity}
    elif family == "cdi":
        code = str(event.get("code", "")).strip().lower().replace("_", "-")
        if code in CDI_CLASSES:
            label, severity = CDI_CLASSES[code]
            return {"known": True, "label": label, "severity": severity}
    elif family == "capacity":
        code = str(event.get("code", "")).strip().lower().replace("_", "-")
        if code in CAPACITY_CLASSES:
            label, severity = CAPACITY_CLASSES[code]
            return {"known": True, "label": label, "severity": severity}
    severity = str(event.get("severity", "warning")).strip().lower()
    if severity not in {"info", "warning", "error", "critical"}:
        severity = "warning"
    return {"known": False, "label": "unclassified", "severity": severity}


def stable_signature(event: dict[str, Any]) -> str:
    """Hash stable fault identity fields; raw evidence cannot perturb deduplication."""
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    uuid = event.get("uuid") or event.get("gpu_uuid") or evidence.get("uuid") or evidence.get("gpu_uuid")
    if not isinstance(uuid, str) or uuid in {"", "unknown", "N/A"}:
        uuid = None
    location = event.get("pci_bdf") or evidence.get("pci_bdf")
    stable = {
        "signature_version": 2,
        "code": event.get("code"),
        "component": event.get("component"),
        "device": uuid or event.get("device") or location,
        "fault_family": str(event.get("fault_family", "")).lower(),
    }
    # Severity, counts and prose may change within one incident. GPU identity
    # remains stable across BDF moves; an unresolved BDF describes a location.
    return hashlib.sha256(canonical_json(stable)).hexdigest()


def dedup_key(target: str, boot_id: str, family: str, signature: str) -> str:
    """Return the legacy, boot-scoped delivery key.

    This function remains available for callers which persisted baseline keys.
    New lifecycle code uses :func:`stable_incident_id` so a reboot is recorded as
    provenance instead of splitting one continuing fault into two incidents.
    """
    identity = [target, boot_id, family.lower(), signature]
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def stable_incident_id(target: str, source: str, family: str, signature: str) -> str:
    """Return a stable incident identity independent of delivery and boot IDs."""
    identity = [target, source.lower(), family.lower(), signature]
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def delivery_identity(target: str, source: str, source_event_id: str) -> str:
    """Return an idempotency key for an explicitly identified source delivery."""
    identity = [target, source.lower(), source_event_id]
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def evidence_digest(value: Any) -> tuple[str, Any]:
    """Return the digest and bounded/redacted representation stored as provenance."""
    bounded = bounded_evidence(value)
    return hashlib.sha256(canonical_json(bounded)).hexdigest(), bounded
