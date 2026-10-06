// Evaluate: score Weft against text-only RAG on a gold question set with
// known evidence, and show where each system found or missed it.

import { api } from "../api.js";
import { h, fill, put, pct, relTime, emptyState, skeletonLines, toast } from "../ui.js";

const METRICS = [
  ["recall", "Recall", "Share of required evidence found in the top k"],
  ["complete", "Complete", "Questions where every required piece was found"],
  ["mrr", "MRR", "How high the first relevant hit ranks"],
  ["ndcg", "nDCG", "Ranking quality over all relevant evidence"],
  ["precision", "Precision", "Share of the top k that is relevant"],
  ["modality_coverage", "Modalities", "Share of needed modalities covered"],
];
const TYPE_LABELS = {
  cross_modal: "Cross-modal",
  multi_part: "Multi-part",
  visual_only: "Visual only",
  exact_identifier: "Exact identifier",
  text: "Text",
};
const FILTERS = [
  ["all", "All"],
  ["wins", "Weft finds more"],
  ["both", "Both incomplete"],
];

export function renderEvaluate(root) {
  let disposed = false;
  let report = null;
  let k = 5;
  let filter = "all";
  let running = false;

  const kSelect = h("select", { class: "input select", "aria-label": "Evidence cut-off k", onchange: (e) => { k = Number(e.target.value); } },
    [3, 5, 8, 10].map((v) => h("option", { value: v, selected: v === k }, `Top ${v}`)));
  const runBtn = h("button", { class: "btn btn-primary", type: "button", onclick: run }, "Run evaluation");
  const body = h("div", { class: "eval" }, h("div", { class: "card card-pad" }, skeletonLines(5)));

  put(root,
    h("header", { class: "page-head" },
      h("div", {},
        h("h1", {}, "Evaluation"),
        h("p", { class: "muted" }, "A gold question set with known evidence, answered by four retrieval systems over an isolated copy of the demo corpus plus hard distractors."),
      ),
      h("div", { class: "row-inline" }, kSelect, runBtn),
    ),
    body,
  );

  async function run() {
    if (running) return;
    running = true;
    runBtn.disabled = true;
    runBtn.textContent = "Running…";
    try {
      report = await api.evalRun({ k });
      if (disposed) return;
      toast(`Scored ${report.question_count} questions at k=${report.k}.`, "ok");
      render();
    } catch (error) {
      if (!disposed) toast(error.message, "err");
    } finally {
      running = false;
      runBtn.disabled = false;
      runBtn.textContent = "Run evaluation";
    }
  }

  function render() {
    if (!report) {
      fill(body, h("div", { class: "card" }, emptyState({
        title: "No evaluation yet",
        body: "Run the gold set to compare Weft with a text-only RAG baseline. It builds its own corpus, so your library is untouched.",
        action: h("button", { class: "btn btn-primary", type: "button", onclick: run }, "Run evaluation"),
      })));
      return;
    }
    const names = Object.keys(report.systems);
    fill(body,
      meta(report),
      headline(report),
      h("section", {},
        h("p", { class: "section-label" }, `Retrieval quality at k=${report.k}`),
        systemsTable(report, names),
      ),
      h("section", {},
        h("p", { class: "section-label" }, `Complete@${report.k} by question type`),
        typeTable(report, names),
      ),
      h("section", {},
        h("div", { class: "section-row eval-q-head" },
          h("p", { class: "section-label" }, "Questions"),
          h("div", { class: "chips" }, FILTERS.map(([key, label]) => h("button", {
            class: "chip", type: "button", "aria-pressed": String(filter === key),
            onclick: () => { filter = key; render(); },
          }, label))),
        ),
        questionList(report),
      ),
    );
  }

  function meta(r) {
    const proxy = /hash/i.test(r.embedding || "");
    return h("div", { class: "eval-meta" },
      h("span", {}, h("b", {}, r.question_count), " questions"),
      h("span", {}, h("b", {}, r.corpus_segments), " segments"),
      h("span", {}, "corpus ", h("code", {}, r.corpus)),
      h("span", {}, "embedding ", h("code", {}, r.embedding || "default")),
      h("span", { class: "faint" }, `run ${relTime(r.generated_at)}`),
      proxy ? h("span", { class: "badge badge-warn" }, "Proxy embedding: numbers are not representative") : null,
    );
  }

  function headline(r) {
    const base = r.systems.text_rag?.metrics;
    const full = r.systems.weft?.metrics;
    if (!base || !full) return null;
    const tile = (label, key, hint) => {
      const delta = (full[key] || 0) - (base[key] || 0);
      return h("div", { class: "stat" },
        h("span", { class: "stat-l" }, label),
        h("span", { class: "stat-v num" }, key === "mrr" ? full[key].toFixed(2) : pct(full[key])),
        h("span", { class: `eval-delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}` },
          `${delta >= 0 ? "+" : "−"}${key === "mrr" ? Math.abs(delta).toFixed(2) : `${Math.round(Math.abs(delta) * 100)} pts`} vs text-only`),
        h("span", { class: "stat-hint faint" }, hint),
      );
    };
    return h("div", { class: "stats eval-stats" },
      tile(`Complete@${r.k}`, "complete", "every required piece found"),
      tile(`Recall@${r.k}`, "recall", "required evidence found"),
      tile("MRR", "mrr", "rank of first relevant hit"),
      tile("Modality coverage", "modality_coverage", "needed modalities reached"),
    );
  }

  function systemsTable(r, names) {
    const best = Object.fromEntries(METRICS.map(([key]) => [key, Math.max(...names.map((n) => r.systems[n].metrics[key] || 0))]));
    return h("div", { class: "card table-card" }, h("div", { class: "table-scroll" }, h("table", { class: "table eval-table" },
      h("thead", {}, h("tr", {},
        h("th", {}, "System"),
        METRICS.map(([, label, hint]) => h("th", { class: "num", title: hint }, label)),
        h("th", { class: "num", title: "Median retrieval latency" }, "p50"),
      )),
      h("tbody", {}, names.map((name) => {
        const s = r.systems[name];
        return h("tr", { class: name === "weft" ? "is-weft" : "" },
          h("td", { class: "cell-name", title: s.label }, h("span", { class: "name" }, shortName(name)), h("span", { class: "eval-sub faint" }, subLabel(name))),
          METRICS.map(([key]) => {
            const v = s.metrics[key] || 0;
            return h("td", { class: `num ${v === best[key] && v > 0 ? "is-best" : ""}` },
              h("span", { class: "bar-cell" },
                h("span", { class: "bar" }, h("i", { style: { width: `${v * 100}%` } })),
                key === "mrr" || key === "ndcg" ? v.toFixed(2) : pct(v)),
            );
          }),
          h("td", { class: "num faint" }, s.metrics.latency_ms_p50 != null ? `${s.metrics.latency_ms_p50} ms` : "—"),
        );
      })),
    )));
  }

  function typeTable(r, names) {
    const types = [...new Set(names.flatMap((n) => Object.keys(r.systems[n].by_type)))];
    return h("div", { class: "card table-card" }, h("div", { class: "table-scroll" }, h("table", { class: "table eval-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Question type"), h("th", { class: "num" }, "n"),
        names.map((n) => h("th", { class: `num ${n === "weft" ? "is-weft" : ""}`, title: r.systems[n].label }, shortName(n))))),
      h("tbody", {}, types.map((t) => {
        const row = names.map((n) => r.systems[n].by_type[t]?.complete ?? 0);
        const top = Math.max(...row);
        return h("tr", {},
          h("td", {}, TYPE_LABELS[t] || t),
          h("td", { class: "num faint" }, r.systems[names[0]].by_type[t]?.n ?? ""),
          row.map((v, i) => h("td", { class: `num ${names[i] === "weft" ? "is-weft" : ""} ${v === top && v > 0 ? "is-best" : ""}` }, pct(v))),
        );
      })),
    )));
  }

  function questionList(r) {
    const baseKey = r.systems.text_rag ? "text_rag" : Object.keys(r.systems)[0];
    const fullKey = r.systems.weft ? "weft" : Object.keys(r.systems).at(-1);
    const rows = r.questions.filter((q) => {
      const b = q.results[baseKey], f = q.results[fullKey];
      if (filter === "wins") return f.found_required.length > b.found_required.length;
      if (filter === "both") return !f.complete && !b.complete;
      return true;
    });
    if (!rows.length) return h("div", { class: "card" }, emptyState({ title: "No questions match", body: "Try another filter." }));
    return h("ol", { class: "eval-qs" }, rows.map((q) => {
      const col = (key) => {
        const res = q.results[key];
        const found = new Set(res.found_required);
        return h("div", { class: "eval-col" },
          h("div", { class: "eval-col-head" },
            h("span", {}, shortName(key)),
            h("span", { class: `num ${res.complete ? "ok" : "faint"}` }, `${found.size}/${q.required.length}`),
          ),
          h("ul", { class: "eval-ev" }, q.required.map((ref) => h("li", { class: found.has(ref) ? "hit" : "miss" },
            h("i", { class: "eval-mark", "aria-label": found.has(ref) ? "found" : "missed" }),
            h("span", { class: "eval-ref" }, refLabel(ref)),
            viaFor(res, ref),
          ))),
        );
      };
      return h("li", { class: "card eval-q" },
        h("div", { class: "eval-q-top" },
          h("span", { class: "loc" }, q.id),
          h("span", { class: "tag" }, TYPE_LABELS[q.type] || q.type),
        ),
        h("p", { class: "eval-q-text" }, q.question),
        h("div", { class: "eval-cols" }, col(baseKey), col(fullKey)),
      );
    }));
  }

  api.evalLatest()
    .then((data) => { if (!disposed) { report = data; k = data.k || k; kSelect.value = String(k); render(); } })
    .catch((error) => { if (!disposed) { if (error.status !== 404) toast(error.message, "err"); render(); } });

  return () => { disposed = true; };
}

function shortName(key) {
  return { text_rag: "Text-only RAG", dense_multimodal: "Dense multimodal", hybrid: "Hybrid", weft: "Weft" }[key] || key;
}

function subLabel(key) {
  return { text_rag: "baseline: transcript + text", dense_multimodal: "text + visual embeddings", hybrid: "dense + keyword + entity", weft: "+ decomposition + graph" }[key] || "";
}

function refLabel(ref) {
  const [source, loc] = ref.split("#");
  return loc && loc !== "*" ? `${source} · ${loc}` : source;
}

function viaFor(result, ref) {
  // A required piece reached through the graph rather than a direct match.
  const [source] = ref.split("#");
  const hit = result.retrieved.find((r) => r.required && r.via && r.source === source);
  return hit ? h("span", { class: "tag eval-via", title: "Reached through a cross-modal link" }, hit.via) : null;
}
