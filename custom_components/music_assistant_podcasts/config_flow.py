"""Config flow for Music Assistant Podcasts."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from music_assistant_client.exceptions import MusicAssistantClientException

from .api import (
    MAAuthError,
    MAConnectionError,
    MusicAssistantPodcastApi,
    async_login_and_create_token,
)
from .const import (
    CONF_DEFAULT_PLAYER,
    CONF_EPISODES_PER_FEED,
    CONF_FORCE_REFRESH,
    CONF_LIST_SIZE,
    CONF_SERVER_URL,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_EPISODES_PER_FEED,
    DEFAULT_LIST_SIZE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    UPDATE_INTERVAL_RANGE,
)

_LOGGER = logging.getLogger(__name__)


def _guess_server_url(hass: HomeAssistant) -> str | None:
    """Prefill the MA server URL from an existing core music_assistant entry."""
    for entry in hass.config_entries.async_entries("music_assistant"):
        url = entry.data.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _user_schema(hass: HomeAssistant, defaults: dict | None = None) -> vol.Schema:
    """Build the connection form schema."""
    defaults = defaults or {}
    suggested = defaults.get(CONF_SERVER_URL) or _guess_server_url(hass)
    return vol.Schema(
        {
            vol.Required(
                CONF_SERVER_URL,
                default=suggested or "http://homeassistant.local:8095",
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            vol.Optional(
                CONF_TOKEN,
                description={"suggested_value": defaults.get(CONF_TOKEN)},
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_USERNAME): str,
            vol.Optional(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class MAOptionsFlow(config_entries.OptionsFlow):
    """Options flow: list behaviour + default player."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        super().__init__()
        self._entry = entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EPISODES_PER_FEED,
                    default=options.get(
                        CONF_EPISODES_PER_FEED, DEFAULT_EPISODES_PER_FEED
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=50, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_LIST_SIZE,
                    default=options.get(CONF_LIST_SIZE, DEFAULT_LIST_SIZE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=500, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL_MINUTES,
                    default=options.get(
                        CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=UPDATE_INTERVAL_RANGE[0],
                        max=UPDATE_INTERVAL_RANGE[1],
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_DEFAULT_PLAYER,
                    description={"suggested_value": options.get(CONF_DEFAULT_PLAYER)},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Required(
                    CONF_FORCE_REFRESH,
                    default=options.get(CONF_FORCE_REFRESH, False),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Music Assistant Podcasts config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MAOptionsFlow:
        """Get the options flow for this handler."""
        return MAOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the initial connection setup."""
        errors: dict[str, str] = {}
        defaults: dict = {}

        if user_input is not None:
            server_url: str = user_input[CONF_SERVER_URL].rstrip("/")
            token: str = (user_input.get(CONF_TOKEN) or "").strip()
            username: str = (user_input.get(CONF_USERNAME) or "").strip()
            password: str = user_input.get(CONF_PASSWORD) or ""

            try:
                if username and password:
                    # log in and mint a long-lived token
                    token = await async_login_and_create_token(
                        self.hass, server_url, username, password
                    )
                if not token:
                    errors["base"] = "missing_auth"
                else:
                    await MusicAssistantPodcastApi(
                        self.hass, server_url, token
                    ).validate()
            except MAAuthError:
                errors["base"] = "invalid_auth"
            except MAConnectionError:
                errors["base"] = "cannot_connect"
            except (MusicAssistantClientException, OSError, TimeoutError):
                errors["base"] = "unknown"

            if not errors:
                data = {
                    CONF_SERVER_URL: server_url,
                    CONF_TOKEN: token,
                }
                if username:
                    data[CONF_USERNAME] = username
                return self.async_create_entry(
                    title="Music Assistant Podcasts", data=data
                )

            defaults[CONF_SERVER_URL] = server_url

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(self.hass, defaults),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict) -> ConfigFlowResult:
        """Handle reauth when the MA token was revoked."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token or credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            server_url: str = entry.data[CONF_SERVER_URL]
            token: str = (user_input.get(CONF_TOKEN) or "").strip()
            username: str = (user_input.get(CONF_USERNAME) or "").strip()
            password: str = user_input.get(CONF_PASSWORD) or ""
            try:
                if username and password:
                    token = await async_login_and_create_token(
                        self.hass, server_url, username, password
                    )
                if not token:
                    errors["base"] = "missing_auth"
                else:
                    await MusicAssistantPodcastApi(
                        self.hass, server_url, token
                    ).validate()
            except MAAuthError:
                errors["base"] = "invalid_auth"
            except MAConnectionError:
                errors["base"] = "cannot_connect"
            except (MusicAssistantClientException, OSError, TimeoutError):
                errors["base"] = "unknown"

            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_TOKEN: token},
                )
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema(
            {
                vol.Optional(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD
                    )
                ),
                vol.Optional(CONF_USERNAME): str,
                vol.Optional(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )
