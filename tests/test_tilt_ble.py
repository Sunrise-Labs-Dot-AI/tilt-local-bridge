"""Offline transport tests using a deterministic in-memory Tilt peripheral."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, call, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tilt_local_bridge.tilt_ble import (
    AmbiguousPositionWrite,
    PositionVerificationPending,
    TiltShadeClient,
)
from tilt_local_bridge.tilt_bridge_config import (
    BridgeAccessConfig,
    MqttConfig,
    ShadeConfig,
    TiltBridgeConfig,
    authorize_shade_access,
)
from tilt_local_bridge.tilt_protocol import (
    AuthenticationError,
    BleMessageAssembler,
    CryptoCommand,
    ShadeCommand,
    TILT_COMMAND_UUID,
    TILT_RESPONSE_UUID,
    chunk_for_ble,
    crc16,
    make_ble_ack,
    next_sequence,
    pairing_key_proof,
    parse_ble_chunk,
)


KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
NONCE = bytes(range(12))


def _config(*, writes: bool) -> TiltBridgeConfig:
    return TiltBridgeConfig(
        version=1,
        access=BridgeAccessConfig(allow_reads=True, allow_position_writes=writes),
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
    )


def _crypto_response(command: int, payload: bytes) -> bytes:
    header = b"\x80\x00"
    body = bytes([command]) + payload
    return header + body + crc16(header + body).to_bytes(2, "little")


def _decrypt_request(frame: bytes) -> tuple[int, int, ShadeCommand, bytes]:
    header = frame[:2]
    counter = int.from_bytes(header, "big")
    decryptor = Cipher(
        algorithms.AES(KEY[:16]), modes.CTR(NONCE + header + b"\x00\x00")
    ).decryptor()
    plaintext = decryptor.update(frame[2:]) + decryptor.finalize()
    presentation = plaintext[:-2]
    self_checksum = int.from_bytes(plaintext[-2:], "little")
    if crc16(header + presentation) != self_checksum:
        raise AssertionError("fake peripheral received invalid application checksum")
    return counter, presentation[0] & 0x0F, ShadeCommand(presentation[1]), presentation[2:]


def _application_response(
    command: ShadeCommand,
    payload: bytes,
    *,
    counter: int,
    message_id: int,
) -> bytes:
    header = counter.to_bytes(2, "big")
    presentation = bytes([0x40 | message_id, command]) + payload
    plaintext = presentation + crc16(header + presentation).to_bytes(2, "little")
    receive_counter = bytes([header[0] | 0x80, header[1]])
    encryptor = Cipher(
        algorithms.AES(KEY[:16]), modes.CTR(NONCE + receive_counter + b"\x00\x00")
    ).encryptor()
    return header + encryptor.update(plaintext) + encryptor.finalize()


class FakeTiltPeripheral:
    def __init__(
        self,
        address: str,
        *,
        timeout: float,
        pair: bool = False,
        position: int = 25,
        drop_set_response: bool = False,
        apply_dropped_set: bool = True,
        defer_set_position: bool = False,
        drop_status_response_after: int | None = None,
        fail_disconnect: bool = False,
    ) -> None:
        self.address = address
        self.timeout = timeout
        self.pair = pair
        self.position = position
        self.drop_set_response = drop_set_response
        self.apply_dropped_set = apply_dropped_set
        self.defer_set_position = defer_set_position
        self.drop_status_response_after = drop_status_response_after
        self.fail_disconnect = fail_disconnect
        self.is_connected = False
        self.notification_callback: Any = None
        self.client_assembler = BleMessageAssembler()
        self.server_sequence = 1
        self.application_commands: list[ShadeCommand] = []
        self.writes: list[bytes] = []
        self.status_requests = 0

    async def __aenter__(self):
        self.is_connected = True
        return self

    async def __aexit__(self, _type, _value, _traceback) -> None:
        self.is_connected = False

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        if self.fail_disconnect:
            raise RuntimeError("injected disconnect failure")

    async def start_notify(self, uuid: str, callback: Any) -> None:
        if uuid != TILT_RESPONSE_UUID:
            raise AssertionError("unexpected notification UUID")
        self.notification_callback = callback

    async def stop_notify(self, uuid: str) -> None:
        if uuid != TILT_RESPONSE_UUID:
            raise AssertionError("unexpected notification UUID")

    async def write_gatt_char(self, uuid: str, data: bytes) -> None:
        if uuid != TILT_COMMAND_UUID:
            raise AssertionError("unexpected command UUID")
        wire_data = bytes(data)
        self.writes.append(wire_data)
        chunk = parse_ble_chunk(wire_data)
        if chunk.for_bluetooth_layer:
            return
        self._notify(make_ble_ack(chunk.sequence))
        _acknowledged, frame = self.client_assembler.add(wire_data)
        if frame is not None:
            response = self._handle_frame(frame)
            if response is not None:
                self._send_response(response)

    def _notify(self, data: bytes) -> None:
        if self.notification_callback is None:
            raise AssertionError("notifications were not started")
        self.notification_callback(1, bytearray(data))

    def _send_response(self, frame: bytes) -> None:
        chunks = chunk_for_ble(frame, start_sequence=self.server_sequence)
        for chunk in chunks:
            self._notify(chunk)
        self.server_sequence = next_sequence(chunks[-1][1] & 0x3F)

    def _handle_frame(self, frame: bytes) -> bytes | None:
        if frame[:2] == b"\x80\x00":
            command = CryptoCommand(frame[2])
            if command is CryptoCommand.REQUEST_PROTOCOL_VERSIONS:
                return _crypto_response(command, b"\x02\x01\x02")
            if command is CryptoCommand.SELECT_PROTOCOL_VERSION:
                return _crypto_response(command, frame[3:4])
            if command is CryptoCommand.REQUEST_NONCE:
                return _crypto_response(command, NONCE + pairing_key_proof(KEY))
            raise AssertionError("unexpected crypto command")

        counter, message_id, command, payload = _decrypt_request(frame)
        self.application_commands.append(command)
        if command is ShadeCommand.GET_STATUS:
            self.status_requests += 1
            if (
                self.drop_status_response_after is not None
                and self.status_requests > self.drop_status_response_after
            ):
                return None
            status = (
                (self.position * 10).to_bytes(2, "little")
                + bytes([87, 0, 1])
            )
            return _application_response(
                command, status, counter=counter, message_id=message_id
            )
        if command is ShadeCommand.SET_POSITION:
            requested = int.from_bytes(payload[:2], "little") // 10
            if (
                not self.defer_set_position
                and (not self.drop_set_response or self.apply_dropped_set)
            ):
                self.position = requested
            if self.drop_set_response:
                return None
            return _application_response(
                ShadeCommand.ACK,
                bytes([ShadeCommand.SET_POSITION]),
                counter=counter,
                message_id=message_id,
            )
        raise AssertionError(f"unexpected application command {command}")


class TiltBleReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_status_uses_fixed_address_and_authenticates_key(self) -> None:
        config = _config(writes=False)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=False
        )
        created: list[FakeTiltPeripheral] = []

        def factory(address: str, *, timeout: float, pair: bool) -> FakeTiltPeripheral:
            peripheral = FakeTiltPeripheral(address, timeout=timeout, pair=pair)
            created.append(peripheral)
            return peripheral

        client = TiltShadeClient(config.shades[0], KEY, permit, client_factory=factory)
        with patch("tilt_local_bridge.tilt_ble.asyncio.sleep", new_callable=AsyncMock) as sleep:
            status = await client.read_status()

        self.assertEqual(status.position_percent, 25)
        self.assertEqual(status.battery_percent, 87)
        self.assertEqual(created[0].address, config.shades[0].mac)
        self.assertFalse(created[0].pair)
        self.assertEqual(created[0].application_commands, [ShadeCommand.GET_STATUS])
        self.assertEqual(sleep.await_args_list, [call(0.1), call(0.1), call(0.2), call(0.1)])

    async def test_wrong_key_fails_before_any_application_command(self) -> None:
        config = _config(writes=False)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=False
        )
        peripheral = FakeTiltPeripheral(config.shades[0].mac, timeout=1)
        client = TiltShadeClient(
            config.shades[0],
            OTHER_KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
            ack_timeout_seconds=0.1,
            response_timeout_seconds=0.1,
        )
        with self.assertRaises(AuthenticationError):
            await client.read_status()
        self.assertEqual(peripheral.application_commands, [])

    async def test_disconnect_failure_does_not_mask_completed_status(self) -> None:
        config = _config(writes=False)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=False
        )
        peripheral = FakeTiltPeripheral(
            config.shades[0].mac, timeout=1, fail_disconnect=True
        )
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
        )

        status = await client.read_status()

        self.assertEqual(status.position_percent, 25)


class TiltBleWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_permit_rejects_position_before_connecting(self) -> None:
        config = _config(writes=False)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=False
        )
        connected = False

        def factory(*_args, **_kwargs):
            nonlocal connected
            connected = True
            raise AssertionError("write gate should fail before connection")

        client = TiltShadeClient(config.shades[0], KEY, permit, client_factory=factory)
        with self.assertRaises(Exception) as raised:
            await client.set_position_and_read_status(80, settle_seconds=0)
        self.assertIn("not permitted", str(raised.exception))
        self.assertFalse(connected)

    async def test_position_is_written_once_and_verified_by_readback(self) -> None:
        config = _config(writes=True)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=True
        )
        peripheral = FakeTiltPeripheral(config.shades[0].mac, timeout=1)
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
        )
        status, moved = await client.set_position_and_read_status(80, settle_seconds=0)
        self.assertTrue(moved)
        self.assertEqual(status.position_percent, 80)
        self.assertEqual(
            peripheral.application_commands,
            [ShadeCommand.GET_STATUS, ShadeCommand.SET_POSITION, ShadeCommand.GET_STATUS],
        )

    async def test_ambiguous_write_reads_back_without_resending(self) -> None:
        config = _config(writes=True)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=True
        )
        peripheral = FakeTiltPeripheral(
            config.shades[0].mac,
            timeout=1,
            drop_set_response=True,
            apply_dropped_set=True,
        )
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
            response_timeout_seconds=0.01,
        )
        status, moved = await client.set_position_and_read_status(70, settle_seconds=0)
        self.assertTrue(moved)
        self.assertEqual(status.position_percent, 70)
        self.assertEqual(peripheral.application_commands.count(ShadeCommand.SET_POSITION), 1)

    async def test_timeout_with_status_returns_pending_verification(self) -> None:
        config = _config(writes=True)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=True
        )
        peripheral = FakeTiltPeripheral(
            config.shades[0].mac,
            timeout=1,
            drop_set_response=True,
            apply_dropped_set=False,
        )
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
            response_timeout_seconds=0.01,
        )
        with self.assertRaises(PositionVerificationPending) as raised:
            await client.set_position_and_read_status(70, settle_seconds=0)
        self.assertEqual(raised.exception.status.position_percent, 25)
        self.assertEqual(peripheral.application_commands.count(ShadeCommand.SET_POSITION), 1)

    async def test_acknowledged_in_progress_write_returns_pending_status(self) -> None:
        config = _config(writes=True)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=True
        )
        peripheral = FakeTiltPeripheral(
            config.shades[0].mac,
            timeout=1,
            defer_set_position=True,
        )
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
        )

        with self.assertRaises(PositionVerificationPending) as raised:
            await client.set_position_and_read_status(70, settle_seconds=0)

        self.assertEqual(raised.exception.status.position_percent, 25)
        self.assertEqual(peripheral.application_commands.count(ShadeCommand.SET_POSITION), 1)

    async def test_write_timeout_and_failed_readback_remains_ambiguous(self) -> None:
        config = _config(writes=True)
        permit = authorize_shade_access(
            config, request_reads=True, request_position_writes=True
        )
        peripheral = FakeTiltPeripheral(
            config.shades[0].mac,
            timeout=1,
            drop_set_response=True,
            drop_status_response_after=1,
        )
        client = TiltShadeClient(
            config.shades[0],
            KEY,
            permit,
            client_factory=lambda *_args, **_kwargs: peripheral,
            response_timeout_seconds=0.01,
        )

        with self.assertRaises(AmbiguousPositionWrite) as raised:
            await client.set_position_and_read_status(70, settle_seconds=0)

        self.assertNotIsInstance(raised.exception, PositionVerificationPending)
        self.assertEqual(peripheral.application_commands.count(ShadeCommand.SET_POSITION), 1)


if __name__ == "__main__":
    unittest.main()
