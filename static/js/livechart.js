// Live parts of an event page, as on polymarket.com: the crypto "Up or Down" round (the coin price against the
// round's target, roundchart.js) and the one connection that brings prices and the round's buys (/api/live,
// server-sent events). Probability lines are drawn by probchart.js; numbers change by rolling digits (roll.js).
import { api, esc } from "./core.js";
import { fitHeight } from "./chart.js";
import { roll } from "./roll.js";
import { coinDecimals, money as coinMoney, roundChart } from "./roundchart.js";

// ------------------------------------------------------------------ one live connection per page
let feed = null;
const timers = new Set();
const charts = new Set();
export function stopLive() {
  feed?.close();
  feed = null;
  for (const t of timers) clearInterval(t);
  timers.clear();
  for (const c of charts) c.remove();
  charts.clear();
}
/** Something with remove() (a chart, a probChart) that goes away with the page. */
export function keep(c) {
  charts.add(c);
}
export function every(ms, fn) {
  const t = setInterval(fn, ms);
  timers.add(t);
  return t;
}
/** Prices of these outcome tokens (and a coin on a crypto round) as they change: onPrice(token, price, bid, ask),
 * onCoin(t, v); with `round` (a round's slug) also its buys: onTrade("up" | "down", dollars). */
export function liveFeed(tokens, coin, onPrice, onCoin, { round = "", onTrade } = {}) {
  feed?.close();
  if (!window.EventSource || (!tokens.length && !coin)) return;
  const qs = new URLSearchParams();
  if (tokens.length) qs.set("tokens", tokens.slice(0, 40).join(","));
  if (coin) qs.set("crypto", coin);
  if (coin && round) qs.set("round", round);
  feed = new EventSource("/api/live?" + qs);
  feed.addEventListener("trade", (e) => {
    const d = JSON.parse(e.data);
    if (d.u) onTrade?.(d.o, d.u);
  });
  feed.addEventListener("price", (e) => {
    const d = JSON.parse(e.data);
    if (d.p != null) onPrice?.(d.t, d.p, d.b, d.a);
  });
  feed.addEventListener("crypto", (e) => {
    const d = JSON.parse(e.data);
    onCoin?.(d.t, d.v);
  });
}

