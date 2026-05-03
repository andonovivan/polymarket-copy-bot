// polymarket-bot dashboard — vanilla SPA over /api/*
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

  // ---------- shell ----------
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
        <div class="card"><h3>Today Trades</h3><div class="value" id="m-trades">—</div><div class="sub" id="m-trades-sub">—</div></div>
        <div class="card"><h3>Brier (today)</h3><div class="value" id="m-brier">—</div><div class="sub">lower is better</div></div>
      </div>
      <div class="section">
        <h2 class="section-title">Equity curve (last 30d)</h2>
        <div id="equity-chart" style="height: 280px;"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Recent fills</h2>
        <div id="fills"></div>
      </div>`;

    const [stats, curve, fills] = await Promise.all([
      api("/api/stats/today").catch(() => ({})),
      api("/api/equity-curve").catch(() => ({ points: [] })),
      api("/api/fills?limit=10").catch(() => ({ trades: [] })),
    ]);

    $("#m-equity").textContent = fmtUsd(stats.latest_equity);
    const pnlEl = $("#m-pnl");
    pnlEl.textContent = fmtUsd(stats.pnl);
    pnlEl.className = "value " + (stats.pnl > 0 ? "pos" : stats.pnl < 0 ? "neg" : "");
    $("#m-pnl-sub").textContent = `${stats.wins ?? 0}W / ${(stats.trades ?? 0) - (stats.wins ?? 0)}L`;
    $("#m-trades").textContent = stats.trades ?? 0;
    $("#m-trades-sub").textContent = "win rate: " + fmtPct(stats.win_rate);
    $("#m-brier").textContent = fmtNum(stats.brier);

    // uPlot equity curve
    const pts = curve.points || [];
    const xs = pts.map((p) => p.ts);
    const ys = pts.map((p) => p.equity);
    if (xs.length > 1 && window.uPlot) {
      const opts = {
        width: $("#equity-chart").clientWidth,
        height: 260,
        scales: { x: { time: true } },
        series: [{}, { stroke: "rgb(110,168,255)", width: 1.5, fill: "rgba(110,168,255,0.08)" }],
        axes: [{ stroke: "#6b6f7a" }, { stroke: "#6b6f7a" }],
      };
      // eslint-disable-next-line no-new
      new window.uPlot(opts, [xs, ys], $("#equity-chart"));
    } else {
      $("#equity-chart").innerHTML = '<div class="empty">No equity data yet — run the bot in paper mode to populate.</div>';
    }

    const fillsRoot = $("#fills");
    if (!fills.trades.length) {
      fillsRoot.innerHTML = '<div class="empty">No fills yet.</div>';
    } else {
      fillsRoot.innerHTML = `
        <table>
          <thead><tr><th>Settled</th><th>Side</th><th>Outcome</th><th>P_model</th><th>P_market</th><th>Edge</th><th>Stake</th><th>PnL</th></tr></thead>
          <tbody>
            ${fills.trades.map((t) => `
              <tr>
                <td>${fmtTs(t.settled_at)}</td>
                <td>${t.side}</td>
                <td>${t.outcome}</td>
                <td>${fmtNum(t.predicted_p, 3)}</td>
                <td>${fmtNum(t.market_p, 3)}</td>
                <td>${fmtNum(t.edge, 3)}</td>
                <td>${fmtUsd(t.shares * t.entry_price)}</td>
                <td class="${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmtUsd(t.pnl)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }
  }

  async function pageBets() {
    $("#page-title").textContent = "Bets";
    const root = $("#page");
    const data = await api("/api/bets?size=200").catch(() => ({ trades: [] }));
    if (!data.trades.length) {
      root.innerHTML = '<div class="placeholder">No bets recorded yet.</div>';
      return;
    }
    root.innerHTML = `
      <div class="section">
        <table>
          <thead><tr><th>Settled</th><th>Market</th><th>Side</th><th>Outcome</th><th>Edge</th><th>Stake</th><th>PnL</th><th>Brier</th></tr></thead>
          <tbody>
            ${data.trades.map((t) => `
              <tr>
                <td>${fmtTs(t.settled_at)}</td>
                <td title="${t.market_id}">${(t.market_id || '').slice(0, 14)}</td>
                <td>${t.side}</td>
                <td>${t.outcome}</td>
                <td>${fmtNum(t.edge, 3)}</td>
                <td>${fmtUsd(t.shares * t.entry_price)}</td>
                <td class="${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmtUsd(t.pnl)}</td>
                <td>${fmtNum(t.brier, 3)}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  }

  async function pageBacktest() {
    $("#page-title").textContent = "Backtest";
    $("#page").innerHTML = `
      <div class="section">
        <h2 class="section-title">Run a backtest</h2>
        <p style="color: var(--text-dim); font-size: 13px; margin-top: 0;">
          Backtests are launched from the CLI for now. They write metrics + an equity curve to disk.
        </p>
        <pre style="background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-size: 12px; color: var(--text-dim);">
polymarket-bot backtest \\
  --strategy momentum_logit \\
  --from 2025-04-01 --to 2025-05-01 \\
  --kelly-fraction 0.25 --edge-threshold 0.03</pre>
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
          Edit values in <code>.env</code> and restart the bot to apply.
        </p>
      </div>`;
  }

  async function pageLogs() {
    $("#page-title").textContent = "Logs";
    $("#page").innerHTML = `<div class="placeholder">Log streaming not yet wired. Tail <code>stdout</code> from the bot process for now.</div>`;
  }

  const ROUTES = {
    dashboard: pageDashboard,
    bets: pageBets,
    backtest: pageBacktest,
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
  setInterval(route, 15000);
})();
