# Bluetooth Phone Remote

Home Assistant reaches the shades through the bridge over the network. When
the network is down, the bridge Raspberry Pi is still sitting a few metres from
the shades with a working Bluetooth radio. The phone remote uses that radio: a
small app on a phone talks to the bridge directly over Bluetooth Low Energy,
and the bridge moves the shade the same way it does for Home Assistant.

It is deliberately basic. It finds a bridge, pairs once after a person approves
the phone, shows each shade's position and battery, and sets a position.
There is no stop button, for the same reason the [remote guide](REMOTES.md)
gives: the shade offers positions, never motion.

```text
Phone app  ── BLE, signed requests ──>  Raspberry Pi bridge  ── encrypted BLE ──>  Tilt shade
                                              │
                                              └── MQTT (when the network is up) ──> Home Assistant
```

The phone never talks to a shade. The pairing keys never leave the Raspberry
Pi. A phone that is not paired can see that a bridge exists and nothing else.

## How pairing works

Each phone holds an Ed25519 key it generates on first launch and keeps in the
platform keychain. Pairing records the phone's public key on the bridge, and
only after a person approves the request somewhere the phone cannot reach on
its own:

1. The phone connects and asks to pair. The bridge holds one pending request
   at a time, for two minutes, and answers with a six-digit code.
2. The phone shows the code. Home Assistant shows the same code and the phone's
   name in the **Phone pairing request** sensor.
3. If they match, tap the **Approve phone pairing** button in Home Assistant.
   Without Home Assistant, check the bridge journal for the code and send the
   approval signal on the Raspberry Pi instead:

   ```bash
   journalctl -u tilt-local-bridge.service -n 20 --no-pager
   sudo systemctl kill --signal=SIGUSR1 tilt-local-bridge.service
   ```

4. The bridge stores the phone's public key under `/var/lib/tilt-local-bridge/`
   and tells the phone it is paired.

After that, every request the phone sends is signed with its key over a
single-use nonce the bridge issued for that connection. A captured request
cannot be replayed, a phone that was never approved cannot move a shade, and
the approval can be revoked at any time from the Raspberry Pi. The code itself
is not a secret: its job is to let you confirm that the request waiting in Home
Assistant came from the phone in your hand and not from another radio in range.

Someone in Bluetooth range who is not paired can see the bridge's name, ask to
pair, and watch that request appear in Home Assistant. They cannot read shade
state or move a shade. If a request you did not make appears, ignore it; it
expires on its own.

## Two gates, like everything else

The remote is off until both gates are set:

- `bluetooth_remote.enabled` is `true` in `bridge.json`
- the service was installed with `--allow-bluetooth-remote`

Moving a shade from the phone additionally needs the same read and
position-write gates Home Assistant needs. A bridge running read-only reports
positions to the phone and refuses to set one.

## Enable the remote on the Raspberry Pi

Add the section to `/etc/tilt-local-bridge/bridge.json`:

```json
"bluetooth_remote": {
  "enabled": true,
  "name": "Tilt Local Bridge",
  "state_file": "/var/lib/tilt-local-bridge/remote.json"
}
```

`name` is what the phone sees while scanning, at most 24 characters. The state
file holds the bridge identity and the approved phone keys, nothing secret; the
service creates it with mode `0600` inside a systemd state directory.

Reinstall the unit with the launch-time gate. Keep `--allow-position-writes`
only if the service already had it:

```bash
sudo ./scripts/install.sh --activate --enable --allow-position-writes --allow-bluetooth-remote
```

The installer runs `check-runtime --expect-bluetooth-remote` first, which
validates the state location without touching Bluetooth. The journal then
shows `Bluetooth remote is advertising as 'Tilt Local Bridge'`.

To turn the remote off, reinstall without the flag. Paired phones stay recorded
and work again the next time it is enabled.

## Home Assistant entities

With the remote enabled, MQTT discovery adds a **Tilt Local Bridge** device:

- **Phone pairing request**, a diagnostic sensor: `none`, or the phone name
  and code while a request is waiting
- **Approve phone pairing**, a button that approves the waiting request
- **Paired phones**, a diagnostic count

The button publishes `APPROVE_PAIRING` to `<topic_prefix>/bridge/command`,
which the bridge subscribes to only while the remote is enabled. If your broker
uses access-control lists, the bridge account needs write access to
`<topic_prefix>/bridge/pairing_request`, `<topic_prefix>/bridge/paired_phones`,
and `<discovery_prefix>/button/tilt_bridge/#`, and the Home Assistant account
needs write access to `<topic_prefix>/bridge/command`.

## Manage paired phones

