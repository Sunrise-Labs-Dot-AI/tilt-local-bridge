"""BLE client for the bridge's phone protocol, on Home Assistant's Bluetooth stack.

One connection per operation batch: connect, subscribe, exchange, disconnect.
Replies carry the next nonce as ``n``; pushes never do, which is how a reply is
told apart from a status push that lands while a request is in flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakConnectionError,
    BleakNotFoundError,
    establish_connection,
)

from .const import (
    REMOTE_INFO_UUID,
    REMOTE_REQUEST_UUID,
    REMOTE_RESPONSE_UUID,
    REQUEST_TIMEOUT_SECONDS,
)
from .protocol import (
    BridgeStatus,
    Identity,
    MessageAssembler,
    PairingReply,
    ProtocolError,
    canonical_json,
    chunk_message,
    chunk_size_for_mtu,
    parse_pairing,
    parse_status,
    signing_bytes,
)

_LOGGER = logging.getLogger(__name__)


class TiltBridgeError(Exception):
    """A refusal or failure from the bridge, with the bridge's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TiltBridgeUnavailable(TiltBridgeError):
    """The bridge could not be reached over Bluetooth."""

    def __init__(self, message: str) -> None:
        super().__init__("unreachable", message)


class TiltBridgeNotPaired(TiltBridgeError):
    """This Home Assistant is not, or no longer, approved on the bridge."""

    def __init__(self) -> None:
        super().__init__("unauthorized", "This Home Assistant is not paired with the bridge.")


