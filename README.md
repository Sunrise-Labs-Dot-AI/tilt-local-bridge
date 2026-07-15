# Tilt Local Bridge

Tilt Local Bridge exposes compatible Tilt and SmarterHome roller shades to
Home Assistant through MQTT discovery. A Raspberry Pi talks to each shade over
Bluetooth Low Energy. Home Assistant gets a cover with open, close, stop,
position, availability, and battery state.

Runtime control stays on your local network. The legacy Tilt account service is
used only when the optional pairing tool requests and installs a device key.

> [!WARNING]
> Pairing changes the shade's key and can stop the original app from controlling
> it. The project is community-built, experimental, and not affiliated with or
> endorsed by Tilt or SmarterHome. Use it only with shades you own.

## Fastest path: use a coding agent

Paste this prompt into Claude Code, Codex, or another coding tool that can reach
your Raspberry Pi:

```text
Help me install Tilt Local Bridge on a Raspberry Pi and connect my compatible
Tilt or SmarterHome roller shades to Home Assistant.

Use https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge as the source of
truth. Read README.md, SECURITY.md, docs/SETUP.md, docs/PAIRING.md,
docs/HOME_ASSISTANT.md, and docs/TROUBLESHOOTING.md before acting. Inspect my
environment before changing it, confirm which machine is the Raspberry Pi, and
use SSH only after I confirm any new or changed host key. Never bypass SSH host
key verification.

Work in stages. Begin with inspection only. Ask before each privileged package
installation, service change, or edit under /etc. Start the bridge read-only.
The CLI's explicit read, write, and pairing flags are the enforced safety gates;
do not bypass them. Run the offline tests and check-runtime before contacting a
shade, then use probe-status for the first live check because it does not move
the shade.

Treat pairing and movement as separate approval gates. Do not pair, rekey, or
replace an existing shade key until I explicitly approve that step. Pair exactly
one advertising shade at a time. If pairing completion is ambiguous, do not
retry. Preserve the pending key and explain the read-only recovery check. Do not
send any movement command until I separately approve a small first movement
with the shade path clear.

Have me enter account details, MQTT credentials, device addresses, and other
private values directly in the local terminal or protected local files when
needed. Do not ask me to paste them into chat. Never put passwords, access
tokens, MQTT credentials, pairing keys, real BLE addresses, private hostnames,
home-network details, or unredacted terminal output in command arguments, git,
source files, logs, screenshots, issues, or other public output. Store keys and
MQTT credentials only in the protected files documented by the repository and
verify their ownership and permissions.

Stop and explain what you need if protected-file validation fails, more than one
shade advertises pairing mode, an SSH host key changes, the hardware or firmware
differs from the documented flow, or any step would require replacing an
existing key. Tell me what access or physical action you need only when you
reach that step, and keep all reported checks concise and redacted.
```

If the coding tool cannot reach the Pi or Home Assistant, it should stop and
give you the smallest next action. The manual path remains below.

## What works

- Local status reads over BLE
- Calibrated positions from 0 to 100 percent
- Open, close, and exact-position commands in Home Assistant
- Battery and availability sensors
- Multiple shades from one Raspberry Pi
- MQTT discovery, so no custom Home Assistant component is required
- Optional one-shot pairing with credentials entered interactively and never
  written to disk
- Conservative position verification while a shade is moving

## Architecture

```text
Home Assistant cover
        |
   MQTT discovery
        |
Raspberry Pi bridge
        |
  encrypted BLE
        |
Tilt roller shade
```

The bridge exposes only the protocol operations needed for status and position.
It has no reset, calibration, rename, firmware, or arbitrary-command interface.

## Start here

1. Read the [hardware and safety notes](docs/SETUP.md#before-you-start).
2. Follow the [Raspberry Pi and Home Assistant setup](docs/SETUP.md).
3. Use [pairing and key recovery](docs/PAIRING.md) for each shade.
4. Enable [position control](docs/HOME_ASSISTANT.md).
5. Optionally expose the cover to [Google Home](docs/GOOGLE_HOME.md).

Trouble along the way is covered in [troubleshooting](docs/TROUBLESHOOTING.md).

## Proven environment

The implementation has been exercised with Raspberry Pi OS Bookworm, Python
3.11, BlueZ, Home Assistant MQTT discovery, and paired Tilt roller shades. The
offline suite covers protocol framing, authentication, BLE retries, pairing,
configuration gates, discovery payloads, and movement reconciliation.

Other shade hardware and firmware revisions may behave differently. Please
open an issue with the firmware version and redacted logs when reporting one.

## Security model

- Pairing keys and MQTT credentials live outside the repository.
- Secret files must not be symlinks or readable by other local users.
- Reads must be enabled in both config and process arguments.
- Position writes require a second config gate and a second process argument.
- The pairing tool selects exactly one shade advertising the pairing marker and
  refuses ambiguous multi-device scans.
- No Tilt password, access token, pairing key, or MQTT password is logged.

See [Security](SECURITY.md) for reporting and operational guidance.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
python tools/check_public_tree.py
```

## License

MIT. See [LICENSE](LICENSE).
