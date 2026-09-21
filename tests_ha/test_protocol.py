"""The vendored protocol must match the shared golden vectors byte for byte."""

from __future__ import annotations

from custom_components.tilt_bridge.protocol import (
    Identity,
    MessageAssembler,
    canonical_json,
    chunk_message,
    parse_pairing,
    parse_status,
    signing_bytes,
    verify_signature,
)

from .conftest import VECTORS


def test_canonical_vectors() -> None:
    for case in VECTORS["canonical_cases"]:
        assert canonical_json(case["value"]).decode("utf-8") == case["canonical"]


def test_signature_vectors(identity: Identity) -> None:
    assert identity.public_key_hex == VECTORS["public_key_hex"]
    for case in VECTORS["signed_cases"]:
        message = signing_bytes(case["request"])
        assert message.hex() == case["signing_input_hex"]
        signature = identity.sign(message)
        assert signature.hex() == case["signature_hex"]
        assert verify_signature(bytes.fromhex(identity.public_key_hex), message, signature)


def test_framing_vectors() -> None:
    for case in VECTORS["framing_cases"]:
        payload = bytes.fromhex(case["payload_hex"])
        chunks = chunk_message(payload, chunk_size=case["chunk_size"])
        assert [chunk.hex() for chunk in chunks] == case["chunks_hex"]
        assembler = MessageAssembler()
        results = [assembler.add(chunk) for chunk in chunks]
        assert results[-1] == payload


def test_parsers_are_strict() -> None:
    status = parse_status({"t": "status", "writes": True, "shades": [{"id": "door", "name": "Door", "position": 40, "battery": 90, "available": True, "target": None}]})
    assert status.shade("door").position == 40
    pairing = parse_pairing({"status": "pending", "code": "123456", "expires_in": 100, "challenge": {"shade": "door", "name": "Door", "direction": "up"}})
    assert pairing.challenge.direction == "up"
    assert parse_pairing({"status": "approved"}).challenge is None
