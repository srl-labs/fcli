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
    refresh: el("refresh"),
    pause: el("pause"),
    columnsBtn: el("columns-btn"),
    columnsMenu: el("columns-menu"),
    exportBtn: el("export"),
    errors: el("errors"),
    tableWrap: el("table-wrap"),
    headRow: el("head-row"),
    filterRow: el("filter-row"),
    body: el("grid-body"),
    empty: el("empty"),
    rowCount: el("row-count"),
    streamInfo: el("stream-info"),
    updated: el("updated"),
    themeToggle: el("theme-toggle"),
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
  };

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

  /* --------------------------------------------------------- report list */

  async function loadReports() {
    const res = await fetch("/api/reports");
    const data = await res.json();
    state.reports = data.reports;
    dom.version.textContent = "v" + data.version;
    renderReportList();
    const wanted = location.hash.replace(/^#/, "");
    const initial = state.reports.find((r) => r.name === wanted) || state.reports[0];
    if (initial) selectReport(initial);
  }

  function renderReportList() {
    const needle = dom.reportSearch.value.trim().toLowerCase();
    const groups = new Map();
    for (const report of state.reports) {
      const haystack = `${report.title} ${report.name} ${report.description}`;
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

  function selectReport(report) {
    state.report = report;
    state.columns = [];
    state.rows = [];
    state.errors = [];
    state.colFilters.clear();
    state.hidden.clear();
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
    dom.rowCount.textContent = "loading...";
    renderReportList();
    connect();
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
    if (!state.report || state.paused) return;
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

  function renderBody() {
    const rows = filteredRows();
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
      renderBody();
    }, 150)
  );
  dom.invFilter.addEventListener("change", connect);
  dom.refresh.addEventListener("change", connect);

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
