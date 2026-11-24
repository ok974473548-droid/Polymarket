import { $, api, avatar, compact, date, delta, errorBox, esc, icon, num, pct, route, thumb, unwrap, usd, withQuery, yesNo } from "../core.js";
import { lineChart, PALETTE } from "../chart.js";
import { marketCard } from "../cards.js";
import { traderRows } from "./traders.js";

let slide = 0;
let chart = null;

export function sparkline(values, w = 120, h = 36) {
  const v = values.filter((x) => x != null && isFinite(x));
  if (v.length < 2) return "";
  const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
  const pts = v.map((x, i) => `${((i / (v.length - 1)) * w).toFixed(1)},${(h - 3 - ((x - min) / span) * (h - 6)).toFixed(1)}`).join(" ");
  const up = v[v.length - 1] >= v[0];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${up ? "var(--green)" : "var(--red)"}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

export function eventCandidates(e) {
  // Multi-market events list one binary contract per candidate; a single contract lists its outcomes.
  const binary = e.contracts.filter((c) => c.outcomes.length === 2);
  const open = binary.filter((c) => !c.closed);
  // all markets of a finished event (a match with a draw), not the Yes/No of its first one
  const list = open.length ? open : e.closed ? binary : open;
  if (list.length > 1) {
    return list.map((c) => ({ name: c.label || c.question, image: c.image, price: c.outcomes[0].price, token: c.outcomes[0].token,
      volume: c.volume, change: c.change24h, condition: c.condition, index: 0, contract: c }))
      .sort((a, b) => (b.price ?? 0) - (a.price ?? 0));
  }
  const c = open[0] || e.contracts[0];
  if (!c) return [];
  return c.outcomes.map((o, i) => ({ name: yesNo(o.name), image: e.image, price: o.price, token: o.token, volume: null,
    change: i === 0 ? c.change24h : c.change24h != null ? -c.change24h : null, condition: c.condition, index: i, contract: c }));
}

export async function homePage(main) {
  const q = route().q;
  const tab = q.get("show") === "traders" ? "traders" : "markets";
  main.innerHTML = `<div class="page-head"><h1>Контур рынка — данные рынков прогнозов Polymarket</h1>
    <p>Аналитика Polymarket в реальном времени: отслеживайте кошельки китов и крупные сделки, следите за трейдерами с лучшим PnL, смотрите историю цен любого рынка, открытый интерес и объём каждого события.</p></div>
    <div class="home-grid"><section class="card featured" id="featured">${'<div class="sk-block"></div>'}</section>
      <aside class="home-side"><div id="kpis" class="kpis"><div class="kpi sk-block"></div><div class="kpi sk-block"></div></div>
      <section class="card hot-panel" id="hot"></section></aside></div>
    <div class="switch-row"><div class="big-switch" role="tablist">
      <a href="${withQuery({ show: null })}" class="${tab === "markets" ? "cur" : ""}" role="tab" aria-selected="${tab === "markets"}" data-keep-scroll>Рынки</a>
      <a href="${withQuery({ show: "traders" })}" class="${tab === "traders" ? "cur" : ""}" role="tab" aria-selected="${tab === "traders"}" data-keep-scroll>Трейдеры</a></div></div>
    <div id="home-list"></div>`;
  let data;
  try {
    data = await api("/api/home");
  } catch (e) {
    $("#featured").innerHTML = errorBox(e);
    return;
  }
  renderKpis(data);
  renderHot(data.hot);
  const featured = unwrap(data.featured) || [];
  renderFeatured(featured);
  if (tab === "markets") marketsList($("#home-list"), "oi");
  else homeTraders($("#home-list"));
}

function renderKpis(data) {
  const v = data.volume || {};
  const chartVals = (v.data?.chart || []).map((p) => p[1]);
  const vol = v.data?.total24h;
  const move = v.data?.previous24h ? (vol / v.data.previous24h - 1) * 100 : null;
  const m = data.markets || {};
  $("#kpis").innerHTML = `
    <div class="kpi card"><div><div class="kpi-val">${icon("trend", 16)} ${vol != null ? usd(vol, 0) : "—"}</div><small>Объём Polymarket за 24 ч ${delta(move)}</small></div>${sparkline(chartVals)}</div>
    <div class="kpi card"><div><div class="kpi-val">${icon("vol", 16)} ${m.count != null ? num(m.count) : m.scanning ? num(m.seen) + "…" : "—"}</div><small>${m.count != null ? "Доступные рынки" : m.scanning ? "Подсчёт рынков идёт" : "Доступные рынки"}</small></div>${sparkline((m.series || []).map((p) => p[1]))}</div>`;
}

function renderHot(rows) {
  $("#hot").innerHTML = `<div class="hot-head"><a href="/traders">${icon("fire", 16)} Популярные трейдеры ${icon("right", 13)}</a><small>PnL</small><small>15д %</small></div>
    ${rows.length ? rows.slice(0, 5).map((t) => `<a class="hot-row" href="/traders/${t.wallet}">${avatar(t.wallet, t.image, t.name, 30)}<span>${esc(t.name || t.wallet.slice(0, 8))}</span><b>${compact(t.pnl)}</b>${t.pct_15d != null ? delta(t.pct_15d, 2) : '<span class="muted">—</span>'}</a>`).join("")
    : '<p class="muted small pad">Рейтинг загружается в фоне. Обновите страницу через минуту.</p>'}
    <a class="btn btn-ghost btn-block" href="/traders">Все трейдеры ${icon("arrow", 14)}</a>`;
}

async function renderFeatured(list) {
  const host = $("#featured");
  if (!list.length) {
    host.innerHTML = '<p class="muted pad">Нет данных о событиях.</p>';
    return;
  }
  slide = ((slide % list.length) + list.length) % list.length;
  const brief = list[slide];
  let e;
  try {
    e = unwrap(await api("/api/event?slug=" + encodeURIComponent(brief.slug)));
  } catch (err) {
    host.innerHTML = errorBox(err);
    return;
  }
  const cands = eventCandidates(e).slice(0, 4);
  const rules = (e.rules || "").split(/\n+/)[0].slice(0, 260);
  host.innerHTML = `<div class="ft-head">${thumb(e.image, 64, "ft-img")}<div class="ft-title">
      <div class="chips">${e.tags.slice(0, 3).map((t) => `<a class="tchip" href="/tags/${esc(t.slug)}">${esc(t.label)}</a>`).join("")}</div>
      <h2><a href="/event/${esc(e.slug)}">${esc(e.title)}</a></h2><small>${icon("vol", 13)} ${usd(e.volume, 0)} объём</small></div>
      <div class="pager-dots"><button class="round-btn" data-slide="-1" aria-label="Предыдущее событие">${icon("left", 14)}</button><span>${slide + 1} из ${list.length}</span><button class="round-btn" data-slide="1" aria-label="Следующее событие">${icon("right", 14)}</button></div></div>
    <div class="ft-body"><div class="ft-outcomes">${cands.map((c, i) => `<a class="ft-row" href="/event/${esc(e.slug)}?contract=${c.condition}&outcome=${c.index}">
        ${c.image && c.image !== e.image ? thumb(c.image, 36) : `<span class="dot-lg" style="background:${PALETTE[i]}"></span>`}
        <span class="ft-name"><b>${esc(c.name)}</b>${c.volume != null ? `<small>${usd(c.volume, 0)} объём</small>` : ""}</span>
        ${c.change != null ? delta(c.change * 100, 0) : ""}<strong>${pct(c.price)}</strong></a>`).join("")}
        ${rules ? `<div class="ft-rules"><b>Разрешение:</b><p>${esc(rules)}${(e.rules || "").length > 260 ? "…" : ""}</p></div>` : ""}</div>
      <div class="ft-chart"><div class="legend">${cands.map((c, i) => `<span><i style="background:${PALETTE[i]}"></i>${esc(c.name)} <b>${pct(c.price)}</b></span>`).join("")}</div><div id="ft-plot"></div></div></div>`;
  host.querySelectorAll("[data-slide]").forEach((b) => (b.onclick = () => {
    slide += Number(b.dataset.slide);
    renderFeatured(list);
  }));
  const series = await Promise.all(cands.map(async (c, i) => {
    try {
      const pts = unwrap(await api(`/api/prices?token=${c.token}&window=1m`));
      return { name: c.name, color: PALETTE[i], points: pts.map(([t, p]) => [t, p * 100]) };
    } catch {
      return { name: c.name, color: PALETTE[i], points: [] };
    }
  }));
  chart?.destroy();
  if ($("#ft-plot")) chart = lineChart($("#ft-plot"), series, { unit: "%", height: 250 });
}

export async function marketsList(host, order, tag = "") {
  let offset = 0;
  host.innerHTML = '<div class="mgrid" id="mgrid"></div><div class="center"><button class="btn" id="more" hidden>Загрузить ещё</button></div>';
  const grid = $("#mgrid", host), more = $("#more", host);
  async function page() {
    more.disabled = true;
    more.textContent = "Загрузка…";
    try {
      const rows = unwrap(await api(`/api/markets?order=${order}&tag=${encodeURIComponent(tag)}&offset=${offset}`));
      grid.insertAdjacentHTML("beforeend", rows.map(marketCard).join(""));
      offset += 20;
      more.hidden = rows.length < 20;
      if (!grid.children.length) grid.outerHTML = '<p class="muted pad">Активных рынков в этой категории нет.</p>';
    } catch (e) {
      grid.insertAdjacentHTML("afterend", errorBox(e, false));
    }
    more.disabled = false;
    more.textContent = "Загрузить ещё";
  }
  more.onclick = page;
  await page();
}

async function homeTraders(host) {
  host.innerHTML = '<div class="tablewrap" id="ht"></div><div class="center"><a class="btn" href="/traders">Весь рейтинг</a></div>';
  try {
    const data = await api("/api/traders?size=10");
    $("#ht", host).innerHTML = traderRows(data, false);
  } catch (e) {
    $("#ht", host).innerHTML = errorBox(e, false);
  }
}
