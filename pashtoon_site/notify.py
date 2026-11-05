"""Alerts that reach her phone when the dashboard is closed: WhatsApp to her personal number, a free ntfy push, or
email. Every alert made by shop.alert() comes through dispatch(). Sending happens in a background thread so an order
never waits for a slow network. Quiet hours hold alerts and send one summary afterwards."""
import logging
import re
import smtplib
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx

from . import config, meta
from .db import execute, get_setting, q, q1, set_setting

log = logging.getLogger(__name__)
SYNC = False        # tests set True so sends happen inline
KINDS = {"order": "New orders", "question": "A customer needs you", "message": "New customer messages",
         "cancel": "Cancellations", "payment": "Payments"}


def _setting(key):
    from . import shop
    return shop.setting(key)


def _now() -> datetime:
    from . import shop
    return shop.now()


def _log(channel: str, ok: bool, detail: str, text: str) -> None:
    execute("INSERT INTO notify_log(channel,ok,detail,text,created_at) VALUES(?,?,?,?,?)",
            (channel, 1 if ok else 0, detail[:300], text[:200], _now().isoformat(timespec="seconds")))
    execute("DELETE FROM notify_log WHERE id NOT IN (SELECT id FROM notify_log ORDER BY id DESC LIMIT 200)")


def recent_log(n: int = 8) -> list[dict]:
    return q("SELECT * FROM notify_log ORDER BY id DESC LIMIT ?", (n,))


# ---------------------------------------------------------------- quiet hours

def in_quiet(now: datetime | None = None) -> bool:
    a, b = _setting("quiet_from"), _setting("quiet_to")
    if not a or not b or a == b:
        return False
    t = (now or _now()).strftime("%H:%M")
    return (a <= t < b) if a < b else (t >= a or t < b)


# ---------------------------------------------------------------- the channels

def owner_digits() -> str:
    from . import shop
    return shop.wa_number(_setting("owner_phone") or "")


def note_owner_message() -> None:
    """She messaged the business WhatsApp number: WhatsApp now lets us text her freely for 24 hours."""
    set_setting("owner_wa_last_in", _now().isoformat(timespec="seconds"))


def window_open() -> bool:
    last = _setting("owner_wa_last_in")
    try:
        return bool(last) and _now() - datetime.fromisoformat(last) < timedelta(hours=23, minutes=30)
    except ValueError:
        return False


def _template_text(text: str) -> str:
    return re.sub(r"\s{2,}", " ", text.replace("\n", " ").replace("\t", " ")).strip()[:900]


def send_whatsapp(text: str) -> tuple[bool, str]:
    to = owner_digits()
    if not to:
        return False, "no WhatsApp number set"
    if not meta.enabled("whatsapp"):
        return False, "WhatsApp isn't connected (keys missing)"
    if window_open() or not config.WHATSAPP_OWNER_TEMPLATE:
        ok = meta.send_text("whatsapp", to, text)
        return ok, "text message" if ok else ("failed" + ("" if window_open() else ": no template set and her 24 hour window is closed"))
    ok = meta.send_template(to, config.WHATSAPP_OWNER_TEMPLATE, config.WHATSAPP_TEMPLATE_LANG, [_template_text(text)])
    return ok, "template message" if ok else "template send failed"


def send_ntfy(text: str, title: str = "") -> tuple[bool, str]:
    topic = _setting("ntfy_topic")
    if not topic:
        return False, "no ntfy topic"
    try:
        r = httpx.post(f"{config.NTFY_SERVER}/{topic}", content=text.encode(), timeout=10,
                       headers={"Title": (title or _setting("shop_name"))[:80], "Priority": "high", "Tags": "bell"})
        return r.status_code < 300, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, type(e).__name__


def smtp_ready() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def send_email(subject: str, text: str, to: str | None = None, attach: tuple[str, bytes] | None = None) -> tuple[bool, str]:
    to = to or _setting("owner_email")
    if not to:
        return False, "no email address"
    if not smtp_ready():
        return False, "SMTP isn't set up (keys missing)"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject[:150], config.SMTP_FROM, to
    msg.set_content(text)
    if attach:
        msg.add_attachment(attach[1], maintype="application", subtype="octet-stream", filename=attach[0])
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
            s.starttls()
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return True, "sent"
    except (OSError, smtplib.SMTPException) as e:
        return False, type(e).__name__


def send_now(text: str, title: str = "") -> list[tuple[str, bool, str]]:
    """Send to every channel she has switched on. Email goes only if asked for, or if the others all failed."""
    results = []
    if _setting("alert_whatsapp") and _setting("owner_phone"):
        results.append(("whatsapp", *send_whatsapp(text)))
    if _setting("alert_ntfy") and _setting("ntfy_topic"):
        results.append(("ntfy", *send_ntfy(text, title)))
    mode = _setting("alert_email")
    if mode == "always" or (mode == "fallback" and not any(ok for _, ok, _ in results)):
        if _setting("owner_email"):
            results.append(("email", *send_email(title or f"{_setting('shop_name')}: alert", text)))
    for ch, ok, detail in results:
        _log(ch, ok, detail, text)
    return results


# ---------------------------------------------------------------- called from shop.alert()

def dispatch(kind: str, text: str, alert_id: int) -> None:
    if not (_setting("alert_kinds") or {}).get(kind):
        return
    if in_quiet() and not (kind == "cancel" and _setting("quiet_urgent")):
        execute("UPDATE alerts SET notified=2 WHERE id=?", (alert_id,))
        return
    execute("UPDATE alerts SET notified=1 WHERE id=?", (alert_id,))
    if SYNC:
        send_now(text)
    else:
        threading.Thread(target=_safe_send, args=(text,), daemon=True).start()


def _safe_send(text: str) -> None:
    try:
        send_now(text)
    except Exception:  # noqa: BLE001  (an alert must never break an order)
        log.exception("sending an alert failed")


def flush_held() -> int:
    """When quiet hours end, send one summary of everything held. Returns how many alerts were summarised."""
    if in_quiet():
        return 0
    held = q("SELECT * FROM alerts WHERE notified=2 ORDER BY id")
    if not held:
        return 0
    counts = {}
    for a in held:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
    names = {"order": "new order", "question": "customer question", "message": "customer message", "cancel": "cancellation",
             "payment": "payment"}
    parts = [f"{n} {names.get(k, k)}{'' if n == 1 else 's'}" for k, n in counts.items()]
    text = "While you were off: " + ", ".join(parts) + ". " + " | ".join(a["text"][:90] for a in held[-3:])
    execute("UPDATE alerts SET notified=1 WHERE notified=2")
    results = send_now(text, "While you were off")
    return len(held) if results is not None else 0
