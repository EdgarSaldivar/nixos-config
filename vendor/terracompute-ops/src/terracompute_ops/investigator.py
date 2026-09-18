"""Bounded standalone Codex App Server adapter and investigation admission.

This module deliberately knows nothing about codex-seats or other providers.  The
controller owns this client's lifecycle; deterministic monitoring never depends
on it.
"""

from __future__ import annotations

import json
import os
import signal
import queue
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


MAX_RPC_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_AGENT_TEXT_BYTES = 256 * 1024
MAX_PENDING_MESSAGES = 4096
MAX_TRANSPORT_QUEUE = 128
TRANSPORT_IO_TIMEOUT_SECONDS = 2.0
# How long to wait for the App Server to be gone after a kill. It leads a process
# group of its own and may have children of its own to take down with it, and two
# seconds on a loaded Pi was not enough: an unconfirmed exit held the turn's lease
# for ever, which is a far worse failure than waiting a little longer here.
TRANSPORT_REAP_TIMEOUT_SECONDS = 15.0
# Acknowledging `turn/start` is not the same as running the turn. The acknowledgement
# waited 30 seconds, which a cold App Server on this hardware misses while it brings
# itself up, so every first diagnosis after a restart failed. The turn itself keeps
# the caller's budget; this is only the wait for "yes, I have started".
TURN_START_ACK_SECONDS = 120.0
INTERRUPT_CLEANUP_TIMEOUT_SECONDS = 5.0

LEAD_MODEL = "gpt-5.6-sol"
LEAD_EFFORT = "high"
HELPER_MODEL = "gpt-5.6-terra"
HELPER_EFFORT = "medium"
SUMMARY_MODEL = "gpt-5.6-luna"
SUMMARY_EFFORT = "low"
ESCALATION_MODEL = "gpt-6-astra"
ESCALATION_EFFORT = "high"


class InvestigatorError(RuntimeError):
    """A sanitized runtime or admission failure."""


class ProtocolError(InvestigatorError):
    """The App Server peer violated the bounded JSON-RPC contract."""


class RuntimeUnavailable(InvestigatorError):
    """Codex auth, quota, model, or process state cannot admit work."""


class InvestigationTimeout(TimeoutError):
    """A turn exceeded its deadline, with explicit runtime-lifecycle state."""

    def __init__(
        self,
        *,
        cumulative_tokens: int | None,
        execution_terminated: bool,
        terminal_status: str | None = None,
    ):
        super().__init__("investigation-timeout")
        self.cumulative_tokens = cumulative_tokens
        self.execution_terminated = execution_terminated
        self.terminal_status = terminal_status


class TurnLifecycleError(InvestigatorError):
    """An active turn failed with known or unknown runtime termination."""

    def __init__(self, *, execution_terminated: bool):
        super().__init__("active-turn-runtime-failure")
        self.execution_terminated = execution_terminated


