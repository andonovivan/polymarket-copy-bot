# polymarket-bot — agent briefing

A Python service that bets on **Polymarket's daily city-temperature markets**
("Highest temperature in Paris on May 5") using a multi-model weather
ensemble as the probability source. Strategy is pluggable; weather is the only
strategy currently wired up.

> The repo's `README.md` is partially stale (mentions a BTC-direction strategy
> from an earlier iteration). This file is the current source of truth.

## What it does, in one paragraph

Each tick (default 60s in production), the bot enumerates open weather events
across the configured cities for the next few days, fetches the order book per
bucket from Polymarket's CLOB, fetches a 122-member multi-model ensemble
forecast from Open-Meteo, computes the per-bucket probability that the actual
day-max lands there (counts of members per bucket / total), compares to the
current YES ask, and places fractional-Kelly bets on any bucket where the
model's probability beats the ask by more than `EDGE_THRESHOLD`. Settlement
happens automatically when the gamma API reports the event resolved.

## Run modes

- **paper** (default) — real prices and forecasts, simulated fills via
  [`PaperBroker`](src/polymarket_bot/execution/paper_broker.py). Safe.
- **live** — places real orders. Requires both `--live` flag *and*
  `POLYMARKET_BOT_LIVE=1` *and* a `PRIVATE_KEY`. Two-key safety to make
  accidental live trading hard.
- **backtest-weather** — read-only research subcommand; ranks candidate cities
  by historical model-vs-market edge. See [Path A](#backtest-harness-path-a).

## Tick data flow

[`_tick`](src/polymarket_bot/main.py) is the heart of the bot:

```
discover_open_events(cities)             # weather_markets.discover_open_events
    └─ for each event:
       _persist_event                    # one DB row per bucket (markets table)
       populate_quotes                   # fetch yes_bid/ask/depth per bucket
       _attach_model_probabilities       # Open-Meteo ensemble → bucket probs
       strategy.evaluate(BetState)       # weather_forecast.WeatherForecastStrategy
       router.execute(actions)           # Router → Broker (paper / live)
       broker.reconcile_fills            # mark fills, update orders table
_settle_due_events                       # gamma outcome → write Settlement
[research capture, if RESEARCH_ENABLED]  # see Path B
_maybe_sample_equity                     # MTM equity sample for chart
```

## Directory map

```
src/polymarket_bot/
  main.py                CLI: run | redemptions | backtest-weather
  config.py              Pydantic config from env vars
  logging.py             structlog setup
  polymarket/
    markets.py           gamma API constants/helpers
    weather_markets.py   discover events, populate quotes, gamma_outcome
    book.py / quotes.py  CLOB order-book fetch + best-bid/ask parsing
    redeem.py            'redemptions' subcommand
  data/
    weather_feed.py      CITY_REGISTRY + Open-Meteo ensemble client + bucket_probabilities
  strategy/
    base.py              ABC + Bucket / WeatherEvent / BetState dataclasses
    weather_forecast.py  the only strategy (fractional Kelly off ensemble)
    registry.py          name → class
  execution/
    broker.py            ABC
    paper_broker.py      simulates fills against the live book
    live_broker.py       py-clob-client; HALT-on-error semantics
    router.py            wraps a Broker, attaches strategy name to orders
    error_codes.py       known CLOB error codes & severities
  persistence/
    schema.py            CREATE TABLE IF NOT EXISTS + forward-only migrations
    repo.py              typed accessors (Market/Order/Fill/Settlement, etc.)
  dashboard/
    server.py            stdlib HTTPServer; / → static, /api/* → handlers
    api.py               JSON API handlers
    static/              SPA: index.html, app.js, app.css
  backtest/
    weather_city_eval.py Path A: rank candidate cities by historical edge
  research/
    weather_capture.py   Path B: live capture of model_p / market_p / outcome
tests/                   pytest, 60 unit tests
```

## Strategy: `weather_forecast`

[`src/polymarket_bot/strategy/weather_forecast.py`](src/polymarket_bot/strategy/weather_forecast.py).

Inputs per tick (a `BetState`):

- **Open buckets** for an event (e.g. 11 buckets covering 7°C-or-below ... 17°C-or-higher).
- **`model_p`** per bucket from `bucket_probabilities(members, labels)`.
- **YES ask** per bucket from the order book.
- Current bankroll, exposure, lockout window, etc.

For each bucket the strategy computes `edge = model_p − yes_ask`. If `edge ≥
EDGE_THRESHOLD` (default 0.05) and the order-book depth is sufficient, it
sizes a buy via fractional-Kelly:

```
b      = (1 - price) / price
f_full = (b·p − (1−p)) / b
stake  = bankroll · clamp(KELLY_FRACTION · f_full, 0, MAX_BET_PCT)
```

Risk caps:

- `MAX_BET_PCT` per bet
- `MAX_TOTAL_EXPOSURE_PCT` aggregate cap across all open positions
- `LOCK_BUFFER_SECONDS` — stop placing bets within N seconds of resolution
- `MIN_MARKET_DEPTH_USD` — skip thin books

## City registry

The bot only trades cities listed in
[`CITY_REGISTRY`](src/polymarket_bot/data/weather_feed.py) (an allowlist
defining lat/lon/timezone/temperature-unit/event-slug-prefix per city). The
`WEATHER_CITIES` env var is a *further* filter — both must contain a city for
it to be traded.

**Current registry (11 cities, all Celsius, no US):**
paris, madrid, london, tokyo, taipei, moscow, chengdu, shanghai, chongqing,
helsinki, beijing.

The original 4 (paris/madrid/london/tokyo) came from a Phase 0.5 backtest. The
7 added in May 2026 came from the Path A 60-day sweep — they were the cities
with the highest simulated Kelly PnL where the model had a positive expected
edge over Polymarket pricing.

To **promote a new city**: add a `City(...)` entry to `CITY_REGISTRY`
(geocode via Open-Meteo to get lat/lon/tz), then add the slug to
`WEATHER_CITIES` (or update its default in `config.py`).

## Backtest harness (Path A)

[`src/polymarket_bot/backtest/weather_city_eval.py`](src/polymarket_bot/backtest/weather_city_eval.py),
CLI: `polymarket-bot backtest-weather --days 60`.

Ranks **candidate non-US cities** (currently 36 known from gamma) by
historical model-vs-market edge. For each city, walks back N days, fetches
each settled event by exact slug, replays the model from Open-Meteo's
**Historical Forecast API** (3 deterministic models — GFS, ECMWF, ICON — used
as 3 ensemble "members"), pulls each bucket's YES price from the CLOB
`prices-history` endpoint at `end_ts − bet_offset_hours` (default 24h), and
scores Brier + log-loss + simulated Kelly PnL.

**Critical caveat — the harness is a directional filter, not precise.** The
live bot uses Open-Meteo's 122-member Ensemble API. That endpoint *cannot* be
replayed historically (past dates return null), so this harness substitutes
the 3-model deterministic API. With only 3 members, bucket probabilities
collapse to {0, ⅓, ⅔, 1} — much coarser than the 122-member spread the live
bot sees. **Production is expected to outperform Path A's reported edge**, so
treat positive Path A signals as strong "promote" candidates and
slightly-negative ones as ambiguous.

A full 36-city × 60-day run takes **~2 hours** (per-bucket prices-history
calls dominate, ~28k requests with rate-limit sleeps). Output is a sorted
table of `brier_model`, `brier_market`, `bets`, `pnl`.

## Live research capture (Path B)

[`src/polymarket_bot/research/weather_capture.py`](src/polymarket_bot/research/weather_capture.py).

Read-only — never places orders. Off by default; enable with
`RESEARCH_ENABLED=1`. When enabled, each tick:

1. Lazily geocodes the candidate city set (everything in
   `CANDIDATES` from `weather_city_eval.py` minus `CITY_REGISTRY`).
2. Discovers their open events; filters to those settling within
   `RESEARCH_WINDOW_SECONDS` (default 1h).
3. Snapshots `(model_p, yes_mid, yes_bid, yes_ask)` per bucket into the
   `weather_research_obs` table.
4. Dedupes within `RESEARCH_DEDUPE_SECONDS` (default 10 min) per
   (city, slug, bucket).
5. Backfills `outcome` after settlement via the gamma API.

Path B is the **ground-truth dataset** for promoting cities — same model,
same data sources, same closing-price methodology as live trading. No
historical-API leakage. The intent is to let it accumulate ~30 days, then
re-rank candidates from this table directly.

Schema:
[`weather_research_obs`](src/polymarket_bot/persistence/schema.py) —
`(city_key, target_date, slug, bucket_label, model_p, market_yes_mid/bid/ask,
observed_at, outcome, settled_at)`. Indexed for the obs-lookup and
unsettled-rows-per-day queries.

## Dashboard

HTTP server at port `DASHBOARD_PORT` (default 8080; Docker exposes 8085 via
the env file). Vanilla JS SPA over a small JSON API.

**API endpoints** ([`api.py`](src/polymarket_bot/dashboard/api.py)):

- `GET /api/status` — mode, version, strategy, server time
- `GET /api/position` — open orders + inventory + totals (cost / mtm /
  unrealized)
- `GET /api/equity-curve` — `{points: [{ts, equity}]}`
- `GET /api/stats/today` — settlements / wins / pnl / latest_equity
- `GET /api/fills?limit=N&offset=M` — `{fills, offset, has_more}`
- `GET /api/settlements?limit=N&offset=M` — `{settlements, offset, has_more}`
- `GET /api/strategies` — registered strategies + which is enabled
- `GET /api/settings` — current config (secrets masked)
- `GET /api/logs` — placeholder; returns `{lines: []}`

The `has_more` field is computed server-side via the cheap `LIMIT N+1` trick
— no `COUNT(*)` query.

**Frontend** ([`app.js`](src/polymarket_bot/dashboard/static/app.js)):

- A `Table` class powers all data tables. Two modes:
  - **Data mode** (`setRows()`) — caller pushes the full row set; sortable
    headers; no pagination. Used by the dashboard's compact tables and the
    Strategies page.
  - **Fetcher mode** (paginated, lazy-loaded) — caller supplies an async
    `fetcher({offset, limit})`; the table loads pages itself and triggers the
    next page via an `IntersectionObserver` on a sentinel row. Used by the
    Fills and Settlements pages.
- All tables are sortable: click any column header (toggles asc/desc, shows
  ▲/▼ arrow). Sort runs over **currently-loaded** rows only — for paginated
  tables this is a per-window sort, not a global one.
- Auto-refresh: header every 5s, active page every 10s.
  - **Disabled on Fills + Settlements** to avoid merging new rows into the
    user's scroll position. Re-navigating to those pages re-fetches.
  - Dashboard tables refresh via `Table.setRows(...)` so sort state is
    preserved across ticks; only `tbody.innerHTML` is mutated, and even that
    is skipped when the rendered HTML is unchanged (`__lastHTML` cache).

## Persistence

SQLite, single file at `BOT_DB_PATH` (Docker default
`/app/state/bot_state.db`, mounted on the `bot_state` named volume).

Tables ([schema.py](src/polymarket_bot/persistence/schema.py)):
`markets`, `orders`, `fills`, `settlements`, `equity_curve`, `meta`,
`weather_research_obs`. WAL journaling, foreign keys on, busy_timeout 5s.

Schema migrations are forward-only (`CREATE TABLE IF NOT EXISTS` plus
explicit `ALTER TABLE ADD COLUMN` for known new columns). Legacy
direction-prediction tables from a previous iteration are dropped on first
boot.

Inventory and PnL are **derived from `fills` and `settlements`** at query
time — there is no live position table to keep in sync. This makes the
schema simpler at the cost of slightly more SQL per tick.

## Configuration

[`config.py`](src/polymarket_bot/config.py). All fields have defaults; env
vars override. Notable knobs:

- `WEATHER_CITIES` — comma-separated subset of `CITY_REGISTRY`
- `EDGE_THRESHOLD`, `KELLY_FRACTION`, `MAX_BET_PCT`,
  `MAX_TOTAL_EXPOSURE_PCT` — sizing & risk
- `LOCK_BUFFER_SECONDS` — pre-resolution lockout
- `WINNING_FEE_BPS` — Polymarket charges 5% on winnings (taker only); we are
  always takers
- `TICK_SECONDS` — polling cadence
- `DAYS_AHEAD` — how many forward days of markets to consider
- `RESEARCH_ENABLED` / `RESEARCH_WINDOW_SECONDS` / `RESEARCH_DEDUPE_SECONDS`
  — Path B controls
- `POLYMARKET_BOT_LIVE` (must be `1` to enable real orders, paired with
  `--live` flag)

## Running locally

```bash
pip install -e .[dev]
cp .env.example .env       # if a sample exists; otherwise create empty .env
polymarket-bot run                    # paper mode
polymarket-bot backtest-weather --days 60   # Path A
polymarket-bot redemptions            # list winning YES positions awaiting redeem
```

Dashboard: http://localhost:8080 (or whatever `DASHBOARD_PORT` is set to).

## Deployment (Docker)

```bash
docker compose up -d --build bot
docker compose logs -f bot
```

Compose file [`docker-compose.yml`](docker-compose.yml) reads `.env`,
publishes `${DASHBOARD_PORT:-8080}` (host) → same (container), mounts
`bot_state` named volume at `/app/state`.

To change `WEATHER_CITIES` or any other config: edit `.env` and
`docker compose up -d --build bot`. To pick up source changes, the rebuild
flag (`--build`) is required.

## Tests

```bash
pytest -q   # 60 tests, runs in <1s
```

Fast and dependency-free; they don't hit the network. Tests cover
quote parsing, weather feed bucketing, strategy evaluation, settlement fees,
and live-broker error handling.

## Caveats / gotchas

1. **The Open-Meteo Ensemble API can't be replayed historically.** Past
   dates return null members. This is why Path A uses the 3-model
   deterministic Historical Forecast API and Path B captures live data into
   SQLite.

2. **Open-Meteo Historical Forecast may include same-day model runs** for
   recent past dates. Treat Path A's absolute scores skeptically; rely on
   relative ranking + Path B confirmation.

3. **Polymarket's `lastTradePrice` for closed events is post-resolution** —
   it converges to 0 or 1. For backtesting, always use the `prices-history`
   endpoint at a time before resolution; never `lastTradePrice` on settled
   markets.

4. **Equity curve has been bug-prone.**
   [`_repair_equity_curve`](src/polymarket_bot/main.py) detects pollution
   on startup (version bump or heuristic ceiling breach) and resets to the
   seed point. If the chart looks wrong, check `equity_curve_version` in
   `meta`.

5. **The live broker halts on certain CLOB errors** rather than retrying.
   See [`error_codes.py`](src/polymarket_bot/execution/error_codes.py) for
   severities. A halted bot needs operator attention — the loop
   short-circuits with `bot_halted_due_to_clob_error` until restart.

6. **City unit detection** is bucket-label-driven. Most non-US cities use
   °C. Path B currently assumes `unit="celsius"` for all candidates —
   verify before adding US cities or any city Polymarket happens to publish
   in °F.

## Common changes & where they go

| Goal | File(s) |
|---|---|
| Add a new city to production | `data/weather_feed.py:CITY_REGISTRY` + `config.py:weather_cities` |
| Adjust sizing / risk | `config.py` (env-driven, no code change usually) |
| Add a new dashboard endpoint | `dashboard/api.py:dispatch_get` |
| Add a new dashboard page | `dashboard/static/app.js` (`ROUTES` + new `pageX` fn + nav link in `index.html`) |
| Add a column to a table | the relevant `*_COLS` constant in `app.js` |
| Add a new strategy | `strategy/registry.py` + a class implementing `Strategy` |
| Change ensemble model selection | `data/weather_feed.py:get_ensemble` (the `models=` query param) |
| Add a new persistence table | `persistence/schema.py:SCHEMA_DDL` + accessors in `repo.py` |
