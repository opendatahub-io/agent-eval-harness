/*
 * Smart table code wrapping.
 *
 * Base CSS keeps inline code in tables whole (word-break: keep-all), so short
 * identifiers never split mid-word and the column auto-sizes to fit them. But a
 * name too long for its column would then break mid-word via the overflow-wrap
 * fallback (e.g. "agent-eval-harne" / "ss"). For exactly those cells — the ones
 * that STILL don't fit on one line — we inject <wbr> break opportunities after
 * separators (. _ / : @ -) so they wrap at a separator instead. Code that
 * already fits on one line is left untouched (stays whole).
 *
 * Whether a cell fits depends on the column widths, so we re-run on instant
 * navigation and (rAF-coalesced) on resize. Idempotent via a data flag; <wbr>
 * is zero-width and copies as nothing.
 */
(function () {
  "use strict";
  var SEL = '.md-typeset table:not([class]) code';
  var DELIM = /([._/:@-])/g; // insert a break opportunity AFTER each of these
  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  function process() {
    document.querySelectorAll(SEL).forEach(function (c) {
      if (c.dataset.wbr === "1") return;                  // already has break points
      if (c.children.length || c.closest("pre")) return;  // skip highlighted / block code
      if (c.getClientRects().length <= 1) return;         // fits on one line -> keep whole
      var text = c.textContent;
      var html = esc(text).replace(DELIM, "$1<wbr>");
      if (html === esc(text)) return;                     // no separators to break at
      c.innerHTML = html;
      c.dataset.wbr = "1";
    });
  }

  var raf = 0;
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      try { process(); } catch (e) { console.error("table-wrap: failed", e); }
    });
  }

  if (window.document$ && typeof window.document$.subscribe === "function") window.document$.subscribe(schedule);
  if (document.readyState !== "loading") schedule();
  else document.addEventListener("DOMContentLoaded", schedule);
  window.addEventListener("resize", schedule, { passive: true });
})();
