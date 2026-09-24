"""Read-only Vast.ai observations for the single approved machine.

Integration contract: a scheduler constructs :class:`VastClient` with a
``machine_read`` key and optionally an offline ``HttpTransport``.  Public
methods expose only the fixed machine-list, machine-report, and offer-search
requests used during commissioning.  Although offer search is an HTTP POST, it
is semantically read-only and its body is fixed here; no Vast write operation
is represented.  Current Vast documentation categorizes offer search under
``misc`` rather than ``machine_read``, so denial is a normal partial result and
market availability remains unknown.

The returned dataclasses are deliberately independent of the repository's
state store and CLI.  Callers persist them with their own source timestamps and
must not interpret an advertisement, a currently rentable offer, or a proven
successful launch as equivalent facts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from .http_client import HttpClientError, HttpTransport, RestrictedHttpClient


TARGET_MACHINE_ID = 17049
VAST_ORIGIN = "https://console.vast.ai"
MACHINES_PATH = "/api/v0/machines/"
REPORTS_PATH = f"/api/v0/machines/{TARGET_MACHINE_ID}/reports/"
OFFERS_PATH = "/api/v0/bundles"
MAX_OFFERS = 64
MAX_MACHINES = 256
MAX_REPORTS = 100
MAX_GPU_COUNT = 32


class VastDataError(ValueError):
    """Secret-free response validation failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, repr=False)
class MachineObservation:
    machine_id: int
    observed_at: datetime
    name: str | None
    listed: bool | None
    rentable: bool | None
    rented: bool | None
    total_gpus: int | None
    rented_gpus: int | None

    def __repr__(self) -> str:
        return f"MachineObservation(machine_id={self.machine_id})"


@dataclass(frozen=True, repr=False)
class MachineReport:
    problem: str
    message: str
    created_at: str

    def __repr__(self) -> str:
        return "MachineReport(<bounded external evidence>)"


@dataclass(frozen=True)
class OfferSlice:
    offer_id: int | None
    gpu_count: int
    rentable: bool | None
    rented: bool | None


@dataclass(frozen=True)
class MarketObservation:
    machine_id: int
    observed_at: datetime
    search_complete: bool
    offers: tuple[OfferSlice, ...]
    advertised: bool | None
    rentable: bool | None
    launch_proven: bool | None
    advertised_gpu_capacity: int | None
    rentable_gpu_capacity: int | None
    launch_proven_gpu_capacity: int | None
    holds: int | None
    error: str | None


