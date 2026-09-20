"""Setup, covers, battery sensors, and movement tracking against the fake bridge."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.cover import ATTR_CURRENT_POSITION, ATTR_POSITION, CoverState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tilt_bridge.const import (
    CONF_ADDRESS,
    CONF_BRIDGE_ID,
    CONF_BRIDGE_NAME,
    CONF_SEED,
    DOMAIN,
    HOLD_LAST_STATUS_SECONDS,
)
from custom_components.tilt_bridge.protocol import Identity

from .conftest import BRIDGE_ADDRESS, BRIDGE_ID, VECTORS, FakeBridge, client_factory_for


@pytest.fixture
async def setup_entry(hass: HomeAssistant, fake_bridge: FakeBridge, identity: Identity):
    fake_bridge.paired.add(identity.public_key_hex)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ADDRESS,
        title="Office Bridge",
        data={
            CONF_ADDRESS: BRIDGE_ADDRESS,
            CONF_SEED: VECTORS["seed_hex"],
            CONF_BRIDGE_ID: BRIDGE_ID,
            CONF_BRIDGE_NAME: "Office Bridge",
        },
    )
    entry.add_to_hass(hass)
    factory = client_factory_for(fake_bridge)
    from custom_components import tilt_bridge as module

    class PatchedClient(module.TiltBridgeClient):
        def __init__(self, identity, *, name, ble_device_provider, client_factory=None):
            super().__init__(identity, name=name, ble_device_provider=ble_device_provider, client_factory=factory)

    with patch.object(module, "TiltBridgeClient", PatchedClient), patch.object(
        module.bluetooth, "async_ble_device_from_address", return_value=object()
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry


async def test_covers_and_batteries_are_created(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    door = hass.states.get("cover.door")
    assert door is not None
    assert door.state == CoverState.OPEN
    assert door.attributes[ATTR_CURRENT_POSITION] == 100
    right = hass.states.get("cover.right")
    assert right.attributes[ATTR_CURRENT_POSITION] == 40
    assert hass.states.get("sensor.door_battery").state == "91"
    assert hass.states.get("sensor.right_battery").state == "68"


async def test_set_position_reports_movement_until_arrival(
    hass: HomeAssistant, setup_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await hass.services.async_call(
        "cover", "set_cover_position", {ATTR_ENTITY_ID: "cover.door", ATTR_POSITION: 50}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    last_set = [r for r in fake_bridge.requests if r["t"] == "set"][-1]
    assert last_set["shade"] == "door" and last_set["position"] == 50
    assert hass.states.get("cover.door").state == CoverState.CLOSING

    # The shade arrives; the next poll reports it settled.
    fake_bridge.shades[0]["position"] = 50
    fake_bridge.shades[0]["target"] = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done(wait_background_tasks=True)
    door = hass.states.get("cover.door")
    assert door.state == CoverState.OPEN
    assert door.attributes[ATTR_CURRENT_POSITION] == 50

    await hass.services.async_call("cover", "close_cover", {ATTR_ENTITY_ID: "cover.door"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [r for r in fake_bridge.requests if r["t"] == "set"][-1]["position"] == 0


async def test_read_only_bridge_refuses_movement(
    hass: HomeAssistant, setup_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    fake_bridge.writes = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "cover", "open_cover", {ATTR_ENTITY_ID: "cover.right"}, blocking=True
        )


async def test_shade_becomes_unavailable_when_bridge_says_so(
    hass: HomeAssistant, setup_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    fake_bridge.shades[1]["available"] = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("cover.right").state == "unavailable"
    assert hass.states.get("cover.door").state == CoverState.OPEN


async def test_missed_polls_keep_the_last_status_for_a_while(
    hass: HomeAssistant, setup_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    """A marginal link misses polls; the covers stay usable on the last status."""

    fake_bridge.unreachable = True
    for minute in range(1, 4):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70 * minute))
        await hass.async_block_till_done(wait_background_tasks=True)
    door = hass.states.get("cover.door")
    assert door.state == CoverState.OPEN
    assert door.attributes[ATTR_CURRENT_POSITION] == 100
    assert hass.states.get("sensor.door_battery").state == "91"

    # The hold runs out: the next missed poll takes the shades unavailable.
    coordinator = setup_entry.runtime_data
    assert coordinator.missed_polls == 3
    coordinator._last_success_at -= HOLD_LAST_STATUS_SECONDS + 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70 * 4))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("cover.door").state == "unavailable"

    # The bridge answers again: everything comes back.
    fake_bridge.unreachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70 * 5))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("cover.door").state == CoverState.OPEN


async def test_a_command_still_reaches_the_bridge_while_polls_miss(
    hass: HomeAssistant, setup_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    """Held status keeps the controls live; a command opens its own connection."""

    fake_bridge.unreachable = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("cover.door").state == CoverState.OPEN

    fake_bridge.unreachable = False
    await hass.services.async_call(
        "cover", "set_cover_position", {ATTR_ENTITY_ID: "cover.door", ATTR_POSITION: 30}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [r for r in fake_bridge.requests if r["t"] == "set"][-1]["position"] == 30
    assert hass.states.get("cover.door").state == CoverState.CLOSING
