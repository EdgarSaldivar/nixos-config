from __future__ import annotations

import io
import json
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from terracompute_ops.capacity import (
    MarketAssessment,
    assess_market,
    capacity_evaluation_complete,
    merge_events,
    metric_batch_fresh,
    prometheus_failure_event,
    reconcile_capacity,
    reconcile_market,
)
from terracompute_ops.incidents import classify, stable_signature
from terracompute_ops.prometheus import (
    AlertFreshness,
    AlertState,
    MAX_RESPONSE_BYTES,
    MetricBatch,
    PrometheusClient,
    PrometheusError,
    Sample,
    validate_endpoint,
)
from terracompute_ops.vast import (
    MachineObservation,
    MarketObservation,
    OfferSlice,
    VastSnapshot,
)


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
STAMP = NOW.timestamp()


def uuid(index: int) -> str:
    return f"GPU-00000000-0000-4000-8000-{index:012x}"


def sample(name: str | None, value: float, **labels: str) -> Sample:
    if name is not None:
        labels["__name__"] = name
    return Sample(labels, STAMP, value)


def target_probe(*, physical: int = 8, visible: int = 8, vfio: int = 0) -> dict:
    return {
        "target": "terracompute.example",
        "machine_id": 17049,
        "boot_id": "11111111-2222-4333-8444-555555555555",
        "observed_at": "2026-09-14T12:00:00Z",
        "healthy": True,
        "events": [],
        "snapshot": {
            "gpu": {
                "expected_count": 8,
                "pci_count": physical,
                "nvidia_count": visible,
                "vfio_count": vfio,
                "gpus": [
                    {"uuid": uuid(index), "pci_bdf": f"0000:{index:02x}:00.0"}
                    for index in range(visible)
                ],
                "pci_devices": [
                    {
                        "pci_bdf": f"0000:{index:02x}:00.0",
                        "driver": (
                            "nvidia"
                            if index < visible
                            else "vfio-pci"
                            if index < visible + vfio
                            else "unbound"
                        ),
                    }
                    for index in range(physical)
                ],
            }
        },
    }


def metric_batch(
    *,
    total: int = 8,
    rented: int = 3,
    idle: int | None = None,
    visible: int = 8,
    occupancy: bool = True,
    listed: int = 1,
    verified: int = 1,
    vast_up: int = 1,
    dcgm_up: int = 1,
    dcgm_uuids: list[str] | None = None,
    util: float = 0,
) -> MetricBatch:
    vast = [
        sample(
            "vast_machine_hostname",
            1,
            machine_id="17049",
            hostname="terracompute.example",
        ),
        sample("vast_machine_num_gpus", total, machine_id="17049"),
        sample("vastai_machine_gpu_rented_on_demand", rented, machine_id="17049"),
        sample("vastai_machine_gpu_rented_bid_demand", 0, machine_id="17049"),
        sample("vastai_machine_gpu_rented_on_reserved", 0, machine_id="17049"),
        sample("vast_machine_Listed", listed, machine_id="17049"),
        sample("vast_machine_Verification", verified, machine_id="17049"),
        sample(
            "vastai_machine_ErrorDescription",
            1,
            machine_id="17049",
            error_description="",
        ),
    ]
    if idle is not None:
        vast.append(sample("vastai_machine_gpu_idle", idle, machine_id="17049"))
    else:
        vast.append(
            sample("vastai_machine_gpu_idle", total - rented, machine_id="17049")
        )
    if occupancy:
        for index in range(total):
            vast.append(
                sample(
                    "vastai_machine_gpu_occupancy",
                    2 if index < rented else 0,
                    machine_id="17049",
                    gpu=str(index),
                )
            )
    identities = dcgm_uuids if dcgm_uuids is not None else [uuid(i) for i in range(visible)]
    dcgm = []
    for identity in identities:
        dcgm.append(sample("DCGM_FI_DEV_GPU_UTIL", util, UUID=identity))
        dcgm.append(sample("DCGM_FI_DEV_FB_USED", 100, UUID=identity))
    return MetricBatch(
        vast=tuple(vast),
        vast_errors=(sample(None, 0),),
        vast_up=(sample("up", vast_up, job="vastai-exporter"),),
        dcgm=tuple(dcgm),
        dcgm_up=(sample("up", dcgm_up, job="dcgm-exporter"),),
    )


