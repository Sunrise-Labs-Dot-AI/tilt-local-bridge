"""Safety regression coverage for Raspberry Pi replacement guidance."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MigrationDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guide = (ROOT / "docs" / "MIGRATION.md").read_text(
            encoding="utf-8"
        )
        self.normalized = " ".join(self.guide.split())

    def test_primary_docs_link_to_migration_guide(self) -> None:
        for path in (ROOT / "README.md", ROOT / "docs" / "SETUP.md"):
            with self.subTest(path=path.name):
                self.assertIn("MIGRATION.md", path.read_text(encoding="utf-8"))

    def test_old_bridge_is_fenced_before_new_bridge_is_enabled(self) -> None:
        fence = self.guide.index("disable --now tilt-local-bridge.service")
        enable = self.guide.index("## Enable the new bridge")
        self.assertLess(fence, enable)
        self.assertIn("exactly one active bridge", self.normalized)
        self.assertIn("old bridge still fenced", self.guide)

    def test_recovery_is_offline_and_read_only_first(self) -> None:
        self.assertIn("check-runtime --expect-shade-reads", self.guide)
        self.assertIn("probe every configured shade sequentially", self.guide)
        self.assertIn("does not send a movement command", self.normalized)
        self.assertLess(
            self.guide.index("## Validate before service mode"),
            self.guide.index("## Enable the new bridge"),
        )

    def test_missing_keys_do_not_fall_through_to_pairing(self) -> None:
        self.assertIn("Do not re-pair as a recovery shortcut", self.guide)
        self.assertIn("a separate, explicit approval step", self.normalized)
        self.assertNotIn("--permit-live-pairing", self.guide)

    def test_custom_broker_is_a_separate_gate(self) -> None:
        self.assertIn(
            "The Tilt Local Bridge installer does not install an MQTT broker",
            self.normalized,
        )
        self.assertIn("If the old Pi also hosted a custom broker, stop here", self.guide)
        self.assertIn("DHCP reservation or a reliable local DNS name", self.guide)

    def test_transfer_does_not_weaken_ssh_or_secret_handling(self) -> None:
        self.assertIn("stream the protected directory directly", self.normalized)
        self.assertIn("contains no symlinks", self.normalized)
        self.assertNotIn("StrictHostKeyChecking=no", self.guide)
        self.assertNotIn("chmod 777", self.guide)


if __name__ == "__main__":
    unittest.main()
