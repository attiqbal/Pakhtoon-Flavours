"""Business rules: menu, availability, delivery, orders, stock, money and alerts. The website, the kitchen screen
and the AI assistant all go through these functions, so the rules are the same everywhere."""
import math
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config, geo, menu_data
from .db import execute, get_setting, q, q1, set_setting, tx

STATUSES = ["confirmed", "cooking", "ready", "completed", "cancelled"]
PAYMENT_STATUSES = ["unpaid", "reported", "paid"]
UNITS = {"g": ("g", 1), "gram": ("g", 1), "grams": ("g", 1), "kg": ("g", 1000), "ml": ("ml", 1), "l": ("ml", 1000),
         "litre": ("ml", 1000), "tbsp": ("ml", 15), "tsp": ("ml", 5), "cup": ("ml", 240), "pcs": ("pcs", 1), "pc": ("pcs", 1),
         "each": ("pcs", 1)}
WRITE_OFF_REASONS = ["Spoiled", "Out of date", "Dropped or damaged", "Cooking mistake", "Leftover", "Other"]
# Expense categories on the Money page, and the account each one goes to in the books (see accounts.py).
EXPENSE_ACCOUNTS = {"Packaging": "5400", "Spices, herbs and consumables": "5300", "Gas and electric": "6100",
                    "Delivery": "6200", "Small equipment": "6300", "Marketing": "6400", "Card and platform fees": "6500",
                    "Partner commission": "6600", "Phone and internet": "6700", "Other": "6900"}
EXPENSE_CATEGORIES = list(EXPENSE_ACCOUNTS)
PAY_WORDS = {"cash": "cash", "bank": "bank transfer", "card": "card online", "partner": "paid through the delivery app"}
PAID_FROM = {"1110": "Cash", "1120": "Bank", "2110": "Not paid yet (supplier owed)"}
# Words that mark an ingredient as a spice, herb or other small item that isn't weighed out per dish. These are not
# tracked in stock or shopping lists; buying them is a consumables expense.
UNTRACKED_WORDS = ["spice", "masala", "salt", "pepper", "chilli powder", "chili powder", "turmeric", "haldi", "cumin",
                   "zeera", "jeera", "coriander seed", "coriander powder", "garam", "cardamom", "elaichi", "clove", "cinnamon",
                   "bay lea", "star anise", "nutmeg", "mace", "fennel", "ajwain", "carom", "fenugreek", "methi", "saffron",
                   "paprika", "herb", "mint", "coriander leaves", "dhania", "parsley", "dill", "curry lea", "food colour",
                   "kewra", "rose water", "baking", "yeast", "oil spray", "foil", "cling", "gloves"]


class ShopError(Exception):
    """A problem to explain to the person (not a crash)."""


# ---------------------------------------------------------------- time and setup

def now() -> datetime:
    return datetime.now(config.TZ)


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def today() -> str:
    return now().date().isoformat()


def pretty_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%A %-d %B")


