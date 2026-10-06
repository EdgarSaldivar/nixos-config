#!/usr/bin/env python3
"""Offline tests for the terracompute forced-command action helper."""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
ACT_PATH = ROOT / "target" / "terracompute-act.py"
SPEC = importlib.util.spec_from_file_location("terracompute_act", ACT_PATH)
assert SPEC is not None and SPEC.loader is not None
act = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = act
SPEC.loader.exec_module(act)

NOW = dt.datetime(2026, 9, 17, 4, 0, 0, 250000, tzinfo=dt.timezone.utc)
BOOT_ID = "11111111-2222-4333-8444-555555555555"
REQUEST_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
EXECUTION_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"
EXPORTER_ID = "e" * 64
STARTED_BEFORE = "2026-09-15T02:47:18.123456789Z"
STARTED_AFTER = "2026-09-17T04:00:05.000000001Z"
BLOCKED_BDF = "0000:a1:00.0"
VM_BDF = "0000:c1:00.0"
ENVELOPE_KEYS = set(act.ENVELOPE_KEYS)
STATUS_KEYS = ENVELOPE_KEYS | {
    "container",
    "handover_blocked",
    "nvidia_visible_count",
    "pci_gpu_count",
    "tenants",
    "vm_containers",
}
EXECUTION_KEYS = ENVELOPE_KEYS | set(act.EXECUTION_FIELDS)


def container_id(index: int) -> str:
    return f"{index:064x}"


def tsv(*values: object) -> str:
    return "\t".join(json.dumps(value) for value in values) + "\n"


def ok(stdout: str = "") -> object:
    return act.CommandResult(0, stdout=stdout)


def command(operation: str, identifier: str = EXECUTION_ID) -> str:
    return (f"restart-v2 dcgm-exporter {identifier} {BOOT_ID}" if operation == 'restart'
            else f"{operation} dcgm-exporter {identifier}")


class FakeDocker:
    """Answers the catalogued docker reads from an in-memory container table."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.overrides: dict[str, object] = {}
        self.exporter: dict[str, object] | None = {
            "running": True,
            "started_at": STARTED_BEFORE,
            "image": "nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04",
            "runtime": "nvidia",
        }
        self.restart_started_at = STARTED_AFTER
        self.after_restart = None
        self.containers: list[dict[str, object]] = [
            {
                "id": container_id(1),
                "name": "C.26000001",
                "started_at": "2026-09-16T13:38:50.1Z",
                "devices": ["/dev/kvm", "/dev/vfio/vfio"],
            },
            {
                "id": container_id(2),
                "name": "C.26000002",
                "started_at": "2026-09-10T08:00:00Z",
                "devices": [],
            },
            # docker's unanchored "C." regex also matches these; they are not tenants.
            {
                "id": container_id(3),
                "name": "xC.26000003",
                "started_at": "2026-09-11T08:00:00Z",
                "devices": ["/dev/kvm"],
            },
            {
                "id": container_id(4),
                "name": "vast-CA-helper",
                "started_at": "2026-09-12T08:00:00Z",
                "devices": [],
            },
            {
                "id": container_id(5),
                "name": "c.lowercase",
                "started_at": "2026-09-13T08:00:00Z",
                "devices": [],
            },
        ]

    def commands(self) -> list[str]:
        return [command_id for command_id, _ids in self.calls]

    def __call__(self, command_id: str, container_ids: tuple[str, ...] = ()) -> object:
        ids = tuple(container_ids)
        self.calls.append((command_id, ids))
        if command_id in self.overrides:
            override = self.overrides[command_id]
            return override(self) if callable(override) else override
        if command_id == "exporter_inspect":
            if self.exporter is None:
                return act.CommandResult(1, "")
            exporter = self.exporter
            return ok(
                tsv(
                    EXPORTER_ID,
                    "/dcgm-exporter",
                    exporter["running"],
                    exporter["started_at"],
                    exporter["image"],
                    exporter["runtime"],
                )
            )
        if command_id == "exporter_list":
            return ok("" if self.exporter is None else tsv(EXPORTER_ID, "dcgm-exporter"))
        if command_id == "tenant_list":
            return ok(
                "".join(
                    tsv(container["id"], container["name"])
                    for container in self.containers
                    if re.search("C.", str(container["name"]))
                )
            )
        if command_id == "tenant_inspect":
            assert ids, "tenant inspect requires IDs"
            by_id = {container["id"]: container for container in self.containers}
            lines = []
            for identifier in ids:
                container = by_id[identifier]
                values = [container["id"], "/" + str(container["name"]), container["started_at"]]
                values.extend(container["devices"])  # type: ignore[arg-type]
                lines.append(tsv(*values))
            return ok("".join(lines))
        if command_id == "exporter_restart":
            assert self.exporter is not None
            self.exporter["started_at"] = self.restart_started_at
            self.exporter["running"] = True
            if self.after_restart is not None:
                self.after_restart(self)
            return ok("dcgm-exporter\n")
        raise AssertionError(f"uncatalogued command {command_id}")


class FakeSysfs:
    """A PCI sysfs and NVIDIA procfs tree with eight GPUs and their audio functions."""

    GPU_BUSES = (0x01, 0x21, 0x41, 0x61, 0x81, 0xA1, 0xC1, 0xE1)

    def __init__(self, root: Path) -> None:
        self.devices = root / "bus" / "pci" / "devices"
        self.nvrm = root / "proc" / "driver" / "nvidia" / "gpus"
        self.drivers = root / "bus" / "pci" / "drivers"
        for path in (self.devices, self.nvrm, self.drivers):
            path.mkdir(parents=True)
        for driver in ("nvidia", "vfio-pci", "snd_hda_intel", "pcieport"):
            (self.drivers / driver).mkdir()
        self._device("0000:00:01.1", "0x1022", "0x060400", "pcieport")
        for bus in self.GPU_BUSES:
            bdf = f"0000:{bus:02x}:00.0"
            self._device(bdf, "0x10de", "0x030000", "nvidia")
            self._device(bdf[:-1] + "1", "0x10de", "0x040300", "snd_hda_intel")
            (self.nvrm / bdf).mkdir()

    def _device(self, bdf: str, vendor: str, device_class: str, driver: str | None) -> None:
        path = self.devices / bdf
        path.mkdir()
        (path / "vendor").write_text(vendor + "\n", encoding="ascii")
        (path / "class").write_text(device_class + "\n", encoding="ascii")
        self.bind(bdf, driver)

    def errors(self, bdf: str, kind: str, body: str) -> None:
        (self.devices / bdf / f"aer_dev_{kind}").write_text(body, encoding="ascii")

    def bind(self, bdf: str, driver: str | None) -> None:
        link = self.devices / bdf / "driver"
        if link.is_symlink():
            link.unlink()
        if driver is not None:
            link.symlink_to(self.drivers / driver)

    def block(self, bdf: str) -> None:
        """The GPU is stuck mid-handover: unbound, audio on vfio-pci, still registered."""
        self.bind(bdf, None)
        self.bind(bdf[:-1] + "1", "vfio-pci")

    def release(self, bdf: str) -> None:
        """The NVIDIA driver finishes removing the device."""
        shutil.rmtree(self.nvrm / bdf)

    def assign_vm(self, bdf: str) -> None:
        self.bind(bdf, "vfio-pci")
        self.bind(bdf[:-1] + "1", "vfio-pci")
        self.release(bdf)

    def reader(self):
        return lambda: act.read_gpu_functions(self.devices, self.nvrm)


class FakeProc:
    """A /proc tree with processes, their descriptors, names and cgroups."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        (root / "self").mkdir()  # A non-numeric entry the scan must skip.

    def process(self, pid: int, comm: str, devices=(), container: str | None = None,
                unreadable: bool = False) -> None:
        process = self.root / str(pid)
        process.mkdir()
        (process / "comm").write_text(comm + "\n", encoding="ascii")
        # A command line would carry tenant data; the reader must never open it.
        (process / "cmdline").write_text("/home/sleep/tenant/private --key=hunter2", encoding="ascii")
        cgroup = f"0::/system.slice/docker-{container}.scope" if container else "0::/init.scope"
        (process / "cgroup").write_text(cgroup + "\n", encoding="ascii")
        descriptors = process / "fd"
        descriptors.mkdir(mode=0o000 if unreadable else 0o700)
        if unreadable:
            return
        for index, device in enumerate(devices):
            os.symlink(device, descriptors / str(index))
        os.symlink("/dev/null", descriptors / str(len(devices)))


class Harness:
    def __init__(self, test: unittest.TestCase) -> None:
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.docker = FakeDocker()
        self.sysfs = FakeSysfs(self.root / "sys")
        self.proc = FakeProc(self.root / "proc")
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        os.chmod(self.ledger, 0o700)
        self.sleeps: list[float] = []
        self.sleep_hook = None
        self.hostname = "terracompute\n"
        self.board = "ROME2D32GM-2T\n"
        self.env = act.Environment(
            runner=self.docker,
            hostname_reader=lambda: self.hostname,
            board_reader=lambda: self.board,
            boot_id_reader=lambda: BOOT_ID + "\n",
            gpu_reader=self.sysfs.reader(),
            gpu_handle_reader=lambda: act.read_gpu_handles(self.proc.root),
            pci_error_reader=lambda: act.read_pci_errors(self.sysfs.devices),
            clock=lambda: NOW,
            sleep=self._sleep,
            ledger_root=self.ledger,
            ledger_owner_uid=os.getuid(),
        )

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.sleep_hook is not None:
            self.sleep_hook()

    def run(self, ssh_command: str | None, argv: list[str] | None = None) -> tuple[dict[str, object], int, str]:
        environ = {} if ssh_command is None else {"SSH_ORIGINAL_COMMAND": ssh_command}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = act.main(environ, self.env, argv=argv)
        text = stdout.getvalue()
        lines = text.splitlines()
        assert len(lines) == 1 and text.endswith("\n"), text
        return json.loads(lines[0]), exit_code, text

    def ledger_files(self) -> list[str]:
        return sorted(os.listdir(self.ledger))

    def record(self, identifier: str = EXECUTION_ID) -> dict[str, object]:
        return json.loads((self.ledger / f"{identifier}.json").read_text(encoding="ascii"))


def walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key).lower())
            keys |= walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= walk_keys(item)
    return keys


class GrammarTests(unittest.TestCase):
    def test_accepts_only_the_exact_three_token_forms(self) -> None:
        for operation in ("status", "restart", "result"):
            request = act.parse_request(command(operation))
            self.assertEqual(request, act.Request('restart-v2' if operation == 'restart' else operation,
                                                  "dcgm-exporter", EXECUTION_ID, BOOT_ID if operation == 'restart' else None))

    def test_rejects_malformed_requests_with_error_object_and_exit_2(self) -> None:
        bad = [
            None,
            "",
            " ",
            "status",
            "status dcgm-exporter",
            command("stop"),
            command("Status"),
            command("status").replace("dcgm-exporter", "nvidia-exporter"),
            command("status").replace("dcgm-exporter", "DCGM-EXPORTER"),
            command("status", EXECUTION_ID.upper()),
            command("status", EXECUTION_ID[:-1]),
            command("status", EXECUTION_ID + "0"),
            command("status", "-" * 36),
            command("status", "../../../../etc/shadow"),
            command("status") + " extra",
            command("status") + " " + EXECUTION_ID,
            "status  dcgm-exporter " + EXECUTION_ID,
            " " + command("status"),
            command("status") + " ",
            command("status") + "\n",
            command("status").replace(" ", "\t"),
            command("status") + ";reboot",
            command("status") + "|reboot",
            command("status") + "&&reboot",
            "status dcgm-exporter $(reboot)",
            "status dcgm-exporter `reboot`",
            command("status") + ">/etc/passwd",
            command("status") + "'",
            command("status") + '"',
            command("status") + "\\",
            command("status") + "*",
            command("status") + "\x00",
            command("status") + "é",
            command("status") + "\udcff",
            command("status") + " " + "a" * 300,
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                harness = Harness(self)
                response, exit_code, text = harness.run(raw)
                self.assertEqual(exit_code, 2)
                self.assertEqual(set(response), ENVELOPE_KEYS | {"reason"})
                self.assertEqual(response["reason"], "invalid_request")
                self.assertFalse(response["ok"])
                self.assertIsNone(response["operation"])
                self.assertIsNone(response["id"])
                self.assertIsNone(response["component"])
                self.assertEqual(response["machine_id"], 17049)
                self.assertEqual(response["schema_version"], 1)
                self.assertNotIn("reboot", text)
                self.assertEqual(harness.docker.calls, [])
                self.assertEqual(harness.sleeps, [])
                self.assertEqual(harness.ledger_files(), [])

    def test_oversized_variable_is_rejected_before_parsing(self) -> None:
        self.assertIsNone(act.parse_request("a" * 257))
        self.assertIsNone(act.parse_request(command("status") + " " * 200))
        self.assertIsNotNone(act.parse_request(command("status")))


class StatusTests(unittest.TestCase):
    def test_status_schema_with_confirmed_handover_signature(self) -> None:
        harness = Harness(self)
        harness.sysfs.block(BLOCKED_BDF)
        harness.sysfs.assign_vm(VM_BDF)
        response, exit_code, text = harness.run(command("status", REQUEST_ID))

        self.assertEqual(exit_code, 0)
        self.assertEqual(set(response), STATUS_KEYS)
        self.assertEqual(
            text,
            json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["operation"], "status")
        self.assertEqual(response["id"], REQUEST_ID)
        self.assertEqual(response["component"], "dcgm-exporter")
        self.assertEqual(response["observed_at"], "2026-09-17T04:00:00Z")
        self.assertEqual(response["hostname"], "terracompute")
        self.assertEqual(response["board"], "ROME2D32GM-2T")
        self.assertEqual(response["boot_id"], BOOT_ID)
        self.assertEqual(response["machine_id"], 17049)
        self.assertEqual(
            response["container"],
            {
                "present": True,
                "running": True,
                "started_at": STARTED_BEFORE,
                "image": "nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04",
                "runtime": "nvidia",
            },
        )
        self.assertEqual(response["handover_blocked"], [BLOCKED_BDF])
        self.assertEqual(harness.sleeps, [act.HANDOVER_CONFIRM_SECONDS])
        self.assertEqual(act.HANDOVER_CONFIRM_SECONDS, 3.0)
        self.assertEqual(response["pci_gpu_count"], 8)
        self.assertEqual(response["nvidia_visible_count"], 6)
        self.assertEqual(response["tenants"]["count"], 2)
        self.assertEqual(response["tenants"]["names"], ["C.26000001", "C.26000002"])
        self.assertEqual(
            response["tenants"]["members"],
            [
                {"name": "C.26000001", "id": container_id(1), "started_at": "2026-09-16T13:38:50.1Z"},
                {"name": "C.26000002", "id": container_id(2), "started_at": "2026-09-10T08:00:00Z"},
            ],
        )
        self.assertRegex(response["tenants"]["digest"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(response["vm_containers"], ["C.26000001"])
        self.assertNotIn("exporter_restart", harness.docker.commands())
        self.assertEqual(harness.ledger_files(), [])

    def test_transient_handover_is_cleared_by_the_confirming_reread(self) -> None:
        harness = Harness(self)
        harness.sysfs.block(BLOCKED_BDF)
        harness.sleep_hook = lambda: harness.sysfs.release(BLOCKED_BDF)
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertTrue(response["ok"])
        self.assertEqual(harness.sleeps, [3.0])
        self.assertEqual(response["handover_blocked"], [])
        self.assertEqual(response["nvidia_visible_count"], 7)

    def test_each_part_of_the_handover_signature_is_required(self) -> None:
        audio = BLOCKED_BDF[:-1] + "1"
        cases = {
            "gpu_still_bound": lambda sysfs: sysfs.bind(audio, "vfio-pci"),
            "audio_not_vfio": lambda sysfs: sysfs.bind(BLOCKED_BDF, None),
            "not_registered": lambda sysfs: (sysfs.block(BLOCKED_BDF), sysfs.release(BLOCKED_BDF)),
            "vm_assigned": lambda sysfs: sysfs.assign_vm(BLOCKED_BDF),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                harness = Harness(self)
                arrange(harness.sysfs)
                response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
                self.assertTrue(response["ok"])
                self.assertEqual(response["handover_blocked"], [])
                self.assertEqual(harness.sleeps, [])

    def test_gpu_reader_reads_fixed_sysfs_and_procfs_facts(self) -> None:
        harness = Harness(self)
        harness.sysfs.block(BLOCKED_BDF)
        functions = act.read_gpu_functions(harness.sysfs.devices, harness.sysfs.nvrm)
        self.assertEqual(len(functions), 8)
        by_bdf = {function["pci_bdf"]: function for function in functions}
        self.assertEqual(
            by_bdf[BLOCKED_BDF],
            {"pci_bdf": BLOCKED_BDF, "driver": "unbound", "audio_driver": "vfio-pci", "nvrm_registered": True},
        )
        self.assertEqual(
            by_bdf["0000:01:00.0"],
            {"pci_bdf": "0000:01:00.0", "driver": "nvidia", "audio_driver": "snd_hda_intel", "nvrm_registered": True},
        )
        self.assertNotIn("0000:00:01.1", by_bdf)

    def test_unreadable_gpu_state_is_reported_not_guessed(self) -> None:
        harness = Harness(self)

        def unreadable() -> list[dict[str, object]]:
            raise OSError("synthetic")

        harness.env.gpu_reader = unreadable
        response, exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(exit_code, 0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "gpu_state_unavailable")
        self.assertIsNone(response["handover_blocked"])
        self.assertIsNone(response["pci_gpu_count"])
        self.assertIsNone(response["nvidia_visible_count"])
        self.assertEqual(response["tenants"]["count"], 2)

    def test_absent_exporter_is_distinguished_from_docker_failure(self) -> None:
        harness = Harness(self)
        harness.docker.exporter = None
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["container"],
            {"present": False, "running": False, "started_at": None, "image": None, "runtime": None},
        )

        # Inspect fails although the exporter is listed: that is not absence.
        harness = Harness(self)
        harness.docker.overrides["exporter_inspect"] = act.CommandResult(1, "")
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "container_unavailable")
        self.assertIsNone(response["container"])

        harness = Harness(self)
        harness.docker.overrides["exporter_inspect"] = act.CommandResult(None, failure="timeout")
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(response["reason"], "container_unavailable")
        self.assertNotIn("exporter_list", harness.docker.commands())


class SensitiveFieldTests(unittest.TestCase):
    INSPECT_FIELDS = {
        "Id",
        "Name",
        "State.Running",
        "State.StartedAt",
        "Config.Image",
        "HostConfig.Runtime",
        "HostConfig.Devices",
        "PathOnHost",
    }
    LIST_FIELDS = {"ID", "Names", "State", "Status", "Image"}
    PROGRAMS = {"/usr/bin/docker", "/usr/bin/nvidia-smi", "/usr/bin/dmesg"}

    def test_every_catalogued_command_runs_an_allowed_program(self) -> None:
        for command_id, spec in act.COMMANDS.items():
            with self.subTest(command=command_id):
                self.assertIn(spec.argv[0], self.PROGRAMS)
                # A docker filter carries regex anchors and a format template carries
                # separators; neither reaches a shell. Everything else stays plain.
                for index, argument in enumerate(spec.argv):
                    if index and spec.argv[index - 1] == "--format":
                        continue
                    self.assertNotRegex(argument, r"[;&|`\n]")
                self.assertGreater(spec.timeout_seconds, 0)

    def test_docker_templates_request_only_allowed_fields(self) -> None:
        for command_id, spec in act.COMMANDS.items():
            if spec.argv[0] != "/usr/bin/docker":
                continue
            with self.subTest(command=command_id):
                joined = " ".join(spec.argv).lower()
                for forbidden in ("env", "label", "cmd", "mount", "entrypoint", "args", "{{json .}}"):
                    self.assertNotIn(forbidden, joined)
                if "--format" not in spec.argv:
                    continue
                template = spec.argv[spec.argv.index("--format") + 1]
                fields = set(re.findall(r"\.([A-Za-z][A-Za-z.]*)", template))
                allowed = self.INSPECT_FIELDS if spec.argv[1] == "inspect" else self.LIST_FIELDS
                self.assertTrue(fields, template)
                self.assertLessEqual(fields, allowed)

    def test_env_labels_and_cmd_never_appear_in_output(self) -> None:
        harness = Harness(self)
        exporter_line = tsv(
            EXPORTER_ID, "/dcgm-exporter", True, STARTED_BEFORE, "dcgm:latest", "nvidia", "SECRET_TOKEN=hunter2"
        )
        harness.docker.overrides["exporter_inspect"] = ok(exporter_line)
        harness.docker.containers[0]["devices"] = ["/dev/kvm", "LABEL_SECRET=hunter2"]
        response, _exit_code, text = harness.run(command("status", REQUEST_ID))
        self.assertNotIn("hunter2", text)
        self.assertIsNone(response["container"])
        self.assertEqual(response["reason"], "container_unavailable")
        self.assertEqual(response["vm_containers"], ["C.26000001"])
        self.assertFalse(walk_keys(response) & {"env", "labels", "cmd", "mounts", "config", "args"})

        harness = Harness(self)
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertTrue(response["ok"])
        self.assertFalse(walk_keys(response) & {"env", "labels", "cmd", "mounts", "config", "args"})


class TenantDigestTests(unittest.TestCase):
    def test_digest_is_deterministic_and_excludes_non_tenant_names(self) -> None:
        digests = set()
        for seed in range(4):
            harness = Harness(self)
            random.Random(seed).shuffle(harness.docker.containers)
            response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
            digests.add(response["tenants"]["digest"])
            self.assertEqual(response["tenants"]["names"], ["C.26000001", "C.26000002"])
            inspected = [ids for command_id, ids in harness.docker.calls if command_id == "tenant_inspect"]
            self.assertEqual(inspected, [(container_id(1), container_id(2))])
        expected_lines = sorted(
            [
                f"C.26000001 {container_id(1)} 2026-09-16T13:38:50.1Z",
                f"C.26000002 {container_id(2)} 2026-09-10T08:00:00Z",
            ]
        )
        expected = hashlib.sha256("".join(line + "\n" for line in expected_lines).encode()).hexdigest()
        self.assertEqual(digests, {expected})

    def test_no_tenants_skips_inspect_and_hashes_the_empty_set(self) -> None:
        harness = Harness(self)
        harness.docker.containers = harness.docker.containers[2:]
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(
            response["tenants"],
            {"count": 0, "digest": hashlib.sha256(b"").hexdigest(), "names": [], "members": []},
        )
        self.assertEqual(response["vm_containers"], [])
        self.assertNotIn("tenant_inspect", harness.docker.commands())

    def test_digest_changes_with_tenant_start_time(self) -> None:
        rows = [{"name": "C.1", "id": container_id(1), "started_at": "2026-09-16T13:38:50Z"}]
        changed = [dict(rows[0], started_at="2026-09-16T13:38:51Z")]
        self.assertNotEqual(act.tenant_digest(rows), act.tenant_digest(changed))

    def test_incomplete_tenant_inspect_fails_closed(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["tenant_inspect"] = ok(
            tsv(container_id(1), "/C.26000001", "2026-09-16T13:38:50.1Z")
        )
        response, _exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(response["reason"], "tenants_unavailable")
        self.assertIsNone(response["tenants"])
        self.assertIsNone(response["vm_containers"])


class InspectTests(unittest.TestCase):
    """The read-only catalogue: it answers questions and changes nothing."""

    def read(self, harness, topic: str) -> dict:
        response, exit_code, text = harness.run(f"inspect {topic} {REQUEST_ID}")
        self.assertEqual(exit_code, 0)
        self.assertLessEqual(len(text.encode("ascii")), act.MAX_OUTPUT_BYTES)
        self.assertEqual(response["topic"], topic)
        return response

    def test_each_topic_reads_and_changes_nothing(self) -> None:
        for topic in act.READ_TOPICS:
            with self.subTest(topic=topic):
                harness = Harness(self)
                harness.docker.overrides["exporter_logs"] = ok("2026-09-17T06:00:00Z starting\n")
                harness.docker.overrides["gpu_processes"] = ok(
                    "GPU-1111, 330423, /home/sleep/researchers/private/bin/python, 5842 MiB\n"
                )
                harness.docker.overrides["gpu_inventory"] = ok("0, 00000000:01:00.0, RTX 4090\n")
                harness.docker.overrides["kernel_log"] = ok(
                    "Sep 17 06:00:00 host kernel: NVRM: GPU at 0000:a1:00.0 is in use\n"
                    "Sep 17 06:00:01 host kernel: usb 1-1: new device\n"
                )
                harness.docker.overrides["container_list"] = ok("dcgm-exporter | running | Up | dcgm:1\n")
                response = self.read(harness, topic)
                self.assertIsInstance(response["lines"], list)
                self.assertFalse(response["truncated"])
                self.assertNotIn("exporter_restart", harness.docker.commands())
                self.assertEqual(harness.ledger_files(), [])

    def test_gpu_handles_name_the_holder_without_its_command_line(self) -> None:
        harness = Harness(self)
        harness.proc.process(101, "dcgm-exporter", ["/dev/nvidiactl", "/dev/nvidia5"], container="a" * 64)
        harness.proc.process(202, "python", ["/dev/nvidia1"])
        harness.proc.process(303, "sshd")  # No NVIDIA descriptor: never listed.
        harness.proc.process(404, "hidden", ["/dev/nvidia7"], unreadable=True)
        response = self.read(harness, "gpu-handles")
        self.assertEqual(response["lines"], [
            f"pid=101 comm=dcgm-exporter container={'a' * 12} devices=nvidia5,nvidiactl",
            "pid=202 comm=python container=host devices=nvidia1",
        ])
        body = json.dumps(response)
        for secret in ("hunter2", "/home/sleep", "cmdline"):
            self.assertNotIn(secret, body)

    def test_gpu_handles_are_bounded_in_processes_scanned_and_lines_returned(self) -> None:
        harness = Harness(self)
        for pid in range(1, act.MAX_INSPECT_LINES + 20):
            harness.proc.process(pid, f"holder{pid}", ["/dev/nvidia0"])
        response = self.read(harness, "gpu-handles")
        self.assertEqual(len(response["lines"]), act.MAX_INSPECT_LINES)
        # The first holders are kept, so the scan stops rather than the answer being cut.
        self.assertTrue(response["lines"][0].startswith("pid=1 "))
        self.assertLessEqual(act.MAX_PROCESS_DESCRIPTORS, 4096)

    def test_gpu_handles_stop_after_the_process_scan_bound(self) -> None:
        harness = Harness(self)
        for pid in (11, 12, 13, 14, 15):
            harness.proc.process(pid, f"holder{pid}", ["/dev/nvidia0"])
        with mock.patch.object(act, "MAX_SCANNED_PROCESSES", 3):
            response = self.read(harness, "gpu-handles")
        # Only the first three /proc entries are considered.
        self.assertEqual(
            [line.split(" ")[0] for line in response["lines"]], ["pid=11", "pid=12", "pid=13"]
        )

    def test_kernel_log_keeps_pcie_error_lines(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["kernel_log"] = ok(
            "Sep 17 06:00:00 host kernel: AER: Corrected error received id=00a1\n"
            "Sep 17 06:00:01 host kernel: usb 1-1: new device\n"
        )
        response = self.read(harness, "kernel-gpu-log")
        self.assertEqual(len(response["lines"]), 1)
        self.assertIn("AER", response["lines"][0])

    def test_gpu_handles_say_so_when_nothing_holds_a_device(self) -> None:
        harness = Harness(self)
        harness.proc.process(101, "sshd")
        response = self.read(harness, "gpu-handles")
        self.assertEqual(response["lines"], ["no process holds an NVIDIA device open"])
        self.assertTrue(response["ok"])

    def test_pci_errors_report_only_non_zero_counters(self) -> None:
        harness = Harness(self)
        blocked = BLOCKED_BDF
        harness.sysfs.errors(blocked, "correctable", "RxErr 0\nBadTLP 12\nTimeout 0\n")
        harness.sysfs.errors(blocked, "fatal", "TLP 0\nCmpltAbrt 0\n")
        harness.sysfs.errors("0000:01:00.0", "nonfatal", "TLP 3\n")
        response = self.read(harness, "pci-errors")
        self.assertEqual(response["lines"], [
            "0000:01:00.0 nonfatal TLP=3",
            f"{blocked} correctable BadTLP=12",
        ])

    def test_pci_errors_say_so_when_every_counter_is_zero(self) -> None:
        harness = Harness(self)
        harness.sysfs.errors(BLOCKED_BDF, "fatal", "TLP 0\n")
        response = self.read(harness, "pci-errors")
        self.assertEqual(response["lines"], ["no non-zero PCIe error counters"])

    def test_a_reader_that_fails_is_reported_not_faked(self) -> None:
        harness = Harness(self)
        harness.env = dataclasses.replace(
            harness.env, gpu_handle_reader=lambda: (_ for _ in ()).throw(OSError("proc gone"))
        )
        response = self.read(harness, "gpu-handles")
        self.assertEqual((response["ok"], response["lines"]), (False, []))
        self.assertEqual(response["reason"], "read_failed_oserror")

    def test_process_paths_are_reduced_to_the_program_name(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["gpu_processes"] = ok(
            "GPU-1111, 330423, /home/sleep/researchers/cloud9sm/personal/dsr/.venv/bin/python, 4050 MiB\n"
        )
        response = self.read(harness, "gpu-processes")
        self.assertEqual(response["lines"], ["GPU-1111, 330423, python, 4050 MiB"])
        self.assertNotIn("cloud9sm", json.dumps(response))

    def test_a_tenant_path_cannot_hide_behind_a_separator(self) -> None:
        """The path is taken from both ends, so separators inside it change nothing."""
        harness = Harness(self)
        harness.docker.overrides["gpu_processes"] = ok(
            "GPU-1111, 330423, /home/sleep/cloud9sm/Bach, Johann/train.py, 4050 MiB\n"
        )
        response = self.read(harness, "gpu-processes")
        self.assertEqual(response["lines"], ["GPU-1111, 330423, train.py, 4050 MiB"])
        self.assertNotIn("cloud9sm", json.dumps(response))
        self.assertNotIn("Johann", json.dumps(response))

    def test_kernel_log_keeps_only_gpu_lines_and_bounds_them(self) -> None:
        harness = Harness(self)
        noise = "".join(f"Sep 17 06:00:{index:02d} host kernel: usb {index}\n" for index in range(60))
        gpu = "".join(
            f"Sep 17 07:00:00 host kernel: NVRM: Xid {index} {'x' * 400}\n"
            for index in range(act.MAX_INSPECT_LINES + 5)
        )
        # Noise last, so an unfiltered read would keep it in the tail.
        harness.docker.overrides["kernel_log"] = ok(gpu + noise)
        response = self.read(harness, "kernel-gpu-log")
        self.assertEqual(len(response["lines"]), act.MAX_INSPECT_LINES)
        self.assertTrue(response["truncated"])
        self.assertTrue(all("NVRM" in line for line in response["lines"]))
        self.assertNotIn("usb", json.dumps(response["lines"]))
        self.assertTrue(all(len(line) <= act.MAX_INSPECT_LINE_CHARS for line in response["lines"]))

    def test_lines_are_reduced_to_printable_ascii(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["exporter_logs"] = ok("start \x1b[31mred\x07 \u00e9nd\n")
        response = self.read(harness, "exporter-logs")
        self.assertEqual(response["lines"], ["start  [31mred   nd"])

    def test_a_failed_read_is_reported_not_faked(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["gpu_inventory"] = act.CommandResult(None, failure="timeout")
        response = self.read(harness, "gpu-inventory")
        self.assertEqual((response["ok"], response["reason"], response["lines"]), (False, "timeout", []))

    def test_only_catalogued_topics_are_accepted(self) -> None:
        harness = Harness(self)
        for bad in ("inspect /etc/shadow", "inspect gpu-processes; id", "inspect tenant-logs",
                    "inspect dcgm-exporter", "status gpu-processes"):
            with self.subTest(bad=bad):
                response, exit_code, _text = harness.run(f"{bad} {REQUEST_ID}")
                self.assertEqual((exit_code, response["reason"]), (2, "invalid_request"))
                self.assertEqual(harness.docker.calls, [])

    def test_exporter_logs_include_the_stream_the_program_writes_to(self) -> None:
        spec = act.COMMANDS["exporter_logs"]
        self.assertTrue(spec.merge_stderr)
        self.assertEqual(spec.argv[:3], ("/usr/bin/docker", "logs", "--tail"))
        self.assertFalse(act.COMMANDS["container_list"].merge_stderr)


class RestartTests(unittest.TestCase):
    def test_legacy_restart_without_boot_binding_never_runs(self) -> None:
        harness = Harness(self)
        response, _exit, _text = harness.run(f"restart dcgm-exporter {EXECUTION_ID}")
        self.assertFalse(response['ok'])
        self.assertEqual(response['reason'], 'restart_boot_or_host_mismatch')
        self.assertNotIn('exporter_restart', harness.docker.commands())

    def test_reboot_during_status_collection_refuses_restart_at_launch(self) -> None:
        harness = Harness(self)
        boot = [BOOT_ID]
        harness.env = dataclasses.replace(harness.env, boot_id_reader=lambda: boot[0])
        def changed_boot(docker):
            boot[0] = '22222222-3333-4444-8555-666666666666'
            del docker.overrides['tenant_list']
            return docker('tenant_list')
        harness.docker.overrides['tenant_list'] = changed_boot
        response, _exit, _text = harness.run(command('restart'))
        self.assertFalse(response['ok'])
        self.assertEqual(response['reason'], 'restart_boot_or_host_mismatch')
        self.assertNotIn('exporter_restart', harness.docker.commands())

    def test_restart_success_records_all_evidence(self) -> None:
        harness = Harness(self)
        harness.sysfs.block(BLOCKED_BDF)

        def vast_finishes_handover(docker: FakeDocker) -> None:
            # Releasing the handles lets NVML finish and Vast remove the stuck VM rental.
            harness.sysfs.release(BLOCKED_BDF)
            docker.containers = [c for c in docker.containers if c["name"] != "C.26000001"]

        harness.docker.after_restart = vast_finishes_handover
        response, exit_code, _text = harness.run(command("restart"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(set(response), EXECUTION_KEYS | {"replayed"})
        self.assertTrue(response["ok"], response)
        self.assertFalse(response["replayed"])
        self.assertEqual(response["state"], "executed")
        self.assertEqual(response["exit_code"], 0)
        self.assertEqual(response["started_at_before"], STARTED_BEFORE)
        self.assertEqual(response["started_at_after"], STARTED_AFTER)
        self.assertTrue(response["running_before"])
        self.assertTrue(response["running_after"])
        self.assertEqual(response["requested_at"], "2026-09-17T04:00:00Z")
        self.assertEqual(response["completed_at"], "2026-09-17T04:00:00Z")
        self.assertEqual(response["execution_boot_id"], BOOT_ID)
        self.assertEqual(response["tenants_before"]["names"], ["C.26000001", "C.26000002"])
        self.assertEqual(response["tenants_after"]["names"], ["C.26000002"])
        self.assertNotEqual(response["tenants_before"]["digest"], response["tenants_after"]["digest"])
        self.assertEqual(response["vm_containers_before"], ["C.26000001"])
        self.assertEqual(response["vm_containers_after"], [])
        self.assertEqual(response["handover_blocked_before"], [BLOCKED_BDF])
        self.assertEqual(response["handover_blocked_after"], [])

        commands = harness.docker.commands()
        self.assertEqual(commands.count("exporter_restart"), 1)
        restart_index = commands.index("exporter_restart")
        self.assertIn("exporter_inspect", commands[:restart_index])
        self.assertIn("tenant_list", commands[:restart_index])
        self.assertIn("exporter_inspect", commands[restart_index + 1 :])
        self.assertIn("tenant_list", commands[restart_index + 1 :])

        self.assertEqual(harness.ledger_files(), [f"{EXECUTION_ID}.json"])
        self.assertEqual(os.stat(harness.ledger / f"{EXECUTION_ID}.json").st_mode & 0o777, 0o600)
        record = harness.record()
        self.assertEqual(record["state"], "executed")
        self.assertTrue(record["ok"])
        self.assertIsNone(record["reason"])
        for field in act.EXECUTION_FIELDS:
            self.assertEqual(record[field], response[field], field)

    def test_changed_tenant_digest_alone_does_not_fail_the_restart(self) -> None:
        harness = Harness(self)

        def tenant_restarted(docker: FakeDocker) -> None:
            docker.containers[1]["started_at"] = "2026-09-17T04:00:03Z"

        harness.docker.after_restart = tenant_restarted
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertTrue(response["ok"])
        self.assertNotIn("reason", response)
        self.assertEqual(response["tenants_before"]["count"], response["tenants_after"]["count"])
        self.assertNotEqual(response["tenants_before"]["digest"], response["tenants_after"]["digest"])

    def test_repeated_execution_id_returns_record_without_docker(self) -> None:
        harness = Harness(self)
        first, _exit_code, _text = harness.run(command("restart"))
        calls = list(harness.docker.calls)
        sleeps = list(harness.sleeps)
        second, exit_code, _text = harness.run(command("restart"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(harness.docker.calls, calls)
        self.assertEqual(harness.sleeps, sleeps)
        self.assertTrue(second["replayed"])
        self.assertTrue(second["ok"])
        for field in act.EXECUTION_FIELDS:
            self.assertEqual(second[field], first[field], field)

    def test_crash_left_pending_record_reports_unknown_without_restarting(self) -> None:
        harness = Harness(self)
        pending = {
            "schema_version": 1,
            "id": EXECUTION_ID,
            "component": "dcgm-exporter",
            "state": "pending",
            "requested_at": "2026-09-17T03:59:00Z",
            "execution_boot_id": BOOT_ID,
        }
        path = harness.ledger / f"{EXECUTION_ID}.json"
        path.write_text(json.dumps(pending), encoding="ascii")
        os.chmod(path, 0o600)
        # While a run holds the ledger lock, the claim may still be acting.
        blocker = act.Ledger(harness.ledger, os.getuid())
        self.assertTrue(blocker.try_lock())
        response, exit_code, _text = harness.run(command("result"))
        self.assertEqual((response["state"], response["reason"]), ("unknown", "execution_in_progress"))
        self.assertEqual(json.loads(path.read_text(encoding="ascii")), pending)
        blocker.close()
        # Without the lock, the claimant is dead. A claim that was never armed proves
        # docker restart never started; it is finalized as not started and never runs.
        for operation in ("result", "restart"):
            with self.subTest(operation=operation):
                response, exit_code, _text = harness.run(command(operation))
                self.assertEqual(exit_code, 0)
                self.assertEqual((response["state"], response["reason"]), ("refused", "execution_not_started"))
                self.assertFalse(response["ok"])
                self.assertEqual(response["requested_at"], "2026-09-17T03:59:00Z")
                self.assertIsNone(response["exit_code"])
                self.assertEqual(harness.docker.calls, [])
        self.assertEqual(harness.record()["state"], "refused")

        # An armed claim may have started docker restart: it is finalized as interrupted
        # and keeps its baseline start time.
        path.unlink()
        armed = dict(pending, state="armed", running_before=True, started_at_before=STARTED_BEFORE)
        path.write_text(json.dumps(armed), encoding="ascii")
        os.chmod(path, 0o600)
        blocker = act.Ledger(harness.ledger, os.getuid())
        self.assertTrue(blocker.try_lock())
        response, _exit_code, _text = harness.run(command("result"))
        self.assertEqual((response["state"], response["reason"]), ("unknown", "execution_in_progress"))
        self.assertEqual(response["started_at_before"], STARTED_BEFORE)
        blocker.close()
        response, _exit_code, _text = harness.run(command("result"))
        self.assertEqual((response["state"], response["reason"]), ("interrupted", "execution_interrupted"))
        self.assertEqual(response["started_at_before"], STARTED_BEFORE)
        self.assertEqual(harness.docker.calls, [])

        # A crash between O_EXCL creation and the first write leaves an empty file.
        path.unlink()
        path.write_text("", encoding="ascii")
        os.chmod(path, 0o600)
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["state"], "unknown")
        self.assertEqual(response["reason"], "ledger_record_invalid")
        self.assertEqual(harness.docker.calls, [])

    def test_identity_mismatch_refuses_and_is_recorded(self) -> None:
        for attribute, value in (("hostname", "terracompute-2\n"), ("board", "ROMED8-2T\n")):
            with self.subTest(attribute=attribute):
                harness = Harness(self)
                setattr(harness, attribute, value)
                response, exit_code, _text = harness.run(command("restart"))
                self.assertEqual(exit_code, 0)
                self.assertFalse(response["ok"])
                self.assertEqual(response["reason"], "identity_mismatch")
                self.assertEqual(response["state"], "refused")
                self.assertEqual(harness.docker.calls, [])
                # A refused execution ID stays spent even after identity is restored.
                harness.hostname, harness.board = "terracompute\n", "ROME2D32GM-2T\n"
                replay, _exit_code, _text = harness.run(command("restart"))
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["reason"], "identity_mismatch")
                self.assertEqual(harness.docker.calls, [])

    def test_identity_is_rechecked_immediately_before_restart(self) -> None:
        harness = Harness(self)
        harness.sysfs.block(BLOCKED_BDF)

        def hostname_changes() -> None:
            harness.hostname = "someone-else\n"

        harness.sleep_hook = hostname_changes
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["reason"], "identity_mismatch")
        self.assertEqual(response["handover_blocked_before"], [BLOCKED_BDF])
        self.assertNotIn("exporter_restart", harness.docker.commands())

    def test_missing_container_refuses(self) -> None:
        harness = Harness(self)
        harness.docker.exporter = None
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "container_absent")
        self.assertEqual(response["state"], "refused")
        self.assertFalse(response["running_before"])
        self.assertNotIn("exporter_restart", harness.docker.commands())

    def test_incomplete_before_status_refuses(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["tenant_list"] = act.CommandResult(None, failure="timeout")
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["reason"], "status_before_unavailable")
        self.assertEqual(response["state"], "refused")
        self.assertNotIn("exporter_restart", harness.docker.commands())

    def test_unchanged_started_at_is_not_ok(self) -> None:
        harness = Harness(self)
        harness.docker.restart_started_at = STARTED_BEFORE
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "started_at_unchanged")
        self.assertEqual(response["state"], "executed")
        self.assertEqual(response["exit_code"], 0)

    def test_stopped_exporter_after_restart_is_not_ok(self) -> None:
        harness = Harness(self)

        def exporter_exits(docker: FakeDocker) -> None:
            docker.exporter["running"] = False

        harness.docker.after_restart = exporter_exits
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["reason"], "container_not_running")
        self.assertFalse(response["running_after"])

    def test_nonzero_restart_exit_is_recorded(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["exporter_restart"] = act.CommandResult(1, "")
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["reason"], "restart_nonzero_exit")
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(harness.docker.commands().count("exporter_restart"), 1)

    def test_docker_timeout_is_recorded_without_retry(self) -> None:
        harness = Harness(self)
        harness.docker.overrides["exporter_restart"] = act.CommandResult(None, failure="timeout")
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "restart_timeout")
        self.assertEqual(response["state"], "executed")
        self.assertIsNone(response["exit_code"])
        self.assertEqual(response["started_at_after"], STARTED_BEFORE)
        self.assertIsNotNone(response["tenants_after"])
        self.assertEqual(harness.docker.commands().count("exporter_restart"), 1)
        replay, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(replay["reason"], "restart_timeout")
        self.assertEqual(harness.docker.commands().count("exporter_restart"), 1)

    def test_final_ledger_write_failure_is_not_ok_and_later_reads_as_interrupted(self) -> None:
        harness = Harness(self)
        # Arming succeeds; only the final record write fails.
        original_replace = act.Ledger.replace
        writes = []

        def replace(ledger, execution_id, record):
            writes.append(record["state"])
            if len(writes) > 1:
                raise OSError("disk full")
            original_replace(ledger, execution_id, record)

        with mock.patch.object(act.Ledger, "replace", replace):
            response, exit_code, _text = harness.run(command("restart"))
        self.assertEqual(writes, ["armed", "executed"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "ledger_write_failed")
        self.assertEqual(response["exit_code"], 0)
        calls = list(harness.docker.calls)
        # The run has exited, so its claim is finalized: the restart may have happened,
        # but the same execution ID can never restart again.
        replay, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual((replay["state"], replay["reason"]), ("interrupted", "execution_interrupted"))
        self.assertEqual(harness.docker.calls, calls)

    def test_a_claim_that_cannot_be_armed_is_refused_before_docker_runs(self) -> None:
        harness = Harness(self)
        with mock.patch.object(act.Ledger, "replace", side_effect=OSError("disk full")):
            response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual((response["state"], response["reason"]), ("refused", "ledger_write_failed"))
        self.assertNotIn("exporter_restart", harness.docker.commands())
        # The claim was never armed, so it reads as never started.
        response, _exit_code, _text = harness.run(command("result"))
        self.assertEqual((response["state"], response["reason"]), ("refused", "execution_not_started"))
        self.assertNotIn("exporter_restart", harness.docker.commands())

    def test_concurrent_restart_is_refused_without_claiming_the_id(self) -> None:
        harness = Harness(self)
        fd = os.open(harness.ledger, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            response, _exit_code, _text = harness.run(command("restart"))
            self.assertEqual(response["reason"], "actor_busy")
            self.assertEqual(harness.docker.calls, [])
            self.assertEqual(harness.ledger_files(), [])
        finally:
            os.close(fd)
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertTrue(response["ok"])

    def test_restart_command_is_the_exact_catalogued_argv(self) -> None:
        spec = act.COMMANDS["exporter_restart"]
        self.assertEqual(spec.argv, ("/usr/bin/docker", "restart", "--time", "10", "dcgm-exporter"))
        self.assertEqual(spec.timeout_seconds, 45.0)
        self.assertFalse(spec.accepts_container_ids)


class LedgerPermissionTests(unittest.TestCase):
    def test_unsafe_ledger_directory_is_refused(self) -> None:
        def loose(harness: Harness) -> None:
            os.chmod(harness.ledger, 0o755)

        def group_readable(harness: Harness) -> None:
            os.chmod(harness.ledger, 0o750)

        def symlinked(harness: Harness) -> None:
            real = harness.root / "real-ledger"
            real.mkdir()
            os.chmod(real, 0o700)
            harness.ledger.rmdir()
            harness.ledger.symlink_to(real)

        def wrong_owner(harness: Harness) -> None:
            harness.env.ledger_owner_uid = os.getuid() + 1

        def missing(harness: Harness) -> None:
            harness.ledger.rmdir()

        def regular_file(harness: Harness) -> None:
            harness.ledger.rmdir()
            harness.ledger.write_text("", encoding="ascii")
            os.chmod(harness.ledger, 0o700)

        for arrange in (loose, group_readable, symlinked, wrong_owner, missing, regular_file):
            for operation in ("restart", "result"):
                with self.subTest(case=arrange.__name__, operation=operation):
                    harness = Harness(self)
                    arrange(harness)
                    response, exit_code, _text = harness.run(command(operation))
                    self.assertEqual(exit_code, 0)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["reason"], "ledger_unavailable")
                    self.assertEqual(harness.docker.calls, [])

    def test_record_with_loose_mode_or_symlink_is_not_trusted(self) -> None:
        harness = Harness(self)
        harness.run(command("restart"))
        path = harness.ledger / f"{EXECUTION_ID}.json"
        os.chmod(path, 0o644)
        calls = list(harness.docker.calls)
        response, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual(response["state"], "unknown")
        self.assertEqual(response["reason"], "ledger_record_invalid")

        other = "12345678-1234-4234-8234-123456789abc"
        (harness.ledger / f"{other}.json").symlink_to(path)
        response, _exit_code, _text = harness.run(command("restart", other))
        self.assertEqual(response["reason"], "ledger_record_invalid")
        self.assertEqual(harness.docker.calls, calls)


class ResultTests(unittest.TestCase):
    def test_result_for_unknown_and_known_ids(self) -> None:
        harness = Harness(self)
        # A run holding the lock may be about to claim this ID.
        blocker = act.Ledger(harness.ledger, os.getuid())
        self.assertTrue(blocker.try_lock())
        response, exit_code, _text = harness.run(command("result"))
        self.assertEqual(set(response), ENVELOPE_KEYS | {"reason"})
        self.assertEqual(response["reason"], "execution_in_progress")
        self.assertEqual(harness.ledger_files(), [])
        blocker.close()
        # Otherwise the absent ID is recorded as never started, and a late restart
        # request with that ID replays the refusal.
        response, exit_code, _text = harness.run(command("result"))
        self.assertEqual(exit_code, 0)
        self.assertEqual((response["state"], response["reason"]), ("refused", "execution_not_started"))
        self.assertFalse(response["ok"])
        late, _exit_code, _text = harness.run(command("restart"))
        self.assertEqual((late["state"], late["reason"], late["replayed"]), ("refused", "execution_not_started", True))
        self.assertEqual(harness.docker.calls, [])

        harness = Harness(self)
        restart, _exit_code, _text = harness.run(command("restart"))
        calls = list(harness.docker.calls)
        result, exit_code, _text = harness.run(command("result"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(set(result), EXECUTION_KEYS)
        self.assertEqual(result["operation"], "result")
        self.assertTrue(result["ok"])
        for field in act.EXECUTION_FIELDS:
            self.assertEqual(result[field], restart[field], field)
        self.assertEqual(harness.docker.calls, calls)


class OutputBoundTests(unittest.TestCase):
    def test_oversized_status_falls_back_to_bounded_failure(self) -> None:
        harness = Harness(self)
        harness.docker.containers = [
            {
                "id": container_id(100 + index),
                "name": f"C.{index:04d}" + "x" * 96,
                "started_at": "2026-09-16T13:38:50Z",
                "devices": ["/dev/kvm"],
            }
            for index in range(240)
        ]
        response, exit_code, text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(exit_code, 0)
        self.assertLessEqual(len(text.encode("ascii")), act.MAX_OUTPUT_BYTES)
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "output_limit")
        self.assertLessEqual(ENVELOPE_KEYS, set(response))

    def test_oversized_restart_response_keeps_its_outcome(self) -> None:
        harness = Harness(self)
        harness.docker.containers = [
            {
                "id": container_id(100 + index),
                "name": f"C.{index:04d}" + "x" * 96,
                "started_at": "2026-09-16T13:38:50Z",
                "devices": [],
            }
            for index in range(200)
        ]
        response, exit_code, text = harness.run(command("restart", EXECUTION_ID))
        self.assertEqual(exit_code, 0)
        self.assertLessEqual(len(text.encode("ascii")), act.MAX_OUTPUT_BYTES)
        self.assertTrue(response["truncated"])
        self.assertEqual((response["state"], response["ok"]), ("executed", True))
        self.assertNotIn("reason", response)
        self.assertNotEqual(response["started_at_before"], response["started_at_after"])
        # The ledger keeps the complete record.
        self.assertEqual(len(harness.record(EXECUTION_ID)["tenants_before"]["members"]), 200)

    def test_every_normal_response_is_within_bound(self) -> None:
        harness = Harness(self)
        for operation in ("status", "restart", "result"):
            _response, _exit_code, text = harness.run(command(operation))
            self.assertLessEqual(len(text.encode("ascii")), act.MAX_OUTPUT_BYTES)


class SubprocessTests(unittest.TestCase):
    def test_run_command_rejects_unexpected_arguments_without_launch(self) -> None:
        with mock.patch.object(act, "_bounded_exec") as bounded:
            self.assertEqual(act.run_command("shell").failure, "unknown_command")
            self.assertEqual(
                act.run_command("exporter_restart", (container_id(1),)).failure, "invalid_arguments"
            )
            self.assertEqual(act.run_command("tenant_inspect", ()).failure, "invalid_arguments")
            for bad in ("A" * 64, "--format={{json .Config.Env}}", "0" * 63, "0" * 64 + ";"):
                self.assertEqual(act.run_command("tenant_inspect", (bad,)).failure, "invalid_arguments")
            bounded.assert_not_called()
            act.run_command("tenant_inspect", (container_id(1),))
        spec = act.COMMANDS["tenant_inspect"]
        bounded.assert_called_once_with(
            spec.argv + (container_id(1),), spec.timeout_seconds, merge_stderr=False
        )

    def test_bounded_exec_uses_fixed_environment_and_no_shell(self) -> None:
        # The interpreter itself reports its environment: /usr/bin/env is absent from
        # the Nix build sandbox, and Python adds no variables of its own.
        code = "import os; print(sorted(os.environ.items()))"
        with mock.patch.dict(os.environ, {"TERRACOMPUTE_INHERITED": "leak"}), \
                mock.patch.object(act.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            result = act._bounded_exec((sys.executable, "-I", "-c", code), 10.0)
        self.assertEqual(result.returncode, 0, result.failure)
        environment = dict(ast.literal_eval(result.stdout.strip()))
        self.assertEqual(environment["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertNotIn("TERRACOMPUTE_INHERITED", environment)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def test_bounded_exec_returns_stdout_only(self) -> None:
        code = "import sys; sys.stderr.write('private'); sys.stdout.write('public')"
        result = act._bounded_exec((sys.executable, "-c", code), 10.0)
        self.assertEqual((result.returncode, result.stdout, result.failure), (0, "public", None))

    def test_bounded_exec_timeout_kills_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            code = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()
            result = act._bounded_exec((sys.executable, "-c", code, str(pid_file)), 3.0)
            self.assertEqual(result.failure, "timeout")
            self.assertIsNone(result.returncode)
            self.assertLess(time.monotonic() - started, 10.0)
            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("descendant survived the timeout")

    def test_bounded_exec_output_limit_and_launch_failure(self) -> None:
        code = "import sys; sys.stdout.write('x' * 200000)"
        result = act._bounded_exec((sys.executable, "-c", code), 10.0, max_output_bytes=1024)
        self.assertEqual(result.failure, "output_limit")
        self.assertEqual(result.stdout, "")
        missing = act._bounded_exec(("/nonexistent/terracompute-docker",), 1.0)
        self.assertEqual(missing.failure, "launch_failed")


class MainTests(unittest.TestCase):
    def test_main_ignores_argv_and_stdin(self) -> None:
        harness = Harness(self)
        with mock.patch("sys.argv", [str(ACT_PATH), "restart", "dcgm-exporter", EXECUTION_ID]):
            with mock.patch("sys.stdin", io.StringIO(command("restart"))):
                response, exit_code, _text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(exit_code, 0)
        self.assertEqual(response["operation"], "status")
        self.assertNotIn("exporter_restart", harness.docker.commands())

    def test_unexpected_failure_keeps_the_schema_and_exits_1(self) -> None:
        harness = Harness(self)

        def broken() -> list[dict[str, object]]:
            raise RuntimeError("private detail")

        harness.env.gpu_reader = broken
        response, exit_code, text = harness.run(command("status", REQUEST_ID))
        self.assertEqual(exit_code, 1)
        self.assertEqual(response["reason"], "internal_error")
        self.assertEqual(response["operation"], "status")
        self.assertEqual(response["id"], REQUEST_ID)
        self.assertNotIn("private detail", text)

    def test_main_reads_only_ssh_original_command(self) -> None:
        harness = Harness(self)
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": command("status", REQUEST_ID)}):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = act.main(environment=harness.env)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], REQUEST_ID)


class ShellScriptTests(unittest.TestCase):
    SCRIPTS = (
        ROOT / "target" / "install-actor.sh",
        ROOT / "target" / "update-observer.sh",
        ROOT / "target" / "update-actor.sh",
    )

    def test_shell_scripts_parse(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is unavailable")
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [shell, "-n", str(script)], capture_output=True, text=True, timeout=10, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unfilled_placeholders_fail_before_any_host_check(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is unavailable")
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                if not re.search(r"^expected_\w+=REPLACE_WITH_", text, re.MULTILINE):
                    self.skipTest("pinned values have been filled in")
                # Run only the prologue: assignments, fail() and the placeholder guard.
                prologue = text[: text.index('[ "$(id -u)" -eq 0 ]')]
                self.assertIsNone(
                    re.search(
                        r"\b(useradd|usermod|mv|rm|chmod|chown|mktemp|visudo|tee)\b|^\s*install\s|>\s*[\"$/]",
                        prologue,
                        re.MULTILINE,
                    )
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "prologue.sh"
                    path.write_text(prologue, encoding="utf-8")
                    result = subprocess.run(
                        [shell, str(path)], capture_output=True, text=True, timeout=10, check=False
                    )
                self.assertEqual(result.returncode, 1)
                self.assertIn("placeholder", result.stderr)

    def test_install_actor_writes_the_exact_access_rules(self) -> None:
        text = (ROOT / "target" / "install-actor.sh").read_text(encoding="utf-8")
        self.assertIn("actor=terracompute-actor", text)
        self.assertIn("helper=/usr/local/libexec/terracompute-act", text)
        self.assertIn("home=/var/empty/terracompute-actor", text)
        self.assertIn("sudoers=/etc/sudoers.d/terracompute-actor", text)
        self.assertIn(
            "printf 'restrict,command=\"/usr/bin/sudo -n %s\" %s\\n' \"$helper\" \"$public_key\"",
            text,
        )
        self.assertIn(
            'Defaults:$actor env_reset,env_keep="SSH_ORIGINAL_COMMAND",secure_path=$secure_path',
            text,
        )
        # The empty argument list forbids any helper arguments through sudo.
        self.assertIn('$actor ALL=(root) NOPASSWD: $helper ""\n', text)
        self.assertIn('install -d -o root -g root -m 0700 "$state"', text)
        self.assertIn('install -d -o root -g root -m 0700 "$ledger"', text)
        self.assertIn("visudo -cf", text)
        self.assertIn("sshd -t", text)
        self.assertNotRegex(text, r"(useradd|usermod)[^\n]*(-G|--groups)")

    def test_update_actor_replaces_only_the_helper_and_keeps_a_backup(self) -> None:
        text = (ROOT / "target" / "update-actor.sh").read_text(encoding="utf-8")
        self.assertIn("expected_helper_sha256=REPLACE_WITH_HELPER_SHA256", text)
        self.assertIn('install -o root -g root -m 0700 "$helper" "$previous"', text)
        self.assertIn('mktemp "$helper_directory/.terracompute-act.XXXXXX"', text)
        self.assertIn('mv -f "$staged" "$helper"', text)
        self.assertLess(
            text.index('install -o root -g root -m 0700 "$helper" "$previous"'),
            text.index('mv -f "$staged" "$helper"'),
        )
        # It waits for any execution in flight rather than racing it.
        self.assertIn("flock --exclusive --timeout 120", text)
        self.assertLess(text.index("flock --exclusive"), text.index('mv -f "$staged" "$helper"'))
        # The account, key, sudoers rule and ledger are never touched.
        for forbidden in ("useradd", "usermod", "authorized_keys", "visudo -cf", "rm -rf"):
            self.assertNotIn(forbidden, text)
        self.assertIn("/etc/sudoers.d/terracompute-actor", text)

    def test_update_observer_keeps_a_root_only_backup_and_renames_atomically(self) -> None:
        text = (ROOT / "target" / "update-observer.sh").read_text(encoding="utf-8")
        self.assertIn("expected_probe_sha256=REPLACE_WITH_PROBE_SHA256", text)
        self.assertIn('install -o root -g root -m 0700 "$probe" "$previous"', text)
        self.assertIn('mktemp "$probe_directory/.terracompute-observe.XXXXXX"', text)
        self.assertIn('mv -f "$staged" "$probe"', text)
        self.assertLess(
            text.index('install -o root -g root -m 0700 "$probe" "$previous"'),
            text.index('mv -f "$staged" "$probe"'),
        )


class SessionChannelTests(unittest.TestCase):
    """The channel that lets the agent manage the machine instead of picking from a list.

    What is checked here is not a vocabulary. It is the three things that hold when the
    vocabulary is gone: the command never reaches sshd, other people's data is walled
    off, and nothing runs without being written down first.
    """

    def setUp(self) -> None:
        self.harness = Harness(self)
        self.payload = "systemctl status docker"
        self.launcher: str | None = "/usr/bin/systemd-run"
        self.outcome = act.CommandResult(0, stdout="active (running)\n")
        self.ran: list[tuple[str, str, bool]] = []
        self.audited: list[tuple[str, str, bool]] = []
        self.harness.env = dataclasses.replace(
            self.harness.env,
            session_payload_reader=lambda: self.payload,
            session_launcher=lambda: self.launcher,
            session_runner=self._run,
            session_auditor=lambda request_id, script, writable: self.audited.append(
                (request_id, script, writable)
            ),
        )

    def _run(self, launcher: str, script: str, *, writable: bool) -> act.CommandResult:
        self.ran.append((launcher, script, writable))
        return self.outcome

    def session(self, command: str = f"session-v2 host {REQUEST_ID} {BOOT_ID}"):
        return self.harness.run(command)

    # -- the grammar -----------------------------------------------------------

    def test_the_command_never_travels_through_ssh(self) -> None:
        """A session's payload is on stdin; only its boot binding is on the command."""
        request = act.parse_request(f"session-v2 host {REQUEST_ID} {BOOT_ID}")
        self.assertIsNotNone(request)
        self.assertEqual((request.operation, request.component), ("session-v2", "host"))
        # The component slot names the host and nothing else, and the id is still a uuid.
        for rejected in (
            f"session dcgm-exporter {REQUEST_ID}",
            f"session host {REQUEST_ID} extra",
            f"session host {REQUEST_ID}",
            f"session-v2 host {REQUEST_ID} not-a-uuid",
            "session host not-a-uuid",
            f"session {REQUEST_ID}",
            f"sessions host {REQUEST_ID}",
        ):
            with self.subTest(command=rejected):
                self.assertIsNone(act.parse_request(rejected))
        # Everything the grammar refused before, it still refuses.
        self.assertIsNone(act.parse_request(f"session host {REQUEST_ID}; id"))
        self.assertIsNone(act.parse_request("a" * 257))

    # -- the boundary ----------------------------------------------------------

    def test_what_the_machine_itself_said_holds_other_peoples_data(self) -> None:
        """Enumerated against 17049 on 2026-09-19, and pinned by name for that reason.

        The docker paths were a reasonable guess and the guess was short. Vast keeps
        `/var/lib/vastai_kaalia/data/<rental>` and bind-mounts it into the renter's own
        container, and the same directory holds `api_key` at mode 0644 -- the credential
        that lists, unlists and destroys rentals here. A session runs as root, so the
        mode stops nobody. Iterating the tuple proves the walls are applied; only naming
        them proves the right things are in it.
        """
        self.assertIn("/var/lib/vastai_kaalia/data", act.TENANT_DATA_PATHS)
        self.assertIn("/var/lib/vastai_kaalia/api_key", act.TENANT_DATA_PATHS)
        # And not the tree around them: its logs are what a diagnosis reads.
        self.assertNotIn("/var/lib/vastai_kaalia", act.TENANT_DATA_PATHS)
        observe = act.session_argv("/bin/systemd-run", "true", writable=False)
        manage = act.session_argv("/bin/systemd-run", "true", writable=True)
        for argv in (observe, manage):
            self.assertIn(
                "--property=InaccessiblePaths=-/var/lib/vastai_kaalia/data", argv,
                "the renter's own files were reachable",
            )
            self.assertIn(
                "--property=InaccessiblePaths=-/var/lib/vastai_kaalia/api_key", argv,
                "this machine's API key was reachable",
            )
        # The renter's VM is driven through libvirt, and root reaches the socket
        # whatever its mode says. Walled from a look, kept for an approved change.
        self.assertIn(
            "--property=BindReadOnlyPaths=-/dev/null:/run/libvirt/libvirt-sock", observe)
        self.assertNotIn(
            "--property=BindReadOnlyPaths=-/dev/null:/run/libvirt/libvirt-sock", manage)

    def test_other_peoples_data_is_walled_off_and_the_script_is_not_interpolated(self) -> None:
        for writable in (True, False):
            argv = act.session_argv("/usr/bin/systemd-run", "echo hello; rm -rf /",
                                    writable=writable)
            for path in act.TENANT_DATA_PATHS:
                self.assertIn(f"--property=InaccessiblePaths=-{path}", argv)
            self.assertIn(f"--property=RuntimeMaxSec={int(act.SESSION_SECONDS)}", argv)
            # The script is one argv element, handed to sh as data. Nothing this helper
            # builds can be split by it, whatever it contains.
            self.assertEqual(argv[-3:], ("/bin/sh", "-c", "echo hello; rm -rf /"))

    def test_observation_is_read_only_and_management_is_not(self) -> None:
        """The difference between looking and changing is a kernel property, not trust."""
        observe = act.session_argv("/usr/bin/systemd-run", "cat /proc/uptime", writable=False)
        manage = act.session_argv("/usr/bin/systemd-run", "docker pull x", writable=True)
        self.assertIn("--property=ProtectSystem=strict", observe)
        self.assertIn("--property=ProtectHome=read-only", observe)
        # The management profile keeps write access; that is the point of it.
        self.assertNotIn("--property=ProtectSystem=strict", manage)
        self.assertNotIn("--property=ProtectHome=read-only", manage)
        # Tenant data files are walled off from both, writable or not.
        for argv in (observe, manage):
            for path in act.TENANT_DATA_PATHS:
                self.assertIn(f"--property=InaccessiblePaths=-{path}", argv)

    def test_a_read_only_key_cannot_reach_the_operations_that_change_the_machine(self) -> None:
        """The investigator's key can look at the target but never change it.

        The mode comes from the forced command's own argv, which the target sets, not
        from the request the client sends -- so the client cannot ask its way out of it.
        """
        self.payload = "cat /proc/uptime"
        # restart and the writable session are refused, with no attempt to run them.
        for command in (f"restart dcgm-exporter {EXECUTION_ID}",
                        f"session-v2 host {REQUEST_ID} {BOOT_ID}"):
            with self.subTest(command=command):
                response, exit_code, _ = self.harness.run(command, argv=["readonly"])
                self.assertFalse(response["ok"])
                self.assertEqual(response["reason"], "operation_not_permitted_readonly")
                self.assertEqual(exit_code, 2)
        self.assertEqual(self.ran, [], "a forbidden op must not run")
        # observe still works read-only, and runs read-only.
        response, exit_code, _ = self.harness.run(f"observe host {REQUEST_ID}", argv=["readonly"])
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.ran[-1][2], False)
        # Without the read-only argument, the same key would be the actor: session runs writable.
        self.harness.run(f"session-v2 host {REQUEST_ID} {BOOT_ID}")
        self.assertEqual(self.ran[-1][2], True)

    def test_observation_cannot_reach_tenant_data_through_the_runtime(self) -> None:
        """Walling the files is not enough: docker logs reaches the same data.

        An observation therefore loses containerd and libvirt outright -- they drive the
        same containers and the renter's VM below the layer anything can inspect. A
        management session keeps them, because a human approved it.
        """
        observe = act.session_argv("/usr/bin/systemd-run", "docker ps", writable=False)
        manage = act.session_argv("/usr/bin/systemd-run", "docker restart x", writable=True)
        self.assertNotIn("/run/docker.sock", act.RUNTIME_CONTROL_SOCKETS,
                         "docker is proxied, not blanked; it has its own test")
        for path in act.RUNTIME_CONTROL_SOCKETS:
            # Bound over, not made inaccessible: systemd ignores InaccessiblePaths on
            # a path, so that spelling walled nothing at all.
            self.assertIn(f"--property=BindReadOnlyPaths=-/dev/null:{path}", observe)
            self.assertNotIn(f"--property=BindReadOnlyPaths=-/dev/null:{path}", manage)
            self.assertNotIn(f"--property=InaccessiblePaths=-{path}", observe)

    def test_an_observation_gets_the_docker_proxy_when_it_is_running(self) -> None:
        """Blanking docker was too blunt: the agent's own monitoring lives in it.

        On 2026-09-18 a real diagnosis asked for the container config that held the root
        cause, was refused by the blank, and reported a plausible wrong answer. It now
        gets a socket that is real and cannot touch a tenant.
        """
        with mock.patch.object(act, "_is_socket", return_value=True):
            observe = act.session_argv("/usr/bin/systemd-run", "docker ps", writable=False)
            manage = act.session_argv("/usr/bin/systemd-run", "docker restart x", writable=True)
        for path in act.PROXIED_SOCKETS:
            with self.subTest(path):
                self.assertIn(
                    f"--property=BindReadOnlyPaths=-{act.RUNTIME_PROXY_SOCKET}:{path}",
                    observe, "an observation could not reach docker at all",
                )
                # Not blanked as well. Given two binds for one destination systemd
                # keeps the FIRST, measured on imladris on 2026-09-18, so listing both
                # would leave the path blanked and the proxy unreachable.
                self.assertNotIn(f"--property=BindReadOnlyPaths=-/dev/null:{path}", observe)
        # A management session talks to the real dockerd; a person approved that.
        self.assertNotIn(act.RUNTIME_PROXY_SOCKET, " ".join(manage))

    def test_an_observation_loses_docker_entirely_when_the_proxy_is_not_running(self) -> None:
        """The wall must not open on the day the proxy is down.

        Naming the proxy with a leading "-" would have done exactly that: systemd
        ignores a bind whose source is missing, and the REAL socket stays in place. So
        the helper looks first and blanks the socket when there is nothing listening.
        """
        with mock.patch.object(act, "_is_socket", return_value=False):
            observe = act.session_argv("/usr/bin/systemd-run", "docker ps", writable=False)
        for path in act.PROXIED_SOCKETS:
            with self.subTest(path):
                self.assertIn(f"--property=BindReadOnlyPaths=-/dev/null:{path}", observe)
                self.assertNotIn(act.RUNTIME_PROXY_SOCKET, " ".join(observe))

    def test_whether_the_proxy_is_running_is_asked_of_the_filesystem(self) -> None:
        """A regular file, a missing path or an unreadable one are all "no"."""
        directory = tempfile.mkdtemp()
        regular = os.path.join(directory, "not-a-socket")
        Path(regular).write_text("")
        self.assertFalse(act._is_socket(regular))
        self.assertFalse(act._is_socket(os.path.join(directory, "missing")))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        real = os.path.join(directory, "real.sock")
        listener.bind(real)
        try:
            self.assertTrue(act._is_socket(real))
        finally:
            listener.close()

    def test_the_grammar_names_both_host_verbs_and_marks_the_writable_one(self) -> None:
        for verb in ("observe", "session-v2"):
            command = (f"{verb} host {REQUEST_ID} {BOOT_ID}" if verb == "session-v2"
                       else f"{verb} host {REQUEST_ID}")
            request = act.parse_request(command)
            self.assertIsNotNone(request)
            self.assertEqual(request.operation, verb)
        # observe runs read-only and says so; session runs writable and says so.
        self.payload = "cat /proc/uptime"
        observed, _exit, _text = self.harness.run(f"observe host {REQUEST_ID}")
        self.assertFalse(observed["writable"])
        self.assertEqual(self.ran[-1][2], False)
        managed, _exit, _text = self.harness.run(f"session-v2 host {REQUEST_ID} {BOOT_ID}")
        self.assertTrue(managed["writable"])
        self.assertEqual(self.ran[-1][2], True)

    def test_without_the_boundary_nothing_runs_at_all(self) -> None:
        """No systemd-run means no tenant wall, and running anyway would remove it."""
        self.launcher = None
        response, exit_code, _ = self.session()
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "session_boundary_unavailable")
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.ran, [], "ran with no boundary in place")

    def test_reboot_between_approval_check_and_command_launch_is_refused(self) -> None:
        boot = [BOOT_ID]
        def audit(request_id, script, writable):
            self.audited.append((request_id, script, writable))
            boot[0] = "22222222-3333-4444-8555-666666666666"
        self.harness.env = dataclasses.replace(self.harness.env,
            boot_id_reader=lambda: boot[0], session_auditor=audit)
        response, _exit, _text = self.session()
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "session_boot_or_host_mismatch")
        self.assertEqual(response["session_capability"], "boot-bound-v2")
        self.assertEqual(self.ran, [])

    # -- the record ------------------------------------------------------------

    def test_what_was_asked_for_is_written_down_before_it_runs(self) -> None:
        self.session()
        self.assertEqual(self.audited, [(REQUEST_ID, "systemctl status docker", True)])
        self.assertEqual(len(self.ran), 1)

    def test_a_command_that_fails_is_still_on_the_record(self) -> None:
        """A session that breaks the machine must not be the one nobody wrote down."""
        self.outcome = act.CommandResult(None, failure="timeout")
        response, _exit, _text = self.session()
        self.assertEqual(self.audited, [(REQUEST_ID, "systemctl status docker", True)])
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "session_timeout")

    def test_the_real_auditor_records_the_command_and_its_digest(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.object(act, "LEDGER_DIRECTORY", directory / "state" / "ledger"):
            act._record_session(REQUEST_ID, "docker restart dcgm-exporter")
        written = (directory / "state" / "session.log").read_text(encoding="utf-8")
        record = json.loads(written.strip())
        self.assertEqual(record["request"], REQUEST_ID)
        self.assertEqual(record["script"], "docker restart dcgm-exporter")
        self.assertEqual(
            record["sha256"],
            hashlib.sha256(b"docker restart dcgm-exporter").hexdigest(),
        )
        self.assertEqual(oct(os.stat(directory / "state" / "session.log").st_mode)[-3:], "600")

    def test_an_unwritable_ledger_does_not_stop_the_work(self) -> None:
        """Best effort: a full disk must not be why the agent cannot do its job."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.object(act, "LEDGER_DIRECTORY", directory / "state" / "ledger"):
            with mock.patch("builtins.open", side_effect=OSError("no space left")):
                act._record_session(REQUEST_ID, "echo fine")  # must not raise

    # -- what comes back -------------------------------------------------------

    def test_output_is_bounded_and_printable_and_the_exit_code_survives(self) -> None:
        noisy = "\n".join(f"line-{index}\x07" for index in range(act.SESSION_OUTPUT_LINES + 25))
        self.outcome = act.CommandResult(3, stdout=noisy)
        response, _exit, _text = self.session()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["exit_code"], 3)
        self.assertEqual(len(response["lines"]), act.SESSION_OUTPUT_LINES)
        self.assertTrue(response["truncated"])
        self.assertNotIn("\x07", "".join(response["lines"]))

    def test_a_long_json_line_comes_back_whole_and_the_total_is_bounded(self) -> None:
        """2026-09-25: every JSON `docker inspect` came back cut at 300 characters."""
        inspect = '{"HostConfig": ' + '"x"' * 900 + '}'
        self.outcome = act.CommandResult(0, stdout=inspect)
        response, _exit, _text = self.session()
        self.assertEqual(response["lines"], [inspect])
        huge = "\n".join("y" * 3000 for _ in range(500))
        self.outcome = act.CommandResult(0, stdout=huge)
        response, _exit, _text = self.session()
        self.assertLessEqual(sum(len(line) + 1 for line in response["lines"]), act.SESSION_KEPT_BYTES)
        self.assertTrue(response["truncated"])

    def test_an_empty_or_unreadable_payload_is_refused(self) -> None:
        for payload, reason in (("", "session_payload_missing"),
                                ("   \n ", "session_payload_missing"),
                                ("echo \x00 hi", "session_payload_invalid")):
            with self.subTest(payload=payload):
                self.payload = payload
                response, _exit, _text = self.session()
                self.assertFalse(response["ok"])
                self.assertEqual(response["reason"], reason)
                self.assertEqual(self.ran, [])

    def test_an_oversized_payload_never_reaches_a_shell(self) -> None:
        def too_big() -> str:
            raise ValueError("session payload exceeds bound")

        self.harness.env = dataclasses.replace(
            self.harness.env, session_payload_reader=too_big
        )
        response, _exit, _text = self.session()
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "session_payload_unreadable")
        self.assertEqual(self.ran, [])

    def test_the_real_payload_reader_bounds_what_it_reads(self) -> None:
        oversized = io.BytesIO(b"x" * (act.MAX_SESSION_PAYLOAD_BYTES + 1))
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=oversized)):
            with self.assertRaises(ValueError):
                act._read_session_payload()
        exact = io.BytesIO(b"y" * act.MAX_SESSION_PAYLOAD_BYTES)
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=exact)):
            self.assertEqual(len(act._read_session_payload()), act.MAX_SESSION_PAYLOAD_BYTES)


if __name__ == "__main__":
    unittest.main()
