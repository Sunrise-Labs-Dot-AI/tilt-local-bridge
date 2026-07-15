"""One-shot, operator-gated pairing for a Tilt roller shade.

This module is intentionally separate from the production bridge transport.
It has no movement, calibration, reset, rename, firmware, or raw-command API.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol

from .tilt_protocol import (
    BleMessageAssembler,
    SUPPORTED_CRYPTO_PROTOCOLS,
    TILT_COMMAND_UUID,
    TILT_RESPONSE_UUID,
    TILT_SERVICE_UUID,
    TiltProtocolError,
    chunk_for_ble,
    crc16,
    make_ble_ack,
    next_sequence,
    parse_ble_ack,
    parse_ble_chunk,
)


_AUTH0_URL = "https://mysmartblinds.auth0.com/oauth/token"
_AUTH0_CLIENT_ID = "Owjr4yOJ2HauKaQhBpICgmfTf7naJsRd"
_AUTH0_AUDIENCE = "Tilt Settings Storage API"
_AUTH0_CONNECTION = "Username-Password-Authentication"
_AUTH0_SCOPE = "openid profile email offline_access"
_AUTH0_GRANT_TYPE = "http://auth0.com/oauth/grant-type/password-realm"
_TILT_API_URL = "https://api.tiltsmarthome.com/v2/"
_PAIRING_INDICATOR_UUID = "00001add-0000-1000-8000-00805f9b34fb"
_PAIRING_REQUEST_MESSAGE = b"Let's pair - Matthew was here!!!"
_ROLLER_SHADE_DEVICE_TYPE = b"\x04\x01"
_PROTOCOL_THROTTLE_SECONDS = 0.1
_PAIRING_COLLISION_WINDOW_SECONDS = 0.25
_MAX_HTTP_RESPONSE_BYTES = 64 * 1024
_MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


class TiltPairingError(RuntimeError):
    """Base error for the bounded one-shot pairing flow."""


class TiltPairingTimeout(TiltPairingError):
    """Raised when a required BLE acknowledgement or response does not arrive."""


class TiltCloudError(TiltPairingError):
    """Raised when Tilt's authenticated key service rejects a pairing step."""


class PairingNack(TiltPairingError):
    """A structured negative acknowledgement from the shade."""

    def __init__(self, command: int, reason: int) -> None:
        super().__init__(f"Shade rejected pairing command {command} with reason {reason}.")
        self.command = command
        self.reason = reason


class PairingKeyAmbiguousError(TiltPairingError):
    """The shade may have accepted a key that remains in a protected staging file."""

    def __init__(self, staged_path: Path, *, target_mac: str | None = None) -> None:
        target = f" for {target_mac}" if target_mac else ""
        super().__init__(
            f"Pairing-key installation{target} had an ambiguous result; preserve the protected "
            f"staging file at {staged_path}."
        )
        self.staged_path = staged_path
        self.target_mac = target_mac


class PairingCryptoCommand(IntEnum):
    NACK = 0x00
    ACK = 0x01
    REQUEST_PROTOCOL_VERSIONS = 0x02
    SELECT_PROTOCOL_VERSION = 0x03
    SIGN_PAIRING_TOKEN = 0x06
    SET_PAIRING_KEY = 0x07
    REQUEST_PAIRING_AUTH_TOKEN = 0x08
    GET_ECDH_KEY = 0x09


class NackReason(IntEnum):
    INVALID_COMMAND = 0x01
    INVALID_LENGTH = 0x02
    INVALID_CHECKSUM = 0x03
    NOT_READY = 0x04
    NO_MATCH = 0x05
    BAD_STATE = 0x06
    LIMIT_REACHED = 0x07
    CONFLICT = 0x08


@dataclass(frozen=True)
class PairingCryptoResponse:
    command: PairingCryptoCommand
    payload: bytes


@dataclass(frozen=True)
class ServerPairingKeys:
    plaintext_key: bytes
    nonce: bytes
    encrypted_key: bytes


@dataclass(frozen=True)
class PairingResult:
    mac: str
    key_path: Path


class PairingCloud(Protocol):
    def register_device(self, mac: str, ecdh_key: bytes) -> None: ...

    def request_challenge(
        self, mac: str, temporary_key: bytes, pairing_auth_token: bytes
    ) -> bytes: ...

    def request_pairing_keys(self, mac: str, signed_challenge: bytes) -> ServerPairingKeys: ...


