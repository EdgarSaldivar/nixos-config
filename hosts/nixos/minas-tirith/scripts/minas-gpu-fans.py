#!/usr/bin/env python3
"""Fail-full, case-aware RTX 2080 fan control for minas-tirith."""

import argparse
import ctypes
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

LOG = logging.getLogger("minas-gpu-fans")
STOP = threading.Event()


def valid_temp(value, maximum=110):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
        raise ValueError("invalid temperature: {!r}".format(value))
    return float(value)


class Nvml:
    def __init__(self, library, uuid, expected_fans, loader=ctypes.CDLL):
        self.lib = loader(library)
        void_p = ctypes.c_void_p
        uint_p = ctypes.POINTER(ctypes.c_uint)
        self._bind("nvmlInit_v2", [])
        self._bind("nvmlShutdown", [])
        self._bind("nvmlDeviceGetHandleByUUID", [ctypes.c_char_p, ctypes.POINTER(void_p)])
        self._bind("nvmlDeviceGetNumFans", [void_p, uint_p])
        self._bind("nvmlDeviceGetFanSpeed_v2", [void_p, ctypes.c_uint, uint_p])
        self._bind("nvmlDeviceSetFanSpeed_v2", [void_p, ctypes.c_uint, ctypes.c_uint])
        self._bind("nvmlDeviceGetTemperature", [void_p, ctypes.c_uint, uint_p])
        self._check("nvmlInit_v2")
        self.initialized = True
        try:
            self.handle = void_p()
            self._check("nvmlDeviceGetHandleByUUID", uuid.encode("ascii"), ctypes.byref(self.handle))
            count = ctypes.c_uint()
            self._check("nvmlDeviceGetNumFans", self.handle, ctypes.byref(count))
            if count.value != expected_fans:
                raise RuntimeError("expected {} GPU fans, found {}".format(expected_fans, count.value))
            self.fan_count = count.value
        except BaseException:
            self.close()
            raise

    def _bind(self, name, args):
        fn = getattr(self.lib, name)
        fn.argtypes = args
        fn.restype = ctypes.c_int

    def _check(self, name, *args):
        result = getattr(self.lib, name)(*args)
        if result != 0:
            raise RuntimeError("{} failed: NVML code {}".format(name, result))

    def set_both(self, percent):
        # Always command both, including when the first write failed.
        failures = []
        for fan in range(self.fan_count):
            try:
                self._check("nvmlDeviceSetFanSpeed_v2", self.handle, fan, percent)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("; ".join(failures))

    def temperature(self):
        value = ctypes.c_uint()
        self._check("nvmlDeviceGetTemperature", self.handle, 0, ctypes.byref(value))
        return valid_temp(value.value)

    def fan_speeds(self):
        speeds = []
        for fan in range(self.fan_count):
            value = ctypes.c_uint()
            self._check("nvmlDeviceGetFanSpeed_v2", self.handle, fan, ctypes.byref(value))
            if value.value > 100:
                raise ValueError("invalid reported GPU fan speed")
            speeds.append(value.value)
        return speeds

    def close(self):
        if getattr(self, "initialized", False):
            self.initialized = False
            self._check("nvmlShutdown")


def run_command(argv, timeout):
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)


def ipmi_temperature(ipmitool, name):
    result = run_command([ipmitool, "-I", "open", "sensor", "reading", name], 5)
    if result.returncode:
        raise RuntimeError("IPMI {} failed: {}".format(name, result.returncode))
    fields = result.stdout.strip().split("|")
    if len(fields) < 2 or fields[0].strip() != name:
        raise ValueError("IPMI {} reading missing".format(name))
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:degrees C)?\s*", fields[1])
    if not match:
        raise ValueError("IPMI {} temperature invalid".format(name))
    return valid_temp(float(match.group(1)))


def smart_temperature(smartctl, device):
    result = run_command([smartctl, "-a", "-j", "-d", "sat", device], 10)
    # smartctl bits 3-7 report device health/history and may accompany valid JSON.
    # Bits 0-2 mean command/open/protocol failure and must never be accepted.
    if result.returncode < 0 or result.returncode & 0x07:
        raise RuntimeError("SMART {} command failed: {}".format(device, result.returncode))
    data = json.loads(result.stdout)
    value = data.get("temperature", {}).get("current")
    if value is None:
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("id") in (190, 194):
                value = attr.get("raw", {}).get("value")
                break
    return valid_temp(value, 80)


