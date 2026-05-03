// polymarket-bot MM dashboard — vanilla SPA over /api/*
(() => {
  const $ = (sel) => document.querySelector(sel);
  const fmtUsd = (n) => (n == null ? "—" : (n >= 0 ? "" : "-") + "$" + Math.abs(n).toFixed(2));
  const fmtPct = (n) => (n == null ? "—" : (n * 100).toFixed(1) + "%");
  const fmtNum = (n, d = 4) => (n == null ? "—" : Number(n).toFixed(d));
  const fmtTs = (s) => new Date(s * 1000).toLocaleString();

  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + " " + r.status);
    return r.json();
  }

  async function refreshHeader() {
    try {
      const s = await api("/api/status");
      $("#version").textContent = s.version;
      const badge = $("#mode-badge");
      badge.textContent = s.mode;
      badge.className = "badge " + s.mode;
      $("#last-update").textContent = new Date().toLocaleTimeString();
    } catch (e) {
      $("#run-dot").style.background = "var(--danger)";
      $("#run-label").textContent = "offline";
    }
  }

  // ---------- pages ----------
  async function pageDashboard() {
    $("#page-title").textContent = "Dashboard";
    const root = $("#page");
    root.innerHTML = `
      <div class="cards">
        <div class="card"><h3>Equity</h3><div class="value" id="m-equity">—</div><div class="sub">latest</div></div>
        <div class="card"><h3>Today PnL</h3><div class="value" id="m-pnl">—</div><div class="sub" id="m-pnl-sub">—</div></div>
        <div class="card"><h3>Settlements</h3><div class="value" id="m-settlements">—</div><div class="sub" id="m-settlements-sub">—</div></div>
        <div class="card"><h3>Open orders</h3><div class="value" id="m-orders">—</div><div class="sub">resting on book</div></div>
      </div>
      <div class="section">
        <h2 class="section-title">Equity curve</h2>
        <div id="equity-chart" style="height: 280px;"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Current inventory</h2>
        <div id="inventory"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Open orders</h2>
        <div id="open-orders"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Recent fills</h2>
        <div id="fills"></div>
      </div>`;

    const [stats, curve, position, fills] = await Promise.all([
      api("/api/stats/today").catch(() => ({})),
      api("/api/equity-curve").catch(() => ({ points: [] })),
      api("/api/position").catch(() => ({ open_orders: [], inventories: [], count: 0 })),
      api("/api/fills?limit=15").catch(() => ({ fills: [] })),
    ]);

    $("#m-equity").textContent = fmtUsd(stats.latest_equity);
    const pnlEl = $("#m-pnl");
    pnlEl.textContent = fmtUsd(stats.pnl);
    pnlEl.className = "value " + (stats.pnl > 0 ? "pos" : stats.pnl < 0 ? "neg" : "");
    $("#m-pnl-sub").textContent = `${stats.wins ?? 0}W / ${(stats.settlements ?? 0) - (stats.wins ?? 0)}L`;
    $("#m-settlements").textContent = stats.settlements ?? 0;
    $("#m-settlements-sub").textContent = "win rate: " + fmtPct(stats.win_rate);
    $("#m-orders").textContent = position.count ?? 0;

    // Equity curve
    const pts = curve.points || [];
    const xs = pts.map((p) => p.ts);
    const ys = pts.map((p) => p.equity);
    if (xs.length > 1 && window.uPlot) {
      const opts = {
        width: $("#equity-chart").clientWidth, height: 260,
        scales: { x: { time: true } },
        series: [{}, { stroke: "rgb(110,168,255)", width: 1.5, fill: "rgba(110,168,255,0.08)" }],
        axes: [{ stroke: "#6b6f7a" }, { stroke: "#6b6f7a" }],
      };
      // eslint-disable-next-line no-new
      new window.uPlot(opts, [xs, ys], $("#equity-chart"));
    } else {
      $("#equity-chart").innerHTML = '<div class="empty">No equity data yet.</div>';
    }

    // Inventory
    const invRoot = $("#inventory");
    if (!position.inventories.length) {
      invRoot.innerHTML = '<div class="empty">Flat — no open positions.</div>';
    } else {
      invRoot.innerHTML = `
        <table>
          <thead><tr><th>Market</th><th>YES shares</th><th>Avg YES cost</th><th>NO shares</th><th>Avg NO cost</th><th>Imbalance</th></tr></thead>
          <tbody>
            ${position.inventories.map((i) => `
              <tr>
                <td title="${i.market_id}">${(i.market_id || '').slice(0, 14)}…</td>
                <td>${fmtNum(i.yes_shares, 2)}</td>
                <td>${fmtUsd(i.avg_yes_cost)}</td>
                <td>${fmtNum(i.no_shares, 2)}</td>
                <td>${fmtUsd(i.avg_no_cost)}</td>
                <td>${fmtNum(i.yes_shares - i.no_shares, 2)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    // Open orders
    const ordRoot = $("#open-orders");
    if (!position.open_orders.length) {
      ordRoot.innerHTML = '<div class="empty">No open orders.</div>';
    } else {
      ordRoot.innerHTML = `
        <table>
          <thead><tr><th>Placed</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Filled</th></tr></thead>
          <tbody>
            ${position.open_orders.map((o) => `
              <tr>
                <td>${fmtTs(o.placed_at)}</td>
                <td>${o.token_side}</td>
                <td>${o.side}</td>
                <td>${fmtNum(o.price, 3)}</td>
                <td>${fmtNum(o.size, 2)}</td>
                <td>${fmtNum(o.filled, 2)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    // Fills
    const fillsRoot = $("#fills");
    const filled = fills.fills || [];
    if (!filled.length) {
      fillsRoot.innerHTML = '<div class="empty">No fills yet.</div>';
    } else {
      fillsRoot.innerHTML = `
        <table>
          <thead><tr><th>Filled</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th></tr></thead>
          <tbody>
            ${filled.map((f) => `
              <tr>
                <td>${fmtTs(f.fill_ts)}</td>
                <td>${f.token_side}</td>
                <td>${f.side}</td>
                <td>${fmtNum(f.price, 3)}</td>
                <td>${fmtNum(f.size, 2)}</td>
                <td>${fmtUsd(f.size * f.price)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }
  }

  async function pageFills() {
    $("#page-title").textContent = "Fills";
    const data = await api("/api/fills?limit=500").catch(() => ({ fills: [] }));
    const filled = data.fills || [];
    if (!filled.length) {
      $("#page").innerHTML = '<div class="placeholder">No fills yet.</div>';
      return;
    }
    $("#page").innerHTML = `
      <div class="section">
        <table>
          <thead><tr><th>Filled</th><th>Market</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th></tr></thead>
          <tbody>
            ${filled.map((f) => `
              <tr>
                <td>${fmtTs(f.fill_ts)}</td>
                <td title="${f.market_id}">${(f.market_id || '').slice(0, 14)}…</td>
                <td>${f.token_side}</td>
                <td>${f.side}</td>
                <td>${fmtNum(f.price, 3)}</td>
                <td>${fmtNum(f.size, 2)}</td>
                <td>${fmtUsd(f.size * f.price)}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  }

  async function pageSettlements() {
    $("#page-title").textContent = "Settlements";
    const data = await api("/api/settlements?limit=200").catch(() => ({ settlements: [] }));
    const ss = data.settlements || [];
    if (!ss.length) {
      $("#page").innerHTML = '<div class="placeholder">No settled markets yet.</div>';
      return;
    }
    $("#page").innerHTML = `
      <div class="section">
        <table>
          <thead><tr><th>Settled</th><th>Market</th><th>Outcome</th><th>YES</th><th>Avg YES</th><th>NO</th><th>Avg NO</th><th>Cost</th><th>Payout</th><th>PnL</th></tr></thead>
          <tbody>
            ${ss.map((s) => `
              <tr>
                <td>${fmtTs(s.settled_at)}</td>
                <td title="${s.market_id}">${(s.market_id || '').slice(0, 14)}…</td>
                <td>${s.outcome}</td>
                <td>${fmtNum(s.yes_shares, 2)}</td>
                <td>${fmtUsd(s.avg_yes_cost)}</td>
                <td>${fmtNum(s.no_shares, 2)}</td>
                <td>${fmtUsd(s.avg_no_cost)}</td>
                <td>${fmtUsd(s.cost)}</td>
                <td>${fmtUsd(s.payout)}</td>
                <td class="${s.pnl > 0 ? 'pos' : s.pnl < 0 ? 'neg' : ''}">${fmtUsd(s.pnl)}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  }

  async function pageStrategies() {
    $("#page-title").textContent = "Strategies";
    const data = await api("/api/strategies").catch(() => ({ strategies: [] }));
    $("#page").innerHTML = `
      <div class="section">
        <table>
          <thead><tr><th>Name</th><th>Enabled</th></tr></thead>
          <tbody>${data.strategies.map((s) => `
            <tr><td>${s.name}</td><td>${s.enabled ? "✓" : ""}</td></tr>`).join("")}</tbody>
        </table>
      </div>`;
  }

  async function pageSettings() {
    $("#page-title").textContent = "Settings";
    const cfg = await api("/api/settings").catch(() => ({}));
    const rows = Object.entries(cfg).map(([k, v]) => `
      <tr><td style="color: var(--text-dim);">${k}</td><td>${typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>`).join("");
    $("#page").innerHTML = `
      <div class="section">
        <h2 class="section-title">Current configuration</h2>
        <table>${rows}</table>
        <p style="color: var(--text-faint); font-size: 12px; margin-top: 14px;">
          Edit <code>.env</code> and restart the bot to apply.
        </p>
      </div>`;
  }

  async function pageLogs() {
    $("#page-title").textContent = "Logs";
    $("#page").innerHTML = `<div class="placeholder">Log streaming not yet wired. Tail <code>docker compose logs -f bot</code> instead.</div>`;
  }

  const ROUTES = {
    dashboard: pageDashboard,
    fills: pageFills,
    settlements: pageSettlements,
    strategies: pageStrategies,
    settings: pageSettings,
    logs: pageLogs,
  };

  function setActiveNav(name) {
    document.querySelectorAll(".nav a").forEach((a) => {
      a.classList.toggle("active", a.dataset.route === name);
    });
  }

  async function route() {
    const hash = location.hash || "#/dashboard";
    const name = hash.replace(/^#\//, "").split("?")[0] || "dashboard";
    const handler = ROUTES[name] || pageDashboard;
    setActiveNav(name);
    try {
      await handler();
    } catch (e) {
      $("#page").innerHTML = `<div class="placeholder">${String(e)}</div>`;
    }
  }

  window.addEventListener("hashchange", route);
  refreshHeader();
  route();
  setInterval(refreshHeader, 5000);
  setInterval(route, 10000);
})();
