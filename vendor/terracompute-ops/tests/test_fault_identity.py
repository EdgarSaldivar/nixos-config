import unittest
from terracompute_ops.incidents import bounded_evidence, classify, stable_signature


class FaultIdentityTests(unittest.TestCase):
    def test_severity_and_prose_changes_keep_identity_but_gpu_uuid_does_not(self):
        event = {"fault_family": "gpu", "code": "missing", "severity": "warning",
                 "message": "initial", "evidence": {"uuid": "GPU-a", "pci_bdf": "0000:01:00.0"}}
        changed = {**event, "severity": "critical", "message": "worse",
                   "evidence": {"uuid": "GPU-a", "pci_bdf": "0000:02:00.0"}}
        self.assertEqual(stable_signature(event), stable_signature(changed))
        changed["evidence"]["uuid"] = "GPU-b"
        self.assertNotEqual(stable_signature(event), stable_signature(changed))
        self.assertEqual(classify(changed)["severity"], "critical")

    def test_unresolved_locations_remain_distinct_and_api_fields_redact(self):
        event = {"fault_family": "gpu", "code": "missing", "evidence": {"uuid": "unknown", "pci_bdf": "0000:01:00.0"}}
        changed = {**event, "evidence": {"uuid": "unknown", "pci_bdf": "0000:02:00.0"}}
        self.assertNotEqual(stable_signature(event), stable_signature(changed))
        self.assertEqual(bounded_evidence({"api_key": "sentinel", "nested": {"access-key": "sentinel"}}),
                         {"api_key": "[REDACTED]", "nested": {"access-key": "[REDACTED]"}})
