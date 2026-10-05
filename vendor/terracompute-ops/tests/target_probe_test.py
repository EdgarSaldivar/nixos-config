#!/usr/bin/env python3
"""Offline tests for the terracompute forced-command target probe."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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


def gpu_output(count: int = 8, *, absent_serial: set[int] | None = None) -> str:
    absent_serial = absent_serial or set()
    return "".join(
        f"{gpu_uuid(index)}, 00000000:{0x20 + index:02X}:00.0, "
        f"NVIDIA RTX Test, {40 + index}, P8, "
        f"{'N/A' if index in absent_serial else f'SERIAL-{index:04d}'}, "
        "550.90.07, 96.00.5E.00.01\n"
        for index in range(count)
    )


def ok(stdout: str = "") -> object:
    return probe.CommandResult(0, stdout=stdout)


def pci_gpus(
    count: int = 8, *, vfio: set[int] | None = None
) -> list[dict[str, object]]:
    vfio = vfio or set()
    return [
        {
            "pci_bdf": f"0000:{0x20 + index:02x}:00.0",
            "driver": "vfio-pci" if index in vfio else "nvidia",
            "pci_root_path": (
                f"/sys/devices/pci0000:00/0000:00:{index + 1:02x}.0/"
                f"0000:{0x20 + index:02x}:00.0"
            ),
            "numa_node": index % 2,
            "current_link_speed": "16.0 GT/s PCIe",
            "current_link_width": 16,
            "max_link_speed": "16.0 GT/s PCIe",
            "max_link_width": 16,
        }
        for index in range(count)
    ]


def system_identity() -> dict[str, dict[str, str]]:
    return {
        "motherboard": {
            "vendor": "Test Board Vendor",
            "name": "Test Board",
            "version": "1.0",
            "serial": "BOARD-0001",
        },
        "bios": {
            "vendor": "Test BIOS Vendor",
            "version": "1.2.3",
            "date": "09/14/2026",
        },
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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
    runner: FixtureRunner,
    pci_inventory: list[dict[str, object]] | None = None,
    *,
    identity: dict[str, dict[str, str]] | None = None,
    monotonic: object | None = None,
    handover: dict[str, dict[str, object]] | None = None,
    sleeps: list[float] | None = None,
) -> dict[str, object]:
    arguments = {
        "sleep": (sleeps.append if sleeps is not None else lambda _seconds: None),
        "gpu_handover_reader": lambda bdf: (handover or {}).get(
            bdf, {"audio_driver": "snd_hda_intel", "nvrm_registered": False}
        ),
        "runner": runner,
        "hostname_reader": lambda: "Terracompute.Example.",
        "boot_id_reader": lambda: BOOT_ID,
        "pci_gpu_reader": lambda: pci_gpus()
        if pci_inventory is None
        else pci_inventory,
        "system_identity_reader": lambda: system_identity()
        if identity is None
        else identity,
        "now": NOW,
    }
    if monotonic is not None:
        arguments["monotonic"] = monotonic
    return probe.collect_probe(
        **arguments,
    )


def events(result: dict[str, object], family: str) -> list[dict[str, object]]:
    return [event for event in result["events"] if event["fault_family"] == family]


@contextmanager
def admission_files(state: dict[str, object] | str):
    directory = Path(tempfile.mkdtemp())
    os.chmod(directory, 0o700)
    lock_file = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    state_file = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    try:
        lock_file.close()
        state_file.write(
            (state if isinstance(state, str) else json.dumps(state)).encode("ascii")
        )
        state_file.close()
        os.chmod(lock_file.name, 0o600)
        os.chmod(state_file.name, 0o600)
        with (
            mock.patch.object(probe, "OBSERVER_STATE_DIRECTORY", directory),
            mock.patch.object(
                probe, "ADMISSION_LOCK_FILENAME", Path(lock_file.name).name
            ),
            mock.patch.object(
                probe, "ADMISSION_STATE_FILENAME", Path(state_file.name).name
            ),
        ):
            yield Path(state_file.name)
    finally:
        for path in (lock_file.name, state_file.name):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            directory.rmdir()
        except PermissionError:
            # Some restricted test sandboxes permit file cleanup but retain the
            # now-empty temporary directory outside the worktree.
            pass


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
        self.assertEqual(result["snapshot"]["system_identity"], system_identity())
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

    def test_gpu_inventory_extends_fixed_query_and_marks_absent_serial_unknown(self) -> None:
        query = probe.COMMANDS["gpu"].argv[1]
        self.assertEqual(
            query,
            "--query-gpu=uuid,pci.bus_id,name,temperature.gpu,pstate,serial,driver_version,vbios_version",
        )

        result = collect(
            FixtureRunner({"gpu": ok(gpu_output(absent_serial={3}))})
        )
        gpu = result["snapshot"]["gpu"]["gpus"][3]
        self.assertEqual(gpu["uuid"], gpu_uuid(3))
        self.assertEqual(gpu["serial"], "unknown")
        self.assertEqual(gpu["driver_version"], "550.90.07")
        self.assertEqual(gpu["vbios_version"], "96.00.5E.00.01")

    def test_topology_and_fixed_system_identity_are_retained_without_guesses(self) -> None:
        inventory = pci_gpus()
        identity = system_identity()
        identity["motherboard"]["serial"] = "not specified"
        result = collect(FixtureRunner(), inventory, identity=identity)

        pci_device = result["snapshot"]["gpu"]["pci_devices"][0]
        self.assertEqual(
            set(pci_device),
            {
                "pci_bdf",
                "driver",
                "pci_root_path",
                "numa_node",
                "current_link_speed",
                "current_link_width",
                "max_link_speed",
                "max_link_width",
            },
        )
        self.assertEqual(pci_device["numa_node"], 0)
        self.assertEqual(pci_device["current_link_width"], 16)
        self.assertNotIn("port", pci_device)
        self.assertNotIn("psu", pci_device)
        self.assertEqual(
            result["snapshot"]["system_identity"]["motherboard"]["serial"],
            "unknown",
        )

    def test_fixed_sysfs_readers_collect_only_bounded_inventory_fields(self) -> None:
        values = {
            filename: f"{category}-{field}"
            for category, fields in probe._DMI_FIELDS.items()
            for field, filename in fields.items()
        }

        def fixed_read(path: Path, limit: int) -> str:
            self.assertEqual(limit, probe.MAX_INVENTORY_VALUE_CHARS)
            self.assertEqual(path.parent, probe.DMI_ID_PATH)
            return values[path.name]

        with mock.patch.object(
            probe, "_read_bounded_sysfs_text", side_effect=fixed_read
        ) as reader:
            identity = probe._read_system_identity()
        self.assertEqual(reader.call_count, 7)
        self.assertEqual(identity["motherboard"]["name"], "motherboard-name")
        self.assertEqual(identity["bios"]["version"], "bios-version")

        device = mock.Mock()
        device.resolve.return_value = Path(
            "/sys/devices/pci0000:00/0000:00:01.0/0000:20:00.0"
        )
        self.assertEqual(
            probe._pci_root_path(device, "0000:20:00.0"),
            "/sys/devices/pci0000:00/0000:00:01.0/0000:20:00.0",
        )
        device.resolve.assert_called_once_with(strict=True)

    def test_aggregate_deadline_emits_bounded_partial_evidence(self) -> None:
        clock = FakeClock()
        fixture = FixtureRunner()

        def deadline_runner(command_id: str) -> object:
            result = fixture(command_id)
            if command_id == "gpu":
                clock.advance(probe.COLLECTION_DEADLINE_SECONDS)
            return result

        result = collect(deadline_runner, monotonic=clock)
        deadline_event = next(
            event
            for event in result["events"]
            if event["code"] == "collection_deadline_exceeded"
        )
        self.assertFalse(result["healthy"])
        self.assertEqual(len(result["snapshot"]["gpu"]["gpus"]), 8)
        self.assertIsNone(result["snapshot"]["gpu"]["pci_count"])
        self.assertEqual(result["snapshot"]["services"]["vastai"], "unknown")
        self.assertEqual(fixture.calls, ["gpu"])
        self.assertIn("pci_gpu_inventory", deadline_event["evidence"]["skipped_collectors"])
        self.assertIn("gpu_inventory", deadline_event["evidence"]["completed_collectors"])
        encoded = probe._encode_result(result)
        self.assertLess(len(encoded.encode("utf-8")), probe.MAX_JSON_OUTPUT_BYTES)
        self.assertEqual(json.loads(encoded), result)

    def test_xid_79_extracts_bdf_and_corresponding_full_uuid(self) -> None:
        line = "NVRM: Xid (PCI:0000:23:00): 79, GPU has fallen off the bus."
        runner = FixtureRunner({"kernel_journal": ok(line + "\n" + line + "\n")})
        result = collect(runner)

        xid_event = events(result, "xid")[0]
        self.assertFalse(result["healthy"])
        self.assertEqual(xid_event["severity"], "critical")
        self.assertEqual(xid_event["count"], 2)
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
            start_new_session=True,
        )
        for spec in probe.COMMANDS.values():
            self.assertIsInstance(spec.argv, tuple)
            self.assertTrue(spec.argv[0].startswith("/usr/bin/"))
            self.assertNotIn("sudo", spec.argv)
            self.assertFalse(any(token in spec.argv for token in ("exec", "inspect", "logs")))

        with mock.patch.object(probe.subprocess, "Popen", return_value=fake_process) as popen:
            probe._start_process(probe.COMMANDS["gpu"], inherited_fd=19)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (19,))

    def test_false_post_sigkill_check_is_returned_as_unconfirmed(self) -> None:
        process = mock.Mock(pid=4321)
        identity = probe.ProcessIdentity(4321, 4321, 4321, 99)
        with (
            mock.patch.object(
                probe, "_process_group_matches_for_signal", return_value=True
            ),
            mock.patch.object(
                probe, "_wait_for_process_group", side_effect=[False, False]
            ) as wait_group,
            mock.patch.object(probe.os, "killpg") as kill_group,
        ):
            confirmed = probe._stop_process_group(
                process, time.monotonic() + 1, identity
            )

        self.assertFalse(confirmed)
        self.assertEqual(wait_group.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in kill_group.call_args_list],
            [signal.SIGTERM, signal.SIGKILL],
        )

    def test_survivor_aborts_all_remaining_catalog_commands(self) -> None:
        runner = FixtureRunner(
            {
                "gpu": probe.CommandResult(
                    None,
                    failure="cleanup_unconfirmed",
                    cleanup_confirmed=False,
                )
            }
        )
        result = collect(runner)

        self.assertFalse(result["healthy"])
        self.assertFalse(result["cleanup_confirmed"])
        self.assertEqual(runner.calls, ["gpu"])
        abort = next(
            event
            for event in result["events"]
            if event["code"] == "collection_aborted_unconfirmed_cleanup"
        )
        self.assertEqual(abort["evidence"]["blocked_after"], "gpu_inventory")
        self.assertEqual(
            abort["evidence"]["skipped_collectors"],
            [
                "kernel_journal",
                "docker_journal",
                "systemd_vastai",
                "systemd_docker",
                "systemd_nvidia_persistenced",
                "docker_metadata",
            ],
        )

    def test_repeated_invocation_is_rejected_while_lock_is_held(self) -> None:
        with admission_files(
            {"version": probe.ADMISSION_STATE_VERSION, "phase": "idle"}
        ):
            first = probe._acquire_admission(BOOT_ID)
            try:
                with self.assertRaises(probe.AdmissionBusy):
                    probe._acquire_admission(BOOT_ID)
            finally:
                first.close()

    def test_crash_state_quarantines_surviving_group_without_signalling_it(self) -> None:
        identity = probe.ProcessIdentity(721, 721, 721, 12345)
        active = {
            "version": probe.ADMISSION_STATE_VERSION,
            "phase": "active",
            "boot_id": BOOT_ID,
            "command_id": "gpu",
            "process": probe._process_identity_record(identity),
        }
        with admission_files(active):
            with (
                mock.patch.object(
                    probe, "_process_group_identity_exists", return_value=True
                ) as reconcile,
                mock.patch.object(probe.os, "killpg") as kill_group,
            ):
                with self.assertRaises(probe.AdmissionBusy):
                    probe._acquire_admission(BOOT_ID)

        reconcile.assert_called_once()
        kill_group.assert_not_called()

    def test_pre_identity_crash_state_uses_released_inherited_lock_as_proof(self) -> None:
        launching = {
            "version": probe.ADMISSION_STATE_VERSION,
            "phase": "launching",
            "boot_id": BOOT_ID,
            "command_id": "gpu",
            "coordinator": probe._process_identity_record(
                probe.ProcessIdentity(720, 710, 710, 12344)
            ),
        }
        with admission_files(launching) as state_path:
            with mock.patch.object(
                probe, "_process_group_identity_exists"
            ) as reconcile:
                admission = probe._acquire_admission(BOOT_ID)
                admission.close()
            state = json.loads(state_path.read_text(encoding="ascii"))

        reconcile.assert_not_called()
        self.assertEqual(
            state, {"version": probe.ADMISSION_STATE_VERSION, "phase": "idle"}
        )

    def test_reconciliation_admits_only_after_group_is_observed_gone(self) -> None:
        identity = probe.ProcessIdentity(722, 722, 722, 12346)
        active = {
            "version": probe.ADMISSION_STATE_VERSION,
            "phase": "active",
            "boot_id": BOOT_ID,
            "command_id": "gpu",
            "process": probe._process_identity_record(identity),
        }
        with admission_files(active) as state_path:
            with (
                mock.patch.object(
                    probe, "_process_group_identity_exists", return_value=False
                ),
            ):
                admission = probe._acquire_admission(BOOT_ID)
                admission.close()
            state = json.loads(state_path.read_text(encoding="ascii"))
        self.assertEqual(
            state, {"version": probe.ADMISSION_STATE_VERSION, "phase": "idle"}
        )

    def test_boot_change_clears_quarantine_without_pid_observation_or_signal(self) -> None:
        old_boot_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        identity = probe.ProcessIdentity(723, 723, 723, 12347)
        active = {
            "version": probe.ADMISSION_STATE_VERSION,
            "phase": "active",
            "boot_id": old_boot_id,
            "command_id": "gpu",
            "process": probe._process_identity_record(identity),
        }
        with admission_files(active):
            with (
                mock.patch.object(
                    probe, "_process_group_identity_exists"
                ) as reconcile,
                mock.patch.object(probe.os, "killpg") as kill_group,
            ):
                admission = probe._acquire_admission(BOOT_ID)
                admission.close()

        reconcile.assert_not_called()
        kill_group.assert_not_called()

    def test_invalid_admission_state_returns_unknown_json_without_launch(self) -> None:
        valid_idle = {"version": probe.ADMISSION_STATE_VERSION, "phase": "idle"}
        for case in ("missing", "unwritable", "corrupt"):
            with self.subTest(case=case):
                initial_state: dict[str, object] | str = (
                    "not-json" if case == "corrupt" else valid_idle
                )
                with admission_files(initial_state) as state_path:
                    if case == "unwritable":
                        os.chmod(state_path, 0o400)
                    state_name = probe.ADMISSION_STATE_FILENAME
                    if case == "missing":
                        state_name += "-missing"
                    with (
                        mock.patch.object(
                            probe, "ADMISSION_STATE_FILENAME", state_name
                        ),
                        mock.patch.object(probe, "_start_process") as start_process,
                    ):
                        result = probe.collect_probe(
                            boot_id_reader=lambda: BOOT_ID, now=NOW,
                            hostname_reader=lambda: "terracompute",
                        )

                self.assertFalse(result["healthy"])
                self.assertFalse(result["cleanup_confirmed"])
                self.assertEqual(result["target"], "terracompute")
                self.assertEqual(
                    result["events"][0]["code"],
                    "probe_admission_state_unavailable",
                )
                self.assertEqual(result["events"][0]["evidence"]["commands_launched"], 0)
                start_process.assert_not_called()
                self.assertLess(
                    len(probe._encode_result(result).encode("utf-8")),
                    probe.MAX_JSON_OUTPUT_BYTES,
                )

    @unittest.skipUnless(hasattr(os, "killpg"), "requires Unix process groups")
    def test_timeout_terminates_real_command_descendants(self) -> None:
        child_script = "\n".join(
            (
                "import os, signal, sys, time",
                "ready_fd = int(sys.argv[1])",
                "release_fd = int(sys.argv[2])",
                "def stop(_signum, _frame):",
                "    os.read(release_fd, 1)",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGTERM, stop)",
                "os.write(ready_fd, b'R')",
                "time.sleep(60)",
            )
        )
        script = "\n".join(
            (
                "import os, signal, subprocess, sys, time",
                "ready_read, ready_write = os.pipe()",
                "release_read, release_write = os.pipe()",
                "child = subprocess.Popen([sys.executable, '-c', "
                f"{child_script!r}, str(ready_write), str(release_read)], "
                "pass_fds=(ready_write, release_read))",
                "os.close(ready_write)",
                "os.close(release_read)",
                "if os.read(ready_read, 1) != b'R':",
                "    raise RuntimeError('child did not install SIGTERM handler')",
                "def stop(_signum, _frame):",
                "    os.write(release_write, b'X')",
                "    child.wait(timeout=2)",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGTERM, stop)",
                "time.sleep(60)",
            )
        )
        spec = probe.CommandSpec((sys.executable, "-c", script), 2.0)
        started_processes: list[subprocess.Popen[bytes]] = []
        original_start_process = probe._start_process

        def record_start(command: object) -> subprocess.Popen[bytes]:
            process = original_start_process(command)
            started_processes.append(process)
            return process

        with mock.patch.object(probe, "_start_process", side_effect=record_start):
            started_at = time.monotonic()
            result = probe._bounded_exec(spec)

        self.assertEqual(result.failure, "timeout")
        self.assertLess(time.monotonic() - started_at, spec.timeout_seconds + 0.25)
        self.assertEqual(len(started_processes), 1)
        process = started_processes[0]
        group_remained = probe._process_group_exists(process.pid)
        if group_remained:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)
        self.assertIsNotNone(process.poll())
        self.assertFalse(group_remained)

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

    def test_identical_xid_and_aer_occurrences_are_counted_and_bounded(self) -> None:
        xid = "NVRM: Xid (PCI:0000:23:00): 79, GPU has fallen off the bus."
        aer = "pcieport 0000:00:01.0: AER: Corrected error received"
        repetitions = probe.MAX_EVENT_COUNT + 50
        result = collect(
            FixtureRunner(
                {
                    "kernel_journal": ok(
                        "\n".join([xid] * repetitions + [aer] * repetitions)
                    )
                }
            )
        )

        counts = {
            (event["fault_family"], event["code"]): event["count"]
            for event in result["events"]
        }
        self.assertEqual(counts[("xid", "79")], probe.MAX_EVENT_COUNT)
        self.assertEqual(counts[("aer", "correctable")], probe.MAX_EVENT_COUNT)
        self.assertLess(
            len(probe._encode_result(result).encode("utf-8")),
            probe.MAX_JSON_OUTPUT_BYTES,
        )

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

    def test_gpu_stuck_mid_vfio_handover_is_a_distinct_critical_event(self) -> None:
        inventory = pci_gpus()
        inventory[7]["driver"] = "unbound"
        bdf = str(inventory[7]["pci_bdf"])
        stuck = {bdf: {"audio_driver": "vfio-pci", "nvrm_registered": True}}
        result = collect(FixtureRunner({"gpu": ok(gpu_output(7))}), inventory, handover=stuck)
        blocked = [
            event for event in result["events"] if event["code"] == "gpu_vfio_handover_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["severity"], "critical")
        self.assertEqual(
            blocked[0]["evidence"],
            {"pci_bdf": bdf, "audio_driver": "vfio-pci", "nvrm_registered": True},
        )
        # Each part of the signature is required.
        for partial in (
            {"audio_driver": "vfio-pci", "nvrm_registered": False},
            {"audio_driver": "snd_hda_intel", "nvrm_registered": True},
        ):
            with self.subTest(partial=partial):
                other = collect(
                    FixtureRunner({"gpu": ok(gpu_output(7))}), inventory, handover={bdf: partial}
                )
                self.assertNotIn(
                    "gpu_vfio_handover_blocked", {event["code"] for event in other["events"]}
                )

    def test_transient_or_unreadable_handover_state_is_not_blocked(self) -> None:
        inventory = pci_gpus()
        inventory[7]["driver"] = "unbound"
        bdf = str(inventory[7]["pci_bdf"])
        states = iter((
            {"audio_driver": "vfio-pci", "nvrm_registered": True},
            {"audio_driver": "vfio-pci", "nvrm_registered": False},
        ))
        sleeps: list[float] = []
        arguments = dict(
            runner=FixtureRunner({"gpu": ok(gpu_output(7))}),
            hostname_reader=lambda: "Terracompute.Example.",
            boot_id_reader=lambda: BOOT_ID,
            pci_gpu_reader=lambda: inventory,
            system_identity_reader=system_identity,
            now=NOW,
            gpu_handover_reader=lambda _bdf: next(states),
            sleep=sleeps.append,
        )
        # A normal handover finishes removing the device before the confirming read.
        transient = probe.collect_probe(**arguments)
        self.assertEqual(sleeps, [probe.HANDOVER_CONFIRM_SECONDS])
        self.assertNotIn(
            "gpu_vfio_handover_blocked", {event["code"] for event in transient["events"]}
        )

        def unreadable(_bdf: str) -> dict[str, object]:
            raise OSError("synthetic")

        arguments["gpu_handover_reader"] = unreadable
        failed = probe.collect_probe(**arguments)
        codes = {(event["fault_family"], event["code"]) for event in failed["events"]}
        self.assertIn(("probe", "gpu_handover_state_invalid_output"), codes)
        self.assertNotIn(("gpu", "gpu_vfio_handover_blocked"), codes)
        del bdf

    def test_handover_state_reads_only_fixed_sysfs_and_procfs_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            devices = Path(root) / "devices"
            nvrm = Path(root) / "nvrm"
            (devices / "0000:a1:00.1").mkdir(parents=True)
            (Path(root) / "drivers" / "vfio-pci").mkdir(parents=True)
            (devices / "0000:a1:00.1" / "driver").symlink_to(Path(root) / "drivers" / "vfio-pci")
            (nvrm / "0000:a1:00.0").mkdir(parents=True)
            with (
                mock.patch.object(probe, "PCI_DEVICES_PATH", devices),
                mock.patch.object(probe, "NVIDIA_DRIVER_GPUS_PATH", nvrm),
            ):
                self.assertEqual(
                    probe._read_gpu_handover_state("0000:A1:00.0"),
                    {"audio_driver": "vfio-pci", "nvrm_registered": True},
                )
                self.assertEqual(
                    probe._read_gpu_handover_state("0000:24:00.0"),
                    {"audio_driver": "absent", "nvrm_registered": False},
                )
                with self.assertRaises(ValueError):
                    probe._read_gpu_handover_state("0000:a1:00.1")

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
