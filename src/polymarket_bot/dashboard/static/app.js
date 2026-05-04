// polymarket-bot MM/weather dashboard — vanilla SPA over /api/*
//
// Two-phase rendering: each page builds its skeleton once on entry, then a
// background refresh tick mutates only the data-bearing elements (textContent,
// table tbody, uPlot.setData). The whole page never re-renders unless the user
// navigates between routes.
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

  function setText(sel, text, klass) {
    const el = typeof sel === "string" ? $(sel) : sel;
    if (!el) return;
    if (el.textContent !== text) el.textContent = text;
    if (klass != null && el.className !== klass) el.className = klass;
  }

  // setHTML only writes when the body changed — kills the visible flicker on
  // tables that get a tick where nothing changed.
  function setHTMLIfChanged(el, html, cacheKey) {
    if (!el) return;
    if (el.dataset[cacheKey] === html) return;
    el.innerHTML = html;
    el.dataset[cacheKey] = html;
  }

  async function refreshHeader() {
    try {
      const s = await api("/api/status");
      setText("#version", s.version);
      const badge = $("#mode-badge");
      if (badge) {
        if (badge.textContent !== s.mode) badge.textContent = s.mode;
        const cls = "badge " + s.mode;
        if (badge.className !== cls) badge.className = cls;
      }
      setText("#last-update", new Date().toLocaleTimeString());
    } catch (e) {
      const dot = $("#run-dot");
      if (dot) dot.style.background = "var(--danger)";
      setText("#run-label", "offline");
    }
  }

  // ===========================================================================
  // Dashboard — skeleton + surgical data updates
  // ===========================================================================

  let _dashboardBuilt = false;
  let _equityChart = null;

  function buildDashboardSkeleton() {
    $("#page").innerHTML = `
      <div class="cards">
        <div class="card"><h3>Equity</h3><div class="value" id="m-equity">—</div><div class="sub">latest</div></div>
        <div class="card"><h3>Unrealized P&L</h3><div class="value" id="m-unreal">—</div><div class="sub" id="m-unreal-sub">—</div></div>
        <div class="card"><h3>Realized P&L (today)</h3><div class="value" id="m-pnl">—</div><div class="sub" id="m-pnl-sub">—</div></div>
        <div class="card"><h3>Open positions</h3><div class="value" id="m-positions">—</div><div class="sub" id="m-positions-sub">—</div></div>
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
    _equityChart = null;   // reset; will be created on first refresh that has data
  }

  async function refreshDashboardData() {
    const [stats, curve, position, fills] = await Promise.all([
      api("/api/stats/today").catch(() => ({})),
      api("/api/equity-curve").catch(() => ({ points: [] })),
      api("/api/position").catch(() => ({ open_orders: [], inventories: [], totals: {} })),
      api("/api/fills?limit=15").catch(() => ({ fills: [] })),
    ]);

    // Cards.
    setText("#m-equity", fmtUsd(stats.latest_equity));

    const unreal = position.totals?.unrealized_pnl;
    const unrealCls = "value " + (unreal > 0 ? "pos" : unreal < 0 ? "neg" : "");
    setText("#m-unreal", unreal == null ? "—" : fmtUsd(unreal), unrealCls);
    setText("#m-unreal-sub", position.totals?.cost
      ? `cost ${fmtUsd(position.totals.cost)} · mtm ${fmtUsd(position.totals.mtm_value)}`
      : "—");

    const pnlCls = "value " + (stats.pnl > 0 ? "pos" : stats.pnl < 0 ? "neg" : "");
    setText("#m-pnl", fmtUsd(stats.pnl), pnlCls);
    setText("#m-pnl-sub",
      `${stats.settlements ?? 0} settled · ${stats.wins ?? 0}W / ${(stats.settlements ?? 0) - (stats.wins ?? 0)}L · win rate ${fmtPct(stats.win_rate)}`);

    setText("#m-positions", String(position.inventories?.length ?? 0));
    setText("#m-positions-sub", `+ ${position.open_orders?.length ?? 0} open orders`);

    // Equity chart — keep the uPlot instance alive, just .setData on refresh.
    const pts = curve.points || [];
    const xs = pts.map((p) => p.ts);
    const ys = pts.map((p) => p.equity);
    const chartEl = $("#equity-chart");
    if (xs.length > 1 && window.uPlot) {
      if (!_equityChart) {
        chartEl.innerHTML = "";
        _equityChart = new window.uPlot({
          width: chartEl.clientWidth, height: 260,
          scales: { x: { time: true } },
          series: [{}, { stroke: "rgb(110,168,255)", width: 1.5, fill: "rgba(110,168,255,0.08)" }],
          axes: [{ stroke: "#6b6f7a" }, { stroke: "#6b6f7a" }],
        }, [xs, ys], chartEl);
      } else {
        _equityChart.setData([xs, ys]);
      }
    } else if (!_equityChart) {
      setHTMLIfChanged(chartEl, '<div class="empty">No equity data yet.</div>', "empty");
    }

    // Inventory.
    const invs = (position.inventories || []).slice().sort(
      (a, b) => (b.unrealized_pnl ?? 0) - (a.unrealized_pnl ?? 0)
    );
    setHTMLIfChanged($("#inventory"), invs.length ? `
      <table>
        <thead><tr>
          <th>Market</th><th>YES shares</th><th>Avg cost</th>
          <th>Current YES</th><th>Cost</th><th>MTM value</th><th>Unrealized</th>
        </tr></thead>
        <tbody>
          ${invs.map((i) => {
            const title = i.title || ((i.market_id || "").slice(0, 14) + "…");
            const cls = i.unrealized_pnl > 0 ? "pos" : i.unrealized_pnl < 0 ? "neg" : "";
            return `<tr>
              <td title="${i.market_id}">${title}</td>
              <td>${fmtNum(i.yes_shares, 2)}</td>
              <td>${fmtUsd(i.avg_yes_cost)}</td>
              <td>${i.current_yes_price == null ? "—" : fmtUsd(i.current_yes_price)}</td>
              <td>${fmtUsd(i.cost)}</td>
              <td>${i.mtm_value == null ? "—" : fmtUsd(i.mtm_value)}</td>
              <td class="${cls}">${i.unrealized_pnl == null ? "—" : fmtUsd(i.unrealized_pnl)}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>` : '<div class="empty">Flat — no open positions.</div>',
    "inv");

    // Open orders.
    const orders = position.open_orders || [];
    setHTMLIfChanged($("#open-orders"), orders.length ? `
      <table>
        <thead><tr><th>Placed</th><th>Market</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Filled</th></tr></thead>
        <tbody>
          ${orders.map((o) => `<tr>
            <td>${fmtTs(o.placed_at)}</td>
            <td title="${o.market_id}">${o.title || (o.market_id || "").slice(0, 14) + "…"}</td>
            <td>${o.token_side}</td><td>${o.side}</td>
            <td>${fmtNum(o.price, 3)}</td><td>${fmtNum(o.size, 2)}</td><td>${fmtNum(o.filled, 2)}</td>
          </tr>`).join("")}
        </tbody>
      </table>` : '<div class="empty">No open orders.</div>',
    "ord");

    // Recent fills.
    const filled = fills.fills || [];
    setHTMLIfChanged($("#fills"), filled.length ? `
      <table>
        <thead><tr><th>Filled</th><th>Market</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th></tr></thead>
        <tbody>
          ${filled.map((f) => `<tr>
            <td>${fmtTs(f.fill_ts)}</td>
            <td title="${f.market_id}">${f.title || (f.market_id || "").slice(0, 14) + "…"}</td>
            <td>${f.token_side}</td><td>${f.side}</td>
            <td>${fmtNum(f.price, 3)}</td><td>${fmtNum(f.size, 2)}</td>
            <td>${fmtUsd(f.size * f.price)}</td>
          </tr>`).join("")}
        </tbody>
      </table>` : '<div class="empty">No fills yet.</div>',
    "fills");
  }

  async function pageDashboard() {
    setText("#page-title", "Dashboard");
    if (!_dashboardBuilt) {
      buildDashboardSkeleton();
      _dashboardBuilt = true;
    }
    await refreshDashboardData();
  }

  // ===========================================================================
  // Other pages — full re-render on entry, refresh in place via setHTMLIfChanged
  // ===========================================================================

  async function pageFills() {
    setText("#page-title", "Fills");
    const data = await api("/api/fills?limit=500").catch(() => ({ fills: [] }));
    const filled = data.fills || [];
    const pageEl = $("#page");
    if (!filled.length) {
      setHTMLIfChanged(pageEl, '<div class="placeholder">No fills yet.</div>', "fillsPage");
      return;
    }
    setHTMLIfChanged(pageEl, `
      <div class="section">
        <table>
          <thead><tr><th>Filled</th><th>Market</th><th>Token</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th></tr></thead>
          <tbody>
            ${filled.map((f) => `<tr>
              <td>${fmtTs(f.fill_ts)}</td>
              <td title="${f.market_id}">${f.title || (f.market_id || "").slice(0, 14) + "…"}</td>
              <td>${f.token_side}</td><td>${f.side}</td>
              <td>${fmtNum(f.price, 3)}</td><td>${fmtNum(f.size, 2)}</td>
              <td>${fmtUsd(f.size * f.price)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>`, "fillsPage");
  }

  async function pageSettlements() {
    setText("#page-title", "Settlements");
    const data = await api("/api/settlements?limit=200").catch(() => ({ settlements: [] }));
    const ss = data.settlements || [];
    const pageEl = $("#page");
    if (!ss.length) {
      setHTMLIfChanged(pageEl, '<div class="placeholder">No settled markets yet.</div>', "settPage");
      return;
    }
    setHTMLIfChanged(pageEl, `
      <div class="section">
        <table>
          <thead><tr><th>Settled</th><th>Market</th><th>Outcome</th><th>YES</th><th>Avg YES</th><th>NO</th><th>Avg NO</th><th>Cost</th><th>Payout</th><th>PnL</th></tr></thead>
          <tbody>
            ${ss.map((s) => `<tr>
              <td>${fmtTs(s.settled_at)}</td>
              <td title="${s.market_id}">${s.title || (s.market_id || "").slice(0, 14) + "…"}</td>
              <td>${s.outcome}</td>
              <td>${fmtNum(s.yes_shares, 2)}</td><td>${fmtUsd(s.avg_yes_cost)}</td>
              <td>${fmtNum(s.no_shares, 2)}</td><td>${fmtUsd(s.avg_no_cost)}</td>
              <td>${fmtUsd(s.cost)}</td><td>${fmtUsd(s.payout)}</td>
              <td class="${s.pnl > 0 ? 'pos' : s.pnl < 0 ? 'neg' : ''}">${fmtUsd(s.pnl)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>`, "settPage");
  }

  async function pageStrategies() {
    setText("#page-title", "Strategies");
    const data = await api("/api/strategies").catch(() => ({ strategies: [] }));
    setHTMLIfChanged($("#page"), `
      <div class="section">
        <table>
          <thead><tr><th>Name</th><th>Enabled</th></tr></thead>
          <tbody>${data.strategies.map((s) => `<tr><td>${s.name}</td><td>${s.enabled ? "✓" : ""}</td></tr>`).join("")}</tbody>
        </table>
      </div>`, "stratPage");
  }

  async function pageSettings() {
    setText("#page-title", "Settings");
    const cfg = await api("/api/settings").catch(() => ({}));
    const rows = Object.entries(cfg).map(([k, v]) => `
      <tr><td style="color: var(--text-dim);">${k}</td><td>${typeof v === "object" ? JSON.stringify(v) : String(v)}</td></tr>`).join("");
    setHTMLIfChanged($("#page"), `
      <div class="section">
        <h2 class="section-title">Current configuration</h2>
        <table>${rows}</table>
        <p style="color: var(--text-faint); font-size: 12px; margin-top: 14px;">
          Edit <code>.env</code> and restart the bot to apply.
        </p>
      </div>`, "settingsPage");
  }

  async function pageLogs() {
    setText("#page-title", "Logs");
    setHTMLIfChanged($("#page"),
      '<div class="placeholder">Log streaming not yet wired. Tail <code>docker compose logs -f bot</code> instead.</div>',
      "logsPage");
  }

  // ===========================================================================
  // Routing + lifecycle
  // ===========================================================================

  const ROUTES = {
    dashboard:   { enter: pageDashboard,    refresh: refreshDashboardData },
    fills:       { enter: pageFills,        refresh: pageFills            },
    settlements: { enter: pageSettlements,  refresh: pageSettlements      },
    strategies:  { enter: pageStrategies,   refresh: pageStrategies       },
    settings:    { enter: pageSettings,     refresh: null                 },
    logs:        { enter: pageLogs,         refresh: null                 },
  };

  let _activeRoute = "dashboard";

  function setActiveNav(name) {
    document.querySelectorAll(".nav a").forEach((a) => {
      a.classList.toggle("active", a.dataset.route === name);
    });
  }

  async function navigate() {
    const hash = location.hash || "#/dashboard";
    const name = (hash.replace(/^#\//, "").split("?")[0]) || "dashboard";
    const route = ROUTES[name] || ROUTES.dashboard;
    // Switching pages destroys the previous skeleton; reset the dashboard flag.
    if (name !== "dashboard") _dashboardBuilt = false;
    _activeRoute = name;
    setActiveNav(name);
    try {
      await route.enter();
    } catch (e) {
      $("#page").innerHTML = `<div class="placeholder">${String(e)}</div>`;
    }
  }

  async function refreshActive() {
    const route = ROUTES[_activeRoute];
    if (!route || !route.refresh) return;
    try {
      await route.refresh();
    } catch (e) {
      console.error("refresh failed:", e);
    }
  }

  window.addEventListener("hashchange", navigate);
  refreshHeader();
  navigate();
  setInterval(refreshHeader, 5000);
  setInterval(refreshActive, 10000);   // background data tick — no full re-render
})();
