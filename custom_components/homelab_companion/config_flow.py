import re

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import CONF_KEY, DEFAULT_PORT, DOMAIN, address
from .session import Refused, connect, parse_key

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
SERVICE = "._hl-companion._tcp.local."


class HomelabCompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    # Set by the zeroconf step: the name announced and where it listens.
    _found: tuple[str, dict] | None = None

    async def _name(self, user_input: dict, errors: dict) -> str | None:
        """The name of the Companion that answers with this key, or the form's error."""
        try:
            key = parse_key(user_input[CONF_KEY])
        except ValueError:
            errors[CONF_KEY] = "invalid_key"
            return None
        try:
            session = await connect(await address(self.hass, user_input[CONF_HOST]), user_input[CONF_PORT], key)
        except (OSError, ValueError):
            errors["base"] = "cannot_connect"
            return None
        except Refused:
            errors["base"] = "invalid_auth"
            return None
        # The Companion takes one connection: the entry's own comes next.
        session.writer.close()
        return session.name

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and (name := await self._name(user_input, errors)):
            # The name is who the Companion is, wherever it is found. Adding
            # it again moves the entry to the new address and key.
            # Not stopped by a discovery card of the same Companion left
            # waiting: creating the entry closes that card.
            await self.async_set_unique_id(name, raise_on_progress=False)
            self._abort_if_unique_id_configured(updates=user_input)
            return self.async_create_entry(title=name, data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="redondo.local"): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Required(CONF_KEY): PASSWORD,
            }
        )
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        name = discovery_info.name.removesuffix(SERVICE)
        # What the firmware accepts as a name; anything else is not one.
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
            return self.async_abort(reason="invalid_discovery_info")
        # The host comes from the name, not from the announcement's target:
        # an announcement is not authenticated, the handshake is.
        found = {CONF_HOST: f"{name}.local", CONF_PORT: discovery_info.port}
        await self.async_set_unique_id(name)
        # One added by address follows its name from here on.
        self._abort_if_unique_id_configured(updates=found)
        self._found = (name, found)
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_discovered()

    async def async_step_discovered(self, user_input: dict | None = None) -> ConfigFlowResult:
        name, found = self._found
        errors: dict[str, str] = {}
        if user_input is not None and (answered := await self._name(found | user_input, errors)):
            if answered == name:
                return self.async_create_entry(title=name, data=found | user_input)
            # Somebody else answers there: not the one that was shown.
            errors["base"] = "wrong_device"
        schema = vol.Schema({vol.Required(CONF_KEY): PASSWORD})
        return self.async_show_form(step_id="discovered", data_schema=schema, description_placeholders={"name": name}, errors=errors)
