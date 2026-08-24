"""Offline tests for Tilt bridge configuration and access gates."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tilt_local_bridge.tilt_bridge_config import (
    ShadeAccessDisabled,
    TiltBridgeConfigError,
    authorize_shade_access,
    load_config,
    load_pairing_key,
    load_secret,
)


def _config_payload(*, allow_reads: bool = False, allow_writes: bool = False) -> dict[str, object]:
    return {
        "version": 1,
        "access": {
            "allow_reads": allow_reads,
            "allow_position_writes": allow_writes,
        },
        "mqtt": {
            "host": "127.0.0.1",
            "username_file": "/etc/tilt-local-bridge/tilt-mqtt-username",
            "password_file": "/etc/tilt-local-bridge/tilt-mqtt-password",
        },
        "shades": [
            {
                "id": "office_shade",
                "name": "Office Shade",
                "mac": "02:00:00:00:00:01",
                "pairing_key_file": "/etc/tilt-local-bridge/office-shade.key",
            }
        ],
    }


class ConfigParsingTests(unittest.TestCase):
    def _load(self, payload: object):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def test_defaults_are_safe_and_values_are_normalized(self) -> None:
        config = self._load(_config_payload())
        self.assertFalse(config.access.allow_reads)
        self.assertFalse(config.access.allow_position_writes)
        self.assertEqual(config.poll_interval_seconds, 1800)
        self.assertIsNone(config.quiet_hours)
        self.assertEqual(config.shades[0].mac, "02:00:00:00:00:01")
        self.assertNotIn("pairing_key", repr(config.shades[0]))

    def test_overnight_quiet_hours_use_the_configured_timezone(self) -> None:
        payload = _config_payload()
        payload["poll_interval_seconds"] = 1800
        payload["quiet_hours"] = {
            "timezone": "America/Los_Angeles",
            "start": "23:00",
            "end": "06:00",
            "poll_interval_seconds": 7200,
        }
        config = self._load(payload)
        quiet_hours = config.quiet_hours
        assert quiet_hours is not None

        for moment, active in (
            (datetime(2026, 1, 16, 6, 59, tzinfo=timezone.utc), False),
            (datetime(2026, 1, 16, 7, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 1, 16, 13, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc), False),
            (datetime(2026, 7, 16, 5, 59, tzinfo=timezone.utc), False),
            (datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 7, 16, 12, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc), False),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(quiet_hours.is_active(moment), active)
                expected_interval = 7200 if active else 1800
                self.assertEqual(config.poll_interval_at(moment), expected_interval)

    def test_quiet_transition_duration_accounts_for_dst(self) -> None:
        payload = _config_payload()
        payload["quiet_hours"] = {
            "timezone": "America/Los_Angeles",
            "start": "23:00",
            "end": "06:00",
            "poll_interval_seconds": 7200,
        }
        quiet_hours = self._load(payload).quiet_hours
        assert quiet_hours is not None

        before_spring_forward = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        before_fall_back = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(
            quiet_hours.seconds_until_transition(before_spring_forward),
            5 * 3600,
        )
        self.assertEqual(
            quiet_hours.seconds_until_transition(before_fall_back),
            7 * 3600,
        )

    def test_invalid_quiet_hours_are_rejected(self) -> None:
        valid = {
            "timezone": "America/Los_Angeles",
            "start": "23:00",
            "end": "06:00",
            "poll_interval_seconds": 7200,
        }
        invalid_values = (
            {**valid, "timezone": "Mars/Olympus"},
            {**valid, "start": "7:00"},
            {**valid, "end": "23:00"},
            {**valid, "poll_interval_seconds": 1799},
            {**valid, "surprise": True},
        )
        for quiet_hours in invalid_values:
            payload = _config_payload()
            payload["poll_interval_seconds"] = 1800
            payload["quiet_hours"] = quiet_hours
            with self.subTest(quiet_hours=quiet_hours), self.assertRaises(
                TiltBridgeConfigError
            ):
                self._load(payload)

    def test_unknown_fields_and_embedded_keys_are_rejected(self) -> None:
        for field, value in (("surprise", True), ("pairing_key", "00" * 32)):
            payload = _config_payload()
            shade = payload["shades"][0]  # type: ignore[index]
            shade[field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(TiltBridgeConfigError):
                self._load(payload)

    def test_duplicate_ids_addresses_and_key_paths_are_rejected(self) -> None:
        for field in ("id", "mac", "pairing_key_file"):
            payload = _config_payload()
            first = dict(payload["shades"][0])  # type: ignore[index]
            second = dict(first)
            second["id"] = "second_shade"
            second["mac"] = "02:00:00:00:00:02"
            second["pairing_key_file"] = "/etc/tilt-local-bridge/tilt-second.key"
            second[field] = first[field]
            payload["shades"].append(second)  # type: ignore[union-attr]
            with self.subTest(field=field), self.assertRaises(TiltBridgeConfigError):
                self._load(payload)

    def test_writes_cannot_be_configured_without_reads(self) -> None:
        with self.assertRaises(TiltBridgeConfigError):
            self._load(_config_payload(allow_writes=True))


class AccessGateTests(unittest.TestCase):
    def _config(self, *, reads: bool, writes: bool):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            path.write_text(json.dumps(_config_payload(allow_reads=reads, allow_writes=writes)))
            return load_config(path)

    def test_reads_require_config_and_launch_time_approval(self) -> None:
        with self.assertRaises(ShadeAccessDisabled):
            authorize_shade_access(
                self._config(reads=False, writes=False),
                request_reads=True,
                request_position_writes=False,
            )
        with self.assertRaises(ShadeAccessDisabled):
            authorize_shade_access(
                self._config(reads=True, writes=False),
                request_reads=False,
                request_position_writes=False,
            )
        permit = authorize_shade_access(
            self._config(reads=True, writes=False),
            request_reads=True,
            request_position_writes=False,
        )
        permit.assert_valid()
        with self.assertRaises(ShadeAccessDisabled):
            permit.require_position_write()

    def test_write_approval_is_independent_and_requires_readback(self) -> None:
        config = self._config(reads=True, writes=True)
        with self.assertRaises(ShadeAccessDisabled):
            authorize_shade_access(
                config,
                request_reads=False,
                request_position_writes=True,
            )
        permit = authorize_shade_access(
            config,
            request_reads=True,
            request_position_writes=True,
        )
        permit.require_position_write()


class SecretFileTests(unittest.TestCase):
    def test_hex_pairing_key_loads_without_appearing_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "key"
            path.write_text("01" * 32 + "\n", encoding="ascii")
            path.chmod(0o600)
            self.assertEqual(load_pairing_key(path), b"\x01" * 32)

    def test_loose_or_multiline_secret_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "secret"
            path.write_text("secret\nsecond\n", encoding="ascii")
            path.chmod(0o600)
            with self.assertRaises(TiltBridgeConfigError):
                load_secret(path, label="test secret")
            path.write_text("secret\n", encoding="ascii")
            path.chmod(0o604)
            with self.assertRaises(TiltBridgeConfigError):
                load_secret(path, label="test secret")

    @unittest.skipIf(os.name != "posix", "POSIX ownership and mode test")
    def test_symlink_secret_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "target"
            target.write_text("secret\n", encoding="ascii")
            target.chmod(0o600)
            link = Path(tempdir) / "link"
            link.symlink_to(target)
            with self.assertRaises(TiltBridgeConfigError):
                load_secret(link, label="test secret")


if __name__ == "__main__":
    unittest.main()
