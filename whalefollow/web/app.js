/* WhaleFollow — интерфейс по макету. Без сборки: чистый JS, данные из /api. */
"use strict";

// ---------------------------------------------------------------- утилиты
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = () => $("#main");
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#");   // ссылки из внешних источников

function money(v, { sign = false, cents = false } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const k = cents ? 100 : 1;
  v = Math.round(v * k) / k || 0;           // без «−$0»
  const abs = Math.abs(v);
  const s = abs.toLocaleString("ru-RU", { minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
  if (!sign) return (v < 0 ? "−$" : "$") + s;
  if (v > 0) return "↑ +$" + s;
  if (v < 0) return "↓ −$" + s;
  return "→ $" + s;
}
function pct(v, { sign = true } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  v = Math.round(v * 10) / 10 || 0;
  const s = Math.abs(v).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + "%";
  if (!sign) return s;
  return v > 0 ? "↑ +" + s : v < 0 ? "↓ −" + s : "→ " + s;
}
const tone = (v) => (Math.round(v || 0) > 0 ? "up" : Math.round(v || 0) < 0 ? "down" : "");
const signedPct = (v) => { v = Math.round((v || 0) * 10) / 10 || 0; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toLocaleString("ru-RU") + "%"; };
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
};
const days = (n) => `${n} ${plural(n, "день", "дня", "дней")}`;

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch("/api" + path, {
    method, credentials: "same-origin",
    // изменяющие запросы всегда JSON: сервер отклоняет другие (защита от подделки запросов с чужих сайтов)
    headers: method !== "GET" ? { "Content-Type": "application/json" } : {},
    body: method !== "GET" ? JSON.stringify(body || {}) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* пусто */ }
  if (res.status === 401 && data && data.auth && !path.startsWith("/login")) {
    showLogin();
    throw new Error("Нужен вход");
  }
  if (!res.ok) throw new Error((data && data.error) || `Ошибка ${res.status}`);
  return data;
}

function showLogin(err) {
  if (document.body.classList.contains("locked") && !err) return;   // форма уже на экране — не перерисовывать
  const main = $("#main") || document.body;
  document.body.classList.add("locked");
  main.innerHTML = `<div class="card col" style="max-width:380px;margin:12vh auto;gap:14px">
    <h1 style="margin:0">WhaleFollow</h1>
    <div class="sub">Введите пароль интерфейса (переменная WF_UI_PASSWORD).</div>
    <form id="login-form" class="col" style="gap:10px">
      <label for="login-pass">Пароль</label>
      <input id="login-pass" type="password" autocomplete="current-password" required autofocus>
      ${err ? `<div class="err" role="alert">${esc(err)}</div>` : ""}
      <button class="btn p" type="submit">Войти</button>
    </form></div>`;
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/login", { method: "POST", body: { password: $("#login-pass").value } });
      location.reload();
    } catch (ex) { showLogin(ex.message); }
  });
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 4000);
}

function confirmDialog({ title, text, ok = "Подтвердить", danger = false }) {
  const d = $("#dialog");
  d.innerHTML = `<form method="dialog" style="display:flex;flex-direction:column;gap:12px">
    <b style="font-size:20px">${esc(title)}</b><div>${esc(text)}</div>
    <div class="row"><button class="btn ${danger ? "danger solid" : "p"}" value="ok">${esc(ok)}</button>
    <button class="btn" value="cancel">Отмена</button></div></form>`;
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

// печать кита: латунное кольцо с кодом; оттенок глубины зависит от кода, чтобы киты различались с первого взгляда
const SEAL_TONES = [["#21414D", "#10262E"], ["#233B52", "#101F2E"], ["#1F4541", "#0E2622"], ["#3A3348", "#1A1724"], ["#3D3A2A", "#1E1C12"]];
function seal(code, { lg = false } = {}) {
  const c = String(code || "?").slice(0, 4);
  let h = 0;
  for (const ch of c) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [hi, lo] = SEAL_TONES[h % SEAL_TONES.length];
  return `<span class="seal${lg ? " lg" : ""}" style="--seal-hi:${hi};--seal-lo:${lo}" aria-hidden="true">${esc(c)}</span>`;
}

function spark(values, { w = 110, h = 30 } = {}) {
  if (!values || values.length < 2 || values.every((v) => v === values[0])) return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><line x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}" stroke="rgba(236,230,218,.18)" stroke-dasharray="2 4"/></svg>`;
  const lo = Math.min(...values, 0), hi = Math.max(...values, 0);
  const x = (i) => (i * w) / (values.length - 1), y = (v) => 2 + (1 - (v - lo) / (hi - lo || 1)) * (h - 4);
  const last = values[values.length - 1];
  const color = last > 0 ? "#86C9A8" : last < 0 ? "#E58C7A" : "#93A7AA";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <line x1="0" y1="${y(0).toFixed(1)}" x2="${w}" y2="${y(0).toFixed(1)}" stroke="rgba(236,230,218,.14)"/>
    <polyline points="${values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function lineChart(points, { height = 160, label = "", fmtY = (v) => v, color = "#C8A464", marks = [] } = {}) {
  if (!points || points.length < 2) return `<div class="sub">Недостаточно данных для графика</div>`;
  const W = 820, H = height, L = 66, R = 18, T = 14, B = 28;
  const ys = points.map((p) => p.y).concat(marks.map((m) => m.y).filter((v) => v !== null && v !== undefined));
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const xs = points.map((p, i) => (p.x !== undefined ? p.x : i));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const x = (v) => L + ((v - x0) * (W - L - R)) / (x1 - x0 || 1);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = points.map((p, i) => `${x(xs[i]).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  const area = `${x(xs[0]).toFixed(1)},${H - B} ${path} ${x(xs[xs.length - 1]).toFixed(1)},${H - B}`;
  const last = points[points.length - 1];
  const gid = "g" + Math.random().toString(36).slice(2, 8);
  const mk = marks.filter((m) => m.x !== null && m.x !== undefined && m.x >= x0 && m.x <= x1).map((m) => `
    <g><line x1="${x(m.x)}" y1="${T}" x2="${x(m.x)}" y2="${H - B}" stroke="${m.color}" stroke-dasharray="3 4" opacity=".55"/>
    <circle cx="${x(m.x)}" cy="${y(m.y)}" r="5.5" fill="#0E1F26" stroke="${m.color}" stroke-width="2"/>
    <text x="${Math.min(x(m.x) + 9, W - R - 70)}" y="${T + 12 + (m.row || 0) * 16}" fill="${m.color}" font-size="12.5">${esc(m.label)}</text></g>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".22"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <line x1="${L}" y1="${y(hi - pad)}" x2="${W - R}" y2="${y(hi - pad)}" stroke="rgba(236,230,218,.08)"/>
    <line x1="${L}" y1="${y(lo + pad)}" x2="${W - R}" y2="${y(lo + pad)}" stroke="rgba(236,230,218,.08)"/>
    <g font-size="12" fill="#93A7AA" font-family="Golos Text,system-ui,sans-serif">
      <text x="0" y="${y(hi - pad) + 4}">${esc(fmtY(hi - pad))}</text><text x="0" y="${y(lo + pad) + 4}">${esc(fmtY(lo + pad))}</text>
      <text x="${L}" y="${H - 7}">${esc(points[0].label)}</text>
      <text x="${W - R}" y="${H - 7}" text-anchor="end">${esc(last.label)}</text>
    </g>
    <polygon points="${area}" fill="url(#${gid})"/>
    <polyline points="${path}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
    ${mk}
    <circle cx="${x(xs[xs.length - 1])}" cy="${y(last.y)}" r="4.5" fill="${color}"/>
  </svg>`;
}
// темы — как в меню Polymarket (тот же список, что в categories.py)
const TOPICS = [["politics", "Политика"], ["sports", "Спорт"], ["esports", "Киберспорт"], ["crypto", "Крипто"], ["finance", "Финансы"],
  ["culture", "Культура"], ["mentions", "Упоминания"], ["weather", "Погода"], ["economics", "Экономика"], ["tech", "Технологии"]];
const dateLabel = (ts) => new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

// ---------------------------------------------------------------- состояние и опрос
const state = { status: null, timers: [], notifOpen: false, quotes: {}, seenPending: null, sig: null, live: false };

async function refreshStatus() {
  try {
    const st = await api("/status");
    state.status = st;
    const dot = $("#sys-dot"), title = $("#sys-title"), sub = $("#sys-sub");
    dot.className = "dot " + (st.ok ? "ok" : st.stopped || st.paused ? "bad" : "warn");
    title.textContent = st.stopped ? "Система остановлена" : st.paused ? "Система на паузе" : st.ok ? "Система работает" : "Нет свежих данных";
    sub.textContent = st.data_age_s == null ? "Данных пока нет" : `Данные обновлены ${ago(st.data_age_s)}`;
    const b = $("#badge-decide");
    b.hidden = !st.need_decision;
    b.textContent = st.need_decision || "";
    $$(".bell-count").forEach((el) => { el.hidden = !st.unread; el.textContent = st.unread; });
    return st;
  } catch {
    $("#sys-dot").className = "dot bad";
    $("#sys-title").textContent = "Нет связи с сервером";
    $("#sys-sub").textContent = "";
    return null;
  }
}
function ago(s) {
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  return `${Math.floor(s / 3600)} ч назад`;
}

function banners(st) {
  if (!st) return `<div class="banner bad" role="alert"><div class="grow"><b>Нет связи с сервером</b>
    <div>Проверьте, что сервер запущен. Открытые ставки в безопасности.</div></div></div>`;
  let out = "";
  if (st.stopped) out += `<div class="banner gray" role="alert"><div class="grow"><b>Система остановлена</b>
    <div>Новые ставки не создаются. Открытые остаются до закрытия рынков.</div></div>
    <button class="btn" data-act="resume">Возобновить</button></div>`;
  else if (st.paused) out += `<div class="banner bad" role="alert"><div class="grow"><b>Система на паузе</b>
    <div>${esc(st.paused_text || "Сработал защитный лимит.")} ${st.paused === "daily_loss_stop" ? "Работа продолжится завтра автоматически." : "Нужна ручная проверка."}</div></div>
    <a class="btn" href="#/positions">Посмотреть ставки</a></div>`;
  const b = st.bootstrap || {};
  if (!b.auto_discover && b.tracked === 0) out += `<div class="banner info"><div class="grow"><b>Добавьте первого кита</b>
    <div>Система собирает данные только по игрокам из вашего списка. Пока список пуст, она ничего не загружает.</div></div>
    <a class="btn p" href="#/whales/add">Добавить кита</a></div>`;
  else if (!b.auto_discover) { /* данные только по списку — баннер первой загрузки не нужен */ }
  else if (b.running && !b.scored) out += `<div class="banner info"><div class="grow"><b>Идёт первая загрузка данных Polymarket</b>
    <div>Найдено кандидатов: ${b.candidates}, загружено сделок: ${(b.trades || 0).toLocaleString("ru-RU")}. Список китов появится после первого пересчёта — обычно в течение часа.</div></div></div>`;
  else if (b.scored && !b.whales) out += `<div class="banner gray"><div class="grow"><b>Подходящих китов пока нет</b>
    <div>Проверено кандидатов: ${b.candidates}. Требования строгие — система продолжает искать каждые 15 минут и пересчитывает раз в сутки. Можно добавить кита вручную.</div></div>
    <a class="btn" href="#/whales/add">Добавить кита</a></div>`;
  else if (!b.running) out += `<div class="banner gray"><div class="grow"><b>Сбор данных не запущен</b>
    <div>Запустите <code>docker compose up -d</code> или <code>whalefollow run</code> — иначе новые сигналы не появятся.</div></div></div>`;
  if (b.error) out += `<div class="banner warn"><div class="grow"><b>Ошибка при загрузке данных</b><div>${esc(b.error)}</div>
    <div class="sub">Проверьте доступ: <code>docker compose run --rm app whalefollow check</code></div></div></div>`;
  if (st.data_age_s != null && st.data_age_s > 120) out += `<div class="banner gray"><div class="grow"><b>Нет связи с Polymarket уже ${Math.floor(st.data_age_s / 60)} мин</b>
    <div>Новые сигналы не появятся, пока связь не вернётся. Открытые ставки в безопасности.</div></div>
    <span class="chip n">Переподключаемся…</span></div>`;
  return out;
}

function tickTimers() {
  $$("[data-deadline]").forEach((el) => {
    const left = Math.max(0, Math.round((+el.dataset.deadline - Date.now()) / 1000));
    el.textContent = left > 0 ? `актуален ещё ${left} с` : "время вышло";
    if (left === 0) el.classList.add("expired");
  });
  // кольцо обратного отсчёта на карточке сигнала
  $$("[data-ring-deadline]").forEach((el) => {
    const left = Math.max(0, (+el.dataset.ringDeadline - Date.now()) / 1000);
    const frac = Math.min(1, left / Math.max(1, +el.dataset.window));
    const arc = el.querySelector(".ring-arc"), num = el.querySelector(".ring-num");
    if (arc) arc.style.strokeDashoffset = String(arc.dataset.len * (1 - frac));
    if (num) num.textContent = left >= 60 ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}` : String(Math.ceil(left));
    el.classList.toggle("low", left < 15 && left > 0);
    el.classList.toggle("over", left <= 0);
  });
}

// ---------------------------------------------------------------- живые обновления (поток событий сервера)
function soundOn() { try { return localStorage.getItem("wf-sound") === "1"; } catch { return false; } }
function setSound(on) { try { localStorage.setItem("wf-sound", on ? "1" : "0"); } catch { /* без хранилища — до перезагрузки */ } }
function chime() {
  if (!soundOn()) return;
  try {
    const ctx = chime.ctx || (chime.ctx = new (window.AudioContext || window.webkitAudioContext)());
    [[880, 0], [1320, 0.12]].forEach(([f, t]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.35);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.4);
    });
  } catch { /* звук недоступен */ }
}

function applyQuotes() {
  Object.entries(state.quotes).forEach(([tok, q]) => paintQuote(tok, q));
}
function paintQuote(tok, q) {
  const sel = (attr) => $$(`[${attr}]`).filter((el) => el.getAttribute(attr) === tok);
  if (q.ask !== null && q.ask !== undefined) sel("data-q-ask").forEach((el) => flash(el, cents(q.ask * 100)));
  if (q.bid !== null && q.bid !== undefined) {
    sel("data-q-bid").forEach((el) => flash(el, cents(q.bid * 100)));
    sel("data-val-tok").forEach((el) => { el.textContent = "стоит " + money(+el.dataset.shares * q.bid); });
    sel("data-pnl-tok").forEach((el) => {
      const pnl = +el.dataset.shares * q.bid - +el.dataset.cost - +(el.dataset.fees || 0);
      flash(el, money(pnl, { sign: true }));
      el.className = "tnum " + tone(pnl);
    });
  }
  if (state.sig && state.sig.live && state.sig.live.token_id === tok) updateSignalLive(q.ask);
}
function flash(el, text) {
  if (el.textContent === text) return;
  el.textContent = text;
  el.classList.remove("tick"); void el.offsetWidth; el.classList.add("tick");
}

let liveRenderT = null;
function softRender() {
  // перерисовать экран, если человек ничего не вводит и не открыто окно
  clearTimeout(liveRenderT);
  liveRenderT = setTimeout(() => {
    const a = document.activeElement;
    const typing = a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
    if (typing || $("#dialog").open || state.notifOpen || document.body.classList.contains("locked")) return refreshStatus();
    const y = window.scrollY;
    render().then(() => window.scrollTo(0, y));
  }, 120);
}

