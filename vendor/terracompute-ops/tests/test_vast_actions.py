from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from terracompute_ops.actions import (
    ActionAdapter,
    DispatchResult,
    DispatchStatus,
    PostActionEvidence,
)
from terracompute_ops.http_client import HttpRequest, HttpResponse
from terracompute_ops.policy import (
    ActionClass,
    ActionProposal,
    Ownership,
    PolicyDenied,
    PreActionEvidence,
    RentalImpact,
    SourceBinding,
    SourceState,
)
from terracompute_ops.vast_actions import (
    MACHINE_ASKS_PATH,
    MACHINE_MAINTENANCE_PATH,
    MaintenanceCategory,
    MaintenanceWindow,
    StaleSelfTestEvidence,
    VastActionAdapter,
    VastActionError,
    _VastRestTransport,
)


NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)
INSTANCE_ID = 912345


class FakeTransport:
    def __init__(self, responses: list[HttpResponse | BaseException], events=None):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []
        self.events = events

    def request(
        self,
        request: HttpRequest,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        cert_sha256: str | None = None,
    ) -> HttpResponse:
        del timeout_seconds, max_response_bytes, cert_sha256
        self.requests.append(request)
        if self.events is not None:
            self.events.append(request.method)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )


def proposal(action_class: ActionClass) -> ActionProposal:
    if action_class is ActionClass.REMOVE_SELF_TEST_RESOURCE:
        parameters = {
            "operation": "destroy-stale-self-test-instance",
            "instance_id": INSTANCE_ID,
        }
        resource_ids = (f"vast-instance:{INSTANCE_ID}",)
        rentals = (
            RentalImpact(str(INSTANCE_ID), False, Ownership.CONTROLLER),
        )
        sources = ("host", "rentals")
    elif action_class is ActionClass.LISTING_CHANGE:
        parameters = {"operation": "unlist-machine", "machine_id": 17049}
        resource_ids = ("vast-machine:17049",)
        rentals = (RentalImpact("tenant-7", True, Ownership.TENANT, True),)
        sources = ("rentals", "vast")
    else:
        parameters = {"operation": "unsupported"}
        resource_ids = ("host",)
        rentals = ()
        sources = ("host", "rentals")
    return ActionProposal.create(
        proposal_id=f"proposal:{action_class.value}",
        action_class=action_class,
        parameters=parameters,
        resource_ids=resource_ids,
        rental_impacts=rentals,
        affected_domains=("vast",),
        source_bindings=tuple(SourceBinding(item, f"{item}:r1") for item in sources),
        evidence_revision="evidence:r1",
        policy_revision="policy:r1",
        stop_condition="stop on any identity or evidence mismatch",
        clock=lambda: NOW,
    )


def evidence(item: ActionProposal, *, machine_id: int = 17049) -> PreActionEvidence:
    return PreActionEvidence(
        machine_id=machine_id,
        target_identity_verified=True,
        evidence_revision=item.evidence_revision,
        sources=tuple(
            SourceState(binding.source, binding.revision, NOW)
            for binding in item.source_bindings
        ),
        resource_ids=item.resource_ids,
        rental_impacts=item.rental_impacts,
        affected_domains=item.affected_domains,
        mappings_known=item.mappings_known,
        power_domain_proven=item.power_domain_proven,
        evidence_ref="evidence:current",
        backup_ref="backup:current",
        backup_succeeded=True,
    )


def ownership(
    item: ActionProposal, identity, *, controller_owned: bool = True
) -> StaleSelfTestEvidence:
    return StaleSelfTestEvidence(
        identity.instance_id,
        identity.machine_id,
        controller_owned,
        True,
        True,
        item.source_bindings,
        "evidence:self-test-owner",
    )


def postflight(
    _proposal: ActionProposal, execution_id: str, _result: DispatchResult
) -> PostActionEvidence:
    return PostActionEvidence(execution_id, "evidence:post", True, "fixture")


