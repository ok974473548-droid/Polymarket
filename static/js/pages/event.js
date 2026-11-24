// Event card, modelled on polymarketanalytics.com/event/<slug>: header with tags and watchlist, Chart / Largest
// holders / Trades tabs, period selector with a custom range, outcome rows with buy buttons, short rules,
// Live News sidebar and the order ticket (Buy/Sell, Market/Limit, Yes/No, $ amount, To win).
import { $, $$, ago, api, avatar, cents, compact, date, delta, errorBox, esc, go, icon, loadSession, pct, route, short,
  signedUsd, starBtn, state, thumb, toast, unwrap, usd, withQuery, yesNo, shares as fmtShares } from "../core.js";
import { PALETTE } from "../chart.js";
import { cryptoRound, liveFeed, stopLive } from "../livechart.js";
import { probChart } from "../probchart.js";
import { roll, rollHtml } from "../roll.js";
import { eventCandidates } from "./home.js";
import { tradesTable } from "./trader.js";
import { header } from "../layout.js";

const TABS = [["chart", "График"], ["holders", "Крупные держатели"], ["trades", "Сделки"]];
// the periods of polymarket.com's charts; "ВСЕ" is chosen there by default
const WINDOWS = [["1h", "1Ч"], ["6h", "6Ч"], ["1d", "1Д"], ["1w", "1Н"], ["1m", "1М"], ["max", "ВСЕ"]];
let chart = null;
let ticketCtl = null;

// Which chart the page shows is decided by the server (kontur/views.py), as polymarket.com decides it: a crypto
// round, one line, a match, several outcomes, or no chart at all for independent questions.
function viewOf(e) {
  return e.view || { kind: "none", lines: [], main: [] };     // a copy of the event cached before 1.8.0
}

// the main market of a match (who wins) first in the list and in the ticket, then the rest in their order
function mainFirst(cands, v) {
  if (!v.main?.length) return cands;
  const rank = (c) => (v.main.includes(c.condition) ? v.main.indexOf(c.condition) : v.main.length);
  return cands.map((c, i) => [c, i]).sort((x, y) => rank(x[0]) - rank(y[0]) || x[1] - y[1]).map((x) => x[0]);
}

