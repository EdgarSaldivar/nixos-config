#!/usr/bin/env python3
"""Offline tests for the terracompute forced-command target probe."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


PROBE_PATH = Path(__file__).parents[1] / "target" / "terracompute-probe.py"
SPEC = importlib.util.spec_from_file_location("terracompute_probe", PROBE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)

NOW = dt.datetime(2026, 9, 14, 12, 34, 56, tzinfo=dt.timezone.utc)
BOOT_ID = "11111111-2222-4333-8444-555555555555"


def gpu_uuid(index: int) -> str:
    return f"GPU-00000000-0000-4000-8000-{index:012x}"


def gpu_output(count: int = 8) -> str:
    return "".join(
        f"{gpu_uuid(index)}, 00000000:{0x20 + index:02X}:00.0, NVIDIA RTX Test, {40 + index}, P8\n"
        for index in range(count)
    )


def ok(stdout: str = "") -> object:
    return probe.CommandResult(0, stdout=stdout)


def pci_gpus(count: int = 8, *, vfio: set[int] | None = None) -> list[dict[str, str]]:
    vfio = vfio or set()
    return [
        {
            "pci_bdf": f"0000:{0x20 + index:02x}:00.0",
            "driver": "vfio-pci" if index in vfio else "nvidia",
        }
        for index in range(count)
    ]


class FixtureRunner:
    def __init__(self, replacements: dict[str, object] | None = None) -> None:
        self.calls: list[str] = []
        self.results = {
            "gpu": ok(gpu_output()),
            "kernel_journal": ok(),
            "docker_journal": ok(),
            "service_vastai": ok("active\n"),
            "service_docker": ok("active\n"),
            "service_nvidia_persistenced": ok("active\n"),
            "docker_metadata": ok('"tenant-a"\t"Up 2 hours"\n'),
        }
        self.results.update(replacements or {})

    def __call__(self, command_id: str) -> object:
        self.calls.append(command_id)
        return self.results[command_id]


def collect(
    runner: FixtureRunner, pci_inventory: list[dict[str, str]] | None = None
) -> dict[str, object]:
    return probe.collect_probe(
        runner=runner,
        hostname_reader=lambda: "Terracompute.Example.",
        boot_id_reader=lambda: BOOT_ID,
        pci_gpu_reader=lambda: pci_gpus() if pci_inventory is None else pci_inventory,
        now=NOW,
    )


def events(result: dict[str, object], family: str) -> list[dict[str, object]]:
    return [event for event in result["events"] if event["fault_family"] == family]


class TargetProbeTests(unittest.TestCase):
    def test_healthy_eight_gpu_result(self) -> None:
        runner = FixtureRunner()
        result = collect(runner)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["target"], "terracompute.example")
        self.assertEqual(result["machine_id"], 17049)
        self.assertEqual(result["boot_id"], BOOT_ID)
        self.assertEqual(result["observed_at"], "2026-09-14T12:34:56Z")
        self.assertEqual(len(result["snapshot"]["gpu"]["gpus"]), 8)
        self.assertEqual(result["events"], [])
        self.assertEqual(
            runner.calls,
            [
                "gpu",
                "kernel_journal",
                "docker_journal",
                "service_vastai",
                "service_docker",
                "service_nvidia_persistenced",
                "docker_metadata",
            ],
        )

    def test_xid_79_extracts_bdf_and_corresponding_full_uuid(self) -> None:
        line = "NVRM: Xid (PCI:0000:23:00): 79, GPU has fallen off the bus."
        runner = FixtureRunner({"kernel_journal": ok(line + "\n" + line + "\n")})
        result = collect(runner)

        xid_event = events(result, "xid")[0]
        self.assertFalse(result["healthy"])
        self.assertEqual(xid_event["severity"], "critical")
        self.assertEqual(xid_event["count"], 1)
        self.assertEqual(xid_event["evidence"]["pci_bdf"], "0000:23:00.0")
        self.assertEqual(xid_event["evidence"]["uuid"], gpu_uuid(3))

    def test_aer_severity_classification(self) -> None:
        journal = "\n".join(
            (
                "pcieport 0000:00:01.0: AER: Corrected error received",
                "pcieport 0000:00:02.0: AER: Uncorrected (Non-Fatal) error received",
                "pcieport 0000:00:03.0: AER: Uncorrected (Fatal) error received",
            )
        )
        result = collect(FixtureRunner({"kernel_journal": ok(journal)}))
        severities = {event["code"]: event["severity"] for event in events(result, "aer")}
        self.assertEqual(
            severities,
            {
                "correctable": "correctable",
                "nonfatal": "nonfatal",
                "fatal": "fatal",
            },
        )

    def test_cdi_detection(self) -> None:
        result = collect(
            FixtureRunner(
                {
                    "docker_journal": ok(
                        "error setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=all\n"
                        "CDI device injection failed\n"
                    )
                }
            )
        )
        cdi_events = events(result, "cdi")
        self.assertFalse(result["healthy"])
        self.assertEqual(
            {event["code"] for event in cdi_events},
            {"device-missing", "injection-failed"},
        )

    def test_missing_gpu_and_timeout_fail_closed_without_exception_text(self) -> None:
        result = collect(
            FixtureRunner(
                {
                    "gpu": ok(gpu_output(7)),
                    "docker_journal": probe.CommandResult(
                        None, failure="timeout"
                    ),
                }
            )
        )
        self.assertFalse(result["healthy"])
        codes = {event["code"] for event in result["events"]}
        self.assertIn("gpu_driver_unavailable", codes)
        self.assertIn("docker_journal_timeout", codes)
        encoded = probe._encode_result(result)
        self.assertNotIn("Traceback", encoded)
        self.assertNotIn("exception", encoded.lower())

    def test_catalogued_argv_uses_devnull_and_no_shell(self) -> None:
        fake_process = mock.Mock()
        with mock.patch.object(probe.subprocess, "Popen", return_value=fake_process) as popen:
            returned = probe._start_process(probe.COMMANDS["gpu"])

        self.assertIs(returned, fake_process)
        popen.assert_called_once_with(
            list(probe.COMMANDS["gpu"].argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        for spec in probe.COMMANDS.values():
            self.assertIsInstance(spec.argv, tuple)
            self.assertTrue(spec.argv[0].startswith("/usr/bin/"))
            self.assertNotIn("sudo", spec.argv)
            self.assertFalse(any(token in spec.argv for token in ("exec", "inspect", "logs")))

    def test_event_and_json_output_caps(self) -> None:
        many_xids = "\n".join(
            f"NVRM: Xid (PCI:0000:23:00): {index}, synthetic"
            for index in range(1, 400)
        )
        many_containers = "".join(
            f'{json.dumps("n" * 300 + str(index))}\t{json.dumps("s" * 500)}\n'
            for index in range(400)
        )
        result = collect(
            FixtureRunner(
                {
                    "kernel_journal": ok(many_xids),
                    "docker_metadata": ok(many_containers),
                }
            )
        )
        encoded = probe._encode_result(result)
        self.assertLessEqual(len(result["events"]), probe.MAX_EVENTS)
        self.assertLess(len(encoded.encode("utf-8")), probe.MAX_JSON_OUTPUT_BYTES)
        self.assertEqual(result["events"][-1]["code"], "event_limit_reached")

        oversized = dict(result)
        oversized["events"] = [{"payload": "x" * probe.MAX_JSON_OUTPUT_BYTES}]
        fallback = json.loads(probe._encode_result(oversized))
        self.assertFalse(fallback["healthy"])
        self.assertEqual(fallback["events"][0]["code"], "json_output_limit_reached")

        repeated_xid_variants = "\n".join(
            f"NVRM: Xid (PCI:0000:23:00): 79, occurrence {index}"
            for index in range(probe.MAX_EVENT_COUNT + 50)
        )
        capped = probe._parse_kernel_events(
            repeated_xid_variants, {"0000:23:00.0": gpu_uuid(3)}
        )
        self.assertEqual(capped[0]["count"], probe.MAX_EVENT_COUNT)

    def test_docker_output_contains_only_allowed_metadata_fields(self) -> None:
        sensitive_words = {
            "env",
            "environment",
            "labels",
            "mounts",
            "logs",
            "command",
            "image",
            "ports",
        }
        result = collect(
            FixtureRunner(
                {
                    "docker_metadata": ok(
                        '"tenant-a"\t"Up 1 hour (healthy)"\n'
                        '"tenant-b"\t"Exited (0) 2 hours ago"\n'
                    )
                }
            )
        )
        docker_metadata = result["snapshot"]["docker"]
        container_keys = set().union(
            *(container.keys() for container in docker_metadata["containers"])
        )
        self.assertEqual(container_keys, {"name", "status"})
        self.assertTrue(container_keys.isdisjoint(sensitive_words))

    def test_vfio_assigned_gpu_is_not_reported_as_missing(self) -> None:
        result = collect(
            FixtureRunner({"gpu": ok(gpu_output(7))}),
            pci_gpus(vfio={7}),
        )
        self.assertTrue(result["healthy"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["snapshot"]["gpu"]["vfio_count"], 1)

    def test_unbound_gpu_is_reported_as_unavailable(self) -> None:
        inventory = pci_gpus()
        inventory[7]["driver"] = "unbound"
        result = collect(FixtureRunner({"gpu": ok(gpu_output(7))}), inventory)
        unavailable = next(
            event for event in result["events"] if event["code"] == "gpu_driver_unavailable"
        )
        self.assertFalse(result["healthy"])
        self.assertEqual(unavailable["evidence"]["driver"], "unbound")

    def test_main_emits_exactly_one_json_object_and_ignores_stdin(self) -> None:
        fixture_result = collect(FixtureRunner())
        with (
            mock.patch.object(probe, "collect_probe", return_value=fixture_result),
            mock.patch("sys.stdin", io.StringIO("malicious instruction")),
            mock.patch("sys.argv", [str(PROBE_PATH), "malicious instruction"]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            probe.main()
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), fixture_result)


if __name__ == "__main__":
    unittest.main()
