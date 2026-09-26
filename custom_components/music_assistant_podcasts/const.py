"""Constants for the Music Assistant Podcasts integration."""

from __future__ import annotations

DOMAIN = "music_assistant_podcasts"

MANUFACTURER = "Music Assistant Podcasts"

CONF_SERVER_URL = "server_url"
CONF_TOKEN = "token"  # nosec: config option *name*, not a secret
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # nosec: config option *name*, not a secret

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

# Play-state sync
# Short TTL for the api-level cache behind the card's progress polls (so
# rapid polls never hammer the MA server with one connection per poll). Set
# above the burst-poll spacing (2/6/12 s) so the middle poll hits the cache.
STATE_CACHE_TTL = 8
# The lightweight PlayStateCoordinator polls fast while something is playing
# and backs off to a slow interval when nothing is in progress.
PLAY_STATE_ACTIVE_INTERVAL = 60  # seconds
PLAY_STATE_IDLE_INTERVAL = 900  # seconds
# How long a uri the card asked about stays tracked by the play-state
# coordinator (so its finish/reset transition is seen even when it drops
# out of MA's in-progress list).
PLAY_STATE_TRACKED_TTL = 1800  # seconds
# Descriptions are fetched on demand (they can be long, so they never go
# into the episodes sensor) and effectively never change → long cache TTL.
DESC_CACHE_TTL = 6 * 3600  # seconds

# Service names
SERVICE_REFRESH = "refresh"
SERVICE_PLAY_EPISODE = "play_episode"

# Static asset serving for the bundled frontend card (Lovelace resource,
# versioned with the manifest version so the browser cache busts on updates)
CARD_STATIC_PATH = "/api/music_assistant_podcasts"
CARD_FILENAME = "music-assistant-podcasts-card.js"
