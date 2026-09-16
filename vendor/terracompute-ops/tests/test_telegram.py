from __future__ import annotations

import json
import unittest

from terracompute_ops.telegram import (
    AuthenticationUnavailable,
    HTTPResponse,
    InputKind,
    NotificationKind,
    NotificationMetadata,
    TelegramAPIError,
    TelegramClient,
    TelegramDeliveryUncertain,
    TelegramRateLimited,
    TelegramResponseError,
    TelegramTransportError,
    TelegramUpdateConsumer,
    current_human_member,
    parse_operator_input,
)


TOKEN = "12345:test-token"
GROUP = -1001234567890


def response(document: object, status: int = 200) -> HTTPResponse:
    return HTTPResponse(status, {}, json.dumps(document).encode())


class Transport:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict, float, int]] = []

    def request(
        self, path: str, body: bytes, *, timeout: float, max_response_bytes: int
    ) -> HTTPResponse:
        self.calls.append((path, json.loads(body), timeout, max_response_bytes))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, HTTPResponse)
        return result


class TelegramTransportTests(unittest.TestCase):
    def test_send_requires_semantic_ok_and_message_id(self) -> None:
        for document in (
            {"ok": False, "error_code": 400, "description": TOKEN},
            {"ok": True, "result": {}},
        ):
            client = TelegramClient(TOKEN, transport=Transport(response(document)))
            with self.assertRaises((TelegramAPIError, TelegramResponseError)) as raised:
                client.send_message(GROUP, "hello")
            self.assertNotIn(TOKEN, repr(raised.exception))
        transport = Transport(response({"ok": True, "result": {"message_id": 77}}))
        receipt = TelegramClient(TOKEN, transport=transport).send_message(GROUP, "hello")
        self.assertEqual(receipt.message_id, 77)
        self.assertEqual(receipt.delivery_semantics, "telegram-accepted")

    def test_silent_and_metadata_are_explicit(self) -> None:
        metadata = NotificationMetadata(
            kind=NotificationKind.REMINDER,
            incident_id="inc-1",
            severity="critical",
            reminder_number=2,
        )
        transport = Transport(response({"ok": True, "result": {"message_id": 8}}))
        receipt = TelegramClient(TOKEN, transport=transport).send_message(
            GROUP, "reminder", silent=True, metadata=metadata
        )
        payload = transport.calls[0][1]
        self.assertTrue(payload["disable_notification"])
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertNotIn("metadata", payload)
        self.assertEqual(receipt.metadata, metadata)

    def test_redirect_and_oversized_response_are_rejected(self) -> None:
        redirect = TelegramClient(
            TOKEN,
            transport=Transport(response({"ok": True, "result": {}}, status=302)),
        )
        with self.assertRaises(TelegramResponseError) as raised:
            redirect.send_message(GROUP, "hello")
        self.assertEqual(raised.exception.category, "telegram-redirect-rejected")

        too_large = HTTPResponse(200, {}, b"{" + (b"x" * 1024) + b"}")
        oversized = TelegramClient(
            TOKEN, transport=Transport(too_large), max_response_bytes=1024
        )
        with self.assertRaises(TelegramResponseError) as raised:
            oversized.send_message(GROUP, "hello")
        self.assertEqual(raised.exception.category, "telegram-response-too-large")

    def test_429_retry_after_is_interpreted_with_bounds(self) -> None:
        for raw, expected in ((0, 1), (25, 25), (999999, 3600), ("bad", 1)):
            transport = Transport(
                response(
                    {
                        "ok": False,
                        "error_code": 429,
                        "parameters": {"retry_after": raw},
                    },
                    status=429,
                )
            )
            with self.assertRaises(TelegramRateLimited) as raised:
                TelegramClient(TOKEN, transport=transport).send_message(GROUP, "hello")
            self.assertEqual(raised.exception.retry_after, expected)

    def test_ambiguous_send_is_secret_free_and_not_exactly_once(self) -> None:
        client = TelegramClient(
            TOKEN, transport=Transport(OSError(f"failed URL /bot{TOKEN}/sendMessage"))
        )
        self.assertNotIn(TOKEN, repr(client))
        with self.assertRaises(TelegramDeliveryUncertain) as raised:
            client.send_message(GROUP, "hello")
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIn("uncertain", str(raised.exception))


