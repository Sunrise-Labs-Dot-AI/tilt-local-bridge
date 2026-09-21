"""Offline tests for the GATT server glue, with no bus and no radio."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from dbus_fast import DBusError, MessageType, Variant

from dbus_fast.service import ServiceInterface

from tilt_local_bridge.tilt_remote import (
    MessageAssembler,
    RemotePhoneStore,
    RemoteProtocol,
    ShadeSnapshot,
    canonical_json,
    chunk_message,
)
from tilt_local_bridge.tilt_remote_ble import (
    ADVERTISEMENT_PATH,
    APPLICATION_PATH,
    SERVICE_PATH,
    RemoteBleError,
    RemoteConnections,
    RemoteGattServer,
    build_gatt_objects,
)


class StubBridge:
    def shade_snapshots(self) -> tuple[ShadeSnapshot, ...]:
        return (ShadeSnapshot("door", "Door", 40, 90, True, None, 3),)

    def request_position(self, shade_id: str, position_percent: int) -> str:
        return "accepted"

    async def refresh_all(self) -> None:
        return None

    def publish_pairing_request(self, text: str | None) -> None:
        return None

    def publish_paired_phone_count(self, count: int) -> None:
        return None


def _invoke(interface: Any, name: str, *args: Any) -> Any:
    """Call the underlying method body; the dbus-fast wrapper discards returns."""

    for member in ServiceInterface._get_methods(interface):
        if member.name == name:
            return member.fn(interface, *args)
    raise AssertionError(f"no method {name}")


class GattObjectTests(unittest.TestCase):
    def test_object_graph_exposes_the_documented_interfaces(self) -> None:
        objects = build_gatt_objects(
            local_name="Bridge", info_payload=lambda: b"{}", on_write=lambda *_: None
        )
        self.assertEqual(objects["advertisement"].name, "org.bluez.LEAdvertisement1")
        self.assertEqual(objects["service"].name, "org.bluez.GattService1")
        for key in ("info", "request", "response"):
            self.assertEqual(objects[key].name, "org.bluez.GattCharacteristic1")
        self.assertEqual(objects["info"].Flags, ["read"])
        self.assertEqual(objects["request"].Flags, ["write", "write-without-response"])
        self.assertEqual(objects["response"].Flags, ["notify"])
        self.assertEqual(objects["response"].Service, SERVICE_PATH)
        self.assertEqual(objects["advertisement"].Type, "peripheral")
        self.assertEqual(objects["advertisement"].LocalName, "Bridge")

    def test_info_reads_honour_offsets(self) -> None:
        objects = build_gatt_objects(
            local_name="Bridge", info_payload=lambda: b"0123456789", on_write=lambda *_: None
        )
        info = objects["info"]
        self.assertEqual(bytes(_invoke(info, "ReadValue", {})), b"0123456789")
        self.assertEqual(
            bytes(_invoke(info, "ReadValue", {"offset": Variant("q", 4)})), b"456789"
        )
        with self.assertRaises(DBusError):
            info.ReadValue({"offset": Variant("q", 11)})
        with self.assertRaises(DBusError):
            info.WriteValue(b"x", {})

    def test_request_writes_carry_device_and_mtu(self) -> None:
        seen: list[tuple[str, bytes, int | None]] = []
        objects = build_gatt_objects(
            local_name="Bridge",
            info_payload=lambda: b"{}",
            on_write=lambda device, value, mtu: seen.append((device, value, mtu)),
        )
        request = objects["request"]
        request.WriteValue(
            b"\xc0\x00\x01x",
            {"device": Variant("o", "/org/bluez/hci0/dev_AA"), "mtu": Variant("q", 185)},
        )
        request.WriteValue(b"\xc0\x00\x01y", {"device": Variant("o", "/org/bluez/hci0/dev_BB")})
        self.assertEqual(
            seen,
            [
                ("/org/bluez/hci0/dev_AA", b"\xc0\x00\x01x", 185),
                ("/org/bluez/hci0/dev_BB", b"\xc0\x00\x01y", None),
            ],
        )
        with self.assertRaises(DBusError):
            request.WriteValue(b"x", {})
        with self.assertRaises(DBusError):
            request.WriteValue(
                b"x", {"device": Variant("o", "/d"), "offset": Variant("q", 3)}
            )
        with self.assertRaises(DBusError):
            request.ReadValue({})

    def test_response_notify_state_and_value(self) -> None:
        objects = build_gatt_objects(
            local_name="Bridge", info_payload=lambda: b"{}", on_write=lambda *_: None
        )
        response = objects["response"]
        self.assertFalse(response.Notifying)
        response.StartNotify()
        self.assertTrue(response.Notifying)
        response.push(b"\xc0\x00\x01z")
        self.assertEqual(bytes(response.Value), b"\xc0\x00\x01z")
        response.StopNotify()
        self.assertFalse(response.Notifying)
        with self.assertRaises(DBusError):
            objects["info"].StartNotify()


class RemoteConnectionTests(unittest.TestCase):
    def test_chunks_are_reassembled_per_device_with_mtu_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            protocol = RemoteProtocol(
                RemotePhoneStore(Path(tempdir) / "remote.json"),
                StubBridge(),
                name="Bridge",
                position_writes_enabled=True,
            )
            connections = RemoteConnections(protocol)
            payload = canonical_json({"t": "nonce", "cpk": "ab" * 32})
            chunks_a = chunk_message(payload, chunk_size=20)
            chunks_b = chunk_message(b"{}", chunk_size=20)
            self.assertIsNone(connections.receive_chunk("dev_a", chunks_a[0], 185))
            self.assertEqual(connections.receive_chunk("dev_b", chunks_b[0], None), b"{}")
            for chunk in chunks_a[1:-1]:
                self.assertIsNone(connections.receive_chunk("dev_a", chunk, None))
            self.assertEqual(connections.receive_chunk("dev_a", chunks_a[-1], None), payload)
            self.assertEqual(connections.chunk_size("dev_a"), 182)
            self.assertEqual(connections.chunk_size("dev_b"), 20)
            self.assertIsNone(connections.receive_chunk("dev_a", b"", None))
            self.assertIn("dev_a", protocol.sessions)
            connections.close("dev_a")
            self.assertNotIn("dev_a", protocol.sessions)
            self.assertEqual(connections.chunk_size("dev_a"), 20)


class FakeReply:
    def __init__(self, message_type: MessageType, body: list[Any] | None = None, error: str = "") -> None:
        self.message_type = message_type
        self.body = body or []
        self.error_name = error


class FakeBus:
    def __init__(self, *, fail_member: str | None = None) -> None:
        self.exports: dict[str, Any] = {}
        self.calls: list[tuple[str, str, str, list[Any]]] = []
        self.handlers: list[Any] = []
        self.disconnected = False
        self.fail_member = fail_member

    def export(self, path: str, interface: Any) -> None:
        self.exports[path] = interface

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    active_instances = 1

    async def call(self, message: Any) -> FakeReply:
        self.calls.append((message.path, message.interface, message.member, message.body))
        if message.member == self.fail_member:
            return FakeReply(MessageType.ERROR, ["boom"], "org.bluez.Error.Failed")
        if message.member == "Get":
            return FakeReply(MessageType.METHOD_RETURN, [Variant("y", self.active_instances)])
        return FakeReply(MessageType.METHOD_RETURN)

    def disconnect(self) -> None:
        self.disconnected = True


class FakeSignal:
    def __init__(self, path: str, body: list[Any]) -> None:
        self.message_type = MessageType.SIGNAL
        self.interface = "org.freedesktop.DBus.Properties"
        self.member = "PropertiesChanged"
        self.path = path
        self.body = body


class GattServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.store = RemotePhoneStore(Path(self._tempdir.name) / "remote.json")
        self.protocol = RemoteProtocol(
            self.store, StubBridge(), name="Bridge", position_writes_enabled=True
        )

    async def asyncTearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_start_registers_application_and_advertisement(self) -> None:
        bus = FakeBus()

        async def factory() -> FakeBus:
            return bus

        server = RemoteGattServer(
            self.protocol, adapter="hci0", local_name="Bridge", bus_factory=factory
        )
        await server.start()
        try:
            self.assertEqual(
                set(bus.exports),
                {
                    APPLICATION_PATH,
                    SERVICE_PATH,
                    SERVICE_PATH + "/char0",
                    SERVICE_PATH + "/char1",
                    SERVICE_PATH + "/char2",
                    ADVERTISEMENT_PATH,
                },
            )
            members = [(path, member) for path, _iface, member, _body in bus.calls]
            self.assertEqual(
                members,
                [
                    ("/org/freedesktop/DBus", "AddMatch"),
                    ("/org/bluez/hci0", "RegisterApplication"),
                    ("/org/bluez/hci0", "RegisterAdvertisement"),
                ],
            )
            self.assertEqual(bus.calls[1][3], [APPLICATION_PATH, {}])
            self.assertEqual(len(bus.handlers), 1)
        finally:
            await server.stop()
        members = [member for _path, _iface, member, _body in bus.calls]
        self.assertIn("UnregisterAdvertisement", members)
        self.assertIn("UnregisterApplication", members)
        self.assertTrue(bus.disconnected)

    async def test_registration_failure_is_raised(self) -> None:
        bus = FakeBus(fail_member="RegisterApplication")

        async def factory() -> FakeBus:
            return bus

        server = RemoteGattServer(self.protocol, local_name="Bridge", bus_factory=factory)
        with self.assertRaises(RemoteBleError):
            await server.start()
        await server.stop()
        self.assertTrue(bus.disconnected)

    async def test_write_round_trips_through_the_protocol_as_notifications(self) -> None:
        bus = FakeBus()

        async def factory() -> FakeBus:
            return bus

        server = RemoteGattServer(self.protocol, local_name="Bridge", bus_factory=factory)
        await server.start()
        try:
            response = bus.exports[SERVICE_PATH + "/char2"]
            pushed: list[bytes] = []
            response.emit_properties_changed = lambda changed, invalidated=[]: pushed.append(
                bytes(changed["Value"])
            )
            request = bus.exports[SERVICE_PATH + "/char1"]
            payload = canonical_json({"t": "nonce", "cpk": "ab" * 32})
            device = "/org/bluez/hci0/dev_AA_BB"
            for chunk in chunk_message(payload, chunk_size=20):
                request.WriteValue(
                    chunk, {"device": Variant("o", device), "mtu": Variant("q", 185)}
                )
            await asyncio.sleep(0.05)
            assembler = MessageAssembler()
            messages = [m for m in (assembler.add(chunk) for chunk in pushed) if m]
            self.assertEqual(len(messages), 1)
            reply = json.loads(messages[0])
            self.assertEqual(reply["t"], "nonce")
            self.assertEqual(reply["bridge_id"], self.store.bridge_id)
            self.assertTrue(all(len(chunk) <= 182 for chunk in pushed))
            self.assertEqual(len(pushed), 1)

            bus.handlers[0](
                FakeSignal(device, ["org.bluez.Device1", {"Connected": Variant("b", False)}, []])
            )
            self.assertNotIn(device, self.protocol.sessions)
            bus.handlers[0](FakeSignal("/org/bluez/hci0", ["org.bluez.Adapter1", {}, []]))
            with patch("tilt_local_bridge.tilt_remote_ble._READVERTISE_DELAY_SECONDS", 0):
                bus.handlers[0](
                    FakeSignal(device, ["org.bluez.Device1", {"Connected": Variant("b", False)}, []])
                )
                await asyncio.sleep(0.05)
            members = [member for _p, _i, member, _b in bus.calls]
            self.assertEqual(members[-2:], ["UnregisterAdvertisement", "RegisterAdvertisement"])
        finally:
            await server.stop()

    async def test_idle_check_re_advertises_when_bluez_reports_none(self) -> None:
        bus = FakeBus()
        bus.active_instances = 0

        async def factory() -> FakeBus:
            return bus

        server = RemoteGattServer(self.protocol, local_name="Bridge", bus_factory=factory)
        await server.start()
        try:
            with (
                patch("tilt_local_bridge.tilt_remote_ble._TICK_SECONDS", 0.001),
                patch("tilt_local_bridge.tilt_remote_ble._ADVERTISING_CHECK_TICKS", 1),
                patch("tilt_local_bridge.tilt_remote_ble._READVERTISE_DELAY_SECONDS", 0),
            ):
                await asyncio.sleep(0.1)
            members = [member for _p, _i, member, _b in bus.calls]
            self.assertIn("Get", members)
            self.assertGreaterEqual(members.count("RegisterAdvertisement"), 2)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
