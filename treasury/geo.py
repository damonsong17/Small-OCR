"""Turn whatever the client sheet says about location into a point on the map.

Resolution order, first hit wins:

1. explicit ``lat``/``lon`` columns,
2. the ``city`` column against ``data/places.json``,
3. the ``country`` column (ISO-2, ISO-3 or a name) against the country
   centroids built from the world outline, plus ``data/jurisdictions.json``
   for the financial centres too small to have a polygon.

All three files are plain JSON next to the code — a desk can add a city or a
jurisdiction without touching Python, which matters on an air-gapped box.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Names that don't match the world outline's spelling.
COUNTRY_ALIASES = {
    "usa": "US", "u s a": "US", "united states": "US", "us": "US",
    "united states of america": "US", "america": "US",
    "uk": "GB", "u k": "GB", "great britain": "GB", "britain": "GB",
    "united kingdom": "GB", "england": "GB", "scotland": "GB",
    "prc": "CN", "china": "CN", "mainland china": "CN",
    "peoples republic of china": "CN", "china mainland": "CN",
    "hong kong": "HK", "hongkong": "HK", "hk sar": "HK", "hong kong sar": "HK",
    "macau": "MO", "macao": "MO",
    "taiwan": "TW", "chinese taipei": "TW", "roc": "TW",
    "korea": "KR", "south korea": "KR", "republic of korea": "KR",
    "north korea": "KP",
    "uae": "AE", "u a e": "AE", "united arab emirates": "AE", "emirates": "AE",
    "ksa": "SA", "saudi": "SA", "saudi arabia": "SA",
    "switzerland": "CH", "swiss": "CH", "suisse": "CH", "schweiz": "CH",
    "germany": "DE", "deutschland": "DE",
    "netherlands": "NL", "holland": "NL", "the netherlands": "NL",
    "russia": "RU", "russian federation": "RU",
    "vietnam": "VN", "viet nam": "VN",
    "czech republic": "CZ", "czechia": "CZ",
    "turkey": "TR", "turkiye": "TR", "türkiye": "TR",
    "ivory coast": "CI", "cote d ivoire": "CI",
    "myanmar": "MM", "burma": "MM",
    "laos": "LA", "lao pdr": "LA",
    "brunei": "BN", "brunei darussalam": "BN",
    "cayman": "KY", "cayman islands": "KY",
    "bvi": "VG", "british virgin islands": "VG",
    "luxembourg": "LU", "grand duchy of luxembourg": "LU",
    "singapore": "SG", "republic of singapore": "SG",
}

# Home currency of a jurisdiction — used by the map's rates overlay to pick
# which curve to show when you click a country.
COUNTRY_CURRENCY = {
    "US": "USD", "GB": "GBP", "CH": "CHF", "JP": "JPY", "CN": "CNY",
    "HK": "HKD", "MO": "MOP", "TW": "TWD", "KR": "KRW", "SG": "SGD",
    "AU": "AUD", "NZ": "NZD", "CA": "CAD", "IN": "INR", "ID": "IDR",
    "MY": "MYR", "TH": "THB", "PH": "PHP", "VN": "VND", "AE": "AED",
    "SA": "SAR", "QA": "QAR", "KW": "KWD", "BH": "BHD", "OM": "OMR",
    "IL": "ILS", "TR": "TRY", "RU": "RUB", "ZA": "ZAR", "EG": "EGP",
    "NG": "NGN", "KE": "KES", "MA": "MAD", "BR": "BRL", "MX": "MXN",
    "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN", "SE": "SEK",
    "NO": "NOK", "DK": "DKK", "PL": "PLN", "CZ": "CZK", "HU": "HUF",
    "RO": "RON", "UA": "UAH", "KZ": "KZT", "PK": "PKR", "BD": "BDT",
    "LK": "LKR",
}
_EUROZONE = ("DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "FI",
             "GR", "LU", "SK", "SI", "LT", "LV", "EE", "CY", "MT", "HR",
             "MC", "SM", "AD")
for _iso in _EUROZONE:
    COUNTRY_CURRENCY.setdefault(_iso, "EUR")


@dataclass
class Point:
    lat: float
    lon: float
    iso2: str = ""
    label: str = ""
    precision: str = "country"      # exact | city | country


def _load(filename: str) -> Dict[str, Any]:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    return {k: v for k, v in blob.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def countries() -> Dict[str, Dict[str, Any]]:
    """ISO-2 -> {name, iso3, lat, lon}, world outline plus small jurisdictions."""
    merged = dict(_load("country_points.json"))
    merged.update(_load("jurisdictions.json"))
    return merged


@lru_cache(maxsize=1)
def places() -> Dict[str, Dict[str, Any]]:
    return _load("places.json")


@lru_cache(maxsize=1)
def _name_index() -> Dict[str, str]:
    index = dict(COUNTRY_ALIASES)
    for iso2, meta in countries().items():
        index.setdefault(_slug(meta.get("name", "")), iso2)
        iso3 = meta.get("iso3")
        if iso3:
            index.setdefault(_slug(iso3), iso2)
        index.setdefault(_slug(iso2), iso2)
    return index


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def country_code(value: str) -> str:
    """Best-effort ISO-2 for a country name, code, or blank."""
    slug = _slug(value)
    if not slug:
        return ""
    index = _name_index()
    if slug in index:
        return index[slug]
    if len(slug) == 2 and slug.upper() in countries():
        return slug.upper()
    # "Germany (Frankfurt)" / "China - Shanghai"
    head = re.split(r"[(\-/,]", str(value))[0]
    head_slug = _slug(head)
    return index.get(head_slug, "")


def country_name(iso2: str) -> str:
    return countries().get(iso2, {}).get("name", iso2)


def currency_of(iso2: str) -> str:
    return COUNTRY_CURRENCY.get(iso2, "")


def resolve(country: Any = "", city: Any = "", lat: Any = None,
            lon: Any = None) -> Optional[Point]:
    """Locate a client. Returns None when nothing in the row is recognisable."""
    iso2 = country_code(country)
    if lat is not None and lon is not None:
        try:
            latitude, longitude = float(lat), float(lon)
        except (TypeError, ValueError):
            latitude = longitude = None
        if latitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return Point(latitude, longitude, iso2,
                         str(city or country_name(iso2)), "exact")

    key = _slug(city)
    place = places().get(key)
    if place:
        return Point(place["lat"], place["lon"], place.get("iso2", iso2),
                     str(city), "city")

    if iso2:
        meta = countries().get(iso2)
        if meta:
            return Point(meta["lat"], meta["lon"], iso2, meta.get("name", iso2), "country")
    return None


def world_geojson_path() -> str:
    return os.path.join(STATIC_DIR, "world.geo.json")
