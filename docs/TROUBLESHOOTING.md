# Troubleshooting

## The pairing tool finds no shade

- Put exactly one shade into its model-specific physical pairing mode.
- Move the Pi into the same room.
- Confirm `bluetoothctl show` reports a powered controller.
- Increase `--scan-timeout` up to 300 seconds.
- Stop the bridge service while pairing: `sudo systemctl stop tilt-local-bridge`.

The scanner requires the specific Tilt pairing advertisement. A normal BLE
advertisement is not enough.

## More than one shade is found

Take all but one shade out of pairing mode and retry. The tool deliberately
refuses to choose between multiple candidates.

## Sign-in or key service fails

Pairing depends on legacy Tilt account endpoints that this project does not
operate. Check the account credentials in the original app, then retry once.
Repeated retries will not repair an unavailable service.

If you already have a protected key export, use the import path in
[Pairing](PAIRING.md).

## Read-only probe fails

Check:

- the configured MAC exactly matches the address printed during pairing
- the key file contains exactly 64 hexadecimal characters
- the key file owner and permissions satisfy [Security](../SECURITY.md)
- the Pi is near the shade
- no other phone or bridge is holding a BLE connection

Run with `--verbose` for error classes, but never post unredacted logs.

## Home Assistant discovers nothing

Check service and broker connectivity:

```bash
systemctl status tilt-local-bridge.service
journalctl -u tilt-local-bridge.service -n 100 --no-pager
```

Then confirm:

- the MQTT host is reachable from the Pi
- the dedicated MQTT credentials work
- MQTT discovery is enabled in Home Assistant
- `discovery_prefix` matches Home Assistant, normally `homeassistant`
- the bridge and Home Assistant use the same broker

## The shade appears offline while moving

Current releases treat a confirmed write with delayed readback as movement in
progress. The entity should remain available while bounded reconciliation runs.
If it becomes unavailable, the bridge was unable to obtain trustworthy status
after those retries. Check range, power, and competing BLE clients.

## Position is reversed

Do not compensate in automations. Open an issue with the shade model, firmware,
and redacted status output. Different firmware may encode raw positions
differently.

## Disable everything quickly

```bash
sudo systemctl disable --now tilt-local-bridge.service
```

This stops bridge control without changing the pairing key again.
