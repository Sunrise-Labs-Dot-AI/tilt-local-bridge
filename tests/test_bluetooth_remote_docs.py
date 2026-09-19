"""Safety regression coverage for the Bluetooth phone remote guidance."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BluetoothRemoteDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guide = (ROOT / "docs" / "BLUETOOTH_REMOTE.md").read_text(encoding="utf-8")
        self.normalized = " ".join(self.guide.split())

    def test_security_model_precedes_installation(self) -> None:
        self.assertLess(
            self.guide.index("## How pairing works"),
            self.guide.index("## Enable the remote on the Raspberry Pi"),
        )
        for phrase in (
            "The pairing keys never leave the Raspberry Pi.",
            "The phone never talks to a shade.",
            "single-use nonce",
            "cannot be replayed",
            "The code itself is not a secret",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_all_three_approval_paths_are_documented(self) -> None:
        self.assertIn("**Approve phone pairing** button", self.normalized)
        self.assertIn("SIGUSR1", self.guide)
        self.assertIn("one pending request at a time, for two minutes", self.normalized)
        self.assertIn("picks one reachable shade and a direction at random", self.normalized)
        self.assertIn("This path needs no network at all", self.normalized)
        self.assertIn("Any other hand movement in the window denies the request", self.normalized)

    def test_two_gates_and_write_gate_are_explicit(self) -> None:
        self.assertIn("`bluetooth_remote.enabled` is `true`", self.normalized)
        self.assertIn("--allow-bluetooth-remote", self.guide)
        self.assertIn("--expect-bluetooth-remote", self.guide)
        self.assertIn("A bridge running read-only reports positions to the phone and refuses to set one.", self.normalized)

    def test_guide_keeps_the_no_stop_rule_and_radio_expectations(self) -> None:
        self.assertIn("the shade offers positions, never motion", self.normalized)
        self.assertIn("one Bluetooth radio serving every shade and the phone", self.normalized)

    def test_revocation_and_forget_are_kept_apart(self) -> None:
        self.assertIn("remote-phones remove", self.guide)
        self.assertIn("It does not revoke the phone on the bridge", self.normalized)

    def test_other_docs_link_the_guide(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/BLUETOOTH_REMOTE.md", readme)
        self.assertIn("Bluetooth phone remote", readme)
        home_assistant = (ROOT / "docs" / "HOME_ASSISTANT.md").read_text(encoding="utf-8")
        self.assertIn("(BLUETOOTH_REMOTE.md)", home_assistant)
        self.assertIn("Approve phone pairing", home_assistant)
        remotes = (ROOT / "docs" / "REMOTES.md").read_text(encoding="utf-8")
        self.assertIn("(BLUETOOTH_REMOTE.md)", remotes)
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("## Phone remote boundary", security)
        troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
        self.assertIn("## The phone cannot find the bridge", troubleshooting)
        self.assertLess(
            troubleshooting.index("## The phone cannot find the bridge"),
            troubleshooting.index("## Disable everything quickly"),
        )

    def test_protocol_summary_matches_the_implementation(self) -> None:
        from tilt_local_bridge.tilt_remote import REMOTE_SERVICE_UUID, SIGNING_PREFIX

        self.assertIn(REMOTE_SERVICE_UUID, self.guide)
        self.assertIn(SIGNING_PREFIX.decode("ascii").replace("\n", "\\n"), self.guide)
        self.assertIn("tests/fixtures/bluetooth_remote_vectors.json", self.guide)


if __name__ == "__main__":
    unittest.main()
