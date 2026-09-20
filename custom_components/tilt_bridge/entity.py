"""Shared entity base: one device per shade, hung off the bridge device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TiltBridgeCoordinator
from .protocol import ShadeState


def bridge_device_info(coordinator: TiltBridgeCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.bridge_id)},
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        name=coordinator.bridge_name,
        manufacturer="Sunrise Labs",
        model="Tilt Local Bridge",
    )


class TiltShadeEntity(CoordinatorEntity[TiltBridgeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TiltBridgeCoordinator, shade_id: str) -> None:
        super().__init__(coordinator)
        self._shade_id = shade_id
        shade = coordinator.data.shade(shade_id)
        shade_name = shade.name if shade is not None else shade_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.bridge_id}:{shade_id}")},
            name=shade_name,
            manufacturer="Tilt / SmarterHome",
            model="Smart Roller Shade",
            via_device=(DOMAIN, coordinator.bridge_id),
        )

    @property
    def shade(self) -> ShadeState | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.shade(self._shade_id)

    @property
    def available(self) -> bool:
        shade = self.shade
        return super().available and shade is not None and shade.available
