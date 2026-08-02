# Authoring interactive diagrams (docs site)

How to build the animated, step-through SVG diagrams used on the documentation
site (MkDocs Material) — like the Harbor eval-flow widget in
`website/guides/harbor.md`.

## When to use one (and when not to)

| Use | For |
|---|---|
| **Mermaid** (default) | Static flowcharts and overviews. Zero build step, theme-aware, renders on GitHub too. Reach for this first. |
| **Interactive widget** (this guide) | Flows that benefit from step-through / playback / animation — parallelism, request-response choreography, staged pipelines. |
| **D2 → SVG** | One-off complex architecture diagrams where layout quality matters (pre-render to light+dark SVG). |

Interactive widgets are **hand-rolled vanilla JS + SVG, no dependencies** — MkDocs
is not React/MDX, so there's no component library to install. The upside: the
technique is just browser APIs (`getPointAtLength`, `requestAnimationFrame`,
`IntersectionObserver`) and it stays in our pure-Python toolchain.

## Reference implementation

Copy from the working example:

- `website/assets/javascripts/harbor-diagram.js` — the widget
- `website/stylesheets/diagrams.css` — its styling
- `website/guides/harbor.md` — how it's embedded (`<div class="harbor-diagram">`)
- `mkdocs.yml` — registered via `extra_javascript` / `extra_css`

## Architecture

A widget is driven by three data structures and a small render/step engine.

**1. Nodes** — boxes, positioned in viewBox units:

```js
var N = {
  orch: { x: 20, y: 196, w: 200, h: 112, kind: "orch",
          title: "Orchestrator", sub: ["line 1", "line 2"] },
  // ...
};
```

**2. Edges** — connectors between node anchors (`l`/`r`/`t`/`b` side + `frac`
along that side), horizontal or vertical bezier:

```js
var EDGES = [
  { id: "o1", a: ["orch", "r", 0.5], b: ["pod1", "l", 0.5], o: "h" },
  // ...
];
```

**3. Steps** — what lights up at each step, plus which edges get flow particles
and the caption text:

```js
var STEPS = [
  { key: "run", label: "Run agent", status: "Running", row: "agent",
    nodes: ["pod1", "pod2", "pod3", "gateway"],
    edges: ["g1", "g2", "g3"],
    flow: [["g1", 1], ["g2", 1], ["g3", 1]],   // [edgeId, direction]
    caption: "..." },
  // ...
];
```

The engine: draws one `<svg viewBox>` (so it scales to the container width),
with three stacked layers in DOM order — **edges → particles → nodes** (nodes
paint on top so labels are never covered by particles). A stepper (`role=tablist`)
and Prev/Next/Simulate controls sit below in HTML. `go(i)` toggles `.is-active`
classes and animates the step's `flow` edges.

## Authoring a new diagram

1. Copy `harbor-diagram.js` → `your-diagram.js` and `diagrams.css` rules (or add
   a new block). Pick a unique container class (e.g. `pipeline-diagram`) and a
   short CSS prefix; replace `harbor-diagram` / `hd-` throughout.
2. Define `N`, `EDGES`, `STEPS` for your flow. Keep the viewBox ~`1040 × 500`.
3. Embed the mount point in the target page's Markdown:
   ```html
   <div class="pipeline-diagram" aria-label="Describe the diagram for screen readers."></div>
   ```
4. Register the assets in `mkdocs.yml`:
   ```yaml
   extra_javascript:
     - assets/javascripts/your-diagram.js
   extra_css:
     - stylesheets/diagrams.css
   ```
5. Run the QA workflow below.

## Theming contract

Drive **all** colors from CSS variables so the widget follows the light/dark
toggle. Define a light default on the container, override under the slate scheme:

```css
.pipeline-diagram {
  --pd-accent: #3b82f6;
  --pd-node: #ffffff; --pd-fg: #1b1f24; --pd-edge: #c3ccd7; /* ... */
}
[data-md-color-scheme="slate"] .pipeline-diagram {
  --pd-node: #141a22; --pd-fg: #eef2f6; --pd-edge: #38434f; /* ... */
}
.pipeline-diagram text { font-family: var(--md-text-font, inherit); }
```

- Reference Material tokens (`--md-default-fg-color--lightest`, etc.) for borders
  so they blend with the theme.
- Arrowheads: give the `<marker>` path `fill: context-stroke` so it follows each
  edge's stroke color (neutral vs. accent) automatically.

## Gotchas (learned the hard way)