class Backend:
    def __init__(self, cursor: int = 0) -> None:
        self.cursor = cursor
        self.accepted = []
        self.events = []
        self.fail_store = False

    def load_cursor(self, namespace: str) -> int:
        self.events.append(("load", namespace, self.cursor))
        return self.cursor

    def store_accepted(self, namespace: str, envelope: object) -> None:
        self.events.append(("store", namespace, getattr(envelope, "update_id")))
        if self.fail_store:
            raise OSError("synthetic storage failure")
        self.accepted.append(envelope)

    def advance_cursor(self, namespace: str, next_offset: int) -> None:
        self.events.append(("advance", namespace, next_offset))
        self.cursor = next_offset


class Client:
    def __init__(self, updates: list[dict], memberships: dict[int, object] | None = None):
        self.updates = updates
        self.memberships = memberships or {}
        self.update_calls = []
        self.member_calls = []

    def get_updates(self, *, offset: int, poll_timeout: int) -> tuple[dict, ...]:
        self.update_calls.append((offset, poll_timeout))
        return tuple(self.updates)

    def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        self.member_calls.append((chat_id, user_id))
        value = self.memberships.get(user_id, member(user_id))
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, dict)
        return value


def member(user_id: int, status: str = "member", **extra: object) -> dict:
    return {
        "status": status,
        "user": {"id": user_id, "is_bot": False},
        **extra,
    }


def message_update(
    update_id: int,
    text: str = "what happened?",
    *,
    chat_id: int = GROUP,
    user_id: int = 42,
    is_bot: bool = False,
    sender_chat: bool = False,
    chat_type: str = "supergroup",
) -> dict:
    message = {
        "message_id": update_id + 100,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": is_bot},
        "text": text,
    }
    if sender_chat:
        message["sender_chat"] = {"id": chat_id, "type": chat_type}
    return {"update_id": update_id, "message": message}


