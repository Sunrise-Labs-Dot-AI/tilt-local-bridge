#!/usr/bin/env python3
"""Reference command-line client for the Bluetooth phone remote.

Speaks the same protocol as the phone app through bleak, so a laptop can pair
with a bridge, read status, and set a position. Handy for checking a bridge
before installing the app. The client key lives in a private file and is never
printed in full.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tilt_local_bridge.tilt_remote import (  # noqa: E402
    MessageAssembler,
    REMOTE_INFO_UUID,
    REMOTE_REQUEST_UUID,
    REMOTE_RESPONSE_UUID,
    REMOTE_SERVICE_UUID,
    canonical_json,
    chunk_message,
    chunk_size_for_mtu,
    public_key_from_seed,
    sign_message,
    signing_bytes,
)

DEFAULT_KEY_FILE = Path.home() / ".config" / "tilt-local-bridge" / "remote-client.key"


def load_or_create_seed(path: Path) -> bytes:
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        if len(value) != 64:
            raise SystemExit(f"{path} does not hold a 32-byte hex seed.")
        return bytes.fromhex(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(seed.hex() + "\n")
    return seed


class RemoteClient:
    def __init__(self, client: Any, seed: bytes, *, verbose: bool = False) -> None:
        self._client = client
        self._seed = seed
        self._public_key = public_key_from_seed(seed).hex()
        self._assembler = MessageAssembler()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._nonce: str | None = None
        self._bridge_id: str | None = None
        self._verbose = verbose

    @property
    def public_key(self) -> str:
        return self._public_key

    async def start(self) -> None:
        await self._client.start_notify(REMOTE_RESPONSE_UUID, self._on_notify)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        try:
            message = self._assembler.add(bytes(data))
        except Exception as exc:
            if self._verbose:
                print(f"dropped chunk: {exc}", file=sys.stderr)
            return
        if message is None:
            return
        try:
            parsed = json.loads(message.decode("utf-8"))
        except ValueError:
            return
        if parsed.get("to") not in (None, self._public_key[:8]):
            return
        self._inbox.put_nowait(parsed)

    async def info(self) -> dict[str, Any]:
        raw = await self._client.read_gatt_char(REMOTE_INFO_UUID)
        return json.loads(bytes(raw).decode("utf-8"))

    async def _send(self, message: dict[str, Any]) -> None:
        payload = canonical_json(message)
        mtu = getattr(self._client, "mtu_size", None)
        for chunk in chunk_message(payload, chunk_size=chunk_size_for_mtu(mtu)):
            await self._client.write_gatt_char(REMOTE_REQUEST_UUID, chunk, response=True)

    async def _reply(self, timeout: float = 10.0) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
            if "n" in message or message.get("t") == "error":
                if "n" in message:
                    self._nonce = message["n"]
                return message
            print(json.dumps({"push": message}, sort_keys=True))

    async def nonce(self) -> dict[str, Any]:
        await self._send({"t": "nonce", "cpk": self._public_key})
        reply = await self._reply()
        if reply.get("t") != "nonce":
            raise SystemExit(f"unexpected reply: {reply}")
        self._bridge_id = reply["bridge_id"]
        return reply

    async def signed(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._nonce is None or self._bridge_id is None:
            await self.nonce()
        request = dict(body)
        request["cpk"] = self._public_key
        request["n"] = self._nonce
        request["bridge_id"] = self._bridge_id
        request["sig"] = sign_message(self._seed, signing_bytes(request)).hex()
        await self._send(request)
        return await self._reply()

    async def pushes(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                message = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            print(json.dumps({"push": message}, sort_keys=True))


async def find_bridge(name: str | None, timeout: float) -> Any:
    from bleak import BleakScanner
    from bleak.exc import BleakError

    seen_names: dict[str, str] = {}

    def matches(device: Any, advertisement: Any) -> bool:
        uuids = [u.lower() for u in (advertisement.service_uuids or [])]
        if REMOTE_SERVICE_UUID not in uuids:
            return False
        advertised = advertisement.local_name or device.name
        if advertised:
            seen_names[device.address] = advertised
        if name and advertised != name:
            return False
        return True

    # On macOS the first CoreBluetooth manager in a process can report its
    # state a moment late, which bleak surfaces as "turned off" even while the
    # radio is on (and while the privacy prompt is still on screen). A few
    # patient retries cover that; a genuinely disabled radio still fails.
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            device = await BleakScanner.find_device_by_filter(matches, timeout=timeout)
            break
        except BleakError as exc:
            last_error = exc
            if "turned off" not in str(exc) or attempt == 3:
                raise SystemExit(f"Bluetooth is not usable here: {exc}") from exc
            print(f"Bluetooth not ready yet ({exc}); retrying in 3 s", file=sys.stderr)
            await asyncio.sleep(3.0)
    else:  # pragma: no cover - loop always breaks or raises
        raise SystemExit(str(last_error))
    if device is None:
        raise SystemExit("No bridge advertising the remote service was found.")
    # macOS often leaves device.name empty even when the advertisement carried
    # a name; keep what the scan actually showed for the connection banner.
    if not device.name and device.address in seen_names:
        try:
            device.name = seen_names[device.address]
        except AttributeError:
            pass
    return device


async def run(args: argparse.Namespace) -> int:
    from bleak import BleakClient

    seed = load_or_create_seed(args.key_file)
    device = await find_bridge(args.name, args.scan_timeout)
    async with BleakClient(device, timeout=15.0) as client:
        remote = RemoteClient(client, seed, verbose=args.verbose)
        await remote.start()
        print(json.dumps({"connected": str(device.name), "key_prefix": remote.public_key[:8]}))
        if args.command == "info":
            print(json.dumps(await remote.info(), sort_keys=True))
            return 0
        nonce = await remote.nonce()
        print(json.dumps({"bridge": nonce["name"], "paired": nonce["paired"]}, sort_keys=True))
        if args.command == "pair":
            reply = await remote.signed({"t": "pair", "name": args.device_name})
            print(json.dumps(reply, sort_keys=True))
            deadline = time.monotonic() + args.wait
            while reply.get("status") == "pending" and time.monotonic() < deadline:
                await asyncio.sleep(3.0)
                reply = await remote.signed({"t": "pair_status"})
                print(json.dumps(reply, sort_keys=True))
            return 0 if reply.get("status") == "approved" else 1
        if args.command == "status":
            print(json.dumps(await remote.signed({"t": "status"}), sort_keys=True))
            if args.watch:
                await remote.pushes(args.watch)
            return 0
        if args.command == "refresh":
            print(json.dumps(await remote.signed({"t": "refresh"}), sort_keys=True))
            await remote.pushes(args.watch or 20.0)
            return 0
        if args.command == "set":
            reply = await remote.signed(
                {"t": "set", "shade": args.shade, "position": args.position}
            )
            print(json.dumps(reply, sort_keys=True))
            if args.watch:
                await remote.pushes(args.watch)
            return 0 if reply.get("t") == "set" else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", help="Only connect to a bridge advertising this name.")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="Read the unauthenticated info characteristic.")
    pair = commands.add_parser("pair", help="Ask to pair and wait for approval.")
    pair.add_argument("--device-name", default=f"{socket.gethostname()} (laptop)")
    pair.add_argument("--wait", type=float, default=120.0)
    status = commands.add_parser("status", help="Read cached shade status.")
    status.add_argument("--watch", type=float, default=0.0, help="Seconds to print pushes.")
    refresh = commands.add_parser("refresh", help="Ask the bridge to re-read every shade.")
    refresh.add_argument("--watch", type=float, default=0.0)
    set_position = commands.add_parser("set", help="Request one absolute position.")
    set_position.add_argument("--shade", required=True)
    set_position.add_argument("--position", type=int, required=True)
    set_position.add_argument("--watch", type=float, default=0.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "set" and not 0 <= args.position <= 100:
        raise SystemExit("--position must be 0 to 100")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
