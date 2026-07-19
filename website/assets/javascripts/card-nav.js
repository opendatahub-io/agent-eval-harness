/*
 * Make grid cards ("panes") fully clickable — a click anywhere on the card
 * navigates to its link — WITHOUT breaking text selection.
 *
 * A card's target is its last real link (the "→ …" call-to-action). Clicking is
 * suppressed when the user is actually selecting text (a non-empty selection, or
 * the pointer moved between mousedown and click = a drag), or when they clicked a
 * genuine interactive element (which handles itself). Modifier/middle clicks open
 * in a new tab. The real <a> stays in the DOM, so keyboard/screen-reader access
 * is unchanged; this only adds a mouse affordance.
 *
 * Delegated + wired once (survives instant navigation); cards that have a link
 * are marked .card-clickable (cursor affordance) on each page.
 */
(function () {
  "use strict";
  var SEL = ".md-typeset .grid.cards > ul > li, .md-typeset .grid.cards > ol > li, " +
            ".md-typeset .grid.cards > .card, .md-typeset .grid > .card";
  var INTERACTIVE = "a, button, input, select, textarea, label, summary, [role='button'], [onclick]";
  var DRAG = 6; // px of pointer movement that counts as a select-drag, not a click

  function targetLink(card) {
    var links = card.querySelectorAll("a[href]");
    for (var i = links.length - 1; i >= 0; i--) {
      var h = links[i].getAttribute("href");
      if (h && h.charAt(0) !== "#") return links[i]; // skip in-page anchors
    }
    return null;
  }

  function mark() {
    document.querySelectorAll(SEL).forEach(function (c) {
      if (c.__navMarked) return;
      c.__navMarked = true;
      if (targetLink(c)) c.classList.add("card-clickable");
    });
  }

  if (!window.__cardNavWired) {
    window.__cardNavWired = true;
    var downX = 0, downY = 0;
    document.addEventListener("mousedown", function (e) {
      if (e.button === 0) { downX = e.clientX; downY = e.clientY; }
    }, true);

    document.addEventListener("click", function (e) {
      var card = e.target && e.target.closest ? e.target.closest(SEL) : null;
      if (!card) return;
      if (e.target.closest(INTERACTIVE)) return;                 // real link/control clicked
      if (Math.abs(e.clientX - downX) > DRAG || Math.abs(e.clientY - downY) > DRAG) return; // drag = selection
      var sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length > 0) return;                 // text is selected
      var link = targetLink(card);
      if (!link) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey) { window.open(link.href, "_blank", "noopener"); return; }
      link.click(); // let Material's instant navigation handle it
    });

    document.addEventListener("auxclick", function (e) {
      if (e.button !== 1) return; // middle click -> new tab
      var card = e.target && e.target.closest ? e.target.closest(SEL) : null;
      if (!card || e.target.closest(INTERACTIVE)) return;
      var link = targetLink(card);
      if (link) window.open(link.href, "_blank", "noopener");
    });
  }

  if (window.document$ && typeof window.document$.subscribe === "function") window.document$.subscribe(mark);
  if (document.readyState !== "loading") mark();
  else document.addEventListener("DOMContentLoaded", mark);
})();
