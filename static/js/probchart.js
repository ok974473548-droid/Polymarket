// Probability lines drawn as polymarket.com draws them (SVG, no chart library): one sample per two pixels of width,
// so a new price or a wider period morphs the line smoothly (300 ms) instead of redrawing it; a right-hand % axis
// with round steps and a floor on its span (small moves are not blown up); time labels that never repeat; a
// pulsing dot at the end of each line with the outcome's name and chance beside it; on hover (or touch) the part
// right of the pointer fades, the names and chances follow the pointer and the moment is shown on top.
//
//   const pc = probChart(host, [{name, color, points: [[t, p 0..1]], visible}], {height, tags, onHover})
//   pc.add(i, p)          a live price of line i (appends a point now, or moves the last one)
//   pc.setVisible(i, on)  show or hide line i (the % axis adapts)
import { esc } from "./core.js";
import { fitHeight } from "./chart.js";
import { keep } from "./livechart.js";
import { roll, rollHtml } from "./roll.js";

const NS = "http://www.w3.org/2000/svg";
const COL = 2;                 // pixels per sample
const AXIS = 44;               // room for the % labels on the right
const TOP = 12, BOTTOM = 26;   // room above the lines and for the time labels below
const MORPH = 300;             // ms a change takes
const MONTHS = ["янв.", "февр.", "март", "апр.", "май", "июнь", "июль", "авг.", "сент.", "окт.", "нояб.", "дек."];
const reduce = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** "57%", near the edges "0.4%" and "99.6%" as Polymarket shows them; "<0.1%" and ">99.9%" beyond. */
export function chance(p) {
  if (p == null || !isFinite(p)) return "—";
  const v = p * 100;
  if (v > 0 && v < 0.05) return "<0.1%";
  if (v < 100 && v > 99.95) return ">99.9%";
  if (v < 1 || v > 99) return (Math.round(v * 10) / 10).toLocaleString("ru-RU") + "%";
  return Math.round(v) + "%";
}

const svg = (tag, attrs = {}) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

// value of a stepwise line at moment t for each of n columns between t0 and t1 (null before its first point)
function sample(points, t0, t1, n) {
  const out = new Array(n).fill(null);
  let k = 0, last = null;
  for (let c = 0; c < n; c++) {
    const t = n === 1 ? t1 : t0 + ((t1 - t0) * c) / (n - 1);
    while (k < points.length && points[k][0] <= t) last = points[k++][1];
    out[c] = last;
  }
  return out;
}

// % axis: some air around the lines, at least 20 points tall, round steps, at most six of them
export function yDomain(lo, hi) {
  if (lo == null || hi == null) return { lo: 0, hi: 1, step: 0.25 };
  const pad = Math.max(0.05, (hi - lo) * 0.1);
  let a = Math.max(0, lo - pad), b = Math.min(1, hi + pad);
  if (b - a < 0.2) {
    const mid = (a + b) / 2;
    a = Math.max(0, mid - 0.1);
    b = Math.min(1, a + 0.2);
    a = Math.max(0, b - 0.2);
  }
  for (const step of [0.01, 0.02, 0.05, 0.1, 0.25, 0.5]) {
    const s = Math.floor(a / step + 1e-9) * step, e = Math.ceil(b / step - 1e-9) * step;
    if ((e - s) / step <= 6) return { lo: s, hi: Math.min(1, e), step };
  }
  return { lo: 0, hi: 1, step: 0.25 };
}

// time labels: a step that leaves ~80 px per label (at least three), at round local moments (hours, midnights, firsts of months)
const H = 3600, D = 86400;
const STEPS = [[300, "m"], [900, "m"], [1800, "m"], [H, "h"], [2 * H, "h"], [3 * H, "h"], [6 * H, "h"], [12 * H, "h"],
  [D, "d"], [2 * D, "d"], [7 * D, "d"], [14 * D, "d"], [1, "M"], [2, "M"], [3, "M"], [6, "M"], [12, "M"]];
