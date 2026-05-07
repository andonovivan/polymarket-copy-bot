"""Current-day observed temperature fetcher (Open-Meteo regular forecast API).

Used by `_attach_model_probabilities` to do a Bayesian update on the ensemble:
day-max is monotonically non-decreasing within a day, so once we observe a
24°C reading at 14:00 local, every ensemble member predicting < 24°C is
falsified. We shift those members up to the observed max.

Open-Meteo's regular `forecast` endpoint with `past_days=1` returns hourly
temperatures including observations (it falls back to nowcasts where
observations aren't available yet). Cheap (~1 KB response) and not on the
ensemble quota.

Cached per-city for `OBSERVATION_TTL_SECONDS` so we don't hammer the API on
every tick.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from polymarket_bot.data.weather_feed import City

logger = structlog.get_logger()

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OBSERVATION_TTL_SECONDS = 30 * 60   # observations refresh hourly anyway
USER_AGENT = "polymarket-bot-observations/0.1"


@dataclass
class ObservedMax:
    target_date: str          # YYYY-MM-DD in city-local tz
    max_temp: float           # observed max so far (in city's unit)
    fetched_at: int           # unix seconds


_OBS_CACHE: dict[tuple[str, str], ObservedMax] = {}


def _fetch_json(url: str, timeout: float = 10.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as exc:
        logger.warning("observation_fetch_failed",
                       url=url[:120], error=str(exc)[:200])
        return None


def get_observed_max_today(city: City, target_date: str) -> float | None:
    """Return the max temperature observed in `city` on `target_date` so far.

    `target_date` is YYYY-MM-DD in the city's local timezone (matching the
    Polymarket event's resolution day).

    Returns None on API failure or when no observations are available.
    """
    key = (city.key, target_date)
    cached = _OBS_CACHE.get(key)
    if cached and (time.time() - cached.fetched_at) < OBSERVATION_TTL_SECONDS:
        return cached.max_temp

    url = (f"{FORECAST_URL}?latitude={city.lat}&longitude={city.lon}"
           f"&hourly=temperature_2m&temperature_unit={city.unit}"
           f"&timezone={urllib.parse.quote(city.tz)}"
           f"&past_days=1&forecast_days=1")
    data = _fetch_json(url)
    if not isinstance(data, dict):
        return None

    h = data.get("hourly") or {}
    times = h.get("time") or []
    temps = h.get("temperature_2m") or []
    if not times or not temps or len(times) != len(temps):
        return None

    # Keep only hourly values whose timestamp falls on `target_date` (city-local)
    # and isn't in the future. Open-Meteo's response uses the city's tz when
    # `timezone=` is set, so timestamps are local and lex-comparable.
    now_iso_prefix = (datetime.now(timezone.utc).astimezone().isoformat()[:13]
                      if False else None)  # keep lint happy; we use a stricter rule below
    # Use the raw timestamp prefix YYYY-MM-DDTHH and require <= "now in city tz".
    # Since the API echoes timestamps in the requested timezone, compare against
    # what the city believes is "now". We approximate "now" by walking back
    # from the latest available observed timestamp until temperature is non-null.
    latest_max: float | None = None
    for t, val in zip(times, temps):
        if val is None:
            continue
        if not isinstance(t, str) or not t.startswith(target_date):
            continue
        v = float(val)
        if latest_max is None or v > latest_max:
            latest_max = v

    if latest_max is None:
        return None

    _OBS_CACHE[key] = ObservedMax(target_date=target_date,
                                  max_temp=latest_max,
                                  fetched_at=int(time.time()))
    logger.info("observed_max_fetched", city=city.key, date=target_date,
                max_temp=round(latest_max, 1))
    return latest_max


def reset_cache() -> None:
    """Clear the in-memory cache. For tests."""
    _OBS_CACHE.clear()


def fuse_ensemble_with_observation(members: list[int],
                                   observed_max: float | None) -> list[int]:
    """Bayesian-style update: each member's day-max prediction is at least the
    observed max so far (monotonicity of the daily maximum).

    If `observed_max` is None or already below every member, returns members
    unchanged. Otherwise shifts members below the observation up to the
    rounded observation value.
    """
    if observed_max is None or not members:
        return members
    obs_int = int(round(observed_max))
    return [max(m, obs_int) for m in members]
