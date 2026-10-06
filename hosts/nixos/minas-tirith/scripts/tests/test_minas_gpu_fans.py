#!/usr/bin/env python3
"""Offline policy, sensor parsing, failure and actuator contract tests."""

import ctypes
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "minas-gpu-fans.py"
SPEC = importlib.util.spec_from_file_location("minas_gpu_fans", SCRIPT)
fan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fan)

FIXTURE = {
    "floor": 50,
    "steps": [
        {"percent": 65, "gpu": 55, "motherboard": 45, "x570": 65, "hdd": 40},
        {"percent": 80, "gpu": 65, "motherboard": 50, "x570": 75, "hdd": 45},
        {"percent": 100, "gpu": 75, "motherboard": 55, "x570": 85, "hdd": 50},
    ],
    "hysteresis_c": 3, "cool_hold_seconds": 300,
    "down_step_percent": 5, "down_interval_seconds": 60,
    "gpu_uuid": "GPU-8fc251bf-f300-03e1-98eb-d2929b62c23e",
    "expected_fans": 2,
    "hdds": [
        "/dev/disk/by-id/ata-WDC_WD140EDFZ-11A0VA0_9MGH8STK",
        "/dev/disk/by-id/ata-WDC_WD140EMFZ-11A0WA0_Z2HKXURT",
        "/dev/disk/by-id/ata-WDC_WUH721414ALE604_9JH1LNDT",
        "/dev/disk/by-id/ata-WDC_WD141PURP-74B5YY0_9RHGNX7L",
        "/dev/disk/by-id/ata-WDC_WD140EDFZ-11A0VA0_9MGHLSMU",
        "/dev/disk/by-id/ata-WDC_WD140EDFZ-11A0VA0_9MGH838K",
        "/dev/disk/by-id/ata-WDC_WD140EDFZ-11A0VA0_Y5J31TWC",
    ],
    "board_ttl": 30, "hdd_ttl": 180,
}
CONFIG = FIXTURE
if os.environ.get("MINAS_FAN_CONFIG"):
    with open(os.environ["MINAS_FAN_CONFIG"], encoding="utf-8") as stream:
        CONFIG = json.load(stream)


def cold():
    return {"gpu": 40, "motherboard": 35, "x570": 55, "hdd": 30}


