"""Optional: connect her WhatsApp Business number (Meta Cloud API) so the same assistant answers there too.
Nothing here runs unless WHATSAPP_TOKEN and WHATSAPP_PHONE_ID are set."""
import hashlib
import hmac
import logging

import httpx

from . import config

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"


def enabled() -> bool:
    return bool(config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_ID)


def signature_ok(body: bytes, header: str) -> bool:
    if not config.META_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(config.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def parse(payload: dict) -> list[dict]:
    """Incoming text messages: [{'from', 'name', 'text', 'id'}]."""
    out = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                text = m.get("text", {}).get("body", "") if m.get("type") == "text" else \
                    f"[The customer sent a {m.get('type', 'file')}, which can't be read here. Ask them to type it.]"
                out.append({"from": m.get("from", ""), "name": names.get(m.get("from"), ""), "text": text, "id": m.get("id", "")})
    return out


def send_text(to: str, text: str) -> None:
    if not enabled() or not to:
        return
    try:
        httpx.post(f"{GRAPH}/{config.WHATSAPP_PHONE_ID}/messages", timeout=15,
                   headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"},
                   json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
    except httpx.HTTPError:
        log.exception("WhatsApp send failed")
