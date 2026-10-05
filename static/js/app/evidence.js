// Evidence drawer: one segment, opened at its exact location, with its
// provenance and every relation it has in the graph.

import { api } from "./api.js";
import { h, clear, icon, modChip, locatorText, meter, fmtClock, emptyState, skeletonLines, truncate, pct, fill } from "./ui.js";

let drawer;
let panel;
let lastFocus = null;
let currentToken = 0;

function ensureDrawer() {
  if (drawer) return;
  drawer = h("div", { class: "drawer", hidden: true });
  const scrim = h("div", { class: "drawer-scrim", onclick: closeEvidence });
  panel = h("aside", { class: "drawer-panel", role: "dialog", "aria-modal": "true", "aria-label": "Evidence" });
  drawer.append(scrim, panel);
  document.body.append(drawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !drawer.hidden) closeEvidence();
  });
}

export function closeEvidence() {
  if (!drawer || drawer.hidden) return;
  drawer.classList.remove("open");
  panel.querySelectorAll("video, audio").forEach((media) => media.pause());
  setTimeout(() => { drawer.hidden = true; clear(panel); }, 180);
  lastFocus?.focus?.();
}

const queryTerms = (query) => new Set(
  String(query || "").toLowerCase().split(/[^a-z0-9%.]+/).filter((t) => t.length > 2),
);

function regionOverlay(blocks, regions, terms) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 1 1");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "regions");
  const add = (box, kind, label) => {
    if (!box) return;
    const rect = document.createElementNS(svg.namespaceURI, "rect");
    rect.setAttribute("x", box.x); rect.setAttribute("y", box.y);
    rect.setAttribute("width", box.width); rect.setAttribute("height", box.height);
    rect.setAttribute("class", kind);
    rect.setAttribute("vector-effect", "non-scaling-stroke");
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent = label;
    rect.append(title);
    svg.append(rect);
  };
  for (const region of regions || []) add(region.box, "region", `${region.label}${region.description ? ` — ${region.description}` : ""}`);
  for (const block of blocks || []) {
    const words = String(block.text).toLowerCase().split(/[^a-z0-9%.]+/);
    const matched = words.some((w) => terms.has(w));
    add(block.box, matched ? "ocr match" : "ocr", block.text);
  }
  return svg;
}

function figure(src, attributes, terms) {
  const blocks = (attributes.ocr_blocks || []).filter((b) => b.box);
  const regions = (attributes.regions || []).filter((r) => r.box);
  const frame = h("div", { class: "figure" });
  const img = h("img", { src, alt: "Visual evidence", decoding: "async" });
  img.addEventListener("error", () => frame.replaceWith(h("div", { class: "figure-missing" }, "Image file not available on this server.")), { once: true });
  frame.append(img);
  if (!blocks.length && !regions.length) return frame;

  const overlay = regionOverlay(blocks, regions, terms);
  frame.append(overlay);
  const matches = overlay.querySelectorAll(".match").length;
  const toggle = h("button", { class: "chip", type: "button", "aria-pressed": "true" }, "Regions");
  toggle.addEventListener("click", () => {
    const on = toggle.getAttribute("aria-pressed") !== "true";
    toggle.setAttribute("aria-pressed", String(on));
    overlay.style.display = on ? "" : "none";
  });
  const legend = h("div", { class: "figure-legend" },
    toggle,
    h("span", { class: "legend-item" }, h("i", { class: "swatch ocr" }), `${blocks.length} text block${blocks.length === 1 ? "" : "s"}`),
    regions.length ? h("span", { class: "legend-item" }, h("i", { class: "swatch region" }), `${regions.length} region${regions.length === 1 ? "" : "s"}`) : null,
    matches ? h("span", { class: "legend-item" }, h("i", { class: "swatch match" }), `${matches} matching your question`) : null,
  );
  return h("div", { class: "figure-wrap" }, frame, legend);
}

