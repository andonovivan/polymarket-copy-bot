"""Open-Meteo multi-model ensemble fetcher with TTL cache.

Pulls GFS + ECMWF + ICON ensemble forecasts (122 members total) for a given
city's airport station, derives per-member day-max temperature, and exposes a
helper that converts those into per-bucket probabilities matching Polymarket's
labelling.

Public surface:
  CITY_REGISTRY                — name -> City
  get_ensemble(city, date)     — cached EnsembleForecast for that target date
  bucket_probabilities(...)    — counts members per bucket, returns dict
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import json
from dataclasses import dataclass
from typing import Literal

import structlog

logger = structlog.get_logger()

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
CACHE_TTL_SECONDS = 30 * 60   # forecasts update ~6h; 30m is safely fresh
USER_AGENT = "polymarket-bot-weather/0.4"


@dataclass
class City:
    key: str
    lat: float
    lon: float
    tz: str
    unit: Literal["fahrenheit", "celsius"]
    event_slug_prefix: str               # e.g. "highest-temperature-in-paris-on-"


# Allowlist seeded by backtest: cities where the multi-model ensemble beat
# Polymarket pricing on settled markets. US cities are excluded (efficient
# pricing per backtest). Initial 4 (paris/madrid/london/tokyo) used airport
# stations from the Phase 0.5 backtest; the 7 added below were chosen by the
# 36-city × 60-day Path A sweep (top simulated Kelly PnL) and use city-centre
# coordinates from the Open-Meteo geocoder.
CITY_REGISTRY: dict[str, City] = {
    "paris":     City("paris",     49.0097,    2.5479, "Europe/Paris",    "celsius",
                      "highest-temperature-in-paris-on-"),
    "madrid":    City("madrid",    40.4936,   -3.5668, "Europe/Madrid",   "celsius",
                      "highest-temperature-in-madrid-on-"),
    "london":    City("london",    51.5053,    0.0552, "Europe/London",   "celsius",
                      "highest-temperature-in-london-on-"),
    "tokyo":     City("tokyo",     35.5494,  139.7798, "Asia/Tokyo",      "celsius",
                      "highest-temperature-in-tokyo-on-"),
    "taipei":    City("taipei",    25.0531,  121.5264, "Asia/Taipei",     "celsius",
                      "highest-temperature-in-taipei-on-"),
    "moscow":    City("moscow",    55.7522,   37.6156, "Europe/Moscow",   "celsius",
                      "highest-temperature-in-moscow-on-"),
    "chengdu":   City("chengdu",   30.6667,  104.0667, "Asia/Shanghai",   "celsius",
                      "highest-temperature-in-chengdu-on-"),
    "shanghai":  City("shanghai",  31.2222,  121.4581, "Asia/Shanghai",   "celsius",
                      "highest-temperature-in-shanghai-on-"),
    "chongqing": City("chongqing", 29.5603,  106.5577, "Asia/Shanghai",   "celsius",
                      "highest-temperature-in-chongqing-on-"),
    "helsinki":  City("helsinki",  60.1695,   24.9354, "Europe/Helsinki", "celsius",
                      "highest-temperature-in-helsinki-on-"),
    "beijing":   City("beijing",   39.9075,  116.3972, "Asia/Shanghai",   "celsius",
                      "highest-temperature-in-beijing-on-"),
}


@dataclass
class EnsembleForecast:
    city_key: str
    target_date: str                     # YYYY-MM-DD
    fetched_at: int                      # unix seconds
    members: list[int]                   # day-max per member, integer °F or °C


_FORECAST_CACHE: dict[tuple[str, str], EnsembleForecast] = {}


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_ensemble(city: City, target_date: str) -> EnsembleForecast | None:
    """Return cached or freshly-fetched ensemble forecast for `target_date`.

    `target_date` is a YYYY-MM-DD string in the city's local timezone.
    """
    key = (city.key, target_date)
    cached = _FORECAST_CACHE.get(key)
    if cached and (time.time() - cached.fetched_at) < CACHE_TTL_SECONDS:
        return cached

    # Look back enough days that target_date is in the response window.
    today = int(time.time()) // 86400
    target = int(time.mktime(time.strptime(target_date, "%Y-%m-%d"))) // 86400
    past_days = max(0, today - target)

    url = (f"{ENSEMBLE_URL}?latitude={city.lat}&longitude={city.lon}"
           f"&hourly=temperature_2m&temperature_unit={city.unit}"
           f"&timezone={urllib.parse.quote(city.tz)}"
           f"&past_days={past_days}&forecast_days=2"
           f"&models=gfs_seamless,ecmwf_ifs025,icon_seamless")
    try:
        data = _fetch_json(url)
    except Exception as exc:
        logger.warning("ensemble_fetch_failed", city=city.key,
                       date=target_date, error=str(exc)[:200])
        return None

    h = data.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    keep_idx = [i for i, t in enumerate(times) if t.startswith(target_date)]
    if not keep_idx:
        return None
    member_keys = [k for k in h.keys() if k.startswith("temperature_2m")]
    members: list[int] = []
    for k in member_keys:
        vals = [h[k][i] for i in keep_idx if h[k][i] is not None]
        if vals:
            members.append(int(round(max(vals))))
    if not members:
        return None
    forecast = EnsembleForecast(city_key=city.key, target_date=target_date,
                                fetched_at=int(time.time()), members=members)
    _FORECAST_CACHE[key] = forecast
    logger.info("ensemble_fetched", city=city.key, date=target_date,
                members=len(members),
                range=f"{min(members)}-{max(members)}",
                mean=round(sum(members) / len(members), 1))
    return forecast


def in_bucket(t: int, label: str) -> bool:
    """Test whether integer temperature `t` falls in the Polymarket bucket label."""
    if "≤" in label or "or below" in label:
        thresh = int(re.search(r"(\d+)", label).group(1))
        return t <= thresh
    if "≥" in label or "or higher" in label or "or above" in label:
        thresh = int(re.search(r"(\d+)", label).group(1))
        return t >= thresh
    nums = [int(x) for x in re.findall(r"(\d+)", label)]
    if len(nums) == 1:
        return t == nums[0]
    return len(nums) >= 2 and nums[0] <= t <= nums[1]


def bucket_probabilities(members: list[int], labels: list[str]) -> dict[str, float]:
    """Count members per bucket → fraction. Sums to ≤ 1 if labels don't partition all temps."""
    if not members:
        return {label: 0.0 for label in labels}
    n = len(members)
    return {label: sum(1 for t in members if in_bucket(t, label)) / n for label in labels}
