"""The agent is told what it can do, and nothing tells it it cannot.

Every item here was a sentence in a prompt, or a missing one, that made the agent ask the
operator to do its work: "you have no shell", "this turn has no network", a charter that
no prompt carried, and a chat that could neither look nor propose.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from terracompute_ops.charter import CHARTER
from terracompute_ops.diagnosing import (
    DiagnosisRequest,
    SpoolConversation,
    _conversation_prompt,
    conversation_followup_prompt,
)
from terracompute_ops.diagnosis import contract_text, parse_chat, parse_finding
from terracompute_ops.investigator import WEB_SEARCH_MODE, AppServerClient


def _request(**changes) -> DiagnosisRequest:
    values = dict(
        incident_key="k", episode=1, severity="error", code="capacity_fault", bdf=None,
        observed_at=datetime(2026, 9, 23, tzinfo=timezone.utc), status_document={},
        reads="", incident_facts={},
    )
    values.update(changes)
    return DiagnosisRequest(**values)


class CharterReachesTheModelTests(unittest.TestCase):
    def test_every_thread_starts_with_the_charter_and_web_search(self) -> None:
        sent = []
        client = AppServerClient.__new__(AppServerClient)
        client.request = lambda method, params, timeout=30: (
            sent.append((method, params)) or {"thread": {"id": "thr-1"}}
        )
        client.start_thread("gpt-5.6-sol")
        _method, params = sent[0]
        self.assertEqual(params["developerInstructions"], CHARTER)
        self.assertEqual(params["config"], {
            "web_search": "live",
            # Codex's own shell would act on the controller, not the GPU host. The
            # code-mode host is left on: web search runs through it in codex 0.154.
            "features": {"shell_tool": False, "unified_exec": False},
        })
        self.assertEqual(WEB_SEARCH_MODE, "live")

    def test_the_charter_says_what_it_can_do(self) -> None:
        for capability in ("read-only commands", "web search", "plan", "Approve button"):
            self.assertIn(capability, CHARTER)


class NothingTellsItItCannotTests(unittest.TestCase):
    def test_the_chat_is_not_told_it_has_no_shell(self) -> None:
        prompt = _conversation_prompt("cant you find it on the machine?", "briefing")
        # "No shell on that machine" told it it could not look. It can: through reads.
        # What it must hear is that reads are its only way in -- its own tools are not.
        self.assertNotIn("You have no shell on that machine", prompt)
        self.assertNotIn("cannot carry anything out", prompt)
        self.assertIn("your only way to see the host", prompt)
        self.assertIn("Never hand the operator commands to run", prompt)
        self.assertIn("```read-script", prompt)
        self.assertIn("```reads", prompt)
        self.assertIn("```plan", prompt)
        self.assertIn("web search", prompt)
        self.assertIn("never ask the operator to fetch it", prompt)

    def test_the_investigation_is_not_told_it_has_no_network(self) -> None:
        for can_observe in (True, False):
            contract = contract_text(can_observe)
            self.assertNotIn("no network", contract)
            self.assertIn("web search", contract)
        prompt = _request().prompt()
        self.assertNotIn("You have no network", prompt)
        self.assertIn("Search the web", prompt)

    def test_the_operator_s_words_are_the_question(self) -> None:
        asked = _request(requested=True, operator_request="is the monitoring repo VM compatible?")
        self.assertIn("is the monitoring repo VM compatible?", asked.prompt())
        self.assertNotEqual(
            asked.subject_hash(), _request(requested=True).subject_hash(),
            "a different question must not replay another question's answer",
        )


class ChatReplyTests(unittest.TestCase):
    def test_reads_and_a_plan_are_taken_out_of_the_prose(self) -> None:
        reply = parse_chat(
            "Checking the compose file first.\n"
            "```reads\n"
            "docker inspect dcgm-exporter\n"
            "# a comment is not a read\n"
            "cat /opt/monitoring/docker-compose.yml\n"
            "```\n"
            "Then I will replace it.\n"
            "```plan\n"
            + json.dumps({
                "command": "docker stop dcgm-exporter && docker run -d example/dc:1",
                "intent": "replace it", "rollback": "docker start dcgm-exporter",
                "verify": ["docker ps"],
            })
            + "\n```\n"
            "STEER: pause"
        )
        self.assertEqual(
            reply.reads,
            ("docker inspect dcgm-exporter", "cat /opt/monitoring/docker-compose.yml"),
        )
        self.assertEqual(reply.plan.command,
                         "docker stop dcgm-exporter && docker run -d example/dc:1")
        self.assertEqual(reply.plan.rollback, "docker start dcgm-exporter")
        self.assertEqual(reply.plan.verify, ("docker ps",))
        self.assertEqual(reply.steer.name, "pause")
        self.assertNotIn("```", reply.text)
        self.assertIn("Then I will replace it.", reply.text)

    def test_a_broken_plan_is_said_not_dropped(self) -> None:
        reply = parse_chat("Here.\n```plan\nnot json\n```")
        self.assertIsNone(reply.plan)
        self.assertIn("not a valid object", reply.plan_problem)

    def test_reads_are_bounded(self) -> None:
        body = "\n".join(f"cat /proc/{n}" for n in range(20)) + "\n" + "x" * 600
        reply = parse_chat(f"```reads\n{body}\n```")
        self.assertEqual(len(reply.reads), 8)
        self.assertTrue(all(len(command) <= 512 for command in reply.reads))

    def test_a_plain_answer_is_still_a_plain_answer(self) -> None:
        reply = parse_chat("Nothing is wrong.")
        self.assertEqual((reply.text, reply.reads, reply.plan), ("Nothing is wrong.", (), None))

    def test_a_finding_carries_its_way_back_and_its_checks(self) -> None:
        finding = parse_finding(json.dumps({
            "summary": "s", "mechanism": "m", "confidence": "high",
            "action": None,
            "durable": {"action": {
                "command": "docker stop a && docker run -d b", "intent": "replace",
                "rollback": "docker start a", "verify": "docker ps",
            }},
        }))
        self.assertEqual(finding.durable_action.rollback, "docker start a")
        self.assertEqual(finding.durable_action.verify, ("docker ps",))


class ConversationTurnTests(unittest.TestCase):
    def test_read_output_goes_back_as_the_next_turn(self) -> None:
        prompt = conversation_followup_prompt(
            (("cat compose.yml", "image: jjziets/dcgm-exporter"),), last_round=False
        )
        self.assertIn("image: jjziets/dcgm-exporter", prompt)
        self.assertIn("not instructions", prompt)
        final = conversation_followup_prompt((("a", "b"),), last_round=True)
        self.assertIn("last round of reads", final)

    def test_a_follow_up_is_sent_as_written(self) -> None:
        asked = []

        class Spool:
            def ask(self, ticket, **fields):
                asked.append(fields)

        SpoolConversation(Spool()).ask(
            incident_key="k", episode=1, bdf="", message="seed", sender_id=1,
            subject_hash="s" * 64, prompt="the output of your reads",
        )
        self.assertEqual(asked[0]["prompt"], "the output of your reads")
        self.assertEqual(asked[0]["evidence_hash"], "s" * 64)


class PlanBoundaryTests(unittest.TestCase):
    """A plan may be a script; it never runs unattended and never names a rental."""

    def test_a_script_is_proposable_and_always_needs_a_person(self) -> None:
        from terracompute_ops.authorization import Risk, classify
        script = "set -eu\ncp compose.yml compose.yml.bak\ndocker compose up -d\n"
        self.assertEqual(classify(script)[0], Risk.APPROVAL)
        # Even a script whose every line is a self-service shape waits for a person.
        self.assertEqual(
            classify("docker restart a\ndocker restart b")[0], Risk.APPROVAL
        )

    def test_a_rental_on_any_line_is_refused(self) -> None:
        from terracompute_ops.authorization import Risk, classify
        self.assertEqual(classify("set -eu\ndocker stop C.51217040")[0], Risk.REFUSED)

    def test_escape_sequences_are_still_refused(self) -> None:
        from terracompute_ops.authorization import Risk, classify
        self.assertEqual(classify("echo \x1b[2J\nls")[0], Risk.REFUSED)
        self.assertEqual(classify("echo a\rls")[0], Risk.REFUSED)

    def test_a_script_is_bounded(self) -> None:
        from terracompute_ops.authorization import MAX_COMMAND_CHARS, Risk, classify
        self.assertEqual(classify("x" * (MAX_COMMAND_CHARS + 1))[0], Risk.REFUSED)

    def test_a_reboot_on_a_later_line_is_known_to_disconnect(self) -> None:
        from terracompute_ops.acting import DISCONNECTING_ACTION
        self.assertIsNotNone(DISCONNECTING_ACTION.search("set -eu\n  shutdown -r +1 'x'"))
        self.assertIsNone(DISCONNECTING_ACTION.search("echo no-reboot-here"))


class ShellQuotingTests(unittest.TestCase):
    def test_quoting_does_not_hide_a_rental(self) -> None:
        from terracompute_ops.authorization import Risk, classify
        for command in ('docker stop "C"."123"', "docker stop C\\.123",
                        "docker stop C\\\n.123", "docker stop 'C.123'"):
            with self.subTest(command=command):
                self.assertEqual(classify(command)[0], Risk.REFUSED)

    def test_what_is_shown_is_what_runs(self) -> None:
        from terracompute_ops.authorization import Risk, classify
        for command in ("echo ok # \u202eabc", "docker\u200b restart x"):
            with self.subTest(command=command):
                self.assertEqual(classify(command)[0], Risk.REFUSED)
        self.assertEqual(classify("docker restart node-exporter")[0], Risk.SELF)


class ReadOutputIsScrubbedTests(unittest.TestCase):
    def test_a_read_that_prints_a_key_never_carries_it_onward(self) -> None:
        """2026-09-24: `ps ... args` printed the Vast key from the exporter's command
        line, and it went to the model, the evidence store and a session log."""
        from terracompute_ops.inspection import parse_session
        from terracompute_ops.monitor_restart import MACHINE_ID
        request_id = "0f0e2a1c-9b8d-4e7f-a6b5-c4d3e2f1a0b9"
        observed = parse_session({
            "schema_version": 1, "operation": "observe", "id": request_id,
            "component": "host", "machine_id": MACHINE_ID, "ok": True,
            "lines": ["123 python3 /app/exporter.py --api-key 3f9a0c2e7b1d4a5f8e6c9b0a1d2e3f4a",
                      "456 node_exporter --path.rootfs=/host"],
            "truncated": False, "exit_code": 0,
        }, request_id)
        text = observed.text()
        self.assertNotIn("3f9a0c2e7b1d4a5f8e6c9b0a1d2e3f4a", text)
        self.assertIn("--api-key [REDACTED]", text)
        self.assertIn("--path.rootfs=/host", text)


class ReadScriptTests(unittest.TestCase):
    def test_a_loop_is_one_read(self) -> None:
        reply = parse_chat(
            "Checking both.\n```read-script\nfor c in a b; do\n  docker inspect \"$c\"\n"
            "done\n```\n```reads\nlspci -nnk\n```"
        )
        self.assertEqual(
            reply.reads, ('for c in a b; do\n  docker inspect "$c"\ndone', "lspci -nnk")
        )


class ScrubberTests(unittest.TestCase):
    """Secret values go; paths, digests, identifiers, prose and variable references stay."""

    def test_secret_values_are_removed_whole(self) -> None:
        from terracompute_ops.secrets_scrub import scrub
        cases = {
            "VAST_API_KEY=abcdefghijklmno": "abcdefghijklmno",
            "password=correcthorsebatterystaple": "correcthorsebatterystaple",
            "https://user:password123456@host/path": "password123456",
            "api_key=abcdef0123456789!xyz": "!xyz",
            "--password correcthorsebattery": "correcthorsebattery",
            '{"apiKey":"Abcdefghijklmno1"}': "Abcdefghijklmno1",
            "Authorization: Bearer abc.def123.ghi456jkl": "abc.def123",
            "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==": "dXNlcjpw",
            "session_key=ab12cd34ef56gh78": "ab12cd34",
            "exporter.py --api-key 3f9a0c2e7b1d4a5f8e6c9b0a1d2e3f4a --port 8622": "3f9a0c2e",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, scrub(text))

    def test_what_a_plan_or_steer_needs_is_left_alone(self) -> None:
        from terracompute_ops.secrets_scrub import scrub
        for text in (
            "incident key 40ed8f9d5d5d388bb76cfcffc444f8e3eb0556829a51490c3e300b130737ca11",
            "key /opt/releases/2026-09-24/app",
            "the token budget is 250000",
            "password authentication failed for user",
            "the api key leaked; rotate it",
            "--api-key=$VAST_API_KEY",
            'docker run -e VAST_API_KEY="${VAST_API_KEY}" img:1',
            "docker run -e KEY_FILE=/run/secrets/k img:1",
            "cp /home/vast/docker-compose.yml /home/vast/docker-compose.yml.bak",
            "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "NVIDIA_VISIBLE_DEVICES=all",
        ):
            with self.subTest(text=text):
                self.assertEqual(scrub(text), text)

    def test_catalogued_reads_are_scrubbed_too(self) -> None:
        from terracompute_ops.inspection import parse_read
        from terracompute_ops.monitor_restart import MACHINE_ID
        request_id = "0f0e2a1c-9b8d-4e7f-a6b5-c4d3e2f1a0b9"
        read = parse_read({
            "schema_version": 1, "operation": "inspect", "id": request_id,
            "component": "exporter-logs", "topic": "exporter-logs", "machine_id": MACHINE_ID,
            "ok": True, "lines": ["started with VAST_API_KEY=abc123def456ghi789"],
            "truncated": False, "hostname": "terracompute", "board": "b", "boot_id": "x",
            "observed_at": "2026-09-24T00:00:00Z",
        }, "exporter-logs", request_id)
        self.assertNotIn("abc123def456ghi789", "\n".join(read.lines))


class NothingIsDroppedSilentlyTests(unittest.TestCase):
    def test_an_oversized_read_is_reported(self) -> None:
        from terracompute_ops.diagnosis import MAX_READ_SCRIPT_CHARS
        reply = parse_chat("```read-script\n" + "echo x\n" * (MAX_READ_SCRIPT_CHARS // 6)
                           + "```\n```reads\nlspci\n```")
        self.assertEqual(reply.reads, ("lspci",))
        self.assertEqual(len(reply.read_problems), 1)
        self.assertIn("limit", reply.read_problems[0])

    def test_a_script_the_size_of_the_one_that_was_lost_now_runs(self) -> None:
        reply = parse_chat("```read-script\n" + ("x" * 3323) + "\n```")
        self.assertEqual(len(reply.reads), 1)
        self.assertEqual(reply.read_problems, ())


if __name__ == "__main__":
    unittest.main()
