# Security

## Reporting

Please report suspected vulnerabilities through GitHub's private vulnerability
reporting feature. Do not open a public issue that contains pairing keys,
passwords, access tokens, device addresses, private hostnames, or home network
details.

## Secrets

Treat each shade pairing key like a password. Anyone with the key and nearby BLE
access may be able to control that shade. Keep keys and MQTT credentials outside
git, owned by root or the bridge service account, and unavailable to other
users.

Recommended permissions:

```text
directory: 0750 root:tiltbridge
files:     0640 root:tiltbridge
```

The bridge rejects secret files that are symlinks, group-writable, world-readable,
world-writable, or owned by an unexpected account.

## Network boundary

Use a dedicated MQTT account for this bridge. Restrict it to the configured
topic prefix when your broker supports access-control lists. Do not expose an
unencrypted MQTT listener to the internet.

## Phone remote boundary

The optional Bluetooth phone remote is off until both a config gate and a
launch flag enable it. A phone is recorded only after a person approves it from
Home Assistant or with an operator signal on the Raspberry Pi, every request it
sends afterwards is signed over a bridge-issued single-use nonce, and it can
only ask for the status and position operations the bridge already allows. The
shade pairing keys never leave the Raspberry Pi. See
[docs/BLUETOOTH_REMOTE.md](docs/BLUETOOTH_REMOTE.md).

## Pairing boundary

The pairing command signs in to the legacy Tilt account service and changes a
shade's pairing key. It prompts for the password without echo, keeps the token
in memory, and requires `--permit-live-pairing`. Run it interactively on a
trusted Raspberry Pi, one shade at a time.
