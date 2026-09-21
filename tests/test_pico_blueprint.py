"""Static contract checks for the reusable Pico shade-control blueprint."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = (
    ROOT
    / "blueprints"
    / "automation"
    / "tilt_local_bridge"
    / "pico_shade_control.yaml"
)


class PicoBlueprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blueprint = BLUEPRINT.read_text(encoding="utf-8")

    def test_blueprint_never_stops_a_cover(self) -> None:
        """The bridge publishes no stop command, so stopping must never appear."""
        self.assertNotIn("cover.stop_cover", self.blueprint)
        self.assertNotIn("cover.open_cover", self.blueprint)
        self.assertNotIn("cover.close_cover", self.blueprint)
        self.assertIn("cover.set_cover_position", self.blueprint)

    def test_trigger_is_scoped_to_one_remote_and_one_edge(self) -> None:
        """Other remotes share the event type, and one edge only, so a press never double-fires.

        The edge is the release: the Smart Bridge has delivered a release with no
        press before it, never the reverse, so the release is the one to act on.
        """
        self.assertIn("event_type: lutron_caseta_button_event", self.blueprint)
        self.assertIn("device_id: !input pico_device", self.blueprint)
        self.assertIn("action: release", self.blueprint)
        self.assertNotIn("action: press", self.blueprint)

    def test_every_button_maps_to_a_position(self) -> None:
        for phrase in (
            "{{ button == 'on' }}",
            "{{ button == 'off' }}",
            "{{ button == 'stop' }}",
            "{{ button in ['raise', 'lower'] }}",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.blueprint)

    def test_step_moves_one_rung_in_the_pressed_direction(self) -> None:
        """Pin the arithmetic; changing it requires re-verifying the ladder."""
        self.assertIn(
            "{% set rung = (cur // step) if up else -((-cur) // step) %}",
            self.blueprint,
        )
        self.assertIn(
            "{% set snapped = (rung + (1 if up else -1)) * step %}",
            self.blueprint,
        )

    def test_step_result_is_clamped_to_the_cover_range(self) -> None:
        self.assertIn("{{ [[snapped, 0] | max, 100] | min }}", self.blueprint)

    def test_step_is_applied_per_cover_so_offsets_survive(self) -> None:
        repeat = self.blueprint.index("for_each: \"{{ covers }}\"")
        current = self.blueprint.index("current_position", repeat)
        assignment = self.blueprint.index("entity_id: \"{{ repeat.item }}\"", current)
        self.assertLess(repeat, current)
        self.assertLess(current, assignment)

    def test_missing_position_skips_only_that_cover(self) -> None:
        """A cover reporting no position must not abort the whole repeat."""
        guard = self.blueprint.index("{{ current not in [none, 'None',")
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                self.assertIn(state, self.blueprint[guard : guard + 200])
        preceding = self.blueprint.rindex("- if:", 0, guard)
        self.assertLess(preceding, guard)

    def test_presses_are_queued_rather_than_dropped(self) -> None:
        self.assertIn("mode: queued", self.blueprint)
        self.assertNotIn("mode: single", self.blueprint)

    def test_favorite_button_supports_asymmetric_setups(self) -> None:
        self.assertIn("favorite_use_actions:", self.blueprint)
        self.assertIn("favorite_actions:", self.blueprint)
        self.assertIn("selector:\n        action: {}", self.blueprint)
        self.assertIn("then: !input favorite_actions", self.blueprint)

    def test_blueprint_is_reusable_and_carries_no_local_entities(self) -> None:
        self.assertIn("domain: automation", self.blueprint)
        self.assertIn("min_version: 2024.10.0", self.blueprint)
        self.assertIn("source_url: https://github.com/", self.blueprint)
        self.assertNotIn("entity_id: cover.", self.blueprint)
        self.assertIsNone(
            re.search(r"input_number\.[a-z0-9_]+", self.blueprint),
            "blueprint must not reference a specific helper entity",
        )


if __name__ == "__main__":
    unittest.main()
