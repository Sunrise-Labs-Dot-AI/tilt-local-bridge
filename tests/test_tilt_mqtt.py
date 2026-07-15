"""Offline tests for Home Assistant MQTT discovery and bridge behavior."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tilt_local_bridge.tilt_ble import AmbiguousPositionWrite, PositionVerificationPending
from tilt_local_bridge.tilt_bridge_config import (
    BridgeAccessConfig,
    MqttConfig,
    ShadeConfig,
    TiltBridgeConfig,
)
from tilt_local_bridge.tilt_mqtt import (
    IncomingMqttMessage,
    TiltMqttBridge,
    bridge_availability_topic,
    discovery_payloads,
    parse_position_command,
    topics_for,
)
from tilt_local_bridge.tilt_protocol import ShadeStatus


def _config() -> TiltBridgeConfig:
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
    )


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool, int]] = []
        self.subscribed: list[tuple[str, int]] = []

    def publish(self, topic: str, payload: str, *, retain: bool, qos: int = 1) -> None:
        self.published.append((topic, payload, retain, qos))

    def subscribe(self, topic: str, *, qos: int = 1) -> None:
        self.subscribed.append((topic, qos))


class FakeShadeClient:
    def __init__(self, *, position: int = 35) -> None:
        self.status = ShadeStatus(position * 10, 88, 0, True)
        self.targets: list[int] = []
        self.reads = 0
        self.fail_reads = False
        self.position_error: Exception | None = None
        self.position_started: asyncio.Event | None = None
        self.position_release: asyncio.Event | None = None
        self.read_started: asyncio.Event | None = None
        self.read_release: asyncio.Event | None = None

    async def read_status(self) -> ShadeStatus:
        self.reads += 1
        if self.read_started is not None:
            self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        if self.fail_reads:
            raise RuntimeError("synthetic read failure")
        return self.status

    async def set_position_and_read_status(self, target: int):
        self.targets.append(target)
        if self.position_started is not None:
            self.position_started.set()
        if self.position_release is not None:
            await self.position_release.wait()
        if self.position_error is not None:
            raise self.position_error
        self.status = ShadeStatus(target * 10, 87, 0, True)
        return self.status, True


class DiscoveryTests(unittest.TestCase):
    def test_cover_exposes_no_stop_or_raw_command(self) -> None:
        config = _config()
        shade = config.shades[0]
        cover_raw, position_raw, battery_raw = discovery_payloads(config, shade)
        cover = json.loads(cover_raw)
        position = json.loads(position_raw)
        battery = json.loads(battery_raw)
        self.assertIsNone(cover["payload_stop"])
        self.assertEqual(cover["position_closed"], 0)
        self.assertEqual(cover["position_open"], 100)
        self.assertEqual(cover["position_topic"], topics_for(config, shade).position)
        self.assertNotIn("get_position_topic", cover)
        self.assertFalse(cover["retain"])
        self.assertEqual(cover["availability_mode"], "all")
        self.assertEqual(len(cover["availability"]), 2)
        self.assertEqual(position["name"], "Position")
        self.assertEqual(position["state_topic"], topics_for(config, shade).position)
        self.assertEqual(position["command_topic"], topics_for(config, shade).set_position)
        self.assertEqual(position["min"], 0)
        self.assertEqual(position["max"], 100)
        self.assertEqual(position["step"], 1)
        self.assertEqual(position["mode"], "slider")
        self.assertEqual(position["unit_of_measurement"], "%")
        self.assertFalse(position["optimistic"])
        self.assertFalse(position["retain"])
        self.assertTrue(position["enabled_by_default"])
        self.assertTrue(position["visible_by_default"])
        self.assertEqual(position["availability"], cover["availability"])
        self.assertEqual(position["device"]["identifiers"], cover["device"]["identifiers"])
        self.assertEqual(battery["device"]["identifiers"], cover["device"]["identifiers"])

    def test_only_exact_nonretained_commands_are_accepted(self) -> None:
        config = _config()
        topics = topics_for(config, config.shades[0])
        self.assertEqual(
            parse_position_command(IncomingMqttMessage(topics.command, b"OPEN"), topics),
            100,
        )
        self.assertEqual(
            parse_position_command(
                IncomingMqttMessage(topics.set_position, b"42"), topics
            ),
            42,
        )
        for message in (
            IncomingMqttMessage(topics.command, b"STOP"),
            IncomingMqttMessage(topics.set_position, b"42 "),
            IncomingMqttMessage(topics.set_position, b"101"),
            IncomingMqttMessage(topics.command, b"OPEN", retain=True),
        ):
            with self.subTest(message=message):
                self.assertIsNone(parse_position_command(message, topics))


class BridgeBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.config = _config()
        self.publisher = FakePublisher()
        self.client = FakeShadeClient()
        self.bridge = TiltMqttBridge(
            self.config,
            self.publisher,
            {self.config.shades[0].id: self.client},  # type: ignore[dict-item]
        )
        await self.bridge.start()

    async def asyncTearDown(self) -> None:
        await self.bridge.stop()

    async def test_startup_marks_offline_before_fresh_state_then_online(self) -> None:
        shade = self.config.shades[0]
        topics = topics_for(self.config, shade)
        bridge_topic = bridge_availability_topic(self.config)
        bridge_values = [
            payload
            for topic, payload, _retain, _qos in self.publisher.published
            if topic == bridge_topic
        ]
        shade_values = [
            payload
            for topic, payload, _retain, _qos in self.publisher.published
            if topic == topics.availability
        ]
        self.assertEqual(bridge_values[:2], ["offline", "online"])
        self.assertEqual(shade_values[:2], ["offline", "online"])
        self.assertEqual(self.client.reads, 1)
        self.assertIn((topics.command, 1), self.publisher.subscribed)
        self.assertIn((topics.set_position, 1), self.publisher.subscribed)

    async def test_command_updates_state_and_retained_command_is_ignored(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"61", retain=True)
        )
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"62")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(self.client.targets, [62])
        self.assertIn((topics.position, "62", True, 1), self.publisher.published)

    async def test_home_assistant_online_republishes_without_new_ble_read(self) -> None:
        before_reads = self.client.reads
        before_publish = len(self.publisher.published)
        await self.bridge.handle_message(
            IncomingMqttMessage("homeassistant/status", b"online")
        )
        self.assertEqual(self.client.reads, before_reads)
        self.assertGreater(len(self.publisher.published), before_publish)

    async def test_home_assistant_online_does_not_restore_stale_availability(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.fail_reads = True
        await self.bridge.refresh_all()
        self.publisher.published.clear()

        await self.bridge.handle_message(
            IncomingMqttMessage("homeassistant/status", b"online")
        )

        shade_values = [
            payload
            for topic, payload, _retain, _qos in self.publisher.published
            if topic == topics.availability
        ]
        self.assertEqual(shade_values, [])
        self.assertIn(
            (topics.position, "35", True, 1),
            self.publisher.published,
        )

    async def test_reconnect_restores_clean_session_before_fresh_read(self) -> None:
        shade = self.config.shades[0]
        topics = topics_for(self.config, shade)
        bridge_topic = bridge_availability_topic(self.config)
        before_reads = self.client.reads
        self.publisher.published.clear()
        self.publisher.subscribed.clear()

        await self.bridge.handle_reconnect()

        self.assertEqual(self.client.reads, before_reads + 1)
        self.assertEqual(
            self.publisher.subscribed,
            [
                (topics.command, 1),
                (topics.set_position, 1),
                ("homeassistant/status", 1),
            ],
        )
        bridge_values = [
            payload
            for topic, payload, _retain, _qos in self.publisher.published
            if topic == bridge_topic
        ]
        shade_values = [
            payload
            for topic, payload, _retain, _qos in self.publisher.published
            if topic == topics.availability
        ]
        self.assertEqual(bridge_values, ["offline", "online"])
        self.assertEqual(shade_values, ["offline", "online"])
        self.assertIn(
            topics.cover_discovery,
            [topic for topic, _payload, _retain, _qos in self.publisher.published],
        )
        self.assertIn(
            topics.position_discovery,
            [topic for topic, _payload, _retain, _qos in self.publisher.published],
        )

    async def test_latest_target_wins_while_worker_is_waiting(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        await self.bridge.handle_message(IncomingMqttMessage(topics.set_position, b"20"))
        await self.bridge.handle_message(IncomingMqttMessage(topics.set_position, b"40"))
        await self.bridge.handle_message(IncomingMqttMessage(topics.set_position, b"60"))
        await asyncio.sleep(0.01)
        self.assertEqual(self.client.targets, [60])

    async def test_new_target_is_rejected_while_command_is_in_flight(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.position_started = asyncio.Event()
        self.client.position_release = asyncio.Event()

        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"70")
        )
        await self.client.position_started.wait()
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"80")
        )
        self.client.position_release.set()
        await asyncio.sleep(0.01)

        self.assertEqual(self.client.targets, [70])
        self.assertEqual(self.bridge._pending_targets, {})

    async def test_moving_shade_stays_online_and_rejects_another_command(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        moving_status = ShadeStatus(420, 87, 0, True)
        self.client.position_error = PositionVerificationPending(
            "still moving", moving_status
        )
        self.publisher.published.clear()

        with patch("tilt_local_bridge.tilt_mqtt._POSITION_RECONCILE_DELAYS", (3600,)):
            await self.bridge.handle_message(
                IncomingMqttMessage(topics.set_position, b"70")
            )
            await asyncio.sleep(0.01)
            await self.bridge.handle_message(
                IncomingMqttMessage(topics.set_position, b"80")
            )
            await asyncio.sleep(0.01)

        self.assertEqual(self.client.targets, [70])
        self.assertIn((topics.position, "42", True, 1), self.publisher.published)
        self.assertNotIn(
            (topics.availability, "offline", True, 1), self.publisher.published
        )

    async def test_moving_shade_reconciles_to_verified_position(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.position_error = PositionVerificationPending(
            "still moving", ShadeStatus(420, 87, 0, True)
        )
        self.client.status = ShadeStatus(700, 86, 0, True)
        self.publisher.published.clear()

        with patch("tilt_local_bridge.tilt_mqtt._POSITION_RECONCILE_DELAYS", (0,)):
            await self.bridge.handle_message(
                IncomingMqttMessage(topics.set_position, b"70")
            )
            await asyncio.sleep(0.01)

        self.assertIn((topics.position, "70", True, 1), self.publisher.published)
        self.assertIn((topics.availability, "online", True, 1), self.publisher.published)
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"80")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(self.client.targets, [70])
        self.assertEqual(
            self.bridge._pending_targets,
            {self.config.shades[0].id: 80},
        )

    async def test_ambiguous_write_without_status_marks_shade_offline(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.position_error = AmbiguousPositionWrite("no readback")
        self.publisher.published.clear()

        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"70")
        )
        await asyncio.sleep(0.01)

        self.assertIn(
            (topics.availability, "offline", True, 1), self.publisher.published
        )

    async def test_pending_verification_without_any_status_marks_shade_offline(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.position_error = PositionVerificationPending(
            "still moving", ShadeStatus(420, 87, 0, True)
        )
        self.client.fail_reads = True
        self.publisher.published.clear()

        with patch("tilt_local_bridge.tilt_mqtt._POSITION_RECONCILE_DELAYS", (0,)):
            await self.bridge.handle_message(
                IncomingMqttMessage(topics.set_position, b"70")
            )
            await asyncio.sleep(0.01)

        self.assertIn(
            (topics.availability, "offline", True, 1), self.publisher.published
        )

    async def test_command_is_ignored_after_status_contact_fails(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.fail_reads = True
        await self.bridge.refresh_all()

        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"70")
        )
        await asyncio.sleep(0.01)

        self.assertEqual(self.client.targets, [])

    async def test_queued_command_is_dropped_if_shade_goes_offline(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"60")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(self.client.targets, [60])

        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"70")
        )
        await asyncio.sleep(0.01)
        self.client.fail_reads = True
        await self.bridge.refresh_all()
        await asyncio.sleep(self.config.command_cooldown_seconds + 0.05)

        self.assertEqual(self.client.targets, [60])
        self.assertEqual(self.bridge._pending_targets, {})

    async def test_command_waits_for_in_progress_refresh_before_final_check(self) -> None:
        topics = topics_for(self.config, self.config.shades[0])
        self.client.fail_reads = True
        self.client.read_started = asyncio.Event()
        self.client.read_release = asyncio.Event()
        refresh = asyncio.create_task(self.bridge.refresh_all())
        await self.client.read_started.wait()

        await self.bridge.handle_message(
            IncomingMqttMessage(topics.set_position, b"70")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(self.client.targets, [])

        self.client.read_release.set()
        await refresh
        await asyncio.sleep(0.01)

        self.assertEqual(self.client.targets, [])
        self.assertEqual(self.bridge._pending_targets, {})


if __name__ == "__main__":
    unittest.main()
