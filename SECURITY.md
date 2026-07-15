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

## Pairing boundary

The pairing command signs in to the legacy Tilt account service and changes a
shade's pairing key. It prompts for the password without echo, keeps the token
in memory, and requires `--permit-live-pairing`. Run it interactively on a
trusted Raspberry Pi, one shade at a time.
