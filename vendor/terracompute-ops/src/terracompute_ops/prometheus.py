"""Bounded, read-only access to a configured Prometheus HTTP API."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .http_client import HttpClientError, HttpRequest, StdlibTransport


MAX_RESPONSE_BYTES = 256 * 1024
MAX_SERIES = 256
MAX_LABELS = 32
MAX_LABEL_CHARS = 256
MAX_ALERTS = 128
MAX_ANNOTATIONS = 16
REQUEST_TIMEOUT_SECONDS = 2
AGGREGATE_TIMEOUT_SECONDS = 8.0
FUTURE_SKEW_SECONDS = 30

_MACHINE_ID = re.compile(r"[0-9]{1,12}\Z")
_JOB_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\Z"
)
_METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\Z")

VAST_METRIC_NAMES = (
    "vast_machine_hostname",
    "vast_machine_num_gpus",
    "vastai_machine_gpu_rented_on_demand",
    "vastai_machine_gpu_rented_bid_demand",
    "vastai_machine_gpu_rented_on_reserved",
    "vastai_machine_gpu_idle",
    "vastai_machine_gpu_occupancy",
    "vast_machine_Listed",
    "vast_machine_Verification",
    "vastai_machine_ErrorDescription",
)
DCGM_METRIC_NAMES = (
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_FB_USED",
)


class PrometheusError(ValueError):
    """A secret-free, fixed-category failure suitable for incident evidence."""

    def __init__(self, reason: str, query_id: str = "configuration"):
        super().__init__(reason)
        self.reason = reason
        self.query_id = query_id


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    timestamp: float
    value: float


@dataclass(frozen=True)
class MetricBatch:
    vast: tuple[Sample, ...]
    vast_errors: tuple[Sample, ...]
    vast_up: tuple[Sample, ...]
    dcgm: tuple[Sample, ...]
    dcgm_up: tuple[Sample, ...]


class AlertState(str, Enum):
    FIRING = "firing"
    PENDING = "pending"
    UNKNOWN = "unknown"


class AlertFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    FUTURE = "future"


@dataclass(frozen=True)
class PrometheusAlert:
    labels: dict[str, str]
    annotations: dict[str, str]
    state: AlertState
    active_at: datetime | None
    value: str | None


@dataclass(frozen=True)
class AlertSnapshot:
    """A complete, validated fixed-endpoint alert response."""

    observed_at: datetime
    alerts: tuple[PrometheusAlert, ...]
    complete: bool = True
    max_age_seconds: int = 180

    @property
    def firing(self) -> tuple[PrometheusAlert, ...]:
        return tuple(alert for alert in self.alerts if alert.state is AlertState.FIRING)

    @property
    def pending(self) -> tuple[PrometheusAlert, ...]:
        return tuple(alert for alert in self.alerts if alert.state is AlertState.PENDING)

    @property
    def unknown(self) -> tuple[PrometheusAlert, ...]:
        return tuple(alert for alert in self.alerts if alert.state is AlertState.UNKNOWN)

    @property
    def healthy(self) -> bool:
        # Parser failures never produce a snapshot.  An explicit incomplete
        # snapshot therefore cannot turn an empty list into healthy evidence.
        return self.complete and not self.alerts

    def freshness_at(self, now: datetime) -> AlertFreshness:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise PrometheusError("invalid_clock", "alerts")
        age = now.astimezone(timezone.utc).timestamp() - self.observed_at.timestamp()
        if age < -FUTURE_SKEW_SECONDS:
            return AlertFreshness.FUTURE
        if age > self.max_age_seconds:
            return AlertFreshness.STALE
        return AlertFreshness.FRESH

    def healthy_at(self, now: datetime) -> bool:
        return self.healthy and self.freshness_at(now) is AlertFreshness.FRESH


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_endpoint(value: str) -> str:
    """Accept one configured API origin, never credentials, paths or parameters."""
    if not isinstance(value, str) or len(value) > 512:
        raise PrometheusError("invalid_endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise PrometheusError("invalid_endpoint") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
        or not 1 <= port <= 65535
    ):
        raise PrometheusError("invalid_endpoint")
    hostname = parsed.hostname
    if hostname not in {"localhost", "::1"} and not _HOST.fullmatch(hostname):
        try:
            ipaddress.ip_address(hostname)
        except ValueError as error:
            raise PrometheusError("invalid_endpoint") from error
    bracketed_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{parsed.scheme}://{bracketed_host}:{port}"


def validate_job_name(value: str) -> str:
    if not isinstance(value, str) or not _JOB_NAME.fullmatch(value):
        raise PrometheusError("invalid_job_name")
    return value


def _metric_selector(names: tuple[str, ...]) -> str:
    return "|".join(re.escape(name) for name in names)


def _fixed_queries(machine_id: str, vast_job: str, dcgm_job: str) -> dict[str, str]:
    if not _MACHINE_ID.fullmatch(machine_id):
        raise PrometheusError("invalid_machine_id")
    vast_job = validate_job_name(vast_job)
    dcgm_job = validate_job_name(dcgm_job)
    vast_selector = (
        f'{{__name__=~"{_metric_selector(VAST_METRIC_NAMES)}",'
        f'machine_id="{machine_id}",job="{vast_job}"}}'
    )
    dcgm_selector = (
        f'{{__name__=~"{_metric_selector(DCGM_METRIC_NAMES)}",job="{dcgm_job}"}}'
    )
    # The community compose stack groups several targets under each job. Port
    # identity keeps `up` specific without accepting a caller-supplied selector.
    vast_up = f'up{{job="{vast_job}",instance=~".*:8622"}}'
    dcgm_up = f'up{{job="{dcgm_job}",instance=~".*:9400"}}'
    return {
        "vast": vast_selector,
        "vast_errors": (
            f'sum(increase(vastai_exporter_errors_total{{job="{vast_job}"}}[10m])) '
            "or vector(0)"
        ),
        "vast_up": vast_up,
        "vast_age": f"min(timestamp({vast_selector}) or timestamp({vast_up}))",
        "dcgm": dcgm_selector,
        "dcgm_up": dcgm_up,
        "dcgm_age": f"min(timestamp({dcgm_selector}) or timestamp({dcgm_up}))",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise PrometheusError("redirect_rejected")


class PrometheusClient:
    """Run only the fixed capacity query set against one configured origin."""

    def __init__(
        self,
        endpoint: str,
        *,
        max_age_seconds: int = 180,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], datetime] = utc_now,
        opener: object | None = None,
        aggregate_timeout_seconds: float = AGGREGATE_TIMEOUT_SECONDS,
    ):
        self.endpoint = validate_endpoint(endpoint)
        if not 30 <= max_age_seconds <= 900:
            raise PrometheusError("invalid_max_age")
        if not 0.1 <= timeout_seconds <= 10:
            raise PrometheusError("invalid_timeout")
        if not 0.1 <= aggregate_timeout_seconds <= 70:
            raise PrometheusError("invalid_aggregate_timeout")
        if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise PrometheusError("invalid_response_limit")
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = timeout_seconds
        self.aggregate_timeout_seconds = aggregate_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.clock = clock
        # Keep the opener seam for the established offline fixtures. Production
        # calls use StdlibTransport's no-proxy/no-redirect absolute deadline.
        self.opener = opener
        self._transport = None if opener is not None else StdlibTransport()

    def _query(
        self, query_id: str, expression: str, *, deadline: float
    ) -> tuple[Sample, ...]:
        encoded = urllib.parse.urlencode({"query": expression})
        body = self._request_body(
            query_id,
            f"{self.endpoint}/api/v1/query?{encoded}",
            deadline=deadline,
        )
        samples = self._decode(
            query_id, body, allow_empty=query_id in {"vast", "dcgm"}
        )
        self._require_time_remaining(deadline, query_id)
        return samples

    def _request_body(self, query_id: str, url: str, *, deadline: float) -> bytes:
        remaining = self._remaining_timeout(deadline, query_id)
        headers = {"Accept": "application/json"}
        if self.opener is None:
            assert self._transport is not None
            try:
                response = self._transport.request(
                    HttpRequest("GET", url, headers),
                    timeout_seconds=remaining,
                    max_response_bytes=self.max_response_bytes,
                )
            except HttpClientError as error:
                reason = {
                    "http_status": "http_failure",
                    "redirect_rejected": "redirect_rejected",
                    "response_too_large": "response_limit",
                }.get(error.reason, "request_failed")
                raise PrometheusError(reason, query_id) from None
            body = response.body
        else:
            request = urllib.request.Request(
                url,
                headers=headers,
                method="GET",
            )
            try:
                # Preserve the exact historical timeout argument for alert
                # opener fixtures; the post-read deadline check still prevents
                # a fixture result from becoming fresh evidence after expiry.
                opener_timeout = (
                    self.timeout_seconds if query_id == "alerts" else remaining
                )
                with self.opener.open(request, timeout=opener_timeout) as response:
                    if getattr(response, "status", 200) != 200:
                        raise PrometheusError("http_failure", query_id)
                    raw_length = response.headers.get("Content-Length")
                    if (
                        raw_length is not None
                        and int(raw_length) > self.max_response_bytes
                    ):
                        raise PrometheusError("response_limit", query_id)
                    body = response.read(self.max_response_bytes + 1)
            except PrometheusError:
                raise
            except (OSError, ValueError, urllib.error.URLError) as error:
                raise PrometheusError("request_failed", query_id) from error
        self._require_time_remaining(deadline, query_id)
        if len(body) > self.max_response_bytes:
            raise PrometheusError("response_limit", query_id)
        return body

    def _remaining_timeout(self, deadline: float, query_id: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PrometheusError("request_failed", query_id)
        return min(self.timeout_seconds, remaining)

    @staticmethod
    def _require_time_remaining(deadline: float, query_id: str) -> None:
        if time.monotonic() >= deadline:
            raise PrometheusError("request_failed", query_id)

    def fetch_alerts(self) -> AlertSnapshot:
        """Fetch only Prometheus's fixed ``/api/v1/alerts`` endpoint."""
        query_id = "alerts"
        deadline = time.monotonic() + float(self.timeout_seconds)
        body = self._request_body(
            query_id,
            f"{self.endpoint}/api/v1/alerts",
            deadline=deadline,
        )
        alerts = self._decode_alerts(body)
        snapshot = AlertSnapshot(
            observed_at=_aware_now(self.clock, query_id),
            alerts=alerts,
            complete=True,
            max_age_seconds=self.max_age_seconds,
        )
        self._require_time_remaining(deadline, query_id)
        return snapshot

    @staticmethod
    def _decode_alerts(body: bytes) -> tuple[PrometheusAlert, ...]:
        try:
            payload = json.loads(body)
            data = payload["data"]
            items = data["alerts"]
            if (
                payload.get("status") != "success"
                or not isinstance(data, dict)
                or not isinstance(items, list)
                or len(items) > MAX_ALERTS
            ):
                raise ValueError
            return tuple(_decode_alert(item) for item in items)
        except (KeyError, TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
            raise PrometheusError("malformed_response", "alerts") from error

    def _decode(
        self, query_id: str, body: bytes, *, allow_empty: bool = False
    ) -> tuple[Sample, ...]:
        try:
            payload = json.loads(body)
            data = payload["data"]
            result = data["result"]
            if (
                payload.get("status") != "success"
                or data.get("resultType") != "vector"
                or not isinstance(result, list)
                or (not result and not allow_empty)
                or len(result) > MAX_SERIES
            ):
                raise ValueError
            samples = tuple(self._decode_sample(item) for item in result)
        except (KeyError, TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
            raise PrometheusError("malformed_response", query_id) from error

        now = self.clock().astimezone(timezone.utc).timestamp()
        for sample in samples:
            age = now - sample.timestamp
            if age > self.max_age_seconds or age < -FUTURE_SKEW_SECONDS:
                raise PrometheusError("stale_data", query_id)
        return samples

    @staticmethod
    def _decode_sample(item: object) -> Sample:
        if not isinstance(item, dict):
            raise ValueError
        labels = item.get("metric")
        pair = item.get("value")
        if (
            not isinstance(labels, dict)
            or len(labels) > MAX_LABELS
            or not isinstance(pair, list)
            or len(pair) != 2
        ):
            raise ValueError
        clean_labels: dict[str, str] = {}
        for raw_name, raw_value in labels.items():
            if (
                not isinstance(raw_name, str)
                or not isinstance(raw_value, str)
                or not _METRIC_NAME.fullmatch(raw_name)
                or len(raw_value) > MAX_LABEL_CHARS
            ):
                raise ValueError
            clean_labels[raw_name] = raw_value
        timestamp = float(pair[0])
        value = float(pair[1])
        if not math.isfinite(timestamp) or not math.isfinite(value) or timestamp <= 0:
            raise ValueError
        return Sample(clean_labels, timestamp, value)

    def fetch(self, machine_id: str, vast_job: str, dcgm_job: str) -> MetricBatch:
        queries = _fixed_queries(machine_id, vast_job, dcgm_job)
        deadline = time.monotonic() + self.aggregate_timeout_seconds
        results = {
            query_id: self._query(query_id, expression, deadline=deadline)
            for query_id, expression in queries.items()
        }
        self._validate_source_age("vast_age", results["vast_age"])
        self._validate_source_age("dcgm_age", results["dcgm_age"])
        batch = MetricBatch(
            vast=results["vast"],
            vast_errors=results["vast_errors"],
            vast_up=results["vast_up"],
            dcgm=results["dcgm"],
            dcgm_up=results["dcgm_up"],
        )
        self._require_time_remaining(deadline, "aggregate")
        return batch

    def _validate_source_age(self, query_id: str, samples: tuple[Sample, ...]) -> None:
        if len(samples) != 1:
            raise PrometheusError("malformed_response", query_id)
        now = self.clock().astimezone(timezone.utc).timestamp()
        age = now - samples[0].value
        if age > self.max_age_seconds or age < -FUTURE_SKEW_SECONDS:
            raise PrometheusError("stale_data", query_id)


def _decode_alert(item: object) -> PrometheusAlert:
    if not isinstance(item, dict) or len(item) > 16:
        raise ValueError
    labels = _decode_text_map(item.get("labels"), MAX_LABELS, required=True)
    annotations = _decode_text_map(
        item.get("annotations", {}), MAX_ANNOTATIONS, required=False
    )
    raw_state = item.get("state")
    if not isinstance(raw_state, str) or len(raw_state) > 32:
        state = AlertState.UNKNOWN
    elif raw_state == AlertState.FIRING.value:
        state = AlertState.FIRING
    elif raw_state == AlertState.PENDING.value:
        state = AlertState.PENDING
    else:
        state = AlertState.UNKNOWN
    active_at = _optional_alert_time(item.get("activeAt"))
    value = item.get("value")
    if value is not None and (
        not isinstance(value, str) or not value or len(value) > MAX_LABEL_CHARS
    ):
        raise ValueError
    return PrometheusAlert(labels, annotations, state, active_at, value)


def _decode_text_map(
    value: object, limit: int, *, required: bool
) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > limit or (required and not value):
        raise ValueError
    result: dict[str, str] = {}
    for key, text in value.items():
        if (
            not isinstance(key, str)
            or not _METRIC_NAME.fullmatch(key)
            or not isinstance(text, str)
            or len(text) > MAX_LABEL_CHARS
        ):
            raise ValueError
        result[key] = text
    return result


def _optional_alert_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _aware_now(clock: Callable[[], datetime], query_id: str) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PrometheusError("invalid_clock", query_id)
    return value.astimezone(timezone.utc)
