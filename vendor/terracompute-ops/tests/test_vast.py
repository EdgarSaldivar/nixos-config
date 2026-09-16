from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from terracompute_ops.http_client import HttpRequest, HttpResponse
from terracompute_ops.vast import (
    MACHINES_PATH,
    MAX_OFFERS,
    OFFERS_PATH,
    REPORTS_PATH,
    VastClient,
    VastDataError,
    with_market_freshness,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: dict[str, HttpResponse]):
        self.responses = responses
        self.requests: list[HttpRequest] = []

    def request(self, request: HttpRequest, **_kwargs: object) -> HttpResponse:
        self.requests.append(request)
        return self.responses[request.url]


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )


def client_with(responses: dict[str, HttpResponse]) -> tuple[VastClient, FakeTransport]:
    transport = FakeTransport(
        {f"https://console.vast.ai{path}": value for path, value in responses.items()}
    )
    return VastClient("synthetic-machine-read-key", transport=transport, clock=lambda: NOW), transport


class VastClientTests(unittest.TestCase):
    def test_machine_and_reports_use_only_fixed_read_routes(self) -> None:
        client, transport = client_with(
            {
                MACHINES_PATH: response(
                    {
                        "machines": [
                            {"id": 7, "name": "other"},
                            {
                                "id": "17049",
                                "name": "target",
                                "listed": True,
                                "rentable": False,
                                "num_gpus": 8,
                                "gpu_rented": 3,
                            },
                        ]
                    }
                ),
                REPORTS_PATH: response(
                    [
                        {
                            "problem": "self-test",
                            "message": "synthetic failure",
                            "created_at": "2026-09-14T11:00:00Z",
                        }
                    ]
                ),
            }
        )
        machine = client.get_machine()
        reports = client.get_reports()
        self.assertEqual((machine.machine_id, machine.total_gpus, machine.rented_gpus), (17049, 8, 3))
        self.assertTrue(machine.listed)
        self.assertFalse(machine.rentable)
        self.assertTrue(machine.rented)
        self.assertEqual(reports[0].problem, "self-test")
        self.assertEqual([request.method for request in transport.requests], ["GET", "GET"])
        self.assertEqual(
            [request.url for request in transport.requests],
            [
                "https://console.vast.ai/api/v0/machines",
                "https://console.vast.ai/api/v0/machines/17049/reports/",
            ],
        )
        self.assertNotIn("synthetic-machine-read-key", repr(client))
        self.assertNotIn("synthetic-machine-read-key", repr(transport.requests[0]))

    def test_overlapping_offer_slices_are_not_additive(self) -> None:
        offers = [
            {
                "id": 100 + gpu_count,
                "machine_id": 17049,
                "num_gpus": gpu_count,
                "rentable": True,
                "rented": False,
            }
            for gpu_count in (1, 2, 4, 8)
        ]
        client, transport = client_with({OFFERS_PATH: response({"offers": offers})})
        market = client.search_offers()
        self.assertTrue(market.search_complete)
        self.assertTrue(market.advertised)
        self.assertTrue(market.rentable)
        self.assertEqual(market.advertised_gpu_capacity, 8)
        self.assertEqual(market.rentable_gpu_capacity, 8)
        self.assertIsNone(market.launch_proven)
        self.assertIsNone(market.launch_proven_gpu_capacity)
        self.assertIsNone(market.holds)
        request = transport.requests[0]
        self.assertEqual((request.method, request.url), ("POST", "https://console.vast.ai/api/v0/bundles"))
        self.assertEqual(
            json.loads(request.body or b""),
            {"limit": MAX_OFFERS, "machine_id": {"eq": 17049}},
        )

    def test_missing_market_fields_stay_unknown(self) -> None:
        client, _ = client_with(
            {
                OFFERS_PATH: response(
                    {"offers": [{"id": 1, "machine_id": 17049, "num_gpus": 8}]}
                )
            }
        )
        market = client.search_offers()
        self.assertTrue(market.advertised)
        self.assertIsNone(market.rentable)
        self.assertIsNone(market.rentable_gpu_capacity)
        self.assertIsNone(market.holds)

        stale = with_market_freshness(market, now=NOW + timedelta(seconds=181))
        self.assertFalse(stale.search_complete)
        self.assertEqual(stale.error, "stale_observation")
        self.assertIsNone(stale.advertised)
        self.assertIsNone(stale.rentable)
        self.assertIsNone(stale.advertised_gpu_capacity)

    def test_incomplete_and_failed_searches_are_unknown_not_empty(self) -> None:
        full_page = [
            {"id": index, "machine_id": 17049, "num_gpus": 1, "rentable": True}
            for index in range(MAX_OFFERS)
        ]
        client, _ = client_with({OFFERS_PATH: response({"offers": full_page})})
        incomplete = client.search_offers()
        self.assertFalse(incomplete.search_complete)
        self.assertIsNone(incomplete.advertised)
        self.assertIsNone(incomplete.rentable)
        self.assertIsNone(incomplete.advertised_gpu_capacity)

        explicit_client, _ = client_with(
            {
                OFFERS_PATH: response(
                    {
                        "offers": [
                            {"id": 1, "machine_id": 17049, "num_gpus": 1, "rentable": True}
                        ],
                        "total": 2,
                        "next_url": "https://evil.test/do-not-follow",
                    }
                )
            }
        )
        explicit = explicit_client.search_offers()
        self.assertFalse(explicit.search_complete)
        self.assertIsNone(explicit.advertised)

        failed, _ = client_with({OFFERS_PATH: response({"error": "denied"}, 403)})
        unknown = failed.search_offers()
        self.assertFalse(unknown.search_complete)
        self.assertEqual(unknown.error, "http_status")
        self.assertIsNone(unknown.advertised)
        self.assertIsNone(unknown.rentable)
        self.assertEqual(unknown.offers, ())

    def test_wrong_target_offer_and_ambiguous_machine_fail_closed(self) -> None:
        client, _ = client_with(
            {
                OFFERS_PATH: response(
                    {"offers": [{"id": 1, "machine_id": 999, "num_gpus": 8}]}
                )
            }
        )
        market = client.search_offers()
        self.assertEqual(market.error, "target_identity_mismatch")
        self.assertIsNone(market.advertised)

        machine_client, _ = client_with(
            {
                MACHINES_PATH: response(
                    {"machines": [{"id": 17049}, {"id": "17049"}]}
                )
            }
        )
        with self.assertRaisesRegex(VastDataError, "ambiguous_target"):
            machine_client.get_machine()


if __name__ == "__main__":
    unittest.main()
