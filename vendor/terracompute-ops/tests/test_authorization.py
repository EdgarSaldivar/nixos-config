"""Who decides a proposed command.

The catalogue answered this by enumerating five actions, so anything else was
impossible rather than askable. The point of these tests is the DEFAULT: an
unrecognised command must reach a person, not a wall.
"""

from __future__ import annotations

import unittest

from terracompute_ops.authorization import (
    MAX_COMMAND_CHARS,
    Risk,
    classify,
    needs_a_person,
)


class RefusalTests(unittest.TestCase):
    """The one thing nobody here may do: touch somebody else's rental."""

    def test_a_rental_is_refused_however_it_is_named(self) -> None:
        for command in (
            "docker restart C.51217040",
            "docker exec C.51217040 sh",
            "docker logs  C.51217040",
            "cat /var/lib/docker/containers/C.51217040/config.v2.json",
            "docker inspect C.1",
        ):
            with self.subTest(command):
                risk, reason = classify(command)
                self.assertIs(risk, Risk.REFUSED)
                self.assertIn("customer", reason)

    def test_a_lookalike_is_not_a_rental(self) -> None:
        """Same anchoring as the proxy: docker's own --filter name=C. is a substring."""
        for command in (
            "docker restart notC.51217040",
            "docker restart C.thing",
            "docker restart myC.5",
            "docker restart vast-prometheus-1",
        ):
            with self.subTest(command):
                self.assertIsNot(classify(command)[0], Risk.REFUSED)

    def test_nothing_useful_is_smuggled_past_review(self) -> None:
        for command in ("", "   ", None, 7, "x" * (MAX_COMMAND_CHARS + 1), "ls\x00-la"):
            with self.subTest(repr(command)):
                self.assertIs(classify(command)[0], Risk.REFUSED)


class SelfServiceTests(unittest.TestCase):
    """The only path with no person in it, so it stays short and reversible."""

    def test_restarting_our_own_monitoring_needs_nobody(self) -> None:
        for command in (
            "docker restart dcgm-exporter",
            "docker restart vast-gddr6-metrics-exporter-1",
            "docker start node-exporter",
            "docker stop cadvisor",
            "systemctl restart docker",
        ):
            with self.subTest(command):
                risk, reason = classify(command)
                self.assertIs(risk, Risk.SELF, reason)

    def test_a_second_command_cannot_ride_along(self) -> None:
        """A shape matched the first half; what runs after the separator did not."""
        for command in (
            "docker restart dcgm-exporter; rm -rf /",
            "docker restart dcgm-exporter && reboot",
            "docker restart dcgm-exporter | sh",
            "docker restart $(cat /tmp/x)",
            "docker restart `cat /tmp/x`",
            "docker restart dcgm-exporter\nreboot",
        ):
            with self.subTest(command):
                risk, _ = classify(command)
                self.assertIsNot(risk, Risk.SELF, "a compound command ran unattended")

    def test_a_compound_command_is_asked_about_not_refused(self) -> None:
        """A person may well want one run. It just cannot go without asking."""
        risk, reason = classify("docker restart a && docker restart b")
        self.assertIs(risk, Risk.APPROVAL)
        self.assertIn("more than one", reason)


class DefaultTests(unittest.TestCase):
    """The whole point of replacing the catalogue: unknown means ask, not refuse."""

    def test_anything_unrecognised_reaches_a_person(self) -> None:
        for command in (
            "shutdown -r +1 'approved reboot'",
            "echo 0000:a1:00.0 > /sys/bus/pci/drivers/nvidia/unbind",
            "docker run --rm nvidia/cuda:12.4.0-base nvidia-smi",
            "nvidia-smi -r -i 3",
            "some-tool-nobody-has-written-yet --fix",
            "vastai unlist-machine 17049",
        ):
            with self.subTest(command):
                risk, _ = classify(command)
                self.assertIs(risk, Risk.APPROVAL, "an action was impossible, not asked")
                self.assertTrue(needs_a_person(command))

    def test_a_reboot_is_askable(self) -> None:
        """It used to be nameable and unperformable: no adapter, so approval failed."""
        risk, _ = classify("shutdown -r +1 'terracompute: approved host reboot'")
        self.assertIs(risk, Risk.APPROVAL)

    def test_the_reason_is_written_to_be_read(self) -> None:
        """It appears beside the command in a group chat, for somebody deciding fast."""
        for command in ("shutdown -r now", "docker restart C.51217040", "docker restart x"):
            with self.subTest(command):
                reason = classify(command)[1]
                self.assertTrue(reason and reason[0].islower() or reason.startswith("that"))
                self.assertNotIn("_", reason, "that reads like a symbol, not a sentence")


if __name__ == "__main__":
    unittest.main()
