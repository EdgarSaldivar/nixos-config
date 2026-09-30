from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone

from terracompute_ops.http_client import HttpClientError, HttpRequest, HttpResponse, StdlibTransport
from terracompute_ops.redfish import (
    REDFISH_ORIGIN,
    RedfishClient,
    RedfishDataError,
    RedfishResource,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
PIN = hashlib.sha256(b"synthetic-der").hexdigest()


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )


class FakeTransport:
    def __init__(self, responses: dict[str, HttpResponse]):
        self.responses = responses
        self.requests: list[tuple[HttpRequest, str | None]] = []

    def request(
        self, request: HttpRequest, *, cert_sha256: str | None = None, **_kwargs: object
    ) -> HttpResponse:
        self.requests.append((request, cert_sha256))
        return self.responses[request.url]


class AdvancingTransport(FakeTransport):
    def __init__(self, responses: dict[str, HttpResponse], clock: list[float]):
        super().__init__(responses)
        self.clock = clock

    def request(
        self, request: HttpRequest, *, cert_sha256: str | None = None, **_kwargs: object
    ) -> HttpResponse:
        self.clock[0] += 4
        return super().request(request, cert_sha256=cert_sha256)


class FakeSocket:
    def __init__(self, events: list[str]):
        self.events = events

    def getpeercert(self, *, binary_form: bool) -> bytes:
        self.events.append("certificate")
        return b"wrong-der"