def adapter(transport: FakeTransport, **changes) -> VastActionAdapter:
    values = {
        "capture_evidence": evidence,
        "capture_stale_self_test": ownership,
        "capture_postflight": postflight,
        "transport": transport,
        "clock": lambda: NOW,
        "monotonic": lambda: 10.0,
    }
    values.update(changes)
    return VastActionAdapter("synthetic-write-key", **values)


class VastActionTests(unittest.TestCase):
    def test_repr_does_not_expose_write_key_or_maintenance_reason(self) -> None:
        client = adapter(FakeTransport([]))
        rendered = repr(client) + repr(client._rest)
        self.assertNotIn("synthetic-write-key", rendered)
        window = MaintenanceWindow(
            NOW, 2, "synthetic private reason", MaintenanceCategory.SOFTWARE
        )
        self.assertNotIn("synthetic private reason", repr(window))

    def test_adapter_supports_only_exact_existing_semantics(self) -> None:
        client = adapter(FakeTransport([]))
        self.assertIsInstance(client, ActionAdapter)
        self.assertTrue(client.supports(ActionClass.REMOVE_SELF_TEST_RESOURCE))
        self.assertTrue(client.supports(ActionClass.LISTING_CHANGE))
        for action_class in ActionClass:
            if action_class not in {
                ActionClass.REMOVE_SELF_TEST_RESOURCE,
                ActionClass.LISTING_CHANGE,
            }:
                self.assertFalse(client.supports(action_class))
                with self.assertRaisesRegex(PolicyDenied, "unsupported"):
                    client.validate(proposal(action_class))

    def test_strict_ids_and_exact_proposal_shapes_fail_before_http(self) -> None:
        transport = FakeTransport([])
        client = adapter(transport)
        base = proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE)
        for invalid in (True, "912345", 0, -1):
            changed = ActionProposal.create(
                proposal_id="invalid",
                action_class=base.action_class,
                parameters={
                    "operation": "destroy-stale-self-test-instance",
                    "instance_id": invalid,
                },
                resource_ids=(f"vast-instance:{invalid}",),
                rental_impacts=(
                    RentalImpact(str(invalid), False, Ownership.CONTROLLER),
                ),
                affected_domains=base.affected_domains,
                source_bindings=base.source_bindings,
                evidence_revision=base.evidence_revision,
                policy_revision=base.policy_revision,
                stop_condition=base.stop_condition,
                clock=lambda: NOW,
            )
            with self.assertRaises(PolicyDenied):
                client.validate(changed)
        with self.assertRaises(PolicyDenied):
            client.validate(
                ActionProposal.create(
                    proposal_id="extra",
                    action_class=base.action_class,
                    parameters={
                        "operation": "destroy-stale-self-test-instance",
                        "instance_id": INSTANCE_ID,
                        "extra": "denied",
                    },
                    resource_ids=base.resource_ids,
                    rental_impacts=base.rental_impacts,
                    affected_domains=base.affected_domains,
                    source_bindings=base.source_bindings,
                    evidence_revision=base.evidence_revision,
                    policy_revision=base.policy_revision,
                    stop_condition=base.stop_condition,
                    clock=lambda: NOW,
                )
            )
        self.assertEqual(transport.requests, [])

    def test_destroy_revalidates_api_identity_ownership_and_current_evidence(self) -> None:
        events: list[str] = []
        transport = FakeTransport(
            [
                response(
                    {
                        "instances": {
                            "id": INSTANCE_ID,
                            "machine_id": 17049,
                            "jupyter_token": "sensitive-response-value",
                        }
                    }
                ),
                response({"success": True, "msg": "Instance destroyed successfully"}),
            ],
            events,
        )

        def capture_owner(item, identity):
            events.append("ownership")
            return ownership(item, identity)

        def capture_evidence(item):
            events.append("evidence")
            return evidence(item)

        client = adapter(
            transport,
            capture_evidence=capture_evidence,
            capture_stale_self_test=capture_owner,
        )
        result = client.dispatch(
            proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE), "execution-1"
        )
        self.assertEqual(result.status, DispatchStatus.SUCCEEDED)
        self.assertEqual(events, ["GET", "ownership", "evidence", "DELETE"])
        self.assertEqual(
            [(item.method, item.url, item.body) for item in transport.requests],
            [
                (
                    "GET",
                    f"https://console.vast.ai/api/v0/instances/{INSTANCE_ID}/",
                    None,
                ),
                (
                    "DELETE",
                    f"https://console.vast.ai/api/v0/instances/{INSTANCE_ID}/",
                    None,
                ),
            ],
        )
        self.assertNotIn("synthetic-write-key", repr(transport.requests[0]))
        self.assertNotIn("sensitive-response-value", repr(result))

    def test_destroy_fails_closed_before_delete_on_any_identity_or_owner_doubt(self) -> None:
        wrong_machine = FakeTransport(
            [response({"instances": {"id": INSTANCE_ID, "machine_id": 17050}})]
        )
        result = adapter(wrong_machine).dispatch(
            proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE), "execution-2"
        )
        self.assertEqual(result.status, DispatchStatus.FAILED)
        self.assertEqual([item.method for item in wrong_machine.requests], ["GET"])

        not_owned = FakeTransport(
            [response({"instances": {"id": INSTANCE_ID, "machine_id": 17049}})]
        )
        result = adapter(
            not_owned,
            capture_stale_self_test=lambda item, identity: ownership(
                item, identity, controller_owned=False
            ),
        ).dispatch(proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE), "execution-3")
        self.assertEqual(result.status, DispatchStatus.FAILED)
        self.assertEqual([item.method for item in not_owned.requests], ["GET"])

    def test_stale_source_or_changed_rental_fails_closed_before_machine_write(self) -> None:
        item = proposal(ActionClass.LISTING_CHANGE)

        def stale(current: ActionProposal) -> PreActionEvidence:
            base = evidence(current)
            return PreActionEvidence(
                **{
                    **base.__dict__,
                    "sources": tuple(
                        SourceState(binding.source, binding.revision, NOW - timedelta(minutes=2))
                        for binding in current.source_bindings
                    ),
                }
            )

        transport = FakeTransport([])
        result = adapter(transport, capture_evidence=stale).dispatch(item, "execution-4")
        self.assertEqual(result.status, DispatchStatus.FAILED)
        self.assertEqual(transport.requests, [])

        malformed = FakeTransport([])
        result = adapter(
            malformed,
            capture_evidence=lambda current: PreActionEvidence(
                **{**evidence(current).__dict__, "sources": (object(),)}
            ),
        ).dispatch(item, "execution-malformed")
        self.assertEqual(result.status, DispatchStatus.FAILED)
        self.assertEqual(malformed.requests, [])

    def test_unlist_uses_only_fixed_machine_route_and_exact_success_schema(self) -> None:
        transport = FakeTransport(
            [response({"success": True, "machine_id": 17049, "user_id": 44})]
        )
        result = adapter(transport).dispatch(
            proposal(ActionClass.LISTING_CHANGE), "execution-5"
        )
        self.assertEqual(result.status, DispatchStatus.SUCCEEDED)
        request = transport.requests[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.url, "https://console.vast.ai" + MACHINE_ASKS_PATH)
        self.assertIsNone(request.body)

        extra_field = FakeTransport(
            [
                response(
                    {
                        "success": True,
                        "machine_id": 17049,
                        "user_id": 44,
                        "extra": True,
                    }
                )
            ]
        )
        uncertain = adapter(extra_field).dispatch(
            proposal(ActionClass.LISTING_CHANGE), "execution-6"
        )
        self.assertEqual(uncertain.status, DispatchStatus.UNKNOWN)

    def test_any_post_delete_contract_failure_is_unknown(self) -> None:
        transport = FakeTransport(
            [
                response({"instances": {"id": INSTANCE_ID, "machine_id": 17049}}),
                response({"success": True, "msg": "ok", "unexpected": True}),
            ]
        )
        result = adapter(transport).dispatch(
            proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE), "execution-7"
        )
        self.assertEqual(result.status, DispatchStatus.UNKNOWN)
        self.assertEqual([item.method for item in transport.requests], ["GET", "DELETE"])

    def test_reconciliation_is_read_only(self) -> None:
        no_requests = FakeTransport([])
        result = adapter(no_requests).reconcile(
            proposal(ActionClass.LISTING_CHANGE), "execution-8"
        )
        self.assertEqual(result.status, DispatchStatus.UNKNOWN)
        self.assertEqual(no_requests.requests, [])

        absent = FakeTransport([response({}, status=404)])
        result = adapter(absent).reconcile(
            proposal(ActionClass.REMOVE_SELF_TEST_RESOURCE), "execution-9"
        )
        self.assertEqual(result.status, DispatchStatus.SUCCEEDED)
        self.assertEqual([item.method for item in absent.requests], ["GET"])

    def test_postflight_discards_external_detail_and_requires_evidence_ref(self) -> None:
        client = adapter(FakeTransport([]))
        item = proposal(ActionClass.LISTING_CHANGE)
        result = DispatchResult("execution-10", DispatchStatus.SUCCEEDED, "known")
        captured = client.postflight(item, "execution-10", result)
        self.assertEqual(captured.evidence_ref, "evidence:post")
        self.assertNotIn("fixture", captured.detail)

        rejected = adapter(
            FakeTransport([]),
            capture_postflight=lambda *_args: PostActionEvidence(
                "execution-10", None, True, "synthetic-secret"
            ),
        ).postflight(item, "execution-10", result)
        self.assertFalse(rejected.postcondition_ok)
        self.assertNotIn("synthetic-secret", repr(rejected))

    def test_private_maintenance_rest_shape_is_exact_but_adapter_does_not_expose_it(self) -> None:
        transport = FakeTransport(
            [response({"success": True, "you_sent": "2 notifications sent"})]
        )
        rest = _VastRestTransport(
            "synthetic-write-key",
            transport=transport,
            timeout_seconds=10,
            max_response_bytes=128 * 1024,
            monotonic=lambda: 10.0,
        )
        window = MaintenanceWindow(
            NOW, 2, "Routine hardware check", MaintenanceCategory.SOFTWARE
        )
        rest.schedule_maintenance(window, deadline=20.0)
        request = transport.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(
            request.url, "https://console.vast.ai" + MACHINE_MAINTENANCE_PATH
        )
        self.assertEqual(
            json.loads(request.body),
            {
                "sdate": "2026-09-15T18:00:00Z",
                "duration": 2,
                "maintenance_reason": "Routine hardware check",
                "maintenance_category": "software",
            },
        )
        self.assertFalse(hasattr(adapter(FakeTransport([])), "schedule_maintenance"))

    def test_maintenance_values_and_success_response_are_strict(self) -> None:
        with self.assertRaises(VastActionError):
            MaintenanceWindow(NOW, True, "reason", MaintenanceCategory.OTHER)
        with self.assertRaises(VastActionError):
            MaintenanceWindow(NOW, 2, "line one\nline two", MaintenanceCategory.OTHER)
        transport = FakeTransport(
            [response({"success": True, "you_sent": "sent", "extra": "denied"})]
        )
        rest = _VastRestTransport(
            "synthetic-write-key",
            transport=transport,
            timeout_seconds=10,
            max_response_bytes=128 * 1024,
            monotonic=lambda: 10.0,
        )
        with self.assertRaisesRegex(VastActionError, "malformed_maintenance_response"):
            rest.schedule_maintenance(
                MaintenanceWindow(NOW, 1, "reason", MaintenanceCategory.OTHER),
                deadline=20.0,
            )


if __name__ == "__main__":
    unittest.main()