def parse_date(d: str) -> date:
    try:
        return datetime.strptime((d or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ShopError("Please choose a date.") from None


def parse_time(t: str) -> str:
    try:
        return datetime.strptime((t or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ShopError("Please choose a time.") from None


def setting(key: str):
    return get_setting(key, menu_data.DEFAULT_SETTINGS.get(key))


def settings() -> dict:
    return {k: setting(k) for k in menu_data.DEFAULT_SETTINGS}


def install() -> None:
    """First start: load her menu and settings into an empty database. Never touches existing data."""
    if not get_setting("installed"):
        with tx() as c:
            for sort, (slug, name, cat, desc, img, notice, opts) in enumerate(menu_data.MENU):
                cur = c.execute("INSERT INTO menu_items(slug,name,category,description,image,notice_hours,sort) VALUES(?,?,?,?,?,?,?)",
                                (slug, name, cat, desc, img, notice, sort))
                for i, (label, price) in enumerate(opts):
                    c.execute("INSERT INTO menu_options(item_id,label,price,sort) VALUES(?,?,?,?)", (cur.lastrowid, label, price, i))
        set_setting("installed", True)
        set_setting("added_tiffin", True)
    if not get_setting("added_tiffin"):        # sites started before the tiffin service existed
        set_setting("added_tiffin", True)
        if not q1("SELECT id FROM menu_items WHERE slug='tiffin'"):
            slug, name, cat, desc, img, notice, _ = next(m for m in menu_data.MENU if m[0] == "tiffin")
            sort = q1("SELECT COALESCE(MAX(sort),0)+1 n FROM menu_items")["n"]
            execute("INSERT INTO menu_items(slug,name,category,description,image,notice_hours,sort) VALUES(?,?,?,?,?,?,?)",
                    (slug, name, cat, desc, img, notice, sort))
    if not get_setting("media_v1"):            # her own photos and clips replace the first crops from the printed menu
        set_setting("media_v1", True)
        for slug, (photo, clip) in menu_data.DISH_MEDIA.items():
            execute("UPDATE menu_items SET image=?, video=COALESCE(video,?) WHERE slug=? AND (image IS NULL OR image NOT LIKE '/media/%')",
                    (photo, clip, slug))
        for kind, url, title in menu_data.LIBRARY_MEDIA:
            execute("INSERT INTO media(kind,url,title,created_at) VALUES(?,?,?,?)", (kind, url, title, iso_now()))
    for slug in menu_data.ENQUIRY_ITEMS:
        execute("UPDATE menu_items SET enquiry=1 WHERE slug=?", (slug,))
    if not get_setting("partner_key"):
        set_setting("partner_key", secrets.token_urlsafe(24))
    for key in ("calendar_token", "kitchen_token"):
        if not get_setting(key):
            set_setting(key, secrets.token_urlsafe(18))


def money(x: float) -> str:
    return f"£{x:,.2f}"


# ---------------------------------------------------------------- menu

def menu(include_hidden: bool = False) -> list[dict]:
    items = q("SELECT * FROM menu_items ORDER BY sort, id")
    opts = defaultdict(list)
    for o in q("SELECT * FROM menu_options WHERE active=1 ORDER BY sort, id"):
        opts[o["item_id"]].append(o)
    out = []
    for it in items:
        it["options"] = opts[it["id"]]
        if include_hidden or (it["available"] and (it["options"] or it["enquiry"])):
            out.append(it)
    return out


def menu_by_category(include_hidden: bool = False) -> list[tuple[str, str, list[dict]]]:
    items = menu(include_hidden)
    return [(k, label, [i for i in items if i["category"] == k]) for k, label in menu_data.CATEGORIES
            if any(i["category"] == k for i in items)]


def option(option_id: int) -> dict:
    o = q1("""SELECT o.*, i.name AS dish, i.notice_hours, i.available, i.id AS item_id FROM menu_options o
              JOIN menu_items i ON i.id=o.item_id WHERE o.id=?""", (option_id,))
    if not o or not o["active"]:
        raise ShopError("That dish isn't on the menu.")
    return o


def find_option(dish: str, portion: str = "") -> dict:
    """Match a dish name (and optional portion text) to a menu option, for the assistant."""
    dish_l, portion_l = (dish or "").lower().strip(), (portion or "").lower().strip()
    best = []
    for it in menu():
        if dish_l and (dish_l in it["name"].lower() or it["name"].lower() in dish_l or dish_l == it["slug"]):
            best = it["options"]
            break
    if not best:
        raise ShopError(f"'{dish}' isn't on the menu. Use get_menu for the exact dish names.")
    if len(best) == 1:
        return option(best[0]["id"])
    for o in best:
        if portion_l and (portion_l in o["label"].lower() or o["label"].lower() in portion_l):
            return option(o["id"])
    raise ShopError(f"Which portion of {dish}? Options: " + "; ".join(f"{o['label']} £{o['price']:.2f}" for o in best))


def update_price(option_id: int, price: float) -> str:
    o = option(option_id)
    if not 0 < price < 10000:
        raise ShopError("The price must be more than zero.")
    execute("UPDATE menu_options SET price=? WHERE id=?", (round(price, 2), option_id))
    return f"{o['dish']} ({o['label']}): £{o['price']:.2f} -> £{price:.2f}"


def set_available(item_id: int, available: bool) -> str:
    it = q1("SELECT * FROM menu_items WHERE id=?", (item_id,))
    if not it:
        raise ShopError("That dish isn't on the menu.")
    execute("UPDATE menu_items SET available=? WHERE id=?", (1 if available else 0, item_id))
    return f"{it['name']} is now {'on' if available else 'off'} the menu."


def save_item(item_id: int | None, name: str, category: str, description: str, notice_hours: int,
              options: list[tuple[int | None, str, float]], available: bool = True, enquiry: bool | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ShopError("The dish needs a name.")
    if category not in dict(menu_data.CATEGORIES):
        raise ShopError("Choose a section for the dish.")
    options = [(oid, (lbl or "").strip(), p) for oid, lbl, p in options if (lbl or "").strip()]
    if enquiry is None:
        enquiry = bool(item_id and (q1("SELECT enquiry FROM menu_items WHERE id=?", (item_id,)) or {}).get("enquiry"))
    if not options and not enquiry:
        raise ShopError("Add at least one portion with a price.")
    for _, lbl, p in options:
        if p is None or not 0 < p < 10000:
            raise ShopError(f"'{lbl}' needs a price.")
    with tx() as c:
        if item_id:
            c.execute("UPDATE menu_items SET name=?,category=?,description=?,notice_hours=?,available=?,enquiry=? WHERE id=?",
                      (name, category, description.strip(), notice_hours, 1 if available else 0, 1 if enquiry else 0, item_id))
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "-" + secrets.token_hex(2)
            sort = (c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM menu_items").fetchone()[0])
            item_id = c.execute("INSERT INTO menu_items(slug,name,category,description,notice_hours,available,sort,enquiry) VALUES(?,?,?,?,?,?,?,?)",
                                (slug, name, category, description.strip(), notice_hours, 1 if available else 0, sort,
                                 1 if enquiry else 0)).lastrowid
        keep = []
        for i, (oid, lbl, p) in enumerate(options):
            if oid:
                c.execute("UPDATE menu_options SET label=?,price=?,sort=?,active=1 WHERE id=? AND item_id=?", (lbl, round(p, 2), i, oid, item_id))
                keep.append(oid)
            else:
                keep.append(c.execute("INSERT INTO menu_options(item_id,label,price,sort) VALUES(?,?,?,?)",
                                      (item_id, lbl, round(p, 2), i)).lastrowid)
        marks = ",".join("?" * len(keep))
        c.execute(f"UPDATE menu_options SET active=0 WHERE item_id=? AND id NOT IN ({marks or 'NULL'})", (item_id, *keep))
    return item_id


def item(item_id: int) -> dict:
    it = q1("SELECT * FROM menu_items WHERE id=?", (item_id,))
    if not it:
        raise ShopError("That dish isn't on the menu.")
    it["options"] = q("SELECT * FROM menu_options WHERE item_id=? AND active=1 ORDER BY sort, id", (item_id,))
    return it


def find_item(name_or_id) -> dict:
    if isinstance(name_or_id, int) or str(name_or_id).strip().isdigit():
        return item(int(name_or_id))
    n = str(name_or_id or "").lower().strip()
    for it in menu(include_hidden=True):
        if n and (n == it["slug"] or n in it["name"].lower() or it["name"].lower() in n):
            return item(it["id"])
    raise ShopError(f"'{name_or_id}' isn't on the menu. Use get_menu for the exact names.")


def set_item_media(item_id: int, image: str | None = None, video: str | None = None) -> str:
    """Set a dish's photo or video to an uploaded file ('' removes it). None leaves it as it is."""
    it = item(item_id)
    if image is not None:
        execute("UPDATE menu_items SET image=? WHERE id=?", (image or None, item_id))
    if video is not None:
        execute("UPDATE menu_items SET video=? WHERE id=?", (video or None, item_id))
    return f"Updated the media for {it['name']}."


# ---------------------------------------------------------------- availability and delivery

def time_slots() -> list[str]:
    start, end, step = parse_time(setting("slot_start")), parse_time(setting("slot_end")), int(setting("slot_minutes") or 30)
    t, stop, out = datetime.strptime(start, "%H:%M"), datetime.strptime(end, "%H:%M"), []
    while t <= stop and len(out) < 100:
        out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=max(step, 5))
    return out


def orders_on(d: str, include_cancelled: bool = False) -> list[dict]:
    sql = "SELECT * FROM orders WHERE date=?" + ("" if include_cancelled else " AND status!='cancelled'") + " ORDER BY time, id"
    return [_with_items(o) for o in q(sql, (d,))]


def check_slot(d: str, t: str, notice_hours: int = 24, ignore_order: int | None = None) -> None:
    """Raise ShopError if the kitchen can't do this date and time."""
    if not setting("accepting_orders"):
        raise ShopError("Sorry, we're not taking new orders at the moment.")
    day, t = parse_date(d), parse_time(t)
    when = datetime.combine(day, datetime.strptime(t, "%H:%M").time(), tzinfo=config.TZ)
    if when <= now():
        raise ShopError("That time has already passed.")
    if when - now() < timedelta(hours=notice_hours):
        earliest = now() + timedelta(hours=notice_hours)
        raise ShopError(f"We need at least {notice_hours} hours' notice for this order, so the earliest is "
                        f"{earliest.strftime('%A %-d %B')} from {earliest.strftime('%H:%M')}.")
    if day.isoformat() in (setting("closed_dates") or []):
        raise ShopError(f"Sorry, we're closed on {pretty_date(day.isoformat())}.")
    slots = time_slots()
    if slots and not (slots[0] <= t <= slots[-1]):
        raise ShopError(f"We cook for collection or delivery between {slots[0]} and {slots[-1]}.")
    limit = int(setting("daily_order_limit") or 0)
    if limit:
        taken = [o for o in orders_on(day.isoformat()) if o["id"] != ignore_order and o["source"] != "tiffin"]
        if len(taken) >= limit:
            raise ShopError(f"Sorry, {pretty_date(day.isoformat())} is fully booked. Please choose another day.")


def earliest_day(notice_hours: int = 24) -> str | None:
    """The first day a new order can be cooked for, as 'Saturday 7 November' (None if not taking orders)."""
    if not setting("accepting_orders"):
        return None
    slots = time_slots()
    first = now() + timedelta(hours=notice_hours)
    for add in range(0, 60):
        day = (first + timedelta(days=add)).date()
        if day.isoformat() in (setting("closed_dates") or []):
            continue
        if add == 0 and slots and first.strftime("%H:%M") > slots[-1]:
            continue
        return pretty_date(day.isoformat())
    return None


def delivery_quote(postcode: str, food_total: float = 0) -> dict:
    """{'miles', 'fee', 'minimum'} for a UK postcode, or ShopError."""
    if not setting("offers_delivery"):
        raise ShopError("Sorry, we're only doing collection at the moment.")
    if not geo.valid(postcode):
        raise ShopError("Please enter a full UK postcode for delivery.")
    miles = geo.miles_between_postcodes(setting("kitchen_postcode"), postcode)
    if miles is None:
        raise ShopError("We couldn't find that postcode. Please check it, or choose collection.")
    cap = float(setting("max_delivery_miles") or 0)
    if cap and miles > cap:
        raise ShopError(f"Sorry, we deliver up to {cap:g} miles and that's about {miles:.1f} miles away.")
    for upto, fee, minimum in setting("delivery_bands"):
        if upto is None or miles <= upto:
            if minimum and food_total and food_total < minimum:
                raise ShopError(f"Delivery to {geo.normalise(postcode)} (about {miles:.1f} miles) needs a minimum order of £{minimum:.2f}.")
            return {"miles": miles, "fee": float(fee), "minimum": float(minimum or 0)}
    raise ShopError("Sorry, that's outside our delivery area.")


def delivery_bands_text() -> str:
    out, prev = [], 0
    for upto, fee, minimum in setting("delivery_bands"):
        extra = f" (minimum order £{minimum:g})" if minimum else ""
        out.append(f"Up to {upto:g} miles: £{fee:.2f}" if upto is not None and prev == 0 else
                   f"{prev:g} to {upto:g} miles: £{fee:.2f}" if upto is not None else f"Over {prev:g} miles: £{fee:.2f}{extra}")
        if upto is not None and minimum:
            out[-1] += extra
        prev = upto or prev
    return "\n".join(out)


# ---------------------------------------------------------------- orders

def _with_items(o: dict) -> dict:
    o["items"] = q("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (o["id"],))
    o["date_pretty"] = pretty_date(o["date"])
    return o


def get_order(order_id: int) -> dict:
    o = q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if not o:
        raise ShopError(f"There's no order number {order_id}.")
    return _with_items(o)


def get_order_by_code(code: str) -> dict | None:
    o = q1("SELECT * FROM orders WHERE code=?", (code,))
    return _with_items(o) if o else None


def clean_phone(phone: str) -> str:
    p = re.sub(r"[^\d+]", "", phone or "")
    if len(re.sub(r"\D", "", p)) < 10:
        raise ShopError("Please enter a mobile number so we can reach you about your order.")
    return p


def wa_number(phone: str) -> str:
    """UK number for a wa.me link: 07... -> 447..."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("0"):
        d = "44" + d[1:]
    return d


def quote(lines: list[dict], fulfilment: str, postcode: str = "", d: str = "", t: str = "", check_time: bool = True) -> dict:
    """lines: [{'option_id', 'qty'}]. Returns a priced quote or raises ShopError."""
    items, notice = [], 24
    for ln in lines:
        qty = int(ln.get("qty") or 0)
        if qty <= 0:
            continue
        if qty > 50:
            raise ShopError("For more than 50 of one dish, please message us so we can plan it with you.")
        o = option(int(ln["option_id"]))
        if not o["available"]:
            raise ShopError(f"Sorry, {o['dish']} isn't available right now.")
        notice = max(notice, o["notice_hours"])
        items.append({"option_id": o["id"], "dish": o["dish"], "portion": o["label"], "qty": qty, "unit_price": o["price"],
                      "line_total": round(o["price"] * qty, 2)})
    if not items:
        raise ShopError("Your basket is empty.")
    food = round(sum(i["line_total"] for i in items), 2)
    if fulfilment not in ("collection", "delivery"):
        raise ShopError("Choose collection or delivery.")
    if fulfilment == "collection" and not setting("offers_collection"):
        raise ShopError("Sorry, we're only doing delivery at the moment.")
    fee, miles = 0.0, None
    if fulfilment == "delivery":
        dq = delivery_quote(postcode, food)
        fee, miles = dq["fee"], dq["miles"]
    if check_time:
        check_slot(d, t, notice)
    return {"items": items, "food_total": food, "delivery_fee": fee, "miles": miles, "total": round(food + fee, 2),
            "notice_hours": notice, "fulfilment": fulfilment}


def place_order(lines: list[dict], name: str, phone: str, fulfilment: str, d: str, t: str, payment_method: str,
                address: str = "", postcode: str = "", email: str = "", notes: str = "", source: str = "web",
                by_owner: bool = False) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        raise ShopError("Please tell us your name.")
    phone = clean_phone(phone)
    from . import payments
    allowed = ("cash", "bank", "card") if by_owner else tuple(m for m, _ in payments.methods())
    if payment_method not in allowed:
        raise ShopError("Choose how you'll pay: " + ", ".join(lbl.lower() for m, lbl in payments.methods()) + ".")
    if fulfilment == "delivery" and len((address or "").strip()) < 5:
        raise ShopError("Please enter the delivery address.")
    qt = quote(lines, fulfilment, postcode, d, t)
    stamp = iso_now()
    with tx() as c:
        oid = c.execute("""INSERT INTO orders(code,customer_name,phone,email,fulfilment,address,postcode,miles,delivery_fee,
                           food_total,total,date,time,payment_method,notes,source,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (secrets.token_urlsafe(9), name, phone, (email or "").strip()[:120] or None, fulfilment,
                         (address or "").strip()[:300] if fulfilment == "delivery" else None,
                         geo.normalise(postcode) if fulfilment == "delivery" else None, qt["miles"], qt["delivery_fee"],
                         qt["food_total"], qt["total"], parse_date(d).isoformat(), parse_time(t), payment_method,
                         (notes or "").strip()[:500] or None, source, stamp, stamp)).lastrowid
        for i in qt["items"]:
            c.execute("INSERT INTO order_items(order_id,option_id,dish,portion,qty,unit_price) VALUES(?,?,?,?,?,?)",
                      (oid, i["option_id"], i["dish"], i["portion"], i["qty"], i["unit_price"]))
    o = get_order(oid)
    returning = q1("SELECT COUNT(*) n FROM orders WHERE phone=? AND id!=?", (phone, oid))["n"]
    alert("order", f"New order #{oid} from {name}: {money(o['total'])} for {o['date_pretty']} at {o['time']} ({fulfilment})"
          + ("" if returning else " · first-time customer"), f"/owner/orders/{oid}")
    return o


def place_partner_order(partner: str, lines: list[dict], name: str, d: str, t: str, total: float | None = None,
                        reference: str = "", notes: str = "", phone: str = "", unmatched: list[str] | None = None) -> dict:
    """An order taken by Uber Eats, Deliveroo or Just Eat. The partner collects the money and pays her later, less
    commission, so it's booked as owed by the partner. Notice and slot rules aren't applied: the partner's app has them.
    Sending the same reference twice returns the first order. Lines the menu couldn't match go in `unmatched` and are kept
    in the order's notes for her to check."""
    partners = setting("partners") or {}
    if partner not in partners:
        raise ShopError("Unknown delivery partner.")
    label = dict(menu_data.PARTNERS).get(partner, partner)
    if not partners[partner].get("on"):
        raise ShopError(f"{label} isn't switched on. Turn it on in Settings first.")
    reference = (reference or "").strip()[:60]
    if reference:
        dup = q1("SELECT id FROM orders WHERE partner=? AND partner_ref=?", (partner, reference))
        if dup:
            o = get_order(dup["id"])
            o["duplicate"] = True
            return o
    items = []
    for ln in lines:
        o = option(int(ln["option_id"]))
        items.append((o["id"], o["dish"], o["label"], int(ln.get("qty") or 1), o["price"]))
    menu_total = round(sum(i[3] * i[4] for i in items), 2)
    gross = round(float(total), 2) if total else menu_total
    if not gross and not unmatched:
        raise ShopError("The order has no items and no total.")
    fee = round(gross * float(partners[partner].get("commission") or 0) / 100, 2)
    stamp = iso_now()
    note = (f"{label} ref {reference}. " if reference else "") + (notes or "")[:300]
    if unmatched:
        note += " CHECK THESE ITEMS (not matched to the menu): " + "; ".join(unmatched)[:400]
    with tx() as c:
        oid = c.execute("""INSERT INTO orders(code,customer_name,phone,fulfilment,delivery_fee,food_total,total,date,time,
                           payment_method,payment_status,paid_at,notes,source,partner,partner_fee,partner_ref,created_at,updated_at)
                           VALUES(?,?,?,'collection',0,?,?,?,?,'partner','paid',?,?,'partner',?,?,?,?,?)""",
                        (secrets.token_urlsafe(9), (name or label + " customer")[:80], phone or "-", gross, gross,
                         parse_date(d).isoformat(), parse_time(t), parse_date(d).isoformat(), note.strip() or None,
                         partner, fee, reference or None, stamp, stamp)).lastrowid
        for oid_, dish, portion, qty, price in items:
            c.execute("INSERT INTO order_items(order_id,option_id,dish,portion,qty,unit_price) VALUES(?,?,?,?,?,?)",
                      (oid, oid_, dish, portion, qty, price))
        if not items:
            c.execute("INSERT INTO order_items(order_id,option_id,dish,portion,qty,unit_price) VALUES(?,NULL,?,?,1,?)",
                      (oid, "Items to check (see notes)", f"{label} order", gross))
    alert("order", f"New {label} order #{oid}: {money(gross)} for {pretty_date(parse_date(d).isoformat())} at {parse_time(t)}"
          + (" · some items need checking" if unmatched or not items else ""), f"/owner/orders/{oid}")
    return get_order(oid)


def partners_on() -> list[dict]:
    p = setting("partners") or {}
    return [{"key": k, "name": n, **p.get(k, {})} for k, n in menu_data.PARTNERS if p.get(k, {}).get("on")]


def set_status(order_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ShopError("Unknown status.")
    o = get_order(order_id)
    execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, iso_now(), order_id))
    if status in ("cooking", "ready", "completed") and not o["stock_used"]:
        use_stock_for_order(order_id)
    if status == "cancelled" and o["stock_used"]:
        return_stock_for_order(order_id)
    return get_order(order_id)


def set_payment(order_id: int, payment_status: str) -> dict:
    if payment_status not in PAYMENT_STATUSES:
        raise ShopError("Unknown payment status.")
    o = get_order(order_id)
    paid_at = (o["paid_at"] or today()) if payment_status == "paid" else None
    execute("UPDATE orders SET payment_status=?, paid_at=?, updated_at=? WHERE id=?", (payment_status, paid_at, iso_now(), order_id))
    return get_order(order_id)


def report_payment(order_id: int) -> dict:
    o = get_order(order_id)
    if o["payment_status"] == "unpaid":
        set_payment(order_id, "reported")
        alert("payment", f"{o['customer_name']} says they've paid {money(o['total'])} for order #{order_id}. Please check your bank.",
              f"/owner/orders/{order_id}")
    return get_order(order_id)


def mark_card_paid(order_id: int) -> None:
    o = get_order(order_id)
    if o["payment_status"] != "paid":
        set_payment(order_id, "paid")
        alert("payment", f"{o['customer_name']} paid {money(o['total'])} by card for order #{order_id}.", f"/owner/orders/{order_id}")


def hours_until(o: dict) -> float:
    when = datetime.combine(parse_date(o["date"]), datetime.strptime(o["time"], "%H:%M").time(), tzinfo=config.TZ)
    return (when - now()).total_seconds() / 3600


def customer_cancel(order_id: int) -> dict:
    o = get_order(order_id)
    if o["status"] not in ("confirmed",):
        raise ShopError("This order can't be cancelled online any more. Please message us.")
    if hours_until(o) < 24:
        raise ShopError("Orders can only be cancelled online at least 24 hours before. Please message us and we'll help.")
    set_status(order_id, "cancelled")
    alert("cancel", f"{o['customer_name']} cancelled order #{order_id} for {o['date_pretty']}.", f"/owner/orders/{order_id}")
    return get_order(order_id)


def list_orders(start: str | None = None, end: str | None = None, status: str | None = None, search: str = "") -> list[dict]:
    sql, args = "SELECT * FROM orders WHERE 1=1", []
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    if status == "open":
        sql += " AND status IN ('confirmed','cooking','ready')"
    elif status:
        sql += " AND status=?"
        args.append(status)
    if search:
        sql += " AND (customer_name LIKE ? OR phone LIKE ? OR CAST(id AS TEXT)=?)"
        args += [f"%{search}%", f"%{search}%", search.lstrip("#")]
    sql += " ORDER BY date, time, id"
    return [_with_items(o) for o in q(sql, tuple(args))]


def unpaid_orders() -> list[dict]:
    return [_with_items(o) for o in q("SELECT * FROM orders WHERE payment_status!='paid' AND status!='cancelled' ORDER BY date, time")]


# ---------------------------------------------------------------- alerts

def alert(kind: str, text: str, link: str = "") -> None:
    aid = execute("INSERT INTO alerts(kind,text,link,created_at) VALUES(?,?,?,?)", (kind, text, link, iso_now()))
    try:
        from . import notify
        notify.dispatch(kind, text, aid)
    except Exception:  # noqa: BLE001  (a failed phone alert must never break an order)
        pass


def alerts(limit: int = 20) -> list[dict]:
    return q("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))


def unseen_alerts() -> int:
    return q1("SELECT COUNT(*) n FROM alerts WHERE seen=0")["n"]


# ---------------------------------------------------------------- stock

def to_base(qty: float, unit: str) -> tuple[float, str]:
    base, mult = UNITS.get((unit or "pcs").lower().strip(), (None, None))
    if not base:
        raise ShopError(f"Use one of these units: {', '.join(UNITS)}.")
    return float(qty) * mult, base


def fmt_qty(qty: float, unit: str) -> str:
    if unit == "g" and abs(qty) >= 1000:
        return f"{qty / 1000:.2f}".rstrip("0").rstrip(".") + " kg"
    if unit == "ml" and abs(qty) >= 1000:
        return f"{qty / 1000:.2f}".rstrip("0").rstrip(".") + " l"
    return f"{qty:.1f}".rstrip("0").rstrip(".") + f" {unit}"


def _positive(x: float, what: str = "quantity") -> float:
    if x is None or math.isnan(x) or not 0 < x < 1e8:
        raise ShopError(f"The {what} must be more than zero.")
    return x


def ingredient(name_or_id) -> dict:
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        i = q1("SELECT * FROM ingredients WHERE id=?", (int(name_or_id),))
    else:
        i = q1("SELECT * FROM ingredients WHERE name=?", (str(name_or_id).strip(),))
    if not i:
        raise ShopError(f"'{name_or_id}' isn't in your stock list yet. Add it with a purchase first.")
    return i


def looks_untracked(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in UNTRACKED_WORDS)


def ingredients(tracked: bool | None = True) -> list[dict]:
    """Stock items. tracked=True: the ones counted in stock (default); False: spices, herbs and consumables; None: all."""
    sql = "SELECT * FROM ingredients" + ("" if tracked is None else f" WHERE tracked={1 if tracked else 0}") + " ORDER BY name"
    out = q(sql)
    for i in out:
        i["stock_txt"] = fmt_qty(i["stock"], i["unit"])
        i["low"] = bool(i["tracked"]) and (i["stock"] < 0 or (i["low_at"] > 0 and i["stock"] <= i["low_at"]))
    return out


def set_tracked(name_or_id, tracked: bool) -> str:
    i = ingredient(name_or_id)
    execute("UPDATE ingredients SET tracked=? WHERE id=?", (1 if tracked else 0, i["id"]))
    return (f"{i['name']} is now counted in stock and shopping lists." if tracked else
            f"{i['name']} is no longer counted; buying it goes to spices, herbs and consumables.")


def ensure_ingredient(name: str, unit: str) -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise ShopError("Name the ingredient.")
    _, base = to_base(1, unit)
    i = q1("SELECT * FROM ingredients WHERE name=?", (name,))
    if i:
        if i["unit"] != base:
            raise ShopError(f"{i['name']} is counted in {i['unit']}; use a unit like that.")
        return i
    execute("INSERT INTO ingredients(name,unit,tracked) VALUES(?,?,?)", (name, base, 0 if looks_untracked(name) else 1))
    return ingredient(name)


def record_purchase(name: str, qty: float, unit: str, cost: float, d: str = "", paid_from: str = "1110") -> str:
    """A food purchase. Tracked items go into stock; spices, herbs and consumables are logged as an expense."""
    _positive(qty)
    if cost is None or cost < 0:
        raise ShopError("Enter what it cost (0 if it was free).")
    if paid_from not in PAID_FROM:
        paid_from = "1110"
    i = ensure_ingredient(name, unit)
    base_qty, _ = to_base(qty, unit)
    per = cost / base_qty if base_qty else 0
    if not i["tracked"]:
        execute("UPDATE ingredients SET cost_per_unit=? WHERE id=?", (per or i["cost_per_unit"], i["id"]))
        if cost > 0:
            add_expense(d or today(), "Spices, herbs and consumables", f"{i['name']} ({fmt_qty(base_qty, i['unit'])})", cost,
                        paid_from=paid_from)
        return f"Logged {i['name']} ({money(cost)}) as spices, herbs and consumables. It isn't counted in stock."
    with tx() as c:
        c.execute("UPDATE ingredients SET stock=stock+?, cost_per_unit=? WHERE id=?", (base_qty, per or i["cost_per_unit"], i["id"]))
        c.execute("INSERT INTO stock_moves(ingredient_id,kind,qty,cost,reason,date,created_at) VALUES(?,?,?,?,?,?,?)",
                  (i["id"], "purchase", base_qty, round(cost, 2), paid_from, (d or today()), iso_now()))
    return f"Added {fmt_qty(base_qty, i['unit'])} of {i['name']} ({money(cost)})."


def write_off(name, qty: float, unit: str, reason: str, d: str = "") -> str:
    _positive(qty)
    if not (reason or "").strip():
        raise ShopError("Give a reason for the write-off (spoiled, out of date...).")
    i = ingredient(name)
    if not i["tracked"]:
        raise ShopError(f"{i['name']} isn't counted in stock, so there's nothing to write off. It was expensed when bought.")
    base_qty, base = to_base(qty, unit or i["unit"])
    if base != i["unit"]:
        raise ShopError(f"{i['name']} is counted in {i['unit']}.")
    value = round(base_qty * i["cost_per_unit"], 2)
    with tx() as c:
        c.execute("UPDATE ingredients SET stock=stock-? WHERE id=?", (base_qty, i["id"]))
        c.execute("INSERT INTO stock_moves(ingredient_id,kind,qty,cost,reason,date,created_at) VALUES(?,?,?,?,?,?,?)",
                  (i["id"], "writeoff", -base_qty, value, reason.strip()[:120], d or today(), iso_now()))
    return f"Wrote off {fmt_qty(base_qty, i['unit'])} of {i['name']} ({reason.strip()}), worth about {money(value)}."


def count_stock(name, actual: float, unit: str) -> str:
    i = ingredient(name)
    base_qty, base = to_base(actual, unit or i["unit"])
    if base != i["unit"] or base_qty < 0:
        raise ShopError(f"{i['name']} is counted in {i['unit']}.")
    diff = base_qty - i["stock"]
    with tx() as c:
        c.execute("UPDATE ingredients SET stock=?, tracked=1 WHERE id=?", (base_qty, i["id"]))
        c.execute("INSERT INTO stock_moves(ingredient_id,kind,qty,cost,reason,date,created_at) VALUES(?,?,?,?,?,?,?)",
                  (i["id"], "count", diff, round(diff * i["cost_per_unit"], 2), "Stock count", today(), iso_now()))
    return f"{i['name']} counted: {fmt_qty(base_qty, i['unit'])}."


def set_low_level(ingredient_id: int, low_at: float) -> None:
    execute("UPDATE ingredients SET low_at=? WHERE id=?", (max(0.0, low_at), ingredient_id))


def recipe(option_id: int) -> list[dict]:
    return q("""SELECT r.*, i.name, i.unit, i.cost_per_unit, i.tracked FROM recipe_lines r JOIN ingredients i ON i.id=r.ingredient_id
                WHERE r.option_id=? ORDER BY i.name""", (option_id,))


LINE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Z]+)\s+(.+?)\s*$")


def set_recipe(option_id: int, text: str) -> list[dict]:
    """One ingredient per line, for ONE portion: '250 g lamb', '1 pcs onion'."""
    option(option_id)
    parsed = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        m = LINE.match(raw)
        if not m:
            raise ShopError(f"Couldn't read '{raw.strip()}'. Write it like '250 g lamb'.")
        qty, unit, name = float(m.group(1).replace(",", ".")), m.group(2), m.group(3)
        if unit.lower() not in UNITS:
            name, unit = f"{unit} {name}", "pcs"
        parsed.append((_positive(qty), unit, name))
    rows = []
    for qty, unit, name in parsed:
        i = ensure_ingredient(name, unit)
        rows.append((i["id"], to_base(qty, unit)[0]))
    with tx() as c:
        c.execute("DELETE FROM recipe_lines WHERE option_id=?", (option_id,))
        for iid, qty in rows:
            c.execute("INSERT INTO recipe_lines(option_id,ingredient_id,qty) VALUES(?,?,?)", (option_id, iid, qty))
    return recipe(option_id)


def recipe_text(option_id: int) -> str:
    return "\n".join(f"{r['qty']:g} {r['unit']} {r['name']}" for r in recipe(option_id))


def recipe_cost(option_id: int) -> float:
    return round(sum(r["qty"] * r["cost_per_unit"] for r in recipe(option_id)), 2)


def _needs(order_list: list[dict], tracked_only: bool = True) -> dict[int, float]:
    need = defaultdict(float)
    for o in order_list:
        for it in o["items"]:
            if it["option_id"]:
                for r in recipe(it["option_id"]):
                    if r["tracked"] or not tracked_only:
                        need[r["ingredient_id"]] += r["qty"] * it["qty"]
    return need


def use_stock_for_order(order_id: int) -> None:
    o = get_order(order_id)
    with tx() as c:
        for iid, qty in _needs([o]).items():
            cpu = c.execute("SELECT cost_per_unit FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
            c.execute("UPDATE ingredients SET stock=stock-? WHERE id=?", (qty, iid))
            c.execute("INSERT INTO stock_moves(ingredient_id,kind,qty,cost,reason,order_id,date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (iid, "use", -qty, round(qty * cpu, 2), f"Order #{order_id}", order_id, o["date"], iso_now()))
        c.execute("UPDATE orders SET stock_used=1 WHERE id=?", (order_id,))


def return_stock_for_order(order_id: int) -> None:
    """A cancelled order that had already been cooked from stock: put the ingredients back (as positive 'use' moves, which the
    books treat as a reversal)."""
    with tx() as c:
        for m in c.execute("SELECT * FROM stock_moves WHERE order_id=? AND kind='use' AND qty<0", (order_id,)).fetchall():
            c.execute("UPDATE ingredients SET stock=stock+? WHERE id=?", (-m["qty"], m["ingredient_id"]))
            c.execute("INSERT INTO stock_moves(ingredient_id,kind,qty,cost,reason,order_id,date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (m["ingredient_id"], "use", -m["qty"], m["cost"], f"Order #{order_id} cancelled, stock returned", order_id,
                       today(), iso_now()))
        c.execute("UPDATE orders SET stock_used=0 WHERE id=?", (order_id,))


def shopping_list(start: str, end: str) -> dict:
    """What to buy for the orders between two dates that haven't used stock yet."""
    upcoming = [o for o in list_orders(start, end, "open") if not o["stock_used"]]
    need = _needs(upcoming)
    rows = []
    for i in ingredients():
        n = need.get(i["id"], 0)
        if n <= 0 and not i["low"]:
            continue
        buy = max(0.0, n - max(i["stock"], 0)) if n else max(0.0, i["low_at"] - i["stock"])
        if buy > 0:
            rows.append({"name": i["name"], "need": fmt_qty(n, i["unit"]), "have": i["stock_txt"], "buy": fmt_qty(buy, i["unit"]),
                         "cost": round(buy * i["cost_per_unit"], 2)})
    missing = sorted({f"{it['dish']} ({it['portion']})" for o in upcoming for it in o["items"]
                      if it["option_id"] and not recipe(it["option_id"])})
    used = _needs(upcoming, tracked_only=False)
    check = sorted(i["name"] for i in ingredients(tracked=False) if used.get(i["id"]))
    return {"orders": len(upcoming), "rows": rows, "no_recipe": missing, "total": round(sum(r["cost"] for r in rows), 2),
            "check": check}


def stock_history(limit: int = 60) -> list[dict]:
    out = q("""SELECT m.*, i.name, i.unit FROM stock_moves m JOIN ingredients i ON i.id=m.ingredient_id
               ORDER BY m.id DESC LIMIT ?""", (limit,))
    for m in out:
        m["qty_txt"] = fmt_qty(m["qty"], m["unit"])
    return out


# ---------------------------------------------------------------- money

def add_expense(d: str, category: str, description: str, amount: float, paid_from: str = "1110", account: str = "") -> str:
    parse_date(d)
    _positive(amount, "amount")
    if not (description or "").strip():
        raise ShopError("Say what the expense was for.")
    category = category if category in EXPENSE_ACCOUNTS else "Other"
    execute("INSERT INTO expenses(date,category,description,amount,account,paid_from,created_at) VALUES(?,?,?,?,?,?,?)",
            (d, category, description.strip()[:200], round(amount, 2), account or EXPENSE_ACCOUNTS[category],
             paid_from if paid_from in PAID_FROM else "1110", iso_now()))
    return f"Logged {money(amount)} for {description.strip()} on {pretty_date(d)}."


def expenses(start: str, end: str) -> list[dict]:
    return q("SELECT * FROM expenses WHERE date BETWEEN ? AND ? ORDER BY date DESC, id DESC", (start, end))


def summary(start: str, end: str) -> dict:
    """Profit and loss for a period (dates inclusive)."""
    orders = q("SELECT * FROM orders WHERE status!='cancelled' AND date BETWEEN ? AND ?", (start, end))
    sales = round(sum(o["total"] for o in orders), 2)
    delivery = round(sum(o["delivery_fee"] for o in orders), 2)
    paid = round(sum(o["total"] for o in orders if o["payment_status"] == "paid"), 2)
    purchases = round(q1("SELECT COALESCE(SUM(cost),0) s FROM stock_moves WHERE kind='purchase' AND date BETWEEN ? AND ?", (start, end))["s"], 2)
    waste = round(q1("SELECT COALESCE(SUM(cost),0) s FROM stock_moves WHERE kind='writeoff' AND date BETWEEN ? AND ?", (start, end))["s"], 2)
    other = round(q1("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE date BETWEEN ? AND ?", (start, end))["s"], 2)
    by_cat = q("SELECT category, SUM(amount) total FROM expenses WHERE date BETWEEN ? AND ? GROUP BY category ORDER BY total DESC", (start, end))
    dishes = q("""SELECT i.dish, SUM(i.qty) qty, SUM(i.qty*i.unit_price) total FROM order_items i JOIN orders o ON o.id=i.order_id
                  WHERE o.status!='cancelled' AND o.date BETWEEN ? AND ? GROUP BY i.dish ORDER BY total DESC""", (start, end))
    return {"start": start, "end": end, "orders": len(orders), "sales": sales, "delivery": delivery, "paid": paid,
            "unpaid": round(sales - paid, 2), "purchases": purchases, "other": other, "waste": waste,
            "profit": round(sales - purchases - other, 2), "by_category": by_cat, "dishes": dishes}


def period(name: str) -> tuple[str, str]:
    """Whole calendar periods, so orders booked for later in the month already show as sales."""
    d = now().date()
    if name == "week":
        mon = d - timedelta(days=d.weekday())
        return mon.isoformat(), (mon + timedelta(days=6)).isoformat()
    if name == "last_month":
        first = d.replace(day=1)
        prev_end = first - timedelta(days=1)
        return prev_end.replace(day=1).isoformat(), prev_end.isoformat()
    if name == "year":
        return d.replace(month=1, day=1).isoformat(), d.replace(month=12, day=31).isoformat()
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- calendar

def ics_feed() -> str:
    """Her orders as a calendar feed she can subscribe to from Google Calendar or her iPhone."""
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    start = (now().date() - timedelta(days=60)).isoformat()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//The Pashtoon Flavours//Orders//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{esc(setting('shop_name'))} orders", "X-WR-TIMEZONE:Europe/London"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for o in list_orders(start, None):
        if o["status"] == "cancelled":
            continue
        begin = datetime.combine(parse_date(o["date"]), datetime.strptime(o["time"], "%H:%M").time())
        dishes = ", ".join(f"{i['qty']} x {i['dish']} ({i['portion']})" for i in o["items"])
        desc = (f"{dishes}\nTotal {money(o['total'])} ({'paid' if o['payment_status'] == 'paid' else 'not paid yet'}, "
                f"{PAY_WORDS.get(o['payment_method'], o['payment_method'])})\nPhone {o['phone']}"
                + (f"\nDeliver to {o['address']} {o['postcode']}" if o["fulfilment"] == "delivery" else "")
                + (f"\nNotes: {o['notes']}" if o["notes"] else "") + f"\n{config.BASE_URL}/owner/orders/{o['id']}")
        lines += ["BEGIN:VEVENT", f"UID:pashtoon-order-{o['id']}@pashtoonflavours", f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID=Europe/London:{begin.strftime('%Y%m%dT%H%M%S')}",
                  f"DTEND;TZID=Europe/London:{(begin + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%S')}",
                  f"SUMMARY:{esc(f'#{o['id']} {o['customer_name']} · {o['fulfilment']} · {money(o['total'])}')}",
                  f"DESCRIPTION:{esc(desc)}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def month_grid(year: int, month: int) -> list[list[dict]]:
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    counts = defaultdict(list)
    for o in list_orders(start.isoformat(), (last + timedelta(days=7)).isoformat()):
        if o["status"] != "cancelled":
            counts[o["date"]].append(o)
    weeks, d = [], start
    while d <= last or d.weekday() != 0:
        if d.weekday() == 0:
            weeks.append([])
        iso = d.isoformat()
        weeks[-1].append({"date": iso, "day": d.day, "in_month": d.month == month, "orders": counts.get(iso, []),
                          "today": iso == today(), "closed": iso in (setting("closed_dates") or [])})
        d += timedelta(days=1)
    return weeks
