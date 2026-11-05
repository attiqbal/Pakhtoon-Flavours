"""Tiffin service: weekly meal plans customers subscribe to. A subscription is the days of the week they want
(for example Monday to Friday), a collection time, a start date and how many weeks (or until they cancel). Each meal
becomes an ordinary order on the kitchen screen, created a few weeks ahead and topped up every day, so cooking,
stock, payments and the accounts all work the same as for any other order."""
import secrets
from datetime import date, datetime, timedelta

from . import geo, shop
from .db import execute, q, q1, tx
from .shop import ShopError, iso_now, parse_date, parse_time, setting, today

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HORIZON = 21            # days of meals created ahead
PAYMENTS = ("cash", "bank")      # card is per order in Stripe, so tiffin is paid weekly in cash or by transfer


# ---------------------------------------------------------------- plans

def plans(active_only: bool = True) -> list[dict]:
    out = q("SELECT * FROM tiffin_plans" + (" WHERE active=1" if active_only else "") + " ORDER BY sort, id")
    for p in out:
        p["meal_price"] = round(p["price_week"] / max(p["max_days"], 1), 2)
    return out


def plan(plan_id: int) -> dict:
    p = q1("SELECT * FROM tiffin_plans WHERE id=?", (int(plan_id),))
    if not p:
        raise ShopError("That tiffin plan doesn't exist.")
    p["meal_price"] = round(p["price_week"] / max(p["max_days"], 1), 2)
    return p


def find_plan(name_or_id) -> dict:
    if str(name_or_id).strip().isdigit():
        return plan(int(name_or_id))
    n = str(name_or_id or "").lower().strip()
    for p in plans(active_only=False):
        if n and (n in p["name"].lower() or p["name"].lower() in n):
            return p
    raise ShopError(f"No tiffin plan called '{name_or_id}'. Use get_tiffin_plans.")


def save_plan(plan_id: int | None, name: str, description: str, max_days: int, price_week: float, active: bool = True) -> int:
    name = (name or "").strip()[:80]
    if not name:
        raise ShopError("The plan needs a name.")
    if not 1 <= int(max_days) <= 7:
        raise ShopError("Meals per week must be between 1 and 7.")
    if not price_week or not 0 < float(price_week) < 10000:
        raise ShopError("The weekly price must be more than zero.")
    if plan_id:
        plan(plan_id)
        execute("UPDATE tiffin_plans SET name=?,description=?,max_days=?,price_week=?,active=? WHERE id=?",
                (name, (description or "").strip()[:600], int(max_days), round(float(price_week), 2), 1 if active else 0, plan_id))
        return plan_id
    sort = q1("SELECT COALESCE(MAX(sort),0)+1 n FROM tiffin_plans")["n"]
    return execute("INSERT INTO tiffin_plans(name,description,max_days,price_week,active,sort) VALUES(?,?,?,?,?,?)",
                   (name, (description or "").strip()[:600], int(max_days), round(float(price_week), 2), 1 if active else 0, sort))


def plans_text() -> str:
    rows = plans()
    if not rows:
        return "No tiffin plans are set up yet."
    return "\n".join(f"Plan {p['id']}: {p['name']}: {p['description']} Up to {p['max_days']} meals a week, £{p['price_week']:.2f} a week "
                     f"(£{p['meal_price']:.2f} a meal). Paid weekly by cash or bank transfer." for p in rows)


# ---------------------------------------------------------------- subscriptions

def _days(days) -> list[int]:
    out = []
    for d in days or []:
        if isinstance(d, str) and d[:3].title() in DAYS:
            d = DAYS.index(d[:3].title())
        try:
            d = int(d)
        except (TypeError, ValueError):
            raise ShopError("Choose the days of the week you want your meals.") from None
        if not 0 <= d <= 6:
            raise ShopError("Choose the days of the week you want your meals.")
        if d not in out:
            out.append(d)
    return sorted(out)


