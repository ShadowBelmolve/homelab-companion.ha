"""The Companion's commands (`cmd`). A new one is one entry in BUTTONS."""

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import protocol
from .entity import CompanionEntity

# `key` is the command's `do`, the unique id and the translation key.
BUTTONS = (
    ButtonEntityDescription(key="wake"),
    ButtonEntityDescription(key="clear"),
    ButtonEntityDescription(key="restart", device_class=ButtonDeviceClass.RESTART, entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(CompanionButton(entry, description) for description in BUTTONS)


class CompanionButton(CompanionEntity, ButtonEntity):
    # A command needs the connection, not a state.
    needs_state = False

    def __init__(self, entry: ConfigEntry, description: ButtonEntityDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Sends the command; HomeAssistantError when the Companion is not connected."""
        await self.link.send(protocol.cmd(self.entity_description.key))