def completed(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class PolicyTests(unittest.TestCase):
    def test_config_matches_explicit_policy(self):
        for key in ("floor", "steps", "hysteresis_c", "cool_hold_seconds",
                    "down_step_percent", "down_interval_seconds", "gpu_uuid", "expected_fans"):
            self.assertEqual(CONFIG[key], FIXTURE[key], key)
        self.assertEqual(CONFIG["hdds"], FIXTURE["hdds"])

    def test_exact_boundaries_and_highest_input(self):
        for key in cold():
            for step in CONFIG["steps"]:
                values = cold()
                values[key] = step[key] - 0.1
                self.assertLess(fan.demand(CONFIG["steps"], values, 50), step["percent"])
                values[key] = step[key]
                self.assertEqual(fan.demand(CONFIG["steps"], values, 50), step["percent"])
        values = cold()
        values.update(gpu=65, motherboard=55)
        self.assertEqual(fan.demand(CONFIG["steps"], values, 50), 100)

    def test_startup_full_and_immediate_ramp(self):
        ctl = fan.Controller(CONFIG)
        self.assertEqual((ctl.commanded, ctl.held_stage), (100, 100))
        self.assertEqual(ctl.update(cold(), 0)[0], 100)
        ctl.update(cold(), 300)
        self.assertEqual(ctl.commanded, 95)
        hot = cold()
        hot["hdd"] = 50
        self.assertEqual(ctl.update(hot, 301)[0], 100)
        self.assertIsNone(ctl.cool_since)
        self.assertIsNone(ctl.last_drop)

    def test_deadband_all_inputs_and_300_monotonic_seconds(self):
        ctl = fan.Controller(CONFIG)
        values = cold()
        values["gpu"] = 72.1  # demand 80, but not three degrees below 100's GPU threshold
        self.assertEqual(ctl.update(values, 0)[0], 100)
        self.assertEqual(ctl.update(values, 400)[0], 100)
        values["gpu"] = 72
        values["motherboard"] = 52.1
        ctl.update(values, 500)
        self.assertIsNone(ctl.cool_since)
        values["motherboard"] = 52
        self.assertEqual(ctl.update(values, 600)[0], 100)
        self.assertEqual(ctl.update(values, 899.9)[0], 100)
        self.assertEqual(ctl.update(values, 900)[0], 95)
        self.assertEqual(ctl.held_stage, 80)

    def test_down_rate_and_each_stage_hold(self):
        ctl = fan.Controller(CONFIG)
        values = cold()
        ctl.update(values, 0)
        self.assertEqual(ctl.update(values, 300)[0], 95)
        self.assertEqual(ctl.update(values, 359)[0], 95)
        self.assertEqual(ctl.update(values, 360)[0], 90)
        self.assertEqual(ctl.update(values, 420)[0], 85)
        self.assertEqual(ctl.update(values, 480)[0], 80)
        self.assertEqual(ctl.update(values, 599)[0], 80)
        self.assertEqual(ctl.update(values, 600)[0], 80)
        self.assertEqual(ctl.update(values, 660)[0], 75)
        self.assertEqual(ctl.held_stage, 65)
        self.assertEqual(ctl.update(values, 720)[0], 70)

    def test_reheating_never_reduces_a_higher_actual_command(self):
        ctl = fan.Controller(CONFIG)
        ctl.update(cold(), 0)
        self.assertEqual(ctl.update(cold(), 300)[0], 95)
        ctl.update(cold(), 301)
        self.assertEqual(ctl.update(cold(), 601)[0], 90)
        self.assertEqual(ctl.held_stage, 65)
        warm = cold()
        warm["gpu"] = 65
        self.assertEqual(ctl.update(warm, 602)[0], 90)
        self.assertEqual(ctl.last_drop, 601)
        self.assertEqual(ctl.update(warm, 603)[0], 90)
        self.assertEqual(ctl.update(warm, 661)[0], 85)

    def test_failure_cancels_cooldown_and_restores_full(self):
        ctl = fan.Controller(CONFIG)
        ctl.update(cold(), 0)
        self.assertEqual(ctl.update({"gpu": 40}, 299)[0], 100)
        self.assertEqual(ctl.update(cold(), 300)[0], 100)
        self.assertEqual(ctl.update(cold(), 599)[0], 100)
        self.assertEqual(ctl.update(cold(), 600)[0], 95)
        for invalid in (None, float("nan"), -1, 111, True):
            values = cold()
            values["gpu"] = invalid
            self.assertEqual(ctl.update(values, 700)[0], 100)
            self.assertIsNone(ctl.cool_since)
            self.assertIsNone(ctl.last_drop)

    def test_cache_failure_invalidates_prior_success_and_stale(self):
        holder = fan.Sensors.__new__(fan.Sensors)
        holder.config = dict(CONFIG)
        holder.lock = __import__("threading").Lock()
        holder.cache = {key: (40, 100, None) for key in ("motherboard", "x570", *CONFIG["hdds"])}
        values, detail = holder.snapshot(120)
        self.assertEqual(values["hdd"], 40)
        holder.cache[CONFIG["hdds"][0]] = (None, 121, "SMART error")
        values, detail = holder.snapshot(121)
        self.assertNotIn("hdd", values)
        self.assertEqual(detail[CONFIG["hdds"][0]]["error"], "SMART error")
        self.assertEqual(fan.Controller(CONFIG).update(values, 121)[0], 100)
        self.assertEqual(holder.snapshot(281)[1]["motherboard"]["error"], "stale")

    def test_collector_replaces_success_with_failure(self):
        holder = fan.Sensors.__new__(fan.Sensors)
        holder.lock = __import__("threading").Lock()
        holder.cache = {"motherboard": (42, 90, None)}
        holder.threads = []
        holder.clock = lambda: 100
        calls = iter([False, False, True])

        class FakeStop:
            def is_set(self):
                return next(calls)
            def wait(self, seconds):
                pass

        class FakeThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()

        with mock.patch.object(fan, "STOP", FakeStop()), \
             mock.patch.object(fan.threading, "Thread", FakeThread):
            holder._start("motherboard", 10, mock.Mock(side_effect=[42, ValueError("broken")]))
        self.assertEqual(holder.cache["motherboard"], (None, 100, "broken"))

    def test_smart_health_bits_and_malformed_temperature(self):
        data = json.dumps({"temperature": {"current": 42}})
        with mock.patch.object(fan, "run_command", return_value=completed(data, 8)) as run:
            self.assertEqual(fan.smart_temperature("smartctl", "/disk"), 42)
            self.assertEqual(run.call_args.args[0], ["smartctl", "-a", "-j", "-d", "sat", "/disk"])
        for code, payload in ((2, data), (1, data), (0, "bad JSON"), (0, "{}"),
                              (0, '{"temperature":{"current":null}}')):
            with self.subTest(code=code, payload=payload):
                with mock.patch.object(fan, "run_command", return_value=completed(payload, code)):
                    with self.assertRaises((RuntimeError, ValueError, TypeError, json.JSONDecodeError)):
                        fan.smart_temperature("smartctl", "/disk")

    def test_ipmi_parse_and_bmc_raw_fixed_full(self):
        for reading in ("MB Temp | 52\n", "MB Temp | 52 degrees C | ok\n"):
            with mock.patch.object(fan, "run_command", return_value=completed(reading)):
                self.assertEqual(fan.ipmi_temperature("ipmitool", "MB Temp"), 52)
        with mock.patch.object(fan, "run_command", return_value=completed("MB Temp | na | na\n")):
            with self.assertRaises(ValueError):
                fan.ipmi_temperature("ipmitool", "MB Temp")
        cfg = {"ipmitool": "ipmitool"}
        readback = " ".join(["64"] * 8 + ["00"] * 8)
        with mock.patch.object(fan, "run_command", side_effect=[completed(""), completed(""), completed(readback)]) as run:
            fan.bmc_full(cfg)
            commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-17:], ["0xd8"] + ["0x01"] * 16)
        self.assertEqual(commands[1][-17:], ["0xd6"] + ["0x64"] * 16)
        self.assertEqual(commands[2][-1], "0xda")


