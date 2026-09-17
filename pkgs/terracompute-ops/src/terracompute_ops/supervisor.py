"""Normalize observations into deterministic, deduplicated incidents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .incidents import classify, dedup_key, stable_signature
from .state import StateStore, utc_text


@dataclass(frozen=True)
class ObservationResult:
    healthy: bool
    created: tuple[Path, ...]
    duplicates: int


class Supervisor:
    def __init__(self, store: StateStore, expected_machine_id: str = "17049"):
        self.store = store
        self.expected_machine_id = expected_machine_id

    def observe(self, probe: dict[str, Any]) -> ObservationResult:
        if not isinstance(probe, dict):
            raise ValueError("probe must be a JSON object")
        target = str(probe.get("target", "")).strip()
        machine_id = str(probe.get("machine_id", "")).strip()
        boot_id = str(probe.get("boot_id", "")).strip()
        if not target or not boot_id:
            raise ValueError("probe target and boot_id are required")
        if len(target) > 255 or len(boot_id) > 128:
            raise ValueError("probe identity fields exceed their normalized limits")
        if machine_id != self.expected_machine_id:
            raise ValueError("probe machine_id does not match the configured target")
        healthy = probe.get("healthy")
        events = probe.get("events", [])
        if not isinstance(healthy, bool) or not isinstance(events, list):
            raise ValueError("probe healthy must be boolean and events must be a list")
        if len(events) > 64:
            raise ValueError("probe contains more than 64 events")
        observed_at = str(probe.get("observed_at", utc_text())).strip()
        if len(observed_at) > 64 or not observed_at.endswith("Z"):
            raise ValueError("observed_at must be a bounded UTC timestamp ending in Z")
        try:
            datetime.fromisoformat(observed_at[:-1] + "+00:00")
        except ValueError as error:
            raise ValueError("observed_at must be valid ISO-8601") from error
        if healthy and not events:
            return ObservationResult(healthy=True, created=(), duplicates=0)
        if not events:
            events = [
                {
                    "fault_family": "probe",
                    "code": "unexplained-unhealthy",
                    "message": "probe reported unhealthy without an event",
                }
            ]

        contradictory = healthy and bool(events)
        created: list[Path] = []
        duplicates = 0
        for raw_event in events:
            if not isinstance(raw_event, dict):
                raise ValueError("each event must be a JSON object")
            event = dict(raw_event)
            family = str(event.get("fault_family", "unknown")).strip().lower() or "unknown"
            event["fault_family"] = family
            signature = stable_signature(event)
            key = dedup_key(target, boot_id, family, signature)
            classification = classify(event)
            incident = {
                "schema_version": 1,
                "observation_only": True,
                "target": target,
                "machine_id": machine_id,
                "boot_id": boot_id,
                "observed_at": observed_at,
                "fault_family": family,
                "stable_signature": signature,
                "dedup_key": key,
                "classification": classification,
                "contradictory": contradictory,
            }
            label = classification["label"]
            notification = (
                f"terracompute observation: {label} on {target} "
                f"(machine {machine_id}, boot {boot_id[:12]}, incident {key[:12]})"
            )
            needs_analysis = contradictory or not classification["known"]
            model_request = None
            if needs_analysis:
                model_request = {
                    "schema_version": 1,
                    "purpose": "bounded incident analysis for later operator review",
                    "execution": "disabled",
                    "incident": incident,
                    "evidence": event,
                }
            bundle = self.store.create_incident(
                key, incident, event, notification, model_request
            )
            if bundle is None:
                duplicates += 1
            else:
                created.append(bundle)
        return ObservationResult(healthy=False, created=tuple(created), duplicates=duplicates)
