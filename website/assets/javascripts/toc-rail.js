/*
 * "Clerk"-style table-of-contents rail (Fumadocs / cuga.dev inspired).
 *
 * Draws a faint vertical rail down the right-hand TOC that DENTS to follow the
 * heading hierarchy — top-level (h2) entries sit at one x, nested (h3/h4)
 * entries step inward, with rounded jog connectors. An accent "thumb" rides the
 * rail at the active section's position and column, sliding as you scroll.
 *
 * MkDocs Material marks the active entry with `.md-nav__link--active`.
 * Instant-navigation safe (document$ + DOM-ready fallback, idempotent),
 * rAF-throttled, and honors prefers-reduced-motion.
 */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var X0 = 3;   // rail x for top-level entries
  var X1 = 14;  // rail x for nested entries (2 stops only, like the reference)
  var INSET = 3; // trim the rail a little inside each entry's box

  function depth(link, root) {
    var d = 0, ul = link.closest(".md-nav__list");
    while (ul && ul !== root) {
      d++;
      ul = ul.parentElement ? ul.parentElement.closest(".md-nav__list") : null;
    }
    return d;
  }
  function railX(d) { return d === 0 ? X0 : X1; }

  function setup(list) {
    if (list.__railed) return;
    var links = Array.prototype.slice.call(list.querySelectorAll("a.md-nav__link"));
    if (!links.length) return;
    list.__railed = true;
    list.classList.add("md-toc-railed");

    var PILL = 20; // arc-length of the moving highlight (px)
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "md-toc-railsvg");
    svg.setAttribute("preserveAspectRatio", "none");
    var railPath = document.createElementNS(SVGNS, "path");
    railPath.setAttribute("class", "md-toc-railpath");
    // The highlight is a copy of the SAME path, revealed via stroke-dash so it
    // rides the track continuously — through the angled jog segments — as it
    // slides between sections.
    var hlPath = document.createElementNS(SVGNS, "path");
    hlPath.setAttribute("class", "md-toc-railhl");
    svg.appendChild(railPath);
    svg.appendChild(hlPath);
    list.appendChild(svg);

    var total = 0;
    var currentHl = null; // the single TOC item colored as highlighted

    function buildRail() {
      if (!list.isConnected) return;
      var lr = list.getBoundingClientRect();
      var scroll = list.scrollTop;
      var pts = links.map(function (a) {
        var r = a.getBoundingClientRect();
        return { x: railX(depth(a, list)), top: r.top - lr.top + scroll, bot: r.top - lr.top + scroll + r.height };
      });
      var H = Math.max(list.scrollHeight, lr.height);
      svg.setAttribute("viewBox", "0 0 20 " + H);
      svg.style.height = H + "px";
      var d = "";
      pts.forEach(function (it, i) {
        var t = it.top + INSET, bmid = it.bot - INSET;
        if (i === 0) {
          d += "M " + it.x + " " + t;
        } else {
          var prev = pts[i - 1];
          if (prev.x === it.x) {
            d += " L " + it.x + " " + t;
          } else {
            var my = (prev.bot - INSET + t) / 2;
            d += " C " + prev.x + " " + my + " " + it.x + " " + my + " " + it.x + " " + t;
          }
        }
        d += " L " + it.x + " " + bmid;
      });
      railPath.setAttribute("d", d);
      hlPath.setAttribute("d", d);
      total = hlPath.getTotalLength();
      // one visible dash of length PILL, then a gap covering the whole path
      hlPath.style.strokeDasharray = PILL + " " + (total + PILL);
      positionHighlight();
    }

    // Binary-search the path for the arc-length whose point is at content-y.
    // (y increases monotonically along the path.)
    function lenAtY(y) {
      var lo = 0, hi = total;
      for (var i = 0; i < 22; i++) {
        var mid = (lo + hi) / 2;
        if (hlPath.getPointAtLength(mid).y < y) lo = mid; else hi = mid;
      }
      return (lo + hi) / 2;
    }

    function positionHighlight() {
      if (!list.isConnected || !total) return;
      // Material's scroll-spy leaves nothing active above the first heading and
      // can't activate the last heading at the page bottom. Fix both edges so
      // the bar always marks the section you're actually on: first at the top,
      // last at the bottom, Material's active in between.
      var docEl = document.documentElement;
      var scrollable = docEl.scrollHeight > window.innerHeight + 8;
      var atBottom = scrollable && window.scrollY + window.innerHeight >= docEl.scrollHeight - 6;
      var link = atBottom
        ? links[links.length - 1]
        : list.querySelector("a.md-nav__link--active") || links[0];
      if (!link) { hlPath.style.opacity = "0"; return; }
      // Color the text of EXACTLY this item — the single source of truth for
      // "highlighted". Material's own active text color is neutralized in CSS,
      // so at the edges its stale active item (n-1/n-2) can't stay red alongside
      // the one we pick. Only mutate on change to avoid a MutationObserver loop.
      if (link !== currentHl) {
        if (currentHl) currentHl.classList.remove("md-toc-hl");
        link.classList.add("md-toc-hl");
        currentHl = link;
      }
      var lr = list.getBoundingClientRect();
      var ar = link.getBoundingClientRect();
      var cy = ar.top - lr.top + list.scrollTop + ar.height / 2;
      var center = lenAtY(cy);
      var off = Math.max(0, Math.min(center - PILL / 2, total - PILL));
      hlPath.style.opacity = "1";
      // negative dashoffset shifts the visible dash to start at `off`
      hlPath.style.strokeDashoffset = -off + "px";
    }

    var pending = false;
    function scheduleThumb() {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; if (list.isConnected) positionHighlight(); });
    }

    // Active entry changes as you scroll (Material toggles --active).
    new MutationObserver(scheduleThumb).observe(list, {
      subtree: true, attributes: true, attributeFilter: ["class"],
    });
    // Scroll drives the top/bottom edge cases (Material's active class doesn't
    // change there, so the observer alone wouldn't re-fire).
    window.addEventListener("scroll", scheduleThumb, { passive: true });
    window.addEventListener("resize", buildRail, { passive: true });
    // Fonts/late layout can shift positions; rebuild shortly after load.
    buildRail();
    setTimeout(buildRail, 400);
  }

  function initAll() {
    document.querySelectorAll('.md-sidebar--secondary [data-md-component="toc"]').forEach(function (list) {
      try { setup(list); } catch (e) { console.error("toc-rail: setup failed", e); }
    });
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initAll);
  }
  if (document.readyState !== "loading") initAll();
  else document.addEventListener("DOMContentLoaded", initAll);
})();
