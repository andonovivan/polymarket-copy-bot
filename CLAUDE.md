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
  accidental live trading hard. Always run [`preflight`](#first-live-runbook)
  before flipping the switch.
- **backtest-weather** — read-only research subcommand; ranks candidate cities
  by historical model-vs-market edge. See [Path A](#backtest-harness-path-a).
- **preflight** — `polymarket-bot preflight` runs the live-mode safety
  checks (config, gamma reachability, USDC balance + allowance, risk caps).
  Exits non-zero on any failure. Run before the first live session and
  after wallet/key/allowance changes.

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

## Strategies (multi-strategy support)

The bot now runs **multiple strategies in parallel** per tick. `STRATEGY` is a
comma-separated list (e.g. `"weather_forecast,bucket_arbitrage"`); each
strategy gets its own `Router` so per-strategy PnL attribution is preserved
through the existing `fills.strategy` column. The first name in the list is
the "primary" strategy that owns settlement-row attribution.

### `weather_forecast`

[`src/polymarket_bot/strategy/weather_forecast.py`](src/polymarket_bot/strategy/weather_forecast.py).

Inputs per tick (a `BetState`):

- **Open buckets** for an event (e.g. 11 buckets covering 7°C-or-below ... 17°C-or-higher).
- **`model_p`** per bucket from `bucket_probabilities(members, labels)`.
- **YES + NO bid/ask** per bucket from the order book (NO only when
  `NO_SIDE_ENABLED=1`).
- Current bankroll, exposure, lockout window, etc.

Three actions per bucket each tick (in priority order):

1. **Profit-take exit (#3).** If we hold YES shares and the current `yes_bid`
   exceeds `0.95·model_p + PROFIT_TAKE_BUFFER` (default 0.10), place a SELL
   for the full position at `yes_bid`. Locks in variance reduction on
   lottery tickets that paid off; the 0.95 factor accounts for the 5%
   winning fee on holding to resolution.
2. **YES entry.** `edge = model_p − yes_ask`. If `edge ≥ EDGE_THRESHOLD`
   (default 0.05), BUY YES sized fractional-Kelly.
3. **NO entry (#2).** `edge_no = (1 − model_p) − no_ask`. If `edge_no ≥
   EDGE_THRESHOLD`, BUY NO sized fractional-Kelly. Symmetric to YES; only
   evaluated when `NO_SIDE_ENABLED=1` (Bucket has no_bid/no_ask).

Sizing:

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

### `bucket_arbitrage` (#1)

[`src/polymarket_bot/strategy/bucket_arbitrage.py`](src/polymarket_bot/strategy/bucket_arbitrage.py).

Model-independent structural alpha. Each event's 11 mutually-exclusive YES
buckets must sum to ~$1 in a fair market. When the total YES asks across
all buckets is materially below $1 (≥ `ARBITRAGE_THRESHOLD = 7%` headroom),
the strategy buys every bucket at its ask in equal shares — the eventual
winner pays $1 regardless, locking in `(net_payout − cost) / cost` net of
the 5% taker fee. Conservative depth checks and a per-event cap
(`MAX_ARBITRAGE_PCT = 5%` of bankroll) prevent fat-finger blowups.

### Bayesian fusion with observed temperatures (#4)

Inside `_attach_model_probabilities`, before bucketing: when an event is
within `BAYESIAN_FUSION_WITHIN_SECONDS` (default 6h) of resolution, the
bot fetches the city's max temperature observed so far today
([`data/observations.py`](src/polymarket_bot/data/observations.py)) and
shifts every ensemble member up to at least that value. Daily max is
monotonically non-decreasing, so once we've observed 22°C every member
predicting <22°C is falsified — this sharpens probabilities right before
the betting cutoff. Off via `BAYESIAN_FUSION_ENABLED=0`.

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
`RESEARCH_ENABLED=1` (production deployment sets this in
[`docker-compose.yml`](docker-compose.yml)). When enabled, each tick:

1. Builds a capture registry of **production cities only** by default
   (`RESEARCH_CAPTURE_CANDIDATES=0`). The candidate set (~36 non-promoted
   cities) is opt-in via `RESEARCH_CAPTURE_CANDIDATES=1` — bumps cache
   slots from 11 to 40, which can blow Open-Meteo's free-tier quota.
   Production-only is enough to bootstrap calibration for the cities we
   actually trade.
2. Pre-filters slugs by expected end-date so we only gamma-fetch events
   plausibly settling within `RESEARCH_WINDOW_SECONDS` (default 1h, ±1d
   margin to absorb timezone quirks).
3. Snapshots `(model_p, model_day_max_mean, yes_mid, yes_bid, yes_ask)`
   per bucket into the `weather_research_obs` table.
4. Dedupes within `RESEARCH_DEDUPE_SECONDS` (default 10 min) per
   (city, slug, bucket).
5. Backfills `outcome` after settlement via the gamma API; gives up on
   events older than `UNRESOLVED_GIVE_UP_DAYS` (30) so cancelled markets
   don't generate forever-retries.

Path B serves two purposes:

- **City promotion** — same model, same data sources, same methodology
  as live trading. Use to confirm Path A's directional signal before
  promoting a city to `CITY_REGISTRY`.
- **Strategy calibration** — the calibration layer (below) reads back
  from this table to compute per-city bias and probability calibrators.

Schema:
[`weather_research_obs`](src/polymarket_bot/persistence/schema.py) —
`(city_key, target_date, slug, bucket_label, model_p, model_day_max_mean,
market_yes_mid/bid/ask, observed_at, outcome, settled_at)`. Indexed for
the obs-lookup and unsettled-rows-per-day queries.

## Strategy calibration

[`src/polymarket_bot/strategy/calibration.py`](src/polymarket_bot/strategy/calibration.py).

Two layers stacked on top of the raw ensemble inside
`_attach_model_probabilities`. Both are **pass-through (no-op) until enough
Path B data accumulates**, so the bot keeps trading the raw ensemble in the
meantime and the corrections kick in automatically once data is present.

**Layer 1 — per-city, temperature-conditional bias correction.** For each
settled event we have `model_day_max_mean` (ensemble mean) and the winning
bucket's midpoint (actual day-max with bucket-resolution noise). We fit a
**linear regression of forecast error against forecast temperature** over
the last 30 days (≥10 events required):

    error(model_temp) = a + b · model_temp

Each ensemble member is then shifted by its locally-evaluated bias before
bucketing — cold members get the cold-end correction, warm members the
warm-end correction. This handles the seasonality blind spot that a single
median bias would mask: a model that's +1°C in winter and -0.5°C in summer
shows up as a non-zero slope that adapts naturally as the lookback window
slides through the seasons. Inputs outside the observed temperature range
are clamped to the boundary values (no wild extrapolation).

**Layer 2 — isotonic probability calibration.** Counting-style ensemble
probabilities are usually under-dispersive (overconfident on the modal
bucket). We fit an `IsotonicRegression(model_p, won)` per city over the
last 30 days of bucket-level observations (≥110 obs required) and run
each bucket's probability through the fitted curve. Standard ML
calibration trick; reduces log-loss meaningfully when the raw model is
miscalibrated.

Both fits are cached in-process for `CACHE_TTL_SECONDS` (1h). The
in-memory caches reset on bot restart, then re-fit lazily on first use.

## Dashboard

HTTP server at port `DASHBOARD_PORT` (default 8080; Docker exposes 8085 via
the env file). Vanilla JS SPA over a small JSON API.

**API endpoints** ([`api.py`](src/polymarket_bot/dashboard/api.py)):

- `GET /api/status` — mode, version, strategy, server time
- `GET /api/dashboard?days=30` — **bundled endpoint** that drives the entire
  Dashboard route in a single request: `stats_today`, `totals`,
  `inventories`, `open_orders`, `equity_curve`, `daily_pnl`,
  `strategy_pnl`. Replaces the old 4-parallel-fetch pattern.
- `GET /api/position` — open orders + inventory + totals (kept for
  back-compat; internally reuses `_build_position_payload` with batched
  `inventory_snapshot` + `markets_bulk`)
- `GET /api/equity-curve` — `{points: [{ts, equity}]}`
- `GET /api/stats/today` — settlements / wins / pnl / latest_equity
- `GET /api/fills?limit=N&offset=M` — `{fills, offset, has_more}`
- `GET /api/settlements?limit=N&offset=M` — `{settlements, offset, has_more}`
- `GET /api/strategies` — registered strategies, each with `name`,
  `display_name`, and the **persisted-meta-backed** `enabled` flag
- `POST /api/strategies/enabled` — body `{names: [...]}` writes the
  enabled set into `meta['enabled_strategies']`. The Router checks this
  on every `execute()` and **drops only BUY** actions for disabled
  strategies — SELLs (profit-takes) and CancelOrders pass through so
  existing positions can wind down.
- `GET /api/settings` — current config (secrets masked)
- `GET /api/logs` — placeholder; returns `{lines: []}`

`/api/dashboard` accepts an optional `?strategies=A,B` query — when
provided, `strategy_pnl` is filtered server-side. The frontend
persists the selection in `localStorage['dashboard.strategy_filter']`
and rebuilds the chip group on every dashboard tick.

The `has_more` field on paginated endpoints is computed server-side via the
cheap `LIMIT N+1` trick — no `COUNT(*)` query.

**Dashboard charts and visualisations:**

- **Strategy filter chips** — at the top of the page, one per registered
  strategy. Click to include/exclude from `strategy_pnl`. Selection
  persists in `localStorage`.
- **Equity curve** — uPlot line, full width.
- **Daily P&L** (last 30 days) — uPlot bars, half width. Green/red by sign.
- **Win-rate trend** (last 30 days) — uPlot sparkline, half width, y in [0,1].
- **P&L by strategy** — HTML/CSS horizontal bars, full width. Uses
  `display_name` on each row, sourced from the registry.

The two uPlot instances share `_makeChart(container, data, opts)`; the bar
list reuses `_renderBarList(container, rows, opts)`.

**Strategies page** displays each strategy with its `display_name`, registry
key (subdued monospace), and a checkbox bound to
`POST /api/strategies/enabled`. Toggling propagates to the dashboard's
chip filter so disabled strategies disappear from stats automatically.

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
  ▲/▼ arrow). **Sort is client-side over currently-loaded rows only.** For
  the paginated Fills / Settlements pages, this means sorting by Price (or
  any non-default column) only orders the rows already loaded into the DOM;
  later pages still arrive in the server's order (most-recent first). To
  sort globally across the whole dataset we'd need server-side sort params.
- `Table.destroy()` disconnects the `IntersectionObserver`. Standalone-page
  tables are tracked at module scope so `navigate()` can release them on
  route exit.
- Auto-refresh: header every 5s, active page every **15s** (matches the
  bot's tick cadence).
  - Dashboard fetches a **single** `/api/dashboard` request per cycle —
    one request replacing the previous 4 parallel calls.
  - **Disabled on Fills + Settlements** to avoid merging new rows into the
    user's scroll position. Re-navigating to those pages re-fetches.
  - Dashboard tables refresh via `Table.setRows(...)` so sort state is
    preserved across ticks; only `tbody.innerHTML` is mutated, and even that
    is skipped when the rendered HTML is unchanged (`__lastHTML` cache).

## Persistence

**PostgreSQL** (Phase B, May 2026). Connection-pooled via
[`psycopg_pool.ConnectionPool`](src/polymarket_bot/persistence/schema.py)
in `_pool`, configured from `DATABASE_URL` (docker-compose default points
at the bundled `postgres` service).

Tables ([schema.py](src/polymarket_bot/persistence/schema.py)):
`markets`, `orders`, `fills` (BIGSERIAL id), `settlements`, `equity_curve`,
`meta`, `weather_research_obs`. Foreign keys enforced. Indexes include a
**partial index** `idx_research_obs_unsettled ON weather_research_obs
(target_date) WHERE outcome IS NULL` for the hot path that backfills
outcomes after settlement.

Connection model:
- Every repo function does
  `with get_pool().connection() as conn: conn.execute(...)`.
- The pool is thread-safe — the dashboard server thread and the tick
  loop hit it concurrently without external locking. (The legacy
  `lock()` helper from the SQLite era is gone.)
- DDL is idempotent (`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT
  EXISTS`); `init_db()` is safe to call multiple times.

Tests use [`testcontainers[postgres]`](tests/conftest.py) — a
`postgres:16-alpine` container is started once per session and tables
are `TRUNCATE … RESTART IDENTITY CASCADE`'d between tests.

Inventory and PnL are **derived from `fills` and `settlements`** at query
time — there is no live position table to keep in sync. This makes the
schema simpler at the cost of slightly more SQL per tick (mitigated by
`inventory_snapshot` and `markets_bulk` doing one round-trip per tick).

**Switching from the SQLite era** (paper data is disposable):
```bash
docker compose down -v   # destroy old SQLite volume
docker compose up -d --build   # fresh Postgres + new pool
```

## Architecture (Phase C — microservices)

The bot ships as **five long-running containers** sharing one Postgres
instance. Each strategy lives in its own process; a single
orders-watcher reconciles all CLOB fills + settles resolved events; the
dashboard is HTTP-only.

```
postgres                                — DB + healthcheck
strategy-weather       (strategy_runner) — runs WeatherForecastStrategy only
strategy-bucket-arb    (strategy_runner) — runs BucketArbitrageStrategy only
orders-watcher         (orders_watcher)  — owns CLOB fill polling + settlement
dashboard              (dashboard)       — HTTP server only, read-only on the DB
```

**Why split it.** SQLite was the original bottleneck (single writer →
deadlocks under concurrent tick loops). With Postgres in place, each
strategy gets its own httpx pool, its own ticker, its own bankroll
slice, and can be independently scaled / restarted / temporarily
disabled without dragging down the others. The orders-watcher runs as
a single instance so we never double-fill (paper) or double-settle.

### Container responsibilities

- **`strategy-*`** — runs `polymarket-bot strategy <name>`. Each tick:
  1. Reads `enabled_strategies` from `meta` — sleeps if its own name is
     missing (the dashboard toggle from Phase A).
  2. Fetches its bankroll slice via `config.strategy_share(name)` (env
     `BANKROLL_SHARE_<UPPER_NAME>`, defaults to 1/N).
  3. Discovers open events for the configured cities, populates quotes,
     attaches model probabilities, calls `strategy.evaluate(BetState)`.
  4. Routes resulting actions through `Router → Broker`.
  5. **Does NOT** call `reconcile_fills` or settle — those live in the
     orders-watcher. This is the key invariant of the split.

- **`orders-watcher`** — runs `polymarket-bot orders-watcher`. Each tick:
  1. `SELECT * FROM orders WHERE status='open'` — across all
     strategies.
  2. Groups by event-slug-prefix; synthesises a `WeatherEvent` shell
     (token-ids only, no probabilities) from the `markets` table; calls
     `broker.reconcile_fills(stub)`.
  3. Calls `_settle_resolved_events(config)` which, per Phase C.5,
     dispatches to `polymarket.settle.settle_resolved_event` — the
     payout is decomposed into one Settlement row per `(market_id,
     strategy)` based on `fills.strategy` aggregation.

- **`dashboard`** — runs `polymarket-bot dashboard`. Wraps the existing
  `start_dashboard()` HTTP thread, then heartbeats `last_running_ts:dashboard`
  every 30s so docker-compose can prove liveness.

### Per-strategy bankroll

Each strategy commits inside its own slice. The runner reads it lazily:

```
share = config.strategy_share(strategy.name)   # 0.0 .. 1.0
strat_bankroll = global_equity * share
```

Override per strategy with env vars: `BANKROLL_SHARE_WEATHER_FORECAST=0.7`,
`BANKROLL_SHARE_BUCKET_ARBITRAGE=0.3`. Default is 1/N for the N
registered strategies. Out-of-range values clamp to `[0, 1]`. Inside
`_strategy_tick`, exposure is also strategy-scoped via
`inventory_snapshot_for(strategy.name, market_ids)` — strategies can
hold YES on the same market without competing for each other's exposure
budget.

### Settlement decomposition

`settlements` PK is now **composite**: `(market_id, strategy)`. When a
market resolves, `settle_resolved_event` aggregates `fills` by
strategy and writes one row per strategy that took a fill on it. This
means per-strategy P&L on the dashboard is accurate even when both
strategies bought the same bucket. `insert_settlement` upserts on
`(market_id, strategy)` — re-running settlement is idempotent.

### Shared forecast cache

N strategy containers calling Open-Meteo independently would multiply
fetches by N and trip rate limits. Mitigation:

- L1 in-process dict, TTL 3h (per `weather_feed.CACHE_TTL_SECONDS`).
- L2 Postgres `forecast_cache (city_key, target_date, fetched_at,
  members JSONB)`. `get_ensemble` checks L1 → L2 → Open-Meteo, writing
  back to L2 (and L1) on every external fetch. All strategy services
  share the L2 table, so the second container's first call after the
  first one populates the cache is free.

CLOB books stay per-service (cheap, no rate-limit risk).

### What changed in the legacy `polymarket-bot run` path

Nothing functionally — the old in-process loop still works for local
dev (`pip install -e .[dev]; polymarket-bot run`) and is still used by
several integration tests. The `bot` service has been removed from
`docker-compose.yml`; if you want it back, point a one-shot container
at the same Postgres URL and run `polymarket-bot run`.

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
  / `RESEARCH_CAPTURE_CANDIDATES` — Path B controls
- `BAYESIAN_FUSION_ENABLED` / `BAYESIAN_FUSION_WITHIN_SECONDS` — observed-temp
  fusion in `_attach_model_probabilities` (#4)
- `NO_SIDE_ENABLED` — pulls the NO order book in `populate_quotes` and lets
  `weather_forecast` buy NO on over-priced buckets (#2). Doubles per-bucket
  CLOB calls; off by default.
- `STRATEGY` — comma-separated list (e.g. `"weather_forecast,bucket_arbitrage"`).
  First entry is the primary strategy for settlement attribution.
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
docker compose up -d --build
docker compose logs -f orders-watcher
docker compose logs -f strategy-weather
```

Compose file [`docker-compose.yml`](docker-compose.yml) defines five
services: `postgres:16-alpine` (with `pg_data` named volume),
`strategy-weather`, `strategy-bucket-arb`, `orders-watcher`, and
`dashboard`. All four app services `depends_on: postgres { condition:
service_healthy }`, share `.env`, and connect via `DATABASE_URL` to the
in-network `postgres` host. Only `dashboard` publishes a host port
(`${DASHBOARD_PORT:-8080}`).

To change `WEATHER_CITIES` or any other shared config: edit `.env` and
`docker compose up -d --build`. To pick up source changes, the rebuild
flag (`--build`) is required.

To pause one strategy without restarting anything: hit the dashboard's
Strategies page and toggle it off — the runner reads
`enabled_strategies` from the DB on every tick and will stop emitting
new BUYs (existing positions still profit-take and settle through the
orders-watcher).

## Tests

```bash
pytest -q   # ~170 tests; ~10s on first run (Postgres testcontainer warm-up), <3s thereafter
```

Tests use a session-scoped `postgres:16-alpine` testcontainer with
`TRUNCATE … RESTART IDENTITY CASCADE` between tests; they don't hit
the network. Coverage spans quote parsing, weather feed bucketing,
strategy evaluation, settlement fees, live-broker error handling,
Phase C per-strategy helpers (`inventory_snapshot_for`,
`total_open_exposure_for`, `forecast_cache_*`), the strategy-runner
disabled-gate, and per-strategy `strategy_share` config logic.

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

## First-live runbook

The bot has a two-key safety on the live path: `--live` flag **and**
`POLYMARKET_BOT_LIVE=1` env. The first time you flip the switch, follow
this in order:

1. **`polymarket-bot preflight`** — checks `PRIVATE_KEY` is set, chain id
   is Polygon mainnet (137), gamma is reachable, the wallet has USDC and
   has approved the CLOB exchange contract for spending. Exits non-zero
   on any failure. Re-run after any wallet / key / allowance change.
2. **Set the cap small first.** `MAX_ORDER_NOTIONAL_USD=5` for the first
   session. The Router blocks any single BUY/SELL above this cap.
3. **Drift watch.** `sync_wallet_balance` runs every 5 min and emits an
   `equity_drift` warning when wallet cash diverges from our derived
   realized cash by > $1. If you see one, stop the bot and audit the
   `fills` / `settlements` tables manually before resuming.
4. **HALT recovery.** A HALT-class CLOB error (`Unauthorized`, `address
   banned`, etc.) sets a process-wide `_HALTED` flag and the next tick
   exits with `bot_halted_due_to_clob_error`. Inspect the logs for the
   triggering error, fix it (rotate key / re-approve contract / update
   creds in `.env`), then `docker compose restart bot`.
5. **Retry semantics.** Transient CLOB errors (rate-limit, matching-engine
   restart, transport blips) are retried up to 3× with exponential backoff
   inside `LiveBroker._place_with_retry`. SKIP-class errors (tick size,
   min order, expiry) propagate immediately — fix the strategy, don't
   retry the order. After `_RETRY_MAX` failed attempts the order is
   abandoned with `place_gave_up`; the strategy will re-evaluate on the
   next tick.
6. **Fill-price accuracy.** `LiveBroker.reconcile_fills` calls
   `client.get_trades_for_order(...)` and computes the weighted-avg
   price across CLOB trades to attribute the new chunk's fill price.
   Falls back to the order's limit price when trades are unavailable
   (correct approximation for taker fills against a static book — the
   bot's predominant order shape).

## Common changes & where they go

| Goal | File(s) |
|---|---|
| Add a new city to production | `data/weather_feed.py:CITY_REGISTRY` + `config.py:weather_cities` |
| Adjust sizing / risk | `config.py` (env-driven, no code change usually) |
| Add a new dashboard endpoint | `dashboard/api.py:dispatch_get` |
| Add a new dashboard page | `dashboard/static/app.js` (`ROUTES` + new `pageX` fn + nav link in `index.html`) |
| Add a column to a table | the relevant `*_COLS` constant in `app.js` |
| Add a new strategy | `strategy/registry.py` + class implementing `BettingStrategy` + a new `strategy-<name>` service in `docker-compose.yml` running `polymarket-bot strategy <name>` |
| Change ensemble model selection | `data/weather_feed.py:get_ensemble` (the `models=` query param) |
| Add a new persistence table | `persistence/schema.py:SCHEMA_DDL` + accessors in `repo.py` |
| Tune per-strategy bankroll split | `BANKROLL_SHARE_<UPPER_NAME>` env vars (read in `config.from_env`); defaults to 1/N |
| Change orders-watcher cadence | `TICK_SECONDS` env var (shared with strategy services today; can be split later) |
