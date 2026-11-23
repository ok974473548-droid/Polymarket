// Market card as on polymarketanalytics.com: two contracts with Yes/No, or two big buttons
// for a single two-way contract; volume and watchlist star in the footer.
import { compact, esc, pct, starBtn, thumb, yesNo } from "./core.js";

function contractName(e, c) {
  const raw = (c.label || c.question || "").trim();
  return raw || e.title;
}

export function marketCard(e) {
  const open = (e.contracts || []).filter((c) => !c.closed && c.outcomes?.length === 2);
  const single = (e.count || open.length) <= 1 || open.length < 2;
  const first = open[0] || (e.contracts || [])[0];
  const link = (c, i) => `/event/${encodeURIComponent(e.slug)}?contract=${c.condition}&outcome=${i}`;
  let body = "";
  if (first && single) {
    body = `<div class="mc-big">${first.outcomes.slice(0, 2).map((o, i) =>
      `<a class="mc-btn ${i ? "no" : "yes"}" href="${link(first, i)}">${esc(yesNo(o.name))}</a>`).join("")}</div>`;
  } else if (first) {
    body = `<div class="mc-rows">${open.slice(0, 2).map((c) =>
      `<div class="mc-row"><span title="${esc(c.question)}">${esc(contractName(e, c))}</span><b>${pct(c.outcomes[0].price)}</b><a class="yes" href="${link(c, 0)}">Да</a><a class="no" href="${link(c, 1)}">Нет</a></div>`).join("")}</div>`;
  }
  const chance = single && first ? first.outcomes[0].price : null;
  return `<article class="mcard">
    <div class="mc-head">${thumb(e.image, 36)}<h3><a href="/event/${encodeURIComponent(e.slug)}">${esc(e.title)}</a></h3>${chance != null ? `<span class="mc-chance ${chance >= 0.5 ? "up" : "down"}">${pct(chance)}</span>` : ""}</div>
    ${body}
    <div class="mc-foot"><span>${compact(e.volume)} объём</span>${starBtn("event", e.slug, e.title, e.image, true)}</div>
  </article>`;
}
