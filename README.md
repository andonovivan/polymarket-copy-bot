# polymarket-bot

Auto-bettor on Polymarket's recurring **BTC up/down 5-minute** binary markets.

The bot estimates `P(BTC closes up over the next 5 min)` from a logistic-regression model
trained on recent 5-min bar features, compares that estimate to the YES price on Polymarket,
and bets the side with positive edge using **fractional-Kelly** sizing. Strategies are
pluggable; backtests use the same code path as live execution.

## Quick start

```bash
pip install -e .[dev]
cp .env.example .env
# Fill in PRIVATE_KEY (only required for --live or signed CLOB reads)

polymarket-bot backfill --days 60     # cache 60 days of BTC 5m bars
polymarket-bot train --window-days 60 # fit the v1 logit model
polymarket-bot run                    # paper mode by default; dashboard at :8080
```

## Modes

- **paper** (default): real prices, simulated fills. Safe.
- **live**: requires `--live` AND `POLYMARKET_BOT_LIVE=1` set.
- **backtest**: invoked via `polymarket-bot backtest …`.

## Mathematical concept

For each upcoming 5-min market the model produces `P_model`; the Polymarket YES price is
`P_market`. The bet is taken when `|edge| = |P_model - P_market| > edge_threshold`.
Sizing is fractional-Kelly:

```
b      = (1 - price) / price            # decimal odds minus 1
p      = P_model_for_chosen_side
q      = 1 - p
f_full = (b·p - q) / b
stake  = bankroll · clamp(kelly_fraction · f_full, 0, max_bet_pct)
```

See [the plan](.claude/plans/i-want-you-to-groovy-seal.md) for full design notes.

## Layout

```
src/polymarket_bot/
  main.py            CLI: run | backfill | train | backtest
  config.py          Pydantic config from .env
  polymarket/        CLOB client, market discovery, quotes, settlement
  data/              BTC OHLCV ingestion (Binance public klines)
  features/          Feature builders + bar-history → vector pipeline
  model/             Probability model interface + LogitModel + trainer
  strategy/          Strategy ABC + registry + MomentumLogitStrategy
  risk/              Fractional-Kelly sizing, lockout, cooldown
  execution/         Broker ABC + Paper / Live / Backtest brokers + Router
  backtest/          Bar-replay engine, fees, slippage, metrics, report
  persistence/       SQLite schema + typed accessors
  dashboard/         HTTP server + JSON API + static SPA (left-nav, dark)
```

## License

Private project — not licensed for redistribution.
