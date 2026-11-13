import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import config_validation as cv

from . import DEFAULT_PORT, DOMAIN


class RedondoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        # Not tried here: the entry keeps trying by itself, so a Companion
        # that is off can be added.
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST].lower())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_HOST], data=user_input)
        schema = {vol.Required(CONF_HOST, default="redondo.local"): str, vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port}
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema))
