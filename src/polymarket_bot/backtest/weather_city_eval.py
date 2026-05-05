"""Path A: rank candidate weather cities by historical Polymarket-vs-model edge.

For each non-US candidate city:
  1. Geocode via Open-Meteo.
  2. Walk back N days, fetching settled gamma events by exact slug.
  3. Replay model probabilities from the Open-Meteo Historical Forecast API
     (3 deterministic models: GFS, ECMWF, ICON). Day-max per model is treated
     as one ensemble "member"; bucket prob = members_in_bucket / 3.
  4. For each bucket, fetch its YES-token price history from the CLOB and
     pick the price `--bet-offset-hours` before resolution (default 24h).
     This is the price you'd actually pay if you bet a day before close —
     not the lastTradePrice (which converges to the resolved outcome).
  5. Score each (event, bucket): Brier + log-loss vs that "bet-time"
     market price, plus a Kelly-PnL sim against a $1 reference bankroll.
  6. Print a sorted table.

CAVEAT — read this before promoting cities:

  The live bot uses Open-Meteo's 122-member Ensemble API, which can't be
  replayed historically (past dates return null). This harness uses the
  3-model historical-forecast API as a substitute, which is a much coarser
  signal (bucket probabilities collapse to {0, 1/3, 2/3, 1}). Treat the
  output as a *directional* filter — promote only cities with large,
  consistent edge, and confirm via the live-capture research log
  (research/weather_capture.py) before deploying real capital.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from polymarket_bot.data.weather_feed import bucket_probabilities
from polymarket_bot.polymarket.markets import GAMMA_API_URL

logger = structlog.get_logger()

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
HISTORICAL_FORECAST_URL = (
    "https://historical-forecast-api.open-meteo.com/v1/forecast"
)
CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"

# Discovered from gamma 2026-05; non-US daily highest-temp markets.
CANDIDATES = [
    "amsterdam", "ankara", "beijing", "buenos-aires", "busan",
    "cape-town", "chengdu", "chongqing", "guangzhou", "helsinki",
    "hong-kong", "istanbul", "jakarta", "jeddah", "karachi",
    "kuala-lumpur", "lagos", "lucknow", "manila", "mexico-city",
    "milan", "moscow", "munich", "panama-city", "qingdao",
    "sao-paulo", "seoul", "shanghai", "shenzhen", "singapore",
    "taipei", "tel-aviv", "toronto", "warsaw", "wellington", "wuhan",
]

# Slug → geocoder query when slug doesn't directly resolve.
GEOCODE_OVERRIDE = {
    "panama-city": "Panama City, Panama",  # disambiguate from Panama City, FL
    "sao-paulo": "São Paulo",
}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


@dataclass
class GeoInfo:
    lat: float
    lon: float
    tz: str
    country: str


@dataclass
class CityScore:
    city: str
    country: str
    n_events: int
    n_buckets: int
    brier_model: float
    brier_market: float
    log_loss_model: float
    log_loss_market: float
    kelly_pnl: float
    n_bets: int


_GEOCODE_CACHE: dict[str, GeoInfo | None] = {}


def _http_get_json(url: str, timeout: float = 15.0) -> dict | list | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-bot-backtest/0.1"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as exc:
        logger.warning("http_error", url=url[:120], error=str(exc)[:200])
        return None


def geocode(slug: str) -> GeoInfo | None:
    if slug in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[slug]
    name = GEOCODE_OVERRIDE.get(slug, slug.replace("-", " ").title())
    url = f"{GEOCODE_URL}?name={urllib.parse.quote(name)}&count=1&format=json"
    data = _http_get_json(url)
    if not isinstance(data, dict) or not data.get("results"):
        _GEOCODE_CACHE[slug] = None
        return None
    r = data["results"][0]
    info = GeoInfo(
        lat=float(r["latitude"]),
        lon=float(r["longitude"]),
        tz=r.get("timezone", "UTC"),
        country=r.get("country", "?"),
    )
    _GEOCODE_CACHE[slug] = info
    return info


def _slug_for(city: str, d: datetime) -> str:
    month = _MONTH_NAMES[d.month - 1]
    return f"highest-temperature-in-{city}-on-{month}-{d.day}-{d.year}"


def fetch_settled_event(slug: str) -> dict | None:
    """Return event dict with `winner`, `unit`, `end_ts`, and per-bucket
    `(label, yes_token_id)` entries, or None if not found / not settled."""
    url = f"{GAMMA_API_URL}/events?slug={urllib.parse.quote(slug)}"
    data = _http_get_json(url)
    if not isinstance(data, list) or not data:
        return None
    e = data[0]
    if not e.get("closed"):
        return None
    end_iso = e.get("endDate")
    try:
        end_ts = int(datetime.fromisoformat(
            (end_iso or "").replace("Z", "+00:00")).timestamp())
    except Exception:
        return None
    unit: str | None = None
    winner: str | None = None
    buckets: list[tuple[str, str]] = []
    for m in e.get("markets") or []:
        label = m.get("groupItemTitle") or ""
        if unit is None:
            if "°F" in label:
                unit = "fahrenheit"
            elif "°C" in label:
                unit = "celsius"
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception:
                op = None
        if not op or len(op) < 2:
            continue
        tids = m.get("clobTokenIds")
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                tids = None
        if not tids or len(tids) < 2:
            continue
        try:
            yes_op = float(op[0])
            no_op = float(op[1])
        except (TypeError, ValueError):
            continue
        if (yes_op, no_op) == (1.0, 0.0):
            winner = label
        buckets.append((label, str(tids[0])))
    if winner is None or not buckets:
        return None
    return {"slug": slug, "winner": winner, "buckets": buckets,
            "unit": unit or "celsius", "end_ts": end_ts}


def fetch_yes_price_at(token_id: str, target_ts: int) -> float | None:
    """Latest YES-side trade price at or before `target_ts`, or None."""
    url = f"{CLOB_PRICES_HISTORY_URL}?market={token_id}&interval=max"
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return None
    history = data.get("history") or []
    if not history:
        return None
    latest_p: float | None = None
    for pt in history:
        try:
            t = int(pt["t"])
            p = float(pt["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if t <= target_ts:
            latest_p = p
        else:
            break
    return latest_p


def historical_day_max(geo: GeoInfo, target_date: str, unit: str) -> list[int]:
    """Return list of 1–3 day-max integer temperatures, one per available model."""
    url = (
        f"{HISTORICAL_FORECAST_URL}?latitude={geo.lat}&longitude={geo.lon}"
        f"&start_date={target_date}&end_date={target_date}"
        f"&hourly=temperature_2m&temperature_unit={unit}"
        f"&timezone={urllib.parse.quote(geo.tz)}"
        f"&models=gfs_seamless,ecmwf_ifs025,icon_seamless"
    )
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return []
    h = data.get("hourly") or {}
    times = h.get("time") or []
    keep = [i for i, t in enumerate(times) if t.startswith(target_date)]
    if not keep:
        return []
    out: list[int] = []
    for k, vs in h.items():
        if not k.startswith("temperature_2m") or not isinstance(vs, list):
            continue
        vals = [vs[i] for i in keep if i < len(vs) and vs[i] is not None]
        if vals:
            out.append(int(round(max(vals))))
    return out


def _kelly_fraction(p: float, market_p: float, kelly: float, max_pct: float) -> float:
    if market_p <= 0 or market_p >= 1 or p <= 0 or p >= 1:
        return 0.0
    b = (1 - market_p) / market_p
    f_full = (b * p - (1 - p)) / b
    return max(0.0, min(kelly * f_full, max_pct))


def evaluate_city(
    city: str,
    *,
    days: int = 60,
    edge_threshold: float = 0.05,
    kelly: float = 0.25,
    max_bet_pct: float = 0.05,
    bet_offset_hours: float = 24.0,
    request_sleep: float = 0.05,
) -> CityScore | None:
    geo = geocode(city)
    if geo is None:
        logger.warning("geocode_failed", city=city)
        return None

    today = datetime.now(timezone.utc).date()
    log_m = log_k = 0.0
    bri_m = bri_k = 0.0
    n_buckets = n_events = n_bets = 0
    pnl = 0.0
    bet_offset = int(bet_offset_hours * 3600)

    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        ev = fetch_settled_event(
            _slug_for(city, datetime(d.year, d.month, d.day)))
        time.sleep(request_sleep)
        if ev is None:
            continue
        target_date = d.strftime("%Y-%m-%d")
        members = historical_day_max(geo, target_date, ev["unit"])
        time.sleep(request_sleep)
        if not members:
            continue
        labels = [b[0] for b in ev["buckets"]]
        probs = bucket_probabilities(members, labels)
        bet_ts = ev["end_ts"] - bet_offset
        n_priced = 0
        for label, yes_token in ev["buckets"]:
            market_p = fetch_yes_price_at(yes_token, bet_ts)
            time.sleep(request_sleep)
            if market_p is None:
                continue
            p = probs.get(label, 0.0)
            won = 1.0 if label == ev["winner"] else 0.0
            pc = max(min(p, 0.999), 0.001)
            mc = max(min(market_p, 0.999), 0.001)
            log_m += -(won * math.log(pc) + (1 - won) * math.log(1 - pc))
            log_k += -(won * math.log(mc) + (1 - won) * math.log(1 - mc))
            bri_m += (p - won) ** 2
            bri_k += (market_p - won) ** 2
            edge = p - market_p
            if edge >= edge_threshold:
                f = _kelly_fraction(p, market_p, kelly, max_bet_pct)
                if f > 0:
                    n_bets += 1
                    pnl += f * ((1.0 / market_p - 1.0) if won else -1.0)
            n_buckets += 1
            n_priced += 1
        if n_priced > 0:
            n_events += 1
        logger.debug(
            "evaluated", city=city, date=target_date,
            winner=ev["winner"], members=members, priced=n_priced,
        )

    if n_events == 0:
        return None
    return CityScore(
        city=city,
        country=geo.country,
        n_events=n_events,
        n_buckets=n_buckets,
        brier_model=bri_m / n_buckets,
        brier_market=bri_k / n_buckets,
        log_loss_model=log_m / n_buckets,
        log_loss_market=log_k / n_buckets,
        kelly_pnl=pnl,
        n_bets=n_bets,
    )


def cmd_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="polymarket-bot backtest-weather")
    ap.add_argument("--days", type=int, default=60, help="Lookback window in days.")
    ap.add_argument("--cities", default=",".join(CANDIDATES),
                    help="Comma-separated city slugs (default: all 36 candidates).")
    ap.add_argument("--edge-threshold", type=float, default=0.05)
    ap.add_argument("--kelly", type=float, default=0.25)
    ap.add_argument("--max-bet-pct", type=float, default=0.05)
    ap.add_argument("--bet-offset-hours", type=float, default=24.0,
                    help="How long before resolution we'd have placed the bet.")
    ap.add_argument("--request-sleep", type=float, default=0.05,
                    help="Seconds between HTTP calls (rate-limit safety).")
    args = ap.parse_args(argv)

    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    rows: list[CityScore] = []
    for i, c in enumerate(cities):
        logger.info("city_start", city=c, n=i + 1, total=len(cities))
        s = evaluate_city(
            c,
            days=args.days,
            edge_threshold=args.edge_threshold,
            kelly=args.kelly,
            max_bet_pct=args.max_bet_pct,
            bet_offset_hours=args.bet_offset_hours,
            request_sleep=args.request_sleep,
        )
        if s is not None:
            rows.append(s)

    rows.sort(key=lambda r: (r.kelly_pnl, r.brier_market - r.brier_model),
              reverse=True)

    print()
    print(
        f"{'city':<16}{'country':<24}{'evt':>4}{'bkt':>5} "
        f"{'brier_m':>8}{'brier_k':>8}{'Δbri':>8} "
        f"{'log_m':>7}{'log_k':>7}{'bets':>5}{'pnl':>8}"
    )
    print("-" * 110)
    for r in rows:
        print(
            f"{r.city:<16}{r.country[:23]:<24}{r.n_events:>4}{r.n_buckets:>5} "
            f"{r.brier_model:>8.4f}{r.brier_market:>8.4f}"
            f"{r.brier_market - r.brier_model:>+8.4f} "
            f"{r.log_loss_model:>7.3f}{r.log_loss_market:>7.3f}"
            f"{r.n_bets:>5}{r.kelly_pnl:>+8.3f}"
        )
    print()
    print("Δbri > 0  → model lower (better) Brier than market.")
    print("pnl       → simulated Kelly PnL per $1 bankroll, summed across all bets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_main())
