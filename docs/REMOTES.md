# Physical Remotes

A wall remote is often the point of this project. This guide covers driving the
shades from a Lutron Caséta Pico remote through Home Assistant, and the one
design rule that any remote must follow.

## Buttons select positions, never motion

The bridge exposes status reads and absolute position writes. It publishes no
stop command. The MQTT discovery record sets `payload_stop` to `null`, so Home
Assistant creates the cover without a stop control and `cover.stop_cover` does
nothing. The cover reports `supported_features: 7`, which is open, close, and
set position, with the stop bit absent.

That single fact decides the whole design. A conventional up/stop/down remote
has a dead middle button here. Map every button to a position instead:

- A press selects where the shade should end up.
- Raise and lower move by a fixed step, they do not hold the shade in motion.
- Releasing a button does nothing, because nothing was holding.

Any remote works if you follow that rule. The rest of this guide uses a Pico
because it needs no new hub if you already run Caséta, and its battery lasts
about ten years.

## What you need

- A Lutron Caséta hub already paired to Home Assistant through the
  `lutron_caseta` integration.
- A Pico remote. The raise/lower models with a favorite button in the middle
  give you five positions from one remote. Wall-mount kits that include the
  bracket and wallplate exist if you want it to read as a light switch.

## Pair the remote

Add the Pico in the Lutron app as you would any remote. The app may ask what it
controls; that binding governs what Lutron itself does with the press.

Home Assistant receives the press independently, over its own connection to the
hub, whatever the remote is bound to. So a Pico bound to nothing useful, or
bound to a device you do not care about, still drives this automation. If the
app insists on an assignment and you do not want the button touching a Lutron
device, point it somewhere harmless and ignore it.

After pairing, reload the Caséta integration so Home Assistant picks up the new
device. Open **Settings**, **Devices & services**, **Lutron Caséta**, then the
three-dot menu and **Reload**. The remote appears as a device named after
whatever you called it in the Lutron app.

The integration also creates several `button` entities on that device, disabled
by default. Those are outbound: they let Home Assistant trigger the remote's
programmed Lutron action. They are not the inbound presses this automation
needs. Leave them disabled.

## Import the blueprint

1. Open **Settings**, **Automations & scenes**, then **Blueprints**.
2. Import this blueprint URL:
   `https://github.com/Sunrise-Labs-Dot-AI/tilt-local-bridge/blob/main/blueprints/automation/tilt_local_bridge/pico_shade_control.yaml`
3. Select **Create automation**, choose the Pico and the shades it should
   control, and adjust the positions if the defaults do not suit you.

Create one automation per remote. Select every shade the remote should move in
a single automation rather than making one automation per shade.

## Default button map

| Button | Reported as | Default |
| --- | --- | --- |
| On, top | `on` | Open, 100 |
| Raise, up arrow | `raise` | Up one step |
| Favorite, middle | `stop` | 50 |
| Lower, down arrow | `lower` | Down one step |
| Off, bottom | `off` | Closed, 0 |

Lutron reports the middle button as `stop` for historic lighting reasons. On a
shade it is the favorite preset. It never stops movement, because there is
nothing to stop.

The raise and lower buttons move to the next multiple of the step size in that
direction, so presses walk a predictable ladder. With the default step of 25
that ladder is 0, 25, 50, 75, 100. A shade sitting off the ladder, because a
schedule or a voice assistant put it somewhere else, moves to the nearest rung
in the direction you pressed rather than jumping past it.

Each shade steps from its own position, so shades left at different heights keep
their offset.

## Confirm your remote's button names

Other Pico models report different button names. To see what yours sends, open
**Developer tools**, then **Events**, listen to `lutron_caseta_button_event`,
and press each button. Every press produces two events, `press` and `release`.
The blueprint acts only on `press`, so a single press does not fire twice.

The blueprint ignores button names it does not recognise, so an unmapped button
does nothing rather than misbehaving. If your remote reports names outside the
table above, copy the blueprint and add the branches you need.

## Shades that should not all go to the same place

Set **Use custom actions for the favorite button**, then put whatever you want
under **Favorite button actions**. That replaces the favorite position and lets
one button close one shade while leaving another part-way open, for example a
door shade at 50 with the window beside it fully closed.

The other four buttons continue to apply a single position to every selected
shade. If you need asymmetric behaviour on more than one button, call a script
from the favorite actions or copy the blueprint.

## What to expect

These shades are slow, and one Bluetooth radio serves every shade on the bridge.
On the reference hardware, with two shades on one Raspberry Pi:

- A one-step move settles both shades in about 30 seconds.
- A full close-to-open move takes about 70 seconds.
- The second shade starts roughly 10 seconds after the first, because the
  bridge talks to them one at a time.

So a press is not instant, and the shade is still moving well after you walk
away. That is the hardware, not the automation.

Pressing a second button while a shade is still moving is safe. The bridge drops
the position it was confirming and takes the new one, so the shade ends where
you last asked rather than stalling at the earlier target. A repeated press of
the same button is ignored while that position is still being confirmed.

This is also why a rotary dial is a poor fit. A dial implies continuous, immediate
response, and these shades answer in tens of seconds. Discrete positions match
what the hardware can actually do.

## Other remotes

Nothing here is specific to Lutron beyond the trigger. Any remote that reaches
Home Assistant works the same way: swap the event trigger for whatever your
remote produces, an `event` entity for Zigbee and Z-Wave buttons or a device
trigger for others, and keep every branch pointed at a position.

Do not put remote input on the bridge Raspberry Pi itself unless it is gated
and authenticated the way the [Bluetooth phone remote](BLUETOOTH_REMOTE.md)
is. Its single Bluetooth radio is already the contended resource serving the
shades, and an unauthenticated local listener would let anything in radio
range move them. The phone remote pays for its place on that radio with an
approval step, signed requests, and the same position-only rule as every other
remote.
