// DOM helpers, formatting and small shared components.
// All text goes through text nodes: content from the server is never parsed as HTML.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Append children (flattening arrays, skipping null/false) to ``node``. */
export function put(node, ...children) {
  return append(node, children);
}

/** Replace all children of ``node``. */
export function fill(node, ...children) {
  return append(clear(node), children);
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

// Static, trusted SVG icons only.
const ICONS = {
  search: '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/>',
  library: '<rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/><path d="M2.5 6h11M6 6v7.5"/>',
  graph: '<circle cx="4" cy="4" r="1.8"/><circle cx="12" cy="5" r="1.8"/><circle cx="7" cy="12" r="1.8"/><path d="M5.6 4.4 10.3 4.7M5 5.6l1.4 4.8M11 6.6 8.2 10.6"/>',
  upload: '<path d="M8 10.5V2.5M5 5.5l3-3 3 3"/><path d="M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10"/>',
  close: '<path d="m4 4 8 8M12 4l-8 8"/>',
  trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5"/>',
  arrowLeft: '<path d="M13 8H3M7 4 3 8l4 4"/>',
  arrowRight: '<path d="M3 8h10M9 4l4 4-4 4"/>',
  play: '<path d="M5 3.5v9l7-4.5-7-4.5Z"/>',
  menu: '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/>',
  spark: '<path d="M8 2v3M8 11v3M2 8h3M11 8h3M4 4l2 2M10 10l2 2M12 4l-2 2M4 12l2-2"/>',
  check: '<path d="m3 8.5 3 3 7-7"/>',
  alert: '<path d="M8 5v3.5M8 11h.01"/><circle cx="8" cy="8" r="6"/>',
  file: '<path d="M4 1.5h5L12.5 5v9.5h-8.5z"/><path d="M9 1.5V5h3.5"/>',
  layers: '<path d="m8 2 6 3-6 3-6-3 6-3Z"/><path d="m2 8 6 3 6-3M2 11l6 3 6-3"/>',
  external: '<path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M11 9.5v4H2.5V5h4"/>',
};

export function icon(name, size = 16) {
  const span = document.createElement("span");
  span.className = "icon";
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
  return span;
}

// ------------------------------------------------------------------ format

const MODALITY_LABEL = { video: "Video", audio: "Audio", image: "Image", pdf: "PDF", json: "JSON" };
export const modalityLabel = (m) => MODALITY_LABEL[m] || (m ? String(m) : "Unknown");

export function modChip(modality) {
  return h("span", { class: `chip mod mod-${modality || "x"}` }, h("i", { class: "dot" }), modalityLabel(modality));
}

export function fmtClock(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
  const total = Math.max(0, Math.floor(seconds));
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = String(mins).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return hrs ? `${hrs}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function locatorText(locator) {
  if (!locator) return "";
  const parts = [];
  if (locator.start_seconds != null && locator.end_seconds != null) parts.push(`${fmtClock(locator.start_seconds)} – ${fmtClock(locator.end_seconds)}`);
  else if (locator.start_seconds != null) parts.push(fmtClock(locator.start_seconds));
  if (locator.page_number != null) parts.push(`Page ${locator.page_number}`);
  if (!parts.length && locator.label) parts.push(locator.label);
  return parts.join(" · ");
}

export function fmtBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 && unit ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const diff = (Date.now() - then) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)} d ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export const pct = (value) => `${Math.round((value || 0) * 100)}%`;

export function meter(value, label = null) {
  const v = Math.max(0, Math.min(1, Number(value) || 0));
  const level = v >= 0.8 ? "hi" : v >= 0.6 ? "mid" : "lo";
  return h(
    "span",
    { class: `meter ${level}`, title: label || `Confidence ${pct(v)}` },
    h("span", { class: "meter-bar" }, h("i", { style: { width: `${v * 100}%` } })),
    pct(v),
  );
}

export function skeletonLines(count = 3) {
  return h("div", { class: "sk-stack", "aria-hidden": "true" },
    Array.from({ length: count }, (_, i) => h("div", { class: "skeleton", style: { height: "14px", width: `${92 - i * 14}%` } })));
}

export function emptyState({ title, body, action }) {
  return h("div", { class: "empty" }, h("h3", {}, title), body ? h("p", {}, body) : null, action || null);
}

export function thumb(src, modality, alt = "") {
  const box = h("div", { class: `thumb mod-${modality || "x"}` });
  const glyph = h("span", { class: "thumb-glyph" }, modalityLabel(modality).slice(0, 3).toUpperCase());
  if (src) {
    const img = h("img", { src, alt, loading: "lazy", decoding: "async" });
    img.addEventListener("error", () => { img.remove(); box.append(glyph); }, { once: true });
    box.append(img);
  } else {
    box.append(glyph);
  }
  return box;
}

// ------------------------------------------------------------------ toasts

export function toast(message, kind = "info", timeout = 4500) {
  const host = document.querySelector(".toasts");
  if (!host) return;
  const node = h("div", { class: `toast ${kind}`, role: kind === "err" ? "alert" : "status" }, h("i", { class: "dot" }), h("span", {}, message));
  host.append(node);
  setTimeout(() => node.remove(), timeout);
}

export function debounce(fn, wait = 250) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

export function truncate(text, max = 240) {
  if (!text) return "";
  const clean = String(text).replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}
