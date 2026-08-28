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
    active: "state-down",
    idle: "state-down",
    connect: "state-down",
    opensent: "state-down",
    openconfirm: "state-down",
    inactive: "state-inactive",
  };

  const el = (id) => document.getElementById(id);
  const dom = {
    reportSearch: el("report-search"),
    reportList: el("report-list"),
    nodeList: el("node-list"),
    nodeSummary: el("node-summary"),
    topoBadge: el("topo-badge"),
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
    kpiCardNodes: el("kpi-card-nodes"),
    kpiCardBgp: el("kpi-card-bgp"),
    kpiCardItf: el("kpi-card-itf"),
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
    kpiCardBd: el("kpi-card-bd"),
    kpiBdTotal: el("kpi-bd-total"),
    kpiBdUp: el("kpi-bd-up"),
    kpiBdDegraded: el("kpi-bd-degraded"),
    kpiBdDown: el("kpi-bd-down"),
    kpiBdInstances: el("kpi-bd-instances"),
    kpiCardRouters: el("kpi-card-routers"),
    kpiRoutersTotal: el("kpi-routers-total"),
    kpiRoutersUp: el("kpi-routers-up"),
    kpiRoutersDegraded: el("kpi-routers-degraded"),
    kpiRoutersDown: el("kpi-routers-down"),
    kpiRoutersInstances: el("kpi-routers-instances"),
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
    collapsedCards: new Set(),
    collapsedNodes: new Set(),
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
      } else if (["bridge_domains", "services", "routers"].includes(state.report.name)) {
        // Node identifiers shown next to the name in the tree, not as table columns.
        ["System IPv4", "System IPv6"].forEach((c) => state.hidden.add(c));
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
    if (data.topo_name) {
      if (dom.topoBadge) {
        dom.topoBadge.textContent = "clab: " + data.topo_name;
        dom.topoBadge.title = "Containerlab topology: " + data.topo_name;
        dom.topoBadge.hidden = false;
      }
    } else if (dom.topoBadge) {
      dom.topoBadge.hidden = true;
    }
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

      if (dom.kpiCardNodes) {
        dom.kpiCardNodes.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.nodes.total > 0) {
          if (data.nodes.connected === data.nodes.total && data.nodes.unreachable === 0) {
            dom.kpiCardNodes.classList.add("kpi-ok");
          } else if (data.nodes.connected === 0) {
            dom.kpiCardNodes.classList.add("kpi-err");
          } else {
            dom.kpiCardNodes.classList.add("kpi-warn");
          }
        }
      }

      if (dom.kpiCardBgp) {
        dom.kpiCardBgp.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.bgp.total > 0) {
          if (data.bgp.established === data.bgp.total && data.bgp.down === 0) {
            dom.kpiCardBgp.classList.add("kpi-ok");
          } else if (data.bgp.established === 0) {
            dom.kpiCardBgp.classList.add("kpi-err");
          } else {
            dom.kpiCardBgp.classList.add("kpi-warn");
          }
        }
      }

      if (dom.kpiCardItf) {
        dom.kpiCardItf.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.interfaces.total > 0) {
          if (data.interfaces.down === 0 && data.interfaces.errors === 0) {
            dom.kpiCardItf.classList.add("kpi-ok");
          } else {
            dom.kpiCardItf.classList.add("kpi-warn");
          }
        }
      }

      dom.kpiItfTotal.textContent = data.interfaces.total;
      dom.kpiItfDown.textContent = `${data.interfaces.down} oper down`;
      dom.kpiItfErrors.textContent = `${data.interfaces.errors} errors/discards`;

      if (data.bridge_domains) {
        dom.kpiBdTotal.textContent = data.bridge_domains.total;
        dom.kpiBdUp.textContent = `${data.bridge_domains.up} up`;
        dom.kpiBdDegraded.textContent = data.bridge_domains.degraded > 0 ? ` · ${data.bridge_domains.degraded} degraded` : "";
        dom.kpiBdDown.textContent = data.bridge_domains.down > 0 ? ` · ${data.bridge_domains.down} down` : "";
        dom.kpiBdInstances.textContent = ` (${data.bridge_domains.instances} inst)`;

        if (dom.kpiCardBd) {
          dom.kpiCardBd.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
          if (data.bridge_domains.total > 0) {
            if (data.bridge_domains.down > 0) {
              dom.kpiCardBd.classList.add("kpi-err");
            } else if (data.bridge_domains.degraded > 0) {
              dom.kpiCardBd.classList.add("kpi-warn");
            } else {
              dom.kpiCardBd.classList.add("kpi-ok");
            }
          }
        }
      }

      if (data.routers) {
        dom.kpiRoutersTotal.textContent = data.routers.total;
        dom.kpiRoutersUp.textContent = `${data.routers.up} up`;
        dom.kpiRoutersDegraded.textContent = data.routers.degraded > 0 ? ` · ${data.routers.degraded} degraded` : "";
        dom.kpiRoutersDown.textContent = data.routers.down > 0 ? ` · ${data.routers.down} down` : "";
        dom.kpiRoutersInstances.textContent = ` (${data.routers.instances} inst)`;

        if (dom.kpiCardRouters) {
          dom.kpiCardRouters.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
          if (data.routers.total > 0) {
            if (data.routers.down > 0) {
              dom.kpiCardRouters.classList.add("kpi-err");
            } else if (data.routers.degraded > 0) {
              dom.kpiCardRouters.classList.add("kpi-warn");
            } else {
              dom.kpiCardRouters.classList.add("kpi-ok");
            }
          }
        }
      }

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
    const activeEl = document.activeElement;
    const activeColumn = activeEl && activeEl.dataset ? activeEl.dataset.column : null;
    const selStart = activeEl && typeof activeEl.selectionStart === "number" ? activeEl.selectionStart : null;
    const selEnd = activeEl && typeof activeEl.selectionEnd === "number" ? activeEl.selectionEnd : null;

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
      input.dataset.column = column;
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
          th.classList.toggle("filtered", Boolean(value));
          renderBody();
        }, 150)
      );
      filterCell.append(input);
      dom.filterRow.append(filterCell);
    }

    if (activeColumn) {
      const newInput = dom.filterRow.querySelector(`input[data-column="${CSS.escape(activeColumn)}"]`);
      if (newInput) {
        newInput.focus();
        if (selStart !== null && selEnd !== null) {
          try {
            newInput.setSelectionRange(selStart, selEnd);
          } catch (_err) {}
        }
      }
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
      // Ensure parent card is expanded
      const parentCard = targetEl.closest(".bd-card");
      if (parentCard && parentCard.classList.contains("is-collapsed")) {
        parentCard.classList.remove("is-collapsed");
        const body = parentCard.querySelector(".bd-body");
        if (body) body.hidden = false;
        const header = parentCard.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "true");
        const cardKey = parentCard.dataset.cardKey;
        if (cardKey) state.collapsedCards.delete(cardKey);
      }

      // Ensure parent node is expanded
      const parentNode = targetEl.closest(".bd-node");
      if (parentNode && parentNode.classList.contains("is-collapsed")) {
        parentNode.classList.remove("is-collapsed");
        const content = parentNode.querySelector(".bd-node-content");
        if (content) content.hidden = false;
        const title = parentNode.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "true");
        const nodeKey = parentNode.dataset.nodeKey;
        if (nodeKey) state.collapsedNodes.delete(nodeKey);
      }

      targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
      targetEl.classList.remove("highlight-pulse");
      void targetEl.offsetWidth; // trigger reflow
      targetEl.classList.add("highlight-pulse");
      setTimeout(() => targetEl.classList.remove("highlight-pulse"), 3000);
      state.pendingJump = null;
    }
  }

  const UP_STATES = ["up", "enable", "enabled", "active", "established"];
  const DOWN_STATES = ["down", "disable", "disabled"];

  // Anything that is neither plainly up nor plainly down counts as degraded -
  // notably the "degraded" a service gets when only some of its interfaces are
  // up, which red would put on a par with a service that is entirely gone.
  function stateKind(state) {
    const st = String(state || "").toLowerCase().trim();
    if (!st) return "";
    if (UP_STATES.includes(st)) return "up";
    if (DOWN_STATES.includes(st)) return "down";
    return "degraded";
  }

  // The reports label an interface with its state, and a down one carries the
  // reason with it: "irb0.0 [down: net-inst-down]: 172.16.20.254/24".
  function labelledState(text) {
    const match = /\[([^\]]+)\]/.exec(text || "");
    return match ? stateKind(match[1].split(":")[0]) : "";
  }

  // *markUp* is off for pills that already carry a colour of their own to say
  // what kind of object they are; those only change colour when something is
  // wrong with them.
  function applyPillState(pill, text, { markUp = true } = {}) {
    const kind = labelledState(text);
    if (!kind || (kind === "up" && !markUp)) return;
    pill.classList.add(`pill-${kind}`);
  }

  function aggregateServiceState(rows) {
    if (!rows || !rows.length) {
      return { status: "unknown", className: "state-badge-down", badgeText: "UNKNOWN" };
    }
    const counts = { up: 0, down: 0, degraded: 0 };
    for (const row of rows) {
      counts[stateKind(row["Oper State"]) || "degraded"] += 1;
    }

    if (counts.up === rows.length) {
      return { status: "up", className: "state-badge-up", badgeText: "UP" };
    }
    if (counts.down === rows.length) {
      return { status: "down", className: "state-badge-down", badgeText: "DOWN" };
    }
    return { status: "degraded", className: "state-badge-warn", badgeText: "DEGRADED" };
  }

  function nodeNameWithIps(nodeName, nodeRows) {
    const wrap = document.createElement("span");
    wrap.className = "bd-node-name";

    const name = document.createElement("span");
    name.textContent = `🖥️ Node: ${nodeName}`;
    wrap.append(name);

    const row = (nodeRows && nodeRows[0]) || {};
    const ips = [row["System IPv4"], row["System IPv6"]]
      .map((v) => (typeof v === "string" ? v.trim() : ""))
      .filter(Boolean);
    if (ips.length) {
      const ipSpan = document.createElement("span");
      ipSpan.className = "bd-node-ips";
      ipSpan.textContent = ips.join("  ·  ");
      wrap.append(ipSpan);
    }
    return wrap;
  }

  // A network-instance covered by another report, as a button that jumps to it -
  // the association is worth a control of its own rather than a link buried in
  // the text of a pill.
  function makeJumpButton(report, target, node, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pill pill-jump";
    button.textContent = target;
    button.title = title;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      jumpToVrf(report, target, node);
    });
    return button;
  }

  function renderBridgeDomainsCards(rows) {
    const bdMap = new Map();
    for (const row of rows) {
      const bdName = row["Bridge Domain"] || row["Route Targets"] || "unassigned";
      if (!bdMap.has(bdName)) bdMap.set(bdName, []);
      bdMap.get(bdName).push(row);
    }

    for (const [bdName, bdRows] of bdMap) {
      const cardKey = `bd:${bdName}`;
      const isCardCollapsed = state.collapsedCards.has(cardKey);
      const cardAgg = aggregateServiceState(bdRows);

      const card = document.createElement("div");
      card.className = `bd-card bd-state-${cardAgg.status}`;
      card.dataset.cardKey = cardKey;
      if (isCardCollapsed) card.classList.add("is-collapsed");

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
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-expanded", isCardCollapsed ? "false" : "true");

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const chevron = document.createElement("span");
      chevron.className = "bd-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "▼";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🌉";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Bridge-domain: ${vrfTitleStr}${subnetsStr}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(bdRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title, stateBadge, badge);
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
      if (isCardCollapsed) body.hidden = true;

      const toggleCard = () => {
        const collapsed = card.classList.toggle("is-collapsed");
        body.hidden = collapsed;
        header.setAttribute("aria-expanded", collapsed ? "false" : "true");
        if (collapsed) {
          state.collapsedCards.add(cardKey);
        } else {
          state.collapsedCards.delete(cardKey);
        }
      };

      header.addEventListener("click", (e) => {
        if (e.target.closest("a, button")) return;
        toggleCard();
      });

      header.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest("a, button")) return;
          e.preventDefault();
          toggleCard();
        }
      });

      const nodeMap = new Map();
      for (const row of bdRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeKey = `${cardKey}:node:${nodeName}`;
        const isNodeCollapsed = state.collapsedNodes.has(nodeKey);

        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;
        nodeDiv.dataset.nodeKey = nodeKey;
        if (isNodeCollapsed) nodeDiv.classList.add("is-collapsed");

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.setAttribute("role", "button");
        nodeTitle.setAttribute("tabindex", "0");
        nodeTitle.setAttribute("aria-expanded", isNodeCollapsed ? "false" : "true");

        const nodeChevron = document.createElement("span");
        nodeChevron.className = "bd-node-chevron";
        nodeChevron.setAttribute("aria-hidden", "true");
        nodeChevron.textContent = "▼";

        const nodeText = nodeNameWithIps(nodeName, nodeRows);

        const nodeAgg = aggregateServiceState(nodeRows);

        const nodeStateBadge = document.createElement("span");
        nodeStateBadge.className = `bd-node-state ${nodeAgg.className}`;
        nodeStateBadge.textContent = nodeAgg.badgeText;

        const nodeBadge = document.createElement("span");
        nodeBadge.className = "bd-node-count";
        nodeBadge.textContent = `${nodeRows.length} item${nodeRows.length === 1 ? "" : "s"}`;

        nodeTitle.append(nodeChevron, nodeText, nodeStateBadge, nodeBadge);
        nodeDiv.append(nodeTitle);

        const nodeContent = document.createElement("div");
        nodeContent.className = "bd-node-content";
        if (isNodeCollapsed) nodeContent.hidden = true;

        const toggleNode = () => {
          const collapsed = nodeDiv.classList.toggle("is-collapsed");
          nodeContent.hidden = collapsed;
          nodeTitle.setAttribute("aria-expanded", collapsed ? "false" : "true");
          if (collapsed) {
            state.collapsedNodes.add(nodeKey);
          } else {
            state.collapsedNodes.delete(nodeKey);
          }
        };

        nodeTitle.addEventListener("click", (e) => {
          if (e.target.closest("a, button")) return;
          toggleNode();
        });

        nodeTitle.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            if (e.target.closest("a, button")) return;
            e.preventDefault();
            toggleNode();
          }
        });

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
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;

          vrfHeader.append(vrfName, stateSpan);
          vrfDiv.append(vrfHeader);

          const details = document.createElement("div");
          details.className = "bd-details";

          // 1. IRB interface (max 1; its ip-vrf, if any, gets its own button)
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
            // Split only where a new IRB starts, so the commas inside an address
            // list or a list of VRFs stay where they belong.
            irbStr.split(/,\s*(?=irb)/i).forEach((entry) => {
              const [itfText, ...vrfText] = entry.split("->");

              const p = document.createElement("span");
              p.className = "pill";
              applyPillState(p, itfText);
              p.textContent = itfText.trim();
              pillGroup.append(p);

              const vrfs = vrfText
                .join("->")
                .split(",")
                .map((v) => v.trim())
                .filter(Boolean);
              vrfs.forEach((vrf) => {
                const arrow = document.createElement("span");
                arrow.className = "pill-arrow";
                arrow.textContent = "→";
                pillGroup.append(
                  arrow,
                  makeJumpButton("routers", vrf, nodeName, `Jump to Router ${vrf}`),
                );
              });
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
              applyPillState(p, s);
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
          nodeContent.append(vrfDiv);
        }
        nodeDiv.append(nodeContent);
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderRoutersCards(rows) {
    const routerMap = new Map();
    for (const row of rows) {
      if (row["IP-VRF"] === "mgmt") continue;
      const routerName = row["Router"] || row["Route Targets"] || "unassigned";
      if (!routerMap.has(routerName)) routerMap.set(routerName, []);
      routerMap.get(routerName).push(row);
    }

    for (const [routerName, rRows] of routerMap) {
      const cardKey = `router:${routerName}`;
      const isCardCollapsed = state.collapsedCards.has(cardKey);
      const cardAgg = aggregateServiceState(rRows);

      const card = document.createElement("div");
      card.className = `bd-card bd-state-${cardAgg.status}`;
      card.dataset.cardKey = cardKey;
      if (isCardCollapsed) card.classList.add("is-collapsed");

      const ipVrfFirstName = rRows.map((r) => r["IP-VRF"]).find(Boolean) || "";
      if (ipVrfFirstName) card.dataset.vrfName = ipVrfFirstName;

      // Union of IP-VRF names in this Router
      const vrfNames = [...new Set(rRows.map((r) => r["IP-VRF"]).filter(Boolean))].sort();
      const hasVrfMismatch = vrfNames.length > 1;

      const header = document.createElement("div");
      header.className = "bd-header";
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-expanded", isCardCollapsed ? "false" : "true");

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const chevron = document.createElement("span");
      chevron.className = "bd-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "▼";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🔀";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Router: ${vrfTitleStr}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(rRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title, stateBadge, badge);
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      const isIsolated = !routerName || routerName === "none (isolated)" || routerName.startsWith("none (isolated)") || routerName.startsWith("ip-vrf:") || routerName === "unassigned";
      rtLabel.textContent = isIsolated ? "Route-target: none (isolated)" : `Route-target: ${routerName}`;
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
      if (isCardCollapsed) body.hidden = true;

      const toggleCard = () => {
        const collapsed = card.classList.toggle("is-collapsed");
        body.hidden = collapsed;
        header.setAttribute("aria-expanded", collapsed ? "false" : "true");
        if (collapsed) {
          state.collapsedCards.add(cardKey);
        } else {
          state.collapsedCards.delete(cardKey);
        }
      };

      header.addEventListener("click", (e) => {
        if (e.target.closest("a, button")) return;
        toggleCard();
      });

      header.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest("a, button")) return;
          e.preventDefault();
          toggleCard();
        }
      });

      const nodeMap = new Map();
      for (const row of rRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeKey = `${cardKey}:node:${nodeName}`;
        const isNodeCollapsed = state.collapsedNodes.has(nodeKey);

        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;
        nodeDiv.dataset.nodeKey = nodeKey;
        if (isNodeCollapsed) nodeDiv.classList.add("is-collapsed");

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.setAttribute("role", "button");
        nodeTitle.setAttribute("tabindex", "0");
        nodeTitle.setAttribute("aria-expanded", isNodeCollapsed ? "false" : "true");

        const nodeChevron = document.createElement("span");
        nodeChevron.className = "bd-node-chevron";
        nodeChevron.setAttribute("aria-hidden", "true");
        nodeChevron.textContent = "▼";

        const nodeText = nodeNameWithIps(nodeName, nodeRows);

        const nodeAgg = aggregateServiceState(nodeRows);

        const nodeStateBadge = document.createElement("span");
        nodeStateBadge.className = `bd-node-state ${nodeAgg.className}`;
        nodeStateBadge.textContent = nodeAgg.badgeText;

        const nodeBadge = document.createElement("span");
        nodeBadge.className = "bd-node-count";
        nodeBadge.textContent = `${nodeRows.length} item${nodeRows.length === 1 ? "" : "s"}`;

        nodeTitle.append(nodeChevron, nodeText, nodeStateBadge, nodeBadge);
        nodeDiv.append(nodeTitle);

        const nodeContent = document.createElement("div");
        nodeContent.className = "bd-node-content";
        if (isNodeCollapsed) nodeContent.hidden = true;

        const toggleNode = () => {
          const collapsed = nodeDiv.classList.toggle("is-collapsed");
          nodeContent.hidden = collapsed;
          nodeTitle.setAttribute("aria-expanded", collapsed ? "false" : "true");
          if (collapsed) {
            state.collapsedNodes.add(nodeKey);
          } else {
            state.collapsedNodes.delete(nodeKey);
          }
        };

        nodeTitle.addEventListener("click", (e) => {
          if (e.target.closest("a, button")) return;
          toggleNode();
        });

        nodeTitle.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            if (e.target.closest("a, button")) return;
            e.preventDefault();
            toggleNode();
          }
        });

        for (const row of nodeRows) {
          const vrfDiv = document.createElement("div");
          vrfDiv.className = "bd-vrf";
          if (row["IP-VRF"]) vrfDiv.dataset.ipVrf = row["IP-VRF"];

          const vrfHeader = document.createElement("div");
          vrfHeader.className = "bd-vrf-header";

          const vrfName = document.createElement("span");
          vrfName.textContent = `📦 IP-VRF: ${row["IP-VRF"] || "-"}`;

          const stateSpan = document.createElement("span");
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;

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
              // Keeps its own colour while healthy - it says "mac-vrf", and the
              // name in it is the link across to the Bridge Domains view.
              applyPillState(p, itemStr, { markUp: false });

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
              applyPillState(p, s);
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
          nodeContent.append(vrfDiv);
        }
        nodeDiv.append(nodeContent);
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderTreeControls() {
    const controls = document.createElement("div");
    controls.className = "tree-controls";

    const expandBtn = document.createElement("button");
    expandBtn.className = "btn btn-sm";
    expandBtn.type = "button";
    expandBtn.textContent = "📂 Expand All";
    expandBtn.addEventListener("click", () => {
      state.collapsedCards.clear();
      state.collapsedNodes.clear();
      dom.servicesTreeView.querySelectorAll(".bd-card.is-collapsed").forEach((card) => {
        card.classList.remove("is-collapsed");
        const body = card.querySelector(".bd-body");
        if (body) body.hidden = false;
        const header = card.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "true");
      });
      dom.servicesTreeView.querySelectorAll(".bd-node.is-collapsed").forEach((node) => {
        node.classList.remove("is-collapsed");
        const content = node.querySelector(".bd-node-content");
        if (content) content.hidden = false;
        const title = node.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "true");
      });
    });

    const collapseBtn = document.createElement("button");
    collapseBtn.className = "btn btn-sm";
    collapseBtn.type = "button";
    collapseBtn.textContent = "📁 Collapse All";
    collapseBtn.addEventListener("click", () => {
      dom.servicesTreeView.querySelectorAll(".bd-card").forEach((card) => {
        const cardKey = card.dataset.cardKey;
        if (cardKey) state.collapsedCards.add(cardKey);
        card.classList.add("is-collapsed");
        const body = card.querySelector(".bd-body");
        if (body) body.hidden = true;
        const header = card.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "false");
      });
      dom.servicesTreeView.querySelectorAll(".bd-node").forEach((node) => {
        const nodeKey = node.dataset.nodeKey;
        if (nodeKey) state.collapsedNodes.add(nodeKey);
        node.classList.add("is-collapsed");
        const content = node.querySelector(".bd-node-content");
        if (content) content.hidden = true;
        const title = node.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "false");
      });
    });

    controls.append(expandBtn, collapseBtn);
    return controls;
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

    dom.servicesTreeView.append(renderTreeControls());

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
        let stateClass = STATE_CLASSES[String(value).toLowerCase()];
        if (state.report && state.report.name === "bgp_peers" && column.toLowerCase().includes("state")) {
          if (String(value).toLowerCase() === "established") {
            stateClass = "state-established";
          } else {
            stateClass = "state-down";
          }
        }
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

  if (dom.kpiCardBd) {
    dom.kpiCardBd.addEventListener("click", () => {
      const report = state.reports.find((r) => r.name === "bridge_domains");
      if (report) selectReport(report);
    });
  }

  if (dom.kpiCardRouters) {
    dom.kpiCardRouters.addEventListener("click", () => {
      const report = state.reports.find((r) => r.name === "routers");
      if (report) selectReport(report);
    });
  }

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
