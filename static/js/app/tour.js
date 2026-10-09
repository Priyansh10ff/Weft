// Guided tour: four stops (Ask, Evidence, Graph, Evaluate) over the sample
// data. A small panel stays in the corner across route changes; each step can
// act for the user (ask the question, open the evidence) so the tour works
// with no typing.

import { api } from "./api.js";
import { openEvidence, closeEvidence } from "./evidence.js";
import { h, fill, icon, toast } from "./ui.js";

const QUESTION = "How did the team fix the checkout timeout issue, and how do we know it worked?";

const STEPS = [
  {
    title: "Ask across every format",
    body: "This question needs a recording, a postmortem and a dashboard screenshot. Weft splits it into parts, searches by meaning, keyword and entity, and answers with numbered citations.",
    action: "Ask it",
    run: ({ navigate }) => navigate(`#/ask?q=${encodeURIComponent(QUESTION)}`),
  },
  {
    title: "Open the evidence",
    body: "Each card says where the match is and why it was returned. Opening one plays the video from the cited second, or shows the page or the matched region of an image, with everything linked to it.",
    action: "Open the top evidence",
    run: () => {
      // Prefer a video: it opens at the cited second.
      const card = document.querySelector(".ev-card.mod-video[id^='ev-']") || document.querySelector(".ev-card[id^='ev-']");
      if (card) card.click();
      else toast("Ask the question first, then open the evidence.", "info");
    },
  },
  {
    title: "See how it connects",
    body: "Slides are linked to what was said over them, charts to the documents that state the same numbers, and the same system or metric across files becomes one entity.",
    action: "Open the graph",
    run: ({ navigate }) => { closeEvidence(); navigate("#/graph"); },
  },
  {
    title: "Check it against text-only RAG",
    body: "A gold set of 24 questions with known evidence, scored for text-only RAG, dense, hybrid and full Weft. Run it to see where each system finds or misses the evidence.",
    action: "Open the evaluation",
    run: ({ navigate }) => navigate("#/evaluate"),
  },
];

let panel = null;
let index = 0;

export function startTour({ navigate, store }) {
  index = 0;
  if (!panel) {
    panel = h("aside", { class: "tour card", role: "dialog", "aria-label": "Guided tour", "aria-live": "polite" });
    document.body.append(panel);
  }
  const ensureData = async () => {
    if (store.stats?.segments) return true;
    if (!store.health?.demo_enabled) {
      toast("The sample data is disabled on this server. Upload your own files to explore.", "err");
      return false;
    }
    try {
      const result = await api.seedDemo();
      toast(`Loaded ${result.sources} sample sources.`, "ok");
      await store.refresh();
      return true;
    } catch (error) {
      toast(error.message, "err");
      return false;
    }
  };

  const draw = () => {
    const step = STEPS[index];
    fill(panel,
      h("div", { class: "tour-head" },
        h("span", { class: "section-label" }, `Tour · ${index + 1} of ${STEPS.length}`),
        h("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "End tour", onclick: end }, icon("close", 14)),
      ),
      h("h3", { class: "tour-title" }, step.title),
      h("p", { class: "tour-body" }, step.body),
      h("div", { class: "tour-actions" },
        h("button", {
          class: "btn btn-primary btn-sm", type: "button",
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            if (await ensureData()) step.run({ navigate });
            button.disabled = false;
          },
        }, step.action),
        h("span", { class: "tour-nav" },
          index > 0 ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { index -= 1; draw(); } }, "Back") : null,
          index < STEPS.length - 1
            ? h("button", { class: "btn btn-sm", type: "button", onclick: () => { index += 1; draw(); } }, "Next", icon("arrowRight", 13))
            : h("button", { class: "btn btn-sm", type: "button", onclick: end }, "Finish"),
        ),
      ),
      h("div", { class: "tour-dots", "aria-hidden": "true" }, STEPS.map((_, i) => h("i", { class: i === index ? "on" : "" }))),
    );
  };

  function end() {
    panel?.remove();
    panel = null;
  }

  draw();
  ensureData();
}
