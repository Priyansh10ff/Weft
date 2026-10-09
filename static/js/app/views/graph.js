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
  let showEntities = params.get("entities") !== "0";
  const hidden = new Set((params.get("hide") || "").split(",").filter(Boolean));
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
      }, `Depth ${d}`))) : h("button", {
        class: "chip", type: "button", "aria-pressed": String(showEntities),
        onclick: () => { showEntities = !showEntities; setParam("entities", showEntities ? null : "0"); load(); },
      }, "Entities"),
    );
  }

  async function load() {
    renderControls();
    try {
      data = id ? await api.graph(id, depth) : await api.graphOverview({ entities: showEntities });
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
    const nodes = data.nodes.map((n) => ({ ...n }));
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
    const W = 1000, H = 640;
    nodes.forEach((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2;
      const r = n.id === data.center ? 0 : 180 + (i % 7) * 18;
      n.x = W / 2 + Math.cos(a) * r; n.y = H / 2 + Math.sin(a) * r; n.vx = 0; n.vy = 0;
      n.r = n.kind === "entity" ? 4 + Math.min(8, Math.sqrt(degree.get(n.id) || 1) * 1.6) : 6.5;
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
      const g = el("g", { class: `g-node ${n.kind}${n.modality ? ` mod-${n.modality}` : ""}${n.id === data.center ? " center" : ""}`, tabindex: "0", role: "button", "aria-label": n.label });
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
      const k = Math.sqrt((W * H) / Math.max(1, nodes.length)) * 0.55;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.01;
          if (d2 > 90000) continue;
          const f = (k * k) / d2 * 0.06 * alpha;
          dx *= f; dy *= f;
          a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
        }
      }
      for (const e of edges) {
        const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const rest = e.relation === "mentions" ? k * 0.9 : k * 0.7;
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
    if (reduced) { for (let i = 0; i < 300; i++) tick(); paint(); }
    else {
      const run = () => {
        if (disposed) return;
        for (let i = 0; i < 2; i++) tick();
        paint();
        if (alpha > 0.02) frame = requestAnimationFrame(run);
      };
      run();
    }

    // ---- pan, zoom and drag
    let scale = 1, tx = 0, ty = 0;
    const applyView = () => view.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
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
      const mods = new Map();
      for (const n of nodes) if (n.modality) mods.set(n.modality, (mods.get(n.modality) || 0) + 1);
      const rels = new Map();
      for (const e of data.edges) rels.set(e.relation, (rels.get(e.relation) || 0) + 1);
      const entityCount = nodes.filter((n) => n.kind === "entity").length;

      fill(side,
        selected ? detail(selected) : null,
        h("div", { class: "card card-pad graph-legend" },
          h("p", { class: "section-label" }, "Evidence"),
          h("ul", { class: "legend-list" },
            MODALITIES.filter((m) => mods.has(m)).map((m) => h("li", { class: `mod mod-${m}` },
              h("i", { class: "dot" }), h("span", {}, MOD_LABEL[m]), h("span", { class: "num faint" }, mods.get(m)))),
            entityCount ? h("li", {}, h("i", { class: "legend-entity" }), h("span", {}, "Entity"), h("span", { class: "num faint" }, entityCount)) : null,
          ),
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
          h("p", { class: "graph-hint faint" }, "Click a node for details. Drag to move it, drag the background to pan, scroll to zoom."),
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

    fill(stage, svg);
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
