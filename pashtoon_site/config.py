"""Settings from environment variables. Everything has a safe default so the site runs with no setup."""
import os
import secrets
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = _env("PASHTOON_DATA_DIR", "data")
DB_PATH = _env("PASHTOON_DB", os.path.join(DATA_DIR, "pashtoon_site.db"))
BASE_URL = _env("PASHTOON_BASE_URL", "http://localhost:8000").rstrip("/")
OWNER_PASSWORD = _env("OWNER_PASSWORD")          # used once, to set the owner's password on first start
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-5-5")
TZ = ZoneInfo("Europe/London")

# Optional: WhatsApp Cloud API, so the same assistant answers her WhatsApp. Leave empty to use the website chat only.
WHATSAPP_TOKEN = _env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = _env("WHATSAPP_PHONE_ID")
WHATSAPP_VERIFY_TOKEN = _env("WHATSAPP_VERIFY_TOKEN")
META_APP_SECRET = _env("META_APP_SECRET")


def secret_key() -> str:
    """SESSION_SECRET from the environment, or one generated once and kept in the data folder."""
    key = _env("SESSION_SECRET")
    if key:
        return key
    path = os.path.join(DATA_DIR, ".session_secret")
    try:
        if os.path.exists(path):
            return open(path).read().strip()
        os.makedirs(DATA_DIR, exist_ok=True)
        key = secrets.token_hex(32)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(key)
        return key
    except OSError:
        return secrets.token_hex(32)