class PairingTransport(Protocol):
    async def negotiate_protocol(self) -> int: ...

    async def request_pairing_auth_token(self, signed_request: bytes) -> bytes: ...

    async def get_ecdh_key(self) -> bytes: ...

    async def sign_pairing_token(self, challenge: bytes) -> bytes: ...

    async def set_pairing_key(self, encrypted_key: bytes, nonce: bytes) -> None: ...


def _encode_pairing_crypto_request(command: PairingCryptoCommand, payload: bytes = b"") -> bytes:
    expected_lengths = {
        PairingCryptoCommand.REQUEST_PROTOCOL_VERSIONS: 0,
        PairingCryptoCommand.SELECT_PROTOCOL_VERSION: 1,
        PairingCryptoCommand.SIGN_PAIRING_TOKEN: 32,
        PairingCryptoCommand.SET_PAIRING_KEY: 61,
        PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN: 32,
        PairingCryptoCommand.GET_ECDH_KEY: 0,
    }
    expected = expected_lengths.get(command)
    if expected is None:
        raise TiltProtocolError("Command is not part of the bounded pairing protocol.")
    if len(payload) != expected:
        raise TiltProtocolError(
            f"Pairing command {command.name} requires exactly {expected} payload bytes."
        )
    header = b"\x80\x00"
    body = bytes([command]) + payload
    return header + body + crc16(header + body).to_bytes(2, "little")


def _parse_pairing_crypto_response(frame: bytes) -> PairingCryptoResponse:
    if len(frame) < 5 or frame[:2] != b"\x80\x00":
        raise TiltProtocolError("Pairing response has an invalid crypto-layer header.")
    body = frame[2:-2]
    expected_checksum = crc16(frame[:2] + body).to_bytes(2, "little")
    if not hmac.compare_digest(frame[-2:], expected_checksum):
        raise TiltProtocolError("Pairing response checksum verification failed.")
    try:
        command = PairingCryptoCommand(body[0])
    except (IndexError, ValueError) as exc:
        raise TiltProtocolError("Pairing response contains an unknown command.") from exc
    payload = body[1:]
    if command is PairingCryptoCommand.NACK:
        if len(payload) != 2:
            raise TiltProtocolError("Pairing NACK has an invalid payload.")
        raise PairingNack(payload[0], payload[1])
    return PairingCryptoResponse(command, payload)


def _require_response(
    frame: bytes,
    command: PairingCryptoCommand,
    payload_length: int,
) -> bytes:
    response = _parse_pairing_crypto_response(frame)
    if response.command is not command or len(response.payload) != payload_length:
        raise TiltProtocolError(f"Shade returned an invalid {command.name} response.")
    return response.payload


def _require_ack(frame: bytes, command: PairingCryptoCommand) -> None:
    response = _parse_pairing_crypto_response(frame)
    if response.command is not PairingCryptoCommand.ACK or response.payload != bytes([command]):
        raise TiltProtocolError(f"Shade did not acknowledge {command.name}.")


