from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import CompanionEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ConnectedSensor(entry)])


class ConnectedSensor(CompanionEntity, BinarySensorEntity):
    """On while the encrypted connection to the Companion is open."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "connected")
        # Named by its device class and identified as before there were other entities.
        self._attr_translation_key = None
        self._attr_unique_id = entry.entry_id

    @property
    def available(self) -> bool:
        """It says whether the others can be: never unavailable itself."""
        return True

    @property
    def is_on(self) -> bool:
        return self.link.session is not None
