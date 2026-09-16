"""Normalize bounded source observations into a durable incident lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .incidents import (
    MAX_EVENT_ID_CHARS,
    MAX_SOURCE_CHARS,
    classify,
    delivery_identity,
    evidence_digest,
    stable_incident_id,
    stable_signature,
)
from .state import MACHINE_ID, StateStore, utc_text


@dataclass(frozen=True)
class ObservationResult:
    healthy: bool
    created: tuple[Path, ...]
    duplicates: int
    material_changed: bool = False


def _valid_utc(value: Any, field: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 64 or not text.endswith("Z"):
        raise ValueError(f"{field} must be a bounded UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field} must be valid ISO-8601") from error
    return text


class Supervisor:
    """Validate observations and preserve delivery, evidence, and boot provenance."""

    def __init__(
        self, store: StateStore, expected_machine_id: str = MACHINE_ID,
        *, max_source_age_seconds: int = 180,
    ):
        if str(expected_machine_id) != MACHINE_ID:
            raise ValueError("this controller is scoped only to Vast machine 17049")
        if (isinstance(max_source_age_seconds, bool)
                or not isinstance(max_source_age_seconds, int)
                or not 1 <= max_source_age_seconds <= 3600):
            raise ValueError("source age limit must be between 1 and 3600 seconds")
        self.store = store
        self.expected_machine_id = MACHINE_ID
        self.max_source_age_seconds = max_source_age_seconds

    def observe(self, probe: dict[str, Any]) -> ObservationResult:
        if not isinstance(probe, dict):
            raise ValueError("probe must be a JSON object")
        target = str(probe.get("target", "")).strip()
        machine_id = str(probe.get("machine_id", "")).strip()
        boot_id = str(probe.get("boot_id", "")).strip()
        if not target or len(target) > 255 or machine_id != self.expected_machine_id:
            raise ValueError("probe identity does not match Vast machine 17049")
        if not boot_id or len(boot_id) > 128:
            raise ValueError("probe boot_id is required and bounded to 128 characters")

        source = str(probe.get("source", "target-probe")).strip().lower()
        if not source or len(source) > MAX_SOURCE_CHARS:
            raise ValueError("probe source is required and bounded")
        observed_at = _valid_utc(
            probe.get("source_timestamp", probe.get("observed_at", "")),
            "observed_at",
        )
        received_at = _valid_utc(utc_text(self.store.clock()), "receipt timestamp")
        age = (datetime.fromisoformat(received_at[:-1] + "+00:00")
               - datetime.fromisoformat(observed_at[:-1] + "+00:00")).total_seconds()
        if age < -30:
            # Reject before ordering state changes; a future source timestamp must
            # never make subsequent valid observations appear out of order.
            raise ValueError("source timestamp exceeds allowed future skew")
        events = probe.get("events", [])
        if not isinstance(events, list):
            raise ValueError("probe events must be a list")
        if len(events) > 128:
            raise ValueError("probe contains more than 128 events")

        explicit_status = probe.get("status")
        healthy_value = probe.get("healthy")
        if explicit_status is None:
            if not isinstance(healthy_value, bool):
                raise ValueError("probe healthy must be boolean when status is absent")
            status = "healthy" if healthy_value else "unhealthy"
        else:
            status = str(explicit_status).strip().lower()
            if status not in {"healthy", "unhealthy", "unknown", "stale"}:
                raise ValueError("probe status must be healthy, unhealthy, unknown, or stale")
            if healthy_value is not None and not isinstance(healthy_value, bool):
                raise ValueError("probe healthy must be boolean when supplied")
        freshness = str(probe.get("freshness", "stale" if status == "stale" else "fresh")).strip().lower()
        if freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("probe freshness must be fresh, stale, or unknown")
        if age > self.max_source_age_seconds:
            freshness = "stale"

        contradictory = status == "healthy" and (
            bool(events) or healthy_value is False
        )
        if contradictory:
            status = "unhealthy"
        base_event_id = probe.get("source_event_id", probe.get("delivery_id"))
        if base_event_id is not None:
            base_event_id = str(base_event_id).strip()
            if not base_event_id or len(base_event_id) > MAX_EVENT_ID_CHARS:
                raise ValueError("source_event_id must be non-empty and bounded")

        if status == "healthy" and not events:
            digest, _ = evidence_digest(probe)
            observation = self._observation(
                target, machine_id, source, base_event_id, observed_at, received_at,
                boot_id, status, freshness, digest,
            )
            write_result = self.store.record_observation(observation, evidence=probe)
            _, duplicate = write_result
            return ObservationResult(
                healthy=freshness == "fresh",
                created=(),
                duplicates=int(duplicate),
                material_changed=write_result.material_changed,
            )

        if not events:
            code = {
                "unhealthy": "unexplained-unhealthy",
                "unknown": "source-unknown",
                "stale": "source-stale",
            }[status]
            events = [{
                "fault_family": "source",
                "code": code,
                "component": source,
                "message": f"{source} reported {status}",
            }]

        created: list[Path] = []
        duplicates = 0
        material_changed = False
        for index, raw_event in enumerate(events):
            if not isinstance(raw_event, dict):
                raise ValueError("each event must be a JSON object")
            event = dict(raw_event)
            family = str(event.get("fault_family", "unknown")).strip().lower() or "unknown"
            if len(family) > 128:
                raise ValueError("fault_family exceeds its normalized limit")
            event["fault_family"] = family
            event_id_value = event.get("source_event_id", base_event_id)
            event_id: str | None
            if event_id_value is None:
                event_id = None
            else:
                event_id = str(event_id_value).strip()
                if not event_id or len(event_id) > MAX_EVENT_ID_CHARS:
                    raise ValueError("event source_event_id must be non-empty and bounded")
                if len(events) > 1 and "source_event_id" not in event:
                    event_id = f"{event_id}#{index}"
                if len(event_id) > MAX_EVENT_ID_CHARS:
                    raise ValueError("event source_event_id exceeds its normalized limit")
            signature = stable_signature(event)
            key = stable_incident_id(target, source, family, signature)
            classification = classify(event)
            digest, _ = evidence_digest(event)
            observation = self._observation(
                target, machine_id, source, event_id, observed_at, received_at,
                boot_id, status, freshness, digest,
            )
            incident = {
                "schema_version": 2,
                "observation_only": True,
                "target": target,
                "machine_id": machine_id,
                "source": source,
                "boot_id": boot_id,
                "observed_at": observed_at,
                "fault_family": family,
                "stable_signature": signature,
                "dedup_key": key,
                "classification": classification,
                "contradictory": contradictory,
                "status": status,
                "freshness": freshness,
                "evidence_sha256": digest,
            }
            label = classification["label"]
            notification = (
                f"terracompute observation: {label} on {target} "
                f"(machine {machine_id}, boot {boot_id[:12]}, incident {key[:12]})"
            )
            model_request = None
            if contradictory or not classification["known"]:
                model_request = {
                    "schema_version": 1,
                    "purpose": "bounded incident analysis for later operator review",
                    "execution": "disabled",
                    "incident": incident,
                    "evidence": event,
                }
            silent_value = event.get("silent", probe.get("silent"))
            if silent_value is not None and not isinstance(silent_value, bool):
                raise ValueError("silent metadata must be boolean")
            write_result = self.store.record_observation(
                observation,
                incident,
                event,
                notification,
                model_request,
                severity=str(classification["severity"]),
                silent=silent_value,
            )
            bundle, duplicate = write_result
            material_changed = material_changed or write_result.material_changed
            if duplicate:
                duplicates += 1
            elif bundle is not None:
                created.append(bundle)
        return ObservationResult(
            healthy=False,
            created=tuple(created),
            duplicates=duplicates,
            material_changed=material_changed,
        )

    @staticmethod
    def _observation(
        target: str,
        machine_id: str,
        source: str,
        event_id: str | None,
        observed_at: str,
        received_at: str,
        boot_id: str,
        status: str,
        freshness: str,
        digest: str,
    ) -> dict[str, Any]:
        return {
            "target": target,
            "machine_id": machine_id,
            "source": source,
            "source_event_id": event_id,
            "delivery_key": (
                None if event_id is None else delivery_identity(target, source, event_id)
            ),
            "source_utc": observed_at,
            "receipt_utc": received_at,
            "boot_id": boot_id,
            "status": status,
            "freshness": freshness,
            "evidence_sha256": digest,
        }
