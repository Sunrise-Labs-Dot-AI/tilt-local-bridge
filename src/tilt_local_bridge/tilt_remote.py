"""Transport-free protocol for the Bluetooth phone remote.

A phone talks to the bridge over one custom GATT service. Every message is a
small JSON object. Nothing here can scan for, connect to, or write to a
Bluetooth device; the BLE transport lives in ``tilt_remote_ble`` and the shade
operations stay behind the same bridge entry points Home Assistant uses.

Security model, in one paragraph: a phone holds an Ed25519 key. Pairing records
the phone's public key on the bridge, and only after a person approves the
request from somewhere the phone cannot reach on its own (a Home Assistant
button, or an operator signal on the Raspberry Pi). Every later request is
signed over a bridge-issued single-use nonce, so a request cannot be replayed
and an unpaired radio in range cannot move a shade.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
REMOTE_SERVICE_UUID = "4e9a0001-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_REQUEST_UUID = "4e9a0002-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_RESPONSE_UUID = "4e9a0003-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_INFO_UUID = "4e9a0004-3b7c-4f2e-9d61-5c8a2f7b0e10"

SIGNING_PREFIX = b"tilt-remote/1\n"
MAX_MESSAGE_BYTES = 4096
MIN_CHUNK_BYTES = 8
DEFAULT_CHUNK_BYTES = 20
MAX_CHUNK_BYTES = 244
PAIRING_TTL_SECONDS = 120.0
SESSION_IDLE_SECONDS = 600.0
STATUS_PUSH_DELAY_SECONDS = 0.2
# While a request is pending the bridge re-reads the shades this often, so the
# button press the app asked for can approve the phone without a network.
PAIRING_PROBE_SECONDS = 15.0
MANUAL_MOVE_MIN_PERCENT = 5
CHALLENGE_DIRECTIONS = ("up", "down")
MAX_PHONE_NAME_LENGTH = 40
MAX_PAIRED_PHONES = 16

APPROVE_PAIRING_PAYLOAD = "APPROVE_PAIRING"
DENY_PAIRING_PAYLOAD = "DENY_PAIRING"

_CHUNK_START = 0x80
_CHUNK_END = 0x40
_SEQUENCE_MASK = 0x3F
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
_HEX_NONCE = re.compile(r"^[0-9a-f]{32}$")
_HEX_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_HEX_BRIDGE_ID = re.compile(r"^[0-9a-f]{32}$")
_SIGNED_TYPES = frozenset({"pair", "pair_status", "status", "set", "refresh"})


class RemoteProtocolError(RuntimeError):
    """Raised for malformed frames, messages, or unsafe state files."""


# --------------------------------------------------------------------------
# Canonical encoding and signatures
# --------------------------------------------------------------------------


def canonical_json(value: object) -> bytes:
    """Encode JSON the same way the phone does: sorted keys, no whitespace, UTF-8.

    Only objects, arrays, strings, integers, booleans, and null are allowed.
    Floats are rejected because two implementations rarely print them the same
    way, and nothing in this protocol needs one.
    """

    _require_canonical_value(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _require_canonical_value(value: object) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise RemoteProtocolError("Canonical JSON does not allow floating point numbers.")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RemoteProtocolError("Canonical JSON keys must be strings.")
            _require_canonical_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _require_canonical_value(item)
        return
    raise RemoteProtocolError("Value cannot be encoded as canonical JSON.")


def signing_bytes(request: Mapping[str, Any]) -> bytes:
    """Return the bytes a phone signs for one request, excluding its signature."""

    unsigned = {key: value for key, value in request.items() if key != "sig"}
    return SIGNING_PREFIX + canonical_json(unsigned)


def verify_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RemoteProtocolError(
            "The Bluetooth remote requires the cryptography package."
        ) from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_message(seed: bytes, message: bytes) -> bytes:
    """Sign with a 32-byte Ed25519 seed. Used by tests and the reference client."""

    if len(seed) != 32:
        raise RemoteProtocolError("Ed25519 seeds must be exactly 32 bytes.")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def public_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise RemoteProtocolError("Ed25519 seeds must be exactly 32 bytes.")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


# --------------------------------------------------------------------------
# Chunk framing shared by both directions
# --------------------------------------------------------------------------


def chunk_message(payload: bytes, *, chunk_size: int = DEFAULT_CHUNK_BYTES) -> tuple[bytes, ...]:
    """Split one message into BLE-sized chunks.

    Each chunk starts with one header byte: bit 7 marks the first chunk of a
    message, bit 6 the last, and the low six bits count chunks within the
    message. The first chunk then carries the total payload length as two
    big-endian bytes. A receiver restarts assembly on any first-chunk header,
    so a lost or truncated chunk corrupts at most one message.
    """

    if not MIN_CHUNK_BYTES <= chunk_size <= MAX_CHUNK_BYTES:
        raise RemoteProtocolError(
            f"Chunk size must be between {MIN_CHUNK_BYTES} and {MAX_CHUNK_BYTES} bytes."
        )
    if len(payload) > MAX_MESSAGE_BYTES:
        raise RemoteProtocolError("Remote message exceeds the maximum size.")
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

    @property
    def in_progress(self) -> bool:
        return self._expected_length is not None

    def reset(self) -> None:
        self._expected_sequence = 0
        self._expected_length = None
        self._buffer.clear()

    def add(self, chunk: bytes) -> bytes | None:
        """Add one chunk. Return the full payload when a message completes."""

        if len(chunk) < 1:
            self.reset()
            raise RemoteProtocolError("Remote chunk is empty.")
        header = chunk[0]
        sequence = header & _SEQUENCE_MASK
        piece = chunk[1:]
        if header & _CHUNK_START:
            self.reset()
            if len(piece) < 2:
                raise RemoteProtocolError("Remote message start is missing its length.")
            length = int.from_bytes(piece[:2], "big")
            if length > MAX_MESSAGE_BYTES:
                raise RemoteProtocolError("Remote message exceeds the maximum size.")
            self._expected_length = length
            piece = piece[2:]
        elif self._expected_length is None:
            raise RemoteProtocolError("Remote chunk arrived without a message start.")
        if sequence != self._expected_sequence:
            self.reset()
            raise RemoteProtocolError("Remote chunk sequence is not contiguous.")
        self._expected_sequence = (sequence + 1) & _SEQUENCE_MASK
        self._buffer.extend(piece)
        assert self._expected_length is not None
        if len(self._buffer) > self._expected_length:
            self.reset()
            raise RemoteProtocolError("Remote message is longer than announced.")
        if header & _CHUNK_END:
            if len(self._buffer) != self._expected_length:
                self.reset()
                raise RemoteProtocolError("Remote message ended before its announced length.")
            message = bytes(self._buffer)
            self.reset()
            return message
        return None


def chunk_size_for_mtu(mtu: int | None) -> int:
    """Return the largest safe chunk for an ATT MTU, or the conservative default."""

    if mtu is None or mtu < 23:
        return DEFAULT_CHUNK_BYTES
    return max(MIN_CHUNK_BYTES, min(MAX_CHUNK_BYTES, mtu - 3))


# --------------------------------------------------------------------------
# Paired-phone store
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedPhone:
    public_key: str
    name: str
    paired_at: str


class RemotePhoneStore:
    """The bridge identity plus the phones a person approved, on disk."""

    def __init__(self, path: Path, *, wall_clock: Callable[[], datetime] | None = None) -> None:
        if not path.is_absolute():
            raise RemoteProtocolError("Remote state file path must be absolute.")
        self._path = path
        self._wall_clock = wall_clock or _utc_now
        self._bridge_id: str | None = None
        self._phones: dict[str, PairedPhone] = {}
        self._loaded_signature: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def bridge_id(self) -> str:
        self.load()
        assert self._bridge_id is not None
        return self._bridge_id

    def load(self) -> None:
        """Load the state file, creating the bridge identity on first use."""

        try:
            info = self._path.lstat()
        except FileNotFoundError:
            if self._bridge_id is None:
                self._bridge_id = secrets.token_hex(16)
                self._phones = {}
                self._write()
            return
        except OSError as exc:
            raise RemoteProtocolError("Unable to read the remote state file.") from exc
        signature = (info.st_mtime_ns, info.st_size)
        if signature == self._loaded_signature:
            return
        _require_private_regular_file(info, label="remote state")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RemoteProtocolError("Unable to parse the remote state file.") from exc
        self._apply(raw)
        self._loaded_signature = signature

    def _apply(self, raw: object) -> None:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RemoteProtocolError("Remote state file has an unsupported layout.")
        bridge_id = raw.get("bridge_id")
        if not isinstance(bridge_id, str) or not _HEX_BRIDGE_ID.fullmatch(bridge_id):
            raise RemoteProtocolError("Remote state file has an invalid bridge id.")
        phones_raw = raw.get("phones", [])
        if not isinstance(phones_raw, list):
            raise RemoteProtocolError("Remote state file phones must be a list.")
        phones: dict[str, PairedPhone] = {}
        for entry in phones_raw:
            if not isinstance(entry, dict):
                raise RemoteProtocolError("Remote state file phone entry is invalid.")
            key = entry.get("public_key")
            name = entry.get("name")
            paired_at = entry.get("paired_at")
            if (
                not isinstance(key, str)
                or not _HEX_KEY.fullmatch(key)
                or not isinstance(name, str)
                or not isinstance(paired_at, str)
            ):
                raise RemoteProtocolError("Remote state file phone entry is invalid.")
            phones[key] = PairedPhone(public_key=key, name=name, paired_at=paired_at)
        self._bridge_id = bridge_id
        self._phones = phones

    def phones(self) -> tuple[PairedPhone, ...]:
        self.load()
        return tuple(self._phones.values())

    def is_paired(self, public_key: str) -> bool:
        self.load()
        return public_key in self._phones

    def add(self, public_key: str, name: str) -> PairedPhone:
        self.load()
        if not _HEX_KEY.fullmatch(public_key):
            raise RemoteProtocolError("Phone public key must be 64 lowercase hex digits.")
        if len(self._phones) >= MAX_PAIRED_PHONES and public_key not in self._phones:
            raise RemoteProtocolError("Too many paired phones; remove one first.")
        phone = PairedPhone(
            public_key=public_key,
            name=clean_phone_name(name),
            paired_at=self._wall_clock().isoformat(timespec="seconds"),
        )
        self._phones[public_key] = phone
        self._write()
        return phone

    def remove(self, key_prefix: str) -> tuple[PairedPhone, ...]:
        """Remove every phone whose public key starts with the prefix."""

        self.load()
        prefix = key_prefix.strip().lower()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise RemoteProtocolError("Key prefix must be at least 8 hex digits.")
        removed = tuple(
            phone for key, phone in self._phones.items() if key.startswith(prefix)
        )
        for phone in removed:
            del self._phones[phone.public_key]
        if removed:
            self._write()
        return removed

    def _write(self) -> None:
        payload = {
            "version": 1,
            "bridge_id": self._bridge_id,
            "phones": [
                {
                    "public_key": phone.public_key,
                    "name": phone.name,
                    "paired_at": phone.paired_at,
                }
                for phone in self._phones.values()
            ],
        }
        directory = self._path.parent
        temporary = directory / f".{self._path.name}.{os.getpid()}.tmp"
        owner: tuple[int, int] | None = None
        try:
            existing = self._path.lstat()
            owner = (existing.st_uid, existing.st_gid)
        except FileNotFoundError:
            owner = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if owner is not None and os.geteuid() == 0:
                os.chown(temporary, owner[0], owner[1])
            os.replace(temporary, self._path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RemoteProtocolError("Unable to write the remote state file.") from exc
        info = self._path.lstat()
        self._loaded_signature = (info.st_mtime_ns, info.st_size)


def _require_private_regular_file(info: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RemoteProtocolError(f"{label} file must be a regular non-symlink file.")
    if info.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise RemoteProtocolError(
            f"{label} file must not be group-writable or accessible by other users."
        )
    if info.st_uid not in {0, os.geteuid()}:
        raise RemoteProtocolError(f"{label} file has an unexpected owner.")


def check_state_location(path: Path) -> dict[str, object]:
    """Validate the remote state file or its directory without changing either."""

    if not path.is_absolute():
        raise RemoteProtocolError("Remote state file path must be absolute.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        directory = path.parent
        if not directory.is_dir():
            raise RemoteProtocolError("Remote state directory does not exist.")
        if not os.access(directory, os.W_OK | os.X_OK):
            raise RemoteProtocolError("Remote state directory is not writable.")
        return {"state_file_present": False, "paired_phone_count": 0}
    except OSError as exc:
        raise RemoteProtocolError("Unable to read the remote state file.") from exc
    _require_private_regular_file(info, label="remote state")
    store = RemotePhoneStore(path)
    store.load()
    return {"state_file_present": True, "paired_phone_count": len(store.phones())}


def clean_phone_name(value: object) -> str:
    if not isinstance(value, str):
        return "Phone"
    cleaned = "".join(
        character for character in value if character.isprintable()
    ).strip()
    if not cleaned:
        return "Phone"
    return cleaned[:MAX_PHONE_NAME_LENGTH]


# --------------------------------------------------------------------------
# Bridge surface the protocol drives
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadeSnapshot:
    id: str
    name: str
    position_percent: int | None
    battery_percent: int | None
    available: bool
    target_percent: int | None
    age_seconds: int | None
    # Monotonic time of the last position the bridge itself commanded, from
    # any source, so a movement it did not ask for can be told apart.
    commanded_at: float | None = None


class RemoteBridge(Protocol):
    def shade_snapshots(self) -> tuple[ShadeSnapshot, ...]: ...

    def request_position(self, shade_id: str, position_percent: int) -> str: ...

    async def refresh_all(self) -> None: ...

    def publish_pairing_request(self, text: str | None) -> None: ...

    def publish_paired_phone_count(self, count: int) -> None: ...


@dataclass(frozen=True)
class PairingChallenge:
    """The one physical button press that approves a request: a shade and a direction."""

    shade_id: str
    shade_name: str
    direction: str  # "up" opens (position rises), "down" closes (position falls)

    def as_message(self) -> dict[str, str]:
        return {"shade": self.shade_id, "name": self.shade_name, "direction": self.direction}

    def instruction(self) -> str:
        return f"tap {self.direction} on {self.shade_name}"


@dataclass
class PendingPairing:
    public_key: str
    name: str
    code: str
    requested_at: float
    expires_at: float
    # Positions when the request arrived; the button press is judged against these.
    baseline: dict[str, int] = field(default_factory=dict)
    challenge: PairingChallenge | None = None
    last_probe_at: float = 0.0


@dataclass
class RemoteSession:
    connection: str
    public_key: str | None = None
    nonce: bytes | None = None
    last_seen: float = 0.0
    pushes: bool = False


SendCallback = Callable[[str, bytes], Awaitable[None]]


class RemoteProtocol:
    """Handle whole messages for every phone connection."""

    def __init__(
        self,
        store: RemotePhoneStore,
        bridge: RemoteBridge,
        *,
        name: str,
        position_writes_enabled: bool,
        monotonic_clock: Callable[[], float] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
        random_code: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._name = name
        self._position_writes_enabled = position_writes_enabled
        self._monotonic = monotonic_clock or time.monotonic
        self._random_bytes = random_bytes or secrets.token_bytes
        self._random_code = random_code or _random_pairing_code
        self._random_choice: Callable[[Sequence[Any]], Any] = secrets.choice
        self._sessions: dict[str, RemoteSession] = {}
        self._pending: PendingPairing | None = None
        self._last_resolution: dict[str, str] = {}
        self._send: SendCallback | None = None
        self._status_push_handle: asyncio.TimerHandle | None = None
        self._background: set[asyncio.Task[None]] = set()

    # ----- wiring -----

    def set_sender(self, send: SendCallback) -> None:
        self._send = send

    def start(self) -> None:
        self._store.load()
        self._bridge.publish_paired_phone_count(len(self._store.phones()))
        self._bridge.publish_pairing_request(None)

    def info_payload(self) -> bytes:
        return canonical_json(
            {
                "v": PROTOCOL_VERSION,
                "bridge_id": self._store.bridge_id,
                "name": self._name,
            }
        )

    @property
    def pending(self) -> PendingPairing | None:
        return self._pending

    @property
    def sessions(self) -> Mapping[str, RemoteSession]:
        return self._sessions

    # ----- connection lifecycle -----

    def open_connection(self, connection: str) -> RemoteSession:
        session = self._sessions.get(connection)
        if session is None:
            session = RemoteSession(connection=connection)
            self._sessions[connection] = session
        session.last_seen = self._monotonic()
        return session

    def close_connection(self, connection: str) -> None:
        self._sessions.pop(connection, None)

    def tick(self) -> list[Awaitable[None]]:
        """Expire stale state. Returns awaitables the transport should run."""

        now = self._monotonic()
        work: list[Awaitable[None]] = []
        pending = self._pending
        if pending is not None and now >= pending.expires_at:
            self._pending = None
            self._last_resolution[pending.public_key] = "expired"
            self._bridge.publish_pairing_request(None)
            _LOGGER.info("Phone pairing request from %r expired", pending.name)
            work.extend(self._notify_pairing(pending.public_key, "expired"))
        elif pending is not None and now - pending.last_probe_at >= PAIRING_PROBE_SECONDS:
            pending.last_probe_at = now
            work.append(self._probe_for_manual_move())
        for connection, session in list(self._sessions.items()):
            if now - session.last_seen > SESSION_IDLE_SECONDS:
                self._sessions.pop(connection, None)
        return work

    # ----- inbound -----

    async def receive_message(self, connection: str, payload: bytes) -> None:
        session = self.open_connection(connection)
        try:
            request = self._parse_request(payload)
        except RemoteProtocolError as exc:
            await self._reply(session, _error("bad_request", str(exc)))
            return
        request_type = request["t"]
        if request_type == "nonce":
            await self._handle_nonce(session, request)
            return
        if request_type not in _SIGNED_TYPES:
            await self._reply(session, _error("bad_request", "Unknown request type."))
            return
        error = self._authenticate(session, request)
        if error is not None:
            await self._reply(session, error)
            return
        # A valid signature consumes the nonce even if the request is refused.
        next_nonce = self._issue_nonce(session)
        public_key = request["cpk"]
        response = await self._dispatch(session, request_type, request, public_key)
        response["n"] = next_nonce
        await self._reply(session, response)

    def _parse_request(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise RemoteProtocolError("Request is too large.")
        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteProtocolError("Request is not valid JSON.") from exc
        if not isinstance(request, dict) or not isinstance(request.get("t"), str):
            raise RemoteProtocolError("Request must be an object with a string type.")
        public_key = request.get("cpk")
        if not isinstance(public_key, str) or not _HEX_KEY.fullmatch(public_key):
            raise RemoteProtocolError("Request is missing a valid phone public key.")
        return request

    async def _handle_nonce(self, session: RemoteSession, request: dict[str, Any]) -> None:
        public_key = request["cpk"]
        session.public_key = public_key
        nonce = self._issue_nonce(session)
        await self._reply(
            session,
            {
                "t": "nonce",
                "v": PROTOCOL_VERSION,
                "bridge_id": self._store.bridge_id,
                "name": self._name,
                "n": nonce,
                "paired": self._store.is_paired(public_key),
                "to": public_key[:8],
            },
        )

    def _authenticate(
        self, session: RemoteSession, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        public_key = request["cpk"]
        if session.public_key != public_key:
            return _error("nonce", "Request a nonce for this key first.")
        if request.get("bridge_id") != self._store.bridge_id:
            return _error("bridge", "Request was signed for a different bridge.")
        nonce = request.get("n")
        if (
            session.nonce is None
            or not isinstance(nonce, str)
            or not _HEX_NONCE.fullmatch(nonce)
            or not hmac.compare_digest(nonce, session.nonce.hex())
        ):
            return {
                **_error("nonce", "Nonce is missing or stale."),
                "n": self._issue_nonce(session),
            }
        signature = request.get("sig")
        if not isinstance(signature, str) or not _HEX_SIGNATURE.fullmatch(signature):
            return _error("signature", "Signature is missing or malformed.")
        try:
            message = signing_bytes(request)
        except RemoteProtocolError as exc:
            return _error("bad_request", str(exc))
        if not verify_signature(bytes.fromhex(public_key), message, bytes.fromhex(signature)):
            return _error("signature", "Signature did not verify.")
        return None

    async def _dispatch(
        self,
        session: RemoteSession,
        request_type: str,
        request: dict[str, Any],
        public_key: str,
    ) -> dict[str, Any]:
        if request_type == "pair":
            return self._handle_pair(session, request, public_key)
        if request_type == "pair_status":
            return {"t": "pair", "status": self._pairing_status(public_key)}
        if not self._store.is_paired(public_key):
            return {**_error("unauthorized", "This phone is not paired."), "paired": False}
        session.pushes = True
        if request_type == "status":
            return self._status_message()
        if request_type == "refresh":
            self._spawn(self._bridge.refresh_all())
            return {"t": "refresh", "accepted": True}
        if request_type == "set":
            return self._handle_set(request)
        return _error("bad_request", "Unknown request type.")

    def _handle_pair(
        self, session: RemoteSession, request: dict[str, Any], public_key: str
    ) -> dict[str, Any]:
        if self._store.is_paired(public_key):
            return {"t": "pair", "status": "approved"}
        now = self._monotonic()
        pending = self._pending
        if pending is not None and pending.public_key != public_key:
            return {
                **_error("busy", "Another phone is waiting for approval."),
                "retry_in": max(1, int(pending.expires_at - now) + 1),
            }
        if pending is None:
            baseline = self._position_baseline()
            pending = PendingPairing(
                public_key=public_key,
                name=clean_phone_name(request.get("name")),
                code=self._random_code(),
                requested_at=now,
                expires_at=now + PAIRING_TTL_SECONDS,
                baseline=baseline,
                challenge=self._pick_challenge(baseline),
                last_probe_at=now,
            )
            self._pending = pending
            self._last_resolution.pop(public_key, None)
            self._bridge.publish_pairing_request(pairing_request_text(pending))
            _LOGGER.warning(
                "Phone %r asks to pair with code %s. Approve within %d seconds by %s, "
                "from the Home Assistant button, or by sending SIGUSR1 to this service.",
                pending.name,
                pending.code,
                int(PAIRING_TTL_SECONDS),
                pending.challenge.instruction() if pending.challenge else "no shade press (none reachable)",
            )
        return {
            "t": "pair",
            "status": "pending",
            "code": pending.code,
            "expires_in": max(0, int(pending.expires_at - now)),
            "challenge": pending.challenge.as_message() if pending.challenge else None,
        }

    def _position_baseline(self) -> dict[str, int]:
        return {
            shade.id: shade.position_percent
            for shade in self._bridge.shade_snapshots()
            if shade.position_percent is not None and shade.available
        }

    def _pick_challenge(self, baseline: dict[str, int]) -> PairingChallenge | None:
        """Choose one reachable shade and a direction it can still move in."""

        candidates = [
            shade for shade in self._bridge.shade_snapshots()
            if shade.id in baseline and shade.target_percent is None
        ]
        if not candidates:
            return None
        shade = self._random_choice(candidates)
        position = baseline[shade.id]
        if position >= 100 - MANUAL_MOVE_MIN_PERCENT:
            direction = "down"
        elif position <= MANUAL_MOVE_MIN_PERCENT:
            direction = "up"
        else:
            direction = self._random_choice(CHALLENGE_DIRECTIONS)
        return PairingChallenge(shade_id=shade.id, shade_name=shade.name, direction=direction)

    async def _probe_for_manual_move(self) -> None:
        """Re-read the shades and judge any hand movement against the challenge."""

        try:
            await self._bridge.refresh_all()
        except Exception as exc:
            _LOGGER.debug("Pairing probe read failed: %s", type(exc).__name__)
        for work in self._judge_manual_movement():
            await work

    def _judge_manual_movement(self) -> list[Awaitable[None]]:
        verdict = self.manual_movement_since_request()
        if verdict is None:
            return []
        outcome, description = verdict
        if outcome == "approve":
            return self.approve_pending(f"the shade press it asked for ({description})")
        _LOGGER.warning("Pairing denied: %s", description)
        return self.deny_pending(description)

    def manual_movement_since_request(self) -> tuple[str, str] | None:
        """Return ("approve"|"deny", description) for a hand movement, or None.

        Only the shade and direction the app asked for approve. Any other hand
        movement during the window denies, because it is either a mistake worth
        a retry or somebody else's hands.
        """

        pending = self._pending
        if pending is None or pending.challenge is None:
            return None
        challenge = pending.challenge
        for shade in self._bridge.shade_snapshots():
            before = pending.baseline.get(shade.id)
            after = shade.position_percent
            if before is None or after is None:
                continue
            delta = after - before
            if abs(delta) < MANUAL_MOVE_MIN_PERCENT:
                continue
            if shade.target_percent is not None:
                continue
            if shade.commanded_at is not None and shade.commanded_at >= pending.requested_at:
                continue
            direction = "up" if delta > 0 else "down"
            description = f"{shade.name} moved {direction} from {before} to {after}"
            if shade.id == challenge.shade_id and direction == challenge.direction:
                return "approve", description
            return "deny", f"{description}, but the request asked to {challenge.instruction()}"
        return None

    def _pairing_status(self, public_key: str) -> str:
        if self._store.is_paired(public_key):
            return "approved"
        pending = self._pending
        if pending is not None and pending.public_key == public_key:
            return "pending"
        return self._last_resolution.get(public_key, "none")

    def _handle_set(self, request: dict[str, Any]) -> dict[str, Any]:
        shade_id = request.get("shade")
        position = request.get("position")
        if not isinstance(shade_id, str):
            return _error("bad_request", "Set requests need a shade id.")
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 100:
            return _error("invalid_position", "Position must be an integer from 0 to 100.")
        if not self._position_writes_enabled:
            return _error("writes_disabled", "Position writes are disabled on this bridge.")
        outcome = self._bridge.request_position(shade_id, position)
        if outcome == "unknown_shade":
            return _error("unknown_shade", "No configured shade has that id.")
        if outcome == "unavailable":
            return _error("not_available", "The shade is not reachable right now.")
        return {
            "t": "set",
            "shade": shade_id,
            "position": position,
            "outcome": outcome,
        }

    def _status_message(self) -> dict[str, Any]:
        return {
            "t": "status",
            "writes": self._position_writes_enabled,
            "shades": [
                {
                    "id": shade.id,
                    "name": shade.name,
                    "position": shade.position_percent,
                    "battery": shade.battery_percent,
                    "available": shade.available,
                    "target": shade.target_percent,
                    "age": shade.age_seconds,
                }
                for shade in self._bridge.shade_snapshots()
            ],
        }

    # ----- approvals -----

    def approve_pending(self, source: str) -> list[Awaitable[None]]:
        pending = self._pending
        if pending is None:
            _LOGGER.info("Pairing approval from %s ignored: nothing is pending", source)
            return []
        if self._monotonic() >= pending.expires_at:
            return self.tick()
        self._pending = None
        try:
            self._store.add(pending.public_key, pending.name)
        except RemoteProtocolError as exc:
            _LOGGER.error("Unable to record paired phone: %s", exc)
            self._last_resolution[pending.public_key] = "denied"
            self._bridge.publish_pairing_request(None)
            return self._notify_pairing(pending.public_key, "denied")
        self._last_resolution.pop(pending.public_key, None)
        self._bridge.publish_pairing_request(None)
        self._bridge.publish_paired_phone_count(len(self._store.phones()))
        _LOGGER.warning(
            "Phone %r paired after approval from %s (key %s)",
            pending.name,
            source,
            pending.public_key[:8],
        )
        for session in self._sessions.values():
            if session.public_key == pending.public_key:
                session.pushes = True
        return self._notify_pairing(pending.public_key, "approved")

    def deny_pending(self, source: str) -> list[Awaitable[None]]:
        pending = self._pending
        if pending is None:
            return []
        self._pending = None
        self._last_resolution[pending.public_key] = "denied"
        self._bridge.publish_pairing_request(None)
        _LOGGER.warning("Phone %r pairing denied from %s", pending.name, source)
        return self._notify_pairing(pending.public_key, "denied")

    async def handle_bridge_command(self, payload: bytes) -> None:
        try:
            text = payload.decode("ascii").strip()
        except UnicodeDecodeError:
            return
        if text == APPROVE_PAIRING_PAYLOAD:
            work = self.approve_pending("Home Assistant")
        elif text == DENY_PAIRING_PAYLOAD:
            work = self.deny_pending("Home Assistant")
        else:
            return
        for item in work:
            await item

    def _notify_pairing(self, public_key: str, status: str) -> list[Awaitable[None]]:
        message = {"t": "pair", "status": status, "to": public_key[:8]}
        return [
            self._reply(session, dict(message))
            for session in self._sessions.values()
            if session.public_key == public_key
        ]

    # ----- pushes -----

    def notify_status_changed(self, _shade_id: str | None = None) -> None:
        """Called by the bridge whenever a shade status or availability changes."""

        if self._pending is not None:
            for work in self._judge_manual_movement():
                self._spawn(work)
        if not any(session.pushes for session in self._sessions.values()):
            return
        if self._status_push_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._status_push_handle = loop.call_later(
            STATUS_PUSH_DELAY_SECONDS, self._start_status_push
        )

    def _start_status_push(self) -> None:
        self._status_push_handle = None
        self._spawn(self.push_status())

    async def push_status(self) -> None:
        message = self._status_message()
        for session in list(self._sessions.values()):
            if session.pushes and session.public_key is not None:
                if not self._store.is_paired(session.public_key):
                    session.pushes = False
                    continue
                await self._reply(session, dict(message))

    # ----- helpers -----

    def _issue_nonce(self, session: RemoteSession) -> str:
        nonce = self._random_bytes(16)
        session.nonce = nonce
        session.last_seen = self._monotonic()
        return nonce.hex()

    async def _reply(self, session: RemoteSession, message: dict[str, Any]) -> None:
        if "to" not in message and session.public_key is not None:
            message["to"] = session.public_key[:8]
        if self._send is None:
            return
        try:
            await self._send(session.connection, canonical_json(message))
        except Exception as exc:
            _LOGGER.warning(
                "Remote reply to %s failed: %s", session.connection, type(exc).__name__
            )

    def _spawn(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._background.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._background.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.warning("Remote background task failed: %s", type(exc).__name__)

        task.add_done_callback(done)

    async def close(self) -> None:
        if self._status_push_handle is not None:
            self._status_push_handle.cancel()
            self._status_push_handle = None
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._background.clear()


def pairing_request_text(pending: PendingPairing) -> str:
    text = f"{pending.name} (code {pending.code})"
    if pending.challenge is not None:
        text += f": {pending.challenge.instruction()}"
    return text


def _error(code: str, message: str) -> dict[str, Any]:
    return {"t": "error", "code": code, "message": message}


def _random_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
