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
OWNER_EMAIL = _env("OWNER_EMAIL").lower()        # the owner's login email; with OWNER_PASSWORD it creates the owner on first start
OWNER_MUST_CHANGE = _env("OWNER_MUST_CHANGE") == "1"   # make the owner choose a new password at first login
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# The strongest model by default: it handles unusual questions, menu edits and accounts best. Owners can pick a
# cheaper one in Settings. Override here with CLAUDE_MODEL in .env.
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-5-5")
MODELS = [("claude-opus-5-5", "Opus 5.5 (best, recommended)"), ("claude-sonnet-5-5", "Sonnet 5.5 (faster, cheaper)"),
          ("claude-haiku-5-5", "Haiku 5.5 (cheapest)")]
# Which AI company runs the assistant: "anthropic" (Claude) or "openai" (ChatGPT, or anything that speaks the same
# protocol, such as OpenRouter, Gemini's compatible endpoint or a local server). Can also be changed in Settings.
AI_PROVIDER = _env("AI_PROVIDER", "anthropic").lower()
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-5")
TZ = ZoneInfo("Europe/London")

# Optional Meta channels (see docs/meta-setup.md). One webhook, /webhook/meta, for all three. Empty = off.
META_APP_SECRET = _env("META_APP_SECRET")                 # Meta app > App settings > Basic > App secret
META_VERIFY_TOKEN = _env("META_VERIFY_TOKEN") or _env("WHATSAPP_VERIFY_TOKEN")   # any word you choose
WHATSAPP_TOKEN = _env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = _env("WHATSAPP_PHONE_ID")
INSTAGRAM_TOKEN = _env("INSTAGRAM_TOKEN")                 # Instagram API with Instagram Login, messages permission
META_PAGE_TOKEN = _env("META_PAGE_TOKEN")                 # Facebook Page access token, for Messenger

# Optional card payments through Stripe Checkout (cards, Apple Pay, Google Pay). Empty = off.
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")


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


# Alerts to her phone and email. All optional.
WHATSAPP_OWNER_TEMPLATE = _env("WHATSAPP_OWNER_TEMPLATE")           # approved utility template with one body variable
WHATSAPP_TEMPLATE_LANG = _env("WHATSAPP_TEMPLATE_LANG", "en_GB")
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM")
# Background jobs (backups, held alerts, tiffin orders). Run one server process; tests switch this off.
BACKGROUND = _env("PASHTOON_BACKGROUND", "1") != "0"
BACKUP_DIR = _env("PASHTOON_BACKUP_DIR") or os.path.join(DATA_DIR, "backups")
BACKUP_KEEP = int(_env("PASHTOON_BACKUP_KEEP", "30") or 30)
