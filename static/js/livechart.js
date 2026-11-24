// Live event charts, as on polymarket.com: a crypto "Up or Down" round (the coin price against the round's target),
// a match (one line per team with its name and chance at the end of the line) and a market with several outcomes.
// Prices arrive over /api/live (server-sent events) and numbers change by rolling digits (roll.js).
import { api, esc } from "./core.js";
import { fitHeight } from "./chart.js";
import { roll, rollHtml } from "./roll.js";

const GRID = "rgba(148,163,184,.08)", TEXT = "#8696b0";
export const TEAM_COLORS = ["#2dd4bf", "#f97316"];

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
export function every(ms, fn) {
  const t = setInterval(fn, ms);
  timers.add(t);
  return t;
}
/** Prices of these outcome tokens (and a coin on a crypto round) as they change: onPrice(token, price, bid, ask), onCoin(t, v). */
export function liveFeed(tokens, coin, onPrice, onCoin) {
  feed?.close();
  if (!window.EventSource || (!tokens.length && !coin)) return;
  const qs = new URLSearchParams();
  if (tokens.length) qs.set("tokens", tokens.slice(0, 40).join(","));
  if (coin) qs.set("crypto", coin);
  feed = new EventSource("/api/live?" + qs);
  feed.addEventListener("price", (e) => {
    const d = JSON.parse(e.data);
    if (d.p != null) onPrice?.(d.t, d.p, d.b, d.a);
  });
  feed.addEventListener("crypto", (e) => {
    const d = JSON.parse(e.data);
    onCoin?.(d.t, d.v);
  });
}

function base(el, opts = {}) {
  const L = window.LightweightCharts;
  const chart = L.createChart(el, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: TEXT, fontSize: 11, fontFamily: "Inter, system-ui, sans-serif", attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: GRID } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.14, bottom: 0.1 } },
    timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 2 },
    crosshair: { horzLine: { visible: false, labelVisible: false }, vertLine: { color: "rgba(148,163,184,.35)", style: 3, labelVisible: false } },
    handleScroll: false, handleScale: false, localization: { locale: "ru-RU" }, ...opts,
  });
  charts.add(chart);
  return chart;
}

// ------------------------------------------------------------------ probability lines (match, several outcomes)
const pct = (p) => Math.round(p * 100) + "%";
/**
 * lines: [{name, color, points: [[t, p 0..1]], visible}]; returns {add(i, p), setVisible(i, on)}.
 * Live points join the last bucket of the history (its typical spacing), so the time axis keeps its scale.
 */
