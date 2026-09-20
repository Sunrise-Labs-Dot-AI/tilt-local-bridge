"""Battery sensors, one per shade."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TiltBridgeConfigEntry
from .coordinator import TiltBridgeCoordinator
from .entity import TiltShadeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TiltBridgeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(TiltShadeBattery(coordinator, shade.id) for shade in coordinator.data.shades)


class TiltShadeBattery(TiltShadeEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "battery"

    def __init__(self, coordinator: TiltBridgeCoordinator, shade_id: str) -> None:
        super().__init__(coordinator, shade_id)
        self._attr_unique_id = f"{coordinator.bridge_id}:{shade_id}:battery"

    @property
    def native_value(self) -> int | None:
        shade = self.shade
        return shade.battery if shade is not None else None
