// Library: upload sources (as background jobs with live progress) and manage
// everything that has been ingested.

import { api, uploadFile, modalityFor, UPLOAD_ROUTES } from "../api.js";
import { h, clear, icon, modChip, fmtBytes, relTime, emptyState, toast, skeletonLines, fill, put } from "../ui.js";
import { navigate, store } from "../main.js";

const ACCEPT = Object.keys(UPLOAD_ROUTES).join(",");
const STAGE_LABEL = {
  queued: "Queued", requeued: "Queued", starting: "Starting", extracting: "Extracting",
  "attributing speakers": "Identifying speakers", "extracting entities": "Extracting entities",
  extracted: "Extracted", indexing: "Indexing", done: "Done", failed: "Failed",
};

// Upload rows (and their polling) survive navigation within the session;
// whichever Library view is mounted re-renders when they change.
const uploads = [];
const active = { render: null, refresh: null };

async function poll(item) {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    let job;
    try { job = await api.job(item.jobId); } catch { continue; }
    item.progress = job.progress;
    item.message = STAGE_LABEL[job.stage] || job.stage;
    if (job.status === "succeeded") {
      item.state = "done";
      item.sourceId = job.result?.source_id;
      const segs = job.result?.segments ?? 0;
      const ents = job.result?.entities ?? 0;
      item.message = `${segs} segment${segs === 1 ? "" : "s"} · ${ents} entit${ents === 1 ? "y" : "ies"}`;
      (job.warnings || []).forEach((w) => toast(`${item.name}: ${w}`, "warn", 7000));
      toast(`${item.name} is ready.`, "ok");
      break;
    }
    if (job.status === "failed") {
      item.state = "error";
      item.message = job.error || "Processing failed";
      toast(`${item.name}: ${item.message}`, "err", 8000);
      break;
    }
    active.render?.();
  }
  active.render?.();
  if (active.refresh) active.refresh(); else store.refresh();
}

function statCell(label, value) {
  return h("div", { class: "stat" }, h("span", { class: "stat-v num" }, value ?? "—"), h("span", { class: "stat-l" }, label));
}