export async function eventPage(main, slug) {
  stopLive();
  chart = null;
  const q = route().q;
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "chart";
  main.innerHTML = `<div class="event-layout"><div class="ev-main" id="ev-main"><div class="sk-block tall"></div></div>
    <aside class="ev-side"><div id="ticket" class="card ticket"></div><section class="card news" id="news"></section></aside></div>`;
  let e;
  try {
    e = unwrap(await api("/api/event?slug=" + encodeURIComponent(slug)));
  } catch (err) {
    $("#ev-main").innerHTML = errorBox(err);
    $(".ev-side").remove();
    return;
  }
  document.title = e.title + " · Контур рынка";
  const v = viewOf(e);
  const cands = mainFirst(eventCandidates(e), v);
  const listOnly = v.kind === "none" || v.kind === "asset";
  const host = $("#ev-main");
  const showAllTags = q.get("alltags") === "1";
  const tags = showAllTags ? e.tags : e.tags.slice(0, 4);
  host.innerHTML = `<header class="ev-head">${thumb(e.image, 72, "ev-img")}<div class="ev-title">
      <div class="chips">${tags.map((t) => `<a class="tchip" href="/tags/${esc(t.slug)}">${esc(t.label)}</a>`).join("")}
        ${e.tags.length > 4 ? `<a class="tchip more" href="${withQuery({ alltags: showAllTags ? null : "1" })}" data-keep-scroll>${showAllTags ? "− Меньше" : "+" + (e.tags.length - 4) + " ещё"}</a>` : ""}</div>
      <h1>${esc(e.title)}</h1>
      <div class="ev-meta"><span>${icon("vol", 14)} ${usd(e.volume, 0)} объём</span>${e.end ? `<span>${icon("calendar", 14)} ${date(e.end)}</span>` : ""}
        ${e.oi != null ? `<span>${icon("bank", 14)} ${compact(e.oi)} открытый интерес</span>` : ""}${e.closed ? '<span class="badge badge-red">Закрыто</span>' : ""}</div></div>
      <div class="ev-actions">${starBtn("event", e.slug, e.title, e.image, true)}
        <a class="btn btn-sm" href="https://polymarket.com/event/${esc(e.slug)}" target="_blank" rel="noopener noreferrer">${icon("ext", 14)} Polymarket</a></div></header>
    <nav class="tabs" aria-label="Разделы события">${TABS.map(([k, l]) =>
      `<a href="${withQuery({ tab: k === "chart" ? null : k, cursor: null })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${k === "chart" && listOnly ? "Исходы" : l}</a>`).join("")}</nav>
    <div id="ev-tab"></div>`;
  const tabHost = $("#ev-tab");
  if (tab === "chart") chartTab(tabHost, e, cands);
  else if (tab === "holders") holdersTab(tabHost, e, cands);
  else tradesTab(tabHost, e);
  ticket(e, cands);
  newsBox(e);
  goLive(e, cands, tab);
}

// ------------------------------------------------------------------ live prices on the page
// One connection brings every price change of the outcomes shown (and the coin price on a crypto round); the numbers
// roll to their new values, the chart lines grow.
function goLive(e, cands, tab) {
  if (e.closed) return;
  const v = viewOf(e);
  const toks = v.lines.map((l) => l.token).filter(Boolean);
  const shown = [...new Set([...cands.slice(0, 12).map((c) => c.contract), ...e.contracts.filter((c) => !c.closed)])];
  for (const c of shown) for (const o of c.outcomes.slice(0, 2)) if (o.token) toks.push(o.token);
  const coin = v.kind === "round" && tab === "chart" ? v.symbol : "";
  liveFeed([...new Set(toks)], coin, (tok, p) => {
    for (const c of e.contracts) for (const o of c.outcomes) if (o.token === tok) o.price = p;
    document.querySelectorAll(`[data-p="${tok}"]`).forEach((el) => roll(el, pct(p)));
    document.querySelectorAll(`[data-c="${tok}"]`).forEach((el) => roll(el, cents(p)));
    chart?.price?.(tok, p);
  }, (t, v) => chart?.coin?.(t, v));
}

// ------------------------------------------------------------------ chart + outcomes
function rulesHtml(e) {
  return e.rules ? `<section class="card rules"><h2>${icon("info", 16)} Правила разрешения</h2><p>${esc(shortRules(e.rules))}</p>
      ${e.resolution ? `<p class="muted small">Источник: ${/^https?:\/\//.test(e.resolution) ? `<a href="${esc(e.resolution)}" target="_blank" rel="noopener noreferrer">${esc(e.resolution)}</a>` : esc(e.resolution)}</p>` : ""}</section>` : "";
}

function chartTab(host, e, cands) {
  const v = viewOf(e);
  if (v.kind === "round") return cryptoTab(host, e, cands);
  if (v.kind === "single" || v.kind === "match") return linesTab(host, e, cands, v);
  if (v.kind === "multi") return multiTab(host, e, cands, v);
  // independent questions ("will he say…", "by which date…"): polymarket.com shows the list only, no chart
  host.innerHTML = `<section class="outcomes" id="outcomes"></section>${rulesHtml(e)}`;
  outcomes($("#outcomes"), e, cands);
}

async function cryptoTab(host, e, cands) {
  host.innerHTML = `<div id="lv-host"></div><section class="outcomes" id="outcomes"></section>${rulesHtml(e)}`;
  outcomes($("#outcomes"), e, cands);
  const round = await cryptoRound($("#lv-host"), e.slug);
  if (round) chart = { coin: (t, v) => round.coin(t, v) };
}

const windowOf = (q) => (WINDOWS.some((w) => w[0] === q.get("w")) ? q.get("w") : "max");
const periods = (win, custom = false) => `<div class="seg" role="group" aria-label="Период">${WINDOWS.map(([k, l]) =>
  `<a href="${withQuery({ w: k === "max" ? null : k, from: null, to: null })}" class="${win === k && !custom ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div>`;
// the event's creation: "ВСЕ" of a young market then comes in fine steps (see polymarket.prices)
const sinceOf = (e) => Math.floor(Date.parse(e.created || e.start || "") / 1000) || 0;
// "Yes" / "Up" in the header of a one-line chart, in Polymarket's words
const OUTCOME_WORDS = { yes: "Да", no: "Нет", up: "Вырастет", down: "Упадёт" };
const outcomeWord = (name) => OUTCOME_WORDS[String(name || "").toLowerCase()] || name;

async function loadLines(e, list, win, range = "", visible = () => true) {
  const since = win === "max" && !range ? sinceOf(e) : 0;
  return Promise.all(list.map(async (l, i) => {
    let pts = [];
    try {
      pts = l.token ? unwrap(await api(`/api/prices?token=${l.token}&window=${win}${range}${since ? "&since=" + since : ""}`)) : [];
    } catch { /* an empty line */ }
    return { name: yesNo(l.name), color: l.color, points: pts, token: l.token, visible: visible(i) };
  }));
}

function changeOf(e, line) {
  const c = e.contracts.find((x) => x.condition === line.condition);
  if (c?.change24h == null) return "";
  const ch = (line.index ? -c.change24h : c.change24h) * 100;
  return delta(ch, Math.abs(ch) < 1 ? 1 : 0);
}

// one line ("3% chance") or a match (the sides in their colours)
async function linesTab(host, e, cands, v) {
  const q = route().q;
  const win = windowOf(q);
  const single = v.kind === "single";
  const first = v.lines[0];
  const head = single
    ? `<div class="lv-single"><small>${esc(outcomeWord(first.name))}</small>
        <div><b><span data-p="${first.token}">${rollHtml(pct(first.price))}</span> вероятность</b>${changeOf(e, first)}</div></div>`
    : `<div class="lv-teams">${v.lines.map((l) => `<span><i style="background:${l.color}"></i>${esc(yesNo(l.name))}</span>`).join("")}</div>
       ${e.live ? '<span class="lv-live">В эфире</span>' : ""}`;
  host.innerHTML = `<section class="card chart-card lv-card ${single ? "lv-one" : ""}">
      <div class="lv-pair-head">${head}${periods(win)}</div>
      <div id="plot"></div></section>
    <section class="outcomes" id="outcomes"></section>${rulesHtml(e)}`;
  outcomes($("#outcomes"), e, cands);
  const lines = await loadLines(e, v.lines, win);
  if (!$("#plot")) return;
  // one line: the header shows the chance at the moment under the pointer, and the current one again after
  const headNum = single ? $(".lv-single [data-p]") : null;
  const current = () => e.contracts.find((c) => c.condition === first.condition)?.outcomes[first.index]?.price;
  const pc = probChart($("#plot"), lines, { height: 300, tags: !single,
    onHover: headNum ? (vals) => roll(headNum, pct(vals ? vals[0] : current())) : null });
  chart = { price: (tok, p) => lines.forEach((l, i) => l.token === tok && pc.add(i, p)) };
}

// several outcomes: the four most likely drawn, the rest a click away in the legend
function multiTab(host, e, cands, v) {
  const q = route().q;
  const win = windowOf(q);
  const start = q.get("from"), end = q.get("to");
  const showAll = q.get("all") === "1";
  const top = v.lines;
  const hidden = new Set((q.get("hide") || "").split(",").filter(Boolean).map(Number));
  host.innerHTML = `<section class="card chart-card">
      <div class="chart-tools">${periods(win, Boolean(start))}
        <details class="range-pick"><summary class="btn btn-sm">${icon("calendar", 14)} ${start ? esc(start) + " — " + esc(end || "сегодня") : "Диапазон"}</summary>
          <form class="range-form"><label>С<input type="date" name="from" value="${esc(start || "")}" required></label>
          <label>По<input type="date" name="to" value="${esc(end || "")}"></label><button class="btn btn-primary btn-sm">Показать</button>
          ${start ? `<a class="btn btn-sm" href="${withQuery({ from: null, to: null })}" data-keep-scroll>Сбросить</a>` : ""}</form></details></div>
      <div class="legend" id="legend">${top.map((l, i) => `<button class="lg ${hidden.has(i) ? "off" : ""} ${i >= 4 && !showAll ? "extra" : ""}" data-i="${i}" aria-pressed="${!hidden.has(i)}"><i style="background:${l.color}"></i>${esc(l.name)} <b data-p="${l.token}">${rollHtml(pct(l.price))}</b></button>`).join("")}
        ${top.length > 4 ? `<a class="lg-more" href="${withQuery({ all: showAll ? null : "1" })}" data-keep-scroll>${showAll ? "− Меньше" : "+" + (top.length - 4) + " ещё"}</a>` : ""}</div>
      <div id="plot" class="plot"></div></section>
    <section class="outcomes" id="outcomes"></section>
    ${rulesHtml(e)}`;
  $(".range-form", host).onsubmit = (ev) => {
    ev.preventDefault();
    const f = ev.target;
    go(withQuery({ from: f.from.value, to: f.to.value || null, w: null }));
  };
  outcomes($("#outcomes"), e, cands);
  drawChart(e, top, win, start, end, hidden, showAll);
}

function shortRules(text) {
  const first = text.split(/\n\s*\n/)[0].trim();
  return first.length > 600 ? first.slice(0, 600).replace(/\s+\S*$/, "") + "…" : first;
}

async function drawChart(e, top, win, start, end, hidden, showAll) {
  const plot = $("#plot");
  const toTs = (d, endOfDay) => (d ? Math.floor(new Date(d + (endOfDay ? "T23:59:59" : "T00:00:00")).getTime() / 1000) : "");
  const range = start ? `&start=${toTs(start)}&end=${toTs(end, true) || Math.floor(Date.now() / 1000)}` : "";
  const series = await loadLines(e, top, win, range, (i) => !hidden.has(i) && (i < 4 || showAll));
  if (!plot.isConnected) return;
  const pc = probChart(plot, series, { height: 320 });
  // a chosen past range does not grow with live prices
  chart = { price: (tok, p) => !start && series.forEach((l, i) => l.token === tok && pc.add(i, p)) };
  for (const b of $$("#legend .lg")) {
    b.onclick = () => {
      const i = Number(b.dataset.i);
      const off = !b.classList.toggle("off");
      b.setAttribute("aria-pressed", !off);
      pc.setVisible(i, !off);
      const now = new Set($$("#legend .lg.off").map((x) => x.dataset.i));
      history.replaceState({}, "", withQuery({ hide: [...now].join(",") || null }));
    };
  }
}

function outcomes(host, e, cands) {
  const q = route().q;
  const all = q.get("rows") === "all";
  const list = all ? cands : cands.slice(0, 8);
  const multi = cands.length && cands[0].contract !== cands[cands.length - 1].contract;
  const closed = e.contracts.filter((c) => c.closed);
  // the row's dot in the colour of its line on the chart; a match's Yes/No markets (a team, the draw) by the line's name
  const v = viewOf(e);
  const lineColor = new Map(v.lines.map((l) => [l.token, l.color]));
  const lineName = new Map(v.kind === "match" ? v.lines.filter((l) => l.name === "Ничья").map((l) => [l.token, l.name]) : []);
  host.innerHTML = `<div class="out-head"><span>Исход</span><span>Вероятность</span><span></span></div>
    ${list.map((c, i) => {
      const yes = c.contract.outcomes[0], no = c.contract.outcomes[1];
      const live = !c.contract.closed && c.contract.accepting;
      return `<div class="out-row">
        <div class="out-name">${multi && c.image ? thumb(c.image, 40) : `<span class="dot-lg" style="background:${lineColor.get(c.token) || PALETTE[i % PALETTE.length]}"></span>`}
          <div><b>${esc(lineName.get(c.token) || c.name)}</b><small>${c.volume != null ? usd(c.volume, 0) + " объём" : ""}</small></div></div>
        <div class="out-pct"><strong data-p="${c.token}">${rollHtml(pct(c.price))}</strong>${c.change != null ? delta(c.change * 100, 0) : ""}</div>
        <div class="out-btns">${multi
          ? `<button class="buy yes" data-pick="${c.contract.condition}" data-o="0" ${live ? "" : "disabled"}><span class="bn">Купить ${esc(yesNo(yes.name))}</span> <span data-c="${yes.token}">${rollHtml(cents(yes.price))}</span></button>
             <button class="buy no" data-pick="${c.contract.condition}" data-o="1" ${live ? "" : "disabled"}><span class="bn">Купить ${esc(yesNo(no.name))}</span> <span data-c="${no.token}">${rollHtml(cents(no.price))}</span></button>`
          : `<button class="buy ${c.index ? "no" : "yes"}" data-pick="${c.contract.condition}" data-o="${c.index}" ${live ? "" : "disabled"}>Купить <span data-c="${c.token}">${rollHtml(cents(c.price))}</span></button>`}</div></div>`;
    }).join("")}
    ${cands.length > 8 ? `<a class="show-all" href="${withQuery({ rows: all ? null : "all" })}" data-keep-scroll>${all ? "Свернуть" : `Показать все позиции (${cands.length})`}</a>` : ""}
    ${closed.length && multi ? `<details class="closed-list"><summary>Завершённые рынки (${closed.length})</summary>${closed.map((c) =>
      `<div class="out-row closed"><div class="out-name"><div><b>${esc(c.label || c.question)}</b></div></div><div class="out-pct">${c.outcomes.map((o) => `${esc(yesNo(o.name))} ${pct(o.price)}`).join(" · ")}</div><div></div></div>`).join("")}</details>` : ""}`;
  host.onclick = (ev) => {
    const b = ev.target.closest("[data-pick]");
    if (!b) return;
    ticketCtl?.select(b.dataset.pick, Number(b.dataset.o), "buy");
    if (innerWidth < 1000) $("#ticket").scrollIntoView({ behavior: "smooth", block: "start" });
  };
}

// ------------------------------------------------------------------ largest holders
async function holdersTab(host, e, cands) {
  const q = route().q;
  const contracts = e.contracts.filter((c) => c.outcomes.length === 2);
  const multi = contracts.length > 1;
  const sel = contracts.find((c) => c.condition === q.get("contract")) || (cands[0]?.contract || contracts[0]);
  if (!sel) {
    host.innerHTML = '<p class="muted pad">Нет рынков.</p>';
    return;
  }
  const side = q.get("side") === "1" ? 1 : 0;
  host.innerHTML = `<section class="card"><div class="filters-row">
      ${multi ? `<label class="field-inline">Исход <select id="h-contract" class="input-sm">${contracts.map((c) =>
        `<option value="${c.condition}" ${c === sel ? "selected" : ""}>${esc(c.label || c.question)}</option>`).join("")}</select></label>` : ""}
      <div class="seg">${sel.outcomes.map((o, i) => `<a href="${withQuery({ side: i ? "1" : null })}" class="${side === i ? "cur" : ""}" data-keep-scroll>${esc(yesNo(o.name))} · ${cents(o.price)}</a>`).join("")}</div></div>
    <div class="tablewrap" id="h-table"><div class="sk-block tall"></div></div></section>`;
  if (multi) $("#h-contract").onchange = (ev) => go(withQuery({ contract: ev.target.value, side: null }));
  try {
    const groups = unwrap(await api("/api/holders?condition=" + sel.condition));
    const tok = sel.outcomes[side].token;
    const group = groups.find((g) => g.token === tok) || groups.find((g) => g.rows.some((r) => r.outcome_index === side)) || { rows: [] };
    const rows = group.rows;
    if (!rows.length) {
      $("#h-table").innerHTML = '<p class="muted pad">Держателей нет.</p>';
      return;
    }
    $("#h-table").innerHTML = `<table class="table"><thead><tr><th>#</th><th>Трейдер</th><th class="num">Акции</th><th class="num">Ср. цена</th><th class="num">Стоимость</th><th class="num">PnL</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr><td class="muted">${i + 1}</td><td><a class="trader-cell" href="/traders/${r.wallet}">${avatar(r.wallet, r.image, r.name, 26)}<span>${esc(r.name || short(r.wallet))}</span></a></td>
        <td class="num">${fmtShares(r.shares)}</td><td class="num">${cents(r.avg)}</td><td class="num">${usd(r.value, 0)}</td>
        <td class="num ${(r.pnl || 0) >= 0 ? "up" : "down"}">${r.pnl != null ? signedUsd(r.pnl, 0) : "—"}${r.pnl_pct != null ? ` <small>(${r.pnl_pct.toFixed(1)}%)</small>` : ""}</td></tr>`).join("")}</tbody></table>`;
  } catch (err) {
    $("#h-table").innerHTML = errorBox(err, false);
  }
}

// ------------------------------------------------------------------ trades
async function tradesTab(host, e) {
  const q = route().q;
  const buy = q.get("buy") !== "0", sell = q.get("sell") !== "0";
  const tagsOn = q.get("tags") !== "0";
  const side = buy && !sell ? "BUY" : sell && !buy ? "SELL" : "";
  host.innerHTML = `<section class="card"><div class="filters-row">
      <label class="check"><input type="checkbox" id="f-buy" ${buy ? "checked" : ""}> Покупки</label>
      <label class="check"><input type="checkbox" id="f-sell" ${sell ? "checked" : ""}> Продажи</label>
      <label class="toggle"><input type="checkbox" id="f-tags" ${tagsOn ? "checked" : ""}><span></span> Показать метки</label>
      <span class="muted small">Сделки тейкеров по всем рынкам события, новые сверху. Метка «Крупная сделка» — от $10 000.</span></div>
    <div class="tablewrap" id="t-table"><div class="sk-block tall"></div></div>
    <div class="center"><button class="btn" id="t-more" hidden>Загрузить ещё</button></div></section>`;
  $("#f-buy").onchange = (ev) => go(withQuery({ buy: ev.target.checked ? null : "0", sell: !ev.target.checked && !sell ? null : q.get("sell") }));
  $("#f-sell").onchange = (ev) => go(withQuery({ sell: ev.target.checked ? null : "0", buy: !ev.target.checked && !buy ? null : q.get("buy") }));
  $("#f-tags").onchange = (ev) => go(withQuery({ tags: ev.target.checked ? null : "0" }));
  if (!buy && !sell) {
    $("#t-table").innerHTML = '<p class="muted pad">Выберите хотя бы одну сторону сделки.</p>';
    return;
  }
  let rows = [], cursor = "";
  async function load() {
    try {
      const res = unwrap(await api(`/api/event-trades?event_id=${e.id}&side=${side}&cursor=${encodeURIComponent(cursor)}`));
      rows = rows.concat(res.rows);
      cursor = res.next || "";
      $("#t-more").hidden = !cursor;
      $("#t-table").innerHTML = rows.length ? tradesTable(rows, { withTrader: true, showTags: tagsOn }) : '<p class="muted pad">Сделок нет.</p>';
    } catch (err) {
      $("#t-table").innerHTML = errorBox(err, false);
    }
  }
  $("#t-more").onclick = load;
  load();
}

// ------------------------------------------------------------------ live news
async function newsBox(e) {
  const box = $("#news");
  box.innerHTML = `<h2>${icon("news", 16)} Новости <span class="live-dot" aria-hidden="true"></span></h2><div class="news-list">${'<div class="sk-line"></div>'.repeat(4)}</div>`;
  try {
    const d = unwrap(await api("/api/news?title=" + encodeURIComponent(e.title)));
    const items = d.items || [];
    $(".news-list", box).innerHTML = items.length ? items.slice(0, 12).map((n) => `<a class="news-item" href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">
      <b>${esc(n.title)}</b><small>${esc(n.source)}${n.at ? " · " + ago(n.at) : ""} · ${n.provider === "bing" ? "Bing" : "Google"}</small></a>`).join("")
      : '<p class="muted small">Свежих новостей по событию не найдено.</p>';
  } catch (err) {
    $(".news-list", box).innerHTML = `<p class="muted small">Новости недоступны: ${esc(err.message)}</p>`;
  }
}

// ------------------------------------------------------------------ order ticket
const EXPIRY = [["", "Без срока"], ["3600", "1 час"], ["21600", "6 часов"], ["86400", "24 часа"], ["604800", "7 дней"], ["eod", "До конца дня"]];

function ticket(e, cands) {
  const box = $("#ticket");
  const q = route().q;
  const contracts = e.contracts.filter((c) => c.outcomes.length === 2 || c.outcomes.length > 0);
  const first = contracts.find((c) => c.condition === q.get("contract")) || cands[0]?.contract || contracts[0];
  const st = { c: first, o: Number(q.get("outcome")) === 1 ? 1 : 0, side: "buy", type: "market", amount: "", shares: "", price: "", expiry: "", info: null };
  if (!first) {
    box.innerHTML = '<p class="muted">Нет доступных рынков.</p>';
    return;
  }
  st.price = (first.outcomes[st.o]?.price ?? 0.5).toFixed(2);
  let quoteTimer = 0, quoteSeq = 0;
  const multi = contracts.filter((c) => !c.closed).length > 1;

  function draw() {
    const c = st.c;
    const live = !c.closed && c.accepting;
    const out = c.outcomes;
    const label = multi ? c.label || c.question : e.title;
    box.innerHTML = `<div class="tk-head">${thumb(multi ? c.image || e.image : e.image, 36)}<div class="tk-title">
        ${multi ? `<select id="tk-contract" class="tk-select" aria-label="Исход">${contracts.filter((x) => !x.closed).map((x) => `<option value="${x.condition}" ${x === c ? "selected" : ""}>${esc(x.label || x.question)}</option>`).join("")}</select>` : `<b>${esc(label)}</b>`}</div></div>
      <div class="tk-row"><div class="tk-sides" role="tablist">${["buy", "sell"].map((s) => `<button role="tab" aria-selected="${st.side === s}" class="${st.side === s ? "cur" : ""}" data-side="${s}">${s === "buy" ? "Купить" : "Продать"}</button>`).join("")}</div>
        <select id="tk-type" class="tk-type" aria-label="Тип заявки"><option value="market">Рыночная</option><option value="limit" ${st.type === "limit" ? "selected" : ""}>Лимитная</option></select></div>
      <div class="tk-outs">${out.slice(0, 2).map((o, i) => `<button class="tk-out ${i ? "no" : "yes"} ${st.o === i ? "cur" : ""}" data-o="${i}">${esc(yesNo(o.name))} <b data-c="${o.token}">${rollHtml(cents(o.price))}</b></button>`).join("")}</div>
      ${!live ? '<p class="tk-closed">Рынок закрыт для торговли.</p>' : st.type === "limit" ? limitFields() : st.side === "buy" ? buyFields() : sellFields()}
      <div class="tk-summary" id="tk-sum"></div>
      ${state.user ? `<button class="btn btn-primary btn-block tk-go" id="tk-go" ${live ? "" : "disabled"}>${st.side === "buy" ? "Купить" : "Продать"} ${esc(yesNo(out[st.o]?.name || ""))}</button>`
        : `<button class="btn btn-primary btn-block" data-login>Войдите, чтобы торговать</button>`}
      <p class="tk-note">${icon("info", 13)} Виртуальная торговля по живому стакану Polymarket. ${c.fee_rate ? `Комиссия тейкера по формуле Polymarket (ставка ${(c.fee_rate * 100).toFixed(2)}%).` : "На этом рынке нет комиссии."}</p>`;
    bind();
    requestQuote();
  }

  const buyFields = () => `<label class="tk-label">Сумма</label>
    <div class="tk-amount"><span>$</span><input id="tk-amount" type="number" inputmode="decimal" min="1" step="0.01" placeholder="0" value="${esc(st.amount)}"></div>
    <div class="chips tk-quick">${[1, 5, 10, 100].map((v) => `<button class="chip" data-add="${v}">+$${v}</button>`).join("")}<button class="chip" data-max>Макс.</button></div>`;
  const sellFields = () => `<label class="tk-label">Акции</label>
    <div class="tk-amount"><input id="tk-shares" type="number" inputmode="decimal" min="0" step="0.01" placeholder="0" value="${esc(st.shares)}"></div>
    <div class="chips tk-quick">${[25, 50, 75].map((v) => `<button class="chip" data-part="${v}">${v}%</button>`).join("")}<button class="chip" data-part="100">Макс.</button></div>`;
  const limitFields = () => `<label class="tk-label">Лимитная цена</label>
    <div class="tk-step"><button data-step="-1" aria-label="Уменьшить цену">−</button><span>$</span><input id="tk-price" type="number" min="0.001" max="0.999" step="0.01" value="${esc(st.price)}" aria-label="Цена в долларах"><button data-step="1" aria-label="Увеличить цену">+</button></div>
    <label class="tk-label">Акции</label>
    <div class="tk-amount"><input id="tk-shares" type="number" inputmode="decimal" min="0" step="0.01" placeholder="0" value="${esc(st.shares)}"></div>
    ${st.side === "sell" ? `<div class="chips tk-quick">${[25, 50, 75].map((v) => `<button class="chip" data-part="${v}">${v}%</button>`).join("")}<button class="chip" data-part="100">Макс.</button></div>` : ""}
    <label class="tk-label">Срок действия</label><select id="tk-exp" class="input-sm tk-exp">${EXPIRY.map(([v, l]) => `<option value="${v}" ${st.expiry === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;

  function bind() {
    $("#tk-contract", box)?.addEventListener("change", (ev) => select(ev.target.value, st.o));
    $$("[data-side]", box).forEach((b) => (b.onclick = () => {
      st.side = b.dataset.side;
      st.shares = "";
      draw();
    }));
    $("#tk-type", box).onchange = (ev) => {
      st.type = ev.target.value;
      draw();
    };
    $$(".tk-out", box).forEach((b) => (b.onclick = () => {
      st.o = Number(b.dataset.o);
      st.price = (st.c.outcomes[st.o]?.price ?? 0.5).toFixed(2);
      draw();
    }));
    const amount = $("#tk-amount", box), sh = $("#tk-shares", box), price = $("#tk-price", box), exp = $("#tk-exp", box);
    if (amount) amount.oninput = () => { st.amount = amount.value; requestQuote(); };
    if (sh) sh.oninput = () => { st.shares = sh.value; requestQuote(); };
    if (price) price.oninput = () => { st.price = price.value; requestQuote(); };
    if (exp) exp.onchange = () => { st.expiry = exp.value; };
    $$("[data-add]", box).forEach((b) => (b.onclick = () => {
      st.amount = String(Math.round(((Number(st.amount) || 0) + Number(b.dataset.add)) * 100) / 100);
      amount.value = st.amount;
      requestQuote();
    }));
    $("[data-max]", box)?.addEventListener("click", () => {
      st.amount = String(Math.floor((state.account?.cash || 0) * 100) / 100);
      amount.value = st.amount;
      requestQuote();
    });
    $$("[data-part]", box).forEach((b) => (b.onclick = () => {
      const have = st.info?.available_shares || 0;
      st.shares = String(Math.floor((have * Number(b.dataset.part)) / 100 * 100) / 100);
      sh.value = st.shares;
      requestQuote();
    }));
    $$("[data-step]", box).forEach((b) => (b.onclick = () => {
      const tick = st.c.tick || 0.01;
      const v = Math.min(1 - tick, Math.max(tick, (Number(st.price) || 0) + Number(b.dataset.step) * tick));
      st.price = v.toFixed(tick < 0.01 ? 3 : 2);
      price.value = st.price;
      requestQuote();
    }));
    $("#tk-go", box)?.addEventListener("click", submit);
  }

  function body() {
    const tok = st.c.outcomes[st.o]?.token;
    const b = { condition: st.c.condition, token: tok, side: st.side, type: st.type };
    if (st.type === "market" && st.side === "buy") b.amount = Number(st.amount);
    else b.shares = Number(st.shares);
    if (st.type === "limit") {
      b.price = Number(st.price);
      if (st.expiry === "eod") {
        const d = new Date();
        d.setHours(23, 59, 59, 0);
        b.expires = Math.floor(d.getTime() / 1000);
      } else if (st.expiry) b.expires = Math.floor(Date.now() / 1000) + Number(st.expiry);
    }
    return b;
  }

  function requestQuote() {
    clearTimeout(quoteTimer);
    const sum = $("#tk-sum", box);
    if (!sum) return;
    const b = body();
    const avg = (st.c.outcomes[st.o]?.price ?? null);
    if (!state.user) {
      sum.innerHTML = summaryHtml(null, b, avg);
      return;
    }
    quoteTimer = setTimeout(async () => {
      const seq = ++quoteSeq;
      try {
        const qb = { ...b };
        if (!(qb.amount > 0) && st.type === "market" && st.side === "buy") qb.amount = 1;
        if (!(qb.shares > 0) && !(st.type === "market" && st.side === "buy")) qb.shares = 1;
        if (st.type === "limit" && !(qb.price > 0 && qb.price < 1)) return;
        const res = await api("/api/quote", qb);
        if (seq !== quoteSeq) return;
        st.info = res;
        sum.innerHTML = summaryHtml(res, b, avg);
      } catch (err) {
        if (seq === quoteSeq) sum.innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
      }
    }, 300);
  }

  function summaryHtml(res, b, fallback) {
    const empty = st.type === "market" && st.side === "buy" ? !(b.amount > 0) : !(b.shares > 0);
    const rows = [];
    if (res && state.user) {
      rows.push(st.side === "buy" ? ["Доступно", usd(res.balance)] : ["Ваши акции", fmtShares(res.available_shares)]);
    }
    if (!empty && res) {
      if (st.side === "buy") {
        rows.push(["Ср. цена", res.avg != null ? cents(res.avg) : st.type === "limit" ? cents(b.price) : "—"]);
        if (res.fees) rows.push(["Комиссия", usd(res.fees)]);
        if (st.type === "limit") rows.push(["Итого", usd(res.cost)]);
        if (res.resting) rows.push(["В стакан", fmtShares(res.resting) + " акц."]);
        rows.push(["К выигрышу", `<b class="up">${usd(res.to_win)}</b>`]);
      } else {
        rows.push(["Ср. цена", res.avg != null ? cents(res.avg) : st.type === "limit" ? cents(b.price) : "—"]);
        if (res.fees) rows.push(["Комиссия", usd(res.fees)]);
        if (res.resting) rows.push(["В стакан", fmtShares(res.resting) + " акц."]);
        rows.push(["Вы получите", `<b class="up">${usd(res.receive)}</b>`]);
      }
    } else if (!empty && !state.user) {
      rows.push(["Цена", cents(fallback)]);
      if (st.side === "buy" && st.type === "market" && fallback) rows.push(["К выигрышу ≈", usd(b.amount / fallback)]);
    }
    if (res && !empty && st.type === "market" && st.side === "buy" && res.cost != null && b.amount - res.cost > 0.01)
      rows.push(["", `<small class="muted">Ликвидности хватает на ${usd(res.cost)}</small>`]);
    return rows.map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`).join("");
  }

  async function submit() {
    const b = body();
    const btn = $("#tk-go", box);
    btn.disabled = true;
    btn.textContent = "Отправка…";
    try {
      const o = await api("/api/order", b);
      const filled = o.filled ? `${fmtShares(o.filled)} акц. по ${cents(o.avg)}` : "";
      toast(o.status === "filled" ? `Исполнено: ${filled}` : o.status === "open" ? `Заявка размещена${o.filled ? ", частично исполнена: " + filled : ""}` : "Заявка: " + o.status, "good");
      st.amount = "";
      st.shares = "";
      await loadSession();
      header();
      draw();
    } catch (err) {
      toast(err.message, "bad");
      btn.disabled = false;
      btn.textContent = st.side === "buy" ? "Купить" : "Продать";
    }
  }

  function select(cond, o, side) {
    const c = e.contracts.find((x) => x.condition === cond);
    if (!c) return;
    st.c = c;
    st.o = o;
    if (side) st.side = side;
    st.price = (c.outcomes[o]?.price ?? 0.5).toFixed(2);
    st.info = null;
    history.replaceState({}, "", withQuery({ contract: cond, outcome: o || null }));
    draw();
  }
  ticketCtl = { select };
  draw();
}

