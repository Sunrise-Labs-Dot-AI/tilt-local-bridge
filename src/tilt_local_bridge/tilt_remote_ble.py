"""BlueZ GATT server that carries the Bluetooth phone remote.

This module is the only place the remote touches Bluetooth. It publishes one
service with three characteristics on the bridge's own adapter, advertises it,
and hands whole reassembled messages to ``tilt_remote.RemoteProtocol``. It
cannot connect to a shade: shade sessions stay in ``tilt_ble`` behind the
existing permits.

D-Bus type annotations below are plain signature strings, which is why this
module deliberately does not enable postponed annotations.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .tilt_remote import (
    MessageAssembler,
    RemoteProtocol,
    RemoteProtocolError,
    REMOTE_INFO_UUID,
    REMOTE_REQUEST_UUID,
    REMOTE_RESPONSE_UUID,
    REMOTE_SERVICE_UUID,
    chunk_message,
    chunk_size_for_mtu,
)


_LOGGER = logging.getLogger(__name__)

BLUEZ_BUS_NAME = "org.bluez"
APPLICATION_PATH = "/org/tilt_local_bridge/remote"
SERVICE_PATH = APPLICATION_PATH + "/service0"
ADVERTISEMENT_PATH = APPLICATION_PATH + "/advertisement0"
_TICK_SECONDS = 5.0
_NOTIFY_GAP_SECONDS = 0.005
_READVERTISE_DELAY_SECONDS = 1.0
_ADVERTISING_CHECK_TICKS = 12


class RemoteBleError(RuntimeError):
    """Raised when the GATT service cannot be published."""


def _service_interfaces() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        from dbus_fast import DBusError, PropertyAccess, Variant
        from dbus_fast.service import ServiceInterface, dbus_property, method
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RemoteBleError("The Bluetooth remote requires the dbus-fast package.") from exc
    return ServiceInterface, dbus_property, method, PropertyAccess, Variant, DBusError


def build_gatt_objects(
    *,
    local_name: str,
    info_payload: Callable[[], bytes],
    on_write: Callable[[str, bytes, int | None], None],
) -> dict[str, Any]:
    """Create the D-Bus objects for the service. Pure construction, no bus."""

    ServiceInterface, dbus_property, method, PropertyAccess, Variant, DBusError = (
        _service_interfaces()
    )

    class Application(ServiceInterface):
        """Root node; dbus-fast answers GetManagedObjects for its children."""

        def __init__(self) -> None:
            super().__init__("org.tilt_local_bridge.RemoteApplication1")

    class Advertisement(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.LEAdvertisement1")

        @method()
        def Release(self) -> None:
            _LOGGER.info("BlueZ released the remote advertisement")

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":
            return "peripheral"

        @dbus_property(access=PropertyAccess.READ)
        def ServiceUUIDs(self) -> "as":
            return [REMOTE_SERVICE_UUID]

        @dbus_property(access=PropertyAccess.READ)
        def LocalName(self) -> "s":
            return local_name

        @dbus_property(access=PropertyAccess.READ)
        def Discoverable(self) -> "b":
            return True

    class Service(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.GattService1")

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return REMOTE_SERVICE_UUID

        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b":
            return True

    class Characteristic(ServiceInterface):
        def __init__(self, uuid: str, flags: list[str]) -> None:
            super().__init__("org.bluez.GattCharacteristic1")
            self._uuid = uuid
            self._flags = flags
            self._value = b""
            self._notifying = False

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return self._uuid

        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o":
            return SERVICE_PATH

        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as":
            return self._flags

        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay":
            return self._value

        @dbus_property(access=PropertyAccess.READ)
        def Notifying(self) -> "b":
            return self._notifying

        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":
            raise DBusError("org.bluez.Error.NotSupported", "Read is not supported.")

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}") -> None:
            raise DBusError("org.bluez.Error.NotSupported", "Write is not supported.")

        @method()
        def StartNotify(self) -> None:
            raise DBusError("org.bluez.Error.NotSupported", "Notify is not supported.")

        @method()
        def StopNotify(self) -> None:
            raise DBusError("org.bluez.Error.NotSupported", "Notify is not supported.")

    class InfoCharacteristic(Characteristic):
        def __init__(self) -> None:
            super().__init__(REMOTE_INFO_UUID, ["read"])

        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":
            offset = _option_int(options, "offset", Variant)
            payload = info_payload()
            if offset > len(payload):
                raise DBusError("org.bluez.Error.InvalidOffset", "Offset is past the value.")
            return payload[offset:]

    class RequestCharacteristic(Characteristic):
        def __init__(self) -> None:
            super().__init__(REMOTE_REQUEST_UUID, ["write", "write-without-response"])

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}") -> None:
            device = _option_str(options, "device", Variant)
            if device is None:
                raise DBusError("org.bluez.Error.NotPermitted", "Write has no device.")
            if _option_int(options, "offset", Variant) != 0:
                raise DBusError("org.bluez.Error.NotSupported", "Offset writes are not supported.")
            mtu = _option_int(options, "mtu", Variant) or None
            on_write(device, bytes(value), mtu)

    class ResponseCharacteristic(Characteristic):
        def __init__(self) -> None:
            super().__init__(REMOTE_RESPONSE_UUID, ["notify"])

        @method()
        def StartNotify(self) -> None:
            self._notifying = True

        @method()
        def StopNotify(self) -> None:
            self._notifying = False

        def push(self, chunk: bytes) -> None:
            self._value = chunk
            self.emit_properties_changed({"Value": chunk})

    return {
        "application": Application(),
        "advertisement": Advertisement(),
        "service": Service(),
        "info": InfoCharacteristic(),
        "request": RequestCharacteristic(),
        "response": ResponseCharacteristic(),
    }


def _option_int(options: Any, key: str, variant_type: Any) -> int:
    value = options.get(key) if isinstance(options, dict) else None
    if value is None:
        return 0
    raw = value.value if isinstance(value, variant_type) else value
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _option_str(options: Any, key: str, variant_type: Any) -> str | None:
    value = options.get(key) if isinstance(options, dict) else None
    if value is None:
        return None
    raw = value.value if isinstance(value, variant_type) else value
    return raw if isinstance(raw, str) and raw else None


class RemoteConnections:
    """Per-device chunk assembly and MTU tracking, independent of D-Bus."""

    def __init__(self, protocol: RemoteProtocol) -> None:
        self._protocol = protocol
        self._assemblers: dict[str, MessageAssembler] = {}
        self._mtus: dict[str, int] = {}

    def chunk_size(self, connection: str) -> int:
        return chunk_size_for_mtu(self._mtus.get(connection))

    def receive_chunk(self, connection: str, chunk: bytes, mtu: int | None) -> bytes | None:
        if mtu is not None and mtu >= 23:
            self._mtus[connection] = mtu
        assembler = self._assemblers.get(connection)
        if assembler is None:
            assembler = MessageAssembler()
            self._assemblers[connection] = assembler
        self._protocol.open_connection(connection)
        try:
            return assembler.add(chunk)
        except RemoteProtocolError as exc:
            _LOGGER.debug("Dropped remote chunk from %s: %s", connection, exc)
            return None

    def close(self, connection: str) -> None:
        self._assemblers.pop(connection, None)
        self._mtus.pop(connection, None)
        self._protocol.close_connection(connection)


class RemoteGattServer:
    """Publish the remote service on the bridge adapter and route messages."""

    def __init__(
        self,
        protocol: RemoteProtocol,
        *,
        adapter: str = "hci0",
        local_name: str,
        bus_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._protocol = protocol
        self._adapter_path = f"/org/bluez/{adapter}"
        self._local_name = local_name
        self._bus_factory = bus_factory or _system_bus
        self._bus: Any = None
        self._objects: dict[str, Any] = {}
        self._connections = RemoteConnections(protocol)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._registered_application = False
        self._registered_advertisement = False
        self._notify_lock = asyncio.Lock()
        self._inbound: set[asyncio.Task[None]] = set()
        self._readvertise_task: asyncio.Task[None] | None = None
        self._ticks = 0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._objects = build_gatt_objects(
            local_name=self._local_name,
            info_payload=self._protocol.info_payload,
            on_write=self._on_write,
        )
        self._protocol.set_sender(self.send)
        bus = await self._bus_factory()
        self._bus = bus
        bus.export(APPLICATION_PATH, self._objects["application"])
        bus.export(SERVICE_PATH, self._objects["service"])
        bus.export(SERVICE_PATH + "/char0", self._objects["info"])
        bus.export(SERVICE_PATH + "/char1", self._objects["request"])
        bus.export(SERVICE_PATH + "/char2", self._objects["response"])
        bus.export(ADVERTISEMENT_PATH, self._objects["advertisement"])
        bus.add_message_handler(self._on_bus_message)
        await self._call_bluez(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "AddMatch",
            "s",
            [
                "type='signal',sender='org.bluez',"
                "interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',arg0='org.bluez.Device1'"
            ],
        )
        await self._call_bluez(
            BLUEZ_BUS_NAME,
            self._adapter_path,
            "org.bluez.GattManager1",
            "RegisterApplication",
            "oa{sv}",
            [APPLICATION_PATH, {}],
        )
        self._registered_application = True
        await self._call_bluez(
            BLUEZ_BUS_NAME,
            self._adapter_path,
            "org.bluez.LEAdvertisingManager1",
            "RegisterAdvertisement",
            "oa{sv}",
            [ADVERTISEMENT_PATH, {}],
        )
        self._registered_advertisement = True
        self._protocol.start()
        self._tick_task = self._loop.create_task(self._tick_loop(), name="tilt-remote-tick")
        _LOGGER.info("Bluetooth remote is advertising as %r", self._local_name)

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        if self._readvertise_task is not None:
            self._readvertise_task.cancel()
            try:
                await self._readvertise_task
            except (asyncio.CancelledError, Exception):
                pass
            self._readvertise_task = None
        for task in list(self._inbound):
            task.cancel()
        await self._protocol.close()
        bus = self._bus
        if bus is None:
            return
        try:
            if self._registered_advertisement:
                await self._call_bluez(
                    BLUEZ_BUS_NAME,
                    self._adapter_path,
                    "org.bluez.LEAdvertisingManager1",
                    "UnregisterAdvertisement",
                    "o",
                    [ADVERTISEMENT_PATH],
                )
            if self._registered_application:
                await self._call_bluez(
                    BLUEZ_BUS_NAME,
                    self._adapter_path,
                    "org.bluez.GattManager1",
                    "UnregisterApplication",
                    "o",
                    [APPLICATION_PATH],
                )
        except Exception as exc:
            _LOGGER.warning("Remote unregister failed: %s", type(exc).__name__)
        finally:
            self._registered_advertisement = False
            self._registered_application = False
            try:
                bus.disconnect()
            except Exception:
                pass
            self._bus = None

    async def send(self, connection: str, payload: bytes) -> None:
        response = self._objects.get("response")
        if response is None:
            return
        chunks = chunk_message(payload, chunk_size=self._connections.chunk_size(connection))
        async with self._notify_lock:
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(_NOTIFY_GAP_SECONDS)
                response.push(chunk)

    def _on_write(self, device: str, value: bytes, mtu: int | None) -> None:
        message = self._connections.receive_chunk(device, value, mtu)
        if message is None or self._loop is None:
            return
        task = self._loop.create_task(self._protocol.receive_message(device, message))
        self._inbound.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._inbound.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.warning("Remote request failed: %s", type(exc).__name__)

        task.add_done_callback(done)

    def _on_bus_message(self, message: Any) -> None:
        try:
            from dbus_fast import MessageType
        except ImportError:  # pragma: no cover - deployment dependency
            return
        if message.message_type is not MessageType.SIGNAL:
            return
        if (
            message.interface != "org.freedesktop.DBus.Properties"
            or message.member != "PropertiesChanged"
            or not isinstance(message.path, str)
            or not message.path.startswith(self._adapter_path + "/dev_")
        ):
            return
        body = message.body
        if len(body) < 2 or body[0] != "org.bluez.Device1" or not isinstance(body[1], dict):
            return
        connected = body[1].get("Connected")
        value = getattr(connected, "value", connected)
        if value is False:
            self._connections.close(message.path)
            _LOGGER.debug("Remote connection %s closed", message.path)
            self._schedule_readvertise("disconnect")

    def _schedule_readvertise(self, reason: str) -> None:
        """Re-register the advertisement after a phone drops off.

        Some controllers, the Raspberry Pi Zero 2 W's among them, do not
        resume advertising on their own once a central disconnects, even
        though BlueZ still counts the instance as active. Registering it again
        forces the enable through.
        """

        if self._loop is None or self._bus is None or not self._registered_advertisement:
            return
        if self._readvertise_task is not None and not self._readvertise_task.done():
            return
        self._readvertise_task = self._loop.create_task(
            self._readvertise(reason), name="tilt-remote-readvertise"
        )

    async def _readvertise(self, reason: str) -> None:
        await asyncio.sleep(_READVERTISE_DELAY_SECONDS)
        if self._bus is None:
            return
        try:
            await self._call_bluez(
                BLUEZ_BUS_NAME,
                self._adapter_path,
                "org.bluez.LEAdvertisingManager1",
                "UnregisterAdvertisement",
                "o",
                [ADVERTISEMENT_PATH],
            )
        except RemoteBleError as exc:
            _LOGGER.debug("Unregister before re-advertising: %s", exc)
        try:
            await self._call_bluez(
                BLUEZ_BUS_NAME,
                self._adapter_path,
                "org.bluez.LEAdvertisingManager1",
                "RegisterAdvertisement",
                "oa{sv}",
                [ADVERTISEMENT_PATH, {}],
            )
        except RemoteBleError as exc:
            _LOGGER.warning("Re-advertising after %s failed: %s", reason, exc)
            return
        _LOGGER.info("Bluetooth remote re-advertised after %s", reason)

    async def _active_advertising_instances(self) -> int | None:
        try:
            body = await self._call_bluez(
                BLUEZ_BUS_NAME,
                self._adapter_path,
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                ["org.bluez.LEAdvertisingManager1", "ActiveInstances"],
            )
        except RemoteBleError:
            return None
        if not body:
            return None
        value = getattr(body[0], "value", body[0])
        return value if isinstance(value, int) else None

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            for work in self._protocol.tick():
                try:
                    await work
                except Exception as exc:
                    _LOGGER.warning("Remote tick work failed: %s", type(exc).__name__)
            self._ticks += 1
            if self._ticks % _ADVERTISING_CHECK_TICKS == 0:
                active = await self._active_advertising_instances()
                if active == 0:
                    _LOGGER.warning("BlueZ reports no active advertisement; re-advertising")
                    self._schedule_readvertise("an idle check")

    async def _call_bluez(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
    ) -> Any:
        from dbus_fast import Message, MessageType

        reply = await self._bus.call(
            Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
            )
        )
        if reply is None:
            raise RemoteBleError(f"{member} received no reply from BlueZ.")
        if reply.message_type is MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise RemoteBleError(f"{member} failed: {reply.error_name} {detail}".strip())
        return reply.body


async def _system_bus() -> Any:
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RemoteBleError("The Bluetooth remote requires the dbus-fast package.") from exc
    return await MessageBus(bus_type=BusType.SYSTEM).connect()
