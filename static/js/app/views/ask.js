// Ask: one question across every modality, a grounded answer with citations,
// and the evidence behind it. Optional side-by-side with text-only RAG.

import { api } from "../api.js";
import { openEvidence } from "../evidence.js";
import { h, clear, icon, modChip, meter, pct, thumb, truncate, skeletonLines, emptyState, toast, modalityLabel, fmtClock, fill, put } from "../ui.js";
import { navigate, store } from "../main.js";

const EXAMPLES = [
  "How did the team fix the checkout timeout issue, and how do we know it worked?",
  "Why was the onboarding permissions flow redesigned, and did completion improve?",
  "What does the retry policy look like?",
  "Who reported problems during the incident?",
];

const CITATION = /\(\s*(?:Evidence|Ev\.?|Source)\s*\d+(?:\s*(?:,|and|&)\s*(?:Evidence|Ev\.?|Source)?\s*\d+)*\s*\)|\[(\d+(?:\s*,\s*\d+)*)\]/gi;

function renderAnswerText(text, results, onCite) {
  const container = h("p", { class: "answer-text" });
  let last = 0;
  for (const match of text.matchAll(CITATION)) {
    container.append(text.slice(last, match.index).replace(/\s+$/, ""));
    for (const num of match[0].match(/\d+/g) || []) {
      const index = Number(num) - 1;
      const hit = results[index];
      if (!hit) continue;
      container.append(h("button", {
        class: `cite mod-${hit.modality}`, type: "button",
        title: `${hit.source || ""} ${hit.timestamp || ""}`.trim(),
        onclick: () => onCite(index),
      }, num));
    }
    last = match.index + match[0].length;
  }
  container.append(text.slice(last));
  return container;
}

const VIA_PHRASE = {
  depicts: { out: "Linked: explained in", in: "Linked: shown in" },
  corroborates: { out: "Linked: also stated in", in: "Linked: also stated in" },
  co_occurs: { out: "Linked: same scene as", in: "Linked: same scene as" },
  shows_same: { out: "Linked: same slide as", in: "Linked: same slide as" },
};

function whyLine(hit, indexOf) {
  if (hit.via) {
    const from = indexOf.get(hit.via.from_segment_id);
    const phrase = VIA_PHRASE[hit.via.relation]?.[hit.via.direction === "out" ? "in" : "out"] || `Linked (${hit.via.relation})`;
    const shared = hit.via.shared_entities?.length ? ` · shared: ${hit.via.shared_entities.slice(0, 3).join(", ")}` : "";
    return h("p", { class: "ev-why via" }, icon("graph", 13), `${phrase} ${from != null ? `#${from + 1}` : "another hit"}${shared}`);
  }
  const s = hit.signals || {};
  const parts = [];
  if (s.dense_rank) parts.push("meaning");
  if (s.keyword_rank) parts.push("keywords");
  if (s.matched_entities?.length) parts.push(`entities: ${s.matched_entities.slice(0, 3).join(", ")}`);
  return parts.length ? h("p", { class: "ev-why" }, `Matched on ${parts.join(" · ")}`) : null;
}

function evidenceCard(hit, index, cited, query, indexOf = new Map()) {
  const span = hit.matched_span;
  const at = span?.start_seconds ?? null;
  const open = () => hit.segment_id && openEvidence(hit.segment_id, { query, at });
  const card = h("article", {
    class: `ev-card mod-${hit.modality}${cited ? " cited" : ""}${hit.via ? " linked" : ""}`, tabindex: "0", role: "button",
    "aria-label": `Evidence ${index + 1}: ${hit.source || "unknown source"} ${hit.timestamp || ""}`,
    id: `ev-${index}`,
    onclick: open,
    onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } },
  },
  thumb(hit.frame_path, hit.modality),
  h("div", { class: "ev-body" },
    h("div", { class: "ev-head" },
      h("span", { class: "ev-n" }, String(index + 1)),
      modChip(hit.modality),
      h("span", { class: "ev-source", title: hit.source || "" }, hit.source || "Unknown source"),
      span?.start_seconds != null
        ? h("span", { class: "loc loc-exact", title: `Window ${hit.timestamp || ""}` }, `at ${fmtClock(span.start_seconds)}`)
        : hit.timestamp ? h("span", { class: "loc" }, hit.timestamp) : null,
    ),
    span?.text
      ? h("p", { class: "ev-text" }, span.speaker ? h("span", { class: "speaker-inline" }, `${span.speaker}: `) : null, `“${truncate(span.text, 220)}”`)
      : hit.transcript ? h("p", { class: "ev-text" }, truncate(hit.transcript, 260)) : null,
    hit.matched_regions?.length
      ? h("p", { class: "ev-visual" }, h("span", { class: "faint" }, "On screen: "), hit.matched_regions.map((r) => `“${truncate(r.text, 80)}”`).join("  "))
      : hit.visual_summary ? h("p", { class: "ev-visual" }, h("span", { class: "faint" }, "Shown: "), truncate(hit.visual_summary, 200)) : null,
    whyLine(hit, indexOf),
    h("div", { class: "ev-foot" },
      hit.score != null ? h("span", { class: "num", title: "Fused, confidence-weighted relevance" }, `score ${pct(Math.min(1, hit.score))}`) : h("span", { class: "num" }, `match ${pct(hit.similarity_score)}`),
      hit.confidence != null ? meter(hit.confidence) : null,
      (hit.speakers || []).slice(0, 2).map((name) => h("span", { class: "chip chip-sm chip-speaker" }, name)),
      (hit.entities || []).slice(0, 4).map((name) => h("span", { class: "chip chip-sm" }, name)),
    ),
  ));
  return card;
}