class TelegramInputTests(unittest.TestCase):
    def test_polling_is_inert_until_explicitly_enabled(self) -> None:
        client = Client([message_update(1)])
        consumer = TelegramUpdateConsumer(client, Backend(), group_id=GROUP)
        self.assertEqual(consumer.poll_once(), ())
        self.assertEqual(client.update_calls, [])

    def test_accepted_input_is_committed_before_cursor_advance(self) -> None:
        backend = Backend()
        client = Client([message_update(10, "/ack inc-1")])
        consumer = TelegramUpdateConsumer(
            client, backend, group_id=GROUP, enabled=True
        )
        accepted = consumer.poll_once(poll_timeout=0)
        self.assertEqual(accepted[0].kind, InputKind.ACKNOWLEDGEMENT)
        self.assertEqual(accepted[0].subject_id, "inc-1")
        self.assertEqual([event[0] for event in backend.events], ["load", "store", "advance"])
        self.assertEqual(backend.cursor, 11)

        failed_backend = Backend()
        failed_backend.fail_store = True
        failed = TelegramUpdateConsumer(
            client, failed_backend, group_id=GROUP, enabled=True
        )
        with self.assertRaises(OSError):
            failed.poll_once(poll_timeout=0)
        self.assertEqual(failed_backend.cursor, 0)
        self.assertNotIn("advance", [event[0] for event in failed_backend.events])

    def test_mixed_chat_bots_anonymous_and_left_members_are_rejected(self) -> None:
        updates = [
            message_update(1, chat_id=-99),
            message_update(2, is_bot=True),
            message_update(3, sender_chat=True),
            message_update(4, user_id=44),
            message_update(5, "/approve proposal nonce12345", user_id=45),
        ]
        client = Client(updates, memberships={44: member(44, "left")})
        backend = Backend()
        accepted = TelegramUpdateConsumer(
            client, backend, group_id=GROUP, enabled=True
        ).poll_once(poll_timeout=0)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].sender_id, 45)
        self.assertEqual(accepted[0].kind, InputKind.APPROVAL_COMMAND)
        self.assertEqual(backend.cursor, 6)
        self.assertEqual(client.member_calls, [(GROUP, 44), (GROUP, 45)])

    def test_migration_requires_explicit_configuration(self) -> None:
        old_group = -123
        update = message_update(7, "/ack inc", chat_id=old_group)
        no_migration = Backend()
        self.assertEqual(
            TelegramUpdateConsumer(
                Client([update]), no_migration, group_id=GROUP, enabled=True
            ).poll_once(poll_timeout=0),
            (),
        )
        configured = Backend()
        accepted = TelegramUpdateConsumer(
            Client([update]),
            configured,
            group_id=GROUP,
            enabled=True,
            migrations={old_group: GROUP},
        ).poll_once(poll_timeout=0)
        self.assertEqual(accepted[0].group_id, GROUP)

    def test_callback_and_message_ids_are_normalized(self) -> None:
        update = message_update(9)
        message = update.pop("message")
        update["callback_query"] = {
            "id": "callback-9",
            "from": {"id": "42", "is_bot": False},
            "message": message,
            "data": "approve:proposal-9:nonce12345",
        }
        accepted = TelegramUpdateConsumer(
            Client([update]), Backend(), group_id=str(GROUP), enabled=True
        ).poll_once(poll_timeout=0)[0]
        self.assertEqual(accepted.callback_id, "callback-9")
        self.assertEqual(accepted.message_id, 109)
        self.assertEqual(accepted.sender_id, 42)
        self.assertEqual(accepted.kind, InputKind.APPROVAL_COMMAND)

    def test_membership_outage_retains_cursor_for_retry(self) -> None:
        backend = Backend()
        client = Client(
            [message_update(12)],
            memberships={42: TelegramTransportError("membership-failed")},
        )
        consumer = TelegramUpdateConsumer(
            client, backend, group_id=GROUP, enabled=True
        )
        with self.assertRaises(AuthenticationUnavailable):
            consumer.poll_once(poll_timeout=0)
        self.assertEqual(backend.cursor, 0)

    def test_member_statuses_and_human_identity_are_fail_closed(self) -> None:
        for status in ("creator", "administrator", "member"):
            self.assertTrue(current_human_member(member(42, status), 42))
        self.assertTrue(current_human_member(member(42, "restricted", is_member=True), 42))
        self.assertFalse(current_human_member(member(42, "restricted", is_member=False), 42))
        self.assertFalse(current_human_member(member(42, "left"), 42))
        self.assertFalse(current_human_member(member(42, "kicked"), 42))
        bot = member(42)
        bot["user"]["is_bot"] = True
        self.assertFalse(current_human_member(bot, 42))
        self.assertFalse(current_human_member(member(99), 42))

    def test_unknown_questions_acks_and_approvals_are_distinct(self) -> None:
        self.assertEqual(parse_operator_input("why?")[0], InputKind.UNKNOWN_QUESTION)
        self.assertEqual(
            parse_operator_input("/ack inc-2"),
            (InputKind.ACKNOWLEDGEMENT, "inc-2", None),
        )
        self.assertEqual(
            parse_operator_input("/approve proposal-2 nonce12345"),
            (InputKind.APPROVAL_COMMAND, "proposal-2", "nonce12345"),
        )
        self.assertEqual(
            parse_operator_input("/approve@TerraComputeBot proposal-2 nonce12345"),
            (InputKind.APPROVAL_COMMAND, "proposal-2", "nonce12345"),
        )
        self.assertEqual(
            parse_operator_input("/approve@foreignbot proposal-2 nonce12345"),
            (InputKind.UNKNOWN_QUESTION, None, None),
        )
        self.assertEqual(
            parse_operator_input("/ack@foreignbot inc-2"),
            (InputKind.UNKNOWN_QUESTION, None, None),
        )


if __name__ == "__main__":
    unittest.main()
