"""Static contract checks for the reusable Home Assistant battery alert."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = (
    ROOT
    / "blueprints"
    / "automation"
    / "tilt_local_bridge"
    / "low_battery_alert.yaml"
)


class LowBatteryBlueprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blueprint = BLUEPRINT.read_text(encoding="utf-8")

    def test_blueprint_has_battery_thresholds_and_debounce(self) -> None:
        for phrase in (
            "device_class: battery",
            "low_threshold:",
            "recovery_threshold:",
            "debounce_minutes:",
            "below: !input low_threshold",
            "above: !input recovery_threshold",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.blueprint)

    def test_blueprint_recovers_after_home_assistant_restart(self) -> None:
        self.assertIn("trigger: homeassistant", self.blueprint)
        self.assertIn("event: start", self.blueprint)
        self.assertIn('hours: "/12"', self.blueprint)
        self.assertIn("persistent_notification.create", self.blueprint)
        self.assertIn("persistent_notification.dismiss", self.blueprint)

    def test_reminder_reuses_the_low_battery_debounce(self) -> None:
        reminder_check = self.blueprint.index("trigger_id == 'reminder'")
        reminder_delay = self.blueprint.index(
            "minutes: !input debounce_minutes", reminder_check
        )
        notification = self.blueprint.index(
            "persistent_notification.create", reminder_delay
        )
        self.assertLess(reminder_check, reminder_delay)
        self.assertLess(reminder_delay, notification)

    def test_selector_ranges_enforce_a_hysteresis_gap(self) -> None:
        low_input = self.blueprint.index("low_threshold:")
        recovery_input = self.blueprint.index("recovery_threshold:")
        self.assertIn("max: 49", self.blueprint[low_input:recovery_input])
        self.assertIn("min: 50", self.blueprint[recovery_input:])

    def test_blueprint_keeps_optional_notifications_user_configurable(self) -> None:
        self.assertIn("notification_actions:", self.blueprint)
        self.assertIn("selector:\n        action: {}", self.blueprint)
        self.assertNotIn("notify.mobile_app_", self.blueprint)


if __name__ == "__main__":
    unittest.main()
