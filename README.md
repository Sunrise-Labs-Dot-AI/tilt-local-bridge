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
