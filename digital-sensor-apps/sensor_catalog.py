"""Persistence and manufacturer lookup for channel sensor metadata."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


_SENSITIVITY_PATTERN = re.compile(
    r"\bSensitivity\s*:\s*(?:\([^)]*\)\s*)?"
    r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s*(mV|V)\s*/\s*"
    r"\(?\s*(m/s(?:\^?2|²)|[A-Za-zµμ]+(?:/[A-Za-zµμ]+)?(?:\^?[0-9]+|²)?)\s*\)?",
    re.IGNORECASE,
)


def fetch_nominal_sensitivity(manufacturer, model, timeout=12):
    """Look up a sensitivity expressed in mV per engineering unit."""
    manufacturer = manufacturer.strip()
    model = model.strip()
    if not manufacturer or not model:
        raise ValueError("Enter both a manufacturer and model number")

    if manufacturer.casefold().startswith("pcb"):
        url = f"https://www.pcb.com/products?m={quote(model.lower(), safe='')}"
    else:
        query = f'"{manufacturer}" "{model}" sensitivity mV'
        url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"

    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        page = response.read().decode("utf-8", errors="replace")

    parser = _TextParser()
    parser.feed(page)
    match = _SENSITIVITY_PATTERN.search(" ".join(parser.parts))
    if not match:
        raise ValueError(f"No sensitivity in mV per engineering unit was found at {url}")

    value = float(match.group(1))
    if match.group(2).casefold() == "v":
        value *= 1000
    return {
        "nominal_sensitivity": value,
        "engineering_unit": match.group(3),
        "source_url": url,
    }


def load_sensor_catalog(path):
    """Load sensor records and device/channel assignments from JSON."""
    try:
        with Path(path).open(encoding="utf-8") as catalog_file:
            data = json.load(catalog_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, {}

    if not isinstance(data, dict):
        return {}, {}
    sensor_records = data.get("sensors", [])
    if not isinstance(sensor_records, list):
        sensor_records = []
    sensors = {
        sensor["id"]: sensor
        for sensor in sensor_records
        if isinstance(sensor, dict)
        and isinstance(sensor.get("id"), str)
        and isinstance(sensor.get("manufacturer"), str)
        and isinstance(sensor.get("model"), str)
    }
    assignments = data.get("assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}
    assignments = {
        str(channel_key): sensor_id
        for channel_key, sensor_id in assignments.items()
        if isinstance(sensor_id, str) and sensor_id in sensors
    }
    return sensors, assignments


def save_sensor_catalog(path, sensors, assignments):
    """Write sensor records and channel assignments to JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as catalog_file:
        json.dump(
            {"sensors": list(sensors.values()), "assignments": assignments},
            catalog_file,
            indent=2,
        )