export function probChart(host, lines, { height = 320, intraday = true, gapRight = 0.3, tags = true } = {}) {
  const L = window.LightweightCharts;
  host.innerHTML = `<div class="lv-plot" style="height:${fitHeight(height)}px"><div class="lv-tags"></div></div>`;
  const plot = host.firstElementChild, tagsBox = plot.firstElementChild;
  if (!L || !lines.some((l) => l.points.length)) {
    plot.innerHTML = '<div class="chart-empty">Нет истории цен за выбранный период</div>';
    return { add() {}, setVisible() {} };
  }
  const longest = Math.max(...lines.map((l) => l.points.length));
  const chart = base(plot, {
    localization: { locale: "ru-RU", priceFormatter: (v) => Math.round(v * 100) + "%" },
    timeScale: { borderVisible: false, timeVisible: intraday, secondsVisible: false, rightOffset: 4 },
  });
  const items = lines.map((l) => {
    // strictly increasing times: Polymarket's history may repeat the last moment, which the chart library rejects
    const pts = [...new Map(l.points.filter((p) => p[1] != null).map((p) => [p[0], p])).values()].sort((a, b) => a[0] - b[0]);
    const s = chart.addSeries(L.LineSeries, { color: l.color, lineWidth: 2.5, priceLineVisible: false, lastValueVisible: false,
      // the pulsing "now" dot; an empty line has no last value to pulse (the chart library fails on it every frame)
      lastPriceAnimation: pts.length && l.visible !== false ? L.LastPriceAnimationMode.Continuous : L.LastPriceAnimationMode.Disabled,
      crosshairMarkerRadius: 4, visible: l.visible !== false });
    s.setData(pts.map(([time, value]) => ({ time, value })));
    const gaps = pts.slice(1).map((p, i) => p[0] - pts[i][0]).sort((a, b) => a - b);
    return { s, l, last: pts[pts.length - 1] || null, step: Math.max(1, gaps[Math.floor(gaps.length / 2)] || 60), on: l.visible !== false };
  });
  // room on the right for the names and percentages next to the "now" dots (fitContent would drop it)
  // the whole history across the width, plus room on the right for the names and percentages next to the "now" dots
  const slots = new Set(items.flatMap((it) => it.l.points.map((p) => p[0]))).size;   // the time axis holds every line's moments
  const fit = () => {
    const gap = Math.round(slots * gapRight) + 4, w = plot.clientWidth - chart.priceScale("right").width();
    if (w > 0) chart.timeScale().applyOptions({ minBarSpacing: 0.05, barSpacing: Math.max(0.05, w / (slots + gap)), rightOffset: gap });
    chart.timeScale().scrollToRealTime();
  };
  fit();
  new ResizeObserver(() => requestAnimationFrame(fit)).observe(plot);
  const place = () => {
    if (!tags) return;
    const placed = items.filter((it) => it.on && it.last).map((it) => ({ it, y: it.s.priceToCoordinate(it.last[1]), x: chart.timeScale().timeToCoordinate(it.last[0]) }))
      .filter((p) => p.y != null && p.x != null).sort((a, b) => a.y - b.y);
    for (let i = 1; i < placed.length; i++) if (placed[i].y - placed[i - 1].y < 46) placed[i].y = placed[i - 1].y + 46;
    // pushed below the chart (several outcomes near 0%): move the stack back up, keeping the spacing
    const bottom = plot.clientHeight - 56;
    for (let i = placed.length - 1; i >= 0; i--) {
      const limit = i === placed.length - 1 ? bottom : placed[i + 1].y - 46;
      if (placed[i].y > limit) placed[i].y = limit;
    }
    const seen = new Set();
    for (const { it, y, x } of placed) {
      let tag = tagsBox.querySelector(`[data-n="${CSS.escape(it.l.name)}"]`);
      if (!tag) {
        tag = document.createElement("div");
        tag.className = "lv-tag";
        tag.dataset.n = it.l.name;
        tag.innerHTML = `<small>${esc(it.l.name)}</small><b>${rollHtml(pct(it.last[1]))}</b>`;
        tagsBox.appendChild(tag);
      } else roll(tag.querySelector("b"), pct(it.last[1]));
      tag.style.color = it.l.color;
      tag.style.top = y + "px";
      // right of the "now" dot when there is room before the price axis, otherwise left of it
      const room = plot.clientWidth - chart.priceScale("right").width() - 6;
      tag.style.left = (x + 10 + tag.offsetWidth <= room ? x + 10 : Math.max(0, x - 10 - tag.offsetWidth)) + "px";
      seen.add(tag);
    }
    for (const t of tagsBox.children) if (!seen.has(t)) t.remove();
  };
  chart.timeScale().subscribeVisibleTimeRangeChange(() => requestAnimationFrame(place));
  new ResizeObserver(() => requestAnimationFrame(place)).observe(plot);
  requestAnimationFrame(place);
  return {
    add(i, p) {
      const it = items[i];
      if (!it || p == null) return;
      const now = Math.floor(Date.now() / 1000);
      const t = !it.last ? now : now - it.last[0] < it.step ? it.last[0] : it.last[0] + Math.max(it.step, Math.floor((now - it.last[0]) / it.step) * it.step);
      it.s.update({ time: t, value: p });
      if (!it.last) it.s.applyOptions({ lastPriceAnimation: L.LastPriceAnimationMode.Continuous });
      it.last = [t, p];
      place();
    },
    setVisible(i, on) {
      if (!items[i]) return;
      items[i].on = on;
      items[i].s.applyOptions({ visible: on, lastPriceAnimation: on && items[i].last ? L.LastPriceAnimationMode.Continuous : L.LastPriceAnimationMode.Disabled });
      place();
    },
  };
}

