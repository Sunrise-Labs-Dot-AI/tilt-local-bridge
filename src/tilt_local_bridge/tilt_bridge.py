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
    authorize_bluetooth_remote,
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
from .tilt_remote import (
    RemotePhoneStore,
    RemoteProtocol,
    RemoteProtocolError,
    check_state_location,
)
from .tilt_remote_ble import RemoteBleError, RemoteGattServer


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
    runtime_check.add_argument(
        "--expect-bluetooth-remote",
        action="store_true",
        help="Require the Bluetooth phone remote to be enabled and its state location usable.",
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
    serve.add_argument(
        "--allow-bluetooth-remote",
        action="store_true",
        help="Second gate, with bluetooth_remote.enabled, for the phone remote.",
    )

    phones = subparsers.add_parser(
        "remote-phones",
        help="List or remove phones paired with the Bluetooth remote.",
    )
    phone_actions = phones.add_subparsers(dest="phone_action", required=True)
    phone_actions.add_parser("list", help="List paired phones without secrets.")
    remove = phone_actions.add_parser("remove", help="Remove phones by key prefix.")
    remove.add_argument(
        "--key-prefix",
        required=True,
        help="At least eight hex digits of the phone key shown by list.",
    )
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
    expect_remote = bool(getattr(args, "expect_bluetooth_remote", False))
    if expect_remote and not args.expect_shade_reads:
        raise ShadeAccessDisabled("The Bluetooth remote requires the read expectation flag.")
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
    report: dict[str, Any] = {
        "ready": True,
        "mqtt_credentials_valid": True,
        "pairing_key_count": key_count,
        "expected_read_access": args.expect_shade_reads,
        "expected_position_write_access": args.expect_position_writes,
        "expected_bluetooth_remote": expect_remote,
    }
    if expect_remote:
        remote = authorize_bluetooth_remote(config, request_remote=True)
        report.update(check_state_location(remote.state_file))
    print(json.dumps(report, sort_keys=True))
    return 0


def _run_remote_phones(config: TiltBridgeConfig, args: argparse.Namespace) -> int:
    remote = config.bluetooth_remote
    if remote is None:
        raise TiltBridgeConfigError("bluetooth_remote is not configured.")
    store = RemotePhoneStore(remote.state_file)
    if args.phone_action == "list":
        if not remote.state_file.exists():
            print(json.dumps({"bridge_id": None, "phones": []}, sort_keys=True))
            return 0
        print(
            json.dumps(
                {
                    "bridge_id": store.bridge_id,
                    "phones": [
                        {
                            "name": phone.name,
                            "key_prefix": phone.public_key[:8],
                            "paired_at": phone.paired_at,
                        }
                        for phone in store.phones()
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.phone_action == "remove":
        if not remote.state_file.exists():
            raise TiltBridgeConfigError("No phones have been paired yet.")
        removed = store.remove(args.key_prefix)
        print(
            json.dumps(
                {
                    "removed": [
                        {"name": phone.name, "key_prefix": phone.public_key[:8]}
                        for phone in removed
                    ]
                },
                sort_keys=True,
            )
        )
        return 0
    raise TiltBridgeConfigError("Unknown remote-phones action.")


async def _run_service(config: TiltBridgeConfig, args: argparse.Namespace) -> int:
    if args.allow_position_writes and not args.allow_shade_reads:
        raise ShadeAccessDisabled("Position writes require the read launch flag.")
    allow_remote = bool(getattr(args, "allow_bluetooth_remote", False))
    if allow_remote and not args.allow_shade_reads:
        raise ShadeAccessDisabled("The Bluetooth remote requires the read launch flag.")
    # Every gate is checked before any secret is read.
    remote_config = (
        authorize_bluetooth_remote(config, request_remote=True) if allow_remote else None
    )
    shade_clients: dict[str, TiltShadeClient] = {}
    position_writes_enabled = False
    if args.allow_shade_reads:
        permit = authorize_shade_access(
            config,
            request_reads=True,
            request_position_writes=args.allow_position_writes,
        )
        position_writes_enabled = permit.can_write_position
        for shade in config.shades:
            shade_clients[shade.id] = TiltShadeClient(
                shade,
                load_pairing_key(shade.pairing_key_file),
                permit,
            )

    connection = PahoMqttConnection(config)
    bridge = TiltMqttBridge(
        config, connection, shade_clients, pairing_surface=remote_config is not None
    )
    remote_protocol: RemoteProtocol | None = None
    remote_server: RemoteGattServer | None = None
    if remote_config is not None:
        store = RemotePhoneStore(remote_config.state_file)
        store.load()
        remote_protocol = RemoteProtocol(
            store,
            bridge,
            name=remote_config.name,
            position_writes_enabled=position_writes_enabled,
        )
        bridge.set_bridge_command_handler(remote_protocol.handle_bridge_command)
        bridge.add_status_listener(remote_protocol.notify_status_changed)
        remote_server = RemoteGattServer(
            remote_protocol,
            adapter=remote_config.adapter,
            local_name=remote_config.name,
        )
    await connection.connect(bridge.handle_message)
    connection.set_reconnect_handler(bridge.handle_reconnect)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except NotImplementedError:  # pragma: no cover - POSIX deployment
            pass
    if remote_protocol is not None:
        protocol = remote_protocol

        def approve_from_operator() -> None:
            for work in protocol.approve_pending("SIGUSR1"):
                asyncio.ensure_future(work)

        try:
            loop.add_signal_handler(signal.SIGUSR1, approve_from_operator)
        except NotImplementedError:  # pragma: no cover - POSIX deployment
            pass
    try:
        await bridge.start()
        if remote_server is not None:
            remote_server = await _start_remote_or_continue(remote_server)
        await stop.wait()
    finally:
        if remote_server is not None:
            await remote_server.stop()
        await bridge.stop()
        connection.close()
    return 0


async def _start_remote_or_continue(
    remote_server: RemoteGattServer,
) -> RemoteGattServer | None:
    """Publish the phone remote, or log and carry on serving Home Assistant.

    A bridge whose adapter refuses a GATT registration still has every shade
    and its MQTT job to do. Failing the whole service here would take the
    shades offline for the sake of the optional path.
    """

    try:
        await remote_server.start()
    except (RemoteBleError, RemoteProtocolError, OSError) as exc:
        _LOGGER.error(
            "Bluetooth remote could not start; continuing without it: %s", exc
        )
        try:
            await remote_server.stop()
        except Exception as cleanup_error:
            _LOGGER.debug("Remote cleanup after failed start: %s", cleanup_error)
        return None
    return remote_server


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
    if args.command == "remote-phones":
        return _run_remote_phones(config, args)
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
    except (
        TiltBridgeConfigError,
        TiltBleError,
        TiltProtocolError,
        RemoteProtocolError,
        RemoteBleError,
    ) as exc:
        _LOGGER.error("Tilt bridge stopped: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
