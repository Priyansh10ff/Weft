// Graph: the knowledge base as one picture. Evidence (coloured by modality),
// the entities that tie sources together, and the typed links between them.
// Overview at #/graph, a node's neighbourhood at #/graph/{id}.

import { api } from "../api.js";
import { openEvidence } from "../evidence.js";
import { h, fill, put, icon, emptyState, skeletonLines } from "../ui.js";
import { navigate } from "../main.js";

const SVG = "http://www.w3.org/2000/svg";
const MODALITIES = ["video", "audio", "image", "pdf", "json"];
const RELATIONS = [
  ["depicts", "Depicts", "A visual that illustrates a statement in another source"],
  ["corroborates", "Corroborates", "Two sources that state the same thing"],
  ["co_occurs", "Co-occurs", "Speech while the same visual was on screen"],
  ["shows_same", "Shows same", "The same slide shown again"],
  ["spoken_by", "Spoken by", "Speech and its speaker"],
  ["same_as", "Same as", "Two names for one entity"],
  ["mentions", "Mentions", "Evidence that names an entity"],
];
const MOD_LABEL = { video: "Video", audio: "Audio", image: "Image", pdf: "PDF", json: "JSON / text" };
const DASHED = new Set(["co_occurs", "shows_same", "same_as"]);

function el(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  return node;
}

