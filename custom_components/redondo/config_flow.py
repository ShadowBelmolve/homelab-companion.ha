import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import CONF_KEY, DEFAULT_PORT, DOMAIN, address
from .session import Refused, connect, parse_key

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class RedondoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
            await self.async_set_unique_id(name)
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
