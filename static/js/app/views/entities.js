// Entities: the people, systems, metrics and concepts Weft found, and where
// each one appears across files and modalities.

import { api } from "../api.js";
import { openEvidence } from "../evidence.js";
import { h, clear, icon, modChip, locatorText, truncate, emptyState, skeletonLines, debounce, meter, fill, put } from "../ui.js";
import { navigate } from "../main.js";
import { segmentLabel } from "./source.js";

const TYPE_ORDER = ["person", "speaker", "system", "component", "product", "metric", "organization", "concept", "event", "location"];

export function renderEntities(root, params) {
  let disposed = false;
  let all = [];
  let typeFilter = params.get("type") || "";
  const input = h("input", { class: "input", type: "search", placeholder: "Filter entities…", "aria-label": "Filter entities", value: params.get("q") || "" });
  const filters = h("div", { class: "chips filter-row" });
  const host = h("div", { class: "card table-card" }, h("div", { class: "card-pad" }, skeletonLines(5)));

  put(root, 
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, "Entities"), h("p", { class: "muted" }, "Named things found across your sources. The same entity in a video, a PDF and a ticket is one node.")),
    ),
    h("div", { class: "toolbar" }, h("div", { class: "search-box" }, icon("search", 16), input), filters),
    host,
  );

  function renderFilters() {
    const counts = new Map();
    for (const item of all) counts.set(item.entity.entity_type, (counts.get(item.entity.entity_type) || 0) + 1);
    const rank = (t) => { const i = TYPE_ORDER.indexOf(t); return i === -1 ? TYPE_ORDER.length : i; };
    const types = [...counts.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    fill(filters, 
      h("button", { class: "chip", type: "button", "aria-pressed": String(!typeFilter), onclick: () => { typeFilter = ""; renderFilters(); renderTable(); } }, `All ${all.length}`),
      types.map((t) => h("button", { class: "chip", type: "button", "aria-pressed": String(typeFilter === t), onclick: () => { typeFilter = t; renderFilters(); renderTable(); } }, `${t} ${counts.get(t)}`)),
    );
  }

  function renderTable() {
    const rows = all.filter((item) => !typeFilter || item.entity.entity_type === typeFilter);
    clear(host);
    if (!rows.length) {
      put(host, emptyState({ title: all.length ? "No entities match" : "No entities yet", body: all.length ? "Try another filter." : "Entities appear as sources are ingested." }));
      return;
    }
    put(host, h("div", { class: "table-scroll" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Entity", "Type", "Mentions", "Sources", "Modalities"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows.map((item) => {
        const go = () => navigate(`#/entities/${item.entity.id}`);
        return h("tr", { class: "row-link", tabindex: "0", onclick: go, onkeydown: (e) => { if (e.key === "Enter") go(); } },
          h("td", { class: "cell-name" }, h("span", { class: "name" }, item.entity.name)),
          h("td", {}, h("span", { class: "tag" }, item.entity.entity_type)),
          h("td", { class: "num" }, item.mention_count),
          h("td", { class: "num" }, item.source_count),
          h("td", {}, h("span", { class: "mods" }, item.modalities.map((m) => h("span", { class: `mod mod-${m}`, title: m }, h("i", { class: "dot" }))))),
        );
      })),
    )));
  }

  async function load() {
    const q = input.value.trim();
    try {
      all = await api.entities(q, 500);
      if (disposed) return;
      renderFilters();
      renderTable();
    } catch (error) {
      if (!disposed) fill(host, emptyState({ title: "Couldn't load entities", body: error.message }));
    }
  }

  input.addEventListener("input", debounce(load, 220));
  load();
  return () => { disposed = true; };
}

