"""Offline CLI boundary tests for the Tilt bridge."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tilt_local_bridge.tilt_bridge import _async_main, _run_runtime_check, build_parser, main
from tilt_local_bridge.tilt_bridge_config import ShadeAccessDisabled
from tilt_local_bridge.tilt_protocol import TiltProtocolError


class TiltBridgeParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_status_probe_requires_one_named_shade(self) -> None:
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["probe-status", "--allow-shade-reads"])
        args = self.parser.parse_args(
            ["probe-status", "--shade", "office_shade", "--allow-shade-reads"]
        )
        self.assertEqual(args.shade, "office_shade")
        self.assertTrue(args.allow_shade_reads)
        self.assertFalse(hasattr(args, "allow_position_writes"))

    def test_service_access_flags_default_to_disabled(self) -> None:
        args = self.parser.parse_args(["serve"])
        self.assertFalse(args.allow_shade_reads)
        self.assertFalse(args.allow_position_writes)

    def test_config_check_has_no_live_access_flags(self) -> None:
        args = self.parser.parse_args(["check-config"])
        self.assertIsInstance(args, argparse.Namespace)
        self.assertFalse(hasattr(args, "allow_shade_reads"))
        self.assertFalse(hasattr(args, "allow_position_writes"))

    def test_runtime_check_uses_expectation_flags_not_live_access_flags(self) -> None:
        args = self.parser.parse_args(
            [
                "check-runtime",
                "--expect-shade-reads",
                "--expect-position-writes",
            ]
        )
        self.assertTrue(args.expect_shade_reads)
        self.assertTrue(args.expect_position_writes)
        self.assertFalse(hasattr(args, "allow_shade_reads"))
        self.assertFalse(hasattr(args, "allow_position_writes"))

    def test_cloud_import_has_no_live_access_flags(self) -> None:
        args = self.parser.parse_args(
            ["import-cloud-store", "--input", "/private/tmp/tilt-store.json"]
        )
        self.assertFalse(args.replace_existing)
        self.assertFalse(hasattr(args, "allow_shade_reads"))
        self.assertFalse(hasattr(args, "allow_position_writes"))

    def test_protocol_failure_exits_cleanly(self) -> None:
        parser = Mock()
        parser.parse_args.return_value = argparse.Namespace(verbose=False)
        failure = AsyncMock(side_effect=TiltProtocolError("synthetic failure"))
        with (
            patch("tilt_local_bridge.tilt_bridge.build_parser", return_value=parser),
            patch("tilt_local_bridge.tilt_bridge._async_main", new=failure),
            patch("tilt_local_bridge.tilt_bridge._LOGGER.error") as error,
        ):
            self.assertEqual(main(), 2)
        error.assert_called_once()


class TiltBridgeRuntimeCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_check_validates_secrets_without_live_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            username = root / "mqtt.username"
            password = root / "mqtt.password"
            pairing_key = root / "shade.key"
            for path, value in (
                (username, "bridge-user\n"),
                (password, "bridge-password\n"),
                (pairing_key, "11" * 32 + "\n"),
            ):
                path.write_text(value, encoding="ascii")
                path.chmod(0o600)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "access": {
                            "allow_reads": True,
                            "allow_position_writes": True,
                        },
                        "mqtt": {
                            "host": "127.0.0.1",
                            "username_file": str(username),
                            "password_file": str(password),
                        },
                        "shades": [
                            {
                                "id": "door",
                                "name": "Door",
                                "mac": "02:00:00:00:00:01",
                                "pairing_key_file": str(pairing_key),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "--config",
                    str(config_path),
                    "check-runtime",
                    "--expect-shade-reads",
                    "--expect-position-writes",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = await _async_main(args)

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["pairing_key_count"], 1)
        self.assertNotIn("11" * 32, output.getvalue())

    async def test_runtime_check_rejects_write_expectation_without_reads(self) -> None:
        args = argparse.Namespace(
            expect_shade_reads=False,
            expect_position_writes=True,
        )
        with self.assertRaises(ShadeAccessDisabled):
            _run_runtime_check(Mock(mqtt=Mock()), args)


if __name__ == "__main__":
    unittest.main()