def subscribe(plan_id: int, name: str, phone: str, days, time: str, start_date: str, weeks: int = 0, fulfilment: str = "collection",
              address: str = "", postcode: str = "", payment_method: str = "cash", email: str = "", notes: str = "",
              by_owner: bool = False) -> dict:
    p = plan(plan_id)
    if not p["active"] and not by_owner:
        raise ShopError("That plan isn't available right now.")
    name = (name or "").strip()[:80]
    if not name:
        raise ShopError("Please tell us your name.")
    phone = shop.clean_phone(phone)
    days = _days(days)
    if not days:
        raise ShopError("Choose the days of the week you want your meals.")
    if len(days) > p["max_days"]:
        raise ShopError(f"This plan covers up to {p['max_days']} meals a week.")
    if payment_method not in PAYMENTS:
        raise ShopError("Tiffin is paid weekly in cash or by bank transfer.")
    if payment_method == "bank" and not setting("pay_bank") and not by_owner:
        raise ShopError("Bank transfer isn't switched on. Please choose cash.")
    if fulfilment not in ("collection", "delivery"):
        raise ShopError("Choose collection or delivery.")
    if fulfilment == "delivery":
        if len((address or "").strip()) < 5:
            raise ShopError("Please enter the delivery address.")
        shop.delivery_quote(postcode)
    t = parse_time(time)
    start = parse_date(start_date)
    # The first meal needs the same notice as any order.
    first = next((start + timedelta(days=i) for i in range(7) if (start + timedelta(days=i)).weekday() in days), start)
    if not by_owner:
        shop.check_slot(first.isoformat(), t, 24)
    weeks = max(0, min(int(weeks or 0), 52))
    stamp = iso_now()
    sid = execute("""INSERT INTO tiffin_subs(code,plan_id,plan_name,customer_name,phone,email,fulfilment,address,postcode,days,time,start_date,
                     weeks,payment_method,meal_price,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (secrets.token_urlsafe(9), p["id"], p["name"], name, phone, (email or "").strip()[:120] or None, fulfilment,
                   (address or "").strip()[:300] or None, geo.normalise(postcode) if postcode else None,
                   ",".join(map(str, days)), t, start.isoformat(), weeks, payment_method, p["meal_price"],
                   (notes or "").strip()[:400] or None, stamp))
    generate(sid)
    s = get(sid)
    shop.alert("order", f"New tiffin sign-up: {name}, {p['name']}, {', '.join(DAYS[d] for d in days)} at {t} from "
               f"{shop.pretty_date(start.isoformat())} ({'until cancelled' if not weeks else f'{weeks} weeks'}), £{p['meal_price'] * len(days):.2f} a week",
               f"/owner/tiffin")
    return s


def get(sub_id: int) -> dict:
    s = q1("SELECT * FROM tiffin_subs WHERE id=?", (int(sub_id),))
    if not s:
        raise ShopError("That tiffin subscription doesn't exist.")
    return _decorate(s)


def get_by_code(code: str) -> dict | None:
    s = q1("SELECT * FROM tiffin_subs WHERE code=?", (code or "",))
    return _decorate(s) if s else None


def _decorate(s: dict) -> dict:
    s["day_list"] = [int(x) for x in s["days"].split(",") if x]
    s["day_names"] = ", ".join(DAYS[d] for d in s["day_list"])
    s["week_total"] = round(s["meal_price"] * len(s["day_list"]), 2)
    return s


def subscriptions(status: str | None = None) -> list[dict]:
    rows = q("SELECT * FROM tiffin_subs" + (" WHERE status=?" if status else "") + " ORDER BY id DESC", (status,) if status else ())
    return [_decorate(r) for r in rows]


def meals(sub_id: int, upcoming_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM orders WHERE tiffin_id=?" + (" AND date>=?" if upcoming_only else "") + " ORDER BY date, id"
    return q(sql, (sub_id, today()) if upcoming_only else (sub_id,))


def _end_date(s: dict) -> date | None:
    return parse_date(s["start_date"]) + timedelta(days=7 * s["weeks"]) if s["weeks"] else None


def generate(sub_id: int, horizon: int = HORIZON) -> int:
    """Create the meals for the next few weeks that don't exist yet. Returns how many were made."""
    s = get(sub_id)
    if s["status"] != "active":
        return 0
    start, end = parse_date(s["start_date"]), _end_date(s)
    d, last = max(start, shop.now().date()), shop.now().date() + timedelta(days=horizon)
    closed = set(setting("closed_dates") or [])
    have = {r["date"] for r in q("SELECT date FROM orders WHERE tiffin_id=?", (s["id"],))}
    fee = 0.0
    if s["fulfilment"] == "delivery" and setting("offers_delivery"):
        try:
            fee = shop.delivery_quote(s["postcode"])["fee"]
        except ShopError:
            fee = 0.0
    made = 0
    while d <= last:
        iso = d.isoformat()
        if d.weekday() in s["day_list"] and (not end or d < end) and iso not in have and iso not in closed:
            stamp = iso_now()
            with tx() as c:
                oid = c.execute("""INSERT INTO orders(code,customer_name,phone,email,fulfilment,address,postcode,delivery_fee,food_total,total,date,time,
                                   payment_method,notes,source,tiffin_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (secrets.token_urlsafe(9), s["customer_name"], s["phone"], s["email"], s["fulfilment"], s["address"], s["postcode"],
                                 fee, s["meal_price"], round(s["meal_price"] + fee, 2), iso, s["time"], s["payment_method"],
                                 f"Tiffin: {s['plan_name']}" + (f". {s['notes']}" if s["notes"] else ""), "tiffin", s["id"], stamp, stamp)).lastrowid
                c.execute("INSERT INTO order_items(order_id,option_id,dish,portion,qty,unit_price) VALUES(?,NULL,?,?,1,?)",
                          (oid, f"Tiffin: {s['plan_name']}", "Meal", s["meal_price"]))
            made += 1
        d += timedelta(days=1)
    return made


def extend_all() -> int:
    return sum(generate(s["id"]) for s in subscriptions("active"))


def set_status(sub_id: int, status: str, by_customer: bool = False) -> str:
    s = get(sub_id)
    if status not in ("active", "paused", "cancelled"):
        raise ShopError("Status must be active, paused or cancelled.")
    execute("UPDATE tiffin_subs SET status=? WHERE id=?", (status, sub_id))
    if status in ("paused", "cancelled"):
        # Meals more than 24 hours away stop; ones already close stay (the food is being planned). The owner can cancel any.
        n = 0
        for m in meals(sub_id):
            if m["status"] == "confirmed" and (not by_customer or shop.hours_until(m) >= 24):
                execute("UPDATE orders SET status='cancelled', notes=COALESCE(notes,'') || ?, updated_at=? WHERE id=?",
                        (" [tiffin " + status + "]", iso_now(), m["id"]))
                n += 1
        return f"{s['customer_name']}'s tiffin is {status}; {n} upcoming meal{'' if n == 1 else 's'} cancelled."
    # resume: remove meals cancelled by the pause so they can be made again
    for m in q("SELECT id FROM orders WHERE tiffin_id=? AND status='cancelled' AND notes LIKE '%[tiffin paused]%' AND date>=?", (sub_id, today())):
        execute("DELETE FROM order_items WHERE order_id=?", (m["id"],))
        execute("DELETE FROM orders WHERE id=?", (m["id"],))
    generate(sub_id)
    return f"{s['customer_name']}'s tiffin is running again."