// ------------------------------------------------------------------ crypto round
const price = (v) => coinMoney(v, coinDecimals(v));            // $60 181,97 · $2,3412
const move = (d, v) => coinMoney(Math.abs(d), Math.abs(v) >= 10000 ? 0 : coinDecimals(v));   // ▲ $12 · ▲ $1,28
const hm = (s) => new Date(s * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dm = (s) => new Date(s * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

export async function cryptoRound(host, slug, { onEnd } = {}) {
  host.innerHTML = '<section class="card lv-card"><div class="sk-block tall"></div></section>';
  let r;
  try {
    r = await api("/api/crypto-round?slug=" + encodeURIComponent(slug));
  } catch (e) {
    host.innerHTML = "";
    return null;
  }
  const span = r.end - r.start, long = span >= 3600, hours = span >= 4 * 3600;
  const when = long ? `${dm(r.start)}, ${hm(r.start)}–${hm(r.end)}` : `${hm(r.start)}–${hm(r.end)}`;
  // a daily round starts at the same time every day: name it by the date it ends on ("Up or Down on November 24"),
  // as Polymarket does
  const roundName = (s) => (span >= 86400 ? dm(s + span) : hm(s));
  host.innerHTML = `<section class="card lv-card lv-crypto">
    <div class="lv-head"><div class="lv-prices">
        <div><small>Целевая цена</small><b class="lv-target">${r.target ? price(r.target) : "—"}</b></div><span class="sep"></span>
        <div class="now"><small><span class="lv-now-label">${r.finished ? "Итоговая цена" : "Текущая цена"}</span> <span class="lv-delta"></span></small><b class="lv-now"></b></div></div>
      <div class="lv-timer" aria-label="До конца раунда">${hours ? '<div><b class="t-h">00</b><small>Ч</small></div>' : ""}<div><b class="t-m">00</b><small>МИН</small></div><div><b class="t-s">00</b><small>СЕК</small></div></div>
      <a class="lv-golive" href="/event/${esc(r.finished ? r.live : r.next)}" ${r.finished ? "" : "hidden"}>К текущему раунду ›</a></div>
    <div class="lv-plot"></div>
    <div class="lv-foot">
      <span class="lv-past" title="Итоги прошлых раундов: зелёный — вверх, красный — вниз">Прошлые ${r.past.slice(-5).map((p) => `<i class="${p.outcome === "up" ? "up" : "down"}"></i>`).join("")}</span>
      <a class="lv-round" href="/event/${esc(r.prev)}">${roundName(r.start - span)}</a><span class="lv-round cur ${r.finished ? "done" : ""}">${roundName(r.start)}</span><a class="lv-round" href="/event/${esc(r.next)}">${roundName(r.end)}</a>
      <div class="lv-modes" role="group" aria-label="Вид графика"><button class="cur" data-mode="line">Линия</button><button data-mode="candles">Свечи</button></div></div>
    <p class="lv-note">${esc(r.name)}, ${when}: цена по Chainlink — по ней Polymarket решает исход. ${r.finished ? "Показан весь раунд." : "Видны последние 30 секунд, в режиме «Свечи» — весь известный отрезок раунда."}
      Пунктир «Цель» — цена на старте раунда: если к концу цена выше, исход «Вверх».${r.finished ? "" : " Слева внизу всплывают покупки: зелёные — «Вверх», красные — «Вниз»."}</p></section>`;
  const card = host.firstElementChild, plot = $q(card, ".lv-plot");
  const chart = roundChart(plot, r, { height: fitHeight(300) });
  charts.add(chart);
  let lastT = -Infinity;
  const show = (t, v) => {               // the price in the header and how far it is from the target
    if (t < lastT) return;
    lastT = t;
    roll($q(card, ".lv-now"), price(v));
    if (r.target) {
      const d = v - r.target, el = $q(card, ".lv-delta");
      roll(el, (d >= 0 ? "▲ " : "▼ ") + move(d, v));
      el.className = "lv-delta " + (d >= 0 ? "up" : "down");
    }
  };
  const last = r.points[r.points.length - 1];
  if (last) show(last[0], last[1]);
  card.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => {
    card.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("cur", x === b));
    chart.mode(b.dataset.mode);
  }));
  let ended = false;
  const tick = () => {
    const left = Math.max(0, r.end - Date.now() / 1000);
    const two = (n) => String(Math.floor(n)).padStart(2, "0");
    if (hours) roll($q(card, ".t-h"), two(left / 3600));
    roll($q(card, ".t-m"), hours ? two((left % 3600) / 60) : two(left / 60));
    roll($q(card, ".t-s"), two(left % 60));
    if (!left && !ended) {
      // the round is over: show it whole and offer the round being played now (Polymarket does not jump there by itself)
      ended = true;
      card.classList.add("ended");
      $q(card, ".lv-now-label").textContent = "Итоговая цена";
      $q(card, ".lv-golive").hidden = false;
      $q(card, ".lv-round.cur").classList.add("done");
      setTimeout(() => chart.finish(), 2500);       // the last second of the round arrives a moment later
      onEnd?.(r);
    }
  };
  tick();
  if (!r.finished) every(250, tick);
  else { ended = true; card.classList.add("ended"); }
  // the target price is published a few seconds after the round starts
  if (!r.target) every(5000, async () => {
    if (r.target) return;
    try {
      const fresh = await api("/api/crypto-round?slug=" + encodeURIComponent(slug));
      if (fresh.target) {
        r.target = fresh.target;
        roll($q(card, ".lv-target"), price(r.target));
        chart.setTarget(r.target);
        const l = fresh.points[fresh.points.length - 1];
        if (l) show(l[0], l[1]);
      }
    } catch { /* next time */ }
  });
  return {
    info: r,
    coin(t, v) {
      if (t > r.end + 2) return;
      chart.add(t, v);
      show(t, v);
    },
    trade(side, usd) {
      if (!ended) chart.trade(side, usd);
    },
  };
}

const $q = (root, sel) => root.querySelector(sel);