function mentionRow(seg, query, extra = null) {
  const open = () => openEvidence(seg.id, { query });
  return h("li", {
    class: `seg-row mod-${seg.modality}`, tabindex: "0", onclick: open,
    onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
  h("span", { class: "seg-loc loc" }, segmentLabel(seg)),
  h("div", { class: "seg-body" },
    extra,
    seg.text ? h("p", { class: "ev-text" }, truncate(seg.text, 220)) : null,
    seg.visual_summary ? h("p", { class: "ev-visual" }, h("span", { class: "faint" }, "Shown: "), truncate(seg.visual_summary, 160)) : null,
  ),
  h("span", { class: "seg-conf" }, meter(seg.confidence)));
}

const dayFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

function timelineView(timeline, query) {
  if (!timeline.entries.length) return emptyState({ title: "No mentions", body: "This entity isn't linked to any segment." });
  const days = new Map();
  for (const entry of timeline.entries) {
    const key = dayFormat.format(new Date(entry.when));
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(entry);
  }
  return h("ol", { class: "timeline-list" }, [...days.entries()].map(([day, entries]) => h("li", { class: "tl-day" },
    h("div", { class: "tl-date" },
      h("span", { class: "tl-dot" }),
      h("span", {}, day),
      entries[0].when_source === "ingested_at" ? h("span", { class: "faint", title: "No recording date was given; using upload time" }, "uploaded") : null,
    ),
    h("ol", { class: "seg-list card" }, entries.map((entry) => mentionRow(entry.segment, query,
      h("div", { class: "row-inline mention-src" }, modChip(entry.source.modality),
        h("a", { class: "link", href: `#/sources/${entry.source.id}`, onclick: (e) => e.stopPropagation() }, entry.source.filename))))),
  )));
}

function bySourceView(timeline, query) {
  const groups = new Map();
  for (const entry of timeline.entries) {
    if (!groups.has(entry.source.id)) groups.set(entry.source.id, { source: entry.source, segs: [] });
    groups.get(entry.source.id).segs.push(entry.segment);
  }
  if (!groups.size) return emptyState({ title: "No mentions", body: "This entity isn't linked to any segment." });
  return [...groups.values()].map(({ source, segs }) => h("section", { class: "entity-group" },
    h("div", { class: "group-head" },
      modChip(source.modality),
      h("a", { class: "link", href: `#/sources/${source.id}` }, source.filename),
      h("span", { class: "faint" }, `${segs.length}`),
    ),
    h("ol", { class: "seg-list card" }, segs.map((seg) => mentionRow(seg, query))),
  ));
}

export function renderEntity(root, params, id) {
  let disposed = false;
  put(root, h("div", { class: "card-pad" }, skeletonLines(5)));

  api.entityTimeline(id).then((timeline) => {
    if (disposed) return;
    const { entity, aliases, entries } = timeline;
    const sourceCount = new Set(entries.map((e) => e.source.id)).size;
    const modalities = [...new Set(entries.map((e) => e.segment.modality))];
    const query = [entity.name, ...aliases.map((a) => a.name)].join(" ");
    let mode = params.get("view") === "sources" ? "sources" : "timeline";
    const body = h("div", { class: "entity-body" });
    const tabs = h("div", { class: "tabs", role: "tablist" });

    const draw = () => {
      fill(tabs,
        ["timeline", "sources"].map((key) => h("button", {
          class: "tab", type: "button", role: "tab", "aria-selected": String(mode === key),
          onclick: () => { mode = key; draw(); },
        }, key === "timeline" ? "Timeline" : "By source")));
      fill(body, mode === "timeline" ? timelineView(timeline, query) : bySourceView(timeline, query));
    };

    fill(root,
      h("a", { class: "back", href: "#/entities" }, icon("arrowLeft", 14), "Entities"),
      h("header", { class: "page-head" },
        h("div", {},
          h("span", { class: "tag" }, entity.entity_type),
          h("h1", { class: "source-title" }, entity.name),
          h("p", { class: "muted meta-line" },
            `${entries.length} mention${entries.length === 1 ? "" : "s"} across ${sourceCount} source${sourceCount === 1 ? "" : "s"}`,
            modalities.length > 1 ? ` and ${modalities.length} modalities` : "",
          ),
          aliases.length ? h("p", { class: "faint alias-line" }, "Also seen as: ", aliases.map((a) => a.name).join(", ")) : null,
        ),
        h("div", { class: "head-actions" }, modalities.map(modChip)),
      ),
      tabs,
      body,
    );
    draw();
  }).catch((error) => {
    if (disposed) return;
    fill(root, h("a", { class: "back", href: "#/entities" }, icon("arrowLeft", 14), "Entities"),
      emptyState({ title: error.status === 404 ? "Entity not found" : "Couldn't load this entity", body: error.message }));
  });

  return () => { disposed = true; };
}
