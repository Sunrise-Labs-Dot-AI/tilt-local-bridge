"""Allowlisted Tilt BLE transport with explicit read and write capabilities."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .tilt_bridge_config import ShadeAccessPermit, ShadeConfig
from .tilt_protocol import (
    AuthenticationError,
    BleMessageAssembler,
    CryptoCommand,
    ShadeCommand,
    ShadeStatus,
    SUPPORTED_CRYPTO_PROTOCOLS,
    TILT_COMMAND_UUID,
    TILT_RESPONSE_UUID,
    TiltProtocolError,
    chunk_for_ble,
    decode_application_response,
    encode_nonce_request,
    encode_position_request,
    encode_protocol_selection,
    encode_protocol_versions_request,
    encode_read_request,
    make_ble_ack,
    next_sequence,
    pairing_key_matches_proof,
    parse_ble_ack,
    parse_ble_chunk,
    parse_crypto_response,
    parse_nonce_response,
    parse_protocol_versions,
    parse_status,
)


_ResultT = TypeVar("_ResultT")
_PROTOCOL_THROTTLE_SECONDS = 0.1
_SECURE_CONNECTOR_THROTTLE_SECONDS = 0.1
_BLE_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


class TiltBleError(RuntimeError):
    """Base error for bounded Tilt BLE sessions."""


class TiltBleTimeout(TiltBleError):
    """Raised when a required BLE ACK or response does not arrive."""


class AmbiguousPositionWrite(TiltBleError):
    """Raised when a position write cannot be confirmed by readback."""


class PositionVerificationPending(AmbiguousPositionWrite):
    """Raised when the shade responds but has not reached the requested target."""

    def __init__(self, message: str, status: ShadeStatus) -> None:
        super().__init__(message)
        self.status = status


def _global_ble_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _BLE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _BLE_LOCKS[loop] = lock
    return lock


def _default_client_factory(address: str, *, timeout: float, pair: bool) -> Any:
    try:
        from bleak import BleakClient
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise TiltBleError("The Tilt bridge requires the bleak package.") from exc
    return BleakClient(address, timeout=timeout, pair=pair)


class TiltShadeClient:
    """Open short, globally serialized sessions to one fixed allowlisted shade."""

    def __init__(
        self,
        shade: ShadeConfig,
        pairing_key: bytes,
        permit: ShadeAccessPermit,
        *,
        client_factory: Callable[..., Any] | None = None,
        connect_timeout_seconds: float = 12.0,
        ack_timeout_seconds: float = 2.0,
        response_timeout_seconds: float = 6.0,
    ) -> None:
        permit.assert_valid()
        if len(pairing_key) != 32:
            raise TiltProtocolError("Tilt pairing keys must be exactly 32 bytes.")
        self.shade = shade
        self._pairing_key = pairing_key
        self._permit = permit
        self._client_factory = client_factory or _default_client_factory
        self._connect_timeout_seconds = connect_timeout_seconds
        self._ack_timeout_seconds = ack_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds

    async def read_status(self) -> ShadeStatus:
        self._permit.assert_valid()
        return await self._run_session(lambda session: session.read_status())

    async def set_position_and_read_status(
        self,
        position_percent: int,
        *,
        settle_seconds: float = 2.0,
    ) -> tuple[ShadeStatus, bool]:
        """Set one bounded target once, then verify it without resending."""

        self._permit.require_position_write()
        if isinstance(position_percent, bool) or not isinstance(position_percent, int):
            raise TiltProtocolError("Position must be an integer percent.")
        if not 0 <= position_percent <= 100:
            raise TiltProtocolError("Position must be between 0 and 100 percent.")

        async def operation(session: _TiltBleSession) -> tuple[ShadeStatus, bool]:
            before = await session.read_status()
            if before.position_percent == position_percent:
                return before, False
            try:
                await session.set_position(position_percent)
            except TiltBleTimeout:
                try:
                    after_timeout = await session.read_status()
                except Exception as readback_error:
                    raise AmbiguousPositionWrite(
                        "Position write timed out and readback also failed."
                    ) from readback_error
                if after_timeout.position_percent == position_percent:
                    return after_timeout, True
                raise PositionVerificationPending(
                    "Position write timed out and the responding shade has not reached the target.",
                    after_timeout,
                )
            if settle_seconds > 0:
                await asyncio.sleep(settle_seconds)
            after = await session.read_status()
            if after.position_percent != position_percent:
                raise PositionVerificationPending(
                    "Position write completed and the responding shade has not reached the target.",
                    after,
                )
            return after, True

        return await self._run_session(operation)

    async def _run_session(
        self,
        operation: Callable[["_TiltBleSession"], Awaitable[_ResultT]],
    ) -> _ResultT:
        async with _global_ble_lock():
            client = self._client_factory(
                self.shade.mac,
                timeout=self._connect_timeout_seconds,
                pair=False,
            )
            await client.connect()
            try:
                if not client.is_connected:
                    raise TiltBleError(f"Unable to connect to configured shade {self.shade.id}.")
                session = _TiltBleSession(
                    client,
                    self._pairing_key,
                    ack_timeout_seconds=self._ack_timeout_seconds,
                    response_timeout_seconds=self._response_timeout_seconds,
                )
                await session.start()
                try:
                    await session.authenticate()
                    return await operation(session)
                finally:
                    await session.stop()
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass


class _TiltBleSession:
    def __init__(
        self,
        client: Any,
        pairing_key: bytes,
        *,
        ack_timeout_seconds: float,
        response_timeout_seconds: float,
    ) -> None:
        self._client = client
        self._pairing_key = pairing_key
        self._ack_timeout_seconds = ack_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._notification_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        self._response_queue: asyncio.Queue[bytes | BaseException] = asyncio.Queue(maxsize=4)
        self._ack_waiters: dict[int, asyncio.Future[None]] = {}
        self._assembler = BleMessageAssembler()
        self._worker: asyncio.Task[None] | None = None
        self._tx_sequence = 1
        self._counter = 1
        self._message_id = 1
        self._nonce: bytes | None = None

    async def start(self) -> None:
        await self._client.start_notify(TILT_RESPONSE_UUID, self._notification_received)
        self._worker = asyncio.create_task(
            self._notification_worker(), name="tilt-ble-notifications"
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

    async def authenticate(self) -> None:
        await asyncio.sleep(_PROTOCOL_THROTTLE_SECONDS)
        versions_frame = await self._send_frame(
            encode_protocol_versions_request(), retry_chunks=True
        )
        versions = parse_protocol_versions(parse_crypto_response(versions_frame))
        selected = next(
            (version for version in SUPPORTED_CRYPTO_PROTOCOLS if version in versions),
            None,
        )
        if selected is None:
            raise TiltProtocolError("Shade does not support an allowlisted crypto protocol.")

        await asyncio.sleep(_PROTOCOL_THROTTLE_SECONDS)
        selection_frame = await self._send_frame(
            encode_protocol_selection(selected), retry_chunks=True
        )
        selection = parse_crypto_response(selection_frame)
        if selection.command is CryptoCommand.SELECT_PROTOCOL_VERSION:
            if selection.payload != bytes([selected]):
                raise TiltProtocolError("Shade selected an unexpected crypto protocol.")
        elif selection.command is CryptoCommand.ACK:
            if selection.payload != bytes([CryptoCommand.SELECT_PROTOCOL_VERSION]):
                raise TiltProtocolError("Shade returned an invalid protocol-selection ACK.")
        else:
            raise TiltProtocolError("Shade did not acknowledge protocol selection.")

        await asyncio.sleep(
            _PROTOCOL_THROTTLE_SECONDS + _SECURE_CONNECTOR_THROTTLE_SECONDS
        )
        nonce_frame = await self._send_frame(encode_nonce_request(), retry_chunks=True)
        nonce_response = parse_nonce_response(parse_crypto_response(nonce_frame))
        if not pairing_key_matches_proof(self._pairing_key, nonce_response.key_proof):
            raise AuthenticationError("Configured pairing key did not authenticate this shade.")
        self._nonce = nonce_response.nonce
        await asyncio.sleep(_SECURE_CONNECTOR_THROTTLE_SECONDS)

    async def read_status(self) -> ShadeStatus:
        response = await self._application_request(ShadeCommand.GET_STATUS, retry_chunks=True)
        return parse_status(response)

    async def set_position(self, position_percent: int) -> None:
        nonce = self._require_nonce()
        counter, message_id = self._take_application_ids()
        frame = encode_position_request(
            position_percent,
            key=self._pairing_key,
            nonce=nonce,
            counter=counter,
            message_id=message_id,
        )
        response_frame = await self._send_frame(frame, retry_chunks=False)
        decode_application_response(
            response_frame,
            key=self._pairing_key,
            nonce=nonce,
            expected_command=ShadeCommand.SET_POSITION,
            expected_message_id=message_id,
            expected_counter=counter,
        )

    async def _application_request(
        self,
        command: ShadeCommand,
        *,
        retry_chunks: bool,
    ):
        nonce = self._require_nonce()
        counter, message_id = self._take_application_ids()
        frame = encode_read_request(
            command,
            key=self._pairing_key,
            nonce=nonce,
            counter=counter,
            message_id=message_id,
        )
        response_frame = await self._send_frame(frame, retry_chunks=retry_chunks)
        return decode_application_response(
            response_frame,
            key=self._pairing_key,
            nonce=nonce,
            expected_command=command,
            expected_message_id=message_id,
            expected_counter=counter,
        )

    def _require_nonce(self) -> bytes:
        if self._nonce is None:
            raise AuthenticationError("Tilt session was not authenticated.")
        return self._nonce

    def _take_application_ids(self) -> tuple[int, int]:
        counter = self._counter
        message_id = self._message_id
        self._counter = 1 if counter == 0x7FFF else counter + 1
        self._message_id = 1 if message_id == 15 else message_id + 1
        return counter, message_id

    def _notification_received(self, _sender: Any, data: bytearray) -> None:
        try:
            self._notification_queue.put_nowait(bytes(data))
        except asyncio.QueueFull:
            self._fail(TiltBleError("Tilt notification queue overflowed."))

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

    async def _send_frame(self, frame: bytes, *, retry_chunks: bool) -> bytes:
        if not self._response_queue.empty():
            raise TiltBleError("Unexpected stale Tilt response before request.")
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
                        raise TiltBleTimeout(
                            f"Timed out waiting for BLE ACK sequence {sequence}."
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
            raise TiltBleTimeout("Timed out waiting for Tilt response.") from exc
        if isinstance(response, BaseException):
            raise response
        return response