class PinMismatchConnection:
    def __init__(self, events: list[str]):
        self.events = events
        self.sock = FakeSocket(events)

    def connect(self) -> None:
        self.events.append("connect")

    def request(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("authorization-sent")

    def close(self) -> None:
        self.events.append("close")


class RedfishClientTests(unittest.TestCase):
    def test_fixed_discovery_normalizes_health_and_uses_get_only(self) -> None:
        responses = {
            f"{REDFISH_ORIGIN}/redfish/v1/": response(
                {
                    "Id": "Root",
                    "Name": "Service Root",
                    "Systems": {"@odata.id": "/redfish/v1/Systems"},
                    "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                    "Managers": {"@odata.id": "/redfish/v1/Managers"},
                }
            ),
            f"{REDFISH_ORIGIN}/redfish/v1/Systems": response(
                {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}
            ),
            f"{REDFISH_ORIGIN}/redfish/v1/Chassis": response({"Members": []}),
            f"{REDFISH_ORIGIN}/redfish/v1/Managers": response({"Members": []}),
            f"{REDFISH_ORIGIN}/redfish/v1/Systems/1": response(
                {
                    "Id": "1",
                    "Name": "Synthetic system",
                    "Status": {"State": "Enabled", "Health": "OK"},
                }
            ),
        }
        transport = FakeTransport(responses)
        client = RedfishClient(
            "synthetic-user",
            "synthetic-password",
            PIN,
            transport=transport,
            clock=lambda: NOW,
        )
        snapshot = client.discover()
        self.assertTrue(snapshot.complete)
        system = next(item for item in snapshot.resources if item.path.endswith("/Systems/1"))
        self.assertEqual((system.state, system.health), ("Enabled", "OK"))
        self.assertTrue(all(request.method == "GET" for request, _pin in transport.requests))
        self.assertTrue(all(request.url.startswith(REDFISH_ORIGIN + "/redfish/v1") for request, _pin in transport.requests))
        self.assertTrue(all(pin == PIN for _request, pin in transport.requests))
        self.assertNotIn("synthetic-user", repr(client))
        self.assertNotIn("synthetic-password", repr(client))
        self.assertNotIn("synthetic-password", repr(transport.requests[0][0]))

    def test_public_get_rejects_caller_path_and_redirect(self) -> None:
        transport = FakeTransport(
            {
                f"{REDFISH_ORIGIN}/redfish/v1/": HttpResponse(
                    302, {"Location": "https://evil.test/redfish/v1/"}, b""
                )
            }
        )
        client = RedfishClient("user", "password", PIN, transport=transport)
        with self.assertRaisesRegex(RedfishDataError, "resource_not_allowed"):
            client.get("/redfish/v1/Systems" )  # type: ignore[arg-type]
        with self.assertRaisesRegex(HttpClientError, "redirect_rejected"):
            client.get(RedfishResource.ROOT)
        self.assertEqual(len(transport.requests), 1)

    def test_cross_origin_and_non_allowlisted_discovery_links_are_not_fetched(self) -> None:
        transport = FakeTransport(
            {
                f"{REDFISH_ORIGIN}/redfish/v1/": response({}),
                f"{REDFISH_ORIGIN}/redfish/v1/Systems": response(
                    {"Members": [{"@odata.id": "https://evil.test/redfish/v1/Systems/1"}]}
                ),
                f"{REDFISH_ORIGIN}/redfish/v1/Chassis": response({"Members": []}),
                f"{REDFISH_ORIGIN}/redfish/v1/Managers": response({"Members": []}),
            }
        )
        client = RedfishClient("user", "password", PIN, transport=transport, clock=lambda: NOW)
        snapshot = client.discover()
        self.assertFalse(snapshot.complete)
        self.assertIn("systems:cross_origin_link", snapshot.errors)
        self.assertFalse(any("evil.test" in request.url for request, _pin in transport.requests))

    def test_paginated_collection_is_partial_and_next_url_is_not_followed(self) -> None:
        transport = FakeTransport(
            {
                f"{REDFISH_ORIGIN}/redfish/v1/": response({}),
                f"{REDFISH_ORIGIN}/redfish/v1/Systems": response(
                    {
                        "Members": [],
                        "Members@odata.count": 1,
                        "Members@odata.nextLink": "https://evil.test/page-2",
                    }
                ),
                f"{REDFISH_ORIGIN}/redfish/v1/Chassis": response({"Members": []}),
                f"{REDFISH_ORIGIN}/redfish/v1/Managers": response({"Members": []}),
            }
        )
        client = RedfishClient("user", "password", PIN, transport=transport, clock=lambda: NOW)
        snapshot = client.discover()
        self.assertFalse(snapshot.complete)
        self.assertIn("systems:collection_incomplete", snapshot.errors)
        self.assertFalse(any("evil.test" in request.url for request, _pin in transport.requests))

    def test_pin_mismatch_sends_no_basic_authorization(self) -> None:
        events: list[str] = []
        connection = PinMismatchConnection(events)
        transport = StdlibTransport(connection_factory=lambda *_args, **_kwargs: connection)
        client = RedfishClient("synthetic-user", "synthetic-password", PIN, transport=transport)
        with self.assertRaisesRegex(HttpClientError, "certificate_pin_mismatch"):
            client.get(RedfishResource.ROOT)
        self.assertEqual(events, ["connect", "certificate", "close"])

    def test_descendant_catalog_preserves_typed_hardware_sensor_and_log_evidence(self) -> None:
        base = f"{REDFISH_ORIGIN}/redfish/v1"
        responses = {
            f"{base}/": response({}),
            f"{base}/Systems": response({"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}),
            f"{base}/Chassis": response({"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]}),
            f"{base}/Managers": response({"Members": []}),
            f"{base}/Systems/1": response({
                "Id": "1", "Manufacturer": "Synthetic", "Model": "Host", "SerialNumber": "SYS-1",
                "PowerState": "On", "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
            }),
            f"{base}/Chassis/1": response({
                "Id": "1", "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"},
                "Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
                "Sensors": {"@odata.id": "/redfish/v1/Chassis/1/Sensors"},
                "LogServices": {"@odata.id": "/redfish/v1/Chassis/1/LogServices"},
            }),
            f"{base}/Systems/1/Processors": response({"Members": [{"@odata.id": "/redfish/v1/Systems/1/Processors/CPU1"}]}),
            f"{base}/Systems/1/Processors/CPU1": response({"Id": "CPU1", "Model": "Synthetic CPU", "TotalCores": 64, "Status": {"Health": "OK"}}),
            f"{base}/Chassis/1/Power": response({"PowerSupplies": [{"MemberId": "PSU1", "Name": "Unmapped PSU record", "Model": "", "SerialNumber": "", "PowerInputWatts": None, "Status": {"Health": "OK"}}]}),
            f"{base}/Chassis/1/Thermal": response({"Temperatures": [{"MemberId": "T1", "Name": "CPU temp", "ReadingCelsius": 42, "Status": {"Health": "OK"}}]}),
            f"{base}/Chassis/1/Sensors": response({"Members": [{"@odata.id": "/redfish/v1/Chassis/1/Sensors/Fan1"}]}),
            f"{base}/Chassis/1/Sensors/Fan1": response({"Id": "Fan1", "Name": "Fan 1", "Reading": 5000, "ReadingUnits": "RPM", "Status": {"State": "Enabled", "Health": "OK"}}),
            f"{base}/Chassis/1/LogServices": response({"Members": [{"@odata.id": "/redfish/v1/Chassis/1/LogServices/SEL"}]}),
            f"{base}/Chassis/1/LogServices/SEL": response({"Id": "SEL", "Entries": {"@odata.id": "/redfish/v1/Chassis/1/LogServices/SEL/Entries"}}),
            f"{base}/Chassis/1/LogServices/SEL/Entries": response({"Members": [{"@odata.id": "/redfish/v1/Chassis/1/LogServices/SEL/Entries/7"}]}),
            f"{base}/Chassis/1/LogServices/SEL/Entries/7": response({"Id": "7", "Severity": "Warning", "Message": "Synthetic event", "Created": "2026-09-14T11:59:00Z"}),
        }
        snapshot = RedfishClient("user", "password", PIN, transport=FakeTransport(responses), clock=lambda: NOW).discover()
        self.assertTrue(snapshot.complete)
        system = next(item for item in snapshot.resources if item.path.endswith("/Systems/1"))
        self.assertEqual((system.power_state, system.hardware_identity.serial_number), ("On", "SYS-1"))
        processor = next(item for item in snapshot.resources if item.path.endswith("/Processors/CPU1"))
        self.assertEqual(processor.hardware_identity.total_cores, 64)
        power = next(item for item in snapshot.resources if item.path.endswith("/Power"))
        self.assertEqual(power.power[0].kind, "power_supply")
        self.assertIsNone(power.power[0].hardware_identity)
        thermal = next(item for item in snapshot.resources if item.path.endswith("/Thermal"))
        self.assertEqual(thermal.thermal[0].reading_celsius, 42)
        sensor = next(item for item in snapshot.resources if item.path.endswith("/Sensors/Fan1"))
        self.assertEqual((sensor.sensors[0].reading, sensor.sensors[0].units), (5000, "RPM"))
        entry = next(item for item in snapshot.resources if item.path.endswith("/Entries/7"))
        self.assertEqual(entry.log_entries[0].message, "Synthetic event")

    def test_descendants_reject_sensitive_and_cross_origin_links(self) -> None:
        base = f"{REDFISH_ORIGIN}/redfish/v1"
        transport = FakeTransport({
            f"{base}/": response({}),
            f"{base}/Systems": response({"Members": [
                {"@odata.id": "/redfish/v1/Systems/1"},
                {"@odata.id": "/redfish/v1/Systems/2"},
            ]}),
            f"{base}/Chassis": response({"Members": []}),
            f"{base}/Managers": response({"Members": []}),
            f"{base}/Systems/1": response({
                "Accounts": {"@odata.id": "/redfish/v1/Managers/1/Accounts"},
            }),
            f"{base}/Systems/2": response({
                "Processors": {"@odata.id": "https://evil.test/redfish/v1/Systems/2/Processors"},
            }),
        })
        snapshot = RedfishClient("user", "password", PIN, transport=transport, clock=lambda: NOW).discover()
        self.assertFalse(snapshot.complete)
        self.assertIn("descendant:cross_origin_link", snapshot.errors)
        self.assertFalse(any("evil.test" in request.url for request, _pin in transport.requests))
        self.assertFalse(any("Accounts" in request.url for request, _pin in transport.requests))

    def asrock_responses(self) -> dict[str, HttpResponse]:
        """Shapes captured from the 17049 ASRock BMC on 2026-09-30."""
        base = f"{REDFISH_ORIGIN}/redfish/v1"
        sel = "/redfish/v1/Managers/1/LogServices/SEL/Entries"
        entries = [
            {
                "@odata.id": f"{sel}/{index}",
                "@odata.type": "#LogEntry.v1_4_3.LogEntry",
                "Id": str(index),
                "Created": f"2026-09-{index:02d}T00:00:00+00:00",
                "Severity": "OK",
                "Message": f"event {index}",
            }
            for index in range(1, 21)
        ]
        entries.append({"@odata.id": f"{sel}/99", "Id": "99", "Resolved": "not-a-bool"})
        return {
            f"{base}/": response({}),
            f"{base}/Systems": response({"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}),
            f"{base}/Chassis": response({"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]}),
            f"{base}/Managers": response({"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]}),
            f"{base}/Systems/1": response({
                "Id": "1", "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
            }),
            f"{base}/Systems/1/Storage": response(
                {"Members": [{"@odata.id": "/redfish/v1/Systems/1/Storage/StorageUnit_0"}]}
            ),
            f"{base}/Systems/1/Storage/StorageUnit_0": response({
                "Id": "StorageUnit_0",
                "Drives": [{"@odata.id": "/redfish/v1/Systems/1/Storage/StorageUnit_0/Drives/NVMe0"}],
            }),
            f"{base}/Systems/1/Storage/StorageUnit_0/Drives/NVMe0": response({
                "Id": "NVMe0", "Model": "Synthetic NVMe", "Status": {"State": "Enabled", "Health": "OK"},
            }),
            f"{base}/Chassis/1": response({"Id": "1", "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"}}),
            f"{base}/Chassis/1/Power": response({"PowerSupplies": [{
                "MemberId": "0", "Name": "PSU1", "Model": "", "LineInputVoltage": "0.00V",
                "PowerInputWatts": None, "Status": {"Health": "N/A", "State": "Absent"},
            }]}),
            f"{base}/Managers/1": response(
                {"Id": "1", "LogServices": {"@odata.id": "/redfish/v1/Managers/1/LogServices"}}
            ),
            f"{base}/Managers/1/LogServices": response(
                {"Members": [{"@odata.id": "/redfish/v1/Managers/1/LogServices/SEL"}]}
            ),
            f"{base}/Managers/1/LogServices/SEL": response({"Id": "SEL", "Entries": {"@odata.id": sel}}),
            f"{REDFISH_ORIGIN}{sel}": response({"Members": entries, "Members@odata.count": len(entries)}),
        }

    def test_asrock_absent_psu_drives_and_inline_logs_complete_discovery(self) -> None:
        transport = FakeTransport(self.asrock_responses())
        snapshot = RedfishClient(
            "user", "password", PIN, transport=transport, clock=lambda: NOW
        ).discover()
        self.assertTrue(snapshot.complete, snapshot.errors)

        power = next(item for item in snapshot.resources if item.path.endswith("/Power"))
        psu = power.power[0]
        self.assertEqual((psu.name, psu.state, psu.health), ("PSU1", "Absent", None))
        self.assertIsNone(psu.line_input_voltage)

        drive = next(item for item in snapshot.resources if item.path.endswith("/Drives/NVMe0"))
        self.assertEqual(drive.hardware_identity.model, "Synthetic NVMe")

        log = next(item for item in snapshot.resources if item.path.endswith("/SEL/Entries"))
        self.assertEqual(len(log.log_entries), 16)
        self.assertEqual(log.log_entries[0].message, "event 20")
        self.assertFalse(any("/Entries/" in request.url for request, _pin in transport.requests))

    def test_present_component_with_a_text_reading_is_still_malformed(self) -> None:
        responses = self.asrock_responses()
        responses[f"{REDFISH_ORIGIN}/redfish/v1/Chassis/1/Power"] = response({"PowerSupplies": [{
            "MemberId": "0", "Name": "PSU1", "LineInputVoltage": "230V",
            "Status": {"Health": "OK", "State": "Enabled"},
        }]})
        snapshot = RedfishClient(
            "user", "password", PIN, transport=FakeTransport(responses), clock=lambda: NOW
        ).discover()
        self.assertFalse(snapshot.complete)
        self.assertIn("descendant:malformed_sensor_data", snapshot.errors)

    def test_discovery_budget_is_separate_from_each_request_timeout(self) -> None:
        base = f"{REDFISH_ORIGIN}/redfish/v1"
        clock = [0.0]
        transport = AdvancingTransport({
            f"{base}/": response({}),
            f"{base}/Systems": response({"Members": []}),
            f"{base}/Chassis": response({"Members": []}),
            f"{base}/Managers": response({"Members": []}),
        }, clock)
        snapshot = RedfishClient(
            "user", "password", PIN, transport=transport, clock=lambda: NOW,
            timeout_seconds=15, discovery_seconds=30, monotonic=lambda: clock[0],
        ).discover()
        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertEqual(len(transport.requests), 4)
        for invalid in (10, 121):
            with self.subTest(discovery_seconds=invalid), self.assertRaisesRegex(
                RedfishDataError, "invalid_discovery_deadline"
            ):
                RedfishClient("user", "password", PIN, timeout_seconds=15,
                              discovery_seconds=invalid)

    def test_single_discovery_deadline_preserves_accumulated_data(self) -> None:
        base = f"{REDFISH_ORIGIN}/redfish/v1"
        clock = [0.0]
        transport = AdvancingTransport({
            f"{base}/": response({}),
            f"{base}/Systems": response({"Members": []}),
            f"{base}/Chassis": response({"Members": []}),
            f"{base}/Managers": response({"Members": []}),
        }, clock)
        snapshot = RedfishClient(
            "user", "password", PIN, transport=transport, clock=lambda: NOW,
            timeout_seconds=15, monotonic=lambda: clock[0],
        ).discover()
        self.assertFalse(snapshot.complete)
        self.assertEqual([item.path for item in snapshot.resources], [
            "/redfish/v1/", "/redfish/v1/Systems", "/redfish/v1/Chassis",
        ])
        self.assertIn("managers:deadline_exceeded", snapshot.errors)
        self.assertEqual(len(transport.requests), 4)


if __name__ == "__main__":
    unittest.main()
