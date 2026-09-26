"""Deterministic cross-source GPU capacity reconciliation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .prometheus import MetricBatch, PrometheusError, Sample
from .vast import MAX_OFFERS, TARGET_MACHINE_ID, OfferSlice, VastSnapshot


MAX_GPU_COUNT = 32
MAX_RECONCILIATION_EVENTS = 24
class CapacityDataError(ValueError):
    def __init__(self, source: str, reason: str = "malformed"):
        super().__init__(reason)
        self.source = source
        self.reason = reason


@dataclass(frozen=True)
class TargetCapacity:
    physical: int
    healthy: int
    nvidia_visible: int
    vfio: int
    uuids: frozenset[str]


@dataclass(frozen=True)
class VastCapacity:
    total: int
    rented: int
    idle: int
    occupancy: dict[str, int]
    listed: bool
    verified: bool
    error_description: str


def _event(
    code: str,
    severity: str,
    message: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "fault_family": "capacity",
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": evidence or {},
    }


def prometheus_failure_event(error: PrometheusError) -> dict[str, object]:
    reason_codes = {
        "request_failed": "prometheus_query_failed",
        "http_failure": "prometheus_query_failed",
        "redirect_rejected": "prometheus_query_failed",
        "response_limit": "prometheus_response_oversized",
        "malformed_response": "prometheus_response_malformed",
        "stale_data": "prometheus_data_stale",
    }
    if error.reason == "stale_data" and error.query_id.startswith("vast"):
        code = "vast_scrape_stale"
    elif error.reason == "stale_data" and error.query_id.startswith("dcgm"):
        code = "dcgm_scrape_stale"
    else:
        code = reason_codes.get(error.reason, "prometheus_data_invalid")
    event = _event(
        code,
        "error",
        "Prometheus capacity evidence is unavailable or invalid",
        {"query": error.query_id, "reason": error.reason},
    )
    event["component"] = error.query_id
    return event


def merge_events(
    probe: dict[str, Any],
    reconciliation_events: list[dict[str, object]],
) -> dict[str, Any]:
    """Return a new probe whose health includes reconciliation results."""
    existing = probe.get("events")
    original_healthy = probe.get("healthy")
    if not isinstance(existing, list) or not isinstance(original_healthy, bool):
        raise ValueError("probe health and events have invalid types")
    merged_events = [*existing, *reconciliation_events]
    if len(merged_events) > 128:
        raise ValueError("combined probe contains more than 128 events")
    result = dict(probe)
    result["events"] = merged_events
    # Preserve a contradiction produced by the target so Supervisor can retain
    # its existing unknown-analysis behavior. A clean target plus a known
    # reconciliation event is simply unhealthy and needs no model request.
    result["healthy"] = (
        original_healthy if existing else original_healthy and not merged_events
    )
    return result


def _bounded_count(value: object, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_GPU_COUNT:
        raise CapacityDataError(source)
    return value


def _target_capacity(probe: dict[str, Any], now: datetime, max_age_seconds: int) -> TargetCapacity:
    try:
        observed_text = probe["observed_at"]
        if not isinstance(observed_text, str) or not observed_text.endswith("Z"):
            raise ValueError
        observed = datetime.fromisoformat(observed_text[:-1] + "+00:00")
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if age > max_age_seconds or age < -30:
            raise CapacityDataError("target", "stale")
        gpu = probe["snapshot"]["gpu"]
        physical = _bounded_count(gpu["pci_count"], "target")
        nvidia = _bounded_count(gpu["nvidia_count"], "target")
        vfio = _bounded_count(gpu["vfio_count"], "target")
        visible = gpu["gpus"]
        pci_devices = gpu["pci_devices"]
        if (
            not isinstance(visible, list)
            or not isinstance(pci_devices, list)
            or len(visible) != nvidia
            or len(pci_devices) != physical
            or nvidia + vfio > physical
        ):
            raise ValueError
        pci_by_bdf: dict[str, str] = {}
        for item in pci_devices:
            if not isinstance(item, dict):
                raise ValueError
            bdf = item.get("pci_bdf")
            driver = item.get("driver")
            if (
                not isinstance(bdf, str)
                or not bdf
                or len(bdf) > 32
                or not isinstance(driver, str)
                or not driver
                or len(driver) > 64
                or bdf in pci_by_bdf
            ):
                raise ValueError
            pci_by_bdf[bdf] = driver
        if sum(driver == "vfio-pci" for driver in pci_by_bdf.values()) != vfio:
            raise ValueError

        uuids = []
        visible_bdfs: set[str] = set()
        for item in visible:
            if not isinstance(item, dict):
                raise ValueError
            uuid = item.get("uuid")
            bdf = item.get("pci_bdf")
            if (
                not isinstance(uuid, str)
                or not uuid.startswith("GPU-")
                or len(uuid) > 80
                or not isinstance(bdf, str)
                or bdf in visible_bdfs
                or pci_by_bdf.get(bdf) != "nvidia"
            ):
                raise ValueError
            uuids.append(uuid)
            visible_bdfs.add(bdf)
        if len(set(uuids)) != len(uuids):
            raise ValueError
    except CapacityDataError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise CapacityDataError("target") from error
    return TargetCapacity(physical, len(visible_bdfs) + vfio, nvidia, vfio, frozenset(uuids))


def _named(samples: tuple[Sample, ...], name: str) -> list[Sample]:
    return [sample for sample in samples if sample.labels.get("__name__") == name]


def _integer_gauge(sample: Sample, source: str) -> int:
    value = int(sample.value)
    if sample.value != value or not 0 <= value <= MAX_GPU_COUNT:
        raise CapacityDataError(source)
    return value


def _one(samples: tuple[Sample, ...], name: str, source: str) -> Sample:
    matches = _named(samples, name)
    if len(matches) != 1:
        raise CapacityDataError(source, "missing_or_ambiguous")
    return matches[0]


def _vast_capacity(samples: tuple[Sample, ...], machine_id: str) -> VastCapacity:
    if any(sample.labels.get("machine_id") != machine_id for sample in samples):
        raise CapacityDataError("vast", "identity_mismatch")
    info = _one(samples, "vast_machine_hostname", "vast")
    if info.value != 1 or not info.labels.get("hostname"):
        raise CapacityDataError("vast", "identity_mismatch")
    total = _integer_gauge(
        _one(samples, "vast_machine_num_gpus", "vast"), "vast"
    )

    rented = sum(
        _integer_gauge(_one(samples, metric, "vast"), "vast")
        for metric in (
            "vastai_machine_gpu_rented_on_demand",
            "vastai_machine_gpu_rented_bid_demand",
            "vastai_machine_gpu_rented_on_reserved",
        )
    )
    if rented > MAX_GPU_COUNT:
        raise CapacityDataError("vast")
    idle = _integer_gauge(_one(samples, "vastai_machine_gpu_idle", "vast"), "vast")

    occupancy_samples = _named(samples, "vastai_machine_gpu_occupancy")
    if not occupancy_samples:
        raise CapacityDataError("vast", "missing_per_gpu_occupancy")
    occupancy: dict[str, int] = {}
    for sample in occupancy_samples:
        state = int(sample.value)
        if sample.value != state or not 0 <= state <= 8:
            raise CapacityDataError("vast")
        gpu = sample.labels.get("gpu", "")
        if (
            not gpu
            or len(gpu) > 80
            or gpu in occupancy
        ):
            raise CapacityDataError("vast")
        occupancy[gpu] = state
    if not occupancy or len(occupancy) > MAX_GPU_COUNT:
        raise CapacityDataError("vast")

    listed_value = _one(samples, "vast_machine_Listed", "vast").value
    verified_value = _one(samples, "vast_machine_Verification", "vast").value
    if listed_value not in {0.0, 1.0} or verified_value not in {0.0, 1.0}:
        raise CapacityDataError("vast")
    error_sample = _one(samples, "vastai_machine_ErrorDescription", "vast")
    error_description = error_sample.labels.get("error_description", "")
    if len(error_description) > 512:
        raise CapacityDataError("vast")
    return VastCapacity(
        total=total,
        rented=rented,
        idle=idle,
        occupancy=occupancy,
        listed=bool(listed_value),
        verified=bool(verified_value),
        error_description=error_description,
    )


def _up_value(samples: tuple[Sample, ...], source: str) -> bool:
    if len(samples) != 1 or samples[0].value not in {0.0, 1.0}:
        raise CapacityDataError(source, "missing_or_ambiguous_up")
    return bool(samples[0].value)


def _dcgm_uuid_sets(samples: tuple[Sample, ...]) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for metric_name in ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED"):
        by_uuid: dict[str, Sample] = {}
        for sample in _named(samples, metric_name):
            uuid = sample.labels.get("UUID") or sample.labels.get("uuid")
            if not uuid or not uuid.startswith("GPU-") or len(uuid) > 80 or uuid in by_uuid:
                raise CapacityDataError("dcgm")
            if metric_name == "DCGM_FI_DEV_GPU_UTIL" and not 0 <= sample.value <= 100:
                raise CapacityDataError("dcgm")
            if metric_name == "DCGM_FI_DEV_FB_USED" and sample.value < 0:
                raise CapacityDataError("dcgm")
            by_uuid[uuid] = sample
        if not by_uuid:
            raise CapacityDataError("dcgm", "missing_series")
        result[metric_name] = frozenset(by_uuid)
    return result


def _data_error_event(error: CapacityDataError) -> dict[str, object]:
    code = "target_capacity_invalid" if error.source == "target" else "prometheus_data_missing"
    if error.reason == "stale":
        code = "target_probe_stale"
    event = _event(
        code,
        "error",
        "Required capacity evidence is missing, stale, ambiguous, or malformed",
        {"source": error.source, "reason": error.reason},
    )
    event["component"] = error.source
    return event


# reconcile_capacity returns these alone, before or instead of the remaining checks.
INCOMPLETE_CAPACITY_CODES = frozenset(
    {
        "target_capacity_invalid",
        "target_probe_stale",
        "prometheus_data_missing",
        "vast_scrape_down",
        "dcgm_scrape_down",
    }
)


def metric_batch_fresh(metrics: MetricBatch, now: datetime, max_age_seconds: int) -> bool:
    """True when every capacity sample in the batch is within the freshness bound.

    The runtime keeps the last batch after Prometheus collection fails; judging a
    new target capture against it would record old metrics as current health.
    """
    samples = (
        *metrics.vast, *metrics.vast_errors, *metrics.vast_up, *metrics.dcgm, *metrics.dcgm_up
    )
    return bool(samples) and all(
        _fresh_sample(sample, now, max_age_seconds) for sample in samples
    )


def capacity_evaluation_complete(events: list[dict[str, object]]) -> bool:
    """True when reconcile_capacity ran every check rather than stopping early."""
    return not any(event.get("code") in INCOMPLETE_CAPACITY_CODES for event in events)


@dataclass(frozen=True)
class MarketAssessment:
    events: list[dict[str, object]]
    # False when no source could rule out idle GPUs hidden by an unrentable market.
    conclusive: bool


def reconcile_capacity(
    probe: dict[str, Any],
    metrics: MetricBatch,
    *,
    now: datetime,
    max_age_seconds: int = 180,
    probe_max_age_seconds: int | None = None,
) -> list[dict[str, object]]:
    """Compare physical, Vast and DCGM evidence without inferring from utilization.

    ``probe_max_age_seconds`` bounds the target probe separately, because the full
    SSH capture runs far less often than the metric freshness bound.
    """
    events: list[dict[str, object]] = []
    try:
        vast_up = _up_value(metrics.vast_up, "vast")
        dcgm_up = _up_value(metrics.dcgm_up, "dcgm")
    except CapacityDataError as error:
        return [_data_error_event(error)]
    if not vast_up:
        events.append(
            _event("vast_scrape_down", "error", "The Vast exporter scrape target is down")
        )
    if not dcgm_up:
        events.append(
            _event("dcgm_scrape_down", "error", "The DCGM scrape target is down")
        )
    # A missing Vast scrape makes every Vast-derived capacity claim unavailable.
    # A missing DCGM scrape does not: listing, verification, the exporter-provided
    # machine error and the target/Vast capacity comparison are independent of DCGM.
    # Returning for either outage used to hide a live Vast machine error whenever
    # DCGM was down -- exactly when the whole-machine answer most needed to preserve
    # every source that was still available.
    if not vast_up:
        return events

    try:
        target = _target_capacity(
            probe,
            now,
            max_age_seconds if probe_max_age_seconds is None else probe_max_age_seconds,
        )
        vast = _vast_capacity(metrics.vast, str(probe.get("machine_id", "")))
        if len(metrics.vast_errors) != 1 or metrics.vast_errors[0].value < 0:
            raise CapacityDataError("vast", "invalid_error_series")
    except CapacityDataError as error:
        return [_data_error_event(error)]

    recent_errors = metrics.vast_errors[0].value
    if recent_errors > 0:
        events.append(
            _event(
                "vast_exporter_recent_errors",
                "warning",
                "The Vast exporter reported API errors in its bounded lookback",
                {"recent_errors": min(recent_errors, 999.0)},
            )
        )
    if not vast.listed:
        events.append(
            _event("vast_machine_unlisted", "warning", "Vast reports the machine unlisted")
        )
    if not vast.verified:
        events.append(
            _event("vast_machine_unverified", "warning", "Vast reports the machine unverified")
        )
    if vast.error_description:
        events.append(
            _event(
                "vast_machine_error",
                "error",
                "Vast reports a machine error",
                {"error_description": vast.error_description},
            )
        )

    counts = {
        "physical": target.physical,
        "healthy": target.healthy,
        "nvidia_visible": target.nvidia_visible,
        "vfio": target.vfio,
        "vast_total": vast.total,
        "vast_rented": vast.rented,
        "vast_idle": vast.idle,
        "occupancy_entries": len(vast.occupancy),
    }
    if vast.total > target.physical:
        events.append(
            _event(
                "vast_total_exceeds_physical",
                "critical",
                "Vast advertises more GPUs than the physical PCI inventory",
                counts,
            )
        )
    elif vast.total != target.physical:
        events.append(
            _event(
                "vast_total_differs_physical",
                "error",
                "Vast GPU capacity differs from the physical PCI inventory",
                counts,
            )
        )
    if vast.rented > target.healthy:
        events.append(
            _event(
                "vast_rented_exceeds_healthy",
                "critical",
                "Vast reports more rented GPUs than the target has healthy GPUs",
                counts,
            )
        )
    if vast.total != vast.rented + vast.idle:
        events.append(
            _event(
                "vast_capacity_arithmetic_mismatch",
                "error",
                "Vast total, rented and idle GPU counts do not reconcile",
                counts,
            )
        )
    if len(vast.occupancy) != vast.total:
        events.append(
            _event(
                "vast_occupancy_mismatch",
                "error",
                "Vast per-GPU occupancy entry count does not match total capacity",
                counts,
            )
        )

    physical_free = max(0, target.healthy - vast.rented)
    if physical_free > vast.idle or (
        physical_free > 0 and (not vast.listed or not vast.verified)
    ):
        events.append(
            _event(
                "physical_free_vast_unavailable",
                "error",
                "Healthy physical GPUs appear free but Vast does not expose them as idle",
                {
                    **counts,
                    "physical_free": physical_free,
                    "listed": vast.listed,
                    "verified": vast.verified,
                },
            )
        )
    elif vast.idle > physical_free:
        events.append(
            _event(
                "vast_idle_exceeds_physical_free",
                "error",
                "Vast reports more idle GPUs than healthy unrented capacity",
                {**counts, "physical_free": physical_free},
            )
        )

    if dcgm_up:
        try:
            dcgm_sets = _dcgm_uuid_sets(metrics.dcgm)
        except CapacityDataError as error:
            events.append(_data_error_event(error))
        else:
            util_uuids = dcgm_sets["DCGM_FI_DEV_GPU_UTIL"]
            memory_uuids = dcgm_sets["DCGM_FI_DEV_FB_USED"]
            if util_uuids != target.uuids or memory_uuids != target.uuids:
                events.append(
                    _event(
                        "dcgm_identity_mismatch",
                        "error",
                        "DCGM UUID identity does not match the NVIDIA-visible target inventory",
                        {
                            "target_uuid_count": len(target.uuids),
                            "dcgm_util_uuid_count": len(util_uuids),
                            "dcgm_memory_uuid_count": len(memory_uuids),
                            "missing_util_count": len(target.uuids - util_uuids),
                            "missing_memory_count": len(target.uuids - memory_uuids),
                            "unexpected_util_count": len(util_uuids - target.uuids),
                            "unexpected_memory_count": len(memory_uuids - target.uuids),
                        },
                    )
                )

    # DCGM utilization and framebuffer use prove telemetry identity and liveness.
    # They deliberately do not decide rental availability: a rented workload can
    # be idle, and a low utilization sample is not a free-GPU signal.
    return events[:MAX_RECONCILIATION_EVENTS]


def reconcile_market(
    probe: dict[str, Any] | None,
    snapshot: VastSnapshot,
    *,
    metrics: MetricBatch | None = None,
    now: datetime,
    max_age_seconds: int = 180,
    probe_max_age_seconds: int | None = None,
) -> list[dict[str, object]]:
    """Return the events of :func:`assess_market`."""
    return assess_market(
        probe,
        snapshot,
        metrics=metrics,
        now=now,
        max_age_seconds=max_age_seconds,
        probe_max_age_seconds=probe_max_age_seconds,
    ).events


def assess_market(
    probe: dict[str, Any] | None,
    snapshot: VastSnapshot,
    *,
    metrics: MetricBatch | None = None,
    now: datetime,
    max_age_seconds: int = 180,
    probe_max_age_seconds: int | None = None,
    cross_source_only: bool = False,
    require_target: bool = False,
) -> MarketAssessment:
    """Reconcile direct Vast machine/market evidence with a target capture.

    Offer absence is conclusive only for a complete search.  A denied or incomplete
    search, unknown holds, and lack of a launch test remain explicit unknowns.  This
    function never creates a canary rental or interprets low utilization as capacity.
    Idle capacity hidden by an unrentable market is decided by the target probe when
    it can be evaluated, otherwise by agreeing Prometheus exporter evidence.  When
    neither decides, the assessment is inconclusive rather than healthy.

    ``cross_source_only`` omits findings about a single source's own evidence (stale,
    partial or unknown Vast data, unusable target captures). The ``vast`` and
    ``capacity-reconciliation`` sources already report those; repeating them would
    open a duplicate incident for one fault.

    ``require_target`` is for callers that collect target captures. Findings that
    compare the capture with Vast can only clear against a usable capture, so the
    assessment is inconclusive without one.
    """

    if not isinstance(snapshot, VastSnapshot):
        raise CapacityDataError("vast-direct", "malformed")
    events: list[dict[str, object]] = []
    market = snapshot.market
    age = (
        now.astimezone(timezone.utc)
        - snapshot.observed_at.astimezone(timezone.utc)
    ).total_seconds()
    if age > max_age_seconds or age < -30:
        if cross_source_only:
            return MarketAssessment([], False)
        return MarketAssessment(
            [
                _event(
                    "vast_direct_stale",
                    "error",
                    "Direct Vast machine and market evidence is stale",
                    {"source": "vast-direct"},
                )
            ],
            True,
        )

    if snapshot.machine is None and not cross_source_only:
        events.append(
            _event(
                "vast_machine_unknown",
                "error",
                "Direct Vast machine state is unavailable",
                {"errors": list(snapshot.errors[:8])},
            )
        )
    if snapshot.errors and not cross_source_only:
        events.append(
            _event(
                "vast_direct_partial",
                "error",
                "Direct Vast collection returned partial evidence",
                {"errors": list(snapshot.errors[:8])},
            )
        )
    # The reports API is a timestamped report history, without a documented
    # active/resolved marker. Preserve it as evidence in the source snapshot;
    # report existence alone cannot establish a current self-test failure.
    # https://docs.vast.ai/api-reference/machines/show-reports
    if not market.search_complete and not cross_source_only:
        events.append(
            _event(
                "vast_market_unknown",
                "warning",
                "Vast offer availability is unknown because the fixed search is incomplete",
                {
                    "reason": market.error or "search_incomplete",
                    "holds": market.holds,
                    "launch_proven": market.launch_proven,
                },
            )
        )

    target: TargetCapacity | None = None
    if probe is not None:
        try:
            target = _target_capacity(
                probe,
                now,
                max_age_seconds if probe_max_age_seconds is None else probe_max_age_seconds,
            )
        except CapacityDataError as error:
            if not cross_source_only:
                events.append(_data_error_event(error))
    machine = snapshot.machine
    target_compared = target is not None and machine is not None
    if target is not None and machine is not None:
        counts = {
            "physical": target.physical,
            "healthy": target.healthy,
            "machine_total": machine.total_gpus,
            "machine_rented": machine.rented_gpus,
            "market_advertised": market.advertised_gpu_capacity,
            "market_rentable": market.rentable_gpu_capacity,
        }
        if machine.total_gpus is not None and machine.total_gpus != target.physical:
            events.append(
                _event(
                    "vast_direct_total_differs_physical",
                    "critical" if machine.total_gpus > target.physical else "error",
                    "Direct Vast GPU capacity differs from physical target evidence",
                    counts,
                )
            )
        if machine.rented_gpus is not None and machine.rented_gpus > target.healthy:
            events.append(
                _event(
                    "vast_direct_rented_exceeds_healthy",
                    "critical",
                    "Direct Vast rented capacity exceeds healthy target capacity",
                    counts,
                )
            )
        for field, count in (
            ("advertised", market.advertised_gpu_capacity),
            ("rentable", market.rentable_gpu_capacity),
        ):
            if count is not None and count > target.healthy:
                events.append(
                    _event(
                        f"vast_market_{field}_exceeds_healthy",
                        "critical",
                        f"Vast market {field} capacity exceeds healthy target capacity",
                        counts,
                    )
                )
        rented = machine.rented_gpus
        if rented is not None and target.healthy > rented:
            physical_free = target.healthy - rented
            known_unavailable = (
                machine.rentable is False
                or (market.search_complete and market.rentable is False)
            )
            if known_unavailable:
                events.append(
                    _event(
                        "physical_free_vast_market_unavailable",
                        "error",
                        "Healthy unrented GPUs are not available through Vast",
                        {**counts, "physical_free": physical_free, "basis": "target"},
                    )
                )
    market_blocked = (machine is not None and machine.rentable is False) or (
        market.search_complete and market.rentable is False
    )
    market_available = (machine is not None and machine.rentable is True) or (
        market.search_complete and market.rentable is True
    )
    # The capture decides hidden capacity when no healthy GPU is free, or when the
    # market state is known. Free GPUs behind an unknown market are not health.
    hidden_decided = (
        target is not None
        and machine is not None
        and machine.rented_gpus is not None
        and (target.healthy <= machine.rented_gpus or market_blocked or market_available)
    )
    if not hidden_decided:
        prometheus_decided, idle_market_event = _prometheus_idle_market_assessment(
            snapshot,
            metrics,
            now=now,
            max_age_seconds=max_age_seconds,
        )
        hidden_decided = prometheus_decided or (market_available and not market_blocked)
        if idle_market_event is not None:
            events.append(idle_market_event)
    # Capacity and rental mismatches against the capture can only clear when a
    # usable capture was compared with known Vast values; a check skipped for an
    # unknown value must not look like a cleared fault. A complete search with no
    # offers leaves advertised capacity unset, which means zero, not unknown.
    comparison_inputs_known = (
        machine is not None
        and machine.total_gpus is not None
        and machine.rented_gpus is not None
        and market.search_complete
        and market.rentable_gpu_capacity is not None
    )
    conclusive = (
        hidden_decided
        and (target_compared or not require_target)
        and (not target_compared or comparison_inputs_known)
    )
    return MarketAssessment(events[:MAX_RECONCILIATION_EVENTS], conclusive)


def _prometheus_idle_market_assessment(
    snapshot: VastSnapshot,
    metrics: MetricBatch | None,
    *,
    now: datetime,
    max_age_seconds: int,
) -> tuple[bool, dict[str, object] | None]:
    """Return (decided, event); both sources must be conclusive and agree."""
    if metrics is None:
        return False, None
    market = snapshot.market
    machine = snapshot.machine
    if (
        market.machine_id != TARGET_MACHINE_ID
        or (machine is not None and machine.machine_id != TARGET_MACHINE_ID)
        or not market.search_complete
        or market.rentable is not False
        or market.rentable_gpu_capacity != 0
        or not _fresh_datetime(market.observed_at, now, max_age_seconds)
    ):
        return False, None
    offer_summary = _offer_slice_summary(market.offers)
    if offer_summary is None:
        return False, None

    try:
        if not _up_value(metrics.vast_up, "vast"):
            return False, None
        if len(metrics.vast_errors) != 1 or metrics.vast_errors[0].value != 0:
            return False, None
        samples = (*metrics.vast, *metrics.vast_errors, *metrics.vast_up)
        if not samples or any(
            not _fresh_sample(sample, now, max_age_seconds) for sample in samples
        ):
            return False, None
        capacity = _vast_capacity(metrics.vast, str(TARGET_MACHINE_ID))
    except CapacityDataError:
        return False, None
    if (
        capacity.total != capacity.rented + capacity.idle
        or len(capacity.occupancy) != capacity.total
    ):
        return False, None
    # Each source may be up to max_age_seconds old. A machine that just filled or
    # emptied must not be judged from exporter counts the direct API contradicts.
    if machine is not None and (
        (machine.rented_gpus is not None and machine.rented_gpus != capacity.rented)
        or (machine.total_gpus is not None and machine.total_gpus != capacity.total)
    ):
        return False, None
    if capacity.idle < 1:
        return True, None
    return True, _event(
        "physical_free_vast_market_unavailable",
        "error",
        "Prometheus reports idle GPUs but the complete Vast market search has no rentable offer",
        {
            "idle": capacity.idle,
            "rented": capacity.rented,
            "total": capacity.total,
            "offer_slice_summary": offer_summary,
            "basis": "prometheus",
        },
    )


def _fresh_datetime(
    observed_at: object,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or not isinstance(now, datetime)
        or now.tzinfo is None
    ):
        return False
    age = (
        now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
    ).total_seconds()
    return -30 <= age <= max_age_seconds


def _fresh_sample(sample: Sample, now: datetime, max_age_seconds: int) -> bool:
    if not isinstance(sample, Sample) or not math.isfinite(sample.timestamp):
        return False
    age = now.astimezone(timezone.utc).timestamp() - sample.timestamp
    return -30 <= age <= max_age_seconds


def _offer_slice_summary(
    offers: tuple[OfferSlice, ...],
) -> list[dict[str, object]] | None:
    if not isinstance(offers, tuple) or len(offers) > MAX_OFFERS:
        return None
    slices: list[dict[str, object]] = []
    for offer in offers:
        if (
            not isinstance(offer, OfferSlice)
            or isinstance(offer.gpu_count, bool)
            or not isinstance(offer.gpu_count, int)
            or not 0 <= offer.gpu_count <= MAX_GPU_COUNT
            or offer.rentable is not False
            or (offer.rented is not None and not isinstance(offer.rented, bool))
        ):
            return None
        slices.append(
            {
                "gpu_count": offer.gpu_count,
                "rentable": offer.rentable,
                "rented": offer.rented,
            }
        )
    return sorted(
        slices,
        key=lambda item: (
            int(item["gpu_count"]),
            str(item["rentable"]),
            str(item["rented"]),
        ),
    )
