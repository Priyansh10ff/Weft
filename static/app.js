"use strict";

const API_BASE = ""; // same-origin: dashboard is served by the FastAPI app itself

const EXTENSION_ROUTES = {
  ".mp4": { endpoint: "/upload/video", modality: "video" },
  ".mp3": { endpoint: "/upload/audio", modality: "audio" },
  ".wav": { endpoint: "/upload/audio", modality: "audio" },
  ".m4a": { endpoint: "/upload/audio", modality: "audio" },
  ".aac": { endpoint: "/upload/audio", modality: "audio" },
  ".flac": { endpoint: "/upload/audio", modality: "audio" },
  ".ogg": { endpoint: "/upload/audio", modality: "audio" },
  ".png": { endpoint: "/upload/image", modality: "image" },
  ".jpg": { endpoint: "/upload/image", modality: "image" },
  ".jpeg": { endpoint: "/upload/image", modality: "image" },
  ".pdf": { endpoint: "/upload/pdf", modality: "pdf" },
  ".json": { endpoint: "/upload/json", modality: "json" },
  ".txt": { endpoint: "/upload/json", modality: "json" },
};

// ---------------------------------------------------------------
// health
// ---------------------------------------------------------------

async function checkHealth() {
  const indicator = document.getElementById("status-indicator");
  try {
    const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    indicator.dataset.state = "online";
    indicator.querySelector(".status-text").textContent = "online";
  } catch (err) {
    indicator.dataset.state = "offline";
    indicator.querySelector(".status-text").textContent = "offline";
  }
}

// ---------------------------------------------------------------
// pipeline log
// ---------------------------------------------------------------

function logLine(message, kind = "muted") {
  const feed = document.getElementById("log-feed");
  const line = document.createElement("div");
  line.className = `log-line log-${kind}`;
  const time = new Date().toLocaleTimeString([], { hour12: false });
  line.innerHTML = `<span class="log-prefix">[${time}]</span><span>${escapeHtml(message)}</span>`;
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
}

document.getElementById("clear-log").addEventListener("click", () => {
  document.getElementById("log-feed").innerHTML = "";
  logLine("log cleared", "muted");
});

// ---------------------------------------------------------------
// ingestion
// ---------------------------------------------------------------

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (event) => {
    event.preventDefault();
    dropzone.classList.add("drag-over");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (event) => {
    event.preventDefault();
    dropzone.classList.remove("drag-over");
  })
);
dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files?.[0];
  if (file) uploadSource(file);
});
fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  if (file) uploadSource(file);
  fileInput.value = "";
});

function extensionOf(filename) {
  const idx = filename.lastIndexOf(".");
  return idx === -1 ? "" : filename.slice(idx).toLowerCase();
}

async function uploadSource(file) {
  const route = EXTENSION_ROUTES[extensionOf(file.name)];
  if (!route) {
    logLine(`unsupported file type: ${file.name}`, "err");
    return;
  }

  logLine(`uploading ${file.name} (${route.modality}) &rarr; ${route.endpoint}`, "info");
  dropzone.classList.add("drag-over");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch(`${API_BASE}${route.endpoint}`, {
      method: "POST",
      body: formData,
    });
    const payload = await safeJson(response);
    if (!response.ok) {
      const detail = payload?.detail || `HTTP ${response.status}`;
      logLine(`failed: ${detail}`, "err");
      return;
    }
    const indexed = payload.indexed_count ?? payload.processed_nodes ?? 0;
    if (payload.status === "deduplicated") {
      logLine(`${file.name} was already ingested (${indexed} segment(s)); skipped`, "info");
    } else {
      const extra = payload.entity_count ? `, ${payload.entity_count} entities, ${payload.relation_count} relations` : "";
      logLine(`indexed ${indexed} segment(s) from ${file.name}${extra}`, "ok");
    }
    (payload.warnings || []).forEach((warning) => logLine(`warning: ${warning}`, "info"));
  } catch (err) {
    logLine(`could not reach the backend: ${err.message}`, "err");
  } finally {
    dropzone.classList.remove("drag-over");
  }
}

// ---------------------------------------------------------------
// query + comparison
// ---------------------------------------------------------------

const queryForm = document.getElementById("query-form");
const queryInput = document.getElementById("query-input");
const runBtn = document.getElementById("run-btn");
const emptyState = document.getElementById("empty-state");
const resultsEl = document.getElementById("results");

queryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  runBtn.disabled = true;
  runBtn.textContent = "running…";
  logLine(`query: "${query}"`, "info");

  try {
    const [compareRes, searchRes] = await Promise.all([
      fetch(`${API_BASE}/query/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 5 }),
      }),
      fetch(`${API_BASE}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 8 }),
      }),
    ]);

    const comparePayload = await safeJson(compareRes);
    const searchPayload = await safeJson(searchRes);

    if (!compareRes.ok) throw new Error(comparePayload?.detail || `HTTP ${compareRes.status}`);
    if (!searchRes.ok) throw new Error(searchPayload?.detail || `HTTP ${searchRes.status}`);

    renderComparison(query, comparePayload);
    renderHits(searchPayload.results || []);

    emptyState.hidden = true;
    resultsEl.hidden = false;
    logLine(`returned ${searchPayload.results?.length ?? 0} ranked result(s)`, "ok");
  } catch (err) {
    logLine(`query failed: ${err.message}`, "err");
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "run";
  }
});

function renderComparison(query, comparison) {
  const header = document.getElementById("diff-hunk-header");
  const mmScore = comparison.multimodal_result?.similarity_score;
  const baseScore = comparison.text_only_baseline_result?.similarity_score;
  header.textContent =
    `@@ "${query}" @@ multimodal ${fmtScore(mmScore)}  |  baseline ${fmtScore(baseScore)}`;

  document.getElementById("diff-multimodal").innerHTML = renderDiffCol(
    comparison.multimodal_result,
    true
  );
  document.getElementById("diff-baseline").innerHTML = renderDiffCol(
    comparison.text_only_baseline_result,
    false
  );
}

function renderDiffCol(result, isMultimodal) {
  if (!result) {
    return `<div class="diff-empty">no ${isMultimodal ? "multimodal" : "text-only"} match indexed yet</div>`;
  }
  const rows = [
    ["source", result.source || "—"],
    ["modality", result.modality || "—"],
    ["at", result.timestamp || "—"],
    ["score", fmtScore(result.similarity_score)],
    ["transcript", result.transcript || (isMultimodal ? null : "missed — no transcript text matched")],
  ];
  if (isMultimodal) {
    rows.push(["visual", result.visual_summary || "no visual evidence for this hit"]);
  }

  let html = rows
    .map(([key, val]) => {
      const isDim = val === null || val === undefined;
      return `<div class="diff-row">
        <div class="diff-row-key">${key}</div>
        <div class="diff-row-val${isDim ? " dim" : ""}">${escapeHtml(String(val ?? "—"))}</div>
      </div>`;
    })
    .join("");

  if (result.frame_path) {
    html += `<img class="diff-frame" src="${API_BASE}${result.frame_path}" alt="Extracted visual evidence" loading="lazy" />`;
  }
  return html;
}

function renderHits(hits) {
  const list = document.getElementById("hits-list");
  const count = document.getElementById("hits-count");
  count.textContent = `${hits.length} hit${hits.length === 1 ? "" : "s"}`;
  list.innerHTML = "";

  if (hits.length === 0) {
    list.innerHTML = `<div class="diff-empty">no results — upload sources first</div>`;
    return;
  }

  for (const hit of hits) {
    const card = document.createElement("div");
    card.className = "hit-card";

    const thumb = hit.frame_path
      ? `<img class="hit-thumb" src="${API_BASE}${hit.frame_path}" alt="" loading="lazy" />`
      : `<div class="hit-thumb-placeholder">no frame</div>`;

    const snippet = hit.transcript || hit.visual_summary || "(no text content)";
    const scorePct = Math.round((hit.similarity_score ?? 0) * 100);

    card.innerHTML = `
      ${thumb}
      <div class="hit-body">
        <div class="hit-meta">
          <span class="tag tag-${hit.modality || "pdf"}">${hit.modality || "unknown"}</span>
          <span class="hit-source">${escapeHtml(hit.source || "unknown source")}</span>
          <span>${escapeHtml(hit.timestamp || "")}</span>
        </div>
        <div class="hit-text">${escapeHtml(snippet)}</div>
      </div>
      <div class="hit-score">
        <strong>${scorePct}%</strong>
        match
        <div class="score-bar"><div class="score-bar-fill" style="width:${scorePct}%"></div></div>
      </div>
    `;
    list.appendChild(card);
  }
}

// ---------------------------------------------------------------
// helpers
// ---------------------------------------------------------------

function fmtScore(score) {
  if (score === null || score === undefined) return "—";
  return `${Math.round(score * 100)}%`;
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

// ---------------------------------------------------------------
// boot
// ---------------------------------------------------------------

checkHealth();
setInterval(checkHealth, 15000);