export function renderGraph(root, params, id) {
  let disposed = false;
  let frame = 0;
  const depth = Math.min(3, Math.max(1, Number(params.get("depth")) || 1));
  const hidden = new Set((params.get("hide") || "").split(",").filter(Boolean));
  // Node kinds switched off with the filter chips: a modality, or "entity".
  const off = new Set((params.get("off") || "").split(",").filter(Boolean));
  let data = null;
  let selected = null;

  const stage = h("div", { class: "graph-stage" }, h("div", { class: "card-pad" }, skeletonLines(4)));
  const side = h("aside", { class: "graph-side" });
  const links = h("section", { class: "graph-links" });
  const controls = h("div", { class: "row-inline" });

  put(root,
    id ? h("a", { class: "back", href: "#/graph" }, icon("arrowLeft", 14), "Whole graph") : null,
    h("header", { class: "page-head" },
      h("div", {},
        h("h1", {}, id ? "Neighbourhood" : "Graph"),
        h("p", { class: "muted" }, id
          ? "Everything linked to one piece of evidence or entity."
          : "Evidence that is linked across files, and the entities that tie sources together."),
      ),
      controls,
    ),
    h("div", { class: "graph-layout" }, h("div", { class: "card graph-card" }, stage), side),
    links,
  );

  function setParam(key, value) {
    const next = new URLSearchParams(params);
    if (value === null || value === "") next.delete(key); else next.set(key, value);
    const qs = next.toString();
    navigate(`#/graph${id ? `/${encodeURIComponent(id)}` : ""}${qs ? `?${qs}` : ""}`, { replace: true, silent: true });
    params = next;
  }

  function renderControls() {
    fill(controls,
      id ? h("div", { class: "chips", role: "group", "aria-label": "Depth" }, [1, 2, 3].map((d) => h("button", {
        class: "chip", type: "button", "aria-pressed": String(depth === d),
        onclick: () => navigate(`#/graph/${encodeURIComponent(id)}?depth=${d}`),
      }, `Depth ${d}`))) : null,
    );
  }

  async function load() {
    renderControls();
    try {
      data = id ? await api.graph(id, depth) : await api.graphOverview({ entities: true });
      if (disposed) return;
      if (!data.nodes.length) {
        fill(stage, emptyState({
          title: "Nothing linked yet",
          body: "Links appear when evidence in different files shows or states the same thing. Upload related sources, or load the sample data from Ask.",
          action: h("a", { class: "btn", href: "#/library" }, "Go to Library"),
        }));
        fill(side); fill(links);
        return;
      }
      draw();
    } catch (error) {
      if (disposed) return;
      fill(stage, emptyState({ title: error.status === 404 ? "Node not found" : "Couldn't load the graph", body: error.message }));
    }
  }

  function draw() {
    cancelAnimationFrame(frame);
    const kindOf = (n) => (n.kind === "entity" ? "entity" : n.modality);
    const nodes = data.nodes.filter((n) => n.id === data.center || !off.has(kindOf(n))).map((n) => ({ ...n }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = data.edges.filter((e) => !hidden.has(e.relation) && byId.has(e.source) && byId.has(e.target))
      .map((e) => ({ ...e, s: byId.get(e.source), t: byId.get(e.target) }));
    const degree = new Map();
    for (const e of data.edges) {
      degree.set(e.source, (degree.get(e.source) || 0) + 1);
      degree.set(e.target, (degree.get(e.target) || 0) + 1);
    }
    const neighbours = new Map(nodes.map((n) => [n.id, new Set()]));
    for (const e of edges) { neighbours.get(e.source).add(e.target); neighbours.get(e.target).add(e.source); }

    // ---- layout: a small force simulation, animated while it settles.
    // 1000 units across; the height follows the canvas shape (taller on phones).
    const cw = stage.clientWidth || 1000;
    const ch = Math.max(cw * 0.62, Math.min(520, window.innerHeight * 0.62));
    const W = 1000, H = Math.round((1000 * ch) / cw);
    nodes.forEach((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2;
      const r = n.id === data.center ? 0 : 180 + (i % 7) * 18;
      n.x = W / 2 + Math.cos(a) * r; n.y = H / 2 + Math.sin(a) * r; n.vx = 0; n.vy = 0;
      n.r = n.kind === "entity" ? 5 + Math.min(8, Math.sqrt(degree.get(n.id) || 1) * 1.7) : 7.5;
    });
    const center = byId.get(data.center);
    if (center) { center.x = W / 2; center.y = H / 2; center.r += 3; }

    const svg = el("svg", { class: `graph-svg${id && nodes.length <= 25 ? " labels-on" : ""}`, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Graph of ${nodes.length} nodes and ${edges.length} links` });
    const view = el("g");
    const edgeLayer = el("g", { class: "g-edges" });
    const nodeLayer = el("g", { class: "g-nodes" });
    view.append(edgeLayer, nodeLayer);
    svg.append(view);

    for (const e of edges) {
      e.line = el("line", { class: `g-edge rel-${e.relation}${DASHED.has(e.relation) ? " dashed" : ""}`, "stroke-opacity": 0.35 + 0.65 * (e.confidence ?? 1) });
      const title = el("title"); title.textContent = `${e.s.label} · ${e.relation.replace("_", " ")} · ${e.t.label}`;
      e.line.append(title);
      edgeLayer.append(e.line);
    }
    for (const n of nodes) {
      const g = el("g", { class: `g-node ${n.kind}${n.modality ? ` mod-${n.modality}` : ""}${n.id === data.center ? " center" : ""}${(degree.get(n.id) || 0) >= 4 ? " big" : ""}`, tabindex: "0", role: "button", "aria-label": n.label });
      if (n.kind === "entity") g.append(el("rect", { x: -n.r, y: -n.r, width: n.r * 2, height: n.r * 2, rx: 2.5 }));
      else g.append(el("circle", { r: n.r }));
      const label = el("text", { class: "g-label", x: n.r + 5, y: 4 });
      label.textContent = n.kind === "entity" ? n.label : shortLabel(n.label);
      g.append(label);
      g.addEventListener("pointerenter", () => highlight(n));
      g.addEventListener("pointerleave", () => highlight(selected));
      g.addEventListener("focus", () => highlight(n));
      g.addEventListener("keydown", (event) => { if (event.key === "Enter") select(n, event); });
      g.addEventListener("pointerdown", (event) => startDrag(event, n));
      n.g = g;
      nodeLayer.append(g);
    }

    function paint() {
      for (const e of edges) {
        e.line.setAttribute("x1", e.s.x); e.line.setAttribute("y1", e.s.y);
        e.line.setAttribute("x2", e.t.x); e.line.setAttribute("y2", e.t.y);
      }
      for (const n of nodes) n.g.setAttribute("transform", `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`);
    }

    let alpha = 1;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    function tick() {
      const k = Math.sqrt((W * H) / Math.max(1, nodes.length)) * 0.85;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.01;
          if (d2 > 160000) continue;
          const f = (k * k) / d2 * 0.06 * alpha;
          dx *= f; dy *= f;
          a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
        }
      }
      for (const e of edges) {
        const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const rest = e.relation === "mentions" ? k * 1.05 : k * 0.8;
        const f = ((d - rest) / d) * 0.05 * alpha;
        e.s.vx += dx * f; e.s.vy += dy * f; e.t.vx -= dx * f; e.t.vy -= dy * f;
      }
      for (const n of nodes) {
        if (n.pinned) { n.vx = 0; n.vy = 0; continue; }
        n.vx += (W / 2 - n.x) * 0.004 * alpha;
        n.vy += (H / 2 - n.y) * 0.004 * alpha;
        n.vx *= 0.6; n.vy *= 0.6;
        // Entities keep room on the right for their label.
        n.x = Math.max(20, Math.min(W - (n.kind === "entity" ? 120 : 20), n.x + n.vx));
        n.y = Math.max(20, Math.min(H - 20, n.y + n.vy));
      }
      alpha *= 0.985;
    }
    // ---- pan, zoom and drag
    let scale = 1, tx = 0, ty = 0;
    // Labels keep their on-screen size whatever the zoom or canvas width.
    const applyView = (animate = false) => {
      view.classList.toggle("easing", animate);
      view.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
      const px = (svg.clientWidth || W) / W;
      svg.style.setProperty("--zs", String(scale * px));
    };
    // Zoom so every node (and an entity's label) fits the canvas.
    function fit(animate = true) {
      if (!nodes.length) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of nodes) {
        x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
        x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
      }
      // Labels keep a fixed on-screen size, so leave room for the longest
      // entity label in screen pixels, converted to graph units.
      const px = (svg.clientWidth || stage.clientWidth || W) / W;
      const longest = Math.max(0, ...nodes.filter((n) => n.kind === "entity").map((n) => n.label.length));
      // On a phone that would squash the graph, so cap it; labels may then need a pan.
      const labelRoom = Math.min((longest * 7.5 + 12) / px, W * 0.22);
      const pad = 40 / px;
      scale = Math.max(0.3, Math.min(2.4, (W - pad * 2 - labelRoom) / Math.max(1, x1 - x0), (H - pad * 2 - 40) / Math.max(1, y1 - y0)));
      x1 += labelRoom / scale;
      tx = (W - (x0 + x1) * scale) / 2;
      ty = (H + 40 - (y0 + y1) * scale) / 2;  // the toolbar takes the top 40 units
      applyView(animate);
    }
    function zoomBy(factor) {
      const cx = W / 2, cy = H / 2;
      const gx = (cx - tx) / scale, gy = (cy - ty) / scale;
      scale = Math.min(4, Math.max(0.4, scale * factor));
      tx = cx - gx * scale; ty = cy - gy * scale;
      applyView(true);
    }
    function centerOn(n) {
      scale = Math.max(scale, 1.6);
      tx = W / 2 - n.x * scale; ty = H / 2 - n.y * scale;
      applyView(true);
    }

    for (let i = 0; i < (reduced ? 300 : 140); i++) tick();
    paint();
    fit(false);
    if (!reduced) {
      const run = () => {
        if (disposed) return;
        for (let i = 0; i < 2; i++) tick();
        paint();
        if (alpha > 0.02) frame = requestAnimationFrame(run);
        else fit(true);
      };
      frame = requestAnimationFrame(run);
    }
    const toGraph = (event) => {
      const rect = svg.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * W;
      const py = ((event.clientY - rect.top) / rect.height) * H;
      return [(px - tx) / scale, (py - ty) / scale, px, py];
    };
    svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      const [gx, gy, px, py] = toGraph(event);
      scale = Math.min(4, Math.max(0.4, scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
      tx = px - gx * scale; ty = py - gy * scale;
      applyView();
    }, { passive: false });
    svg.addEventListener("dblclick", () => fit(true));

    let drag = null, dragMoved = false;
    function startDrag(event, n) {
      event.stopPropagation();
      dragMoved = false;
      drag = { n, start: toGraph(event) };
      svg.setPointerCapture(event.pointerId);
    }
    svg.addEventListener("pointerdown", (event) => {
      dragMoved = false;
      drag = { pan: true, x: event.clientX, y: event.clientY, tx, ty };
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drag) return;
      if (drag.pan) {
        const rect = svg.getBoundingClientRect();
        tx = drag.tx + ((event.clientX - drag.x) / rect.width) * W;
        ty = drag.ty + ((event.clientY - drag.y) / rect.height) * H;
        if (Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 3) dragMoved = true;
        applyView();
        return;
      }
      const [gx, gy] = toGraph(event);
      if (Math.abs(gx - drag.start[0]) + Math.abs(gy - drag.start[1]) > 3) dragMoved = true;
      drag.n.x = gx; drag.n.y = gy; drag.n.pinned = true;
      paint();
    });
    svg.addEventListener("pointerup", (event) => {
      // Pointer capture retargets the click to the svg, so taps are resolved here.
      if (drag && !dragMoved) {
        if (drag.n) select(drag.n, event);
        else { selected = null; highlight(null); renderSide(); }
      }
      drag = null;
    });

    function highlight(n) {
      const on = n ? new Set([n.id, ...neighbours.get(n.id)]) : null;
      svg.classList.toggle("focused", Boolean(n));
      for (const m of nodes) m.g.classList.toggle("lit", Boolean(on?.has(m.id)));
      for (const e of edges) e.line.classList.toggle("lit", Boolean(n && (e.source === n.id || e.target === n.id)));
    }

    function select(n, event) {
      if (n.kind === "segment" && (event?.metaKey || event?.ctrlKey)) { openEvidence(n.id); return; }
      selected = n;
      highlight(n);
      renderSide();
    }

    function renderSide() {
      const rels = new Map();
      for (const e of data.edges) rels.set(e.relation, (rels.get(e.relation) || 0) + 1);

      fill(side,
        selected ? detail(selected) : null,
        h("div", { class: "card card-pad graph-legend" },
          h("p", { class: "section-label" }, "Links"),
          h("ul", { class: "legend-list" }, RELATIONS.filter(([key]) => rels.has(key)).map(([key, label, hint]) => h("li", {},
            h("label", { class: "legend-toggle", title: hint },
              h("input", { type: "checkbox", checked: !hidden.has(key), onchange: (event) => {
                if (event.target.checked) hidden.delete(key); else hidden.add(key);
                setParam("hide", [...hidden].join(","));
                draw();
              } }),
              h("i", { class: `legend-line rel-${key}${DASHED.has(key) ? " dashed" : ""}` }),
              h("span", {}, label),
            ),
            h("span", { class: "num faint" }, rels.get(key)),
          ))),
          h("p", { class: "graph-hint faint" }, "Click a node for details. Drag to move it, drag the background to pan, scroll to zoom, double-click to fit."),
        ),
      );
    }

    function detail(n) {
      const linked = [...neighbours.get(n.id)].map((nid) => byId.get(nid));
      const relOf = (other) => edges.find((e) => (e.source === n.id && e.target === other.id) || (e.target === n.id && e.source === other.id))?.relation;
      return h("div", { class: "card card-pad graph-detail" },
        h("p", { class: "section-label" }, n.kind === "entity" ? (n.entity_type || "entity") : n.modality),
        h("h3", { class: "graph-detail-title" }, n.label),
        h("div", { class: "row-inline graph-actions" },
          n.kind === "segment"
            ? h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => openEvidence(n.id) }, "Open evidence")
            : h("a", { class: "btn btn-sm btn-primary", href: `#/entities/${encodeURIComponent(n.id)}` }, "Open entity"),
          n.id !== data.center ? h("a", { class: "btn btn-sm", href: `#/graph/${encodeURIComponent(n.id)}` }, "Focus here") : null,
        ),
        linked.length ? h("ul", { class: "graph-neighbours" }, linked.slice(0, 12).map((m) => h("li", {},
          h("button", { class: "link", type: "button", onclick: () => select(m) }, shortLabel(m.label, 42)),
          h("span", { class: "tag" }, (relOf(m) || "").replace("_", " ")),
        ))) : h("p", { class: "faint" }, "No visible links."),
        linked.length > 12 ? h("p", { class: "faint" }, `and ${linked.length - 12} more`) : null,
      );
    }

    // ---- toolbar: search, kind filters, zoom
    const counts = new Map();
    for (const n of data.nodes) counts.set(kindOf(n), (counts.get(kindOf(n)) || 0) + 1);
    const results = h("ul", { class: "g-results", role: "listbox", hidden: true });
    const search = h("input", { class: "g-search-input", type: "search", placeholder: "Find a file or entity…", "aria-label": "Find a node", autocomplete: "off" });
    const pick = (n) => { search.value = ""; results.hidden = true; select(n); centerOn(n); };
    const matches = () => {
      const q = search.value.trim().toLowerCase();
      return q ? nodes.filter((n) => n.label.toLowerCase().includes(q)).slice(0, 8) : [];
    };
    search.addEventListener("input", () => {
      const found = matches();
      fill(results, found.length
        ? found.map((n) => h("li", {}, h("button", { type: "button", class: `g-result${n.modality ? ` mod-${n.modality}` : ""}`, onclick: () => pick(n) },
          n.kind === "entity" ? h("i", { class: "legend-entity" }) : h("i", { class: "dot" }), h("span", {}, n.label))))
        : h("li", { class: "faint g-none" }, "No match"));
      results.hidden = !search.value.trim();
    });
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { const first = matches()[0]; if (first) pick(first); }
      if (event.key === "Escape") { search.value = ""; results.hidden = true; }
    });
    const kinds = [...MODALITIES, "entity"].filter((k) => counts.has(k));
    const toggleKind = (k) => {
      if (off.has(k)) off.delete(k); else off.add(k);
      setParam("off", [...off].join(","));
      draw();
    };
    const toolbar = h("div", { class: "g-toolbar" },
      h("div", { class: "g-search" }, icon("search", 15), search, results),
      h("div", { class: "g-kinds", role: "group", "aria-label": "Show node types" }, kinds.map((k) => h("button", {
        class: `chip g-kind${k === "entity" ? "" : ` mod mod-${k}`}`, type: "button", "aria-pressed": String(!off.has(k)),
        title: `${off.has(k) ? "Show" : "Hide"} ${k === "entity" ? "entities" : MOD_LABEL[k]}`, onclick: () => toggleKind(k),
      }, k === "entity" ? h("i", { class: "legend-entity" }) : h("i", { class: "dot" }), `${k === "entity" ? "Entities" : k === "json" ? "JSON" : MOD_LABEL[k]} ${counts.get(k)}`))),
    );
    const zoom = h("div", { class: "g-zoom" },
      h("button", { type: "button", "aria-label": "Zoom in", onclick: () => zoomBy(1.25) }, "+"),
      h("button", { type: "button", "aria-label": "Zoom out", onclick: () => zoomBy(1 / 1.25) }, "−"),
      h("button", { type: "button", "aria-label": "Fit to view", class: "g-fit", onclick: () => fit(true) }, "Fit"),
    );
    const meta = h("p", { class: "g-meta" }, `${nodes.length} nodes · ${edges.length} links`);

    fill(stage, h("div", { class: "graph-canvas" }, toolbar, svg, zoom, meta));
    applyView();  // now that the svg has a size, the label scale can be measured
    const onResize = () => { if (disposed || !svg.isConnected) window.removeEventListener("resize", onResize); else applyView(); };
    window.addEventListener("resize", onResize);
    renderSide();
    renderLinks(edges);
  }

  function renderLinks(edges) {
    const cross = edges.filter((e) => e.relation !== "mentions");
    if (!cross.length) { fill(links); return; }
    fill(links,
      h("details", { class: "graph-table" },
        h("summary", { class: "section-label" }, `All links as a table (${cross.length})`),
        h("div", { class: "card table-card" }, h("div", { class: "table-scroll" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["From", "Link", "To", "Confidence"].map((t) => h("th", {}, t)))),
          h("tbody", {}, cross.map((e) => h("tr", {},
            h("td", {}, nodeButton(e.s)),
            h("td", {}, h("span", { class: "tag" }, e.relation.replace("_", " "))),
            h("td", {}, nodeButton(e.t)),
            h("td", { class: "num" }, `${Math.round((e.confidence ?? 1) * 100)}%`),
          ))),
        ))),
      ),
    );
  }

  function nodeButton(n) {
    return n.kind === "segment"
      ? h("button", { class: "link", type: "button", onclick: () => openEvidence(n.id) }, n.label)
      : h("a", { class: "link", href: `#/entities/${encodeURIComponent(n.id)}` }, n.label);
  }

  load();
  return () => { disposed = true; cancelAnimationFrame(frame); };
}

// "onboarding-walkthrough.mp4 · 00:11 - 00:26" -> "onboarding-walkt… · 00:11 - 00:26":
// the locator is the useful part, so the filename is what gets shortened.
function shortLabel(label, max = 30) {
  const text = String(label || "");
  if (text.length <= max) return text;
  const [name, ...rest] = text.split(" · ");
  const where = rest.join(" · ");
  if (!where) return `${text.slice(0, max - 1)}…`;
  const room = Math.max(8, max - where.length - 3);
  return `${name.length > room ? `${name.slice(0, room - 1)}…` : name} · ${where}`;
}
