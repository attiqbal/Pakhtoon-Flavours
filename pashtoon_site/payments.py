"""Optional card payments with Stripe Checkout. Stripe's hosted page shows cards, Apple Pay and Google Pay (whichever
are switched on in the Stripe dashboard), so no card details ever touch this site. Off until STRIPE_SECRET_KEY is set
and card payments are ticked in Settings."""
import hashlib
import hmac
import logging
import time

import httpx

from . import config
from .db import get_setting

log = logging.getLogger(__name__)
API = "https://api.stripe.com/v1"


def configured() -> bool:
    return bool(config.STRIPE_SECRET_KEY)


def card_enabled() -> bool:
    return configured() and bool(get_setting("pay_card", True))


def methods() -> list[tuple[str, str]]:
    """Payment methods customers can choose, in order."""
    out = []
    if card_enabled():
        out.append(("card", "Card, Apple Pay or Google Pay"))
    if get_setting("pay_cash", True):
        out.append(("cash", "Cash on collection or delivery"))
    if get_setting("pay_bank", False):
        out.append(("bank", "Bank transfer"))
    return out or [("cash", "Cash on collection or delivery")]


def label(method: str) -> str:
    return {"card": "Card (online)", "cash": "Cash", "bank": "Bank transfer"}.get(method, method)


def create_checkout(o: dict) -> str:
    """A Stripe Checkout page for this order. Returns its URL, or raises RuntimeError."""
    data = {
        "mode": "payment",
        "client_reference_id": str(o["id"]),
        "metadata[order_id]": str(o["id"]),
        "metadata[order_code]": o["code"],
        "payment_intent_data[metadata][order_id]": str(o["id"]),
        "success_url": f"{config.BASE_URL}/o/{o['code']}?stripe_session={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{config.BASE_URL}/o/{o['code']}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": str(round(o["total"] * 100)),
        "line_items[0][price_data][product_data][name]": f"Order no. {o['id']} for {o['date_pretty']}",
        "expires_at": str(int(time.time()) + 60 * 60),
    }
    if o.get("email"):
        data["customer_email"] = o["email"]
    try:
        r = httpx.post(f"{API}/checkout/sessions", data=data, auth=(config.STRIPE_SECRET_KEY, ""), timeout=20)
    except httpx.HTTPError as e:
        raise RuntimeError("Couldn't reach Stripe") from e
    if r.status_code >= 300:
        log.warning("Stripe checkout failed: %s %s", r.status_code, r.text[:300])
        raise RuntimeError("Stripe refused the payment page")
    return r.json()["url"]


def paid_order_id(session_id: str) -> int | None:
    """Ask Stripe whether a checkout session was paid; returns its order id if so."""
    if not configured() or not session_id.startswith("cs_"):
        return None
    try:
        r = httpx.get(f"{API}/checkout/sessions/{session_id}", auth=(config.STRIPE_SECRET_KEY, ""), timeout=20)
        s = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    if s.get("payment_status") == "paid":
        try:
            return int((s.get("metadata") or {}).get("order_id"))
        except (TypeError, ValueError):
            return None
    return None


def webhook_ok(body: bytes, header: str, tolerance: int = 300) -> bool:
    """Check Stripe's signature header (t=...,v1=...) with STRIPE_WEBHOOK_SECRET."""
    if not config.STRIPE_WEBHOOK_SECRET or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    sigs = [p.split("=", 1)[1] for p in header.split(",") if p.startswith("v1=")]
    try:
        ts = int(parts.get("t", "0"))
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(config.STRIPE_WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)
