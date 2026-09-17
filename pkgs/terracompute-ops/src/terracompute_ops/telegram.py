"""Outbound-only Telegram delivery for the durable notification outbox."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Callable

from .state import StateStore


def read_credential(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or len(value) > 4096:
        raise ValueError("credential file is empty or unexpectedly large")
    return value


def send_message(token: str, chat_id: str, message: str, timeout: int = 15) -> None:
    payload = json.dumps(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Ignore ambient proxy variables: the bot token is part of Telegram's URL and
    # must not be disclosed to a machine-global proxy by configuration drift.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError("Telegram returned a non-success status")


def drain_outbox(
    store: StateStore,
    token: str,
    chat_id: str,
    sender: Callable[[str, str, str], None] = send_message,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    for item in store.due_notifications():
        try:
            sender(token, chat_id, item["message"])
        except Exception:
            # Error details can contain request URLs (and therefore the bot token).
            # Persist only a fixed secret-free category.
            store.mark_failed(item["id"], item["attempts"], "delivery-failed")
            failed += 1
        else:
            store.mark_sent(item["id"])
            sent += 1
    return sent, failed
