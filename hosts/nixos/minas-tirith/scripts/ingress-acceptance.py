#!/usr/bin/env python3
"""Verify a local Traefik ingress against a pre-cutover baseline."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import re
import socket
import ssl
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit


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
    error: Optional[str] = None


@dataclass(frozen=True)
class RedirectObservation:
    status: Optional[int] = None
    location: Optional[str] = None
    error: Optional[str] = None


@dataclass
class Observations:
    statuses: Dict[str, StatusObservation] = field(default_factory=dict)
    certificates: Dict[str, CertificateIdentity] = field(default_factory=dict)
    fingerprints: Dict[str, str] = field(default_factory=dict)
    routers: Dict[str, str] = field(default_factory=dict)
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
        if len(parts) != 2:
            raise ValueError(f"line {line_number}: expected hostname and status code")
        hostname, expected = parts
        if expected != "000ERR" and not re.fullmatch(r"[0-9]{3}", expected):
            raise ValueError(f"line {line_number}: invalid status code {expected!r}")
        if hostname in statuses:
            raise ValueError(f"line {line_number}: duplicate hostname")
        statuses[hostname] = expected

    if not statuses:
        raise ValueError("baseline contains no hostname/status entries")
    return Baseline(statuses=statuses, certificates=certificates)


def normalize_fingerprint(value: str) -> str:
    return value.replace(":", "").lower()


def redirect_matches(hostname: str, observation: RedirectObservation) -> bool:
    if observation.status is None or not 300 <= observation.status <= 399:
        return False
    if not observation.location:
        return False
    parsed = urlsplit(observation.location)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") == hostname.lower().rstrip(".")
        and port in (None, 443)
    )


def evaluate(
    baseline: Baseline,
    observations: Observations,
    *,
    expected_fingerprint: Optional[str] = None,
    require_routers: bool = False,
    require_redirect: bool = True,
) -> Evaluation:
    """Pure comparison of expected state and already-collected observations."""
    checks: List[CheckResult] = []

    for hostname, expected in baseline.statuses.items():
        observed = observations.statuses.get(hostname)
        if observed is None:
            checks.append(CheckResult("https", hostname, expected, "absent", "missing"))
            continue

        if expected == "000ERR":
            # WHY: this baseline records an intentionally dead backend; becoming live is drift.
            if observed.error:
                checks.append(
                    CheckResult("https", hostname, expected, observed.error, "matched")
                )
            elif observed.status is not None:
                checks.append(
                    CheckResult(
                        "https", hostname, expected, str(observed.status), "drifted"
                    )
                )
            else:
                checks.append(CheckResult("https", hostname, expected, "absent", "missing"))
            continue

        if observed.error:
            checks.append(CheckResult("https", hostname, expected, observed.error, "errors"))
        elif observed.status is None:
            checks.append(CheckResult("https", hostname, expected, "absent", "missing"))
        else:
            actual = str(observed.status)
            # WHY: authentication middleware intentionally makes several healthy routes non-200.
            # A 200 is therefore only correct when the baseline explicitly says 200.
            outcome = "matched" if actual == expected else "drifted"
            checks.append(CheckResult("https", hostname, expected, actual, outcome))

    for hostname, expected in baseline.certificates.items():
        actual = observations.certificates.get(hostname)
        if actual is None:
            checks.append(
                CheckResult("certificate", hostname, expected.display(), "absent", "missing")
            )
        else:
            outcome = "matched" if actual == expected else "drifted"
            checks.append(
                CheckResult(
                    "certificate", hostname, expected.display(), actual.display(), outcome
                )
            )

    if expected_fingerprint is not None:
        expected_normalized = normalize_fingerprint(expected_fingerprint)
        # Expected-dead hosts are excluded — see Baseline.reachable_hostnames().
        for hostname in baseline.reachable_hostnames():
            actual = observations.fingerprints.get(hostname)
            if actual is None:
                checks.append(
                    CheckResult(
                        "fingerprint", hostname, expected_normalized, "absent", "missing"
                    )
                )
            else:
                actual_normalized = normalize_fingerprint(actual)
                outcome = (
                    "matched" if actual_normalized == expected_normalized else "drifted"
                )
                checks.append(
                    CheckResult(
                        "fingerprint",
                        hostname,
                        expected_normalized,
                        actual_normalized,
                        outcome,
                    )
                )

    if require_redirect:
        first_hostname = next(iter(baseline.statuses))
        redirect = observations.redirect
        if redirect is None:
            checks.append(
                CheckResult(
                    "http-redirect",
                    first_hostname,
                    f"3xx https://{first_hostname}",
                    "absent",
                    "missing",
                )
            )
        elif redirect.error:
            checks.append(
                CheckResult(
                    "http-redirect",
                    first_hostname,
                    f"3xx https://{first_hostname}",
                    redirect.error,
                    "errors",
                )
            )
        else:
            actual = f"{redirect.status} {redirect.location or ''}".rstrip()
            outcome = "matched" if redirect_matches(first_hostname, redirect) else "drifted"
            checks.append(
                CheckResult(
                    "http-redirect",
                    first_hostname,
                    f"3xx https://{first_hostname}",
                    actual,
                    outcome,
                )
            )

    if require_routers:
        # Expected-dead hosts are excluded — see Baseline.reachable_hostnames().
        for hostname in baseline.reachable_hostnames():
            router = observations.routers.get(hostname)
            if router is None:
                checks.append(CheckResult("router", hostname, "*@file", "absent", "missing"))
            else:
                # WHY: after cutover only file-provider routes are authoritative; another
                # provider winning (especially @docker) means the wrong routing graph served it.
                outcome = "matched" if router.endswith("@file") else "drifted"
                checks.append(CheckResult("router", hostname, "*@file", router, outcome))

    for message in observations.collection_errors:
        checks.append(CheckResult("collection", "-", "no error", message, "errors"))

    return Evaluation(tuple(checks))


def _read_tlv(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    if offset >= len(data):
        raise ValueError("truncated DER tag")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("truncated DER length")
    first_length = data[offset]
    offset += 1
    if first_length & 0x80:
        length_bytes = first_length & 0x7F
        if length_bytes == 0 or offset + length_bytes > len(data):
            raise ValueError("invalid DER length")
        length = int.from_bytes(data[offset : offset + length_bytes], "big")
        offset += length_bytes
    else:
        length = first_length
    end = offset + length
    if end > len(data):
        raise ValueError("truncated DER value")
    return tag, data[offset:end], end


def _tlvs(data: bytes) -> Iterable[Tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        tag, value, offset = _read_tlv(data, offset)
        yield tag, value


def _decode_oid(data: bytes) -> str:
    values: List[int] = []
    current = 0
    for byte in data:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            values.append(current)
            current = 0
    if current or not values:
        raise ValueError("invalid DER object identifier")
    first = values.pop(0)
    if first < 40:
        arcs = [0, first]
    elif first < 80:
        arcs = [1, first - 40]
    else:
        arcs = [2, first - 80]
    arcs.extend(values)
    return ".".join(str(arc) for arc in arcs)


def _decode_der_string(tag: int, value: bytes) -> str:
    if tag == 0x1E:
        return value.decode("utf-16-be")
    if tag == 0x1C:
        return value.decode("utf-32-be")
    if tag == 0x0C:
        return value.decode("utf-8")
    return value.decode("latin-1")


def _parse_name(value: bytes) -> Dict[str, List[str]]:
    attributes: Dict[str, List[str]] = {}
    for set_tag, set_value in _tlvs(value):
        if set_tag != 0x31:
            continue
        for sequence_tag, sequence_value in _tlvs(set_value):
            if sequence_tag != 0x30:
                continue
            parts = list(_tlvs(sequence_value))
            if len(parts) < 2 or parts[0][0] != 0x06:
                continue
            oid = _decode_oid(parts[0][1])
            text = _decode_der_string(parts[1][0], parts[1][1])
            attributes.setdefault(oid, []).append(text)
    return attributes


def _display_name(attributes: Dict[str, List[str]], issuer: bool = False) -> str:
    common_name = (attributes.get("2.5.4.3") or [""])[0]
    organization = (attributes.get("2.5.4.10") or [""])[0]
    if issuer and organization and common_name and organization != common_name:
        return f"{organization} / {common_name}"
    if common_name:
        return common_name
    if organization:
        return organization
    return " / ".join(value for values in attributes.values() for value in values)


def _format_certificate_time(tag: int, value: bytes) -> str:
    text = value.decode("ascii")
    if not text.endswith("Z"):
        raise ValueError("certificate time is not UTC")
    if tag == 0x17 and len(text) == 13:
        short_year = int(text[0:2])
        year = 2000 + short_year if short_year <= 49 else 1900 + short_year
        remainder = text[2:]
    elif tag == 0x18 and len(text) == 15:
        year = int(text[0:4])
        remainder = text[4:]
    else:
        raise ValueError("unsupported certificate time format")
    try:
        parsed = datetime.datetime.strptime(
            f"{year:04d}{remainder}", "%Y%m%d%H%M%SZ"
        )
    except ValueError as exc:
        raise ValueError("invalid certificate time") from exc
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    return (
        f"{months[parsed.month - 1]} {parsed.day:2d} "
        f"{parsed:%H:%M:%S} {parsed.year:04d} GMT"
    )


def decode_certificate_identity(der: bytes) -> CertificateIdentity:
    outer_tag, outer_value, outer_end = _read_tlv(der)
    if outer_tag != 0x30 or outer_end != len(der):
        raise ValueError("certificate is not a DER sequence")
    outer_parts = list(_tlvs(outer_value))
    if not outer_parts or outer_parts[0][0] != 0x30:
        raise ValueError("certificate has no TBSCertificate")
    tbs_parts = list(_tlvs(outer_parts[0][1]))
    index = 1 if tbs_parts and tbs_parts[0][0] == 0xA0 else 0
    if len(tbs_parts) < index + 6:
        raise ValueError("truncated TBSCertificate")
    issuer_tag, issuer_value = tbs_parts[index + 2]
    validity_tag, validity_value = tbs_parts[index + 3]
    subject_tag, subject_value = tbs_parts[index + 4]
    if issuer_tag != 0x30 or validity_tag != 0x30 or subject_tag != 0x30:
        raise ValueError("invalid certificate identity fields")
    validity = list(_tlvs(validity_value))
    if len(validity) != 2 or validity[1][0] not in (0x17, 0x18):
        raise ValueError("invalid certificate validity")
    not_after = _format_certificate_time(validity[1][0], validity[1][1])

    sans: List[str] = []
    for tag, extension_wrapper in tbs_parts[index + 6 :]:
        if tag != 0xA3:
            continue
        wrapper = list(_tlvs(extension_wrapper))
        if len(wrapper) != 1 or wrapper[0][0] != 0x30:
            continue
        for extension_tag, extension_value in _tlvs(wrapper[0][1]):
            if extension_tag != 0x30:
                continue
            extension_parts = list(_tlvs(extension_value))
            if not extension_parts or extension_parts[0][0] != 0x06:
                continue
            if _decode_oid(extension_parts[0][1]) != "2.5.29.17":
                continue
            octet_strings = [value for part_tag, value in extension_parts if part_tag == 0x04]
            if not octet_strings:
                continue
            san_tag, san_value, san_end = _read_tlv(octet_strings[-1])
            if san_tag != 0x30 or san_end != len(octet_strings[-1]):
                raise ValueError("invalid subjectAltName extension")
            for name_tag, name_value in _tlvs(san_value):
                if name_tag == 0x82:
                    sans.append(name_value.decode("ascii"))

    return CertificateIdentity(
        issuer=_display_name(_parse_name(issuer_value), issuer=True),
        subject=_display_name(_parse_name(subject_value)),
        not_after=not_after,
        sans=tuple(sans),
    )


def parse_target(value: str) -> Tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise argparse.ArgumentTypeError("target must be HOST:PORT")
        host, port_text = value[1:closing], value[closing + 2 :]
    else:
        if ":" not in value:
            raise argparse.ArgumentTypeError("target must be HOST:PORT")
        host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target port must be an integer") from exc
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("target must contain a host and port 1..65535")
    return host, port


def _request_bytes(hostname: str) -> bytes:
    ascii_hostname = hostname.encode("idna").decode("ascii")
    return (
        f"GET / HTTP/1.1\r\nHost: {ascii_hostname}\r\n"
        "User-Agent: ingress-acceptance/1\r\nAccept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")


def probe_https_get(
    target: Tuple[str, int], hostname: str, context: ssl.SSLContext, timeout: float
) -> Tuple[StatusObservation, Optional[bytes]]:
    raw_socket: Optional[socket.socket] = None
    tls_socket: Optional[ssl.SSLSocket] = None
    certificate: Optional[bytes] = None
    try:
        raw_socket = socket.create_connection(target, timeout=timeout)
        raw_socket.settimeout(timeout)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
        tls_socket.settimeout(timeout)
        certificate = tls_socket.getpeercert(binary_form=True)
        tls_socket.sendall(_request_bytes(hostname))
        response = http.client.HTTPResponse(tls_socket, method="GET")
        response.begin()
        status = response.status
        response.close()
        return StatusObservation(status=status), certificate
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        return StatusObservation(error=f"{type(exc).__name__}: {exc}"), certificate
    finally:
        if tls_socket is not None:
            tls_socket.close()
        elif raw_socket is not None:
            raw_socket.close()


def probe_tls_certificate(
    target: Tuple[str, int], hostname: str, context: ssl.SSLContext, timeout: float
) -> Tuple[Optional[bytes], Optional[str]]:
    raw_socket: Optional[socket.socket] = None
    tls_socket: Optional[ssl.SSLSocket] = None
    try:
        raw_socket = socket.create_connection(target, timeout=timeout)
        raw_socket.settimeout(timeout)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
        tls_socket.settimeout(timeout)
        return tls_socket.getpeercert(binary_form=True), None
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if tls_socket is not None:
            tls_socket.close()
        elif raw_socket is not None:
            raw_socket.close()


def probe_http_redirect(
    target: Tuple[str, int], hostname: str, timeout: float
) -> RedirectObservation:
    plain_socket: Optional[socket.socket] = None
    try:
        plain_socket = socket.create_connection(target, timeout=timeout)
        plain_socket.settimeout(timeout)
        plain_socket.sendall(_request_bytes(hostname))
        response = http.client.HTTPResponse(plain_socket, method="GET")
        response.begin()
        result = RedirectObservation(
            status=response.status, location=response.getheader("Location")
        )
        response.close()
        return result
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        return RedirectObservation(error=f"{type(exc).__name__}: {exc}")
    finally:
        if plain_socket is not None:
            plain_socket.close()


def read_access_log(path: Path, hostnames: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
    wanted = {hostname.lower().rstrip(".") for hostname in hostnames}
    routers: Dict[str, str] = {}
    errors: List[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"access log line {line_number}: invalid JSON: {exc.msg}")
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"access log line {line_number}: JSON value is not an object")
                    continue
                request_host = entry.get("RequestHost")
                if not isinstance(request_host, str):
                    continue
                normalized_host = request_host.rsplit(":", 1)[0].lower().rstrip(".")
                if normalized_host not in wanted:
                    continue
                router_name = entry.get("RouterName")
                routers[normalized_host] = (
                    router_name if isinstance(router_name, str) else "<missing RouterName>"
                )
    except OSError as exc:
        errors.append(f"cannot read access log {path}: {exc}")
    return routers, errors


def collect_observations(
    baseline: Baseline,
    target: Tuple[str, int],
    http_target: Tuple[str, int],
    access_log: Optional[Path],
    insecure: bool,
    timeout: float,
) -> Observations:
    observations = Observations()
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    certificate_der: Dict[str, bytes] = {}

    # One loop and no retries guarantees exactly one HTTPS GET per status hostname.
    for hostname in baseline.statuses:
        status, certificate = probe_https_get(target, hostname, context, timeout)
        observations.statuses[hostname] = status
        if certificate:
            certificate_der[hostname] = certificate

    # Status hosts are never retried: in particular, an expected 000ERR may consume
    # one timeout during its sole GET, but it must never consume a second timeout.
    for hostname in baseline.tls_only_hostnames():
        certificate, error = probe_tls_certificate(target, hostname, context, timeout)
        if certificate:
            certificate_der[hostname] = certificate
        elif error:
            observations.collection_errors.append(f"certificate probe {hostname}: {error}")

    for hostname, certificate in certificate_der.items():
        observations.fingerprints[hostname] = hashlib.sha256(certificate).hexdigest()
    for hostname in baseline.certificates:
        certificate = certificate_der.get(hostname)
        if certificate is None:
            continue
        try:
            observations.certificates[hostname] = decode_certificate_identity(certificate)
        except (ValueError, UnicodeError) as exc:
            observations.collection_errors.append(
                f"cannot decode certificate for {hostname}: {exc}"
            )

    first_hostname = next(iter(baseline.statuses))
    observations.redirect = probe_http_redirect(http_target, first_hostname, timeout)

    if access_log is not None:
        routers, errors = read_access_log(access_log, baseline.statuses)
        observations.routers.update(routers)
        observations.collection_errors.extend(errors)
    return observations


def render_human(evaluation: Evaluation) -> str:
    rows = [("CHECK", "HOST", "EXPECTED", "OBSERVED", "RESULT")]
    rows.extend(
        (check.check, check.hostname, check.expected, check.observed, check.outcome.upper())
        for check in evaluation.checks
    )
    widths = [max(len(row[index]) for row in rows) for index in range(5)]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    ]
    lines.append(evaluation.summary)
    return "\n".join(lines)


def render_json(evaluation: Evaluation) -> str:
    return json.dumps(
        {
            "passed": evaluation.ok,
            "counts": evaluation.counts,
            "summary": evaluation.summary,
            "checks": [
                {
                    "check": check.check,
                    "hostname": check.hostname,
                    "expected": check.expected,
                    "observed": check.observed,
                    "outcome": check.outcome,
                }
                for check in evaluation.checks
            ],
        },
        sort_keys=True,
    )


def run_selftest() -> int:
    failures: List[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    sample = parse_baseline_text(SAMPLE_BASELINE)
    check(
        sample.statuses
        == {
            "anime.saldivar.io": "401",
            "bookrequests.saldivar.io": "200",
            "drive.saldivar.io": "302",
            "dungeon.saldivar.io": "000ERR",
            "tautulli.saldivar.io": "303",
            "traefik.saldivar.io": "401",
        },
        "sample status parsing",
    )
    check(
        sample.certificates.get("immich.saldivar.io")
        == CertificateIdentity(
            "Let's Encrypt / YR1",
            "saldivar.io",
            "Sep 16 02:41:50 2026 GMT",
            ("*.saldivar.io", "saldivar.io"),
        ),
        "sample certificate parsing",
    )

    basic = Baseline({"auth.example": "401", "dead.example": "000ERR"}, {})
    all_match = evaluate(
        basic,
        Observations(
            statuses={
                "auth.example": StatusObservation(status=401),
                "dead.example": StatusObservation(error="connection reset"),
            }
        ),
        require_redirect=False,
    )
    check(all_match.ok and all_match.counts["drifted"] == 0, "all codes match")

    auth_drift = evaluate(
        Baseline({"auth.example": "401"}, {}),
        Observations(statuses={"auth.example": StatusObservation(status=200)}),
        require_redirect=False,
    )
    check(
        not auth_drift.ok
        and auth_drift.counts["drifted"] == 1
        and "auth.example" in render_human(auth_drift),
        "401 becoming 200 is named drift",
    )

    dead_live = evaluate(
        Baseline({"dead.example": "000ERR"}, {}),
        Observations(statuses={"dead.example": StatusObservation(status=200)}),
        require_redirect=False,
    )
    check(not dead_live.ok and dead_live.counts["drifted"] == 1, "000ERR became live")

    dead_still_dead = evaluate(
        Baseline({"dead.example": "000ERR"}, {}),
        Observations(statuses={"dead.example": StatusObservation(error="timed out")}),
        require_redirect=False,
    )
    check(dead_still_dead.ok, "000ERR connection failure passes")

    router_base = Baseline({"plex.example": "200"}, {})
    router_docker = evaluate(
        router_base,
        Observations(
            statuses={"plex.example": StatusObservation(status=200)},
            routers={"plex.example": "plex@docker"},
        ),
        require_routers=True,
        require_redirect=False,
    )
    router_file = evaluate(
        router_base,
        Observations(
            statuses={"plex.example": StatusObservation(status=200)},
            routers={"plex.example": "k8s-plex@file"},
        ),
        require_routers=True,
        require_redirect=False,
    )
    check(not router_docker.ok and router_file.ok, "router provider enforcement")

    missing = evaluate(
        Baseline({"present.example": "200", "missing.example": "200"}, {}),
        Observations(statuses={"present.example": StatusObservation(status=200)}),
        require_redirect=False,
    )
    check(missing.counts["missing"] == 1, "absent hostname counted missing")

    fingerprint_base = Baseline({"cert.example": "200"}, {})
    fingerprint_match = evaluate(
        fingerprint_base,
        Observations(
            statuses={"cert.example": StatusObservation(status=200)},
            fingerprints={"cert.example": "AA:BB"},
        ),
        expected_fingerprint="aabb",
        require_redirect=False,
    )
    fingerprint_drift = evaluate(
        fingerprint_base,
        Observations(
            statuses={"cert.example": StatusObservation(status=200)},
            fingerprints={"cert.example": "ccdd"},
        ),
        expected_fingerprint="AA:BB",
        require_redirect=False,
    )
    check(fingerprint_match.ok and not fingerprint_drift.ok, "fingerprint comparison")

    identity = CertificateIdentity("Issuer / CA", "example", "soon", ("example",))
    certificate_match = evaluate(
        Baseline({"example": "200"}, {"example": identity}),
        Observations(
            statuses={"example": StatusObservation(status=200)},
            certificates={"example": identity},
        ),
        require_redirect=False,
    )
    certificate_drift = evaluate(
        Baseline({"example": "200"}, {"example": identity}),
        Observations(
            statuses={"example": StatusObservation(status=200)},
            certificates={
                "example": CertificateIdentity("Other / CA", "example", "soon", ("example",))
            },
        ),
        require_redirect=False,
    )
    check(certificate_match.ok and not certificate_drift.ok, "certificate identity comparison")

    redirect_base = Baseline({"first.example": "200"}, {})
    redirect_ok = evaluate(
        redirect_base,
        Observations(
            statuses={"first.example": StatusObservation(status=200)},
            redirect=RedirectObservation(301, "https://first.example/"),
        ),
    )
    redirect_wrong = evaluate(
        redirect_base,
        Observations(
            statuses={"first.example": StatusObservation(status=200)},
            redirect=RedirectObservation(301, "https://other.example/"),
        ),
    )
    check(redirect_ok.ok and not redirect_wrong.ok, "HTTP redirect comparison")

    check(
        sample.probe_hostnames().count("immich.saldivar.io") == 1
        and len(sample.probe_hostnames()) == 7,
        "unique probe plan",
    )
    overlap = Baseline(
        {"dead.example": "000ERR"}, {"dead.example": sample.certificates["immich.saldivar.io"]}
    )
    check(overlap.tls_only_hostnames() == [], "000ERR host is never retried")
    check(
        _format_certificate_time(0x17, b"260916024150Z")
        == "Sep 16 02:41:50 2026 GMT",
        "certificate time formatting",
    )

    if failures:
        for failure in failures:
            print(f"SELFTEST FAIL: {failure}")
        return 1
    print("SELFTEST OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Traefik ingress responses with a recorded baseline. Run this only "
            "on minas-tirith: hairpin NAT makes off-host probing invalid. A 200 is NEVER "
            "inherently successful; every status must exactly match its baseline."
        )
    )
    parser.add_argument("--baseline", type=Path, help="baseline file (required normally)")
    parser.add_argument(
        "--target", type=parse_target, default=parse_target("127.0.0.1:443"), metavar="HOST:PORT"
    )
    parser.add_argument(
        "--http-target",
        type=parse_target,
        default=parse_target("127.0.0.1:80"),
        metavar="HOST:PORT",
    )
    parser.add_argument("--access-log", type=Path, help="optional Traefik JSON access log")
    parser.add_argument(
        "--expect-fingerprint", metavar="SHA256HEX", help="required leaf SHA256 for every SNI"
    )
    parser.add_argument(
        "--insecure", action="store_true", help="skip certificate chain validation"
    )
    parser.add_argument("--timeout", type=float, default=10.0, metavar="SECS")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--selftest", action="store_true", help="run offline hermetic tests")
    return parser


def fatal_evaluation(message: str) -> Evaluation:
    return Evaluation((CheckResult("fatal", "-", "successful run", message, "errors"),))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.baseline is None:
        parser.error("--baseline is required unless --selftest is used")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.expect_fingerprint is not None:
        normalized = normalize_fingerprint(args.expect_fingerprint)
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            parser.error("--expect-fingerprint must be a SHA256 hex digest")

    try:
        baseline = parse_baseline_text(args.baseline.read_text(encoding="utf-8"))
        observations = collect_observations(
            baseline,
            args.target,
            args.http_target,
            args.access_log,
            args.insecure,
            args.timeout,
        )
        evaluation = evaluate(
            baseline,
            observations,
            expected_fingerprint=args.expect_fingerprint,
            require_routers=args.access_log is not None,
        )
    except (OSError, ValueError) as exc:
        evaluation = fatal_evaluation(f"{type(exc).__name__}: {exc}")

    print(render_json(evaluation) if args.json else render_human(evaluation))
    return 0 if evaluation.ok else 1


if __name__ == "__main__":
    sys.exit(main())
