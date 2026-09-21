"""Wire protocol shared with the bridge and the phone app, vendored for Home Assistant.

Kept byte-for-byte compatible with tilt_local_bridge.tilt_remote and checked
against the same golden vectors. Nothing here touches Bluetooth.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PROTOCOL_VERSION = 1
SIGNING_PREFIX = b"tilt-remote/1\n"
MAX_MESSAGE_BYTES = 4096
MIN_CHUNK_BYTES = 8
DEFAULT_CHUNK_BYTES = 20
MAX_CHUNK_BYTES = 244

_CHUNK_START = 0x80
_CHUNK_END = 0x40
_SEQUENCE_MASK = 0x3F


class ProtocolError(RuntimeError):
    """Malformed frame or message."""


def canonical_json(value: object) -> bytes:
    _require_canonical(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _require_canonical(value: object) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        if isinstance(value, float):
            raise ProtocolError("Canonical JSON does not allow floating point numbers.")
        return
    if isinstance(value, float):
        raise ProtocolError("Canonical JSON does not allow floating point numbers.")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("Canonical JSON keys must be strings.")
            _require_canonical(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _require_canonical(item)
        return
    raise ProtocolError("Value cannot be encoded as canonical JSON.")


def signing_bytes(request: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in request.items() if key != "sig"}
    return SIGNING_PREFIX + canonical_json(unsigned)


@dataclass(frozen=True)
class Identity:
    seed: bytes
    public_key_hex: str

    @classmethod
    def from_seed(cls, seed: bytes) -> Identity:
        if len(seed) != 32:
            raise ProtocolError("Ed25519 seeds must be exactly 32 bytes.")
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cls(seed=seed, public_key_hex=public.hex())

    def sign(self, message: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(self.seed).sign(message)


def verify_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def chunk_size_for_mtu(mtu: int | None) -> int:
    if mtu is None or mtu < 23:
        return DEFAULT_CHUNK_BYTES
    return max(MIN_CHUNK_BYTES, min(MAX_CHUNK_BYTES, mtu - 3))


def chunk_message(payload: bytes, *, chunk_size: int = DEFAULT_CHUNK_BYTES) -> tuple[bytes, ...]:
    if not MIN_CHUNK_BYTES <= chunk_size <= MAX_CHUNK_BYTES:
        raise ProtocolError("Chunk size is outside the supported range.")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Remote message exceeds the maximum size.")
    body = len(payload).to_bytes(2, "big") + payload
    chunks: list[bytes] = []
    capacity = chunk_size - 1
    offset = 0
    sequence = 0
    while True:
        piece = body[offset : offset + capacity]
        offset += len(piece)
        header = sequence & _SEQUENCE_MASK
        if sequence == 0:
            header |= _CHUNK_START
        if offset >= len(body):
            header |= _CHUNK_END
        chunks.append(bytes([header]) + piece)
        if offset >= len(body):
            return tuple(chunks)
        sequence += 1


class MessageAssembler:
    """Reassemble chunked messages for one connection."""

    def __init__(self) -> None:
        self._expected_sequence = 0
        self._expected_length: int | None = None
        self._buffer = bytearray()

    def reset(self) -> None:
        self._expected_sequence = 0
        self._expected_length = None
        self._buffer.clear()

    def add(self, chunk: bytes) -> bytes | None:
        if not chunk:
            self.reset()
            raise ProtocolError("Remote chunk is empty.")
        header = chunk[0]
        sequence = header & _SEQUENCE_MASK
        piece = chunk[1:]
        if header & _CHUNK_START:
            self.reset()
            if len(piece) < 2:
                raise ProtocolError("Remote message start is missing its length.")
            length = int.from_bytes(piece[:2], "big")
            if length > MAX_MESSAGE_BYTES:
                raise ProtocolError("Remote message exceeds the maximum size.")
            self._expected_length = length
            piece = piece[2:]
        elif self._expected_length is None:
            raise ProtocolError("Remote chunk arrived without a message start.")
        if sequence != self._expected_sequence:
            self.reset()
            raise ProtocolError("Remote chunk sequence is not contiguous.")
        self._expected_sequence = (sequence + 1) & _SEQUENCE_MASK
        self._buffer.extend(piece)
        assert self._expected_length is not None
        if len(self._buffer) > self._expected_length:
            self.reset()
            raise ProtocolError("Remote message is longer than announced.")
        if header & _CHUNK_END:
            if len(self._buffer) != self._expected_length:
                self.reset()
                raise ProtocolError("Remote message ended before its announced length.")
            message = bytes(self._buffer)
            self.reset()
            return message
        return None


@dataclass(frozen=True)
class ShadeState:
    id: str
    name: str
    position: int | None
    battery: int | None
    available: bool
    target: int | None


@dataclass(frozen=True)
class BridgeStatus:
    shades: tuple[ShadeState, ...]
    writes: bool

    def shade(self, shade_id: str) -> ShadeState | None:
        for shade in self.shades:
            if shade.id == shade_id:
                return shade
        return None


@dataclass(frozen=True)
class PairingChallenge:
    shade_id: str
    shade_name: str
    direction: str


@dataclass(frozen=True)
class PairingReply:
    status: str
    code: str | None
    expires_in: int | None
    challenge: PairingChallenge | None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_status(raw: Mapping[str, Any]) -> BridgeStatus:
    shades_raw = raw.get("shades")
    if not isinstance(shades_raw, list):
        raise ProtocolError("Status reply has no shade list.")
    shades = []
    for item in shades_raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise ProtocolError("Status reply has a malformed shade.")
        shades.append(
            ShadeState(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                position=_int(item.get("position")),
                battery=_int(item.get("battery")),
                available=bool(item.get("available")),
                target=_int(item.get("target")),
            )
        )
    return BridgeStatus(shades=tuple(shades), writes=bool(raw.get("writes")))


def parse_pairing(raw: Mapping[str, Any]) -> PairingReply:
    status = raw.get("status")
    if status not in {"pending", "approved", "denied", "expired", "none"}:
        raise ProtocolError("Pairing reply has an unknown status.")
    challenge = None
    challenge_raw = raw.get("challenge")
    if isinstance(challenge_raw, Mapping):
        shade_id = challenge_raw.get("shade")
        direction = challenge_raw.get("direction")
        if isinstance(shade_id, str) and direction in {"up", "down"}:
            challenge = PairingChallenge(
                shade_id=shade_id,
                shade_name=str(challenge_raw.get("name") or shade_id),
                direction=direction,
            )
    code = raw.get("code")
    return PairingReply(
        status=status,
        code=code if isinstance(code, str) else None,
        expires_in=_int(raw.get("expires_in")),
        challenge=challenge,
    )