function mediaBlock(detail, terms) {
  const { segment, media_url: mediaUrl } = detail;
  const loc = segment.locator || {};
  const attrs = segment.attributes || {};
  const nodes = [];

  if ((segment.modality === "video" || segment.modality === "audio") && mediaUrl) {
    const tag = segment.modality === "video" ? "video" : "audio";
    const media = h(tag, { src: mediaUrl, controls: true, preload: "metadata", class: `player ${tag}` });
    if (segment.modality === "video" && segment.frame_path) media.setAttribute("poster", segment.frame_path);
    media.addEventListener("loadedmetadata", () => {
      if (loc.start_seconds != null) media.currentTime = loc.start_seconds;
    }, { once: true });
    media.addEventListener("error", () => {
      const fallback = segment.frame_path
        ? h("div", { class: "figure-wrap" }, figure(segment.frame_path, attrs, terms),
          h("p", { class: "faint fallback-note" }, "This browser can't play the recording. Showing the keyframe; ", h("a", { class: "link", href: mediaUrl, target: "_blank", rel: "noopener" }, "open the original"), "."))
        : h("div", { class: "figure-missing" }, "The original recording can't be played here.");
      media.replaceWith(fallback);
    }, { once: true });
    nodes.push(media);
    if (segment.modality === "video" && segment.frame_path && (attrs.ocr_blocks?.length || attrs.regions?.length)) {
      nodes.push(h("p", { class: "section-label" }, "Keyframe"), figure(segment.frame_path, attrs, terms));
    }
    return { nodes, media };
  }
  const src = segment.frame_path || (segment.modality === "image" ? mediaUrl : null);
  if (src) nodes.push(figure(src, attrs, terms));
  return { nodes, media: null };
}

function transcriptBlock(segment, media) {
  const spans = segment.attributes?.speech_segments;
  if (Array.isArray(spans) && spans.length) {
    return h("ol", { class: "speech" }, spans.map((span) => {
      const seek = h("button", { class: "ts", type: "button", title: media ? "Play from here" : "" }, fmtClock(span.start_seconds));
      if (media) seek.addEventListener("click", () => { media.currentTime = span.start_seconds; media.play().catch(() => {}); });
      else seek.disabled = true;
      return h("li", {},
        seek,
        h("div", {},
          span.speaker ? h("span", { class: "speaker" }, span.speaker) : null,
          h("p", {}, span.text),
        ),
      );
    }));
  }
  return segment.text ? h("p", { class: "prose" }, segment.text) : null;
}

