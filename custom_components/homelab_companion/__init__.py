"""Home Lab Companion: Home Assistant connects to the round display and sends it notifications."""

import asyncio
import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from zeroconf import AddressResolver, IPVersion

from . import protocol
from .session import Refused, Session, connect, parse_key

_LOGGER = logging.getLogger(__name__)

DOMAIN = "homelab_companion"
DEFAULT_PORT = 7010
PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CONF_KEY = "key"
RETRY_SECONDS = 5
MDNS_TIMEOUT_MS = 3000
# The boot time is the clock minus the uptime the Companion reports: it
# moves a little with every state, and only a move above this is a new boot.
BOOT_JITTER = timedelta(seconds=10)

NOTIFY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        # Not checked against a list: the Companion decides what a kind shows.
        vol.Required("kind"): cv.string,
        vol.Required("urgent"): cv.boolean,
        # On the wire it is milliseconds, which the Companion takes in 32 bits.
        vol.Required("seconds"): vol.All(vol.Coerce(int), vol.Range(min=1, max=(2**32 - 1) // 1000)),
        vol.Required("title"): cv.string,
        vol.Optional("body", default=""): cv.string,
    }
)


def signal(entry_id: str) -> str:
    """The dispatcher signal sent when an entry's connection opens or ends, or its Companion's state arrives."""
    return f"{DOMAIN}_connected_{entry_id}"


def event_signal(entry_id: str) -> str:
    """The dispatcher signal that carries an `event` message of an entry's Companion."""
    return f"{DOMAIN}_event_{entry_id}"


async def address(hass: HomeAssistant, host: str) -> str:
    """A `.local` name's IPv4 address by Home Assistant's own mDNS; any other host as it is."""
    # Home Assistant's container has no mDNS in its system resolver. Asked
    # at every try, so a Companion whose address changed is found again.
    name = host.rstrip(".").lower()
    if name.endswith(".local"):
        resolver = AddressResolver(f"{name}.")
        if await resolver.async_request(await zeroconf.async_get_instance(hass), MDNS_TIMEOUT_MS):
            if found := resolver.ip_addresses_by_version(IPVersion.V4Only):
                return str(found[0])
    return host


class Link:
    """The connection to one Companion, kept open by `run`."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.session: Session | None = None
        # The Companion's last `state` message; None until one arrives on this connection.
        self.state: dict | None = None
        # When the Companion started, from the uptime in its state.
        self.boot: datetime | None = None

    def _set(self, session: Session | None) -> None:
        self.session = session
        self.state = None
        async_dispatcher_send(self.hass, signal(self.entry.entry_id))

    def _receive(self, line: bytes) -> None:
        """One line of the Companion, to what shows it."""
        message = protocol.decode(line)
        if message is None:
            _LOGGER.debug("%s: a line that is no message dropped", self.entry.title)
        elif message["t"] == "state":
            self.state = message
            if type(uptime := message.get("uptime_ms")) is int:
                boot = dt_util.utcnow() - timedelta(milliseconds=uptime)
                if self.boot is None or abs(boot - self.boot) > BOOT_JITTER:
                    self.boot = boot
            if isinstance(version := message.get("version"), str):
                # A no-op when the version did not change.
                dr.async_get(self.hass).async_get_or_create(
                    config_entry_id=self.entry.entry_id, identifiers={(DOMAIN, self.entry.entry_id)}, sw_version=version
                )
            async_dispatcher_send(self.hass, signal(self.entry.entry_id))
        elif message["t"] == "event":
            async_dispatcher_send(self.hass, event_signal(self.entry.entry_id), message)

    async def run(self) -> None:
        host, port = self.entry.data[CONF_HOST], self.entry.data[CONF_PORT]
        key = parse_key(self.entry.data[CONF_KEY])
        problem = None
        while True:
            try:
                session = await connect(await address(self.hass, host), port, key)
                if session.name != self.entry.unique_id:
                    session.writer.close()
                    raise Refused(f"it is the Companion {session.name!r}, not {self.entry.unique_id!r}")
            # OSError includes the timeout; ValueError is a host name that
            # cannot be encoded (`a..b`). Either way the next try may work.
            except (OSError, ValueError) as err:
                _LOGGER.debug("%s:%s: %s", host, port, err)
            except Refused as err:
                # Once, not at every try: this one needs somebody to act.
                text = str(err) or "the key was refused, or it is not a Companion"
                if text != problem:
                    _LOGGER.warning("%s:%s: %s", host, port, text)
                problem = text
            else:
                problem = None
                self._set(session)
                try:
                    # Ends when the Companion closes (EOF) or vanishes (the
                    # keepalive's error).
                    async for line in session.lines():
                        try:
                            self._receive(line)
                        # Whatever a line holds, this task lives: nothing else would reconnect.
                        except Exception:
                            _LOGGER.exception("%s: a line dropped", self.entry.title)
                except OSError as err:
                    _LOGGER.debug("%s:%s: %s", host, port, err)
                finally:
                    self._set(None)
                    session.writer.close()
            await asyncio.sleep(RETRY_SECONDS)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def notify(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError("config_entry_id is not a Home Lab Companion")
        data = call.data
        try:
            payload = protocol.encode(
                protocol.notify(data["kind"], data["urgent"], data["seconds"] * 1000, data["title"], data["body"])
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        session = entry.runtime_data.session if entry.state is ConfigEntryState.LOADED else None
        if session is None:
            raise HomeAssistantError(f"{entry.title} is not connected")
        try:
            await session.send(payload)
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
