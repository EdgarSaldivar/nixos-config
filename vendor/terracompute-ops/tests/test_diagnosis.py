"""A model answer is data: the contract accepts only what it recognises."""

from __future__ import annotations

import json
import unittest

from terracompute_ops.authorization import Risk

from terracompute_ops.diagnosis import (
    parse_reply,
    ours,
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
    "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
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
        self.assertEqual(finding.action.command, "docker restart dcgm-exporter")
        self.assertEqual(finding.tier, Tier.REPAIR)
        self.assertIn("docker restart dcgm-exporter", finding.action.describe())
        self.assertIsNone(finding.unsupported_request)

    def test_prose_and_code_fences_around_the_object_are_tolerated(self) -> None:
        for wrapped in (
            f"Here is my finding:\n```json\n{answer()}\n```",
            f"```\n{answer()}\n```\nHappy to dig further.",
            f"{answer()}\n",
        ):
            with self.subTest(wrapped=wrapped[:20]):
                self.assertEqual(parse_finding(wrapped).summary, GOOD["summary"])



    def test_a_tenant_command_is_reported_rather_than_carried_out(self) -> None:
        """Refusing the ACTION must not discard the finding.

        The model may genuinely conclude the answer lies inside somebody's rental. It
        does not get to act on that, and a person should read it rather than have the
        whole diagnosis thrown away over its last field.
        """
        for command in ("docker restart C.51217040", "docker exec C.51217040 sh"):
            with self.subTest(command):
                finding = parse_finding(answer(action={"command": command, "intent": "x"}))
                self.assertIsNone(finding.action, "it acted on a tenant's container")
                self.assertIn("customer", finding.unsupported_request)
                self.assertEqual(finding.summary, GOOD["summary"], "the finding was lost")

    def test_a_compound_command_is_kept_but_needs_a_person(self) -> None:
        """Not everything unusual is forbidden. Most of it is simply asked about."""
        finding = parse_finding(answer(action={
            "command": "docker restart dcgm-exporter && systemctl restart docker",
            "intent": "release the handles and settle the runtime",
        }))
        self.assertIsNotNone(finding.action)
        self.assertIs(finding.action.risk, Risk.APPROVAL)
        self.assertEqual(finding.tier, Tier.CHANGE)

    def test_something_nobody_has_done_here_is_proposable(self) -> None:
        """The whole point of dropping the catalogue: unknown means ask, not refuse.

        Every one of these was impossible before -- either unnameable, or nameable and
        unperformable because no adapter existed for it.
        """
        for command in (
            "shutdown -r +1 'approved reboot'",
            "nvidia-smi -r -i 3",
            "echo 0000:a1:00.0 > /sys/bus/pci/drivers/nvidia/unbind",
            "some-tool-nobody-has-written-yet --repair",
        ):
            with self.subTest(command):
                finding = parse_finding(answer(action={"command": command, "intent": "x"}))
                self.assertIsNotNone(finding.action, "it was impossible rather than asked")
                self.assertIs(finding.action.risk, Risk.APPROVAL)

    def test_restarting_our_own_monitoring_still_needs_nobody(self) -> None:
        finding = parse_finding(answer())
        self.assertIs(finding.action.risk, Risk.SELF)
        self.assertEqual(finding.tier, Tier.REPAIR)

    def test_an_action_that_is_not_a_command_is_refused(self) -> None:
        for action in ("restart-monitoring-container", {"intent": "no command"},
                       {"command": ""}, {"command": "   "}, {"command": 7}):
            with self.subTest(repr(action)):
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



    def test_the_containers_it_may_touch_are_whatever_is_not_a_rental(self) -> None:
        """A list of names had to be written before we knew what was on the machine.

        Two of its first three did not exist -- `gddr6-exporter` is really
        `vast-gddr6-metrics-exporter-1`, `vast-node-exporter` is `node-exporter` -- so a
        finding naming one passed validation and failed at execution with "No such
        object". A list is also stale the day the monitoring stack is replaced. What
        does not go stale is that Vast names every rental C.<digits> and a tenant cannot
        choose that name.
        """
        for name in (
            "dcgm-exporter", "node-exporter", "vast-gddr6-metrics-exporter-1",
            "cadvisor", "some-exporter-nobody-has-written-yet",
        ):
            with self.subTest(name):
                self.assertTrue(ours(name))
        for rental in ("C.51217040", "C.1"):
            with self.subTest(rental):
                self.assertFalse(ours(rental), "a tenant's container is not ours to touch")
        for bad in ("", "-leading", "two words", "a;rm -rf /", "a" * 65, None, 7):
            with self.subTest(bad):
                self.assertFalse(ours(bad))

    def test_the_name_it_may_touch_cannot_carry_a_shell(self) -> None:
        """It is pasted into `docker restart <name>`, so the shape is the escaping."""
        for character in " ;|&$`\\'\"\n\t<>()*?[]{}!#~":
            with self.subTest(character):
                self.assertFalse(ours(f"exporter{character}x"))

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
            "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
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
        self.assertEqual(finding.action.command, "docker restart dcgm-exporter")
        self.assertIsNone(finding.durable_action)
        self.assertIn("nvcr.io", finding.durable_recommendation)


    def test_a_durable_fix_that_touches_a_tenant_reaches_a_person(self):
        """The only thing refused outright is somebody else's rental.

        It used to be anything outside a list of five, which is why the durable answer
        so often arrived as prose: there was no way to say it.
        """
        finding = self.finding(durable={"action": {
            "command": "docker update --restart=no C.51217040", "intent": "stop it"}})
        self.assertIsNone(finding.durable_action)
        self.assertIn("customer", finding.durable_unsupported)

    def test_a_durable_fix_nobody_thought_of_is_now_expressible(self):
        finding = self.finding(durable={"action": {
            "command": "docker run -d --name dcgm-exporter --gpus 0,1 nvcr.io/nvidia/dcgm-exporter:4.1",
            "intent": "pin the exporter to GPUs no VM will take",
        }})
        self.assertIsNotNone(finding.durable_action, "it had to be written as prose again")
        self.assertIn("nvcr.io", finding.durable_action.command)

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
            "action": {"command": "docker restart dcgm-exporter", "intent": "release its GPU handles"},
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
        self.assertEqual(finding.action.command, "docker restart dcgm-exporter")
        self.assertIn("nvcr.io/nvidia/k8s/dcgm-exporter", finding.durable_recommendation)
        self.assertTrue(finding.palliative, "a stopgap that does not say so is the bug")
        self.assertEqual(len(finding.evidence), 9, "nine citations must not cost the answer")
        self.assertEqual(finding.confidence, "medium")

    def test_the_contract_asks_for_what_this_answer_gives(self):
        """Two fields for one question is how the durable answer went undisplayed."""
        contract = contract_text()
        self.assertIn("durable", contract)
        self.assertNotIn('"prevention"', contract)


class SteerSubjectTests(unittest.TestCase):
    """A steer may name a GPU or any other fault.

    Only PCI addresses could be named before, so an incident with no GPU -- a BMC
    fault, a capacity fault -- could be looked at once per episode and never again,
    however plainly somebody asked. An operator asking is the other half of "re-fire
    when the fault changes", so it has to reach every fault.
    """

    def test_a_gpu_can_still_be_named(self) -> None:
        _prose, steer = parse_reply("Looking again.\nSTEER: look-again 0000:a1:00.0")
        self.assertIsNotNone(steer)
        self.assertEqual(steer.argument, "0000:a1:00.0")

    def test_an_incident_key_can_be_named(self) -> None:
        key = "50c1d47a1bd167e98adf3271a3954d09d05063edc0075848893822dbc7712def"
        _prose, steer = parse_reply(f"On it.\nSTEER: look-again {key}")
        self.assertIsNotNone(steer, "a fault with no GPU could not be asked about")
        self.assertEqual(steer.argument, key)

    def test_a_subject_that_is_neither_is_dropped(self) -> None:
        """An unparseable steer is never guessed at; the reply still reaches them."""
        for bad in ("../../etc", "not-a-key", "0000:zz:00.0", "'; rm -rf /"):
            with self.subTest(bad):
                prose, steer = parse_reply(f"Sure.\nSTEER: look-again {bad}")
                self.assertIsNone(steer)
                self.assertIn("Sure.", prose)


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
        self.assertEqual(result.action.command, "docker restart dcgm-exporter")

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


