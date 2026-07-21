# Setup

This path puts the MQTT broker in Home Assistant and the BLE bridge on a nearby
Raspberry Pi. It is the simplest arrangement for most homes.

## Before you start

You need:

- A Raspberry Pi with onboard Bluetooth or a supported Bluetooth adapter
- Raspberry Pi OS Bookworm with network access
- Home Assistant with permission to install integrations and apps
- One or more compatible Tilt or SmarterHome roller shades
- The shade owner's Tilt account only if you need to pair a shade
- Physical access to each shade for pairing and recovery

Keep the Pi close enough for reliable BLE. Start with it in the same room as the
shade. Do not test movement when the shade path is obstructed.

Pairing can replace the key used by the original app. Read [Pairing](PAIRING.md)
before changing a shade.

## 1. Set up MQTT in Home Assistant

Home Assistant recommends its official Mosquitto broker app.

1. In Home Assistant, open **Settings**, **Apps**, then **Install app**.
2. Install and start **Mosquitto broker**.
3. Open your Home Assistant profile and enable **Advanced mode** if the user
   controls below are hidden.
4. Go to **Settings**, **People**, **Users** and create a dedicated user for the
   bridge. Do not use the reserved names `homeassistant` or `addons`.
5. Go to **Settings**, **Devices & services**. Accept the discovered **MQTT**
   integration and leave discovery enabled.

The official [Home Assistant MQTT documentation](https://www.home-assistant.io/integrations/mqtt/)
and [Mosquitto app documentation](https://github.com/home-assistant/addons/blob/master/mosquitto/DOCS.md)
are the source of truth if labels have changed.

## 2. Install the bridge software on the Pi

Connect to the Pi and run:

```bash
git clone https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge.git
cd tilt-local-bridge
sudo ./scripts/install.sh --activate --install-system-packages
```

This installs a disabled system service and an example config. It does not
contact or move a shade.

Check that Linux can see the Bluetooth controller:

```bash
bluetoothctl show
```

If that reports no controller, fix Bluetooth before continuing.

## 3. Store the MQTT credentials

Replace the sample values with the dedicated Home Assistant user created above:

```bash
printf '%s\n' 'tiltbridge' | sudo tee /etc/tilt-local-bridge/mqtt.username >/dev/null
sudo bash -c 'umask 0077; read -rsp "MQTT password: " password; printf "\n"; printf "%s\n" "$password" > /etc/tilt-local-bridge/mqtt.password'
sudo chown root:tiltbridge /etc/tilt-local-bridge/mqtt.username /etc/tilt-local-bridge/mqtt.password
sudo chmod 0640 /etc/tilt-local-bridge/mqtt.username /etc/tilt-local-bridge/mqtt.password
```

The second command waits for you to type or paste the MQTT password and press
Enter. It does not echo the value.

## 4. Pair or import each shade key

Follow [Pairing](PAIRING.md). Each shade needs a protected 32-byte key file and
its BLE address.

## 5. Configure your shades

Edit `/etc/tilt-local-bridge/bridge.json`. Replace the sample broker host, MAC,
shade id, name, and key path. Add one object to `shades` for each shade.

Start with writes disabled:

```json
"access": {
  "allow_reads": true,
  "allow_position_writes": false
}
```

Validate the protected files without contacting a shade:

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  check-runtime --expect-shade-reads
```

## 6. Run a read-only probe

This contacts one shade but does not move it:

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  probe-status --shade office_shade --allow-shade-reads
```

Replace `office_shade` with your configured id. A successful response includes
position, battery, charge state, and calibration state.

## 7. Start read-only service mode

```bash
sudo ./scripts/install.sh --activate --enable
systemctl status tilt-local-bridge.service
```

Home Assistant should discover one device per shade. Once reads are stable,
continue to [Home Assistant position control](HOME_ASSISTANT.md).

## Updating

```bash
cd tilt-local-bridge
git pull --ff-only
sudo ./scripts/install.sh --activate --enable --allow-position-writes
```

Omit `--allow-position-writes` if your service is intentionally read-only.

To replace the Raspberry Pi, do not repeat first-time pairing. Follow
[Pi replacement and protected-state migration](MIGRATION.md).
