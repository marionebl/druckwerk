"""Constants for Druckwerk."""

from datetime import timedelta

DOMAIN = "druckwerk"
CONF_URL = "url"  # bridge URL; only the camera stream uses it
CONF_HUB_URL = "hub_url"
CONF_HUB_SECRET = "hub_secret"
CONF_RECIPIENTS = "recipients"  # {person entity_id: notify service}
CONF_UPLOAD_SECRET = "upload_secret"
UPLOAD_SECRET_HEADER = "X-Druckwerk-Secret"
HUB_SECRET_HEADER = "X-Hub-Secret"
ACT_AS_HEADER = "X-Druckwerk-Act-As"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MIN_SECRET_LENGTH = 24
HUB_PORT = 8099
BRIDGE_PORT = 8098
HUB_SLUG_SUFFIX = "_druckwerk"
# Hostname computed from the repository hash; used only when the Supervisor cannot tell.
DEFAULT_HUB_URL = "http://c9a833d4-druckwerk:8098"
SUPERVISOR_URL = "http://supervisor"
STATE_INTERVAL = timedelta(seconds=5)
API_VERSION = 1  # highest manifest api_version this integration speaks
FORWARD_TIMEOUT = 1860  # seconds; the longest app call is a start (400 s), this leaves room
PRESS_TIMEOUT = 400  # seconds; a print send waits up to 300 s for the bridge
UPLOAD_TIMEOUT = 300
CAMERA_MAX_AGE = 5  # seconds before a viewer triggers a new snapshot
CAMERA_STALE = 60  # older frames are not served; the viewer waits for a new one
CAMERA_TIMEOUT = 120  # the app itself waits up to 90 s for the bridge
