"""Data models and recorded-baseline parsing for ingress acceptance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


SAMPLE_BASELINE = """\
# traefik pre-cutover ingress baseline, 20260810T051832Z
# Captured from MINAS: hairpin NAT makes off-host checks lie.
anime.saldivar.io            401
bookrequests.saldivar.io     200
drive.saldivar.io            302
dungeon.saldivar.io          000ERR
tautulli.saldivar.io         303
traefik.saldivar.io          401

# certificate identity - must be unchanged after cutover
  immich.saldivar.io: {"issuer": "Let's Encrypt / YR1", "subject": "saldivar.io", "notAfter": "Sep 16 02:41:50 2026 GMT", "sans": ["*.saldivar.io", "saldivar.io"]}
"""

MONITORING_CERTIFICATE_MINIMUM_DAYS = 21
MONITORING_CERTIFICATE_ISSUER_ORGANIZATION = "Lets Encrypt"
MONITORING_CERTIFICATE_SANS = frozenset(("*.saldivar.io", "saldivar.io"))


@dataclass(frozen=True)
class CertificateIdentity:
    issuer: str
    subject: str
    not_after: str
    sans: Tuple[str, ...]

    def display(self) -> str:
        return json.dumps(
            {
                "issuer": self.issuer,
                "subject": self.subject,
                "notAfter": self.not_after,
                "sans": list(self.sans),
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class Baseline:
    statuses: Dict[str, str]
    certificates: Dict[str, CertificateIdentity]
    redirect_hosts: Dict[str, str] = field(default_factory=dict)

    def probe_hostnames(self) -> List[str]:
        return list(dict.fromkeys([*self.statuses, *self.certificates]))

    def reachable_hostnames(self) -> List[str]:
        """Probe hostnames MINUS the ones the baseline says are expected to be dead.

        ⛔ A `000ERR` host never completes a TLS handshake and never reaches a
        backend, so it can produce neither a certificate fingerprint nor an
        access-log entry. Including it in those two checks makes them report
        `missing` on every single run — a gate that always fails is worse than no
        gate, because it teaches you to ignore the one run where it means something.
        """
        return [
            hostname
            for hostname in self.probe_hostnames()
            if self.statuses.get(hostname) != "000ERR"
        ]

    def tls_only_hostnames(self) -> List[str]:
        return [hostname for hostname in self.certificates if hostname not in self.statuses]


@dataclass(frozen=True)
class StatusObservation:
    status: Optional[int] = None
    location: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RedirectObservation:
    status: Optional[int] = None
    location: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ResolutionObservation:
    addresses: Tuple[str, ...] = ()
    error: Optional[str] = None


@dataclass
class Observations:
    statuses: Dict[str, StatusObservation] = field(default_factory=dict)
    certificates: Dict[str, CertificateIdentity] = field(default_factory=dict)
    fingerprints: Dict[str, str] = field(default_factory=dict)
    routers: Dict[str, str] = field(default_factory=dict)
    resolutions: Dict[str, ResolutionObservation] = field(default_factory=dict)
    redirect: Optional[RedirectObservation] = None
    collection_errors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CheckResult:
    check: str
    hostname: str
    expected: str
    observed: str
    outcome: str


@dataclass(frozen=True)
class Evaluation:
    checks: Tuple[CheckResult, ...]

    @property
    def counts(self) -> Dict[str, int]:
        counts = {"matched": 0, "drifted": 0, "missing": 0, "errors": 0}
        for check in self.checks:
            counts[check.outcome] += 1
        return counts

    @property
    def ok(self) -> bool:
        counts = self.counts
        return not (counts["drifted"] or counts["missing"] or counts["errors"])

    @property
    def summary(self) -> str:
        counts = self.counts
        return (
            f"matched={counts['matched']} drifted={counts['drifted']} "
            f"missing={counts['missing']} errors={counts['errors']}"
        )


def parse_baseline_text(text: str) -> Baseline:
    statuses: Dict[str, str] = {}
    certificates: Dict[str, CertificateIdentity] = {}
    redirect_hosts: Dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0].isspace():
            hostname, separator, payload = raw_line.strip().partition(":")
            if not separator or not hostname or not payload.strip():
                raise ValueError(f"line {line_number}: invalid certificate identity")
            try:
                value = json.loads(payload.strip())
                if not isinstance(value, dict):
                    raise TypeError("certificate identity must be a JSON object")
                if not isinstance(value.get("sans"), list):
                    raise TypeError("sans must be a JSON array")
                identity = CertificateIdentity(
                    issuer=value["issuer"],
                    subject=value["subject"],
                    not_after=value["notAfter"],
                    sans=tuple(value["sans"]),
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(
                    f"line {line_number}: invalid certificate identity: {exc}"
                ) from exc
            if not all(
                isinstance(item, str)
                for item in (
                    identity.issuer,
                    identity.subject,
                    identity.not_after,
                    *identity.sans,
                )
            ):
                raise ValueError(f"line {line_number}: certificate fields must be strings")
            if hostname in certificates:
                raise ValueError(f"line {line_number}: duplicate certificate hostname")
            certificates[hostname] = identity
            continue

        parts = raw_line.split()
        if len(parts) not in (2, 3):
            raise ValueError(
                f"line {line_number}: expected hostname, status code, "
                "and optional HTTPS redirect hostname"
            )
        hostname, expected = parts[:2]
        if expected != "000ERR" and not re.fullmatch(r"[0-9]{3}", expected):
            raise ValueError(f"line {line_number}: invalid status code {expected!r}")
        if hostname in statuses:
            raise ValueError(f"line {line_number}: duplicate hostname")
        statuses[hostname] = expected
        if len(parts) == 3:
            redirect_hostname = parts[2].lower().rstrip(".")
            if not expected.startswith("3"):
                raise ValueError(
                    f"line {line_number}: redirect hostname requires a 3xx status"
                )
            if not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                redirect_hostname,
            ):
                raise ValueError(
                    f"line {line_number}: invalid redirect hostname {parts[2]!r}"
                )
            redirect_hosts[hostname] = redirect_hostname

    if not statuses:
        raise ValueError("baseline contains no hostname/status entries")
    return Baseline(
        statuses=statuses,
        certificates=certificates,
        redirect_hosts=redirect_hosts,
    )
