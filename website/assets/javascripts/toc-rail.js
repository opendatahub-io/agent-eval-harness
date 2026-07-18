/*
 * "Clerk"-style table-of-contents rail (Fumadocs / cuga.dev inspired).
 *
 * Draws a faint vertical rail down the right-hand TOC that DENTS to follow the
 * heading hierarchy — top-level (h2) entries sit at one x, nested (h3/h4)
 * entries step inward, with rounded jog connectors. An accent "thumb" rides the
 * rail at the active section's position and column, sliding as you scroll.
 *
 * We compute the active heading OURSELVES from geometry rather than following
 * Material's `.md-nav__link--active`, which is unreliable at the page bottom: it
 * skips trailing sections that can't scroll to the top and, on direct #anchor
 * navigation to a near-bottom section, marks the NEXT heading active. Our
 * scroll-spy (a) redistributes trailing headings that can't reach the reading
 * line across the final scroll band so every section gets a turn (the last
 * activating exactly at the bottom), and (b) honors the URL hash after #anchor
 * navigation until the user scrolls away.
 *
 * Instant-navigation safe (document$ + DOM-ready fallback, idempotent, window
 * listeners wired once), rAF-throttled, honors prefers-reduced-motion.
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

  // The heading a TOC link points at (via its #fragment).
  function headingFor(link) {
    var href = link.getAttribute("href") || "";
    var i = href.indexOf("#");
    if (i < 0) return null;
    var id = href.slice(i + 1);
    if (!id) return null;
    try { return document.getElementById(decodeURIComponent(id)); }
    catch (e) { return document.getElementById(id); }
  }

  // Reading line: where a section "becomes current". Align it just BELOW where
  // #anchor navigation lands a heading, so a clicked/navigated section counts as
  // "reached" by the scroll-spy even when the hash pin has been dropped (e.g. by
  // trackpad momentum). Otherwise a heading that lands a few px below the line
  // reads as the PREVIOUS section (landing at ~68 with a line at 60 highlights
  // the section above the one you navigated to). Material sets scroll-margin-top
  // (default --md-scroll-margin: 3.6rem) only on the :target, so read it there
  // when present, else resolve the default via the root font size.
  function readingOffset() {
    var rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    var landing = 3.6 * rem;
    var tgt = document.querySelector(".md-content :target");
    if (tgt) { var s = parseFloat(getComputedStyle(tgt).scrollMarginTop); if (s) landing = s; }
    var hdr = document.querySelector(".md-header");
    var floor = (hdr ? hdr.getBoundingClientRect().height : 48) + 12;
    return Math.max(floor, landing + 6);
  }

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
    var currentHl = null;   // the single TOC item colored as highlighted
    var pinned = null;      // heading id honored after #anchor navigation
    var pinnedAt = 0;       // when the pin was set (to ignore momentum scroll)
    var pinBaseY = null;    // scroll baseline; pin releases when user scrolls off it
    var actScroll = [];     // per-link absolute scrollY at which it activates

    // Absolute scrollY at which each section becomes current. Sections near the
    // bottom can't scroll their heading up to the reading line, so their natural
    // activation points pile up against the max scroll and collapse to tiny (or
    // zero) windows — e.g. a section reachable by only 11px before the page
    // bottoms out would never be highlightable while scrolling. Walk from the
    // bottom and guarantee each section a minimum window, with the last
    // activating exactly at the bottom, so every section gets a fair, reachable
    // slice. For well-spaced sections this is a no-op.
    function computeActivation() {
      var off = readingOffset();
      var maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      var raw = links.map(function (a) {
        var h = headingFor(a);
        if (!h) return null;
        var abs = h.getBoundingClientRect().top + window.scrollY;
        return Math.max(0, abs - off);
      });
      if (maxScroll > 1) {
        var MIN = 72;         // smallest scroll window a section may occupy
        var upper = maxScroll; // top of the current section's window
        for (var i = raw.length - 1; i >= 0; i--) {
          if (raw[i] == null) continue;
          var cap = upper - MIN; // must start >= MIN below its window's top
          if (raw[i] > cap) raw[i] = cap;
          if (raw[i] < 0) raw[i] = 0;
          upper = raw[i];
        }
      }
      actScroll = raw;
    }

    // Index of the last section whose activation scroll we've passed (-1 = above
    // the first, i.e. at the very top).
    function spyIndex() {
      var y = window.scrollY;
      var chosen = -1;
      for (var i = 0; i < actScroll.length; i++) {
        if (actScroll[i] == null) continue;
        if (y + 1 >= actScroll[i]) chosen = i; else break;
      }
      return chosen;
    }

    function activeLink() {
      if (pinned) {
        for (var i = 0; i < links.length; i++) {
          var h = headingFor(links[i]);
          if (h && h.id === pinned) return links[i];
        }
        pinned = null; // hash points at nothing in this TOC — drop it
      }
      var idx = spyIndex();
      return idx < 0 ? links[0] : links[idx];
    }

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
      computeActivation();
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
      var link = activeLink();
      if (!link) { hlPath.style.opacity = "0"; return; }
      // Color the text of EXACTLY this item — the single source of truth for
      // "highlighted". Material's own active text color is neutralized in CSS,
      // so no stale active item can stay red alongside the one we pick. Only
      // mutate on change to avoid a MutationObserver-style loop.
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

    function setPinFromHash() {
      var h = location.hash ? location.hash.slice(1) : "";
      if (h) { try { h = decodeURIComponent(h); } catch (e) {} }
      pinned = h || null;
      if (pinned) { pinnedAt = Date.now(); pinBaseY = null; }
    }

    var pending = false;
    function scheduleThumb() {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; if (list.isConnected) positionHighlight(); });
    }

    // Exposed to the shared, once-only window listeners.
    list.__toc = {
      rebuild: buildRail,
      // The hash pin is released only when the user genuinely scrolls the page
      // AWAY from where the anchor landed — keyed on scroll POSITION, not wheel
      // events. So trackpad momentum, and "scroll down" while already at the
      // bottom (which moves nothing, fires no scroll), never drop it — the case
      // where a near-bottom anchor would otherwise flip to the last section. A
      // 700ms guard lets the jump + momentum settle; during it we track the
      // landing, then release once the user moves off that baseline.
      onScroll: function () {
        if (pinned !== null) {
          if (Date.now() - pinnedAt < 700) {
            pinBaseY = window.scrollY;
          } else if (pinBaseY === null) {
            pinBaseY = window.scrollY;
          } else if (Math.abs(window.scrollY - pinBaseY) > 24) {
            pinned = null;
          }
        }
        scheduleThumb();
      },
      syncHash: function () { setPinFromHash(); scheduleThumb(); },
    };

    setPinFromHash();       // honor an #anchor we loaded on
    buildRail();
    setTimeout(buildRail, 400); // fonts/late layout can shift positions

    // Content height can change without a window resize (late images, content
    // tabs, collapsible admonitions), which would stale the activation array.
    // Recompute when the content box changes size. (buildRail only mutates the
    // TOC's own SVG, so this can't feed back into a loop.)
    if (window.ResizeObserver) {
      var roPending = false;
      var ro = new ResizeObserver(function () {
        if (roPending) return;
        roPending = true;
        requestAnimationFrame(function () { roPending = false; if (list.isConnected) buildRail(); });
      });
      ro.observe(document.querySelector(".md-content") || document.body);
    }
  }

  function eachList(fn) {
    document.querySelectorAll('.md-sidebar--secondary [data-md-component="toc"]').forEach(function (list) {
      if (list.__toc) { try { fn(list.__toc); } catch (e) {} }
    });
  }

  // Window listeners wired ONCE (instant navigation replaces the TOC element,
  // so stale lists simply drop out of the query — no per-page listener leak).
  var wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    window.addEventListener("scroll", function () { eachList(function (t) { t.onScroll(); }); }, { passive: true });
    window.addEventListener("resize", function () { eachList(function (t) { t.rebuild(); }); }, { passive: true });
    window.addEventListener("hashchange", function () { eachList(function (t) { t.syncHash(); }); });
  }

  function initAll() {
    document.querySelectorAll('.md-sidebar--secondary [data-md-component="toc"]').forEach(function (list) {
      try { setup(list); } catch (e) { console.error("toc-rail: setup failed", e); }
    });
    wire();
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initAll);
  }
  if (document.readyState !== "loading") initAll();
  else document.addEventListener("DOMContentLoaded", initAll);
})();
