"""Redondo Companion: Home Assistant connects to the round display and sends it notifications."""

import asyncio
import logging
import socket

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "redondo"
DEFAULT_PORT = 7010
PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CONNECT_TIMEOUT = 10
RETRY_SECONDS = 5
# Kernel keepalive: a Companion that vanished without closing is noticed
# within idle + interval * probes = 30 s. It needs no heartbeat line.
# TCP_USER_TIMEOUT (in ms) covers what keepalive does not: a line sent and
# never acknowledged, which would otherwise be retried for some 15 minutes.
KEEPALIVE = {socket.TCP_KEEPIDLE: 10, socket.TCP_KEEPINTVL: 5, socket.TCP_KEEPCNT: 4, socket.TCP_USER_TIMEOUT: 30_000}

NOTIFY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        # Not checked against a list: the Companion decides what a kind shows.
        vol.Required("kind"): cv.string,
        vol.Required("urgent"): cv.boolean,
        # The Companion drops a line whose seconds do not fit in 32 bits.
        vol.Required("seconds"): vol.All(vol.Coerce(int), vol.Range(min=1, max=2**32 - 1)),
        vol.Required("title"): cv.string,
        vol.Optional("body", default=""): cv.string,
    }
)


def signal(entry_id: str) -> str:
    """The dispatcher signal sent when an entry's connection opens or ends."""
    return f"{DOMAIN}_connected_{entry_id}"


class Link:
    """The connection to one Companion, kept open by `run`."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.writer: asyncio.StreamWriter | None = None

    def _set(self, writer: asyncio.StreamWriter | None) -> None:
        self.writer = writer
        async_dispatcher_send(self.hass, signal(self.entry.entry_id))

    async def run(self) -> None:
        host, port = self.entry.data[CONF_HOST], self.entry.data[CONF_PORT]
        while True:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    reader, writer = await asyncio.open_connection(host, port)
            # OSError includes the timeout; ValueError is a host name that
            # cannot be encoded (`a..b`). Either way the next try may work.
            except (OSError, ValueError) as err:
                _LOGGER.debug("%s:%s: %s", host, port, err)
            else:
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for option, value in KEEPALIVE.items():
                    sock.setsockopt(socket.IPPROTO_TCP, option, value)
                self._set(writer)
                try:
                    # The Companion sends nothing: this ends when it closes
                    # (EOF) or vanishes (the keepalive's error).
                    while await reader.read(1024):
                        pass
                except OSError as err:
                    _LOGGER.debug("%s:%s: %s", host, port, err)
                finally:
                    self._set(None)
                    writer.close()
            await asyncio.sleep(RETRY_SECONDS)


def line(data: dict) -> bytes:
    """The Companion's `kind|urgent|seconds|title|body` line for a notify call."""
    for name in ("kind", "title"):
        if "|" in data[name]:
            raise ServiceValidationError(f"'{name}' cannot contain '|', the field separator")
    fields = (data["kind"], "1" if data["urgent"] else "0", str(data["seconds"]), data["title"], data["body"])
    # A line break would start another notification.
    return ("|".join(fields).translate({10: " ", 13: " "}) + "\n").encode()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def notify(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError("config_entry_id is not a Redondo Companion")
        payload = line(call.data)
        writer = entry.runtime_data.writer if entry.state is ConfigEntryState.LOADED else None
        if writer is None:
            raise HomeAssistantError(f"{entry.title} is not connected")
        try:
            writer.write(payload)
            await writer.drain()
        except OSError as err:
            raise HomeAssistantError(f"{entry.title}: {err}") from err

    hass.services.async_register(DOMAIN, "notify", notify, schema=NOTIFY_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry.runtime_data = link = Link(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Cancelled by Home Assistant when the entry unloads.
    entry.async_create_background_task(hass, link.run(), f"{DOMAIN} {entry.title}")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
