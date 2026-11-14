"""What every entity of a Companion shares: its device, when it is available, when it updates."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import DOMAIN, Link, signal


class CompanionEntity(Entity):
    """An entity of the Companion of `entry`, named by the translation of `key`."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title)

    @property
    def link(self) -> Link:
        return self._entry.runtime_data

    @property
    def available(self) -> bool:
        """What the Companion reports is known only while it is connected and has sent its state."""
        return self.link.state is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, signal(self._entry.entry_id), self.async_write_ha_state))
