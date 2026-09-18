#!/usr/bin/env python3
"""
SVAR data layer
---------------
Pulls current/forecast weather from SMHI's point forecast API and active
warnings from SMHI's Impact Based Weather Warnings API, then turns both
into spoken Swedish phrases ready to hand off to svar_tts.py.

APIs used (both free, no key required):
  Forecast: https://opendata-download-metfcst.smhi.se
  Warnings: https://opendata-download-warnings.smhi.se

Usage:
    python3 svar_data.py                     # prints generated phrases
    python3 svar_data.py --lat 55.6 --lon 13.0 --place "Malmö"
"""

import argparse
import sys
from datetime import datetime, timezone

import requests

FORECAST_URL = (
    "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/"
    "geotype/point/lon/{lon}/lat/{lat}/data.json"
)
MISSING_VALUE = 9999
WARNINGS_URL = "https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json"

# Skåne county code used in SMHI district data (Skåne län)
SKANE_DISTRICT_NAMES = {"Skåne län", "Skåne"}

# Representative points across the county.  A point forecast is deliberately
# used for each place: SMHI's forecast API is point-based, rather than a
# single county-wide forecast.
SKANE_LOCATIONS = (
    ("Malmö", 55.6050, 13.0038),
    ("Helsingborg", 56.0465, 12.6945),
    ("Landskrona", 55.8708, 12.8302),
    ("Hässleholm", 56.1591, 13.7664),
    ("Kristianstad", 56.0294, 14.1567),
    ("Ystad", 55.4295, 13.8204),
)

WIND_DIRECTIONS = [
    (0, "nordlig"), (45, "nordostlig"), (90, "ostlig"), (135, "sydostlig"),
    (180, "sydlig"), (225, "sydvästlig"), (270, "västlig"), (315, "nordvästlig"),
    (360, "nordlig"),
]

WSYMB_SV = {
    1: "klart väder", 2: "mestadels klart", 3: "växlande molnighet",
    4: "halvklart väder", 5: "molnigt väder", 6: "mulet väder",
    7: "dimma", 8: "lätta regnskurar", 9: "måttliga regnskurar",
    10: "kraftiga regnskurar", 11: "åska", 12: "lätt snöblandat regn",
    13: "måttligt snöblandat regn", 14: "kraftigt snöblandat regn",
    15: "lätta snöbyar", 16: "måttliga snöbyar", 17: "kraftiga snöbyar",
    18: "lätt regn", 19: "måttligt regn", 20: "kraftigt regn",
    21: "åska", 22: "lätt snöblandat regn", 23: "måttligt snöblandat regn",
    24: "kraftigt snöblandat regn", 25: "lätt snöfall", 26: "måttligt snöfall",
    27: "kraftigt snöfall",
}


def wind_dir_word(degrees: float) -> str:
    for upper, word in WIND_DIRECTIONS:
        if degrees <= upper:
            return word
    return "varierande"


def fetch_forecast(lat: float, lon: float) -> dict:
    url = FORECAST_URL.format(lat=lat, lon=lon)
    headers = {"User-Agent": "svar-weather-radio/1.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_param(data: dict, key: str):
    """Pull a parameter from the new flat 'data' dict, treating 9999 as missing."""
    val = data.get(key)
    if val is None or val == MISSING_VALUE:
        return None
    return val


def fetch_warnings() -> list:
    resp = requests.get(WARNINGS_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def current_conditions_phrase(forecast: dict, place: str) -> str:
    """Take the first (nearest-to-now) timeSeries entry and build a phrase."""
    entry = forecast["timeSeries"][0]
    data = entry["data"]

    temp = round(get_param(data, "air_temperature") or 0)
    wind_speed = round(get_param(data, "wind_speed") or 0)
    wind_dir = wind_dir_word(get_param(data, "wind_from_direction") or 0)
    symbol = int(get_param(data, "symbol_code") or 1)
    condition = WSYMB_SV.get(symbol, "växlande väder")

    return (
        f"Just nu i {place}: {temp} grader, {condition}. "
        f"Vind {wind_dir} vid {wind_speed} meter per sekund."
    )


def short_forecast_phrase(forecast: dict, place: str) -> str:
    """Look ~6 hours ahead for a short outlook line."""
    series = forecast["timeSeries"]
    entry = series[min(6, len(series) - 1)]
    data = entry["data"]

    temp = round(get_param(data, "air_temperature") or 0)
    symbol = int(get_param(data, "symbol_code") or 1)
    condition = WSYMB_SV.get(symbol, "växlande väder")

    return f"Prognos för {place} senare i dag: {condition}, omkring {temp} grader."


def skane_forecast_phrases() -> list:
    """Return a short current-conditions bulletin for representative Skåne points.

    A failed point must not prevent the rest of the county from being reported.
    The caller can still fetch warnings independently.
    """
    phrases = []
    for place, lat, lon in SKANE_LOCATIONS:
        try:
            forecast = fetch_forecast(lat, lon)
        except requests.RequestException as e:
            print(f"[warn] forecast fetch failed for {place}: {e}", file=sys.stderr)
            continue
        phrases.append(current_conditions_phrase(forecast, place))
        phrases.append(short_forecast_phrase(forecast, place))
    return phrases


def active_warning_phrases(warnings_data) -> list:
    """Filter warnings to Skåne and build alert phrases."""
    phrases = []
    # API returns a bare list of warning objects at the top level
    warning_list = warnings_data if isinstance(warnings_data, list) else warnings_data.get("warnings", [])

    for warning in warning_list:
        areas = warning.get("area", [])
        # area entries typically have 'affectedAreas' -> list of district names
        district_names = set()
        for area in areas:
            for affected in area.get("affectedAreas", []):
                district_names.add(affected.get("name", ""))

        if not district_names & SKANE_DISTRICT_NAMES:
            continue

        event = warning.get("event", {}).get("sv", "varning")
        level = warning.get("warningLevel", {}).get("sv", "")
        descr = warning.get("descriptions", [{}])
        description_text = ""
        for d in descr:
            if d.get("title", {}).get("sv", "").lower() == "vad":
                description_text = d.get("text", {}).get("sv", "")
                break

        phrase = f"Varning: {level} för {event} i Skåne. {description_text}".strip()
        phrases.append(phrase)

    return phrases


def main():
    parser = argparse.ArgumentParser(description="SVAR SMHI data fetcher")
    parser.add_argument("--lat", type=float, default=55.6050, help="Latitude (default: Malmö)")
    parser.add_argument("--lon", type=float, default=13.0038, help="Longitude (default: Malmö)")
    parser.add_argument("--place", default="Malmö", help="Place name to speak")
    parser.add_argument("--skane", action="store_true",
                        help="Print a bulletin covering representative places in Skåne")
    args = parser.parse_args()

    if args.skane:
        for phrase in skane_forecast_phrases():
            print(phrase)
    else:
        try:
            forecast = fetch_forecast(args.lat, args.lon)
        except requests.RequestException as e:
            sys.exit(f"Failed to fetch forecast: {e}")
        print(current_conditions_phrase(forecast, args.place))
        print(short_forecast_phrase(forecast, args.place))

    try:
        warnings_data = fetch_warnings()
    except requests.RequestException as e:
        print(f"Warning: failed to fetch warnings ({e}), continuing without alerts", file=sys.stderr)
        warnings_data = {}

    for alert_phrase in active_warning_phrases(warnings_data):
        print(f"[ALERT] {alert_phrase}")


if __name__ == "__main__":
    main()
