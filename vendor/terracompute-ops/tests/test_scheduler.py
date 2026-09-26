from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from terracompute_ops.scheduler import (
    CollectionScheduler,
    CollectionStatus,
    CollectorSpec,
    BMC_CADENCE_SECONDS,
    BMC_TIMEOUT_SECONDS,
    FULL_SSH_CADENCE_SECONDS,
    FULL_SSH_TIMEOUT_SECONDS,
    LIGHTWEIGHT_CADENCE_SECONDS,
    LIGHTWEIGHT_TIMEOUT_SECONDS,
    VAST_CADENCE_SECONDS,
    VAST_TIMEOUT_SECONDS,
    daemon_collectors,
    default_collectors,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Handle:
    def __init__(self) -> None:
        self.finished = False
        self.value = None
        self.error = None
        self.cancelled = False

    def succeed(self, value: object) -> None:
        self.finished = True
        self.value = value

    def fail(self) -> None:
        self.finished = True
        self.error = "collector-failed"

    def poll(self) -> tuple[bool, object, str | None]:
        return self.finished, self.value, self.error

    def cancel(self) -> None:
        self.cancelled = True
        self.finished = True
        self.error = "collector-cancelled"


class UnconfirmedHandle(Handle):
    def cancel(self) -> bool:
        self.cancelled = True
        return False


class Backend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.handles: dict[str, list[Handle]] = {}
        self.fail_start: set[str] = set()

    def start(self, name: str, collector: object) -> Handle:
        del collector
        self.started.append(name)
        if name in self.fail_start:
            raise RuntimeError("synthetic start failure")
        handle = Handle()
        self.handles.setdefault(name, []).append(handle)
        return handle

    def latest(self, name: str) -> Handle:
        return self.handles[name][-1]


def never_called() -> None:
    raise AssertionError("fake backend must not invoke collectors")


def spawn_descendant(marker: str) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    def reap_child(_signal, _frame):
        child.wait(timeout=2)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, reap_child)
    temporary = Path(marker + ".ready")
    temporary.write_text(str(child.pid), encoding="ascii")
    os.replace(temporary, marker)
    time.sleep(60)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SchedulerTests(unittest.TestCase):
    def test_daemon_profile_has_four_independent_source_process_budgets(self) -> None:
        specs = daemon_collectors(
            ssh=never_called,
            prometheus=never_called,
            vast=never_called,
            bmc=never_called,
        )
        by_name = {spec.name: spec for spec in specs}
        self.assertEqual(set(by_name), {"ssh", "prometheus", "vast", "bmc"})
        self.assertEqual(
            (by_name["ssh"].cadence_seconds, by_name["ssh"].timeout_seconds),
            (FULL_SSH_CADENCE_SECONDS, FULL_SSH_TIMEOUT_SECONDS),
        )
        self.assertEqual(
            (
                by_name["prometheus"].cadence_seconds,
                by_name["prometheus"].timeout_seconds,
            ),
            (30, 10),
        )
        self.assertEqual(
            (by_name["vast"].cadence_seconds, by_name["vast"].timeout_seconds),
            (60, 15),
        )
        self.assertEqual(
            (by_name["bmc"].cadence_seconds, by_name["bmc"].timeout_seconds),
            (BMC_CADENCE_SECONDS, BMC_TIMEOUT_SECONDS),
        )
        self.assertEqual(len({item.concurrency_source for item in specs}), 4)

    def test_approved_default_cadences_keep_full_ssh_separate(self) -> None:
        specs = default_collectors(
            lightweight=never_called, vast=never_called, full_ssh=never_called
        )
        by_name = {spec.name: spec for spec in specs}
        self.assertEqual(
            (by_name["lightweight"].cadence_seconds, by_name["lightweight"].timeout_seconds),
            (LIGHTWEIGHT_CADENCE_SECONDS, LIGHTWEIGHT_TIMEOUT_SECONDS),
        )
        self.assertEqual(
            (by_name["vast"].cadence_seconds, by_name["vast"].timeout_seconds),
            (VAST_CADENCE_SECONDS, VAST_TIMEOUT_SECONDS),
        )
        self.assertEqual(
            (by_name["full_ssh"].cadence_seconds, by_name["full_ssh"].timeout_seconds),
            (FULL_SSH_CADENCE_SECONDS, FULL_SSH_TIMEOUT_SECONDS),
        )
        self.assertTrue(by_name["full_ssh"].on_material_event)

    def test_external_cap_four_and_one_in_flight_per_source(self) -> None:
        clock = Clock()
        backend = Backend()
        specs = [
            CollectorSpec(f"c{i}", never_called, 30, 10, source="same" if i < 2 else f"s{i}")
            for i in range(6)
        ]
        scheduler = CollectionScheduler(
            specs, on_observation=lambda _value: None, execution=backend, clock=clock
        )
        scheduler.tick()
        self.assertEqual(len(scheduler.running), 4)
        self.assertIn("c0", scheduler.running)
        self.assertNotIn("c1", scheduler.running)

    def test_source_failure_does_not_stop_other_source_or_delivery(self) -> None:
        clock = Clock()
        backend = Backend()
        observed = []
        deliveries = []
        scheduler = CollectionScheduler(
            [
                CollectorSpec("ssh", never_called, 300, 45),
                CollectorSpec("vast", never_called, 60, 15),
            ],
            on_observation=observed.append,
            on_material=lambda: deliveries.append("delivered"),
            execution=backend,
            clock=clock,
            material_coalesce_seconds=0,
        )
        scheduler.tick()
        backend.latest("ssh").fail()
        backend.latest("vast").succeed({"fresh": True})
        scheduler.material_event()
        outcomes = scheduler.tick()
        self.assertEqual({item.name for item in outcomes}, {"ssh", "vast"})
        self.assertEqual(
            {item.status for item in outcomes},
            {CollectionStatus.FAILED, CollectionStatus.SUCCESS},
        )
        self.assertEqual(deliveries, ["delivered"])
        self.assertEqual(len(observed), 2)

    def test_timeout_hard_cancels_only_hung_handle_and_frees_capacity(self) -> None:
        clock = Clock()
        backend = Backend()
        scheduler = CollectionScheduler(
            [
                CollectorSpec("hung", never_called, 30, 10, source="shared"),
                CollectorSpec("next", never_called, 30, 10, source="shared"),
                CollectorSpec("vast", never_called, 60, 15),
            ],
            on_observation=lambda _value: None,
            execution=backend,
            clock=clock,
        )
        scheduler.tick()
        hung = backend.latest("hung")
        vast = backend.latest("vast")
        vast.succeed("ok")
        clock.advance(10)
        outcomes = scheduler.tick()
        self.assertTrue(hung.cancelled)
        self.assertEqual(outcomes[0].status, CollectionStatus.TIMED_OUT)
        self.assertIn("next", scheduler.running)

    def test_late_completion_is_timed_out_and_value_is_discarded(self) -> None:
        clock = Clock()
        backend = Backend()
        scheduler = CollectionScheduler(
            [CollectorSpec("late", never_called, 30, 10)],
            on_observation=lambda _value: None,
            execution=backend,
            clock=clock,
        )
        scheduler.tick()
        backend.latest("late").succeed("must-not-be-accepted")
        clock.advance(11)
        outcome = scheduler.tick()[0]
        self.assertEqual(outcome.status, CollectionStatus.TIMED_OUT)
        self.assertIsNone(outcome.value)

    def test_unconfirmed_cancellation_retains_source_capacity(self) -> None:
        clock = Clock()

        class StickyBackend(Backend):
            def start(self, name: str, collector: object) -> Handle:
                del collector
                self.started.append(name)
                handle: Handle = UnconfirmedHandle() if name == "hung" else Handle()
                self.handles.setdefault(name, []).append(handle)
                return handle

        backend = StickyBackend()
        scheduler = CollectionScheduler(
            [
                CollectorSpec("hung", never_called, 30, 1, source="shared"),
                CollectorSpec("next", never_called, 30, 1, source="shared"),
            ],
            on_observation=lambda _value: None,
            execution=backend,
            clock=clock,
        )
        scheduler.tick()
        clock.advance(1)
        outcome = scheduler.tick()[0]
        self.assertEqual(outcome.status, CollectionStatus.TIMED_OUT)
        self.assertEqual(scheduler.running, ("hung",))
        self.assertNotIn("next", backend.started)

    def test_real_collector_timeout_removes_complete_process_group(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            marker = Path(directory) / "descendant.pid"
            scheduler = CollectionScheduler(
                [
                    CollectorSpec(
                        "tree",
                        functools.partial(spawn_descendant, str(marker)),
                        30,
                        0.2,
                    )
                ],
                on_observation=lambda _value: None,
            )
            scheduler.tick()
            marker_deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < marker_deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "collector descendant did not start")
            child_pid = int(marker.read_text(encoding="ascii"))
            time.sleep(0.25)
            outcome = scheduler.tick()[0]
            self.assertEqual(outcome.status, CollectionStatus.TIMED_OUT)
            self.assertEqual(scheduler.running, ())
            gone_deadline = time.monotonic() + 1
            while process_exists(child_pid) and time.monotonic() < gone_deadline:
                time.sleep(0.01)
            self.assertFalse(process_exists(child_pid))
            scheduler.shutdown()

    def test_scheduler_shutdown_also_cleans_nested_ssh_descendants(self) -> None:
        from terracompute_ops.cli import fixed_ssh_probe

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "ssh-child.pid"
            executable = root / "ssh-fixture"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            executable.chmod(0o700)
            scheduler = CollectionScheduler(
                [CollectorSpec("ssh", functools.partial(
                    fixed_ssh_probe, str(executable), "observer@fixture",
                    root / "identity", root / "hosts"), 300, 65)],
                on_observation=lambda value: None,
            )
            try:
                scheduler.tick()
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                child_pid = int(marker.read_text())
                scheduler.shutdown()
                self.assertEqual(scheduler.running, ())
                self.assertFalse(process_exists(child_pid))
            finally:
                scheduler.shutdown()

    def test_material_triggers_coalesce_to_one_followup_while_running(self) -> None:
        clock = Clock()
        backend = Backend()
        deliveries = []
        scheduler = CollectionScheduler(
            [
                CollectorSpec(
                    "full_ssh", never_called, 300, 45, on_material_event=True
                )
            ],
            on_observation=lambda _value: None,
            on_material=lambda: deliveries.append(clock.now),
            execution=backend,
            clock=clock,
            material_coalesce_seconds=2,
        )
        scheduler.tick()
        scheduler.material_event()
        scheduler.material_event()
        scheduler.material_event()
        clock.advance(2)
        scheduler.tick()
        self.assertEqual(deliveries, [2.0])
        self.assertEqual(backend.started, ["full_ssh"])
        backend.latest("full_ssh").succeed("capture")
        scheduler.tick()
        self.assertEqual(backend.started, ["full_ssh", "full_ssh"])

    def test_callback_and_start_errors_are_isolated(self) -> None:
        clock = Clock()
        backend = Backend()
        backend.fail_start.add("bad")

        def bad_callback(_value: object) -> None:
            raise RuntimeError("synthetic callback failure")

        scheduler = CollectionScheduler(
            [
                CollectorSpec("bad", never_called, 30, 10),
                CollectorSpec("good", never_called, 30, 10),
            ],
            on_observation=bad_callback,
            execution=backend,
            clock=clock,
        )
        outcomes = scheduler.tick()
        self.assertEqual(outcomes[0].error_category, "collector-start-failed")
        self.assertIn("good", scheduler.running)

    def test_shutdown_cancels_every_handle_and_is_idempotent(self) -> None:
        backend = Backend()
        scheduler = CollectionScheduler(
            [CollectorSpec("a", never_called, 30, 10), CollectorSpec("b", never_called, 30, 10)],
            on_observation=lambda _value: None,
            execution=backend,
            clock=Clock(),
        )
        scheduler.tick()
        scheduler.shutdown()
        scheduler.shutdown()
        self.assertTrue(backend.latest("a").cancelled)
        self.assertTrue(backend.latest("b").cancelled)
        with self.assertRaises(RuntimeError):
            scheduler.tick()


if __name__ == "__main__":
    unittest.main()
