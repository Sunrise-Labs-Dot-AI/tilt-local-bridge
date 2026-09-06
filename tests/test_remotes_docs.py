"""Safety regression coverage for the physical-remote guidance."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RemotesDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guide = (ROOT / "docs" / "REMOTES.md").read_text(encoding="utf-8")
        self.normalized = " ".join(self.guide.split())

    def test_no_stop_rule_is_stated_before_any_wiring(self) -> None:
        self.assertLess(
            self.guide.index("## Buttons select positions, never motion"),
            self.guide.index("## Import the blueprint"),
        )
        self.assertIn("publishes no stop command", self.normalized)
        self.assertIn("`payload_stop` to `null`", self.normalized)

    def test_guide_explains_that_lutron_binding_does_not_gate_home_assistant(
        self,
    ) -> None:
        self.assertIn(
            "Home Assistant receives the press independently", self.normalized
        )
        self.assertIn("Reload", self.guide)

    def test_guide_warns_off_the_integration_button_entities(self) -> None:
        self.assertIn("Those are outbound", self.normalized)
        self.assertIn("Leave them disabled", self.normalized)

    def test_guide_sets_expectations_about_slow_shared_hardware(self) -> None:
        for phrase in (
            "one Bluetooth radio serves every shade",
            "a press is not instant",
            "the bridge talks to them one at a time",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_guide_keeps_remote_input_off_the_bridge_pi(self) -> None:
        self.assertIn(
            "Do not put remote input on the bridge Raspberry Pi itself",
            self.normalized,
        )
        self.assertIn("anything in radio range move them", self.normalized)

    def test_guide_tells_the_reader_how_to_find_other_button_names(self) -> None:
        self.assertIn("lutron_caseta_button_event", self.guide)
        self.assertIn("ignores button names it does not recognise", self.normalized)

    def test_home_assistant_guide_links_the_remote_guide(self) -> None:
        linked = (ROOT / "docs" / "HOME_ASSISTANT.md").read_text(encoding="utf-8")
        self.assertIn("(REMOTES.md)", linked)
        self.assertIn("dead middle button", linked)

    def test_readme_lists_the_remote_step(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/REMOTES.md", readme)


if __name__ == "__main__":
    unittest.main()
