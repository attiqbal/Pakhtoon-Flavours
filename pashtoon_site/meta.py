"""Meta channels: WhatsApp (Cloud API), Instagram direct messages and Facebook Messenger, so the same assistant answers
customers there. One webhook (/webhook/meta) receives all three. Each channel stays off until its keys are in .env."""
import hashlib
import hmac
import logging

import httpx

from . import config

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"
CHANNELS = {"whatsapp": "WhatsApp", "instagram": "Instagram", "messenger": "Facebook Messenger"}


def enabled(channel: str) -> bool:
    if channel == "whatsapp":
        return bool(config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_ID)
    if channel == "instagram":
        return bool(config.INSTAGRAM_TOKEN)
    if channel == "messenger":
        return bool(config.META_PAGE_TOKEN)
    return False


def status() -> dict:
    return {"webhook_ready": bool(config.META_APP_SECRET and config.META_VERIFY_TOKEN),
            **{c: enabled(c) for c in CHANNELS}}


def signature_ok(body: bytes, header: str) -> bool:
    if not config.META_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(config.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _unsupported(kind: str) -> str:
    return f"[The customer sent a {kind or 'file'}, which can't be read here. Ask them to type it.]"


def parse(payload: dict) -> list[dict]:
    """Incoming customer messages from any of the three channels: [{'channel', 'from', 'name', 'text', 'id'}].
    Messages the business sent itself (echoes) are left out."""
    out, obj = [], payload.get("object", "")
    for entry in payload.get("entry", []):
        if obj == "whatsapp_business_account":
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    text = m.get("text", {}).get("body", "") if m.get("type") == "text" else _unsupported(m.get("type"))
                    out.append({"channel": "whatsapp", "from": m.get("from", ""), "name": names.get(m.get("from"), ""),
                                "text": text, "id": m.get("id", "")})
        elif obj in ("instagram", "page"):
            channel = "instagram" if obj == "instagram" else "messenger"
            for ev in entry.get("messaging", []):
                msg = ev.get("message") or {}
                if not msg or msg.get("is_echo"):
                    continue
                text = msg.get("text") or _unsupported((msg.get("attachments") or [{}])[0].get("type", "file"))
                out.append({"channel": channel, "from": (ev.get("sender") or {}).get("id", ""), "name": "",
                            "text": text, "id": msg.get("mid", "")})
    return [m for m in out if m["from"]]


def send_text(channel: str, to: str, text: str) -> bool:
    """Send a reply on the channel the customer used. Returns False if the channel isn't set up or the send failed."""
    if not enabled(channel) or not to:
        return False
    try:
        if channel == "whatsapp":
            r = httpx.post(f"{GRAPH}/{config.WHATSAPP_PHONE_ID}/messages", timeout=15,
                           headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"},
                           json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
        else:
            token = config.INSTAGRAM_TOKEN if channel == "instagram" else config.META_PAGE_TOKEN
            host = "https://graph.instagram.com/v21.0" if channel == "instagram" else GRAPH
            r = httpx.post(f"{host}/me/messages", timeout=15, params={"access_token": token},
                           json={"recipient": {"id": to}, "messaging_type": "RESPONSE", "message": {"text": text[:1900]}})
        if r.status_code >= 300:
            log.warning("%s send failed: %s %s", channel, r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError:
        log.exception("%s send failed", channel)
        return False