function startLive() {
  if (!window.EventSource || startLive.es) return;
  const es = new EventSource("/api/events");
  startLive.es = es;
  const setLive = (on) => {
    if (state.live === on) return;
    state.live = on;
    $$(".live-badge").forEach((b) => b.outerHTML = liveBadge());
  };
  es.addEventListener("hello", () => setLive(true));
  es.addEventListener("ping", () => setLive(true));
  es.addEventListener("change", (e) => {
    setLive(true);
    const c = JSON.parse(e.data);
    const hash = location.hash || "#/";
    if (c.new_pending && c.new_pending.length) {
      chime();
      if (hash !== "#/" && !hash.startsWith("#/signal/")) toast(c.new_pending.length > 1 ? `Новых сигналов: ${c.new_pending.length}` : "Новый сигнал — откройте «Сигналы»");
    }
    const on = (k) => c.what.includes(k);
    if (hash === "#/" && (on("signals") || on("bets"))) return softRender();
    if (hash.startsWith("#/signal/") && on("signals")) {
      // по открытому сигналу — только если изменился он сам (истёк, куплен), а не пришёл другой
      return api("/signals/" + encodeURIComponent(state.sig ? state.sig.id : "")).then((s) => {
        if (s.status !== "pending" || (s.pending_ids || []).length !== (state.sig.pending || []).length) softRender();
      }).catch(() => {});
    }
    if ((hash.startsWith("#/positions") || hash.startsWith("#/bet/")) && on("bets")) return softRender();
    refreshStatus();
  });
  es.addEventListener("price", (e) => {
    const q = JSON.parse(e.data);
    state.quotes[q.token_id] = q;
    paintQuote(q.token_id, q);
  });
  es.onerror = () => setLive(false);           // браузер переподключится сам
}

// ---------------------------------------------------------------- горячие клавиши
const SHORTCUTS = [["B", "купить по открытому сигналу"], ["S", "пропустить сигнал"], ["J / K", "следующий / предыдущий сигнал"],
  ["Enter", "открыть выбранный сигнал"], ["Esc", "назад к списку сигналов"], ["G затем S", "к сигналам"], ["G затем B", "к моим ставкам"], ["G затем W", "к китам"], ["?", "эта подсказка"]];
function showShortcuts() {
  const d = $("#dialog");
  d.innerHTML = `<form method="dialog" style="display:flex;flex-direction:column;gap:12px;min-width:320px">
    <b style="font-size:20px">Горячие клавиши</b>
    <div class="col" style="gap:8px">${SHORTCUTS.map(([k, t]) => `<div class="bet"><span>${esc(t)}</span><span>${k.split(" ").map((x) => (x === "/" || x === "затем" ? `<span class="sub">${x}</span>` : `<kbd>${esc(x)}</kbd>`)).join(" ")}</span></div>`).join("")}</div>
    <div class="sub">Работают при любой раскладке. В полях ввода отключены.</div>
    <div class="row"><button class="btn p" value="ok">Понятно</button></div></form>`;
  d.showModal();
}
function hotkey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  const t = e.target;
  if (t && (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable)) return false;
  if ($("#dialog").open || document.body.classList.contains("locked")) return false;
  const hash = location.hash || "#/";
  const code = e.code;
  if (e.key === "?" || (code === "Slash" && e.shiftKey)) { showShortcuts(); return true; }
  if (hotkey.g && Date.now() - hotkey.g < 1200) {
    hotkey.g = 0;
    if (code === "KeyS") { location.hash = "#/"; return true; }
    if (code === "KeyB") { location.hash = "#/positions"; return true; }
    if (code === "KeyW") { location.hash = "#/whales"; return true; }
  }
  if (code === "KeyG") { hotkey.g = Date.now(); return true; }
  if (hash.startsWith("#/signal/")) {
    const ids = (state.sig && state.sig.pending) || [], i = ids.indexOf(state.sig && state.sig.id);
    if (code === "KeyB") { const b = $("[data-act=confirm]"); if (b && !b.disabled) b.click(); return true; }
    if (code === "KeyS") { const b = $("[data-act=skip]"); if (b) b.click(); return true; }
    if (code === "KeyJ" && i >= 0 && i < ids.length - 1) { location.hash = "#/signal/" + encodeURIComponent(ids[i + 1]); return true; }
    if (code === "KeyK" && i > 0) { location.hash = "#/signal/" + encodeURIComponent(ids[i - 1]); return true; }
    if (e.key === "Escape") { location.hash = "#/"; return true; }
  }
  if (hash === "#/" && (code === "KeyJ" || code === "KeyK")) {
    const rows = $$("#pending-list .sig-row");
    if (!rows.length) return true;
    const i = rows.indexOf(document.activeElement);
    const next = code === "KeyJ" ? Math.min(rows.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
    rows[next].focus();
    return true;
  }
  return false;
}
function nextPendingAfter(id) {
  const ids = ((state.sig && state.sig.pending) || []).filter((x) => x !== id);
  return ids.length ? "#/signal/" + encodeURIComponent(ids[0]) : "#/";
}

