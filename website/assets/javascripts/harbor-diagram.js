/*
 * Interactive Harbor-on-Kubernetes eval diagram.
 *
 * Self-contained, dependency-free vanilla JS + SVG. Renders into any
 * <div class="harbor-diagram"></div> on the page. Shows the eval flow:
 * an orchestrator (`harbor run`) driving N parallel trial pods through the
 * exec back-and-forth (launch -> setup -> run -> test -> collect -> score),
 * with animated particles along the connectors.
 *
 * Theme-aware (colors come from CSS variables in stylesheets/diagrams.css),
 * accessible (tablist stepper, keyboard, aria-live caption), respects
 * prefers-reduced-motion, and re-initialises under MkDocs Material's
 * instant navigation via the document$ observable.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var VW = 1040, VH = 500;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Node layout (in viewBox units) -------------------------------------
  var N = {
    orch:      { x: 20,  y: 196, w: 188, h: 112, kind: "orch",
                 title: "Orchestrator", sub: ["harbor run · workstation / CI", "Harbor Job API — concurrency N"] },
    cluster:   { x: 250, y: 20,  w: 566, h: 462, kind: "cluster", title: "OpenShift / Kubernetes cluster" },
    gateway:   { x: 300, y: 56,  w: 466, h: 44,  kind: "gateway",
                 title: "LiteLLM gateway", sub: ["model inference · HTTP (in-cluster)"] },
    pod1:      { x: 300, y: 116, w: 300, h: 90,  kind: "pod", title: "Trial pod — case-001" },
    pod2:      { x: 300, y: 214, w: 300, h: 90,  kind: "pod", title: "Trial pod — case-002" },
    pod3:      { x: 300, y: 312, w: 300, h: 90,  kind: "pod", title: "Trial pod — case-003" },
    resources: { x: 300, y: 420, w: 466, h: 40,  kind: "res",
                 title: "Secret (envFrom) · ConfigMap (project + eval.yaml)" },
    report:    { x: 858, y: 168, w: 162, h: 96,  kind: "report",
                 title: "Score → report", sub: ["summary.yaml", "report.html"] },
    mlflow:    { x: 858, y: 300, w: 162, h: 60,  kind: "mlflow",
                 title: "MLflow", sub: ["experiment tracking"] },
  };
  var PODS = ["pod1", "pod2", "pod3"];

  // ---- Edges: from-node/side -> to-node/side ------------------------------
  // side: l/r/t/b ; frac positions the anchor along that side (0..1)
  var EDGES = [
    { id: "o1", a: ["orch", "r", 0.5], b: ["pod1", "l", 0.5], o: "h" },
    { id: "o2", a: ["orch", "r", 0.5], b: ["pod2", "l", 0.5], o: "h" },
    { id: "o3", a: ["orch", "r", 0.5], b: ["pod3", "l", 0.5], o: "h" },
    { id: "g1", a: ["pod1", "t", 0.5], b: ["gateway", "b", 0.30], o: "v" },
    { id: "g2", a: ["pod2", "t", 0.5], b: ["gateway", "b", 0.50], o: "v" },
    { id: "g3", a: ["pod3", "t", 0.5], b: ["gateway", "b", 0.70], o: "v" },
    { id: "r1", a: ["pod1", "r", 0.5], b: ["report", "l", 0.30], o: "h" },
    { id: "r2", a: ["pod2", "r", 0.5], b: ["report", "l", 0.50], o: "h" },
    { id: "r3", a: ["pod3", "r", 0.5], b: ["report", "l", 0.70], o: "h" },
    { id: "rm", a: ["report", "b", 0.5], b: ["mlflow", "t", 0.5], o: "v" },
  ];

  // ---- Steps --------------------------------------------------------------
  // status: per-pod pill text; row: which pod row to highlight (agent|verifier)
  var STEPS = [
    { key: "launch", label: "Launch", status: "Pending", row: null,
      nodes: ["orch", "cluster", "pod1", "pod2", "pod3", "resources"],
      edges: ["o1", "o2", "o3"], flow: [["o1", 1], ["o2", 1], ["o3", 1]],
      caption: "harbor run creates one pod per test case via KubernetesEnvironment — N in parallel (-n). Each pod pulls the agent-eval-harness image; the credentials Secret is injected with envFrom and the project ConfigMap is mounted." },
    { key: "setup", label: "Setup", status: "Setup", row: null,
      nodes: ["orch", "pod1", "pod2", "pod3"],
      edges: ["o1", "o2", "o3"], flow: [["o1", 1], ["o2", 1], ["o3", 1]],
      caption: "The harness uploads each task's environment/ (input.yaml, hooks, .claude/settings.json) into the pod workspace as tar + base64 chunks streamed over the Kubernetes exec websocket (30s keepalive)." },
    { key: "run", label: "Run agent", status: "Running", row: "agent",
      nodes: ["pod1", "pod2", "pod3", "gateway"],
      edges: ["g1", "g2", "g3"], flow: [["g1", 1], ["g2", 1], ["g3", 1]],
      caption: "Harbor execs the agent (e.g. claude-code) with instruction.md as the prompt. The agent runs the skill or prompt inside the pod, calling the model through the in-cluster LiteLLM gateway over HTTP." },
    { key: "test", label: "Test", status: "Verifying", row: "verifier",
      nodes: ["pod1", "pod2", "pod3"], edges: [], flow: [],
      caption: "Harbor uploads tests/ and execs test.sh → python3 -m agent_eval.harbor.reward, which runs the same judges as a local run and writes reward.json + judges.json inside each pod." },
    { key: "collect", label: "Collect", status: "Done", row: null,
      nodes: ["pod1", "pod2", "pod3", "report"],
      edges: ["r1", "r2", "r3"], flow: [["r1", 1], ["r2", 1], ["r3", 1]],
      caption: "The harness downloads each pod's outputs — agent transcript & trajectory, reward.json, judges.json, artifacts — via tar + base64 over exec, then tears the pods down (unless KEEP_RUN=1)." },
    { key: "score", label: "Score & report", status: "Done", row: null,
      nodes: ["report", "mlflow"], edges: ["rm"], flow: [["rm", 1]],
      caption: "Per-case rewards and judge scores are aggregated into summary.yaml + report.html; suite-level regression thresholds are checked and results are logged to MLflow." },
  ];

  // ---- Small helpers ------------------------------------------------------
  function mk(tag, attrs, parent) {
    var e = document.createElementNS(SVGNS, tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function html(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function anchor(spec) {
    var n = N[spec[0]], side = spec[1], f = spec[2];
    if (side === "l") return { x: n.x, y: n.y + n.h * f };
    if (side === "r") return { x: n.x + n.w, y: n.y + n.h * f };
    if (side === "t") return { x: n.x + n.w * f, y: n.y };
    return { x: n.x + n.w * f, y: n.y + n.h }; // b
  }
  function edgeD(e) {
    var a = anchor(e.a), b = anchor(e.b);
    if (e.o === "h") {
      var mx = (a.x + b.x) / 2;
      return "M " + a.x + " " + a.y + " C " + mx + " " + a.y + ", " + mx + " " + b.y + ", " + b.x + " " + b.y;
    }
    var my = (a.y + b.y) / 2;
    return "M " + a.x + " " + a.y + " C " + a.x + " " + my + ", " + b.x + " " + my + ", " + b.x + " " + b.y;
  }

  // ---- Build one diagram --------------------------------------------------
  function build(container) {
    container.textContent = "";
    container.classList.add("hd-ready");

    var stage = html("div", "hd-stage", container);
    var svg = mk("svg", { viewBox: "0 0 " + VW + " " + VH, role: "img",
      "aria-label": "Harbor eval flow: an orchestrator driving parallel Kubernetes trial pods through setup, run, test, and collect." }, stage);

    // defs: arrowhead marker (color follows the path stroke via context-stroke)
    var defs = mk("defs", null, svg);
    var marker = mk("marker", { id: "hd-arrow-" + uid(), markerWidth: 8, markerHeight: 8,
      refX: 6.5, refY: 3.5, orient: "auto", markerUnits: "userSpaceOnUse" }, defs);
    mk("path", { d: "M0,0 L7,3.5 L0,7 Z", class: "hd-arrowhead" }, marker);
    var markerRef = "url(#" + marker.id + ")";

    var layers = {
      edges: mk("g", { class: "hd-edges" }, svg),
      particles: mk("g", { class: "hd-particles" }, svg),
      nodes: mk("g", { class: "hd-nodes" }, svg),
    };

    // cluster boundary first (behind everything inside it)
    drawNode(layers.nodes, "cluster");

    // edges
    var edgeEls = {};
    EDGES.forEach(function (e) {
      var p = mk("path", { d: edgeD(e), class: "hd-edge", "marker-end": markerRef, "data-edge": e.id }, layers.edges);
      edgeEls[e.id] = p;
    });

    // nodes (skip cluster, already drawn)
    var nodeEls = {};
    Object.keys(N).forEach(function (id) {
      if (id === "cluster") return;
      nodeEls[id] = drawNode(layers.nodes, id);
    });

    // ---- controls ----
    var controls = html("div", "hd-controls", container);

    var tablist = html("div", "hd-steps", controls);
    tablist.setAttribute("role", "tablist");
    tablist.setAttribute("aria-label", "Harbor eval steps");
    var tabs = STEPS.map(function (s, i) {
      var b = html("button", "hd-step", tablist);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", i === 0 ? "true" : "false");
      b.tabIndex = i === 0 ? 0 : -1;
      var num = html("span", "hd-step-num", b); num.textContent = String(i + 1);
      var lab = html("span", "hd-step-label", b); lab.textContent = s.label;
      b.addEventListener("click", function () { stopPlay(); go(i); });
      return b;
    });

    var bar = html("div", "hd-bar", controls);
    var prev = html("button", "hd-btn hd-prev", bar); prev.type = "button";
    prev.setAttribute("aria-label", "Previous step"); prev.textContent = "‹ Prev";
    var counter = html("span", "hd-counter", bar);
    var next = html("button", "hd-btn hd-next", bar); next.type = "button";
    next.setAttribute("aria-label", "Next step"); next.textContent = "Next ›";
    var play = html("button", "hd-btn hd-play", bar); play.type = "button";
    play.setAttribute("aria-pressed", "false");
    play.textContent = reduce ? "▶ Step through" : "▶ Simulate";

    var caption = html("p", "hd-caption", controls);
    caption.setAttribute("role", "status");
    caption.setAttribute("aria-live", "polite");

    prev.addEventListener("click", function () { stopPlay(); go(cur - 1); });
    next.addEventListener("click", function () { stopPlay(); go(cur + 1); });
    play.addEventListener("click", togglePlay);
    tablist.addEventListener("keydown", onKey);

    // ---- state ----
    var cur = -1, gen = 0, playing = false, playTimer = null, autostarted = false;

    function drawNode(parent, id) {
      var n = N[id];
      var g = mk("g", { class: "hd-node hd-" + n.kind, "data-node": id }, parent);
      mk("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx: n.kind === "cluster" ? 14 : 9, class: "hd-box" }, g);
      if (n.kind === "cluster") {
        var ct = mk("text", { x: n.x + 14, y: n.y + 22, class: "hd-cluster-title" }, g);
        ct.textContent = n.title;
        return g;
      }
      if (n.kind === "res") {
        var rt = mk("text", { x: n.x + n.w / 2, y: n.y + n.h / 2 + 4, "text-anchor": "middle", class: "hd-res-title" }, g);
        rt.textContent = n.title;
        return g;
      }
      // title
      var t = mk("text", { x: n.x + 14, y: n.y + 24, class: "hd-title" }, g);
      t.textContent = n.title;
      // sub lines
      (n.sub || []).forEach(function (line, i) {
        var st = mk("text", { x: n.x + 14, y: n.y + 44 + i * 16, class: "hd-sub" }, g);
        st.textContent = line;
      });
      if (n.kind === "pod") {
        // status pill
        var pill = mk("g", { class: "hd-pill" }, g);
        mk("rect", { x: n.x + n.w - 96, y: n.y + 12, width: 84, height: 22, rx: 11, class: "hd-pill-box" }, pill);
        var pt = mk("text", { x: n.x + n.w - 54, y: n.y + 27, "text-anchor": "middle", class: "hd-pill-text" }, pill);
        pt.textContent = "—";
        g._pill = pt; g._pillbox = pill;
        // rows
        var rowA = mk("g", { class: "hd-row", "data-row": "agent" }, g);
        mk("circle", { cx: n.x + 22, cy: n.y + 52, r: 4, class: "hd-dot" }, rowA);
        var ra = mk("text", { x: n.x + 34, y: n.y + 56, class: "hd-row-text" }, rowA);
        ra.textContent = "agent · claude-code";
        var rowV = mk("g", { class: "hd-row", "data-row": "verifier" }, g);
        mk("circle", { cx: n.x + 22, cy: n.y + 74, r: 4, class: "hd-dot" }, rowV);
        var rv = mk("text", { x: n.x + 34, y: n.y + 78, class: "hd-row-text" }, rowV);
        rv.textContent = "verifier · reward.py → reward.json";
        g._rows = { agent: rowA, verifier: rowV };
      }
      return g;
    }

    function clearActive() {
      Object.keys(nodeEls).forEach(function (id) { nodeEls[id].classList.remove("is-active"); });
      EDGES.forEach(function (e) { edgeEls[e.id].classList.remove("is-active"); });
      PODS.forEach(function (id) {
        var g = nodeEls[id];
        if (g._rows) { g._rows.agent.classList.remove("is-active"); g._rows.verifier.classList.remove("is-active"); }
      });
    }

    function go(i) {
      if (i < 0 || i >= STEPS.length) return;
      cur = i; gen++;
      var s = STEPS[i];
      clearActive();
      s.nodes.forEach(function (id) { if (nodeEls[id]) nodeEls[id].classList.add("is-active"); });
      s.edges.forEach(function (id) { edgeEls[id].classList.add("is-active"); });
      // pod status + rows
      var done = s.key === "collect" || s.key === "score";
      PODS.forEach(function (id) {
        var g = nodeEls[id];
        if (!g._pill) return;
        g._pill.textContent = s.status;
        g._pillbox.classList.toggle("is-done", done);
        g._pillbox.classList.toggle("is-run", s.key === "run" || s.key === "test" || s.key === "setup");
        if (s.row && g._rows[s.row]) g._rows[s.row].classList.add("is-active");
      });
      // tabs
      tabs.forEach(function (t, ti) {
        var sel = ti === i;
        t.setAttribute("aria-selected", sel ? "true" : "false");
        t.tabIndex = sel ? 0 : -1;
      });
      counter.textContent = (i + 1) + " / " + STEPS.length;
      caption.textContent = s.caption;
      prev.disabled = i === 0;
      next.disabled = i === STEPS.length - 1;
      // particles
      if (!reduce) runFlow(s.flow, gen);
    }

    function runFlow(flow, myGen) {
      if (!flow || !flow.length) return;
      flow.forEach(function (f, idx) {
        var path = edgeEls[f[0]];
        if (path) animate(path, f[1], idx * 90, myGen);
      });
    }

    function animate(path, dir, delay, myGen) {
      var len = path.getTotalLength();
      var count = 3, dur = 1100;
      var dots = [];
      for (var i = 0; i < count; i++) {
        dots.push(mk("circle", { r: 2.6, class: "hd-particle", cx: -10, cy: -10, opacity: 0 }, layers.particles));
      }
      var start = null;
      function frame(ts) {
        if (myGen !== gen || !container.isConnected) { dots.forEach(function (d) { d.remove(); }); return; }
        if (start === null) start = ts + delay;
        var base = (ts - start) / dur;
        dots.forEach(function (d, i) {
          var p = base - i * 0.24;
          p = p - Math.floor(p); // loop 0..1
          if (base - i * 0.24 < 0) { d.setAttribute("opacity", 0); return; }
          var pos = dir > 0 ? p : 1 - p;
          var pt = path.getPointAtLength(pos * len);
          d.setAttribute("cx", pt.x); d.setAttribute("cy", pt.y);
          d.setAttribute("opacity", (Math.sin(p * Math.PI) * 0.9).toFixed(3));
        });
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }

    function onKey(ev) {
      var i = STEPS.findIndex(function (_, ti) { return tabs[ti].getAttribute("aria-selected") === "true"; });
      if (ev.key === "ArrowRight" || ev.key === "ArrowDown") { ev.preventDefault(); stopPlay(); go(Math.min(cur + 1, STEPS.length - 1)); tabs[cur].focus(); }
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") { ev.preventDefault(); stopPlay(); go(Math.max(cur - 1, 0)); tabs[cur].focus(); }
      else if (ev.key === "Home") { ev.preventDefault(); stopPlay(); go(0); tabs[0].focus(); }
      else if (ev.key === "End") { ev.preventDefault(); stopPlay(); go(STEPS.length - 1); tabs[cur].focus(); }
    }

    function togglePlay() { playing ? stopPlay() : startPlay(); }
    function startPlay() {
      playing = true; play.setAttribute("aria-pressed", "true"); play.textContent = "■ Stop";
      if (cur >= STEPS.length - 1) go(0);
      schedule();
    }
    function schedule() {
      playTimer = window.setTimeout(function () {
        if (!playing) return;
        if (cur >= STEPS.length - 1) { stopPlay(); return; }
        go(cur + 1); schedule();
      }, 2600);
    }
    function stopPlay() {
      playing = false; if (playTimer) { clearTimeout(playTimer); playTimer = null; }
      play.setAttribute("aria-pressed", "false");
      play.textContent = reduce ? "▶ Step through" : (cur >= STEPS.length - 1 ? "↺ Replay" : "▶ Simulate");
    }

    go(0);

    // Autoplay once when scrolled into view (unless reduced motion).
    if (!reduce && "IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting && !autostarted) { autostarted = true; startPlay(); io.disconnect(); }
        });
      }, { threshold: 0.4 });
      io.observe(container);
    }
  }

  var _uid = 0;
  function uid() { return (_uid++) + "-" + Math.floor(performance.now()); }

  function initAll() {
    var list = document.querySelectorAll(".harbor-diagram:not(.hd-ready)");
    for (var i = 0; i < list.length; i++) {
      try { build(list[i]); } catch (err) { console.error("harbor-diagram: build failed", err); }
    }
  }

  // MkDocs Material instant navigation: document$ emits on every page load.
  // Also attach a DOM-ready fallback (idempotent via the :not(.hd-ready) guard)
  // in case document$ is unavailable or errors are swallowed by the observable.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initAll);
  }
  if (document.readyState !== "loading") {
    initAll();
  } else {
    document.addEventListener("DOMContentLoaded", initAll);
  }
})();
