# Protocol Scope

This implementation was recovered from observed app and shade behavior for the
purpose of interoperating with hardware owned by the operator.

## Runtime operations

The runtime allowlist contains only:

- negotiate crypto protocol
- request a nonce
- authenticate with the 32-byte shade key
- read name, position, status, and battery
- set a calibrated position from 0 to 100 percent

Every application frame is encrypted and authenticated. BLE messages use the
shade's chunk and acknowledgement layer with bounded retries.

## Pairing operations

The separate pairing executable can:

- detect the Tilt pairing advertisement
- negotiate the pairing protocol
- request the shade's pairing token and public key material
- call the three observed legacy pairing endpoints
- install the returned device key

It cannot move, reset, calibrate, rename, or update firmware.

## Deliberate omissions

The project does not expose arbitrary frame writes or undocumented commands.
Adding a new operation requires a concrete owned-device use case, captured
fixtures with no private data, explicit protocol validation, and tests proving
that the command cannot escape its stated bounds.
