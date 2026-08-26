/* fcli server UI: live SR Linux report tables fed by a server-sent event stream. */
(() => {
  "use strict";

  const WINDOW_STEP = 250; // rows appended per scroll batch
  const STATE_CLASSES = {
    up: "state-up",
    down: "state-down",
    enable: "state-enable",
    disable: "state-disable",
    established: "state-established",
    active: "state-up",
    inactive: "state-inactive",
  };

  const el = (id) => document.getElementById(id);
  const dom = {
    reportSearch: el("report-search"),
    reportList: el("report-list"),
    nodeList: el("node-list"),
    nodeSummary: el("node-summary"),
    version: el("version"),
    title: el("report-title"),
    desc: el("report-desc"),
    liveDot: el("live-dot"),
    liveLabel: el("live-label"),
    globalSearch: el("global-search"),
    invFilter: el("inv-filter"),
    clearFiltersBtn: el("clear-filters-btn"),
    filterBadge: el("filter-badge"),
    refresh: el("refresh"),
    pause: el("pause"),
    columnsBtn: el("columns-btn"),
    columnsMenu: el("columns-menu"),
    exportBtn: el("export"),
    errors: el("errors"),
    tableWrap: el("table-wrap"),
    overviewDashboard: el("overview-dashboard"),
    servicesTreeView: el("services-tree-view"),
    viewModeBtn: el("view-mode-btn"),
    headRow: el("head-row"),
    filterRow: el("filter-row"),
    body: el("grid-body"),
    empty: el("empty"),
    rowCount: el("row-count"),
    streamInfo: el("stream-info"),
    updated: el("updated"),
    themeToggle: el("theme-toggle"),
    // KPI elements
    kpiNodesTotal: el("kpi-nodes-total"),
    kpiNodesConnected: el("kpi-nodes-connected"),
    kpiNodesStreaming: el("kpi-nodes-streaming"),
    kpiNodesUnreachable: el("kpi-nodes-unreachable"),
    kpiBgpEstablished: el("kpi-bgp-established"),
    kpiBgpSub: el("kpi-bgp-sub"),
    kpiBgpTotal: el("kpi-bgp-total"),
    kpiBgpDown: el("kpi-bgp-down"),
    kpiItfTotal: el("kpi-itf-total"),
    kpiItfDown: el("kpi-itf-down"),
    kpiItfErrors: el("kpi-itf-errors"),
    kpiSubCount: el("kpi-sub-count"),
    kpiCacheCount: el("kpi-cache-count"),
    kpiResyncInt: el("kpi-resync-int"),
  };

  const state = {
    reports: [],
    report: null,
    columns: [],
    rows: [],
    errors: [],
    hidden: new Set(),
    colFilters: new Map(),
    sort: { column: null, dir: 1 },
    windowSize: WINDOW_STEP,
    paused: false,
    source: null,
    previous: new Map(), // row identity -> previous row values
    identityColumn: null,
    firstPaint: true,
    viewMode: "tree",
  };

  let overviewTimer = null;

  /* ------------------------------------------------------------ helpers */

  const debounce = (fn, ms) => {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  };

  /** Build a case-insensitive matcher; falls back to substring on a bad regex. */
  function matcher(pattern) {
    if (!pattern) return null;
    try {
      const re = new RegExp(pattern, "i");
      return (value) => re.test(value);
    } catch (_err) {
      const needle = pattern.toLowerCase();
      return (value) => value.toLowerCase().includes(needle);
    }
  }

  const isNumeric = (value) =>
    value !== "" && value !== null && value !== undefined && !isNaN(Number(value));

  /** Natural compare, so ethernet-1/10 sorts after ethernet-1/2. */
  const collator = new Intl.Collator(undefined, {
    numeric: true,
    sensitivity: "base",
  });

  function compare(a, b) {
    if (isNumeric(a) && isNumeric(b)) return Number(a) - Number(b);
    return collator.compare(String(a ?? ""), String(b ?? ""));
  }

  /* ------------------------------------------------ persistence */

  function saveReportPreferences() {
    if (!state.report || state.report.name === "overview") return;
    try {
      localStorage.setItem(
        `fcli-hidden-${state.report.name}`,
        JSON.stringify([...state.hidden])
      );
      localStorage.setItem(
        `fcli-filters-${state.report.name}`,
        JSON.stringify([...state.colFilters.entries()])
      );
      localStorage.setItem("fcli-global-search", dom.globalSearch.value);
      localStorage.setItem("fcli-inv-filter", dom.invFilter.value);
      localStorage.setItem("fcli-refresh", dom.refresh.value);
    } catch (_err) {
      /* storage unavailable */
    }
  }

  function loadReportPreferences() {
    if (!state.report || state.report.name === "overview") return;
    state.hidden.clear();
    state.colFilters.clear();
    try {
      const hiddenData = localStorage.getItem(`fcli-hidden-${state.report.name}`);
      if (hiddenData) {
        JSON.parse(hiddenData).forEach((col) => state.hidden.add(col));
      }
      const filtersData = localStorage.getItem(`fcli-filters-${state.report.name}`);
      if (filtersData) {
        JSON.parse(filtersData).forEach(([col, val]) => state.colFilters.set(col, val));
      }
    } catch (_err) {
      /* storage unavailable */
    }
  }

  function updateFilterUI() {
    let count = 0;
    if (dom.globalSearch.value.trim()) count++;
    if (dom.invFilter.value.trim()) count++;
    count += state.colFilters.size;
    if (count > 0) {
      dom.clearFiltersBtn.hidden = false;
      dom.filterBadge.textContent = count;
    } else {
      dom.clearFiltersBtn.hidden = true;
      dom.filterBadge.textContent = "";
    }
  }

  function clearAllFilters() {
    dom.globalSearch.value = "";
    dom.invFilter.value = "";
    state.colFilters.clear();
    saveReportPreferences();
    updateFilterUI();
    renderHead();
    renderBody();
    if (state.report && state.report.name !== "overview") {
      connect();
    }
  }

  /* --------------------------------------------------------- report list */

  async function loadReports() {
    const res = await fetch("/api/reports");
    const data = await res.json();
    state.reports = data.reports;
    dom.version.textContent = "v" + data.version;
    renderReportList();

    try {
      const savedGlobal = localStorage.getItem("fcli-global-search");
      if (savedGlobal) dom.globalSearch.value = savedGlobal;
      const savedInv = localStorage.getItem("fcli-inv-filter");
      if (savedInv) dom.invFilter.value = savedInv;
      const savedRefresh = localStorage.getItem("fcli-refresh");
      if (savedRefresh) dom.refresh.value = savedRefresh;
    } catch (_err) {}

    const wanted = location.hash.replace(/^#/, "");
    const initial = state.reports.find((r) => r.name === wanted) || state.reports[0];
    if (initial) selectReport(initial);
  }

  function renderReportList() {
    const needle = dom.reportSearch.value.trim().toLowerCase();
    const groups = new Map();
    for (const report of state.reports) {
      const haystack = `${report.title} ${report.name} ${report.description} ${report.category}`;
      if (needle && !haystack.toLowerCase().includes(needle)) continue;
      if (!groups.has(report.category)) groups.set(report.category, []);
      groups.get(report.category).push(report);
    }
    dom.reportList.replaceChildren();
    for (const [category, reports] of groups) {
      const section = document.createElement("div");
      section.className = "report-group";
      const heading = document.createElement("h3");
      heading.textContent = category;
      section.append(heading);
      for (const report of reports) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "report-item";
        button.textContent = report.title;
        button.title = report.description;
        if (state.report && state.report.name === report.name) {
          button.setAttribute("aria-current", "true");
        }
        button.addEventListener("click", () => selectReport(report));
        section.append(button);
      }
      dom.reportList.append(section);
    }
  }

  async function loadOverview() {
    if (!state.report || state.report.name !== "overview") return;
    try {
      const params = new URLSearchParams();
      const inv = dom.invFilter.value.trim();
      if (inv) params.set("inv_filter", inv);
      const url = "/api/overview" + (inv ? `?${params}` : "");
      const res = await fetch(url);
      const data = await res.json();
      dom.kpiNodesTotal.textContent = data.nodes.total;
      dom.kpiNodesConnected.textContent = `${data.nodes.connected} connected`;
      dom.kpiNodesStreaming.textContent = `${data.nodes.streaming} streaming`;
      dom.kpiNodesUnreachable.textContent = `${data.nodes.unreachable} unreachable`;

      dom.kpiBgpEstablished.textContent = data.bgp.established;
      dom.kpiBgpSub.textContent = `${data.bgp.established} established`;
      dom.kpiBgpTotal.textContent = `${data.bgp.total} total`;
      dom.kpiBgpDown.textContent = data.bgp.down > 0 ? ` · ${data.bgp.down} down` : "";

      dom.kpiItfTotal.textContent = data.interfaces.total;
      dom.kpiItfDown.textContent = `${data.interfaces.down} oper down`;
      dom.kpiItfErrors.textContent = `${data.interfaces.errors} errors/discards`;

      dom.kpiSubCount.textContent = data.telemetry.subscriptions;
      dom.kpiCacheCount.textContent = `${data.telemetry.cached_tables} cached tables`;
      dom.kpiResyncInt.textContent = `${data.telemetry.resync_interval}s resync`;

      setLive("live", "live");
      dom.rowCount.textContent = "Executive Dashboard";
      dom.streamInfo.textContent = "KPI overview metrics";
      dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
    } catch (_err) {
      setLive("error", "error");
    }
  }

  function selectReport(report) {
    if (state.report) saveReportPreferences();
    state.report = report;
    state.columns = [];
    state.rows = [];
    state.errors = [];
    state.sort = { column: null, dir: 1 };
    state.previous.clear();
    state.identityColumn = null;
    state.firstPaint = true;
    state.windowSize = WINDOW_STEP;
    location.hash = report.name;
    dom.title.textContent = report.title;
    dom.desc.textContent = report.description;
    dom.body.replaceChildren();
    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();

    if (overviewTimer) {
      clearInterval(overviewTimer);
      overviewTimer = null;
    }

    loadReportPreferences();
    updateFilterUI();
    renderReportList();

    if (report.name === "overview") {
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = true;
      dom.viewModeBtn.hidden = true;
      dom.columnsBtn.hidden = true;
      dom.exportBtn.hidden = true;
      dom.overviewDashboard.hidden = false;
      if (state.source) {
        state.source.close();
        state.source = null;
      }
      loadOverview();
      overviewTimer = setInterval(loadOverview, 5000);
    } else {
      dom.overviewDashboard.hidden = true;
      dom.columnsBtn.hidden = false;
      dom.exportBtn.hidden = false;
      if (["bridge_domains", "services", "routers"].includes(report.name)) {
        state.viewMode = localStorage.getItem(`fcli-viewmode-${report.name}`) || "tree";
        dom.viewModeBtn.hidden = false;
        dom.viewModeBtn.textContent = state.viewMode === "tree" ? "📊 Table View" : "🌲 Services View";
      } else {
        dom.viewModeBtn.hidden = true;
      }
      dom.rowCount.textContent = "loading...";
      connect();
    }
  }

  /* ------------------------------------------------------------- stream */

  function setLive(kind, label) {
    dom.liveDot.className = "dot " + kind;
    dom.liveLabel.textContent = label;
  }

  function connect() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    if (!state.report || state.report.name === "overview" || state.paused) return;
    const params = new URLSearchParams({ refresh: dom.refresh.value });
    const inv = dom.invFilter.value.trim();
    if (inv) params.set("inv_filter", inv);
    const source = new EventSource(
      `/api/stream/${encodeURIComponent(state.report.name)}?${params}`
    );
    state.source = source;
    setLive("live", "connecting");
    source.addEventListener("table", (event) => {
      setLive("live", "live");
      ingest(JSON.parse(event.data));
    });
    source.addEventListener("error", (event) => {
      if (event.data) {
        try {
          showErrors([{ node: "server", error: JSON.parse(event.data).error }]);
        } catch (_err) {
          /* non-JSON error payload */
        }
      }
      setLive("error", "reconnecting");
    });
  }

  function ingest(table) {
    const columnsChanged = table.columns.join(" ") !== state.columns.join(" ");
    state.columns = table.columns;
    state.rows = table.rows;
    state.errors = table.errors || [];
    if (columnsChanged) {
      state.identityColumn = null;
      renderHead();
      renderColumnsMenu();
    }
    showErrors(state.errors);
    dom.streamInfo.textContent = `${table.nodes} node(s), rendered in ${table.render_ms} ms`;
    dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
    renderBody();
  }

  function showErrors(errors) {
    if (!errors || !errors.length) {
      dom.errors.hidden = true;
      dom.errors.replaceChildren();
      return;
    }
    dom.errors.hidden = false;
    dom.errors.replaceChildren(
      ...errors.map((entry) => {
        const line = document.createElement("div");
        line.textContent = `${entry.node}: ${entry.error}`;
        return line;
      })
    );
  }

  /* -------------------------------------------------------------- table */

  function visibleColumns() {
    return state.columns.filter((c) => !state.hidden.has(c));
  }

  function renderHead() {
    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();
    for (const column of visibleColumns()) {
      const th = document.createElement("th");
      th.textContent = column;
      if (state.colFilters.has(column)) {
        th.classList.add("filtered");
      }
      if (state.sort.column === column) {
        const arrow = document.createElement("span");
        arrow.className = "sort-arrow";
        arrow.textContent = state.sort.dir === 1 ? "▲" : "▼";
        th.append(arrow);
      }
      th.addEventListener("click", () => {
        if (state.sort.column === column) {
          state.sort.dir = -state.sort.dir;
        } else {
          state.sort = { column, dir: 1 };
        }
        state.windowSize = WINDOW_STEP;
        renderHead();
        renderBody();
      });
      dom.headRow.append(th);

      const filterCell = document.createElement("th");
      const input = document.createElement("input");
      input.type = "search";
      input.placeholder = "filter";
      input.value = state.colFilters.get(column) || "";
      input.addEventListener(
        "input",
        debounce(() => {
          const value = input.value.trim();
          if (value) state.colFilters.set(column, value);
          else state.colFilters.delete(column);
          state.windowSize = WINDOW_STEP;
          saveReportPreferences();
          updateFilterUI();
          renderHead();
          renderBody();
        }, 150)
      );
      filterCell.append(input);
      dom.filterRow.append(filterCell);
    }
  }

  function renderColumnsMenu() {
    dom.columnsMenu.replaceChildren();
    for (const column of state.columns) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !state.hidden.has(column);
      box.addEventListener("change", () => {
        if (box.checked) state.hidden.delete(column);
        else state.hidden.add(column);
        saveReportPreferences();
        renderHead();
        renderBody();
      });
      label.append(box, document.createTextNode(column));
      dom.columnsMenu.append(label);
    }
  }

  /** Rows left after the global search and the per-column filters. */
  function filteredRows() {
    const global = matcher(dom.globalSearch.value.trim());
    const columnMatchers = [...state.colFilters.entries()].map(([column, pattern]) => [
      column,
      matcher(pattern),
    ]);
    return state.rows.filter((row) => {
      for (const [column, test] of columnMatchers) {
        if (test && !test(String(row[column] ?? ""))) return false;
      }
      if (global) {
        const joined = visibleColumns()
          .map((c) => String(row[c] ?? ""))
          .join(" ");
        if (!global(joined)) return false;
      }
      return true;
    });
  }

  /**
   * Pick the column that identifies a row, so an update can be matched against
   * the previous render and only the cells that really changed are flashed.
   */
  function identityColumn(rows) {
    if (state.identityColumn !== null) return state.identityColumn;
    for (const column of state.columns.filter((c) => c !== "Node")) {
      const seen = new Set();
      let unique = true;
      for (const row of rows) {
        const key = `${row.Node} ${row[column]}`;
        if (seen.has(key)) {
          unique = false;
          break;
        }
        seen.add(key);
      }
      if (unique) {
        state.identityColumn = column;
        return column;
      }
    }
    state.identityColumn = "";
    return "";
  }

  const rowKey = (row, column) =>
    column ? `${row.Node} ${row[column]}` : state.columns.map((c) => row[c]).join(" ");

  function jumpToVrf(targetType, vrfName, nodeName) {
    state.pendingJump = { targetType, vrfName, nodeName };
    const currentReportName = state.report ? state.report.name : "";
    const needSwitch = currentReportName !== "services" && currentReportName !== targetType;

    if (needSwitch) {
      const targetReport = state.reports.find((r) => r.name === targetType);
      if (targetReport) {
        selectReport(targetReport);
      }
    } else {
      executePendingJump();
    }
  }

  function executePendingJump() {
    if (!state.pendingJump) return;
    const { targetType, vrfName, nodeName } = state.pendingJump;

    let targetEl = null;
    if (targetType === "routers" || targetType === "ip-vrf") {
      if (nodeName) {
        targetEl = dom.servicesTreeView.querySelector(
          `[data-node="${CSS.escape(nodeName)}"] [data-ip-vrf="${CSS.escape(vrfName)}"]`
        );
      }
      if (!targetEl) {
        targetEl =
          dom.servicesTreeView.querySelector(`[data-ip-vrf="${CSS.escape(vrfName)}"]`) ||
          dom.servicesTreeView.querySelector(`[data-vrf-name="${CSS.escape(vrfName)}"]`);
      }
    } else {
      if (nodeName) {
        targetEl = dom.servicesTreeView.querySelector(
          `[data-node="${CSS.escape(nodeName)}"] [data-mac-vrf="${CSS.escape(vrfName)}"]`
        );
      }
      if (!targetEl) {
        targetEl =
          dom.servicesTreeView.querySelector(`[data-mac-vrf="${CSS.escape(vrfName)}"]`) ||
          dom.servicesTreeView.querySelector(`[data-vrf-name="${CSS.escape(vrfName)}"]`);
      }
    }

    if (targetEl) {
      targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
      targetEl.classList.remove("highlight-pulse");
      void targetEl.offsetWidth; // trigger reflow
      targetEl.classList.add("highlight-pulse");
      setTimeout(() => targetEl.classList.remove("highlight-pulse"), 3000);
      state.pendingJump = null;
    }
  }

  function renderBridgeDomainsCards(rows) {
    const bdMap = new Map();
    for (const row of rows) {
      const bdName = row["Bridge Domain"] || row["Route Targets"] || "unassigned";
      if (!bdMap.has(bdName)) bdMap.set(bdName, []);
      bdMap.get(bdName).push(row);
    }

    for (const [bdName, bdRows] of bdMap) {
      const card = document.createElement("div");
      card.className = "bd-card";
      const macVrfFirstName = bdRows.map((r) => r["MAC-VRF"]).find(Boolean) || "";
      if (macVrfFirstName) card.dataset.vrfName = macVrfFirstName;

      // Union of MAC-VRF names in this Bridge Domain
      const vrfNames = [...new Set(bdRows.map((r) => r["MAC-VRF"]).filter(Boolean))].sort();
      const hasVrfMismatch = vrfNames.length > 1;

      // Union of Subnets across child nodes in this Bridge Domain
      const allSubnets = [];
      for (const r of bdRows) {
        if (r["Subnets"]) {
          r["Subnets"].split(",").map((s) => s.trim()).forEach((s) => {
            if (s && !allSubnets.includes(s)) allSubnets.push(s);
          });
        }
      }
      const subnetsStr = allSubnets.length ? ` (${allSubnets.join(", ")})` : "";

      const header = document.createElement("div");
      header.className = "bd-header";

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🌉";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Bridge-domain: ${vrfTitleStr}${subnetsStr}`;

      const nodesCount = new Set(bdRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(icon, title, badge);
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      rtLabel.textContent = `Route-target: ${bdName}`;
      subRow.append(rtLabel);

      if (hasVrfMismatch) {
        const mismatchBadge = document.createElement("span");
        mismatchBadge.className = "bd-mismatch-badge";
        mismatchBadge.textContent = `⚠️ Mismatched VRF Names (${vrfNames.join(" vs ")})`;
        subRow.append(mismatchBadge);
      }

      header.append(subRow);
      card.append(header);

      const body = document.createElement("div");
      body.className = "bd-body";

      const nodeMap = new Map();
      for (const row of bdRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.textContent = `🖥️ Node: ${nodeName}`;
        nodeDiv.append(nodeTitle);

        for (const row of nodeRows) {
          const vrfDiv = document.createElement("div");
          vrfDiv.className = "bd-vrf";
          if (row["MAC-VRF"]) vrfDiv.dataset.macVrf = row["MAC-VRF"];

          const vrfHeader = document.createElement("div");
          vrfHeader.className = "bd-vrf-header";

          const vrfName = document.createElement("span");
          const subnetsStr = row["Subnets"] ? ` (${row["Subnets"]})` : "";
          vrfName.textContent = `📦 MAC-VRF: ${row["MAC-VRF"] || "-"}${subnetsStr}`;

          const stateSpan = document.createElement("span");
          const stateVal = (row["Oper State"] || "unknown").toLowerCase();
          stateSpan.className = stateVal === "up" ? "state-badge-up" : "state-badge-down";
          stateSpan.textContent = row["Oper State"] || "unknown";

          vrfHeader.append(vrfName, stateSpan);
          vrfDiv.append(vrfHeader);

          const details = document.createElement("div");
          details.className = "bd-details";

          // 1. IRB interface (max 1, includes associated ip-vrf name if present)
          const irbStr = row["IRB Interface"] || "-";
          if (irbStr !== "-") {
            const irbRowDiv = document.createElement("div");
            irbRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "IRB interface:";
            irbRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            irbStr.split(",").forEach((s) => {
              const p = document.createElement("span");
              p.className = "pill pill-irb";

              if (s.includes("->")) {
                const parts = s.split("->");
                const preText = parts[0] + "-> ";
                const vrfsText = parts.slice(1).join("->").trim();

                p.append(document.createTextNode(preText));

                const vrfs = vrfsText.split(",").map((v) => v.trim()).filter(Boolean);
                vrfs.forEach((vrf, idx) => {
                  if (idx > 0) p.append(document.createTextNode(", "));
                  const link = document.createElement("a");
                  link.className = "vrf-link";
                  link.textContent = vrf;
                  link.href = "#";
                  link.title = `Jump to Router ${vrf}`;
                  link.addEventListener("click", (e) => {
                    e.preventDefault();
                    jumpToVrf("routers", vrf, nodeName);
                  });
                  p.append(link);
                });
              } else {
                p.textContent = s.trim();
              }
              pillGroup.append(p);
            });
            irbRowDiv.append(pillGroup);
            details.append(irbRowDiv);
          }

          // 2. bridge sub-interfaces
          const subitfStr = row["Sub-Interfaces"] || "-";
          if (subitfStr !== "-") {
            const subRowDiv = document.createElement("div");
            subRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "bridge sub-interfaces:";
            subRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            subitfStr.split(",").forEach((s) => {
              const p = document.createElement("span");
              p.className = "pill";
              p.textContent = s.trim();
              pillGroup.append(p);
            });
            subRowDiv.append(pillGroup);
            details.append(subRowDiv);
          }

          // 3. vxlan-interface
          const vxlanStr = row["VXLAN Interface"] || "-";
          if (vxlanStr !== "-") {
            const vxRowDiv = document.createElement("div");
            vxRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "vxlan-interface:";
            vxRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            vxlanStr.split(",").forEach((v) => {
              const p = document.createElement("span");
              p.className = "pill pill-vxlan";
              p.textContent = v.trim();
              pillGroup.append(p);
            });
            vxRowDiv.append(pillGroup);
            details.append(vxRowDiv);
          }

          vrfDiv.append(details);
          nodeDiv.append(vrfDiv);
        }
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderRoutersCards(rows) {
    const routerMap = new Map();
    for (const row of rows) {
      const routerName = row["Router"] || row["Route Targets"] || "unassigned";
      if (!routerMap.has(routerName)) routerMap.set(routerName, []);
      routerMap.get(routerName).push(row);
    }

    for (const [routerName, rRows] of routerMap) {
      const card = document.createElement("div");
      card.className = "bd-card";
      const ipVrfFirstName = rRows.map((r) => r["IP-VRF"]).find(Boolean) || "";
      if (ipVrfFirstName) card.dataset.vrfName = ipVrfFirstName;

      // Union of IP-VRF names in this Router
      const vrfNames = [...new Set(rRows.map((r) => r["IP-VRF"]).filter(Boolean))].sort();
      const hasVrfMismatch = vrfNames.length > 1;

      const header = document.createElement("div");
      header.className = "bd-header";

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🔀";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Router: ${vrfTitleStr}`;

      const nodesCount = new Set(rRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(icon, title, badge);
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      rtLabel.textContent = `Route-target: ${routerName}`;
      subRow.append(rtLabel);

      if (hasVrfMismatch) {
        const mismatchBadge = document.createElement("span");
        mismatchBadge.className = "bd-mismatch-badge";
        mismatchBadge.textContent = `⚠️ Mismatched VRF Names (${vrfNames.join(" vs ")})`;
        subRow.append(mismatchBadge);
      }

      header.append(subRow);
      card.append(header);

      const body = document.createElement("div");
      body.className = "bd-body";

      const nodeMap = new Map();
      for (const row of rRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.textContent = `🖥️ Node: ${nodeName}`;
        nodeDiv.append(nodeTitle);

        for (const row of nodeRows) {
          const vrfDiv = document.createElement("div");
          vrfDiv.className = "bd-vrf";
          if (row["IP-VRF"]) vrfDiv.dataset.ipVrf = row["IP-VRF"];

          const vrfHeader = document.createElement("div");
          vrfHeader.className = "bd-vrf-header";

          const vrfName = document.createElement("span");
          vrfName.textContent = `📦 IP-VRF: ${row["IP-VRF"] || "-"}`;

          const stateSpan = document.createElement("span");
          const stateVal = (row["Oper State"] || "unknown").toLowerCase();
          stateSpan.className = stateVal === "up" ? "state-badge-up" : "state-badge-down";
          stateSpan.textContent = row["Oper State"] || "unknown";

          vrfHeader.append(vrfName, stateSpan);
          vrfDiv.append(vrfHeader);

          const details = document.createElement("div");
          details.className = "bd-details";

          // 1. MAC-VRF's
          const macVrfsStr = row["MAC-VRFs"] || "-";
          if (macVrfsStr !== "-") {
            const macRowDiv = document.createElement("div");
            macRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "MAC-VRF's:";
            macRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            const items = macVrfsStr.split(/,\s*(?=[^\s(]+\s*\()/g);
            items.forEach((itemStr) => {
              const p = document.createElement("span");
              p.className = "pill pill-macvrf";

              const match = itemStr.trim().match(/^([^\s(]+)(.*)$/);
              if (match) {
                const macName = match[1];
                const restText = match[2];

                const link = document.createElement("a");
                link.className = "vrf-link";
                link.textContent = macName;
                link.href = "#";
                link.title = `Jump to Bridge Domain ${macName}`;
                link.addEventListener("click", (e) => {
                  e.preventDefault();
                  jumpToVrf("bridge_domains", macName, nodeName);
                });

                p.append(link, document.createTextNode(restText));
              } else {
                p.textContent = itemStr.trim();
              }
              pillGroup.append(p);
            });
            macRowDiv.append(pillGroup);
            details.append(macRowDiv);
          }

          // 2. Routed interfaces
          const routedStr = row["Routed Interfaces"] || "-";
          if (routedStr !== "-") {
            const routedRowDiv = document.createElement("div");
            routedRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "Routed interfaces:";
            routedRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            routedStr.split(",").forEach((s) => {
              const p = document.createElement("span");
              p.className = "pill";
              p.textContent = s.trim();
              pillGroup.append(p);
            });
            routedRowDiv.append(pillGroup);
            details.append(routedRowDiv);
          }

          // 3. VXLAN-interface
          const vxlanStr = row["VXLAN Interface"] || "-";
          if (vxlanStr !== "-") {
            const vxRowDiv = document.createElement("div");
            vxRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "VXLAN-interface:";
            vxRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            vxlanStr.split(",").forEach((v) => {
              const p = document.createElement("span");
              p.className = "pill pill-vxlan";
              p.textContent = v.trim();
              pillGroup.append(p);
            });
            vxRowDiv.append(pillGroup);
            details.append(vxRowDiv);
          }

          vrfDiv.append(details);
          nodeDiv.append(vrfDiv);
        }
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderBridgeDomainsTree(rows) {
    dom.servicesTreeView.replaceChildren();
    if (!rows || !rows.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No Services found.";
      dom.servicesTreeView.append(p);
      return;
    }

    const bdRows = rows.filter((r) => r["Bridge Domain"] || r["MAC-VRF"] || r["Service Type"] === "Bridge Domain");
    const routerRows = rows.filter((r) => r["Router"] || r["IP-VRF"] || r["Service Type"] === "Router");

    if (bdRows.length > 0) {
      if (routerRows.length > 0) {
        const bdHeader = document.createElement("div");
        bdHeader.className = "services-section-header";
        bdHeader.textContent = "🌉 Bridge Domains";
        dom.servicesTreeView.append(bdHeader);
      }
      renderBridgeDomainsCards(bdRows);
    }

    if (routerRows.length > 0) {
      if (bdRows.length > 0) {
        const rtHeader = document.createElement("div");
        rtHeader.className = "services-section-header";
        rtHeader.textContent = "🔀 Routers";
        dom.servicesTreeView.append(rtHeader);
      }
      renderRoutersCards(routerRows);
    }

    executePendingJump();
  }

  function renderBody() {
    const rows = filteredRows();

    if (
      state.report &&
      ["bridge_domains", "services", "routers"].includes(state.report.name) &&
      state.viewMode === "tree"
    ) {
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = false;
      renderBridgeDomainsTree(rows);
      dom.rowCount.textContent = `${rows.length} service entry/entries`;
      return;
    }

    dom.servicesTreeView.hidden = true;
    dom.tableWrap.hidden = false;

    if (state.sort.column) {
      const { column, dir } = state.sort;
      rows.sort((a, b) => dir * compare(a[column], b[column]));
    }
    const identity = identityColumn(state.rows);
    const columns = visibleColumns();
    const shown = rows.slice(0, state.windowSize);
    const fragment = document.createDocumentFragment();

    for (const row of shown) {
      const key = rowKey(row, identity);
      const previous = state.previous.get(key);
      const tr = document.createElement("tr");
      if (!state.firstPaint && previous === undefined) tr.className = "added";
      for (const column of columns) {
        const value = row[column] ?? "";
        const td = document.createElement("td");
        td.textContent = value;
        const stateClass = STATE_CLASSES[String(value).toLowerCase()];
        if (stateClass) td.classList.add(stateClass);
        else if (isNumeric(value) && value !== "") td.classList.add("num");
        if (
          !state.firstPaint &&
          previous !== undefined &&
          previous[column] !== undefined &&
          previous[column] !== value
        ) {
          td.classList.add("changed");
        }
        tr.append(td);
      }
      fragment.append(tr);
    }
    // Remember every row of the payload, not just the ones on screen, so that
    // changing a filter or scrolling does not make old rows look new.
    const next = new Map();
    for (const row of state.rows) next.set(rowKey(row, identity), row);

    dom.body.replaceChildren(fragment);
    state.previous = next;
    state.firstPaint = false;
    dom.empty.hidden = rows.length > 0;
    const suffix = rows.length === state.rows.length ? "" : ` of ${state.rows.length}`;
    const windowed = shown.length < rows.length ? ` (showing ${shown.length})` : "";
    const plural = rows.length === 1 ? "" : "s";
    dom.rowCount.textContent = `${rows.length} row${plural}${suffix}${windowed}`;
  }

  /* ------------------------------------------------------------- export */

  function exportCsv() {
    const columns = visibleColumns();
    const rows = filteredRows();
    const escape = (value) => {
      const text = String(value ?? "");
      return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    };
    const lines = [columns.join(",")];
    for (const row of rows) lines.push(columns.map((c) => escape(row[c])).join(","));
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = (state.report ? state.report.name : "fcli") + ".csv";
    link.click();
    URL.revokeObjectURL(link.href);
  }

  /* ---------------------------------------------------------- inventory */

  async function loadInventory() {
    try {
      const res = await fetch("/api/inventory");
      const data = await res.json();
      dom.nodeList.replaceChildren(
        ...data.hosts.map((host) => {
          const item = document.createElement("li");
          const dot = document.createElement("span");
          dot.className =
            "dot " +
            (!host.connected ? "error" : host.streaming ? "live" : "paused");
          const name = document.createElement("span");
          name.className = "node-name";
          name.textContent = host.name;
          item.title = host.error
            ? `${host.name}: ${host.error}`
            : `${host.name} (${host.hostname}) - ${
                host.streaming ? "streaming" : "connected, not subscribed"
              }`;
          item.append(dot, name);
          return item;
        })
      );
      const up = data.hosts.filter((h) => h.connected).length;
      dom.nodeSummary.textContent = `${up}/${data.hosts.length} up`;
    } catch (_err) {
      dom.nodeSummary.textContent = "unavailable";
    }
  }

  /* ------------------------------------------------------------- wiring */

  dom.reportSearch.addEventListener("input", debounce(renderReportList, 120));
  dom.globalSearch.addEventListener(
    "input",
    debounce(() => {
      state.windowSize = WINDOW_STEP;
      saveReportPreferences();
      updateFilterUI();
      renderBody();
    }, 150)
  );
  dom.invFilter.addEventListener(
    "change",
    () => {
      saveReportPreferences();
      updateFilterUI();
      connect();
    }
  );
  dom.refresh.addEventListener(
    "change",
    () => {
      saveReportPreferences();
      connect();
    }
  );

  dom.clearFiltersBtn.addEventListener("click", clearAllFilters);

  dom.viewModeBtn.addEventListener("click", () => {
    state.viewMode = state.viewMode === "tree" ? "table" : "tree";
    if (state.report) {
      try {
        localStorage.setItem(`fcli-viewmode-${state.report.name}`, state.viewMode);
      } catch (_err) {}
    }
    dom.viewModeBtn.textContent = state.viewMode === "tree" ? "📊 Table View" : "🌲 Services View";
    renderBody();
  });

  dom.pause.addEventListener("click", () => {
    state.paused = !state.paused;
    dom.pause.textContent = state.paused ? "▶ Resume" : "⏸ Pause";
    if (state.paused) {
      if (state.source) state.source.close();
      state.source = null;
      setLive("paused", "paused");
    } else {
      connect();
    }
  });

  dom.columnsBtn.addEventListener("click", () => {
    dom.columnsMenu.hidden = !dom.columnsMenu.hidden;
  });
  document.addEventListener("click", (event) => {
    if (!dom.columnsMenu.hidden && !event.target.closest(".menu")) {
      dom.columnsMenu.hidden = true;
    }
  });

  dom.exportBtn.addEventListener("click", exportCsv);

  dom.tableWrap.addEventListener("scroll", () => {
    const { scrollTop, scrollHeight, clientHeight } = dom.tableWrap;
    if (scrollHeight - scrollTop - clientHeight < 400) {
      const rows = filteredRows();
      if (state.windowSize < rows.length) {
        state.windowSize += WINDOW_STEP;
        renderBody();
      }
    }
  });

  dom.themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("fcli-theme", next);
    } catch (_err) {
      /* storage may be unavailable */
    }
  });

  try {
    const stored = localStorage.getItem("fcli-theme");
    if (stored) document.documentElement.dataset.theme = stored;
  } catch (_err) {
    /* storage may be unavailable */
  }

  loadReports();
  loadInventory();
  setInterval(loadInventory, 10000);
})();
