"""Google Calendar sync: every order becomes an event in a calendar called "<shop> orders" in her own Google account.

She signs in once (Owner > Settings > Connect Google Calendar). After that the site keeps the calendar in step:
new orders appear, changes move or rewrite the event, cancelled orders are marked CANCELLED. No duplicates: each
order always uses the same event id ("pforder000012"), so a retry updates the event instead of adding a second one.

Only needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (docs/google-calendar.md). Uses the narrow scope
calendar.app.created, so the site can only touch the calendar it made, never the rest of her Google Calendar.
Calendar trouble never blocks an order: failures are logged, shown in Settings and retried every minute.
"""
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import config
from .db import execute, get_setting, q, q1, set_setting

log = logging.getLogger(__name__)
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
SYNC = False            # tests set True so kick() runs inline
LOOKBACK_DAYS = 7       # orders older than this are left alone
_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}


class GoogleError(Exception):
    pass


def configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def connected() -> bool:
    return bool(get_setting("google_refresh_token") and get_setting("google_calendar_id"))


def status() -> dict:
    return {"configured": configured(), "connected": connected(), "error": get_setting("google_error", ""),
            "last_sync": get_setting("google_last_sync", ""), "redirect_uri": redirect_uri(),
            "calendar_url": "https://calendar.google.com/calendar/"}


def redirect_uri() -> str:
    return f"{config.BASE_URL}/owner/google/callback"


# ---------------------------------------------------------------- OAuth

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state, "include_granted_scopes": "false"})


