"""A model answer is data: the contract accepts only what it recognises."""

from __future__ import annotations

import json
import unittest

from terracompute_ops.diagnosis import (
    MONITORING_CONTAINERS,
    CATALOGUE,
    MAX_READS_PER_ROUND,
    MAX_READ_COMMAND_CHARS,
    MAX_TEXT_CHARS,
    Finding,
    FindingRejected,
    ReadRequest,
    Tier,
    contract_text,
    parse_finding,
    parse_response,
)

GOOD = {
    "summary": "GPU 0000:a1:00.0 cannot be handed to its VM rental",
    "mechanism": "The NVIDIA driver still holds the GPU while the audio function is on vfio-pci",
    "evidence": ["evidence:abc123", "incident:key-0000:a1:00.0", "target-read@gpu-processes"],
    "action": {"name": "restart-monitoring-container", "parameters": {"container": "dcgm-exporter"}},
    "expected_effect": "The GPU leaves the NVIDIA driver and the rental starts",
    "alternatives": ["A tenant process holds it; check gpu-processes for a non-monitoring pid"],
    "prevention": "Run an exporter that does not keep GPU handles open",
    "confidence": "medium",
}


def answer(**changes) -> str:
    document = dict(GOOD)
    document.update(changes)
    return json.dumps(document)