class RpcTransport(Protocol):
    def send(self, message: Mapping[str, Any]) -> None: ...

    def receive(self, timeout: float) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class SubprocessJsonRpcTransport:
    """Newline JSON-RPC over an injected or explicitly configured subprocess."""

    def __init__(
        self,
        executable: Sequence[str] = ("codex", "app-server"),
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        environment: Mapping[str, str] | None = None,
        max_message_bytes: int = MAX_RPC_BYTES,
        io_timeout: float = TRANSPORT_IO_TIMEOUT_SECONDS,
    ):
        if not executable or any(not isinstance(value, str) or not value for value in executable):
            raise ValueError("App Server executable must be a non-empty argv sequence")
        if max_message_bytes < 1024 or max_message_bytes > 16 * 1024 * 1024:
            raise ValueError("invalid App Server message bound")
        if io_timeout <= 0 or io_timeout > 30:
            raise ValueError("invalid App Server I/O timeout")
        self.max_message_bytes = max_message_bytes
        self.io_timeout = io_timeout
        if environment is None:
            environment = private_codex_environment(
                Path("/var/lib/imladris/terracompute-codex")
            )
        self._process = popen_factory(
            tuple(executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment) if environment is not None else None,
            bufsize=0,
            # Its own process group, so stopping it stops what it started. Killing
            # the leader alone leaves children running and the exit unconfirmed.
            start_new_session=True,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeUnavailable("app-server-start-failed")
        self._incoming: queue.Queue[object] = queue.Queue(maxsize=MAX_TRANSPORT_QUEUE)
        self._outgoing: queue.Queue[tuple[bytes, threading.Event, list[Exception]] | None] = queue.Queue(
            maxsize=MAX_TRANSPORT_QUEUE
        )
        self._closed = threading.Event()
        self._process_lock = threading.Lock()
        self._termination_confirmed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._reader.start()
        self._writer.start()

    def _publish(self, item: object) -> bool:
        try:
            self._incoming.put(item, timeout=self.io_timeout)
            return True
        except queue.Full:
            # A peer that outruns the bounded consumer is unusable. Terminating
            # it also releases a reader blocked in readline without blocking this
            # queue producer forever.
            self.terminate()
            return False

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._process.stdout.readline(self.max_message_bytes + 1)
                if not line:
                    self._publish(EOFError())
                    return
                if len(line) > self.max_message_bytes or not line.endswith(b"\n"):
                    self._publish(ProtocolError("app-server-message-too-large"))
                    return
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._publish(ProtocolError("app-server-invalid-json"))
                    return
                if not isinstance(message, dict):
                    self._publish(ProtocolError("app-server-invalid-message"))
                    return
                if not self._publish(message):
                    return
        except Exception:
            if not self._closed.is_set():
                self._publish(RuntimeUnavailable("app-server-read-failed"))

    def _write_loop(self) -> None:
        while True:
            try:
                item = self._outgoing.get(timeout=self.io_timeout)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            if item is None:
                return
            encoded, completed, errors = item
            try:
                self._process.stdin.write(encoded)
                self._process.stdin.flush()
            except Exception:
                errors.append(RuntimeUnavailable("app-server-write-failed"))
            finally:
                completed.set()

    def send(self, message: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as error:
            raise ProtocolError("app-server-request-not-json") from error
        if len(encoded) > self.max_message_bytes:
            raise ProtocolError("app-server-request-too-large")
        if self._closed.is_set():
            raise RuntimeUnavailable("app-server-exited")
        completed = threading.Event()
        errors: list[Exception] = []
        try:
            self._outgoing.put((encoded, completed, errors), timeout=self.io_timeout)
        except queue.Full as error:
            self.terminate()
            raise RuntimeUnavailable("app-server-write-queue-full") from error
        if not completed.wait(self.io_timeout):
            self.terminate()
            raise RuntimeUnavailable("app-server-write-timeout")
        if errors:
            self.terminate()
            raise errors[0]

    def receive(self, timeout: float) -> Mapping[str, Any]:
        try:
            item = self._incoming.get(timeout=max(0.0, timeout))
        except queue.Empty as error:
            raise TimeoutError("app-server-timeout") from error
        if isinstance(item, Exception):
            if isinstance(item, EOFError):
                raise RuntimeUnavailable("app-server-exited")
            raise item
        if not isinstance(item, dict):
            raise ProtocolError("app-server-invalid-message")
        return item

    def _signal_group(self, number: int, alone: Callable[[], None]) -> None:
        """Signal the whole group, and the leader alone if that is not possible.

        A signal delivered only to the leader leaves whatever it started still
        running, and the wait that follows then never confirms -- which held a turn's
        lease for ever. An injected process has no group, so it keeps its own method.
        """
        try:
            os.killpg(os.getpgid(self._process.pid), number)
            return
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
        try:
            alone()
        except Exception:
            pass

    def _wait(self, timeout: float) -> bool:
        try:
            self._process.wait(timeout=timeout)
            return True
        except Exception:
            return False

    def _stop(self, *, graceful: bool) -> bool:
        with self._process_lock:
            if self._termination_confirmed:
                return True
            self._closed.set()
            try:
                self._outgoing.put_nowait(None)
            except queue.Full:
                pass
            if graceful:
                closed = threading.Event()

                def close_stdin() -> None:
                    try:
                        self._process.stdin.close()
                    except Exception:
                        pass
                    finally:
                        closed.set()

                threading.Thread(target=close_stdin, daemon=True).start()
                closed.wait(self.io_timeout)
                self._termination_confirmed = self._wait(self.io_timeout)
            if not self._termination_confirmed:
                self._signal_group(signal.SIGTERM, self._process.terminate)
                self._termination_confirmed = self._wait(self.io_timeout)
            if not self._termination_confirmed:
                self._signal_group(signal.SIGKILL, self._process.kill)
                self._termination_confirmed = self._wait(TRANSPORT_REAP_TIMEOUT_SECONDS)
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=self.io_timeout)
        if threading.current_thread() is not self._writer:
            self._writer.join(timeout=self.io_timeout)
        return self._termination_confirmed

    def terminate(self) -> bool:
        """Boundedly terminate/kill/wait and report confirmed process exit."""
        return self._stop(graceful=False)

    def close(self) -> None:
        """Boundedly close the stream, escalating through terminate and kill."""
        self._stop(graceful=True)


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    status: str
    agent_text: str
    cumulative_tokens: int | None
    error: str | None = None


class AppServerClient:
    """Synchronous facade with response/event demultiplexing and fail-closed requests."""

    def __init__(
        self,
        transport: RpcTransport,
        *,
        request_handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_message_bytes: int = MAX_RPC_BYTES,
    ):
        self.transport = transport
        self.request_handlers = dict(request_handlers or {})
        self.clock = clock
        self.max_message_bytes = max_message_bytes
        self._next_id = 1
        self._responses: dict[int | str, Mapping[str, Any]] = {}
        self._notifications: list[Mapping[str, Any]] = []
        self._initialized = False

    def close(self) -> None:
        self.transport.close()

    def _send(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > self.max_message_bytes:
            raise ProtocolError("app-server-request-too-large")
        self.transport.send(message)

    def _deadline(self, timeout: float) -> float:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        return self.clock() + timeout

    def _receive(self, deadline: float) -> Mapping[str, Any]:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("app-server-timeout")
        message = self.transport.receive(remaining)
        if not isinstance(message, dict):
            raise ProtocolError("app-server-invalid-message")
        try:
            size = len(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode())
        except (TypeError, ValueError) as error:
            raise ProtocolError("app-server-invalid-message") from error
        if size > self.max_message_bytes:
            raise ProtocolError("app-server-message-too-large")
        if "id" in message and "method" in message:
            if not isinstance(message["id"], (int, str)) or isinstance(message["id"], bool):
                raise ProtocolError("app-server-invalid-request-id")
            self._handle_server_request(message)
        elif "id" in message:
            if not isinstance(message["id"], (int, str)) or isinstance(message["id"], bool):
                raise ProtocolError("app-server-invalid-response-id")
            if len(self._responses) >= MAX_PENDING_MESSAGES:
                raise ProtocolError("app-server-too-many-pending-responses")
            self._responses[message["id"]] = message
        elif isinstance(message.get("method"), str):
            if len(self._notifications) >= MAX_PENDING_MESSAGES:
                raise ProtocolError("app-server-too-many-notifications")
            self._notifications.append(message)
        else:
            raise ProtocolError("app-server-invalid-message")
        return message

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            self._send({"id": request_id, "error": {"code": -32600, "message": "invalid request"}})
            return
        handler = self.request_handlers.get(method)
        if handler is not None:
            try:
                result = handler(params)
                if not isinstance(result, Mapping):
                    raise ValueError
                self._send({"id": request_id, "result": dict(result)})
            except Exception:
                self._send({"id": request_id, "error": {"code": -32603, "message": "request rejected"}})
            return

        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            self._send({"id": request_id, "result": {"decision": "decline"}})
        elif method == "item/permissions/requestApproval":
            self._send({"id": request_id, "result": {"permissions": []}})
        elif method == "mcpServer/elicitation/request":
            self._send({"id": request_id, "result": {"action": "decline", "content": None}})
        else:
            self._send({"id": request_id, "error": {"code": -32601, "message": "request not allowed"}})

    def request(self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float = 30) -> Any:
        if not self._initialized and method != "initialize":
            raise ProtocolError("app-server-not-initialized")
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)
        deadline = self._deadline(timeout)
        while request_id not in self._responses:
            self._receive(deadline)
        response = self._responses.pop(request_id)
        if "error" in response:
            raise RuntimeUnavailable("app-server-rpc-error")
        if "result" not in response:
            raise ProtocolError("app-server-response-missing-result")
        return response["result"]

    def initialize(self, *, timeout: float = 30) -> Mapping[str, Any]:
        result = self.request(
            "initialize",
            {"clientInfo": {"name": "terracompute_ops", "title": "Terracompute Ops", "version": "0.1.0"}},
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise ProtocolError("app-server-invalid-initialize")
        self._send({"method": "initialized", "params": {}})
        self._initialized = True
        return result

    def account_available(self, *, timeout: float = 30) -> bool:
        result = self.request("account/read", {"refreshToken": False}, timeout=timeout)
        if not isinstance(result, dict):
            return False
        if result.get("requiresOpenaiAuth") is True and result.get("account") is None:
            return False
        return result.get("account") is not None or result.get("requiresOpenaiAuth") is False

    def account_limits_available(self, *, timeout: float = 30) -> bool:
        result = self.request("account/rateLimits/read", timeout=timeout)
        if not isinstance(result, dict):
            return False
        buckets = result.get("rateLimitsByLimitId")
        if isinstance(buckets, dict):
            values = list(buckets.values())
        else:
            values = [result.get("rateLimits")]
        found = False
        for bucket in values:
            if not isinstance(bucket, dict):
                continue
            found = True
            if bucket.get("rateLimitReachedType") is not None:
                return False
            for name in ("primary", "secondary"):
                window = bucket.get(name)
                if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)):
                    if window["usedPercent"] >= 100:
                        return False
        return found

    def model_available(self, model: str, effort: str, *, timeout: float = 30) -> bool:
        result = self.request("model/list", {"limit": 100, "includeHidden": False}, timeout=timeout)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return False
        for entry in result["data"]:
            if not isinstance(entry, dict) or entry.get("model", entry.get("id")) != model:
                continue
            supported = entry.get("supportedReasoningEfforts")
            if not isinstance(supported, list):
                return False
            return any(isinstance(item, dict) and item.get("reasoningEffort") == effort for item in supported)
        return False

    def start_thread(self, model: str, *, timeout: float = 30) -> str:
        result = self.request(
            "thread/start",
            # These two spellings are not interchangeable: `sandbox` here is kebab-case
            # while `sandboxPolicy.type` on a turn is camelCase. The app server rejects
            # the wrong one outright, which is how a whole diagnosis went missing.
            {"model": model, "approvalPolicy": "never", "sandbox": "read-only",
             "serviceName": "terracompute_ops"},
            timeout=timeout,
        )
        try:
            thread_id = result["thread"]["id"]
        except (KeyError, TypeError) as error:
            raise ProtocolError("app-server-invalid-thread") from error
        if not isinstance(thread_id, str) or not thread_id:
            raise ProtocolError("app-server-invalid-thread")
        return thread_id

    def resume_thread(self, thread_id: str, *, timeout: float = 30) -> None:
        result = self.request("thread/resume", {"threadId": thread_id}, timeout=timeout)
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise ProtocolError("app-server-invalid-thread")
        if result["thread"].get("id") != thread_id:
            raise ProtocolError("app-server-thread-mismatch")

    @staticmethod
    def _usage_total(params: Mapping[str, Any], thread_id: str) -> int | None:
        if params.get("threadId") != thread_id:
            return None
        token_usage = params.get("tokenUsage")
        if not isinstance(token_usage, dict) or not isinstance(token_usage.get("total"), dict):
            return None
        value = token_usage["total"].get("totalTokens")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _drain_turn_notifications(
        self,
        thread_id: str,
        turn_id: str,
        pieces: list[str],
        cumulative: int | None,
    ) -> tuple[str | None, int | None]:
        terminal_status: str | None = None
        index = 0
        while index < len(self._notifications):
            event = self._notifications[index]
            method = event.get("method")
            params = event.get("params", {})
            if method == "thread/tokenUsage/updated" and isinstance(params, dict):
                value = self._usage_total(params, thread_id)
                if value is not None:
                    cumulative = value if cumulative is None else max(cumulative, value)
                    self._notifications.pop(index)
                    continue
            if method == "item/agentMessage/delta" and isinstance(params, dict):
                if params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                    delta = params.get("delta")
                    if isinstance(delta, str):
                        if sum(len(piece.encode("utf-8")) for piece in pieces) + len(delta.encode("utf-8")) > MAX_AGENT_TEXT_BYTES:
                            raise ProtocolError("app-server-agent-output-too-large")
                        pieces.append(delta)
                        self._notifications.pop(index)
                        continue
            if method == "turn/completed" and isinstance(params, dict):
                turn = params.get("turn")
                if (
                    params.get("threadId") == thread_id
                    and isinstance(turn, dict)
                    and turn.get("id") == turn_id
                ):
                    self._notifications.pop(index)
                    status = turn.get("status")
                    if status not in {"completed", "interrupted", "failed"}:
                        raise ProtocolError("app-server-invalid-turn-status")
                    terminal_status = status
                    continue
            index += 1
        return terminal_status, cumulative

    def _terminate_runtime(self) -> bool:
        terminate = getattr(self.transport, "terminate", None)
        if callable(terminate):
            try:
                return terminate() is True
            except Exception:
                return False
        try:
            self.transport.close()
        except Exception:
            pass
        # Legacy/injected transports cannot prove that close terminated their
        # backing runtime, so admission must remain held.
        return False

    def _finish_timed_out_turn(
        self,
        thread_id: str,
        turn_id: str,
        pieces: list[str],
        cumulative: int | None,
    ) -> None:
        cleanup_deadline = self.clock() + INTERRUPT_CLEANUP_TIMEOUT_SECONDS
        try:
            remaining = cleanup_deadline - self.clock()
            if remaining > 0:
                self.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=remaining,
                )
        except (InvestigatorError, TimeoutError, OSError):
            pass
        while True:
            try:
                terminal_status, cumulative = self._drain_turn_notifications(
                    thread_id, turn_id, pieces, cumulative
                )
            except InvestigatorError as error:
                confirmed = self._terminate_runtime()
                raise TurnLifecycleError(execution_terminated=confirmed) from error
            if terminal_status is not None:
                raise InvestigationTimeout(
                    cumulative_tokens=cumulative,
                    execution_terminated=True,
                    terminal_status=terminal_status,
                )
            if self.clock() >= cleanup_deadline:
                break
            try:
                self._receive(cleanup_deadline)
            except (InvestigatorError, TimeoutError, OSError):
                break
        confirmed = self._terminate_runtime()
        raise InvestigationTimeout(
            cumulative_tokens=cumulative,
            execution_terminated=confirmed,
        )

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        model: str,
        effort: str,
        timeout: float = 600,
        on_started: Callable[[str], None] | None = None,
    ) -> TurnResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("prompt must be non-empty and at most 64 KiB")
        try:
            result = self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                    # readOnly no longer takes `access`: the App Server refuses it and
                    # points at a permission profile, which turn parameters do not
                    # carry. So readable roots can no longer be pinned to none here,
                    # and the filesystem side of this sandbox is weaker than it was.
                    # Network access stays off, which this version does still accept.
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "model": model,
                    "effort": effort,
                    "summary": "concise",
                },
                timeout=min(timeout, TURN_START_ACK_SECONDS),
            )
        except (InvestigatorError, TimeoutError, OSError) as error:
            confirmed = self._terminate_runtime()
            raise TurnLifecycleError(execution_terminated=confirmed) from error
        try:
            turn_id = result["turn"]["id"]
        except (KeyError, TypeError) as error:
            raise ProtocolError("app-server-invalid-turn") from error
        if not isinstance(turn_id, str) or not turn_id:
            raise ProtocolError("app-server-invalid-turn")
        if on_started is not None:
            try:
                on_started(turn_id)
            except Exception as error:
                try:
                    self._finish_timed_out_turn(thread_id, turn_id, [], None)
                except (InvestigationTimeout, TurnLifecycleError) as cleanup:
                    raise TurnLifecycleError(
                        execution_terminated=cleanup.execution_terminated
                    ) from error

        deadline = self._deadline(timeout)
        pieces: list[str] = []
        cumulative: int | None = None
        try:
            while True:
                terminal_status, cumulative = self._drain_turn_notifications(
                    thread_id, turn_id, pieces, cumulative
                )
                if terminal_status is not None:
                    error = "turn-failed" if terminal_status == "failed" else None
                    return TurnResult(
                        thread_id,
                        turn_id,
                        terminal_status,
                        "".join(pieces),
                        cumulative,
                        error,
                    )
                self._receive(deadline)
        except TimeoutError:
            self._finish_timed_out_turn(
                thread_id, turn_id, pieces, cumulative
            )
        except InvestigatorError as error:
            confirmed = self._terminate_runtime()
            raise TurnLifecycleError(execution_terminated=confirmed) from error


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    turn_row_id: int | None = None


