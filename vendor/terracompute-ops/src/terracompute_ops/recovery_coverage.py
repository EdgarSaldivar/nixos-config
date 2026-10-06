"""Small producer coverage contract, independent of classification and incident identity.

Coverage rows name a check (``family:actual-event-code`` plus component when
needed to distinguish predicates on the same device), a stable resource,
pass/fail/unknown, and an evidence pointer in the retained batch document. No
row means unknown. ``complete`` alone never proves that a device still exists.
"""
from __future__ import annotations

from typing import Any


def condition(event: dict[str, Any]) -> tuple[str, str]:
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    resource = (event.get("uuid") or event.get("gpu_uuid") or evidence.get("uuid")
                or evidence.get("gpu_uuid") or event.get("device")
                or event.get("pci_bdf") or evidence.get("pci_bdf")
                or event.get("component") or evidence.get("service") or "host")
    check = f"{event.get('fault_family', 'unknown')}:{event.get('code', 'unknown')}"
    component = event.get("component")
    if component and component != resource:
        check += "@" + str(component)
    return check, str(resource)


def validate_coverage(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 512:
        raise ValueError("coverage must be a list of at most 512 checks")
    result = []
    identities = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("coverage rows must be objects")
        normalized = {}
        for key in ("check", "resource", "result", "evidence_ref"):
            item = row.get(key)
            if not isinstance(item, str) or not item or len(item) > 512:
                raise ValueError(f"coverage {key} must be a bounded nonempty string")
            normalized[key] = item
        if normalized["result"] not in {"pass", "fail", "unknown"}:
            raise ValueError("coverage result must be pass, fail or unknown")
        identity = (normalized["check"], normalized["resource"])
        if identity in identities:
            raise ValueError("duplicate coverage check/resource")
        identities.add(identity)
        result.append(normalized)
    return result


def evidence_pointer(document: Any, pointer: str) -> bool:
    """Only actual JSON pointers into this retained measurement are accepted."""
    if pointer == "/":
        return True
    if not pointer.startswith("/"):
        return False
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            document = document[int(part)] if isinstance(document, list) else document[part]
        return document is not None
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def producer_coverage(probe: dict[str, Any], conditions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Explicit bounded adapters for the existing collectors, not a rule engine.

    Unknown collectors/codes fail closed. Local checks are independent: a failed
    kernel read does not erase an observed service state. Device checks require
    stable identity in this sample; a location-only GPU cannot prove replacement
    did not occur and therefore remains unknown.
    """
    events = probe.get("events", [])
    failures = {condition(event): index for index, event in enumerate(events)}
    snapshot = probe.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    source = probe.get("source", "target-probe")
    complete = probe.get("complete") is True
    rows = {}
    for index, event in enumerate(events):
        check, resource = condition(event)
        rows[(check, resource)] = dict(check=check, resource=resource, result="fail", evidence_ref=f"/events/{index}")
    for previous in conditions[:384]:
        check, resource = previous["check_name"], previous["resource"]
        if (check, resource) in failures:
            continue
        family, _, code = check.partition(":")
        code = code.partition("@")[0]
        passed = False
        ref = "/snapshot" if snapshot else "/events"
        if source in {"ssh", "target-probe"}:
            failure_codes = [str(event.get("code", "")) for event in events if event.get("fault_family") == "probe"]
            aborted = any(code in {"event_limit_reached", "json_output_limit_reached", "collection_deadline_exceeded", "collection_aborted_unconfirmed_cleanup"} for code in failure_codes)
            gpu = snapshot.get("gpu", {})
            gpu = gpu if isinstance(gpu, dict) else {}
            devices = gpu.get("gpus", [])
            devices = devices if isinstance(devices, list) else []
            identities = {str(item.get("uuid")) for item in devices if isinstance(item, dict)}
            if family == "systemd":
                for service, state in snapshot.get("services", {}).items():
                    if code == f"service_{service.replace('-', '_')}_not_active":
                        passed = state == "active"
                        ref = "/snapshot/services/" + service.replace("~", "~0").replace("/", "~1")
            elif family in {"gpu", "xid", "aer"}:
                pci = gpu.get("pci_devices", [])
                pci = pci if isinstance(pci, list) else []
                by_bdf = {item.get("pci_bdf"): item for item in pci if isinstance(item, dict)}
                original = previous.get("original_document", {})
                original = original if isinstance(original, dict) else {}
                old_evidence = original.get("evidence", {})
                old_evidence = old_evidence if isinstance(old_evidence, dict) else {}
                old_bdf = old_evidence.get("pci_bdf") or original.get("pci_bdf")
                old_gpu = original.get("snapshot", {}).get("gpu", {}) if isinstance(original.get("snapshot"), dict) else {}
                old_gpu = old_gpu if isinstance(old_gpu, dict) else {}
                old_visible = old_gpu.get("gpus", [])
                old_visible = old_visible if isinstance(old_visible, list) else []
                old_uuid = old_evidence.get("uuid") or next((item.get("uuid") for item in old_visible
                    if isinstance(item, dict) and item.get("pci_bdf") == resource), None)
                inventory_ok = (not aborted and not any(item.startswith(("gpu_inventory", "pci_gpu_inventory"))
                    for item in failure_codes) and isinstance(gpu.get("pci_count"), int)
                    and gpu["pci_count"] == len(pci) and isinstance(gpu.get("gpus"), list))
                journal_ok = not any(item.startswith("kernel_journal") for item in failure_codes)
                if family == "gpu" and code == "pci_gpu_count_mismatch" and resource == "host":
                    expected = gpu.get("expected_count")
                    passed = (inventory_ok and type(expected) is int and expected > 0
                              and expected == old_evidence.get("expected")
                              and gpu["pci_count"] == expected)
                    ref = "/snapshot/gpu/pci_count"
                elif family == "gpu" and code in {"gpu_driver_unavailable", "gpu_vfio_handover_blocked"}:
                    device = by_bdf.get(resource)
                    # A BDF is only a location. A previously observed UUID or
                    # physical PCI root path is required to rule out replacement.
                    same = (old_bdf == resource and old_uuid and any(item.get("uuid") == old_uuid
                        and item.get("pci_bdf") == resource for item in devices if isinstance(item, dict)))
                    passed = bool(inventory_ok and same and isinstance(device, dict)
                                  and device.get("driver") == "nvidia")
                    ref = "/snapshot/gpu/pci_devices"
                elif family == "gpu" and code == "gpu_missing_from_pci":
                    passed = bool(inventory_ok and resource in identities and old_bdf
                                  and any(item.get("uuid") == resource and item.get("pci_bdf") == old_bdf
                                          for item in devices if isinstance(item, dict))
                                  and old_bdf in by_bdf)
                    ref = "/snapshot/gpu"
                elif family == "gpu" and code == "gpu_temperature_high":
                    passed = bool(inventory_ok and any(item.get("uuid") == resource
                        and isinstance(item.get("temperature_c"), int) and item["temperature_c"] < 90
                        for item in devices if isinstance(item, dict)))
                    ref = "/snapshot/gpu/gpus"
                elif family == "xid" and str(code).isdigit():
                    passed = bool(inventory_ok and journal_ok and resource in identities
                                  and old_bdf and any(item.get("uuid") == resource
                                  and item.get("pci_bdf") == old_bdf for item in devices if isinstance(item, dict)))
                    ref = "/snapshot/gpu/gpus"
                elif family == "aer" and code in {"fatal", "nonfatal", "correctable", "unknown"}:
                    # The AER event names a BDF. Correlate its original
                    # capture to a UUID, then demand the same UUID now.
                    old_uuid = old_evidence.get("uuid") or next((item.get("uuid") for item in old_visible
                        if isinstance(item, dict) and item.get("pci_bdf") == resource), None)
                    passed = bool(inventory_ok and journal_ok and old_uuid
                                  and any(item.get("uuid") == old_uuid and item.get("pci_bdf") == resource
                                          for item in devices if isinstance(item, dict)))
                    ref = "/snapshot/gpu/gpus"
            elif family == "cdi":
                passed = not aborted and not any(code.startswith("docker_journal") for code in failure_codes) and "docker" in snapshot
            elif family in {"probe", "source"}:
                passed = complete
        elif source == "bmc":
            original = previous.get("original_document", {}).get("snapshot", {}).get("resources", [])
            original_paths = {item["path"]: item for item in original}
            current_paths = {item["path"]: item for item in snapshot.get("resources", [])}
            def same_hardware(old, new):
                before, after = old.get("hardware_identity") or {}, new.get("hardware_identity") or {}
                identifiers = [name for name in ("uuid", "serial_number") if before.get(name)]
                return bool(identifiers) and all(before[name] == after.get(name) for name in identifiers)
            if code == "redfish_sensor_unhealthy":
                for item in snapshot.get("resources", []):
                    for sensor in (*item.get("power", []), *item.get("thermal", []), *item.get("sensors", [])):
                        identity = sensor.get("member_id") or sensor.get("name") or sensor.get("kind")
                        if resource == f"{item['path']}:{sensor['kind']}:{identity}":
                            old_resource = original_paths.get(item["path"], {})
                            old_sensors = [old for name in ("power", "thermal", "sensors") for old in old_resource.get(name, [])]
                            old_sensor = next((old for old in old_sensors if
                                (old.get("member_id") or old.get("name") or old.get("kind")) == identity), {})
                            passed = (same_hardware(old_sensor, sensor)
                                      and str(sensor.get("health", "")).lower() == "ok"
                                      and str(sensor.get("state", "")).lower() not in {"absent", "disabled", "unavailable"})
            elif code == "redfish_health_unhealthy":
                passed = (complete and bool(original_paths)
                    and original_paths.keys() <= current_paths.keys()
                    and all(same_hardware(item, current_paths[path]) for path, item in original_paths.items())
                    and all(
                    str(item.get("health", "")).lower() == "ok"
                    and str(item.get("state", "")).lower() not in {"absent", "disabled", "unavailable"}
                    for item in snapshot["resources"]))
            elif family == "source":
                passed = complete
        elif source == "prometheus-alerts":
            # A complete alert query proves the alert predicate is absent, not
            # hardware health. Its event code remains explicit in the contract.
            passed = complete and not snapshot.get("unknown")
        elif source in {"vast", "prometheus", "capacity-reconciliation", "market-reconciliation"}:
            passed = complete and family in {"source", "capacity", "market", "vast"}
        rows[(check, resource)] = dict(check=check, resource=resource,
                                      result="pass" if passed else "unknown", evidence_ref=ref)
    return list(rows.values())
