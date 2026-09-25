"""Constants for the Music Assistant Podcasts integration."""

from __future__ import annotations

DOMAIN = "music_assistant_podcasts"

MANUFACTURER = "Music Assistant Podcasts"

CONF_SERVER_URL = "server_url"
CONF_TOKEN = "token"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Options
CONF_EPISODES_PER_FEED = "episodes_per_feed"
CONF_LIST_SIZE = "list_size"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"
CONF_DEFAULT_PLAYER = "default_player"
CONF_FORCE_REFRESH = "force_refresh"

DEFAULT_EPISODES_PER_FEED = 5
DEFAULT_LIST_SIZE = 50
DEFAULT_UPDATE_INTERVAL_MINUTES = 60
DEFAULT_FORCE_REFRESH = False

MA_AUTH_DISPLAY_NAME = "Home Assistant Podcasts"

UPDATE_INTERVAL_RANGE = (5, 1440)  # minutes

# Sensor attributes
ATTR_EPISODES = "episodes"
ATTR_FEED_COUNT = "feed_count"
ATTR_LAST_UPDATED = "last_updated"
ATTR_FAILED_FEEDS = "failed_feeds"

# Episode dict keys (also consumed by the JS card)
EP_TITLE = "title"
EP_PODCAST = "podcast"
EP_PODCAST_URI = "podcast_uri"
EP_URI = "uri"
EP_PUBLISHED = "published"
EP_DURATION = "duration"
EP_POSITION = "position"
EP_PLAYED = "played"
EP_STATE = "state"  # unplayed | in_progress | finished
EP_IMAGE = "image"
EP_PODCAST_FAV = "podcast_fav"

# Service names
SERVICE_REFRESH = "refresh"
SERVICE_PLAY_EPISODE = "play_episode"

# Static asset serving for the bundled frontend card (Lovelace resource,
# versioned with the manifest version so the browser cache busts on updates)
CARD_STATIC_PATH = "/api/music_assistant_podcasts"
CARD_FILENAME = "music-assistant-podcasts-card.js"
