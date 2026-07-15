"""Regression coverage for the repository privacy guard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_public_tree import scan


class PublicTreeGuardTests(unittest.TestCase):
    def _scan_text(self, value: str) -> list[str]:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=Path(__file__).resolve().parents[1], delete=False
        ) as handle:
            handle.write(value)
            path = Path(handle.name)
        try:
            return scan(path)
        finally:
            path.unlink()

    def test_examples_are_allowed(self) -> None:
        self.assertEqual(
            self._scan_text("owner@example.com 02:00:00:00:00:01"),
            [],
        )

    def test_private_home_details_are_rejected(self) -> None:
        value = "/" + "Users/person/private " + "192." + "168.4.20"
        failures = self._scan_text(value)
        self.assertEqual(len(failures), 2)

    def test_personal_email_and_real_mac_are_rejected(self) -> None:
        value = "person@" + "gmail.com D3:6E:" + "8B:50:DE:81"
        failures = self._scan_text(value)
        self.assertEqual(len(failures), 2)


if __name__ == "__main__":
    unittest.main()
