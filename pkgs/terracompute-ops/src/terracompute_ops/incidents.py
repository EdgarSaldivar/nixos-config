"""Deterministic classification and bounded incident evidence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_MODEL_REQUEST_BYTES = 16 * 1024
MAX_STRING_CHARS = 4096

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private.?key|secret|token)", re.I
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


def canonical_json(value: Any) -> bytes:
    """Return stable, compact UTF-8 JSON bytes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(k)[:128]: _redact(v, str(k))
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_redact(v) for v in value[:256]]
    if isinstance(value, str):
        value = _BEARER.sub("[REDACTED]", value)
        value = _BOT_URL.sub("https://api.telegram.org/bot[REDACTED]", value)
        if len(value) > MAX_STRING_CHARS:
            return value[:MAX_STRING_CHARS] + "…[truncated]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_CHARS]


def bounded_evidence(value: Any, limit: int = MAX_EVIDENCE_BYTES) -> Any:
    """Redact evidence, replacing oversized material with a safe digest/preview."""
    redacted = _redact(value)
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
    """Classify known Xid, PCIe AER and CDI events without heuristics."""
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
    return {"known": False, "label": "unclassified", "severity": "warning"}


def stable_signature(event: dict[str, Any]) -> str:
    """Hash stable fault identity fields; raw evidence cannot perturb deduplication."""
    stable = {
        "code": event.get("code"),
        "component": event.get("component"),
        "device": event.get("device"),
        "fault_family": str(event.get("fault_family", "")).lower(),
        "message": " ".join(str(event.get("message", "")).split())[:512],
        "severity": str(event.get("severity", "")).lower(),
    }
    return hashlib.sha256(canonical_json(stable)).hexdigest()


def dedup_key(target: str, boot_id: str, family: str, signature: str) -> str:
    identity = [target, boot_id, family.lower(), signature]
    return hashlib.sha256(canonical_json(identity)).hexdigest()
