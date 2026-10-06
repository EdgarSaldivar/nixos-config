"""Small, text-free identity for one spool request and its validity window.

Version 6 requests carry this exact owner. A coordinator may publish it with
``SpoolInvestigator.ask(owner=...)`` and must compare it again at ``peek``.
The owner is identity, not a lease: only runtime progress reports dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_HASH = re.compile(r"[0-9a-f]{64}")
_INCIDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class WorkOwner:
    machine_id: str
    request_id: str
    loop_id: str
    boot_id: str
    evidence_generation: str
    incident_id: str | None = None
    episode_id: int | None = None
    operator_generation: str | None = None

    def __post_init__(self) -> None:
        incident = self.incident_id is not None and self.episode_id is not None
        operator = self.operator_generation is not None
        if (
            self.machine_id != "17049"
            or not isinstance(self.request_id, str) or not _REQUEST.fullmatch(self.request_id)
            or not isinstance(self.loop_id, str) or not _UUID.fullmatch(self.loop_id)
            or not isinstance(self.boot_id, str) or not _UUID.fullmatch(self.boot_id)
            or not isinstance(self.evidence_generation, str)
            or not _HASH.fullmatch(self.evidence_generation)
            or incident == operator
        ):
            raise ValueError("invalid work owner")
        if incident:
            if (not isinstance(self.incident_id, str)
                or not _INCIDENT.fullmatch(self.incident_id)
                or type(self.episode_id) is not int or not 0 < self.episode_id < 2**63
                or self.operator_generation is not None):
                raise ValueError("invalid incident owner")
        elif (self.incident_id is not None or self.episode_id is not None
              or not isinstance(self.operator_generation, str)
              or not _UUID.fullmatch(self.operator_generation)):
            raise ValueError("invalid operator owner")

    def document(self) -> dict[str, Any]:
        common = {"machine_id": self.machine_id, "request_id": self.request_id,
                  "loop_id": self.loop_id, "boot_id": self.boot_id,
                  "evidence_generation": self.evidence_generation}
        if self.operator_generation is not None:
            return dict(common, operator_generation=self.operator_generation)
        return dict(common, incident_id=self.incident_id, episode_id=self.episode_id)

    @classmethod
    def parse(cls, raw: object) -> "WorkOwner":
        if not isinstance(raw, dict):
            raise ValueError("invalid work owner")
        common = {"machine_id", "request_id", "loop_id", "boot_id", "evidence_generation"}
        if set(raw) not in (common | {"incident_id", "episode_id"},
                            common | {"operator_generation"}):
            raise ValueError("invalid work owner")
        return cls(**raw)


def expiry_text(value: datetime, *, now: datetime) -> str:
    """Require an aware UTC expiry no more than one day ahead."""
    if (not isinstance(value, datetime) or value.tzinfo is None
        or not isinstance(now, datetime) or now.tzinfo is None):
        raise ValueError("invalid expiry")
    seconds = (value - now).total_seconds()
    if not 0 < seconds <= 86400:
        raise ValueError("invalid expiry")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_expiry(raw: object) -> datetime:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", raw):
        raise ValueError("invalid expiry")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid expiry") from error
