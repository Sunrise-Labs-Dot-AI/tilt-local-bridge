"""Offline tests for the Bluetooth phone remote protocol."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tilt_local_bridge.tilt_remote import (
    APPROVE_PAIRING_PAYLOAD,
    DENY_PAIRING_PAYLOAD,
    MAX_MESSAGE_BYTES,
    PAIRING_PROBE_SECONDS,
    PAIRING_TTL_SECONDS,
    SESSION_IDLE_SECONDS,
    MessageAssembler,
    RemotePhoneStore,
    RemoteProtocol,
    RemoteProtocolError,
    ShadeSnapshot,
    canonical_json,
    check_state_location,
    chunk_message,
    chunk_size_for_mtu,
    clean_phone_name,
    public_key_from_seed,
    sign_message,
    signing_bytes,
    verify_signature,
)


ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads(
    (ROOT / "tests" / "fixtures" / "bluetooth_remote_vectors.json").read_text(encoding="utf-8")
)
SEED = bytes.fromhex(VECTORS["seed_hex"])
PUBLIC_KEY = VECTORS["public_key_hex"]
OTHER_SEED = bytes(range(32, 64))
OTHER_PUBLIC_KEY = public_key_from_seed(OTHER_SEED).hex()


class CanonicalJsonTests(unittest.TestCase):
    def test_vectors_match(self) -> None:
        for case in VECTORS["canonical_cases"]:
            with self.subTest(case=case["canonical"]):
                self.assertEqual(
                    canonical_json(case["value"]).decode("utf-8"), case["canonical"]
                )

    def test_floats_are_rejected(self) -> None:
        with self.assertRaises(RemoteProtocolError):
            canonical_json({"position": 50.0})

    def test_non_json_values_are_rejected(self) -> None:
        with self.assertRaises(RemoteProtocolError):
            canonical_json({"raw": b"bytes"})


class SignatureTests(unittest.TestCase):
    def test_public_key_matches_fixture(self) -> None:
        self.assertEqual(public_key_from_seed(SEED).hex(), PUBLIC_KEY)

    def test_signed_vectors_round_trip(self) -> None:
        for case in VECTORS["signed_cases"]:
            with self.subTest(case=case["name"]):
                message = signing_bytes(case["request"])
                self.assertEqual(message.hex(), case["signing_input_hex"])
                signature = sign_message(SEED, message)
                self.assertEqual(signature.hex(), case["signature_hex"])
                self.assertTrue(verify_signature(bytes.fromhex(PUBLIC_KEY), message, signature))
                signed = dict(case["request"], sig=signature.hex())
                self.assertEqual(canonical_json(signed).decode("utf-8"), case["signed_canonical"])

    def test_signature_excludes_only_the_sig_field(self) -> None:
        request = {"t": "status", "cpk": PUBLIC_KEY, "sig": "ff" * 64, "n": "00" * 16}
        self.assertEqual(
            signing_bytes(request),
            b"tilt-remote/1\n" + canonical_json({"t": "status", "cpk": PUBLIC_KEY, "n": "00" * 16}),
        )

    def test_tampered_message_fails(self) -> None:
        message = b"tilt-remote/1\n{}"
        signature = sign_message(SEED, message)
        self.assertFalse(verify_signature(bytes.fromhex(PUBLIC_KEY), message + b" ", signature))
        self.assertFalse(verify_signature(bytes.fromhex(OTHER_PUBLIC_KEY), message, signature))
        self.assertFalse(verify_signature(b"short", message, signature))


class FramingTests(unittest.TestCase):
    def test_vectors_match(self) -> None:
        for case in VECTORS["framing_cases"]:
            with self.subTest(size=case["chunk_size"], length=len(case["payload_hex"]) // 2):
                payload = bytes.fromhex(case["payload_hex"])
                chunks = chunk_message(payload, chunk_size=case["chunk_size"])
                self.assertEqual([chunk.hex() for chunk in chunks], case["chunks_hex"])
                assembler = MessageAssembler()
                results = [assembler.add(chunk) for chunk in chunks]
                self.assertEqual(results[-1], payload)
                self.assertTrue(all(result is None for result in results[:-1]))

    def test_every_chunk_fits_and_flags_are_set(self) -> None:
        payload = bytes(range(256)) * 3
        chunks = chunk_message(payload, chunk_size=20)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertTrue(chunks[0][0] & 0x80)
        self.assertTrue(chunks[-1][0] & 0x40)
        self.assertEqual([chunk[0] & 0x3F for chunk in chunks[:3]], [0, 1, 2])

    def test_start_chunk_resets_a_partial_message(self) -> None:
        assembler = MessageAssembler()
        first = chunk_message(b"abcdefghijklmnopqrstuvwxyz", chunk_size=12)
        second = chunk_message(b"hello", chunk_size=12)
        self.assertIsNone(assembler.add(first[0]))
        self.assertEqual(assembler.add(second[0]), b"hello")

    def test_bad_sequences_and_lengths_are_rejected(self) -> None:
        assembler = MessageAssembler()
        chunks = chunk_message(b"0123456789" * 4, chunk_size=12)
        assembler.add(chunks[0])
        with self.assertRaises(RemoteProtocolError):
            assembler.add(chunks[2])
        with self.assertRaises(RemoteProtocolError):
            MessageAssembler().add(chunks[1])
        with self.assertRaises(RemoteProtocolError):
            MessageAssembler().add(bytes([0xC0, 0xFF, 0xFF]))
        with self.assertRaises(RemoteProtocolError):
            chunk_message(b"x" * (MAX_MESSAGE_BYTES + 1))
        with self.assertRaises(RemoteProtocolError):
            chunk_message(b"x", chunk_size=4)

    def test_chunk_size_follows_mtu(self) -> None:
        self.assertEqual(chunk_size_for_mtu(None), 20)
        self.assertEqual(chunk_size_for_mtu(23), 20)
        self.assertEqual(chunk_size_for_mtu(185), 182)
        self.assertEqual(chunk_size_for_mtu(517), 244)


class PhoneStoreTests(unittest.TestCase):
    def test_creates_private_identity_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "remote.json"
            store = RemotePhoneStore(path)
            bridge_id = store.bridge_id
            self.assertRegex(bridge_id, r"^[0-9a-f]{32}$")
            self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
            self.assertEqual(RemotePhoneStore(path).bridge_id, bridge_id)
            self.assertEqual(check_state_location(path)["paired_phone_count"], 0)

    def test_add_remove_and_reload_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "remote.json"
            store = RemotePhoneStore(path)
            store.add(PUBLIC_KEY, "  James’s iPhone\x00 ")
            self.assertTrue(store.is_paired(PUBLIC_KEY))
            self.assertEqual(store.phones()[0].name, "James’s iPhone")
            other = RemotePhoneStore(path)
            self.assertTrue(other.is_paired(PUBLIC_KEY))
            removed = other.remove(PUBLIC_KEY[:8])
            self.assertEqual([phone.public_key for phone in removed], [PUBLIC_KEY])
            os.utime(path, ns=(1, 1))
            self.assertFalse(store.is_paired(PUBLIC_KEY))
            with self.assertRaises(RemoteProtocolError):
                other.remove("abc")

    def test_rejects_symlink_and_open_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            real = Path(tempdir) / "real.json"
            real.write_text(
                json.dumps({"version": 1, "bridge_id": "ab" * 16, "phones": []}),
                encoding="utf-8",
            )
            real.chmod(0o644)
            with self.assertRaises(RemoteProtocolError):
                RemotePhoneStore(real).load()
            real.chmod(0o600)
            link = Path(tempdir) / "link.json"
            link.symlink_to(real)
            with self.assertRaises(RemoteProtocolError):
                RemotePhoneStore(link).load()
            with self.assertRaises(RemoteProtocolError):
                check_state_location(Path(tempdir) / "missing" / "remote.json")

    def test_rejects_relative_paths_and_bad_layouts(self) -> None:
        with self.assertRaises(RemoteProtocolError):
            RemotePhoneStore(Path("remote.json"))
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "remote.json"
            path.write_text('{"version": 2}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(RemoteProtocolError):
                RemotePhoneStore(path).load()

    def test_phone_names_are_cleaned(self) -> None:
        self.assertEqual(clean_phone_name(None), "Phone")
        self.assertEqual(clean_phone_name("   "), "Phone")
        self.assertEqual(clean_phone_name("x" * 100), "x" * 40)


class FakeBridge:
    def __init__(self) -> None:
        self.snapshots: tuple[ShadeSnapshot, ...] = (
            ShadeSnapshot("door", "Door", 40, 90, True, None, 12),
            ShadeSnapshot("right", "Right", None, None, False, None, None),
        )
        self.requests: list[tuple[str, int]] = []
        self.outcome = "accepted"
        self.refreshes = 0
        self.pairing_texts: list[str | None] = []
        self.counts: list[int] = []

    def shade_snapshots(self) -> tuple[ShadeSnapshot, ...]:
        return self.snapshots

    def request_position(self, shade_id: str, position_percent: int) -> str:
        self.requests.append((shade_id, position_percent))
        if shade_id not in {"door", "right"}:
            return "unknown_shade"
        if shade_id == "right":
            return "unavailable"
        return self.outcome

    async def refresh_all(self) -> None:
        self.refreshes += 1

    def publish_pairing_request(self, text: str | None) -> None:
        self.pairing_texts.append(text)

    def publish_paired_phone_count(self, count: int) -> None:
        self.counts.append(count)


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ProtocolHarness:
    def __init__(self, tempdir: str, *, writes: bool = True) -> None:
        self.store = RemotePhoneStore(Path(tempdir) / "remote.json")
        self.bridge = FakeBridge()
        self.clock = ManualClock()
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.codes = iter(["482913", "111222", "333444"])
        self.protocol = RemoteProtocol(
            self.store,
            self.bridge,
            name="Test Bridge",
            position_writes_enabled=writes,
            monotonic_clock=self.clock,
            random_code=lambda: next(self.codes),
        )
        # Deterministic challenge: the first candidate shade, direction "up".
        self.protocol._random_choice = lambda options: options[0]
        self.protocol.set_sender(self._send)
        self.protocol.start()

    async def _send(self, connection: str, payload: bytes) -> None:
        self.sent.append((connection, json.loads(payload.decode("utf-8"))))

    async def send(self, connection: str, message: dict[str, Any]) -> dict[str, Any]:
        before = len(self.sent)
        await self.protocol.receive_message(connection, canonical_json(message))
        replies = [reply for conn, reply in self.sent[before:] if conn == connection]
        assert replies, "expected a reply"
        return replies[-1]

    async def nonce(self, connection: str, seed: bytes = SEED) -> dict[str, Any]:
        return await self.send(
            connection, {"t": "nonce", "cpk": public_key_from_seed(seed).hex()}
        )

    async def signed(
        self,
        connection: str,
        request: dict[str, Any],
        *,
        nonce: str,
        seed: bytes = SEED,
        bridge_id: str | None = None,
    ) -> dict[str, Any]:
        body = dict(request)
        body["cpk"] = public_key_from_seed(seed).hex()
        body["n"] = nonce
        body["bridge_id"] = bridge_id or self.store.bridge_id
        body["sig"] = sign_message(seed, signing_bytes(body)).hex()
        return await self.send(connection, body)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.harness = ProtocolHarness(self._tempdir.name)

    async def asyncTearDown(self) -> None:
        await self.harness.protocol.close()
        self._tempdir.cleanup()

    async def test_nonce_reports_identity_and_pairing_state(self) -> None:
        reply = await self.harness.nonce("dev_a")
        self.assertEqual(reply["t"], "nonce")
        self.assertEqual(reply["bridge_id"], self.harness.store.bridge_id)
        self.assertEqual(reply["name"], "Test Bridge")
        self.assertFalse(reply["paired"])
        self.assertEqual(reply["to"], PUBLIC_KEY[:8])
        self.assertRegex(reply["n"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            json.loads(self.harness.protocol.info_payload()),
            {"v": 1, "bridge_id": self.harness.store.bridge_id, "name": "Test Bridge"},
        )

    async def test_signed_request_without_nonce_is_refused(self) -> None:
        reply = await self.harness.signed("dev_a", {"t": "status"}, nonce="00" * 16)
        self.assertEqual(reply["code"], "nonce")

    async def test_pairing_flow_with_home_assistant_approval(self) -> None:
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed(
            "dev_a", {"t": "pair", "name": "James’s iPhone"}, nonce=nonce
        )
        self.assertEqual(reply["status"], "pending")
        self.assertEqual(reply["code"], "482913")
        self.assertEqual(reply["expires_in"], int(PAIRING_TTL_SECONDS))
        self.assertEqual(reply["challenge"], {"shade": "door", "name": "Door", "direction": "up"})
        self.assertEqual(
            self.harness.bridge.pairing_texts[-1], "James’s iPhone (code 482913): tap up on Door"
        )
        # Asking again keeps the same code and consumes the rotated nonce.
        again = await self.harness.signed("dev_a", {"t": "pair", "name": "x"}, nonce=reply["n"])
        self.assertEqual(again["code"], "482913")
        status = await self.harness.signed("dev_a", {"t": "pair_status"}, nonce=again["n"])
        self.assertEqual(status["status"], "pending")
        unauthorized = await self.harness.signed("dev_a", {"t": "status"}, nonce=status["n"])
        self.assertEqual(unauthorized["code"], "unauthorized")
        self.assertFalse(unauthorized["paired"])

        await self.harness.protocol.handle_bridge_command(APPROVE_PAIRING_PAYLOAD.encode())
        approved = [m for _c, m in self.harness.sent if m.get("status") == "approved"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["to"], PUBLIC_KEY[:8])
        self.assertTrue(self.harness.store.is_paired(PUBLIC_KEY))
        self.assertEqual(self.harness.bridge.counts[-1], 1)
        self.assertIsNone(self.harness.bridge.pairing_texts[-1])
        self.assertEqual(self.harness.store.phones()[0].name, "James’s iPhone")

        fresh = (await self.harness.nonce("dev_a"))["n"]
        status = await self.harness.signed("dev_a", {"t": "status"}, nonce=fresh)
        self.assertEqual(status["t"], "status")
        self.assertTrue(status["writes"])
        self.assertEqual(status["shades"][0]["id"], "door")
        self.assertEqual(status["shades"][0]["position"], 40)
        self.assertEqual(status["shades"][1]["available"], False)

    async def _request_pairing(self) -> dict[str, Any]:
        nonce = (await self.harness.nonce("dev_a"))["n"]
        return await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=nonce)

    def _door_at(self, position: int, *, target: int | None = None, commanded_at: float | None = None) -> None:
        _door, right = self.harness.bridge.snapshots
        self.harness.bridge.snapshots = (
            ShadeSnapshot("door", "Door", position, 90, True, target, 1, commanded_at),
            right,
        )

    async def test_pressing_the_named_button_approves_without_any_network(self) -> None:
        reply = await self._request_pairing()
        self.assertEqual(reply["challenge"]["direction"], "up")
        self._door_at(70)
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self.assertTrue(self.harness.store.is_paired(PUBLIC_KEY))
        self.assertEqual(self.harness.sent[-1][1], {"t": "pair", "status": "approved", "to": PUBLIC_KEY[:8]})
        self.assertIsNone(self.harness.bridge.pairing_texts[-1])

    async def test_wrong_direction_or_shade_denies(self) -> None:
        reply = await self._request_pairing()
        self._door_at(10)  # asked for up, moved down
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self.assertFalse(self.harness.store.is_paired(PUBLIC_KEY))
        self.assertEqual(self.harness.sent[-1][1]["status"], "denied")
        status = await self.harness.signed("dev_a", {"t": "pair_status"}, nonce=reply["n"])
        self.assertEqual(status["status"], "denied")

    async def test_bridge_commanded_or_tiny_movements_do_not_count(self) -> None:
        await self._request_pairing()
        self._door_at(43)
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self._door_at(90, target=90)
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self._door_at(90, commanded_at=self.harness.clock.now)
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self.assertIsNotNone(self.harness.protocol.pending)
        self.assertFalse(self.harness.store.is_paired(PUBLIC_KEY))

    async def test_pending_request_probes_the_shades_on_a_cadence(self) -> None:
        await self._request_pairing()
        self.assertEqual(self.harness.protocol.tick(), [])
        self.harness.clock.now += PAIRING_PROBE_SECONDS
        work = self.harness.protocol.tick()
        self.assertEqual(len(work), 1)
        self._door_at(80)
        await work[0]
        self.assertEqual(self.harness.bridge.refreshes, 1)
        self.assertTrue(self.harness.store.is_paired(PUBLIC_KEY))

    async def test_challenge_direction_respects_the_ends_of_travel(self) -> None:
        self._door_at(100)
        reply = await self._request_pairing()
        self.assertEqual(reply["challenge"]["direction"], "down")
        self.harness.protocol.deny_pending("test")
        self._door_at(0)
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=nonce)
        self.assertEqual(reply["challenge"]["direction"], "up")

    async def test_no_reachable_shade_leaves_only_the_network_paths(self) -> None:
        self.harness.bridge.snapshots = (ShadeSnapshot("door", "Door", None, None, False, None, None),)
        reply = await self._request_pairing()
        self.assertIsNone(reply["challenge"])
        self.assertEqual(self.harness.bridge.pairing_texts[-1], "A (code 482913)")
        self.harness.bridge.snapshots = (ShadeSnapshot("door", "Door", 50, 90, True, None, 1),)
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.01)
        self.assertFalse(self.harness.store.is_paired(PUBLIC_KEY))

    async def test_second_phone_is_told_to_wait(self) -> None:
        nonce_a = (await self.harness.nonce("dev_a"))["n"]
        await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=nonce_a)
        nonce_b = (await self.harness.nonce("dev_b", OTHER_SEED))["n"]
        reply = await self.harness.signed(
            "dev_b", {"t": "pair", "name": "B"}, nonce=nonce_b, seed=OTHER_SEED
        )
        self.assertEqual(reply["code"], "busy")
        self.assertGreater(reply["retry_in"], 0)

    async def test_pending_request_expires_and_denial_is_reported(self) -> None:
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=nonce)
        self.harness.clock.now += PAIRING_TTL_SECONDS + 1
        for work in self.harness.protocol.tick():
            await work
        self.assertIsNone(self.harness.protocol.pending)
        self.assertEqual(self.harness.sent[-1][1]["status"], "expired")
        status = await self.harness.signed("dev_a", {"t": "pair_status"}, nonce=reply["n"])
        self.assertEqual(status["status"], "expired")
        self.assertEqual(self.harness.protocol.approve_pending("SIGUSR1"), [])

        again = await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=status["n"])
        self.assertEqual(again["status"], "pending")
        await self.harness.protocol.handle_bridge_command(DENY_PAIRING_PAYLOAD.encode())
        self.assertEqual(self.harness.sent[-1][1]["status"], "denied")
        self.assertFalse(self.harness.store.is_paired(PUBLIC_KEY))

    async def test_operator_signal_approves_pending_request(self) -> None:
        nonce = (await self.harness.nonce("dev_a"))["n"]
        await self.harness.signed("dev_a", {"t": "pair", "name": "A"}, nonce=nonce)
        for work in self.harness.protocol.approve_pending("SIGUSR1"):
            await work
        self.assertTrue(self.harness.store.is_paired(PUBLIC_KEY))

    async def test_replay_stale_nonce_and_bad_signature_are_refused(self) -> None:
        self.harness.store.add(PUBLIC_KEY, "A")
        nonce = (await self.harness.nonce("dev_a"))["n"]
        request = {"t": "status", "cpk": PUBLIC_KEY, "n": nonce, "bridge_id": self.harness.store.bridge_id}
        request["sig"] = sign_message(SEED, signing_bytes(request)).hex()
        first = await self.harness.send("dev_a", request)
        self.assertEqual(first["t"], "status")
        replay = await self.harness.send("dev_a", request)
        self.assertEqual(replay["code"], "nonce")
        self.assertRegex(replay["n"], r"^[0-9a-f]{32}$")
        forged = dict(request, n=replay["n"])
        forged_reply = await self.harness.send("dev_a", forged)
        self.assertEqual(forged_reply["code"], "signature")
        wrong_bridge = await self.harness.signed(
            "dev_a", {"t": "status"}, nonce=replay["n"], bridge_id="ff" * 16
        )
        self.assertEqual(wrong_bridge["code"], "bridge")
        other_key = await self.harness.signed(
            "dev_a", {"t": "status"}, nonce=replay["n"], seed=OTHER_SEED
        )
        self.assertEqual(other_key["code"], "nonce")

    async def test_set_and_refresh_reach_the_bridge(self) -> None:
        self.harness.store.add(PUBLIC_KEY, "A")
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "door", "position": 50}, nonce=nonce
        )
        self.assertEqual(reply["t"], "set")
        self.assertEqual(reply["outcome"], "accepted")
        self.assertEqual(self.harness.bridge.requests, [("door", 50)])
        unavailable = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "right", "position": 50}, nonce=reply["n"]
        )
        self.assertEqual(unavailable["code"], "not_available")
        unknown = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "nope", "position": 50}, nonce=unavailable["n"]
        )
        self.assertEqual(unknown["code"], "unknown_shade")
        invalid = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "door", "position": 101}, nonce=unknown["n"]
        )
        self.assertEqual(invalid["code"], "invalid_position")
        boolean = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "door", "position": True}, nonce=invalid["n"]
        )
        self.assertEqual(boolean["code"], "invalid_position")
        refresh = await self.harness.signed("dev_a", {"t": "refresh"}, nonce=boolean["n"])
        self.assertTrue(refresh["accepted"])
        await asyncio.sleep(0)
        self.assertEqual(self.harness.bridge.refreshes, 1)

    async def test_writes_disabled_is_reported_without_touching_the_bridge(self) -> None:
        await self.harness.protocol.close()
        self.harness = ProtocolHarness(self._tempdir.name, writes=False)
        self.harness.store.add(PUBLIC_KEY, "A")
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed(
            "dev_a", {"t": "set", "shade": "door", "position": 50}, nonce=nonce
        )
        self.assertEqual(reply["code"], "writes_disabled")
        self.assertEqual(self.harness.bridge.requests, [])
        status = await self.harness.signed("dev_a", {"t": "status"}, nonce=reply["n"])
        self.assertFalse(status["writes"])

    async def test_status_pushes_go_only_to_paired_sessions(self) -> None:
        self.harness.store.add(PUBLIC_KEY, "A")
        nonce = (await self.harness.nonce("dev_a"))["n"]
        await self.harness.signed("dev_a", {"t": "status"}, nonce=nonce)
        await self.harness.nonce("dev_b", OTHER_SEED)
        before = len(self.harness.sent)
        self.harness.protocol.notify_status_changed("door")
        self.harness.protocol.notify_status_changed("door")
        await asyncio.sleep(0.35)
        pushed = self.harness.sent[before:]
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], "dev_a")
        self.assertEqual(pushed[0][1]["t"], "status")

    async def test_bad_requests_get_a_bad_request_error(self) -> None:
        reply = await self.harness.send("dev_a", {"t": "nonce"})
        self.assertEqual(reply["code"], "bad_request")
        await self.harness.protocol.receive_message("dev_a", b"not json")
        self.assertEqual(self.harness.sent[-1][1]["code"], "bad_request")
        nonce = (await self.harness.nonce("dev_a"))["n"]
        reply = await self.harness.signed("dev_a", {"t": "dance"}, nonce=nonce)
        self.assertEqual(reply["code"], "bad_request")

    async def test_idle_sessions_are_evicted(self) -> None:
        await self.harness.nonce("dev_a")
        self.assertIn("dev_a", self.harness.protocol.sessions)
        self.harness.clock.now += SESSION_IDLE_SECONDS + 1
        self.harness.protocol.tick()
        self.assertNotIn("dev_a", self.harness.protocol.sessions)
        await self.harness.nonce("dev_b")
        self.harness.protocol.close_connection("dev_b")
        self.assertNotIn("dev_b", self.harness.protocol.sessions)


if __name__ == "__main__":
    unittest.main()
