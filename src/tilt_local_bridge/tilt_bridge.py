"""Executable Tilt BLE to Home Assistant MQTT bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .tilt_ble import TiltBleError, TiltShadeClient
from .tilt_bridge_config import (
    ShadeAccessDisabled,
    TiltBridgeConfig,
    TiltBridgeConfigError,
    authorize_shade_access,
    load_config,
    load_pairing_key,
    load_secret,
)
from .tilt_mqtt import (
    IncomingMqttMessage,
    TiltMqttBridge,
    bridge_availability_topic,
)
from .tilt_key_import import import_pairing_keys
from .tilt_protocol import TiltProtocolError


_LOGGER = logging.getLogger(__name__)
_DEFAULT_CONFIG = Path("/etc/tilt-local-bridge/bridge.json")


class PahoMqttConnection:
    """Small threaded Paho adapter that hands messages to the asyncio bridge."""

    def __init__(self, config: TiltBridgeConfig) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise TiltBridgeConfigError(
                "The Tilt bridge requires the paho-mqtt package."
            ) from exc
        self._mqtt = mqtt
        self._config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()
        self._connect_error: TiltBridgeConfigError | None = None
        self._message_handler: Callable[[IncomingMqttMessage], Awaitable[None]] | None = None
        self._reconnect_handler: Callable[[], Awaitable[None]] | None = None
        self._ever_connected = False
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="tilt-local-bridge",
            clean_session=True,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=True,
        )
        username = load_secret(config.mqtt.username_file, label="MQTT username")
        password = load_secret(config.mqtt.password_file, label="MQTT password")
        self._client.username_pw_set(username, password)
        self._client.will_set(
            bridge_availability_topic(config),
            payload="offline",
            qos=1,
            retain=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    async def connect(
        self,
        message_handler: Callable[[IncomingMqttMessage], Awaitable[None]],
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._message_handler = message_handler
        self._client.connect_async(
            self._config.mqtt.host,
            port=self._config.mqtt.port,
            keepalive=self._config.mqtt.keepalive_seconds,
        )
        self._client.loop_start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._client.loop_stop()
            raise TiltBridgeConfigError("Timed out connecting to the MQTT broker.") from exc
        if self._connect_error is not None:
            self._client.loop_stop()
            raise self._connect_error

    def set_reconnect_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        self._reconnect_handler = handler

    def publish(self, topic: str, payload: str, *, retain: bool, qos: int = 1) -> None:
        info = self._client.publish(topic, payload=payload, qos=qos, retain=retain)
        if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
            raise TiltBridgeConfigError("MQTT publish was rejected by the client.")

    def subscribe(self, topic: str, *, qos: int = 1) -> None:
        result, _message_id = self._client.subscribe(topic, qos=qos)
        if result != self._mqtt.MQTT_ERR_SUCCESS:
            raise TiltBridgeConfigError("MQTT subscription was rejected by the client.")

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(
        self,
        _client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        loop = self._loop
        if loop is None:
            return
        if getattr(reason_code, "is_failure", False):
            self._connect_error = TiltBridgeConfigError(
                "MQTT broker rejected the connection."
            )
            loop.call_soon_threadsafe(self._connected.set)
            return
        was_connected = self._ever_connected
        self._ever_connected = True
        loop.call_soon_threadsafe(self._connected.set)
        if was_connected and self._reconnect_handler is not None:
            self._schedule(self._reconnect_handler())

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        if self._message_handler is None:
            return
        incoming = IncomingMqttMessage(
            topic=str(message.topic),
            payload=bytes(message.payload),
            retain=bool(message.retain),
        )
        self._schedule(self._message_handler(incoming))

    def _schedule(self, awaitable: Awaitable[None]) -> None:
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)

        def completed(result: Any) -> None:
            try:
                result.result()
            except Exception as exc:
                _LOGGER.warning("MQTT callback failed: %s", type(exc).__name__)

        future.add_done_callback(completed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "check-config",
        help="Validate configuration without loading secrets or using network devices.",
    )

    runtime_check = subparsers.add_parser(
        "check-runtime",
        help="Validate protected runtime files without using network devices.",
    )
    runtime_check.add_argument(
        "--expect-shade-reads",
        action="store_true",
        help="Require read access and validate every configured pairing-key file.",
    )
    runtime_check.add_argument(
        "--expect-position-writes",
        action="store_true",
        help="Require configured position-write access in addition to reads.",
    )

    probe = subparsers.add_parser(
        "probe-status",
        help="Perform one allowlisted status read for one named shade.",
    )
    probe.add_argument("--shade", required=True, help="Exact configured shade id.")
    probe.add_argument(
        "--allow-shade-reads",
        action="store_true",
        help="Second gate required in addition to config access.allow_reads.",
    )

    key_import = subparsers.add_parser(
        "import-cloud-store",
        help="Import configured shade keys from a protected Tilt cloud-store export.",
    )
    key_import.add_argument("--input", type=Path, required=True)
    key_import.add_argument(
        "--replace-existing",
        action="store_true",
        help="Allow replacement when an existing protected key differs.",
    )

    serve = subparsers.add_parser("serve", help="Run the MQTT bridge.")
    serve.add_argument("--allow-shade-reads", action="store_true")
    serve.add_argument("--allow-position-writes", action="store_true")
    return parser


async def _run_probe(config: TiltBridgeConfig, args: argparse.Namespace) -> int:
    permit = authorize_shade_access(
        config,
        request_reads=args.allow_shade_reads,
        request_position_writes=False,
    )
    shade = next((item for item in config.shades if item.id == args.shade), None)
    if shade is None:
        raise TiltBridgeConfigError("Requested shade id is not configured.")
    client = TiltShadeClient(shade, load_pairing_key(shade.pairing_key_file), permit)
    status = await client.read_status()
    print(
        json.dumps(
            {
                "shade": shade.id,
                "position_percent": status.position_percent,
                "battery_percent": status.battery_percent,
                "charge_status": status.charge_status,
                "calibrated": status.calibrated,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_runtime_check(config: TiltBridgeConfig, args: argparse.Namespace) -> int:
    if args.expect_position_writes and not args.expect_shade_reads:
        raise ShadeAccessDisabled("Position writes require the read expectation flag.")
    load_secret(config.mqtt.username_file, label="MQTT username")
    load_secret(config.mqtt.password_file, label="MQTT password")
    key_count = 0
    if args.expect_shade_reads or args.expect_position_writes:
        authorize_shade_access(
            config,
            request_reads=args.expect_shade_reads,
            request_position_writes=args.expect_position_writes,
        )
        for shade in config.shades:
            load_pairing_key(shade.pairing_key_file)
            key_count += 1
    print(
        json.dumps(
            {
                "ready": True,
                "mqtt_credentials_valid": True,
                "pairing_key_count": key_count,
                "expected_read_access": args.expect_shade_reads,
                "expected_position_write_access": args.expect_position_writes,
            },
            sort_keys=True,
        )
    )
    return 0


async def _run_service(config: TiltBridgeConfig, args: argparse.Namespace) -> int:
    if args.allow_position_writes and not args.allow_shade_reads:
        raise ShadeAccessDisabled("Position writes require the read launch flag.")
    shade_clients: dict[str, TiltShadeClient] = {}
    if args.allow_shade_reads:
        permit = authorize_shade_access(
            config,
            request_reads=True,
            request_position_writes=args.allow_position_writes,
        )
        for shade in config.shades:
            shade_clients[shade.id] = TiltShadeClient(
                shade,
                load_pairing_key(shade.pairing_key_file),
                permit,
            )

    connection = PahoMqttConnection(config)
    bridge = TiltMqttBridge(config, connection, shade_clients)
    await connection.connect(bridge.handle_message)
    connection.set_reconnect_handler(bridge.handle_reconnect)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except NotImplementedError:  # pragma: no cover - POSIX deployment
            pass
    try:
        await bridge.start()
        await stop.wait()
    finally:
        await bridge.stop()
        connection.close()
    return 0


async def _async_main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.command == "check-config":
        print(
            json.dumps(
                {
                    "valid": True,
                    "shade_count": len(config.shades),
                    "configured_read_access": config.access.allow_reads,
                    "configured_position_write_access": config.access.allow_position_writes,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "check-runtime":
        return _run_runtime_check(config, args)
    if args.command == "probe-status":
        return await _run_probe(config, args)
    if args.command == "import-cloud-store":
        result = import_pairing_keys(
            args.input,
            config,
            replace_existing=args.replace_existing,
        )
        print(
            json.dumps(
                {
                    "imported_shades": list(result.imported_shade_ids),
                    "unchanged_shades": list(result.unchanged_shade_ids),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "serve":
        return await _run_service(config, args)
    raise TiltBridgeConfigError("Unknown Tilt bridge command.")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_async_main(args))
    except (TiltBridgeConfigError, TiltBleError, TiltProtocolError) as exc:
        _LOGGER.error("Tilt bridge stopped: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
