"""Conservative normalization; syntactic validity is not contact verification."""

import math
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def key(value):
    return text(value).casefold()


def phone(value):
    value = text(value)
    if not re.fullmatch(r"(?:\+|00)[\d\s().-]+", value):
        return None  # Do not guess a country from a local number.
    digits = re.sub(r"\D", "", value)
    if value.startswith("00"):
        digits = digits[2:]
    return "+" + digits if re.fullmatch(r"[1-9]\d{7,14}", digits) else None


def email(value):
    value = text(value).casefold()
    return value if re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value) else None


def website(value):
    value = text(value)
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        if parts.port not in (None, 80, 443) or any(c.isspace() for c in parts.netloc):
            return None
        return urlunsplit((parts.scheme.lower(), parts.hostname.lower(), parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def coordinate(value, limit):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid coordinate")
    result = float(value)
    if not math.isfinite(result) or not -limit <= result <= limit:
        raise ValueError("Coordinate outside permitted range")
    return result


def contacts(value, kind):
    normalize = phone if kind == "phone" else email
    return sorted({v for raw in str(value or "").split(";") if (v := normalize(raw))})


def normalize_elements(payload):
    """Normalize an OSM-style elements envelope; quarantine invalid records."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Expected an elements array")
    accepted, rejected = [], []
    for index, element in enumerate(payload["elements"]):
        try:
            if not isinstance(element, dict) or not isinstance(element.get("tags", {}), dict):
                raise ValueError("Invalid element or tags")
            tags = element.get("tags", {})
            name = text(tags.get("name"))
            if not name:
                raise ValueError("Missing business name")
            address = text(tags.get("addr:full")) or text(" ".join(text(tags.get(k)) for k in
                          ["addr:street", "addr:housenumber", "addr:city", "addr:postcode"]))
            center = element.get("center") or {}
            if not isinstance(center, dict):
                raise ValueError("Invalid center")
            lat = coordinate(element.get("lat", center.get("lat")), 90)
            lon = coordinate(element.get("lon", center.get("lon")), 180)
            if (lat is None) != (lon is None):
                raise ValueError("Incomplete coordinate pair")
            sid = element.get("id")
            source_id = None
            if sid is not None:
                if type(sid) is not int or sid < 1 or element.get("type") not in {"node", "way", "relation"}:
                    raise ValueError("Invalid source identifier")
                source_id = f"{element['type']}:{sid}"
            accepted.append({"name": name, "address": address, "category": text(tags.get("shop") or tags.get("amenity") or tags.get("leisure")),
                             "lat": lat, "lon": lon, "source_id": source_id,
                             "website": website(tags.get("contact:website") or tags.get("website")),
                             "phones": contacts(tags.get("contact:phone") or tags.get("phone"), "phone"),
                             "emails": contacts(tags.get("contact:email") or tags.get("email"), "email"),
                             "input_index": index})
        except (ValueError, TypeError) as exc:
            rejected.append({"input_index": index, "reason": str(exc)})
    return accepted, rejected