class Sensors:
    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = {}
        self.threads = []
        for key, name in (("motherboard", "MB Temp"), ("x570", "X570 Temp")):
            self._start(key, config["board_period"], lambda name=name: ipmi_temperature(config["ipmitool"], name))
        for device in config["hdds"]:
            self._start(device, config["hdd_period"], lambda device=device: smart_temperature(config["smartctl"], device))

    def _start(self, key, period, reader):
        def collect():
            while not STOP.is_set():
                try:
                    value = reader()
                    entry = (value, self.clock(), None)
                except Exception as exc:
                    entry = (None, self.clock(), str(exc))
                    LOG.warning("sensor %s invalid: %s", key, exc)
                with self.lock:
                    self.cache[key] = entry
                STOP.wait(period)
        thread = threading.Thread(target=collect, name="sensor-" + key, daemon=True)
        thread.start()
        self.threads.append(thread)

    def snapshot(self, now):
        values = {}
        detail = {}
        with self.lock:
            cache = dict(self.cache)
        for key in ("motherboard", "x570", *self.config["hdds"]):
            value, stamp, error = cache.get(key, (None, now, "missing"))
            age = max(0, now - stamp)
            ttl = self.config["hdd_ttl"] if key in self.config["hdds"] else self.config["board_ttl"]
            if error is None and age > ttl:
                error = "stale"
            detail[key] = {"celsius": value, "age_seconds": round(age, 1), "error": error}
            if error is None:
                values[key] = value
        if len(values) == len(detail):
            values["hdd"] = max(values.pop(device) for device in self.config["hdds"])
        return values, detail


def demand(steps, values, floor):
    for step in reversed(steps):
        if any(values[key] >= step[key] for key in ("gpu", "motherboard", "x570", "hdd")):
            return step["percent"]
    return floor


class Controller:
    def __init__(self, config):
        self.config = config
        self.held_stage = 100
        self.commanded = 100
        self.cool_since = None
        self.last_drop = None

    def reset_full(self):
        self.held_stage = self.commanded = 100
        self.cool_since = self.last_drop = None

    def update(self, values, now):
        cfg = self.config
        if set(values) != {"gpu", "motherboard", "x570", "hdd"}:
            self.reset_full()
            return 100, None, "missing or invalid sensor"
        try:
            values = {key: valid_temp(value, 80 if key == "hdd" else 110) for key, value in values.items()}
        except ValueError:
            self.reset_full()
            return 100, None, "invalid sensor"
        request = demand(cfg["steps"], values, cfg["floor"])
        if request > self.held_stage:
            self.held_stage = request
            if request > self.commanded:
                self.commanded = request
                self.last_drop = None
            self.cool_since = None
            return self.commanded, request, "heat: immediate increase"
        if request < self.held_stage:
            stage = next(step for step in cfg["steps"] if step["percent"] == self.held_stage)
            if all(values[key] <= stage[key] - cfg["hysteresis_c"] for key in values):
                if self.cool_since is None:
                    self.cool_since = now
                if now - self.cool_since >= cfg["cool_hold_seconds"]:
                    levels = [cfg["floor"]] + [step["percent"] for step in cfg["steps"]]
                    self.held_stage = levels[levels.index(self.held_stage) - 1]
                    self.cool_since = None
            else:
                self.cool_since = None
        else:
            self.cool_since = None
        if self.commanded > self.held_stage and (
            self.last_drop is None or now - self.last_drop >= cfg["down_interval_seconds"]
        ):
            self.commanded = max(self.held_stage, self.commanded - cfg["down_step_percent"])
            self.last_drop = now
        return self.commanded, request, "cooldown" if request < self.held_stage else "steady"


def bmc_full(config):
    prefix = [config["ipmitool"], "-I", "open", "raw", "0x3a"]
    for command in (["0xd8"] + ["0x01"] * 16, ["0xd6"] + ["0x64"] * 16):
        result = run_command(prefix + command, 5)
        if result.returncode:
            raise RuntimeError("BMC fan command failed: {}".format(result.returncode))
    for attempt in range(8):
        result = run_command(prefix + ["0xda"], 5)
        if result.returncode == 0:
            octets = re.findall(r"\b[0-9a-fA-F]{2}\b", result.stdout)
            if len(octets) >= 16 and all(int(octet, 16) == 100 for octet in octets[:8]):
                LOG.info("BMC manual mode and first eight duties verified at 100%")
                return
        if attempt < 7:
            time.sleep(2)
    raise RuntimeError("BMC duty readback did not verify first eight fans at 100%")


