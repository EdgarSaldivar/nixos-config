"""Command-line and offline selftest orchestration."""

from __future__ import annotations

import argparse
import datetime
import re
from pathlib import Path
from typing import List, Optional

from .certificate import _format_certificate_time
from .evaluation import evaluate, normalize_fingerprint
from .models import (
    MONITORING_CERTIFICATE_MINIMUM_DAYS,
    SAMPLE_BASELINE,
    Baseline,
    CertificateIdentity,
    CheckResult,
    Evaluation,
    Observations,
    RedirectObservation,
    ResolutionObservation,
    StatusObservation,
    parse_baseline_text,
)
from .network import collect_observations, connection_target, parse_target
from .rendering import render_human, render_json


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

    auth_redirect_base = parse_baseline_text(
        "admin.example 302 auth.example\n"
    )
    check(
        auth_redirect_base.redirect_hosts == {"admin.example": "auth.example"},
        "HTTPS redirect-host parsing",
    )
    auth_redirect_ok = evaluate(
        auth_redirect_base,
        Observations(
            statuses={
                "admin.example": StatusObservation(
                    status=302,
                    location="https://auth.example/application/o/authorize/",
                )
            }
        ),
        require_redirect=False,
    )
    auth_redirect_wrong = evaluate(
        auth_redirect_base,
        Observations(
            statuses={
                "admin.example": StatusObservation(
                    status=302,
                    location="https://admin.example/login",
                )
            }
        ),
        require_redirect=False,
    )
    check(
        auth_redirect_ok.ok and not auth_redirect_wrong.ok,
        "HTTPS authentication redirect-host enforcement",
    )
    try:
        parse_baseline_text("admin.example 200 auth.example\n")
    except ValueError:
        pass
    else:
        failures.append("non-3xx redirect-host baseline rejected")

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

    monitoring_expected = CertificateIdentity(
        "Let's Encrypt / YR1",
        "saldivar.io",
        "Sep 16 02:41:50 2026 GMT",
        ("*.saldivar.io", "saldivar.io"),
    )
    renewed = CertificateIdentity(
        "Lets Encrypt / R13",
        "*.saldivar.io",
        "Dec 20 02:41:50 2026 GMT",
        ("saldivar.io", "*.saldivar.io"),
    )
    monitoring_base = Baseline(
        {"cert.example": "200"}, {"cert.example": monitoring_expected}
    )
    monitoring_observations = Observations(
        statuses={"cert.example": StatusObservation(status=200)},
        certificates={"cert.example": renewed},
    )
    monitor_renewed = evaluate(
        monitoring_base,
        monitoring_observations,
        require_redirect=False,
        monitoring_certificate_minimum_days=45,
        now=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
    )
    strict_renewed = evaluate(
        monitoring_base,
        monitoring_observations,
        require_redirect=False,
    )
    check(
        monitor_renewed.ok,
        "monitor accepts renewal with alternate subject CN and intermediate rotation",
    )
    check(not strict_renewed.ok, "strict certificate identity still detects renewal")

    expires_too_soon = CertificateIdentity(
        renewed.issuer,
        renewed.subject,
        "Sep 20 00:00:00 2026 GMT",
        renewed.sans,
    )
    monitor_expiry = evaluate(
        monitoring_base,
        Observations(
            statuses={"cert.example": StatusObservation(status=200)},
            certificates={"cert.example": expires_too_soon},
        ),
        require_redirect=False,
        monitoring_certificate_minimum_days=45,
        now=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
    )
    check(not monitor_expiry.ok, "monitor rejects certificate below expiry minimum")

    for wrong_identity in (
        CertificateIdentity(
            "Other CA / Root", renewed.subject, renewed.not_after, renewed.sans
        ),
        CertificateIdentity(renewed.issuer, renewed.subject, renewed.not_after, ("saldivar.io",)),
    ):
        wrong_monitor = evaluate(
            monitoring_base,
            Observations(
                statuses={"cert.example": StatusObservation(status=200)},
                certificates={"cert.example": wrong_identity},
            ),
            require_redirect=False,
            monitoring_certificate_minimum_days=45,
            now=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        )
        check(not wrong_monitor.ok, "monitor rejects wildcard identity drift")

    dns_base = Baseline({"live.example": "200", "dead.example": "000ERR"}, {})
    dns_ok = evaluate(
        dns_base,
        Observations(
            statuses={
                "live.example": StatusObservation(status=200),
                "dead.example": StatusObservation(error="timed out"),
            },
            resolutions={
                "live.example": ResolutionObservation(("203.0.113.10",)),
                "dead.example": ResolutionObservation(("203.0.113.10",)),
            },
        ),
        require_redirect=False,
        require_dns=True,
    )
    dns_dead_missing = evaluate(
        dns_base,
        Observations(
            statuses={
                "live.example": StatusObservation(status=200),
                "dead.example": StatusObservation(error="Name or service not known"),
            },
            resolutions={
                "live.example": ResolutionObservation(("203.0.113.10",)),
                "dead.example": ResolutionObservation(error="gaierror: NXDOMAIN"),
            },
        ),
        require_redirect=False,
        require_dns=True,
    )
    check(dns_ok.ok, "public DNS observations pass, including expected-dead route")
    check(not dns_dead_missing.ok, "expected-dead route still requires public DNS")
    check(
        connection_target(None, "public.example", 443) == ("public.example", 443)
        and connection_target(("127.0.0.1", 8443), "public.example", 443)
        == ("127.0.0.1", 8443),
        "public and fixed connection target selection",
    )

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
            "Compare Traefik ingress responses with a recorded baseline. Fixed --target "
            "mode is for local cutover acceptance; --public-dns is for an external "
            "monitor and connects to every hostname through its public DNS. A 200 is "
            "NEVER inherently successful; every status must exactly match its baseline."
        )
    )
    parser.add_argument("--baseline", type=Path, help="baseline file (required normally)")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--target",
        type=parse_target,
        metavar="HOST:PORT",
        help="fixed HTTPS target (default: 127.0.0.1:443)",
    )
    target_group.add_argument(
        "--public-dns",
        action="store_true",
        help="resolve and connect to each baseline hostname publicly",
    )
    parser.add_argument(
        "--http-target",
        type=parse_target,
        metavar="HOST:PORT",
        help="fixed HTTP redirect target (default: 127.0.0.1:80)",
    )
    parser.add_argument("--access-log", type=Path, help="optional Traefik JSON access log")
    parser.add_argument(
        "--expect-fingerprint", metavar="SHA256HEX", help="required leaf SHA256 for every SNI"
    )
    parser.add_argument(
        "--insecure", action="store_true", help="skip certificate chain validation"
    )
    parser.add_argument(
        "--monitor-certificate",
        action="store_true",
        help=(
            "allow subject-CN/intermediate rotation while requiring verified TLS hostname, "
            "the canonical Lets Encrypt issuer organization, the exact wildcard SAN set, "
            f"and at least {MONITORING_CERTIFICATE_MINIMUM_DAYS} days validity"
        ),
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
    if args.public_dns and args.http_target is not None:
        parser.error("--http-target cannot be combined with --public-dns")
    if args.monitor_certificate and args.insecure:
        parser.error("--monitor-certificate cannot be combined with --insecure")
    if args.expect_fingerprint is not None:
        normalized = normalize_fingerprint(args.expect_fingerprint)
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            parser.error("--expect-fingerprint must be a SHA256 hex digest")

    try:
        baseline = parse_baseline_text(args.baseline.read_text(encoding="utf-8"))
        if args.monitor_certificate and not baseline.certificates:
            raise ValueError("--monitor-certificate requires a certificate baseline entry")
        target = args.target
        http_target = args.http_target
        if not args.public_dns:
            target = target or parse_target("127.0.0.1:443")
            http_target = http_target or parse_target("127.0.0.1:80")
        observations = collect_observations(
            baseline,
            target,
            http_target,
            args.access_log,
            args.insecure,
            args.timeout,
            public_dns=args.public_dns,
        )
        evaluation = evaluate(
            baseline,
            observations,
            expected_fingerprint=args.expect_fingerprint,
            require_routers=args.access_log is not None,
            require_dns=args.public_dns,
            monitoring_certificate_minimum_days=(
                MONITORING_CERTIFICATE_MINIMUM_DAYS if args.monitor_certificate else None
            ),
        )
    except (OSError, ValueError) as exc:
        evaluation = fatal_evaluation(f"{type(exc).__name__}: {exc}")

    print(render_json(evaluation) if args.json else render_human(evaluation))
    return 0 if evaluation.ok else 1
