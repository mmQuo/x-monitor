"""Local place candidates. Coordinates always come from the reviewed gazetteer."""

import json
import re
from pathlib import Path

PLACES = {p["id"]: p for p in json.loads(
    (Path(__file__).parent / "config/map_places.json").read_text())["places"]}
LEVELS = {"city": "城市范围", "country": "国家范围", "region": "区域范围"}
PATTERNS = []
for place in PLACES.values():
    expressions = []
    for alias in place["aliases"]:
        # Short abbreviations such as US must not match the pronoun "us".
        token = re.escape(alias)
        if alias.isascii():
            token = r"(?<![A-Za-z])" + token + r"(?![A-Za-z])"
        flags = 0 if alias.isascii() and len(alias.replace(".", "")) <= 3 else re.I
        expressions.append(re.compile(token, flags))
    PATTERNS.append((place, expressions))


def candidates(text):
    # URLs, handles and feed attribution are not event locations.
    text = re.sub(r"https?://\S+|@[A-Za-z0-9_]+", " ", text)
    found = []
    for place, patterns in PATTERNS:
        matches = [m for pattern in patterns if (m := pattern.search(text))]
        if matches:
            match = min(matches, key=lambda m: m.start())
            found.append((match.start(), {"id": place["id"], "name": place["name"],
                "level": place["level"], "mention": match[0]}))
    return [place for _, place in sorted(found, key=lambda row: row[0])][:16]


def validate_locations(items, text, allowed):
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError("Invalid map locations")
    by_id = {p["id"]: p for p in allowed}
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid map location")
        key, quote = item.get("place_id"), item.get("evidence")
        relation = item.get("relation")
        if (key not in by_id or key in seen or not isinstance(quote, str)
                or not 2 <= len(quote) <= 240 or quote not in text
                or relation not in {"event", "scope"}):
            raise ValueError("Unsupported map location")
        if key not in {p["id"] for p in candidates(quote)}:
            raise ValueError("Location missing from evidence")
        place = PLACES[key]
        result.append({"id": key, "name": place["name"], "level": place["level"],
            "precision": LEVELS[place["level"]], "coordinates": place["coordinates"],
            "relation": relation, "evidence": quote})
        seen.add(key)
    return result
