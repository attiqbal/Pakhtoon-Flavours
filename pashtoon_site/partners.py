"""Reading orders that come from delivery apps. Uber Eats, Deliveroo and Just Eat only give restaurant orders to
approved integration partners, so without their keys the practical route is the order email each one sends to the
restaurant. Forward those emails to an inbound-email address (Mailgun, Postmark, Zapier Email Parser, a Cloudflare email
worker...) that posts here, and this reads them. It is best-effort: anything it can't match is kept in the order's notes
and she is told to check it."""
import re

from . import shop
from .shop import ShopError

NAMES = {"ubereats": ("uber eats", "ubereats", "uber.com"), "deliveroo": ("deliveroo",), "justeat": ("just eat", "just-eat", "justeat", "takeaway.com")}
REF = re.compile(r"order\s*(?:id|number|no\.?|#|ref(?:erence)?)?\s*[:#]?\s*([A-Z0-9]{2,}[A-Z0-9\-]{2,})", re.I)
TOTAL = re.compile(r"(?:order\s+)?total[^£\d\n]{0,30}£\s*([\d,]+(?:\.\d{1,2})?)", re.I)
ITEM_A = re.compile(r"^\W*(\d{1,2})\s*[x×]\s+(.+?)(?:\s+£\s*[\d.,]+)?\s*$", re.I)
ITEM_B = re.compile(r"^\W*(.+?)\s+[x×]\s*(\d{1,2})(?:\s+£\s*[\d.,]+)?\s*$", re.I)
TIME = re.compile(r"(?:pick\s*-?up|collect(?:ion)?|ready|due|deliver(?:y)?)[^\d\n]{0,25}(\d{1,2})[:.](\d{2})", re.I)
CUSTOMER = re.compile(r"(?:customer|name)\s*[:\-]\s*([^\n]{2,60})", re.I)


def detect(text: str, subject: str = "", sender: str = "") -> str | None:
    hay = f"{sender} {subject} {text[:600]}".lower()
    for key, words in NAMES.items():
        if any(w in hay for w in words):
            return key
    return None


def parse_email(text: str, subject: str = "", sender: str = "") -> dict:
    """{'partner', 'reference', 'total', 'name', 'time', 'lines': [{'option_id','qty'}], 'unmatched': [...]}."""
    text = (text or "").replace("\r", "")
    out = {"partner": detect(text, subject, sender), "reference": "", "total": None, "name": "", "time": "", "lines": [], "unmatched": []}
    m = REF.search(subject + "\n" + text)
    if m and any(ch.isdigit() for ch in m.group(1)):
        out["reference"] = m.group(1)
    totals = TOTAL.findall(text)
    if totals:
        out["total"] = float(totals[-1].replace(",", ""))
    m = CUSTOMER.search(text)
    if m:
        out["name"] = m.group(1).strip()
    m = TIME.search(text)
    if m and int(m.group(1)) < 24:
        out["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or len(line) > 140:
            continue
        mm = ITEM_A.match(line)
        qty, name = (int(mm.group(1)), mm.group(2)) if mm else (None, None)
        if not mm:
            mm = ITEM_B.match(line)
            if mm:
                name, qty = mm.group(1), int(mm.group(2))
        if not mm or not name or re.search(r"total|subtotal|fee|delivery|service|vat|tip|discount", name, re.I):
            continue
        try:
            opt = shop.find_option(re.sub(r"\(.*?\)", "", name).strip(), name)
            out["lines"].append({"option_id": opt["id"], "qty": qty})
        except ShopError:
            out["unmatched"].append(f"{qty} x {name.strip()}")
    return out


def import_email(text: str, subject: str = "", sender: str = "", partner: str = "", d: str = "") -> dict:
    p = parse_email(text, subject, sender)
    partner = partner or p["partner"]
    if not partner:
        raise ShopError("Couldn't tell which delivery app this email is from. Add 'partner' to the request.")
    if not p["lines"] and not p["unmatched"] and not p["total"]:
        raise ShopError("Couldn't find any items or a total in that email.")
    return shop.place_partner_order(partner, p["lines"], p["name"], d or shop.today(), p["time"] or "18:00", p["total"], p["reference"],
                                    notes="Imported from email.", unmatched=p["unmatched"])
