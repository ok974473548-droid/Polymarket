// Numbers that roll like an odometer when they change, as on polymarket.com: only the digits that changed move,
// up when the value grows and down when it falls. Text other than digits (signs, $, %, ¢, spaces) stays still.
//
//   roll(el, "33¢")        set or change the text of el
//   rollHtml("33¢")        markup for a first render inside a template string (then roll() animates it)

const DIGITS = "0123456789";

function column(d) {
  return `<span class="rd" aria-hidden="true"><span class="rd-col" style="transform:translateY(${-d * 10}%)">${[...DIGITS].map((x) => `<span>${x}</span>`).join("")}</span></span>`;
}
function glyph(ch) {
  return DIGITS.includes(ch) ? column(Number(ch)) : `<span class="rs" aria-hidden="true">${ch === " " ? "&nbsp;" : ch.replace(/[<>&]/g, "")}</span>`;
}
const value = (text) => Number(String(text).replace(/[^\d,.-]/g, "").replace(/\s/g, "").replace(",", ".")) || 0;
const shape = (text) => String(text).replace(/\d/g, "0");

export function rollHtml(text) {
  const t = String(text);
  return `<span class="roll" data-v="${t.replace(/"/g, "&quot;")}" aria-label="${t.replace(/"/g, "&quot;")}">${[...t].map(glyph).join("")}</span>`;
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
  if (shape(prev) !== shape(t)) {
    // a digit more or less (9 → 10, $999 → $1 000): rebuild, then let the digits roll in from their old places
    host.innerHTML = [...t].map(glyph).join("");
    return;
  }
  const cols = host.querySelectorAll(".rd-col");
  let i = 0;
  for (const ch of t) {
    if (DIGITS.includes(ch)) cols[i++].style.transform = `translateY(${-Number(ch) * 10}%)`;
  }
}
