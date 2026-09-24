from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import unittest

from terracompute_ops.webhooks import (
    MAX_BODY_BYTES,
    EnqueueResult,
    WebhookError,
    WebhookEvent,
    verify_and_enqueue,
    webhook_server,
)


SECRET = "synthetic-webhook-secret"
NOW = 1_772_490_100


def body(**changes: object) -> bytes:
    payload = {
        "event_id": "7e9a2c4e6f9e4a24a53b77c2d8e3f0aa",
        "user_id": 123,
        "notif_type": "future_unrecognized_type",
        "subject": "Synthetic notification",
        "message": "Synthetic message",
        "timestamp": 1_772_490_000.123,
    }
    payload.update(changes)
    return json.dumps(payload, separators=(",", ":")).encode()


def headers(raw: bytes, *, timestamp: int = NOW, attempt: int = 1) -> dict[str, str]:
    digest = hmac.new(
        SECRET.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    event_id = json.loads(raw).get("event_id", "") if raw.startswith(b"{") else ""
    return {
        "Content-Type": "application/json",
        "X-Vast-Event-Id": event_id,
        "X-Vast-Delivery-Attempt": str(attempt),
        "X-Vast-Timestamp": str(timestamp),
        "X-Vast-Signature-256": f"sha256={digest}",
    }


class DurableQueue:
    def __init__(self):
        self.events: dict[str, WebhookEvent] = {}
        self.calls = 0

    def __call__(self, event: WebhookEvent) -> EnqueueResult:
        self.calls += 1
        if event.event_id in self.events:
            return EnqueueResult.DUPLICATE
        self.events[event.event_id] = event
        return EnqueueResult.ACCEPTED


class WebhookTests(unittest.TestCase):
    def test_foreign_listener_host_is_rejected_before_bind(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            webhook_server("192.0.2.10", 0, SECRET, DurableQueue())

    def test_real_loopback_connection_has_absolute_accept_to_body_deadline(self) -> None:
        queue = DurableQueue()
        server = webhook_server(
            "127.0.0.1", 0, SECRET, queue, request_deadline=1.0
        )
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        started = time.monotonic()
        connection = socket.create_connection(server.server_address, timeout=1)
        try:
            raw = body()
            request_headers = (
                f"POST /vast/v1/events HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n"
                f"X-Vast-Event-Id: {json.loads(raw)['event_id']}\r\n"
                f"X-Vast-Delivery-Attempt: 1\r\n"
                f"X-Vast-Timestamp: {NOW}\r\n"
                f"X-Vast-Signature-256: {headers(raw)['X-Vast-Signature-256']}\r\n"
                "\r\n"
            ).encode("ascii")
            connection.sendall(request_headers + raw[:1])
            # Each byte arrives within the old one-second inactivity timeout, while
            # the complete body intentionally exceeds the absolute deadline.
            time.sleep(0.45)
            connection.sendall(raw[1:2])
            time.sleep(0.45)
            connection.sendall(raw[2:3])
            time.sleep(0.25)
            connection.settimeout(1)
            self.assertEqual(connection.recv(1024), b"")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(queue.calls, 0)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_shutdown_is_bounded_by_active_connection_deadline(self) -> None:
        server = webhook_server(
            "127.0.0.1", 0, SECRET, DurableQueue(), request_deadline=1.0
        )
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        connection = socket.create_connection(server.server_address, timeout=1)
        try:
            connection.sendall(
                b"POST /vast/v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            )
            started = time.monotonic()
            server.shutdown()
            server.server_close()
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            connection.close()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_official_raw_body_signature_contract_and_unknown_type(self) -> None:
        raw = body()
        self.assertEqual(
            headers(raw)["X-Vast-Signature-256"],
            "sha256=ac7135f7b23120270b8e59b14d572f2895036a61894ea8a7d9797038f71a7697",
        )
        queue = DurableQueue()
        receipt = verify_and_enqueue(headers(raw), raw, SECRET, queue, clock=lambda: NOW)
        self.assertEqual(receipt.result, EnqueueResult.ACCEPTED)
        event = queue.events[receipt.event_id]
        self.assertEqual(event.notif_type, "future_unrecognized_type")
        self.assertEqual(event.raw_body, raw)
        self.assertIsNone(event.machine_id)
        self.assertIsNone(event.target_match)
        self.assertEqual(event.reconcile_machine_id, 17049)

    def test_raw_body_mutation_fails_signature_before_enqueue(self) -> None:
        raw = body()
        signed_headers = headers(raw)
        mutated = raw.replace(b"Synthetic message", b"Mutated message")
        queue = DurableQueue()
        with self.assertRaisesRegex(WebhookError, "signature_mismatch"):
            verify_and_enqueue(signed_headers, mutated, SECRET, queue, clock=lambda: NOW)
        self.assertEqual(queue.calls, 0)

    def test_replay_window_rejects_old_and_future_deliveries(self) -> None:
        raw = body()
        for timestamp in (NOW - 301, NOW + 301):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(WebhookError, "stale_timestamp"):
                    verify_and_enqueue(
                        headers(raw, timestamp=timestamp),
                        raw,
                        SECRET,
                        DurableQueue(),
                        clock=lambda: NOW,
                    )

    def test_duplicate_delivery_is_success_only_after_queue_deduplication(self) -> None:
        raw = body(machine_id=17049)
        queue = DurableQueue()
        first = verify_and_enqueue(headers(raw), raw, SECRET, queue, clock=lambda: NOW)
        second = verify_and_enqueue(
            headers(raw, attempt=2), raw, SECRET, queue, clock=lambda: NOW
        )
        self.assertEqual(first.result, EnqueueResult.ACCEPTED)
        self.assertEqual(second.result, EnqueueResult.DUPLICATE)
        self.assertEqual(len(queue.events), 1)
        self.assertTrue(queue.events[first.event_id].target_match)

    def test_wrong_and_unknown_machine_ids_only_request_fixed_reconciliation(self) -> None:
        for machine_id, expected_match in ((999, False), ("unknown", None), (None, None)):
            with self.subTest(machine_id=machine_id):
                raw = body(machine_id=machine_id)
                queue = DurableQueue()
                verify_and_enqueue(headers(raw), raw, SECRET, queue, clock=lambda: NOW)
                event = next(iter(queue.events.values()))
                self.assertEqual(event.target_match, expected_match)
                self.assertEqual(event.reconcile_machine_id, 17049)

    def test_malformed_oversize_and_invalid_types_never_enqueue(self) -> None:
        queue = DurableQueue()
        malformed = b"not-json"
        with self.assertRaisesRegex(WebhookError, "malformed_json"):
            verify_and_enqueue(
                headers(malformed), malformed, SECRET, queue, clock=lambda: NOW
            )
        oversized = b"{" + b"x" * MAX_BODY_BYTES
        with self.assertRaisesRegex(WebhookError, "body_too_large"):
            verify_and_enqueue({}, oversized, SECRET, queue, clock=lambda: NOW)
        invalid = body(user_id=True)
        with self.assertRaisesRegex(WebhookError, "invalid_user_id"):
            verify_and_enqueue(headers(invalid), invalid, SECRET, queue, clock=lambda: NOW)
        self.assertEqual(queue.calls, 0)

    def test_no_success_when_durable_callback_fails_or_returns_invalid_value(self) -> None:
        raw = body()

        def fail(_event: WebhookEvent) -> EnqueueResult:
            raise OSError("synthetic secret-bearing storage detail")

        with self.assertRaisesRegex(WebhookError, "durable_enqueue_failed") as raised:
            verify_and_enqueue(headers(raw), raw, SECRET, fail, clock=lambda: NOW)
        self.assertNotIn("storage detail", repr(raised.exception))
        with self.assertRaisesRegex(WebhookError, "durable_enqueue_failed"):
            verify_and_enqueue(
                headers(raw), raw, SECRET, lambda _event: True, clock=lambda: NOW  # type: ignore[arg-type,return-value]
            )


if __name__ == "__main__":
    unittest.main()
