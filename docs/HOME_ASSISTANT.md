# Home Assistant and Position Control

The bridge publishes retained MQTT discovery records. Home Assistant creates a
device for each shade with these entities:

- **Cover**: open, close, current position, and set position
- **Position**: a 0 to 100 percent slider for dashboards that do not surface the
  cover's position control clearly
- **Battery**: diagnostic percentage sensor

`0` means closed and `100` means fully open.

## Confirm discovery

1. Open **Settings**, **Devices & services**, then **MQTT**.
2. Open **Devices** and select the shade.
3. Confirm the cover reports a plausible position and the device is available.
4. Do not enable movement yet if the reported position is wrong or the shade is
   not calibrated.

MQTT discovery is enabled by default. The bridge also listens for Home
Assistant's `homeassistant/status` birth message and republishes discovery after
Home Assistant restarts.

## Add a low-battery alert

The repository includes a reusable Home Assistant blueprint. It creates a
persistent notification after one battery sensor stays below 40 percent for 30
minutes, reminds twice daily while the battery remains low, and dismisses the
notification after the battery rises above 50 percent. The gap between the two
thresholds prevents a noisy battery reading from repeatedly clearing and
reopening the alert.

1. Open **Settings**, **Automations & scenes**, then **Blueprints**.
2. Import this blueprint URL:
   `https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge/blob/main/blueprints/automation/tilt_local_bridge/low_battery_alert.yaml`
3. Select **Create automation**, choose one shade's Battery entity, and adjust
   the thresholds if needed.
4. Optionally add a mobile notification under **Additional notification
   actions**.
5. Repeat for each shade.

The first low reading and each twice-daily reminder are debounced. A Home
Assistant restart checks the current reading directly so an already-low battery
is not forgotten after automations reload. Home Assistant's numeric-state
debounce does not survive a restart, so this startup check is intentional.

## Enable position writes

There are two gates. Both must be enabled.

First, edit `bridge.json`:

```json
"access": {
  "allow_reads": true,
  "allow_position_writes": true
}
```

Then reinstall the unit with the launch-time gate:

```bash
sudo ./scripts/install.sh \
  --activate \
  --enable \
  --allow-position-writes
```

The service validates both gates before it starts.

## First movement test

Clear the shade path. Start from a known calibrated endpoint and request a small
change, such as 100 to 95 percent. Watch the physical shade and Home Assistant.

During movement, the bridge keeps the entity available and publishes observed
positions when the shade responds. It rejects overlapping commands and checks
again on a bounded schedule. A shade becomes unavailable only when the bridge
cannot re-establish a trustworthy readback.

## Dashboards

Use the cover entity for ordinary control. Add the Position number only when you
want an always-visible slider. Expose the cover, not the helper slider or battery
sensor, to voice assistants.

## Read-only rollback

To remove movement authority without uninstalling:

1. Set `allow_position_writes` to `false` in `bridge.json`.
2. Reinstall without the write flag:

```bash
sudo ./scripts/install.sh --activate --enable
```

The cover remains visible and continues reporting position and battery.
