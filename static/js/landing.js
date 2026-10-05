// Landing page: builds the hero "loom" and handles small nav niceties.

const MODALITIES = [
  ["VIDEO", "var(--m-video)"],
  ["AUDIO", "var(--m-audio)"],
  ["IMAGE", "var(--m-image)"],
  ["PDF", "var(--m-pdf)"],
  ["JSON", "var(--m-json)"],
];

const NS = "http://www.w3.org/2000/svg";
const svgEl = (name, attrs = {}) => {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};

function buildLoom(host) {
  const width = 520;
  const height = 440;
  const left = 84;
  const right = 504;
  const rows = MODALITIES.map((_, i) => 78 + i * 70);
  const warps = Array.from({ length: 10 }, (_, i) => 112 + i * 42);
  const queryIndex = 6;
  const queryX = warps[queryIndex];
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Five modality threads woven together, with one query thread crossing all of them" });

  // Warp threads (the structure every source is woven onto).
  warps.forEach((x, i) => {
    if (i === queryIndex) return;
    svg.append(svgEl("line", { x1: x, y1: 36, x2: x, y2: height - 24, stroke: "var(--line-2)", "stroke-width": 2, "stroke-linecap": "round" }));
  });

  // Weft threads, one per modality.
  rows.forEach((y, r) => {
    const [label, color] = MODALITIES[r];
    const text = svgEl("text", { x: 0, y: y + 4, class: "label" });
    text.textContent = label;
    svg.append(text);
    const path = svgEl("line", { x1: left, y1: y, x2: right, y2: y, stroke: color, "stroke-width": 3, "stroke-linecap": "round", class: "weft" });
    path.style.setProperty("--len", String(right - left));
    path.style.animationDelay = `${r * 0.12}s`;
    svg.append(path);
  });

  // Where a warp passes over a weft, redraw it on top with a gap.
  rows.forEach((y, r) => {
    warps.forEach((x, c) => {
      if (c === queryIndex || (r + c) % 2 === 0) return;
      svg.append(svgEl("line", { x1: x, y1: y - 9, x2: x, y2: y + 9, stroke: "var(--bg)", "stroke-width": 6 }));
      svg.append(svgEl("line", { x1: x, y1: y - 9, x2: x, y2: y + 9, stroke: "var(--line-2)", "stroke-width": 2, "stroke-linecap": "round" }));
    });
  });

  // The query: one thread that crosses every modality.
  const qLabel = svgEl("text", { x: queryX, y: 20, "text-anchor": "middle", class: "q-label" });
  qLabel.textContent = "QUERY";
  svg.append(qLabel);
  svg.append(svgEl("line", { x1: queryX, y1: 36, x2: queryX, y2: height - 24, stroke: "var(--bg)", "stroke-width": 8 }));
  svg.append(svgEl("line", { x1: queryX, y1: 36, x2: queryX, y2: height - 24, stroke: "var(--signal)", "stroke-width": 2.5, "stroke-linecap": "round", class: "warp-q" }));

  rows.forEach((y, r) => {
    const group = svgEl("g", { class: "hit" });
    group.style.animationDelay = `${1.5 + r * 0.12}s`;
    group.append(svgEl("circle", { cx: queryX, cy: y, r: 9, fill: "var(--bg)", stroke: MODALITIES[r][1], "stroke-width": 2 }));
    group.append(svgEl("circle", { cx: queryX, cy: y, r: 3.2, fill: MODALITIES[r][1] }));
    svg.append(group);
  });

  if (!reduced) {
    const pulse = svgEl("circle", { cx: queryX, cy: 36, r: 3, fill: "var(--signal)" });
    const anim = svgEl("animate", { attributeName: "cy", values: `36;${height - 24}`, dur: "4.5s", begin: "2.4s", repeatCount: "indefinite" });
    const fade = svgEl("animate", { attributeName: "opacity", values: "0;1;1;0", keyTimes: "0;0.1;0.9;1", dur: "4.5s", begin: "2.4s", repeatCount: "indefinite" });
    pulse.setAttribute("opacity", "0");
    pulse.append(anim, fade);
    svg.append(pulse);
  }

  host.append(svg);
}

const loom = document.querySelector("[data-loom]");
if (loom) buildLoom(loom);

const nav = document.querySelector(".site-nav");
const onScroll = () => nav?.classList.toggle("scrolled", window.scrollY > 8);
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

document.querySelectorAll("[data-year]").forEach((node) => { node.textContent = String(new Date().getFullYear()); });