class FakeFunction:
    def __init__(self, impl):
        self.impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.impl(*args)


class FakeNvmlLibrary:
    def __init__(self):
        self.calls = []
        self.fail_fan = None
        self.nvmlInit_v2 = FakeFunction(lambda: 0)
        self.nvmlShutdown = FakeFunction(lambda: self._shutdown())
        self.nvmlDeviceGetHandleByUUID = FakeFunction(self._uuid)
        self.nvmlDeviceGetNumFans = FakeFunction(lambda h, p: self._out(p, 2))
        self.nvmlDeviceGetFanSpeed_v2 = FakeFunction(lambda h, n, p: self._out(p, 100))
        self.nvmlDeviceSetFanSpeed_v2 = FakeFunction(self._set)
        self.nvmlDeviceGetTemperature = FakeFunction(lambda h, n, p: self._out(p, 46))

    def _out(self, pointer, value):
        pointer._obj.value = value
        return 0

    def _uuid(self, uuid, pointer):
        self.calls.append(("uuid", uuid))
        return self._out(pointer, 1234)

    def _set(self, handle, number, percent):
        self.calls.append((number, percent))
        return 1 if number == self.fail_fan else 0

    def _shutdown(self):
        self.calls.append(("shutdown",))
        return 0


class ActuatorTests(unittest.TestCase):
    def test_uuid_pointer_bindings_two_fans_and_no_restore(self):
        lib = FakeNvmlLibrary()
        nvml = fan.Nvml("library", CONFIG["gpu_uuid"], 2, loader=lambda _: lib)
        self.assertEqual(lib.calls[0], ("uuid", CONFIG["gpu_uuid"].encode()))
        self.assertEqual(lib.nvmlDeviceGetHandleByUUID.argtypes,
                         [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)])
        self.assertEqual(lib.nvmlDeviceGetFanSpeed_v2.argtypes,
                         [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)])
        self.assertEqual(nvml.temperature(), 46)
        self.assertEqual(nvml.fan_speeds(), [100, 100])
        nvml.set_both(100)
        nvml.close()
        self.assertEqual(lib.calls[-3:], [(0, 100), (1, 100), ("shutdown",)])

    def test_failed_first_fan_still_commands_second_and_recovery(self):
        lib = FakeNvmlLibrary()
        lib.fail_fan = 0
        nvml = fan.Nvml("library", CONFIG["gpu_uuid"], 2, loader=lambda _: lib)
        with self.assertRaises(RuntimeError):
            nvml.set_both(80)
        self.assertEqual(lib.calls[-2:], [(0, 80), (1, 80)])
        fan.full_on_failure(nvml)
        self.assertEqual(lib.calls[-2:], [(0, 100), (1, 100)])
        nvml.close()

    def test_hold_full_mode_sets_both_without_default_restore(self):
        cfg = {"nvml_library": "library", "gpu_uuid": CONFIG["gpu_uuid"], "expected_fans": 2}
        lib = FakeNvmlLibrary()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps(cfg))
            real_nvml = fan.Nvml
            with mock.patch.object(fan, "Nvml", side_effect=lambda *args: real_nvml(*args, loader=lambda _: lib)):
                self.assertEqual(fan.main(["--config", str(path), "--hold-full"]), 0)
        self.assertEqual(lib.calls[-3:], [(0, 100), (1, 100), ("shutdown",)])

    def test_serve_sets_full_before_sensors_and_on_stop(self):
        lib = FakeNvmlLibrary()
        real_nvml = fan.Nvml

        class StopAfterOne:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True

        class Collector:
            def __init__(self, config):
                self.asserted = False
            def snapshot(self, now):
                assert lib.calls[-2:] == [(0, 100), (1, 100)]
                return ({"motherboard": 35, "x570": 55, "hdd": 30}, {})

        cfg = dict(CONFIG, nvml_library="library", status_path="/unused", control_period=10)
        with mock.patch.object(fan, "Nvml", side_effect=lambda *args: real_nvml(*args, loader=lambda _: lib)), \
             mock.patch.object(fan, "Sensors", Collector), \
             mock.patch.object(fan, "STOP", StopAfterOne()), \
             mock.patch.object(fan, "write_status"), \
             mock.patch.object(fan, "sd_notify") as notify:
            fan.serve(cfg)
        self.assertEqual(lib.calls[-3:], [(0, 100), (1, 100), ("shutdown",)])
        notify.assert_called_once_with("READY=1\nWATCHDOG=1")

    def test_serve_exception_retries_full_before_shutdown(self):
        lib = FakeNvmlLibrary()
        real_nvml = fan.Nvml

        class BrokenCollector:
            def __init__(self, config):
                pass
            def snapshot(self, now):
                raise RuntimeError("collector crash")

        cfg = dict(CONFIG, nvml_library="library")
        with mock.patch.object(fan, "Nvml", side_effect=lambda *args: real_nvml(*args, loader=lambda _: lib)), \
             mock.patch.object(fan, "Sensors", BrokenCollector), \
             mock.patch.object(fan.STOP, "is_set", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "collector crash"):
                fan.serve(cfg)
        self.assertEqual(lib.calls[-3:], [(0, 100), (1, 100), ("shutdown",)])

    def test_once_reads_real_inputs_without_any_fan_write(self):
        lib = FakeNvmlLibrary()
        real_nvml = fan.Nvml
        cfg = dict(CONFIG, nvml_library="library", ipmitool="ipmitool", smartctl="smartctl")
        with mock.patch.object(fan, "Nvml", side_effect=lambda *args: real_nvml(*args, loader=lambda _: lib)), \
             mock.patch.object(fan, "ipmi_temperature", return_value=35), \
             mock.patch.object(fan, "smart_temperature", return_value=30), \
             mock.patch("builtins.print") as output:
            self.assertEqual(fan.one_shot(cfg), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["requested_percent"], 50)
        self.assertFalse(any(isinstance(call[0], int) for call in lib.calls))

    def test_atomic_status_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "status.json"
            fan.write_status(str(path), {"commanded_percent": 100})
            self.assertEqual(json.loads(path.read_text())["commanded_percent"], 100)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
