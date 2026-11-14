"""The Companion's `event` message: how a notification left its screen."""

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import event_signal
from .entity import CompanionEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([NotificationEvent(entry, "notification")])


class NotificationEvent(CompanionEntity, EventEntity):
    """`dismissed` by a tap or `expired` when its time ended, with the notification's `kind` and `title`."""

    _attr_event_types = ["dismissed", "expired"]

    @property
    def available(self) -> bool:
        """An event needs no state: the Companion may send one before its first."""
        return self.link.session is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, event_signal(self._entry.entry_id), self._ended))

    @callback
    def _ended(self, message: dict) -> None:
        # A newer firmware may send events this version does not know.
        if message.get("event") not in self.event_types:
            return
        self._trigger_event(message["event"], {"kind": message.get("kind"), "title": message.get("title")})
        self.async_write_ha_state()