const pad2 = (n) => String(n).padStart(2, "0");
function timeTicks(t0, t1, width) {
  const span = Math.max(1, t1 - t0), want = Math.max(3, Math.floor(width / 80));
  const [size, unit] = STEPS.find(([s, u]) => (u === "M" ? s * 30 * D : s) * want >= span) || STEPS[STEPS.length - 1];
  const out = [];
  const d = new Date(t0 * 1000);
  if (unit === "M") {
    d.setDate(1); d.setHours(0, 0, 0, 0);
    d.setMonth(Math.floor(d.getMonth() / size) * size);
    while (d.getTime() / 1000 < t0) d.setMonth(d.getMonth() + size);
    for (; d.getTime() / 1000 <= t1; d.setMonth(d.getMonth() + size))
      out.push([d.getTime() / 1000, d.getMonth() === 0 ? String(d.getFullYear()) : MONTHS[d.getMonth()]]);
    return out;
  }
  if (unit === "d") {
    d.setHours(0, 0, 0, 0);
    while (d.getTime() / 1000 < t0) d.setDate(d.getDate() + 1);
    const days = size / D;
    for (; d.getTime() / 1000 <= t1; d.setDate(d.getDate() + days)) out.push([d.getTime() / 1000, `${d.getDate()} ${MONTHS[d.getMonth()]}`]);
    return out;
  }
  // minutes and hours: local round moments; a midnight is labelled by its date
  d.setSeconds(0, 0);
  const stepMin = size / 60;
  d.setMinutes(Math.ceil((d.getHours() * 60 + d.getMinutes()) / stepMin) * stepMin - d.getHours() * 60);
  for (; d.getTime() / 1000 <= t1; d.setTime(d.getTime() + size * 1000)) {
    const t = d.getTime() / 1000;
    if (t < t0) continue;
    out.push([t, d.getHours() === 0 && d.getMinutes() === 0 ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : `${pad2(d.getHours())}:${pad2(d.getMinutes())}`]);
  }
  return out;
}

function moment(t) {
  const d = new Date(t * 1000);
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== new Date().getFullYear() ? " " + d.getFullYear() : ""}, ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

const ease = (k) => 1 - Math.pow(1 - k, 3);

