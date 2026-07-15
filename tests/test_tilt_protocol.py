"""Offline tests for the allowlisted Tilt BLE protocol codec."""

from __future__ import annotations

import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tilt_local_bridge.tilt_protocol import (
    ApplicationResponse,
    AuthenticationError,
    BleMessageAssembler,
    CryptoCommand,
    ShadeCommand,
    TiltProtocolError,
    UnsafeCommandError,
    chunk_for_ble,
    crc16,
    decode_application_response,
    encode_nonce_request,
    encode_position_request,
    encode_protocol_selection,
    encode_protocol_versions_request,
    encode_read_request,
    make_ble_ack,
    pairing_key_matches_proof,
    pairing_key_proof,
    parse_battery,
    parse_ble_ack,
    parse_crypto_response,
    parse_name,
    parse_nonce_response,
    parse_protocol_versions,
    parse_raw_position,
    parse_status,
    raw_position_to_percent,
)


KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
NONCE = bytes(range(12))


def _crypto_response(command: int, payload: bytes) -> bytes:
    header = b"\x80\x00"
    body = bytes([command]) + payload
    return header + body + crc16(header + body).to_bytes(2, "little")


def _application_response(
    command: ShadeCommand,
    payload: bytes,
    *,
    key: bytes = KEY,
    nonce: bytes = NONCE,
    counter: int = 1,
    message_id: int = 1,
) -> bytes:
    header = counter.to_bytes(2, "big")
    presentation = bytes([0x40 | message_id, command]) + payload
    plaintext = presentation + crc16(header + presentation).to_bytes(2, "little")
    receive_counter = bytes([header[0] | 0x80, header[1]])
    iv = nonce + receive_counter + b"\x00\x00"
    encryptor = Cipher(algorithms.AES(key[:16]), modes.CTR(iv)).encryptor()
    return header + encryptor.update(plaintext) + encryptor.finalize()


class ChecksumAndProofTests(unittest.TestCase):
    def test_crc_matches_standard_vector(self) -> None:
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_pairing_key_proof_matches_fixed_vector(self) -> None:
        self.assertEqual(
            pairing_key_proof(KEY).hex(),
            "66f993162f1641ea4e163f6dccffb58e59d5a836e374009ad520a98cee184b76",
        )
        self.assertTrue(pairing_key_matches_proof(KEY, pairing_key_proof(KEY)))
        self.assertFalse(pairing_key_matches_proof(OTHER_KEY, pairing_key_proof(KEY)))

    def test_key_and_proof_lengths_fail_closed(self) -> None:
        with self.assertRaises(TiltProtocolError):
            pairing_key_proof(b"short")
        self.assertFalse(pairing_key_matches_proof(KEY, b"short"))


class CryptoHandshakeTests(unittest.TestCase):
    def test_protocol_request_has_fixed_wire_vector(self) -> None:
        self.assertEqual(encode_protocol_versions_request().hex(), "80000284d7")

    def test_only_supported_protocol_versions_can_be_selected(self) -> None:
        self.assertEqual(encode_protocol_selection(2)[2], CryptoCommand.SELECT_PROTOCOL_VERSION)
        with self.assertRaises(UnsafeCommandError):
            encode_protocol_selection(7)

    def test_nonce_request_has_no_payload(self) -> None:
        self.assertEqual(encode_nonce_request()[2], CryptoCommand.REQUEST_NONCE)

    def test_parse_protocol_versions(self) -> None:
        response = parse_crypto_response(
            _crypto_response(CryptoCommand.REQUEST_PROTOCOL_VERSIONS, b"\x02\x01\x02")
        )
        self.assertEqual(parse_protocol_versions(response), (1, 2))

    def test_parse_nonce_response(self) -> None:
        proof = pairing_key_proof(KEY)
        response = parse_crypto_response(
            _crypto_response(CryptoCommand.REQUEST_NONCE, NONCE + proof)
        )
        parsed = parse_nonce_response(response)
        self.assertEqual(parsed.nonce, NONCE)
        self.assertTrue(pairing_key_matches_proof(KEY, parsed.key_proof))

    def test_tampered_crypto_response_is_rejected(self) -> None:
        frame = bytearray(
            _crypto_response(CryptoCommand.REQUEST_PROTOCOL_VERSIONS, b"\x01\x02")
        )
        frame[-1] ^= 1
        with self.assertRaises(AuthenticationError):
            parse_crypto_response(bytes(frame))


