# Pairing and Key Recovery

The bridge needs a unique 32-byte pairing key for each shade. The key is not a
general account password. It is the credential used for the encrypted BLE
session with that physical shade.

## Choose a path

Use one of these paths:

1. **Pair directly** if the legacy Tilt account service still accepts your
   account and the shade can advertise pairing mode.
2. **Import a protected export** if you already have a JSON store containing
   the shade MAC and `pairingKey` field.
3. **Keep an existing bridge key** if another local bridge already controls the
   shade. Copy it through a secure channel instead of pairing again.

There is no offline method here to invent a valid key for an already-paired
shade. Pairing requires both physical pairing mode and the legacy account
service.

## Direct pairing

### What pairing changes

Pairing requests a device-specific key package from Tilt's service and installs
the new key over BLE. That can invalidate the original app's key. The command:

- prompts for the Tilt password without echo
- keeps the password and access token in memory only
- writes the resulting shade key with mode `0600`
- requires `--permit-live-pairing`
- refuses to proceed when more than one shade advertises pairing mode

### Put exactly one shade in pairing mode

Use the physical pairing procedure for your shade model. Button timing and
motor feedback vary by hardware revision, so this project does not guess a
universal button sequence. Confirm pairing mode by running the command below:
the tool will report `status=pairing_shade_detected` only when it sees the
specific Tilt pairing advertisement.

Leave every other shade out of pairing mode.

### Run the one-shot tool

```bash
sudo install -d -o root -g root -m 0700 /var/lib/tilt-local-pairing
sudo env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_pairing \
  --output /var/lib/tilt-local-pairing/office_shade.key \
  --scan-timeout 30 \
  --permit-live-pairing
```

Replace the output filename. Enter the Tilt account email and password at the
interactive prompts so neither value appears in the command or shell history.

Success prints the shade's BLE address and key path. Install the key for the
service:

```bash
sudo install -o root -g tiltbridge -m 0640 \
  /var/lib/tilt-local-pairing/office_shade.key \
  /etc/tilt-local-bridge/keys/office_shade.key
sudo rm /var/lib/tilt-local-pairing/office_shade.key
```

Record the printed BLE address only in the protected local `bridge.json`. Do not
paste pairing output into chat, logs, screenshots, issues, or other public
messages.

### Ambiguous completion

If the final acknowledgement is lost, the shade may have accepted a key even
though the tool cannot prove it. The tool preserves a private pending key and
prints its path. Do not pair again immediately. Install that pending key and run
a read-only probe first.

## Import a protected JSON store

The importer recursively searches JSON objects for:

```json
{
  "id": "02:00:00:00:00:01",
  "pairingKey": "64 hexadecimal characters"
}
```

Every MAC in `bridge.json` must be present, and conflicting records fail closed.
The input file must be absolute, private, owned by root or the current user, and
smaller than the built-in limit.

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  import-cloud-store --input /absolute/path/to/protected-store.json
```

The project does not include tooling to defeat device backup encryption or
extract another person's data.