export function probChart(host, input, { height = 300, tags = true, onHover = null } = {}) {
  const lines = input.map((l) => ({
    name: l.name, color: l.color, on: l.visible !== false,
    // one point per moment, in time order
    points: [...new Map((l.points || []).filter((p) => p && p[1] != null).map((p) => [p[0], p])).values()].sort((a, b) => a[0] - b[0]),
  }));
  host.innerHTML = `<div class="pc" style="height:${fitHeight(height)}px"><div class="pc-tags" aria-hidden="true"></div><div class="pc-when" hidden></div></div>`;
  const box = host.firstElementChild, tagsBox = box.querySelector(".pc-tags"), when = box.querySelector(".pc-when");
  if (!lines.some((l) => l.points.length)) {
    box.innerHTML = '<div class="chart-empty">Нет истории цен за выбранный период</div>';
    return { add() {}, setVisible() {}, remove() {} };
  }
  const root = svg("svg", { class: "pc-svg", role: "img", "aria-label": "График вероятности исходов" });
  const defs = svg("defs");
  const uid = Math.random().toString(36).slice(2, 8);
  const clipL = svg("rect", { x: 0, y: -50, width: 4000, height: 4000 }), clipR = svg("rect", { x: 4000, y: -50, width: 0, height: 4000 });
  const cl = svg("clipPath", { id: "pcl" + uid }), cr = svg("clipPath", { id: "pcr" + uid });
  cl.appendChild(clipL); cr.appendChild(clipR); defs.append(cl, cr);
  const grid = svg("g", { class: "pc-grid" }), xaxis = svg("g", { class: "pc-x" });
  const linesG = svg("g"), dotsG = svg("g"), hoverG = svg("g", { class: "pc-hover", visibility: "hidden" });
  const cursor = svg("line", { class: "pc-cursor" });
  hoverG.appendChild(cursor);
  root.append(defs, grid, xaxis, linesG, dotsG, hoverG);
  box.insertBefore(root, tagsBox);
  for (const l of lines) {
    l.full = svg("path", { stroke: l.color, "clip-path": `url(#pcl${uid})` });
    l.dim = svg("path", { stroke: l.color, class: "pc-dim", "clip-path": `url(#pcr${uid})` });
    l.halo = svg("circle", { r: 4, fill: l.color, class: "pc-halo" });
    l.dot = svg("circle", { r: 4, fill: l.color });
    l.hdot = svg("circle", { r: 4, fill: l.color, stroke: "var(--bg, #0b1015)", "stroke-width": 2 });
    linesG.append(l.dim, l.full);
    dotsG.append(l.halo, l.dot);
    hoverG.appendChild(l.hdot);
    if (tags) {
      l.tag = document.createElement("div");
      l.tag.className = "pc-tag";
      l.tag.style.color = l.color;
      l.tag.innerHTML = `<small>${esc(l.name)}</small><b></b>`;
      tagsBox.appendChild(l.tag);
    }
  }

  // ---- geometry and state
  let W = 0, Hh = 0, right = 0, n = 0, t0 = 0, t1 = 0;
  let shown = null;          // what is on screen: {dom, vals: [[...]]} (values, not pixels: the axis may change too)
  let target = null, from = null, t0anim = 0, raf = 0, hoverX = null, alive = true;

  function measure() {
    W = box.clientWidth; Hh = box.clientHeight;
    const tagW = tags ? Math.min(150, Math.max(84, W * 0.16)) : 14;
    right = Math.max(40, W - AXIS - tagW);
    n = Math.max(2, Math.floor(right / COL) + 1);
  }
  function compute() {
    const vis = lines.filter((l) => l.on && l.points.length);
    const all = vis.length ? vis : lines.filter((l) => l.points.length);
    t0 = Math.min(...all.map((l) => l.points[0][0]));
    t1 = Math.max(...all.map((l) => l.points[l.points.length - 1][0]));
    if (t1 <= t0) t1 = t0 + 60;
    const vals = lines.map((l) => (l.points.length ? sample(l.points, t0, t1, n) : new Array(n).fill(null)));
    let lo = Infinity, hi = -Infinity;
    lines.forEach((l, i) => l.on && vals[i].forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
    return { dom: yDomain(isFinite(lo) ? lo : null, isFinite(hi) ? hi : null), vals, t0, t1 };
  }
  const yOf = (v, dom) => TOP + (1 - (v - dom.lo) / (dom.hi - dom.lo || 1)) * (Hh - TOP - BOTTOM);
  const xOf = (c) => (n === 1 ? right : (c / (n - 1)) * right);

  function paint(state) {
    const { dom, vals } = state;
    // grid and % labels: the round values of the axis being reached, placed on the axis as it is now (while it
    // changes they slide into place instead of showing in-between values)
    grid.textContent = "";
    const ticks = state.ticks || dom;
    for (let v = ticks.lo; v <= ticks.hi + 1e-9; v += ticks.step) {
      const y = Math.round(yOf(v, dom)) + 0.5;
      if (y < TOP - 8 || y > Hh - BOTTOM + 8) continue;
      grid.append(svg("line", { x1: 0, x2: W - AXIS + 6, y1: y, y2: y }),
        Object.assign(svg("text", { x: W - 2, y: y + 4, "text-anchor": "end" }), { textContent: Math.round(v * 100) + "%" }));
    }
    // time labels (they stay where the moments are; the line morphs under them)
    xaxis.textContent = "";
    let lastX = -Infinity;
    for (const [t, label] of timeTicks(state.t0, state.t1, right)) {
      const x = ((t - state.t0) / (state.t1 - state.t0)) * right;
      if (x < 16 || x > right - 16 || x - lastX < 58) continue;
      lastX = x;
      xaxis.appendChild(Object.assign(svg("text", { x, y: Hh - 7, "text-anchor": "middle" }), { textContent: label }));
    }
    // lines
    lines.forEach((l, i) => {
      let d = "", started = false;
      if (l.on) {
        const v = vals[i];
        for (let c = 0; c < n; c++) {
          if (v[c] == null) { started = false; continue; }
          d += (started ? "L" : "M") + xOf(c).toFixed(1) + "," + yOf(v[c], dom).toFixed(1);
          started = true;
        }
      }
      l.full.setAttribute("d", d);
      l.dim.setAttribute("d", d);
      const last = l.on ? vals[i][n - 1] : null;
      for (const el of [l.dot, l.halo]) {
        el.setAttribute("visibility", last == null ? "hidden" : "visible");
        if (last != null) { el.setAttribute("cx", right); el.setAttribute("cy", yOf(last, dom)); }
      }
    });
    if (hoverX == null) placeTags(state, n - 1);
  }

  // names and chances beside the line ends (or beside the pointer), pushed apart so they never overlap
  function placeTags(state, c) {
    if (!tags) return;
    const xAt = xOf(c);
    const items = lines.filter((l) => l.on && state.vals[lines.indexOf(l)][c] != null)
      .map((l) => ({ l, v: state.vals[lines.indexOf(l)][c], y: yOf(state.vals[lines.indexOf(l)][c], state.dom) }))
      .sort((a, b) => a.y - b.y);
    const gap = 40, bottom = Hh - BOTTOM - 4;
    for (let i = 1; i < items.length; i++) if (items[i].y - items[i - 1].y < gap) items[i].y = items[i - 1].y + gap;
    for (let i = items.length - 1; i >= 0; i--) {
      const limit = i === items.length - 1 ? bottom - 16 : items[i + 1].y - gap;
      if (items[i].y > limit) items[i].y = limit;
    }
    const left = hoverX == null ? right + 12 : xAt + 12;
    const shownSet = new Set(items.map((x) => x.l));
    for (const l of lines) if (l.tag) l.tag.hidden = !shownSet.has(l);
    for (const { l, v, y } of items) {
      const b = l.tag.lastElementChild;
      if (!b.firstChild) b.innerHTML = rollHtml(chance(v)); else roll(b, chance(v));
      const w = l.tag.offsetWidth || 70;
      // beside the pointer: to its left when there is no room on the right
      const x = hoverX != null && left + w > W - AXIS ? xAt - 12 - w : left;
      l.tag.style.transform = `translate(${Math.round(x)}px, ${Math.round(Math.max(TOP, y) - 20)}px)`;
    }
  }

  function frame(now) {
    raf = 0;
    if (!alive) return;
    const k = reduce() || !from ? 1 : Math.min(1, (now - t0anim) / MORPH);
    const e = ease(k);
    if (k >= 1 || !from) shown = target;
    else {
      const mix = (a, b) => (a == null || b == null ? b : a + (b - a) * e);
      shown = {
        t0: target.t0, t1: target.t1, ticks: target.dom,
        dom: { lo: mix(from.dom.lo, target.dom.lo), hi: mix(from.dom.hi, target.dom.hi), step: target.dom.step },
        vals: target.vals.map((row, i) => row.map((v, c) => mix(from.vals[i]?.[c], v))),
      };
    }
    paint(shown);
    if (hoverX != null) hover(hoverX);
    if (k < 1 && from) raf = requestAnimationFrame(frame);
  }
  function render(morph) {
    const oldN = n;
    measure();
    const next = compute();
    from = morph && shown && oldN === n ? shown : null;
    target = next;
    t0anim = performance.now();
    root.setAttribute("viewBox", `0 0 ${W} ${Hh}`);
    root.setAttribute("width", W);
    root.setAttribute("height", Hh);
    clipL.setAttribute("width", hoverX == null ? W : hoverX);
    if (!raf) raf = requestAnimationFrame(frame);
  }

  // ---- hover / touch
  function hover(x) {
    const c = Math.max(0, Math.min(n - 1, Math.round(x / COL)));
    const xs = xOf(c);
    hoverX = xs;
    clipL.setAttribute("width", xs);
    clipR.setAttribute("x", xs);
    clipR.setAttribute("width", Math.max(0, W - xs));
    cursor.setAttribute("x1", xs); cursor.setAttribute("x2", xs);
    cursor.setAttribute("y1", TOP - 4); cursor.setAttribute("y2", Hh - BOTTOM);
    hoverG.setAttribute("visibility", "visible");
    box.classList.add("hovering");
    const vals = [];
    lines.forEach((l, i) => {
      const v = l.on ? shown.vals[i][c] : null;
      vals.push(v);
      l.hdot.setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { l.hdot.setAttribute("cx", xs); l.hdot.setAttribute("cy", yOf(v, shown.dom)); }
    });
    const t = shown.t0 + ((shown.t1 - shown.t0) * c) / (n - 1 || 1);
    when.hidden = false;
    when.textContent = moment(t);
    when.style.transform = `translateX(${Math.round(Math.min(Math.max(0, xs - when.offsetWidth / 2), W - AXIS - when.offsetWidth))}px)`;
    placeTags(shown, c);
    onHover?.(vals, t);
  }
  function leave() {
    hoverX = null;
    clipL.setAttribute("width", W);
    clipR.setAttribute("width", 0);
    hoverG.setAttribute("visibility", "hidden");
    box.classList.remove("hovering");
    when.hidden = true;
    if (shown) placeTags(shown, n - 1);
    onHover?.(null, null);
  }
  const pointer = (ev) => {
    if (!shown) return;
    const r = root.getBoundingClientRect();
    const x = ev.clientX - r.left;
    if (x < 0 || x > right + 2) return leave();
    hover(x);
  };
  root.addEventListener("pointermove", pointer);
  root.addEventListener("pointerdown", pointer);
  root.addEventListener("pointerleave", leave);
  root.addEventListener("pointercancel", leave);

  const ro = new ResizeObserver(() => render(false));
  ro.observe(box);
  render(false);

  // live prices: a point "now" when the last one is older than the line's usual step, else the last one moves
  let pending = 0;
  const api = {
    add(i, p) {
      const l = lines[i];
      if (!l || p == null) return;
      const now = Math.floor(Date.now() / 1000);
      const pts = l.points;
      const gaps = pts.slice(-20).map((x, j, a) => (j ? x[0] - a[j - 1][0] : 0)).filter(Boolean).sort((a, b) => a - b);
      const step = Math.max(60, gaps[Math.floor(gaps.length / 2)] || 60);
      if (pts.length && now - pts[pts.length - 1][0] < step) pts[pts.length - 1] = [pts[pts.length - 1][0], p];
      else pts.push([now, p]);
      if (!pending) pending = setTimeout(() => { pending = 0; if (alive) render(true); }, 250);
    },
    setVisible(i, on) {
      if (!lines[i]) return;
      lines[i].on = on;
      render(true);
    },
    remove() {
      alive = false;
      ro.disconnect();
      clearTimeout(pending);
      if (raf) cancelAnimationFrame(raf);
    },
  };
  keep(api);
  return api;
}
