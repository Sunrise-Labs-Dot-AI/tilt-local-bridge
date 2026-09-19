"""Offline tests for the bridge surfaces the Bluetooth remote depends on."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tilt_local_bridge.tilt_bridge import (
    _async_main,
    _run_runtime_check,
    _run_service,
    build_parser,
)
from tilt_local_bridge.tilt_bridge_config import (
    BluetoothRemoteConfig,
    BridgeAccessConfig,
    MqttConfig,
    ShadeAccessDisabled,
    ShadeConfig,
    TiltBridgeConfig,
    TiltBridgeConfigError,
    authorize_bluetooth_remote,
    load_config,
)
from tilt_local_bridge.tilt_mqtt import (
    IncomingMqttMessage,
    TiltMqttBridge,
    bridge_topics_for,
    pairing_discovery_payloads,
    topics_for,
)
from tilt_local_bridge.tilt_protocol import ShadeStatus
from tilt_local_bridge.tilt_remote import RemotePhoneStore


ROOT = Path(__file__).resolve().parents[1]


def _config(*, remote: BluetoothRemoteConfig | None = None) -> TiltBridgeConfig:
    return TiltBridgeConfig(
        version=1,
        access=BridgeAccessConfig(allow_reads=True, allow_position_writes=True),
        mqtt=MqttConfig(
            host="127.0.0.1",
            port=1883,
            username_file=Path("/mqtt-user"),
            password_file=Path("/mqtt-password"),
        ),
        shades=(
            ShadeConfig(
                id="office_shade",
                name="Office Shade",
                mac="02:00:00:00:00:01",
                pairing_key_file=Path("/shade-key"),
            ),
        ),
        poll_interval_seconds=3600,
        command_cooldown_seconds=2,
        bluetooth_remote=remote,
    )


def _config_payload() -> dict[str, object]:
    return {
        "version": 1,
        "access": {"allow_reads": True, "allow_position_writes": False},
        "mqtt": {
            "host": "127.0.0.1",
            "username_file": "/etc/tilt-local-bridge/mqtt.username",
            "password_file": "/etc/tilt-local-bridge/mqtt.password",
        },
        "shades": [
            {
                "id": "office_shade",
                "name": "Office Shade",
                "mac": "02:00:00:00:00:01",
                "pairing_key_file": "/etc/tilt-local-bridge/keys/office_shade.key",
            }
        ],
    }


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool, int]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.fail = False

    def publish(self, topic: str, payload: str, *, retain: bool, qos: int = 1) -> None:
        if self.fail:
            raise RuntimeError("broker unreachable")
        self.published.append((topic, payload, retain, qos))

    def subscribe(self, topic: str, *, qos: int = 1) -> None:
        self.subscribed.append((topic, qos))


class FakeShadeClient:
    def __init__(self) -> None:
        self.status = ShadeStatus(400, 88, 0, True)
        self.targets: list[int] = []

    async def read_status(self) -> ShadeStatus:
        return self.status

    async def set_position_and_read_status(self, target: int):
        self.targets.append(target)
        self.status = ShadeStatus(target * 10, 87, 0, True)
        return self.status, True


class ConfigTests(unittest.TestCase):
    def _load(self, payload: object) -> TiltBridgeConfig:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def test_remote_section_is_optional_and_defaults_are_safe(self) -> None:
        self.assertIsNone(self._load(_config_payload()).bluetooth_remote)
        payload = _config_payload()
        payload["bluetooth_remote"] = {"enabled": True}
        remote = self._load(payload).bluetooth_remote
        assert remote is not None
        self.assertTrue(remote.enabled)
        self.assertEqual(remote.name, "Tilt Local Bridge")
        self.assertEqual(remote.state_file, Path("/var/lib/tilt-local-bridge/remote.json"))
        self.assertEqual(remote.adapter, "hci0")

    def test_example_config_ships_with_the_remote_disabled(self) -> None:
        config = load_config(ROOT / "examples" / "bridge.example.json")
        assert config.bluetooth_remote is not None
        self.assertFalse(config.bluetooth_remote.enabled)

    def test_remote_section_rejects_bad_values(self) -> None:
        for bad in (
            {"enabled": "yes"},
            {"enabled": True, "adapter": "wlan0"},
            {"enabled": True, "name": "x" * 25},
            {"enabled": True, "state_file": "relative.json"},
            {"enabled": True, "extra": 1},
            {},
        ):
            payload = _config_payload()
            payload["bluetooth_remote"] = bad
            with self.subTest(bad=bad), self.assertRaises(TiltBridgeConfigError):
                self._load(payload)

    def test_bridge_is_a_reserved_shade_id(self) -> None:
        payload = _config_payload()
        payload["shades"][0]["id"] = "bridge"  # type: ignore[index]
        with self.assertRaises(TiltBridgeConfigError):
            self._load(payload)

    def test_remote_authorization_needs_both_gates_and_reads(self) -> None:
        enabled = BluetoothRemoteConfig(enabled=True)
        with self.assertRaises(ShadeAccessDisabled):
            authorize_bluetooth_remote(_config(remote=enabled), request_remote=False)
        with self.assertRaises(ShadeAccessDisabled):
            authorize_bluetooth_remote(_config(), request_remote=True)
        with self.assertRaises(ShadeAccessDisabled):
            authorize_bluetooth_remote(
                _config(remote=BluetoothRemoteConfig(enabled=False)), request_remote=True
            )
        no_reads = TiltBridgeConfig(
            version=1,
            access=BridgeAccessConfig(),
            mqtt=_config().mqtt,
            shades=_config().shades,
            bluetooth_remote=enabled,
        )
        with self.assertRaises(ShadeAccessDisabled):
            authorize_bluetooth_remote(no_reads, request_remote=True)
        self.assertIs(
            authorize_bluetooth_remote(_config(remote=enabled), request_remote=True), enabled
        )


class PairingSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.config = _config()
        self.publisher = FakePublisher()
        self.client = FakeShadeClient()
        self.bridge = TiltMqttBridge(
            self.config,
            self.publisher,
            {"office_shade": self.client},
            pairing_surface=True,
        )
        await self.bridge.start()

    async def asyncTearDown(self) -> None:
        await self.bridge.stop()

    def test_discovery_payloads_describe_the_approval_entities(self) -> None:
        request, approve, phones = (json.loads(p) for p in pairing_discovery_payloads(self.config))
        topics = bridge_topics_for(self.config)
        self.assertEqual(request["state_topic"], topics.pairing_request)
        self.assertEqual(approve["command_topic"], topics.command)
        self.assertEqual(approve["payload_press"], "APPROVE_PAIRING")
        self.assertEqual(phones["state_topic"], topics.paired_phones)
        self.assertEqual(request["device"]["identifiers"], ["tilt_local_bridge"])
        self.assertEqual(topics.command, "tilt/local/bridge/command")
        self.assertEqual(
            topics.approve_pairing_discovery,
            "homeassistant/button/tilt_bridge/approve_pairing/config",
        )

    async def test_start_announces_entities_and_subscribes_to_the_command_topic(self) -> None:
        topics = bridge_topics_for(self.config)
        published = {topic for topic, _p, _r, _q in self.publisher.published}
        self.assertIn(topics.pairing_request_discovery, published)
        self.assertIn(topics.approve_pairing_discovery, published)
        self.assertIn(topics.paired_phones_discovery, published)
        self.assertIn((topics.pairing_request, "none", True, 1), self.publisher.published)
        self.assertIn((topics.command, 1), self.publisher.subscribed)

    async def test_bridge_command_reaches_the_handler_but_never_a_shade(self) -> None:
        received: list[bytes] = []

        async def handler(payload: bytes) -> None:
            received.append(payload)

        self.bridge.set_bridge_command_handler(handler)
        topics = bridge_topics_for(self.config)
        await self.bridge.handle_message(IncomingMqttMessage(topics.command, b"APPROVE_PAIRING"))
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.command, b"APPROVE_PAIRING", retain=True)
        )
        await asyncio.sleep(0.01)
        self.assertEqual(received, [b"APPROVE_PAIRING"])
        self.assertEqual(self.client.targets, [])

    async def test_pairing_state_is_retained_and_republished(self) -> None:
        topics = bridge_topics_for(self.config)
        self.bridge.publish_pairing_request("Phone (code 123456)")
        self.bridge.publish_paired_phone_count(2)
        self.publisher.published.clear()
        await self.bridge.handle_message(IncomingMqttMessage("homeassistant/status", b"online"))
        self.assertIn(
            (topics.pairing_request, "Phone (code 123456)", True, 1), self.publisher.published
        )
        self.assertIn((topics.paired_phones, "2", True, 1), self.publisher.published)
        self.assertIn(
            topics.approve_pairing_discovery,
            {topic for topic, _p, _r, _q in self.publisher.published},
        )

    async def test_snapshots_and_position_requests(self) -> None:
        snapshot = self.bridge.shade_snapshots()[0]
        self.assertEqual(snapshot.id, "office_shade")
        self.assertEqual(snapshot.position_percent, 40)
        self.assertEqual(snapshot.battery_percent, 88)
        self.assertTrue(snapshot.available)
        self.assertIsNone(snapshot.target_percent)
        self.assertIsNotNone(snapshot.age_seconds)
        self.assertEqual(self.bridge.request_position("nope", 10), "unknown_shade")
        self.assertEqual(self.bridge.request_position("office_shade", 60), "accepted")
        self.assertEqual(self.bridge.shade_snapshots()[0].target_percent, 60)
        await asyncio.sleep(0.05)
        self.assertEqual(self.client.targets, [60])
        self.assertEqual(self.bridge.shade_snapshots()[0].position_percent, 60)

    async def test_status_listeners_hear_updates_and_outages(self) -> None:
        heard: list[str] = []
        self.bridge.add_status_listener(heard.append)
        await self.bridge.refresh_all()
        self.assertEqual(heard, ["office_shade"])

        async def failing_read() -> ShadeStatus:
            raise RuntimeError("radio")

        self.client.read_status = failing_read  # type: ignore[method-assign]
        await self.bridge.refresh_all()
        self.assertEqual(heard, ["office_shade", "office_shade"])
        self.assertFalse(self.bridge.shade_snapshots()[0].available)
        self.assertEqual(self.bridge.request_position("office_shade", 10), "unavailable")

    async def test_broker_outage_does_not_stop_status_reads(self) -> None:
        self.publisher.fail = True
        await self.bridge.refresh_all()
        self.assertTrue(self.bridge.shade_snapshots()[0].available)
        self.publisher.fail = False
        self.publisher.published.clear()
        await self.bridge.refresh_all()
        topics = topics_for(self.config, self.config.shades[0])
        self.assertIn((topics.position, "40", True, 1), self.publisher.published)


class NoSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_surface_is_silent_when_the_remote_is_off(self) -> None:
        config = _config()
        publisher = FakePublisher()
        bridge = TiltMqttBridge(config, publisher, {"office_shade": FakeShadeClient()})
        await bridge.start()
        try:
            topics = bridge_topics_for(config)
            bridge.publish_pairing_request("x")
            bridge.publish_paired_phone_count(1)
            published = {topic for topic, _p, _r, _q in publisher.published}
            self.assertNotIn(topics.pairing_request, published)
            self.assertNotIn(topics.approve_pairing_discovery, published)
            self.assertNotIn((topics.command, 1), publisher.subscribed)
        finally:
            await bridge.stop()


class CliTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_exposes_the_remote_gates_and_phone_management(self) -> None:
        parser = build_parser()
        serve = parser.parse_args(["serve"])
        self.assertFalse(serve.allow_bluetooth_remote)
        serve = parser.parse_args(["serve", "--allow-shade-reads", "--allow-bluetooth-remote"])
        self.assertTrue(serve.allow_bluetooth_remote)
        check = parser.parse_args(["check-runtime", "--expect-shade-reads", "--expect-bluetooth-remote"])
        self.assertTrue(check.expect_bluetooth_remote)
        self.assertFalse(hasattr(check, "allow_bluetooth_remote"))
        phones = parser.parse_args(["remote-phones", "list"])
        self.assertEqual(phones.phone_action, "list")
        remove = parser.parse_args(["remote-phones", "remove", "--key-prefix", "0123abcd"])
        self.assertEqual(remove.key_prefix, "0123abcd")
        with self.assertRaises(SystemExit):
            parser.parse_args(["remote-phones"])

    async def test_service_refuses_the_remote_without_reads_or_config(self) -> None:
        args = argparse.Namespace(
            allow_shade_reads=False, allow_position_writes=False, allow_bluetooth_remote=True
        )
        with self.assertRaises(ShadeAccessDisabled):
            await _run_service(Mock(), args)
        args = argparse.Namespace(
            allow_shade_reads=True, allow_position_writes=False, allow_bluetooth_remote=True
        )
        with self.assertRaises(ShadeAccessDisabled):
            await _run_service(_config(), args)

    def test_runtime_check_refuses_remote_expectation_without_reads(self) -> None:
        args = argparse.Namespace(
            expect_shade_reads=False, expect_position_writes=False, expect_bluetooth_remote=True
        )
        with self.assertRaises(ShadeAccessDisabled):
            _run_runtime_check(Mock(mqtt=Mock()), args)

    async def test_runtime_check_and_phone_management_use_the_state_file(self) -> None:
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
            state_dir = root / "state"
            state_dir.mkdir()
            config_path = root / "config.json"
            payload = _config_payload()
            payload["mqtt"] = {  # type: ignore[assignment]
                "host": "127.0.0.1",
                "username_file": str(username),
                "password_file": str(password),
            }
            payload["shades"][0]["pairing_key_file"] = str(pairing_key)  # type: ignore[index]
            payload["bluetooth_remote"] = {
                "enabled": True,
                "state_file": str(state_dir / "remote.json"),
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = await _async_main(
                    build_parser().parse_args(
                        [
                            "--config",
                            str(config_path),
                            "check-runtime",
                            "--expect-shade-reads",
                            "--expect-bluetooth-remote",
                        ]
                    )
                )
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report["expected_bluetooth_remote"])
            self.assertFalse(report["state_file_present"])
            self.assertFalse((state_dir / "remote.json").exists())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                await _async_main(
                    build_parser().parse_args(
                        ["--config", str(config_path), "remote-phones", "list"]
                    )
                )
            self.assertEqual(json.loads(output.getvalue()), {"bridge_id": None, "phones": []})
            self.assertFalse((state_dir / "remote.json").exists())

            store = RemotePhoneStore(state_dir / "remote.json")
            store.add("ab" * 32, "Phone A")
            store.add("cd" * 32, "Phone B")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                await _async_main(
                    build_parser().parse_args(
                        ["--config", str(config_path), "remote-phones", "list"]
                    )
                )
            listed = json.loads(output.getvalue())
            self.assertEqual([p["key_prefix"] for p in listed["phones"]], ["abababab", "cdcdcdcd"])
            self.assertNotIn("ab" * 32, output.getvalue())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                await _async_main(
                    build_parser().parse_args(
                        [
                            "--config",
                            str(config_path),
                            "remote-phones",
                            "remove",
                            "--key-prefix",
                            "abababab",
                        ]
                    )
                )
            self.assertEqual(
                json.loads(output.getvalue()),
                {"removed": [{"name": "Phone A", "key_prefix": "abababab"}]},
            )
            self.assertEqual([p.name for p in RemotePhoneStore(state_dir / "remote.json").phones()], ["Phone B"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = await _async_main(
                    build_parser().parse_args(
                        [
                            "--config",
                            str(config_path),
                            "check-runtime",
                            "--expect-shade-reads",
                            "--expect-bluetooth-remote",
                        ]
                    )
                )
            report = json.loads(output.getvalue())
            self.assertEqual(report["paired_phone_count"], 1)
            self.assertTrue(report["state_file_present"])


class InstallerTests(unittest.TestCase):
    def test_installer_has_a_separate_remote_gate_and_state_directory(self) -> None:
        installer = ROOT / "scripts" / "install.sh"
        source = installer.read_text(encoding="utf-8")
        self.assertIn("allow_remote=0", source)
        self.assertIn("--expect-bluetooth-remote", source)
        self.assertIn("--allow-bluetooth-remote", source)
        self.assertIn("StateDirectory=tilt-local-bridge", source)
        result = subprocess.run(
            ["bash", str(installer), "--help"], check=False, capture_output=True, text=True
        )
        self.assertIn("--allow-bluetooth-remote", result.stdout)

    def test_ci_runs_the_app_checks(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("apps/shade-remote", workflow)
        self.assertIn("npm run typecheck", workflow)


if __name__ == "__main__":
    unittest.main()
