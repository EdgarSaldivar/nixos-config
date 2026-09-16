"""Fixed-shape, currently unwired Vast actions for machine 17049.

The public adapter fits :class:`terracompute_ops.actions.ActionAdapter`; it is
not constructed by the CLI, runtime, or Nix modules.  Every dispatch recaptures
injected current evidence.  Instance destruction additionally uses Vast's
authenticated single-instance GET and an independent ownership callback before
the DELETE.  The REST helper deliberately has no arbitrary URL, path, method,
or JSON-body entry point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from .actions import (
    DispatchResult,
    DispatchStatus,
    PostActionEvidence,
)
from .http_client import HttpClientError, HttpTransport, RestrictedHttpClient
from .policy import (
    MACHINE_ID,
    MAX_SOURCE_AGE,
    ActionClass,
    ActionProposal,
    Ownership,
    PolicyDenied,
    PreActionEvidence,
    SourceBinding,
    SourceState,
    require_utc,
    utc_now,
    utc_text,
)


VAST_ORIGIN = "https://console.vast.ai"
MACHINE_ASKS_PATH = f"/api/v0/machines/{MACHINE_ID}/asks/"
MACHINE_MAINTENANCE_PATH = f"/api/v0/machines/{MACHINE_ID}/dnotify"
MAX_ACTION_RESPONSE_BYTES = 128 * 1024
MAX_INSTANCE_ID = 2**63 - 1
MAX_REASON_LENGTH = 512

_REMOVE_OPERATION = "destroy-stale-self-test-instance"
_UNLIST_OPERATION = "unlist-machine"
_SUPPORTED = frozenset(
    {ActionClass.REMOVE_SELF_TEST_RESOURCE, ActionClass.LISTING_CHANGE}
)
_REQUIRED_SOURCES = {
    ActionClass.REMOVE_SELF_TEST_RESOURCE: frozenset({"host", "rentals"}),
    ActionClass.LISTING_CHANGE: frozenset({"rentals", "vast"}),
}


class VastActionError(ValueError):
    """A bounded, secret-free action contract failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

    def __repr__(self) -> str:
        return f"VastActionError(reason={self.reason!r})"


class MaintenanceCategory(str, Enum):
    POWER = "power"
    INTERNET = "internet"
    DISK = "disk"
    GPU = "gpu"
    SOFTWARE = "software"
    OTHER = "other"


@dataclass(frozen=True, repr=False)
class MaintenanceWindow:
    """The complete documented ``dnotify`` request, with local safety bounds."""

    starts_at: datetime
    duration_hours: int
    reason: str
    category: MaintenanceCategory

    def __post_init__(self) -> None:
        require_utc(self.starts_at, "starts_at")
        if (
            isinstance(self.duration_hours, bool)
            or not isinstance(self.duration_hours, int)
            or not 1 <= self.duration_hours <= 168
        ):
            raise VastActionError("invalid_maintenance_duration")
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or len(self.reason) > MAX_REASON_LENGTH
            or "\r" in self.reason
            or "\n" in self.reason
        ):
            raise VastActionError("invalid_maintenance_reason")
        if not isinstance(self.category, MaintenanceCategory):
            raise VastActionError("invalid_maintenance_category")

    def __repr__(self) -> str:
        return (
            f"MaintenanceWindow(starts_at={utc_text(self.starts_at)!r}, "
            f"duration_hours={self.duration_hours!r}, category={self.category.value!r})"
        )


@dataclass(frozen=True, repr=False)
class VastInstanceIdentity:
    """Only the identity facts retained from Vast's potentially sensitive body."""

    instance_id: int
    machine_id: int

    def __repr__(self) -> str:
        return (
            f"VastInstanceIdentity(instance_id={self.instance_id!r}, "
            f"machine_id={self.machine_id!r})"
        )


@dataclass(frozen=True, repr=False)
class StaleSelfTestEvidence:
    """Independent proof required in addition to API-account visibility."""

    instance_id: int
    machine_id: int
    controller_owned: bool
    self_test: bool
    stale: bool
    source_bindings: tuple[SourceBinding, ...]
    evidence_ref: str

    def __repr__(self) -> str:
        return (
            f"StaleSelfTestEvidence(instance_id={self.instance_id!r}, "
            f"machine_id={self.machine_id!r}, controller_owned={self.controller_owned!r}, "
            f"self_test={self.self_test!r}, stale={self.stale!r})"
        )