class DiagnosisContractTests(unittest.TestCase):
    def test_a_complete_answer_is_parsed(self) -> None:
        finding = parse_finding(answer())
        self.assertEqual(finding.confidence, "medium")
        self.assertEqual(finding.action.name, "restart-monitoring-container")
        self.assertEqual(finding.action.parameters, {"container": "dcgm-exporter"})
        self.assertEqual(finding.tier, Tier.REPAIR)
        self.assertEqual(finding.action.describe(), "restart-monitoring-container(container=dcgm-exporter)")
        self.assertIsNone(finding.unsupported_request)

    def test_prose_and_code_fences_around_the_object_are_tolerated(self) -> None:
        for wrapped in (
            f"Here is my finding:\n```json\n{answer()}\n```",
            f"```\n{answer()}\n```\nHappy to dig further.",
            f"{answer()}\n",
        ):
            with self.subTest(wrapped=wrapped[:20]):
                self.assertEqual(parse_finding(wrapped).summary, GOOD["summary"])

    def test_an_action_outside_the_catalogue_is_reported_not_run(self) -> None:
        finding = parse_finding(answer(action={"name": "run-shell", "parameters": {"cmd": "rm -rf /"}}))
        self.assertIsNone(finding.action)
        self.assertIsNone(finding.tier)
        self.assertEqual(finding.unsupported_request, "run-shell")

    def test_tenant_containers_and_bad_parameters_are_refused(self) -> None:
        cases = {
            "tenant container": {"name": "restart-monitoring-container", "parameters": {"container": "C.51217040"}},
            "wrong parameter name": {"name": "restart-monitoring-container", "parameters": {"name": "dcgm-exporter"}},
            "extra parameter": {"name": "restart-monitoring-container",
                                "parameters": {"container": "dcgm-exporter", "force": "yes"}},
            "missing parameters": {"name": "rebind-gpu", "parameters": {"bdf": "0000:a1:00.0"}},
            "bad address": {"name": "rebind-gpu", "parameters": {"bdf": "a1", "driver": "nvidia"}},
            "unknown driver": {"name": "rebind-gpu", "parameters": {"bdf": "0000:a1:00.0", "driver": "acme"}},
            "injection in a parameter": {"name": "restart-monitoring-container",
                                         "parameters": {"container": "dcgm-exporter; rm -rf /"}},
            "image without a tag": {"name": "replace-monitoring-container",
                                    "parameters": {"container": "dcgm-exporter", "image": "someone/exporter"}},
            "action as a string": "restart-monitoring-container",
        }
        for name, action in cases.items():
            with self.subTest(name):
                with self.assertRaises(FindingRejected):
                    parse_finding(answer(action=action))

    def test_a_finding_without_an_action_is_allowed(self) -> None:
        finding = parse_finding(answer(action=None, expected_effect=""))
        self.assertIsNone(finding.action)
        self.assertEqual(finding.summary, GOOD["summary"])

    def test_malformed_answers_are_refused(self) -> None:
        cases = {
            "empty": "",
            "not json": "I think the exporter is holding it.",
            "not an object": "[1, 2, 3]",
            "no confidence": json.dumps({k: v for k, v in GOOD.items() if k != "confidence"}),
            "bad confidence": answer(confidence="very"),
            "summary too long": answer(summary="x" * (MAX_TEXT_CHARS + 1)),
            "control characters": answer(mechanism="bad\x00text"),
            "evidence not a list": answer(evidence="evidence:abc"),
            "too many evidence refs": answer(evidence=[f"evidence:{index}" for index in range(17)]),
            "alternatives not a list": answer(alternatives={"a": 1}),
            "oversized": json.dumps(dict(GOOD, prevention="x" * 200), indent=4) + "x" * 20000,
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(FindingRejected):
                    parse_finding(text)

    def test_every_catalogue_entry_is_described_to_the_model(self) -> None:
        text = contract_text()
        for name, entry in CATALOGUE.items():
            self.assertIn(name, text)
            self.assertIn(entry.summary, text)
        self.assertIn("NOT YET IMPLEMENTED", text)
        self.assertIn("needs human approval", text)
        # Only the restart is carried out today, and only it is tier repair.
        implemented = {name for name, entry in CATALOGUE.items() if entry.implemented}
        self.assertEqual(implemented, {"restart-monitoring-container"})
        repair = {name for name, entry in CATALOGUE.items() if entry.tier is Tier.REPAIR}
        self.assertEqual(repair, {"restart-monitoring-container"})

    def test_anything_that_touches_tenants_or_the_machine_needs_approval(self) -> None:
        for name in ("replace-monitoring-container", "rebind-gpu", "destroy-rental", "reboot-host"):
            with self.subTest(name):
                self.assertIs(CATALOGUE[name].tier, Tier.CHANGE)

    def test_the_containers_it_may_touch_are_the_ones_that_exist(self) -> None:
        """Enumerated on 17049 on 2026-09-19: two of the three names here did not exist.

        A finding naming one passed validation and then failed at execution with "No
        such object", so two thirds of the only action this service can take were
        unreachable. Tenants are named C.<id> by Vast and appear in no list here.
        """
        self.assertIn("dcgm-exporter", MONITORING_CONTAINERS)
        self.assertIn("node-exporter", MONITORING_CONTAINERS)
        self.assertIn("vast-gddr6-metrics-exporter-1", MONITORING_CONTAINERS)
        for gone in ("gddr6-exporter", "vast-node-exporter"):
            self.assertNotIn(gone, MONITORING_CONTAINERS, "a name the machine does not have")
        for name in MONITORING_CONTAINERS:
            self.assertFalse(name.startswith("C."), "a tenant's container is not ours to touch")

    def test_a_finding_is_immutable(self) -> None:
        finding = parse_finding(answer())
        with self.assertRaises(Exception):
            finding.summary = "changed"  # type: ignore[misc]
        self.assertIsInstance(finding, Finding)


class TwoHorizonsTests(unittest.TestCase):
    """One action cannot answer both "restore service" and "stop this recurring"."""

    def finding(self, **changes):
        document = {
            "summary": "the exporter holds the GPU being handed over",
            "mechanism": "it opens every nvidia node and reopens them on start",
            "evidence": ["target-read@gpu-handles: PID 5466 holds nvidia0-7"],
            "action": {"name": "restart-monitoring-container",
                       "parameters": {"container": "dcgm-exporter"}},
            "expected_effect": "the handover retry succeeds",
            "alternatives": [], "prevention": "", "confidence": "high",
        }
        document.update(changes)
        return parse_finding(json.dumps(document))

    def test_a_durable_fix_can_be_named_where_no_action_expresses_it(self):
        finding = self.finding(durable={
            "action": None,
            "recommendation": "replace the stale image with nvcr.io/nvidia/k8s/dcgm-exporter",
        })
        self.assertEqual(finding.action.name, "restart-monitoring-container")
        self.assertIsNone(finding.durable_action)
        self.assertIn("nvcr.io", finding.durable_recommendation)

    def test_a_durable_fix_may_be_a_catalogued_action_of_its_own(self):
        finding = self.finding(durable={"action": {
            "name": "replace-monitoring-container",
            "parameters": {"container": "dcgm-exporter",
                           "image": "nvcr.io/nvidia/k8s/dcgm-exporter:4.1.1"},
        }})
        self.assertEqual(finding.action.tier, Tier.REPAIR)
        self.assertEqual(finding.durable_action.tier, Tier.CHANGE)

    def test_a_durable_fix_outside_the_catalogue_reaches_a_person(self):
        finding = self.finding(durable={"action": {"name": "pin-exporter-to-idle-gpus",
                                                   "parameters": {}}})
        self.assertIsNone(finding.durable_action)
        self.assertEqual(finding.durable_unsupported, "pin-exporter-to-idle-gpus")

    def test_a_stopgap_says_so_rather_than_leaving_it_to_be_noticed(self):
        finding = self.finding(recurrence={
            "expected": True, "mechanism": "the exporter reopens every GPU node",
            "ends_when": "it excludes vfio-assigned GPUs or is replaced",
        })
        self.assertTrue(finding.palliative)
        self.assertIn("reopens", finding.recurrence.mechanism)
        # Saying nothing about recurrence is not the same as claiming there is none.
        self.assertFalse(self.finding().palliative)
        self.assertIsNone(self.finding().recurrence)

    def test_recurrence_must_commit_rather_than_hedge(self):
        for bad in ({"expected": "maybe"}, {"mechanism": "x"}, {"expected": 1}):
            with self.assertRaises(FindingRejected):
                self.finding(recurrence=bad)


class EvidenceIsForAPersonTests(unittest.TestCase):
    def test_a_finding_is_not_thrown_away_over_the_shape_of_its_citations(self):
        """The action is bound to the catalogue; the evidence is prose for a human.

        A real diagnosis was refused for writing its evidence as sentences rather
        than reference tokens, and the whole finding -- including a correct refusal
        to act on ambiguous telemetry -- was discarded over formatting.
        """
        finding = parse_finding(json.dumps({
            "summary": "the handover cannot be confirmed from what is available",
            "mechanism": "every target read returned ActorError, so the binding is unknown",
            "evidence": [
                "Target status at 2026-09-18T05:04:48Z still lists 0000:a1:00.0 blocked.",
                "containers, exporter logs and GPU handles are all unavailable with ActorError.",
            ],
            "action": None,
            "expected_effect": "nothing is disrupted while the binding is unverified",
            "alternatives": ["the GPU is already bound to vfio-pci and the alert is stale"],
            "prevention": "carry the current driver and blocking owners in the alert",
            "confidence": "medium",
        }))
        self.assertIsNone(finding.action)
        self.assertEqual(len(finding.evidence), 2)

    def test_the_punctuation_a_model_writes_does_not_lose_a_diagnosis(self):
        """An em dash cost a whole finding, including its refusal to act."""
        finding = parse_finding(json.dumps({
            "summary": "the handover cannot be confirmed",
            "mechanism": "7 of 8 GPUs visible is consistent with\u2014but not proof of\u2014a detach",
            "evidence": ["status still lists 0000:a1:00.0 blocked \u2014 and every read failed"],
            "action": None, "expected_effect": "nothing is disrupted",
            "alternatives": ["the alert is stale; check the driver \u201cbound\u201d state"],
            "prevention": "carry the current driver in the alert",
            "confidence": "medium",
        }))
        self.assertIsNone(finding.action)
        self.assertIn("\u2014", finding.mechanism)

    def test_control_characters_are_still_refused(self):
        """Escapes reach a terminal; punctuation does not. Only the first is a hazard."""
        for bad in ("\x1b[31mred", "line\x00break", "bell\x07"):
            with self.assertRaises(FindingRejected):
                parse_finding(json.dumps({
                    "summary": bad, "mechanism": "m", "evidence": ["e"], "action": None,
                    "expected_effect": "e", "alternatives": [], "prevention": "p",
                    "confidence": "low",
                }))

    def test_an_action_is_still_bound_to_the_catalogue(self):
        """Relaxing the citations must not relax what may be asked for."""
        def finding(action):
            return json.dumps({
                "summary": "s", "mechanism": "m", "evidence": ["a sentence, with commas"],
                "action": action, "expected_effect": "e", "alternatives": [],
                "prevention": "p", "confidence": "low",
            })

        # A parameter outside its accepted values is refused outright.
        with self.assertRaises(FindingRejected):
            parse_finding(finding(
                {"name": "restart-monitoring-container", "parameters": {"container": "; id"}}))
        # An action nobody has heard of is reported to a person rather than refused:
        # wanting something we cannot do is worth reading, and cannot be carried out.
        parsed = parse_finding(finding({"name": "rm -rf /", "parameters": {}}))
        self.assertIsNone(parsed.action)
        self.assertEqual(parsed.unsupported_request, "rm -rf /")


class ARealAnswerSurvivesTests(unittest.TestCase):
    """The answer the deployed model actually produced, parsed as it arrives.

    Captured from a live turn against imladris on 2026-09-18, with everything about it
    a contract written in the abstract tends to reject: a sentence of prose before the
    JSON, curly quotes, URLs, nine citations, and paragraphs longer than anything a
    handwritten fixture contains. Three separate over-strict checks have each discarded
    a complete, correct diagnosis in production; this is the shape that keeps happening.
    """

    ANSWER = (
        "I\u2019m checking the installed DCGM exporter image\u2019s upstream status, then "
        "I\u2019ll separate the safest immediate recovery from the durable fix."
        + json.dumps({
            "summary": "The all-GPU dcgm-exporter is holding NVIDIA device handles open.",
            "mechanism": "target-read@gpu-handles shows dcgm-exporter PID 5466 holding "
                         "nvidia0 through nvidia7 \u2014 an open NVML handle makes the "
                         "unbind required for VFIO return busy.",
            "evidence": [
                "target-read@gpu-handles: dcgm-exporter holds every numbered device.",
                "target-read@exporter-logs: \"Initializing system entities of type: GPU\"",
                "target-read@gpu-processes: no compute processes were reported.",
                "target-read@pci-errors: no non-zero PCIe error counters.",
                "target status: 8 PCI GPUs but only 7 NVIDIA-visible.",
                "Project check: https://hub.docker.com/u/jjziets reports the image last "
                "updated about two years ago.",
                "https://github.com/jjziets/DCMontoring is archived and read-only since "
                "2026-01-30.",
                "NVIDIA's maintained upstream publishes current pinned images: "
                "https://github.com/NVIDIA/dcgm-exporter/blob/main/dcgm-exporter.yaml",
                "target-read@containers: dcgm-exporter runs jjziets/dcgm-exporter:latest.",
            ],
            "action": {"name": "restart-monitoring-container",
                       "parameters": {"container": "dcgm-exporter"}},
            "durable": {
                "action": None,
                "recommendation": "Replace the abandoned jjziets/dcgm-exporter:latest "
                                  "with the maintained nvcr.io/nvidia/k8s/dcgm-exporter; "
                                  "replacement alone will not prevent recurrence if it "
                                  "still opens every GPU.",
            },
            "recurrence": {
                "expected": True,
                "mechanism": "The exporter reopens every NVIDIA-bound device on start.",
                "ends_when": "Monitoring no longer opens GPUs eligible for VFIO.",
            },
            "expected_effect": "The handover retry binds 0000:a1:00.0 to vfio-pci.",
            "alternatives": ["nvidia-persistenced also holds most device nodes."],
            "confidence": "medium",
        })
    )

    def test_it_parses_and_keeps_both_horizons(self):
        finding = parse_finding(self.ANSWER)
        self.assertEqual(finding.action.name, "restart-monitoring-container")
        self.assertIn("nvcr.io/nvidia/k8s/dcgm-exporter", finding.durable_recommendation)
        self.assertTrue(finding.palliative, "a stopgap that does not say so is the bug")
        self.assertEqual(len(finding.evidence), 9, "nine citations must not cost the answer")
        self.assertEqual(finding.confidence, "medium")

    def test_the_contract_asks_for_what_this_answer_gives(self):
        """Two fields for one question is how the durable answer went undisplayed."""
        contract = contract_text()
        self.assertIn("durable", contract)
        self.assertNotIn('"prevention"', contract)


if __name__ == "__main__":
    unittest.main()


class ReadRequestTests(unittest.TestCase):
    """Before it concludes, the model may ask to look -- and that is not a finding."""

    def test_a_read_request_is_recognised_and_carries_its_commands(self) -> None:
        result = parse_response(json.dumps({
            "reads_requested": ["ls -l /proc/5466/fd", "cat /sys/bus/pci/devices/0000:a1:00.0/driver"],
            "note": "confirm which process holds the device",
        }))
        self.assertIsInstance(result, ReadRequest)
        self.assertEqual(len(result.commands), 2)
        self.assertIn("confirm which process", result.note)

    def test_an_ordinary_finding_still_parses_as_a_finding(self) -> None:
        result = parse_response(answer())
        self.assertIsInstance(result, Finding)
        self.assertEqual(result.action.name, "restart-monitoring-container")

    def test_an_empty_reads_field_is_not_a_read_request(self) -> None:
        # A finding that happens to carry reads_requested: [] is still a finding.
        for empty in ([], None, "", "none"):
            result = parse_response(answer(reads_requested=empty))
            self.assertIsInstance(result, Finding, empty)

    def test_a_malformed_read_request_is_rejected(self) -> None:
        for bad in (
            ["ok", ""],                                  # a blank command
            ["ok", 5],                                   # a non-string
            ["x" * (MAX_READ_COMMAND_CHARS + 1)],        # too long
            ["cmd\x00rest"],                             # a NUL
            ["c"] * (MAX_READS_PER_ROUND + 1),           # too many at once
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(FindingRejected):
                    parse_response(json.dumps({"reads_requested": bad}))

    def test_the_contract_tells_the_model_it_may_ask_to_look(self) -> None:
        contract = contract_text()
        self.assertIn("reads_requested", contract)
        self.assertIn("read-only", contract)