@dataclass(frozen=True)
class InvestigationResult:
    status: str
    episode_id: int
    thread_id: str | None
    turn_id: str | None
    text: str
    reported_tokens: int | None
    overshoot_tokens: int
    reason: str | None = None


class InvestigationStore:
    """Namespaced SQLite persistence without changing the database user_version."""

    def __init__(self, database: str | Path | sqlite3.Connection):
        self.db = database if isinstance(database, sqlite3.Connection) else sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS terracompute_investigation_episodes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              incident_id TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              severity TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              thread_id TEXT,
              accounting_available INTEGER NOT NULL DEFAULT 1,
              created_utc TEXT NOT NULL,
              completed_utc TEXT,
              UNIQUE(incident_id, evidence_hash)
            );
            CREATE TABLE IF NOT EXISTS terracompute_investigation_turns (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              episode_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              model TEXT NOT NULL,
              status TEXT NOT NULL,
              started_utc TEXT NOT NULL,
              completed_utc TEXT,
              runtime_turn_id TEXT,
              cumulative_tokens INTEGER,
              reported_tokens INTEGER NOT NULL DEFAULT 0,
              usage_available INTEGER NOT NULL DEFAULT 1,
              overshoot_tokens INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(episode_id) REFERENCES terracompute_investigation_episodes(id)
            );
            CREATE INDEX IF NOT EXISTS terracompute_investigation_turns_started
              ON terracompute_investigation_turns(started_utc);
            """
        )
        self._correct_unacknowledged_spend()
        self.db.commit()

    STALE_LEASE_SECONDS = 4 * 3600

    def release_stale_leases(self, now: datetime) -> None:
        """Release a turn's lease once no turn could still be running under it.

        A lease is held so that a turn is never started twice. A result is only ever
        accepted over the App Server transport that produced it, and that transport
        does not outlive the process holding it, so after a bound far beyond any turn
        this runtime will wait for, no result from that turn can still be accepted and
        the lease protects nothing. Holding it anyway refuses every later turn until
        somebody edits this database, which is how the investigator went silent three
        times in one evening.
        """
        cutoff = self._utc(now - timedelta(seconds=self.STALE_LEASE_SECONDS))
        self.db.execute(
            """UPDATE terracompute_investigation_turns
                 SET status='lease-expired', completed_utc=?
               WHERE status='in_flight' AND started_utc < ?""",
            (self._utc(now), cutoff),
        )
        self.db.commit()

    def _correct_unacknowledged_spend(self) -> None:
        """A turn the App Server never acknowledged spent nothing; say so.

        Recorded as unknown, one such turn refuses every later turn in its episode and
        in the whole rolling window, so a single refused call costs a day of diagnosis
        and needs a hand on the database to undo. The record is corrected where it is
        provably wrong, and only there: a turn that has a runtime turn ID really did
        run, and its unknown spend is left exactly as it stands.
        """
        self.db.execute(
            """UPDATE terracompute_investigation_turns
                 SET usage_available=1, reported_tokens=0, cumulative_tokens=0
               WHERE usage_available=0 AND runtime_turn_id IS NULL"""
        )
        self.db.execute(
            """UPDATE terracompute_investigation_episodes SET accounting_available=1
               WHERE accounting_available=0 AND id NOT IN (
                 SELECT episode_id FROM terracompute_investigation_turns WHERE usage_available=0
               )"""
        )

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _utc(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def episode(self, incident_id: str, evidence_hash: str, severity: str, now: datetime) -> tuple[sqlite3.Row, bool]:
        if not incident_id or len(incident_id) > 128 or not evidence_hash or len(evidence_hash) > 128 or severity not in {"info", "warning", "error", "critical"}:
            raise ValueError("invalid episode identity")
        existing = self.db.execute(
            "SELECT * FROM terracompute_investigation_episodes WHERE incident_id=? AND evidence_hash=?",
            (incident_id, evidence_hash),
        ).fetchone()
        if existing is not None:
            return existing, False
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO terracompute_investigation_episodes(incident_id,evidence_hash,severity,created_utc) VALUES(?,?,?,?)",
            (incident_id, evidence_hash, severity, self._utc(now)),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT * FROM terracompute_investigation_episodes WHERE incident_id=? AND evidence_hash=?",
            (incident_id, evidence_hash),
        ).fetchone()
        assert row is not None
        return row, cursor.rowcount == 1

    def set_thread(self, episode_id: int, thread_id: str) -> None:
        self.db.execute(
            "UPDATE terracompute_investigation_episodes SET thread_id=? WHERE id=? AND thread_id IS NULL",
            (thread_id, episode_id),
        )
        self.db.commit()

    def admit(self, episode_id: int, role: str, model: str, now: datetime) -> AdmissionDecision:
        if role not in {"lead", "helper"}:
            raise ValueError("role must be lead or helper")
        stamp = self._utc(now)
        cutoff = self._utc(now - timedelta(hours=24))
        try:
            self.db.execute("BEGIN IMMEDIATE")
            episode = self.db.execute(
                "SELECT * FROM terracompute_investigation_episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if episode is None or episode["status"] != "open":
                self.db.rollback()
                return AdmissionDecision(False, "episode-closed")
            active = self.db.execute(
                "SELECT role,episode_id FROM terracompute_investigation_turns WHERE status='in_flight'"
            ).fetchall()
            if any(row["role"] == role for row in active):
                self.db.rollback()
                return AdmissionDecision(False, f"{role}-concurrency-cap")
            if role == "helper" and any(row["episode_id"] != episode_id for row in active):
                self.db.rollback()
                return AdmissionDecision(False, "helper-episode-mismatch")
            episode_unknown = self.db.execute(
                "SELECT 1 FROM terracompute_investigation_turns WHERE episode_id=? AND usage_available=0 LIMIT 1",
                (episode_id,),
            ).fetchone()
            if episode_unknown is not None:
                self.db.rollback()
                return AdmissionDecision(False, "episode-token-accounting-unavailable")
            rolling_unknown = self.db.execute(
                "SELECT 1 FROM terracompute_investigation_turns WHERE started_utc>=? AND usage_available=0 LIMIT 1",
                (cutoff,),
            ).fetchone()
            if rolling_unknown is not None:
                self.db.rollback()
                return AdmissionDecision(False, "rolling-token-accounting-unavailable")
            episode_stats = self.db.execute(
                "SELECT COUNT(*) turns, COALESCE(SUM(reported_tokens),0) tokens FROM terracompute_investigation_turns WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if episode_stats["turns"] >= 4:
                self.db.rollback()
                return AdmissionDecision(False, "episode-turn-cap")
            if episode_stats["tokens"] >= 60_000:
                self.db.rollback()
                return AdmissionDecision(False, "episode-token-cap")
            day_stats = self.db.execute(
                "SELECT COUNT(*) turns, COALESCE(SUM(reported_tokens),0) tokens FROM terracompute_investigation_turns WHERE started_utc>=?",
                (cutoff,),
            ).fetchone()
            if day_stats["turns"] >= 20:
                self.db.rollback()
                return AdmissionDecision(False, "rolling-turn-cap")
            daily_cap = 300_000 if episode["severity"] == "critical" else 200_000
            if day_stats["tokens"] >= daily_cap:
                self.db.rollback()
                return AdmissionDecision(False, "critical-reserve" if daily_cap == 200_000 else "rolling-token-cap")
            if model == ESCALATION_MODEL:
                episode_astra = self.db.execute(
                    "SELECT COUNT(*) FROM terracompute_investigation_turns WHERE episode_id=? AND model=?",
                    (episode_id, ESCALATION_MODEL),
                ).fetchone()[0]
                daily_astra = self.db.execute(
                    "SELECT COUNT(*) FROM terracompute_investigation_turns WHERE started_utc>=? AND model=?",
                    (cutoff, ESCALATION_MODEL),
                ).fetchone()[0]
                if episode_astra >= 1 or daily_astra >= 2:
                    self.db.rollback()
                    return AdmissionDecision(False, "astra-cap")
            cursor = self.db.execute(
                "INSERT INTO terracompute_investigation_turns(episode_id,role,model,status,started_utc) VALUES(?,?,?,'in_flight',?)",
                (episode_id, role, model, stamp),
            )
            self.db.commit()
            return AdmissionDecision(True, "admitted", int(cursor.lastrowid))
        except Exception:
            self.db.rollback()
            raise

    def record_usage(self, turn_row_id: int, thread_id: str, cumulative_tokens: int | None) -> int | None:
        if cumulative_tokens is None:
            self.db.execute(
                "UPDATE terracompute_investigation_turns SET usage_available=0 WHERE id=?", (turn_row_id,)
            )
            self.db.execute(
                "UPDATE terracompute_investigation_episodes SET accounting_available=0 WHERE id=(SELECT episode_id FROM terracompute_investigation_turns WHERE id=?)",
                (turn_row_id,),
            )
            self.db.commit()
            return None
        if cumulative_tokens < 0:
            raise ValueError("negative token usage")
        row = self.db.execute(
            "SELECT episode_id,cumulative_tokens,reported_tokens FROM terracompute_investigation_turns WHERE id=?",
            (turn_row_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown turn")
        prior = self.db.execute(
            """SELECT MAX(cumulative_tokens) FROM terracompute_investigation_turns t
               JOIN terracompute_investigation_episodes e ON e.id=t.episode_id
               WHERE e.thread_id=? AND t.id<>?""",
            (thread_id, turn_row_id),
        ).fetchone()[0]
        own = row["cumulative_tokens"]
        high_water = max(value for value in (prior, own, 0) if value is not None)
        if cumulative_tokens < high_water:
            self.record_usage(turn_row_id, thread_id, None)
            return None
        delta = max(0, cumulative_tokens - high_water)
        new_reported = row["reported_tokens"] + delta
        self.db.execute(
            "UPDATE terracompute_investigation_turns SET cumulative_tokens=?,reported_tokens=? WHERE id=?",
            (max(high_water, cumulative_tokens), new_reported, turn_row_id),
        )
        self.db.commit()
        return delta

    def set_runtime_turn(self, turn_row_id: int, runtime_turn_id: str) -> None:
        if not runtime_turn_id or len(runtime_turn_id) > 256:
            raise ValueError("invalid runtime turn id")
        cursor = self.db.execute(
            "UPDATE terracompute_investigation_turns SET runtime_turn_id=? WHERE id=? AND status='in_flight'",
            (runtime_turn_id, turn_row_id),
        )
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError("turn admission is not in flight")

    def finish_turn(self, turn_row_id: int, runtime_turn_id: str | None, status: str, now: datetime) -> int:
        row = self.db.execute(
            """SELECT t.episode_id,e.severity
               FROM terracompute_investigation_turns t
               JOIN terracompute_investigation_episodes e ON e.id=t.episode_id
               WHERE t.id=?""",
            (turn_row_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown turn")
        episode_tokens = self.db.execute(
            "SELECT COALESCE(SUM(reported_tokens),0) FROM terracompute_investigation_turns WHERE episode_id=?",
            (row["episode_id"],),
        ).fetchone()[0]
        cutoff = self._utc(now - timedelta(hours=24))
        daily_tokens = self.db.execute(
            "SELECT COALESCE(SUM(reported_tokens),0) FROM terracompute_investigation_turns WHERE started_utc>=?",
            (cutoff,),
        ).fetchone()[0]
        daily_cap = 300_000 if row["severity"] == "critical" else 200_000
        overshoot = max(0, episode_tokens - 60_000, daily_tokens - daily_cap)
        self.db.execute(
            "UPDATE terracompute_investigation_turns SET status=?,completed_utc=?,runtime_turn_id=COALESCE(?,runtime_turn_id),overshoot_tokens=? WHERE id=?",
            (status, self._utc(now), runtime_turn_id, overshoot, turn_row_id),
        )
        self.db.commit()
        return overshoot

    def complete_episode(self, episode_id: int, now: datetime) -> None:
        self.db.execute(
            "UPDATE terracompute_investigation_episodes SET status='completed',completed_utc=? WHERE id=?",
            (self._utc(now), episode_id),
        )
        self.db.commit()

    def reopen_episode(self, episode_id: int) -> None:
        self.db.execute(
            "UPDATE terracompute_investigation_episodes SET status='open',completed_utc=NULL WHERE id=? AND status='completed'",
            (episode_id,),
        )
        self.db.commit()


class Investigator:
    """Lead-turn coordinator; helper creation is disabled until runtime validation."""

    def __init__(
        self,
        client: AppServerClient,
        store: InvestigationStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        native_helpers_verified: bool = False,
    ):
        self.client = client
        self.store = store
        self.now = now
        self.native_helpers_verified = native_helpers_verified

    def require_helper_capability(self) -> None:
        if not self.native_helpers_verified:
            raise RuntimeUnavailable("native-helper-capability-not-commissioned")

    def investigate(
        self,
        incident_id: str,
        evidence_hash: str,
        prompt: str,
        *,
        severity: str = "error",
        model: str = LEAD_MODEL,
        effort: str = LEAD_EFFORT,
        escalation_justified: bool = False,
        timeout: float = 600,
    ) -> InvestigationResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("prompt must be non-empty and at most 64 KiB")
        if timeout <= 0 or timeout > 600:
            raise ValueError("lead timeout must be at most ten minutes")
        if (model, effort) not in {
            (LEAD_MODEL, LEAD_EFFORT),
            (ESCALATION_MODEL, ESCALATION_EFFORT),
        }:
            raise ValueError("unsupported lead route")
        episode, created = self.store.episode(incident_id, evidence_hash, severity, self.now())
        explicit_escalation = model == ESCALATION_MODEL and escalation_justified
        if not created and episode["status"] == "completed" and not explicit_escalation:
            return InvestigationResult("unchanged", episode["id"], episode["thread_id"], None, "", 0, 0, "unchanged-evidence")
        if model == ESCALATION_MODEL and not escalation_justified:
            return InvestigationResult("rejected", episode["id"], episode["thread_id"], None, "", None, 0, "astra-escalation-not-justified")
        try:
            if not self.client.account_available() or not self.client.account_limits_available():
                return InvestigationResult("unavailable", episode["id"], episode["thread_id"], None, "", None, 0, "auth-or-quota-unavailable")
            if not self.client.model_available(model, effort):
                return InvestigationResult("unavailable", episode["id"], episode["thread_id"], None, "", None, 0, "model-unavailable")
        except (InvestigatorError, TimeoutError, OSError):
            return InvestigationResult("unavailable", episode["id"], episode["thread_id"], None, "", None, 0, "runtime-unavailable")
        reopened = False
        if not created and episode["status"] == "completed" and explicit_escalation:
            self.store.reopen_episode(episode["id"])
            reopened = True
        admission = self.store.admit(episode["id"], "lead", model, self.now())
        if not admission.admitted or admission.turn_row_id is None:
            if reopened:
                self.store.complete_episode(episode["id"], self.now())
            return InvestigationResult("rejected", episode["id"], episode["thread_id"], None, "", None, 0, admission.reason)
        row_id = admission.turn_row_id
        thread_id = episode["thread_id"]
        runtime_turn_id: str | None = None

        def persist_runtime_turn(turn_id: str) -> None:
            nonlocal runtime_turn_id
            self.store.set_runtime_turn(row_id, turn_id)
            runtime_turn_id = turn_id

        try:
            if thread_id:
                self.client.resume_thread(thread_id)
            else:
                thread_id = self.client.start_thread(model)
                self.store.set_thread(episode["id"], thread_id)
            turn = self.client.run_turn(
                thread_id,
                prompt,
                model=model,
                effort=effort,
                timeout=timeout,
                on_started=persist_runtime_turn,
            )
            runtime_turn_id = turn.turn_id
            reported_tokens = self.store.record_usage(
                row_id, thread_id, turn.cumulative_tokens
            )
            overshoot = self.store.finish_turn(row_id, turn.turn_id, turn.status, self.now())
            if turn.status == "completed":
                self.store.complete_episode(episode["id"], self.now())
            return InvestigationResult(turn.status, episode["id"], thread_id, turn.turn_id, turn.agent_text, reported_tokens, overshoot, turn.error)
        except InvestigationTimeout as error:
            reported_tokens = self.store.record_usage(
                row_id, thread_id or "", error.cumulative_tokens
            )
            if error.terminal_status is None and error.cumulative_tokens is not None:
                # A pre-kill cumulative update is a useful lower bound, but is
                # not proof of final usage when no terminal event was observed.
                self.store.record_usage(row_id, thread_id or "", None)
            if error.execution_terminated:
                overshoot = self.store.finish_turn(
                    row_id, runtime_turn_id, "timeout", self.now()
                )
                reason = "investigation-timeout"
            else:
                overshoot = 0
                reason = "investigation-timeout-execution-unknown"
            return InvestigationResult(
                "timeout",
                episode["id"],
                thread_id,
                runtime_turn_id,
                "",
                reported_tokens,
                overshoot,
                reason,
            )
        except TimeoutError:
            # An injected client that cannot report runtime termination is not
            # allowed to release admission merely because its local call ended.
            self.store.record_usage(row_id, thread_id or "", None)
            return InvestigationResult(
                "timeout",
                episode["id"],
                thread_id,
                runtime_turn_id,
                "",
                None,
                0,
                "investigation-timeout-execution-unknown",
            )
        except TurnLifecycleError as error:
            self.store.record_usage(row_id, thread_id or "", _spend_before(runtime_turn_id))
            if error.execution_terminated:
                self.store.finish_turn(
                    row_id, runtime_turn_id, "runtime-failure", self.now()
                )
                reason = "runtime-unavailable"
            else:
                reason = "runtime-failure-execution-unknown"
            return InvestigationResult(
                "unavailable",
                episode["id"],
                thread_id,
                runtime_turn_id,
                "",
                None,
                0,
                reason,
            )
        except (InvestigatorError, OSError):
            # Before a runtime turn ID is persisted there is no evidence that a
            # possibly accepted turn was terminated. Keep the durable lease.
            self.store.record_usage(row_id, thread_id or "", _spend_before(runtime_turn_id))
            return InvestigationResult(
                "unavailable",
                episode["id"],
                thread_id,
                runtime_turn_id,
                "",
                None,
                0,
                "runtime-failure-execution-unknown",
            )

    def run_helper(self, *_args: Any, **_kwargs: Any) -> None:
        self.require_helper_capability()
        raise RuntimeUnavailable("native-helper-orchestration-not-implemented")


def helper_route(kind: str) -> tuple[str, str]:
    """Return the optional bounded helper route without creating a helper."""
    if kind == "comparison":
        return HELPER_MODEL, HELPER_EFFORT
    if kind == "summary":
        return SUMMARY_MODEL, SUMMARY_EFFORT
    raise ValueError("unknown helper kind")


def _spend_before(runtime_turn_id: str | None) -> int | None:
    """What a turn spent when it failed: nothing, if it was never acknowledged.

    Unknown spend disables every later turn for the whole rolling window, which is the
    right answer when a turn ran and we lost count of it -- and a false one when the
    App Server never accepted a turn at all. Those are different unknowns, and
    conflating them turns one refused call into a day without diagnosis.
    """
    return None if runtime_turn_id else 0


def private_codex_environment(service_home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a private default-HOME environment; no custom seat or provider state."""
    if not service_home.is_absolute():
        raise ValueError("service home must be absolute")
    source = base if base is not None else os.environ
    safe_names = {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    environment = {name: source[name] for name in safe_names if name in source}
    environment["HOME"] = str(service_home)
    return environment
