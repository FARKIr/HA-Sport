"""Buttons: refresh data, send a test notification."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SportConfigEntry
from .entity import SportEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: SportConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    rt = entry.runtime_data
    async_add_entities([RefreshButton(rt), TestNotifyButton(rt)])


class RefreshButton(SportEntity, ButtonEntity):
    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, runtime) -> None:
        super().__init__(runtime.coordinator, "refresh")

    async def async_press(self) -> None:
        self.coordinator._full_ts = 0
        self.coordinator._heavy_ts = 0
        await self.coordinator.async_request_refresh()


class TestNotifyButton(SportEntity, ButtonEntity):
    _attr_translation_key = "test_notification"
    _attr_icon = "mdi:bell-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime) -> None:
        super().__init__(runtime.coordinator, "test_notification")
        self.notifier = runtime.notifier

    async def async_press(self) -> None:
        self.notifier.async_test()
