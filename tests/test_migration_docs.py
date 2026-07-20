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

    def test_position_write_gate_comes_from_installed_service(self) -> None:
        self.assertIn(
            "systemctl show --property=ExecStart --value",
            self.guide,
        )
        self.assertIn(
            "A write-enabled value in `bridge.json` is not sufficient authority",
            self.normalized,
        )
        self.assertIn(
            "old installed `ExecStart` included that exact flag",
            self.normalized,
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
        self.assertIn("actual bridge account can connect", self.normalized)
        self.assertIn("subscribe to the required command topics", self.normalized)
        self.assertIn(
            "A successful Home Assistant connection does not validate",
            self.normalized,
        )

    def test_transfer_does_not_weaken_ssh_or_secret_handling(self) -> None:
        self.assertIn("stream the protected directory directly", self.normalized)
        self.assertIn("re-check the extracted tree for symlinks", self.normalized)
        self.assertNotIn("StrictHostKeyChecking=no", self.guide)
        self.assertNotIn("chmod 777", self.guide)
        old_symlink_check = self.guide.index(
            "Reject symlinks in the old protected tree"
        )
        transfer = self.guide.index("stream the protected directory directly")
        new_symlink_check = self.guide.index(
            "Before any ownership or mode change, re-check"
        )
        ownership_change = self.guide.index(
            "Restore the expected ownership and modes"
        )
        self.assertLess(old_symlink_check, transfer)
        self.assertLess(transfer, new_symlink_check)
        self.assertLess(new_symlink_check, ownership_change)

    def test_old_protected_state_has_retirement_boundary(self) -> None:
        self.assertIn("## Retire the old protected state", self.guide)
        self.assertIn("documented rollback window", self.normalized)
        self.assertIn("Do not wipe it while rollback remains possible", self.guide)
        self.assertIn("supported secure-erasure process", self.normalized)


if __name__ == "__main__":
    unittest.main()
