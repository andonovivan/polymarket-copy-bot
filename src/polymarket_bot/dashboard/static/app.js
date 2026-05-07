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

  // Escape any string going into an HTML context. Polymarket market titles
  // aren't typically hostile, but they're external data — never trust them
  // raw inside an attribute or text node.
  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]));

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
      this._rows = (opts.initialRows || []).slice();
      this._sortKey = opts.initialSort && opts.initialSort.key || null;
      this._sortDir = opts.initialSort && opts.initialSort.dir || "desc";
      this._offset = 0;
      this._hasMore = false;
      this._loading = false;
      // Data mode is "ready" immediately. Fetcher mode becomes ready after the
      // first fetch resolves; if initialRows were provided, treat that as the
      // first page and skip the auto-fetch.
      this._initialFetched = !this.fetcher || this._rows.length > 0;
      this._observer = null;
      this._render();
      if (this.fetcher && this._rows.length === 0) this._loadMore();
    }

    destroy() {
      if (this._observer) {
        this._observer.disconnect();
        this._observer = null;
      }
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
              // Format functions own their HTML output (and are responsible for
              // escaping); the raw fallback escapes by default.
              const v = c.format ? c.format(row) : esc(row[c.key]);
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

  const titleCell = (r) => {
    const mid = r.market_id || "";
    const display = r.title || (mid.slice(0, 14) + "…");
    return `<span title="${esc(mid)}">${esc(display)}</span>`;
  };
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
    { key: "token_side", label: "Token",  format: (r) => esc(r.token_side) },
    { key: "side",       label: "Side",   format: (r) => esc(r.side) },
    { key: "price",      label: "Price",  format: (r) => fmtNum(r.price, 3) },
    { key: "size",       label: "Size",   format: (r) => fmtNum(r.size, 2) },
    { key: "filled",     label: "Filled", format: (r) => fmtNum(r.filled, 2) },
  ];

  const FILL_COLS = [
    { key: "fill_ts",    label: "Filled", format: (r) => fmtTs(r.fill_ts) },
    { key: "title",      label: "Market", format: titleCell, sortKey: titleSort },
    { key: "token_side", label: "Token",  format: (r) => esc(r.token_side) },
    { key: "side",       label: "Side",   format: (r) => esc(r.side) },
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
    { key: "outcome",      label: "Outcome", format: (r) => esc(r.outcome) },
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
    {
      key: "display_name", label: "Name",
      format: (r) =>
        `<div>${esc(r.display_name || r.name)}</div>` +
        `<div class="strategy-name-key">${esc(r.name)}</div>`,
      sortKey: (r) => (r.display_name || r.name).toLowerCase(),
    },
    {
      key: "enabled", label: "Enabled",
      // Rendered as a checkbox; the Strategies page attaches a delegated
      // change handler that POSTs /api/strategies/enabled.
      format: (r) =>
        `<input type="checkbox" class="strategy-toggle" ` +
        `data-name="${esc(r.name)}"${r.enabled ? " checked" : ""}>`,
      sortKey: (r) => (r.enabled ? 1 : 0),
    },
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
  let _dailyPnlChart = null;
  let _winRateChart = null;
  let _invTable = null;
  let _ordersTable = null;
  // Tables on standalone pages — destroyed and recreated when the user
  // navigates between routes. Tracking them lets us release the
  // IntersectionObserver on tear-down and preserve sort state when a
  // refresh tick wants to update a page in place.
  let _fillsPageTable = null;
  let _settlementsPageTable = null;
  let _strategiesTable = null;

  function _destroyStandaloneTables() {
    [_fillsPageTable, _settlementsPageTable, _strategiesTable].forEach((t) => {
      if (t) t.destroy();
    });
    _fillsPageTable = _settlementsPageTable = _strategiesTable = null;
  }

  // ---- dashboard strategy filter ----

  const FILTER_KEY = "dashboard.strategy_filter";

  // Load the user's saved filter set (set of strategy names). Empty set
  // means "no override" — the API will fall back to the enabled set.
  function _loadFilter() {
    try {
      const raw = localStorage.getItem(FILTER_KEY);
      if (raw == null) return null;   // never set → use server default
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? new Set(arr) : null;
    } catch {
      return null;
    }
  }

  function _saveFilter(set) {
    try {
      localStorage.setItem(FILTER_KEY, JSON.stringify([...set]));
    } catch {
      /* localStorage disabled — silently degrade */
    }
  }

  // Called from the Strategies page after a toggle: if the user's filter
  // currently matches the previous-enabled set, slide it to track the new
  // enabled set so the dashboard naturally hides what they just disabled.
  function _refreshDashboardFilterFromEnabled(newEnabled) {
    _saveFilter(new Set(newEnabled));
    // If the dashboard skeleton is already mounted, redraw the chips and
    // refresh the data.
    if (_dashboardBuilt) {
      _renderFilterChips();
      refreshDashboardData().catch(() => {});
    }
  }

  function _renderFilterChips() {
    const el = $("#strategy-filter");
    if (!el) return;
    api("/api/strategies").then((d) => {
      const filter = _loadFilter();
      const html = (d.strategies || []).map((s) => {
        const active = filter == null ? s.enabled : filter.has(s.name);
        return `<button type="button" class="chip${active ? " active" : ""}" ` +
          `data-name="${esc(s.name)}">${esc(s.display_name || s.name)}</button>`;
      }).join("");
      setHTMLIfChanged(el, html);
    }).catch(() => {});
  }

  function buildDashboardSkeleton() {
    $("#page").innerHTML = `
      <div class="strategy-filter-bar">
        <span class="strategy-filter-label">Show:</span>
        <div id="strategy-filter" class="chip-group"></div>
      </div>
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
      <div class="charts-row">
        <div class="section section-half">
          <h2 class="section-title">Daily P&L (last 30d)</h2>
          <div id="daily-pnl-chart" style="height: 200px;"></div>
        </div>
        <div class="section section-half">
          <h2 class="section-title">Win rate (last 30d)</h2>
          <div id="winrate-chart" style="height: 200px;"></div>
        </div>
      </div>
      <div class="section">
        <h2 class="section-title">P&L by strategy</h2>
        <div id="strategy-pnl" class="bar-list"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Current inventory</h2>
        <div id="inventory"></div>
      </div>
      <div class="section">
        <h2 class="section-title">Open orders</h2>
        <div id="open-orders"></div>
      </div>`;
    _equityChart = null;
    _dailyPnlChart = null;
    _winRateChart = null;
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
    // Populate the filter chips and wire click → toggle.
    _renderFilterChips();
    $("#strategy-filter").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      const filter = _loadFilter() || _enabledFromCachedChips();
      const name = chip.dataset.name;
      if (filter.has(name)) filter.delete(name);
      else filter.add(name);
      _saveFilter(filter);
      _renderFilterChips();
      refreshDashboardData().catch(() => {});
    });
  }

  // Read the chip group's current visual state (active class) — used as a
  // fallback when localStorage hasn't been initialised yet.
  function _enabledFromCachedChips() {
    const chips = document.querySelectorAll("#strategy-filter .chip.active");
    return new Set([...chips].map((c) => c.dataset.name));
  }

  // ---------- chart helpers ----------

  // Reusable uPlot factory: pass the container, dataset and series spec.
  // Returns the chart instance — caller stashes it and uses .setData() on
  // subsequent ticks.
  function _makeChart(container, data, opts) {
    if (!window.uPlot) return null;
    container.innerHTML = "";
    const cfg = {
      width: container.clientWidth,
      height: opts.height || 200,
      scales: opts.scales || { x: { time: false } },
      series: opts.series,
      axes: opts.axes || [{ stroke: "#6b6f7a" }, { stroke: "#6b6f7a" }],
      legend: { show: false },
    };
    return new window.uPlot(cfg, data, container);
  }

  // Bar-paths factory for uPlot — emulates a vertical bar chart with a line
  // series (uPlot has no native bars on this version). Returns a paths fn.
  function _barPaths(width = 0.7) {
    return (u, seriesIdx) => {
      const path = new Path2D();
      const xs = u.data[0];
      const ys = u.data[seriesIdx];
      const zeroY = u.valToPos(0, "y", true);
      for (let i = 0; i < xs.length; i++) {
        const v = ys[i];
        if (v == null) continue;
        const xPx = u.valToPos(xs[i], "x", true);
        const yPx = u.valToPos(v, "y", true);
        const halfW = (u.bbox.width / xs.length) * width / 2;
        const top = Math.min(zeroY, yPx);
        const h = Math.abs(zeroY - yPx);
        path.rect(xPx - halfW, top, halfW * 2, h);
      }
      return { fill: path };
    };
  }

  // Render a list of {label, primary, secondary, pnl, color_from} bars into
  // a `<div class="bar-list">` container. Used for Inventory-by-city and
  // PnL-by-strategy. Pure HTML/CSS — no chart library.
  function _renderBarList(container, rows, opts) {
    if (!rows || rows.length === 0) {
      setHTMLIfChanged(container, '<div class="empty">' + (opts.emptyText || "No data.") + '</div>');
      return;
    }
    const maxAbs = Math.max(...rows.map((r) => Math.abs(r.barValue || 0)), 1e-9);
    const html = rows.map((r) => {
      const pct = (Math.abs(r.barValue || 0) / maxAbs) * 100;
      const cls = r.barValue > 0 ? "pos" : r.barValue < 0 ? "neg" : "";
      return `<div class="bar-row">
        <div class="bar-row-label">${esc(r.label)}</div>
        <div class="bar-row-track">
          <div class="bar-row-fill ${cls}" style="width: ${pct.toFixed(1)}%"></div>
        </div>
        <div class="bar-row-value">${esc(r.valueText || "")}</div>
      </div>`;
    }).join("");
    setHTMLIfChanged(container, html);
  }

  // ---------- main refresh ----------

  async function refreshDashboardData() {
    // Single bundled fetch replacing the 4 parallel calls of the old
    // implementation. See dashboard/api.py:dispatch_get("/api/dashboard").
    // ?strategies= filter is appended when the user has narrowed the chips.
    const filter = _loadFilter();
    const filterParam = filter && filter.size > 0
      ? "?strategies=" + [...filter].map(encodeURIComponent).join(",")
      : "";
    const data = await api("/api/dashboard" + filterParam).catch(() => ({
      stats_today: {}, totals: {}, inventories: [], open_orders: [],
      equity_curve: [], daily_pnl: [], strategy_pnl: [],
    }));

    // Re-render the chip group on every refresh tick so a freshly
    // registered/enabled strategy shows up without a route re-entry.
    // _renderFilterChips() is idempotent and reads from /api/strategies.
    _renderFilterChips();

    const stats = data.stats_today || {};
    const totals = data.totals || {};

    // ---- cards ----

    setText("#m-equity", fmtUsd(stats.latest_equity));

    const unreal = totals.unrealized_pnl;
    const unrealCls = "value " + (unreal > 0 ? "pos" : unreal < 0 ? "neg" : "");
    setText("#m-unreal", unreal == null ? "—" : fmtUsd(unreal), unrealCls);
    setText("#m-unreal-sub", totals.cost
      ? `cost ${fmtUsd(totals.cost)} · mtm ${fmtUsd(totals.mtm_value)}`
      : "—");

    const pnlCls = "value " + (stats.pnl > 0 ? "pos" : stats.pnl < 0 ? "neg" : "");
    setText("#m-pnl", fmtUsd(stats.pnl), pnlCls);
    setText("#m-pnl-sub",
      `${stats.settlements ?? 0} settled · ${stats.wins ?? 0}W / ${(stats.settlements ?? 0) - (stats.wins ?? 0)}L · win rate ${fmtPct(stats.win_rate)}`);

    setText("#m-positions", String(data.inventories?.length ?? 0));
    setText("#m-positions-sub", `+ ${data.open_orders?.length ?? 0} open orders`);

    // ---- equity curve ----
    const pts = data.equity_curve || [];
    const xs = pts.map((p) => p.ts);
    const ys = pts.map((p) => p.equity);
    const equityEl = $("#equity-chart");
    if (xs.length > 1) {
      if (!_equityChart) {
        _equityChart = _makeChart(equityEl, [xs, ys], {
          height: 260,
          scales: { x: { time: true } },
          series: [{}, {
            stroke: "rgb(110,168,255)", width: 1.5,
            fill: "rgba(110,168,255,0.08)",
          }],
        });
      } else {
        _equityChart.setData([xs, ys]);
      }
    } else if (!_equityChart) {
      setHTMLIfChanged(equityEl, '<div class="empty">No equity data yet.</div>');
    }

    // ---- daily P&L bar chart ----
    // Two parallel series — one for positive bars, one for negative bars,
    // each with its own color. uPlot's per-series `fill` cannot color
    // individual bars within a single series, so this is the cleanest way
    // to get per-bar green/red without custom drawing.
    const daily = data.daily_pnl || [];
    const dailyEl = $("#daily-pnl-chart");
    if (daily.length > 0) {
      const dayIdx = daily.map((_, i) => i);
      const positives = daily.map((d) => (d.pnl >= 0 ? d.pnl : null));
      const negatives = daily.map((d) => (d.pnl < 0 ? d.pnl : null));
      if (!_dailyPnlChart) {
        _dailyPnlChart = _makeChart(dailyEl, [dayIdx, positives, negatives], {
          height: 180,
          scales: { x: { time: false } },
          series: [
            {},
            { stroke: "transparent", width: 0,
              fill: "rgba(72,187,120,0.55)", paths: _barPaths(0.7) },
            { stroke: "transparent", width: 0,
              fill: "rgba(255,107,107,0.55)", paths: _barPaths(0.7) },
          ],
        });
      } else {
        _dailyPnlChart.setData([dayIdx, positives, negatives]);
      }
    } else if (!_dailyPnlChart) {
      setHTMLIfChanged(dailyEl, '<div class="empty">No settlements yet.</div>');
    }

    // ---- win rate sparkline ----
    const winRateEl = $("#winrate-chart");
    if (daily.length > 1) {
      const idx = daily.map((_, i) => i);
      const rates = daily.map((d) =>
        d.n_settlements > 0 ? d.n_wins / d.n_settlements : null);
      if (!_winRateChart) {
        _winRateChart = _makeChart(winRateEl, [idx, rates], {
          height: 180,
          scales: { x: { time: false }, y: { range: [0, 1] } },
          series: [{}, {
            stroke: "rgb(187,134,252)", width: 1.5,
            fill: "rgba(187,134,252,0.08)",
          }],
        });
      } else {
        _winRateChart.setData([idx, rates]);
      }
    } else if (!_winRateChart) {
      setHTMLIfChanged(winRateEl,
        '<div class="empty">Need ≥2 days of settlements.</div>');
    }

    // ---- P&L by strategy ----
    const stratRows = (data.strategy_pnl || []).map((s) => ({
      label: `${s.display_name || s.strategy} (${s.n_settlements})`,
      barValue: s.pnl,
      valueText: `${s.pnl >= 0 ? "+" : ""}${fmtUsd(s.pnl)} · ${s.n_wins}W`,
    }));
    _renderBarList($("#strategy-pnl"), stratRows,
      { emptyText: "No settlements yet." });

    // ---- existing tables ----
    if (_invTable) _invTable.setRows(data.inventories || []);
    if (_ordersTable) _ordersTable.setRows(data.open_orders || []);
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
    _fillsPageTable = new Table($("#fills-table"), {
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
    _settlementsPageTable = new Table($("#settlements-table"), {
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

  async function _toggleStrategyEnabled(name, becomingEnabled) {
    // Snapshot the current set, mutate, POST.
    const data = await api("/api/strategies").catch(() => ({ strategies: [] }));
    const current = new Set((data.strategies || [])
      .filter((s) => s.enabled).map((s) => s.name));
    if (becomingEnabled) current.add(name);
    else current.delete(name);
    // Network errors land in catch; non-2xx responses are inspected on the
    // returned object (no throw-then-catch dance).
    let r = null;
    try {
      r = await fetch("/api/strategies/enabled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: [...current] }),
      });
    } catch (e) {
      console.error("strategy toggle failed (network):", e);
    }
    if (r && !r.ok) {
      console.error("strategy toggle failed (HTTP):", r.status);
    }
    // Refresh whichever surfaces depend on the enabled set.
    if (_strategiesTable) {
      const fresh = await api("/api/strategies").catch(() => ({ strategies: [] }));
      _strategiesTable.setRows(fresh.strategies || []);
    }
    // Push the new enabled set into the dashboard's filter state too.
    _refreshDashboardFilterFromEnabled([...current]);
  }

  async function pageStrategies() {
    setText("#page-title", "Strategies");
    const data = await api("/api/strategies").catch(() => ({ strategies: [] }));
    if (_strategiesTable) {
      // Refresh tick: just push new rows so the user's sort state is preserved.
      _strategiesTable.setRows(data.strategies || []);
      return;
    }
    $("#page").innerHTML = `<div class="section"><div id="strategies-table"></div></div>`;
    const tableEl = $("#strategies-table");
    _strategiesTable = new Table(tableEl, {
      columns: STRATEGY_COLS,
      emptyText: "No strategies registered.",
      initialSort: { key: "enabled", dir: "desc" },
      initialRows: data.strategies || [],
    });
    // Delegated change handler — toggling any checkbox fires the POST.
    tableEl.addEventListener("change", (e) => {
      const cb = e.target.closest(".strategy-toggle");
      if (!cb) return;
      _toggleStrategyEnabled(cb.dataset.name, cb.checked);
    });
  }

  async function pageSettings() {
    setText("#page-title", "Settings");
    const cfg = await api("/api/settings").catch(() => ({}));
    const rows = Object.entries(cfg).map(([k, v]) => {
      const value = typeof v === "object" ? JSON.stringify(v) : String(v);
      return `<tr><td style="color: var(--text-dim);">${esc(k)}</td><td>${esc(value)}</td></tr>`;
    }).join("");
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
    _destroyStandaloneTables();
    _activeRoute = name;
    setActiveNav(name);
    try {
      await route.enter();
    } catch (e) {
      $("#page").innerHTML = `<div class="placeholder">${esc(String(e))}</div>`;
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
  // Header refresh stays at 5s (cheap status/version ping).
  // Active-page refresh at 15s — matches the bot's tick_seconds. The
  // dashboard now uses a single bundled `/api/dashboard` call, so this
  // is one request per cycle (was 4 every 10s).
  setInterval(refreshHeader, 5000);
  setInterval(refreshActive, 15000);
})();