def market_snapshot(
    *,
    observed_at: datetime = NOW,
    complete: bool = True,
    offers: tuple[OfferSlice, ...] = (
        OfferSlice(101, 1, False, False),
        OfferSlice(102, 4, False, False),
    ),
    rentable: bool | None = False,
    total_gpus: int | None = 8,
    rented_gpus: int | None = 3,
) -> VastSnapshot:
    return VastSnapshot(
        observed_at,
        MachineObservation(
            17049,
            observed_at,
            "terracompute",
            True,
            True,
            False,
            total_gpus,
            rented_gpus,
        ),
        (),
        MarketObservation(
            17049,
            observed_at,
            complete,
            offers,
            True if complete else None,
            rentable if complete else None,
            None,
            max((offer.gpu_count for offer in offers), default=0) if complete else None,
            0 if complete and rentable is False else None,
            None,
            None,
            None if complete else "search_incomplete",
        ),
        (),
    )


class CapacityReconciliationTests(unittest.TestCase):
    def test_merge_marks_clean_probe_unhealthy_without_hiding_target_contradictions(self) -> None:
        capacity_event = {
            "fault_family": "capacity",
            "code": "vast_scrape_down",
            "message": "down",
        }
        clean = merge_events(target_probe(), [capacity_event])
        self.assertFalse(clean["healthy"])

        contradictory_probe = target_probe()
        contradictory_probe["events"] = [
            {"fault_family": "xid", "code": 43, "message": "target event"}
        ]
        contradictory = merge_events(contradictory_probe, [capacity_event])
        self.assertTrue(contradictory["healthy"])

    def test_matching_sources_are_healthy_even_when_rented_gpus_have_low_utilization(self) -> None:
        events = reconcile_capacity(target_probe(), metric_batch(util=0), now=NOW)
        self.assertEqual(events, [])

        vfio_events = reconcile_capacity(
            target_probe(visible=7, vfio=1),
            metric_batch(rented=1, visible=7, util=0),
            now=NOW,
        )
        self.assertEqual(vfio_events, [])

    def test_vast_cannot_advertise_or_rent_more_than_physical_healthy_capacity(self) -> None:
        events = reconcile_capacity(
            target_probe(physical=8, visible=7),
            metric_batch(total=9, rented=9, visible=7),
            now=NOW,
        )
        codes = {event["code"] for event in events}
        self.assertIn("vast_total_exceeds_physical", codes)
        self.assertIn("vast_rented_exceeds_healthy", codes)

    def test_physical_free_but_vast_unavailable_is_distinct(self) -> None:
        events = reconcile_capacity(
            target_probe(), metric_batch(rented=2, idle=0), now=NOW
        )
        codes = {event["code"] for event in events}
        self.assertIn("physical_free_vast_unavailable", codes)
        self.assertIn("vast_capacity_arithmetic_mismatch", codes)

        unlisted = reconcile_capacity(
            target_probe(), metric_batch(listed=0), now=NOW
        )
        self.assertIn(
            "physical_free_vast_unavailable", {event["code"] for event in unlisted}
        )

    def test_scrape_down_is_reported_without_trusting_cached_capacity(self) -> None:
        events = reconcile_capacity(
            target_probe(), metric_batch(vast_up=0, dcgm_up=0), now=NOW
        )
        self.assertEqual(
            {event["code"] for event in events},
            {"vast_scrape_down", "dcgm_scrape_down"},
        )

    def test_dcgm_uuid_mismatch_is_not_hidden_by_matching_counts(self) -> None:
        identities = [uuid(index) for index in range(7)] + [uuid(99)]
        events = reconcile_capacity(
            target_probe(), metric_batch(dcgm_uuids=identities), now=NOW
        )
        self.assertIn("dcgm_identity_mismatch", {event["code"] for event in events})

    def test_missing_per_gpu_occupancy_fails_closed(self) -> None:
        events = reconcile_capacity(
            target_probe(), metric_batch(occupancy=False), now=NOW
        )
        self.assertEqual(events[0]["code"], "prometheus_data_missing")
        self.assertEqual(events[0]["evidence"]["reason"], "missing_per_gpu_occupancy")

    def test_vast_listing_verification_and_recent_errors_are_known_state(self) -> None:
        metrics = metric_batch(listed=0, verified=0)
        metrics = MetricBatch(
            vast=metrics.vast,
            vast_errors=(sample(None, 2),),
            vast_up=metrics.vast_up,
            dcgm=metrics.dcgm,
            dcgm_up=metrics.dcgm_up,
        )
        codes = {
            event["code"]
            for event in reconcile_capacity(target_probe(), metrics, now=NOW)
        }
        self.assertTrue(
            {
                "vast_machine_unlisted",
                "vast_machine_unverified",
                "vast_exporter_recent_errors",
            }.issubset(codes)
        )

    def test_live_community_exporter_machine_error_is_known(self) -> None:
        metrics = metric_batch()
        vast = tuple(
            sample(
                item.labels.get("__name__"),
                item.value,
                **{
                    key: value
                    for key, value in item.labels.items()
                    if key not in {"__name__", "error_description"}
                },
                error_description="failed to inject CDI devices",
            )
            if item.labels.get("__name__") == "vastai_machine_ErrorDescription"
            else item
            for item in metrics.vast
        )
        events = reconcile_capacity(
            target_probe(),
            MetricBatch(vast, metrics.vast_errors, metrics.vast_up, metrics.dcgm, metrics.dcgm_up),
            now=NOW,
        )
        event = next(event for event in events if event["code"] == "vast_machine_error")
        self.assertEqual(event["evidence"]["error_description"], "failed to inject CDI devices")
        self.assertTrue(classify(event)["known"])

    def test_unmonitored_dcgm_skips_only_dcgm_checks(self) -> None:
        full = metric_batch()
        unmonitored = MetricBatch(
            full.vast, full.vast_errors, full.vast_up, (), (), dcgm_monitored=False
        )
        events = reconcile_capacity(target_probe(), unmonitored, now=NOW)
        self.assertEqual(events, [])
        self.assertTrue(capacity_evaluation_complete(events))
        self.assertTrue(metric_batch_fresh(unmonitored, NOW, 180))

        # Physical and Vast capacity checks still run without DCGM.
        short = metric_batch(total=8, rented=3)
        short = MetricBatch(
            short.vast, short.vast_errors, short.vast_up, (), (), dcgm_monitored=False
        )
        events = reconcile_capacity(target_probe(physical=6, visible=6), short, now=NOW)
        self.assertIn("vast_total_exceeds_physical", {event["code"] for event in events})

    def test_monitored_dcgm_with_no_up_series_still_fails_closed(self) -> None:
        full = metric_batch()
        missing = MetricBatch(full.vast, full.vast_errors, full.vast_up, full.dcgm, ())
        events = reconcile_capacity(target_probe(), missing, now=NOW)
        self.assertEqual([event["code"] for event in events], ["prometheus_data_missing"])
        self.assertFalse(capacity_evaluation_complete(events))

    def test_dcgm_outage_does_not_hide_an_independent_vast_machine_error(self) -> None:
        """One unavailable source must not erase a valid fault from another source."""
        metrics = metric_batch(dcgm_up=0)
        vast = tuple(
            sample(
                item.labels.get("__name__"),
                item.value,
                **{
                    key: value
                    for key, value in item.labels.items()
                    if key not in {"__name__", "error_description"}
                },
                error_description="bad bandwidthtest2 on gpu 0",
            )
            if item.labels.get("__name__") == "vastai_machine_ErrorDescription"
            else item
            for item in metrics.vast
        )

        events = reconcile_capacity(
            target_probe(),
            MetricBatch(vast, metrics.vast_errors, metrics.vast_up, metrics.dcgm, metrics.dcgm_up),
            now=NOW,
        )

        codes = {event["code"] for event in events}
        self.assertIn("dcgm_scrape_down", codes)
        self.assertIn("vast_machine_error", codes)

    def test_known_reconciliation_discrepancy_never_needs_model_classification(self) -> None:
        classification = classify(
            {"fault_family": "capacity", "code": "vast_total_exceeds_physical"}
        )
        self.assertTrue(classification["known"])
        self.assertEqual(classification["label"], "vast-total-exceeds-physical")


