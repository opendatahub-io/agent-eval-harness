/*
 * Ambient "eval loop" hero diagram.
 *
 * Self-contained, dependency-free vanilla JS + SVG. Renders into any
 * <div class="loop-diagram"></div>. Shows the six-stage loop as a left-to-right
 * pipeline (Analyze -> Dataset -> Run -> Review -> Trace -> Optimize) that
 * closes back on itself via a feedback arc from Optimize to Analyze — the
 * "measurable AND improvable" story from the hero, made visual.
 *
 * A soft spotlight walks the stages; particles flow along the connector into
 * each newly-active stage, and around the feedback arc when the loop closes.
 *
 * Theme-aware (colors come from CSS variables in stylesheets/diagrams.css),
 * accessible (role="img" with a descriptive label; the page's stage cards are
 * the textual equivalent), respects prefers-reduced-motion, pauses when
 * off-screen, and re-initialises under MkDocs Material's instant navigation
 * via the document$ observable.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var VW = 1120, VH = 300;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Stages -------------------------------------------------------------
  var STAGES = [
    { key: "analyze",  label: "Analyze",  cmd: "/eval-analyze" },
    { key: "dataset",  label: "Dataset",  cmd: "/eval-dataset" },
    { key: "run",      label: "Run & judge", cmd: "/eval-run" },
    { key: "review",   label: "Review",   cmd: "/eval-review" },
    { key: "trace",    label: "Trace",    cmd: "/eval-mlflow" },
    { key: "optimize", label: "Optimize", cmd: "/eval-optimize" },
  ];

  // ---- Layout (in viewBox units) ------------------------------------------
  var M = 28, BW = 156, BH = 96, BY = 60;
  var GAP = (VW - 2 * M - STAGES.length * BW) / (STAGES.length - 1);
  function nodeX(i) { return M + i * (BW + GAP); }
  var MIDY = BY + BH / 2;          // connector height (box vertical center)
  var ARC_Y = 250;                 // feedback arc low point

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

  var _uid = 0;
  function uid() { return (_uid++) + "-" + Math.floor(performance.now()); }

  // ---- Build one diagram --------------------------------------------------
  function build(container) {
    container.textContent = "";
    container.classList.add("ld-ready");

    var label = "The evaluation loop: " +
      STAGES.map(function (s) { return s.label; }).join(", then ") +
      ", then optimize feeds back to analyze — a closed, repeatable loop.";

    var svg = mk("svg", {
      viewBox: "0 0 " + VW + " " + VH, role: "img", "aria-label": label,
    }, container);

    // defs: arrowhead marker (color follows the path stroke via context-stroke)
    var defs = mk("defs", null, svg);
    var mId = "ld-arrow-" + uid();
    var marker = mk("marker", {
      id: mId, markerWidth: 9, markerHeight: 9, refX: 6.5, refY: 3.5,
      orient: "auto", markerUnits: "userSpaceOnUse",
    }, defs);
    mk("path", { d: "M0,0 L7,3.5 L0,7 Z", class: "ld-arrowhead" }, marker);
    var markerRef = "url(#" + mId + ")";

    var layers = {
      edges: mk("g", { class: "ld-edges" }, svg),
      particles: mk("g", { class: "ld-particles" }, svg),
      nodes: mk("g", { class: "ld-nodes" }, svg),
    };

    // ---- feedback arc (behind the boxes): Optimize -> Analyze -----------
    var xFirst = nodeX(0) + BW / 2;
    var xLast = nodeX(STAGES.length - 1) + BW / 2;
    var arcD = "M " + xLast + " " + (BY + BH) +
               " C " + xLast + " " + ARC_Y + ", " + xFirst + " " + ARC_Y +
               ", " + xFirst + " " + (BY + BH);
    var arc = mk("path", {
      d: arcD, class: "ld-arc", "marker-end": markerRef,
    }, layers.edges);
    var arcLabel = mk("text", {
      x: VW / 2, y: ARC_Y + 22, "text-anchor": "middle", class: "ld-arc-label",
    }, layers.edges);
    arcLabel.textContent = "re-run · compare · keep only real gains";

    // ---- connectors (straight, between consecutive boxes) --------------
    var edgeEls = [];
    for (var i = 0; i < STAGES.length - 1; i++) {
      var x1 = nodeX(i) + BW, x2 = nodeX(i + 1);
      var p = mk("path", {
        d: "M " + x1 + " " + MIDY + " L " + x2 + " " + MIDY,
        class: "ld-edge", "marker-end": markerRef,
      }, layers.edges);
      edgeEls.push(p);
    }

    // ---- nodes ---------------------------------------------------------
    var nodeEls = STAGES.map(function (s, i) {
      var x = nodeX(i);
      var g = mk("g", { class: "ld-node ld-" + s.key, "data-stage": i }, layers.nodes);
      mk("rect", { x: x, y: BY, width: BW, height: BH, rx: 12, class: "ld-box" }, g);
      // number badge
      mk("circle", { cx: x + 26, cy: BY + 30, r: 14, class: "ld-badge" }, g);
      var bn = mk("text", {
        x: x + 26, y: BY + 35, "text-anchor": "middle", class: "ld-badge-num",
      }, g);
      bn.textContent = String(i + 1);
      // title
      var t = mk("text", { x: x + 50, y: BY + 36, class: "ld-title" }, g);
      t.textContent = s.label;
      // command
      var c = mk("text", { x: x + 20, y: BY + 70, class: "ld-cmd" }, g);
      c.textContent = s.cmd;
      return g;
    });

    // ---- caption (static, below the diagram) ---------------------------
    var caption = html("p", "ld-caption", container);
    caption.textContent =
      "One eval.yaml drives every stage. Only analyze → dataset → run are required for a first score.";

    // ---- animation -----------------------------------------------------
    var cur = -1, gen = 0, timer = null, running = false;

    function setActive(i) {
      nodeEls.forEach(function (g, gi) { g.classList.toggle("is-active", gi === i); });
      edgeEls.forEach(function (e) { e.classList.remove("is-active"); });
      arc.classList.remove("is-active");
      if (i > 0 && i < STAGES.length && edgeEls[i - 1]) edgeEls[i - 1].classList.add("is-active");
    }

    function flowAlong(path, delay, myGen) {
      if (reduce || !path) return;
      var len = path.getTotalLength();
      var count = 3, dur = 900;
      var dots = [];
      for (var i = 0; i < count; i++) {
        dots.push(mk("circle", { r: 2.8, class: "ld-particle", cx: -10, cy: -10, opacity: 0 }, layers.particles));
      }
      var start = null;
      function frame(ts) {
        if (myGen !== gen || !container.isConnected) { dots.forEach(function (d) { d.remove(); }); return; }
        if (start === null) start = ts + delay;
        var base = (ts - start) / dur;
        if (base > 1.35) { dots.forEach(function (d) { d.remove(); }); return; }
        dots.forEach(function (d, i) {
          var p = base - i * 0.16;
          if (p < 0 || p > 1) { d.setAttribute("opacity", 0); return; }
          var pt = path.getPointAtLength(p * len);
          d.setAttribute("cx", pt.x);
          d.setAttribute("cy", pt.y);
          d.setAttribute("opacity", (Math.sin(p * Math.PI) * 0.95).toFixed(3));
        });
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }

    // A "tick" advances the spotlight. Index STAGES.length is the feedback
    // phase: the arc lights up and a particle rides it back to Analyze.
    function tick() {
      gen++;
      cur = (cur + 1) % (STAGES.length + 1);
      if (cur < STAGES.length) {
        setActive(cur);
        if (cur > 0) flowAlong(edgeEls[cur - 1], 0, gen);
      } else {
        // feedback phase
        nodeEls.forEach(function (g) { g.classList.remove("is-active"); });
        edgeEls.forEach(function (e) { e.classList.remove("is-active"); });
        arc.classList.add("is-active");
        flowAlong(arc, 0, gen);
      }
    }

    function start() {
      if (running || reduce) return;
      running = true;
      timer = window.setInterval(tick, 1550);
    }
    function stop() {
      running = false;
      if (timer) { window.clearInterval(timer); timer = null; }
    }

    // First frame: highlight Analyze so the diagram reads even before motion.
    setActive(0);
    cur = 0;

    if (reduce || !("IntersectionObserver" in window)) {
      // Static: leave stage 1 highlighted, arc + connectors visible.
      return;
    }

    // Run only while in view (perf + politeness); the interval is cheap but a
    // perpetual hero animation off-screen is wasteful.
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!container.isConnected) { stop(); io.disconnect(); return; }
        if (en.isIntersecting) start(); else stop();
      });
    }, { threshold: 0.25 });
    io.observe(container);
  }

  function initAll() {
    var list = document.querySelectorAll(".loop-diagram:not(.ld-ready)");
    for (var i = 0; i < list.length; i++) {
      try { build(list[i]); } catch (err) { console.error("loop-diagram: build failed", err); }
    }
  }

  // MkDocs Material instant navigation: document$ emits on every page load.
  // DOM-ready fallback stays idempotent via the :not(.ld-ready) guard.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initAll);
  }
  if (document.readyState !== "loading") {
    initAll();
  } else {
    document.addEventListener("DOMContentLoaded", initAll);
  }
})();
