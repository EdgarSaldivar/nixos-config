"""Certificate-pinned, GET-only Redfish discovery for the approved BMC.

Discovery follows a small catalog of hardware, sensor and log descendants.
Every request shares one aggregate deadline and partial evidence is retained.
"""

from __future__ import annotations

import base64
import math
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .http_client import HttpClientError, HttpTransport, RestrictedHttpClient


REDFISH_HOST = "10.0.15.237"
REDFISH_ORIGIN = f"https://{REDFISH_HOST}"
MAX_DISCOVERY_VISITS = 128
MAX_DISCOVERED_MEMBERS = MAX_DISCOVERY_VISITS - 4
MAX_EVIDENCE_ITEMS = 256
MAX_ERRORS = 64
_ID = r"[A-Za-z0-9_.~-]{1,64}"
_TOP = re.compile(rf"/redfish/v1/(Systems|Chassis|Managers)/({_ID})/?\Z")
_COLLECTION = re.compile(
    rf"/redfish/v1/(Systems|Chassis|Managers)/({_ID})/"
    rf"(Processors|Memory|Storage|Sensors|Drives|LogServices)/?\Z"
)
_COLLECTION_MEMBER = re.compile(
    rf"/redfish/v1/(Systems|Chassis|Managers)/({_ID})/"
    rf"(Processors|Memory|Storage|Sensors|Drives|LogServices)/({_ID})/?\Z"
)
_LOG_ENTRIES = re.compile(
    rf"/redfish/v1/(Systems|Chassis|Managers)/({_ID})/LogServices/({_ID})/Entries/?\Z"
)
_LOG_ENTRY = re.compile(
    rf"/redfish/v1/(Systems|Chassis|Managers)/({_ID})/LogServices/({_ID})/Entries/({_ID})/?\Z"
)
_SINGLETON = re.compile(rf"/redfish/v1/Chassis/({_ID})/(Power|Thermal)/?\Z")
class RedfishDataError(ValueError):
    """Secret-free fixed-category Redfish validation failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RedfishResource(Enum):
    ROOT = "/redfish/v1/"
    SYSTEMS = "/redfish/v1/Systems"
    CHASSIS = "/redfish/v1/Chassis"
    MANAGERS = "/redfish/v1/Managers"


@dataclass(frozen=True)
class HardwareIdentity:
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    part_number: str | None = None
    sku: str | None = None
    asset_tag: str | None = None
    firmware_version: str | None = None
    processor_type: str | None = None
    uuid: str | None = None
    capacity_mib: int | None = None
    total_cores: int | None = None


@dataclass(frozen=True)
class SensorReading:
    kind: str
    member_id: str | None
    name: str | None
    reading: float | None
    units: str | None
    state: str | None
    health: str | None
    reading_celsius: float | None = None
    reading_rpm: float | None = None
    power_consumed_watts: float | None = None
    power_input_watts: float | None = None
    power_output_watts: float | None = None
    line_input_voltage: float | None = None
    lower_threshold: float | None = None
    upper_threshold: float | None = None
    hardware_identity: HardwareIdentity | None = None


@dataclass(frozen=True)
class LogEntry:
    entry_id: str | None
    name: str | None
    created: str | None
    severity: str | None
    message: str | None
    message_id: str | None
    entry_type: str | None
    resolved: bool | None


@dataclass(frozen=True, repr=False)
class ResourceObservation:
    path: str
    resource_id: str | None
    name: str | None
    state: str | None
    health: str | None
    power_state: str | None = None
    hardware_identity: HardwareIdentity | None = None
    power: tuple[SensorReading, ...] = ()
    thermal: tuple[SensorReading, ...] = ()
    sensors: tuple[SensorReading, ...] = ()
    log_entries: tuple[LogEntry, ...] = ()

    def __repr__(self) -> str:
        return f"ResourceObservation(path={self.path!r})"


@dataclass(frozen=True)
class RedfishSnapshot:
    observed_at: datetime
    resources: tuple[ResourceObservation, ...]
    complete: bool
    errors: tuple[str, ...]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RedfishClient:
    """Fixed-host GET-only discovery, independent of controller state."""

    def __init__(
        self,
        username: str,
        password: str,
        cert_sha256: str,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 15,
        max_response_bytes: int = 128 * 1024,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (
            not isinstance(username, str) or not username or ":" in username
            or len(username) > 256 or not isinstance(password, str)
            or not password or len(password) > 4096
        ):
            raise RedfishDataError("invalid_credentials")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = f"Basic {token}"
        self._cert_sha256 = cert_sha256
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._monotonic = monotonic
        self._make_http(RedfishResource.ROOT.value, timeout_seconds)

    def __repr__(self) -> str:
        return f"RedfishClient(host={REDFISH_HOST!r})"

    def get(self, resource: RedfishResource) -> ResourceObservation:
        """Fetch one of the fixed public resource enums; no path strings."""
        if not isinstance(resource, RedfishResource):
            raise RedfishDataError("resource_not_allowed")
        deadline = self._monotonic() + self._timeout_seconds
        return _normalize_resource(resource.value, self._fetch_path(resource.value, deadline))

    def discover(self) -> RedfishSnapshot:
        """Return bounded evidence collected within one aggregate deadline."""
        observed_at = _now(self._clock)
        deadline = self._monotonic() + self._timeout_seconds
        observations: list[ResourceObservation] = []
        errors: list[str] = []
        evidence_items = 0
        visits = 0
        try:
            root_payload = self._fetch_path(RedfishResource.ROOT.value, deadline)
            visits += 1
            observations.append(_normalize_resource(RedfishResource.ROOT.value, root_payload))
            for resource in (RedfishResource.SYSTEMS, RedfishResource.CHASSIS, RedfishResource.MANAGERS):
                root_link = root_payload.get(resource.name.title())
                if root_link is not None and _odata_link(root_link) != resource.value:
                    raise RedfishDataError("unexpected_collection_link")
        except (HttpClientError, RedfishDataError) as error:
            return RedfishSnapshot(observed_at, (), False, (f"root:{error.reason}",))

        pending = [RedfishResource.SYSTEMS.value, RedfishResource.CHASSIS.value, RedfishResource.MANAGERS.value]
        queued = set(pending)
        while pending:
            if visits >= MAX_DISCOVERY_VISITS:
                _append_error(errors, "discovery:visit_limit")
                break
            path = pending.pop(0)
            if self._monotonic() >= deadline:
                _append_error(errors, "discovery:deadline_exceeded")
                break
            try:
                payload = self._fetch_path(path, deadline)
                visits += 1
                observation = _normalize_resource(path, payload)
                added = _evidence_count(observation)
                if evidence_items + added > MAX_EVIDENCE_ITEMS:
                    raise RedfishDataError("evidence_limit")
                observations.append(observation)
                evidence_items += added
                for linked_path in _discovery_links(path, payload):
                    if linked_path not in queued:
                        if len(queued) >= MAX_DISCOVERY_VISITS:
                            raise RedfishDataError("visit_limit")
                        queued.add(linked_path)
                        pending.append(linked_path)
            except (HttpClientError, RedfishDataError) as error:
                _append_error(errors, f"{_error_scope(path)}:{error.reason}")
                if error.reason in {"deadline_exceeded", "evidence_limit", "visit_limit"}:
                    break
        return RedfishSnapshot(observed_at, tuple(observations), not errors and not pending, tuple(errors))

    def _fetch_path(self, path: str, deadline: float) -> dict[str, object]:
        path = _allowed_resource_path(path)
        remaining = deadline - self._monotonic()
        if remaining < 0.1:
            raise HttpClientError("deadline_exceeded")
        payload = self._make_http(path, min(self._timeout_seconds, remaining)).request_json(
            "GET", path, headers={"Authorization": self._authorization}, deadline=deadline
        )
        if self._monotonic() >= deadline:
            raise HttpClientError("deadline_exceeded")
        if not isinstance(payload, dict) or len(payload) > 128:
            raise RedfishDataError("malformed_resource")
        return payload

    def _make_http(self, path: str, timeout_seconds: float) -> RestrictedHttpClient:
        return RestrictedHttpClient(
            REDFISH_ORIGIN, {path: frozenset({"GET"})}, transport=self._transport,
            timeout_seconds=timeout_seconds, max_response_bytes=self._max_response_bytes,
            max_concurrency=1, cert_sha256=self._cert_sha256, monotonic=self._monotonic,
        )


def _allowed_resource_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise RedfishDataError("resource_path_not_allowed")
    if value in {resource.value for resource in RedfishResource}:
        return value
    collection = _COLLECTION.fullmatch(value) or _COLLECTION_MEMBER.fullmatch(value)
    if collection:
        allowed = {
            "Systems": {"Processors", "Memory", "Storage", "LogServices"},
            "Chassis": {"Sensors", "Drives", "LogServices"},
            "Managers": {"LogServices"},
        }
        if collection.group(3) not in allowed[collection.group(1)]:
            raise RedfishDataError("resource_path_not_allowed")
        return value.rstrip("/")
    if any(pattern.fullmatch(value) for pattern in (_TOP, _LOG_ENTRIES, _LOG_ENTRY, _SINGLETON)):
        return value.rstrip("/")
    raise RedfishDataError("resource_path_not_allowed")


def _odata_link(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"@odata.id"}:
        raise RedfishDataError("malformed_odata_link")
    link = value.get("@odata.id")
    if not isinstance(link, str) or len(link) > 512:
        raise RedfishDataError("malformed_odata_link")
    parsed = urllib.parse.urlsplit(link)
    if parsed.scheme or parsed.netloc:
        try:
            same_origin = parsed.scheme == "https" and parsed.hostname == REDFISH_HOST and parsed.port in {None, 443} and not parsed.query and not parsed.fragment
        except ValueError:
            raise RedfishDataError("cross_origin_link") from None
        if not same_origin:
            raise RedfishDataError("cross_origin_link")
        link = parsed.path
    elif parsed.query or parsed.fragment or parsed.path != link:
        raise RedfishDataError("resource_path_not_allowed")
    return _allowed_resource_path(link)


def _discovery_links(path: str, payload: dict[str, object]) -> tuple[str, ...]:
    if path in {RedfishResource.SYSTEMS.value, RedfishResource.CHASSIS.value, RedfishResource.MANAGERS.value} or _COLLECTION.fullmatch(path) or _LOG_ENTRIES.fullmatch(path):
        return _collection_members(path, payload)
    match = _TOP.fullmatch(path)
    if match:
        keys = {
            "Systems": ("Processors", "Memory", "Storage", "LogServices"),
            "Chassis": ("Power", "Thermal", "Sensors", "Drives", "LogServices"),
            "Managers": ("LogServices",),
        }[match.group(1)]
        return _named_links(payload, keys)
    member = _COLLECTION_MEMBER.fullmatch(path)
    if member and member.group(3) == "LogServices":
        return _named_links(payload, ("Entries",))
    if member and member.group(3) == "Storage":
        links = _many_links(payload.get("Drives"))
        if any("/Drives/" not in link for link in links):
            raise RedfishDataError("descendant_path_not_allowed")
        return links
    return ()


def _collection_members(path: str, payload: dict[str, object]) -> tuple[str, ...]:
    members = payload.get("Members")
    if not isinstance(members, list) or len(members) > MAX_DISCOVERED_MEMBERS:
        raise RedfishDataError("malformed_members")
    links = tuple(_odata_link(member) for member in members)
    count = payload.get("Members@odata.count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < len(members)):
        raise RedfishDataError("malformed_member_count")
    if payload.get("Members@odata.nextLink") is not None or (isinstance(count, int) and count > len(members)):
        raise RedfishDataError("collection_incomplete")
    if any(not _is_direct_member(path, link) for link in links):
        raise RedfishDataError("member_path_not_allowed")
    return links


def _is_direct_member(collection: str, member: str) -> bool:
    prefix = collection.rstrip("/") + "/"
    return member.startswith(prefix) and "/" not in member[len(prefix):]


def _named_links(payload: dict[str, object], keys: tuple[str, ...]) -> tuple[str, ...]:
    links: list[str] = []
    for key in keys:
        if key in payload:
            found = _many_links(payload[key])
            if any(not link.rstrip("/").endswith("/" + key) for link in found):
                raise RedfishDataError("descendant_path_not_allowed")
            links.extend(found)
    return tuple(links)


def _many_links(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        if len(value) > MAX_DISCOVERED_MEMBERS:
            raise RedfishDataError("discovery_limit")
        return tuple(_odata_link(item) for item in value)
    return (_odata_link(value),)


def _normalize_resource(path: str, payload: dict[str, object]) -> ResourceObservation:
    state, health = _status(payload.get("Status"))
    power: list[SensorReading] = []
    thermal: list[SensorReading] = []
    sensors: list[SensorReading] = []
    if _SINGLETON.fullmatch(path):
        if path.endswith("/Thermal"):
            thermal.extend(_sensor_array(payload, "Temperatures", "temperature"))
            thermal.extend(_sensor_array(payload, "Fans", "fan"))
        else:
            power.extend(_sensor_array(payload, "PowerControl", "power_control"))
            power.extend(_sensor_array(payload, "Voltages", "voltage"))
            power.extend(_sensor_array(payload, "PowerSupplies", "power_supply"))
    member = _COLLECTION_MEMBER.fullmatch(path)
    if member and member.group(3) == "Sensors":
        sensors.append(_sensor(payload, "sensor"))
    return ResourceObservation(
        path=path, resource_id=_optional_text(payload.get("Id"), 128),
        name=_optional_text(payload.get("Name"), 256), state=state, health=health,
        power_state=_optional_text(payload.get("PowerState"), 64),
        hardware_identity=_hardware_identity(payload), power=tuple(power),
        thermal=tuple(thermal), sensors=tuple(sensors),
        log_entries=(_log_entry(payload),) if _LOG_ENTRY.fullmatch(path) else (),
    )


def _hardware_identity(payload: dict[str, object]) -> HardwareIdentity | None:
    identity = HardwareIdentity(
        manufacturer=_optional_identity_text(payload.get("Manufacturer"), 256),
        model=_optional_identity_text(payload.get("Model"), 256),
        serial_number=_optional_identity_text(payload.get("SerialNumber"), 256),
        part_number=_optional_identity_text(payload.get("PartNumber"), 256),
        sku=_optional_identity_text(payload.get("SKU"), 256),
        asset_tag=_optional_identity_text(payload.get("AssetTag"), 256),
        firmware_version=_optional_identity_text(payload.get("FirmwareVersion"), 256),
        processor_type=_optional_identity_text(payload.get("ProcessorType"), 256),
        uuid=_optional_identity_text(payload.get("UUID"), 128),
        capacity_mib=_optional_int(payload.get("CapacityMiB")),
        total_cores=_optional_int(payload.get("TotalCores")),
    )
    return identity if any(value is not None for value in identity.__dict__.values()) else None


def _sensor_array(payload: dict[str, object], key: str, kind: str) -> tuple[SensorReading, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise RedfishDataError("malformed_sensor_data")
    return tuple(_sensor(item, kind) for item in value if _nonblank_sensor(item))


def _nonblank_sensor(value: object) -> bool:
    if not isinstance(value, dict):
        raise RedfishDataError("malformed_sensor_data")
    keys = {"MemberId", "Name", "Reading", "ReadingCelsius", "ReadingRPM", "PowerConsumedWatts", "PowerInputWatts", "PowerOutputWatts", "LineInputVoltage", "Manufacturer", "Model", "SerialNumber", "PartNumber"}
    return any(value.get(key) is not None and value.get(key) != "" for key in keys)


def _sensor(payload: object, kind: str) -> SensorReading:
    if not isinstance(payload, dict) or len(payload) > 64:
        raise RedfishDataError("malformed_sensor_data")
    state, health = _status(payload.get("Status"))
    return SensorReading(
        kind, _optional_text(payload.get("MemberId", payload.get("Id")), 128),
        _optional_text(payload.get("Name"), 256), _optional_number(payload.get("Reading")),
        _optional_text(payload.get("ReadingUnits"), 64), state, health,
        reading_celsius=_optional_number(payload.get("ReadingCelsius")),
        reading_rpm=_optional_number(payload.get("ReadingRPM")),
        power_consumed_watts=_optional_number(payload.get("PowerConsumedWatts")),
        power_input_watts=_optional_number(payload.get("PowerInputWatts")),
        power_output_watts=_optional_number(payload.get("PowerOutputWatts")),
        line_input_voltage=_optional_number(payload.get("LineInputVoltage")),
        lower_threshold=_first_number(payload, ("LowerThresholdCritical", "LowerThresholdFatal")),
        upper_threshold=_first_number(payload, ("UpperThresholdCritical", "UpperThresholdFatal")),
        hardware_identity=_hardware_identity(payload),
    )


def _log_entry(payload: dict[str, object]) -> LogEntry:
    resolved = payload.get("Resolved")
    if resolved is not None and not isinstance(resolved, bool):
        raise RedfishDataError("malformed_log_entry")
    return LogEntry(
        _optional_text(payload.get("Id"), 128), _optional_text(payload.get("Name"), 256),
        _optional_text(payload.get("Created"), 128), _optional_text(payload.get("Severity"), 64),
        _optional_text(payload.get("Message"), 1024), _optional_text(payload.get("MessageId"), 256),
        _optional_text(payload.get("EntryType"), 64), resolved,
    )


def _status(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or len(value) > 16:
        raise RedfishDataError("malformed_status")
    return _optional_text(value.get("State"), 64), _optional_text(value.get("Health", value.get("HealthRollup")), 64)


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise RedfishDataError("malformed_text")
    return value


def _optional_identity_text(value: object, limit: int) -> str | None:
    if value is None or value == "":
        return None
    return _optional_text(value, limit)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RedfishDataError("malformed_hardware_identity")
    return value


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RedfishDataError("malformed_sensor_data")
    result = float(value)
    if not math.isfinite(result):
        raise RedfishDataError("malformed_sensor_data")
    return result


def _first_number(payload: dict[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if payload.get(key) is not None:
            return _optional_number(payload[key])
    return None


def _evidence_count(observation: ResourceObservation) -> int:
    return (
        (1 if observation.hardware_identity is not None else 0)
        + len(observation.power)
        + len(observation.thermal)
        + len(observation.sensors)
        + len(observation.log_entries)
    )


def _error_scope(path: str) -> str:
    if path in {resource.value for resource in RedfishResource}:
        return path.rsplit("/", 1)[-1].lower()
    return "descendant"


def _append_error(errors: list[str], value: str) -> None:
    if len(errors) < MAX_ERRORS and value not in errors:
        errors.append(value)


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RedfishDataError("invalid_clock")
    return value.astimezone(timezone.utc)
