"""The display snapshot for Terra, the console on the host's monitor.

Two small jobs, run by two units under different users:

``agent-status`` runs as the actions user with no credentials and no network. It reads
the actions database and writes a few allowlisted fields: whether the agent is
investigating, its round, whether a plan waits and until when, Astra's latest verdict
as one word, and recent agent events as fixed phrases. No model text, command,
Telegram message or question leaves it.

``publish`` runs as the display user in the state group. It reads the collector's
latest artifacts read-only, the agent status file, the backup unit's last run and,
if configured, Vast earnings. It builds snapshot v1 (the contract lives in
terracompute-terra, schema/snapshot.v1.schema.json) and pushes it to the host over
SSH, where a forced command validates and stores it.

Nothing here can change the controller, the host or a rental.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .http_client import HttpClientError, HttpRequest, HttpTransport, RestrictedHttpClient, StdlibTransport
from .state import CURRENT_SCHEMA_VERSION

SCHEMA = "terracompute.display/1"
AGENT_SCHEMA = "terracompute.display-agent/1"
MACHINE_ID = "17049"
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_EVENTS = 40
GENERIC_APPROVAL_LIFETIME = timedelta(minutes=30)
MAX_CHAT_ROUNDS = 10
MAX_OBSERVE_ROUNDS = 6
SSH_STALE_AFTER = timedelta(minutes=15)
EVENT_WINDOW = timedelta(hours=24)
VAST_ORIGIN = "https://console.vast.ai"
VAST_MACHINES_PATH = "/api/v0/machines/"
VAST_EARNINGS_PATH = "/api/v0/users/me/machine-earnings/"
BACKUP_UNIT = "terracompute-backup.service"
BUSY_PSTATES = frozenset({"P0", "P1", "P2", "P3"})
_UNIX_TIMESTAMP = re.compile(r"^@([0-9]{1,12})$")
_BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
_PSU = re.compile(r"^[A-Z0-9]{1,2}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Astra's verdict, read from the fixed opening of the bot's own messages. Only the
# word on the right ever leaves this module.
ASTRA_PHRASES = (
    ("Astra approves", "approve"),
    ("Astra still has concerns", "concerns"),
    ("Astra asked for changes", "revise"),
    ("Astra gave no verdict", "no verdict"),
    ("reviewer (Astra) is checking", "reviewing"),
)
PLAN_RESULTS = {
    "succeeded": ("plan_answered", "approved plan ran"),
    "failed": ("plan_answered", "approved plan failed"),
    "expired": ("plan_expired", "plan expired unapproved"),
    "refused_by_operator": ("plan_answered", "plan left alone"),
    "withdrawn_by_operator": ("plan_answered", "plan withdrawn"),
    "superseded": ("plan_answered", "plan replaced by a newer one"),
}


class DisplayError(RuntimeError):
    """A fixed, secret-free failure."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: object, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < lo or value > hi:
        return None
    return float(value)