class TiltPairingCloudClient:
    """Minimal authenticated client for only the three pairing endpoints."""

    def __init__(
        self,
        access_token: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not access_token or len(access_token) > 16_384:
            raise TiltCloudError("Tilt access token is invalid.")
        self._access_token = access_token
        self._opener = opener or urllib.request.urlopen
        self._timeout_seconds = timeout_seconds

    def register_device(self, mac: str, ecdh_key: bytes) -> None:
        if len(ecdh_key) != 64:
            raise TiltProtocolError("Shade ECDH key must be exactly 64 bytes.")
        self._post(
            self._device_path(mac),
            {"pubKey": ecdh_key.hex().upper(), "device": _ROLLER_SHADE_DEVICE_TYPE.hex().upper()},
            expect_json=False,
        )

    def request_challenge(
        self, mac: str, temporary_key: bytes, pairing_auth_token: bytes
    ) -> bytes:
        if len(temporary_key) != 32 or len(pairing_auth_token) != 32:
            raise TiltProtocolError("Temporary pairing values must be exactly 32 bytes.")
        payload = self._post(
            self._device_path(mac) + "/token",
            {"key": temporary_key.hex().upper(), "token": pairing_auth_token.hex().upper()},
        )
        return _hex_field(payload, "token", 32)

    def request_pairing_keys(self, mac: str, signed_challenge: bytes) -> ServerPairingKeys:
        if len(signed_challenge) != 32:
            raise TiltProtocolError("Signed pairing challenge must be exactly 32 bytes.")
        payload = self._post(
            self._device_path(mac) + "/pair",
            {"signed": signed_challenge.hex().upper()},
        )
        return ServerPairingKeys(
            plaintext_key=_hex_field(payload, "key", 32),
            nonce=_hex_field(payload, "nonce", 13),
            encrypted_key=_hex_field(payload, "package", 48),
        )

    def _device_path(self, mac: str) -> str:
        normalized = _normalize_mac(mac)
        return "devices/" + urllib.parse.quote(normalized, safe="")

    def _post(
        self,
        path: str,
        payload: Mapping[str, str],
        *,
        expect_json: bool = True,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            urllib.parse.urljoin(_TILT_API_URL, path),
            data=json.dumps(payload, separators=(",", ":")).encode("ascii"),
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "tilt-local-pair/1",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            finally:
                raise TiltCloudError(
                    f"Tilt key service rejected a pairing step ({exc.code})."
                ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TiltCloudError("Tilt key service was unavailable during pairing.") from exc
        if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
            raise TiltCloudError("Tilt key service returned an oversized response.")
        if not expect_json:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TiltCloudError("Tilt key service returned an invalid response.") from exc
        if not isinstance(decoded, Mapping):
            raise TiltCloudError("Tilt key service returned an unexpected response.")
        return decoded


def authenticate_tilt(
    username: str,
    password: str,
    *,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 15.0,
) -> str:
    """Exchange credentials for a short-lived token without persisting either."""

    if not username or not password:
        raise TiltCloudError("Tilt username and password are required.")
    payload = {
        "client_id": _AUTH0_CLIENT_ID,
        "username": username,
        "password": password,
        "realm": _AUTH0_CONNECTION,
        "grant_type": _AUTH0_GRANT_TYPE,
        "audience": _AUTH0_AUDIENCE,
        "scope": _AUTH0_SCOPE,
    }
    request = urllib.request.Request(
        _AUTH0_URL,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    open_request = opener or urllib.request.urlopen
    try:
        with open_request(request, timeout=timeout_seconds) as response:
            raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        finally:
            raise TiltCloudError(f"Tilt sign-in was rejected ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TiltCloudError("Tilt sign-in service was unavailable.") from exc
    if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
        raise TiltCloudError("Tilt sign-in returned an oversized response.")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TiltCloudError("Tilt sign-in returned an invalid response.") from exc
    token = decoded.get("access_token") if isinstance(decoded, Mapping) else None
    if not isinstance(token, str) or not token or len(token) > 16_384:
        raise TiltCloudError("Tilt sign-in did not return a usable access token.")
    return token


def _hex_field(payload: Mapping[str, Any], name: str, byte_length: int) -> bytes:
    value = payload.get(name)
    if not isinstance(value, str) or not re.fullmatch(
        rf"[0-9A-Fa-f]{{{byte_length * 2}}}", value
    ):
        raise TiltCloudError(f"Tilt key service returned an invalid {name} field.")
    return bytes.fromhex(value)


class _TiltPairingGattSession:
    def __init__(
        self,
        client: Any,
        *,
        ack_timeout_seconds: float = 2.0,
        response_timeout_seconds: float = 8.0,
    ) -> None:
        self._client = client
        self._ack_timeout_seconds = ack_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._notification_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        self._response_queue: asyncio.Queue[bytes | BaseException] = asyncio.Queue(maxsize=4)
        self._ack_waiters: dict[int, asyncio.Future[None]] = {}
        self._assembler = BleMessageAssembler()
        self._worker: asyncio.Task[None] | None = None
        self._tx_sequence = 1

    async def start(self) -> None:
        await self._client.start_notify(TILT_RESPONSE_UUID, self._notification_received)
        self._worker = asyncio.create_task(
            self._notification_worker(), name="tilt-pair-notifications"
        )

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        try:
            result = self._client.stop_notify(TILT_RESPONSE_UUID)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def negotiate_protocol(self) -> int:
        await asyncio.sleep(_PROTOCOL_THROTTLE_SECONDS)
        frame = await self._send(
            _encode_pairing_crypto_request(PairingCryptoCommand.REQUEST_PROTOCOL_VERSIONS)
        )
        response = _parse_pairing_crypto_response(frame)
        if response.command is not PairingCryptoCommand.REQUEST_PROTOCOL_VERSIONS:
            raise TiltProtocolError("Shade returned an invalid protocol-version response.")
        payload = response.payload
        if not payload:
            raise TiltProtocolError("Protocol response is missing its version count.")
        count = payload[0]
        versions = tuple(payload[1 : 1 + count])
        if len(versions) != count or len(payload) != count + 1:
            raise TiltProtocolError("Protocol response has an invalid version list.")
        selected = next(
            (version for version in SUPPORTED_CRYPTO_PROTOCOLS if version in versions), None
        )
        if selected is None:
            raise TiltProtocolError("Shade has no supported crypto protocol.")
        await asyncio.sleep(_PROTOCOL_THROTTLE_SECONDS)
        frame = await self._send(
            _encode_pairing_crypto_request(
                PairingCryptoCommand.SELECT_PROTOCOL_VERSION, bytes([selected])
            )
        )
        response = _parse_pairing_crypto_response(frame)
        direct = (
            response.command is PairingCryptoCommand.SELECT_PROTOCOL_VERSION
            and response.payload == bytes([selected])
        )
        ack = (
            response.command is PairingCryptoCommand.ACK
            and response.payload == bytes([PairingCryptoCommand.SELECT_PROTOCOL_VERSION])
        )
        if not direct and not ack:
            raise TiltProtocolError("Shade did not accept the selected crypto protocol.")
        await asyncio.sleep(_PROTOCOL_THROTTLE_SECONDS)
        return selected

    async def request_pairing_auth_token(self, signed_request: bytes) -> bytes:
        frame = await self._send(
            _encode_pairing_crypto_request(
                PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN, signed_request
            )
        )
        return _require_response(frame, PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN, 32)

    async def get_ecdh_key(self) -> bytes:
        frame = await self._send(
            _encode_pairing_crypto_request(PairingCryptoCommand.GET_ECDH_KEY)
        )
        return _require_response(frame, PairingCryptoCommand.GET_ECDH_KEY, 64)

    async def sign_pairing_token(self, challenge: bytes) -> bytes:
        frame = await self._send(
            _encode_pairing_crypto_request(PairingCryptoCommand.SIGN_PAIRING_TOKEN, challenge)
        )
        return _require_response(frame, PairingCryptoCommand.SIGN_PAIRING_TOKEN, 32)

    async def set_pairing_key(self, encrypted_key: bytes, nonce: bytes) -> None:
        if len(encrypted_key) != 48 or len(nonce) != 13:
            raise TiltProtocolError("Server key package has invalid lengths.")
        frame = await self._send(
            _encode_pairing_crypto_request(
                PairingCryptoCommand.SET_PAIRING_KEY, nonce + encrypted_key
            )
        )
        _require_ack(frame, PairingCryptoCommand.SET_PAIRING_KEY)

    def _notification_received(self, _sender: Any, data: bytearray) -> None:
        try:
            self._notification_queue.put_nowait(bytes(data))
        except asyncio.QueueFull:
            self._fail(TiltPairingError("Pairing notification queue overflowed."))

    async def _notification_worker(self) -> None:
        while True:
            data = await self._notification_queue.get()
            try:
                chunk = parse_ble_chunk(data)
                if chunk.for_bluetooth_layer:
                    acknowledged = parse_ble_ack(data)
                    waiter = self._ack_waiters.get(acknowledged)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                    continue
                acknowledged, message = self._assembler.add(data)
                await self._client.write_gatt_char(
                    TILT_COMMAND_UUID, make_ble_ack(acknowledged)
                )
                if message is not None:
                    self._response_queue.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(exc)

    def _fail(self, error: BaseException) -> None:
        for waiter in self._ack_waiters.values():
            if not waiter.done():
                waiter.set_exception(error)
        try:
            self._response_queue.put_nowait(error)
        except asyncio.QueueFull:
            pass

    async def _send(self, frame: bytes, *, retry_chunks: bool = True) -> bytes:
        if not self._response_queue.empty():
            raise TiltPairingError("Unexpected stale shade response during pairing.")
        chunks = chunk_for_ble(frame, start_sequence=self._tx_sequence)
        for chunk in chunks:
            sequence = chunk[1] & 0x3F
            attempts = 2 if retry_chunks else 1
            for attempt in range(attempts):
                waiter = asyncio.get_running_loop().create_future()
                self._ack_waiters[sequence] = waiter
                try:
                    await self._client.write_gatt_char(TILT_COMMAND_UUID, chunk)
                    await asyncio.wait_for(waiter, timeout=self._ack_timeout_seconds)
                    break
                except asyncio.TimeoutError as exc:
                    if attempt + 1 == attempts:
                        raise TiltPairingTimeout(
                            f"Timed out waiting for pairing BLE ACK {sequence}."
                        ) from exc
                finally:
                    if self._ack_waiters.get(sequence) is waiter:
                        self._ack_waiters.pop(sequence, None)
            self._tx_sequence = next_sequence(sequence)
        try:
            response = await asyncio.wait_for(
                self._response_queue.get(), timeout=self._response_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise TiltPairingTimeout("Timed out waiting for a shade pairing response.") from exc
        if isinstance(response, BaseException):
            raise response
        return response


class _PrivateKeyStager:
    def __init__(self, final_path: Path, *, replace_existing: bool) -> None:
        if not final_path.is_absolute():
            raise TiltPairingError("Pairing-key output path must be absolute.")
        self.final_path = final_path
        self.replace_existing = replace_existing
        self.staged_path: Path | None = None

    def prepare(self, key: bytes) -> Path:
        if len(key) != 32:
            raise TiltProtocolError("Pairing key must be exactly 32 bytes.")
        parent = self.final_path.parent
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            raise TiltPairingError("Pairing-key output directory is unavailable.") from exc
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise TiltPairingError("Pairing-key output directory is unsafe.")
        if parent_info.st_uid not in {0, os.geteuid()}:
            raise TiltPairingError("Pairing-key output directory has an unexpected owner.")
        try:
            existing = self.final_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TiltPairingError("Unable to inspect existing pairing-key file.") from exc
        else:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise TiltPairingError("Existing pairing-key path is unsafe.")
            if existing.st_uid not in {0, os.geteuid()}:
                raise TiltPairingError("Existing pairing-key file has an unexpected owner.")
            if existing.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise TiltPairingError("Existing pairing-key file is not private.")
            if not self.replace_existing:
                raise TiltPairingError(
                    "Pairing-key file already exists; replacement was not approved."
                )

        staged = parent / f".{self.final_path.name}.pending.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(staged, flags, 0o600)
            payload = memoryview((key.hex() + "\n").encode("ascii"))
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    raise OSError("Pairing-key staging write made no progress.")
                payload = payload[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            staged.chmod(0o600)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            raise TiltPairingError("Unable to stage the recovered pairing key.") from exc
        self.staged_path = staged
        return staged

    def commit(self) -> None:
        if self.staged_path is None:
            raise TiltPairingError("No pairing key has been staged.")
        try:
            os.replace(self.staged_path, self.final_path)
        except OSError as exc:
            raise PairingKeyAmbiguousError(self.staged_path) from exc
        self.staged_path = None
        try:
            self.final_path.chmod(0o600)
            directory = os.open(self.final_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise PairingKeyAmbiguousError(self.final_path) from exc


async def complete_pairing(
    transport: PairingTransport,
    cloud: PairingCloud,
    mac: str,
    key_stager: _PrivateKeyStager,
    *,
    temporary_key: bytes | None = None,
) -> PairingResult:
    """Complete the recovered app protocol and atomically retain the new key."""

    normalized_mac = _normalize_mac(mac)
    key = temporary_key or secrets.token_bytes(32)
    if len(key) != 32:
        raise TiltProtocolError("Temporary pairing key must be exactly 32 bytes.")

    await transport.negotiate_protocol()
    signed_request = hmac.new(key, _PAIRING_REQUEST_MESSAGE, hashlib.sha256).digest()
    try:
        pairing_auth_token = await transport.request_pairing_auth_token(signed_request)
    except PairingNack as exc:
        if (
            exc.command != PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN
            or exc.reason != NackReason.NOT_READY
        ):
            raise
        ecdh_key = await transport.get_ecdh_key()
        cloud.register_device(normalized_mac, ecdh_key)
        pairing_auth_token = await transport.request_pairing_auth_token(signed_request)

    challenge = cloud.request_challenge(normalized_mac, key, pairing_auth_token)
    signed_challenge = await transport.sign_pairing_token(challenge)
    pairing_keys = cloud.request_pairing_keys(normalized_mac, signed_challenge)
    staged_path = key_stager.prepare(pairing_keys.plaintext_key)
    install_attempted = False
    try:
        install_attempted = True
        await transport.set_pairing_key(pairing_keys.encrypted_key, pairing_keys.nonce)
        key_stager.commit()
    except Exception as exc:
        if install_attempted:
            ambiguous_path = (
                exc.staged_path if isinstance(exc, PairingKeyAmbiguousError) else staged_path
            )
            raise PairingKeyAmbiguousError(
                ambiguous_path, target_mac=normalized_mac
            ) from exc
        raise
    return PairingResult(normalized_mac, key_stager.final_path)


def _normalize_mac(mac: str) -> str:
    if not _MAC_PATTERN.fullmatch(mac):
        raise TiltPairingError("Pairing target did not expose a valid Bluetooth MAC address.")
    return mac.upper()


def _default_scanner_factory() -> Any:
    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise TiltPairingError("The Tilt pairing tool requires bleak.") from exc
    return BleakScanner


def _default_client_factory(address: str, *, timeout: float, pair: bool) -> Any:
    try:
        from bleak import BleakClient
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise TiltPairingError("The Tilt pairing tool requires bleak.") from exc
    return BleakClient(address, timeout=timeout, pair=pair)


async def discover_pairing_shade(
    *,
    timeout_seconds: float,
    scanner_factory: Callable[[], Any] | None = None,
) -> Any:
    candidates: dict[str, Any] = {}
    found = asyncio.Event()

    def detection_callback(device: Any, advertisement: Any) -> None:
        service_data = getattr(advertisement, "service_data", {}) or {}
        normalized_keys = {str(key).lower() for key in service_data}
        if _PAIRING_INDICATOR_UUID in normalized_keys:
            candidates[str(device.address).upper()] = device
            found.set()

    make_scanner = scanner_factory or _default_scanner_factory()
    scanner = make_scanner(
        detection_callback=detection_callback,
        service_uuids=[TILT_SERVICE_UUID],
    )
    try:
        async with scanner:
            await asyncio.wait_for(found.wait(), timeout=timeout_seconds)
            await asyncio.sleep(_PAIRING_COLLISION_WINDOW_SECONDS)
    except asyncio.TimeoutError as exc:
        raise TiltPairingTimeout("No roller shade advertised the Tilt pairing marker.") from exc
    if not candidates:
        raise TiltPairingTimeout("No roller shade advertised the Tilt pairing marker.")
    if len(candidates) != 1:
        raise TiltPairingError(
            "More than one roller shade is in pairing mode; pair only one shade at a time."
        )
    selected = next(iter(candidates.values()))
    _normalize_mac(selected.address)
    return selected


async def pair_advertising_shade(
    cloud: PairingCloud,
    output_path: Path,
    *,
    scan_timeout_seconds: float,
    replace_existing: bool,
    scanner_factory: Callable[[], Any] | None = None,
    client_factory: Callable[..., Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> PairingResult:
    status = status_callback or (lambda _message: None)
    status("waiting_for_pairing_advertisement")
    device = await discover_pairing_shade(
        timeout_seconds=scan_timeout_seconds, scanner_factory=scanner_factory
    )
    status("pairing_shade_detected")
    make_client = client_factory or _default_client_factory
    client = make_client(device.address, timeout=12.0, pair=False)
    await client.connect()
    try:
        if not client.is_connected:
            raise TiltPairingError("Unable to connect to the advertising roller shade.")
        status("pairing_exchange_started")
        transport = _TiltPairingGattSession(client)
        await transport.start()
        try:
            return await complete_pairing(
                transport,
                cloud,
                device.address,
                _PrivateKeyStager(output_path, replace_existing=replace_existing),
            )
        finally:
            await transport.stop()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tilt-local-pair",
        description="Pair exactly one advertising Tilt roller shade and retain its key.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Protected key output path.")
    parser.add_argument("--username", help="Tilt account email; prompts when omitted.")
    parser.add_argument("--scan-timeout", type=float, default=30.0)
    parser.add_argument("--replace-existing-key", action="store_true")
    parser.add_argument(
        "--permit-live-pairing",
        action="store_true",
        help="Required acknowledgement that this command changes the shade's pairing key.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.permit_live_pairing:
        print(
            "error: --permit-live-pairing is required because pairing changes the shade key",
            file=sys.stderr,
        )
        return 2
    if args.scan_timeout <= 0 or args.scan_timeout > 300:
        print("error: --scan-timeout must be between 0 and 300 seconds", file=sys.stderr)
        return 2
    username = args.username or input("Tilt account email: ").strip()
    password = getpass.getpass("Tilt account password: ")
    try:
        token = authenticate_tilt(username, password)
        cloud = TiltPairingCloudClient(token)
        result = asyncio.run(
            pair_advertising_shade(
                cloud,
                args.output,
                scan_timeout_seconds=args.scan_timeout,
                replace_existing=args.replace_existing_key,
                status_callback=lambda message: print(f"status={message}", flush=True),
            )
        )
    except (TiltPairingError, TiltProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"paired_shade={result.mac}")
    print(f"key_stored={result.key_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
