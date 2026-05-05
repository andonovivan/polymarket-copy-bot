// polymarket-bot MM/weather dashboard — vanilla SPA over /api/*
//
// Two-phase rendering: each page builds its skeleton once on entry, then a
// background refresh tick mutates only the data-bearing elements (textContent,
// table tbody, uPlot.setData). The whole page never re-renders unless the user
// navigates between routes.
//
// Tables are sortable everywhere and lazy-paginate on the dedicated Fills /
// Settlements pages (IntersectionObserver on a sentinel row triggers the next
// page fetch). The dashboard's compact tables are sortable but unpaginated —
// they're bounded by current open positions and recent fills.
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

  function setHTMLIfChanged(el, html) {
    if (!el) return;
    if (el.__lastHTML === html) return;
    el.innerHTML = html;
    el.__lastHTML = html;
  }

  // ===========================================================================
  // Table — sortable headers + optional lazy pagination via fetcher.
  //
  // Two modes:
  //   data mode  — caller supplies rows via setRows(); refresh tick replaces.
  //   fetcher mode — caller supplies async fetcher({offset, limit}); the table
  //                  loads pages itself and lazy-loads more as the sentinel row
  //                  becomes visible.
  // ===========================================================================

  class Table {
    constructor(container, opts) {
      this.container = container;
      this.columns = opts.columns;
      this.fetcher = opts.fetcher || null;
      this.pageSize = opts.pageSize || 50;
      this.emptyText = opts.emptyText || "No rows.";
      this._rows = [];
      this._sortKey = opts.initialSort && opts.initialSort.key || null;
      this._sortDir = opts.initialSort && opts.initialSort.dir || "desc";
      this._offset = 0;
      this._hasMore = false;
      this._loading = false;
      this._initialFetched = !this.fetcher;   // data mode is "ready" immediately
      this._observer = null;
      this._render();
      if (this.fetcher) this._loadMore();
    }

    setRows(rows) {
      this._rows = rows.slice();
      this._renderBody();
    }

    async _loadMore() {
      if (!this.fetcher || this._loading) return;
      if (this._offset > 0 && !this._hasMore) return;
      this._loading = true;
      try {
        const res = await this.fetcher({ offset: this._offset, limit: this.pageSize });
        const items = res.items || [];
        if (this._offset === 0) this._rows = [];
        this._rows = this._rows.concat(items);
        this._hasMore = !!res.has_more;
        this._offset += items.length;
        this._initialFetched = true;
        this._renderBody();
      } catch (e) {
        console.error("table fetch failed:", e);
        this._initialFetched = true;
        this._renderBody();
      } finally {
        this._loading = false;
      }
    }

    _onSort(key) {
      const col = this.columns.find((c) => c.key === key);
      if (!col || col.sortable === false) return;
      if (this._sortKey === key) {
        this._sortDir = this._sortDir === "asc" ? "desc" : "asc";
      } else {
        this._sortKey = key;
        this._sortDir = "desc";
      }
      this._renderHead();
      this._renderBody();
    }

    _sortedRows() {
      if (!this._sortKey) return this._rows;
      const col = this.columns.find((c) => c.key === this._sortKey);
      if (!col) return this._rows;
      const get = col.sortKey || ((r) => r[col.key]);
      const dir = this._sortDir === "asc" ? 1 : -1;
      return this._rows.slice().sort((a, b) => {
        const ka = get(a), kb = get(b);
        if (ka == null && kb == null) return 0;
        if (ka == null) return 1;
        if (kb == null) return -1;
        if (ka < kb) return -dir;
        if (ka > kb) return dir;
        return 0;
      });
    }

    _renderHead() {
      const ths = this.columns.map((c) => {
        const sortable = c.sortable !== false;
        const arrow = (this._sortKey === c.key)
          ? (this._sortDir === "asc" ? " ▲" : " ▼")
          : "";
        const cls = sortable ? 'class="sortable"' : "";
        const data = sortable ? ` data-sort-key="${c.key}"` : "";
        return `<th ${cls}${data}>${c.label}${arrow}</th>`;
      }).join("");
      const html = `<tr>${ths}</tr>`;
      if (this._thead.__lastHTML !== html) {
        this._thead.innerHTML = html;
        this._thead.__lastHTML = html;
      }
    }

    _renderBody() {
      let html;
      if (this.fetcher && !this._initialFetched) {
        html = `<tr><td colspan="${this.columns.length}" class="loading">Loading…</td></tr>`;
      } else {
        const rows = this._sortedRows();
        if (rows.length === 0) {
          html = `<tr><td colspan="${this.columns.length}" class="empty">${this.emptyText}</td></tr>`;
        } else {
          html = rows.map((row) => {
            const tds = this.columns.map((c) => {
              const v = c.format ? c.format(row) : (row[c.key] == null ? "" : String(row[c.key]));
              return `<td>${v}</td>`;
            }).join("");
            return `<tr>${tds}</tr>`;
          }).join("");
          if (this.fetcher && this._hasMore) {
            html += `<tr class="sentinel"><td colspan="${this.columns.length}" class="loading">Loading more…</td></tr>`;
          }
        }
      }
      if (this._tbody.__lastHTML !== html) {
        this._tbody.innerHTML = html;
        this._tbody.__lastHTML = html;
      }
      this._setupSentinel();
    }

    _setupSentinel() {
      if (!this.fetcher) return;
      if (this._observer) this._observer.disconnect();
      const sentinel = this._tbody.querySelector("tr.sentinel");
      if (!sentinel) return;
      this._observer = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) this._loadMore();
      }, { root: null, rootMargin: "300px", threshold: 0 });
      this._observer.observe(sentinel);
    }

    _render() {
      this.container.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead></thead>
            <tbody></tbody>
          </table>
        </div>`;
      this._thead = this.container.querySelector("thead");
      this._tbody = this.container.querySelector("tbody");
      this._renderHead();
      this._renderBody();
      this._thead.addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort-key]");
        if (th) this._onSort(th.dataset.sortKey);
      });
    }
  }

  // ===========================================================================
  // Column sets
  // ===========================================================================

  const titleCell = (r) =>
    `<span title="${r.market_id || ""}">${r.title || (r.market_id || "").slice(0, 14) + "…"}</span>`;
  const titleSort = (r) => (r.title || r.market_id || "").toLowerCase();

  const INVENTORY_COLS = [
    { key: "title",             label: "Market",      format: titleCell, sortKey: titleSort },
    { key: "yes_shares",        label: "YES shares",  format: (r) => fmtNum(r.yes_shares, 2) },
    { key: "avg_yes_cost",      label: "Avg cost",    format: (r) => fmtUsd(r.avg_yes_cost) },
    { key: "current_yes_price", label: "Current YES", format: (r) => r.current_yes_price == null ? "—" : fmtUsd(r.current_yes_price) },
    { key: "cost",              label: "Cost",        format: (r) => fmtUsd(r.cost) },
    { key: "mtm_value",         label: "MTM value",   format: (r) => r.mtm_value == null ? "—" : fmtUsd(r.mtm_value) },
    {
      key: "unrealized_pnl", label: "Unrealized",
      format: (r) => {
        if (r.unrealized_pnl == null) return "—";
        const cls = r.unrealized_pnl > 0 ? "pos" : r.unrealized_pnl < 0 ? "neg" : "";
        return `<span class="${cls}">${fmtUsd(r.unrealized_pnl)}</span>`;
      },
    },
  ];

  const ORDER_COLS = [
    { key: "placed_at",  label: "Placed", format: (r) => fmtTs(r.placed_at) },
    { key: "title",      label: "Market", format: titleCell, sortKey: titleSort },
    { key: "token_side", label: "Token",  format: (r) => r.token_side },
    { key: "side",       label: "Side",   format: (r) => r.side },
    { key: "price",      label: "Price",  format: (r) => fmtNum(r.price, 3) },
    { key: "size",       label: "Size",   format: (r) => fmtNum(r.size, 2) },
    { key: "filled",     label: "Filled", format: (r) => fmtNum(r.filled, 2) },
  ];

  const FILL_COLS = [
    { key: "fill_ts",    label: "Filled", format: (r) => fmtTs(r.fill_ts) },
    { key: "title",      label: "Market", format: titleCell, sortKey: titleSort },
    { key: "token_side", label: "Token",  format: (r) => r.token_side },
    { key: "side",       label: "Side",   format: (r) => r.side },
    { key: "price",      label: "Price",  format: (r) => fmtNum(r.price, 3) },
    { key: "size",       label: "Size",   format: (r) => fmtNum(r.size, 2) },
    {
      key: "notional", label: "Notional",
      format: (r) => fmtUsd(r.size * r.price),
      sortKey: (r) => r.size * r.price,
    },
  ];

  const SETTLEMENT_COLS = [
    { key: "settled_at",   label: "Settled", format: (r) => fmtTs(r.settled_at) },
    { key: "title",        label: "Market",  format: titleCell, sortKey: titleSort },
    { key: "outcome",      label: "Outcome", format: (r) => r.outcome },
    { key: "yes_shares",   label: "YES",     format: (r) => fmtNum(r.yes_shares, 2) },
    { key: "avg_yes_cost", label: "Avg YES", format: (r) => fmtUsd(r.avg_yes_cost) },
    { key: "no_shares",    label: "NO",      format: (r) => fmtNum(r.no_shares, 2) },
    { key: "avg_no_cost",  label: "Avg NO",  format: (r) => fmtUsd(r.avg_no_cost) },
    { key: "cost",         label: "Cost",    format: (r) => fmtUsd(r.cost) },
    { key: "payout",       label: "Payout",  format: (r) => fmtUsd(r.payout) },
    {
      key: "pnl", label: "PnL",
      format: (r) => {
        const cls = r.pnl > 0 ? "pos" : r.pnl < 0 ? "neg" : "";
        return `<span class="${cls}">${fmtUsd(r.pnl)}</span>`;
      },
    },
  ];

  const STRATEGY_COLS = [
    { key: "name",    label: "Name",    format: (r) => r.name },
    { key: "enabled", label: "Enabled", format: (r) => (r.enabled ? "✓" : "") },
  ];

  // ===========================================================================
  // Header refresh
  // ===========================================================================

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
  // Dashboard — skeleton + Table instances reused across refresh ticks
  // ===========================================================================

  let _dashboardBuilt = false;
  let _equityChart = null;
  let _invTable = null;
  let _ordersTable = null;
  let _dashFillsTable = null;

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
    _equityChart = null;
    _invTable = new Table($("#inventory"), {
      columns: INVENTORY_COLS,
      emptyText: "Flat — no open positions.",
      initialSort: { key: "unrealized_pnl", dir: "desc" },
    });
    _ordersTable = new Table($("#open-orders"), {
      columns: ORDER_COLS,
      emptyText: "No open orders.",
      initialSort: { key: "placed_at", dir: "desc" },
    });
    _dashFillsTable = new Table($("#fills"), {
      columns: FILL_COLS,
      emptyText: "No fills yet.",
      initialSort: { key: "fill_ts", dir: "desc" },
    });
  }

  async function refreshDashboardData() {
    const [stats, curve, position, fills] = await Promise.all([
      api("/api/stats/today").catch(() => ({})),
      api("/api/equity-curve").catch(() => ({ points: [] })),
      api("/api/position").catch(() => ({ open_orders: [], inventories: [], totals: {} })),
      api("/api/fills?limit=15").catch(() => ({ fills: [] })),
    ]);

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
      setHTMLIfChanged(chartEl, '<div class="empty">No equity data yet.</div>');
    }

    if (_invTable) _invTable.setRows(position.inventories || []);
    if (_ordersTable) _ordersTable.setRows(position.open_orders || []);
    if (_dashFillsTable) _dashFillsTable.setRows(fills.fills || []);
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
  // Standalone pages — paginated tables build a fresh Table on entry.
  // ===========================================================================

  function pageFills() {
    setText("#page-title", "Fills");
    $("#page").innerHTML = `<div class="section"><div id="fills-table"></div></div>`;
    new Table($("#fills-table"), {
      columns: FILL_COLS,
      pageSize: 50,
      fetcher: ({ offset, limit }) =>
        api(`/api/fills?limit=${limit}&offset=${offset}`).then((r) => ({
          items: r.fills || [], has_more: !!r.has_more,
        })),
      emptyText: "No fills yet.",
      initialSort: { key: "fill_ts", dir: "desc" },
    });
  }

  function pageSettlements() {
    setText("#page-title", "Settlements");
    $("#page").innerHTML = `<div class="section"><div id="settlements-table"></div></div>`;
    new Table($("#settlements-table"), {
      columns: SETTLEMENT_COLS,
      pageSize: 50,
      fetcher: ({ offset, limit }) =>
        api(`/api/settlements?limit=${limit}&offset=${offset}`).then((r) => ({
          items: r.settlements || [], has_more: !!r.has_more,
        })),
      emptyText: "No settled markets yet.",
      initialSort: { key: "settled_at", dir: "desc" },
    });
  }

  async function pageStrategies() {
    setText("#page-title", "Strategies");
    $("#page").innerHTML = `<div class="section"><div id="strategies-table"></div></div>`;
    const data = await api("/api/strategies").catch(() => ({ strategies: [] }));
    new Table($("#strategies-table"), {
      columns: STRATEGY_COLS,
      emptyText: "No strategies registered.",
      initialSort: { key: "enabled", dir: "desc" },
    }).setRows(data.strategies || []);
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
      </div>`);
  }

  async function pageLogs() {
    setText("#page-title", "Logs");
    setHTMLIfChanged($("#page"),
      '<div class="placeholder">Log streaming not yet wired. Tail <code>docker compose logs -f bot</code> instead.</div>');
  }

  // ===========================================================================
  // Routing + lifecycle
  // ===========================================================================

  // Paginated pages skip auto-refresh — re-entering them via the navbar is the
  // refresh action. Avoids merging new rows into the user's scroll position.
  const ROUTES = {
    dashboard:   { enter: pageDashboard,    refresh: refreshDashboardData },
    fills:       { enter: pageFills,        refresh: null                 },
    settlements: { enter: pageSettlements,  refresh: null                 },
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
  setInterval(refreshActive, 10000);
})();
