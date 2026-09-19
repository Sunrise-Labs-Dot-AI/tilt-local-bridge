"""Home Assistant MQTT discovery and orchestration for Tilt shades."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from .tilt_ble import PositionVerificationPending, TiltShadeClient
from .tilt_bridge_config import ShadeConfig, TiltBridgeConfig
from .tilt_protocol import ShadeStatus
from .tilt_remote import (
    APPROVE_PAIRING_PAYLOAD,
    ShadeSnapshot,
)


_LOGGER = logging.getLogger(__name__)
_POSITION_PAYLOAD = re.compile(r"(?:0|[1-9][0-9]?|100)")
_POSITION_RECONCILE_DELAYS = (5.0, 10.0, 20.0, 40.0)


class MqttPublisher(Protocol):
    def publish(self, topic: str, payload: str, *, retain: bool, qos: int = 1) -> None: ...

    def subscribe(self, topic: str, *, qos: int = 1) -> None: ...


@dataclass(frozen=True)
class IncomingMqttMessage:
    topic: str
    payload: bytes
    retain: bool = False


@dataclass(frozen=True)
class ShadeTopics:
    command: str
    set_position: str
    position: str
    battery: str
    availability: str
    cover_discovery: str
    position_discovery: str
    battery_discovery: str


def topics_for(config: TiltBridgeConfig, shade: ShadeConfig) -> ShadeTopics:
    base = f"{config.mqtt.topic_prefix}/{shade.id}"
    discovery = config.mqtt.discovery_prefix
    return ShadeTopics(
        command=f"{base}/command",
        set_position=f"{base}/set_position",
        position=f"{base}/position",
        battery=f"{base}/battery",
        availability=f"{base}/availability",
        cover_discovery=f"{discovery}/cover/tilt_bridge/{shade.id}/config",
        position_discovery=f"{discovery}/number/tilt_bridge/{shade.id}_position/config",
        battery_discovery=f"{discovery}/sensor/tilt_bridge/{shade.id}_battery/config",
    )


@dataclass(frozen=True)
class BridgeTopics:
    availability: str
    command: str
    pairing_request: str
    paired_phones: str
    pairing_request_discovery: str
    approve_pairing_discovery: str
    paired_phones_discovery: str


def bridge_topics_for(config: TiltBridgeConfig) -> BridgeTopics:
    base = f"{config.mqtt.topic_prefix}/bridge"
    discovery = config.mqtt.discovery_prefix
    return BridgeTopics(
        availability=f"{base}/availability",
        command=f"{base}/command",
        pairing_request=f"{base}/pairing_request",
        paired_phones=f"{base}/paired_phones",
        pairing_request_discovery=f"{discovery}/sensor/tilt_bridge/pairing_request/config",
        approve_pairing_discovery=f"{discovery}/button/tilt_bridge/approve_pairing/config",
        paired_phones_discovery=f"{discovery}/sensor/tilt_bridge/paired_phones/config",
    )


def bridge_availability_topic(config: TiltBridgeConfig) -> str:
    return bridge_topics_for(config).availability


def pairing_discovery_payloads(config: TiltBridgeConfig) -> tuple[str, str, str]:
    """Home Assistant entities for approving a phone over the Bluetooth remote."""

    topics = bridge_topics_for(config)
    availability = [
        {
            "topic": topics.availability,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
    ]
    remote = config.bluetooth_remote
    device = {
        "identifiers": ["tilt_local_bridge"],
        # The same name the phone sees while scanning, so the device in Home
        # Assistant and the bridge in the app are recognisably one thing.
        "name": remote.name if remote is not None else "Tilt Local Bridge",
        "manufacturer": "Sunrise Labs",
        "model": "Bluetooth phone remote",
    }
    origin = {
        "name": "Tilt Local Bridge",
        "support_url": "https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge",
    }
    pairing_request = {
        "name": "Phone pairing request",
        "unique_id": "tilt_bridge_pairing_request",
        "state_topic": topics.pairing_request,
        "icon": "mdi:cellphone-link",
        "entity_category": "diagnostic",
        "availability": availability,
        "device": device,
        "origin": origin,
    }
    approve = {
        "name": "Approve phone pairing",
        "unique_id": "tilt_bridge_approve_pairing",
        "command_topic": topics.command,
        "payload_press": APPROVE_PAIRING_PAYLOAD,
        "icon": "mdi:cellphone-check",
        "entity_category": "config",
        "retain": False,
        "availability": availability,
        "device": device,
        "origin": origin,
    }
    paired_phones = {
        "name": "Paired phones",
        "unique_id": "tilt_bridge_paired_phones",
        "state_topic": topics.paired_phones,
        "icon": "mdi:cellphone",
        "entity_category": "diagnostic",
        "availability": availability,
        "device": device,
        "origin": origin,
    }
    return _json(pairing_request), _json(approve), _json(paired_phones)


def discovery_payloads(
    config: TiltBridgeConfig, shade: ShadeConfig
) -> tuple[str, str, str]:
    topics = topics_for(config, shade)
    availability = [
        {
            "topic": bridge_availability_topic(config),
            "payload_available": "online",
            "payload_not_available": "offline",
        },
        {
            "topic": topics.availability,
            "payload_available": "online",
            "payload_not_available": "offline",
        },
    ]
    device = {
        "identifiers": [f"tilt_{shade.id}"],
        "name": shade.name,
        "manufacturer": "Tilt / SmarterHome",
        "model": "Smart Roller Shade",
    }
    origin = {
        "name": "Tilt Local Bridge",
        "support_url": "https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge",
    }
    cover = {
        "name": None,
        "unique_id": f"tilt_{shade.id}",
        "device_class": "shade",
        "command_topic": topics.command,
        "set_position_topic": topics.set_position,
        "position_topic": topics.position,
        "payload_open": "OPEN",
        "payload_close": "CLOSE",
        "payload_stop": None,
        "position_open": 100,
        "position_closed": 0,
        "optimistic": False,
        "retain": False,
        "availability": availability,
        "availability_mode": "all",
        "device": device,
        "origin": origin,
    }
    position = {
        "name": "Position",
        "unique_id": f"tilt_{shade.id}_position",
        "state_topic": topics.position,
        "command_topic": topics.set_position,
        "min": 0,
        "max": 100,
        "step": 1,
        "mode": "slider",
        "unit_of_measurement": "%",
        "optimistic": False,
        "retain": False,
        "enabled_by_default": True,
        "visible_by_default": True,
        "availability": availability,
        "availability_mode": "all",
        "device": device,
        "origin": origin,
    }
    battery = {
        "name": "Battery",
        "unique_id": f"tilt_{shade.id}_battery",
        "state_topic": topics.battery,
        "device_class": "battery",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "availability": availability,
        "availability_mode": "all",
        "device": device,
        "origin": origin,
    }
    return _json(cover), _json(position), _json(battery)


def parse_position_command(message: IncomingMqttMessage, topics: ShadeTopics) -> int | None:
    if message.retain:
        return None
    try:
        payload = message.payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    if message.topic == topics.command:
        return {"OPEN": 100, "CLOSE": 0}.get(payload)
    if message.topic == topics.set_position and _POSITION_PAYLOAD.fullmatch(payload):
        return int(payload)
    return None


class TiltMqttBridge:
    """Coordinate MQTT state and bounded BLE operations without raw commands."""

    def __init__(
        self,
        config: TiltBridgeConfig,
        publisher: MqttPublisher,
        shade_clients: dict[str, TiltShadeClient],
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        pairing_surface: bool = False,
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._shade_clients = shade_clients
        self._shade_by_id = {shade.id: shade for shade in config.shades}
        self._topics = {shade.id: topics_for(config, shade) for shade in config.shades}
        self._bridge_topics = bridge_topics_for(config)
        self._pairing_surface = pairing_surface
        self._bridge_command_handler: Callable[[bytes], Awaitable[None]] | None = None
        self._status_listeners: list[Callable[[str], None]] = []
        self._status_updated: dict[str, float] = {}
        self._commanded_at: dict[str, float] = {}
        self._pairing_request_text: str | None = None
        self._paired_phone_count: int | None = None
        self._publish_failures = 0
        self._topic_to_shade = {
            topic: shade_id
            for shade_id, topics in self._topics.items()
            for topic in (topics.command, topics.set_position)
        }
        self._status_cache: dict[str, ShadeStatus] = {}
        self._available_shades: set[str] = set()
        self._pending_targets: dict[str, int] = {}
        self._verification_targets: dict[str, int] = {}
        self._command_events = {shade.id: asyncio.Event() for shade in config.shades}
        self._workers: list[asyncio.Task[None]] = []
        self._refresh_lock = asyncio.Lock()
        self._wall_clock = wall_clock or _utc_now
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._last_refresh_monotonic: float | None = None
        self._stopping = False

    async def start(self) -> None:
        self._publish_bridge_availability("offline")
        for shade in self._config.shades:
            topics = self._topics[shade.id]
            self._available_shades.discard(shade.id)
            self._publish(topics.availability, "offline", retain=True)
            cover, position, battery = discovery_payloads(self._config, shade)
            self._publish(topics.cover_discovery, cover, retain=True)
            self._publish(topics.position_discovery, position, retain=True)
            self._publish(topics.battery_discovery, battery, retain=True)
            self._workers.append(
                asyncio.create_task(
                    self._command_worker(shade.id),
                    name=f"tilt-command-{shade.id}",
                )
            )
        self._publish_pairing_surface()
        self._subscribe_topics()
        await self.refresh_all()
        self._workers.append(
            asyncio.create_task(self._poll_loop(), name="tilt-status-poll")
        )

    async def stop(self) -> None:
        self._stopping = True
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        for shade in self._config.shades:
            self._available_shades.discard(shade.id)
            self._publish(self._topics[shade.id].availability, "offline", retain=True)
        self._publish_bridge_availability("offline")

    # ----- surface used by the Bluetooth phone remote -----

    def set_bridge_command_handler(
        self, handler: Callable[[bytes], Awaitable[None]] | None
    ) -> None:
        self._bridge_command_handler = handler

    def add_status_listener(self, listener: Callable[[str], None]) -> None:
        self._status_listeners.append(listener)

    def shade_snapshots(self) -> tuple[ShadeSnapshot, ...]:
        now = self._monotonic_clock()
        snapshots = []
        for shade in self._config.shades:
            status = self._status_cache.get(shade.id)
            updated = self._status_updated.get(shade.id)
            target = self._pending_targets.get(shade.id)
            if target is None:
                target = self._verification_targets.get(shade.id)
            snapshots.append(
                ShadeSnapshot(
                    id=shade.id,
                    name=shade.name,
                    position_percent=status.position_percent if status else None,
                    battery_percent=status.battery_percent if status else None,
                    available=shade.id in self._available_shades,
                    target_percent=target,
                    age_seconds=int(max(0.0, now - updated)) if updated is not None else None,
                    commanded_at=self._commanded_at.get(shade.id),
                )
            )
        return tuple(snapshots)

    def request_position(self, shade_id: str, target: int) -> str:
        """Queue one absolute position the same way a Home Assistant command is."""

        if shade_id not in self._shade_by_id:
            return "unknown_shade"
        if shade_id not in self._shade_clients or shade_id not in self._available_shades:
            return "unavailable"
        if shade_id in self._verification_targets:
            if target == self._verification_targets[shade_id]:
                _LOGGER.info(
                    "Tilt shade %s is already verifying position %s; ignoring duplicate",
                    shade_id,
                    target,
                )
                return "duplicate"
            _LOGGER.info(
                "Tilt shade %s asked for position %s while verifying; superseding",
                shade_id,
                target,
            )
        self._pending_targets[shade_id] = target
        self._commanded_at[shade_id] = self._monotonic_clock()
        self._command_events[shade_id].set()
        return "accepted"

    def publish_pairing_request(self, text: str | None) -> None:
        self._pairing_request_text = text
        if self._pairing_surface:
            self._publish(self._bridge_topics.pairing_request, text or "none", retain=True)

    def publish_paired_phone_count(self, count: int) -> None:
        self._paired_phone_count = count
        if self._pairing_surface:
            self._publish(self._bridge_topics.paired_phones, str(count), retain=True)

    def _publish_pairing_surface(self) -> None:
        if not self._pairing_surface:
            return
        request, approve, phones = pairing_discovery_payloads(self._config)
        topics = self._bridge_topics
        self._publish(topics.pairing_request_discovery, request, retain=True)
        self._publish(topics.approve_pairing_discovery, approve, retain=True)
        self._publish(topics.paired_phones_discovery, phones, retain=True)
        self._publish(topics.pairing_request, self._pairing_request_text or "none", retain=True)
        if self._paired_phone_count is not None:
            self._publish(topics.paired_phones, str(self._paired_phone_count), retain=True)

    def _notify_status_listeners(self, shade_id: str) -> None:
        for listener in self._status_listeners:
            try:
                listener(shade_id)
            except Exception as exc:
                _LOGGER.warning("Status listener failed: %s", type(exc).__name__)

    def _mark_offline(self, shade_id: str) -> None:
        self._available_shades.discard(shade_id)
        self._publish(self._topics[shade_id].availability, "offline", retain=True)
        self._notify_status_listeners(shade_id)

    def _publish(self, topic: str, payload: str, *, retain: bool) -> None:
        """Publish without letting a broker outage take the bridge down.

        The Bluetooth remote exists for the moments the network is gone, so a
        rejected publish is logged and the cached state is republished when the
        broker comes back, rather than raised into the BLE path.
        """

        try:
            self._publisher.publish(topic, payload, retain=retain)
        except Exception as exc:
            self._publish_failures += 1
            log = _LOGGER.warning if self._publish_failures == 1 else _LOGGER.debug
            log("MQTT publish to %s failed: %s", topic, type(exc).__name__)
            return
        if self._publish_failures:
            _LOGGER.info(
                "MQTT publishing recovered after %d failures", self._publish_failures
            )
            self._publish_failures = 0

    async def refresh_all(self) -> None:
        async with self._refresh_lock:
            succeeded = False
            for shade_id in self._shade_by_id:
                if await self._refresh_shade(shade_id):
                    succeeded = True
            self._publish_bridge_availability(
                "online" if succeeded or self._available_shades else "offline"
            )
            self._last_refresh_monotonic = self._monotonic_clock()

    async def handle_reconnect(self) -> None:
        """Restore the clean MQTT session before publishing fresh availability."""

        if self._stopping:
            return
        self._publish_bridge_availability("offline")
        for shade in self._config.shades:
            self._available_shades.discard(shade.id)
            self._publish(self._topics[shade.id].availability, "offline", retain=True)
        self._subscribe_topics()
        self._republish_discovery_and_cached_state(mark_available=False)
        await self.refresh_all()

    async def handle_message(self, message: IncomingMqttMessage) -> None:
        if message.topic == "homeassistant/status":
            if not message.retain and message.payload == b"online":
                self._republish_discovery_and_cached_state(mark_available=True)
            return
        if message.topic == self._bridge_topics.command:
            handler = self._bridge_command_handler
            if handler is not None and not message.retain:
                await handler(message.payload)
            return
        shade_id = self._topic_to_shade.get(message.topic)
        if shade_id is None:
            return
        target = parse_position_command(message, self._topics[shade_id])
        if target is None:
            return
        self.request_position(shade_id, target)

    async def _refresh_shade(self, shade_id: str) -> bool:
        client = self._shade_clients.get(shade_id)
        if client is None:
            return False
        try:
            status = await client.read_status()
        except Exception as exc:
            if shade_id in self._verification_targets:
                _LOGGER.warning(
                    "Tilt shade %s status read deferred during position verification: %s",
                    shade_id,
                    type(exc).__name__,
                )
                return False
            self._mark_offline(shade_id)
            _LOGGER.warning("Tilt shade %s status read failed: %s", shade_id, type(exc).__name__)
            return False
        self._publish_status(shade_id, status)
        return True

    def _publish_status(self, shade_id: str, status: ShadeStatus) -> None:
        self._status_cache[shade_id] = status
        self._status_updated[shade_id] = self._monotonic_clock()
        self._available_shades.add(shade_id)
        topics = self._topics[shade_id]
        self._publish(topics.position, str(status.position_percent), retain=True)
        self._publish(topics.battery, str(status.battery_percent), retain=True)
        self._publish(topics.availability, "online", retain=True)
        self._notify_status_listeners(shade_id)

    async def _command_worker(self, shade_id: str) -> None:
        event = self._command_events[shade_id]
        last_attempt = 0.0
        while True:
            await event.wait()
            event.clear()
            wait_seconds = self._config.command_cooldown_seconds - (
                time.monotonic() - last_attempt
            )
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            client = self._shade_clients.get(shade_id)
            if client is None:
                self._pending_targets.pop(shade_id, None)
                event.clear()
                continue
            verification_pending = False
            async with self._refresh_lock:
                # Polling may have held the lock while Home Assistant sent a
                # newer target. Select the latest target only when the BLE
                # operation can actually begin, then consume its wakeup.
                target = self._pending_targets.pop(shade_id, None)
                event.clear()
                if target is None:
                    continue
                if shade_id not in self._available_shades:
                    continue
                last_attempt = time.monotonic()
                try:
                    status, _moved = await client.set_position_and_read_status(target)
                except PositionVerificationPending as exc:
                    self._verification_targets[shade_id] = target
                    self._publish_status(shade_id, exc.status)
                    verification_pending = True
                    _LOGGER.info(
                        "Tilt shade %s is still moving toward position %s; verification pending",
                        shade_id,
                        target,
                    )
                except Exception as exc:
                    self._mark_offline(shade_id)
                    _LOGGER.warning(
                        "Tilt shade %s position request failed: %s",
                        shade_id,
                        type(exc).__name__,
                    )
                    continue
                else:
                    self._publish_status(shade_id, status)
            if verification_pending:
                await self._reconcile_position(shade_id, target)
                continue
            if shade_id in self._pending_targets:
                event.set()

    async def _superseded_within(self, shade_id: str, delay: float) -> bool:
        """Wait out the delay, returning True as soon as a newer target is queued.

        The command event is only ever set alongside a pending target, so an
        event seen here means Home Assistant asked for a different position
        while the previous one was still being verified.
        """

        try:
            await asyncio.wait_for(self._command_events[shade_id].wait(), timeout=delay)
        except TimeoutError:
            return False
        return shade_id in self._pending_targets

    async def _reconcile_position(self, shade_id: str, target: int) -> None:
        client = self._shade_clients.get(shade_id)
        if client is None:
            self._verification_targets.pop(shade_id, None)
            return
        observed_status = False
        last_error: Exception | None = None
        for delay in _POSITION_RECONCILE_DELAYS:
            if await self._superseded_within(shade_id, delay):
                self._verification_targets.pop(shade_id, None)
                _LOGGER.info(
                    "Tilt shade %s dropped verification of position %s for a newer command",
                    shade_id,
                    target,
                )
                return
            if self._verification_targets.get(shade_id) != target:
                return
            try:
                status = await client.read_status()
            except Exception as exc:
                last_error = exc
                continue
            observed_status = True
            self._publish_status(shade_id, status)
            if status.position_percent == target:
                self._verification_targets.pop(shade_id, None)
                _LOGGER.info(
                    "Tilt shade %s reached verified position %s", shade_id, target
                )
                return

        self._verification_targets.pop(shade_id, None)
        if observed_status:
            _LOGGER.warning(
                "Tilt shade %s did not confirm position %s within the verification window",
                shade_id,
                target,
            )
            self._notify_status_listeners(shade_id)
            return
        self._mark_offline(shade_id)
        _LOGGER.warning(
            "Tilt shade %s position verification failed without a status response: %s",
            shade_id,
            type(last_error).__name__ if last_error is not None else "UnknownError",
        )

    async def _poll_loop(self) -> None:
        while not self._stopping:
            delay = self._poll_delay_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            await self.refresh_all()

    def _poll_delay_seconds(self) -> float:
        if self._last_refresh_monotonic is None:
            return 0.0
        moment = self._wall_clock()
        elapsed = max(0.0, self._monotonic_clock() - self._last_refresh_monotonic)
        delay = max(0.0, self._config.poll_interval_at(moment) - elapsed)
        if self._config.quiet_hours is not None:
            delay = min(
                delay,
                self._config.quiet_hours.seconds_until_transition(moment),
            )
        return delay

    def _subscribe_topics(self) -> None:
        for topics in self._topics.values():
            self._publisher.subscribe(topics.command)
            self._publisher.subscribe(topics.set_position)
        self._publisher.subscribe("homeassistant/status")
        if self._pairing_surface:
            self._publisher.subscribe(self._bridge_topics.command)

    def _republish_discovery_and_cached_state(self, *, mark_available: bool) -> None:
        for shade in self._config.shades:
            topics = self._topics[shade.id]
            cover, position, battery = discovery_payloads(self._config, shade)
            self._publish(topics.cover_discovery, cover, retain=True)
            self._publish(topics.position_discovery, position, retain=True)
            self._publish(topics.battery_discovery, battery, retain=True)
            if status := self._status_cache.get(shade.id):
                self._publish(topics.position, str(status.position_percent), retain=True)
                self._publish(topics.battery, str(status.battery_percent), retain=True)
                if mark_available and shade.id in self._available_shades:
                    self._publish(topics.availability, "online", retain=True)
        self._publish_pairing_surface()

    def _publish_bridge_availability(self, value: str) -> None:
        self._publish(bridge_availability_topic(self._config), value, retain=True)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
