"""Digest pins in the target install scripts must match what they pin, or be placeholders.

A concrete pin that no longer matches the file shipped beside it looks correct and
refuses the right file; install-observer.sh carried one for a week.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import unittest

TARGET = pathlib.Path(__file__).resolve().parent.parent / "target"
PINS = {
    "expected_probe_sha256": "terracompute-probe.py",
    "expected_helper_sha256": "terracompute-act.py",
    "expected_proxy_sha256": "terracompute-docker-proxy.py",
}


class TargetPinTests(unittest.TestCase):
    def test_every_concrete_pin_matches_its_file(self) -> None:
        checked = 0
        for script in sorted(TARGET.glob("*.sh")):
            for line in script.read_text().splitlines():
                match = re.match(r"^(expected_[a-z]+_sha256)=([^\s#]+)", line)
                if not match or match.group(1) not in PINS:
                    continue
                value = match.group(2)
                with self.subTest(script=script.name, pin=match.group(1)):
                    if value.startswith("REPLACE_WITH_"):
                        continue
                    actual = hashlib.sha256((TARGET / PINS[match.group(1)]).read_bytes()).hexdigest()
                    self.assertEqual(value, actual, f"{script.name} pins a stale digest")
                    checked += 1
        self.assertGreaterEqual(checked, 0)


if __name__ == "__main__":
    unittest.main()