def _connect_ro(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise DisplayError(f"database unavailable: {path.name}")
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def write_atomic(path: Path, data: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- agent status


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def agent_status(db: sqlite3.Connection, now: datetime) -> dict:
    """Project the actions database onto the display's allowlisted agent fields."""
    state, round_, max_rounds, activity, expires = "idle", None, None, "", None

    for proposal, has_command, created in _rows(
        db,
        """SELECT proposal_id, command IS NOT NULL, created_utc FROM tc_action_cycles
            WHERE stage='awaiting_answer' ORDER BY created_utc DESC, rowid DESC LIMIT 1""",
    ):
        created_at = _parse_time(created)
        if has_command and created_at is not None:
            deadline = created_at + GENERIC_APPROVAL_LIFETIME
            if deadline > now:
                state, expires, activity = "awaiting_approval", deadline, "waiting for approval"
        elif not has_command:
            # The handover restart has no fixed deadline; it is withdrawn when it stops applying.
            state, activity = "awaiting_approval", "waiting for approval"
        del proposal

    if state == "idle":
        reviewing = _rows(db, "SELECT 1 FROM tc_action_notes WHERE name LIKE 'review:%' LIMIT 1")
        loops = _rows(
            db,
            """SELECT rounds FROM tc_action_observe_loops WHERE state IN ('open','final')
                ORDER BY updated_utc DESC LIMIT 1""",
        )
        pending = {name.split(":", 1)[0] for (name,) in _rows(
            db,
            """SELECT name FROM tc_action_schedule
                WHERE name LIKE 'diagnosis:%' OR name LIKE 'conversation:%'""",
        )}
        chat = _rows(db, "SELECT round FROM tc_action_conversations ORDER BY updated_utc DESC LIMIT 1")
        if reviewing:
            state, activity = "investigating", "Astra is reviewing a plan"
        elif "conversation" in pending and chat:
            state, activity = "investigating", "answering a question"
            round_, max_rounds = int(chat[0][0] or 0) or 1, MAX_CHAT_ROUNDS
        elif loops:
            state, activity = "investigating", "reading the machine"
            round_, max_rounds = int(loops[0][0] or 0) or 1, MAX_OBSERVE_ROUNDS
        elif "diagnosis" in pending:
            state, activity = "investigating", "diagnosing a fault"

    astra, astra_at = "", None
    events: list[dict] = []
    for sent, text in _rows(
        db,
        """SELECT sent_utc, text FROM tc_action_outbox
            WHERE text LIKE '%Astra%' ORDER BY id DESC LIMIT 40""",
    ):
        when = _parse_time(sent)
        if when is None or not isinstance(text, str):
            continue
        verdict = next((word for phrase, word in ASTRA_PHRASES if phrase in text[:400]), None)
        if verdict is None:
            continue
        if astra_at is None:
            astra, astra_at = verdict, when
        if now - when <= EVENT_WINDOW:
            events.append({"at": _iso(when), "kind": "astra", "text": f"Astra: {verdict}"})
    if astra_at is None or now - astra_at > timedelta(hours=2):
        astra = ""

    for created, finished, result, has_command in _rows(
        db,
        """SELECT created_utc, finished_utc, result, command IS NOT NULL FROM tc_action_cycles
            ORDER BY created_utc DESC LIMIT 20""",
    ):
        created_at, finished_at = _parse_time(created), _parse_time(finished)
        what = "plan" if has_command else "restart request"
        if created_at is not None and now - created_at <= EVENT_WINDOW:
            text = "plan sent for approval · 30 min" if has_command else "restart request sent for approval"
            events.append({"at": _iso(created_at), "kind": "plan_sent", "text": text})
        if finished_at is not None and now - finished_at <= EVENT_WINDOW and result in PLAN_RESULTS:
            kind, text = PLAN_RESULTS[result]
            events.append({"at": _iso(finished_at), "kind": kind,
                           "text": text if has_command else text.replace("plan", what, 1)})

    for started, rounds in _rows(
        db,
        "SELECT started_utc, rounds FROM tc_action_observe_loops ORDER BY started_utc DESC LIMIT 10",
    ):
        when = _parse_time(started)
        if when is not None and now - when <= EVENT_WINDOW:
            events.append({"at": _iso(when), "kind": "agent_round", "text": "agent: looking into a fault"})
        del rounds
    for created, in _rows(db, "SELECT created_utc FROM tc_action_conversations ORDER BY created_utc DESC LIMIT 10"):
        when = _parse_time(created)
        if when is not None and now - when <= EVENT_WINDOW:
            events.append({"at": _iso(when), "kind": "agent_round", "text": "agent: a question from Edgar"})

    events.sort(key=lambda e: e["at"])
    return {
        "schema": AGENT_SCHEMA,
        "generated_at": _iso(now),
        "state": state,
        "round": round_,
        "max_rounds": max_rounds,
        "activity": activity,
        "astra": astra,
        "approval_expires_at": _iso(expires) if expires else None,
        "events": events[-20:],
    }


# ------------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Card:
    index: int
    pci_bdf: str
    psu: str
    position: int


@dataclass(frozen=True)
class Config:
    state_dir: Path
    actions_db: Path
    agent_status: Path
    work_dir: Path
    display_tz: str
    cards: tuple[Card, ...]
    psu_capacity_w: dict
    push_target: str | None = None
    identity_file: Path | None = None
    known_hosts_file: Path | None = None
    vast_api_key_file: Path | None = None
    vast_every_seconds: int = 300
    systemctl: str = "/run/current-system/sw/bin/systemctl"
    ssh: str = "ssh"


def _cards(raw: object) -> tuple[Card, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise DisplayError("cards must list 1 to 8 GPUs")
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            raise DisplayError("each card must be an object")
        index, bdf, psu = item.get("index"), item.get("pci_bdf"), item.get("psu")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 15:
            raise DisplayError("card index must be 0-15")
        if not isinstance(bdf, str) or not _BDF.fullmatch(bdf.lower()):
            raise DisplayError("card pci_bdf must look like 0000:01:00.0")
        if not isinstance(psu, str) or not _PSU.fullmatch(psu):
            raise DisplayError("card psu must be one or two capitals or digits")
        parsed.append((index, bdf.lower(), psu, item.get("position")))
    if len({p[0] for p in parsed}) != len(parsed) or len({p[1] for p in parsed}) != len(parsed):
        raise DisplayError("card index and pci_bdf must be unique")
    # Without a known left-to-right order, cards sit in pairs by PSU so each boiler on the
    # screen feeds the two cards that PSU really powers.
    ordered = sorted(parsed, key=lambda p: (p[3] if isinstance(p[3], int) else 99, p[2], p[0]))
    return tuple(Card(index, bdf, psu, position) for position, (index, bdf, psu, _) in enumerate(ordered))


def load_config(path: Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    push = raw.get("push") or {}
    vast = raw.get("vast") or {}
    capacity = {k: float(v) for k, v in (raw.get("psu_capacity_w") or {}).items()
                if isinstance(k, str) and _PSU.fullmatch(k) and _number(v, 1, 5000) is not None}
    return Config(
        state_dir=Path(raw["state_dir"]),
        actions_db=Path(raw.get("actions_db", "/var/lib/terracompute-actions/actions.sqlite3")),
        agent_status=Path(raw.get("agent_status", "/run/terracompute-display/agent.json")),
        work_dir=Path(raw["work_dir"]),
        display_tz=str(raw.get("display_tz", "UTC"))[:64],
        cards=_cards(raw.get("cards")),
        psu_capacity_w=capacity,
        push_target=push.get("target"),
        identity_file=Path(push["identity_file"]) if push.get("identity_file") else None,
        known_hosts_file=Path(push["known_hosts_file"]) if push.get("known_hosts_file") else None,
        vast_api_key_file=Path(vast["api_key_file"]) if vast.get("api_key_file") else None,
        vast_every_seconds=int(vast.get("every_seconds", 300)),
        systemctl=str(raw.get("systemctl", "/run/current-system/sw/bin/systemctl")),
        ssh=str(raw.get("ssh", "ssh")),
    )


# ------------------------------------------------------------------------------- inputs


@dataclass
class Inputs:
    """Everything the snapshot is built from, already read."""

    now: datetime
    ssh: dict | None = None
    ssh_at: datetime | None = None
    prometheus: dict | None = None
    bmc: dict | None = None
    vast: dict | None = None
    incidents: list[dict] = field(default_factory=list)
    incident_events: list[dict] = field(default_factory=list)
    last_incident_at: datetime | None = None
    boots: list[tuple[str, datetime]] = field(default_factory=list)  # (boot_id, first seen)
    agent: dict | None = None
    backup: dict | None = None
    money: dict | None = None


def _latest_artifact(db: sqlite3.Connection, source: str) -> tuple[dict | None, datetime | None]:
    row = db.execute(
        """SELECT observed_utc, document_json FROM terracompute_observation_artifacts
            WHERE machine_id=? AND source=? ORDER BY artifact_id DESC LIMIT 1""",
        (MACHINE_ID, source),
    ).fetchone()
    if row is None:
        return None, None
    try:
        raw = row[1]
        document = json.loads(bytes(raw) if isinstance(raw, (bytes, memoryview)) else raw)
    except (TypeError, ValueError):
        return None, None
    return (document if isinstance(document, dict) else None), _parse_time(row[0])


def _incident_label(state_dir: Path, bundle: str | None) -> str | None:
    if not bundle or not re.fullmatch(r"[0-9A-Za-z_]{1,80}", bundle):
        return None
    try:
        label = json.loads((state_dir / "incidents" / bundle / "incident.json").read_text())["classification"]["label"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(label, str) and _CODE.fullmatch(label) and label != "unclassified":
        return label
    return None


def _incident_name(state_dir: Path, bundle: str | None, code: str | None, family: str) -> str:
    """A short name: the event code, an Xid or AER spelled out, or the classifier label."""
    code = code if code and _CODE.fullmatch(code) else None
    label = _incident_label(state_dir, bundle)
    if family == "xid" and code and code.isdigit():
        named = f"Xid {code} {label}" if label else f"Xid {code}"
        return named if len(named) <= 32 else f"Xid {code}"
    if family == "aer" and code:
        return f"AER {code}"[:32]
    if code and len(code) <= 32:
        return code
    if label and len(label) <= 32:
        return label
    name = code or label or family or "incident"
    return name if len(name) <= 32 else name[:31] + "…"


def read_state(state_dir: Path, now: datetime) -> Inputs:
    """One consistent read-only pass over the collector's state database."""
    db = _connect_ro(state_dir / "state.sqlite3")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise DisplayError("state schema is not the one this builder reads")
        db.execute("BEGIN")
        inputs = Inputs(now=now)
        inputs.ssh, inputs.ssh_at = _latest_artifact(db, "ssh")
        inputs.prometheus, _ = _latest_artifact(db, "prometheus")
        inputs.bmc, _ = _latest_artifact(db, "bmc")
        inputs.vast, _ = _latest_artifact(db, "vast")
        for key, bundle, family, severity, first, recovered, evidence in _rows(
            db,
            """SELECT i.dedup_key, i.bundle_name, i.fault_family, i.severity, i.first_occurrence_utc,
                      i.recovered_utc, o.evidence_json
                 FROM incidents i LEFT JOIN observations o ON o.id=(
                      SELECT MAX(r.id) FROM observations r WHERE r.incident_key=i.dedup_key)
                WHERE i.status IN ('open','recovery_pending')
                   OR i.first_occurrence_utc >= ? OR i.recovered_utc >= ?
             ORDER BY i.first_occurrence_utc DESC LIMIT 32""",
            (_iso(now - EVENT_WINDOW), _iso(now - EVENT_WINDOW)),
        ):
            try:
                event = json.loads(bytes(evidence) if isinstance(evidence, (bytes, memoryview)) else evidence or "{}")
            except (TypeError, ValueError):
                event = {}
            event = event if isinstance(event, dict) else {}
            detail = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
            name = _incident_name(state_dir, bundle, event.get("code") if isinstance(event.get("code"), str) else None,
                                  str(family or ""))
            opened, closed = _parse_time(first), _parse_time(recovered)
            item = {"key": key, "title": name, "severity": severity, "opened_at": opened, "recovered_at": closed,
                    "bdf": str(detail.get("pci_bdf", "")).lower() or None,
                    "uuid": str(detail.get("uuid", "")) or None}
            inputs.incidents.append(item)
            if opened is not None and now - opened <= EVENT_WINDOW:
                inputs.incident_events.append({"at": _iso(opened), "kind": "incident_opened", "text": f"incident: {name}"})
            if closed is not None and now - closed <= EVENT_WINDOW:
                inputs.incident_events.append({"at": _iso(closed), "kind": "incident_closed", "text": f"resolved: {name}"})
        last = db.execute("SELECT MAX(first_occurrence_utc) FROM incidents").fetchone()
        inputs.last_incident_at = _parse_time(last[0]) if last else None
        db.execute("COMMIT")
        return inputs
    finally:
        db.close()


def read_boots(state_dir: Path) -> list[tuple[str, datetime]]:
    """Every host boot the collector has seen, oldest first. A full scan, so it runs rarely."""
    db = _connect_ro(state_dir / "state.sqlite3")
    try:
        boots = []
        for boot_id, first in _rows(
            db,
            """SELECT boot_id, MIN(source_utc) FROM observations
                WHERE source='ssh' AND boot_id NOT IN ('unknown','') GROUP BY boot_id""",
        ):
            when = _parse_time(first)
            if when is not None and isinstance(boot_id, str) and len(boot_id) <= 64:
                boots.append((boot_id, when))
        return sorted(boots, key=lambda b: b[1])
    finally:
        db.close()


def read_backup(systemctl: str, runner: Callable = subprocess.run) -> dict | None:
    try:
        result = runner(
            [systemctl, "show", BACKUP_UNIT, "--property=Result,ExecMainStatus,ExecMainExitTimestamp",
             "--timestamp=unix"],
            capture_output=True, text=True, timeout=10, check=False,
            env={"PATH": "/run/current-system/sw/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 4096:
        return None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    finish = _UNIX_TIMESTAMP.fullmatch(values.get("ExecMainExitTimestamp", ""))
    if finish is None or int(finish.group(1)) == 0:
        return None
    ok = values.get("Result") == "success" and values.get("ExecMainStatus") == "0"
    return {"at": datetime.fromtimestamp(int(finish.group(1)), timezone.utc), "ok": ok}


def read_agent(path: Path, now: datetime) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != AGENT_SCHEMA:
        return None
    generated = _parse_time(raw.get("generated_at"))
    if generated is None or now - generated > timedelta(minutes=5):
        return None  # a stale status must not keep showing an old plan
    return raw


# --------------------------------------------------------------------------------- money


def _earnings_total(payload: object) -> float | None:
    """Sum the earnings figures in a machine-earnings response, touching nothing else."""
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if isinstance(summary, dict):
        figures = [_number(v, 0, 10**7) for k, v in summary.items() if isinstance(k, str) and k.startswith("total_")]
        figures = [f for f in figures if f is not None]
        if figures:
            return round(sum(figures), 2)
    machines = payload.get("per_machine")
    if isinstance(machines, list):
        for item in machines:
            if isinstance(item, dict) and str(item.get("machine_id")) == MACHINE_ID:
                figures = [_number(v, 0, 10**7) for k, v in item.items() if isinstance(k, str) and k.endswith("_earn")]
                figures = [f for f in figures if f is not None]
                if figures:
                    return round(sum(figures), 2)
    return None


def read_money(api_key: str, now: datetime, tz, transport: HttpTransport | None = None) -> dict:
    """Reliability, prices and earnings from Vast. Each piece is optional."""
    money: dict = {}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        client = RestrictedHttpClient(VAST_ORIGIN, {VAST_MACHINES_PATH: frozenset({"GET"})},
                                      transport=transport, timeout_seconds=15, max_response_bytes=256 * 1024)
        payload = client.request_json("GET", VAST_MACHINES_PATH, headers=headers)
        machines = payload.get("machines") if isinstance(payload, dict) else None
        for item in machines if isinstance(machines, list) else []:
            if isinstance(item, dict) and str(item.get("id", item.get("machine_id"))) == MACHINE_ID:
                money["reliability"] = _number(item.get("reliability2"), 0, 1)
                money["on_demand_price"] = _number(item.get("listed_gpu_cost"), 0, 100)
                money["bid_price"] = _number(item.get("min_bid_price"), 0, 100)
    except (HttpClientError, ValueError):
        pass
    local = now.astimezone(tz)
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    # The shared client allows exact routes without queries, so this one request goes
    # straight to the same bounded, no-redirect transport. Its query is built from
    # numbers only, against a fixed origin and path.
    raw_transport = transport or StdlibTransport()
    for key, start in (("today", day0), ("month", month0)):
        sday = start.timestamp() / 86400
        eday = now.timestamp() / 86400
        url = (f"{VAST_ORIGIN}{VAST_EARNINGS_PATH}?owner=me&sday={sday:.6f}&eday={eday:.6f}"
               f"&machid={int(MACHINE_ID)}")
        try:
            response = raw_transport.request(HttpRequest("GET", url, headers), timeout_seconds=15,
                                             max_response_bytes=512 * 1024)
            payload = json.loads(response.body) if response.status == 200 else None
            money[key] = _earnings_total(payload)
        except (HttpClientError, ValueError, OSError):
            money[key] = None
    money["fetched_at"] = _iso(now)
    return money


# ------------------------------------------------------------------------------- building


def _metric(samples: object, name: str) -> list[tuple[dict, float]]:
    out = []
    for sample in samples if isinstance(samples, list) else []:
        if not isinstance(sample, dict):
            continue
        labels = sample.get("labels")
        value = _number(sample.get("value"), -1e9, 1e9)
        if isinstance(labels, dict) and labels.get("__name__") == name and value is not None:
            out.append((labels, value))
    return out


def _gpu_readings(inputs: Inputs) -> tuple[dict, dict, dict, dict]:
    """Per-BDF driver and probe readings, per-index Vast occupancy and DCGM utilization."""
    drivers, probe = {}, {}
    gpu = (inputs.ssh or {}).get("snapshot", {}).get("gpu", {}) if isinstance(inputs.ssh, dict) else {}
    fresh = inputs.ssh_at is not None and inputs.now - inputs.ssh_at <= SSH_STALE_AFTER
    if fresh and isinstance(gpu, dict):
        for device in gpu.get("pci_devices") or []:
            if isinstance(device, dict) and isinstance(device.get("pci_bdf"), str):
                drivers[device["pci_bdf"].lower()] = str(device.get("driver") or "")
        for card in gpu.get("gpus") or []:
            if isinstance(card, dict) and isinstance(card.get("pci_bdf"), str):
                probe[card["pci_bdf"].lower()] = card
    metrics = ((inputs.prometheus or {}).get("snapshot") or {}).get("metrics") or {}
    occupancy = {}
    for labels, value in _metric(metrics.get("vast"), "vastai_machine_gpu_occupancy"):
        if str(labels.get("gpu", "")).isdigit():
            occupancy[int(labels["gpu"])] = value
    util = {}
    for labels, value in _metric(metrics.get("dcgm"), "DCGM_FI_DEV_GPU_UTIL"):
        if str(labels.get("gpu", "")).isdigit():
            util[int(labels["gpu"])] = value
    return drivers, probe, occupancy, util


def _rented_counts(inputs: Inputs) -> tuple[int, int]:
    metrics = ((inputs.prometheus or {}).get("snapshot") or {}).get("metrics") or {}
    on_demand = sum(v for _, v in _metric(metrics.get("vast"), "vastai_machine_gpu_rented_on_demand"))
    bid = sum(v for _, v in _metric(metrics.get("vast"), "vastai_machine_gpu_rented_bid_demand"))
    return int(on_demand), int(bid)


def _listed(inputs: Inputs) -> bool | None:
    machine = ((inputs.vast or {}).get("snapshot") or {}).get("machine")
    if isinstance(machine, dict) and isinstance(machine.get("listed"), bool):
        return machine["listed"]
    return None


def _bmc_fans(inputs: Inputs) -> float | None:
    readings = []
    for resource in ((inputs.bmc or {}).get("snapshot") or {}).get("resources") or []:
        for item in (resource.get("thermal") or []) if isinstance(resource, dict) else []:
            if isinstance(item, dict) and item.get("kind") == "fan" and item.get("state") == "Enabled":
                # Readings carry every field; an unused one is present and null.
                raw = item.get("reading_rpm") if item.get("reading_rpm") is not None else item.get("reading")
                rpm = _number(raw, 1, 50000)
                if rpm is not None:
                    readings.append(rpm)
    return round(sum(readings) / len(readings)) if readings else None


def _bmc_inlet(inputs: Inputs) -> float | None:
    for resource in ((inputs.bmc or {}).get("snapshot") or {}).get("resources") or []:
        for item in (resource.get("thermal") or []) if isinstance(resource, dict) else []:
            if (isinstance(item, dict) and item.get("kind") == "temperature"
                    and "INLET" in str(item.get("name", "")).upper()):
                value = _number(item.get("reading_celsius"), -20, 80)
                if value is not None:
                    return value
    return None


def _hour_key(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def build(config: Config, inputs: Inputs, memory: dict) -> dict:
    """The snapshot, and an updated memory. Pure apart from reading `memory`."""
    now = inputs.now
    drivers, probe, occupancy, util = _gpu_readings(inputs)
    have_probe = bool(drivers)
    listed = _listed(inputs)
    previous = memory.setdefault("gpu_states", {})
    hours = memory.setdefault("hours", {})
    events: list[dict] = [e for e in memory.get("events", []) if isinstance(e, dict)]

    def remember(kind: str, text: str, gpu: int | None = None, code: int | None = None, at: datetime | None = None):
        event = {"at": _iso(at or now), "kind": kind, "text": text[:56]}
        if gpu is not None:
            event["gpu"] = gpu
        if code is not None:
            event["code"] = code
        events.append(event)

    gpus = []
    uuid_to_index = {}
    for card in config.cards:
        present = card.pci_bdf in drivers if have_probe else previous.get(str(card.index)) != "missing"
        driver = drivers.get(card.pci_bdf, "")
        reading = probe.get(card.pci_bdf, {})
        # On PCI but lost by the driver (an Xid 79, say) is as gone as a card can be.
        # Only when nvidia-smi answered at all, so one failed probe cannot empty the hall.
        if present and probe and driver != "vfio-pci" and card.pci_bdf not in probe:
            present = False
        if isinstance(reading.get("uuid"), str):
            uuid_to_index[reading["uuid"]] = card.index
        if not present:
            state = "missing"
        elif driver == "vfio-pci":
            state = "vm"
        elif occupancy.get(card.index, 0) > 0:
            state = "rented"
        elif listed is False:
            state = "unlisted"
        else:
            state = "idle"
        before = previous.get(str(card.index))
        if before is not None and before != state:
            renting = ("rented", "vm")
            if state in renting and before not in renting:
                remember("rental_started", f"GPU {card.index} rented" + (" as a VM" if state == "vm" else ""), card.index)
            elif before in renting and state not in renting and state != "missing":
                remember("rental_ended", f"GPU {card.index} rental ended", card.index)
            if state == "missing":
                remember("gpu_missing", f"GPU {card.index} dropped off PCI", card.index)
            elif before == "missing":
                remember("gpu_returned", f"GPU {card.index} is back on PCI", card.index)
        previous[str(card.index)] = state

        buckets = hours.setdefault(str(card.index), {})
        bucket = buckets.setdefault(_hour_key(now), {})
        bucket[state] = bucket.get(state, 0) + 1
        past = []
        for back in range(24, 0, -1):
            counts = buckets.get(_hour_key(now - timedelta(hours=back)))
            past.append(max(counts, key=counts.get) if counts else "unknown")
        for key in [k for k in buckets if k < _hour_key(now - timedelta(hours=25))]:
            del buckets[key]

        pstate = reading.get("pstate")
        entry = {
            "index": card.index,
            "position": card.position,
            "psu": card.psu,
            "state": state,
            "util_pct": _number(util.get(card.index), 0, 100),
            "temp_c": _number(reading.get("temperature_c"), -20, 130),
            "power_w": None,
            "active": (pstate in BUSY_PSTATES) if isinstance(pstate, str) else None,
            "hours": past[-23:],
        }
        gpus.append(entry)

    # Reboots and Xid errors, from the probe.
    boots = memory.setdefault("boots", {})
    boot_id = (inputs.ssh or {}).get("boot_id") if isinstance(inputs.ssh, dict) else None
    if isinstance(boot_id, str) and boot_id not in ("", "unknown") and len(boot_id) <= 64:
        if boots.get("current") != boot_id:
            if boots.get("current") is not None:
                boots["number"] = int(boots.get("number", 0)) + 1
                boots["booted_at"] = _iso(inputs.ssh_at or now)
                remember("reboot", f"host booted (#{boots['number']})", at=inputs.ssh_at or now)
            else:
                history = inputs.boots or [(boot_id, inputs.ssh_at or now)]
                boots["number"] = len(history)
                boots["booted_at"] = _iso(next((t for b, t in history if b == boot_id), inputs.ssh_at or now))
            boots["current"] = boot_id
    seen_xid = set(memory.get("xid_seen", []))
    for event in (inputs.ssh or {}).get("events") or [] if isinstance(inputs.ssh, dict) else []:
        if not isinstance(event, dict) or event.get("fault_family") != "xid":
            continue
        code = str(event.get("code", ""))
        evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
        bdf = str(evidence.get("pci_bdf", "")).lower()
        key = f"{boot_id}|{code}|{bdf}|{event.get('count')}"
        if key in seen_xid or not code.isdigit():
            continue
        seen_xid.add(key)
        index = next((c.index for c in config.cards if c.pci_bdf == bdf), None)
        where = f" on GPU {index}" if index is not None else ""
        remember("xid", f"Xid {code}{where}", index, int(code) if int(code) <= 999 else None)
    memory["xid_seen"] = sorted(seen_xid)[-200:]

    backup = inputs.backup
    if backup is not None:
        stamp = _iso(backup["at"])
        if memory.get("backup_seen") not in (None, stamp):
            if backup["ok"]:
                remember("backup_shipped", "backup shipped to Minas", at=backup["at"])
            else:
                remember("backup_failed", "backup to Minas failed", at=backup["at"])
        memory["backup_seen"] = stamp

    # Events with their own timestamps come fresh from their sources each time.
    events = [e for e in events if (_parse_time(e.get("at")) or now) >= now - EVENT_WINDOW][-MAX_EVENTS:]
    memory["events"] = events
    merged = {(e["at"], e["kind"], e["text"]): e for e in events + inputs.incident_events
              + list((inputs.agent or {}).get("events", []))}
    all_events = sorted(merged.values(), key=lambda e: e["at"])[-MAX_EVENTS:]

    incidents = []
    for item in inputs.incidents:
        if item["recovered_at"] is not None:
            continue
        index = next((c.index for c in config.cards if item["bdf"] and c.pci_bdf == item["bdf"]), None)
        if index is None and item["uuid"]:
            index = uuid_to_index.get(item["uuid"])
        incidents.append({"title": item["title"], "opened_at": _iso(item["opened_at"]) if item["opened_at"] else None,
                          "gpu": index})
    days = None
    if inputs.last_incident_at is not None:
        days = max(0, (now - inputs.last_incident_at).days)

    agent = inputs.agent or {}
    money = inputs.money or {}
    on_demand, bid = _rented_counts(inputs)
    per_hour = None
    if money.get("on_demand_price") is not None:
        per_hour = round(on_demand * money["on_demand_price"] + bid * (money.get("bid_price") or 0), 2)

    snapshot = {
        "schema": SCHEMA,
        "generated_at": _iso(now),
        "machine": {"id": MACHINE_ID, "display_tz": config.display_tz},
        "gpus": gpus,
        "psus": [{"id": psu, "capacity_w": config.psu_capacity_w.get(psu)}
                 for psu in sorted({c.psu for c in config.cards})],
        "power": {"wall_w": None, "inlet_c": _bmc_inlet(inputs), "fan_rpm": _bmc_fans(inputs)},
        "vast": {"listed": listed, "reliability": money.get("reliability"),
                 "earnings": {"per_hour_usd": per_hour, "today_usd": money.get("today"),
                              "month_usd": money.get("month")}},
        "incidents": {"open": incidents[:8], "days_without": days},
        "agent": {
            "state": agent.get("state", "idle") if agent.get("state") in ("idle", "investigating", "awaiting_approval") else "idle",
            "round": agent.get("round"),
            "max_rounds": agent.get("max_rounds"),
            "activity": str(agent.get("activity", ""))[:36],
            "astra": str(agent.get("astra", ""))[:28],
            "approval_expires_at": agent.get("approval_expires_at"),
        },
        "events": all_events,
    }
    if boots.get("number") is not None:
        snapshot["machine"]["boot_number"] = boots["number"]
    if boots.get("booted_at"):
        snapshot["machine"]["booted_at"] = boots["booted_at"]
    if backup is not None:
        snapshot["backup"] = {"last_at": _iso(backup["at"]), "last_ok": backup["ok"]}
    check(snapshot)
    return snapshot


def check(snapshot: dict) -> None:
    """The shape terracompute-terra's parser needs, checked before anything leaves."""
    problems = []
    if snapshot.get("schema") != SCHEMA or _parse_time(snapshot.get("generated_at")) is None:
        problems.append("envelope")
    gpus = snapshot.get("gpus")
    if not isinstance(gpus, list) or not 1 <= len(gpus) <= 8:
        problems.append("gpus")
    else:
        if len({g["position"] for g in gpus}) != len(gpus) or len({g["index"] for g in gpus}) != len(gpus):
            problems.append("gpu identity")
        for g in gpus:
            if g["state"] not in ("rented", "vm", "idle", "unlisted", "missing") or not 0 <= g["position"] <= 7:
                problems.append(f"gpu {g['index']}")
    for event in snapshot.get("events", []):
        if len(event.get("text", "")) > 56:
            problems.append("event text")
    size = len(json.dumps(snapshot, separators=(",", ":")).encode())
    if size > MAX_SNAPSHOT_BYTES:
        problems.append(f"{size} bytes")
    if problems:
        raise DisplayError("snapshot failed its own check: " + ", ".join(problems))


# --------------------------------------------------------------------------------- push


def push(config: Config, data: bytes, runner: Callable = subprocess.run) -> str:
    """Hand the snapshot to the host's forced command. Returns its one-line answer."""
    if not config.push_target or not config.identity_file or not config.known_hosts_file:
        raise DisplayError("push is not configured")
    command = [
        config.ssh, "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={config.known_hosts_file}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2", "-o", f"IdentityFile={config.identity_file}", "-T",
        config.push_target,
    ]
    try:
        result = runner(command, input=data, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise DisplayError("push timed out") from None
    except OSError:
        raise DisplayError("ssh could not start") from None
    answer = (result.stdout or b"")[:200].decode("utf-8", "replace").strip()
    if result.returncode != 0:
        detail = (result.stderr or b"")[:200].decode("utf-8", "replace").strip().splitlines()
        raise DisplayError(f"push refused: {detail[-1] if detail else f'exit {result.returncode}'}")
    return answer


# ---------------------------------------------------------------------------------- main


def _load_memory(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def publish(config: Config, now: datetime | None = None, *, dry_run: bool = False,
            runner: Callable = subprocess.run, transport: HttpTransport | None = None) -> dict:
    now = now or _utc_now()
    memory_path = config.work_dir / "memory.json"
    memory = _load_memory(memory_path)
    inputs = read_state(config.state_dir, now)
    boot_id = (inputs.ssh or {}).get("boot_id") if isinstance(inputs.ssh, dict) else None
    if boot_id and memory.get("boots", {}).get("current") is None:
        inputs.boots = read_boots(config.state_dir)
    inputs.agent = read_agent(config.agent_status, now)
    inputs.backup = read_backup(config.systemctl, runner)
    money = memory.get("money") or {}
    fetched = _parse_time(money.get("fetched_at"))
    if config.vast_api_key_file is not None and (fetched is None or (now - fetched).total_seconds() >= config.vast_every_seconds):
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(config.display_tz)
        except Exception:  # noqa: BLE001 - an unknown zone just means UTC days
            tz = timezone.utc
        key = config.vast_api_key_file.read_text(encoding="utf-8").strip()
        money = read_money(key, now, tz, transport)
        del key
        memory["money"] = money
    inputs.money = money
    snapshot = build(config, inputs, memory)
    data = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    write_atomic(config.work_dir / "snapshot.json", data)
    if not dry_run:
        push(config, data, runner)
    write_atomic(memory_path, json.dumps(memory, separators=(",", ":")).encode("utf-8"), 0o600)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terracompute-display", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    agent = sub.add_parser("agent-status", help="write the allowlisted agent status (runs as the actions user)")
    agent.add_argument("--actions-db", required=True, type=Path)
    agent.add_argument("--out", required=True, type=Path)
    pub = sub.add_parser("publish", help="build the snapshot and push it to the host")
    pub.add_argument("--config", required=True, type=Path)
    pub.add_argument("--dry-run", action="store_true", help="build and save the snapshot, but do not push")
    pub.add_argument("--ssh-executable", default=None)
    pub.add_argument("--systemctl-executable", default=None)
    args = parser.parse_args(argv)
    now = _utc_now()
    started = time.monotonic()
    try:
        if args.command == "agent-status":
            db = _connect_ro(args.actions_db)
            try:
                status = agent_status(db, now)
            finally:
                db.close()
            write_atomic(args.out, json.dumps(status, separators=(",", ":")).encode("utf-8"))
            print(f"agent status: {status['state']}")
        else:
            config = load_config(args.config)
            overrides = {}
            if args.ssh_executable:
                overrides["ssh"] = args.ssh_executable
            if args.systemctl_executable:
                overrides["systemctl"] = args.systemctl_executable
            if overrides:
                from dataclasses import replace

                config = replace(config, **overrides)
            snapshot = publish(config, now, dry_run=args.dry_run)
            states = {}
            for gpu in snapshot["gpus"]:
                states[gpu["state"]] = states.get(gpu["state"], 0) + 1
            summary = ", ".join(f"{n} {s}" for s, n in sorted(states.items()))
            action = "built" if args.dry_run else "pushed"
            print(f"display snapshot {action} in {time.monotonic() - started:.1f}s: {summary}")
    except (DisplayError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"terracompute-display: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
