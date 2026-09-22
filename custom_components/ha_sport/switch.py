"""Switches: notifications on/off, live notifications on/off."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SportConfigEntry
from .entity import SportEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: SportConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    rt = entry.runtime_data
    async_add_entities([NotifySwitch(rt, "enabled", "notifications"), NotifySwitch(rt, "live_enabled", "live_notifications")])


class NotifySwitch(SportEntity, SwitchEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime, attr: str, key: str) -> None:
        super().__init__(runtime.coordinator, key)
        self.notifier = runtime.notifier
        self.attr = attr
        self._attr_translation_key = key
        self._attr_icon = "mdi:bell-ring" if attr == "enabled" else "mdi:bell-badge"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_OFF:
            setattr(self.notifier, self.attr, False)

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.notifier, self.attr))

    async def async_turn_on(self, **kwargs: Any) -> None:
        setattr(self.notifier, self.attr, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        setattr(self.notifier, self.attr, False)
        self.async_write_ha_state()
