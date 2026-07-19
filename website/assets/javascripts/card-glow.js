/*
 * Pointer-following glow for grid cards ("panes"), à la qwen.ai.
 *
 * Tracks the cursor and writes its position within the hovered card to
 * --glow-x / --glow-y CSS variables; extra.css uses those to place two
 * radial-gradient layers (a soft interior glow + a masked border highlight)
 * that fade in on hover and slide with the pointer.
 *
 * One delegated pointermove listener wired once — survives instant navigation
 * (cards get replaced, the document listener persists), rAF-coalesced, and a
 * no-op on devices without a fine pointer / with reduced motion.
 */
(function () {
  "use strict";
  if (window.__cardGlowWired) return;

  var fine = !window.matchMedia || window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!fine || reduce) return;

  window.__cardGlowWired = true;
  var SEL = ".md-typeset .grid.cards > ul > li, .md-typeset .grid.cards > ol > li, " +
            ".md-typeset .grid.cards > .card, .md-typeset .grid > .card";

  var raf = 0, pending = null;
  document.addEventListener("pointermove", function (e) {
    var card = e.target && e.target.closest ? e.target.closest(SEL) : null;
    if (!card) return;
    pending = { card: card, x: e.clientX, y: e.clientY };
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      var d = pending;
      if (!d || !d.card.isConnected) return;
      var r = d.card.getBoundingClientRect();
      d.card.style.setProperty("--glow-x", (d.x - r.left) + "px");
      d.card.style.setProperty("--glow-y", (d.y - r.top) + "px");
    });
  }, { passive: true });
})();