Run these as the service user so the state file keeps its owner:

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  remote-phones list
```

`list` prints each phone's name, the first eight digits of its key, and when it
was approved. Remove one with `remote-phones remove --key-prefix <digits>`.
The running service notices the change on its next request; nothing needs a
restart. A phone that loses its key, for example after a reinstall of the app,
shows up as a new pairing request and must be approved again.

## Using the app

Open the app near the bridge. It scans for the service, connects, and either
shows the shades or asks to pair. Each shade shows its last known position and
battery, with buttons for closed, a quarter, half, three quarters, and open,
plus a slider that commits when released. A moving shade shows the target it is
heading for until the bridge confirms it arrived.

What to expect:

- The bridge has one Bluetooth radio serving every shade and the phone. A phone
  command waits for any shade session in progress, so it can take a couple of
  seconds to be acknowledged and tens of seconds for the shade to arrive.
- Position reads are the bridge's cached status. Pull to refresh asks the
  bridge to read every shade again over Bluetooth.
- Bluetooth Low Energy reaches a room or two. If the app cannot find the bridge,
  move closer to the Raspberry Pi.
- Home Assistant keeps working alongside the phone. Whichever asked last wins,
  and the bridge reports the outcome to both.

Forget bridge in the app clears the pinned bridge identity on the phone. It
does not revoke the phone on the bridge; use `remote-phones remove` for that.

## Build and install the app

The app lives in `apps/shade-remote` and is an Expo application written in
TypeScript with `react-native-ble-plx` for Bluetooth. Bluetooth needs a
development build, not Expo Go.

```bash
cd apps/shade-remote
npm ci
npm run typecheck
npm test
```

iOS, onto a connected iPhone with Xcode installed and signed in:

```bash
npx expo prebuild --platform ios
npx expo run:ios --device
```

Android, with the Android SDK installed:

```bash
npx expo prebuild --platform android
npx expo run:android --device
```

`eas build --profile preview` produces installable builds from Expo's servers
if you use EAS; the repository ships an `eas.json` with development, preview,
and production profiles and no account details.

The iOS simulator has no Bluetooth. Set `EXPO_PUBLIC_MOCK_BRIDGE=1` when
starting Metro to run the app against an in-process pretend bridge that pairs
after a short wait and moves pretend shades, which is how the screens are
exercised without hardware.

A reference command-line client for laptops sits in `tools/remote_client.py`.
It speaks the same protocol through `bleak` and is useful for checking the
bridge before installing the app:

```bash
python3 tools/remote_client.py pair
python3 tools/remote_client.py status
python3 tools/remote_client.py set --shade office_shade --position 50
```

It keeps its key in `~/.config/tilt-local-bridge/remote-client.key` and prints
only the first digits of it.

## Protocol summary

One GATT service, `4e9a0001-3b7c-4f2e-9d61-5c8a2f7b0e10`, with three
characteristics:

| Characteristic | UUID suffix | Use |
| --- | --- | --- |
| Info | `...0004` | Read: protocol version, bridge id, name |
| Request | `...0002` | Write: chunked JSON requests |
| Response | `...0003` | Notify: chunked JSON replies and pushes |

Messages are compact JSON split into chunks. Each chunk starts with one header
byte: bit 7 marks the first chunk of a message, bit 6 the last, and the low six
bits count chunks. The first chunk then carries the message length as two
big-endian bytes. Chunks are sized to the negotiated MTU minus three.

| Request | Reply | Notes |
| --- | --- | --- |
| `nonce` | `nonce` | Unsigned. Returns the bridge id, name, a nonce, and whether the phone is paired. |
| `pair` | `pair` | `pending` with a code, `approved`, or `error busy` |
| `pair_status` | `pair` | `pending`, `approved`, `denied`, `expired`, or `none` |
| `status` | `status` | Cached position, battery, availability, and target for every shade |
| `set` | `set` | Queues one absolute position, exactly like a Home Assistant command |
| `refresh` | `refresh` | Asks the bridge to read every shade again |

Every request except `nonce` carries the phone's public key, the current
nonce, the bridge id, and an Ed25519 signature over
`tilt-remote/1\n` followed by the canonical JSON of the request without its
signature. Canonical JSON sorts keys, uses no whitespace, allows no floating
point numbers, and is encoded as UTF-8. A verified request rotates the nonce;
the reply carries the next one as `n`. Replies always carry `n`; pushes, which
the bridge sends when a shade changes or a pairing resolves, never do. Every
reply carries `to`, the first eight digits of the phone key it is for.

`tests/fixtures/bluetooth_remote_vectors.json` holds golden vectors for the
canonical encoding, the signatures, and the chunking. Both the bridge and the
app test against it, and `tools/make_remote_vectors.py` regenerates it after an
intentional protocol change.

## When it does not work

See [troubleshooting](TROUBLESHOOTING.md#the-phone-cannot-find-the-bridge).
