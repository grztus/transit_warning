"""Transport and parsing helpers for METAR data."""

import re

import requests


def fetch_metar_text(url):
    """Return the response text for HTTP 200, or None for another status."""
    response = requests.get(url)
    if response.status_code == 200:
        return response.text
    return None


def parse_metar_qnh(text):
    """Return a plausible QNH value from METAR text, or None."""
    pressure_match = re.search(r"Q(\d{4})", text)
    if not pressure_match:
        return None
    qnh = int(pressure_match.group(1))
    if 800 < qnh < 1100:
        return qnh
    return None
