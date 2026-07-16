"""Safety regression coverage for public recovery guidance."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TroubleshootingDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guide = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(
            encoding="utf-8"
        )
        self.normalized = " ".join(self.guide.split())

    def test_all_shades_offline_checks_mqtt_before_ble(self) -> None:
        self.assertLess(
            self.guide.index("## All shade entities go offline at once"),
            self.guide.index("## The shade appears offline while moving"),
        )
        self.assertIn(
            "In the default setup, the bridge Pi is the MQTT client",
            self.normalized,
        )
        self.assertIn("check the MQTT path before BLE", self.normalized)

    def test_recovery_does_not_pair_rotate_or_move(self) -> None:
        required = (
            "become available without moving a shade",
            "Do not rotate MQTT credentials or certificates",
            "re-pair shades as the first response",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_custom_tls_topology_is_explicitly_conditional(self) -> None:
        self.assertIn(
            "If you deliberately run a custom broker on the bridge Pi",
            self.normalized,
        )
        self.assertIn("If that custom connection uses TLS", self.normalized)


if __name__ == "__main__":
    unittest.main()
