"""Lightweight web dashboard for real-time P&L monitoring.

Runs a tiny HTTP server in a background thread. The single page auto-refreshes
every 10 seconds and shows open positions with live unrealized P&L.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from polymarket_copy_bot.client import PolymarketClient
    from polymarket_copy_bot.copier import TradeCopier

logger = structlog.get_logger()

# Cache midpoint lookups so we don't hammer the API on every page refresh.
_price_cache: dict[str, tuple[float, float]] = {}  # asset_id -> (price, timestamp)
_CACHE_TTL = 15  # seconds


def _get_cached_midpoint(client: PolymarketClient, asset_id: str) -> float | None:
    """Get midpoint with a short TTL cache."""
    now = time.time()
    cached = _price_cache.get(asset_id)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]
    mid = client.get_midpoint(asset_id)
    if mid is not None:
        _price_cache[asset_id] = (mid, now)
    return mid


def _build_dashboard_data(copier: TradeCopier, client: PolymarketClient) -> dict[str, Any]:
    """Collect all data needed for the dashboard."""
    positions = []
    total_unrealized = 0.0
    total_cost = 0.0

    for asset_id, shares in copier._shares_held.items():
        if shares <= 0:
            continue

        buy_price = copier._buy_prices.get(asset_id, 0.0)
        current_price = _get_cached_midpoint(client, asset_id)
        cost = buy_price * shares

        if current_price is not None:
            current_value = current_price * shares
            unrealized = current_value - cost
        else:
            current_value = None
            unrealized = None

        positions.append({
            "asset_id": asset_id,
            "shares": round(shares, 4),
            "buy_price": round(buy_price, 4),
            "current_price": round(current_price, 4) if current_price else None,
            "cost": round(cost, 4),
            "current_value": round(current_value, 4) if current_value else None,
            "unrealized_pnl": round(unrealized, 4) if unrealized is not None else None,
        })

        if unrealized is not None:
            total_unrealized += unrealized
        total_cost += cost

    pnl = copier._pnl
    total_trades = pnl["total_trades"]
    win_rate = pnl["winning_trades"] / total_trades * 100 if total_trades > 0 else 0

    return {
        "positions": positions,
        "total_unrealized": round(total_unrealized, 4),
        "total_cost": round(total_cost, 4),
        "realized": {
            "total": round(pnl["total_realized"], 4),
            "trades": total_trades,
            "winning": pnl["winning_trades"],
            "losing": pnl["losing_trades"],
            "win_rate": round(win_rate, 1),
        },
        "combined_pnl": round(pnl["total_realized"] + total_unrealized, 4),
        "recent_trades": pnl["trade_history"][-20:][::-1],  # last 20, newest first
        "dry_run": copier.config.dry_run,
        "tracked_wallets": len(copier.config.tracked_wallets),
        "ts": int(time.time()),
    }


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Copy Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
         background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 4px; font-size: 1.4em; }
  .subtitle { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
  .dry-run-badge { background: #d29922; color: #0d1117; padding: 2px 8px;
                   border-radius: 4px; font-size: 0.75em; font-weight: bold; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card-label { color: #8b949e; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.05em; }
  .card-value { font-size: 1.5em; font-weight: bold; margin-top: 4px; }
  .positive { color: #3fb950; }
  .negative { color: #f85149; }
  .neutral { color: #c9d1d9; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  th { text-align: left; color: #8b949e; font-size: 0.75em; text-transform: uppercase;
       letter-spacing: 0.05em; padding: 8px 12px; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.9em; }
  tr:hover { background: #161b22; }
  .section-title { color: #58a6ff; font-size: 1em; margin: 20px 0 10px; }
  .asset-id { font-family: monospace; font-size: 0.8em; color: #8b949e; }
  .footer { color: #484f58; font-size: 0.75em; margin-top: 24px; }
  @media (max-width: 600px) {
    .cards { grid-template-columns: 1fr 1fr; }
    td, th { padding: 6px 8px; font-size: 0.8em; }
  }
</style>
</head>
<body>
<h1>Polymarket Copy Bot <span id="badge"></span></h1>
<p class="subtitle">Tracking <span id="wallets">-</span> wallets &middot; Updated <span id="updated">-</span></p>

<div class="cards">
  <div class="card">
    <div class="card-label">Combined P&amp;L</div>
    <div class="card-value" id="combined">-</div>
  </div>
  <div class="card">
    <div class="card-label">Unrealized P&amp;L</div>
    <div class="card-value" id="unrealized">-</div>
  </div>
  <div class="card">
    <div class="card-label">Realized P&amp;L</div>
    <div class="card-value" id="realized">-</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value" id="winrate">-</div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value neutral" id="positions-count">-</div>
  </div>
  <div class="card">
    <div class="card-label">Total Invested</div>
    <div class="card-value neutral" id="invested">-</div>
  </div>
</div>

<h2 class="section-title">Open Positions</h2>
<table>
  <thead>
    <tr><th>Asset</th><th>Shares</th><th>Buy Price</th><th>Current</th><th>Cost</th><th>Value</th><th>P&amp;L</th></tr>
  </thead>
  <tbody id="positions-body"><tr><td colspan="7">Loading...</td></tr></tbody>
</table>

<h2 class="section-title">Recent Closed Trades</h2>
<table>
  <thead>
    <tr><th>Asset</th><th>Buy</th><th>Sell</th><th>Shares</th><th>P&amp;L</th><th>Time</th></tr>
  </thead>
  <tbody id="history-body"><tr><td colspan="6">Loading...</td></tr></tbody>
</table>

<div class="footer">Auto-refreshes every 10s</div>

<script>
function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }
function fmt(v, prefix) {
  if (v === null || v === undefined) return '-';
  const s = (prefix || '') + Math.abs(v).toFixed(4);
  return v >= 0 ? '+' + s : '-' + s;
}
function fmtUsd(v) { return fmt(v, '$'); }
function shortId(id) { return id ? id.slice(0, 8) + '...' + id.slice(-6) : '-'; }
function timeAgo(ts) {
  const diff = Math.floor(Date.now()/1000) - ts;
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();

    document.getElementById('badge').innerHTML = d.dry_run ? '<span class="dry-run-badge">DRY RUN</span>' : '';
    document.getElementById('wallets').textContent = d.tracked_wallets;
    document.getElementById('updated').textContent = new Date(d.ts * 1000).toLocaleTimeString();

    const ce = document.getElementById('combined');
    ce.textContent = fmtUsd(d.combined_pnl); ce.className = 'card-value ' + pnlClass(d.combined_pnl);

    const ue = document.getElementById('unrealized');
    ue.textContent = fmtUsd(d.total_unrealized); ue.className = 'card-value ' + pnlClass(d.total_unrealized);

    const re = document.getElementById('realized');
    re.textContent = fmtUsd(d.realized.total); re.className = 'card-value ' + pnlClass(d.realized.total);

    const wr = document.getElementById('winrate');
    wr.textContent = d.realized.trades > 0 ? d.realized.win_rate + '% (' + d.realized.winning + '/' + d.realized.trades + ')' : '-';
    wr.className = 'card-value ' + (d.realized.win_rate >= 50 ? 'positive' : d.realized.trades > 0 ? 'negative' : 'neutral');

    document.getElementById('positions-count').textContent = d.positions.length;
    document.getElementById('invested').textContent = '$' + d.total_cost.toFixed(2);

    // Positions table
    const pb = document.getElementById('positions-body');
    if (d.positions.length === 0) {
      pb.innerHTML = '<tr><td colspan="7" style="color:#8b949e">No open positions</td></tr>';
    } else {
      pb.innerHTML = d.positions.map(p => {
        const pnl = p.unrealized_pnl;
        return '<tr>' +
          '<td class="asset-id">' + shortId(p.asset_id) + '</td>' +
          '<td>' + p.shares + '</td>' +
          '<td>$' + p.buy_price + '</td>' +
          '<td>' + (p.current_price !== null ? '$' + p.current_price : '-') + '</td>' +
          '<td>$' + p.cost + '</td>' +
          '<td>' + (p.current_value !== null ? '$' + p.current_value : '-') + '</td>' +
          '<td class="' + pnlClass(pnl) + '">' + (pnl !== null ? fmtUsd(pnl) : '-') + '</td>' +
        '</tr>';
      }).join('');
    }

    // History table
    const hb = document.getElementById('history-body');
    if (d.recent_trades.length === 0) {
      hb.innerHTML = '<tr><td colspan="6" style="color:#8b949e">No closed trades yet</td></tr>';
    } else {
      hb.innerHTML = d.recent_trades.map(t => {
        return '<tr>' +
          '<td class="asset-id">' + shortId(t.asset_id) + '</td>' +
          '<td>$' + t.buy_price + '</td>' +
          '<td>$' + t.sell_price + '</td>' +
          '<td>' + t.shares + '</td>' +
          '<td class="' + pnlClass(t.pnl) + '">' + fmtUsd(t.pnl) + '</td>' +
          '<td>' + timeAgo(t.ts) + '</td>' +
        '</tr>';
      }).join('');
    }
  } catch(e) { console.error('Dashboard refresh failed', e); }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


class _DashboardHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the dashboard."""

    copier: TradeCopier
    client: PolymarketClient

    def do_GET(self) -> None:
        if self.path == "/api/data":
            data = _build_dashboard_data(self.copier, self.client)
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "/index.html":
            body = _HTML_TEMPLATE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logs — too noisy."""
        pass


def start_dashboard(
    copier: TradeCopier,
    client: PolymarketClient,
    port: int = 8080,
) -> HTTPServer:
    """Start the dashboard HTTP server in a background daemon thread."""
    handler = type(
        "Handler",
        (_DashboardHandler,),
        {"copier": copier, "client": client},
    )
    server = HTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("dashboard_started", url=f"http://localhost:{port}")
    return server
