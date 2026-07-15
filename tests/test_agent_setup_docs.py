"""Regression coverage for the agent-first public setup path."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgentSetupDocsTests(unittest.TestCase):
    def test_readme_puts_agent_setup_before_manual_content(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLess(
            readme.index("## Fastest path: use a coding agent"),
            readme.index("## What works"),
        )

    def test_agent_prompt_keeps_pairing_and_movement_gated(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split())
        required = (
            "Never bypass SSH host key verification.",
            "Treat pairing and movement as separate approval gates.",
            "Do not ask me to paste them into chat.",
            "probe-status for the first live check",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)

    def test_pairing_example_prompts_for_account_identity(self) -> None:
        pairing = (ROOT / "docs" / "PAIRING.md").read_text(encoding="utf-8")
        self.assertNotIn("--username", pairing)
        self.assertIn("interactive prompts", pairing)


if __name__ == "__main__":
    unittest.main()