function strategyNote(response) {
  const st = response.strategy || {};
  const bits = [st.hybrid ? "hybrid search" : "vector search"];
  if (st.subqueries) bits.push(`${st.subqueries} sub-questions`);
  if (st.expanded_hits) bits.push(`${st.expanded_hits} via graph links`);
  return h("span", { class: "faint strategy" }, bits.join(" · "));
}

function compareColumn(title, subtitle, hit, query, accent) {
  return h("div", { class: `cmp-col${accent ? " accent" : ""}` },
    h("div", { class: "cmp-head" }, h("h3", {}, title), h("p", { class: "muted" }, subtitle)),
    hit ? evidenceCard(hit, 0, false, query) : emptyState({ title: "No match", body: "Nothing passed the relevance threshold." }),
    hit && !accent && !hit.visual_summary ? h("p", { class: "cmp-note" }, "Text only: what was shown on screen is not searchable here.") : null,
  );
}

export function renderAsk(root, params) {
  const initial = params.get("q") || "";
  let controller = null;

  const input = h("textarea", {
    class: "ask-input", rows: "1", placeholder: "Ask across every recording, slide, screenshot and document…",
    "aria-label": "Question", maxlength: "1000",
  });
  input.value = initial;
  const compare = h("input", { type: "checkbox", id: "cmp-toggle" });
  compare.checked = params.get("compare") === "1";
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Ask", h("kbd", { class: "kbd-inv" }, "↵"));
  const form = h("form", { class: "ask-form", role: "search" },
    h("div", { class: "ask-field" }, icon("search", 18), input, submit),
    h("div", { class: "ask-options" },
      h("label", { class: "switch", for: "cmp-toggle" }, compare, h("span", { class: "switch-ui" }), "Compare with text-only RAG"),
      h("span", { class: "faint hide-sm" }, h("kbd", {}, "/"), " to focus"),
    ),
  );
  const out = h("div", { class: "ask-out", "aria-live": "polite" });

  put(root, 
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, "Ask"), h("p", { class: "muted" }, "Questions are answered from speech, on-screen visuals, documents and records together.")),
    ),
    form,
    out,
  );

  const autosize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });

  function showIntro() {
    const examples = h("div", { class: "examples" }, EXAMPLES.map((q) =>
      h("button", { class: "example", type: "button", onclick: () => { input.value = q; autosize(); form.requestSubmit(); } }, icon("arrowRight", 14), q)));
    const stats = store.stats;
    const emptyLibrary = stats && stats.segments === 0;
    fill(out, 
      emptyLibrary
        ? h("div", { class: "card card-pad notice" },
          h("div", {}, h("h3", {}, "Your library is empty"), h("p", { class: "muted" }, "Upload sources in the Library, or load the sample dataset: an incident review and an onboarding redesign spread across video, PDF, screenshots and tickets.")),
          h("div", { class: "notice-actions" },
            store.health?.demo_enabled ? h("button", { class: "btn btn-primary", type: "button", onclick: seed }, "Load sample data") : null,
            h("a", { class: "btn", href: "#/library" }, icon("upload", 14), "Upload"),
          ))
        : null,
      h("p", { class: "section-label" }, "Try asking"),
      examples,
    );
  }

  async function seed(event) {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      const result = await api.seedDemo();
      toast(`Loaded ${result.sources} sample sources (${result.segments} segments).`, "ok");
      await store.refresh();
      showIntro();
    } catch (error) {
      toast(error.message, "err");
      button.disabled = false;
      button.textContent = "Load sample data";
    }
  }

  async function run(query) {
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    const withCompare = compare.checked;
    fill(out, h("div", { class: "card answer-card" }, skeletonLines(4)), h("div", { class: "ev-grid" }, [0, 1, 2].map(() => h("div", { class: "ev-card skeleton-card" }, h("div", { class: "skeleton thumb" }), skeletonLines(3)))));
    submit.disabled = true;
    try {
      const [response, comparison] = await Promise.all([
        api.query(query, 8, signal),
        withCompare ? api.compare(query, 8, signal) : Promise.resolve(null),
      ]);
      renderResults(query, response, comparison);
    } catch (error) {
      if (error.name === "AbortError") return;
      fill(out, emptyState({ title: "The question couldn't be answered", body: error.message }));
    } finally {
      submit.disabled = false;
    }
  }

  function renderResults(query, response, comparison) {
    const results = response.results || [];
    clear(out);
    if (!results.length) {
      put(out, emptyState({
        title: "No evidence matched",
        body: store.stats?.segments ? "Nothing in your library was close enough to this question. Try rephrasing, or upload sources that cover it." : "Your library is empty. Upload sources or load the sample data first.",
        action: store.stats?.segments ? null : h("a", { class: "btn", href: "#/library" }, "Go to Library"),
      }));
      return;
    }

    const answer = response.answer;
    const citedSet = new Set((answer?.sources || []).filter((s) => s.cited).map((s) => s.evidence_index - 1));
    const modalities = [...new Set(results.map((r) => r.modality).filter(Boolean))];
    const indexOf = new Map(results.map((r, i) => [r.segment_id, i]));
    const focusEvidence = (index) => {
      const card = out.querySelector(`#ev-${index}`);
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
      card?.classList.add("flash");
      setTimeout(() => card?.classList.remove("flash"), 1200);
      if (results[index]?.segment_id) openEvidence(results[index].segment_id, { query, at: results[index].matched_span?.start_seconds ?? null });
    };

    if (answer) {
      const method = answer.method === "llm" ? "Synthesized by Gemini" : "Extractive summary (no LLM key configured)";
      put(out, h("section", { class: "card answer-card" },
        h("div", { class: "answer-meta" },
          h("span", { class: `badge ${answer.grounded ? "ok" : "warn"}` }, icon(answer.grounded ? "check" : "alert", 13), answer.grounded ? "Grounded in evidence" : "Not supported by evidence"),
          h("span", { class: "faint" }, method),
          strategyNote(response),
          h("span", { class: "answer-mods" }, modalities.map((m) => h("span", { class: `mod mod-${m}`, title: modalityLabel(m) }, h("i", { class: "dot" })))),
        ),
        renderAnswerText(answer.answer, results, focusEvidence),
        response.subqueries?.length
          ? h("div", { class: "subqs" }, h("span", { class: "faint" }, "Answered in parts:"), response.subqueries.map((q) => h("span", { class: "chip chip-sm" }, truncate(q, 70))))
          : null,
      ));
    }

    if (comparison) {
      const same = comparison.multimodal_result?.segment_id && comparison.multimodal_result.segment_id === comparison.text_only_baseline_result?.segment_id;
      put(out, h("section", { class: "cmp" },
        h("p", { class: "section-label" }, "Top match: Weft vs text-only RAG"),
        h("div", { class: "cmp-grid" },
          compareColumn("Weft", "Speech, text and visual descriptions", comparison.multimodal_result, query, true),
          compareColumn("Text-only baseline", "Transcript and document text only", comparison.text_only_baseline_result, query, false),
        ),
        same ? h("p", { class: "cmp-note" }, "Both found the same top segment for this question. The difference shows on questions about what was on screen.") : null,
      ));
    }

    put(out, 
      h("div", { class: "ev-head-row" },
        h("p", { class: "section-label" }, `Evidence · ${results.length} segment${results.length === 1 ? "" : "s"} from ${new Set(results.map((r) => r.source_id || r.source)).size} source${new Set(results.map((r) => r.source_id || r.source)).size === 1 ? "" : "s"}`),
      ),
      h("div", { class: "ev-grid" }, results.map((hit, i) => evidenceCard(hit, i, citedSet.has(i), query, indexOf))),
    );
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) { input.focus(); return; }
    const next = new URLSearchParams({ q: query });
    if (compare.checked) next.set("compare", "1");
    navigate(`#/ask?${next}`, { replace: true, silent: true });
    run(query);
  });

  autosize();
  if (initial) run(initial); else showIntro();
  if (!initial) setTimeout(() => input.focus(), 0);

  return () => controller?.abort();
}
