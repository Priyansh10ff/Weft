// Workspace shell: hash router, shared store, sidebar status, shortcuts.

import { api } from "./api.js";
import { closeEvidence } from "./evidence.js";
import { h, clear, toast, fill } from "./ui.js";
import { renderAsk } from "./views/ask.js";
import { renderLibrary } from "./views/library.js";
import { renderSource } from "./views/source.js";
import { renderEntities, renderEntity } from "./views/entities.js";

export const store = {
  stats: null,
  health: null,
  async refresh() {
    const [stats, health] = await Promise.allSettled([api.stats(), api.health()]);
    if (stats.status === "fulfilled") this.stats = stats.value;
    if (health.status === "fulfilled") this.health = health.value;
    renderStatus(health.status === "fulfilled");
    return this.stats;
  },
};

const ROUTES = [
  [/^\/?$/, "ask", renderAsk, "Ask"],
  [/^\/ask$/, "ask", renderAsk, "Ask"],
  [/^\/library$/, "library", renderLibrary, "Library"],
  [/^\/sources\/([^/]+)$/, "library", renderSource, "Source"],
  [/^\/entities$/, "entities", renderEntities, "Entities"],
  [/^\/entities\/([^/]+)$/, "entities", renderEntity, "Entity"],
];

const view = document.getElementById("view");
let cleanup = null;
let silentNext = false;

export function navigate(hash, { replace = false, silent = false } = {}) {
  if (replace) {
    // replaceState never fires hashchange, so route manually unless silent.
    history.replaceState(null, "", hash);
    if (!silent) route();
    return;
  }
  if (location.hash === hash) { if (!silent) route(); return; }
  if (silent) silentNext = true;
  location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/ask";
  const [path, query = ""] = raw.split("?");
  return { path, params: new URLSearchParams(query) };
}

function route() {
  if (silentNext) { silentNext = false; return; }
  const { path, params } = parseHash();
  const match = ROUTES.find(([pattern]) => pattern.test(path));
  cleanup?.();
  cleanup = null;
  closeEvidence();
  clear(view);
  document.body.classList.remove("nav-open");

  if (!match) {
    view.append(h("div", { class: "empty" }, h("h3", {}, "Page not found"), h("a", { class: "btn", href: "#/ask" }, "Go to Ask")));
    return;
  }
  const [pattern, nav, render, title] = match;
  const args = path.match(pattern).slice(1).map(decodeURIComponent);
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const on = link.dataset.nav === nav;
    link.classList.toggle("active", on);
    if (on) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.title = `${title} · Weft`;
  cleanup = render(view, params, ...args) || null;
  view.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function renderStatus(reachable) {
  const host = document.getElementById("status");
  if (!host) return;
  const s = store.stats;
  const p = store.health?.providers;
  const row = (label, on, value) => h("li", {}, h("i", { class: `dot ${on ? "on" : "off"}` }), h("span", {}, label), value ? h("span", { class: "faint" }, value) : null);
  fill(host, 
    reachable
      ? h("ul", { class: "status-list" },
        row("Vision", p?.vision, p?.vision ? "Gemini" : "off"),
        row("Transcription", p?.transcription, p?.transcription ? "Groq" : "off"),
        row("Speakers", p?.speakers, p?.speakers ? "on" : "off"),
        row("Entities", true, p?.entities === "llm" ? "LLM" : "patterns"),
      )
      : h("p", { class: "status-down" }, h("i", { class: "dot off" }), "Server unreachable"),
    s ? h("p", { class: "status-counts num" }, `${s.sources} sources · ${s.segments} segments`) : null,
  );
}

document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  if ((event.key === "k" && (event.metaKey || event.ctrlKey)) || (event.key === "/" && !typing)) {
    event.preventDefault();
    const { path } = parseHash();
    if (path !== "/ask" && path !== "/") navigate("#/ask");
    requestAnimationFrame(() => document.querySelector(".ask-input")?.focus());
  }
});

document.getElementById("nav-toggle")?.addEventListener("click", () => document.body.classList.toggle("nav-open"));
document.querySelector(".side-scrim")?.addEventListener("click", () => document.body.classList.remove("nav-open"));
window.addEventListener("hashchange", route);
window.addEventListener("unhandledrejection", (event) => {
  if (event.reason?.name === "AbortError") return;
  console.error(event.reason);
  toast(event.reason?.message || "Something went wrong.", "err");
});

store.refresh().finally(route);
setInterval(() => { if (!document.hidden) store.refresh(); }, 30000);
