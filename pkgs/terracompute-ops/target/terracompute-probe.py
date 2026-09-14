#!/usr/bin/env python3
"""Read-only, forced-command observation probe for the terracompute target.

The executable intentionally has no command-line or stdin interface.  Every
external command is selected from the fixed COMMANDS table below.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import selectors
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


MACHINE_ID = 17049
EXPECTED_GPU_COUNT = 8
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PCI_DEVICES_PATH = Path("/sys/bus/pci/devices")
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_JSON_OUTPUT_BYTES = 512 * 1024
MAX_EVENTS = 96
MAX_EVENT_COUNT = 999
MAX_CONTAINERS = 256
MAX_CONTAINER_NAME_CHARS = 128
MAX_CONTAINER_STATUS_CHARS = 256


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float


# These are the complete set of subprocess argument vectors the probe can run.
COMMANDS: dict[str, CommandSpec] = {
    "gpu": CommandSpec(
        (
            "/usr/bin/nvidia-smi",
            "--query-gpu=uuid,pci.bus_id,name,temperature.gpu,pstate",
            "--format=csv,noheader,nounits",
        ),
        8.0,
    ),
    "kernel_journal": CommandSpec(
        (
            "/usr/bin/journalctl",
            "--boot=0",
            "-k",
            "--since=-15min",
            "--no-pager",
            "--output=cat",
        ),
        8.0,
    ),
    "docker_journal": CommandSpec(
        (
            "/usr/bin/journalctl",
            "--boot=0",
            "--unit=docker.service",
            "--since=-15min",
            "--no-pager",
            "--output=cat",
        ),
        8.0,
    ),
    "service_vastai": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "vastai.service"), 4.0
    ),
    "service_docker": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "docker.service"), 4.0
    ),
    "service_nvidia_persistenced": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "nvidia-persistenced.service"), 4.0
    ),
    "docker_metadata": CommandSpec(
        (
            "/usr/bin/docker",
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .Names}}\t{{json .Status}}",
        ),
        8.0,
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str = ""
    failure: str | None = None


def _start_process(spec: CommandSpec) -> subprocess.Popen[bytes]:
    """Start one catalogued command without a shell or inherited stdin."""
    return subprocess.Popen(
        list(spec.argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
    )


def _bounded_exec(spec: CommandSpec) -> CommandResult:
    """Capture a command while enforcing wall-clock and combined-output bounds."""
    try:
        process = _start_process(spec)
    except (OSError, subprocess.SubprocessError):
        return CommandResult(None, failure="launch_failed")

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        return CommandResult(None, failure="capture_failed")

    selector = selectors.DefaultSelector()
    captured = bytearray()
    total_output_bytes = 0
    deadline = time.monotonic() + spec.timeout_seconds
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        while selector.get_map():
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                process.kill()
                process.wait()
                return CommandResult(None, failure="timeout")
            for key, _mask in selector.select(remaining_time):
                remaining_bytes = MAX_COMMAND_OUTPUT_BYTES - total_output_bytes
                if remaining_bytes <= 0:
                    process.kill()
                    process.wait()
                    return CommandResult(None, failure="output_limit")
                try:
                    chunk = os.read(key.fd, min(65536, remaining_bytes + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > remaining_bytes:
                    process.kill()
                    process.wait()
                    return CommandResult(None, failure="output_limit")
                total_output_bytes += len(chunk)
                # stderr is deliberately consumed but never returned or reported.
                if key.fileobj is process.stdout:
                    captured.extend(chunk)

        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            process.kill()
            process.wait()
            return CommandResult(None, failure="timeout")
        try:
            returncode = process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return CommandResult(None, failure="timeout")
    except (OSError, ValueError, subprocess.SubprocessError):
        process.kill()
        process.wait()
        return CommandResult(None, failure="capture_failed")
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    return CommandResult(
        returncode,
        captured.decode("utf-8", errors="replace"),
    )


def run_command(command_id: str) -> CommandResult:
    """Run only a named, immutable command from the catalog."""
    spec = COMMANDS.get(command_id)
    if spec is None:
        return CommandResult(None, failure="unknown_command")
    return _bounded_exec(spec)


def _event(
    family: str,
    code: str,
    severity: str,
    message: str,
    evidence: dict[str, object] | None = None,
    count: int = 1,
) -> dict[str, object]:
    return {
        "fault_family": family,
        "code": code,
        "severity": severity,
        "message": message,
        "count": max(1, min(count, MAX_EVENT_COUNT)),
        "evidence": evidence or {},
    }


def _probe_failure(collector: str, failure: str = "invalid_output") -> dict[str, object]:
    allowed_failures = {
        "launch_failed",
        "capture_failed",
        "timeout",
        "output_limit",
        "nonzero_exit",
        "invalid_output",
    }
    normalized = failure if failure in allowed_failures else "invalid_output"
    return _event(
        "probe",
        f"{collector}_{normalized}",
        "error",
        f"Required {collector} collector failed",
        {"collector": collector, "failure": normalized},
    )


def _command_failure(collector: str, result: CommandResult) -> dict[str, object] | None:
    if result.failure:
        return _probe_failure(collector, result.failure)
    if result.returncode != 0:
        return _probe_failure(collector, "nonzero_exit")
    return None


_UUID_RE = re.compile(r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_BDF_RE = re.compile(
    r"(?<![0-9a-fA-F])(?P<bdf>[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?)(?![0-9a-fA-F])"
)
_XID_RE = re.compile(
    r"Xid\s*\(PCI:(?P<bdf>[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?)\)\s*:\s*(?P<xid>[0-9]{1,4})",
    re.IGNORECASE,
)
_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)


def _normalize_bdf(value: str) -> str | None:
    match = _BDF_RE.fullmatch(value.strip())
    if not match:
        return None
    domain, bus, slot_function = match.group("bdf").lower().split(":")
    if "." not in slot_function:
        slot_function += ".0"
    return f"{int(domain, 16):04x}:{bus}:{slot_function}"


def _parse_gpus(text: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    gpus: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    seen_uuids: set[str] = set()
    seen_bdfs: set[str] = set()
    try:
        rows = csv.reader(io.StringIO(text))
        for row in rows:
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) != 5:
                raise ValueError
            uuid, raw_bdf, name, raw_temperature, pstate = (
                field.strip() for field in row
            )
            bdf = _normalize_bdf(raw_bdf)
            if not _UUID_RE.fullmatch(uuid) or bdf is None:
                raise ValueError
            if uuid in seen_uuids or bdf in seen_bdfs:
                raise ValueError
            temperature = int(raw_temperature)
            if not 0 <= temperature <= 125:
                raise ValueError
            if not re.fullmatch(r"P(?:[0-9]|1[0-5])", pstate):
                raise ValueError
            clean_name = _clean_text(name, 96)
            if not clean_name:
                raise ValueError
            seen_uuids.add(uuid)
            seen_bdfs.add(bdf)
            gpus.append(
                {
                    "uuid": uuid,
                    "pci_bdf": bdf,
                    "name": clean_name,
                    "temperature_c": temperature,
                    "pstate": pstate,
                }
            )
    except (csv.Error, TypeError, ValueError):
        return [], [_probe_failure("gpu_inventory")]

    gpus.sort(key=lambda gpu: (str(gpu["pci_bdf"]), str(gpu["uuid"])))
    for gpu in gpus:
        if int(gpu["temperature_c"]) >= 90:
            events.append(
                _event(
                    "gpu",
                    "gpu_temperature_high",
                    "error",
                    "GPU temperature is above the probe threshold",
                    {
                        "uuid": gpu["uuid"],
                        "pci_bdf": gpu["pci_bdf"],
                        "temperature_c": gpu["temperature_c"],
                    },
                )
            )
    return gpus, events


def _read_pci_gpus() -> list[dict[str, str]]:
    """Read the fixed sysfs PCI inventory without invoking tenant-controlled code."""
    devices: list[dict[str, str]] = []
    for device in sorted(PCI_DEVICES_PATH.iterdir(), key=lambda path: path.name):
        vendor = (device / "vendor").read_text(encoding="ascii").strip().lower()
        device_class = (device / "class").read_text(encoding="ascii").strip().lower()
        if vendor != "0x10de" or not device_class.startswith(("0x0300", "0x0302")):
            continue
        bdf = _normalize_bdf(device.name)
        if bdf is None:
            raise ValueError("invalid PCI device name")
        driver_path = device / "driver"
        driver = driver_path.resolve().name if driver_path.is_symlink() else "unbound"
        devices.append({"pci_bdf": bdf, "driver": driver})
        if len(devices) > 32:
            raise ValueError("PCI GPU inventory exceeds bound")
    return devices


def _correlate_gpu_inventory(
    visible_gpus: list[dict[str, object]], pci_gpus: list[dict[str, str]]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    visible_by_bdf = {str(gpu["pci_bdf"]): gpu for gpu in visible_gpus}
    pci_by_bdf = {gpu["pci_bdf"]: gpu for gpu in pci_gpus}
    if len(pci_gpus) != EXPECTED_GPU_COUNT:
        events.append(
            _event(
                "gpu",
                "pci_gpu_count_mismatch",
                "error",
                "Physical PCI GPU count differs from expected count",
                {"expected": EXPECTED_GPU_COUNT, "observed": len(pci_gpus)},
            )
        )
    for bdf, pci_gpu in sorted(pci_by_bdf.items()):
        if bdf in visible_by_bdf or pci_gpu["driver"] == "vfio-pci":
            continue
        events.append(
            _event(
                "gpu",
                "gpu_driver_unavailable",
                "error",
                "Physical GPU is unavailable to NVIDIA and is not assigned to VFIO",
                {"pci_bdf": bdf, "driver": pci_gpu["driver"]},
            )
        )
    for bdf in sorted(set(visible_by_bdf) - set(pci_by_bdf)):
        events.append(
            _event(
                "gpu",
                "gpu_missing_from_pci",
                "critical",
                "NVIDIA reported a GPU absent from the physical PCI inventory",
                {"pci_bdf": bdf, "uuid": visible_by_bdf[bdf]["uuid"]},
            )
        )
    snapshot = {
        "expected_count": EXPECTED_GPU_COUNT,
        "pci_count": len(pci_gpus),
        "nvidia_count": len(visible_gpus),
        "vfio_count": sum(gpu["driver"] == "vfio-pci" for gpu in pci_gpus),
        "gpus": visible_gpus,
        "pci_devices": pci_gpus,
    }
    return snapshot, events


def _deduplicated_lines(text: str) -> Iterable[str]:
    seen: set[str] = set()
    for line in text.splitlines():
        normalized = line.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized


def _parse_kernel_events(
    text: str, gpu_by_bdf: dict[str, str]
) -> list[dict[str, object]]:
    aggregates: dict[tuple[str, str, str, str], int] = {}
    for line in _deduplicated_lines(text):
        xid_match = _XID_RE.search(line)
        if xid_match:
            bdf = _normalize_bdf(xid_match.group("bdf")) or "unknown"
            xid = xid_match.group("xid")
            uuid = gpu_by_bdf.get(bdf, "unknown")
            severity = "critical" if xid == "79" else "error"
            key = (xid, severity, bdf, uuid)
            aggregates[key] = min(aggregates.get(key, 0) + 1, MAX_EVENT_COUNT)

        if "aer:" not in line.lower():
            continue
        bdf_match = _BDF_RE.search(line)
        bdf = _normalize_bdf(bdf_match.group("bdf")) if bdf_match else None
        lower = line.lower()
        if "uncorrected (fatal)" in lower or "fatal error" in lower:
            code, severity = "fatal", "fatal"
        elif "uncorrected" in lower or "non-fatal" in lower:
            code, severity = "nonfatal", "nonfatal"
        elif "corrected" in lower:
            code, severity = "correctable", "correctable"
        else:
            code, severity = "unknown", "warning"
        key = (code, severity, bdf or "unknown", "")
        aggregates[key] = min(aggregates.get(key, 0) + 1, MAX_EVENT_COUNT)

    events: list[dict[str, object]] = []
    for (code, severity, bdf, uuid), count in sorted(aggregates.items()):
        if uuid:
            xid = code
            events.append(
                _event(
                    "xid",
                    xid,
                    severity,
                    f"NVIDIA Xid {xid} observed in the current boot window",
                    {"xid": int(xid), "pci_bdf": bdf, "uuid": uuid},
                    count,
                )
            )
        else:
            events.append(
                _event(
                    "aer",
                    code,
                    severity,
                    "PCIe AER event observed in the current boot window",
                    {"pci_bdf": bdf},
                    count,
                )
            )
    return events


def _parse_cdi_events(text: str) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for line in _deduplicated_lines(text):
        lower = line.lower()
        if "cdi" not in lower:
            continue
        if any(token in lower for token in ("unresolvable", "unresolved", "cannot resolve")):
            kind = "unresolvable"
        elif (
            "failed to inject" in lower
            or "injection failed" in lower
            or ("inject" in lower and "failed" in lower)
        ):
            kind = "injection_failed"
        else:
            continue
        counts[kind] = min(counts.get(kind, 0) + 1, MAX_EVENT_COUNT)

    return [
        _event(
            "cdi",
            "device-missing" if kind == "unresolvable" else "injection-failed",
            "error",
            "Docker reported a CDI device injection failure",
            {"failure": kind},
            count,
        )
        for kind, count in sorted(counts.items())
    ]


def _clean_text(value: str, limit: int) -> str:
    return "".join(character if character.isprintable() else "?" for character in value)[
        :limit
    ]


def _parse_containers(text: str) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    containers: list[dict[str, str]] = []
    total = 0
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                raise ValueError
            name_value = json.loads(fields[0])
            status_value = json.loads(fields[1])
            if not isinstance(name_value, str) or not isinstance(status_value, str):
                raise ValueError
            name = _clean_text(name_value, MAX_CONTAINER_NAME_CHARS)
            status = _clean_text(status_value, MAX_CONTAINER_STATUS_CHARS)
            if not name or not status:
                raise ValueError
            total += 1
            if len(containers) < MAX_CONTAINERS:
                containers.append({"name": name, "status": status})
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, _probe_failure("docker_metadata")

    containers.sort(key=lambda container: (container["name"], container["status"]))
    return {
        "total_count": total,
        "listed_count": len(containers),
        "truncated": total > len(containers),
        "containers": containers,
    }, None


def _read_boot_id() -> str:
    return BOOT_ID_PATH.read_text(encoding="ascii").strip().lower()


def _utc_timestamp(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def collect_probe(
    runner: Callable[[str], CommandResult] = run_command,
    hostname_reader: Callable[[], str] = socket.gethostname,
    boot_id_reader: Callable[[], str] = _read_boot_id,
    pci_gpu_reader: Callable[[], list[dict[str, str]]] = _read_pci_gpus,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Collect one observation. Injectable readers exist solely for offline tests."""
    events: list[dict[str, object]] = []

    try:
        raw_target = hostname_reader()
        if not isinstance(raw_target, str):
            raise TypeError
        target = _clean_text(raw_target.strip().lower().rstrip("."), 253)
        if not _HOSTNAME_RE.fullmatch(target):
            raise ValueError
    except (OSError, TypeError, ValueError):
        target = "unknown"
        events.append(_probe_failure("hostname"))

    try:
        raw_boot_id = boot_id_reader()
        if not isinstance(raw_boot_id, str):
            raise TypeError
        boot_id = raw_boot_id.strip().lower()
        if not _BOOT_ID_RE.fullmatch(boot_id):
            raise ValueError
    except (OSError, UnicodeError, TypeError, ValueError):
        boot_id = "unknown"
        events.append(_probe_failure("boot_id"))

    gpu_result = runner("gpu")
    failure = _command_failure("gpu_inventory", gpu_result)
    if failure:
        gpus: list[dict[str, object]] = []
        events.append(failure)
    else:
        gpus, gpu_events = _parse_gpus(gpu_result.stdout)
        events.extend(gpu_events)
    gpu_by_bdf = {str(gpu["pci_bdf"]): str(gpu["uuid"]) for gpu in gpus}

    try:
        pci_gpus = pci_gpu_reader()
        if not isinstance(pci_gpus, list):
            raise TypeError
        gpu_snapshot, gpu_correlation_events = _correlate_gpu_inventory(gpus, pci_gpus)
        events.extend(gpu_correlation_events)
    except (OSError, UnicodeError, TypeError, ValueError):
        gpu_snapshot = {
            "expected_count": EXPECTED_GPU_COUNT,
            "pci_count": None,
            "nvidia_count": len(gpus),
            "vfio_count": None,
            "gpus": gpus,
            "pci_devices": [],
        }
        events.append(_probe_failure("pci_gpu_inventory"))

    kernel_result = runner("kernel_journal")
    failure = _command_failure("kernel_journal", kernel_result)
    if failure:
        events.append(failure)
    else:
        events.extend(_parse_kernel_events(kernel_result.stdout, gpu_by_bdf))

    docker_journal_result = runner("docker_journal")
    failure = _command_failure("docker_journal", docker_journal_result)
    if failure:
        events.append(failure)
    else:
        events.extend(_parse_cdi_events(docker_journal_result.stdout))

    service_ids = (
        ("vastai", "service_vastai"),
        ("docker", "service_docker"),
        ("nvidia-persistenced", "service_nvidia_persistenced"),
    )
    service_states: dict[str, str] = {}
    valid_states = {
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "unknown",
    }
    for service_name, command_id in service_ids:
        service_result = runner(command_id)
        collector_name = f"systemd_{service_name.replace('-', '_')}"
        if service_result.failure:
            events.append(_probe_failure(collector_name, service_result.failure))
            service_states[service_name] = "unknown"
            continue
        state = service_result.stdout.strip().lower()
        if state not in valid_states or (service_result.returncode == 0) != (state == "active"):
            events.append(_probe_failure(collector_name))
            service_states[service_name] = "unknown"
            continue
        service_states[service_name] = state
        if state != "active":
            events.append(
                _event(
                    "systemd",
                    f"service_{service_name.replace('-', '_')}_not_active",
                    "error",
                    f"Required service {service_name} is not active",
                    {"service": service_name, "state": state},
                )
            )
    containers_result = runner("docker_metadata")
    failure = _command_failure("docker_metadata", containers_result)
    metadata: dict[str, object] | None = None
    parse_failure: dict[str, object] | None = None
    if failure:
        events.append(failure)
    else:
        metadata, parse_failure = _parse_containers(containers_result.stdout)
        if parse_failure:
            events.append(parse_failure)

    if len(events) > MAX_EVENTS:
        original_count = len(events)
        events = events[: MAX_EVENTS - 1]
        events.append(
            _event(
                "probe",
                "event_limit_reached",
                "error",
                "Probe event limit reached",
                {"observed": original_count, "emitted": MAX_EVENTS},
            )
        )

    healthy = not events
    return {
        "target": target,
        "machine_id": MACHINE_ID,
        "boot_id": boot_id,
        "observed_at": _utc_timestamp(now),
        "healthy": healthy,
        "events": events,
        "snapshot": {
            "gpu": gpu_snapshot,
            "services": service_states,
            "docker": metadata,
        },
    }


def _encode_result(result: dict[str, object]) -> str:
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) < MAX_JSON_OUTPUT_BYTES:
        return encoded

    fallback = {
        "target": result.get("target", "unknown"),
        "machine_id": MACHINE_ID,
        "boot_id": result.get("boot_id", "unknown"),
        "observed_at": result.get("observed_at", _utc_timestamp()),
        "healthy": False,
        "events": [
            _event(
                "probe",
                "json_output_limit_reached",
                "error",
                "Probe JSON output limit reached",
            )
        ],
    }
    return json.dumps(fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def main() -> None:
    # Deliberately do not inspect argv, environment instructions, or stdin.
    try:
        result = collect_probe()
    except Exception:  # Last-resort schema preservation; exception details stay private.
        result = {
            "target": "unknown",
            "machine_id": MACHINE_ID,
            "boot_id": "unknown",
            "observed_at": _utc_timestamp(),
            "healthy": False,
            "events": [_probe_failure("probe")],
        }
    print(_encode_result(result), flush=True)


if __name__ == "__main__":
    main()
