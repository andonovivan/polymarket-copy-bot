"""v1 strategy: features → logistic-regression P(up) → fractional-Kelly bet."""

from __future__ import annotations

import structlog

from polymarket_bot.features.pipeline import build_features
from polymarket_bot.model.base import Model
from polymarket_bot.persistence.repo import Bar
from polymarket_bot.polymarket.markets import DiscoveredMarket
from polymarket_bot.polymarket.quotes import Quote
from polymarket_bot.risk.sizing import fractional_kelly_stake
from polymarket_bot.strategy.base import Bet, Strategy, StrategyContext

logger = structlog.get_logger()


class MomentumLogitStrategy(Strategy):
    """Bet only when |P_model − P_market| > edge_threshold; size with fractional Kelly."""

    name = "momentum_logit"

    def __init__(self, model: Model | None) -> None:
        self.model = model

    def on_market(
        self,
        market: DiscoveredMarket,
        bars: list[Bar],
        quote: Quote,
        ctx: StrategyContext,
    ) -> Bet | None:
        # Need both YES/NO ask prices to evaluate either direction.
        if quote.yes_ask is None or quote.no_ask is None:
            return None
        if quote.yes_mid is None:
            return None

        # No model trained yet → no edge → no bet.
        if self.model is None:
            return None

        fv = build_features(bars)
        if fv is None:
            return None

        p_model = self.model.predict_proba(fv.values)
        p_market = quote.yes_mid
        edge = p_model - p_market

        if abs(edge) < ctx.edge_threshold:
            return None

        side = "YES" if edge > 0 else "NO"
        price_paid = quote.yes_ask if side == "YES" else quote.no_ask
        if not (0.0 < price_paid < 1.0):
            return None

        # Liquidity gate: only the side we'll cross matters.
        depth = quote.depth_yes_ask_usd if side == "YES" else quote.depth_no_ask_usd
        if depth < ctx.min_market_depth_usd:
            return None

        stake = fractional_kelly_stake(
            p_model=p_model, side=side, price_paid=price_paid,
            bankroll=ctx.bankroll, kelly_fraction=ctx.kelly_fraction,
            max_bet_pct=ctx.max_bet_pct,
        )
        if stake <= 0:
            return None

        return Bet(
            market_id=market.market_id, side=side, stake=stake,
            entry_price=price_paid, predicted_p=p_model, market_p=p_market,
            edge=edge, strategy=self.name,
            model_version=getattr(self.model, "version", "uninit"),
        )