def sd_notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())


def write_status(path, status):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".status-", dir=directory)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as output:
            json.dump(status, output, sort_keys=True)
            output.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def full_on_failure(nvml):
    try:
        nvml.set_both(100)
    except Exception:
        LOG.exception("failed to hold both GPU fans at 100%")


def open_nvml(config, attempts):
    for attempt in range(attempts):
        try:
            return Nvml(config["nvml_library"], config["gpu_uuid"], config["expected_fans"])
        except Exception:
            if attempt == attempts - 1:
                raise
            LOG.warning("NVML unavailable; retrying in 2s", exc_info=True)
            time.sleep(2)
    raise AssertionError("unreachable")


def one_shot(config):
    nvml = open_nvml(config, 1)
    try:
        sensors = {}
        errors = {}
        try:
            sensors["gpu"] = nvml.temperature()
        except Exception as exc:
            errors["gpu"] = str(exc)
        for key, name in (("motherboard", "MB Temp"), ("x570", "X570 Temp")):
            try:
                sensors[key] = ipmi_temperature(config["ipmitool"], name)
            except Exception as exc:
                errors[key] = str(exc)
        hdds = {}
        for device in config["hdds"]:
            try:
                hdds[device] = smart_temperature(config["smartctl"], device)
            except Exception as exc:
                errors[device] = str(exc)
        if len(hdds) == len(config["hdds"]):
            sensors["hdd"] = max(hdds.values())
        print(json.dumps({"sensors_celsius": sensors, "hdds_celsius": hdds, "errors": errors,
                          "requested_percent": demand(config["steps"], sensors, config["floor"]) if not errors else 100,
                          "writes": False}, sort_keys=True))
        return 0 if not errors else 1
    finally:
        nvml.close()


def serve(config):
    nvml = open_nvml(config, 6)
    try:
        # The first actuator operation precedes every sensor read and every lower command.
        nvml.set_both(100)
        collector = Sensors(config)
        controller = Controller(config)
        ready = False
        last_log = -float("inf")
        last_signature = None
        while not STOP.is_set():
            now = time.monotonic()
            values, sensor_detail = collector.snapshot(now)
            try:
                values["gpu"] = nvml.temperature()
            except Exception as exc:
                sensor_detail["gpu"] = {"celsius": None, "age_seconds": 0, "error": str(exc)}
            command, request, reason = controller.update(values, now)
            nvml.set_both(command)
            reported = nvml.fan_speeds()
            if request is None:
                reason += ": " + ", ".join(key for key, entry in sensor_detail.items() if entry["error"])
            status = {"timestamp": datetime.now(timezone.utc).isoformat(),
                      "sensors_celsius": values, "sensor_detail": sensor_detail,
                      "requested_percent": request, "held_stage_percent": controller.held_stage,
                      "commanded_percent": command, "reported_percent": reported, "reason": reason}
            write_status(config["status_path"], status)
            signature = (tuple(sorted(values.items())), request, controller.held_stage, command, tuple(reported), reason)
            if signature != last_signature or now - last_log >= 60:
                LOG.info("sensors=%s request=%s held=%s command=%s reported=%s reason=%s",
                         values, request, controller.held_stage, command, reported, reason)
                last_signature, last_log = signature, now
            sd_notify("READY=1\nWATCHDOG=1" if not ready else "WATCHDOG=1")
            ready = True
            STOP.wait(config["control_period"])
    except BaseException:
        full_on_failure(nvml)
        raise
    finally:
        full_on_failure(nvml)
        nvml.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON controller configuration")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--hold-full", action="store_true", help="set both GPU fans to 100%")
    actions.add_argument("--bmc-full", action="store_true", help="set BMC manual mode and 100% duty")
    actions.add_argument("--once", action="store_true", help="read sensors and calculate demand without fan writes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as source:
        config = json.load(source)
    if args.bmc_full:
        bmc_full(config)
    elif args.hold_full:
        nvml = open_nvml(config, 3)
        try:
            nvml.set_both(100)
        finally:
            nvml.close()
    elif args.once:
        return one_shot(config)
    else:
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        serve(config)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOG.exception("fan controller failed; GPU full-speed recovery requested")
        sys.exit(1)