class _VastRestTransport:
    """Exact Vast REST calls.  Construction and use remain adapter-private."""

    def __init__(
        self,
        write_api_key: str,
        *,
        transport: HttpTransport | None,
        timeout_seconds: float,
        max_response_bytes: int,
        monotonic: Callable[[], float],
    ):
        if (
            not isinstance(write_api_key, str)
            or not write_api_key
            or len(write_api_key) > 8192
            or "\r" in write_api_key
            or "\n" in write_api_key
        ):
            raise VastActionError("invalid_write_api_key")
        self._write_api_key = write_api_key
        self._transport = transport
        self.timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._monotonic = monotonic
        # Validate all constructor bounds without sending a request.
        self._machine_asks_http()

    def __repr__(self) -> str:
        return f"_VastRestTransport(machine_id={MACHINE_ID})"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._write_api_key}"}

    def _instance_http(
        self, instance_id: int, *, delete: bool
    ) -> RestrictedHttpClient:
        instance_id = _strict_identifier(instance_id, "invalid_instance_id")
        path = _instance_path(instance_id)
        method = "DELETE" if delete else "GET"
        return RestrictedHttpClient(
            VAST_ORIGIN,
            {path: frozenset({method})},
            transport=self._transport,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            max_concurrency=1,
            monotonic=self._monotonic,
        )

    def _machine_asks_http(self) -> RestrictedHttpClient:
        return RestrictedHttpClient(
            VAST_ORIGIN,
            {MACHINE_ASKS_PATH: frozenset({"DELETE"})},
            transport=self._transport,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            max_concurrency=1,
            monotonic=self._monotonic,
        )

    def _maintenance_http(self) -> RestrictedHttpClient:
        return RestrictedHttpClient(
            VAST_ORIGIN,
            {MACHINE_MAINTENANCE_PATH: frozenset({"PUT"})},
            transport=self._transport,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            max_concurrency=1,
            monotonic=self._monotonic,
        )

    def verify_instance(
        self, instance_id: int, *, deadline: float
    ) -> VastInstanceIdentity:
        instance_id = _strict_identifier(instance_id, "invalid_instance_id")
        path = _instance_path(instance_id)
        payload = self._instance_http(instance_id, delete=False).request_json(
            "GET", path, headers=self._headers, deadline=deadline
        )
        if not isinstance(payload, dict) or set(payload) != {"instances"}:
            raise VastActionError("malformed_instance_response")
        instance = payload["instances"]
        if not isinstance(instance, dict):
            raise VastActionError("malformed_instance_response")
        returned_id = _strict_identifier(
            instance.get("id"), "malformed_instance_identity"
        )
        machine_id = _strict_identifier(
            instance.get("machine_id"), "malformed_instance_identity"
        )
        if returned_id != instance_id:
            raise VastActionError("instance_identity_mismatch")
        if machine_id != MACHINE_ID:
            raise VastActionError("machine_identity_mismatch")
        return VastInstanceIdentity(returned_id, machine_id)

    def destroy_instance(self, instance_id: int, *, deadline: float) -> None:
        instance_id = _strict_identifier(instance_id, "invalid_instance_id")
        path = _instance_path(instance_id)
        payload = self._instance_http(instance_id, delete=True).request_json(
            "DELETE", path, headers=self._headers, deadline=deadline
        )
        _validate_destroy_success(payload)

    def unlist_machine(self, *, deadline: float) -> None:
        payload = self._machine_asks_http().request_json(
            "DELETE", MACHINE_ASKS_PATH, headers=self._headers, deadline=deadline
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"success", "machine_id", "user_id"}
            or payload.get("success") is not True
            or _strict_identifier(
                payload.get("machine_id"), "malformed_unlist_response"
            )
            != MACHINE_ID
        ):
            raise VastActionError("malformed_unlist_response")
        _strict_identifier(payload.get("user_id"), "malformed_unlist_response")

    def schedule_maintenance(
        self, window: MaintenanceWindow, *, deadline: float
    ) -> None:
        if not isinstance(window, MaintenanceWindow):
            raise VastActionError("invalid_maintenance_window")
        payload = self._maintenance_http().request_json(
            "PUT",
            MACHINE_MAINTENANCE_PATH,
            headers=self._headers,
            payload={
                "sdate": utc_text(window.starts_at),
                "duration": window.duration_hours,
                "maintenance_reason": window.reason,
                "maintenance_category": window.category.value,
            },
            deadline=deadline,
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"success", "you_sent"}
            or payload.get("success") is not True
            or not _valid_bounded_text(payload.get("you_sent"), 512)
        ):
            raise VastActionError("malformed_maintenance_response")


