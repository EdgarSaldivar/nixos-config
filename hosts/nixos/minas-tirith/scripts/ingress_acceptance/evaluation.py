"""Pure evaluation of ingress observations against a baseline."""

from __future__ import annotations

import datetime
import re
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from .models import (
    MONITORING_CERTIFICATE_ISSUER_ORGANIZATION,
    MONITORING_CERTIFICATE_SANS,
    MONITORING_CERTIFICATE_SUBJECT,
    Baseline,
    CertificateIdentity,
    CheckResult,
    Evaluation,
    Observations,
    RedirectObservation,
)


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


def parse_certificate_time(value: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.strptime(value, "%b %d %H:%M:%S %Y GMT")
    except ValueError as exc:
        raise ValueError(f"invalid certificate notAfter {value!r}") from exc
    return parsed.replace(tzinfo=datetime.timezone.utc)


def _issuer_organization(value: str) -> str:
    return value.split(" / ", 1)[0]


def _canonical_organization(value: str) -> str:
    # Certificate renderers disagree about the apostrophe in Let's Encrypt.
    return re.sub(r"[^a-z0-9]", "", value.lower())


def monitoring_certificate_matches(
    actual: CertificateIdentity,
    minimum_days: int,
    now: datetime.datetime,
) -> Tuple[bool, str, str]:
    """Compare stable wildcard identity while allowing ordinary LE renewal.

    The leaf expiry and the intermediate common name deliberately are not stable
    identity: both change during healthy automatic renewal/chain rotation.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = now.astimezone(datetime.timezone.utc)
    expires = parse_certificate_time(actual.not_after)
    remaining = expires - now
    expected_stable = (
        _canonical_organization(MONITORING_CERTIFICATE_ISSUER_ORGANIZATION),
        MONITORING_CERTIFICATE_SUBJECT,
        MONITORING_CERTIFICATE_SANS,
    )
    actual_stable = (
        _canonical_organization(_issuer_organization(actual.issuer)),
        actual.subject,
        frozenset(actual.sans),
    )
    expected_text = (
        f"issuer organization={MONITORING_CERTIFICATE_ISSUER_ORGANIZATION!r}, "
        f"subject={MONITORING_CERTIFICATE_SUBJECT!r}, "
        f"sans={sorted(MONITORING_CERTIFICATE_SANS)!r}, "
        f"valid for at least {minimum_days} days"
    )
    observed_text = (
        f"issuer organization={_issuer_organization(actual.issuer)!r}, "
        f"subject={actual.subject!r}, sans={sorted(actual.sans)!r}, "
        f"notAfter={actual.not_after!r}, remaining={remaining}"
    )
    return expected_stable == actual_stable and remaining >= datetime.timedelta(
        days=minimum_days
    ), expected_text, observed_text


def evaluate(
    baseline: Baseline,
    observations: Observations,
    *,
    expected_fingerprint: Optional[str] = None,
    require_routers: bool = False,
    require_redirect: bool = True,
    require_dns: bool = False,
    monitoring_certificate_minimum_days: Optional[int] = None,
    now: Optional[datetime.datetime] = None,
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

    # A status code alone is not enough to prove an authentication gate. Radarr and
    # Prowlarr, for example, also return 302 from their native login flows. Baselines
    # may therefore name the required HTTPS redirect host as a third column.
    for hostname, expected_redirect_host in baseline.redirect_hosts.items():
        observed = observations.statuses.get(hostname)
        expected = f"3xx https://{expected_redirect_host}"
        if observed is None:
            checks.append(
                CheckResult("https-redirect", hostname, expected, "absent", "missing")
            )
        elif observed.error:
            checks.append(
                CheckResult(
                    "https-redirect", hostname, expected, observed.error, "errors"
                )
            )
        else:
            redirect = RedirectObservation(
                status=observed.status,
                location=observed.location,
            )
            actual = f"{observed.status} {observed.location or ''}".rstrip()
            outcome = (
                "matched"
                if redirect_matches(expected_redirect_host, redirect)
                else "drifted"
            )
            checks.append(
                CheckResult("https-redirect", hostname, expected, actual, outcome)
            )

    for hostname, expected in baseline.certificates.items():
        actual = observations.certificates.get(hostname)
        if actual is None:
            checks.append(
                CheckResult("certificate", hostname, expected.display(), "absent", "missing")
            )
        elif monitoring_certificate_minimum_days is not None:
            try:
                matches, expected_text, actual_text = monitoring_certificate_matches(
                    actual,
                    monitoring_certificate_minimum_days,
                    now or datetime.datetime.now(datetime.timezone.utc),
                )
            except ValueError as exc:
                checks.append(
                    CheckResult(
                        "certificate-monitor",
                        hostname,
                        "valid certificate identity and expiry",
                        str(exc),
                        "errors",
                    )
                )
                continue
            checks.append(
                CheckResult(
                    "certificate-monitor",
                    hostname,
                    expected_text,
                    actual_text,
                    "matched" if matches else "drifted",
                )
            )
        else:
            outcome = "matched" if actual == expected else "drifted"
            checks.append(
                CheckResult(
                    "certificate", hostname, expected.display(), actual.display(), outcome
                )
            )

    if require_dns:
        for hostname in baseline.probe_hostnames():
            resolution = observations.resolutions.get(hostname)
            if resolution is None:
                checks.append(
                    CheckResult("dns", hostname, "resolvable", "absent", "missing")
                )
            elif resolution.error:
                checks.append(
                    CheckResult("dns", hostname, "resolvable", resolution.error, "errors")
                )
            elif not resolution.addresses:
                checks.append(
                    CheckResult(
                        "dns", hostname, "resolvable", "no addresses", "errors"
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "dns",
                        hostname,
                        "resolvable",
                        ",".join(resolution.addresses),
                        "matched",
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
