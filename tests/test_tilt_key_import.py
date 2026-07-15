"""Offline tests for protected Tilt cloud-store key import."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from tilt_local_bridge.tilt_bridge_config import TiltBridgeConfigError, load_config
from tilt_local_bridge.tilt_key_import import _write_all, import_pairing_keys


class TiltKeyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = self._load_config()

    def _load_config(self):
        payload = {
            "version": 1,
            "mqtt": {
                "host": "127.0.0.1",
                "username_file": str(self.root / "mqtt-user"),
                "password_file": str(self.root / "mqtt-password"),
            },
            "shades": [
                {
                    "id": "door",
                    "name": "Door",
                    "mac": "02:00:00:00:00:01",
                    "pairing_key_file": str(self.root / "door.key"),
                },
                {
                    "id": "window",
                    "name": "Window",
                    "mac": "02:00:00:00:00:02",
                    "pairing_key_file": str(self.root / "window.key"),
                },
            ],
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_config(path)

    def _write_store(self, payload: object, *, mode: int = 0o600) -> Path:
        path = self.root / "store.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(mode)
        return path

    def _store(self) -> dict[str, object]:
        return {
            "rooms": [
                {
                    "rollerShades": [
                        {
                            "id": "020000000001",
                            "pairingKey": "11" * 32,
                        },
                        {
                            "id": "02:00:00:00:00:02",
                            "pairingKey": "22" * 32,
                        },
                    ]
                }
            ]
        }

    def test_import_matches_only_configured_macs_and_writes_mode_600(self) -> None:
        payload = self._store()
        payload["unrelated"] = {"id": "001122334455", "pairingKey": "33" * 32}
        result = import_pairing_keys(self._write_store(payload), self.config)

        self.assertEqual(result.imported_shade_ids, ("door", "window"))
        self.assertEqual((self.root / "door.key").read_text(), "11" * 32 + "\n")
        self.assertEqual((self.root / "window.key").read_text(), "22" * 32 + "\n")
        self.assertEqual(stat.S_IMODE((self.root / "door.key").stat().st_mode), 0o600)

    def test_identical_existing_keys_are_unchanged(self) -> None:
        store = self._write_store(self._store())
        import_pairing_keys(store, self.config)
        result = import_pairing_keys(store, self.config)
        self.assertEqual(result.imported_shade_ids, ())
        self.assertEqual(result.unchanged_shade_ids, ("door", "window"))

    def test_different_existing_key_requires_explicit_replacement(self) -> None:
        store = self._write_store(self._store())
        existing = self.root / "door.key"
        existing.write_text("44" * 32 + "\n", encoding="ascii")
        existing.chmod(0o600)
        with self.assertRaisesRegex(TiltBridgeConfigError, "replacement was not approved"):
            import_pairing_keys(store, self.config)

        result = import_pairing_keys(store, self.config, replace_existing=True)
        self.assertIn("door", result.imported_shade_ids)
        self.assertEqual(existing.read_text(), "11" * 32 + "\n")

    def test_missing_or_conflicting_configured_key_is_rejected(self) -> None:
        payload = self._store()
        payload["rooms"][0]["rollerShades"].pop()  # type: ignore[index]
        with self.assertRaisesRegex(TiltBridgeConfigError, "missing configured shades"):
            import_pairing_keys(self._write_store(payload), self.config)

        payload = self._store()
        payload["duplicate"] = {"id": "02:00:00:00:00:01", "pairingKey": "55" * 32}
        with self.assertRaisesRegex(TiltBridgeConfigError, "conflicting pairing keys"):
            import_pairing_keys(self._write_store(payload), self.config)

    def test_store_file_must_be_private(self) -> None:
        with self.assertRaisesRegex(TiltBridgeConfigError, "group or other"):
            import_pairing_keys(self._write_store(self._store(), mode=0o640), self.config)

    def test_protected_key_writer_handles_partial_os_writes(self) -> None:
        with patch("tilt_local_bridge.tilt_key_import.os.write", side_effect=[1, 2]) as write:
            _write_all(17, b"abc")
        self.assertEqual(write.call_args_list, [call(17, b"abc"), call(17, b"bc")])


if __name__ == "__main__":
    unittest.main()