export function renderLibrary(root) {
  let disposed = false;
  const statsRow = h("div", { class: "stats" });
  const uploadList = h("div", { class: "uploads" });
  const tableHost = h("div", { class: "card table-card" });
  const fileInput = h("input", { type: "file", multiple: true, accept: ACCEPT, class: "sr-only", id: "file-input" });

  const drop = h("label", { class: "dropzone", for: "file-input", tabindex: "0" },
    fileInput,
    h("span", { class: "drop-icon" }, icon("upload", 20)),
    h("span", { class: "drop-title" }, "Drop files here, or ", h("u", {}, "browse")),
    h("span", { class: "drop-sub muted" }, "MP4 · MP3 / WAV / M4A · PNG / JPG · PDF · JSON / TXT"),
  );
  drop.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (event) => startUploads([...event.dataTransfer.files]));
  fileInput.addEventListener("change", () => { startUploads([...fileInput.files]); fileInput.value = ""; });

  put(root, 
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, "Library"), h("p", { class: "muted" }, "Everything Weft has ingested. Uploads run in the background; you can keep working.")),
    ),
    statsRow,
    drop,
    uploadList,
    h("div", { class: "section-row" }, h("p", { class: "section-label" }, "Sources")),
    tableHost,
  );

  function renderStats() {
    const s = store.stats;
    fill(statsRow, 
      statCell("Sources", s?.sources), statCell("Segments", s?.segments),
      statCell("Entities", s?.entities), statCell("Relations", s?.relations),
    );
  }

  function renderUploads() {
    clear(uploadList);
    for (const item of uploads) {
      const progress = Math.round((item.progress || 0) * 100);
      uploadList.append(h("div", { class: `upload-row ${item.state}` },
        modChip(item.modality),
        h("span", { class: "up-name", title: item.name }, item.name),
        h("span", { class: "up-stage muted" }, item.message),
        h("span", { class: "up-bar" }, h("i", { style: { width: `${item.state === "done" ? 100 : progress}%` } })),
        item.state === "done" && item.sourceId
          ? h("a", { class: "btn btn-sm", href: `#/sources/${item.sourceId}` }, "Open")
          : h("span", { class: "num up-pct" }, item.state === "error" ? "" : `${progress}%`),
      ));
    }
  }

  async function startUploads(files) {
    for (const file of files) {
      const modality = modalityFor(file.name);
      if (!modality) { toast(`${file.name}: unsupported file type.`, "err"); continue; }
      const item = { name: file.name, modality, state: "uploading", progress: 0, message: "Uploading" };
      uploads.unshift(item);
      active.render?.();
      try {
        const response = await uploadFile(file, {
          onProgress: (fraction) => { item.progress = fraction * 0.3; item.message = `Uploading ${fmtBytes(file.size)}`; active.render?.(); },
        });
        if (response.status === "deduplicated") {
          item.state = "done";
          item.sourceId = response.source_id;
          item.message = "Already in your library";
          active.render?.();
          continue;
        }
        item.state = "processing";
        item.jobId = response.job_id;
        item.message = "Queued";
        active.render?.();
        poll(item);
      } catch (error) {
        item.state = "error";
        item.message = error.message;
        toast(`${file.name}: ${error.message}`, "err", 8000);
        active.render?.();
      }
    }
  }

  async function seed(button) {
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      const result = await api.seedDemo();
      toast(`Loaded ${result.sources} sample sources.`, "ok");
      refresh();
    } catch (error) {
      toast(error.message, "err");
      button.disabled = false;
      button.textContent = "Load sample data";
    }
  }

  function deleteButton(summary) {
    const button = h("button", { class: "btn btn-ghost btn-icon btn-danger", type: "button", title: "Delete source", "aria-label": `Delete ${summary.source.filename}` }, icon("trash", 15));
    let armed = false;
    let timer;
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!armed) {
        armed = true;
        button.classList.add("armed");
        button.replaceChildren(document.createTextNode("Delete?"));
        timer = setTimeout(() => { armed = false; button.classList.remove("armed"); button.replaceChildren(icon("trash", 15)); }, 3000);
        return;
      }
      clearTimeout(timer);
      button.disabled = true;
      try {
        await api.deleteSource(summary.source.id);
        toast(`Deleted ${summary.source.filename}.`, "ok");
        refresh();
      } catch (error) {
        toast(error.message, "err");
        button.disabled = false;
      }
    });
    return button;
  }

  function renderTable(sources) {
    clear(tableHost);
    if (!sources.length) {
      const seedBtn = h("button", { class: "btn btn-primary", type: "button" }, "Load sample data");
      seedBtn.addEventListener("click", () => seed(seedBtn));
      put(tableHost, emptyState({
        title: "No sources yet",
        body: "Upload a recording, a deck or a screenshot above. Or load the sample dataset to explore Weft without API keys.",
        action: store.health?.demo_enabled ? seedBtn : null,
      }));
      return;
    }
    put(tableHost, h("div", { class: "table-scroll" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Name", "Type", "Segments", "Size", "Added", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, sources.map((summary) => {
        const src = summary.source;
        const warnings = src.attributes?.warnings || [];
        return h("tr", { class: "row-link", tabindex: "0", onclick: () => navigate(`#/sources/${src.id}`), onkeydown: (e) => { if (e.key === "Enter") navigate(`#/sources/${src.id}`); } },
          h("td", { class: "cell-name" },
            h("span", { class: "name", title: src.filename }, src.filename),
            src.status === "index_failed" ? h("span", { class: "badge err" }, "Index failed") : null,
            warnings.length ? h("span", { class: "badge warn", title: warnings.join("\n") }, `${warnings.length} warning${warnings.length > 1 ? "s" : ""}`) : null,
          ),
          h("td", {}, modChip(src.modality)),
          h("td", { class: "num" }, summary.segment_count),
          h("td", { class: "num" }, fmtBytes(src.size_bytes)),
          h("td", { class: "muted", title: new Date(src.ingested_at).toLocaleString() }, relTime(src.ingested_at)),
          h("td", { class: "cell-actions" }, deleteButton(summary)),
        );
      })),
    )));
  }

  async function refresh() {
    try {
      const [sources] = await Promise.all([api.sources(), store.refresh()]);
      if (disposed) return;
      renderStats();
      renderTable(sources);
    } catch (error) {
      if (disposed) return;
      fill(tableHost, emptyState({ title: "Couldn't load your library", body: error.message }));
    }
  }

  active.render = renderUploads;
  active.refresh = refresh;
  renderStats();
  renderUploads();
  put(tableHost, h("div", { class: "card-pad" }, skeletonLines(4)));
  refresh();

  return () => {
    disposed = true;
    if (active.render === renderUploads) { active.render = null; active.refresh = null; }
  };
}
