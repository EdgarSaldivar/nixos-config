"""Independent, bounded scheduling for observation collectors.

The scheduler is deliberately an orchestration primitive, not a probe runner.  Callers
inject named collector callables and callbacks which persist observations or drain a
delivery outbox.  :class:`ProcessExecutionBackend` is the production-oriented default:
it gives a timed-out collector a process boundary which can actually be terminated.
Tests and embedded runtimes may inject another :class:`ExecutionBackend`, but a thread
executor must not describe ``cancel()`` as hard cancellation of already-running Python.

``tick()`` never sleeps.  A service loop may call it using its own monotonic clock, while
offline tests can advance a deterministic clock and fake execution handles.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import signal
import time
from dataclasses import dataclass
from enum import Enum
from multiprocessing.connection import Connection
from typing import Any, Callable, Iterable, Protocol


_collector_group = False


def in_collector_group() -> bool:
    """Whether this process belongs to the scheduler-owned cancellation group."""
    return _collector_group


MAX_EXTERNAL_COLLECTIONS = 4
MAX_PROCESS_RESULT_BYTES = 1024 * 1024
LIGHTWEIGHT_CADENCE_SECONDS = 30.0
LIGHTWEIGHT_TIMEOUT_SECONDS = 10.0
PROMETHEUS_CADENCE_SECONDS = LIGHTWEIGHT_CADENCE_SECONDS
PROMETHEUS_TIMEOUT_SECONDS = LIGHTWEIGHT_TIMEOUT_SECONDS
PROMETHEUS_ALERTS_CADENCE_SECONDS = 30.0
PROMETHEUS_ALERTS_TIMEOUT_SECONDS = LIGHTWEIGHT_TIMEOUT_SECONDS
VAST_CADENCE_SECONDS = 60.0
VAST_TIMEOUT_SECONDS = 15.0
BMC_CADENCE_SECONDS = 60.0
BMC_TIMEOUT_SECONDS = 15.0
FULL_SSH_CADENCE_SECONDS = 300.0
# The remote forced command has its own 45-second aggregate deadline.  The local
# SSH collector additionally needs a bounded connection allowance and enough time
# to frame/serialize the partial JSON emitted at that remote deadline.
FULL_SSH_TIMEOUT_SECONDS = 65.0


class CollectionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class CollectorSpec:
    """One independently scheduled source.

    ``source`` is the concurrency domain.  Two specs sharing it will never overlap.
    Set ``on_material_event`` for an expensive collector, such as the full SSH capture,
    that should also run after a coalesced material incident trigger.
    """

    name: str
    collector: Callable[[], Any]
    cadence_seconds: float
    timeout_seconds: float
    source: str | None = None
    external: bool = True
    on_material_event: bool = False

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("collector name must be non-empty and bounded")
        if self.source is not None and (not self.source or len(self.source) > 128):
            raise ValueError("collector source must be non-empty and bounded")
        if self.cadence_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("collector cadence and timeout must be positive")

    @property
    def concurrency_source(self) -> str:
        return self.source or self.name


@dataclass(frozen=True)
class CollectionObservation:
    """Bounded collector outcome passed to the observation callback.

    Exception text is intentionally excluded.  Probe and transport exceptions can carry
    request URLs or other sensitive context; persistence should use the fixed category.
    """

    name: str
    source: str
    status: CollectionStatus
    started_at: float
    finished_at: float
    value: Any = None
    error_category: str | None = None


class ExecutionHandle(Protocol):
    """A running collection operation.

    ``cancel`` must provide hard cancellation if the backend is used for collectors that
    require it.  Thread futures generally do not satisfy that contract once running.
    """

    def poll(self) -> tuple[bool, Any, str | None]: ...

    def cancel(self) -> bool | None: ...


class ExecutionBackend(Protocol):
    def start(self, name: str, collector: Callable[[], Any]) -> ExecutionHandle: ...


def _process_entry(
    connection: Connection,
    group_ready: Connection,
    collector: Callable[[], Any],
) -> None:
    global _collector_group
    try:
        # Every collector and the subprocesses it launches share a private Unix
        # process group.  The parent does not release scheduler capacity until that
        # complete group has terminated.
        os.setsid()
        _collector_group = True
        group_ready.send(True)
        group_ready.close()
        try:
            result = (True, collector(), None)
        except BaseException:
            # Never serialize exception text across the boundary.
            result = (True, None, "collector-failed")
        encoded = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
        if len(encoded) > MAX_PROCESS_RESULT_BYTES:
            encoded = pickle.dumps((True, None, "collector-result-too-large"))
        connection.send_bytes(encoded)
    except BaseException:
        # Parent will classify an exited child with no complete result.
        pass
    finally:
        group_ready.close()
        connection.close()


class _ProcessHandle:
    def __init__(
        self,
        process: multiprocessing.Process,
        connection: Connection,
        group_ready: Connection,
    ):
        self._process = process
        self._connection = connection
        self._group_ready_connection = group_ready
        self._group_ready = False
        self._pending: tuple[bool, Any, str | None] | None = None
        self._finished: tuple[bool, Any, str | None] | None = None

    def poll(self) -> tuple[bool, Any, str | None]:
        if self._finished is not None:
            return self._finished
        self._refresh_group_ready()
        if self._connection.poll(0):
            try:
                encoded = self._connection.recv_bytes(MAX_PROCESS_RESULT_BYTES)
                value = pickle.loads(encoded)
                if not (
                    isinstance(value, tuple)
                    and len(value) == 3
                    and value[0] is True
                    and (value[2] is None or isinstance(value[2], str))
                ):
                    raise ValueError("invalid child result")
                self._pending = value
            except (EOFError, OSError, ValueError, pickle.UnpicklingError):
                self._pending = (True, None, "collector-process-failed")
        if self._pending is not None:
            if not self._terminate_tree():
                return (False, None, None)
            self._finished = self._pending
            self._close_connections()
            return self._finished
        if not self._process.is_alive():
            if not self._terminate_tree():
                return (False, None, None)
            self._finished = (True, None, "collector-process-failed")
            self._close_connections()
            return self._finished
        return (False, None, None)

    def cancel(self) -> bool:
        if self._finished is not None:
            return True
        self._refresh_group_ready()
        if not self._terminate_tree():
            return False
        self._close_connections()
        self._finished = (True, None, "collector-cancelled")
        return True

    def _refresh_group_ready(self) -> None:
        if self._group_ready:
            return
        try:
            if self._group_ready_connection.poll(0):
                self._group_ready = self._group_ready_connection.recv() is True
                self._group_ready_connection.close()
        except (EOFError, OSError):
            self._group_ready_connection.close()

    def _group_alive(self) -> bool:
        if not self._group_ready:
            return False
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _signal_tree(self, number: signal.Signals) -> None:
        try:
            if self._group_ready:
                os.killpg(self._process.pid, number)
            elif self._process.is_alive():
                os.kill(self._process.pid, number)
        except ProcessLookupError:
            pass

    def _terminate_tree(self) -> bool:
        self._signal_tree(signal.SIGTERM)
        self._process.join(timeout=0.5)
        self._refresh_group_ready()
        if self._process.is_alive() or self._group_alive():
            self._signal_tree(signal.SIGKILL)
            self._process.join(timeout=0.5)
        deadline = time.monotonic() + 0.5
        while self._group_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._process.is_alive() and not self._group_alive()

    def _close_connections(self) -> None:
        self._connection.close()
        self._group_ready_connection.close()


class ProcessExecutionBackend:
    """Run each collector in a separately terminable child process."""

    def __init__(self, context: multiprocessing.context.BaseContext | None = None):
        # ``spawn`` avoids inheriting a potentially credentialed controller's open file
        # descriptors.  Production collectors therefore need ordinary picklable callables.
        self._context = context or multiprocessing.get_context("spawn")

    def start(self, name: str, collector: Callable[[], Any]) -> ExecutionHandle:
        del name  # useful to instrumented injected backends, unnecessary here
        parent, child = self._context.Pipe(duplex=False)
        ready_parent, ready_child = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_process_entry, args=(child, ready_child, collector)
        )
        process.daemon = True
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            ready_parent.close()
            ready_child.close()
            raise
        child.close()
        ready_child.close()
        return _ProcessHandle(process, parent, ready_parent)


@dataclass
class _Running:
    spec: CollectorSpec
    handle: ExecutionHandle
    started_at: float
    timeout_reported: bool = False


class CollectionScheduler:
    """Schedule named collectors without allowing one source to block another.

    ``on_observation`` receives success, fixed-category failure, and timeout records.
    ``on_material`` is a local, nonblocking callback which should only persist/enqueue a
    delivery or incident signal; transport delivery belongs to its own bounded worker
    budget. It is invoked once for a burst of :meth:`material_event` calls, and callback
    failure is isolated from collection. Material events also expedite every spec with
    ``on_material_event=True``. A trigger received while that source is running is retained
    as one (and only one) follow-up run.
    """

    def __init__(
        self,
        collectors: Iterable[CollectorSpec],
        *,
        on_observation: Callable[[CollectionObservation], None],
        on_material: Callable[[], None] | None = None,
        execution: ExecutionBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_external: int = MAX_EXTERNAL_COLLECTIONS,
        material_coalesce_seconds: float = 1.0,
    ):
        specs = tuple(collectors)
        if not specs:
            raise ValueError("at least one collector is required")
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("collector names must be unique")
        if not 1 <= max_external <= MAX_EXTERNAL_COLLECTIONS:
            raise ValueError("external concurrency must be between one and four")
        if material_coalesce_seconds < 0:
            raise ValueError("material coalesce interval cannot be negative")
        self._specs = {spec.name: spec for spec in specs}
        self._on_observation = on_observation
        self._on_material = on_material
        self._execution = execution or ProcessExecutionBackend()
        self._clock = clock
        self._max_external = max_external
        self._coalesce_seconds = material_coalesce_seconds
        now = self._clock()
        self._next_due = {spec.name: now for spec in specs}
        self._triggered = {spec.name: False for spec in specs}
        self._running: dict[str, _Running] = {}
        self._material_due: float | None = None
        self._closed = False

    @property
    def running(self) -> tuple[str, ...]:
        return tuple(sorted(self._running))

    def trigger(self, name: str) -> None:
        """Request an immediate run, coalescing repeated requests per collector."""
        self._ensure_open()
        if name not in self._specs:
            raise KeyError(name)
        self._triggered[name] = True
        if name not in self._running:
            self._next_due[name] = min(self._next_due[name], self._clock())

    def material_event(self) -> None:
        """Coalesce delivery and expensive evidence triggers for a material event."""
        self._ensure_open()
        now = self._clock()
        if self._material_due is None:
            self._material_due = now + self._coalesce_seconds
        for spec in self._specs.values():
            if spec.on_material_event:
                self.trigger(spec.name)

    def tick(self) -> tuple[CollectionObservation, ...]:
        """Advance ready work once and return outcomes produced during this tick."""
        self._ensure_open()
        now = self._clock()
        observations: list[CollectionObservation] = []

        for name, running in tuple(self._running.items()):
            if now - running.started_at >= running.spec.timeout_seconds:
                if not running.timeout_reported:
                    observation = CollectionObservation(
                        name=name,
                        source=running.spec.concurrency_source,
                        status=CollectionStatus.TIMED_OUT,
                        started_at=running.started_at,
                        finished_at=now,
                        error_category="collector-timeout",
                    )
                    observations.append(observation)
                    self._notify(observation)
                    running.timeout_reported = True
                if self._cancel_confirmed(running.handle):
                    self._release(name, now)
                # A value observed after the absolute deadline is always discarded.
                # An unconfirmed process retains both its source and external slot.
                continue
            try:
                done, value, error = running.handle.poll()
            except Exception:
                if not self._cancel_confirmed(running.handle):
                    continue
                done, value, error = True, None, "collector-handle-failed"
            if done:
                status = CollectionStatus.SUCCESS if error is None else CollectionStatus.FAILED
                observation = CollectionObservation(
                    name=name,
                    source=running.spec.concurrency_source,
                    status=status,
                    started_at=running.started_at,
                    finished_at=now,
                    value=value if error is None else None,
                    error_category=error,
                )
                self._complete(name, observation, now, observations)

        # Delivery/event callbacks do not consume external collector capacity and are
        # attempted even when a source failed in the same tick.
        if self._material_due is not None and now >= self._material_due:
            self._material_due = None
            if self._on_material is not None:
                try:
                    self._on_material()
                except Exception:
                    pass

        busy_sources = {
            running.spec.concurrency_source for running in self._running.values()
        }
        external_count = sum(
            1 for running in self._running.values() if running.spec.external
        )
        for spec in self._specs.values():
            if spec.name in self._running or now < self._next_due[spec.name]:
                continue
            if spec.concurrency_source in busy_sources:
                continue
            if spec.external and external_count >= self._max_external:
                continue
            try:
                handle = self._execution.start(spec.name, spec.collector)
            except Exception:
                observation = CollectionObservation(
                    name=spec.name,
                    source=spec.concurrency_source,
                    status=CollectionStatus.FAILED,
                    started_at=now,
                    finished_at=now,
                    error_category="collector-start-failed",
                )
                self._next_due[spec.name] = now + spec.cadence_seconds
                self._triggered[spec.name] = False
                observations.append(observation)
                self._notify(observation)
                continue
            self._running[spec.name] = _Running(spec, handle, now)
            self._triggered[spec.name] = False
            busy_sources.add(spec.concurrency_source)
            if spec.external:
                external_count += 1
        return tuple(observations)

    def shutdown(self) -> None:
        """Hard-cancel all running process-isolated collectors; idempotent."""
        if self._closed:
            return
        self._closed = True
        for name, running in tuple(self._running.items()):
            if self._cancel_confirmed(running.handle):
                self._running.pop(name)

    def _complete(
        self,
        name: str,
        observation: CollectionObservation,
        now: float,
        observations: list[CollectionObservation],
    ) -> None:
        self._release(name, now)
        observations.append(observation)
        self._notify(observation)

    def _release(self, name: str, now: float) -> None:
        running = self._running.pop(name)
        self._next_due[name] = (
            now
            if self._triggered[name]
            else running.started_at + running.spec.cadence_seconds
        )
        self._triggered[name] = False

    @staticmethod
    def _cancel_confirmed(handle: ExecutionHandle) -> bool:
        try:
            # Existing injected handles return None; only an explicit False means
            # that the process/tree is still alive and capacity must be retained.
            return handle.cancel() is not False
        except Exception:
            return False

    def _notify(self, observation: CollectionObservation) -> None:
        try:
            self._on_observation(observation)
        except Exception:
            # Storage/callback failure is reported by that subsystem; it must not stop
            # unrelated collection or delivery dispatch.
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("scheduler is shut down")


def default_collectors(
    *,
    lightweight: Callable[[], Any],
    vast: Callable[[], Any],
    full_ssh: Callable[[], Any],
) -> tuple[CollectorSpec, ...]:
    """Return the approved initial cadence/deadline profile.

    The lightweight path remains separate from the five-minute full SSH capture.  ``vast``
    has its own source domain, and only the full capture is expedited by material events.
    """

    return (
        CollectorSpec(
            "lightweight",
            lightweight,
            LIGHTWEIGHT_CADENCE_SECONDS,
            LIGHTWEIGHT_TIMEOUT_SECONDS,
            source="lightweight",
        ),
        CollectorSpec(
            "vast",
            vast,
            VAST_CADENCE_SECONDS,
            VAST_TIMEOUT_SECONDS,
            source="vast",
        ),
        CollectorSpec(
            "full_ssh",
            full_ssh,
            FULL_SSH_CADENCE_SECONDS,
            FULL_SSH_TIMEOUT_SECONDS,
            source="ssh",
            on_material_event=True,
        ),
    )


def daemon_collectors(
    *,
    ssh: Callable[[], Any] | None = None,
    prometheus: Callable[[], Any] | None = None,
    prometheus_alerts: Callable[[], Any] | None = None,
    vast: Callable[[], Any] | None = None,
    bmc: Callable[[], Any] | None = None,
) -> tuple[CollectorSpec, ...]:
    """Build the observation daemon's independently bounded source set.

    Missing callables mean the integration is not configured.  They do not create
    synthetic failures: source absence and source failure are distinct states.
    """

    specs: list[CollectorSpec] = []
    if ssh is not None:
        specs.append(
            CollectorSpec(
                "ssh",
                ssh,
                FULL_SSH_CADENCE_SECONDS,
                FULL_SSH_TIMEOUT_SECONDS,
                source="ssh",
                on_material_event=True,
            )
        )
    if prometheus is not None:
        specs.append(
            CollectorSpec(
                "prometheus",
                prometheus,
                PROMETHEUS_CADENCE_SECONDS,
                PROMETHEUS_TIMEOUT_SECONDS,
                source="prometheus",
            )
        )
    if prometheus_alerts is not None:
        specs.append(
            CollectorSpec(
                "prometheus-alerts",
                prometheus_alerts,
                PROMETHEUS_ALERTS_CADENCE_SECONDS,
                PROMETHEUS_ALERTS_TIMEOUT_SECONDS,
                source="prometheus-alerts",
            )
        )
    if vast is not None:
        specs.append(
            CollectorSpec(
                "vast",
                vast,
                VAST_CADENCE_SECONDS,
                VAST_TIMEOUT_SECONDS,
                source="vast",
            )
        )
    if bmc is not None:
        specs.append(
            CollectorSpec(
                "bmc",
                bmc,
                BMC_CADENCE_SECONDS,
                BMC_TIMEOUT_SECONDS,
                source="bmc",
            )
        )
    if not specs:
        raise ValueError("at least one integration source must be explicitly configured")
    return tuple(specs)


# Concise public name for integrations; retain the descriptive class name for readers.
Scheduler = CollectionScheduler
