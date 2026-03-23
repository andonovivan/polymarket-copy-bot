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
    from polymarket_copy_bot.tracker import TradeTracker

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
            "opened_at": copier._opened_at.get(asset_id, 0),
        })

        if unrealized is not None:
            total_unrealized += unrealized
        total_cost += cost

    pnl = copier._pnl
    total_trades = pnl["total_trades"]
    win_rate = pnl["winning_trades"] / total_trades * 100 if total_trades > 0 else 0

    # Sort positions by opened_at descending (newest first).
    positions.sort(key=lambda p: p.get("opened_at", 0), reverse=True)

    # Sort closed trades by closed_at descending (newest first).
    sorted_history = sorted(
        pnl["trade_history"],
        key=lambda t: t.get("closed_at", t.get("ts", 0)),
        reverse=True,
    )

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
        "recent_trades": sorted_history,
        "dry_run": copier.config.dry_run,
        "tracked_wallets": len(copier.config.tracked_wallets),
        "tracked_wallets_list": list(copier.config.tracked_wallets),
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
  .pagination { display: flex; align-items: center; gap: 8px; margin: 8px 0 20px; }
  .pagination button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 0.8em; }
  .pagination button:hover { background: #30363d; }
  .pagination button:disabled { opacity: 0.4; cursor: default; }
  .pagination .page-info { color: #8b949e; font-size: 0.8em; }
  .wallets-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 24px; }
  .wallets-section h2 { font-size: 1em; color: #58a6ff; margin-bottom: 12px; }
  .wallet-add { display: flex; gap: 8px; margin-bottom: 12px; }
  .wallet-add input { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
    color: #c9d1d9; padding: 6px 10px; font-family: monospace; font-size: 0.85em; }
  .wallet-add input::placeholder { color: #484f58; }
  .wallet-add button { background: #238636; color: #fff; border: none; border-radius: 4px;
    padding: 6px 14px; cursor: pointer; font-size: 0.85em; white-space: nowrap; }
  .wallet-add button:hover { background: #2ea043; }
  .wallet-list { list-style: none; }
  .wallet-item { display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid #21262d; font-family: monospace; font-size: 0.85em; }
  .wallet-item:last-child { border-bottom: none; }
  .wallet-item .addr { color: #8b949e; overflow: hidden; text-overflow: ellipsis; }
  .wallet-item button { background: #da3633; color: #fff; border: none; border-radius: 4px;
    padding: 3px 10px; cursor: pointer; font-size: 0.75em; }
  .wallet-item button:hover { background: #f85149; }
  .wallet-msg { font-size: 0.8em; margin-top: 6px; min-height: 1.2em; }
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

<div class="wallets-section">
  <h2>Tracked Wallets</h2>
  <div class="wallet-add">
    <input type="text" id="wallet-input" placeholder="0x... wallet address" />
    <button onclick="addWallet()">Add Wallet</button>
  </div>
  <ul class="wallet-list" id="wallet-list"></ul>
  <div class="wallet-msg" id="wallet-msg"></div>
</div>

<h2 class="section-title">Open Positions</h2>
<table>
  <thead>
    <tr><th>Asset</th><th>Shares</th><th>Buy Price</th><th>Current</th><th>Cost</th><th>Value</th><th>P&amp;L</th><th>Opened</th></tr>
  </thead>
  <tbody id="positions-body"><tr><td colspan="8">Loading...</td></tr></tbody>
</table>
<div class="pagination" id="positions-pagination"></div>

<h2 class="section-title">Recent Closed Trades</h2>
<table>
  <thead>
    <tr><th>Asset</th><th>Buy</th><th>Sell</th><th>Shares</th><th>P&amp;L</th><th>Opened</th><th>Closed</th></tr>
  </thead>
  <tbody id="history-body"><tr><td colspan="7">Loading...</td></tr></tbody>
</table>
<div class="pagination" id="history-pagination"></div>

<div class="footer">Auto-refreshes every 10s</div>

<script>
const PAGE_SIZE = 10;
let posPage = 0, histPage = 0;
let lastData = null;

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }
function fmt(v, prefix) {
  if (v === null || v === undefined) return '-';
  const s = (prefix || '') + Math.abs(v).toFixed(4);
  return v >= 0 ? '+' + s : '-' + s;
}
function fmtUsd(v) { return fmt(v, '$'); }
function shortId(id) { return id ? id.slice(0, 8) + '...' + id.slice(-6) : '-'; }
function fmtDate(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'}) + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
}

function paginate(items, page) {
  const start = page * PAGE_SIZE;
  return items.slice(start, start + PAGE_SIZE);
}

function renderPagination(containerId, total, currentPage, onPageChange) {
  const el = document.getElementById(containerId);
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (total <= PAGE_SIZE) { el.innerHTML = ''; return; }
  el.innerHTML =
    '<button ' + (currentPage <= 0 ? 'disabled' : '') + ' onclick="' + onPageChange + '(-1)">&#8592; Prev</button>' +
    '<span class="page-info">' + (currentPage + 1) + ' / ' + totalPages + ' (' + total + ' rows)</span>' +
    '<button ' + (currentPage >= totalPages - 1 ? 'disabled' : '') + ' onclick="' + onPageChange + '(1)">Next &#8594;</button>';
}

function changePosPage(delta) {
  if (!lastData) return;
  const maxPage = Math.max(0, Math.ceil(lastData.positions.length / PAGE_SIZE) - 1);
  posPage = Math.max(0, Math.min(maxPage, posPage + delta));
  renderPositions(lastData);
}

function changeHistPage(delta) {
  if (!lastData) return;
  const maxPage = Math.max(0, Math.ceil(lastData.recent_trades.length / PAGE_SIZE) - 1);
  histPage = Math.max(0, Math.min(maxPage, histPage + delta));
  renderHistory(lastData);
}

function renderPositions(d) {
  const pb = document.getElementById('positions-body');
  if (d.positions.length === 0) {
    pb.innerHTML = '<tr><td colspan="8" style="color:#8b949e">No open positions</td></tr>';
  } else {
    const page = paginate(d.positions, posPage);
    pb.innerHTML = page.map(p => {
      const pnl = p.unrealized_pnl;
      return '<tr>' +
        '<td class="asset-id">' + shortId(p.asset_id) + '</td>' +
        '<td>' + p.shares + '</td>' +
        '<td>$' + p.buy_price + '</td>' +
        '<td>' + (p.current_price !== null ? '$' + p.current_price : '-') + '</td>' +
        '<td>$' + p.cost + '</td>' +
        '<td>' + (p.current_value !== null ? '$' + p.current_value : '-') + '</td>' +
        '<td class="' + pnlClass(pnl) + '">' + (pnl !== null ? fmtUsd(pnl) : '-') + '</td>' +
        '<td>' + fmtDate(p.opened_at) + '</td>' +
      '</tr>';
    }).join('');
  }
  renderPagination('positions-pagination', d.positions.length, posPage, 'changePosPage');
}

function renderHistory(d) {
  const hb = document.getElementById('history-body');
  if (d.recent_trades.length === 0) {
    hb.innerHTML = '<tr><td colspan="7" style="color:#8b949e">No closed trades yet</td></tr>';
  } else {
    const page = paginate(d.recent_trades, histPage);
    hb.innerHTML = page.map(t => {
      return '<tr>' +
        '<td class="asset-id">' + shortId(t.asset_id) + '</td>' +
        '<td>$' + t.buy_price + '</td>' +
        '<td>$' + t.sell_price + '</td>' +
        '<td>' + t.shares + '</td>' +
        '<td class="' + pnlClass(t.pnl) + '">' + fmtUsd(t.pnl) + '</td>' +
        '<td>' + fmtDate(t.opened_at) + '</td>' +
        '<td>' + fmtDate(t.closed_at) + '</td>' +
      '</tr>';
    }).join('');
  }
  renderPagination('history-pagination', d.recent_trades.length, histPage, 'changeHistPage');
}

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    lastData = d;

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

    // Clamp pages if data shrunk
    const maxPosPage = Math.max(0, Math.ceil(d.positions.length / PAGE_SIZE) - 1);
    if (posPage > maxPosPage) posPage = maxPosPage;
    const maxHistPage = Math.max(0, Math.ceil(d.recent_trades.length / PAGE_SIZE) - 1);
    if (histPage > maxHistPage) histPage = maxHistPage;

    renderWallets(d.tracked_wallets_list);
    renderPositions(d);
    renderHistory(d);
  } catch(e) { console.error('Dashboard refresh failed', e); }
}

function renderWallets(wallets) {
  const wl = document.getElementById('wallet-list');
  if (!wallets || wallets.length === 0) {
    wl.innerHTML = '<li style="color:#8b949e; padding:6px 0;">No wallets tracked</li>';
    return;
  }
  wl.innerHTML = wallets.map(w =>
    '<li class="wallet-item">' +
      '<span class="addr">' + w + '</span>' +
      '<button data-wallet="' + w + '" onclick="removeWallet(this.dataset.wallet)">Remove</button>' +
    '</li>'
  ).join('');
}

function showWalletMsg(text, isError) {
  const el = document.getElementById('wallet-msg');
  el.textContent = text;
  el.style.color = isError ? '#f85149' : '#3fb950';
  setTimeout(() => { el.textContent = ''; }, 4000);
}

async function addWallet() {
  const input = document.getElementById('wallet-input');
  const addr = input.value.trim().toLowerCase();
  if (!addr) return;
  if (!/^0x[a-f0-9]{40}$/i.test(addr)) { showWalletMsg('Invalid address — must be 0x + 40 hex chars', true); return; }
  try {
    const r = await fetch('/api/wallets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({wallet:addr})});
    const d = await r.json();
    if (d.error) { showWalletMsg(d.error, true); return; }
    input.value = '';
    showWalletMsg('Added ' + addr.slice(0,8) + '...', false);
    refresh();
  } catch(e) { showWalletMsg('Failed to add wallet', true); }
}

async function removeWallet(addr) {
  if (!confirm('Remove wallet ' + addr.slice(0,10) + '... from tracking?')) return;
  try {
    const r = await fetch('/api/wallets', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({wallet:addr})});
    const d = await r.json();
    if (d.error) { showWalletMsg(d.error, true); return; }
    showWalletMsg('Removed ' + addr.slice(0,8) + '...', false);
    refresh();
  } catch(e) { showWalletMsg('Failed to remove wallet', true); }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


def _add_wallet(copier: TradeCopier, tracker: TradeTracker, wallet: str) -> dict[str, Any]:
    """Add a wallet to tracking. Updates copier config, tracker config, and persists."""
    wallet = wallet.strip().lower()
    if not wallet or len(wallet) != 42 or not wallet.startswith("0x"):
        return {"error": "Invalid wallet address"}
    if wallet in copier.config.tracked_wallets:
        return {"error": "Wallet already tracked"}

    copier.config.tracked_wallets.append(wallet)
    tracker.config.tracked_wallets = copier.config.tracked_wallets
    copier._save()
    logger.info("wallet_added", wallet=wallet[:10] + "...")
    return {"ok": True, "wallets": len(copier.config.tracked_wallets)}


def _remove_wallet(copier: TradeCopier, tracker: TradeTracker, wallet: str) -> dict[str, Any]:
    """Remove a wallet from tracking."""
    wallet = wallet.strip().lower()
    if wallet not in copier.config.tracked_wallets:
        return {"error": "Wallet not found"}

    copier.config.tracked_wallets.remove(wallet)
    tracker.config.tracked_wallets = copier.config.tracked_wallets
    copier._save()
    logger.info("wallet_removed", wallet=wallet[:10] + "...")
    return {"ok": True, "wallets": len(copier.config.tracked_wallets)}


class _DashboardHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the dashboard."""

    copier: TradeCopier
    client: PolymarketClient
    tracker: TradeTracker

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        if self.path == "/api/data":
            data = _build_dashboard_data(self.copier, self.client)
            self._send_json(data)
        elif self.path == "/" or self.path == "/index.html":
            body = _HTML_TEMPLATE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/wallets":
            data = self._read_body()
            wallet = data.get("wallet", "")
            result = _add_wallet(self.copier, self.tracker, wallet)
            self._send_json(result, 200 if "ok" in result else 400)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self) -> None:
        if self.path == "/api/wallets":
            data = self._read_body()
            wallet = data.get("wallet", "")
            result = _remove_wallet(self.copier, self.tracker, wallet)
            self._send_json(result, 200 if "ok" in result else 400)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logs — too noisy."""
        pass


def start_dashboard(
    copier: TradeCopier,
    client: PolymarketClient,
    tracker: TradeTracker,
    port: int = 8080,
) -> HTTPServer:
    """Start the dashboard HTTP server in a background daemon thread."""
    handler = type(
        "Handler",
        (_DashboardHandler,),
        {"copier": copier, "client": client, "tracker": tracker},
    )
    server = HTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("dashboard_started", url=f"http://localhost:{port}")
    return server
