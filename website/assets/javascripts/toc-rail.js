/*
 * Scroll-following "rail + thumb" for the right-hand table of contents.
 *
 * MkDocs Material already marks the active TOC entry with
 * `.md-nav__link--active` (and `--passed` for scrolled-past). This adds a
 * vertical rail down the TOC and an accent "thumb" that slides to the active
 * entry as you scroll — the Fumadocs-style affordance.
 *
 * Instant-navigation safe (document$ + DOM-ready fallback, idempotent),
 * rAF-throttled, and honors prefers-reduced-motion.
 */
(function () {
  "use strict";

  function setup(list) {
    if (list.__railed) return;
    if (!list.querySelector("a.md-nav__link")) return; // no headings → no rail
    list.__railed = true;
    list.classList.add("md-toc-railed");

    var thumb = document.createElement("span");
    thumb.className = "md-toc-thumb";
    list.appendChild(thumb);

    var pending = false;
    function apply() {
      pending = false;
      if (!list.isConnected) return;
      var active = list.querySelector("a.md-nav__link--active");
      if (!active) { thumb.style.opacity = "0"; return; }
      var lr = list.getBoundingClientRect();
      var ar = active.getBoundingClientRect();
      var top = ar.top - lr.top + list.scrollTop; // content-space offset
      thumb.style.opacity = "1";
      thumb.style.height = Math.max(ar.height, 14) + "px";
      thumb.style.transform = "translateY(" + top + "px)";
    }
    function schedule() {
      if (pending) return;
      pending = true;
      requestAnimationFrame(apply);
    }

    // Material toggles --active on TOC links as you scroll; observe that.
    new MutationObserver(schedule).observe(list, {
      subtree: true, attributes: true, attributeFilter: ["class"],
    });
    window.addEventListener("resize", schedule, { passive: true });
    schedule();
  }

  function initAll() {
    document.querySelectorAll('.md-sidebar--secondary [data-md-component="toc"]').forEach(setup);
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initAll);
  }
  if (document.readyState !== "loading") initAll();
  else document.addEventListener("DOMContentLoaded", initAll);
})();
