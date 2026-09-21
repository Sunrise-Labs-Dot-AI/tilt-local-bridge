#!/usr/bin/env python3
"""Regenerate the shared Bluetooth-remote golden vectors.

The phone app and the bridge are tested against the same bytes, so a change to
canonical encoding, signing, or chunk framing shows up on both sides. Run this
after an intentional protocol change and commit the fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tilt_local_bridge.tilt_remote import (  # noqa: E402
    PROTOCOL_VERSION,
    canonical_json,
    chunk_message,
    public_key_from_seed,
    sign_message,
    signing_bytes,
)

FIXTURE = ROOT / "tests" / "fixtures" / "bluetooth_remote_vectors.json"
SEED = bytes(range(32))
BRIDGE_ID = "0123456789abcdef0123456789abcdef"
NONCE = "00112233445566778899aabbccddeeff"


def main() -> int:
    public_key = public_key_from_seed(SEED).hex()
    requests = {
        "pair": {
            "t": "pair",
            "cpk": public_key,
            "n": NONCE,
            "bridge_id": BRIDGE_ID,
            "name": "James’s iPhone",
        },
        "set": {
            "t": "set",
            "cpk": public_key,
            "n": NONCE,
            "bridge_id": BRIDGE_ID,
            "shade": "office_shade",
            "position": 50,
        },
        "status": {
            "t": "status",
            "cpk": public_key,
            "n": NONCE,
            "bridge_id": BRIDGE_ID,
        },
    }
    signed_cases = []
    for name, request in requests.items():
        message = signing_bytes(request)
        signature = sign_message(SEED, message)
        signed = dict(request)
        signed["sig"] = signature.hex()
        signed_cases.append(
            {
                "name": name,
                "request": request,
                "signing_input_hex": message.hex(),
                "signature_hex": signature.hex(),
                "signed_canonical": canonical_json(signed).decode("utf-8"),
            }
        )
    canonical_cases = [
        {
            "value": {"b": 1, "a": [True, None, "é’"], "c": {"z": "x", "y": 2}},
            "canonical": canonical_json(
                {"b": 1, "a": [True, None, "é’"], "c": {"z": "x", "y": 2}}
            ).decode("utf-8"),
        },
        {
            "value": {"t": "nonce", "cpk": public_key, "quote": 'say "hi"\n'},
            "canonical": canonical_json(
                {"t": "nonce", "cpk": public_key, "quote": 'say "hi"\n'}
            ).decode("utf-8"),
        },
    ]
    long_payload = canonical_json({"t": "status", "shades": [{"id": f"shade_{i}", "position": i} for i in range(6)]})
    framing_cases = []
    for payload, chunk_size in ((b'{"t":"nonce"}', 20), (long_payload, 20), (long_payload, 182)):
        framing_cases.append(
            {
                "payload_hex": payload.hex(),
                "chunk_size": chunk_size,
                "chunks_hex": [chunk.hex() for chunk in chunk_message(payload, chunk_size=chunk_size)],
            }
        )
    fixture = {
        "protocol_version": PROTOCOL_VERSION,
        "seed_hex": SEED.hex(),
        "public_key_hex": public_key,
        "bridge_id": BRIDGE_ID,
        "nonce_hex": NONCE,
        "signing_prefix_hex": b"tilt-remote/1\n".hex(),
        "canonical_cases": canonical_cases,
        "signed_cases": signed_cases,
        "framing_cases": framing_cases,
    }
    FIXTURE.write_text(json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
