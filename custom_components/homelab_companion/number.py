"""The Companion's settings, kept by Home Assistant. A new one is one entry in NUMBERS.

The Companion stores nothing: the values live in the config entry's options,
keyed by their names on the wire, and go to it in a `config` message after
every handshake (`Link.run`) and when one changes here.
"""

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import protocol
from .entity import CompanionEntity

DAY_MS = 24 * 60 * 60 * 1000


def duration(key: str) -> NumberEntityDescription:
    """A time in milliseconds, as on the wire; the user picks the unit shown. 0 turns its step off."""
    return NumberEntityDescription(
        key=key,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        native_min_value=0,
        native_max_value=DAY_MS,
        native_step=1000,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )


def percent(key: str) -> NumberEntityDescription:
    return NumberEntityDescription(
        key=key,
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    )


# `key` is the field of the `config` message, the option's name, the unique id and the translation key.
NUMBERS = (duration("dim_ms"), duration("off_ms"), percent("awake_pct"), percent("dim_pct"))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(CompanionNumber(entry, description) for description in NUMBERS)


class CompanionNumber(CompanionEntity, NumberEntity):
    def __init__(self, entry: ConfigEntry, description: NumberEntityDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """The value is Home Assistant's: it can be set while the Companion is away."""
        return True

    @property
    def native_value(self) -> int:
        """The stored value; the firmware's own default until one is set."""
        key = self.entity_description.key
        return self._entry.options.get(key, protocol.DEFAULTS[key])

    async def async_set_native_value(self, value: float) -> None:
        key = self.entity_description.key
        # No update listener is registered, so this does not reload the entry (and drop the connection).
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, key: int(value)})
        self.async_write_ha_state()
        try:
            await self.link.send(protocol.config(**{key: int(value)}))
        except HomeAssistantError:
            # Not connected: it goes with the others at the next connection.
            pass
