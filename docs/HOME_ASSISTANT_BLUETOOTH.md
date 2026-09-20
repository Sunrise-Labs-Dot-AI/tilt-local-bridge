# Home Assistant over Bluetooth

The MQTT path in [Home Assistant and position control](HOME_ASSISTANT.md) is the
default: the bridge publishes to a broker over the network and Home Assistant
gets a cover per shade. This page is the other way in. A custom integration
lets Home Assistant talk to the bridge Raspberry Pi directly over Bluetooth
Low Energy, through Home Assistant's own Bluetooth stack, using the same
protocol and the same pairing ceremony as the [phone remote](BLUETOOTH_REMOTE.md).

Use it when the network between Home Assistant and the bridge Pi is the part
that fails. Nothing on the bridge changes: the integration is one more paired
"phone" as far as the bridge is concerned, so it needs the remote enabled
(`bluetooth_remote.enabled` plus `--allow-bluetooth-remote`) and it inherits the
signed-request security described there.

## What you get

- A **Tilt Local Bridge** device named after the bridge, with one device per
  shade hung off it.
- A cover per shade: open, close, and set position, on the 0 closed to 100 open
  scale. There is no stop control, because the shade offers none.
- A battery sensor per shade.
- Polling every minute from the bridge's cached status, every fifteen seconds
  while a shade is on its way somewhere, and a fast read after every command.

If you also keep the MQTT entities, Home Assistant will show each shade twice.
Pick one path per home: keep MQTT when the network is dependable, or remove the
MQTT device from Home Assistant and let this integration be the only path,
which makes Wi-Fi irrelevant to the shades.

## Range comes first

Bluetooth Low Energy reaches a room or two. Before installing anything, check
that the Home Assistant machine's radio can hear the bridge: add the
**Bluetooth** integration if it is not already set up, wait a minute, and
download its diagnostics. The bridge appears as an advertiser carrying the
service `4e9a0001-3b7c-4f2e-9d61-5c8a2f7b0e10`, with a signal strength next to
it. Around -70 dBm is comfortable, -85 is marginal, and below -90 will not hold
a connection. Moving the bridge Pi within its room, a USB Bluetooth adapter
with an external antenna on the Home Assistant side, or an ESPHome Bluetooth
proxy placed near the shades all fix a weak reading. Home Assistant routes this
integration through Bluetooth proxies automatically.

## Install

Home Assistant OS, Supervised, or Container all work; the integration needs no
add-on and no extra Python packages.

With HACS: add `https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge` as a
custom repository of type Integration, install **Tilt Local Bridge**, and
restart Home Assistant.

Without HACS: copy `custom_components/tilt_bridge` from this repository into
`config/custom_components/tilt_bridge` on the Home Assistant machine, using
the Samba, File editor, or SSH add-on, and restart Home Assistant.

## Pair

Home Assistant discovers an advertising bridge on its own and offers it under
**Settings**, **Devices & services**. You can also add it by hand from **Add
integration**, **Tilt Local Bridge**.

The pairing step is the bridge's ceremony. The bridge picks one shade and a
direction at random, and the form says which: "Tap **down** on **Office
Shade**". Press that button on the shade itself, let it move, then press
Submit. The bridge re-reads the shades every fifteen seconds and approves the
request when it sees that shade move that way without having commanded it.
Any other shade or direction cancels the request, and Submit asks for a new
one. Requests expire after two minutes.

When the bridge's MQTT entities are still reachable, the **Approve phone
pairing** button there accepts the code shown on the form instead. The
Home Assistant journal on the bridge shows the same request, and `SIGUSR1`
approves it from the Pi.

Home Assistant keeps its key in the config entry. Removing the integration
does not revoke it on the bridge; `remote-phones remove` does that.

## Troubleshooting

- **Cannot reach the bridge** during pairing: the radio cannot connect even
  though it may hear the advertisement. See range above.
- **Not paired** after it once worked: the bridge forgot this Home Assistant,
  usually after `remote-phones remove` or a new state file. The integration
  asks to reauthenticate; that is the same button-press ceremony.
- **Movement refused**: the bridge is running read-only. The remote respects
  the same position-write gates as MQTT; see
  [enable position writes](HOME_ASSISTANT.md#enable-position-writes).
- The bridge's own log lines for the remote are described in
  [troubleshooting](TROUBLESHOOTING.md#the-phone-cannot-find-the-bridge).