EvidenceCapture = Callable[[ActionProposal], PreActionEvidence]
OwnershipCapture = Callable[
    [ActionProposal, VastInstanceIdentity], StaleSelfTestEvidence
]
PostflightCapture = Callable[
    [ActionProposal, str, DispatchResult], PostActionEvidence
]


class VastActionAdapter:
    """Approval-broker adapter for the two exactly matching action classes.

    Maintenance has no exact :class:`ActionClass` today, so its REST shape is
    present in the private helper but this adapter cannot dispatch it.
    """

    def __init__(
        self,
        write_api_key: str,
        *,
        capture_evidence: EvidenceCapture,
        capture_stale_self_test: OwnershipCapture,
        capture_postflight: PostflightCapture,
        transport: HttpTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 10,
        max_response_bytes: int = MAX_ACTION_RESPONSE_BYTES,
    ):
        if not callable(capture_evidence) or not callable(capture_stale_self_test):
            raise VastActionError("invalid_evidence_callback")
        if not callable(capture_postflight):
            raise VastActionError("invalid_postflight_callback")
        self._capture_evidence = capture_evidence
        self._capture_stale_self_test = capture_stale_self_test
        self._capture_postflight = capture_postflight
        self._clock = clock
        self._monotonic = monotonic
        self._rest = _VastRestTransport(
            write_api_key,
            transport=transport,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            monotonic=monotonic,
        )

    def __repr__(self) -> str:
        return f"VastActionAdapter(machine_id={MACHINE_ID})"

    def supports(self, action_class: ActionClass) -> bool:
        return action_class in _SUPPORTED

    def validate(self, proposal: ActionProposal) -> None:
        if not isinstance(proposal, ActionProposal) or not self.supports(
            proposal.action_class
        ):
            raise PolicyDenied("unsupported Vast action class")
        if proposal.machine_id != MACHINE_ID:
            raise PolicyDenied("Vast target machine identity mismatch")
        if any(
            item.ownership is Ownership.UNKNOWN for item in proposal.rental_impacts
        ):
            raise PolicyDenied("Vast rental ownership is unknown")
        if any(
            item.active and not item.interruption_approved
            for item in proposal.rental_impacts
        ):
            raise PolicyDenied("active Vast rental impact is not approved")
        bound_sources = {item.source for item in proposal.source_bindings}
        if not _REQUIRED_SOURCES[proposal.action_class].issubset(bound_sources):
            raise PolicyDenied("required Vast action source is not bound")
        parameters = dict(proposal.parameters)
        if proposal.action_class is ActionClass.REMOVE_SELF_TEST_RESOURCE:
            if set(parameters) != {"operation", "instance_id"}:
                raise PolicyDenied("invalid stale self-test action parameters")
            if parameters.get("operation") != _REMOVE_OPERATION:
                raise PolicyDenied("invalid stale self-test operation")
            instance_id = _policy_identifier(parameters.get("instance_id"))
            if proposal.resource_ids != (f"vast-instance:{instance_id}",):
                raise PolicyDenied("stale self-test resource identity mismatch")
            if (
                len(proposal.rental_impacts) != 1
                or proposal.rental_impacts[0].rental_id != str(instance_id)
                or proposal.rental_impacts[0].ownership is not Ownership.CONTROLLER
            ):
                raise PolicyDenied("stale self-test controller ownership is not bound")
            return
        if set(parameters) != {"operation", "machine_id"}:
            raise PolicyDenied("invalid unlist action parameters")
        if parameters.get("operation") != _UNLIST_OPERATION:
            raise PolicyDenied("invalid unlist operation")
        if _policy_identifier(parameters.get("machine_id")) != MACHINE_ID:
            raise PolicyDenied("unlist target machine identity mismatch")
        if proposal.resource_ids != (f"vast-machine:{MACHINE_ID}",):
            raise PolicyDenied("unlist resource identity mismatch")

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence:
        self.validate(proposal)
        return self._current_evidence(proposal)

    def dispatch(
        self, proposal: ActionProposal, execution_id: str
    ) -> DispatchResult:
        try:
            self.validate(proposal)
        except (PolicyDenied, VastActionError):
            return DispatchResult(execution_id, DispatchStatus.FAILED, "validation failed")
        deadline = self._monotonic() + self._rest.timeout_seconds
        if proposal.action_class is ActionClass.REMOVE_SELF_TEST_RESOURCE:
            return self._destroy(proposal, execution_id, deadline)
        try:
            # This is the exact-target/current-source revalidation directly
            # before the only supported machine-level write.
            self._current_evidence(proposal)
        except PolicyDenied:
            return DispatchResult(
                execution_id, DispatchStatus.FAILED, "immediate evidence rejected"
            )
        try:
            self._rest.unlist_machine(deadline=deadline)
        except (HttpClientError, VastActionError):
            return DispatchResult(
                execution_id, DispatchStatus.UNKNOWN, "unlist dispatch uncertain"
            )
        return DispatchResult(execution_id, DispatchStatus.SUCCEEDED, "machine unlisted")

    def _destroy(
        self, proposal: ActionProposal, execution_id: str, deadline: float
    ) -> DispatchResult:
        instance_id = int(proposal.parameters["instance_id"])
        try:
            identity = self._rest.verify_instance(instance_id, deadline=deadline)
            proof = self._capture_stale_self_test(proposal, identity)
            self._validate_stale_self_test(proposal, identity, proof)
            # Recapture target/rental/source bindings after the API identity
            # read and ownership proof, immediately before DELETE.
            self._current_evidence(proposal)
        except (HttpClientError, PolicyDenied, VastActionError, TypeError, ValueError):
            return DispatchResult(
                execution_id, DispatchStatus.FAILED, "self-test identity rejected"
            )
        try:
            self._rest.destroy_instance(instance_id, deadline=deadline)
        except (HttpClientError, VastActionError):
            # Once DELETE is attempted, even an HTTP error or malformed 2xx
            # cannot prove that the server did not apply the action.
            return DispatchResult(
                execution_id, DispatchStatus.UNKNOWN, "destroy dispatch uncertain"
            )
        return DispatchResult(
            execution_id, DispatchStatus.SUCCEEDED, "stale self-test destroyed"
        )

    def postflight(
        self,
        proposal: ActionProposal,
        execution_id: str,
        result: DispatchResult,
    ) -> PostActionEvidence:
        try:
            captured = self._capture_postflight(proposal, execution_id, result)
        except Exception:
            return PostActionEvidence(
                execution_id, None, False, "post-action evidence unavailable"
            )
        if (
            not isinstance(captured, PostActionEvidence)
            or captured.execution_id != execution_id
            or not _valid_evidence_ref(captured.evidence_ref)
            or not isinstance(captured.postcondition_ok, bool)
        ):
            return PostActionEvidence(
                execution_id, None, False, "post-action evidence rejected"
            )
        return PostActionEvidence(
            execution_id,
            captured.evidence_ref,
            captured.postcondition_ok,
            "post-action evidence captured",
        )

    def reconcile(
        self, proposal: ActionProposal, execution_id: str
    ) -> DispatchResult:
        """Read only: GET for destroy, and no request for unlisting."""
        try:
            self.validate(proposal)
        except (PolicyDenied, VastActionError):
            return DispatchResult(
                execution_id, DispatchStatus.UNKNOWN, "reconciliation rejected"
            )
        if proposal.action_class is ActionClass.LISTING_CHANGE:
            return DispatchResult(
                execution_id,
                DispatchStatus.UNKNOWN,
                "no read-only unlist reconciliation contract",
            )
        instance_id = int(proposal.parameters["instance_id"])
        deadline = self._monotonic() + self._rest.timeout_seconds
        try:
            self._rest.verify_instance(instance_id, deadline=deadline)
        except HttpClientError as error:
            if error.reason == "http_status" and error.status == 404:
                return DispatchResult(
                    execution_id,
                    DispatchStatus.SUCCEEDED,
                    "instance absent on read-only reconciliation",
                )
        except VastActionError:
            pass
        return DispatchResult(
            execution_id, DispatchStatus.UNKNOWN, "reconciliation inconclusive"
        )

    def _current_evidence(self, proposal: ActionProposal) -> PreActionEvidence:
        try:
            evidence = self._capture_evidence(proposal)
        except Exception:
            raise PolicyDenied("current Vast action evidence unavailable") from None
        if not isinstance(evidence, PreActionEvidence):
            raise PolicyDenied("current Vast action evidence malformed")
        try:
            now = require_utc(self._clock(), "clock")
            if (
                evidence.machine_id != MACHINE_ID
                or evidence.target_identity_verified is not True
            ):
                raise PolicyDenied("immediate Vast target verification failed")
            if evidence.evidence_revision != proposal.evidence_revision:
                raise PolicyDenied("Vast action evidence revision changed")
            if evidence.resource_ids != proposal.resource_ids:
                raise PolicyDenied("Vast action resources changed")
            if evidence.rental_impacts != proposal.rental_impacts:
                raise PolicyDenied("Vast rental bindings changed")
            if evidence.affected_domains != proposal.affected_domains:
                raise PolicyDenied("Vast affected domains changed")
            if not _valid_evidence_ref(evidence.evidence_ref):
                raise PolicyDenied("current Vast evidence was not preserved")
            proposed = {
                item.source: item.revision for item in proposal.source_bindings
            }
            if any(not isinstance(item, SourceState) for item in evidence.sources):
                raise PolicyDenied("Vast source bindings changed")
            current = {item.source: item for item in evidence.sources}
            if (
                len(current) != len(evidence.sources)
                or set(current) != set(proposed)
            ):
                raise PolicyDenied("Vast source bindings changed")
            for source, revision in proposed.items():
                state = current[source]
                age = now - require_utc(state.observed_at, "observed_at")
                if (
                    state.available is not True
                    or state.revision != revision
                    or age.total_seconds() < 0
                    or age > MAX_SOURCE_AGE
                ):
                    raise PolicyDenied("Vast source binding is not current")
        except PolicyDenied:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PolicyDenied("current Vast action evidence malformed") from None
        return evidence

    @staticmethod
    def _validate_stale_self_test(
        proposal: ActionProposal,
        identity: VastInstanceIdentity,
        proof: StaleSelfTestEvidence,
    ) -> None:
        if (
            not isinstance(proof, StaleSelfTestEvidence)
            or not _valid_evidence_ref(proof.evidence_ref)
            or any(
                not isinstance(item, SourceBinding) for item in proof.source_bindings
            )
        ):
            raise PolicyDenied("stale self-test proof unavailable")
        bindings = {item.source: item.revision for item in proof.source_bindings}
        proposed = {item.source: item.revision for item in proposal.source_bindings}
        if (
            proof.instance_id != identity.instance_id
            or proof.machine_id != identity.machine_id
            or identity.machine_id != MACHINE_ID
            or proof.controller_owned is not True
            or proof.self_test is not True
            or proof.stale is not True
            or len(bindings) != len(proof.source_bindings)
            or bindings != proposed
        ):
            raise PolicyDenied("instance is not a proven controller-owned stale self-test")


def _strict_identifier(value: object, reason: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_INSTANCE_ID
    ):
        raise VastActionError(reason)
    return value


def _policy_identifier(value: object) -> int:
    try:
        return _strict_identifier(value, "invalid_identifier")
    except VastActionError:
        raise PolicyDenied("action identifier must be a strict positive integer") from None


def _instance_path(instance_id: int) -> str:
    return f"/api/v0/instances/{instance_id}/"


def _valid_bounded_text(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= limit


def _valid_evidence_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 512
        and "\r" not in value
        and "\n" not in value
    )


def _validate_destroy_success(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise VastActionError("malformed_destroy_response")
    if not {"success"} <= set(payload) <= {"success", "msg"}:
        raise VastActionError("malformed_destroy_response")
    if "msg" in payload and not _valid_bounded_text(payload["msg"], 512):
        raise VastActionError("malformed_destroy_response")