def finish_connect(code: str, shop_name: str) -> None:
    """Swap the one-time code for a long-lived refresh token, make the calendar and queue every order for sending."""
    r = httpx.post(TOKEN_URL, timeout=20, data={
        "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    data = r.json() if r.content else {}
    if r.status_code != 200 or not data.get("refresh_token"):
        raise GoogleError(data.get("error_description") or data.get("error") or "Google did not accept the sign-in.")
    set_setting("google_refresh_token", data["refresh_token"])
    _token.update(value=data.get("access_token"), expires=time.time() + int(data.get("expires_in", 0)) - 60)
    cal = _call("POST", "/calendars", json={"summary": f"{shop_name} orders", "timeZone": str(config.TZ),
                                             "description": "Orders from the website, kept up to date automatically."})
    set_setting("google_calendar_id", cal["id"])
    set_setting("google_error", "")
    execute("UPDATE orders SET gcal_hash=NULL")      # a fresh calendar: send everything again
    kick()


def disconnect() -> None:
    token = get_setting("google_refresh_token")
    if token:
        try:
            httpx.post(REVOKE_URL, data={"token": token}, timeout=10)
        except httpx.HTTPError:
            pass
    for key in ("google_refresh_token", "google_calendar_id", "google_error", "google_last_sync"):
        set_setting(key, "")
    _token.update(value=None, expires=0.0)
    execute("UPDATE orders SET gcal_hash=NULL")


def _access_token() -> str:
    if _token["value"] and _token["expires"] > time.time():
        return _token["value"]
    r = httpx.post(TOKEN_URL, timeout=20, data={
        "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": get_setting("google_refresh_token"), "grant_type": "refresh_token"})
    data = r.json() if r.content else {}
    if r.status_code != 200:
        if data.get("error") == "invalid_grant":     # she removed the site's access in her Google account
            set_setting("google_refresh_token", "")
            set_setting("google_error", "Google access was removed. Connect Google Calendar again.")
        raise GoogleError(data.get("error_description") or data.get("error") or "Could not refresh the Google sign-in.")
    _token.update(value=data["access_token"], expires=time.time() + int(data.get("expires_in", 3600)) - 60)
    return _token["value"]


def _call(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, API + path, timeout=20, headers={"Authorization": f"Bearer {_access_token()}"}, **kw)
    if r.status_code == 401:
        _token.update(value=None, expires=0.0)
    if r.status_code == 409:
        raise FileExistsError(path)
    if r.status_code >= 400:
        raise GoogleError(f"{r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


# ---------------------------------------------------------------- events

def event_id(order_id: int) -> str:
    """Google accepts ids made of a-v and 0-9. Same order, same id, so never two events for one order."""
    return f"pforder{order_id:06d}"


def _event_body(o: dict) -> dict:
    from . import shop
    start = datetime.strptime(f"{o['date']} {o['time']}", "%Y-%m-%d %H:%M")
    cancelled = o["status"] == "cancelled"
    dishes = "\n".join(f"{i['qty']} x {i['dish']} ({i['portion']})" for i in o["items"])
    paid = {"paid": "Paid", "reported": "Customer says paid, check your bank"}.get(o["payment_status"], "Not paid yet")
    where = f"Delivery to {o['address']} {o['postcode'] or ''}".strip() if o["fulfilment"] == "delivery" else "Collection"
    desc = (f"{dishes}\n\nTotal {shop.money(o['total'])} · {paid} · {shop.PAY_WORDS.get(o['payment_method'], o['payment_method'])}"
            f"\nStatus: {o['status']}\n{where}\nCustomer: {o['customer_name']} {o['phone']}"
            + (f"\nNotes: {o['notes']}" if o.get("notes") else "") + f"\n\n{config.BASE_URL}/owner/orders/{o['id']}")
    return {
        "id": event_id(o["id"]),
        "summary": f"{'CANCELLED ' if cancelled else ''}#{o['id']} {o['customer_name']} · {o['fulfilment']} · {shop.money(o['total'])}",
        "description": desc,
        "location": o["address"] if o["fulfilment"] == "delivery" and o.get("address") else "",
        "start": {"dateTime": start.isoformat(), "timeZone": str(config.TZ)},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": str(config.TZ)},
        "colorId": "8" if cancelled else ("2" if o["status"] in ("ready", "completed") else "5"),
        "transparency": "transparent" if cancelled else "opaque",
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
        "extendedProperties": {"private": {"pashtoonOrderId": str(o["id"])}},
    }


def _hash(body: dict) -> str:
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _upsert(cal_id: str, body: dict) -> None:
    base = f"/calendars/{cal_id}/events"
    try:
        _call("POST", base, json=body)
    except FileExistsError:
        _call("PUT", f"{base}/{body['id']}", json={**body, "status": "confirmed"})


def sync_pending() -> int:
    """Send every order that is new or changed since it was last sent. Returns how many were sent."""
    from . import shop
    if not (configured() and connected()) or not _lock.acquire(blocking=False):
        return 0
    sent, failed = 0, False
    try:
        cal_id = get_setting("google_calendar_id")
        since = (shop.now().date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        for row in q("SELECT id, gcal_hash FROM orders WHERE date>=? ORDER BY date, time, id", (since,)):
            o = shop.get_order(row["id"])
            body = _event_body(o)
            h = _hash(body)
            if h == row["gcal_hash"]:
                continue
            if o["status"] == "cancelled" and not row["gcal_hash"]:
                execute("UPDATE orders SET gcal_hash=? WHERE id=?", (h, o["id"]))      # never sent: nothing to cancel
                continue
            try:
                _upsert(cal_id, body)
            except GoogleError as e:
                log.warning("calendar sync failed for order %s: %s", o["id"], e)
                if not connected():          # access was revoked: _access_token already left the reconnect message
                    failed = True
                    break
                set_setting("google_error", str(e)[:200])
                failed = True
                continue
            except httpx.HTTPError as e:
                log.warning("calendar unreachable: %s", e)
                set_setting("google_error", "Could not reach Google. Will retry.")
                failed = True
                break
            execute("UPDATE orders SET gcal_hash=? WHERE id=?", (h, o["id"]))
            sent += 1
        if not failed and get_setting("google_error"):
            set_setting("google_error", "")
        if sent:
            set_setting("google_last_sync", shop.iso_now())
    finally:
        _lock.release()
    return sent


def kick() -> None:
    """An order was just placed or changed: sync now in the background (the minute-by-minute job catches anything missed)."""
    if not (configured() and connected()):
        return
    if SYNC:
        sync_pending()
    else:
        threading.Thread(target=_safe, daemon=True, name="gcal-sync").start()


def _safe() -> None:
    try:
        sync_pending()
    except Exception:  # noqa: BLE001
        log.exception("calendar sync crashed")