function provenance(detail) {
  const { segment, source } = detail;
  const conf = segment.attributes?.confidence_source === "extractor" ? "measured" : "prior";
  const rows = [
    ["Source", h("a", { href: `#/sources/${source.id}`, class: "link" }, source.filename)],
    ["Location", locatorText(segment.locator) || "—"],
    ["Kind", segment.kind.replace("_", " ")],
    ["Extractor", h("code", {}, segment.extractor)],
    ["Confidence", h("span", { class: "row-inline" }, meter(segment.confidence), h("span", { class: "faint" }, conf))],
    ["Segment ID", h("code", { class: "id" }, segment.id)],
  ];
  if (segment.attributes?.visual_extraction_failed) rows.push(["Note", h("span", { style: { color: "var(--warn)" } }, "Vision analysis failed for this segment")]);
  return h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function relations(detail, onOpenSegment) {
  const groups = { previous: [], next: [], speakers: [], other: [] };
  for (const link of detail.links || []) {
    const rel = link.relation.relation;
    if (rel === "mentions") continue;
    if (rel === "next") (link.direction === "out" ? groups.next : groups.previous).push(link);
    else if (rel === "spoken_by") groups.speakers.push(link);
    else groups.other.push(link);
  }
  const nav = h("div", { class: "seg-nav" },
    groups.previous[0]
      ? h("button", { class: "btn btn-sm", type: "button", onclick: () => onOpenSegment(groups.previous[0].node_id) }, icon("arrowLeft", 14), "Previous")
      : h("span"),
    groups.next[0]
      ? h("button", { class: "btn btn-sm", type: "button", onclick: () => onOpenSegment(groups.next[0].node_id) }, "Next", icon("arrowRight", 14))
      : h("span"),
  );
  const other = groups.other.length
    ? h("ul", { class: "rel-list" }, groups.other.map((link) => h("li", {},
      h("span", { class: "tag" }, link.relation.relation.replace("_", " ")),
      link.node_kind === "segment"
        ? h("button", { class: "link", type: "button", onclick: () => onOpenSegment(link.node_id) }, link.label || "segment")
        : h("a", { class: "link", href: `#/entities/${link.node_id}` }, link.label || link.node_id),
      h("span", { class: "faint num" }, pct(link.relation.confidence)),
    )))
    : null;
  return { nav, speakers: groups.speakers, other };
}

export async function openEvidence(segmentId, { query = "" } = {}) {
  ensureDrawer();
  const token = ++currentToken;
  if (drawer.hidden) lastFocus = document.activeElement;
  drawer.hidden = false;
  requestAnimationFrame(() => drawer.classList.add("open"));
  fill(panel, 
    h("header", { class: "drawer-head" }, h("span", { class: "skeleton", style: { width: "180px", height: "18px" } }), closeButton()),
    h("div", { class: "drawer-body" }, h("div", { class: "skeleton", style: { aspectRatio: "16/9", width: "100%" } }), skeletonLines(4)),
  );

  let detail;
  try {
    detail = await api.segment(segmentId);
  } catch (error) {
    if (token !== currentToken) return;
    fill(panel, h("header", { class: "drawer-head" }, h("span"), closeButton()),
      h("div", { class: "drawer-body" }, emptyState({ title: "Couldn't load this evidence", body: error.message })));
    return;
  }
  if (token !== currentToken) return;

  const { segment, source } = detail;
  const terms = queryTerms(query);
  const { nodes: mediaNodes, media } = mediaBlock(detail, terms);
  const transcript = transcriptBlock(segment, media);
  const rel = relations(detail, (id) => openEvidence(id, { query }));
  const ocrText = segment.attributes?.ocr_text && segment.attributes.ocr_text !== segment.text ? segment.attributes.ocr_text : null;

  const entityChips = detail.entities.length
    ? h("div", { class: "chips" }, detail.entities.map(({ entity }) =>
      h("a", { class: "chip", href: `#/entities/${entity.id}`, title: entity.entity_type }, entity.name)))
    : null;
  const speakerChips = rel.speakers.length
    ? h("div", { class: "chips" }, rel.speakers.map((link) =>
      h("a", { class: "chip", href: `#/entities/${link.node_id}` }, h("i", { class: "dot", style: { background: "var(--signal)" } }), link.label || "Speaker")))
    : null;

  fill(panel, 
    h("header", { class: "drawer-head" },
      h("div", { class: "drawer-title" },
        modChip(segment.modality),
        h("a", { class: "drawer-source", href: `#/sources/${source.id}`, title: source.filename }, source.filename),
        h("span", { class: "loc" }, locatorText(segment.locator)),
      ),
      closeButton(),
    ),
    h("div", { class: "drawer-body" },
      mediaNodes,
      rel.nav,
      speakerChips ? section("Spoken by", speakerChips) : null,
      transcript ? section(segment.modality === "video" || segment.modality === "audio" ? "Said" : "Text", transcript) : null,
      segment.visual_summary ? section("Shown", h("p", { class: "prose" }, segment.visual_summary)) : null,
      ocrText ? section("Text on screen", h("p", { class: "prose mono-prose" }, truncate(ocrText, 1200))) : null,
      entityChips ? section("Entities", entityChips) : null,
      rel.other ? section("Linked evidence", rel.other) : null,
      section("Provenance", provenance(detail)),
    ),
  );
  panel.querySelector(".drawer-close")?.focus();
}

function section(title, content) {
  return h("section", { class: "drawer-section" }, h("p", { class: "section-label" }, title), content);
}

function closeButton() {
  return h("button", { class: "btn btn-ghost btn-icon drawer-close", type: "button", "aria-label": "Close evidence", onclick: closeEvidence }, icon("close"));
}
