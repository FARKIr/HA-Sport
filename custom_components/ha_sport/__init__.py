"""HA Sport CZ/SK – football, ice hockey and basketball in Czechia and Slovakia."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import homeassistant.helpers.config_validation as cv

from .api import SofascoreClient
from .const import CONF_BASE_URL, DOMAIN, PLATFORMS
from .coordinator import SportCoordinator
from .frontend import async_register_frontend
from .notifications import SportNotifier
from .services import async_register_services
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class SportRuntime:
    client: SofascoreClient
    coordinator: SportCoordinator
    notifier: SportNotifier


type SportConfigEntry = ConfigEntry[SportRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register things shared by all entries (services, websocket, card)."""
    hass.data.setdefault(DOMAIN, {})
    async_register_services(hass)
    async_register_websocket(hass)
    await async_register_frontend(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SportConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = SofascoreClient(session, entry.options.get(CONF_BASE_URL) or entry.data.get(CONF_BASE_URL))
    hass.data[DOMAIN].setdefault("clients", {})[entry.entry_id] = client
    coordinator = SportCoordinator(hass, entry, client)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    notifier = SportNotifier(hass, coordinator)
    entry.runtime_data = SportRuntime(client, coordinator, notifier)
    notifier.async_start()
    entry.async_on_unload(notifier.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: SportConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SportConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].get("clients", {}).pop(entry.entry_id, None)
    return unloaded


def loaded_runtimes(hass: HomeAssistant) -> list[SportRuntime]:
    """All loaded entries' runtime data."""
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, SportRuntime):
            out.append(runtime)
    return out
