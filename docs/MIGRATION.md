# Replacing the Bridge Raspberry Pi

Replacing the Pi is a protected-state migration, not a new pairing flow. The
pairing keys, MQTT credentials, and bridge configuration live outside git under
`/etc/tilt-local-bridge/`. Home Assistant does not have a copy of the shade
pairing keys.

The safe outcome has exactly one active bridge, the same access gates as
before, and every configured shade verified with a read-only status probe
before service mode starts on the new Pi.

## Before the old Pi is unavailable

1. Confirm the old Pi is the running bridge host. Check the SSH host key through
   a trusted path before connecting. Never bypass a new or changed host key.
2. Record the installed service launch gate:

   ```bash
   systemctl show --property=ExecStart --value tilt-local-bridge.service
   ```

   Position writes are authorized only when the installed `ExecStart` includes
   `--allow-position-writes`. A write-enabled value in `bridge.json` is not
   sufficient authority. The new Pi must preserve the installed launch gate,
   not silently gain or lose movement authority.
3. Reject symlinks in the old protected tree before creating the transfer:

   ```bash
   sudo find /etc/tilt-local-bridge -type l -print -quit
   ```

   Any output is a refusal. Resolve it through a separately reviewed recovery
   step before continuing.
4. Validate the protected files without contacting a shade:

   ```bash
   sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
     python3 -m tilt_local_bridge.tilt_bridge \
     --config /etc/tilt-local-bridge/bridge.json \
     check-runtime --expect-shade-reads
   ```

   Add `--expect-position-writes` only when the installed `ExecStart` includes
   `--allow-position-writes`.
5. Fence the old bridge before copying anything:

   ```bash
   sudo systemctl disable --now tilt-local-bridge.service
   systemctl is-active tilt-local-bridge.service
   ```

   The final command must report `inactive`. Keep the old Pi powered off or
   disconnected after the transfer. An old Pi that later boots with an enabled
   service can contend for the same shades and MQTT topics.

## Prepare the new Pi

Clone the repository on the new Pi and install the software without enabling
the service:

```bash
git clone https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge.git
cd tilt-local-bridge
sudo ./scripts/install.sh --activate --install-system-packages
systemctl is-active tilt-local-bridge.service
```

The final command must report `inactive`. The installer creates the service
account and protected directory before the state transfer.

## Transfer protected state

Verify the SSH host key for both hosts first. From a trusted operator machine,
stream the protected directory directly from the old Pi to the new Pi over
SSH. This keeps the archive off the operator machine's disk and does not print
its contents:

```bash
set -o pipefail
ssh old-bridge.local \
  'sudo tar --create --file=- --directory=/etc tilt-local-bridge' \
  | ssh new-bridge.local \
      'sudo tar --extract --file=- --directory=/etc --no-same-owner'
```

Before any ownership or mode change, re-check the extracted tree for symlinks:

```bash
ssh new-bridge.local \
  'sudo find /etc/tilt-local-bridge -type l -print -quit'
```

Any output is a refusal. Stop and remove the untrusted transfer through a
separately reviewed recovery step. Do not run `chown`, `chmod`, or the bridge
against that tree.

Restore the expected ownership and modes on the new Pi:

```bash
ssh new-bridge.local \
  'sudo chown root:tiltbridge /etc/tilt-local-bridge && \
   sudo chmod 0750 /etc/tilt-local-bridge && \
   sudo chown root:tiltbridge \
     /etc/tilt-local-bridge/bridge.json \
     /etc/tilt-local-bridge/mqtt.username \
     /etc/tilt-local-bridge/mqtt.password && \
   sudo chmod 0640 \
     /etc/tilt-local-bridge/bridge.json \
     /etc/tilt-local-bridge/mqtt.username \
     /etc/tilt-local-bridge/mqtt.password && \
   sudo chown -R root:tiltbridge /etc/tilt-local-bridge/keys && \
   sudo find /etc/tilt-local-bridge/keys -type d -exec chmod 0750 {} + && \
   sudo find /etc/tilt-local-bridge/keys -type f -exec chmod 0640 {} +'
```

Do not post the archive, configuration, key filenames, or command output to an
issue or chat. Do not put credentials or pairing keys in command arguments.

## Validate before service mode

Run the offline check on the new Pi with the same access expectations used on
the old Pi:

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  check-runtime --expect-shade-reads
```

If writes were already enabled, add `--expect-position-writes`. A failure here
means the migration stops. Do not weaken file permissions or replace a key to
make the check pass.

With the old bridge still fenced, probe every configured shade sequentially:

```bash
sudo -u tiltbridge env PYTHONPATH=/opt/tilt-local-bridge/src \
  python3 -m tilt_local_bridge.tilt_bridge \
  --config /etc/tilt-local-bridge/bridge.json \
  probe-status --shade <shade-id> --allow-shade-reads
```

Repeat the probe for each configured shade id. `probe-status` reads status and
does not send a movement command.

## Broker topology gate

The Tilt Local Bridge installer does not install an MQTT broker. In the
default setup, the broker stays in Home Assistant, so replacing the bridge Pi
does not change the Home Assistant MQTT connection.

If the old Pi also hosted a custom broker, stop here. Inventory and migrate the
broker configuration, accounts, access-control lists, and any TLS material
through that broker's supported process. Validate the broker from Home
Assistant before enabling the new bridge. Do not rotate credentials or
certificates merely because the host changed.

Validate the bridge account separately while both bridge services remain
stopped. Using the broker's supported diagnostics or audit log, confirm that
the actual bridge account can connect with its configured TLS identity, publish
only to the required discovery and state topics, and subscribe to the required
command topics. Do not publish a command message during this check. A successful
Home Assistant connection does not validate the bridge account or its ACLs.

Give any broker host a DHCP reservation or a reliable local DNS name before
putting it in `mqtt.host`. A current DHCP lease is not a stable target. When a
custom TLS broker moves, its certificate must cover the chosen stable address.

## Enable the new bridge

From the repository checkout on the new Pi, enable service mode only after the
offline checks, every shade probe, and the broker gate pass:

```bash
sudo ./scripts/install.sh --activate --enable
```

Pass `--allow-position-writes` only when the old installed `ExecStart` included
that exact flag and the new offline check passed with
`--expect-position-writes`. A write-enabled configuration value alone is not
authority. Do not add the flag during a read-only migration.

Confirm the service is active, Home Assistant's MQTT integration is connected,
and every migrated entity becomes available without moving a shade. Keep the
old Pi fenced.

## Rollback

Stop the new bridge first:

```bash
sudo systemctl disable --now tilt-local-bridge.service
```

If a custom broker moved, restore Home Assistant's prior supported MQTT
configuration and confirm the old broker is reachable. Re-enable the old
bridge only after the new bridge is inactive and the two hosts cannot run the
service at the same time.

## Retire the old protected state

Keep the old Pi powered off and its storage under physical control for a
documented rollback window. Do not wipe it while rollback remains possible.
After the replacement is accepted and the rollback window closes, remove the
old protected state with the storage platform's supported secure-erasure
process, or retain or destroy the storage. Do not reuse, sell, or discard media
while it still contains shade keys or MQTT credentials.

## If the old protected state is unavailable

Do not re-pair as a recovery shortcut. Recover the old Pi, its storage, or a
protected backup first. Pairing changes the shade key and may stop the original
app from controlling the shade. If no protected copy can be recovered, treat
pairing as a separate, explicit approval step and follow [Pairing](PAIRING.md)
one shade at a time.
