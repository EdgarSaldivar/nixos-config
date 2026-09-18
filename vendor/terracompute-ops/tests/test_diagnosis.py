"""A model answer is data: the contract accepts only what it recognises."""

from __future__ import annotations

import json
import unittest

from terracompute_ops.diagnosis import (
    CATALOGUE,
    MAX_TEXT_CHARS,
    Finding,
    FindingRejected,
    Tier,
    contract_text,
    parse_finding,
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

    def test_a_finding_is_immutable(self) -> None:
        finding = parse_finding(answer())
        with self.assertRaises(Exception):
            finding.summary = "changed"  # type: ignore[misc]
        self.assertIsInstance(finding, Finding)


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


if __name__ == "__main__":
    unittest.main()
