"""Strict codec for the Tilt roller-shade BLE protocol.

This module is deliberately transport-free. It cannot scan for, connect to, or
write to a Bluetooth device. The live transport uses these helpers through a
small positive allowlist so pairing, calibration, reset, rename, identify, and
firmware commands are never representable at the runtime boundary.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import IntEnum


TILT_SERVICE_UUID = "05960001-d71e-4845-ab02-bf27bb160401"
TILT_COMMAND_UUID = "05960002-d71e-4845-ab02-bf27bb160401"
TILT_RESPONSE_UUID = "05960003-d71e-4845-ab02-bf27bb160401"

_CRYPTO_LAYER_FLAG = 0x8000
_BLUETOOTH_COMMAND_FLAG = 0x80
_END_OF_MESSAGE_FLAG = 0x40
_PRESENTATION_RESPONSE_FLAG = 0x40
_PRESENTATION_RESERVED_FLAGS = 0x30
_MAX_BLE_PAYLOAD = 18
_MAX_SEQUENCE = 63
_MAX_COUNTER = 0x7FFF
_PAIRING_KEY_PROOF_MESSAGE = b"Signed Trogdor string, by Ryjan."


class TiltProtocolError(RuntimeError):
    """Base error for malformed or disallowed Tilt protocol data."""


class UnsafeCommandError(TiltProtocolError):
    """Raised when a caller tries to construct a non-allowlisted command."""


class AuthenticationError(TiltProtocolError):
    """Raised when encrypted data or pairing-key proof cannot be verified."""


class CryptoCommand(IntEnum):
    ACK = 0x01
    REQUEST_PROTOCOL_VERSIONS = 0x02
    SELECT_PROTOCOL_VERSION = 0x03
    REQUEST_NONCE = 0x0A


class ShadeCommand(IntEnum):
    ACK = 0x01
    GET_VERSION = 0x04
    GET_NAME = 0x05
    GET_STATUS = 0x10
    GET_POSITION = 0x12
    SET_POSITION = 0x13
    GET_BATTERY = 0x16


READ_COMMANDS = frozenset(
    {
        ShadeCommand.GET_VERSION,
        ShadeCommand.GET_NAME,
        ShadeCommand.GET_STATUS,
        ShadeCommand.GET_POSITION,
        ShadeCommand.GET_BATTERY,
    }
)
WRITE_COMMANDS = frozenset({ShadeCommand.SET_POSITION})
RUNTIME_COMMANDS = READ_COMMANDS | WRITE_COMMANDS
SUPPORTED_CRYPTO_PROTOCOLS = (2, 1)


@dataclass(frozen=True)
class CryptoResponse:
    command: CryptoCommand
    payload: bytes


@dataclass(frozen=True)
class NonceResponse:
    nonce: bytes
    key_proof: bytes


@dataclass(frozen=True)
class ApplicationResponse:
    command: ShadeCommand
    message_id: int
    counter: int
    payload: bytes


@dataclass(frozen=True)
class ShadeStatus:
    raw_position: int
    battery_percent: int
    charge_status: int
    calibrated: bool

    @property
    def position_percent(self) -> int:
        return raw_position_to_percent(self.raw_position)


@dataclass(frozen=True)
class BleChunk:
    sequence: int
    end_of_message: bool
    for_bluetooth_layer: bool
    payload: bytes


def crc16(data: bytes) -> int:
    """Return the CCITT-FALSE checksum used by the Tilt protocol."""

    value = 0xFFFF
    for byte in data:
        value = (((value << 8) | (value >> 8)) & 0xFFFF) ^ byte
        value ^= (value & 0xFF) >> 4
        value ^= (value << 12) & 0xFFFF
        value ^= ((value & 0xFF) << 5) & 0xFFFF
    return value & 0xFFFF


def _require_key_and_nonce(key: bytes, nonce: bytes) -> None:
    if len(key) != 32:
        raise TiltProtocolError("Tilt pairing keys must be exactly 32 bytes.")
    if len(nonce) != 12:
        raise TiltProtocolError("Tilt nonces must be exactly 12 bytes.")


def pairing_key_proof(key: bytes) -> bytes:
    if len(key) != 32:
        raise TiltProtocolError("Tilt pairing keys must be exactly 32 bytes.")
    return hmac.new(key, _PAIRING_KEY_PROOF_MESSAGE, hashlib.sha256).digest()


def pairing_key_matches_proof(key: bytes, proof: bytes) -> bool:
    if len(proof) != 32:
        return False
    return hmac.compare_digest(pairing_key_proof(key), proof)


def _append_checksum(header: bytes, payload: bytes) -> bytes:
    checksum = crc16(header + payload)
    return header + payload + checksum.to_bytes(2, "little")


def _verify_checksum(header: bytes, payload_with_checksum: bytes) -> bytes:
    if len(payload_with_checksum) < 3:
        raise TiltProtocolError("Tilt message is too short for payload and checksum.")
    payload = payload_with_checksum[:-2]
    received = int.from_bytes(payload_with_checksum[-2:], "little")
    if not hmac.compare_digest(
        received.to_bytes(2, "little"), crc16(header + payload).to_bytes(2, "little")
    ):
        raise AuthenticationError("Tilt message checksum verification failed.")
    return payload


def _encode_crypto_request(command: CryptoCommand, payload: bytes = b"") -> bytes:
    if command is CryptoCommand.REQUEST_PROTOCOL_VERSIONS:
        if payload:
            raise UnsafeCommandError("Protocol-version request does not accept a payload.")
    elif command is CryptoCommand.SELECT_PROTOCOL_VERSION:
        if len(payload) != 1 or payload[0] not in SUPPORTED_CRYPTO_PROTOCOLS:
            raise UnsafeCommandError("Only supported AES-CTR protocol versions may be selected.")
    elif command is CryptoCommand.REQUEST_NONCE:
        if payload:
            raise UnsafeCommandError("Nonce request does not accept a payload.")
    else:
        raise UnsafeCommandError("Crypto command is not in the runtime allowlist.")
    header = _CRYPTO_LAYER_FLAG.to_bytes(2, "big")
    return _append_checksum(header, bytes([command]) + payload)


def encode_protocol_versions_request() -> bytes:
    return _encode_crypto_request(CryptoCommand.REQUEST_PROTOCOL_VERSIONS)


def encode_protocol_selection(version: int) -> bytes:
    try:
        payload = bytes([version])
    except ValueError as exc:
        raise UnsafeCommandError("Protocol version must fit in one byte.") from exc
    return _encode_crypto_request(CryptoCommand.SELECT_PROTOCOL_VERSION, payload)


def encode_nonce_request() -> bytes:
    return _encode_crypto_request(CryptoCommand.REQUEST_NONCE)


def parse_crypto_response(frame: bytes) -> CryptoResponse:
    if len(frame) < 5:
        raise TiltProtocolError("Crypto response is too short.")
    header = frame[:2]
    header_value = int.from_bytes(header, "big")
    if header_value != _CRYPTO_LAYER_FLAG:
        raise TiltProtocolError("Expected an unencrypted crypto-layer response.")
    payload = _verify_checksum(header, frame[2:])
    try:
        command = CryptoCommand(payload[0])
    except (IndexError, ValueError) as exc:
        raise TiltProtocolError("Unknown crypto response command.") from exc
    if command not in {
        CryptoCommand.ACK,
        CryptoCommand.REQUEST_PROTOCOL_VERSIONS,
        CryptoCommand.SELECT_PROTOCOL_VERSION,
        CryptoCommand.REQUEST_NONCE,
    }:
        raise UnsafeCommandError("Crypto response is outside the runtime allowlist.")
    return CryptoResponse(command=command, payload=payload[1:])


def parse_protocol_versions(response: CryptoResponse) -> tuple[int, ...]:
    if response.command is not CryptoCommand.REQUEST_PROTOCOL_VERSIONS:
        raise TiltProtocolError("Expected a protocol-version response.")
    if not response.payload:
        raise TiltProtocolError("Protocol-version response is missing its count.")
    count = response.payload[0]
    versions = tuple(response.payload[1 : 1 + count])
    if len(versions) != count:
        raise TiltProtocolError("Protocol-version response length does not match its count.")
    return versions


def parse_nonce_response(response: CryptoResponse) -> NonceResponse:
    if response.command is not CryptoCommand.REQUEST_NONCE:
        raise TiltProtocolError("Expected a nonce response.")
    if len(response.payload) != 44:
        raise TiltProtocolError("Nonce response must contain 12 nonce and 32 proof bytes.")
    return NonceResponse(nonce=response.payload[:12], key_proof=response.payload[12:])


def _aes_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - exercised on deployment host
        raise TiltProtocolError(
            "The Tilt bridge requires the cryptography package for AES-CTR."
        ) from exc
    encryptor = Cipher(algorithms.AES(key[:16]), modes.CTR(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _counter_header(counter: int) -> bytes:
    if not 1 <= counter <= _MAX_COUNTER:
        raise TiltProtocolError(f"Encryption counter must be between 1 and {_MAX_COUNTER}.")
    return counter.to_bytes(2, "big")


def _message_id_byte(message_id: int, *, response: bool) -> int:
    if not 0 <= message_id <= 0x0F:
        raise TiltProtocolError("Message id must be between 0 and 15.")
    return message_id | (_PRESENTATION_RESPONSE_FLAG if response else 0)


def _encode_application_request(
    command: ShadeCommand,
    payload: bytes,
    *,
    key: bytes,
    nonce: bytes,
    counter: int,
    message_id: int,
) -> bytes:
    if command not in RUNTIME_COMMANDS:
        raise UnsafeCommandError("Shade command is not in the runtime allowlist.")
    _require_key_and_nonce(key, nonce)
    header = _counter_header(counter)
    presentation = bytes([_message_id_byte(message_id, response=False), command]) + payload
    plaintext = presentation + crc16(header + presentation).to_bytes(2, "little")
    iv = nonce + header + b"\x00\x00"
    return header + _aes_ctr(plaintext, key, iv)


def encode_read_request(
    command: ShadeCommand,
    *,
    key: bytes,
    nonce: bytes,
    counter: int = 1,
    message_id: int = 1,
) -> bytes:
    if command not in READ_COMMANDS:
        raise UnsafeCommandError("Only allowlisted read commands may use this encoder.")
    return _encode_application_request(
        command, b"", key=key, nonce=nonce, counter=counter, message_id=message_id
    )


def encode_position_request(
    position_percent: int,
    *,
    key: bytes,
    nonce: bytes,
    counter: int = 1,
    message_id: int = 1,
    speed: int = 100,
) -> bytes:
    if isinstance(position_percent, bool) or not isinstance(position_percent, int):
        raise TiltProtocolError("Position must be an integer percent.")
    if not 0 <= position_percent <= 100:
        raise TiltProtocolError("Position must be between 0 and 100 percent.")
    if isinstance(speed, bool) or not isinstance(speed, int) or not 1 <= speed <= 100:
        raise TiltProtocolError("Speed must be an integer between 1 and 100.")
    raw_position = position_percent * 10
    payload = raw_position.to_bytes(2, "little") + bytes([speed])
    return _encode_application_request(
        ShadeCommand.SET_POSITION,
        payload,
        key=key,
        nonce=nonce,
        counter=counter,
        message_id=message_id,
    )


def decode_application_response(
    frame: bytes,
    *,
    key: bytes,
    nonce: bytes,
    expected_command: ShadeCommand,
    expected_message_id: int,
    expected_counter: int | None = None,
) -> ApplicationResponse:
    _require_key_and_nonce(key, nonce)
    if expected_command not in RUNTIME_COMMANDS:
        raise UnsafeCommandError("Expected command is outside the runtime allowlist.")
    if len(frame) < 6:
        raise TiltProtocolError("Encrypted application response is too short.")
    header = frame[:2]
    header_value = int.from_bytes(header, "big")
    if header_value & _CRYPTO_LAYER_FLAG:
        raise TiltProtocolError("Expected an encrypted application-layer response.")
    counter = header_value & _MAX_COUNTER
    _counter_header(counter)
    if expected_counter is not None and counter != expected_counter:
        raise TiltProtocolError("Application response counter does not match request.")
    receive_counter = bytes([header[0] | 0x80, header[1]])
    plaintext = _aes_ctr(frame[2:], key, nonce + receive_counter + b"\x00\x00")
    presentation = _verify_checksum(header, plaintext)
    if len(presentation) < 2:
        raise TiltProtocolError("Application response has no command.")
    flags = presentation[0]
    if not flags & _PRESENTATION_RESPONSE_FLAG:
        raise TiltProtocolError("Application message is not marked as a response.")
    if flags & _PRESENTATION_RESERVED_FLAGS:
        raise TiltProtocolError("Application response has reserved presentation flags set.")
    message_id = flags & 0x0F
    if message_id != expected_message_id:
        raise TiltProtocolError("Application response message id does not match request.")
    try:
        command = ShadeCommand(presentation[1])
    except ValueError as exc:
        raise TiltProtocolError("Unknown application response command.") from exc
    payload = presentation[2:]
    if command is ShadeCommand.ACK:
        if not payload or payload[0] != expected_command:
            raise TiltProtocolError("Application ACK does not match the requested command.")
    elif command is not expected_command:
        raise TiltProtocolError("Application response command does not match request.")
    return ApplicationResponse(
        command=command,
        message_id=message_id,
        counter=counter,
        payload=payload,
    )


def parse_name(response: ApplicationResponse) -> str:
    _require_response_command(response, ShadeCommand.GET_NAME)
    if not response.payload:
        raise TiltProtocolError("Name response is missing its length.")
    length = response.payload[0]
    name_bytes = response.payload[1 : 1 + length]
    if len(name_bytes) != length or len(response.payload) != length + 1:
        raise TiltProtocolError("Name response length is invalid.")
    try:
        return name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TiltProtocolError("Name response is not valid UTF-8.") from exc


def parse_raw_position(response: ApplicationResponse) -> int:
    if response.command not in {ShadeCommand.GET_POSITION, ShadeCommand.GET_STATUS}:
        raise TiltProtocolError("Response does not contain a shade position.")
    if len(response.payload) < 2:
        raise TiltProtocolError("Position response is too short.")
    raw_position = int.from_bytes(response.payload[:2], "little")
    if not 0 <= raw_position <= 1000:
        raise TiltProtocolError("Shade position is outside the calibrated range.")
    return raw_position


def parse_status(response: ApplicationResponse) -> ShadeStatus:
    _require_response_command(response, ShadeCommand.GET_STATUS)
    if len(response.payload) != 5:
        raise TiltProtocolError("Status response must be exactly five bytes.")
    raw_position = parse_raw_position(response)
    battery = response.payload[2]
    if battery > 100:
        raise TiltProtocolError("Battery percentage is outside 0 to 100.")
    calibrated = response.payload[4]
    if calibrated not in (0, 1):
        raise TiltProtocolError("Calibrated flag is not boolean.")
    return ShadeStatus(
        raw_position=raw_position,
        battery_percent=battery,
        charge_status=response.payload[3],
        calibrated=bool(calibrated),
    )


def parse_battery(response: ApplicationResponse) -> tuple[int, int]:
    _require_response_command(response, ShadeCommand.GET_BATTERY)
    if len(response.payload) != 2 or response.payload[0] > 100:
        raise TiltProtocolError("Battery response is invalid.")
    return response.payload[0], response.payload[1]


def _require_response_command(response: ApplicationResponse, command: ShadeCommand) -> None:
    if response.command is not command:
        raise TiltProtocolError(f"Expected {command.name} response.")


def raw_position_to_percent(raw_position: int) -> int:
    if isinstance(raw_position, bool) or not isinstance(raw_position, int):
        raise TiltProtocolError("Raw position must be an integer.")
    if not 0 <= raw_position <= 1000:
        raise TiltProtocolError("Raw position must be between 0 and 1000.")
    return round(raw_position / 10)


def chunk_for_ble(frame: bytes, *, start_sequence: int = 1) -> tuple[bytes, ...]:
    if not frame:
        raise TiltProtocolError("Cannot chunk an empty Tilt message.")
    if not 1 <= start_sequence <= _MAX_SEQUENCE:
        raise TiltProtocolError("BLE sequence must be between 1 and 63.")
    chunks: list[bytes] = []
    sequence = start_sequence
    for offset in range(0, len(frame), _MAX_BLE_PAYLOAD):
        payload = frame[offset : offset + _MAX_BLE_PAYLOAD]
        end = offset + _MAX_BLE_PAYLOAD >= len(frame)
        flags = sequence | (_END_OF_MESSAGE_FLAG if end else 0)
        chunks.append(bytes([0, flags]) + payload)
        sequence = next_sequence(sequence)
    return tuple(chunks)


def parse_ble_chunk(data: bytes) -> BleChunk:
    if len(data) < 2 or data[0] != 0:
        raise TiltProtocolError("BLE chunk has an invalid header.")
    if len(data) > _MAX_BLE_PAYLOAD + 2:
        raise TiltProtocolError("BLE chunk exceeds the protocol payload limit.")
    flags = data[1]
    sequence = flags & 0x3F
    for_bluetooth_layer = bool(flags & _BLUETOOTH_COMMAND_FLAG)
    if sequence == 0 and not for_bluetooth_layer:
        raise TiltProtocolError("BLE chunk sequence zero is invalid.")
    return BleChunk(
        sequence=sequence,
        end_of_message=bool(flags & _END_OF_MESSAGE_FLAG),
        for_bluetooth_layer=for_bluetooth_layer,
        payload=data[2:],
    )


def make_ble_ack(sequence: int) -> bytes:
    if not 1 <= sequence <= _MAX_SEQUENCE:
        raise TiltProtocolError("Cannot ACK an invalid BLE sequence.")
    return bytes([0, _BLUETOOTH_COMMAND_FLAG | _END_OF_MESSAGE_FLAG, 0x01, sequence])


def parse_ble_ack(data: bytes) -> int:
    chunk = parse_ble_chunk(data)
    if not chunk.for_bluetooth_layer or not chunk.end_of_message:
        raise TiltProtocolError("Expected a Bluetooth-layer ACK.")
    if len(chunk.payload) != 2 or chunk.payload[0] != 0x01:
        raise TiltProtocolError("Bluetooth-layer ACK payload is invalid.")
    if not 1 <= chunk.payload[1] <= _MAX_SEQUENCE:
        raise TiltProtocolError("Bluetooth-layer ACK sequence is invalid.")
    return chunk.payload[1]


def next_sequence(sequence: int) -> int:
    if not 1 <= sequence <= _MAX_SEQUENCE:
        raise TiltProtocolError("BLE sequence must be between 1 and 63.")
    return 1 if sequence == _MAX_SEQUENCE else sequence + 1


class BleMessageAssembler:
    """Reassemble one direction of contiguous Tilt BLE message chunks."""

    def __init__(self, *, expected_sequence: int = 1) -> None:
        if not 1 <= expected_sequence <= _MAX_SEQUENCE:
            raise TiltProtocolError("Expected BLE sequence must be between 1 and 63.")
        self._expected_sequence = expected_sequence
        self._buffer = bytearray()

    @property
    def expected_sequence(self) -> int:
        return self._expected_sequence

    def add(self, data: bytes) -> tuple[int, bytes | None]:
        chunk = parse_ble_chunk(data)
        if chunk.for_bluetooth_layer:
            raise TiltProtocolError("Bluetooth command cannot be added to message assembler.")
        if chunk.sequence != self._expected_sequence:
            self._buffer.clear()
            raise TiltProtocolError("Received a non-contiguous BLE sequence.")
        acknowledged = chunk.sequence
        self._expected_sequence = next_sequence(chunk.sequence)
        self._buffer.extend(chunk.payload)
        if not chunk.end_of_message:
            return acknowledged, None
        message = bytes(self._buffer)
        self._buffer.clear()
        return acknowledged, message
