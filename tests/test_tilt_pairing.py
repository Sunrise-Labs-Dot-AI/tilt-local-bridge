"""Offline tests for the isolated Tilt shade pairing utility."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from tilt_local_bridge.tilt_pairing import (
    NackReason,
    PairingCryptoCommand,
    PairingKeyAmbiguousError,
    PairingNack,
    ServerPairingKeys,
    TiltCloudError,
    TiltPairingCloudClient,
    TiltPairingError,
    _PrivateKeyStager,
    _TiltPairingGattSession,
    _encode_pairing_crypto_request,
    _parse_pairing_crypto_response,
    authenticate_tilt,
    complete_pairing,
    discover_pairing_shade,
    main,
)
from tilt_local_bridge.tilt_protocol import crc16


MAC = "02:00:00:00:00:01"
TEMPORARY_KEY = bytes(range(32))
PLAIN_KEY = bytes(reversed(range(32)))
NONCE = bytes(range(13))
PACKAGE = bytes(range(48))


def _response(command: int, payload: bytes) -> bytes:
    header = b"\x80\x00"
    body = bytes([command]) + payload
    return header + body + crc16(header + body).to_bytes(2, "little")


class _HttpResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class _FakeCloud:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def register_device(self, mac: str, ecdh_key: bytes) -> None:
        self.calls.append(("register", (mac, ecdh_key)))

    def request_challenge(self, mac: str, key: bytes, token: bytes) -> bytes:
        self.calls.append(("challenge", (mac, key, token)))
        return b"c" * 32

    def request_pairing_keys(self, mac: str, signed: bytes) -> ServerPairingKeys:
        self.calls.append(("keys", (mac, signed)))
        return ServerPairingKeys(PLAIN_KEY, NONCE, PACKAGE)


class _FakeTransport:
    def __init__(self, *, initially_not_ready: bool = False, fail_install: bool = False) -> None:
        self.initially_not_ready = initially_not_ready
        self.fail_install = fail_install
        self.calls: list[tuple[str, object]] = []
        self.auth_attempts = 0

    async def negotiate_protocol(self) -> int:
        self.calls.append(("negotiate", None))
        return 2

    async def request_pairing_auth_token(self, signed_request: bytes) -> bytes:
        self.calls.append(("auth", signed_request))
        self.auth_attempts += 1
        if self.initially_not_ready and self.auth_attempts == 1:
            raise PairingNack(
                PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN, NackReason.NOT_READY
            )
        return b"a" * 32

    async def get_ecdh_key(self) -> bytes:
        self.calls.append(("ecdh", None))
        return b"e" * 64

    async def sign_pairing_token(self, challenge: bytes) -> bytes:
        self.calls.append(("sign", challenge))
        return b"s" * 32

    async def set_pairing_key(self, encrypted_key: bytes, nonce: bytes) -> None:
        self.calls.append(("set", (encrypted_key, nonce)))
        if self.fail_install:
            raise RuntimeError("injected failure")


class PairingCodecTests(unittest.TestCase):
    def test_pairing_request_has_fixed_wire_vector(self) -> None:
        frame = _encode_pairing_crypto_request(
            PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN, bytes(range(32))
        )
        self.assertEqual(frame[:3], b"\x80\x00\x08")
        self.assertEqual(frame.hex(), "800008000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1fe8fc")

    def test_nack_is_structured(self) -> None:
        frame = _response(
            PairingCryptoCommand.NACK,
            bytes([PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN, NackReason.NOT_READY]),
        )
        with self.assertRaises(PairingNack) as raised:
            _parse_pairing_crypto_response(frame)
        self.assertEqual(raised.exception.command, PairingCryptoCommand.REQUEST_PAIRING_AUTH_TOKEN)
        self.assertEqual(raised.exception.reason, NackReason.NOT_READY)

    def test_unsafe_pairing_payload_lengths_are_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "exactly 61"):
            _encode_pairing_crypto_request(PairingCryptoCommand.SET_PAIRING_KEY, b"short")


class PairingTransportTimingTests(unittest.TestCase):
    def test_protocol_negotiation_matches_app_throttle_timing(self) -> None:
        session = _TiltPairingGattSession(object())
        session._send = AsyncMock(
            side_effect=[
                _response(PairingCryptoCommand.REQUEST_PROTOCOL_VERSIONS, b"\x02\x02\x01"),
                _response(PairingCryptoCommand.SELECT_PROTOCOL_VERSION, b"\x02"),
            ]
        )

        with patch("tilt_local_bridge.tilt_pairing.asyncio.sleep", new_callable=AsyncMock) as sleep:
            selected = asyncio.run(session.negotiate_protocol())

        self.assertEqual(selected, 2)
        self.assertEqual(sleep.await_args_list, [call(0.1), call(0.1), call(0.1)])

    def test_key_install_allows_bounded_ble_chunk_retry(self) -> None:
        session = _TiltPairingGattSession(object())
        session._send = AsyncMock(
            return_value=_response(
                PairingCryptoCommand.ACK, bytes([PairingCryptoCommand.SET_PAIRING_KEY])
            )
        )

        asyncio.run(session.set_pairing_key(PACKAGE, NONCE))

        _frame, kwargs = session._send.await_args
        self.assertNotEqual(kwargs.get("retry_chunks"), False)


class PairingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.output = Path(self.temporary_directory.name) / "shade.key"

    def test_existing_server_record_pairs_and_commits_key(self) -> None:
        transport = _FakeTransport()
        cloud = _FakeCloud()
        result = asyncio.run(
            complete_pairing(
                transport,
                cloud,
                MAC,
                _PrivateKeyStager(self.output, replace_existing=False),
                temporary_key=TEMPORARY_KEY,
            )
        )
        self.assertEqual(result.mac, MAC)
        self.assertEqual(self.output.read_text(), PLAIN_KEY.hex() + "\n")
        self.assertNotIn("register", [name for name, _ in cloud.calls])
        self.assertEqual([name for name, _ in transport.calls], ["negotiate", "auth", "sign", "set"])

    def test_unregistered_device_posts_ecdh_then_retries(self) -> None:
        transport = _FakeTransport(initially_not_ready=True)
        cloud = _FakeCloud()
        asyncio.run(
            complete_pairing(
                transport,
                cloud,
                MAC,
                _PrivateKeyStager(self.output, replace_existing=False),
                temporary_key=TEMPORARY_KEY,
            )
        )
        self.assertEqual(
            [name for name, _ in transport.calls],
            ["negotiate", "auth", "ecdh", "auth", "sign", "set"],
        )
        self.assertEqual(cloud.calls[0][0], "register")

    def test_install_failure_preserves_candidate_key_without_replacing_final(self) -> None:
        self.output.write_text("11" * 32 + "\n", encoding="ascii")
        self.output.chmod(0o600)
        transport = _FakeTransport(fail_install=True)
        with self.assertRaises(PairingKeyAmbiguousError) as raised:
            asyncio.run(
                complete_pairing(
                    transport,
                    _FakeCloud(),
                    MAC,
                    _PrivateKeyStager(self.output, replace_existing=True),
                    temporary_key=TEMPORARY_KEY,
                )
            )
        self.assertEqual(self.output.read_text(), "11" * 32 + "\n")
        self.assertTrue(raised.exception.staged_path.exists())
        self.assertEqual(raised.exception.staged_path.read_text(), PLAIN_KEY.hex() + "\n")
        self.assertEqual(raised.exception.target_mac, MAC)


class CloudClientTests(unittest.TestCase):
    def test_authentication_uses_password_realm_without_logging_secret(self) -> None:
        requests = []

        def opener(request, *, timeout):
            requests.append((request, timeout))
            return _HttpResponse({"access_token": "token-value"})

        token = authenticate_tilt("person@example.com", "secret", opener=opener)
        self.assertEqual(token, "token-value")
        body = json.loads(requests[0][0].data)
        self.assertEqual(body["realm"], "Username-Password-Authentication")
        self.assertEqual(body["password"], "secret")

    def test_pairing_endpoints_use_hex_and_validate_lengths(self) -> None:
        responses = iter(
            [
                _HttpResponse({"token": "AA" * 32}),
                _HttpResponse(
                    {"key": "11" * 32, "nonce": "22" * 13, "package": "33" * 48}
                ),
            ]
        )
        requests = []

        def opener(request, *, timeout):
            requests.append(request)
            return next(responses)

        client = TiltPairingCloudClient("access", opener=opener)
        self.assertEqual(client.request_challenge(MAC, b"k" * 32, b"t" * 32), b"\xaa" * 32)
        keys = client.request_pairing_keys(MAC, b"s" * 32)
        self.assertEqual(keys.nonce, b"\x22" * 13)
        self.assertIn("devices/02%3A00%3A00%3A00%3A00%3A01/token", requests[0].full_url)
        self.assertEqual(json.loads(requests[0].data)["key"], (b"k" * 32).hex().upper())

    def test_http_error_body_is_not_exposed(self) -> None:
        def opener(_request, *, timeout):
            raise urllib.error.HTTPError("url", 403, "denied secret-body", {}, io.BytesIO())

        client = TiltPairingCloudClient("access", opener=opener)
        with self.assertRaises(TiltCloudError) as raised:
            client.request_challenge(MAC, b"k" * 32, b"t" * 32)
        self.assertNotIn("secret-body", str(raised.exception))


class CliGateTests(unittest.TestCase):
    def test_pairing_refuses_before_credentials_or_network_without_permission(self) -> None:
        stderr = io.StringIO()
        with (
            patch("tilt_local_bridge.tilt_pairing.input") as prompt,
            patch("tilt_local_bridge.tilt_pairing.getpass.getpass") as get_password,
            patch("tilt_local_bridge.tilt_pairing.authenticate_tilt") as authenticate,
            contextlib.redirect_stderr(stderr),
        ):
            result = main(["--output", "/tmp/unused-tilt.key"])

        self.assertEqual(result, 2)
        self.assertIn("--permit-live-pairing is required", stderr.getvalue())
        prompt.assert_not_called()
        get_password.assert_not_called()
        authenticate.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_only_pairing_marker_is_selected(self) -> None:
        class Device:
            def __init__(self, address):
                self.address = address

        class Advertisement:
            def __init__(self, service_data):
                self.service_data = service_data

        class Scanner:
            def __init__(self, detection_callback, **_kwargs):
                self.detection_callback = detection_callback

            async def __aenter__(self):
                self.detection_callback(Device("AA:BB:CC:DD:EE:01"), Advertisement({}))
                self.detection_callback(
                    Device(MAC),
                    Advertisement({"00001add-0000-1000-8000-00805f9b34fb": b""}),
                )
                return self

            async def __aexit__(self, *_args):
                return None

        selected = asyncio.run(
            discover_pairing_shade(timeout_seconds=1, scanner_factory=Scanner)
        )
        self.assertEqual(selected.address, MAC)

    def test_default_scanner_factory_is_instantiated_with_detection_callback(self) -> None:
        constructed = []

        class Scanner:
            def __init__(self, detection_callback, service_uuids):
                constructed.append(tuple(service_uuids))
                self.detection_callback = detection_callback

            async def __aenter__(self):
                device = type("Device", (), {"address": MAC})()
                advertisement = type(
                    "Advertisement",
                    (),
                    {"service_data": {"00001add-0000-1000-8000-00805f9b34fb": b""}},
                )()
                self.detection_callback(device, advertisement)
                return self

            async def __aexit__(self, *_args):
                return None

        with patch("tilt_local_bridge.tilt_pairing._default_scanner_factory", return_value=Scanner):
            selected = asyncio.run(discover_pairing_shade(timeout_seconds=1))

        self.assertEqual(selected.address, MAC)
        self.assertEqual(len(constructed), 1)

    def test_multiple_pairing_shades_fail_closed(self) -> None:
        class Scanner:
            def __init__(self, detection_callback, **_kwargs):
                self.detection_callback = detection_callback

            async def __aenter__(self):
                advertisement = type(
                    "Advertisement",
                    (),
                    {"service_data": {"00001add-0000-1000-8000-00805f9b34fb": b""}},
                )()
                first = type("Device", (), {"address": MAC})()
                second = type("Device", (), {"address": "AA:BB:CC:DD:EE:02"})()
                self.detection_callback(first, advertisement)
                self.detection_callback(second, advertisement)
                return self

            async def __aexit__(self, *_args):
                return None

        with self.assertRaises(TiltPairingError):
            asyncio.run(discover_pairing_shade(timeout_seconds=1, scanner_factory=Scanner))


if __name__ == "__main__":
    unittest.main()
