"""Home Assistant MQTT discovery and orchestration for Tilt shades."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from .tilt_ble import PositionVerificationPending, TiltShadeClient
from .tilt_bridge_config import ShadeConfig, TiltBridgeConfig
from .tilt_protocol import ShadeStatus


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


def bridge_availability_topic(config: TiltBridgeConfig) -> str:
    return f"{config.mqtt.topic_prefix}/bridge/availability"


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
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._shade_clients = shade_clients
        self._shade_by_id = {shade.id: shade for shade in config.shades}
        self._topics = {shade.id: topics_for(config, shade) for shade in config.shades}
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
            self._publisher.publish(topics.availability, "offline", retain=True)
            cover, position, battery = discovery_payloads(self._config, shade)
            self._publisher.publish(topics.cover_discovery, cover, retain=True)
            self._publisher.publish(topics.position_discovery, position, retain=True)
            self._publisher.publish(topics.battery_discovery, battery, retain=True)
            self._workers.append(
                asyncio.create_task(
                    self._command_worker(shade.id),
                    name=f"tilt-command-{shade.id}",
                )
            )
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
            self._publisher.publish(
                self._topics[shade.id].availability, "offline", retain=True
            )
        self._publish_bridge_availability("offline")

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
            self._publisher.publish(
                self._topics[shade.id].availability, "offline", retain=True
            )
        self._subscribe_topics()
        self._republish_discovery_and_cached_state(mark_available=False)
        await self.refresh_all()

    async def handle_message(self, message: IncomingMqttMessage) -> None:
        if message.topic == "homeassistant/status":
            if not message.retain and message.payload == b"online":
                self._republish_discovery_and_cached_state(mark_available=True)
            return
        shade_id = self._topic_to_shade.get(message.topic)
        if shade_id is None:
            return
        target = parse_position_command(message, self._topics[shade_id])
        if (
            target is None
            or shade_id not in self._shade_clients
            or shade_id not in self._available_shades
        ):
            return
        if shade_id in self._verification_targets:
            if target == self._verification_targets[shade_id]:
                _LOGGER.info(
                    "Tilt shade %s is already verifying position %s; ignoring duplicate",
                    shade_id,
                    target,
                )
                return
            _LOGGER.info(
                "Tilt shade %s asked for position %s while verifying; superseding",
                shade_id,
                target,
            )
        self._pending_targets[shade_id] = target
        self._command_events[shade_id].set()

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
            self._available_shades.discard(shade_id)
            self._publisher.publish(
                self._topics[shade_id].availability, "offline", retain=True
            )
            _LOGGER.warning("Tilt shade %s status read failed: %s", shade_id, type(exc).__name__)
            return False
        self._publish_status(shade_id, status)
        return True

    def _publish_status(self, shade_id: str, status: ShadeStatus) -> None:
        self._status_cache[shade_id] = status
        self._available_shades.add(shade_id)
        topics = self._topics[shade_id]
        self._publisher.publish(topics.position, str(status.position_percent), retain=True)
        self._publisher.publish(topics.battery, str(status.battery_percent), retain=True)
        self._publisher.publish(topics.availability, "online", retain=True)

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
                    self._available_shades.discard(shade_id)
                    self._publisher.publish(
                        self._topics[shade_id].availability, "offline", retain=True
                    )
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
            return
        self._available_shades.discard(shade_id)
        self._publisher.publish(
            self._topics[shade_id].availability, "offline", retain=True
        )
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

    def _republish_discovery_and_cached_state(self, *, mark_available: bool) -> None:
        for shade in self._config.shades:
            topics = self._topics[shade.id]
            cover, position, battery = discovery_payloads(self._config, shade)
            self._publisher.publish(topics.cover_discovery, cover, retain=True)
            self._publisher.publish(topics.position_discovery, position, retain=True)
            self._publisher.publish(topics.battery_discovery, battery, retain=True)
            if status := self._status_cache.get(shade.id):
                self._publisher.publish(
                    topics.position, str(status.position_percent), retain=True
                )
                self._publisher.publish(
                    topics.battery, str(status.battery_percent), retain=True
                )
                if mark_available and shade.id in self._available_shades:
                    self._publisher.publish(topics.availability, "online", retain=True)

    def _publish_bridge_availability(self, value: str) -> None:
        self._publisher.publish(
            bridge_availability_topic(self._config), value, retain=True
        )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
