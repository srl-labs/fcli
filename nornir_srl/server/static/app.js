/* fcli server UI: live SR Linux report tables fed by a server-sent event stream. */
(() => {
  "use strict";

  const WINDOW_STEP = 250; // rows appended per scroll batch

  // An interface an ethernet-segment holds down on purpose. It reports both
  // halves of the truth - the port is down, and standby is why - so it is
  // neither counted as a fault nor read as forwarding.
  const STANDBY_STATE = "down/standby";

  const STATE_CLASSES = {
    up: "state-up",
    down: "state-down",
    [STANDBY_STATE]: "state-standby",
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
    sideSplit: el("side-split"),
    sideSplitter: el("side-splitter"),
    nodesBlock: el("nodes-block"),
    nodeList: el("node-list"),
    nodeSummary: el("node-summary"),
    topoBadge: el("topo-badge"),
    version: el("version"),
    navBack: el("nav-back"),
    navForward: el("nav-forward"),
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
    topologyView: el("topology-view"),
    topoCanvas: el("topo-canvas"),
    topoLegend: el("topo-legend"),
    topoStats: el("topo-stats"),
    topoTabs: el("topo-tabs"),
    topoDetail: el("topo-detail"),
    topoPortLabels: el("topo-port-labels"),
    topoMaxBw: el("topo-max-bw"),
    topoMaxBwUnit: el("topo-max-bw-unit"),
    topoHeatScale: el("topo-heat-scale"),
    topoZoomIn: el("topo-zoom-in"),
    topoZoomOut: el("topo-zoom-out"),
    topoZoomLevel: el("topo-zoom-level"),
    topoZoomFit: el("topo-zoom-fit"),
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
    chatOpen: el("chat-open"),
    chatClose: el("chat-close"),
    chatDrawer: el("chat-drawer"),
    chatLog: el("chat-log"),
    chatForm: el("chat-form"),
    chatInput: el("chat-input"),
    chatSend: el("chat-send"),
    chatProvider: el("chat-provider"),
    chatEffort: el("chat-effort"),
    chatResizer: el("chat-resizer"),
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
    topology: null,
    topoSelection: null,
    topoKey: "",
    topoSize: null, // the drawing in its own units, before any zoom
    topoZoom: 1,
    topoFit: true,
    topoFabric: null, // the fabric being drawn, or "all"
    collapsedCards: new Set(),
    collapsedNodes: new Set(),
    navStack: [],
    navIndex: -1,
    chatEnabled: false,
    chatBusy: false,
    chatMessages: [],
    chatAbort: null,
    chatProviders: [],
    chatProvider: null,
    chatEffort: null,
    chatWidth: 360,
  };

  let overviewTimer = null;
  let topologyTimer = null;
  let navSeq = 0;

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

  /** Reports the server computes for a panel of their own, not as a table. */
  const isPanelReport = (name) => name === "overview" || name === "topology";

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
    if (!state.report || isPanelReport(state.report.name)) return;
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
    if (!state.report || isPanelReport(state.report.name)) return;
    state.hidden.clear();
    state.colFilters.clear();
    try {
      const hiddenData = localStorage.getItem(`fcli-hidden-${state.report.name}`);
      if (hiddenData) {
        JSON.parse(hiddenData).forEach((col) => state.hidden.add(col));
      } else if (["bridge_domains", "services", "routers"].includes(state.report.name)) {
        // Node identifiers shown next to the name in the tree, not as table columns.
        ["System IPv4", "System IPv6", "Gateway", "BGP Instance", "Underlay Hosts", "Site"].forEach((c) => state.hidden.add(c));
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
    if (!state.report) return;
    if (state.report.name === "topology") loadTopology();
    else if (state.report.name !== "overview") connect();
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
    if (data.chat && data.chat.enabled && dom.chatOpen) {
      state.chatEnabled = true;
      dom.chatOpen.hidden = false;
      renderChatProviders((data.chat && data.chat.providers) || []);
    } else if (dom.chatOpen) {
      state.chatEnabled = false;
      dom.chatOpen.hidden = true;
      renderChatProviders([]);
      closeChat();
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

  /* ------------------------------------------------------------- topology */

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Create an SVG element with its attributes in one call. */
  function svgEl(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    for (const key in attrs || {}) node.setAttribute(key, attrs[key]);
    return node;
  }

  const ROLE_LABELS = {
    client: "Client",
    segment: "Ethernet segment",
    leaf: "Leaf",
    spine: "Spine",
    dcgw: "DCGW",
    core: "WAN / core",
    unknown: "Unclassified",
    external: "Outside inventory",
  };

  const TOPO = {
    nodeHeight: 46,
    minNodeWidth: 96,
    gapX: 26,
    siteGap: 30,
    rowHeight: 122,
    padLeft: 124, // room for the tier label down the left edge
    padRight: 28,
    padY: 24,
  };

  // Low enough that a fabric wide enough to need panning at any readable size
  // can still be taken in whole.
  const TOPO_ZOOM_MIN = 0.05;
  const TOPO_ZOOM_MAX = 4;
  const TOPO_ZOOM_STEP = 1.25;
  // Fractions of the lab-wide per-link capacity. Emulated nodes share one
  // forwarding budget, so every cable is coloured against the same max.
  const TOPO_BW_TH1 = 0.25;
  const TOPO_BW_TH2 = 0.5;
  const TOPO_BW_TH3 = 0.75;
  const TOPO_MAX_BW_DEFAULT = 10;
  const TOPO_MAX_BW_UNIT_DEFAULT = "1000000";

  const shortPort = (port) => String(port || "").replace(/^ethernet-/, "e");

  async function loadTopology() {
    if (!state.report || state.report.name !== "topology" || state.paused) return;
    try {
      const inv = dom.invFilter.value.trim();
      const params = new URLSearchParams();
      if (inv) params.set("inv_filter", inv);
      const res = await fetch("/api/topology" + (inv ? `?${params}` : ""));
      const graph = await res.json();
      setLive("live", "live");
      dom.streamInfo.textContent = `LLDP topology, rendered in ${graph.render_ms} ms`;
      dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
      // Re-drawing would drop the hover and lose the scroll position. Rates
      // change every poll; the cables themselves do not, so colour in place.
      state.topology = graph;
      const key = topoStructureKey(graph);
      if (key === state.topoKey) {
        recolorTopoLinks(graph);
        if (state.topoSelection && state.topoSelection.kind === "link") {
          renderTopoLinkDetail(state.topoSelection.id);
        }
        return;
      }
      state.topoKey = key;
      renderTopology(graph);
    } catch (_err) {
      setLive("error", "error");
    }
  }

  /** Place every node on the tier its role puts it in, one row per tier. */
  function layoutTopology(graph) {
    const byName = new Map(graph.nodes.map((node) => [node.name, node]));
    const rows = [];
    let widest = 0;
    for (const layer of graph.layers) {
      const nodes = layer.nodes.map((name) => byName.get(name)).filter(Boolean);
      let width = 0;
      let site = null;
      const sized = nodes.map((node) => {
        // Wide enough for whichever of the two lines in the box is the longer.
        const text = Math.max(node.label.length * 8, topoNodeSub(node).length * 6.2);
        const w = Math.max(TOPO.minNodeWidth, text + 28);
        if (width) width += TOPO.gapX;
        if (site !== null && node.site !== site) width += TOPO.siteGap - TOPO.gapX;
        site = node.site;
        const placed = { node, w, offset: width };
        width += w;
        return placed;
      });
      widest = Math.max(widest, width);
      rows.push({ layer, nodes: sized, width });
    }

    const canvasWidth = TOPO.padLeft + widest + TOPO.padRight;
    const positions = new Map();
    rows.forEach((row, index) => {
      const y = TOPO.padY + index * TOPO.rowHeight;
      const start = TOPO.padLeft + (widest - row.width) / 2;
      row.y = y;
      for (const placed of row.nodes) {
        const x = start + placed.offset;
        positions.set(placed.node.name, {
          x,
          y,
          w: placed.w,
          h: TOPO.nodeHeight,
          cx: x + placed.w / 2,
          cy: y + TOPO.nodeHeight / 2,
        });
      }
    });

    return {
      rows,
      positions,
      width: canvasWidth,
      height: TOPO.padY * 2 + rows.length * TOPO.rowHeight,
    };
  }

  function renderTopology(whole) {
    // Everything below draws one fabric; the whole graph stays in state, so a
    // node of another one is still there to be looked up and walked to.
    renderTopoTabs(whole);
    const graph = topoFabricView(whole);
    dom.topoCanvas.replaceChildren();
    renderTopoLegend(graph);
    renderTopoHeatLegend();
    dom.topoStats.textContent = topoSummary(graph);
    dom.rowCount.textContent = `${graph.nodes.length} node(s), ${graph.links.length} link(s)`;
    if (!graph.nodes.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No nodes are streaming LLDP yet.";
      dom.topoCanvas.append(empty);
      state.topoSize = null;
      applyTopoZoom();
      return;
    }

    const layout = layoutTopology(graph);
    state.topoSize = { width: layout.width, height: layout.height };
    const svg = svgEl("svg", {
      class: "topo-svg",
      viewBox: `0 0 ${layout.width} ${layout.height}`,
      role: "img",
      "aria-label": "Fabric topology",
    });

    const bands = svgEl("g", { class: "topo-bands" });
    for (const row of layout.rows) {
      bands.append(
        svgEl("rect", {
          class: "topo-band",
          x: 8,
          y: row.y - 18,
          width: layout.width - 16,
          height: TOPO.nodeHeight + 36,
          rx: 10,
        })
      );
      const label = svgEl("text", {
        class: "topo-band-label",
        x: 20,
        y: row.y + TOPO.nodeHeight / 2 + 4,
      });
      label.textContent = row.layer.label;
      bands.append(label);
    }
    svg.append(bands);

    svg.append(renderTopoLinks(graph, layout));
    svg.append(renderTopoNodes(graph, layout));
    dom.topoCanvas.append(svg);
    applyTopoZoom();

    svg.addEventListener("mouseover", (event) => {
      const target = event.target.closest("[data-node]");
      highlightTopo(target ? target.dataset.node : null);
    });
    svg.addEventListener("mouseleave", () => highlightTopo(null));
    svg.addEventListener("click", (event) => {
      const node = event.target.closest("[data-node]");
      const link = event.target.closest("[data-link]");
      if (node) selectTopo({ kind: "node", id: node.dataset.node });
      else if (link) selectTopo({ kind: "link", id: link.dataset.link });
      else selectTopo(null);
    });

    applyTopoSelection();
  }

  /* ------------------------------------------------------- topology fabrics */

  const topoFabrics = (graph) => (graph && graph.fabrics) || [];

  /** The fabric a tab is on, falling back to the largest one it could be. */
  function currentTopoFabric(graph) {
    const fabrics = topoFabrics(graph);
    if (fabrics.length < 2) return "all";
    if (state.topoFabric === "all") return "all";
    if (fabrics.some((fabric) => fabric.id === state.topoFabric)) return state.topoFabric;
    return fabrics[0].id;
  }

  /** The graph reduced to the fabric on the tab, layers and legend with it. */
  function topoFabricView(graph) {
    const id = currentTopoFabric(graph);
    if (id === "all") return graph;
    const nodes = graph.nodes.filter((node) => (node.fabrics || []).includes(id));
    const names = new Set(nodes.map((node) => node.name));
    return {
      ...graph,
      nodes,
      links: graph.links.filter((link) => names.has(link.a) && names.has(link.b)),
      layers: graph.layers
        .map((layer) => ({ ...layer, nodes: layer.nodes.filter((name) => names.has(name)) }))
        .filter((layer) => layer.nodes.length),
      roles: nodes.reduce((counts, node) => {
        counts[node.role] = (counts[node.role] || 0) + 1;
        return counts;
      }, {}),
      unresolved: graph.unresolved.filter((entry) => names.has(entry.peer)),
    };
  }

  function renderTopoTabs(graph) {
    const fabrics = topoFabrics(graph);
    dom.topoTabs.replaceChildren();
    // One fabric is the whole drawing; a tab strip of one says nothing.
    dom.topoTabs.hidden = fabrics.length < 2;
    if (fabrics.length < 2) return;
    const current = currentTopoFabric(graph);
    state.topoFabric = current;
    const tabs = fabrics.map((fabric) => ({
      id: fabric.id,
      label: fabric.label,
      // The nodes of the fabric rather than the boxes drawn for it: the clients
      // are counted again on every other fabric they are plugged into.
      count: `${fabric.devices} devices`,
      title: topoFabricMembers(graph, fabric.id),
    }));
    tabs.push({
      id: "all",
      label: "All",
      count: `${fabrics.length} fabrics`,
      title: "Every fabric at once, side by side",
    });
    for (const tab of tabs) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-tab" + (tab.id === current ? " is-active" : "");
      button.title = tab.title;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(tab.id === current));
      const label = document.createElement("span");
      label.textContent = tab.label;
      const count = document.createElement("span");
      count.className = "muted";
      count.textContent = tab.count;
      button.append(label, count);
      button.addEventListener("click", () => setTopoFabric(tab.id));
      dom.topoTabs.append(button);
    }
  }

  /** The nodes on a tab, for a tab whose name cannot say which fabric it is. */
  function topoFabricMembers(graph, id) {
    const names = graph.nodes
      .filter((node) => !isTopoAttached(node) && (node.fabrics || []).includes(id))
      .map((node) => node.label);
    const shown = names.slice(0, 6).join(", ");
    return names.length > 6 ? `${shown} and ${names.length - 6} more` : shown;
  }

  function setTopoFabric(id) {
    if (id === state.topoFabric) return;
    state.topoFabric = id;
    try {
      localStorage.setItem("fcli-topo-fabric", id);
    } catch (_err) {
      /* storage may be unavailable */
    }
    dom.topoCanvas.scrollLeft = 0;
    dom.topoCanvas.scrollTop = 0;
    if (state.topology) renderTopology(state.topology);
  }

  /** The fabric holding *name*, when the one on the tab does not. */
  function topoFabricElsewhere(name) {
    if (!state.topology) return null;
    const current = currentTopoFabric(state.topology);
    if (current === "all") return null;
    const node = (state.topology.nodes || []).find((entry) => entry.name === name);
    const fabrics = (node && node.fabrics) || [];
    if (!fabrics.length || fabrics.includes(current)) return null;
    return fabrics[0];
  }

  /* --------------------------------------------------------- topology zoom */

  const clampZoom = (zoom) =>
    Number.isFinite(zoom) ? Math.min(TOPO_ZOOM_MAX, Math.max(TOPO_ZOOM_MIN, zoom)) : 1;

  /** The zoom in force: the fitted one while fit mode is on, else the picked one. */
  const topoZoom = () => (state.topoFit ? topoFitZoom() : state.topoZoom);

  /** The zoom at which the whole drawing fits the canvas, never magnifying it. */
  function topoFitZoom() {
    const size = state.topoSize;
    if (!size || !size.width || !size.height) return 1;
    const style = getComputedStyle(dom.topoCanvas);
    const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const padY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const width = dom.topoCanvas.clientWidth - padX;
    const height = dom.topoCanvas.clientHeight - padY;
    if (width <= 0 || height <= 0) return 1;
    return clampZoom(Math.min(1, width / size.width, height / size.height));
  }

  /** Size the drawing to the current zoom and put the buttons in step with it. */
  function applyTopoZoom() {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    const zoom = topoZoom();
    if (svg && state.topoSize) {
      svg.style.width = `${Math.round(state.topoSize.width * zoom)}px`;
      svg.style.height = `${Math.round(state.topoSize.height * zoom)}px`;
    }
    dom.topoZoomLevel.textContent = `${Math.round(zoom * 100)}%`;
    dom.topoZoomFit.classList.toggle("is-active", state.topoFit);
  }

  /**
   * Zoom to *zoom*, holding still whatever is under *anchor* (a viewport point,
   * the pointer or the middle of the canvas), so the fabric does not slide out
   * from under the part being read.
   */
  function setTopoZoom(zoom, anchor) {
    const before = topoZoom();
    const after = clampZoom(zoom);
    state.topoFit = false;
    state.topoZoom = after;
    if (after === before) {
      applyTopoZoom();
      saveTopoZoom();
      return;
    }
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    const rect = svg ? svg.getBoundingClientRect() : null;
    const point = rect && { x: (anchor.x - rect.left) / before, y: (anchor.y - rect.top) / before };
    applyTopoZoom();
    if (point) {
      dom.topoCanvas.scrollLeft += point.x * (after - before);
      dom.topoCanvas.scrollTop += point.y * (after - before);
    }
    saveTopoZoom();
  }

  function fitTopoZoom() {
    state.topoFit = true;
    dom.topoCanvas.scrollLeft = 0;
    dom.topoCanvas.scrollTop = 0;
    applyTopoZoom();
    saveTopoZoom();
  }

  /** The middle of the canvas, for zooming that did not start at a pointer. */
  function topoCanvasCenter() {
    const rect = dom.topoCanvas.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  function saveTopoZoom() {
    try {
      localStorage.setItem("fcli-topo-zoom", state.topoFit ? "fit" : String(state.topoZoom));
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function restoreTopoZoom() {
    let stored = null;
    try {
      stored = localStorage.getItem("fcli-topo-zoom");
    } catch (_err) {
      /* storage may be unavailable */
    }
    if (!stored || stored === "fit") return;
    const zoom = parseFloat(stored);
    if (!zoom) return;
    state.topoFit = false;
    state.topoZoom = clampZoom(zoom);
  }

  function restoreTopoFabric() {
    try {
      state.topoFabric = localStorage.getItem("fcli-topo-fabric") || null;
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function renderTopoLinks(graph, layout) {
    const group = svgEl("g", { class: "topo-links" });
    for (const link of graph.links) {
      const a = layout.positions.get(link.a);
      const b = layout.positions.get(link.b);
      if (!a || !b) continue;
      const id = `${link.a}\u0000${link.b}`;
      const pair = svgEl("g", {
        class: `topo-link-pair${link.access ? " is-access" : ""}`,
        "data-link": id,
        "data-a": link.a,
        "data-b": link.b,
      });
      const { aIsTop, aPt, bPt } = linkAnchors(a, b);
      let aShape;
      let bShape;
      if (link.intra_layer) {
        // An arc under the tier, so a DCGW mesh or a spine pair does not
        // draw a line straight through the nodes between its ends. Split at
        // the midpoint so each half takes the colour of that end's egress.
        const p0 = { x: a.cx, y: a.y + a.h };
        const p2 = { x: b.cx, y: b.y + b.h };
        const p1 = { x: (a.cx + b.cx) / 2, y: a.y + a.h + 46 };
        const { mid, c1, c2 } = splitQuad(p0, p1, p2);
        aShape = svgEl("path", {
          d: `M ${p0.x} ${p0.y} Q ${c1.x} ${c1.y} ${mid.x} ${mid.y}`,
          fill: "none",
          "data-end": "a",
        });
        bShape = svgEl("path", {
          d: `M ${mid.x} ${mid.y} Q ${c2.x} ${c2.y} ${p2.x} ${p2.y}`,
          fill: "none",
          "data-end": "b",
        });
      } else {
        const mid = midPoint(aPt, bPt);
        aShape = svgEl("line", {
          x1: aPt.x,
          y1: aPt.y,
          x2: mid.x,
          y2: mid.y,
          "data-end": "a",
        });
        bShape = svgEl("line", {
          x1: mid.x,
          y1: mid.y,
          x2: bPt.x,
          y2: bPt.y,
          "data-end": "b",
        });
      }
      aShape.append(svgEl("title"));
      bShape.append(svgEl("title"));
      pair.append(aShape, bShape);
      paintTopoLink(pair, link);

      const [top, bottom] = aIsTop ? [a, b] : [b, a];
      if (link.count > 1) {
        const badge = svgEl("text", {
          class: "topo-link-count",
          x: (a.cx + b.cx) / 2,
          y: (a.cy + b.cy) / 2,
          "text-anchor": "middle",
        });
        badge.textContent = `${link.count}\u00d7`;
        pair.append(badge);
      }
      // Only a single cable can be labelled without the two ends colliding;
      // a bundle shows its size instead, and its ports in the detail panel.
      if (dom.topoPortLabels.checked && !link.intra_layer && link.count === 1) {
        const ports = link.ports[0];
        pair.append(
          portLabel(top, bottom, 0.18, shortPort(aIsTop ? ports.a_port : ports.b_port)),
          portLabel(top, bottom, 0.82, shortPort(aIsTop ? ports.b_port : ports.a_port))
        );
      }
      group.append(pair);
    }
    return group;
  }

  /** Attachment points of a cable: bottom of the upper node, top of the lower. */
  function linkAnchors(a, b) {
    const aIsTop = a.y <= b.y;
    return {
      aIsTop,
      aPt: { x: a.cx, y: aIsTop ? a.y + a.h : a.y },
      bPt: { x: b.cx, y: aIsTop ? b.y : b.y + b.h },
    };
  }

  const midPoint = (p, q) => ({ x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 });

  /** Split a quadratic bezier at t=0.5 so each half can take its own stroke. */
  function splitQuad(p0, p1, p2) {
    const c1 = midPoint(p0, p1);
    const c2 = midPoint(p1, p2);
    return { mid: midPoint(c1, c2), c1, c2 };
  }

  /** The lab-wide per-link capacity, in bits per second. */
  function topoMaxLinkBps() {
    const value = parseFloat(dom.topoMaxBw && dom.topoMaxBw.value);
    const unit = parseFloat(dom.topoMaxBwUnit && dom.topoMaxBwUnit.value);
    if (!(value > 0) || !(unit > 0)) return TOPO_MAX_BW_DEFAULT * Number(TOPO_MAX_BW_UNIT_DEFAULT);
    return value * unit;
  }

  function topoBwClass(bps) {
    if (bps == null || !Number.isFinite(Number(bps))) return "bw-none";
    const max = topoMaxLinkBps();
    if (!(max > 0)) return "bw-none";
    const ratio = Number(bps) / max;
    if (ratio < TOPO_BW_TH1) return "bw-green";
    if (ratio < TOPO_BW_TH2) return "bw-yellow";
    if (ratio < TOPO_BW_TH3) return "bw-orange";
    return "bw-red";
  }

  function topoHalfClass(link, bps) {
    const parts = ["topo-link"];
    // A cable that carries nothing gets no bandwidth colour: a down one has
    // nothing to forward, and a standby one is not forwarding on purpose -
    // which is why standby is coloured apart from down rather than red.
    // The class comes from the kind, because "down/standby" is not a name a
    // CSS class can carry.
    const kind = stateKind(link.state);
    if (kind === "down" || kind === "standby") {
      parts.push(`link-${kind}`);
      return parts.join(" ");
    }
    parts.push(`link-${link.state}`);
    parts.push(topoBwClass(bps));
    return parts.join(" ");
  }

  function paintTopoLink(pair, link) {
    const aHalf = pair.querySelector('[data-end="a"]');
    const bHalf = pair.querySelector('[data-end="b"]');
    if (aHalf) aHalf.setAttribute("class", topoHalfClass(link, link.a_out_bps));
    if (bHalf) bHalf.setAttribute("class", topoHalfClass(link, link.b_out_bps));
    const title = topoLinkTitle(link);
    pair.querySelectorAll("title").forEach((node) => {
      node.textContent = title;
    });
  }

  function recolorTopoLinks(graph) {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    renderTopoHeatLegend();
    const byId = new Map((graph.links || []).map((link) => [`${link.a}\u0000${link.b}`, link]));
    svg.querySelectorAll("[data-link]").forEach((pair) => {
      const link = byId.get(pair.getAttribute("data-link"));
      if (link) paintTopoLink(pair, link);
    });
  }

  /** Identity of the drawing, ignoring rates that only recolour it. */
  function topoStructureKey(graph) {
    const links = (graph.links || []).map((link) => ({
      a: link.a,
      b: link.b,
      count: link.count,
      state: link.state,
      intra_layer: link.intra_layer,
      access: link.access,
      ports: (link.ports || []).map((port) => ({ a_port: port.a_port, b_port: port.b_port })),
    }));
    return JSON.stringify(graph.nodes) + JSON.stringify(links);
  }

  function trimBw(text) {
    return String(text).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function formatBps(bps) {
    if (bps == null || !Number.isFinite(Number(bps))) return "—";
    const n = Number(bps);
    if (n >= 1e9) return `${trimBw((n / 1e9).toFixed(2))} Gbps`;
    if (n >= 1e6) return `${trimBw((n / 1e6).toFixed(2))} Mbps`;
    if (n >= 1e3) return `${trimBw((n / 1e3).toFixed(1))} Kbps`;
    return `${Math.round(n)} bps`;
  }

  function renderTopoHeatLegend() {
    if (!dom.topoHeatScale) return;
    const unit = (dom.topoMaxBwUnit && dom.topoMaxBwUnit.selectedOptions[0]
      ? dom.topoMaxBwUnit.selectedOptions[0].textContent
      : "Mbps");
    const value = trimBw(String(dom.topoMaxBw && dom.topoMaxBw.value ? dom.topoMaxBw.value : TOPO_MAX_BW_DEFAULT));
    dom.topoHeatScale.textContent = `of ${value} ${unit}`;
  }

  function saveTopoMaxBw() {
    try {
      localStorage.setItem("fcli-topo-max-bw", dom.topoMaxBw.value);
      localStorage.setItem("fcli-topo-max-bw-unit", dom.topoMaxBwUnit.value);
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function restoreTopoMaxBw() {
    try {
      const value = localStorage.getItem("fcli-topo-max-bw");
      const unit = localStorage.getItem("fcli-topo-max-bw-unit");
      if (value && dom.topoMaxBw) dom.topoMaxBw.value = value;
      if (unit && dom.topoMaxBwUnit) dom.topoMaxBwUnit.value = unit;
    } catch (_err) {
      /* storage may be unavailable */
    }
    renderTopoHeatLegend();
  }

  function onTopoMaxBwChange() {
    saveTopoMaxBw();
    if (state.topology) recolorTopoLinks(state.topology);
    else renderTopoHeatLegend();
  }

  /** A port name placed a fraction of the way down a link. */
  function portLabel(top, bottom, fraction, text) {
    const label = svgEl("text", {
      class: "topo-port",
      x: top.cx + (bottom.cx - top.cx) * fraction,
      y: top.y + top.h + (bottom.y - top.y - top.h) * fraction,
      "text-anchor": "middle",
    });
    label.textContent = text;
    return label;
  }

  function renderTopoNodes(graph, layout) {
    const group = svgEl("g", { class: "topo-nodes" });
    for (const node of graph.nodes) {
      const box = layout.positions.get(node.name);
      if (!box) continue;
      const cell = svgEl("g", {
        class: `topo-node role-${node.role}${node.connected ? "" : " is-down"}`,
        "data-node": node.name,
        tabindex: "0",
      });
      cell.append(
        svgEl("rect", { x: box.x, y: box.y, width: box.w, height: box.h, rx: 8 })
      );
      const label = svgEl("text", {
        class: "topo-node-label",
        x: box.cx,
        y: box.y + 20,
        "text-anchor": "middle",
      });
      label.textContent = node.label;
      const sub = svgEl("text", {
        class: "topo-node-sub",
        x: box.cx,
        y: box.y + 35,
        "text-anchor": "middle",
      });
      sub.textContent = topoNodeSub(node);
      const title = svgEl("title");
      title.textContent = topoNodeTitle(node);
      cell.append(label, sub, title);
      group.append(cell);
    }
    return group;
  }

  function topoNodeSub(node) {
    if (isTopoAttached(node)) {
      // Being multi-homed is said before what is carried: the leaves under the
      // box are the point of it being a single box.
      const leaves = new Set(node.attachments.map((a) => a.node)).size;
      if (leaves > 1) return `multi-homed, ${leaves} leaves`;
      const services = node.services || [];
      if (services.length === 1) return services[0];
      if (services.length) return `${services.length} services`;
      return `${node.ports} port(s)`;
    }
    if (node.mac_vrfs || node.ip_vrfs) {
      const parts = [];
      if (node.mac_vrfs) parts.push(`${node.mac_vrfs} mac`);
      if (node.ip_vrfs) parts.push(`${node.ip_vrfs} ip`);
      if (node.stitched) parts.push(`${node.stitched} gw`);
      return parts.join(" · ");
    }
    return node.site || ROLE_LABELS[node.role] || node.role;
  }

  /** Whether a node is drawn from its attachments: a client or a segment. */
  function isTopoAttached(node) {
    return node.role === "client" || node.role === "segment";
  }

  function topoNodeTitle(node) {
    const lines = [node.name, ROLE_LABELS[node.role] || node.role];
    if (isTopoAttached(node)) {
      for (const attachment of node.attachments || []) {
        lines.push(attachmentText(attachment));
      }
      return lines.join(" - ");
    }
    if (node.site) lines.push(`site ${node.site}`);
    if (node.mac_vrfs || node.ip_vrfs) {
      lines.push(`${node.mac_vrfs} mac-vrf, ${node.ip_vrfs} ip-vrf`);
    }
    if (node.stitched) lines.push(`${node.stitched} stitched service(s)`);
    if (node.clients) lines.push(`${node.clients} client(s)`);
    if (node.error) lines.push(node.error);
    return lines.join(" - ");
  }

  /** One attachment as a line: where it lands and what it carries. */
  function attachmentText(attachment) {
    const parts = [`${attachment.node} ${shortPort(attachment.subinterface)}`];
    if (attachment.service) parts.push(attachment.service);
    if (attachment.vlan) parts.push(`vlan ${attachment.vlan}`);
    if (attachment.ip) parts.push(attachment.ip);
    return parts.join(" · ");
  }

  function topoLinkTitle(link) {
    const ports = link.ports
      .map((pair) => cableText(pair.a_port, pair.b_port, "↔"))
      .join(", ");
    const parts = [`${link.a} ↔ ${link.b}${ports ? " (" + ports + ")" : ""}`];
    if (link.a_out_bps != null || link.b_out_bps != null) {
      parts.push(`${link.a} out ${formatBps(link.a_out_bps)}`);
      parts.push(`${link.b} out ${formatBps(link.b_out_bps)}`);
    }
    return parts.join(" · ");
  }

  /** Both ends of a cable, or the one end of it a client does not report. */
  function cableText(near, far, arrow) {
    if (!far) return shortPort(near);
    if (!near) return shortPort(far);
    return `${shortPort(near)} ${arrow} ${shortPort(far)}`;
  }

  function topoSummary(graph) {
    const parts = [`${graph.nodes.length} nodes`, `${graph.links.length} links`];
    if (graph.unresolved.length) {
      parts.push(`${graph.unresolved.length} neighbour(s) outside the inventory`);
    }
    return parts.join(" · ");
  }

  function renderTopoLegend(graph) {
    dom.topoLegend.replaceChildren();
    const order = ["client", "segment", "leaf", "spine", "dcgw", "core", "unknown", "external"];
    for (const role of order) {
      const count = graph.roles[role];
      if (!count) continue;
      const chip = document.createElement("span");
      chip.className = `topo-chip role-${role}`;
      const swatch = document.createElement("span");
      swatch.className = "topo-swatch";
      const text = document.createElement("span");
      text.textContent = `${ROLE_LABELS[role]} ${count}`;
      chip.append(swatch, text);
      dom.topoLegend.append(chip);
    }
  }

  /** Dim everything that is not *name* and what it is cabled to. */
  function highlightTopo(name) {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    svg.querySelectorAll(".is-hot").forEach((node) => node.classList.remove("is-hot"));
    if (!name) {
      svg.classList.toggle("is-focused", Boolean(state.topoSelection));
      if (state.topoSelection) applyTopoSelection();
      return;
    }
    svg.classList.add("is-focused");
    markTopoNode(svg, name);
  }

  function markTopoNode(svg, name) {
    const node = svg.querySelector(`[data-node="${CSS.escape(name)}"]`);
    if (node) node.classList.add("is-hot");
    svg
      .querySelectorAll(`[data-a="${CSS.escape(name)}"], [data-b="${CSS.escape(name)}"]`)
      .forEach((link) => {
        link.classList.add("is-hot");
        const peer = link.dataset.a === name ? link.dataset.b : link.dataset.a;
        const box = svg.querySelector(`[data-node="${CSS.escape(peer)}"]`);
        if (box) box.classList.add("is-hot");
      });
  }

  function selectTopo(selection) {
    state.topoSelection = selection;
    // Walking the peer list of a client can lead to a node of another fabric,
    // which is drawn on its own tab; go there rather than nowhere.
    if (selection && selection.kind === "node") {
      const elsewhere = topoFabricElsewhere(selection.id);
      if (elsewhere) {
        setTopoFabric(elsewhere);
        return;
      }
    }
    applyTopoSelection();
  }

  function applyTopoSelection() {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    svg.querySelectorAll(".is-hot").forEach((node) => node.classList.remove("is-hot"));
    const selection = state.topoSelection;
    svg.classList.toggle("is-focused", Boolean(selection));
    if (!selection) {
      dom.topoDetail.hidden = true;
      dom.topoDetail.replaceChildren();
      return;
    }
    if (selection.kind === "node") {
      markTopoNode(svg, selection.id);
      renderTopoNodeDetail(selection.id);
    } else {
      const link = svg.querySelector(`[data-link="${CSS.escape(selection.id)}"]`);
      if (link) {
        link.classList.add("is-hot");
        for (const end of [link.dataset.a, link.dataset.b]) {
          const box = svg.querySelector(`[data-node="${CSS.escape(end)}"]`);
          if (box) box.classList.add("is-hot");
        }
      }
      renderTopoLinkDetail(selection.id);
    }
  }

  function topoDetailShell(title, subtitle) {
    dom.topoDetail.replaceChildren();
    dom.topoDetail.hidden = false;
    const head = document.createElement("header");
    const heading = document.createElement("h2");
    heading.textContent = title;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn btn-ghost";
    close.textContent = "✕";
    close.title = "Close";
    close.addEventListener("click", () => selectTopo(null));
    head.append(heading, close);
    dom.topoDetail.append(head);
    if (subtitle) {
      const sub = document.createElement("p");
      sub.className = "muted";
      sub.textContent = subtitle;
      dom.topoDetail.append(sub);
    }
    return dom.topoDetail;
  }

  function topoDetailRow(term, description) {
    const row = document.createElement("div");
    row.className = "topo-detail-row";
    const label = document.createElement("span");
    label.className = "muted";
    label.textContent = term;
    const value = document.createElement("span");
    value.textContent = description;
    row.append(label, value);
    return row;
  }

  function renderTopoNodeDetail(name) {
    const graph = state.topology;
    if (!graph) return;
    const node = graph.nodes.find((n) => n.name === name);
    if (!node) {
      // The node went away between two polls; drop the selection with it.
      selectTopo(null);
      return;
    }
    if (isTopoAttached(node)) {
      renderTopoAttachedDetail(node, graph);
      return;
    }
    const panel = topoDetailShell(node.label, node.name === node.label ? "" : node.name);
    panel.append(topoDetailRow("role", ROLE_LABELS[node.role] || node.role));
    if (node.site) panel.append(topoDetailRow("site", node.site));
    panel.append(
      topoDetailRow("services", `${node.mac_vrfs} mac-vrf · ${node.ip_vrfs} ip-vrf`)
    );
    if (node.stitched) {
      panel.append(topoDetailRow("stitched", `${node.stitched} service(s), two bgp-vpn instances`));
    }
    if (node.clients) panel.append(topoDetailRow("clients", String(node.clients)));
    if (node.error) panel.append(topoDetailRow("error", node.error));

    const links = graph.links.filter((l) => l.a === name || l.b === name);
    const heading = document.createElement("h3");
    heading.textContent = `${links.length} link(s)`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const link of links) {
      const peer = link.a === name ? link.b : link.a;
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-peer";
      const peerNode = graph.nodes.find((n) => n.name === peer);
      const title = document.createElement("span");
      title.textContent = peerNode ? peerNode.label : peer;
      const ports = document.createElement("span");
      ports.className = "muted";
      ports.textContent = link.ports
        .map((pair) =>
          link.a === name
            ? cableText(pair.a_port, pair.b_port, "→")
            : cableText(pair.b_port, pair.a_port, "→")
        )
        .join(", ");
      button.append(title, ports);
      button.addEventListener("click", () => selectTopo({ kind: "node", id: peer }));
      item.append(button);
      list.append(item);
    }
    panel.append(list);
  }

  /** A client or a segment is its attachments: where it lands, in which service. */
  function renderTopoAttachedDetail(node, graph) {
    const label = (name) => {
      const peer = graph.nodes.find((n) => n.name === name);
      return peer ? peer.label : name;
    };
    const panel = topoDetailShell(node.label, node.peers.map(label).join(", "));
    panel.append(topoDetailRow("role", ROLE_LABELS[node.role] || node.role));
    if (node.advertised) panel.append(topoDetailRow("lldp name", node.advertised));
    if ((node.names || []).length) {
      panel.append(topoDetailRow("configured as", node.names.join(" · ")));
    }
    if (node.site) panel.append(topoDetailRow("site", node.site));
    const kinds = [...new Set(node.attachments.map((a) => a.kind))];
    if (kinds.length) panel.append(topoDetailRow("attached", kinds.join(" · ")));
    if (node.esi) panel.append(topoDetailRow("esi", node.esi));

    const heading = document.createElement("h3");
    heading.textContent = `${node.attachments.length} attachment(s)`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const attachment of node.attachments) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-peer";
      const title = document.createElement("span");
      title.textContent = `${label(attachment.node)} ${shortPort(attachment.subinterface)}`;
      const detail = document.createElement("span");
      detail.className = "muted";
      const parts = [attachment.service];
      if (attachment.vlan) parts.push(`vlan ${attachment.vlan}`);
      if (attachment.ip) parts.push(attachment.ip);
      if (attachment.state) parts.push(attachment.state);
      detail.textContent = parts.filter(Boolean).join(" · ");
      button.append(title, detail);
      button.addEventListener("click", () =>
        selectTopo({ kind: "node", id: attachment.node })
      );
      item.append(button);
      list.append(item);
    }
    panel.append(list);
  }

  function renderTopoLinkDetail(id) {
    const graph = state.topology;
    if (!graph) return;
    const [a, b] = id.split("\u0000");
    const link = graph.links.find((l) => l.a === a && l.b === b);
    if (!link) {
      selectTopo(null);
      return;
    }
    const label = (name) => {
      const node = graph.nodes.find((n) => n.name === name);
      return node ? node.label : name;
    };
    const panel = topoDetailShell(`${label(a)} ↔ ${label(b)}`, "");
    panel.append(topoDetailRow("state", link.state));
    panel.append(topoDetailRow("cables", String(link.count)));
    panel.append(topoDetailRow(`${label(a)} out`, formatBps(link.a_out_bps)));
    panel.append(topoDetailRow(`${label(b)} out`, formatBps(link.b_out_bps)));
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const pair of link.ports) {
      const item = document.createElement("li");
      item.className = "topo-cable";
      const cable = cableText(pair.a_port, pair.b_port, "↔");
      if (pair.a_out_bps != null || pair.b_out_bps != null) {
        item.textContent = `${cable} · ${formatBps(pair.a_out_bps)} out / ${formatBps(pair.b_out_bps)} out`;
      } else {
        item.textContent = cable;
      }
      list.append(item);
    }
    panel.append(list);
  }

  /* ---------------------------------------------------------- page history */

  function currentNavSnap() {
    return {
      id: 0,
      name: state.report.name,
      title: state.report.title,
      filters: [...state.colFilters.entries()],
      viewMode: state.viewMode,
    };
  }

  function navPageKey(snap) {
    const viewMode = ["bridge_domains", "services", "routers"].includes(snap.name)
      ? snap.viewMode
      : "";
    return JSON.stringify({ name: snap.name, filters: snap.filters, viewMode });
  }

  function applyNavSnap(snap) {
    state.colFilters.clear();
    for (const [column, pattern] of snap.filters || []) {
      if (pattern) state.colFilters.set(column, pattern);
    }
    if (snap.viewMode) state.viewMode = snap.viewMode;
    saveReportPreferences();
    if (snap.viewMode && state.report) {
      try {
        localStorage.setItem(`fcli-viewmode-${state.report.name}`, snap.viewMode);
      } catch (_err) {}
    }
  }

  function updateNavButtons() {
    if (!dom.navBack || !dom.navForward) return;
    const canBack = state.navIndex > 0;
    const canFwd = state.navIndex >= 0 && state.navIndex < state.navStack.length - 1;
    dom.navBack.disabled = !canBack;
    dom.navForward.disabled = !canFwd;
    const prev = canBack ? state.navStack[state.navIndex - 1] : null;
    const next = canFwd ? state.navStack[state.navIndex + 1] : null;
    dom.navBack.title = prev ? `Back to ${prev.title}` : "Back";
    dom.navForward.title = next ? `Forward to ${next.title}` : "Forward";
  }

  function syncCurrentVisit() {
    if (!state.report || state.navIndex < 0) return;
    const snap = currentNavSnap();
    snap.id = state.navStack[state.navIndex].id;
    state.navStack[state.navIndex] = snap;
    history.replaceState({ page: snap }, "", "#" + snap.name);
  }

  function recordVisit() {
    if (!state.report) return;
    const snap = currentNavSnap();
    const current = state.navStack[state.navIndex];
    if (current && navPageKey(current) === navPageKey(snap)) {
      snap.id = current.id;
      state.navStack[state.navIndex] = snap;
      history.replaceState({ page: snap }, "", "#" + snap.name);
      updateNavButtons();
      return;
    }
    snap.id = ++navSeq;
    const url = "#" + snap.name;
    if (state.navIndex < 0) {
      state.navStack = [snap];
      state.navIndex = 0;
      history.replaceState({ page: snap }, "", url);
    } else {
      state.navStack = state.navStack.slice(0, state.navIndex + 1);
      state.navStack.push(snap);
      state.navIndex = state.navStack.length - 1;
      history.pushState({ page: snap }, "", url);
    }
    updateNavButtons();
  }

  function restoreNavSnap(snap) {
    const report = state.reports.find((r) => r.name === snap.name);
    if (!report) return;
    if (state.report && state.report.name === snap.name) {
      applyNavSnap(snap);
      updateFilterUI();
      if (["bridge_domains", "services", "routers"].includes(snap.name)) {
        if (dom.viewModeBtn) {
          dom.viewModeBtn.hidden = false;
          dom.viewModeBtn.textContent =
            state.viewMode === "tree" ? "📊 Table View" : "🌲 Services View";
        }
        renderBody();
      } else {
        renderHead();
        renderBody();
      }
      updateNavButtons();
      return;
    }
    selectReport(report, { fromPop: true, snap });
    updateNavButtons();
  }

  function selectReport(report, { fromPop = false, snap = null } = {}) {
    if (state.report && !fromPop) {
      saveReportPreferences();
      syncCurrentVisit();
    }
    state.report = report;
    state.columns = [];
    state.rows = [];
    state.errors = [];
    state.sort = { column: null, dir: 1 };
    state.previous.clear();
    state.identityColumn = null;
    state.firstPaint = true;
    state.windowSize = WINDOW_STEP;
    dom.title.textContent = report.title;
    dom.desc.textContent = report.description;
    dom.body.replaceChildren();
    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();

    if (overviewTimer) {
      clearInterval(overviewTimer);
      overviewTimer = null;
    }
    if (topologyTimer) {
      clearInterval(topologyTimer);
      topologyTimer = null;
    }

    loadReportPreferences();
    if (snap) {
      state.pendingFilters = null;
      applyNavSnap(snap);
    } else {
      applyPendingFilters();
    }
    updateFilterUI();
    renderReportList();

    if (isPanelReport(report.name)) {
      // A panel draws itself from its own endpoint instead of a table stream.
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = true;
      dom.viewModeBtn.hidden = true;
      dom.columnsBtn.hidden = true;
      dom.exportBtn.hidden = true;
      dom.overviewDashboard.hidden = report.name !== "overview";
      dom.topologyView.hidden = report.name !== "topology";
      if (state.source) {
        state.source.close();
        state.source = null;
      }
      if (report.name === "overview") {
        loadOverview();
        overviewTimer = setInterval(loadOverview, 5000);
      } else {
        state.topoKey = "";
        state.topoSelection = null;
        loadTopology();
        topologyTimer = setInterval(loadTopology, 5000);
      }
    } else {
      dom.overviewDashboard.hidden = true;
      dom.topologyView.hidden = true;
      dom.columnsBtn.hidden = false;
      dom.exportBtn.hidden = false;
      if (["bridge_domains", "services", "routers"].includes(report.name)) {
        if (!(snap && snap.viewMode)) {
          state.viewMode = localStorage.getItem(`fcli-viewmode-${report.name}`) || "tree";
        }
        dom.viewModeBtn.hidden = false;
        dom.viewModeBtn.textContent = state.viewMode === "tree" ? "📊 Table View" : "🌲 Services View";
      } else {
        dom.viewModeBtn.hidden = true;
      }
      dom.rowCount.textContent = "loading...";
      connect();
    }
    if (!fromPop) recordVisit();
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
    if (!state.report || isPanelReport(state.report.name) || state.paused) return;
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

  function escapeRegex(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /** A column-filter pattern that matches any of *values* exactly. */
  function exactMatchPattern(values) {
    const unique = [...new Set((values || []).map((v) => String(v || "").trim()).filter(Boolean))];
    if (!unique.length) return "";
    if (unique.length === 1) return `^${escapeRegex(unique[0])}$`;
    return `^(${unique.map(escapeRegex).join("|")})$`;
  }

  function applyPendingFilters() {
    if (!state.pendingFilters) return;
    if (!state.report || state.report.name !== state.pendingFilters.report) return;
    state.colFilters.clear();
    for (const [column, pattern] of Object.entries(state.pendingFilters.filters || {})) {
      if (pattern) state.colFilters.set(column, pattern);
    }
    state.pendingFilters = null;
    saveReportPreferences();
  }

  /** A column-filter pattern that matches any of *values* as a whole token.

  Used for network-instance names, which may share a cell with other VRFs
  (an IRB sits in both a mac-vrf and an ip-vrf).
  */
  function tokenMatchPattern(values) {
    const unique = [...new Set((values || []).map((v) => String(v || "").trim()).filter(Boolean))];
    if (!unique.length) return "";
    const parts = unique.map((v) => {
      const e = escapeRegex(v);
      // The NI cell may be a single name ("vrf1"), a comma list ("vrf1, mac-vrf-1"
      // for an IRB in both VRFs), or a JSON list leftover ("[\"vrf1\"]").
      return `(?:^|[,\\[\\]"'\\s])${e}(?=$|[,\\]\\]"'\\s])`;
    });
    return parts.length === 1 ? parts[0] : `(?:${parts.join("|")})`;
  }

  function jumpToFilteredReport(reportName, niNames, nodeNames) {
    const filters = {};
    const ni = tokenMatchPattern(niNames);
    const nodes = exactMatchPattern(nodeNames);
    if (ni) filters.NI = ni;
    if (nodes) filters.Node = nodes;
    state.pendingFilters = { report: reportName, filters };

    if (state.report && state.report.name === reportName) {
      syncCurrentVisit();
      applyPendingFilters();
      updateFilterUI();
      renderHead();
      renderBody();
      recordVisit();
      return;
    }
    const report = state.reports.find((r) => r.name === reportName);
    if (report) {
      selectReport(report);
    } else {
      state.pendingFilters = null;
    }
  }

  function makeReportJumpButton(label, title, reportName, niNames, nodeNames) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bd-report-jump-btn";
    button.textContent = label;
    button.title = title;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      jumpToFilteredReport(reportName, niNames, nodeNames);
    });
    return button;
  }

  function reportJumpGroup(buttons) {
    const group = document.createElement("div");
    group.className = "bd-report-jump-group";
    group.append(...buttons);
    return group;
  }

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
  // Standby is its own kind: an ethernet-segment holding a port down is intent,
  // and neither red nor orange is the truth about it.
  function stateKind(state) {
    const st = String(state || "").toLowerCase().trim();
    if (!st) return "";
    if (st === STANDBY_STATE) return "standby";
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
    const counts = { up: 0, down: 0, degraded: 0, standby: 0 };
    for (const row of rows) {
      counts[stateKind(row["Oper State"]) || "degraded"] += 1;
    }

    // A service standing by on one node is not a service in trouble.
    if (counts.up + counts.standby === rows.length) {
      return { status: "up", className: "state-badge-up", badgeText: "UP" };
    }
    if (counts.down === rows.length) {
      return { status: "down", className: "state-badge-down", badgeText: "DOWN" };
    }
    return { status: "degraded", className: "state-badge-warn", badgeText: "DEGRADED" };
  }

  function compareSubnets(a, b) {
    const v6a = a.includes(":") ? 1 : 0;
    const v6b = b.includes(":") ? 1 : 0;
    if (v6a !== v6b) return v6a - v6b;
    return a.localeCompare(b);
  }

  function unionSubnets(rows) {
    const seen = [];
    for (const r of rows) {
      if (!r["Subnets"]) continue;
      for (const s of String(r["Subnets"]).split(",").map((x) => x.trim())) {
        if (s && !seen.includes(s)) seen.push(s);
      }
    }
    return seen.sort(compareSubnets);
  }

  function subnetPills(subnets) {
    const el = document.createElement("div");
    el.className = "bd-header-subnets";
    for (const s of subnets) {
      const pill = document.createElement("span");
      pill.className = "bd-subnet-pill";
      pill.textContent = s;
      el.append(pill);
    }
    return el;
  }

  function isGatewayRow(row) {
    const value = row && row["Gateway"];
    return value === "Y" || value === true || value === "true";
  }

  function isDciService(rows) {
    return Boolean(rows && rows.length && rows.every(isGatewayRow));
  }

  function roleBadge(text, className) {
    const el = document.createElement("span");
    el.className = className;
    el.textContent = text;
    return el;
  }

  function nodeNameWithIps(nodeName, nodeRows) {
    const wrap = document.createElement("span");
    wrap.className = "bd-node-name";

    const name = document.createElement("span");
    name.textContent = `🖥️ Node: ${nodeName}`;
    wrap.append(name);

    const isGateway = (nodeRows || []).some(isGatewayRow);
    if (isGateway) wrap.append(roleBadge("Gateway", "bd-gateway-badge"));

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
      const site = row["Site"] ? String(row["Site"]) : "";
      const groupKey = site ? `${bdName}@@${site}` : bdName;
      if (!bdMap.has(groupKey)) bdMap.set(groupKey, []);
      bdMap.get(groupKey).push(row);
    }

    for (const [groupKey, bdRows] of bdMap) {
      const bdName = (bdRows[0] && (bdRows[0]["Bridge Domain"] || bdRows[0]["Route Targets"])) || groupKey.split("@@")[0];
      const site = (bdRows[0] && bdRows[0]["Site"]) ? String(bdRows[0]["Site"]) : "";
      const siteSuffix = site ? ` (${site})` : "";
      const cardKey = `bd:${groupKey}`;
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
      title.textContent = `Bridge-domain: ${vrfTitleStr}${siteSuffix}${subnetsStr}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(bdRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title);
      if (isDciService(bdRows)) topRow.append(roleBadge("DCI", "bd-dci-badge"));
      topRow.append(stateBadge, badge);
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

      const nodeNames = [...new Set(bdRows.map((r) => r.Node).filter(Boolean))];
      const niHint = vrfNames.length ? vrfNames.join(", ") : "this bridge domain";
      const nodeHint = nodeNames.length ? nodeNames.join(", ") : "participating nodes";
      subRow.append(
        reportJumpGroup([
          makeReportJumpButton(
            "Bridge table",
            `MAC table for ${niHint} on ${nodeHint}`,
            "mac",
            vrfNames,
            nodeNames,
          ),
        ]),
      );

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

          const vrfTitle = document.createElement("span");
          vrfTitle.className = "bd-vrf-title";
          vrfTitle.append(vrfName);
          if (row["BGP Instance"]) {
            const instBadge = document.createElement("span");
            instBadge.className = "bd-bgp-inst-badge";
            instBadge.textContent = `bgp-instance ${row["BGP Instance"]}`;
            vrfTitle.append(instBadge);
          }

          const stateSpan = document.createElement("span");
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;

          vrfHeader.append(vrfTitle, stateSpan);
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

          // 2. bridge sub-interfaces, each with the ethernet-segment its
          //    parent port sits on
          const subitfStr = row["Sub-Interfaces"] || "-";
          if (subitfStr !== "-") {
            const subRowDiv = document.createElement("div");
            subRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "bridge sub-interfaces:";
            subRowDiv.append(label);

            const lines = document.createElement("div");
            lines.className = "pill-lines";
            // A line each, so a member and its segment stay side by side:
            // pairing them up across two lists is what made a bridge domain
            // with several multi-homed members unreadable.
            subitfStr.split(";").forEach((entry) => {
              const [itfText, ...esText] = entry.split("->");

              const line = document.createElement("div");
              line.className = "pill-group";

              const p = document.createElement("span");
              p.className = "pill";
              applyPillState(p, itfText);
              p.textContent = itfText.trim();
              line.append(p);

              const es = esText.join("->").trim();
              if (es) {
                const arrow = document.createElement("span");
                arrow.className = "pill-arrow";
                arrow.textContent = "→";

                const esPill = document.createElement("span");
                esPill.className = "pill pill-es";
                const oper = /\boper:\s*(\S+)/.exec(es);
                const kind = oper ? stateKind(oper[1]) : "";
                if (kind && kind !== "up") esPill.classList.add(`pill-${kind}`);
                esPill.textContent = es;
                line.append(arrow, esPill);
              }
              lines.append(line);
            });
            subRowDiv.append(lines);
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
      const site = row["Site"] ? String(row["Site"]) : "";
      const groupKey = site ? `${routerName}@@${site}` : routerName;
      if (!routerMap.has(groupKey)) routerMap.set(groupKey, []);
      routerMap.get(groupKey).push(row);
    }

    for (const [groupKey, rRows] of routerMap) {
      const routerName = (rRows[0] && (rRows[0]["Router"] || rRows[0]["Route Targets"])) || groupKey.split("@@")[0];
      const site = (rRows[0] && rRows[0]["Site"]) ? String(rRows[0]["Site"]) : "";
      const siteSuffix = site ? ` (${site})` : "";
      const cardKey = `router:${groupKey}`;
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
      title.textContent = `Router: ${vrfTitleStr}${siteSuffix}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(rRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title);
      if (isDciService(rRows)) topRow.append(roleBadge("DCI", "bd-dci-badge"));
      topRow.append(stateBadge, badge);
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtBlock = document.createElement("div");
      rtBlock.className = "bd-rt-block";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      const isIsolated = !routerName || routerName === "none (isolated)" || routerName.startsWith("none (isolated)") || routerName.startsWith("ip-vrf:") || routerName === "unassigned";
      rtLabel.textContent = isIsolated ? "Route-target: none (isolated)" : `Route-target: ${routerName}`;
      rtBlock.append(rtLabel);

      const allSubnets = unionSubnets(rRows);
      if (allSubnets.length) rtBlock.append(subnetPills(allSubnets));
      subRow.append(rtBlock);

      if (hasVrfMismatch) {
        const mismatchBadge = document.createElement("span");
        mismatchBadge.className = "bd-mismatch-badge";
        mismatchBadge.textContent = `⚠️ Mismatched VRF Names (${vrfNames.join(" vs ")})`;
        subRow.append(mismatchBadge);
      }

      const nodeNames = [...new Set(rRows.map((r) => r.Node).filter(Boolean))];
      const niHint = vrfNames.length ? vrfNames.join(", ") : "this router";
      const nodeHint = nodeNames.length ? nodeNames.join(", ") : "participating nodes";
      subRow.append(
        reportJumpGroup([
          makeReportJumpButton(
            "IPv4 RIB",
            `IPv4 routes for ${niHint} on ${nodeHint}`,
            "ipv4_rib",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "IPv6 RIB",
            `IPv6 routes for ${niHint} on ${nodeHint}`,
            "ipv6_rib",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "ARP",
            `ARP table for ${niHint} on ${nodeHint}`,
            "arp",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "ND",
            `IPv6 neighbors for ${niHint} on ${nodeHint}`,
            "nd",
            vrfNames,
            nodeNames,
          ),
        ]),
      );

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

          const vrfTitle = document.createElement("span");
          vrfTitle.className = "bd-vrf-title";
          vrfTitle.append(vrfName);
          if (row["BGP Instance"]) {
            const instBadge = document.createElement("span");
            instBadge.className = "bd-bgp-inst-badge";
            instBadge.textContent = `bgp-instance ${row["BGP Instance"]}`;
            vrfTitle.append(instBadge);
          }

          const stateSpan = document.createElement("span");
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;

          vrfHeader.append(vrfTitle, stateSpan);
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
              applyPillState(p, itemStr);

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
    // Overview and Topology own the main area; a table would be drawn over them.
    if (state.report && isPanelReport(state.report.name)) return;
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

  function transferIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", "node-xfer");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      "M2 5h9.5m0 0L9 2.75M11.5 5 9 7.25M14 11H4.5m0 0L7 8.75M4.5 11 7 13.25"
    );
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }

  let inventoryKey = "";
  const getCounts = new Map();

  // A Get is over in milliseconds, so sampling "one is in flight" would almost
  // never catch it. The node's Get counter moving between two polls is what
  // shows gNMI activity.
  function pollsGnmi(host) {
    const seen = getCounts.get(host.name);
    getCounts.set(host.name, host.gets);
    return host.getting || (seen !== undefined && host.gets > seen);
  }

  async function loadInventory() {
    try {
      const res = await fetch("/api/inventory");
      const data = await res.json();
      const hosts = data.hosts.map((host) => ({
        ...host,
        active: pollsGnmi(host),
      }));
      const key = hosts
        .map(
          (h) => `${h.name}:${h.connected}:${h.streaming}:${h.active}:${h.error || ""}`
        )
        .join("|");
      if (key === inventoryKey) return;
      inventoryKey = key;
      dom.nodeList.replaceChildren(
        ...hosts.map((host) => {
          const item = document.createElement("li");
          const dot = document.createElement("span");
          dot.className =
            "dot " +
            (!host.connected ? "error" : host.streaming ? "live" : "paused");
          const name = document.createElement("span");
          name.className = "node-name";
          name.textContent = host.name;
          let status;
          if (host.getting) status = "gNMI Get in flight";
          else if (host.error) status = host.error;
          else if (host.active) status = "gNMI Get just completed";
          else if (host.streaming) status = "streaming";
          else status = "connected, not subscribed";
          item.title = `${host.name} (${host.hostname}) - ${status}`;
          item.append(dot, name);
          if (host.active) item.append(transferIcon());
          return item;
        })
      );
      const up = hosts.filter((h) => h.connected).length;
      dom.nodeSummary.textContent = `${up}/${hosts.length} up`;
    } catch (_err) {
      inventoryKey = "";
      dom.nodeSummary.textContent = "unavailable";
    }
  }

  /* ------------------------------------------------------- sidebar split */

  const SIDE_SPLIT_MIN = 72;
  const SIDE_SPLIT_DEFAULT = 0.38;
  let sideSplitFrac = SIDE_SPLIT_DEFAULT;

  function loadSideSplitFrac() {
    try {
      const stored = parseFloat(localStorage.getItem("fcli-side-split"));
      if (stored > 0 && stored < 1) return stored;
    } catch (_err) {
      /* storage may be unavailable */
    }
    return SIDE_SPLIT_DEFAULT;
  }

  function saveSideSplitFrac(frac) {
    sideSplitFrac = frac;
    try {
      localStorage.setItem("fcli-side-split", String(frac));
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  /** Size the nodes pane to *frac* of the split, leaving reports the rest. */
  function applySideSplit(frac, persist) {
    const split = dom.sideSplit;
    const nodes = dom.nodesBlock;
    const splitter = dom.sideSplitter;
    if (!split || !nodes || !splitter) return;
    const avail = split.clientHeight - splitter.offsetHeight;
    if (avail <= 0) return;
    const min = Math.min(SIDE_SPLIT_MIN, Math.floor(avail / 3));
    const height = Math.round(Math.min(avail - min, Math.max(min, avail * frac)));
    nodes.style.flexBasis = `${height}px`;
    const used = height / avail;
    sideSplitFrac = used;
    splitter.setAttribute("aria-valuenow", String(Math.round(used * 100)));
    if (persist) saveSideSplitFrac(used);
  }

  function initSideSplit() {
    const splitter = dom.sideSplitter;
    const split = dom.sideSplit;
    if (!splitter || !split) return;
    sideSplitFrac = loadSideSplitFrac();
    applySideSplit(sideSplitFrac, false);

    splitter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      const sidebar = split.closest(".sidebar");
      if (sidebar) sidebar.classList.add("is-resizing");
      const box = split.getBoundingClientRect();
      const onMove = (move) => {
        const avail = box.height - splitter.offsetHeight;
        if (avail <= 0) return;
        applySideSplit((box.bottom - move.clientY - splitter.offsetHeight / 2) / avail, false);
      };
      const onUp = () => {
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        if (sidebar) sidebar.classList.remove("is-resizing");
        applySideSplit(sideSplitFrac, true);
      };
      splitter.addEventListener("pointermove", onMove);
      splitter.addEventListener("pointerup", onUp);
      splitter.addEventListener("pointercancel", onUp);
    });

    splitter.addEventListener("keydown", (event) => {
      let next = sideSplitFrac;
      if (event.key === "ArrowUp") next -= 0.05;
      else if (event.key === "ArrowDown") next += 0.05;
      else if (event.key === "Home") next = 0.15;
      else if (event.key === "End") next = 0.85;
      else return;
      event.preventDefault();
      applySideSplit(next, true);
    });

    if (window.ResizeObserver) {
      new ResizeObserver(() => applySideSplit(sideSplitFrac, false)).observe(split);
    }
  }

  /* ------------------------------------------------------------- wiring */

  if (dom.navBack) {
    dom.navBack.addEventListener("click", () => {
      if (state.navIndex > 0) history.back();
    });
  }
  if (dom.navForward) {
    dom.navForward.addEventListener("click", () => {
      if (state.navIndex < state.navStack.length - 1) history.forward();
    });
  }
  window.addEventListener("popstate", (event) => {
    const snap = event.state && event.state.page;
    if (!snap || !state.reports.length) return;
    const idx = state.navStack.findIndex((entry) => entry.id === snap.id);
    if (idx >= 0) {
      state.navIndex = idx;
    } else {
      state.navStack = [snap];
      state.navIndex = 0;
      if (snap.id > navSeq) navSeq = snap.id;
    }
    restoreNavSnap(snap);
  });

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
      if (state.report && state.report.name === "topology") {
        state.topoKey = "";
        loadTopology();
      } else {
        connect();
      }
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
    } else if (state.report && state.report.name === "topology") {
      loadTopology();
    } else {
      connect();
    }
  });

  dom.topoPortLabels.addEventListener("change", () => {
    try {
      localStorage.setItem("fcli-topo-ports", dom.topoPortLabels.checked ? "1" : "");
    } catch (_err) {
      /* storage may be unavailable */
    }
    if (state.topology) renderTopology(state.topology);
  });

  if (dom.topoMaxBw) {
    dom.topoMaxBw.addEventListener("change", onTopoMaxBwChange);
    dom.topoMaxBw.addEventListener("input", onTopoMaxBwChange);
  }
  if (dom.topoMaxBwUnit) {
    dom.topoMaxBwUnit.addEventListener("change", onTopoMaxBwChange);
  }

  dom.topoZoomIn.addEventListener("click", () =>
    setTopoZoom(topoZoom() * TOPO_ZOOM_STEP, topoCanvasCenter())
  );
  dom.topoZoomOut.addEventListener("click", () =>
    setTopoZoom(topoZoom() / TOPO_ZOOM_STEP, topoCanvasCenter())
  );
  dom.topoZoomLevel.addEventListener("click", () => setTopoZoom(1, topoCanvasCenter()));
  dom.topoZoomFit.addEventListener("click", fitTopoZoom);

  dom.topoCanvas.addEventListener(
    "wheel",
    (event) => {
      // A bare wheel scrolls the canvas; ctrl (or a trackpad pinch, which
      // arrives as one) zooms, as it does in a map.
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setTopoZoom(topoZoom() * Math.exp(-event.deltaY * 0.002), {
        x: event.clientX,
        y: event.clientY,
      });
    },
    { passive: false }
  );

  // Dragging the canvas pans it. A drag that moves is not a click, so the
  // selection a node or a link would otherwise take is swallowed below.
  let topoPan = null;
  let topoPanned = false;
  dom.topoCanvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 && event.button !== 1) return;
    topoPanned = false;
    topoPan = {
      x: event.clientX,
      y: event.clientY,
      left: dom.topoCanvas.scrollLeft,
      top: dom.topoCanvas.scrollTop,
      moved: false,
    };
  });
  window.addEventListener("pointermove", (event) => {
    if (!topoPan) return;
    const dx = event.clientX - topoPan.x;
    const dy = event.clientY - topoPan.y;
    if (!topoPan.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
    topoPan.moved = true;
    dom.topoCanvas.classList.add("is-panning");
    dom.topoCanvas.scrollLeft = topoPan.left - dx;
    dom.topoCanvas.scrollTop = topoPan.top - dy;
  });
  window.addEventListener("pointerup", () => {
    if (!topoPan) return;
    topoPanned = topoPan.moved;
    topoPan = null;
    dom.topoCanvas.classList.remove("is-panning");
  });
  dom.topoCanvas.addEventListener(
    "click",
    (event) => {
      if (!topoPanned) return;
      event.stopPropagation();
    },
    true
  );

  document.addEventListener("keydown", (event) => {
    if (dom.topologyView.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target && target.closest && target.closest("input, textarea, select")) return;
    if (event.key === "+" || event.key === "=") setTopoZoom(topoZoom() * TOPO_ZOOM_STEP, topoCanvasCenter());
    else if (event.key === "-" || event.key === "_") setTopoZoom(topoZoom() / TOPO_ZOOM_STEP, topoCanvasCenter());
    else if (event.key === "0") setTopoZoom(1, topoCanvasCenter());
    else if (event.key === "f" || event.key === "F") fitTopoZoom();
    else return;
    event.preventDefault();
  });

  if (window.ResizeObserver) {
    // A fitted drawing follows the canvas, which changes with the window but
    // also when the detail panel opens beside it.
    let fitted = 0;
    const observer = new ResizeObserver(() => {
      if (dom.topologyView.hidden || !state.topoFit) return;
      const zoom = topoFitZoom();
      // Resizing the drawing resizes the canvas back when a scrollbar comes or
      // goes, so ignore what a redraw would not move.
      if (Math.abs(zoom - fitted) < 0.005) return;
      fitted = zoom;
      applyTopoZoom();
    });
    observer.observe(dom.topoCanvas);
  }

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
    dom.topoPortLabels.checked = Boolean(localStorage.getItem("fcli-topo-ports"));
  } catch (_err) {
    /* storage may be unavailable */
  }
  restoreTopoZoom();
  restoreTopoFabric();
  restoreTopoMaxBw();
  initSideSplit();

  /* ---------------------------------------------------------- markdown */

  /* A small CommonMark subset for what the models actually emit: headings,
     lists, fenced code, pipe tables, blockquotes and inline emphasis. Nodes are
     built with the DOM rather than innerHTML, so model output can never inject
     markup. */

  // Underscore emphasis needs a word boundary, or snake_case names the agent
  // deals in all day (bgp_rib_evpn_2, mac_table) would come out italicised.
  const MD_INLINE =
    /(`+)([\s\S]+?)\1|\*\*([\s\S]+?)\*\*|(?<!\w)__([\s\S]+?)__(?!\w)|~~([\s\S]+?)~~|\*(\S[^*\n]*?)\*|(?<!\w)_(\S[^_\n]*?)_(?!\w)|\[([^\]\n]+)\]\(([^)\s]+)\)/;

  /** Append *text* to *parent*, turning inline markdown into elements. */
  function mdInline(text, parent) {
    let rest = String(text);
    while (rest) {
      const match = MD_INLINE.exec(rest);
      if (!match) break;
      if (match.index > 0) parent.append(rest.slice(0, match.index));
      const [full, , codeText, strong1, strong2, strike, em1, em2, linkText, href] =
        match;
      if (codeText !== undefined) {
        const code = document.createElement("code");
        code.textContent = codeText.trim();
        parent.append(code);
      } else if (strong1 !== undefined || strong2 !== undefined) {
        const strong = document.createElement("strong");
        mdInline(strong1 !== undefined ? strong1 : strong2, strong);
        parent.append(strong);
      } else if (strike !== undefined) {
        const del = document.createElement("del");
        mdInline(strike, del);
        parent.append(del);
      } else if (em1 !== undefined || em2 !== undefined) {
        const em = document.createElement("em");
        mdInline(em1 !== undefined ? em1 : em2, em);
        parent.append(em);
      } else if (linkText !== undefined) {
        // Anything but a plain web link stays inert text.
        if (/^(https?:|mailto:)/i.test(href)) {
          const link = document.createElement("a");
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          mdInline(linkText, link);
          parent.append(link);
        } else {
          parent.append(full);
        }
      }
      rest = rest.slice(match.index + full.length);
    }
    if (rest) parent.append(rest);
  }

  function mdParagraph(lines) {
    const p = document.createElement("p");
    lines.forEach((line, index) => {
      if (index) p.append(document.createElement("br"));
      mdInline(line, p);
    });
    return p;
  }

  function mdIndent(line) {
    const match = /^\s*/.exec(line);
    return match[0].replace(/\t/g, "  ").length;
  }

  function mdListItem(line) {
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (bullet) return { ordered: false, text: bullet[1] };
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ordered) return { ordered: true, text: ordered[1] };
    return null;
  }

  function mdTableRow(line) {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((cell) => cell.trim());
  }

  /** Parse *text* into an array of block-level DOM nodes. */
  function mdBlocks(text) {
    const lines = String(text).replace(/\r\n/g, "\n").split("\n");
    const nodes = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i += 1;
        continue;
      }
      const fence = /^\s*(```|~~~)(.*)$/.exec(line);
      if (fence) {
        const body = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        const lang = fence[2].trim();
        if (lang) code.className = "lang-" + lang.split(/\s+/)[0];
        code.textContent = body.join("\n");
        pre.append(code);
        nodes.push(pre);
        continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const el = document.createElement("h" + Math.min(heading[1].length, 4));
        mdInline(heading[2].replace(/\s+#+\s*$/, ""), el);
        nodes.push(el);
        i += 1;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        nodes.push(document.createElement("hr"));
        i += 1;
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        const quote = document.createElement("blockquote");
        mdBlocks(quoted.join("\n")).forEach((node) => quote.append(node));
        nodes.push(quote);
        continue;
      }
      if (
        line.includes("|") &&
        i + 1 < lines.length &&
        /^\s*\|?[\s:-]*-[\s|:-]*$/.test(lines[i + 1]) &&
        lines[i + 1].includes("-")
      ) {
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        for (const cell of mdTableRow(line)) {
          const th = document.createElement("th");
          mdInline(cell, th);
          headRow.append(th);
        }
        thead.append(headRow);
        table.append(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const row = document.createElement("tr");
          for (const cell of mdTableRow(lines[i])) {
            const td = document.createElement("td");
            mdInline(cell, td);
            row.append(td);
          }
          tbody.append(row);
          i += 1;
        }
        table.append(tbody);
        nodes.push(table);
        continue;
      }
      const item = mdListItem(line);
      if (item) {
        const block = [];
        const baseIndent = mdIndent(line);
        while (i < lines.length && (mdListItem(lines[i]) || lines[i].trim())) {
          if (!mdListItem(lines[i]) && mdIndent(lines[i]) <= baseIndent) break;
          block.push(lines[i]);
          i += 1;
        }
        nodes.push(mdList(block, baseIndent));
        continue;
      }
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !mdListItem(lines[i]) &&
        !/^(#{1,6})\s|^\s*(```|~~~|>)/.test(lines[i])
      ) {
        para.push(lines[i].trim());
        i += 1;
      }
      nodes.push(mdParagraph(para));
    }
    return nodes;
  }

  /** Build one list from *lines*, recursing for anything indented deeper. */
  function mdList(lines, baseIndent) {
    const first = mdListItem(lines[0]) || { ordered: false };
    const list = document.createElement(first.ordered ? "ol" : "ul");
    let current = null;
    let nested = [];
    const flush = () => {
      if (!current || !nested.length) {
        nested = [];
        return;
      }
      if (mdListItem(nested[0])) {
        current.append(mdList(nested, mdIndent(nested[0])));
      } else {
        // An indented continuation: a paragraph or code block under the item.
        for (const node of mdBlocks(nested.join("\n"))) current.append(node);
      }
      nested = [];
    };
    for (const line of lines) {
      const item = mdListItem(line);
      if (item && mdIndent(line) <= baseIndent) {
        flush();
        current = document.createElement("li");
        mdInline(item.text, current);
        list.append(current);
      } else if (current) {
        nested.push(line);
      }
    }
    flush();
    return list;
  }

  function renderMarkdown(text, container) {
    container.textContent = "";
    for (const node of mdBlocks(text)) container.append(node);
  }

  /* -------------------------------------------------------------- chat */

  const CHAT_WIDTH_DEFAULT = 360;
  const CHAT_WIDTH_MIN = 280;

  /** Widen or narrow the drawer, within what the window can spare. */
  function applyChatWidth(px, persist) {
    const max = Math.max(CHAT_WIDTH_MIN, Math.round(window.innerWidth * 0.8));
    const width = Math.round(Math.min(max, Math.max(CHAT_WIDTH_MIN, px)));
    document.documentElement.style.setProperty("--chat-width", width + "px");
    state.chatWidth = width;
    if (dom.chatResizer) {
      dom.chatResizer.setAttribute("aria-valuenow", String(width));
    }
    if (persist) {
      try {
        localStorage.setItem("fcli-chat-width", String(width));
      } catch (_err) {
        /* storage may be unavailable */
      }
    }
  }

  function initChatResize() {
    let stored = NaN;
    try {
      stored = parseInt(localStorage.getItem("fcli-chat-width"), 10);
    } catch (_err) {
      /* storage may be unavailable */
    }
    applyChatWidth(stored > 0 ? stored : CHAT_WIDTH_DEFAULT, false);
    const resizer = dom.chatResizer;
    if (!resizer) return;

    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      resizer.setPointerCapture(event.pointerId);
      dom.chatDrawer.classList.add("is-resizing");
      const right = dom.chatDrawer.getBoundingClientRect().right;
      const onMove = (move) => applyChatWidth(right - move.clientX, false);
      const onUp = () => {
        resizer.removeEventListener("pointermove", onMove);
        resizer.removeEventListener("pointerup", onUp);
        resizer.removeEventListener("pointercancel", onUp);
        dom.chatDrawer.classList.remove("is-resizing");
        applyChatWidth(state.chatWidth, true);
      };
      resizer.addEventListener("pointermove", onMove);
      resizer.addEventListener("pointerup", onUp);
      resizer.addEventListener("pointercancel", onUp);
    });

    resizer.addEventListener("dblclick", () =>
      applyChatWidth(CHAT_WIDTH_DEFAULT, true)
    );

    resizer.addEventListener("keydown", (event) => {
      let next = state.chatWidth;
      if (event.key === "ArrowLeft") next += 40;
      else if (event.key === "ArrowRight") next -= 40;
      else if (event.key === "Home") next = CHAT_WIDTH_MIN;
      else if (event.key === "End") next = window.innerWidth * 0.8;
      else return;
      event.preventDefault();
      applyChatWidth(next, true);
    });

    window.addEventListener("resize", () => applyChatWidth(state.chatWidth, false));
  }

  function openChat() {
    if (!state.chatEnabled || !dom.chatDrawer) return;
    dom.chatDrawer.hidden = false;
    if (dom.chatInput) dom.chatInput.focus();
  }

  function closeChat() {
    if (state.chatAbort) {
      state.chatAbort.abort();
      state.chatAbort = null;
    }
    if (dom.chatDrawer) dom.chatDrawer.hidden = true;
    setChatBusy(false);
  }

  function setChatBusy(busy) {
    state.chatBusy = busy;
    if (dom.chatSend) {
      // Send doubles as Stop: a reasoning round can run for a while.
      dom.chatSend.textContent = busy ? "Stop" : "Send";
      dom.chatSend.classList.toggle("stop", busy);
      dom.chatSend.title = busy ? "Stop this answer" : "";
    }
    if (dom.chatInput) dom.chatInput.disabled = busy;
    if (dom.chatProvider) dom.chatProvider.disabled = busy;
    if (dom.chatEffort) dom.chatEffort.disabled = busy;
  }

  function activeChatProvider() {
    return state.chatProviders.find((p) => p.id === state.chatProvider) || null;
  }

  function renderChatEfforts() {
    const select = dom.chatEffort;
    if (!select) return;
    const provider = activeChatProvider();
    const efforts = (provider && provider.efforts) || [];
    select.textContent = "";
    if (!efforts.length) {
      select.hidden = true;
      state.chatEffort = null;
      return;
    }
    const auto = document.createElement("option");
    // "auto" leaves the effort out of the request, so the model's own default
    // applies: medium on GPT-5.6, high on Claude and Grok.
    auto.value = "auto";
    auto.textContent = "auto";
    select.append(auto);
    for (const effort of efforts) {
      const option = document.createElement("option");
      option.value = effort;
      option.textContent = effort;
      select.append(option);
    }
    let saved = null;
    try {
      saved = localStorage.getItem("fcli-chat-effort-" + provider.id);
    } catch (_err) {}
    const chosen =
      (saved && (saved === "auto" || efforts.includes(saved)) && saved) ||
      provider.effort ||
      "auto";
    select.value = chosen;
    state.chatEffort = chosen;
    select.hidden = false;
    select.title = "Reasoning effort";
  }

  function renderChatProviders(providers) {
    state.chatProviders = Array.isArray(providers) ? providers : [];
    const select = dom.chatProvider;
    if (!select) return;
    let saved = null;
    try {
      saved = localStorage.getItem("fcli-chat-provider");
    } catch (_err) {}
    const ids = state.chatProviders.map((p) => p.id);
    const preset = state.chatProviders.find((p) => p.default);
    const chosen =
      (saved && ids.includes(saved) && saved) ||
      (preset && preset.id) ||
      ids[0] ||
      null;
    state.chatProvider = chosen;
    select.textContent = "";
    for (const provider of state.chatProviders) {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.id;
      if (provider.model) option.title = provider.model;
      select.append(option);
    }
    if (chosen) select.value = chosen;
    // With a single key configured there is nothing to pick.
    select.hidden = state.chatProviders.length < 2;
    const active = activeChatProvider();
    select.title = active && active.model ? "Model: " + active.model : "";
    renderChatEfforts();
  }

  function chatContext() {
    const ctx = {};
    if (state.report) ctx.report = state.report.name;
    const inv = dom.invFilter.value.trim();
    if (inv) ctx.inv_filter = inv;
    if (state.topoSelection && state.topoSelection.kind === "node") {
      ctx.topo_node = state.topoSelection.id;
    }
    return ctx;
  }

  function appendChat(role, text) {
    const row = document.createElement("div");
    row.className = "chat-msg " + role;
    if (role === "assistant") {
      const tools = document.createElement("div");
      tools.className = "chat-tools";
      const status = document.createElement("div");
      status.className = "chat-status";
      status.hidden = true;
      const dot = document.createElement("span");
      dot.className = "dot";
      const label = document.createElement("span");
      label.className = "chat-status-text";
      const clock = document.createElement("span");
      clock.className = "chat-status-time";
      status.append(dot, label, clock);
      const body = document.createElement("div");
      body.className = "chat-body";
      // An answer arrives token by token, so the stream fills this in through
      // row._body; the bubble starts empty and *text* is for the user role.
      row.append(tools, status, body);
      row._tools = tools;
      row._status = status;
      row._statusText = label;
      row._statusTime = clock;
      row._body = body;
      row._chips = new Map();
    } else {
      row.textContent = text || "";
    }
    dom.chatLog.append(row);
    scrollChatToEnd();
    return row;
  }

  function scrollChatToEnd() {
    if (dom.chatLog) dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
  }

  /** Show what the agent is doing right now, with a clock since it started. */
  function setChatStatus(row, text) {
    if (!row || !row._status) return;
    stopChatClock(row);
    if (!text) {
      row._status.hidden = true;
      return;
    }
    row._status.hidden = false;
    row._statusText.textContent = text;
    row._statusTime.textContent = "";
    const started = Date.now();
    const tick = () => {
      const seconds = Math.round((Date.now() - started) / 1000);
      row._statusTime.textContent = seconds >= 1 ? seconds + "s" : "";
    };
    row._clock = setInterval(tick, 1000);
    scrollChatToEnd();
  }

  function stopChatClock(row) {
    if (row && row._clock) {
      clearInterval(row._clock);
      row._clock = null;
    }
  }

  function chipLabel(name, args) {
    if (!args) return name;
    // Two details at most: which node, and what was asked of it.
    const parts = [
      args.node || args.inv_filter,
      args.area || args.command || args.path,
    ].filter(Boolean);
    return parts.length ? `${name} ${parts.join(" ")}` : name;
  }

  function addChatChip(row, call) {
    if (!row || !row._tools) return;
    const chip = document.createElement("span");
    chip.className = "chat-chip running";
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.textContent = chipLabel(call.name || "tool", call.args);
    const meta = document.createElement("span");
    meta.className = "chat-chip-meta";
    chip.append(dot, label, meta);
    chip._meta = meta;
    row._tools.append(chip);
    if (call.id) row._chips.set(call.id, chip);
    scrollChatToEnd();
    return chip;
  }

  function finishChatChip(row, result) {
    if (!row || !row._chips) return;
    const chip = row._chips.get(result.id);
    if (!chip) return;
    chip.classList.remove("running");
    chip.classList.add(result.error ? "failed" : "ok");
    const ms = Number(result.ms) || 0;
    chip._meta.textContent = result.repeat
      ? "repeat"
      : ms >= 1000
        ? (ms / 1000).toFixed(1) + "s"
        : ms + "ms";
    if (result.error) chip.title = result.error;
    else if (result.repeat) chip.title = "identical call, reused";
  }

  /** A muted line in the bubble for things that are not the answer or an error. */
  function addChatNote(row, text) {
    if (!row || !row._body) return;
    const note = document.createElement("div");
    note.className = "chat-note";
    note.textContent = text;
    row.insertBefore(note, row._body);
    scrollChatToEnd();
  }

  async function readChatSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message";
        const dataLines = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7).trim();
          else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
        }
        if (!dataLines.length) continue;
        let payload = {};
        try {
          payload = JSON.parse(dataLines.join("\n"));
        } catch (_err) {
          payload = { text: dataLines.join("\n") };
        }
        onEvent(event, payload);
      }
    }
  }

  async function sendChat(event) {
    if (event) event.preventDefault();
    if (!state.chatEnabled) return;
    if (state.chatBusy) {
      if (state.chatAbort) state.chatAbort.abort();
      return;
    }
    const text = (dom.chatInput.value || "").trim();
    if (!text) return;
    dom.chatInput.value = "";
    state.chatMessages.push({ role: "user", content: text });
    appendChat("user", text);
    const bubble = appendChat("assistant");
    setChatStatus(bubble, "Thinking…");
    setChatBusy(true);
    const controller = new AbortController();
    state.chatAbort = controller;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          messages: state.chatMessages,
          context: chatContext(),
          provider: state.chatProvider || undefined,
          effort: state.chatEffort || undefined,
        }),
      });
      if (!res.ok) {
        let detail = res.statusText;
        try {
          const payload = await res.json();
          if (payload && payload.error) detail = payload.error;
        } catch (_err) {}
        throw new Error(detail);
      }
      let reply = "";
      await readChatSse(res, (kind, payload) => {
        if (kind === "start") {
          const where = [
            payload.provider,
            payload.model,
            payload.effort && "effort " + payload.effort,
          ]
            .filter(Boolean)
            .join(" · ");
          bubble.title = where;
          setChatStatus(bubble, "Thinking…");
        } else if (kind === "token" && payload.text) {
          // One event per round: keep rounds apart as paragraphs, or a
          // preamble runs into the answer that follows it.
          reply += (reply ? "\n\n" : "") + payload.text;
          if (bubble._body) renderMarkdown(reply, bubble._body);
          scrollChatToEnd();
        } else if (kind === "tool") {
          addChatChip(bubble, payload);
          const round = payload.round
            ? ` (round ${payload.round}/${payload.rounds})`
            : "";
          setChatStatus(
            bubble,
            "Running " +
              chipLabel(payload.name || "tool", payload.args) +
              "…" +
              round
          );
        } else if (kind === "tool_result") {
          finishChatChip(bubble, payload);
          setChatStatus(bubble, "Thinking…");
        } else if (kind === "notice") {
          addChatNote(bubble, payload.text || "");
        } else if (kind === "done") {
          setChatStatus(bubble, "");
        } else if (kind === "error") {
          throw new Error(payload.error || "chat failed");
        }
      });
      if (reply) state.chatMessages.push({ role: "assistant", content: reply });
    } catch (err) {
      const aborted = err && err.name === "AbortError";
      const message = aborted ? "Stopped." : (err && err.message) || String(err);
      if (!aborted) bubble.classList.add("error");
      if (bubble._body) {
        // Keep whatever the model already said and add the reason below it.
        const note = document.createElement("p");
        note.textContent = message;
        bubble._body.append(note);
      } else {
        bubble.textContent = message;
      }
    } finally {
      setChatStatus(bubble, "");
      state.chatAbort = null;
      setChatBusy(false);
      if (dom.chatInput) dom.chatInput.focus();
    }
  }

  initChatResize();

  if (dom.chatOpen) dom.chatOpen.addEventListener("click", openChat);
  if (dom.chatClose) dom.chatClose.addEventListener("click", closeChat);
  if (dom.chatProvider) {
    dom.chatProvider.addEventListener("change", () => {
      state.chatProvider = dom.chatProvider.value || null;
      const active = activeChatProvider();
      dom.chatProvider.title = active && active.model ? "Model: " + active.model : "";
      renderChatEfforts();
      try {
        if (state.chatProvider) {
          localStorage.setItem("fcli-chat-provider", state.chatProvider);
        }
      } catch (_err) {}
    });
  }
  if (dom.chatEffort) {
    dom.chatEffort.addEventListener("change", () => {
      state.chatEffort = dom.chatEffort.value || null;
      try {
        if (state.chatProvider && state.chatEffort) {
          localStorage.setItem(
            "fcli-chat-effort-" + state.chatProvider,
            state.chatEffort
          );
        }
      } catch (_err) {}
    });
  }
  if (dom.chatForm) dom.chatForm.addEventListener("submit", sendChat);
  if (dom.chatInput) {
    dom.chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        sendChat(event);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && dom.chatDrawer && !dom.chatDrawer.hidden) {
      closeChat();
    }
  });

  loadReports();
  loadInventory();
  // Fast enough for the transfer mark to track gNMI activity; inventory is
  // served from memory, so this is cheap.
  setInterval(loadInventory, 1000);
})();