class TiltBridgeSession:
    """An open connection to the bridge with a live nonce."""

    def __init__(self, client: BleakClient, identity: Identity, *, name: str) -> None:
        self._client = client
        self._identity = identity
        self._name = name
        self._assembler = MessageAssembler()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._nonce: str | None = None
        self._bridge_id: str | None = None
        self.bridge_name: str | None = None
        self.paired: bool | None = None
        self._push_listeners: list[Callable[[BridgeStatus], None]] = []

    @property
    def bridge_id(self) -> str | None:
        return self._bridge_id

    def add_push_listener(self, listener: Callable[[BridgeStatus], None]) -> None:
        self._push_listeners.append(listener)

    async def start(self) -> None:
        await self._client.start_notify(REMOTE_RESPONSE_UUID, self._on_notify)

    async def close(self) -> None:
        try:
            await self._client.stop_notify(REMOTE_RESPONSE_UUID)
        except Exception:  # noqa: BLE001 - the link may already be gone
            pass

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        try:
            message = self._assembler.add(bytes(data))
        except ProtocolError as exc:
            _LOGGER.debug("Dropped chunk from %s: %s", self._name, exc)
            return
        if message is None:
            return
        try:
            parsed = json.loads(message.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(parsed, dict):
            return
        if parsed.get("to") not in (None, self._identity.public_key_hex[:8]):
            return
        if isinstance(parsed.get("n"), str) or parsed.get("t") == "error":
            self._inbox.put_nowait(parsed)
            return
        if parsed.get("t") == "status":
            try:
                status = parse_status(parsed)
            except ProtocolError:
                return
            for listener in self._push_listeners:
                listener(status)

    async def read_info(self) -> dict[str, Any]:
        raw = await self._client.read_gatt_char(REMOTE_INFO_UUID)
        info = json.loads(bytes(raw).decode("utf-8"))
        if isinstance(info, dict) and isinstance(info.get("bridge_id"), str):
            self._bridge_id = info["bridge_id"]
            self.bridge_name = str(info.get("name") or "")
        return info

    async def handshake(self) -> dict[str, Any]:
        reply = await self._exchange({"t": "nonce", "cpk": self._identity.public_key_hex})
        if reply.get("t") != "nonce":
            raise _error_from(reply)
        self._bridge_id = str(reply.get("bridge_id"))
        self.bridge_name = str(reply.get("name") or "")
        self.paired = bool(reply.get("paired"))
        return reply

    async def pair(self, device_name: str) -> PairingReply:
        reply = await self._signed({"t": "pair", "name": device_name})
        if reply.get("t") != "pair":
            raise _error_from(reply)
        return parse_pairing(reply)

    async def pair_status(self) -> PairingReply:
        reply = await self._signed({"t": "pair_status"})
        if reply.get("t") != "pair":
            raise _error_from(reply)
        return parse_pairing(reply)

    async def status(self) -> BridgeStatus:
        reply = await self._signed({"t": "status"})
        if reply.get("t") != "status":
            raise _error_from(reply)
        return parse_status(reply)

    async def set_position(self, shade_id: str, position: int) -> str:
        reply = await self._signed({"t": "set", "shade": shade_id, "position": int(position)})
        if reply.get("t") != "set":
            raise _error_from(reply)
        return str(reply.get("outcome") or "accepted")

    async def refresh(self) -> None:
        reply = await self._signed({"t": "refresh"})
        if reply.get("t") != "refresh":
            raise _error_from(reply)

    async def _signed(self, body: dict[str, Any], retried: bool = False) -> dict[str, Any]:
        if self._nonce is None or self._bridge_id is None:
            await self.handshake()
        request = dict(body)
        request["cpk"] = self._identity.public_key_hex
        request["n"] = self._nonce
        request["bridge_id"] = self._bridge_id
        request["sig"] = self._identity.sign(signing_bytes(request)).hex()
        reply = await self._exchange(request)
        if reply.get("t") == "error" and reply.get("code") == "nonce" and not retried:
            return await self._signed(body, retried=True)
        return reply

    async def _exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        while not self._inbox.empty():
            self._inbox.get_nowait()
        payload = canonical_json(message)
        chunk_size = chunk_size_for_mtu(getattr(self._client, "mtu_size", None))
        for chunk in chunk_message(payload, chunk_size=chunk_size):
            await self._client.write_gatt_char(REMOTE_REQUEST_UUID, chunk, response=True)
        try:
            reply = await asyncio.wait_for(self._inbox.get(), timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise TiltBridgeUnavailable("The bridge did not answer in time.") from exc
        if isinstance(reply.get("n"), str):
            self._nonce = reply["n"]
        return reply


def _error_from(reply: dict[str, Any]) -> TiltBridgeError:
    if reply.get("t") == "error":
        code = str(reply.get("code") or "error")
        if code == "unauthorized":
            return TiltBridgeNotPaired()
        return TiltBridgeError(code, str(reply.get("message") or "The bridge refused the request."))
    return TiltBridgeError("unexpected", f"The bridge answered with {reply.get('t')!r}.")


class TiltBridgeClient:
    """Connect to a bridge for one batch of operations at a time."""

    def __init__(
        self,
        identity: Identity,
        *,
        name: str,
        ble_device_provider: Callable[[], BLEDevice | None],
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._identity = identity
        self._name = name
        self._provider = ble_device_provider
        self._client_factory = client_factory
        self._lock = asyncio.Lock()

    async def run(self, operation: Callable[[TiltBridgeSession], Any]) -> Any:
        """Open a session, run the operation, and always disconnect."""

        async with self._lock:
            device = self._provider()
            if device is None:
                raise TiltBridgeUnavailable("The bridge is not in Bluetooth range of any adapter.")
            try:
                if self._client_factory is not None:
                    client = await self._client_factory(device)
                else:
                    client = await establish_connection(
                        BleakClientWithServiceCache, device, self._name, max_attempts=3
                    )
            except (BleakNotFoundError, BleakConnectionError, TimeoutError) as exc:
                raise TiltBridgeUnavailable(f"Could not connect to the bridge: {exc}") from exc
            session = TiltBridgeSession(client, self._identity, name=self._name)
            try:
                await session.start()
                return await operation(session)
            except (asyncio.TimeoutError, OSError) as exc:
                raise TiltBridgeUnavailable(f"Bluetooth failure talking to the bridge: {exc}") from exc
            finally:
                await session.close()
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - already gone is fine
                    pass
