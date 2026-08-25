#!/usr/bin/env python3
"""Offline characterization tests for the modular ingress acceptance tool."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from ingress_acceptance import certificate, cli, evaluation, models, network, rendering


EXPECTED_MODULES = (
    "ingress_acceptance.certificate",
    "ingress_acceptance.cli",
    "ingress_acceptance.evaluation",
    "ingress_acceptance.models",
    "ingress_acceptance.network",
    "ingress_acceptance.rendering",
)


def _tlv(tag: int, value: bytes) -> bytes:
    if len(value) < 128:
        length = bytes((len(value),))
    else:
        encoded_length = len(value).to_bytes((len(value).bit_length() + 7) // 8, "big")
        length = bytes((0x80 | len(encoded_length),)) + encoded_length
    return bytes((tag,)) + length + value


def _name(*attributes: tuple[bytes, str]) -> bytes:
    encoded = b""
    for oid, text in attributes:
        encoded += _tlv(0x31, _tlv(0x30, _tlv(0x06, oid) + _tlv(0x0C, text.encode())))
    return _tlv(0x30, encoded)


def _certificate_fixture() -> bytes:
    issuer = _name(
        (b"\x55\x04\x0a", "Let's Encrypt"),
        (b"\x55\x04\x03", "R3"),
    )
    subject = _name((b"\x55\x04\x03", "saldivar.io"))
    validity = _tlv(
        0x30,
        _tlv(0x17, b"260818024150Z") + _tlv(0x17, b"261116024150Z"),
    )
    general_names = _tlv(
        0x30,
        _tlv(0x82, b"*.saldivar.io") + _tlv(0x82, b"saldivar.io"),
    )
    san_extension = _tlv(
        0x30,
        _tlv(0x06, b"\x55\x1d\x11") + _tlv(0x04, general_names),
    )
    extensions = _tlv(0xA3, _tlv(0x30, san_extension))
    tbs_certificate = _tlv(
        0x30,
        _tlv(0x02, b"\x01")
        + _tlv(0x30, b"")
        + issuer
        + validity
        + subject
        + _tlv(0x30, b"")
        + extensions,
    )
    return _tlv(0x30, tbs_certificate)


class IngressAcceptanceCharacterization(unittest.TestCase):
    def test_all_cohesive_modules_are_importable(self) -> None:
        imported = tuple(importlib.import_module(name).__name__ for name in EXPECTED_MODULES)
        self.assertEqual(imported, EXPECTED_MODULES)
        self.assertEqual(models.parse_baseline_text.__module__, "ingress_acceptance.models")
        self.assertEqual(evaluation.evaluate.__module__, "ingress_acceptance.evaluation")
        self.assertEqual(
            certificate.decode_certificate_identity.__module__,
            "ingress_acceptance.certificate",
        )
        self.assertEqual(network.collect_observations.__module__, "ingress_acceptance.network")
        self.assertEqual(rendering.render_json.__module__, "ingress_acceptance.rendering")

    def test_baseline_parsing_and_validation(self) -> None:
        baseline = models.parse_baseline_text(
            "admin.example 302 auth.example.\n"
            "dead.example 000ERR\n"
            "  cert.example: {\"issuer\": \"CA\", \"subject\": \"example\", "
            "\"notAfter\": \"Nov 16 02:41:50 2026 GMT\", \"sans\": [\"example\"]}\n"
        )
        self.assertEqual(
            baseline.statuses,
            {"admin.example": "302", "dead.example": "000ERR"},
        )
        self.assertEqual(baseline.redirect_hosts, {"admin.example": "auth.example"})
        self.assertEqual(baseline.probe_hostnames(), ["admin.example", "dead.example", "cert.example"])
        self.assertEqual(baseline.reachable_hostnames(), ["admin.example", "cert.example"])
        with self.assertRaisesRegex(ValueError, "redirect hostname requires a 3xx status"):
            models.parse_baseline_text("admin.example 200 auth.example\n")
        with self.assertRaisesRegex(ValueError, "duplicate hostname"):
            models.parse_baseline_text("same.example 200\nsame.example 200\n")

    def test_pure_evaluation_preserves_check_order_and_outcomes(self) -> None:
        baseline = models.Baseline(
            {"auth.example": "401", "dead.example": "000ERR"},
            {},
        )
        result = evaluation.evaluate(
            baseline,
            models.Observations(
                statuses={
                    "auth.example": models.StatusObservation(status=200),
                    "dead.example": models.StatusObservation(error="timed out"),
                },
                collection_errors=["fixture collection error"],
            ),
            require_redirect=False,
        )
        self.assertEqual(
            [(check.check, check.hostname, check.outcome) for check in result.checks],
            [
                ("https", "auth.example", "drifted"),
                ("https", "dead.example", "matched"),
                ("collection", "-", "errors"),
            ],
        )
        self.assertEqual(
            result.counts,
            {"matched": 1, "drifted": 1, "missing": 0, "errors": 1},
        )
        self.assertFalse(result.ok)

    def test_certificate_der_decoding(self) -> None:
        identity = certificate.decode_certificate_identity(_certificate_fixture())
        self.assertEqual(
            identity,
            models.CertificateIdentity(
                "Let's Encrypt / R3",
                "saldivar.io",
                "Nov 16 02:41:50 2026 GMT",
                ("*.saldivar.io", "saldivar.io"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "truncated DER value"):
            certificate.decode_certificate_identity(b"\x30\x05\x30")

    def test_human_and_json_rendering_are_characterized(self) -> None:
        result = models.Evaluation(
            (models.CheckResult("https", "a.example", "200", "401", "drifted"),)
        )
        self.assertEqual(
            rendering.render_human(result),
            "CHECK  HOST       EXPECTED  OBSERVED  RESULT\n"
            "https  a.example  200       401       DRIFTED\n"
            "matched=0 drifted=1 missing=0 errors=0",
        )
        rendered = rendering.render_json(result)
        self.assertEqual(
            rendered,
            '{"checks": [{"check": "https", "expected": "200", '
            '"hostname": "a.example", "observed": "401", "outcome": "drifted"}], '
            '"counts": {"drifted": 1, "errors": 0, "matched": 0, "missing": 0}, '
            '"passed": false, "summary": "matched=0 drifted=1 missing=0 errors=0"}',
        )
        self.assertEqual(
            list(json.loads(rendered)),
            ["checks", "counts", "passed", "summary"],
        )

    def test_cli_fatal_paths_do_not_probe_network(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        missing = SCRIPTS_DIR / "tests" / "absent-ingress-baseline"
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(["--baseline", str(missing), "--json"])
        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["counts"], {"matched": 0, "drifted": 0, "missing": 0, "errors": 1})
        self.assertEqual(payload["checks"][0]["check"], "fatal")
        self.assertTrue(payload["checks"][0]["observed"].startswith("FileNotFoundError: "))

        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ingress-acceptance.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn(
            "ingress-acceptance.py: error: --baseline is required unless --selftest is used\n",
            completed.stderr,
        )

    def test_existing_selftest_output_and_exit(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(["--selftest"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "SELFTEST OK\n")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
