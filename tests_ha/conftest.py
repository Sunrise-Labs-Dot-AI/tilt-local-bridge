"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from custom_components.tilt_bridge.const import (
    REMOTE_INFO_UUID,
    REMOTE_REQUEST_UUID,
    REMOTE_RESPONSE_UUID,
)
from custom_components.tilt_bridge.protocol import (
    Identity,
    MessageAssembler,
    canonical_json,
    chunk_message,
    signing_bytes,
    verify_signature,
)

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "tests" / "fixtures" / "bluetooth_remote_vectors.json").read_text())
BRIDGE_ID = "0123456789abcdef0123456789abcdef"
BRIDGE_ADDRESS = "AA:BB:CC:DD:EE:01"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the test Home Assistant load custom_components from this repo."""


class FakeBridge:
    """A bridge that speaks the wire protocol over a fake bleak client."""

    def __init__(self, *, paired: set[str] | None = None, writes: bool = True) -> None:
        self.paired: set[str] = set(paired or ())
        self.writes = writes
        self.pending: dict[str, Any] | None = None
        self.approve_on_status = False
        self.shades = [
            {"id": "door", "name": "Door", "position": 100, "battery": 91, "available": True, "target": None, "age": 3},
            {"id": "right", "name": "Right", "position": 40, "battery": 68, "available": True, "target": None, "age": 3},
        ]
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.nonces: dict[str, str] = {}
        self.unreachable = False
        self.stale_services = False
        self.cache_cleared = 0

    def status_message(self) -> dict[str, Any]:
        return {"t": "status", "writes": self.writes, "shades": [dict(s) for s in self.shades]}

    def handle(self, request: dict[str, Any], connection: str) -> dict[str, Any]:
        self.requests.append(request)
        cpk = request.get("cpk")
        to = {"to": cpk[:8]} if isinstance(cpk, str) else {}
        if request.get("t") == "nonce":
            nonce = f"{len(self.requests):032x}"
            self.nonces[connection] = nonce
            return {"t": "nonce", "v": 1, "bridge_id": BRIDGE_ID, "name": "Office Bridge", "n": nonce, "paired": cpk in self.paired, **to}
        if request.get("n") != self.nonces.get(connection):
            nonce = f"{len(self.requests):032x}"
            self.nonces[connection] = nonce
            return {"t": "error", "code": "nonce", "message": "stale", "n": nonce, **to}
        signature = request.get("sig")
        unsigned = {k: v for k, v in request.items() if k != "sig"}
        assert verify_signature(bytes.fromhex(cpk), signing_bytes(unsigned), bytes.fromhex(signature)), "bad signature"
        nonce = f"{len(self.requests) + 1000:032x}"
        self.nonces[connection] = nonce
        n = {"n": nonce, **to}
        kind = request["t"]
        if kind == "pair":
            if cpk in self.paired:
                return {"t": "pair", "status": "approved", **n}
            self.pending = {"cpk": cpk, "name": request.get("name")}
            return {"t": "pair", "status": "pending", "code": "482913", "expires_in": 120, "challenge": {"shade": "door", "name": "Door", "direction": "down"}, **n}
        if kind == "pair_status":
            if cpk in self.paired:
                return {"t": "pair", "status": "approved", **n}
            if self.pending and self.pending["cpk"] == cpk:
                if self.approve_on_status:
                    self.paired.add(cpk)
                    self.pending = None
                    return {"t": "pair", "status": "approved", **n}
                return {"t": "pair", "status": "pending", **n}
            return {"t": "pair", "status": "expired", **n}
        if cpk not in self.paired:
            return {"t": "error", "code": "unauthorized", "message": "not paired", "paired": False, **n}
        if kind == "status":
            return {**self.status_message(), **n}
        if kind == "refresh":
            return {"t": "refresh", "accepted": True, **n}
        if kind == "set":
            shade = next((s for s in self.shades if s["id"] == request.get("shade")), None)
            if shade is None:
                return {"t": "error", "code": "unknown_shade", "message": "no such shade", **n}
            if not self.writes:
                return {"t": "error", "code": "writes_disabled", "message": "read-only bridge", **n}
            shade["target"] = request["position"] if request["position"] != shade["position"] else None
            return {"t": "set", "shade": shade["id"], "position": request["position"], "outcome": "accepted", **n}
        return {"t": "error", "code": "bad_request", "message": "unknown", **n}


class _FakeServices:
    """bleak's service collection, reduced to the one lookup the client makes."""

    def __init__(self, bridge: FakeBridge) -> None:
        self._bridge = bridge

    def get_characteristic(self, uuid: str) -> object | None:
        if self._bridge.stale_services:
            return None
        return object() if uuid in (REMOTE_REQUEST_UUID, REMOTE_RESPONSE_UUID, REMOTE_INFO_UUID) else None


class FakeBleakClient:
    """Enough of bleak's client for the session: notify, write, read, disconnect."""

    def __init__(self, bridge: FakeBridge, connection: str) -> None:
        self._bridge = bridge
        self._connection = connection
        self._assembler = MessageAssembler()
        self._callback: Callable[[Any, bytearray], None] | None = None
        self.mtu_size = 185
        self.disconnected = False
        self.services = _FakeServices(bridge)

    async def clear_cache(self) -> bool:
        self._bridge.cache_cleared += 1
        self._bridge.stale_services = False
        return True

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        assert uuid == REMOTE_RESPONSE_UUID
        self._callback = callback

    async def stop_notify(self, uuid: str) -> None:
        self._callback = None

    async def read_gatt_char(self, uuid: str) -> bytearray:
        assert uuid == REMOTE_INFO_UUID
        return bytearray(canonical_json({"v": 1, "bridge_id": BRIDGE_ID, "name": "Office Bridge"}))

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        assert uuid == REMOTE_REQUEST_UUID
        message = self._assembler.add(bytes(data))
        if message is None:
            return
        reply = self._bridge.handle(json.loads(message.decode("utf-8")), self._connection)
        payload = canonical_json(reply)
        loop = asyncio.get_running_loop()
        for chunk in chunk_message(payload, chunk_size=self.mtu_size - 3):
            loop.call_soon(self._deliver, chunk)

    def _deliver(self, chunk: bytes) -> None:
        if self._callback is not None:
            self._callback(None, bytearray(chunk))

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def identity() -> Identity:
    return Identity.from_seed(bytes.fromhex(VECTORS["seed_hex"]))


@pytest.fixture
def fake_bridge() -> FakeBridge:
    return FakeBridge()


def client_factory_for(bridge: FakeBridge) -> Callable[[Any], Any]:
    async def factory(device: Any) -> FakeBleakClient:
        if bridge.unreachable:
            raise TimeoutError("out of range")
        bridge.connections += 1
        return FakeBleakClient(bridge, f"conn-{bridge.connections}")

    return factory