class MarketPrometheusReconciliationTests(unittest.TestCase):
    def test_idle_gpus_without_rentable_offer_emit_one_bounded_incident(self) -> None:
        events = reconcile_market(
            None,
            market_snapshot(),
            metrics=metric_batch(total=8, rented=3, idle=5),
            now=NOW,
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["code"], "physical_free_vast_market_unavailable")
        self.assertEqual(
            event["evidence"],
            {
                "idle": 5,
                "rented": 3,
                "total": 8,
                "offer_slice_summary": [
                    {"gpu_count": 1, "rentable": False, "rented": False},
                    {"gpu_count": 4, "rentable": False, "rented": False},
                ],
                "basis": "prometheus",
            },
        )
        self.assertEqual(
            (classify(event)["known"], classify(event)["severity"]), (True, "critical")
        )

    def test_zero_idle_or_rentable_market_is_healthy(self) -> None:
        no_idle = assess_market(
            None,
            market_snapshot(total_gpus=3, rented_gpus=3),
            metrics=metric_batch(total=3, rented=3, idle=0),
            now=NOW,
        )
        rentable = reconcile_market(
            None,
            market_snapshot(
                offers=(OfferSlice(103, 2, True, False),),
                rentable=True,
            ),
            metrics=metric_batch(),
            now=NOW,
        )
        self.assertEqual(no_idle, MarketAssessment([], True))
        self.assertEqual(rentable, [])

    def test_prometheus_fallback_does_not_duplicate_conclusive_ssh_incident(self) -> None:
        by_target = [
            event
            for event in reconcile_market(
                target_probe(),
                market_snapshot(),
                metrics=metric_batch(total=8, rented=3, idle=5),
                now=NOW,
            )
            if event["code"] == "physical_free_vast_market_unavailable"
        ]
        self.assertEqual([event["evidence"]["basis"] for event in by_target], ["target"])
        by_prometheus = reconcile_market(
            None,
            market_snapshot(),
            metrics=metric_batch(total=8, rented=3, idle=5),
            now=NOW,
        )
        # Whichever source proves the fault, it is one incident identity.
        self.assertEqual(
            stable_signature(by_target[0]), stable_signature(by_prometheus[0])
        )

    def test_full_ssh_cadence_is_not_a_stale_target_probe(self) -> None:
        probe = target_probe()
        full = market_snapshot(offers=(), total_gpus=8, rented_gpus=8)
        for age, expected in ((400, []), (430, ["target_probe_stale"])):
            with self.subTest(age=age):
                probe["observed_at"] = (NOW - timedelta(seconds=age)).isoformat().replace(
                    "+00:00", "Z"
                )
                assessment = assess_market(
                    probe,
                    full,
                    metrics=metric_batch(total=8, rented=8),
                    now=NOW,
                    probe_max_age_seconds=425,
                )
                self.assertEqual([event["code"] for event in assessment.events], expected)
                capacity_codes = [
                    event["code"]
                    for event in reconcile_capacity(
                        probe,
                        metric_batch(total=8, rented=8),
                        now=NOW,
                        probe_max_age_seconds=425,
                    )
                ]
                self.assertEqual(capacity_codes, expected)

    def test_historical_seven_rented_one_idle_without_target_probe(self) -> None:
        assessment = assess_market(
            None,
            market_snapshot(offers=(), rented_gpus=7),
            metrics=metric_batch(total=8, rented=7),
            now=NOW,
        )
        self.assertEqual(
            [(event["code"], event["evidence"]["idle"]) for event in assessment.events],
            [("physical_free_vast_market_unavailable", 1)],
        )
        self.assertTrue(assessment.conclusive)

    def test_disagreeing_direct_counts_are_inconclusive_not_a_fault(self) -> None:
        # The machine just filled: direct Vast shows 8 rented while exporter counts lag.
        for snapshot in (
            market_snapshot(offers=(), rented_gpus=8),
            market_snapshot(offers=(), total_gpus=7, rented_gpus=7),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(
                    assess_market(
                        None, snapshot, metrics=metric_batch(total=8, rented=7), now=NOW
                    ),
                    MarketAssessment([], False),
                )

    def test_cross_source_mode_leaves_single_source_findings_to_their_owners(self) -> None:
        stale_probe = target_probe()
        stale_probe["observed_at"] = "2026-09-14T11:50:00Z"
        cases = (
            # Stale Vast evidence belongs to the vast source.
            (
                target_probe(),
                market_snapshot(observed_at=datetime(2026, 9, 14, 11, 56, tzinfo=timezone.utc)),
            ),
            # Unknown machine and incomplete search: nothing can rule the fault out.
            (None, replace(market_snapshot(complete=False), machine=None)),
            # A stale target capture belongs to capacity-reconciliation.
            (stale_probe, market_snapshot(offers=(), rented_gpus=8)),
        )
        for probe, snapshot in cases:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(
                    assess_market(
                        probe, snapshot, now=NOW, cross_source_only=True
                    ),
                    MarketAssessment([], False),
                )
        # The same inputs still yield single-source findings for their owners.
        self.assertEqual(
            [event["code"] for event in reconcile_market(stale_probe, market_snapshot(), now=NOW)],
            ["target_probe_stale"],
        )
        available = assess_market(
            None,
            replace(market_snapshot(complete=False), machine=replace(
                market_snapshot().machine, rentable=True
            )),
            now=NOW,
            cross_source_only=True,
        )
        self.assertEqual(available, MarketAssessment([], True))
        # A runtime that collects captures cannot clear capture comparisons without one.
        self.assertFalse(
            assess_market(
                None,
                replace(market_snapshot(complete=False), machine=replace(
                    market_snapshot().machine, rentable=True
                )),
                now=NOW,
                cross_source_only=True,
                require_target=True,
            ).conclusive
        )

    def test_capture_comparisons_with_unknown_vast_values_are_inconclusive(self) -> None:
        known = market_snapshot(offers=(), rented_gpus=8)
        self.assertTrue(
            assess_market(
                target_probe(), known, now=NOW, cross_source_only=True, require_target=True
            ).conclusive
        )
        for snapshot in (
            replace(market_snapshot(offers=(), rented_gpus=8), machine=replace(
                known.machine, total_gpus=None
            )),
            market_snapshot(offers=(), rented_gpus=None),
            market_snapshot(complete=False, rented_gpus=8),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertFalse(
                    assess_market(
                        target_probe(),
                        snapshot,
                        now=NOW,
                        cross_source_only=True,
                        require_target=True,
                    ).conclusive
                )

    def test_free_gpus_behind_unknown_market_are_not_health(self) -> None:
        unknown = replace(
            market_snapshot(complete=False, rented_gpus=7),
            machine=replace(market_snapshot(rented_gpus=7).machine, rentable=None),
        )
        self.assertEqual(
            assess_market(
                target_probe(), unknown, now=NOW, cross_source_only=True, require_target=True
            ),
            MarketAssessment([], False),
        )
        full = replace(
            market_snapshot(complete=False, rented_gpus=8),
            machine=replace(market_snapshot(rented_gpus=8).machine, rentable=None),
        )
        # No GPU is free, but the offer-capacity checks were skipped for the
        # incomplete search, so their incidents must not recover here either.
        self.assertEqual(
            assess_market(
                target_probe(), full, now=NOW, cross_source_only=True, require_target=True
            ),
            MarketAssessment([], False),
        )

    def test_capacity_evaluation_is_complete_only_when_every_check_ran(self) -> None:
        self.assertTrue(
            capacity_evaluation_complete(
                reconcile_capacity(target_probe(physical=7, visible=7), metric_batch(), now=NOW)
            )
        )
        for metrics in (metric_batch(vast_up=0), metric_batch(dcgm_up=0)):
            with self.subTest(metrics=metrics):
                self.assertFalse(
                    capacity_evaluation_complete(
                        reconcile_capacity(target_probe(), metrics, now=NOW)
                    )
                )
        stale = target_probe()
        stale["observed_at"] = "2026-09-14T11:00:00Z"
        self.assertFalse(
            capacity_evaluation_complete(reconcile_capacity(stale, metric_batch(), now=NOW))
        )

    def test_target_without_direct_rented_count_falls_back_to_prometheus(self) -> None:
        events = reconcile_market(
            target_probe(),
            market_snapshot(offers=(), rented_gpus=None),
            metrics=metric_batch(total=8, rented=7),
            now=NOW,
        )
        self.assertEqual(
            [(event["code"], event["evidence"]["basis"]) for event in events],
            [("physical_free_vast_market_unavailable", "prometheus")],
        )

    def test_stale_or_incomplete_sources_do_not_assert_market_unavailability(self) -> None:
        fresh = metric_batch()
        stale_timestamp = STAMP - 181
        stale = MetricBatch(
            vast=tuple(
                Sample(item.labels, stale_timestamp, item.value)
                for item in fresh.vast
            ),
            vast_errors=tuple(
                Sample(item.labels, stale_timestamp, item.value)
                for item in fresh.vast_errors
            ),
            vast_up=tuple(
                Sample(item.labels, stale_timestamp, item.value)
                for item in fresh.vast_up
            ),
            dcgm=fresh.dcgm,
            dcgm_up=fresh.dcgm_up,
        )
        missing_idle = MetricBatch(
            vast=tuple(
                item
                for item in fresh.vast
                if item.labels.get("__name__") != "vastai_machine_gpu_idle"
            ),
            vast_errors=fresh.vast_errors,
            vast_up=fresh.vast_up,
            dcgm=fresh.dcgm,
            dcgm_up=fresh.dcgm_up,
        )
        wrong_identity = MetricBatch(
            vast=tuple(
                Sample(
                    {**item.labels, "machine_id": "99999"},
                    item.timestamp,
                    item.value,
                )
                if item is fresh.vast[0]
                else item
                for item in fresh.vast
            ),
            vast_errors=fresh.vast_errors,
            vast_up=fresh.vast_up,
            dcgm=fresh.dcgm,
            dcgm_up=fresh.dcgm_up,
        )
        cases = (
            (market_snapshot(), stale),
            (market_snapshot(), missing_idle),
            (market_snapshot(), wrong_identity),
            (market_snapshot(complete=False), fresh),
            (
                market_snapshot(
                    observed_at=datetime(
                        2026, 9, 14, 11, 56, tzinfo=timezone.utc
                    )
                ),
                fresh,
            ),
        )
        for snapshot, metrics in cases:
            with self.subTest(snapshot=snapshot, metrics=metrics):
                assessment = assess_market(None, snapshot, metrics=metrics, now=NOW)
                codes = {event["code"] for event in assessment.events}
                self.assertNotIn("physical_free_vast_market_unavailable", codes)
                if not codes:
                    # Missing evidence must not look like a healthy recovery sample.
                    self.assertFalse(assessment.conclusive)

    def test_count_and_offer_changes_preserve_incident_dedup_signature(self) -> None:
        first = reconcile_market(
            None,
            market_snapshot(),
            metrics=metric_batch(total=8, rented=3, idle=5),
            now=NOW,
        )[0]
        second = reconcile_market(
            None,
            market_snapshot(
                offers=(
                    OfferSlice(200, 2, False, True),
                    OfferSlice(201, 8, False, False),
                ),
                rented_gpus=2,
            ),
            metrics=metric_batch(total=8, rented=2, idle=6),
            now=NOW,
        )[0]
        self.assertNotEqual(first["evidence"], second["evidence"])
        self.assertEqual(stable_signature(first), stable_signature(second))


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, *, content_length: int | None = None):
        super().__init__(body)
        self.status = 200
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(
        self,
        body: bytes,
        *,
        content_length: int | None = None,
        source_timestamp: float = STAMP,
    ):
        self.body = body
        self.content_length = content_length
        self.source_timestamp = source_timestamp
        self.requests = []

    def open(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        expression = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request.full_url).query
        )["query"][0]
        body = (
            api_body(value=self.source_timestamp)
            if "timestamp(" in expression
            else self.body
        )
        return FakeResponse(body, content_length=self.content_length)


class AlertOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def open(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        return FakeResponse(self.body)


class DownTargetOpener(FakeOpener):
    def open(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        expression = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request.full_url).query
        )["query"][0]
        if "timestamp(" in expression:
            return FakeResponse(api_body(value=STAMP))
        if expression.startswith("up{"):
            return FakeResponse(api_body(value=0))
        if "vastai_exporter_errors_total" in expression:
            return FakeResponse(api_body(value=0))
        return FakeResponse(
            b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        )


def api_body(timestamp: float = STAMP, value: float = 1) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": "up"}, "value": [timestamp, str(value)]}
                ],
            },
        }
    ).encode()


class PrometheusClientTests(unittest.TestCase):
    def test_endpoint_rejects_credentials_parameters_and_non_api_paths(self) -> None:
        invalid = (
            "http://user:pass@prometheus.example:9090",
            "http://prometheus.example:9090/graph",
            "http://prometheus.example:9090?query=up",
            "file:///tmp/prometheus",
            "http://prometheus.example",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(PrometheusError):
                validate_endpoint(endpoint)
        self.assertEqual(
            validate_endpoint("http://prometheus.example:9090/"),
            "http://prometheus.example:9090",
        )

    def test_fetch_uses_only_fixed_queries_and_bounded_timeout(self) -> None:
        opener = FakeOpener(api_body())
        client = PrometheusClient(
            "http://prometheus.example:9090", clock=lambda: NOW, opener=opener
        )
        client.fetch("17049", "vastai-exporter", "dcgm-exporter")
        self.assertEqual(len(opener.requests), 7)
        for request, timeout in opener.requests:
            parsed = urllib.parse.urlsplit(request.full_url)
            self.assertEqual(parsed.path, "/api/v1/query")
            self.assertEqual(timeout, 2)
            self.assertNotIn("Authorization", request.headers)
        expressions = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["query"][0]
            for request, _timeout in opener.requests
        ]
        self.assertTrue(any("machine_id=\"17049\"" in value for value in expressions))
        self.assertFalse(any("http://" in value or "https://" in value for value in expressions))

    def test_empty_metric_vectors_still_report_explicit_down_targets(self) -> None:
        client = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=DownTargetOpener(api_body()),
        )
        metrics = client.fetch("17049", "prometheus", "Terracompute")
        events = reconcile_capacity(target_probe(), metrics, now=NOW)
        self.assertEqual(
            {event["code"] for event in events},
            {"vast_scrape_down", "dcgm_scrape_down"},
        )

    def test_unmonitored_dcgm_issues_no_dcgm_query_and_is_not_a_scrape_outage(self) -> None:
        opener = DownTargetOpener(api_body())
        client = PrometheusClient(
            "http://prometheus.example:9090", clock=lambda: NOW, opener=opener
        )
        metrics = client.fetch("17049", "prometheus", None)
        expressions = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["query"][0]
            for request, _timeout in opener.requests
        ]
        self.assertEqual(len(expressions), 4)
        self.assertFalse(any("9400" in value or "DCGM" in value for value in expressions))
        self.assertFalse(metrics.dcgm_monitored)
        self.assertEqual((metrics.dcgm, metrics.dcgm_up), ((), ()))
        events = reconcile_capacity(target_probe(), metrics, now=NOW)
        self.assertEqual({event["code"] for event in events}, {"vast_scrape_down"})

    def test_stale_and_oversized_responses_fail_closed(self) -> None:
        stale = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=FakeOpener(api_body(STAMP - 181)),
        )
        with self.assertRaisesRegex(PrometheusError, "stale_data"):
            stale.fetch("17049", "vastai-exporter", "dcgm-exporter")

        stale_source = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=FakeOpener(api_body(), source_timestamp=STAMP - 181),
        )
        with self.assertRaises(PrometheusError) as caught:
            stale_source.fetch("17049", "vastai-exporter", "dcgm-exporter")
        event = prometheus_failure_event(caught.exception)
        self.assertEqual(event["code"], "vast_scrape_stale")
        self.assertEqual(event["component"], "vast_age")

        oversized = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=FakeOpener(api_body(), content_length=MAX_RESPONSE_BYTES + 1),
        )
        with self.assertRaisesRegex(PrometheusError, "response_limit"):
            oversized.fetch("17049", "vastai-exporter", "dcgm-exporter")

        malformed = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=FakeOpener(b'{"status":"success","data":{"result":[]}}'),
        )
        with self.assertRaisesRegex(PrometheusError, "malformed_response"):
            malformed.fetch("17049", "vastai-exporter", "dcgm-exporter")

    def test_job_label_cannot_inject_promql(self) -> None:
        client = PrometheusClient(
            "http://prometheus.example:9090",
            clock=lambda: NOW,
            opener=FakeOpener(api_body()),
        )
        with self.assertRaisesRegex(PrometheusError, "invalid_job_name"):
            client.fetch("17049", 'vastai-exporter"} or up', "dcgm-exporter")

    def test_fixed_alert_endpoint_returns_bounded_typed_freshness(self) -> None:
        body = json.dumps({"status": "success", "data": {"alerts": [
            {"labels": {"alertname": "GpuFault"}, "annotations": {"summary": "synthetic"}, "state": "firing", "activeAt": "2026-09-14T11:58:00Z", "value": "1"},
            {"labels": {"alertname": "Warm"}, "state": "pending"},
            {"labels": {"alertname": "Novel"}, "state": "vendor-state"},
        ]}}).encode()
        opener = AlertOpener(body)
        snapshot = PrometheusClient(
            "http://prometheus.example:9090", clock=lambda: NOW, opener=opener
        ).fetch_alerts()
        request, timeout = opener.requests[0]
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, "/api/v1/alerts")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).query, "")
        self.assertEqual(timeout, 2)
        self.assertEqual([item.state for item in snapshot.alerts], [AlertState.FIRING, AlertState.PENDING, AlertState.UNKNOWN])
        self.assertEqual((len(snapshot.firing), len(snapshot.pending), len(snapshot.unknown)), (1, 1, 1))
        self.assertFalse(snapshot.healthy)
        self.assertEqual(snapshot.freshness_at(NOW), AlertFreshness.FRESH)
        self.assertEqual(snapshot.freshness_at(datetime.fromtimestamp(STAMP + 181, timezone.utc)), AlertFreshness.STALE)

    def test_only_complete_valid_empty_alert_response_is_healthy(self) -> None:
        valid = AlertOpener(b'{"status":"success","data":{"alerts":[]}}')
        snapshot = PrometheusClient(
            "http://prometheus.example:9090", clock=lambda: NOW, opener=valid
        ).fetch_alerts()
        self.assertTrue(snapshot.healthy_at(NOW))
        self.assertFalse(snapshot.__class__(NOW, (), complete=False).healthy)

        malformed = AlertOpener(b'{"status":"success","data":{}}')
        with self.assertRaisesRegex(PrometheusError, "malformed_response"):
            PrometheusClient(
                "http://prometheus.example:9090", clock=lambda: NOW, opener=malformed
            ).fetch_alerts()


if __name__ == "__main__":
    unittest.main()
