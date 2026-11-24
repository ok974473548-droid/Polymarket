// Numbers that roll like a drum when they change, as on polymarket.com: the ten digits of a place stand on a circle
// and turn the short way round (7 → 0 is three steps forward, 9 → 0 one), so only the places that changed move and
// none of them spins through the whole column. A new place (9 → 10) slides in, a lost one slides out; text other
// than digits (signs, $, %, ¢, spaces) stays still.
//
//   roll(el, "33¢")        set or change the text of el
//   rollHtml("33¢")        markup for a first render inside a template string (then roll() animates it)

const DIGITS = "0123456789";
const TURN = 600;                         // ms a place takes to turn
const reduce = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// where digit d stands on the drum when the drum shows position p: -5..5 steps (0 = in the window)
const offset = (d, p) => {
  let o = (((d - p) % 10) + 10) % 10;
  if (o > 5) o -= 10;
  return o;
};
const place = (p) => [...DIGITS].map((d) => {
  const o = offset(Number(d), p);
  return `<span class="rdi" style="transform:translateY(${o * 100}%)${Math.abs(o) > 1.5 ? ";opacity:0" : ""}">${d}</span>`;
}).join("");

function column(d) {
  return `<span class="rd" data-d="${d}" aria-hidden="true">${place(d)}</span>`;
}
function glyph(ch) {
  return DIGITS.includes(ch) ? column(Number(ch)) : `<span class="rs" aria-hidden="true">${ch === " " ? "&nbsp;" : ch.replace(/[<>&]/g, "")}</span>`;
}
const value = (text) => Number(String(text).replace(/[^\d,.-]/g, "").replace(/\s/g, "").replace(",", ".")) || 0;

export function rollHtml(text) {
  const t = String(text);
  return `<span class="roll" data-v="${t.replace(/"/g, "&quot;")}" aria-label="${t.replace(/"/g, "&quot;")}">${[...t].map(glyph).join("")}</span>`;
}

// ---- one animation loop for every turning place on the page
const turning = new Map();               // .rd element -> {from, to, start}
let raf = 0;
function draw(el, p) {
  const kids = el.children;
  for (let d = 0; d < 10; d++) {
    const o = offset(d, p);
    kids[d].style.transform = `translateY(${o * 100}%)`;
    kids[d].style.opacity = Math.abs(o) > 1.5 ? "0" : "";
  }
}
function tick(now) {
  raf = 0;
  for (const [el, a] of turning) {
    const k = Math.min(1, (now - a.start) / TURN);
    const e = 1 - Math.pow(1 - k, 3);
    const p = a.from + (a.to - a.from) * e;
    el._p = p;
    draw(el, p);
    if (k >= 1 || !el.isConnected) turning.delete(el);
  }
  if (turning.size) raf = requestAnimationFrame(tick);
}
function turn(el, d) {
  const cur = el._p ?? Number(el.dataset.d);
  const shown = Math.round(cur);
  let step = (((d - shown) % 10) + 10) % 10;
  if (step > 5) step -= 10;                // the short way round
  el.dataset.d = d;
  if (!step && Math.abs(cur - shown) < 1e-3) return;
  if (reduce()) {
    el._p = d;
    draw(el, d);
    return;
  }
  turning.set(el, { from: cur, to: shown + step, start: performance.now() });
  if (!raf) raf = requestAnimationFrame(tick);
}

export function roll(el, text) {
  if (!el) return;
  const t = String(text);
  let host = el.classList.contains("roll") ? el : el.querySelector(":scope > .roll");
  if (!host) {
    el.innerHTML = rollHtml(t);
    return;
  }
  const prev = host.dataset.v;
  if (prev === t) return;
  host.dataset.v = t;
  host.setAttribute("aria-label", t);
  host.classList.toggle("roll-up", value(t) >= value(prev));
  host.classList.toggle("roll-down", value(t) < value(prev));
  const shape = (s) => String(s).replace(/\d/g, "0");
  if (shape(prev) === shape(t)) {
    const cols = host.querySelectorAll(".rd");
    let i = 0;
    for (const ch of t) if (DIGITS.includes(ch)) turn(cols[i++], Number(ch));
    return;
  }
  // a place more or less (9 → 10, $999 → $1 000): rebuild, matching places from the right so the units keep
  // turning from where they were; new places slide in
  const old = [...host.querySelectorAll(".rd")].map((x) => x._p ?? Number(x.dataset.d));
  host.innerHTML = [...t].map(glyph).join("");
  const cols = [...host.querySelectorAll(".rd")];
  const target = cols.map((c) => Number(c.dataset.d));
  cols.forEach((c, i) => {
    const j = old.length - (cols.length - i);
    if (j >= 0) {
      c._p = old[j];
      draw(c, old[j]);
      turn(c, target[i]);
    } else c.classList.add("rd-in");
  });
}
