"""Static safety checks for the on-Pi installer."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def test_help_is_available_without_mutation(self) -> None:
        result = subprocess.run(
            ["bash", str(INSTALLER), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--allow-position-writes", result.stdout)

    def test_default_is_a_dry_run(self) -> None:
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Dry run only", result.stdout)

    def test_service_is_disabled_unless_explicitly_enabled(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('enable_service=0', source)
        self.assertIn('if [[ "$enable_service" == "1" ]]', source)
        self.assertIn("systemctl disable tilt-local-bridge.service", source)

    def test_position_writes_have_a_separate_launch_gate(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('allow_writes=0', source)
        self.assertIn('--expect-position-writes', source)
        self.assertIn('--allow-position-writes', source)


if __name__ == "__main__":
    unittest.main()