// ------------------------------------------------------------------ crypto round
const STEP = { "5m": 1, "15m": 2, "1h": 5, "4h": 20, "1d": 60 };          // seconds a chart slot covers
const CANDLE = { "5m": 10, "15m": 30, "1h": 60, "4h": 300, "1d": 1800 };  // seconds a candle covers
const nfUsd = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (v) => "$" + nfUsd.format(v);
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
  const L = window.LightweightCharts;
  const span = r.end - r.start, long = span >= 3600, hours = span >= 4 * 3600;
  const when = long ? `${dm(r.start)}, ${hm(r.start)}–${hm(r.end)}` : `${hm(r.start)}–${hm(r.end)}`;
  // a daily round starts at the same time every day: name it by the date it ends on ("Up or Down on November 24"),
  // as Polymarket does
  const roundName = (s) => (span >= 86400 ? dm(s + span) : hm(s));
  host.innerHTML = `<section class="card lv-card lv-crypto">
    <div class="lv-head"><div class="lv-prices">
        <div><small>Целевая цена</small><b class="lv-target">${r.target ? money(r.target) : "—"}</b></div><span class="sep"></span>
        <div class="now"><small><span class="lv-now-label">${r.finished ? "Итоговая цена" : "Текущая цена"}</span> <span class="lv-delta"></span></small><b class="lv-now"></b></div></div>
      <div class="lv-timer" aria-label="До конца раунда">${hours ? '<div><b class="t-h">00</b><small>Ч</small></div>' : ""}<div><b class="t-m">00</b><small>МИН</small></div><div><b class="t-s">00</b><small>СЕК</small></div></div>
      <a class="lv-golive" href="/event/${esc(r.finished ? r.live : r.next)}" ${r.finished ? "" : "hidden"}>К текущему раунду ›</a></div>
    <div class="lv-plot" style="height:${fitHeight(300)}px"></div>
    <div class="lv-foot">
      <span class="lv-past" title="Итоги прошлых раундов: зелёный — вверх, красный — вниз">Прошлые ${r.past.slice(-5).map((p) => `<i class="${p.outcome === "up" ? "up" : "down"}"></i>`).join("")}</span>
      <a class="lv-round" href="/event/${esc(r.prev)}">${roundName(r.start - span)}</a><span class="lv-round cur ${r.finished ? "done" : ""}">${roundName(r.start)}</span><a class="lv-round" href="/event/${esc(r.next)}">${roundName(r.end)}</a>
      <div class="lv-modes" role="group" aria-label="Вид графика"><button class="cur" data-mode="line">Линия</button><button data-mode="candles">Свечи</button></div></div>
    <p class="lv-note">${esc(r.name)}, ${when}: цена по Chainlink — по ней Polymarket решает исход. Пунктир — цена на старте раунда: если к концу цена выше, исход «Вверх».</p></section>`;
  const card = host.firstElementChild, plot = $q(card, ".lv-plot");
  if (!L) return null;
  const step = STEP[r.period] || 1, cstep = CANDLE[r.period] || 10;
  const chart = base(plot, {
    timeScale: { borderVisible: false, timeVisible: true, secondsVisible: r.period === "5m" || r.period === "15m", rightOffset: 0 },
    localization: { locale: "ru-RU", priceFormatter: (v) => "$" + Math.round(v).toLocaleString("ru-RU") },
  });
  const line = chart.addSeries(L.AreaSeries, { lineColor: "#f7931a", lineWidth: 2.5, topColor: "rgba(247,147,26,.18)", bottomColor: "rgba(247,147,26,0)",
    priceLineVisible: false, lastValueVisible: true, lastPriceAnimation: L.LastPriceAnimationMode.Continuous, crosshairMarkerRadius: 5 });
  const candles = chart.addSeries(L.CandlestickSeries, { upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444", visible: false, priceLineVisible: false });
  let targetLine = null;
  const setTarget = (v) => {
    if (!v) return;
    targetLine?.applyOptions({ price: v });
    if (!targetLine) targetLine = line.createPriceLine({ price: v, color: "rgba(226,232,240,.6)", lineWidth: 1, lineStyle: L.LineStyle.Dashed, axisLabelVisible: true, title: "Цель" });
  };
  setTarget(r.target);
  const pts = new Map(r.points.map(([t, v]) => [t, v]));
  let lastPaint = 0, pending = false;
  const paint = () => {
    pending = false;
    lastPaint = Date.now();
    const raw = [...pts].sort((a, b) => a[0] - b[0]);
    if (!raw.length) return;
    const first = Math.max(raw[0][0], r.start - 20) - (Math.max(raw[0][0], r.start - 20) % step), lastT = raw[raw.length - 1][0];
    const data = [];
    let k = 0;
    for (let s = first; s <= r.end; s += step) {           // one slot per step: time to scale, the rest of the round empty
      while (k < raw.length - 1 && raw[k + 1][0] <= s) k++;
      const [t0, v0] = raw[k], [t1, v1] = raw[Math.min(k + 1, raw.length - 1)];
      const v = s <= t0 ? v0 : t1 > t0 ? v0 + (v1 - v0) * Math.min(1, (s - t0) / (t1 - t0)) : v0;
      data.push(s <= lastT ? { time: s, value: v } : { time: s });
    }
    line.setData(data);
    const bars = new Map();
    for (const [t, v] of raw) {
      if (t < r.start - 20) continue;
      const b0 = t - (t % cstep), c = bars.get(b0);
      if (!c) bars.set(b0, { time: b0, open: v, high: v, low: v, close: v });
      else Object.assign(c, { high: Math.max(c.high, v), low: Math.min(c.low, v), close: v });
    }
    candles.setData([...bars.values()]);
    const last = raw[raw.length - 1][1];
    roll($q(card, ".lv-now"), money(last));
    if (r.target) {
      const d = last - r.target, el = $q(card, ".lv-delta");
      roll(el, (d >= 0 ? "▲ $" : "▼ $") + Math.abs(Math.round(d)).toLocaleString("ru-RU"));
      el.className = "lv-delta " + (d >= 0 ? "up" : "down");
    }
  };
  const schedule = () => {              // at most 4 redraws a second
    if (pending) return;
    pending = true;
    setTimeout(paint, Math.max(0, 250 - (Date.now() - lastPaint)));
  };
  paint();
  chart.timeScale().setVisibleRange({ from: Math.max(r.start - 20, r.points[0]?.[0] ?? r.start), to: r.end });
  card.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => {
    card.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("cur", x === b));
    const c = b.dataset.mode === "candles";
    line.applyOptions({ visible: !c });
    candles.applyOptions({ visible: c });
  }));
  let ended = false;
  const tick = () => {
    const left = Math.max(0, r.end - Date.now() / 1000);
    const two = (n) => String(Math.floor(n)).padStart(2, "0");
    if (hours) roll($q(card, ".t-h"), two(left / 3600));
    roll($q(card, ".t-m"), hours ? two((left % 3600) / 60) : two(left / 60));
    roll($q(card, ".t-s"), two(left % 60));
    if (!left && !ended) {
      // the round is over: say so and offer the round being played now (Polymarket does not jump there by itself)
      ended = true;
      card.classList.add("ended");
      $q(card, ".lv-now-label").textContent = "Итоговая цена";
      $q(card, ".lv-golive").hidden = false;
      $q(card, ".lv-round.cur").classList.add("done");
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
        roll($q(card, ".lv-target"), money(r.target));
        setTarget(r.target);
        paint();
      }
    } catch { /* next time */ }
  });
  return {
    info: r,
    coin(t, v) {
      if (t > r.end + 2) return;
      pts.set(t, v);
      schedule();
    },
  };
}

const $q = (root, sel) => root.querySelector(sel);