// ---------------------------------------------------------------- роутер
const routes = [
  [/^#?\/?$/, "signals", viewDashboard],
  [/^#\/signal\/([^/]+)$/, "signals", viewSignal],
  [/^#\/journal$/, "signals", viewJournal],
  [/^#\/positions(?:\/(open|closed))?$/, "positions", viewPositions],
  [/^#\/bet\/([^/]+)$/, "positions", viewBet],
  [/^#\/whales$/, "whales", viewWhales],
  [/^#\/whales\/add$/, "whales", viewAdd],
  [/^#\/whales\/([^/]+)$/, "whales", viewWhale],
  [/^#\/results(?:\/(week|month|all))?$/, "results", viewResults],
  [/^#\/settings$/, "settings", viewSettings],
  [/^#\/diagnostics$/, "settings", viewDiagnostics],
  [/^#\/trial$/, "results", viewTrial],
  [/^#\/welcome(?:\/(\d))?$/, null, viewOnboarding],
];

async function render() {
  const hash = location.hash || "#/";
  const st = await refreshStatus();
  if (st && !st.onboarded && !hash.startsWith("#/welcome")) { location.hash = "#/welcome"; return; }
  for (const [re, nav, view] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    document.body.classList.toggle("bare", nav === null);
    $("#side").hidden = nav === null;
    $$("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === nav));
    try {
      await view(...m.slice(1));
    } catch (e) {
      main().innerHTML = `<div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>${esc(e.message)}</div></div>`;
    }
    tickTimers();
    return;
  }
  main().innerHTML = `<div class="card empty"><b>Такой страницы нет</b><a class="btn" href="#/">К сигналам</a></div>`;
}
window.addEventListener("hashchange", () => { state.notifOpen = false; render().then(() => main().focus()); });

// ---------------------------------------------------------------- «Сигналы»
async function viewDashboard() {
  const d = await api("/dashboard");
  const lim = d.daily_limit, used = lim.limit_usd ? Math.min(100, (lim.spent_usd / lim.limit_usd) * 100) : 0;
  const now = Date.now();
  const seen = state.seenPending;
  const pending = d.pending.length ? `<div class="ledger" id="pending-list">${d.pending.map((p) => `
    <a class="ledger-row sig-row${seen && !seen.has(p.signal_id) ? " fresh" : ""}" data-sid="${esc(p.signal_id)}" style="grid-template-columns:minmax(0,1fr) auto auto auto" href="#/signal/${encodeURIComponent(p.signal_id)}">
      <div class="who">${seal(p.whale)}<div class="grow"><div class="t">${esc(p.question)}</div>
        <div class="m">${p.side ? `<span>«${esc(p.side)}»</span>` : ""}<span>Кит ${esc(p.whale)}</span><span>${esc(p.category)}</span><span data-deadline="${now + p.seconds_left * 1000}"></span></div></div></div>
      <div class="n hide-sm"><div class="tnum"><b data-q-ask="${esc(p.token_id)}">—</b></div><div class="sub">кит ${cents(p.whale_cents)}</div></div>
      <span class="chip b">${esc(p.action)} на ${money(p.size_usd)}</span>
      <span class="btn p">Решить</span></a>`).join("")}</div>
    <div class="sub kbd-hint"><kbd>J</kbd> <kbd>K</kbd> — по сигналам, <kbd>Enter</kbd> — открыть, <kbd>?</kbd> — все клавиши</div>`
    : `<div class="card empty"><b>Новых сигналов нет</b>
      <div class="sub" style="font-size:15px;max-width:52ch">Киты из вашего списка не делали ставок, которые прошли проверку. Когда появится выгодная, придёт уведомление.</div>
      <a class="btn" href="#/settings">Настроить уведомления</a></div>`;
  const recent = d.recent.length ? `<div class="ledger">${d.recent.map((r) => `
    <a class="ledger-row" style="grid-template-columns:minmax(0,1fr) auto" href="#/signal/${encodeURIComponent(r.signal_id)}">
      <div><div class="t">${esc(r.question)}</div><div class="m"><span>${esc(r.text)}</span></div></div>
      <span class="chip ${r.label === "Куплено" ? "g" : "n"}">${esc(r.label)}</span></a>`).join("")}</div>`
    : `<div class="sub">Решений пока не было.</div>`;
  main().innerHTML = `
    ${banners(state.status)}
    <div class="page-head"><div><h1>Сигналы</h1>
      <div class="lede">Ставки китов из вашего списка, которые прошли проверку. Решение за вами, пока не включено автоподтверждение.</div></div>
      <div class="row" style="position:relative">
        ${d.mode === "paper" ? `<a class="chip a" style="text-decoration:none" href="#/trial">Пробный режим, деньги виртуальные</a>` : ""}
        ${state.status && state.status.ai ? `<a class="chip ${state.status.ai.effective === "ai" ? "i" : "n"}" style="text-decoration:none" href="#/settings"
          title="Режим принятия решений">${state.status.ai.effective === "ai" ? "С ИИ" : "Без ИИ, решаете вы"}</a>` : ""}
        <button class="btn icon" data-act="bell" aria-label="Уведомления" aria-expanded="false">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
          <span class="count bell-count" ${d.unread_notifications ? "" : "hidden"}>${d.unread_notifications}</span></button>
        <button class="btn icon" data-act="sound" aria-pressed="${soundOn()}" title="Звук при новом сигнале" aria-label="Звук при новом сигнале">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4z"/>${soundOn() ? '<path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>' : '<path d="m17 9 5 6"/><path d="m22 9-5 6"/>'}</svg></button>
        <button class="btn danger" data-act="stop">Остановить всё</button>
        <div id="notif-slot"></div>
      </div></div>
    <section class="statement" aria-label="Баланс">
      <div><div class="label">Баланс</div><div class="figure">${money(d.balance_usd)}</div>
        <div class="sub" style="margin-top:10px">Лимит на сегодня: потрачено ${money(lim.spent_usd)} из ${money(lim.limit_usd)}</div>
        <div class="bar" style="margin-top:8px;max-width:320px" role="progressbar" aria-label="Потрачено от дневного лимита" aria-valuemin="0" aria-valuemax="${lim.limit_usd}" aria-valuenow="${lim.spent_usd}"><span style="width:${used}%"></span></div></div>
      <div class="facts">
        <div class="fact"><div class="label">За 30 дней</div><div class="v ${tone(d.pnl_30d_usd)}">${money(d.pnl_30d_usd, { sign: true })}<small>${signedPct(d.pnl_30d_pct)}</small></div></div>
        <div class="fact"><div class="label">Открытых ставок</div><div class="v">${d.open_bets}</div></div>
        <div class="fact"><div class="label">Ждут решения</div><div class="v">${d.pending.length}</div></div>
      </div></section>
    <div class="top"><h2>Ждут вашего решения</h2>${liveBadge()}</div>
    ${pending}
    <div class="top" style="margin-top:8px"><h2>Недавние решения</h2><a class="link" href="#/journal">Журнал всех решений</a></div>
    ${recent}`;
  state.seenPending = new Set(d.pending.map((p) => p.signal_id));
  applyQuotes();
}

const liveBadge = () => `<span class="live-badge ${state.live ? "on" : ""}" title="${state.live ? "Новые сигналы и цены приходят сразу" : "Обновление раз в 10 секунд"}"><span class="pulse" aria-hidden="true"></span>${state.live ? "в реальном времени" : "обновление раз в 10 с"}</span>`;

function notifHref(i) {
  if (i.kind === "signal" && i.ref) return "#/signal/" + encodeURIComponent(i.ref);
  if ((i.kind === "whale_check" || i.kind === "whale_status") && i.ref) return "#/whales/" + encodeURIComponent(i.ref);
  if (i.kind === "whale_exit" || i.kind === "settled") return "#/positions";
  if (i.kind === "weekly") return "#/results/week";
  return null;
}

async function openNotifications(btn) {
  const slot = $("#notif-slot");
  if (state.notifOpen) { slot.innerHTML = ""; state.notifOpen = false; btn.setAttribute("aria-expanded", "false"); return; }
  const n = await api("/notifications");
  state.notifOpen = true;
  btn.setAttribute("aria-expanded", "true");
  slot.innerHTML = `<div class="card notif" role="region" aria-label="Уведомления">
    <div class="top" style="padding:14px 20px;border-bottom:1px solid var(--line)"><b style="font-size:16px">Уведомления</b>
      <button class="link" style="background:none;border:0;cursor:pointer;min-height:44px" data-act="read-all">Прочитать все</button></div>
    ${n.items.length ? n.items.map((i) => {
      const href = notifHref(i);
      const inner = `<span class="dot ${i.unread ? "ok" : "read"}" aria-hidden="true"></span>
      <div>${i.unread ? `<b>${esc(i.title)}</b>` : esc(i.title)}<div class="sub">${esc(i.body)}${i.body ? " · " : ""}${esc(i.when)}</div></div>`;
      return href ? `<a class="nt" href="${href}" style="color:inherit;text-decoration:none">${inner}</a>` : `<div class="nt">${inner}</div>`;
    }).join("")
      : `<div class="nt">Уведомлений пока нет</div>`}</div>`;
}

// ---------------------------------------------------------------- карточка сигнала
async function viewSignal(id) {
  const s = await api("/signals/" + encodeURIComponent(id));
  const pending = s.status === "pending";
  const decide = pending && s.recommendation === "BUY" && s.live;
  state.sig = decide ? { id, live: s.live, pending: s.pending_ids || [], size: s.size_usd } : { id, pending: s.pending_ids || [] };
  const now = Date.now();
  const mark = (ok) => (ok === true ? ["g", "✓"] : ok === false ? ["r", "✕"] : ["a", "!"]);
  const why = `<ul class="why">${s.checks.slice(0, 4).map((c) => { const [t, ic] = mark(c.ok); return `<li class="${t}"><span class="ic" aria-hidden="true">${ic}</span><div><b>${esc(c.title)}</b><div class="sub">${esc(c.text)}</div></div></li>`; }).join("")}</ul>`;
  const checks = `<div class="card checks">${s.checks.map((c) => `<div class="check-row">
      <span class="chip ${c.ok === true ? "g" : c.ok === false ? "r" : "a"}">${c.ok === true ? "Да" : c.ok === false ? "Нет" : "Учтите"}</span>
      <div><b>${esc(c.title)}</b><div class="sub">${esc(c.text)}</div></div></div>`).join("")}</div>`;
  const statusChip = { executed: ["g", "Куплено"], skipped: ["n", "Пропущено системой"], rejected: ["n", "Пропущено вами"],
    expired: ["n", "Время вышло"] }[s.status];
  const ids = s.pending_ids || [], pos = ids.indexOf(id);
  const nav = pos >= 0 && ids.length > 1 ? `<div class="sig-nav sub">Сигнал ${pos + 1} из ${ids.length}
      ${pos > 0 ? `<a class="link" href="#/signal/${encodeURIComponent(ids[pos - 1])}"><kbd>K</kbd> предыдущий</a>` : ""}
      ${pos < ids.length - 1 ? `<a class="link" href="#/signal/${encodeURIComponent(ids[pos + 1])}">следующий <kbd>J</kbd></a>` : ""}</div>` : "";
  const head = `<a class="back" href="#/">← Все сигналы</a>
    <div class="sig-head"><div class="grow"><h1>${esc(s.question)}</h1><div class="sub" style="margin-top:6px">Кит
      <button class="link inline" data-act="player" data-id="${esc(s.entity_id)}" data-player="${esc(s.entity_id)}">${esc(s.whale)}</button> · ${esc(s.category)}${s.side ? ` · ставка на «${esc(s.side)}»` : ""}</div></div>${nav}</div>`;

  if (decide) {
    const L = s.live, win = L.window_s, R = 30, len = (2 * Math.PI * R).toFixed(1);
    main().innerHTML = `${head}
      <section class="decide card" aria-label="Решение по сигналу">
        <div class="decide-main">
          <div class="scale-wrap">${priceScale(L)}</div>
          <div class="decide-facts">
            <div><div class="sub">Кит купил по</div><b class="tnum">${cents(L.whale_price * 100)}</b></div>
            <div><div class="sub">Сейчас можно купить по</div><b class="tnum" id="live-ask">${cents(L.ask * 100)}</b>${L.live ? '<span class="live-dot" title="Цена из потока биржи, обновляется сама" aria-label="живая цена"></span>' : ""}</div>
            <div><div class="sub">Задержка стоила</div><b class="tnum" id="delay-cost">—</b></div>
            <div><div class="sub">Запас выгоды сейчас</div><b class="tnum" id="edge-now">—</b><div class="sub" id="edge-need">нужно не меньше ${cents(L.threshold * 100)} на акцию</div></div>
          </div>
        </div>
        <div class="ring" data-ring-deadline="${now + s.seconds_left * 1000}" data-window="${win}" role="timer" aria-label="Сколько осталось на решение">
          <svg viewBox="0 0 72 72" aria-hidden="true"><circle cx="36" cy="36" r="${R}" class="ring-track"/>
            <circle cx="36" cy="36" r="${R}" class="ring-arc" data-len="${len}" stroke-dasharray="${len}" stroke-dashoffset="0" transform="rotate(-90 36 36)"/></svg>
          <div class="ring-num tnum">${s.seconds_left}</div><div class="ring-cap">на решение</div>
        </div>
      </section>
      <section class="decide-act">
        <div class="stake"><div class="sub">Ставка</div><div class="num">${money(s.size_usd)}</div>
          <div class="sub">Сбудется: <b class="up">${money(s.if_win_usd, { sign: true })}</b> · Нет: <b class="down">${money(s.if_lose_usd, { sign: true })}</b></div></div>
        <div class="row" style="gap:10px">
          <button class="btn p xl" data-act="confirm" data-id="${esc(id)}">Купить на ${money(s.size_usd)} <kbd>B</kbd></button>
          <button class="btn xl" data-act="skip" data-id="${esc(id)}">Пропустить <kbd>S</kbd></button>
          <button class="link" style="background:none;border:0;cursor:pointer;min-height:44px" data-act="amount">Изменить сумму</button></div>
      </section>
      <div id="amount" hidden class="card row"><label for="amount-inp">Сумма ставки, $</label>
        <input id="amount-inp" class="inp" type="number" min="1" max="${s.size_usd}" step="1" value="${Math.round(s.size_usd)}" style="width:120px">
        <span class="sub">Не больше ${money(s.size_usd)} — это расчёт системы с учётом лимитов</span></div>
      <div id="edge-gone" class="banner warn" hidden><div class="grow"><b>Выгода ушла</b><div>Цена поднялась — при покупке сейчас запас меньше порога. Система не купит дороже безопасной цены.</div></div></div>
      ${s.mode === "manual" ? `<div class="banner ${s.ai_failed ? "warn" : "gray"}"><div class="grow"><b>${s.ai_failed ? "ИИ не ответил — решение за вами" : "Режим без ИИ — решение за вами"}</b>
        <div>Ниже — всё, что собрала система: история кита, другие рынки прогнозов и новости.</div></div></div>` : ""}
      <h2>Почему стоит купить</h2>
      ${why}
      <div class="sub">Лимит на одну ставку — ${money(s.limits.max_bet_usd)} · на сегодня осталось ${money(s.limits.left_today_usd)}${s.fee_usd ? ` · комиссия ≈ $${s.fee_usd.toFixed(2).replace(".", ",")} уже учтена` : ""}${s.delay_s !== null && s.delay_s !== undefined ? ` · сигнал пришёл через ${Math.round(s.delay_s)} с после сделки кита` : ""}</div>
      <h2>Рынок и кто в нём стоит</h2>
      <div id="market-box"></div>
      <details class="more"><summary class="link">Подробнее: все проверки, оценка ИИ, данные, новости</summary>
        <div class="col" style="gap:16px;margin-top:14px">${checks}${researchBlocks(s)}</div></details>`;
    loadMarketPanel("market-box", s.token_id);
    updateSignalLive(L.ask);
    applyQuotes();
    return;
  }

  main().innerHTML = `${head}
    ${s.recommendation === "BUY" ? `<div class="card hero">
      <div style="flex:1"><span class="chip g">${s.mode === "ai" ? "Рекомендовали купить" : "Расчёт по истории кита: купить"} «${esc(s.side)}»</span>
        <div class="num">${money(s.size_usd)} по цене ${esc(s.price_cents)}¢</div></div>
      <div class="right"><div class="sub">Если сбудется</div><div style="font-size:22px;font-weight:700" class="up">${money(s.if_win_usd, { sign: true })}</div></div>
      <div class="right"><div class="sub">Если нет</div><div style="font-size:22px;font-weight:700" class="down">${money(s.if_lose_usd, { sign: true })}</div></div>
    </div>
    <p style="margin:0;color:var(--ink-2)">${esc(s.plain)}</p>` :
    `<div class="card"><span class="chip n">Пропущено</span><div style="margin-top:8px;font-size:16px">${esc(s.skip_reason || s.plain)}</div></div>`}
    ${statusChip && s.recommendation === "BUY" ? `<div><span class="chip ${statusChip[0]}">${statusChip[1]}</span></div>` : ""}
    ${pending && s.kind === "entry" && s.recommendation !== "BUY" ? "" : ""}
    <h2>${s.mode === "ai" ? "Почему мы так решили" : "Что проверила система"}</h2>
    ${checks}
    <h2>Рынок и кто в нём стоит</h2>
    <div id="market-box"></div>
    ${researchBlocks(s)}`;
  if (s.token_id) loadMarketPanel("market-box", s.token_id);
}

// шкала цен: где купил кит, где цена сейчас, сколько по нашей оценке стоит исход; закрашено — наш запас
function priceScale(L) {
  const pts = [L.whale_price, L.ask, L.q_eff].filter((v) => v !== null && v !== undefined);
  let lo = Math.max(0, Math.min(...pts) - 0.06), hi = Math.min(1, Math.max(...pts) + 0.06);
  if (hi - lo < 0.15) { const c = (hi + lo) / 2; lo = Math.max(0, c - 0.075); hi = Math.min(1, c + 0.075); }
  state.scale = { lo, hi };
  const x = (v) => (((v - lo) / (hi - lo)) * 100).toFixed(2);
  return `<div class="scale" role="img" aria-label="Цена кита ${cents(L.whale_price * 100)}, сейчас ${cents(L.ask * 100)}, наша оценка ${cents(L.q_eff * 100)}">
    <div class="scale-track"></div>
    <div class="scale-zone" id="scale-zone"></div>
    <div class="scale-mk whale" style="left:${x(L.whale_price)}%"><span>кит</span></div>
    <div class="scale-mk fair" style="left:${x(L.q_eff)}%"><span>оценка ${cents(L.q_eff * 100)}</span></div>
    <div class="scale-mk now" id="scale-now" style="left:${x(L.ask)}%"><span>сейчас</span></div>
    <div class="scale-ends sub"><span>${cents(lo * 100)}</span><span>${cents(hi * 100)}</span></div></div>`;
}

function updateSignalLive(ask) {
  const S = state.sig;
  if (!S || !S.live || ask === null || ask === undefined) return;
  const L = S.live;
  const fee = L.fee_rate * ask * (1 - ask);
  const edge = L.q_eff - ask - fee;
  const ok = edge >= L.threshold;
  const e = $("#edge-now"), d = $("#delay-cost"), a = $("#live-ask");
  if (!e) return;
  if (a) a.textContent = cents(ask * 100);
  e.textContent = (edge >= 0 ? "+" : "−") + cents(Math.abs(edge) * 100).replace("¢", "") + "¢";
  e.className = "tnum " + (ok ? "up" : "down");
  const delay = ask - L.whale_price;
  d.textContent = delay > 0.0005 ? cents(delay * 100) + " на акцию" : "ничего";
  const zone = $("#scale-zone"), now = $("#scale-now"), sc = state.scale;
  if (zone && sc) {
    const x = (v) => ((Math.min(Math.max(v, sc.lo), sc.hi) - sc.lo) / (sc.hi - sc.lo)) * 100;
    const l = Math.min(x(ask), x(L.q_eff)), r = Math.max(x(ask), x(L.q_eff));
    zone.style.left = l + "%"; zone.style.width = (r - l) + "%";
    zone.className = "scale-zone " + (ok ? "ok" : "bad");
    now.style.left = x(ask) + "%";
  }
  const gone = $("#edge-gone");
  if (gone) gone.hidden = ok;
  const btn = $("[data-act=confirm]");
  if (btn) btn.classList.toggle("dim", !ok);
}

function researchBlocks(s) {
  const w = s.whale_stats || {}, pr = s.prices || {};
  let out = "";
  if (s.ai) {
    const a = s.ai;
    const list = (arr) => arr.length ? `<ul style="margin:6px 0 0;padding-left:20px">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="sub">—</div>`;
    out += `<div class="card col" style="gap:10px"><div class="top"><h2 style="margin:0">Оценка ИИ</h2>
      <span class="sub">ИИ не видел ни цену рынка, ни ставку кита</span></div>
      <div class="row" style="gap:28px"><div><div class="sub">Вероятность по оценке ИИ</div><div class="big">${a.probability_pct}%</div></div>
        <div><div class="sub">Уверенность</div><div style="font-size:18px;font-weight:600">${esc(a.confidence)}</div></div>
        ${a.base_rate_pct !== null ? `<div><div class="sub">Обычно такие события</div><div style="font-size:18px;font-weight:600">${a.base_rate_pct}%</div></div>` : ""}</div>
      <div class="grid2"><div><b>За</b>${list(a.for)}</div><div><b>Против</b>${list(a.against)}</div></div>
      ${a.uncertain.length ? `<div class="sub">Что может изменить оценку: ${esc(a.uncertain.join("; "))}</div>` : ""}
      ${a.trusted ? "" : `<div class="sub">ИИ ещё не прошёл проверку точности на 300 рынках, поэтому его вес в решении снижен.</div>`}</div>`;
  }
  out += `<div class="card col" style="gap:8px"><h2 style="margin:0">Данные для решения</h2>
    <div class="grid3" style="gap:10px">
      <div><div class="sub">Кит купил по</div><b style="font-size:18px">${cents(pr.whale_cents)}</b></div>
      <div><div class="sub">Сейчас можно купить по</div><b style="font-size:18px">${cents(pr.now_cents)}</b></div>
      <div><div class="sub">Шанс по истории кита</div><b style="font-size:18px">${pr.model_cents ?? "—"}%</b></div>
      <div><div class="sub">Кит за 90 дней</div><b class="${tone(w.return_90d_pct)}">${pct(w.return_90d_pct)}</b></div>
      <div><div class="sub">Событий в истории (в этой теме)</div><b>${w.events ?? "—"} (${w.events_in_topic ?? "—"})</b></div>
      <div><div class="sub">Держит до закрытия рынка</div><b>${w.hold_pct ?? "—"}% ставок</b></div></div>
    <div class="sub">Если цена «сейчас» заметно выше цены кита, выгода уже частично ушла.</div></div>`;
  if (s.forecasts && s.forecasts.length) {
    out += `<div class="card col" style="gap:10px"><div class="top"><h2 style="margin:0">Другие рынки прогнозов</h2><span class="sub">Похожие вопросы — проверьте, что речь о том же</span></div>
      ${s.forecasts.map((f) => `<div class="bet"><a class="link" style="font-weight:500" href="${safeUrl(f.url)}" target="_blank" rel="noopener">${esc(f.title)}</a>
        <span class="chip i">${esc(f.source)} · ${f.probability_pct}%</span></div>`).join("")}</div>`;
  }
  if (s.warnings && s.warnings.length) {
    out += `<div class="banner warn"><div class="grow"><b>Осторожно с новостями</b><ul style="margin:6px 0 0;padding-left:20px">${s.warnings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>`;
  }
  out += `<div class="card col" style="gap:10px"><h2 style="margin:0">Что пишут</h2>
    ${s.news && s.news.length ? s.news.map((n) => `<div class="bet"><div><a class="link" style="font-weight:500" href="${safeUrl(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>
      <div class="sub">${esc(n.source)} · ${esc(n.kind)} · ${esc(n.when)}</div></div>
      <span class="chip ${n.reliable ? "g" : "a"}">${n.reliable ? "надёжный источник" : "непроверенный"}</span></div>`).join("")
      : `<div class="sub">Свежих новостей по этому вопросу не нашлось.</div>`}</div>`;
  return out;
}

// ---------------------------------------------------------------- журнал
async function viewJournal() {
  const j = await api("/journal?limit=200");
  const label = { executed: "Куплено", pending: "Ждёт решения", skipped: "Пропущено", rejected: "Пропущено вами", expired: "Время вышло" };
  main().innerHTML = `<a class="back" href="#/">← Сигналы</a><h1>Журнал решений</h1>
    <div class="sub">Каждый сигнал, включая пропущенные, с причиной.</div>
    ${j.items.length ? j.items.map((r) => `<a class="card item" style="text-decoration:none;color:inherit" href="#/signal/${encodeURIComponent(r.signal_id)}">
      <div class="grow"><b>${esc(r.question)}</b><div class="sub">Кит ${esc(r.whale)} · ${esc(new Date(r.ts * 1000).toLocaleString("ru-RU"))}
      ${r.kind === "whale_exit" ? " · кит вышел" : ""} ${r.reason ? "· " + esc(r.reason) : ""}</div></div>
      <span class="chip ${r.status === "executed" ? "g" : r.status === "pending" ? "a" : "n"}">${esc(label[r.status] || r.status)}</span></a>`).join("")
      : `<div class="card empty">Решений пока не было</div>`}`;
}

// ---------------------------------------------------------------- «Мои ставки»
const cents = (v) => (v === null || v === undefined ? "—" : `${String(Math.round(v * 10) / 10).replace(".", ",")}¢`);
const whaleStateChip = (st) => ({ "держит": `<span class="chip g">кит держит</span>`, "вышел": `<span class="chip a">кит вышел</span>` }[st] || "");

async function viewPositions(tab = "open") {
  const p = await api("/positions?status=" + tab);
  const sm = p.summary || {};
  const decisions = p.need_decision.map((d) => `
    <div class="banner warn" style="flex-direction:column;align-items:stretch;gap:12px">
      <div class="row">${seal(d.whale)}<div class="grow"><b style="font-size:16px">Кит ${esc(d.whale)} продал свою позицию — нужно решение</b>
        <div>«${esc(d.question)}». Мы повторили эту ставку на ${money(d.stake_usd)}. ${esc(d.text)}</div></div></div>
      <div class="row"><button class="btn ${d.recommendation === "hold" ? "p" : ""}" data-act="exit" data-id="${esc(d.signal_id)}" data-v="hold">Держать до конца</button>
        <button class="btn ${d.recommendation === "sell" ? "p" : ""}" data-act="exit" data-id="${esc(d.signal_id)}" data-v="sell">Продать сейчас за ${money(d.sell_now_usd)}</button>
        <span class="sub">Продажа зафиксирует ${money(d.sell_pnl_usd, { sign: true })}</span></div></div>`).join("");
  const open = tab === "open";
  const rows = p.items.length ? p.items.map((r) => `
    <a class="ledger-row bets-grid" href="#/bet/${encodeURIComponent(r.token_id)}">
      <div class="who">${seal(r.whale)}<div class="grow"><div class="t">${esc(r.question)}</div>
        <div class="m"><span>«${esc(r.side)}»</span><span>кит ${esc(r.whale)}</span><span>${esc(r.category)}</span>${open ? whaleStateChip(r.whale_state) : ""}</div></div></div>
      <div class="n"><div class="tnum">${cents(r.entry_cents)}</div><div class="sub">кит ${cents(r.whale_cents)}</div></div>
      <div class="n hide-md"><div class="tnum"${open ? ` data-q-bid="${esc(r.token_id)}"` : ""}>${open ? cents(r.now_cents) : esc(r.result || "")}</div><div class="sub">${open ? "сейчас" : `держали ${String(r.held_days ?? "—").replace(".", ",")} дн`}</div></div>
      <div class="n"><div class="tnum">${money(r.stake_usd)}</div><div class="sub"${open ? ` data-val-tok="${esc(r.token_id)}" data-shares="${r.shares_exact}"` : ""}>${open ? "стоит " + money(r.value_usd) : "получено " + money(r.value_usd)}</div></div>
      <div class="n"><b class="tnum ${tone(r.pnl_usd)}"${open ? ` data-pnl-tok="${esc(r.token_id)}" data-shares="${r.shares_exact}" data-cost="${r.cost_exact}" data-fees="${r.fees_usd}"` : ""}>${money(r.pnl_usd, { sign: true })}</b><div class="sub">${signedPct(r.change_pct)}</div></div>
      <div class="n hide-md"><div>${open ? (r.closes_in_days === 0 ? "сегодня" : "через " + days(r.closes_in_days)) : esc(r.result || "закрыта")}</div><div class="sub">${open ? "закроется" : ""}</div></div></a>`).join("")
    : `<div class="ledger-empty">${open ? "Открытых ставок нет. Новые появятся, когда вы подтвердите сигнал." : "Закрытых ставок пока нет."}</div>`;
  main().innerHTML = `${banners(state.status)}
    <div class="page-head"><div><h1>Мои ставки</h1>
      <div class="lede">Повторённые ставки китов. Нажмите на ставку, чтобы увидеть цену входа, график и что делает кит.</div></div>
      <div class="seg" role="group" aria-label="Какие ставки показать">
        <button aria-pressed="${open}" data-go="#/positions/open">Открытые ${p.counts.open}</button>
        <button aria-pressed="${!open}" data-go="#/positions/closed">Закрытые ${p.counts.closed}</button></div></div>
    <section class="statement" aria-label="Сводка по ставкам">
      <div><div class="label">${open ? "Стоят сейчас" : "Итог закрытых ставок"}</div>
        <div class="figure ${open ? "" : tone(sm.realized_pnl_usd)}">${open ? money(sm.open_value_usd) : money(sm.realized_pnl_usd, { sign: true })}</div>
        <div class="sub" style="margin-top:10px">${open ? `Вложено ${money(p.in_play_usd)} в ${p.items.length} ${plural(p.items.length, "ставку", "ставки", "ставок")}` : "Сумма прибыли и убытков с учётом комиссий"}</div></div>
      <div class="facts">
        <div class="fact"><div class="label">${open ? "Изменение" : "Выигрышей"}</div><div class="v ${open ? tone(sm.open_pnl_usd) : ""}">${open ? money(sm.open_pnl_usd, { sign: true }) : (sm.win_pct === null ? "—" : String(sm.win_pct).replace(".", ",") + "%")}</div></div>
        <div class="fact"><div class="label">${open ? "Закрытые, итог" : "Закрытых ставок"}</div><div class="v ${open ? tone(sm.realized_pnl_usd) : ""}">${open ? money(sm.realized_pnl_usd, { sign: true }) : p.counts.closed}</div></div>
        <div class="fact"><div class="label">Комиссии Polymarket</div><div class="v">${money(sm.fees_usd, { cents: true })}</div></div>
      </div></section>
    ${decisions}
    <div class="ledger">
      <div class="ledger-head bets-grid"><span>Ставка</span><span class="n">Вход</span><span class="n hide-md">${open ? "Цена" : "Итог"}</span><span class="n">Сумма</span><span class="n">Прибыль</span><span class="n hide-md">${open ? "Рынок" : "Результат"}</span></div>
      ${rows}</div>
    <p class="sub" style="margin:0">Стоимость открытой ставки меняется вместе с ценой на рынке${state.live ? " — здесь она обновляется сама" : ""}. Окончательный итог известен, когда рынок закроется.</p>`;
  applyQuotes();
}

async function viewBet(token) {
  const b = await api("/bets/" + encodeURIComponent(token));
  const pts = (b.chart || []).map((p) => ({ x: p.ts, y: p.price * 100, label: dateLabel(p.ts) }));
  const marks = [];
  if (b.marks.whale.ts && b.marks.whale.price !== null) marks.push({ x: b.marks.whale.ts, y: b.marks.whale.price * 100, color: "#C8A464", label: "кит купил", row: 0 });
  if (b.marks.ours.ts && b.marks.ours.price !== null) marks.push({ x: b.marks.ours.ts, y: b.marks.ours.price * 100, color: "#A9CFE3", label: "мы купили", row: 1 });
  const now = Date.now() / 1000;
  main().innerHTML = `
    <a class="back" href="#/positions/${b.is_open ? "open" : "closed"}">← Мои ставки</a>
    <div class="page-head"><div class="who" style="align-items:flex-start">${seal(b.whale, { lg: true })}
      <div><h1 style="font-size:36px">${esc(b.question)}</h1>
        <div class="row" style="margin-top:12px;gap:8px"><span class="chip b">«${esc(b.side)}»</span><span class="chip n">${esc(b.category)}</span>
          <button class="chip n" data-act="player" data-id="${esc(b.entity_id)}" data-player="${esc(b.entity_id)}">кит ${esc(b.whale)}</button>
          ${b.is_open ? whaleStateChip(b.whale_state) : `<span class="chip ${b.pnl_usd > 0 ? "g" : "r"}">${esc(b.result || "закрыта")}</span>`}</div></div></div>
      <div class="row">${b.url ? `<a class="btn" href="${safeUrl(b.url)}" target="_blank" rel="noopener">Открыть на Polymarket</a>` : ""}
        ${b.is_open ? `<button class="btn danger" data-act="sell-bet" data-id="${esc(b.token_id)}">Продать сейчас</button>` : ""}</div></div>
    <section class="statement" aria-label="Итог ставки">
      <div><div class="label">${b.is_open ? "Сейчас прибыль" : "Итог ставки"}</div>
        <div class="figure ${tone(b.pnl_usd)}">${money(b.pnl_usd, { sign: true })}</div>
        <div class="sub" style="margin-top:10px">${signedPct(b.change_pct)} от вложенного, комиссия ${money(b.fees_usd, { cents: true })} учтена</div></div>
      <div class="facts">
        <div class="fact"><div class="label">Вложено</div><div class="v">${money(b.stake_usd)}</div></div>
        <div class="fact"><div class="label">${b.is_open ? "Стоит сейчас" : "Получено"}</div><div class="v">${money(b.value_usd)}</div></div>
        <div class="fact"><div class="label">${b.is_open ? "Если сбудется" : "Держали"}</div><div class="v ${b.is_open ? "up" : ""}">${b.is_open ? money(b.if_win_usd, { sign: true }) : String(b.held_days ?? "—").replace(".", ",") + " дн"}</div></div>
      </div></section>
    <div class="grid3">
      <div class="card"><div class="label">Кит купил по</div><div class="big">${cents(b.whale_cents)}</div></div>
      <div class="card"><div class="label">Мы купили по</div><div class="big">${cents(b.entry_cents)}</div>
        <div class="sub">${b.whale_cents && b.entry_cents ? (b.entry_cents > b.whale_cents ? `на ${cents(b.entry_cents - b.whale_cents)} дороже кита` : "не дороже кита") : ""}</div></div>
      <div class="card"><div class="label">${b.is_open ? "Цена сейчас" : "Акций"}</div><div class="big">${b.is_open ? cents(b.now_cents) : String(b.shares).replace(".", ",")}</div>
        <div class="sub">${b.is_open ? `${String(b.shares).replace(".", ",")} акций, закроется ${b.closes_in_days === 0 ? "сегодня" : "через " + days(b.closes_in_days)}` : ""}</div></div></div>
    <div id="scen-box"></div>
    ${pts.length > 1 ? `<div class="card"><div class="top"><h2>Цена «${esc(b.side)}»</h2><span class="sub">в центах за акцию; 100¢ — если событие произойдёт</span></div>
      ${lineChart(pts, { height: 220, label: "Цена ставки", fmtY: (v) => Math.round(v) + "¢", marks })}</div>` : ""}
    <h2>Рынок</h2>
    <div id="market-box"></div>
    <div class="grid2">
      <div class="card"><h2 style="margin-bottom:16px">Как это было</h2>
        <ol class="timeline">${b.events.map((e) => `<li class="${e.kind === "whale" ? "whale" : ""} ${e.ts > now ? "future" : ""}">
          <div>${esc(e.text)}</div><div class="when">${new Date(e.ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}</div></li>`).join("")}</ol></div>
      <div class="card col" style="gap:12px"><h2>Почему купили</h2>
        <div class="check-row"><span class="chip b">${b.model.q_pct ?? "—"}%</span><div><b>Шанс по расчёту системы</b><div class="sub">История кита в этой теме${b.model.ai_pct ? ` и оценка ИИ ${b.model.ai_pct}%` : ""}</div></div></div>
        <div class="check-row"><span class="chip ${b.model.edge_pct > 0 ? "g" : "n"}">${b.model.edge_pct === null ? "—" : String(b.model.edge_pct).replace(".", ",") + "%"}</span><div><b>Запас выгоды при покупке</b><div class="sub">Шанс минус цена, комиссия и запас на неточность</div></div></div>
        ${b.signal_id ? `<a class="link" href="#/signal/${encodeURIComponent(b.signal_id)}">Открыть исходный сигнал с новостями</a>` : ""}</div></div>`;
  if (b.is_open) $("#scen-box").innerHTML = scenariosHtml(b, null);
  loadMarketPanel("market-box", b.token_id, { noChart: pts.length > 1, before: (m) => { const sb = $("#scen-box"); if (sb && b.is_open) sb.innerHTML = scenariosHtml(b, m); return ""; } });
}

// ---------------------------------------------------------------- «Киты»
const riskTone = { "низкий": "Низкий", "средний": "Средний", "высокий": "Высокий" };
const statusChip = (r) => r.status === "Надёжный" ? "g" : r.status === "Проверяется" ? "a" : r.status === "Только следить" ? "i" : "n";
const pctText = (v) => (v === null || v === undefined ? "—" : String(Math.round(v * 10) / 10).replace(".", ",") + "%");

async function viewWhales() {
  const w = await api("/whales");
  const copying = w.items.filter((r) => r.copy).length;
  const rows = w.items.length ? w.items.map((r) => {
    const st = r.stats || {};
    return `<div class="ledger-row whales-grid click" data-go="#/whales/${encodeURIComponent(r.entity_id)}" role="link" tabindex="0">
      <div class="who" data-player="${esc(r.entity_id)}" data-wfill="${r.pm ? "" : esc(r.entity_id)}">${r.pm ? avatar(r.pm.image, r.name) : seal(r.name)}<div class="grow"><div class="t nm">${esc((r.pm && r.pm.name) || "Кит " + r.name)}</div>
        <div class="m"><span>${esc(r.categories.join(", ") || "темы появятся после пересчёта")}</span>${r.pm && r.pm.form ? `<span class="chip ${FORM_TEXT[r.pm.form][0]} sm">${FORM_TEXT[r.pm.form][1]}</span>` : ""}${r.pm && r.pm.pnl_30d_usd !== null && r.pm.pnl_30d_usd !== undefined ? `<span class="${tone(r.pm.pnl_30d_usd)}">30 дн ${compactMoney(r.pm.pnl_30d_usd, { sign: true })}</span>` : ""}</div></div></div>
      <div><span class="chip ${statusChip(r)}">${esc(r.status)}</span>${r.note ? `<div class="sub" style="margin-top:4px">${esc(r.note)}</div>` : ""}</div>
      <div class="n"><div class="tnum">${pctText(st.win_pct)}</div><div class="sub">из ${st.closed ?? 0}</div></div>
      <div class="n hide-md"><div class="tnum">${st.bets ?? 0}</div><div class="sub">${st.open ? st.open + " откр." : "ставок"}</div></div>
      <div class="n"><b class="tnum ${tone(st.pnl_usd)}">${money(st.pnl_usd, { sign: true })}</b><div class="sub">${st.roi_pct === null || st.roi_pct === undefined ? "" : signedPct(st.roi_pct) + " к вложенному"}</div></div>
      <div class="hide-md hide-sm">${spark(st.spark)}<div class="sub">12 недель</div></div>
      <div>${r.can_copy || r.copy ? `<label class="ck" style="min-height:36px" onclick="event.stopPropagation()"><input type="checkbox" data-act="copy" data-id="${esc(r.entity_id)}" ${r.copy ? "checked" : ""}>${r.copy ? "Копируем" : "Выкл"}</label>`
        : `<span class="sub">${esc(r.note || "Только следить")}</span>`}</div></div>`;
  }).join("") : `<div class="ledger-empty"><div style="font:500 24px/1.2 var(--serif);color:var(--pearl)">Список пуст</div>
      <div style="margin:8px auto 16px;max-width:50ch">Добавьте кита по адресу кошелька или ссылке на профиль. Данные собираются только по китам из этого списка.</div>
      <a class="btn p" href="#/whales/add">Добавить кита</a></div>`;
  main().innerHTML = `${banners(state.status)}
    <div class="page-head"><div><h1>Киты</h1>
      <div class="lede">Игроки, за которыми вы следите. «Надёжный» — преимущество подтверждено на многих независимых событиях, а не удачей.</div></div>
      <a class="btn p" href="#/whales/add">Добавить игрока</a></div>
    ${w.items.length ? `<div class="row" style="gap:28px">
      <div class="fact"><div class="label">В списке</div><div class="v">${w.items.length}</div></div>
      <div class="fact"><div class="label">Копируем</div><div class="v">${copying}</div></div>
      <div class="fact"><div class="label">Открытых ставок у китов</div><div class="v">${w.items.reduce((a, r) => a + ((r.stats || {}).open || 0), 0)}</div></div></div>` : ""}
    ${w.new_candidates ? `<div class="banner info"><div class="grow"><b>Система нашла ${w.new_candidates} ${plural(w.new_candidates, "нового кандидата", "новых кандидата", "новых кандидатов")}</b>
      <div class="sub">Они прошли проверку. Решение за вами.</div></div><a class="btn" href="#/whales/add">Посмотреть</a></div>` : ""}
    <div class="ledger">
      ${w.items.length ? `<div class="ledger-head whales-grid"><span>Кит</span><span>Статус</span><span class="n">Выигрыши</span><span class="n hide-md">Ставок</span><span class="n">Прибыль кита</span><span class="hide-md">Динамика</span><span>Копирование</span></div>` : ""}
      ${rows}</div>`;
  fillWhaleNames();
}

// имена и аватары китов из готовых данных Polymarket — догружаются после показа списка
async function fillWhaleNames() {
  const els = $$("[data-wfill]").filter((e) => e.dataset.wfill);
  for (let i = 0; i < els.length; i += 4) {
    await Promise.all(els.slice(i, i + 4).map(async (el) => {
      try {
        const c = await api(`/players/${encodeURIComponent(el.dataset.wfill)}?mini=1`);
        if (!c || c.ok === false || !document.body.contains(el)) return;
        const sl = el.querySelector(".seal");
        if (c.image && sl) sl.outerHTML = avatar(c.image, c.short);
        if (c.name) el.querySelector(".nm").textContent = c.name;
        const f = c.form && c.form.state;
        el.querySelector(".m").insertAdjacentHTML("beforeend", `${f ? `<span class="chip ${FORM_TEXT[f][0]} sm">${FORM_TEXT[f][1]}</span>` : ""}${c.pnl_30d_usd !== null && c.pnl_30d_usd !== undefined ? `<span class="${tone(c.pnl_30d_usd)}">30 дн ${compactMoney(c.pnl_30d_usd, { sign: true })}</span>` : ""}`);
      } catch { /* без имени — останется короткий код */ }
    }));
  }
}

async function viewWhale(id) {
  const w = await api("/whales/" + encodeURIComponent(id));
  const st = w.stats || {};
  const since = w.tracked_since ? new Date(w.tracked_since * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }) : "—";
  const betRow = (b) => `<div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 80px 100px 130px">
      <div><div class="t">${b.url ? `<a href="${safeUrl(b.url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${esc(b.question)}</a>` : esc(b.question)}</div>
        <div class="m"><span>«${esc(b.side || "")}»</span><span>${esc(b.category)}</span><span>${esc(b.when)}</span></div></div>
      <div class="n tnum">${b.entry_cents}¢</div><div class="n tnum">${money(b.stake_usd)}</div>
      <div class="n">${b.pnl_usd === null ? `<span class="chip n">${esc(b.state)}</span>` : `<b class="tnum ${tone(b.pnl_usd)}">${money(b.pnl_usd, { sign: true })}</b><div class="sub">${esc(b.state)}</div>`}</div></div>`;
  main().innerHTML = `
    <a class="back" href="#/whales">← Все киты</a>
    <div class="page-head"><div class="who"><span id="whale-seal">${seal(w.name, { lg: true })}</span><div>
        <h1 id="whale-title">Кит ${esc(w.name)}</h1>
        <div class="row" style="margin-top:10px;gap:8px"><span class="chip ${w.status === "Надёжный" ? "g" : "n"}">${esc(w.status)}</span><span class="chip n">Риск: ${esc(w.risk)}</span>
          <span class="sub">${w.wallets.length > 1 ? `${w.wallets.length} кошелька одного владельца` : "Кошелёк " + esc(w.wallets[0].slice(0, 6) + "…" + w.wallets[0].slice(-4))}, активен с ${esc(since)}</span></div></div></div>
      ${w.can_copy ? `<label class="ck" style="font-weight:600"><input type="checkbox" data-act="copy" data-id="${esc(w.entity_id)}" ${w.copy ? "checked" : ""}>Копировать ставки</label>` : `<span class="sub">${esc(w.note || "Копирование недоступно")}</span>`}</div>
    <section class="statement" aria-label="Результат кита">
      <div><div class="label">Прибыль кита по закрытым ставкам</div><div class="figure ${tone(st.pnl_usd)}">${money(st.pnl_usd, { sign: true })}</div>
        <div class="sub" style="margin-top:10px">${st.roi_pct === null || st.roi_pct === undefined ? "" : signedPct(st.roi_pct) + " к вложенному, "}за 90 дней ${pct(w.return_90d_pct)}</div></div>
      <div class="facts">
        <div class="fact"><div class="label">Выигрышей</div><div class="v">${pctText(st.win_pct)}<small>из ${st.closed ?? 0}</small></div></div>
        <div class="fact"><div class="label">Средняя ставка</div><div class="v">${money(st.avg_bet_usd)}<small>вход ${st.avg_entry_cents ?? "—"}¢</small></div></div>
        <div class="fact"><div class="label">Держит ставку</div><div class="v">${st.hold_days === null || st.hold_days === undefined ? "—" : String(st.hold_days).replace(".", ",") + " дн"}<small>${esc(w.hold_text)}</small></div></div>
        <div class="fact"><div class="label">Открыто сейчас</div><div class="v">${st.open ?? 0}<small>на ${money(st.open_usd)}</small></div></div>
        <div class="fact"><div class="label">Мы повторили</div><div class="v">${w.copied_count}<small class="${tone(w.copied_pnl_usd)}">${money(w.copied_pnl_usd, { sign: true })}</small></div></div>
        <div class="fact"><div class="label">Последняя ставка</div><div class="v" style="font-size:20px">${st.last_trade_ago_s === null || st.last_trade_ago_s === undefined ? "—" : ago(st.last_trade_ago_s)}</div></div>
      </div></section>
    <div id="pm-box"></div>
    <div id="sim-box"></div>
    <details class="more card"><summary class="link">История ставок кита: ${(w.whale_bets || []).length} последних закрытых${w.whale_open && w.whale_open.length ? ` и ${w.whale_open.length} открытых` : ""}</summary>
      <div class="col" style="gap:12px;margin-top:14px">
      ${w.whale_open && w.whale_open.length ? `<h3>Открытые</h3><div class="ledger">${w.whale_open.map(betRow).join("")}</div>` : ""}
      <h3>Закрытые</h3>
      <div class="ledger">${w.whale_bets && w.whale_bets.length ? w.whale_bets.map(betRow).join("") : `<div class="ledger-empty">Закрытые ставки появятся после загрузки истории.</div>`}</div></div></details>
    <div class="grid2">
      <div class="card"><fieldset><legend>Темы</legend>
        ${w.topics.map((t) => `<label class="ck"><input type="checkbox" data-act="topic" data-id="${esc(w.entity_id)}" data-cat="${esc(t.category)}" ${t.enabled ? "checked" : ""} ${t.proven ? "" : "disabled"}>
          <span><b ${t.proven ? "" : 'style="color:var(--muted)"'}>${esc(t.label[0].toUpperCase() + t.label.slice(1))}</b><span class="sub" style="display:block">${esc(t.text)}</span></span></label>`).join("")}
      </fieldset></div>
      <div class="card"><h2 style="margin-bottom:6px">Наши сигналы по киту</h2>
        ${w.last_bets.length ? w.last_bets.map((b) => `<div class="bet"><div><b style="font-weight:500">${esc(b.question)}</b><div class="sub">«${esc(b.side)}» по ${esc(b.price_cents)}¢, ${esc(b.when)}</div></div>
          <span class="chip ${/Повторили/.test(b.our_action) ? "g" : /Ждёт|вышел/.test(b.our_action) ? "a" : "n"}">${esc(b.our_action)}</span></div>`).join("") : `<div class="sub" style="margin-top:8px">Сигналов ещё не было</div>`}
      </div></div>
    ${w.links && w.links.length ? `<div class="card col" style="gap:8px"><h2>Почему это один игрок</h2>
      <div class="sub">Система объединила ${w.wallets.length} кошелька: так один человек не считается несколькими «умными» игроками.</div>
      ${[...new Set(w.links.map((l) => l.why))].map((t) => `<div class="row" style="gap:8px"><span class="chip n">связь</span>${esc(t)}</div>`).join("")}</div>` : ""}
    <button class="link" style="color:var(--bad-ink);background:none;border:0;text-align:left;padding:0;min-height:44px;cursor:pointer;align-self:flex-start" data-act="unfollow" data-id="${esc(w.entity_id)}">Перестать следить за китом</button>`;
  loadSim("sim-box", w.entity_id, 90, true);
  loadPlayerBox("pm-box", w.wallets[0]);
}

function simLabel(k) {
  return { price_moved: "цена успела уйти", topic_off: "тема выключена у вас", day_limit: "дневной лимит", no_upside: "цена почти 100¢" }[k] || k;
}

const isoDay = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
function simCard(r, { days = 90, withPeriods = false, id = "", range = null } = {}) {
  const s = (r && r.summary) || {};
  if (!r || !r.ok) return `<div class="card"><h2>Если бы вы копировали этого кита</h2><div class="sub" style="margin-top:8px">${esc((r && r.error) || "Нет данных")}</div></div>`;
  const tone2 = { good: "g", bad: "r", few: "a", no_data: "n" }[s.verdict] || "n";
  const label = { good: "Было бы в плюсе", bad: "Было бы в минусе", few: "Мало ставок", no_data: "Нет ставок" }[s.verdict];
  const skipped = Object.entries(s.skipped || {}).filter(([, v]) => v);
  const pts = (s.curve || []).map((c) => ({ x: c.ts, y: c.pnl_usd, label: dateLabel(c.ts) }));
  return `<section class="card col" style="gap:16px" aria-label="Если бы вы копировали этого кита">
    <div class="top"><div><h2>Если бы вы копировали этого кита</h2>
        <div class="sub" style="margin-top:4px">${range ? `Ставки с ${new Date(range[0] * 1000).toLocaleDateString("ru-RU")} по ${new Date(range[1] * 1000).toLocaleDateString("ru-RU")}` : `Последние ${s.days} дней`}, задержка ${s.delay_s} с, ставка ${money(s.stake_usd)} по вашим лимитам, с комиссиями</div></div>
      <div class="row">${withPeriods ? `<div class="seg" role="group" aria-label="Период">${[30, 90, 180].map((d) => `<button type="button" aria-pressed="${d === days && !range}" data-act="sim" data-id="${esc(id)}" data-v="${d}">${d} дн</button>`).join("")}
          <button type="button" aria-pressed="${!!range}" data-act="sim-range" aria-expanded="${!!range}">Свои даты</button></div>` : ""}
        <span class="chip ${tone2}">${label}</span></div></div>
    ${withPeriods ? `<form class="sim-range row" data-id="${esc(id)}" ${range ? "" : "hidden"}>
        <label>С <input class="inp" type="date" name="start" value="${range ? isoDay(range[0]) : isoDay(Date.now() / 1000 - 60 * 86400)}" max="${isoDay(Date.now() / 1000)}"></label>
        <label>по <input class="inp" type="date" name="end" value="${range ? isoDay(range[1]) : isoDay(Date.now() / 1000)}" max="${isoDay(Date.now() / 1000)}"></label>
        <button class="btn p">Посчитать</button>
        <span class="sub">Ставки, которые кит открыл в эти даты, и как они закрылись на самом деле</span></form>` : ""}
    ${s.n ? `<div class="row" style="gap:36px;align-items:flex-end">
      <div><div class="label">Итог закрытых ставок</div><div class="big ${tone(s.closed_pnl_usd)}" style="font-size:40px">${money(s.closed_pnl_usd, { sign: true })}</div>
        <div class="sub">${s.roi_pct === null ? "" : signedPct(s.roi_pct) + " к вложенному"}</div></div>
      <div class="fact"><div class="label">Повторили бы</div><div class="v">${s.n}<small>закрыто ${s.closed}</small></div></div>
      <div class="fact"><div class="label">Выигрышей</div><div class="v">${pctText(s.win_pct)}</div></div>
      <div class="fact"><div class="label">Сам кит на тех же ставках</div><div class="v ${tone(s.whale_roi_pct)}">${s.whale_roi_pct === null ? "—" : signedPct(s.whale_roi_pct)}</div></div>
      <div class="fact"><div class="label">Наибольшая просадка</div><div class="v">${money(-s.max_drawdown_usd)}</div></div>
      <div class="fact"><div class="label">Комиссии</div><div class="v">${money(s.fees_usd, { cents: true })}</div></div></div>
      ${pts.length > 1 ? lineChart(pts, { height: 150, label: "Прибыль копирования по неделям", fmtY: (v) => money(v) }) : ""}` : ""}
    <div style="font-size:14px">${esc(s.text)}</div>
    ${skipped.length ? `<div class="sub">Не повторили бы: ${skipped.map(([k, v]) => `${simLabel(k)} — ${v}`).join("; ")}.</div>` : ""}
    ${s.n ? `<div class="sub">Цена входа оценена по почасовой истории цен: не дешевле цены кита плюс 1¢ на проскальзывание. Это оценка, а не точный расчёт стакана.${s.estimated_share ? ` Для ${Math.round(s.estimated_share * 100)}% ставок истории цен нет — взята цена кита плюс 1¢.` : ""}</div>` : ""}
    ${(r.bets || []).length ? `<details><summary class="link" style="cursor:pointer;display:inline-block">Ставки в расчёте</summary>
      <div class="ledger" style="margin-top:10px">${r.bets.slice(0, 12).map((b) => `<div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 90px 90px 110px">
        <div><div class="t">${esc(b.question)}</div><div class="m"><span>«${esc(b.side || "")}»</span><span>${esc({ resolved: "рынок закрылся", sold: "кит вышел — продали следом", open: "ещё открыта" }[b.status])}</span></div></div>
        <div class="n"><div class="tnum">${cents(b.whale_cents)}</div><div class="sub">кит</div></div>
        <div class="n"><div class="tnum">${cents(b.entry_cents)}</div><div class="sub">мы</div></div>
        <div class="n"><b class="tnum ${tone(b.pnl_usd)}">${money(b.pnl_usd, { sign: true })}</b></div></div>`).join("")}</div></details>` : ""}
  </section>`;
}

async function loadSim(boxId, id, days = 90, withPeriods = false, range = null) {
  const box = $("#" + boxId);
  if (!box) return;
  box.innerHTML = `<div class="card skeleton">Считаем, что было бы при копировании…</div>`;
  try {
    const q = range ? `start=${range[0]}&end=${range[1]}` : `days=${days}`;
    const r = await api(`/whales/${encodeURIComponent(id)}/simulate?${q}`);
    if (document.body.contains(box)) box.innerHTML = simCard(r, { days, withPeriods, id, range });
  } catch (e) { box.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}


// «Добавить игрока» и рейтинг — в panels.js

// ---------------------------------------------------------------- «Диагностика»
async function viewDiagnostics(live = false) {
  if (live) {
    const b = $("[data-act=diag-run]");
    if (b) { b.disabled = true; b.textContent = "Проверяем…"; }
  }
  const d = await api("/diagnostics" + (live ? "?live=1" : ""));
  const p = d.probe;
  const groups = { polymarket: "Polymarket", stream: "Потоки реального времени", kalshi: "Другие рынки", news: "Новости и прогнозы", telegram: "Уведомления", chain: "Блокчейн", odds: "Букмекеры" };
  const speedTone = { "мгновенно": "g", "быстро": "g", "нормально": "n", "медленно": "a" };
  const checks = p ? Object.entries(groups).map(([g, title]) => {
    const items = p.checks.filter((c) => c.group === g);
    if (!items.length) return "";
    return `<div class="ledger-head" style="grid-template-columns:1fr">${title}</div>` + items.map((c) => `
      <div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 150px 96px">
        <div><div class="t">${esc(c.label)}</div><div class="m"><span>${esc(c.detail)}</span></div>${c.hint ? `<div class="sub" style="margin-top:4px;color:var(--warn-ink)">${esc(c.hint)}</div>` : ""}</div>
        <div class="n"><b class="tnum">${c.ms} мс</b>${c.warm_ms !== null && c.warm_ms !== undefined && c.cold_ms ? `<div class="sub" title="Первый запрос с установкой защищённого соединения. Приложение держит соединения открытыми, поэтому рабочие запросы идут быстрее.">с нуля ${c.cold_ms} мс</div>` : ""}</div>
        <div class="n"><span class="chip ${c.ok ? (speedTone[c.speed] || "g") : "r"}">${c.ok ? esc(c.speed || "работает") : "ошибка"}</span></div></div>`).join("");
  }).join("") : `<div class="ledger-empty">Проверка ещё не запускалась — нажмите «Проверить сейчас».</div>`;
  const okN = p ? p.checks.filter((c) => c.ok).length : 0;
  const u = d.usage, cnt = d.counts;
  const st = d.streams;
  const streamLine = (x, name) => {
    const state = !x.started ? "не запущен" : x.live ? "работает" : "нет связи";
    return `<div class="bet"><span class="row" style="gap:8px"><span class="dot ${!x.started ? "" : x.live ? "ok" : "bad"}" aria-hidden="true"></span>${name}</span><span class="sub">${state}${x.connect_ms && x.live ? `, подключение ${x.connect_ms} мс` : ""}</span></div>`;
  };
  const streamsCard = st ? `<div class="card col" style="gap:6px"><h2>Реальное время</h2>
      ${streamLine(st.trades, "Поток сделок китов")}
      <div class="bet"><span>Китов в потоке</span><b class="tnum">${fmtNum(st.trades.wallets)}</b></div>
      <div class="bet"><span>Сделок китов пришло потоком</span><b class="tnum">${fmtNum(st.trades.matched)}</b></div>
      ${st.trades.lag_s !== null && st.trades.lag_s !== undefined ? `<div class="bet"><span>Задержка последней сделки</span><b class="tnum">${String(Math.max(0, st.trades.lag_s)).replace(".", ",")} с</b></div>` : ""}
      ${streamLine(st.books, "Поток стаканов")}
      <div class="bet"><span>Рынков в потоке</span><b class="tnum">${fmtNum(st.books.subscribed)}</b></div>
      <div class="bet"><span>Стаканов для решений: из потока / запросом</span><b class="tnum">${fmtNum(st.agent_books.from_stream)} / ${fmtNum(st.agent_books.by_request)}</b></div>
      ${st.agent_books.request_median_ms ? `<div class="bet"><span>Запрос стакана (медиана)</span><b class="tnum">${st.agent_books.request_median_ms} мс</b></div>` : ""}
      ${(!st.trades.live && st.trades.error) || (!st.books.live && st.books.error) ? `<div class="sub" style="color:var(--warn-ink)">Поток недоступен — агент работает опросом. ${esc((st.trades.error || st.books.error || "").slice(0, 120))}</div>` : ""}
    </div>` : "";
  main().innerHTML = `
    <a class="back" href="#/settings">← Настройки</a>
    <div class="page-head"><div><h1>Диагностика</h1>
      <div class="lede">Проверка доступа ко всем источникам данных прямо из интерфейса. Если что-то не работает, скопируйте отчёт и пришлите его — в нём нет ключей и адресов кошельков.</div></div>
      <div class="row"><button class="btn" data-act="diag-copy">Скопировать отчёт</button><button class="btn p" data-act="diag-run">Проверить сейчас</button></div></div>
    <section class="statement" aria-label="Сводка">
      <div><div class="label">Источники</div><div class="figure ${p ? (p.ok ? "up" : p.checks.some((c) => !c.ok && c.group === "polymarket") ? "down" : "warn-t") : ""}">${p ? `${okN} из ${p.checks.length}` : "—"}</div>
        <div class="sub" style="margin-top:10px">${p ? `работают, проверка ${ago(Math.max(0, Math.round(Date.now() / 1000 - p.ts)))}` : "ещё не проверялись"}</div></div>
      <div class="facts">
        <div class="fact"><div class="label">Отклик Polymarket</div><div class="v" style="font-size:20px">${p && p.polymarket_median_ms !== null && p.polymarket_median_ms !== undefined ? `${p.polymarket_median_ms} мс` : "—"}</div></div>
        <div class="fact"><div class="label">Агент</div><div class="v" style="font-size:20px">${d.agent_ok ? "работает" : "не отвечает"}</div></div>
        <div class="fact"><div class="label">Свежесть данных</div><div class="v" style="font-size:20px">${d.data_age_s === null || d.data_age_s === undefined ? "нет данных" : ago(d.data_age_s)}</div></div>
        <div class="fact"><div class="label">Версия</div><div class="v" style="font-size:20px">${esc(d.version)}</div></div>
      </div></section>
    <div class="ledger">${checks}</div>
    ${streamsCard ? `<div class="grid2">${streamsCard}<div class="card col" style="gap:8px"><h2>Как устроена скорость</h2>
      <div class="sub">Сделки китов и стаканы приходят по постоянному соединению (WebSocket) сами — без ожидания следующего опроса. Экраны приложения читают локальную базу и открываются за миллисекунды.</div>
      <div class="sub">Время запроса к Polymarket — это в основном дорога до его серверов и обратно; её сокращают открытые соединения (без новой установки связи) и потоки, но не код приложения.</div></div></div>` : ""}
    <div class="grid2">
      <div class="card col" style="gap:10px"><h2>Данные</h2>
        <div class="bet"><span>Кошельков китов в списке</span><b class="tnum">${fmtNum(u.tracked_wallets)}</b></div>
        <div class="bet"><span>Сделок китов</span><b class="tnum">${fmtNum(u.trades_tracked)}</b></div>
        <div class="bet"><span>Рынков</span><b class="tnum">${fmtNum(cnt.markets)}</b></div>
        <div class="bet"><span>Точек истории цен</span><b class="tnum">${fmtNum(cnt.prices)}</b></div>
        <div class="bet"><span>Сигналов / наших сделок</span><b class="tnum">${fmtNum(cnt.signals)} / ${fmtNum(cnt.bets)}</b></div>
        ${u.db_size_mb ? `<div class="bet"><span>Размер базы</span><b class="tnum">${String(u.db_size_mb).replace(".", ",")} МБ</b></div>` : ""}</div>
      <div class="card col" style="gap:6px"><h2>Задачи сбора данных</h2>
        ${d.jobs.length ? d.jobs.map((j) => `<div class="bet"><span class="row" style="gap:8px"><span class="dot ${j.ok === false ? "bad" : j.ok ? "ok" : ""}" aria-hidden="true"></span>${esc(j.label)}</span>
          <span class="sub">${j.ok === false ? `<span class="down">${esc((j.error || "").slice(0, 80))}</span>` : j.ago_s === null ? "ещё не запускалась" : ago(j.ago_s)}</span></div>`).join("")
          : `<div class="sub">Планировщик не запущен.</div>`}</div></div>
    <details><summary class="link" style="cursor:pointer;display:inline-block">Текст отчёта</summary>
      <pre id="diag-report" class="card" style="white-space:pre-wrap;font:13px/1.5 ui-monospace,Menlo,monospace;margin-top:10px">${esc(d.report)}</pre></details>`;
  if (live) toast(p && p.ok ? "Все источники работают" : "Есть проблемы — см. список");
}

function fmtNum(n) { return Number(n || 0).toLocaleString("ru-RU"); }

function checkProgress(r) {
  const pct = r.total ? Math.round((r.done / r.total) * 100) : null;
  const count = r.stage === "trades" ? (r.done ? `загружено ${fmtNum(r.done)} ставок` : "")
    : r.total ? `${fmtNum(r.done)} из ${fmtNum(r.total)}` : "";
  return `<div class="card col" style="gap:12px" aria-live="polite">
    <div class="top"><b>Проверяем игрока</b><span class="sub">${Math.round(r.elapsed_s || 0)} с</span></div>
    ${r.stages.map((st) => `<div class="row" style="gap:10px">
      <span class="chip ${st.status === "done" ? "g" : "n"}" style="min-width:64px;text-align:center">${st.status === "done" ? "готово" : st.status === "active" ? "идёт" : "ждёт"}</span>
      <span style="${st.status === "wait" ? "color:var(--muted)" : ""}">${esc(st.text)}${st.status === "active" && count ? ` · <span class="sub">${count}</span>` : ""}</span></div>`).join("")}
    ${pct !== null ? `<div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>` : ""}
    <div class="sub">Можно уйти с этой страницы — проверка продолжится, а когда закончится, придёт уведомление.</div></div>`;
}

function timingText(t) {
  if (!t) return "";
  const names = { profile: "статистика", trades: "ставки", activity: "операции", markets: "рынки", prices: "цены", score: "расчёт" };
  const parts = Object.keys(names).filter((k) => t[k] >= 1).map((k) => `${names[k]} ${Math.round(t[k])} с`);
  return parts.length ? ` (${parts.join(" · ")})` : "";
}

function checkResult(r) {
  const st = r.stats || {};
  return `<h2>Результат проверки</h2>
    <div class="card col" style="border-color:${r.fits ? "var(--accent)" : "var(--line)"};gap:12px">
      <div class="row"><b style="font-size:20px">Игрок ${esc(r.name)}</b><span class="chip ${r.fits ? "g" : "n"}">${r.fits ? "Подходит" : "Не подходит"}</span>
        ${r.risk ? `<span class="sub">Риск: ${esc(r.risk)}${r.strong_topics && r.strong_topics.length ? " · сильные темы: " + esc(r.strong_topics.join(", ")) : ""}</span>` : ""}</div>
      ${st.trades ? `<div class="sub">Проверено ${fmtNum(st.trades)} ${plural(st.trades, "ставка", "ставки", "ставок")} в ${fmtNum(st.markets)} ${plural(st.markets, "рынке", "рынках", "рынках")}${st.seconds != null ? ` за ${Math.round(st.seconds)} с` : ""}${timingText(r.timings)}</div>` : ""}
      ${r.checks.map((c) => `<div class="check-row"><span class="chip ${c.ok ? "g" : "r"}">${c.ok ? "Да" : "Нет"}</span><div><b>${esc(c.title)}</b><div class="sub">${esc(c.text)}</div></div></div>`).join("")}
      <div class="row">${r.fits ? `<button class="btn p" data-act="add" data-id="${esc(r.entity_id)}" data-v="copy">Добавить и копировать</button>` : ""}
        <button class="btn" data-act="add" data-id="${esc(r.entity_id)}" data-v="watch">Только следить</button>
        <a class="link" href="#/whales">Отмена</a></div>
      ${r.hint ? `<div class="sub">${esc(r.hint)}</div>` : ""}</div>
    <div id="check-sim" style="margin-top:16px"></div>`;
}

async function runCheck(addr) {
  const box = $("#check-result");
  const route = location.hash;
  box.innerHTML = `<div class="card skeleton">Начинаем проверку…</div>`;
  try {
    let r = await api("/whales/check", { method: "POST", body: { address: addr } });
    while (r.ok && r.state === "running") {
      if (location.hash !== route || !document.body.contains(box)) return;   // ушли со страницы — проверка идёт в фоне
      box.innerHTML = checkProgress(r);
      await new Promise((ok) => setTimeout(ok, 800));
      r = await api("/whales/check/" + encodeURIComponent(r.job_id));
    }
    if (!document.body.contains(box)) return;
    if (!r.ok) { box.innerHTML = `<div class="card err" role="alert">${esc(r.error)}</div>`; return; }
    box.innerHTML = checkResult(r);
    if (r.entity_id) loadSim("check-sim", r.entity_id, 90, false);
  } catch (e) {
    box.innerHTML = `<div class="card err" role="alert">${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------- «Итоги»
async function viewResults(period = "month") {
  const r = await api("/results?period=" + period);
  const pts = r.series.map((p) => ({ y: p.balance_usd, label: dateLabel(p.ts) }));
  const periodName = { week: "За неделю", month: "За месяц", all: "За всё время" }[period];
  main().innerHTML = `
    <div class="top"><h1>Итоги</h1><div class="row">
      <div class="seg" role="group" aria-label="Период">
        ${["week", "month", "all"].map((p) => `<button aria-pressed="${p === period}" data-go="#/results/${p}">${{ week: "Неделя", month: "Месяц", all: "Всё время" }[p]}</button>`).join("")}</div>
      <button class="btn" data-act="csv">Скачать отчёт</button></div></div>
    <div class="card">
      <div class="row" style="gap:40px;align-items:flex-end">
        <div><div class="sub">${periodName}</div><div class="big ${tone(r.pnl_usd)}" style="font-size:32px">${money(r.pnl_usd, { sign: true })}
          <span style="font-size:16px;font-weight:600">(${signedPct(r.pnl_pct)})</span></div></div>
        <div><div class="sub">Баланс сейчас</div><div class="big" style="font-size:24px">${money(r.balance_usd)}</div></div>
        ${r.mode === "paper" ? `<a class="link" style="margin-left:auto" href="#/trial">Как идёт пробный режим →</a>` : ""}</div>
      ${lineChart(pts, { height: 170, label: "Баланс за период", fmtY: (v) => money(v) })}</div>
    <div class="grid3">
      <div class="card"><div class="sub">Ставок сделано</div><div class="big">${r.bets.made}</div>
        <div style="font-size:14px;margin-top:4px">${r.bets.won} выиграли · ${r.bets.lost} проиграли · ${r.bets.waiting} ждут исхода</div></div>
      <div class="card"><div class="sub">Сигналов пропущено</div><div class="big">${r.skipped_signals}</div>
        <div style="font-size:14px;margin-top:4px">Это нормально: система берёт только выгодные ставки</div></div>
      <div class="card"><div class="sub">Точность прогнозов</div><div style="margin-top:6px"><span class="chip ${r.accuracy.label === "В норме" ? "g" : r.accuracy.label === "Хуже рынка" ? "r" : "n"}">${esc(r.accuracy.label)}</span></div>
        <div style="font-size:14px;margin-top:8px">${esc(r.accuracy.text)}</div></div></div>
    <div class="card col" style="gap:10px"><h2 style="margin:0">Откуда прибыль</h2>
      ${r.by_whale.length ? r.by_whale.map((w) => `<div class="top"><span><b>Кит ${esc(w.whale)}</b> · ${w.bets} ${plural(w.bets, "ставка", "ставки", "ставок")}</span>
        <b class="${tone(w.pnl_usd)}">${money(w.pnl_usd, { sign: true })}</b></div>`).join("") : `<div class="sub">Ставок за период нет</div>`}</div>`;
}

async function downloadCsv() {
  const j = await api("/journal?limit=10000");
  const cols = ["signal_id", "ts", "whale", "question", "kind", "status", "decision", "size_usd", "q_whale", "q_llm", "q_final", "edge_net", "reason", "params_version", "user_action"];
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = [cols.join(","), ...j.items.map((r) => cols.map((c) => q(c === "ts" ? new Date(r.ts * 1000).toISOString() : r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
  a.download = `whalefollow-report-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------------------------------------------------------------- «Пробный режим»
async function viewTrial() {
  const t = await api("/trial");
  const v = t.verdict;
  const vClass = { ready: "info", running: "gray", problem: "bad" }[v.status];
  const chip = (ok) => ok === true ? `<span class="chip g">Выполнено</span>` : ok === false ? `<span class="chip r">Не выполнено</span>` : `<span class="chip n">Идёт</span>`;
  const s = t.signals;
  const started = new Date(t.started_ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
  const jobDot = (j) => (j.ok === false ? "bad" : j.ok ? "ok" : "");
  main().innerHTML = `
    <a class="back" href="#/results">← Итоги</a>
    <div><h1>Пробный режим</h1><div class="sub" style="margin-top:6px">День ${Math.floor(t.days) + 1} · начат ${esc(started)} · все ставки на виртуальные деньги</div></div>
    <div class="banner ${vClass}"><div class="grow"><b>${esc(v.label)}</b><div>${esc(v.text)}</div></div></div>
    <h2>Условия перехода к следующему этапу</h2>
    <div class="grid2">${t.criteria.map((c) => `<div class="card col" style="gap:8px">
      <div class="top"><b>${esc(c.title)}</b>${chip(c.ok)}</div>
      <div class="sub" style="font-size:14px">${esc(c.value)}</div>
      ${c.progress !== null && c.progress !== undefined ? `<div class="bar" role="progressbar" aria-label="${esc(c.title)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(c.progress * 100)}"><span style="width:${Math.round(c.progress * 100)}%"></span></div>` : ""}
    </div>`).join("")}</div>
    <h2>Сигналы</h2>
    <div class="grid3">
      <div class="card"><div class="sub">Всего сигналов</div><div class="big">${s.total}</div>
        <div style="font-size:14px;margin-top:4px">Рекомендаций «купить»: ${s.buy_recommendations}, с известным исходом: ${s.with_outcome}</div></div>
      <div class="card"><div class="sub">Куплено</div><div class="big">${s.executed}</div>
        <div style="font-size:14px;margin-top:4px">Ждут решения: ${s.pending}</div></div>
      <div class="card"><div class="sub">Пропущено</div><div class="big">${s.skipped_by_system + s.skipped_by_you + s.expired}</div>
        <div style="font-size:14px;margin-top:4px">Системой: ${s.skipped_by_system} · вами: ${s.skipped_by_you} · время вышло: ${s.expired}</div></div></div>
    ${s.expired >= 5 && s.expired > s.executed ? `<div class="banner warn"><div class="grow"><b>Много сигналов сгорело по времени</b>
      <div>Подключите уведомления в Telegram или отключите ручное подтверждение в настройках — в пробном режиме это безопасно.</div></div>
      <a class="btn" href="#/settings">Настройки</a></div>` : ""}
    <div class="card col" style="gap:10px"><div class="top"><h2 style="margin:0">Точность прогнозов</h2>
      <span class="sub">${t.calibration.better_than_market === null ? "сравнение с рынком появится после первых исходов"
        : t.calibration.better_than_market ? "прогнозы системы точнее рыночной цены" : "прогнозы системы пока не точнее рыночной цены"}</span></div>
      <div class="sub">Когда система говорит «70%», такие события должны сбываться примерно в 70% случаев. Оценка — в корзинах, где не меньше 30 исходов.</div>
      ${t.calibration.buckets.length ? `<div class="table-head" style="grid-template-columns:1fr 90px 100px 100px 110px 120px;padding:0"><span>Прогноз системы</span><span>Сигналов</span><span>В среднем</span><span>Сбылось</span><span>Отклонение</span><span></span></div>
        ${t.calibration.buckets.map((b) => `<div class="trow" style="grid-template-columns:1fr 90px 100px 100px 110px 120px;padding:6px 0;border-top:1px solid var(--line)">
          <span>${esc(b.range)}</span><span>${b.n}</span><span>${b.predicted_pct}%</span><span>${b.actual_pct}%</span>
          <span>${b.deviation_pp > 0 ? "+" : ""}${b.deviation_pp} п.п.</span><span>${b.ok === null ? '<span class="chip n">Мало данных</span>' : b.ok ? '<span class="chip g">В допуске</span>' : '<span class="chip r">Вне допуска</span>'}</span></div>`).join("")}`
        : `<div class="sub">Исходов пока нет — рынки ещё не закрылись.</div>`}
      ${t.hypothetical_roi_pct !== null ? `<div style="font-size:14px">Если бы вы подтверждали все рекомендации, доходность была бы <b class="${tone(t.hypothetical_roi_pct)}">${signedPct(t.hypothetical_roi_pct)}</b> на ставку.</div>` : ""}
    </div>
    ${tuningCard(t.tuning)}
    <div class="card col" style="gap:8px"><div class="top"><h2 style="margin:0">Скорость сигналов</h2>
      <span class="sub">от сделки кита до сигнала, за 7 дней</span></div>
      ${t.latency && t.latency.n ? `<div class="row" style="gap:28px"><div><div class="sub">Обычно</div><div class="big">${Math.round(t.latency.median_s)} с</div></div>
        <div><div class="sub">9 из 10 сигналов быстрее</div><div class="big">${Math.round(t.latency.p90_s)} с</div></div>
        <div><div class="sub">Сигналов</div><div class="big">${t.latency.n}</div></div></div>
        <div class="sub">Чем быстрее сигнал, тем ближе наша цена к цене кита.</div>`
        : `<div class="sub">Появится после первых сигналов.</div>`}</div>
    <div class="card col" style="gap:8px"><div class="top"><h2 style="margin:0">ИИ</h2>
      <span class="chip ${t.ai.mode.effective === "ai" ? "i" : "n"}">${t.ai.mode.effective === "ai" ? "Работает" : t.ai.mode.available ? "Выключен вами" : "Не подключён"}</span></div>
      <div style="font-size:14px">Оценок сделано: ${t.ai.estimates} · с известным исходом: ${t.ai.with_outcome} из ${t.ai.needed} нужных для проверки</div>
      <div class="bar" role="progressbar" aria-label="Исходы для проверки ИИ" aria-valuemin="0" aria-valuemax="${t.ai.needed}" aria-valuenow="${t.ai.with_outcome}"><span style="width:${Math.min(100, t.ai.with_outcome / t.ai.needed * 100)}%"></span></div>
      <div class="sub">${t.ai.better_than_market === null ? "Сравнение с рынком появится после первых исходов."
        : t.ai.better_than_market ? "Оценки ИИ пока точнее рыночной цены." : "Оценки ИИ пока не точнее рыночной цены — он работает только как фильтр."}
        ${t.ai.trusted ? " ИИ прошёл проверку и получает полный вес в решении." : ""}</div></div>
    <div class="card"><div class="top"><h2 style="margin:0">Баланс</h2><span class="sub">Наибольшая просадка: ${t.max_drawdown_pct}%</span></div>
      ${lineChart(t.series.map((p) => ({ y: p.balance_usd, label: dateLabel(p.ts) })), { height: 150, label: "Баланс в пробном режиме", fmtY: (x) => money(x) })}</div>
    <div class="card col" style="gap:8px"><h2 style="margin:0">Сбор данных</h2>
      ${t.jobs.length ? t.jobs.map((j) => `<div class="top" style="font-size:14px"><span class="row" style="gap:8px"><span class="dot ${jobDot(j)}" aria-hidden="true"></span>${esc(j.label)}</span>
        <span class="sub">${j.ok === false ? `<span class="down">ошибка: ${esc(j.error)}</span>` : j.ago_s === null ? "ещё не запускалось" : "был запуск " + ago(j.ago_s)}</span></div>`).join("")
        : `<div class="sub">Планировщик не запущен. Для пробного режима запустите <code>whalefollow run</code> или <code>docker compose --profile live up -d</code>.</div>`}
    </div>`;
}

function tuningCard(t) {
  if (!t) return "";
  const rows = t.grid.filter((g) => g.n > 0);
  return `<div class="card col" style="gap:10px"><div class="top"><h2 style="margin:0">Подстройка порога выгоды</h2>
      <span class="sub">сейчас ${Math.round(t.current * 100)}%</span></div>
    <div class="sub">Что было бы при другом пороге: по всем сигналам с известным исходом, включая пропущенные. Учитывается комиссия.</div>
    ${rows.length ? `<div class="table-head" style="grid-template-columns:1fr 90px 110px 140px;padding:0"><span>Порог выгоды</span><span>Сигналов</span><span>Выигрышей</span><span>Доход на ставку</span></div>
      ${rows.map((g) => `<div class="trow" style="grid-template-columns:1fr 90px 110px 140px;padding:6px 0;border-top:1px solid var(--line)">
        <span>${Math.round(g.threshold * 100)}%${g.current ? ' <span class="chip n">сейчас</span>' : ""}${t.recommended === g.threshold && !g.current ? ' <span class="chip g">лучше</span>' : ""}</span>
        <span>${g.n}</span><span>${g.win_pct ?? "—"}%</span><b class="${tone(g.avg_return_pct)}">${signedPct(g.avg_return_pct)}</b></div>`).join("")}` : ""}
    <div class="row"><span style="font-size:14px">${esc(t.text)}</span>
      ${t.recommended !== null && Math.abs(t.recommended - t.current) > 1e-9 ? `<button class="btn" data-act="tune" data-v="${t.recommended}">Применить ${Math.round(t.recommended * 100)}%</button>` : ""}</div></div>`;
}

// ---------------------------------------------------------------- «Настройки»
const CATS = [...TOPICS, ["other", "Разное (тема не указана)"]];
async function viewSettings() {
  const s = await api("/settings");
  const n = s.notify || {};
  const num = (id, label, val, suffix = "$") => `<div class="fld"><label for="${id}">${label}, ${suffix}</label>
    <input id="${id}" class="inp" type="number" min="0" step="1" value="${suffix === "%" ? Math.round(val * 100) : val}"></div>`;
  main().innerHTML = `
    <form id="settings-form" class="col">
    <div class="top"><h1>Настройки</h1><div class="row"><button type="button" class="btn" data-go="#/settings">Отменить</button><button class="btn p">Сохранить</button></div></div>
    <div class="grid2">
      <div class="col">
        <div class="card"><fieldset><legend>Режим работы</legend>
          <label class="opt"><input type="radio" name="mode" value="paper" ${s.mode === "paper" ? "checked" : ""}><span><b>Пробный</b><span class="sub" style="display:block">Ставки на виртуальные деньги — безопасно проверить систему</span></span></label>
          <label class="opt disabled"><input type="radio" name="mode" value="manual" disabled><span><b style="color:var(--muted)">Ручной</b><span class="sub" style="display:block">Реальные ставки с подтверждением. Отключено в этой версии: сначала пробный режим и юридическая проверка</span></span></label>
          <label class="opt disabled"><input type="radio" name="mode" value="auto" disabled><span><b style="color:var(--muted)">Автоматический</b><span class="sub" style="display:block">Откроется после 2 месяцев пробного режима с хорошим результатом${s.auto_days_left ? ` · осталось ${days(s.auto_days_left)}` : ""}</span></span></label>
          <label class="ck"><input type="checkbox" name="require_confirmation" ${s.require_confirmation ? "checked" : ""}>Подтверждать каждую ставку вручную</label>
        </fieldset></div>
        <div class="card"><fieldset><legend>Кто принимает решение</legend>
          <label class="opt ${s.ai.available ? "" : "disabled"}"><input type="radio" name="ai_mode" value="ai" ${s.ai_mode === "ai" ? "checked" : ""} ${s.ai.available ? "" : "disabled"}>
            <span><b ${s.ai.available ? "" : 'style="color:var(--muted)"'}>С ИИ</b><span class="sub" style="display:block">${s.ai.available
              ? "Claude оценивает вероятность по новостям вслепую — без цены рынка и ставки кита"
              : "ИИ не подключён: добавьте ANTHROPIC_API_KEY в файл .env и перезапустите сервис"}</span></span></label>
          <label class="opt"><input type="radio" name="ai_mode" value="manual" ${s.ai_mode === "manual" || !s.ai.available ? "checked" : ""}>
            <span><b>Без ИИ</b><span class="sub" style="display:block">Данные собираются и анализируются так же, но каждую ставку решаете вы — система показывает историю кита, цены, другие рынки и новости</span></span></label>
        </fieldset></div>
        <div class="card"><fieldset><legend>Деньги и лимиты</legend>
          ${num("bankroll_usd", "Сумма для торговли", s.bankroll_usd)}
          ${num("max_bet_usd", "Максимум на одну ставку", s.max_bet_usd)}
          ${num("daily_limit_usd", "Лимит в день", s.daily_limit_usd)}
          ${num("daily_loss_stop_frac", "Пауза при убытке за день", s.daily_loss_stop_frac, "%")}
          ${num("monthly_loss_stop_frac", "Пауза при убытке за месяц", s.monthly_loss_stop_frac, "%")}
          <div class="sub">Система никогда не превысит эти суммы, даже если сигнал очень выгодный.</div></fieldset></div>
        <div class="card"><fieldset><legend>Сбор данных</legend>
          <label class="opt"><input type="checkbox" name="auto_discover" ${s.auto_discover ? "checked" : ""}>
            <span><b>Искать новых китов автоматически</b><span class="sub" style="display:block">Выключено — данные собираются только по китам из вашего списка. Включено — система просматривает всю ленту Polymarket и сама предлагает кандидатов: больше трафика и места на диске.</span></span></label>
          <div id="data-usage" class="sub">Считаем объём данных…</div></fieldset></div>
      </div>
      <div class="col">
        <div class="card"><fieldset><legend>Темы</legend>
          <div class="grid2" style="gap:0 12px">${CATS.map(([k, l]) => `<label class="ck"><input type="checkbox" name="cat" value="${k}" ${(s.categories || []).includes(k) ? "checked" : ""}>${l}</label>`).join("")}</div>
          <div class="sub">Сигналы по выключенным темам не приходят.</div></fieldset></div>
        <div class="card"><fieldset><legend>Источники данных</legend>
          <label class="ck"><input type="checkbox" name="src_google_news" ${(s.news_sources || {}).google_news !== false ? "checked" : ""}><span>Новости (Google News)<span class="sub" style="display:block">Поиск свежих статей по вопросу рынка</span></span></label>
          <label class="ck"><input type="checkbox" name="src_manifold" ${(s.news_sources || {}).manifold !== false ? "checked" : ""}><span>Другие рынки прогнозов (Manifold)<span class="sub" style="display:block">Вероятности по похожим вопросам</span></span></label>
          <label class="ck"><input type="checkbox" name="src_kalshi" ${(s.news_sources || {}).kalshi !== false ? "checked" : ""}><span>Kalshi<span class="sub" style="display:block">Цены по похожим вопросам на второй крупной бирже прогнозов</span></span></label>
          <label class="ck"><input type="checkbox" name="src_bookmakers" ${(s.news_sources || {}).bookmakers !== false ? "checked" : ""} ${s.integrations && s.integrations.odds_api ? "" : "disabled"}><span>Букмекеры (спорт)<span class="sub" style="display:block">${s.integrations && s.integrations.odds_api ? "Вероятности по коэффициентам без маржи букмекера" : "Нужен ключ WF_ODDS_API_KEY в .env (the-odds-api.com)"}</span></span></label>
          <label class="ck"><input type="checkbox" name="src_reddit" ${(s.news_sources || {}).reddit ? "checked" : ""}><span>Соцсети (Reddit)<span class="sub" style="display:block">Обсуждения за неделю — как повод проверить, не как доказательство</span></span></label>
          <label for="tg-channels" style="font-weight:600;margin-top:6px">Telegram-каналы</label>
          <textarea id="tg-channels" name="telegram_channels" class="inp" rows="3" style="height:auto;padding:10px 12px;font-weight:500;resize:vertical"
            placeholder="@channel1, https://t.me/channel2">${esc((s.telegram_channels || []).map((c) => "@" + c).join(", "))}</textarea>
          <div class="sub">Только публичные каналы. Сообщения помечаются как непроверенные и служат поводом проверить новость.</div>
          <div class="sub" style="margin-top:4px"><b>Блокчейн (поиск связанных кошельков):</b> ${s.chain.available
            ? `подключён (${esc(s.chain.provider)}) · проверено кошельков: ${s.chain.wallets_checked}, переводов: ${s.chain.transfers}${s.chain.errors ? `, ошибок: ${s.chain.errors}` : ""}`
            : "не подключён — добавьте WF_ETHERSCAN_API_KEY в .env (см. README)"}</div></fieldset></div>
        <div class="card"><div style="font-size:16px;font-weight:700;color:var(--ink-2);margin-bottom:10px">Уведомления</div>
          <div class="grid2" style="gap:0 12px">
            <fieldset style="gap:0"><legend class="sub" style="font-size:13px;margin-bottom:2px">Куда</legend>
              ${[["telegram", "Telegram"], ["push", "Push"], ["email", "Email"]].map(([k, l]) => `<label class="ck"><input type="checkbox" name="n_${k}" ${n[k] ? "checked" : ""}>${l}</label>`).join("")}</fieldset>
            <fieldset style="gap:0"><legend class="sub" style="font-size:13px;margin-bottom:2px">О чём</legend>
              ${[["signals", "Новые сигналы"], ["whale_exit", "Кит вышел из позиции"], ["limits", "Лимиты и паузы"], ["weekly", "Итоги недели"], ["system", "Сбои сбора данных"]].map(([k, l]) => `<label class="ck"><input type="checkbox" name="n_${k}" ${n[k] ? "checked" : ""}>${l}</label>`).join("")}</fieldset>
          </div><div class="sub">${s.integrations && s.integrations.telegram ? "Telegram подключён: под сигналами — кнопки «Купить» и «Пропустить», команды /status, /stop, /resume." : "Telegram работает, если в файле .env указан бот (см. README)."} Push и Email появятся позже; все уведомления видны в колокольчике.</div></div>
        <div class="card col" style="gap:6px"><div style="font-size:16px;font-weight:700;color:var(--ink-2)">Защита и сохранность</div>
          ${securityRows(s.integrations || {})}</div>
        <a class="card item" style="text-decoration:none;color:inherit" href="#/diagnostics"><div class="grow"><b>Диагностика</b>
          <div class="sub">Проверить доступ к Polymarket и другим источникам, посмотреть ошибки и скопировать отчёт.</div></div><span class="btn">Открыть</span></a>
        <div class="card item"><div class="grow"><b>Кошелёк Polymarket</b><div class="sub">Не подключён. Нужен только для реальных ставок — в этой версии они отключены.</div></div>
          <button type="button" class="btn" disabled>Подключить</button></div>
      </div></div></form>`;
  loadDataUsage();
}

function securityRows(i) {
  const row = (ok, title, text) => `<div class="check-row"><span class="chip ${ok ? "g" : "a"}">${ok ? "Да" : "Нет"}</span><div><b>${title}</b><div class="sub">${text}</div></div></div>`;
  return row(i.password, "Пароль на интерфейс", i.password ? "Вход по паролю (WF_UI_PASSWORD)." : "Не задан. Пока интерфейс открыт только на этом компьютере, это допустимо; для доступа с телефона задайте WF_UI_PASSWORD в .env.")
    + row(i.backup_ok, "Резервные копии базы", i.backup_ok ? `Последняя ${ago(i.backup_ago_s)}; хранятся 7 последних.` : (i.backup_ago_s === null || i.backup_ago_s === undefined ? "Первая копия появится в течение суток после запуска." : "Последняя копия не удалась — см. «Пробный режим» → «Сбор данных»."));
}

async function loadDataUsage() {
  const box = $("#data-usage");
  if (!box) return;
  try {
    const u = await api("/data");
    box.innerHTML = `<div>Китов в списке: ${u.tracked_wallets ? fmtNum(u.tracked_wallets) + " " + plural(u.tracked_wallets, "кошелёк", "кошелька", "кошельков") : "нет"}; сделок по ним ${fmtNum(u.trades_tracked)}.${u.db_size_mb ? ` База занимает ${String(u.db_size_mb).replace(".", ",")} МБ.` : ""}</div>
      ${u.trades_other ? `<div class="row" style="margin-top:10px"><span>Данные по другим кошелькам: ${fmtNum(u.trades_other)} сделок (${fmtNum(u.other_wallets)} ${plural(u.other_wallets, "кошелёк", "кошелька", "кошельков")}).</span>
        <button type="button" class="btn danger" data-act="purge">Удалить данные по другим кошелькам</button></div>` : `<div style="margin-top:6px">Лишних данных нет.</div>`}`;
  } catch (e) { box.textContent = e.message; }
}

async function saveSettings(form) {
  const f = new FormData(form);
  const v = (id) => Number(form.querySelector("#" + id).value);
  const notify = {};
  ["telegram", "push", "email", "signals", "whale_exit", "limits", "weekly", "system"].forEach((k) => (notify[k] = f.has("n_" + k)));
  const patch = {
    mode: f.get("mode") || "paper",
    require_confirmation: f.has("require_confirmation"),
    bankroll_usd: v("bankroll_usd"), max_bet_usd: v("max_bet_usd"), daily_limit_usd: v("daily_limit_usd"),
    daily_loss_stop_frac: v("daily_loss_stop_frac") / 100, monthly_loss_stop_frac: v("monthly_loss_stop_frac") / 100,
    categories: f.getAll("cat"), notify,
    ai_mode: f.get("ai_mode") || "manual",
    news_sources: { google_news: f.has("src_google_news"), manifold: f.has("src_manifold"), reddit: f.has("src_reddit"),
      // без ключа флажок неактивен — сохраняем «включено», чтобы источник заработал сразу после добавления ключа
      kalshi: f.has("src_kalshi"), bookmakers: f.has("src_bookmakers") || Boolean(form.querySelector("[name=src_bookmakers]")?.disabled) },
    telegram_channels: String(f.get("telegram_channels") || "").split(/[\s,;]+/).filter(Boolean),
    auto_discover: f.has("auto_discover"),
  };
  const r = await api("/settings", { method: "PUT", body: patch });
  const errs = Object.entries(r.errors || {});
  toast(errs.length ? "Не сохранено: " + errs.map(([k, e]) => `${k} — ${e}`).join("; ") : "Настройки сохранены");
}

// ---------------------------------------------------------------- «Первый запуск»
async function viewOnboarding(step = "1") {
  step = Number(step);
  const steps = ["Проверка доступа", "Режим и сумма", "Лимиты и уведомления"];
  const head = `<p style="margin:0;font-weight:700;font-size:18px">WhaleFollow</p>
    <ol class="steps">${steps.map((s, i) => `<li class="${i + 1 === step ? "on" : i + 1 < step ? "done" : ""}" ${i + 1 === step ? 'aria-current="step"' : ""}><span></span>${i + 1}. ${s}</li>`).join("")}</ol>`;
  let body = "";
  if (step === 1) {
    body = `<h1 style="font-size:30px">Добро пожаловать</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2)">WhaleFollow следит за сильными игроками на Polymarket и предлагает повторить их ставку, только когда это ещё выгодно.</p>
      <div class="card col" style="gap:12px"><b style="font-size:16px">Как это работает</b>
        ${["Находим игроков, которые выигрывают стабильно, а не случайно", "Когда такой игрок делает ставку, проверяем цену и новости", "Предлагаем ставку с понятным расчётом — решение за вами"]
          .map((t, i) => `<div class="row" style="flex-wrap:nowrap"><span class="num-dot">${i + 1}</span><span>${t}</span></div>`).join("")}</div>
      <form class="card" id="ob1"><fieldset><legend>Перед началом подтвердите</legend>
        <label class="ck" style="align-items:flex-start"><input type="checkbox" name="j" required><span>Торговля на Polymarket разрешена в моей стране<span class="sub" style="display:block">Правила различаются по странам — проверьте до реальных ставок</span></span></label>
        <label class="ck"><input type="checkbox" name="r" required><span>Я понимаю, что часть ставок проиграет и можно потерять деньги</span></label>
        <label class="ck" style="align-items:flex-start"><input type="checkbox" checked disabled><span>Начну с пробного режима на виртуальных деньгах<span class="sub" style="display:block">В этой версии доступен только он</span></span></label>
      </fieldset><div class="row" style="margin-top:12px"><button class="btn p" style="min-width:160px">Далее</button><span class="sub">Шаг 1 из 3 · займёт около минуты</span></div></form>`;
  } else if (step === 2) {
    const s = await api("/settings");
    body = `<h1>Режим и сумма</h1>
      <form class="card col" id="ob2"><label class="opt"><input type="radio" checked disabled><span><b>Пробный режим</b><span class="sub" style="display:block">Все ставки на виртуальные деньги. Так вы увидите, как работает система, без риска.</span></span></label>
        <div class="fld"><label for="ob-bank">Виртуальная сумма для торговли, $</label><input id="ob-bank" class="inp" type="number" min="100" step="100" value="${s.bankroll_usd}"></div>
        <label class="ck"><input type="checkbox" id="ob-confirm" ${s.require_confirmation ? "checked" : ""}>Подтверждать каждую ставку вручную (рекомендуем)</label>
        <div class="row"><button class="btn p" style="min-width:160px">Далее</button><a class="link" href="#/welcome/1">Назад</a></div></form>`;
  } else {
    const s = await api("/settings");
    body = `<h1>Лимиты и уведомления</h1>
      <form class="card col" id="ob3">
        <div class="fld"><label for="ob-max">Максимум на одну ставку, $</label><input id="ob-max" class="inp" type="number" min="1" value="${s.max_bet_usd}"></div>
        <div class="fld"><label for="ob-day">Лимит в день, $</label><input id="ob-day" class="inp" type="number" min="1" value="${s.daily_limit_usd}"></div>
        <div class="sub">Дополнительно система ограничивает ставку 2% от суммы и ставит паузу при убытке 5% за день или 10% за месяц.</div>
        <div class="row"><button class="btn p" style="min-width:160px">Начать</button><a class="link" href="#/welcome/2">Назад</a></div></form>`;
  }
  main().innerHTML = `<div class="ob"><div class="col">${head}${body}</div></div>`;
}

// ---------------------------------------------------------------- события
document.addEventListener("click", async (e) => {
  const go = e.target.closest("[data-go]");
  if (go) { e.preventDefault(); location.hash = go.dataset.go; if (location.hash === go.dataset.go) render(); return; }
  const el = e.target.closest("[data-act]");
  if (!el || el.tagName === "INPUT") return;
  const act = el.dataset.act, id = el.dataset.id;
  try {
    if (act === "bell") return openNotifications(el);
    if (act === "sound") { setSound(!soundOn()); if (soundOn()) chime(); toast(soundOn() ? "Звук при новом сигнале включён" : "Звук выключен"); return render(); }
    if (act === "read-all") { await api("/notifications/read", { method: "POST" }); state.notifOpen = false; return render(); }
    if (act === "stop") {
      const ok = await confirmDialog({ title: "Остановить всё?", ok: "Остановить", danger: true,
        text: "Новые ставки создаваться не будут. Открытые ставки останутся до закрытия рынков — их можно продать вручную в разделе «Мои ставки»." });
      if (ok) { await api("/stop", { method: "POST" }); toast("Система остановлена"); render(); }
      return;
    }
    if (act === "resume") { await api("/resume", { method: "POST" }); toast("Работа возобновлена"); return render(); }
    if (act === "amount") { $("#amount").hidden = !$("#amount").hidden; return; }
    if (act === "sim") {
      const holder = el.closest("section").parentElement;
      return loadSim(holder.id, id, Number(el.dataset.v), true);
    }
    if (act === "sim-range") { const f = el.closest("section").querySelector(".sim-range"); f.hidden = !f.hidden; if (!f.hidden) f.querySelector("input").focus(); return; }
    if (act === "lb-check") {
      const inp = $("#addr");
      inp.value = id;
      window.scrollTo({ top: 0, behavior: "smooth" });
      return runCheck(id);
    }
    if (act === "lb-more") return loadLeaderboard(Number(el.dataset.v));
    if (act === "diag-run") return viewDiagnostics(true);
    if (act === "diag-copy") {
      const txt = $("#diag-report").textContent;
      try { await navigator.clipboard.writeText(txt); toast("Отчёт скопирован"); }
      catch { const r = document.createRange(); r.selectNodeContents($("#diag-report")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Выделено — скопируйте сочетанием клавиш"); }
      return;
    }
    if (act === "purge") {
      const ok = await confirmDialog({ title: "Удалить данные по другим кошелькам?", ok: "Удалить", danger: true,
        text: "Будут удалены сделки, позиции и оценки игроков, которых нет в вашем списке китов. Данные китов из списка и ваши ставки останутся. Отменить нельзя." });
      if (!ok) return;
      const r = await api("/data/purge", { method: "POST" });
      toast(`Удалено сделок: ${fmtNum((r.removed || {}).trades || 0)}`);
      return loadDataUsage();
    }
    if (act === "sell-bet") {
      const ok = await confirmDialog({ title: "Продать ставку сейчас?", ok: "Продать", danger: true,
        text: "Ставка продаётся целиком по лучшим ценам покупателей в стакане, с комиссией Polymarket. Отменить продажу нельзя." });
      if (!ok) return;
      const r = await api(`/bets/${encodeURIComponent(id)}/sell`, { method: "POST" });
      toast(r.ok ? `Продано за ${money(r.usd)}` : r.error);
      return render();
    }
    if (act === "tune") {
      const v = Number(el.dataset.v);
      const ok = await confirmDialog({ title: `Порог выгоды ${Math.round(v * 100)}%?`,
        text: "Система будет покупать только сигналы с запасом выгоды не меньше этого порога. Решение принято по исходам пробного режима; вернуть можно так же.", ok: "Применить" });
      if (!ok) return;
      const r = await api("/trial/tune", { method: "POST", body: { edge_threshold: v } });
      toast(r.ok ? "Порог изменён" : r.error);
      return render();
    }
    if (act === "confirm") {
      el.disabled = true;
      const inp = $("#amount-inp");
      const body = inp && !$("#amount").hidden ? { size_usd: Number(inp.value) } : {};
      const r = await api(`/signals/${encodeURIComponent(id)}/confirm`, { method: "POST", body });
      toast(r.status === "executed" ? `Куплено на ${money(r.usd)} по ${Math.round(r.avg_price * 100)}¢` : r.error || "Не выполнено");
      if (r.status === "executed") { location.hash = nextPendingAfter(id); return; }
      return render();
    }
    if (act === "skip") { await api(`/signals/${encodeURIComponent(id)}/skip`, { method: "POST" }); toast("Сигнал пропущен"); location.hash = nextPendingAfter(id); return; }
    if (act === "exit") {
      const r = await api(`/exits/${encodeURIComponent(id)}`, { method: "POST", body: { action: el.dataset.v } });
      toast(r.status === "sold" ? `Продано за ${money(r.usd)}` : r.status === "held" ? "Держим до закрытия рынка" : r.error || "Готово");
      return render();
    }
    if (act === "add") {
      const r = await api("/whales", { method: "POST", body: { entity_id: id, mode: el.dataset.v } });
      toast(r.message || (r.mode === "copy" ? "Кит добавлен, копирование включено" : "Кит добавлен в режиме «Только следить»"));
      location.hash = "#/whales";
      return;
    }
    if (act === "unfollow") {
      const ok = await confirmDialog({ title: "Перестать следить?", ok: "Перестать", danger: true, text: "Сигналы по этому киту приходить не будут. Открытые ставки останутся." });
      if (ok) { await api("/whales/" + encodeURIComponent(id), { method: "PATCH", body: { mode: "off" } }); location.hash = "#/whales"; }
      return;
    }
    if (act === "csv") return downloadCsv();
  } catch (err) {
    toast(err.message);
    el.disabled = false;
  }
});

document.addEventListener("change", async (e) => {
  const el = e.target;
  try {
    if (el.dataset.act === "copy") {
      const r = await api("/whales/" + encodeURIComponent(el.dataset.id), { method: "PATCH", body: { mode: el.checked ? "copy" : "watch" } });
      toast(r.message || (r.mode === "copy" ? "Копирование включено" : "Копирование выключено — только следим"));
      return render();
    }
    if (el.dataset.act === "topic") {
      await api("/whales/" + encodeURIComponent(el.dataset.id), { method: "PATCH", body: { category: el.dataset.cat, enabled: el.checked } });
      toast(el.checked ? "Тема включена" : "Тема выключена для этого кита");
      return render();
    }
  } catch (err) { toast(err.message); }
});

document.addEventListener("submit", async (e) => {
  const f = e.target;
  if (f.method === "dialog") return;         // окна подтверждения закрываются сами
  e.preventDefault();
  try {
    if (f.id === "check-form") return runCheck($("#addr").value.trim());
    if (f.classList.contains("sim-range")) {
      const fd = new FormData(f);
      const st = Math.floor(new Date(fd.get("start") + "T00:00:00Z") / 1000), en = Math.floor(new Date(fd.get("end") + "T23:59:59Z") / 1000);
      if (!st || !en || en <= st) return toast("Проверьте даты: конец должен быть позже начала");
      return loadSim(f.closest("section").parentElement.id, f.dataset.id, 90, true, [st, Math.min(en, Math.floor(Date.now() / 1000))]);
    }
    if (f.id === "settings-form") return saveSettings(f);
    if (f.id === "ob1") {
      const fd = new FormData(f);
      const r = await api("/onboarding", { method: "POST", body: { jurisdiction_ok: fd.has("j"), risk_ack: fd.has("r") } });
      if (!r.ok) return toast(r.error);
      location.hash = "#/welcome/2";
    }
    if (f.id === "ob2") {
      await api("/settings", { method: "PUT", body: { bankroll_usd: Number($("#ob-bank").value), require_confirmation: $("#ob-confirm").checked } });
      location.hash = "#/welcome/3";
    }
    if (f.id === "ob3") {
      const r = await api("/settings", { method: "PUT", body: { max_bet_usd: Number($("#ob-max").value), daily_limit_usd: Number($("#ob-day").value) } });
      if (Object.keys(r.errors || {}).length) return toast("Проверьте значения лимитов");
      toast("Готово! Система работает в пробном режиме");
      location.hash = "#/";
    }
  } catch (err) { toast(err.message); }
});

document.addEventListener("keydown", (e) => {
  if (hotkey(e)) { e.preventDefault(); return; }
  const row = e.target.closest && e.target.closest(".ledger-row[data-go]");
  if (row && (e.key === "Enter" || e.key === " ") && e.target === row) { e.preventDefault(); location.hash = row.dataset.go; return; }
  if (e.key === "Escape" && state.notifOpen) { state.notifOpen = false; $("#notif-slot").innerHTML = ""; }
});

// ---------------------------------------------------------------- запуск
setInterval(tickTimers, 1000);
setInterval(async () => {
  if (document.hidden || state.notifOpen || $("#dialog").open || document.body.classList.contains("locked")) return;
  const hash = location.hash || "#/";
  const active = document.activeElement;
  const typing = active && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
  if (state.live) return refreshStatus();           // поток событий жив — экран обновляется сам
  if ((hash === "#/" || hash === "#/positions" || hash.startsWith("#/positions/")) && !typing) await render();
  else await refreshStatus();
}, 10000);
(async () => {
  try {
    const a = await (await fetch("/api/auth", { credentials: "same-origin" })).json();
    if (a.required && !a.ok) return showLogin();
  } catch { /* сервер недоступен — покажет render */ }
  render();
  startLive();
})();