class ApplicationCodecTests(unittest.TestCase):
    def test_read_encoder_rejects_write_command(self) -> None:
        with self.assertRaises(UnsafeCommandError):
            encode_read_request(ShadeCommand.SET_POSITION, key=KEY, nonce=NONCE)

    def test_position_request_matches_independent_aes_vector(self) -> None:
        frame = encode_position_request(
            42, key=KEY, nonce=NONCE, counter=1, message_id=3
        )
        self.assertEqual(frame.hex(), "0001dc0ca9f3aa8bd0")

    def test_position_encoder_is_strictly_bounded(self) -> None:
        for invalid in (-1, 101, True, 10.5):
            with self.subTest(invalid=invalid), self.assertRaises(TiltProtocolError):
                encode_position_request(invalid, key=KEY, nonce=NONCE)  # type: ignore[arg-type]
        for invalid_speed in (0, 101, True, 50.5):
            with self.subTest(speed=invalid_speed), self.assertRaises(TiltProtocolError):
                encode_position_request(
                    50, key=KEY, nonce=NONCE, speed=invalid_speed  # type: ignore[arg-type]
                )

    def test_status_response_decodes_and_maps_position(self) -> None:
        frame = _application_response(
            ShadeCommand.GET_STATUS,
            (735).to_bytes(2, "little") + bytes([87, 1, 1]),
        )
        response = decode_application_response(
            frame,
            key=KEY,
            nonce=NONCE,
            expected_command=ShadeCommand.GET_STATUS,
            expected_message_id=1,
            expected_counter=1,
        )
        status = parse_status(response)
        self.assertEqual(status.raw_position, 735)
        self.assertEqual(status.position_percent, 74)
        self.assertEqual(status.battery_percent, 87)
        self.assertTrue(status.calibrated)

    def test_wrong_key_fails_authentication(self) -> None:
        frame = _application_response(ShadeCommand.GET_POSITION, b"\xf4\x01")
        with self.assertRaises(AuthenticationError):
            decode_application_response(
                frame,
                key=OTHER_KEY,
                nonce=NONCE,
                expected_command=ShadeCommand.GET_POSITION,
                expected_message_id=1,
            )

    def test_mismatched_message_id_and_command_fail_closed(self) -> None:
        frame = _application_response(ShadeCommand.GET_POSITION, b"\xf4\x01")
        with self.assertRaises(TiltProtocolError):
            decode_application_response(
                frame,
                key=KEY,
                nonce=NONCE,
                expected_command=ShadeCommand.GET_POSITION,
                expected_message_id=2,
            )
        with self.assertRaises(TiltProtocolError):
            decode_application_response(
                frame,
                key=KEY,
                nonce=NONCE,
                expected_command=ShadeCommand.GET_BATTERY,
                expected_message_id=1,
            )
        with self.assertRaises(TiltProtocolError):
            decode_application_response(
                frame,
                key=KEY,
                nonce=NONCE,
                expected_command=ShadeCommand.GET_POSITION,
                expected_message_id=1,
                expected_counter=2,
            )

    def test_response_payload_parsers_are_strict(self) -> None:
        name = ApplicationResponse(ShadeCommand.GET_NAME, 1, 1, b"\x04Door")
        self.assertEqual(parse_name(name), "Door")
        position = ApplicationResponse(ShadeCommand.GET_POSITION, 1, 1, b"\xe8\x03")
        self.assertEqual(parse_raw_position(position), 1000)
        battery = ApplicationResponse(ShadeCommand.GET_BATTERY, 1, 1, b"\x63\x02")
        self.assertEqual(parse_battery(battery), (99, 2))
        with self.assertRaises(TiltProtocolError):
            parse_name(ApplicationResponse(ShadeCommand.GET_NAME, 1, 1, b"\x05Door"))
        with self.assertRaises(TiltProtocolError):
            parse_battery(ApplicationResponse(ShadeCommand.GET_BATTERY, 1, 1, b"\x65\x00"))

    def test_raw_position_mapping_is_not_inverted(self) -> None:
        self.assertEqual(raw_position_to_percent(0), 0)
        self.assertEqual(raw_position_to_percent(1000), 100)


class BluetoothFramingTests(unittest.TestCase):
    def test_chunks_round_trip_and_ack_each_sequence(self) -> None:
        frame = bytes(range(50))
        chunks = chunk_for_ble(frame, start_sequence=62)
        self.assertEqual([chunk[1] & 0x3F for chunk in chunks], [62, 63, 1])
        self.assertFalse(chunks[0][1] & 0x40)
        self.assertTrue(chunks[-1][1] & 0x40)

        assembler = BleMessageAssembler(expected_sequence=62)
        result = None
        for data in chunks:
            sequence, result = assembler.add(data)
            self.assertEqual(parse_ble_ack(make_ble_ack(sequence)), sequence)
        self.assertEqual(result, frame)
        self.assertEqual(assembler.expected_sequence, 2)

    def test_non_contiguous_chunks_fail_closed(self) -> None:
        chunks = chunk_for_ble(bytes(range(40)))
        assembler = BleMessageAssembler()
        assembler.add(chunks[0])
        with self.assertRaises(TiltProtocolError):
            assembler.add(chunks[2])

    def test_invalid_chunk_sequence_and_size_fail_closed(self) -> None:
        assembler = BleMessageAssembler()
        with self.assertRaises(TiltProtocolError):
            assembler.add(b"\x00\x40")
        with self.assertRaises(TiltProtocolError):
            assembler.add(b"\x00\x41" + bytes(19))


if __name__ == "__main__":
    unittest.main()