- **MkDocs instant navigation.** With `navigation.instant`, pages load via XHR and
  `DOMContentLoaded` does not re-fire. Initialise via **both** Material's
  `document$` observable **and** a DOM-ready fallback, and make init idempotent:
  ```js
  function initAll() {
    document.querySelectorAll(".pipeline-diagram:not(.ready)").forEach(function (c) {
      try { build(c); } catch (e) { console.error("pipeline-diagram build failed", e); }
    });
  }
  if (window.document$ && window.document$.subscribe) window.document$.subscribe(initAll);
  if (document.readyState !== "loading") initAll();
  else document.addEventListener("DOMContentLoaded", initAll);
  ```
  **Wrap `build()` in `try/catch`** — a throw inside the `document$` subscriber is
  *swallowed* by RxJS (no console error), so a bug silently renders nothing. This
  bit us; the try/catch surfaces it.
- **SVG `<text>` does not wrap.** Keep labels short, size boxes to fit, and give
  multi-line boxes enough height (title baseline ~`y+24`, sub lines ~`y+44+16i`;
  leave ~8px below the last line). Verify — overflow is silent.
- **Particles over text.** Particles fade at path endpoints (`opacity = sin(p·π)`),
  but route edges into text-free regions of a node anyway. Short connectors between
  vertically-adjacent boxes can "hook" — align the source anchor's x with the
  target anchor's x to keep them straight.
- **Reduced motion.** Gate particle animation and autoplay behind
  `matchMedia("(prefers-reduced-motion: reduce)")`; the stepper must still work.
- **Accessibility.** Use `role="tablist"`/`role="tab"` for steps, wire arrow/Home/End
  keys, and put the caption in an `aria-live="polite"` region.
- **Autoplay.** Start playback once via `IntersectionObserver` when the widget
  scrolls into view; stop it on any manual interaction.

## QA workflow (verify without eyeballing)

You cannot see the browser render, so verify mechanically.

1. **Syntax:** `node --check website/assets/javascripts/your-diagram.js`
2. **Build:** `mkdocs build --strict` (fails on broken links, missing assets).
3. **Headless render + assertions.** Get a browser: the easiest source is the
   `@mermaid-js/mermaid-cli` npx cache, which bundles Puppeteer + a
   `chrome-headless-shell` (`~/.npm/_npx/*/node_modules/puppeteer`,
   `~/.cache/puppeteer/chrome-headless-shell/*`), or `npm i -D puppeteer`. Then
   load the built page over **HTTP** (not `file://`) and assert the widget built
   with no console errors:

   ```js
   // check-widget.js — run: python3 -m http.server -d site 8137 & ; node check-widget.js
   const puppeteer = require("puppeteer");
   (async () => {
     const b = await puppeteer.launch({ headless: "shell", args: ["--no-sandbox"] });
     const p = await b.newPage();
     const errors = [];
     p.on("pageerror", (e) => errors.push(String(e)));
     p.on("console", (m) => m.type() === "error" && errors.push(m.text()));
     await p.goto("http://localhost:8137/guides/your-page/", { waitUntil: "load" });
     await p.waitForSelector(".pipeline-diagram.ready", { timeout: 8000 });
     const stats = await p.evaluate(() => ({
       nodes: document.querySelectorAll(".pipeline-diagram .pd-node").length,
       steps: document.querySelectorAll(".pipeline-diagram .pd-step").length,
       svg: !!document.querySelector(".pipeline-diagram svg"),
     }));
     // screenshot both themes
     const el = await p.$(".pipeline-diagram");
     await el.screenshot({ path: "/tmp/widget-light.png" });
     await p.evaluate(() => document.body.setAttribute("data-md-color-scheme", "slate"));
     await new Promise((r) => setTimeout(r, 400));
     await el.screenshot({ path: "/tmp/widget-dark.png" });
     console.log(JSON.stringify({ stats, errors }));
     await b.close();
   })();
   ```

   Assert: expected node/step counts, `svg: true`, `errors: []`.
4. **Visual QA of the screenshots.** Delegate screenshot inspection to a **sub-agent**
   — do not read images in the main agent context (they can corrupt context after
   compaction). Ask it to check: layout/no clipping, text contained in boxes,
   light/dark contrast, active-step emphasis, connector cleanliness.
5. **Mermaid blocks** (if any on the page): validate with `mmdc -i block.mmd -o /dev/null`.

## Should this become a skill?

Not yet. With one or two diagrams, this guide + copying the reference widget is
enough. Once several exist and the shape stabilises, the higher-leverage step is
to generalise the widget into a **spec-driven engine** (a single `flow-diagram.js`
that reads a nodes/edges/steps spec), after which a scaffolding skill becomes
worthwhile.