@dataclass(frozen=True, repr=False)
class VastSnapshot:
    observed_at: datetime
    machine: MachineObservation | None
    reports: tuple[MachineReport, ...] | None
    market: MarketObservation
    errors: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"VastSnapshot(machine_present={self.machine is not None}, "
            f"report_count={None if self.reports is None else len(self.reports)}, "
            f"market_complete={self.market.search_complete})"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VastClient:
    """Fixed-target observation client with no state, CLI, or action surface."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 15,
        max_response_bytes: int = 256 * 1024,
    ):
        if not isinstance(api_key, str) or not api_key or len(api_key) > 8192:
            raise VastDataError("invalid_api_key")
        self._api_key = api_key
        self._clock = clock
        self._http = RestrictedHttpClient(
            VAST_ORIGIN,
            {
                MACHINES_PATH: frozenset({"GET"}),
                REPORTS_PATH: frozenset({"GET"}),
                OFFERS_PATH: frozenset({"POST"}),
            },
            transport=transport,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_concurrency=1,
        )

    def __repr__(self) -> str:
        return f"VastClient(machine_id={TARGET_MACHINE_ID})"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def get_machine(self) -> MachineObservation:
        payload = self._http.request_json(
            "GET", MACHINES_PATH, headers=self._headers
        )
        if not isinstance(payload, dict):
            raise VastDataError("malformed_machines")
        machines = payload.get("machines")
        if not isinstance(machines, list) or len(machines) > MAX_MACHINES:
            raise VastDataError("malformed_machines")
        matches = []
        for item in machines:
            if not isinstance(item, dict):
                raise VastDataError("malformed_machines")
            machine_id = _identifier(item.get("id", item.get("machine_id")))
            if machine_id == TARGET_MACHINE_ID:
                matches.append(item)
        if len(matches) != 1:
            raise VastDataError(
                "target_not_found" if not matches else "ambiguous_target"
            )
        machine = matches[0]
        total = _optional_count(machine.get("num_gpus"))
        rented_gpus = _optional_count(
            machine.get("gpu_rented", machine.get("rented_gpus"))
        )
        if total is not None and rented_gpus is not None and rented_gpus > total:
            raise VastDataError("invalid_rental_counts")
        rented = _optional_bool(machine.get("rented"))
        if rented is None and rented_gpus is not None:
            rented = rented_gpus > 0
        name = machine.get("name")
        if name is not None and (
            not isinstance(name, str) or not name or len(name) > 256
        ):
            name = None
        return MachineObservation(
            machine_id=TARGET_MACHINE_ID,
            observed_at=_now(self._clock),
            name=name,
            listed=_optional_bool(
                machine.get("listed", machine.get("is_listed"))
            ),
            rentable=_optional_bool(machine.get("rentable")),
            rented=rented,
            total_gpus=total,
            rented_gpus=rented_gpus,
        )

    def get_reports(self) -> tuple[MachineReport, ...]:
        payload = self._http.request_json(
            "GET", REPORTS_PATH, headers=self._headers
        )
        if not isinstance(payload, list) or len(payload) > MAX_REPORTS:
            raise VastDataError("malformed_reports")
        reports = []
        for item in payload:
            if not isinstance(item, dict):
                raise VastDataError("malformed_reports")
            problem = _bounded_text(item.get("problem"), 256)
            message = _bounded_text(item.get("message"), 4096, allow_empty=True)
            created_at = _report_created_at(item.get("created_at"))
            reports.append(MachineReport(problem, message, created_at))
        return tuple(reports)

    def search_offers(self) -> MarketObservation:
        """Return normalized offers; failures/incompleteness remain unknown."""
        observed_at = _now(self._clock)
        try:
            payload = self._http.request_json(
                "POST",
                OFFERS_PATH,
                headers=self._headers,
                payload={
                    "limit": MAX_OFFERS,
                    "machine_id": {"eq": TARGET_MACHINE_ID},
                },
            )
            return _normalize_market(payload, observed_at)
        except (HttpClientError, VastDataError) as error:
            reason = error.reason
            return _unknown_market(observed_at, reason)

    def collect(self) -> VastSnapshot:
        """Collect independent source slices without turning partial into empty."""
        errors: list[str] = []
        machine = None
        reports = None
        try:
            machine = self.get_machine()
        except (HttpClientError, VastDataError) as error:
            errors.append(f"machines:{error.reason}")
        try:
            reports = self.get_reports()
        except (HttpClientError, VastDataError) as error:
            errors.append(f"reports:{error.reason}")
        market = self.search_offers()
        if market.error is not None:
            errors.append(f"offers:{market.error}")
        return VastSnapshot(_now(self._clock), machine, reports, market, tuple(errors))


def with_market_freshness(
    observation: MarketObservation,
    *,
    now: datetime,
    max_age_seconds: int = 180,
) -> MarketObservation:
    """Fail stale persisted market evidence back to unknown.

    The API schema provides no authoritative offer-observation timestamp, so
    ``observed_at`` is the successful HTTP receipt time.  State/controller code
    should apply this helper when reading a persisted observation.
    """
    if not isinstance(observation, MarketObservation):
        raise VastDataError("invalid_market_observation")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise VastDataError("invalid_clock")
    if not 1 <= max_age_seconds <= 900:
        raise VastDataError("invalid_max_age")
    age = (
        now.astimezone(timezone.utc) - observation.observed_at.astimezone(timezone.utc)
    ).total_seconds()
    if -30 <= age <= max_age_seconds:
        return observation
    return replace(
        observation,
        search_complete=False,
        advertised=None,
        rentable=None,
        launch_proven=None,
        advertised_gpu_capacity=None,
        rentable_gpu_capacity=None,
        launch_proven_gpu_capacity=None,
        holds=None,
        error="stale_observation",
    )


def _normalize_market(payload: object, observed_at: datetime) -> MarketObservation:
    if not isinstance(payload, dict):
        raise VastDataError("malformed_offers")
    raw_offers = payload.get("offers")
    # The documented example historically showed an object while the search
    # workflow specifies an array.  A singleton object is normalized without
    # widening the accepted schema further.
    if isinstance(raw_offers, dict):
        raw_offers = [raw_offers]
    if not isinstance(raw_offers, list) or len(raw_offers) > MAX_OFFERS:
        raise VastDataError("malformed_offers")
    search_complete = len(raw_offers) < MAX_OFFERS
    total = payload.get("total", payload.get("count"))
    if total is not None:
        if isinstance(total, bool) or not isinstance(total, int) or total < len(raw_offers):
            raise VastDataError("malformed_offer_count")
        if total > len(raw_offers):
            search_complete = False
    pagination_values = (payload.get(key) for key in ("next", "next_url", "has_more"))
    if any(value is not None and value != "" and value is not False for value in pagination_values):
        # Never follow a server-provided URL.  Callers receive an explicitly
        # incomplete observation instead of a misleading empty/complete one.
        search_complete = False
    offers: list[OfferSlice] = []
    rentable_values: list[bool | None] = []
    for item in raw_offers:
        if not isinstance(item, dict):
            raise VastDataError("malformed_offers")
        if _identifier(item.get("machine_id")) != TARGET_MACHINE_ID:
            raise VastDataError("target_identity_mismatch")
        gpu_count = _required_count(item.get("num_gpus"))
        rentable = _optional_bool(item.get("rentable"))
        rented = _optional_bool(item.get("rented"))
        offer_id = _optional_identifier(item.get("id"))
        offers.append(OfferSlice(offer_id, gpu_count, rentable, rented))
        rentable_values.append(rentable)

    # Bundle offers commonly overlap (1/2/4/8 slices of the same GPUs).  The
    # maximum slice is the only defensible capacity observation; summing would
    # fabricate 15 GPUs from an 8-GPU machine.
    counts = [offer.gpu_count for offer in offers]
    advertised = (True if offers else False) if search_complete else None
    advertised_capacity = max(counts) if offers and search_complete else None
    if not search_complete:
        rentable = None
    elif any(value is True for value in rentable_values):
        rentable = True
    elif search_complete and all(value is False for value in rentable_values):
        rentable = False
    else:
        rentable = None
    rentable_counts = [
        offer.gpu_count for offer in offers if offer.rentable is True
    ]
    rentable_capacity = (
        max(rentable_counts)
        if rentable_counts and search_complete
        else 0
        if search_complete and rentable is False
        else None
    )
    return MarketObservation(
        machine_id=TARGET_MACHINE_ID,
        observed_at=observed_at,
        search_complete=search_complete,
        offers=tuple(offers),
        advertised=advertised,
        rentable=rentable,
        launch_proven=None,
        advertised_gpu_capacity=advertised_capacity,
        rentable_gpu_capacity=rentable_capacity,
        launch_proven_gpu_capacity=None,
        holds=None,
        error=None if search_complete else "search_incomplete",
    )


def _unknown_market(observed_at: datetime, reason: str) -> MarketObservation:
    return MarketObservation(
        machine_id=TARGET_MACHINE_ID,
        observed_at=observed_at,
        search_complete=False,
        offers=(),
        advertised=None,
        rentable=None,
        launch_proven=None,
        advertised_gpu_capacity=None,
        rentable_gpu_capacity=None,
        launch_proven_gpu_capacity=None,
        holds=None,
        error=reason,
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise VastDataError("invalid_clock")
    return value.astimezone(timezone.utc)


def _identifier(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _optional_identifier(value: object) -> int | None:
    if value is None:
        return None
    result = _identifier(value)
    if result is None:
        raise VastDataError("malformed_offer_id")
    return result


def _required_count(value: object) -> int:
    result = _optional_count(value)
    if result is None or result == 0:
        raise VastDataError("missing_offer_gpu_count")
    return result


def _optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise VastDataError("invalid_gpu_count")
    if not 0 <= value <= MAX_GPU_COUNT:
        raise VastDataError("invalid_gpu_count")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if value in {0, 1} and isinstance(value, int):
        return bool(value)
    return None


def _bounded_text(value: object, limit: int, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > limit
    ):
        raise VastDataError("malformed_report")
    return value


def _report_created_at(value: object) -> str:
    if isinstance(value, str):
        return _bounded_text(value, 64)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 4_102_444_800
    ):
        raise VastDataError("malformed_report")
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
