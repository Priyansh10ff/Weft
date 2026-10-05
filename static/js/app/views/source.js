// One source: its original media, a timeline of segments and every segment's
// text, visuals, entities and confidence.

import { api } from "../api.js";
import { openEvidence } from "../evidence.js";
import { h, clear, icon, modChip, meter, fmtBytes, relTime, locatorText, truncate, thumb, emptyState, skeletonLines, fmtClock, fill, put } from "../ui.js";

function timeline(segments, media) {
  const timed = segments.filter((s) => s.locator?.start_seconds != null);
  if (!timed.length) return null;
  const duration = Math.max(...timed.map((s) => s.locator.end_seconds ?? s.locator.start_seconds), 1);
  const track = h("div", { class: "tl-track", role: "list" });
  for (const seg of timed) {
    const start = seg.locator.start_seconds;
    const end = seg.locator.end_seconds ?? start + 1;
    const block = h("button", {
      class: `tl-seg mod-${seg.modality}${seg.visual_summary ? " has-visual" : ""}`, type: "button", role: "listitem",
      style: { left: `${(start / duration) * 100}%`, width: `${Math.max(0.6, ((end - start) / duration) * 100)}%`, opacity: String(0.45 + seg.confidence * 0.55) },
      title: `${locatorText(seg.locator)}\n${truncate(seg.text || seg.visual_summary || "", 140)}`,
      onclick: () => {
        if (media) { media.currentTime = start; }
        openEvidence(seg.id);
      },
    });
    track.append(block);
  }
  const head = h("div", { class: "tl-time" }, h("span", {}, "00:00"), h("span", {}, fmtClock(duration)));
  const cursor = h("i", { class: "tl-cursor" });
  track.append(cursor);
  if (media) media.addEventListener("timeupdate", () => { cursor.style.left = `${Math.min(100, (media.currentTime / duration) * 100)}%`; });
  return h("div", { class: "timeline" }, track, head);
}

export function segmentLabel(seg) {
  const text = locatorText(seg.locator);
  if (text) return text;
  if (seg.modality === "image") return "Full image";
  if (seg.modality === "json") return `Record ${seg.ordinal + 1}`;
  return `#${seg.ordinal + 1}`;
}

function segmentRow(seg) {
  const open = () => openEvidence(seg.id);
  return h("li", { class: `seg-row mod-${seg.modality}`, tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
    h("span", { class: "seg-loc loc" }, segmentLabel(seg)),
    seg.frame_path ? thumb(seg.frame_path, seg.modality) : h("span", { class: "thumb thumb-empty" }),
    h("div", { class: "seg-body" },
      seg.text ? h("p", { class: "ev-text" }, truncate(seg.text, 220)) : null,
      seg.visual_summary ? h("p", { class: "ev-visual" }, h("span", { class: "faint" }, "Shown: "), truncate(seg.visual_summary, 160)) : null,
      !seg.text && !seg.visual_summary ? h("p", { class: "faint" }, "No extracted content") : null,
      seg.attributes?.speakers?.length ? h("p", { class: "faint seg-speakers" }, seg.attributes.speakers.map((s) => s.name || s.label).join(", ")) : null,
    ),
    h("span", { class: "seg-conf" }, meter(seg.confidence)),
  );
}

export function renderSource(root, params, id) {
  let disposed = false;
  put(root, h("div", { class: "card-pad" }, skeletonLines(5)));

  api.source(id).then((detail) => {
    if (disposed) return;
    const { source, segments, media_url: mediaUrl } = detail;
    const warnings = source.attributes?.warnings || [];
    let media = null;
    let mediaNode = null;
    if (mediaUrl && (source.modality === "video" || source.modality === "audio")) {
      media = h(source.modality, { src: mediaUrl, controls: true, preload: "metadata", class: `player ${source.modality}` });
      media.addEventListener("error", () => media.replaceWith(h("div", { class: "figure-missing" },
        "This recording can't be played in this browser. ",
        h("a", { class: "link", href: mediaUrl, target: "_blank", rel: "noopener" }, "Download the original"), ".")), { once: true });
      mediaNode = media;
    } else if (source.modality === "image" && (mediaUrl || segments[0]?.frame_path)) {
      mediaNode = h("div", { class: "figure" }, h("img", { src: mediaUrl || segments[0].frame_path, alt: source.filename }));
    }

    const visualCount = segments.filter((s) => s.visual_summary).length;
    const avgConf = segments.length ? segments.reduce((sum, s) => sum + s.confidence, 0) / segments.length : 0;

    fill(root, 
      h("a", { class: "back", href: "#/library" }, icon("arrowLeft", 14), "Library"),
      h("header", { class: "page-head source-head" },
        h("div", {},
          h("div", { class: "row-inline" }, modChip(source.modality), source.status === "index_failed" ? h("span", { class: "badge err" }, "Index failed") : null),
          h("h1", { class: "source-title" }, source.filename),
          h("p", { class: "muted meta-line" }, [
            `${segments.length} segment${segments.length === 1 ? "" : "s"}`,
            visualCount ? `${visualCount} with visuals` : null,
            source.size_bytes != null ? fmtBytes(source.size_bytes) : null,
            `added ${relTime(source.ingested_at)}`,
          ].filter(Boolean).join(" · ")),
        ),
        h("div", { class: "head-actions" },
          h("span", { class: "muted" }, "avg confidence"), meter(avgConf),
          mediaUrl ? h("a", { class: "btn btn-sm", href: mediaUrl, target: "_blank", rel: "noopener" }, icon("external", 14), "Original") : null,
        ),
      ),
      warnings.length ? h("div", { class: "card card-pad warn-box" }, icon("alert", 16), h("ul", {}, warnings.map((w) => h("li", {}, w)))) : null,
      mediaNode ? h("div", { class: "source-media" }, mediaNode) : null,
      timeline(segments, media),
      h("div", { class: "section-row" }, h("p", { class: "section-label" }, "Segments")),
      segments.length
        ? h("ol", { class: "seg-list card" }, segments.map(segmentRow))
        : emptyState({ title: "No segments", body: "Nothing was extracted from this source." }),
      h("details", { class: "prov card" },
        h("summary", {}, "Source provenance"),
        h("dl", { class: "kv" },
          h("dt", {}, "Source ID"), h("dd", {}, h("code", { class: "id" }, source.id)),
          h("dt", {}, "SHA-256"), h("dd", {}, h("code", { class: "id" }, source.sha256 || "—")),
          h("dt", {}, "Stored at"), h("dd", {}, h("code", {}, source.storage_path || "—")),
          h("dt", {}, "Content type"), h("dd", {}, source.content_type || "—"),
          h("dt", {}, "Ingested"), h("dd", {}, new Date(source.ingested_at).toLocaleString()),
        ),
      ),
    );
  }).catch((error) => {
    if (disposed) return;
    fill(root, h("a", { class: "back", href: "#/library" }, icon("arrowLeft", 14), "Library"),
      emptyState({ title: error.status === 404 ? "Source not found" : "Couldn't load this source", body: error.message }));
  });

  return () => { disposed = true; };
}
