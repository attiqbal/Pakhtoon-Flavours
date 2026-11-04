"""UK postcode distances using the free postcodes.io service (straight-line miles)."""
import math
import re

import httpx

_cache: dict[str, tuple[float, float] | None] = {}
POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")


def normalise(postcode: str) -> str:
    p = re.sub(r"\s+", "", postcode or "").upper()
    return f"{p[:-3]} {p[-3:]}" if len(p) > 3 else p


def valid(postcode: str) -> bool:
    return bool(POSTCODE.match(normalise(postcode).replace(" ", "")))


def lookup(postcode: str) -> tuple[float, float] | None:
    p = normalise(postcode)
    if p in _cache:
        return _cache[p]
    try:
        r = httpx.get(f"https://api.postcodes.io/postcodes/{p.replace(' ', '')}", timeout=8)
        res = r.json().get("result") if r.status_code == 200 else None
        val = (res["latitude"], res["longitude"]) if res else None
    except (httpx.HTTPError, ValueError):
        return None  # don't cache network failures
    _cache[p] = val
    return val


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def miles_between_postcodes(a: str, b: str) -> float | None:
    pa, pb = lookup(a), lookup(b)
    if not pa or not pb:
        return None
    return round(miles_between(pa, pb), 1)
