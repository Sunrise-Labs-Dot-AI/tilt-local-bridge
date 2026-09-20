"""Cover entities: open, close, and set position. No stop, because the shade has none."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
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
    async_add_entities(TiltShadeCover(coordinator, shade.id) for shade in coordinator.data.shades)


class TiltShadeCover(TiltShadeEntity, CoverEntity):
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_name = None
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: TiltBridgeCoordinator, shade_id: str) -> None:
        super().__init__(coordinator, shade_id)
        self._attr_unique_id = f"{coordinator.bridge_id}:{shade_id}:cover"

    @property
    def current_cover_position(self) -> int | None:
        shade = self.shade
        return shade.position if shade is not None else None

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return None if position is None else position == 0

    @property
    def is_opening(self) -> bool:
        shade = self.shade
        return bool(
            shade is not None
            and shade.target is not None
            and shade.position is not None
            and shade.target > shade.position
        )

    @property
    def is_closing(self) -> bool:
        shade = self.shade
        return bool(
            shade is not None
            and shade.target is not None
            and shade.position is not None
            and shade.target < shade.position
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_position(self._shade_id, 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_position(self._shade_id, 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_position(self._shade_id, int(kwargs[ATTR_POSITION]))
