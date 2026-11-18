/* WhaleFollow — игроки, рынки, боковые панели и мини-карточки.
   Данные — готовые цифры Polymarket (/api/players, /api/markets). Подключается до app.js: здесь только
   объявления функций и обработчики событий; общие помощники (esc, money, api…) берутся из app.js. */
"use strict";

// ---------------------------------------------------------------- аватар и короткие форматы
function avatar(img, code, { lg = false, sm = false } = {}) {
  const cls = lg ? " lg" : sm ? " sm" : "";
  if (img && /^https:\/\//.test(img)) {
    return `<span class="ava${cls}"><img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer"
      onerror="this.parentNode.outerHTML=seal('${esc(String(code || "?").slice(0, 4))}',{lg:${lg}})"></span>`;
  }
  return seal(code, { lg });
}
const shortAddr = (w) => (w ? w.slice(0, 6) + "…" + w.slice(-4) : "");
const playerName = (c) => c.name || `Кит ${c.short || shortAddr(c.wallet)}`;
const compactMoney = (v, { sign = false } = {}) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "−" : sign && v > 0 ? "+" : "";
  if (a >= 1e6) return `${s}$${(a / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн`;
  if (a >= 1e4) return `${s}$${(a / 1e3).toLocaleString("ru-RU", { maximumFractionDigits: 0 })} тыс.`;
  return `${s}$${Math.round(a).toLocaleString("ru-RU")}`;
};
const monthsSince = (ts) => {
  if (!ts) return "—";
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / (30.4 * 86400)));
  return m < 1 ? "меньше месяца" : m < 12 ? `${m} мес.` : `${Math.floor(m / 12)} г. ${m % 12 ? (m % 12) + " мес." : ""}`;
};
const untilText = (ts) => {
  if (!ts) return "—";
  const d = Math.round((ts - Date.now() / 1000) / 86400);
  return d < 0 ? "срок прошёл" : d === 0 ? "сегодня" : `через ${days(d)}`;
};
const FORM_TEXT = { hot: ["g", "в форме"], cold: ["r", "остыл"], neutral: ["n", "ровно"] };

// ---------------------------------------------------------------- кривая прибыли с выбором периода
const PNL_PERIODS = [["7", "7 дн"], ["30", "30 дн"], ["90", "90 дн"], ["all", "Всё время"]];
function pnlSlice(series, period) {
  if (!series || !series.length) return [];
  if (period === "all") return series;
  const cut = Date.now() / 1000 - Number(period) * 86400;
  const i = series.findIndex((p) => p.ts >= cut);
  return series.slice(Math.max(0, i - 1));
}
function pnlChartHtml(c, period, { height = 170, id = "" } = {}) {
  const pts = pnlSlice(c.series, period);
  const base = pts.length ? pts[0].pnl : 0;
  const delta = pts.length ? pts[pts.length - 1].pnl - base : null;
  return `<div class="pnl-head"><div><div class="label">Прибыль за период</div><b class="pnl-delta ${tone(delta)}">${compactMoney(delta, { sign: true })}</b></div>
      <div class="seg sm" role="group" aria-label="Период графика">${PNL_PERIODS.map(([k, l]) => `<button type="button" aria-pressed="${k === period}" data-act="pnl-period" data-id="${esc(id)}" data-v="${k}">${l}</button>`).join("")}</div></div>
    ${lineChart(pts.map((p) => ({ x: p.ts, y: p.pnl - base, label: dateLabel(p.ts) })), { height, label: "Прибыль игрока по данным Polymarket", fmtY: (v) => compactMoney(v) })}`;
}

// неделя за неделей: зелёная — неделя в плюсе, красная — в минусе; высота — размер результата
function formStrip(weeks, { h = 46 } = {}) {
  if (!weeks || !weeks.length) return `<div class="sub">Недостаточно истории для ленты формы</div>`;
  const mx = Math.max(...weeks.map((w) => Math.abs(w.pnl)), 1);
  return `<div class="form-strip" role="img" aria-label="Результат по неделям за полгода">${weeks.map((w) => {
    const hh = Math.max(3, Math.round((Math.abs(w.pnl) / mx) * (h / 2)));
    const t = w.pnl > 0.5 ? "g" : w.pnl < -0.5 ? "r" : "z";
    return `<span class="wk ${t}" style="--h:${hh}px" title="Неделя с ${dateLabel(w.ts)}: ${compactMoney(w.pnl, { sign: true })}"></span>`;
  }).join("")}</div>
  <div class="form-axis sub"><span>${dateLabel(weeks[0].ts)}</span><span>эта неделя</span></div>`;
}
function formLine(f) {
  if (!f || !f.weeks) return "";
  const [t, label] = FORM_TEXT[f.state] || FORM_TEXT.neutral;
  const parts = [];
  if (f.streak_now) parts.push(`${f.streak_now} ${plural(f.streak_now, "неделя", "недели", "недель")} подряд в плюсе`);
  if (f.green_share !== null && f.green_share !== undefined) parts.push(`плюсовых недель ${Math.round(f.green_share * 100)}%`);
  if (f.streak_best) parts.push(`лучшая серия ${f.streak_best}`);
  return `<div class="row" style="gap:10px"><span class="chip ${t}">${label}</span><span class="sub">${parts.join(" · ")}</span></div>`;
}

// ---------------------------------------------------------------- позиции игрока
function positionsHtml(list, { limit = 8 } = {}) {
  if (!list || !list.length) return `<div class="sub">Открытых позиций нет</div>`;
  return `<div class="plist">${list.slice(0, limit).map((p) => `
    <a class="prow" ${p.url ? `href="${safeUrl(p.url)}" target="_blank" rel="noopener"` : ""}>
      ${p.icon ? `<img class="picon" src="${esc(p.icon)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : `<span class="picon"></span>`}
      <div class="grow"><div class="t">${esc(p.title)}</div>
        <div class="m"><span class="chip b sm">«${esc(p.outcome)}»</span><span>${cents(p.avg_cents)} → ${cents(p.now_cents)}</span><span>${untilText(p.end_ts)}</span>${p.we_hold ? '<span class="chip g sm">у нас тоже</span>' : ""}</div></div>
      <div class="n"><div class="tnum">${compactMoney(p.value_usd)}</div><div class="sub ${tone(p.pnl_usd)}">${compactMoney(p.pnl_usd, { sign: true })}</div></div></a>`).join("")}</div>`;
}

// ---------------------------------------------------------------- карточка игрока (полная и в панели)
function kpi(label, value, sub = "", cls = "") {
  return `<div class="kpi"><div class="label">${label}</div><div class="v ${cls}">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}
function ourChips(c) {
  const out = [];
  if (c.in_list) out.push(`<span class="chip ${c.mode === "copy" ? "g" : "i"}">${c.mode === "copy" ? "копируем" : "в списке · следим"}</span>`);
  if (c.checking) out.push(`<span class="chip a">идёт проверка</span>`);
  else if (c.checked) out.push(`<span class="chip n">${esc(c.checked.toLowerCase())}</span>`);
  if (c.copy_after_check) out.push(`<span class="chip a">копирование — после проверки</span>`);
  return out.join("");
}
function playerActions(c, { inDrawer = false } = {}) {
  const w = esc(c.wallet);
  const prof = `<a class="btn" href="${safeUrl(c.profile_url || "https://polymarket.com/profile/" + c.wallet)}" target="_blank" rel="noopener">На Polymarket</a>`;
  if (c.in_list) {
    return `<div class="row">${inDrawer ? `<a class="btn p" href="#/whales/${encodeURIComponent(c.entity_id || c.wallet)}" data-act="drawer-close">Открыть профиль</a>` : ""}${prof}</div>`;
  }
  return `<div class="row">
    <button class="btn p" data-act="padd" data-id="${w}" data-v="copy" title="Добавим сразу; копирование включится, если игрок пройдёт проверку">Добавить и копировать</button>
    <button class="btn" data-act="padd" data-id="${w}" data-v="watch">Только следить</button>${prof}</div>`;
}
function playerCardHtml(c, { inDrawer = false } = {}) {
  if (!c || c.ok === false) {
    return `<div class="card col" style="gap:10px"><div class="row">${seal(c && c.short)}<b>${esc(c && c.wallet ? shortAddr(c.wallet) : "Игрок")}</b>${c ? ourChips(c) : ""}</div>
      <div class="banner warn"><div class="grow">${esc((c && c.error) || "Нет данных")}</div></div>${c && c.wallet ? playerActions(c, { inDrawer }) : ""}</div>`;
  }
  const id = "pc" + c.wallet.slice(2, 10);
  state.pnlPeriod = state.pnlPeriod || {};
  const period = state.pnlPeriod[id] || "90";
  state.cards = state.cards || {};
  state.cards[id] = c;
  return `<section class="pcard${inDrawer ? " in-drawer" : ""}" aria-label="Игрок ${esc(playerName(c))}">
    <header class="pcard-head">${avatar(c.image, c.short, { lg: true })}
      <div class="grow"><div class="row" style="gap:8px"><h2 class="pname">${esc(playerName(c))}</h2>${c.verified ? '<span class="chip b sm">проверен Polymarket</span>' : ""}</div>
        <div class="m"><span>${esc(shortAddr(c.wallet))}</span>${c.x ? `<a class="link" href="https://x.com/${encodeURIComponent(c.x)}" target="_blank" rel="noopener">@${esc(c.x)}</a>` : ""}<span>на Polymarket ${monthsSince(c.joined_ts)}</span></div>
        <div class="row" style="gap:6px;margin-top:8px">${ourChips(c)}</div></div></header>
    <div class="kpis">
      ${kpi("Прибыль за всё время", compactMoney(c.pnl_all_usd, { sign: true }), c.rank_pnl ? `место в рейтинге: ${fmtNum(c.rank_pnl)}` : "вне рейтинга", tone(c.pnl_all_usd))}
      ${kpi("За 30 дней", compactMoney(c.pnl_30d_usd, { sign: true }), `за 7 дней ${compactMoney(c.pnl_7d_usd, { sign: true })}`, tone(c.pnl_30d_usd))}
      ${kpi("Объём за 30 дней", compactMoney(c.volume_30d_usd), c.trades_30d ? `${fmtNum(c.trades_30d)} ${plural(c.trades_30d, "сделка", "сделки", "сделок")}` : "")}
      ${kpi("В рынках сейчас", compactMoney(c.value_usd), c.positions ? `${c.positions.length}+ ${plural(c.positions.length, "позиция", "позиции", "позиций")}` : "")}
      ${kpi("Рынков всего", c.markets ? fmtNum(c.markets) : "—", c.biggest_win_usd ? `крупнейший выигрыш ${compactMoney(c.biggest_win_usd)}` : "")}
      ${kpi("Наибольшая просадка", c.max_drawdown_usd ? compactMoney(-c.max_drawdown_usd) : "—", "от пика кривой прибыли")}
    </div>
    <div class="pnl-box" id="${id}">${pnlChartHtml(c, period, { id, height: inDrawer ? 140 : 180 })}</div>
    <div class="col" style="gap:8px"><div class="top"><h3>Форма по неделям</h3>${formLine(c.form)}</div>${formStrip(c.weeks)}</div>
    ${c.positions ? `<div class="col" style="gap:8px"><h3>Открытые позиции</h3>${positionsHtml(c.positions, { limit: inDrawer ? 6 : 10 })}</div>` : ""}
    ${playerActions(c, { inDrawer })}
    <div class="sub">Цифры — готовые данные Polymarket. Они показывают, сколько заработал игрок; можно ли повторить его ставки с задержкой — покажет проверка.</div>
  </section>`;
}

// ---------------------------------------------------------------- боковая панель
function drawerEl() {
  let d = $("#drawer");
  if (!d) {
    document.body.insertAdjacentHTML("beforeend", `<div class="drawer-back" id="drawer-back" hidden></div>
      <aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" hidden>
        <div class="drawer-top"><b id="drawer-title"></b><button class="btn icon" data-act="drawer-close" aria-label="Закрыть">✕</button></div>
        <div class="drawer-body" id="drawer-body"></div></aside>`);
    d = $("#drawer");
  }
  return d;
}
function openDrawer(title, html) {
  const d = drawerEl();
  state.drawerFrom = state.drawerFrom || document.activeElement;
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  d.hidden = false; $("#drawer-back").hidden = false;
  document.body.classList.add("drawer-open");
  requestAnimationFrame(() => d.classList.add("on"));
  d.querySelector("[data-act=drawer-close]").focus({ preventScroll: true });
}
function closeDrawer() {
  const d = $("#drawer");
  if (!d || d.hidden) return false;
  d.classList.remove("on");
  document.body.classList.remove("drawer-open");
  $("#drawer-back").hidden = true;
  setTimeout(() => { d.hidden = true; }, 180);
  if (state.drawerFrom && document.body.contains(state.drawerFrom)) state.drawerFrom.focus({ preventScroll: true });
  state.drawerFrom = null;
  return true;
}
async function drawerPlayer(wallet) {
  openDrawer("Игрок", `<div class="card skeleton">Загружаем данные Polymarket…</div>`);
  try {
    const c = await api("/players/" + encodeURIComponent(wallet));
    $("#drawer-title").textContent = playerName(c);
    $("#drawer-body").innerHTML = playerCardHtml(c, { inDrawer: true });
  } catch (e) { $("#drawer-body").innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}
async function drawerMarket(token) {
  openDrawer("Рынок", `<div class="card skeleton">Загружаем рынок…</div>`);
  try {
    const m = await api("/markets/" + encodeURIComponent(token));
    $("#drawer-title").textContent = m.question || "Рынок";
    $("#drawer-body").innerHTML = marketPanelHtml(m, { compact: true });
  } catch (e) { $("#drawer-body").innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- мини-карточка при наведении
const miniCache = {};
let hoverT = null, hoverEl = null;
function popEl() {
  let p = $("#pop");
  if (!p) { document.body.insertAdjacentHTML("beforeend", `<div class="pop" id="pop" role="tooltip" hidden></div>`); p = $("#pop"); }
  return p;
}
function miniHtml(c) {
  if (!c || c.ok === false) return `<div class="sub">${esc((c && c.error) || "Нет данных")}</div>`;
  return `<div class="row" style="gap:10px">${avatar(c.image, c.short)}<div><b>${esc(playerName(c))}</b><div class="sub">${esc(shortAddr(c.wallet))}</div></div></div>
    <div class="mini-kpis"><div><div class="label">Всё время</div><b class="${tone(c.pnl_all_usd)}">${compactMoney(c.pnl_all_usd, { sign: true })}</b></div>
      <div><div class="label">30 дней</div><b class="${tone(c.pnl_30d_usd)}">${compactMoney(c.pnl_30d_usd, { sign: true })}</b></div>
      <div><div class="label">В рынках</div><b>${compactMoney(c.value_usd)}</b></div></div>
    ${spark((c.weeks || []).slice(-12).reduce((a, w) => (a.push((a.length ? a[a.length - 1] : 0) + w.pnl), a), []), { w: 220, h: 34 })}
    ${formLine(c.form)}<div class="sub">Нажмите — подробнее</div>`;
}
async function showPop(el) {
  const w = el.dataset.player;
  const p = popEl();
  const r = el.getBoundingClientRect();
  p.style.left = Math.min(window.innerWidth - 300, Math.max(8, r.left)) + "px";
  p.style.top = (r.bottom + 8 + window.scrollY) + "px";
  p.innerHTML = miniCache[w] ? miniHtml(miniCache[w]) : `<div class="sub">Загружаем…</div>`;
  p.hidden = false;
  if (!miniCache[w]) {
    try { miniCache[w] = await api(`/players/${encodeURIComponent(w)}?mini=1`); } catch (e) { miniCache[w] = { ok: false, error: e.message }; }
    if (hoverEl === el) p.innerHTML = miniHtml(miniCache[w]);
  }
}
if (window.matchMedia && matchMedia("(hover:hover) and (pointer:fine)").matches) {
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-player]");
    if (el === hoverEl) return;
    clearTimeout(hoverT);
    hoverEl = el;
    if (!el) { const p = $("#pop"); if (p) p.hidden = true; return; }
    hoverT = setTimeout(() => showPop(el), 420);
  });
}

// ---------------------------------------------------------------- «Добавить игрока»
async function viewAdd() {
  const w = await api("/whales");
  main().innerHTML = `
    <a class="back" href="#/whales">← Все киты</a>
    <div class="page-head"><div><h1>Добавить игрока</h1>
      <div class="lede">Найдите игрока по ссылке на профиль, адресу кошелька или имени. Готовая статистика Polymarket появится сразу; проверка, можно ли повторять его ставки, идёт в фоне.</div></div></div>
    <form class="finder card" id="finder" autocomplete="off" role="search">
      <label for="finder-q">Ссылка, адрес или имя игрока</label>
      <div class="row"><input id="finder-q" class="inp" placeholder="polymarket.com/@имя, 0x… или имя" aria-describedby="finder-hint">
        <button class="btn p" type="submit">Найти</button></div>
      <div class="sub" id="finder-hint">Подойдёт ссылка вида polymarket.com/profile/0x… или polymarket.com/@name, адрес 0x… или имя игрока.</div>
    </form>
    <div id="finder-out" aria-live="polite"></div>
    <div id="check-result"></div>
    <div class="top" style="margin-top:6px"><div><h2>Рейтинг Polymarket</h2>
      <div class="sub" style="margin-top:4px">Отметьте игроков и добавьте пачкой. Нажмите на строку — откроется карточка игрока. Высокая прибыль ещё не значит, что ставки можно повторить: это покажет проверка.</div></div>
      <div class="row" id="lb-filters">
        <select class="inp" id="lb-cat" aria-label="Тема">${[["overall", "Все темы"], ...TOPICS].map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
        <select class="inp" id="lb-period" aria-label="Период"><option value="month">За месяц</option><option value="week">За неделю</option><option value="all">За всё время</option><option value="day">За день</option></select>
        <select class="inp" id="lb-order" aria-label="Сортировка"><option value="pnl">По прибыли</option><option value="vol">По объёму</option></select></div></div>
    <div id="lb-box"><div class="card skeleton">Загружаем рейтинг…</div></div>
    <div class="bulk" id="bulk" hidden role="region" aria-label="Выбранные игроки">
      <b id="bulk-n"></b>
      <button class="btn p" data-act="bulk-add" data-v="copy">Добавить и копировать после проверки</button>
      <button class="btn" data-act="bulk-add" data-v="watch">Только следить</button>
      <button class="link" data-act="bulk-clear" style="background:none;border:0;cursor:pointer;min-height:44px">Снять выбор</button></div>
    ${w.candidates.length ? `<h2>Найдены автопоиском</h2><div class="ledger">
      ${w.candidates.map((c) => `<div class="ledger-row" style="grid-template-columns:minmax(0,1fr) auto">
        <div class="who">${seal(c.name)}<div><div class="t nm">Кит ${esc(c.name)}</div><div class="m"><span>${esc(c.categories.join(", "))}</span><span>риск: ${esc(c.risk)}</span></div></div></div>
        <button class="btn" data-act="add" data-id="${esc(c.entity_id)}" data-v="copy">Добавить и копировать</button></div>`).join("")}</div>` : ""}`;
  state.picked = new Set();
  ["lb-cat", "lb-period", "lb-order"].forEach((id) => $("#" + id).addEventListener("change", () => loadLeaderboard(0)));
  const q = $("#finder-q");
  let t = null;
  q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => findPlayer(q.value), 450); });
  loadLeaderboard(0);
  const pre = (location.hash.split("?q=")[1] || "");
  if (pre) { q.value = decodeURIComponent(pre); findPlayer(q.value); }
}

async function findPlayer(text) {
  const out = $("#finder-out");
  text = (text || "").trim();
  if (!out) return;
  if (text.length < 2) { out.innerHTML = ""; return; }
  const my = (findPlayer.seq = (findPlayer.seq || 0) + 1);
  out.innerHTML = `<div class="card skeleton">Ищем игрока…</div>`;
  let r;
  try { r = await api("/players/resolve?q=" + encodeURIComponent(text)); } catch (e) { r = { ok: false, error: e.message, items: [] }; }
  if (my !== findPlayer.seq) return;
  if (!r.ok) { out.innerHTML = `<div class="banner warn"><div class="grow">${esc(r.error)}</div></div>`; return; }
  if (!r.items.length) { out.innerHTML = `<div class="card sub">Никого не нашли. Проверьте написание или вставьте ссылку на профиль.</div>`; return; }
  if (r.items.length === 1 || r.items[0].exact) return showPlayer(r.items[0].wallet);
  out.innerHTML = `<div class="ledger">${r.items.map((i) => `
    <button class="ledger-row pick" style="grid-template-columns:minmax(0,1fr) auto" data-act="pshow" data-id="${esc(i.wallet)}">
      <div class="who">${avatar(i.image, i.short)}<div class="grow"><div class="t">${esc(i.name || i.short)}</div><div class="m"><span>${esc(shortAddr(i.wallet))}</span>${ourChips(i)}</div></div></div>
      <span class="btn">Показать</span></button>`).join("")}</div>`;
}

async function showPlayer(wallet) {
  const out = $("#finder-out");
  if (!out) return;
  out.innerHTML = `<div class="card skeleton">Загружаем статистику Polymarket…</div>`;
  try {
    const c = await api("/players/" + encodeURIComponent(wallet));
    out.innerHTML = playerCardHtml(c);
    if (c.checking) followCheck(c.checking);
  } catch (e) { out.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}

async function followCheck(jobId) {
  const box = $("#check-result");
  if (!box) return;
  const route = location.hash;
  let r = await api("/whales/check/" + encodeURIComponent(jobId));
  while (r.ok && r.state === "running") {
    if (location.hash !== route || !document.body.contains(box)) return;
    box.innerHTML = checkProgress(r);
    await new Promise((ok) => setTimeout(ok, 900));
    r = await api("/whales/check/" + encodeURIComponent(jobId));
  }
  if (!document.body.contains(box)) return;
  box.innerHTML = r.ok ? checkResult(r) : `<div class="card err" role="alert">${esc(r.error)}</div>`;
  if (r.ok && r.entity_id) loadSim("check-sim", r.entity_id, 90, false);
}

async function loadLeaderboard(offset = 0) {
  const box = $("#lb-box");
  if (!box) return;
  const q = `category=${$("#lb-cat").value}&period=${$("#lb-period").value}&order=${$("#lb-order").value}&offset=${offset}`;
  if (!offset) box.innerHTML = `<div class="card skeleton">Загружаем рейтинг…</div>`;
  let r;
  try { r = await api("/leaderboard?" + q); } catch (e) { r = { ok: false, error: e.message, items: [] }; }
  if (!r.ok) { box.innerHTML = `<div class="banner warn"><div class="grow"><b>Рейтинг не загрузился</b><div>${esc(r.error)}</div>
    <div class="sub">Проверьте доступ на экране <a class="link" href="#/diagnostics">«Диагностика»</a>.</div></div></div>`; return; }
  const rows = r.items.map((x) => `<div class="ledger-row lb-row${state.picked.has(x.wallet) ? " picked" : ""}" data-act="lb-open" data-id="${esc(x.wallet)}" tabindex="0" role="button" aria-label="Карточка игрока ${esc(x.name || x.short)}">
      <label class="ck lb-ck" onclick="event.stopPropagation()">${x.in_list ? `<span class="chip g sm" title="Уже в вашем списке" aria-label="в списке">✓</span>` : `<input type="checkbox" data-act="lb-pick" data-id="${esc(x.wallet)}" ${state.picked.has(x.wallet) ? "checked" : ""} aria-label="Выбрать ${esc(x.name || x.short)}">`}</label>
      <div class="tnum sub rank">${x.rank || "—"}</div>
      <div class="who" data-player="${esc(x.wallet)}">${seal(x.short)}<div class="grow"><div class="t">${esc(x.name || x.short)}${x.verified ? ' <span class="chip b sm">проверен</span>' : ""}</div>
        <div class="m"><span>${esc(shortAddr(x.wallet))}</span>${x.checked && !x.in_list ? `<span class="chip n sm">${esc(x.checked.toLowerCase())}</span>` : ""}</div></div></div>
      <div class="n"><b class="tnum ${tone(x.pnl)}">${compactMoney(x.pnl, { sign: true })}</b><div class="sub">прибыль</div></div>
      <div class="n hide-sm"><div class="tnum">${compactMoney(x.volume)}</div><div class="sub">объём</div></div>
      <div class="n hide-md"><div class="tnum">${x.roi_pct === null ? "—" : signedPct(x.roi_pct)}</div><div class="sub">к объёму</div></div></div>`).join("");
  const head = `<div class="ledger-head lb-row"><label class="ck lb-ck"><input type="checkbox" data-act="lb-pick-page" aria-label="Отметить всех на странице"></label><span>#</span><span>Игрок</span><span class="n">Прибыль</span><span class="n hide-sm">Объём</span><span class="n hide-md">К объёму</span></div>`;
  const html = rows || `<div class="ledger-empty">В этой теме за выбранный период рейтинг пуст.</div>`;
  if (offset) {
    $("#lb-more") && $("#lb-more").remove();
    $("#lb-ledger").insertAdjacentHTML("beforeend", html);
  } else {
    box.innerHTML = `<div class="ledger" id="lb-ledger">${rows ? head : ""}${html}</div>`;
  }
  if (r.has_more && offset + 50 <= 1000) box.insertAdjacentHTML("beforeend", `<button class="btn" id="lb-more" style="margin-top:12px" data-act="lb-more" data-v="${offset + 50}">Показать ещё 50</button>`);
}

function syncBulk() {
  const b = $("#bulk");
  if (!b) return;
  const n = state.picked.size;
  b.hidden = n === 0;
  $("#bulk-n").textContent = `Выбрано: ${n}`;
  $$(".lb-row[data-id]").forEach((r) => r.classList.toggle("picked", state.picked.has(r.dataset.id)));
}

async function addPlayers(wallets, mode) {
  const r = await api("/players/add", { method: "POST", body: { wallets, mode } });
  toast(r.message || r.error || "Готово");
  Object.keys(miniCache).forEach((k) => delete miniCache[k]);
  return r;
}

// ---------------------------------------------------------------- панель рынка (ставка, сигнал, боковая панель)
function outcomesBar(outs) {
  if (!outs || !outs.length) return "";
  return `<div class="outs">${outs.map((o) => `<div class="out${o.ours ? " ours" : ""}">
    <div class="top"><span>${esc(o.label)}${o.ours ? ' <span class="chip b sm">наша сторона</span>' : ""}</span><b class="tnum">${o.price === null ? "—" : cents(o.price * 100)}</b></div>
    <div class="obar"><span style="width:${Math.round((o.price || 0) * 100)}%"></span></div></div>`).join("")}</div>`;
}
function holdersHtml(hs) {
  if (!hs || !hs.length) return `<div class="sub">Данных о держателях нет</div>`;
  return `<div class="hlist">${hs.slice(0, 10).map((h) => `
    <button class="hrow" data-act="player" data-id="${esc(h.wallet)}" data-player="${esc(h.wallet)}">
      ${avatar(h.image, h.short, { sm: true })}
      <div class="grow"><div class="t">${esc(h.name || h.short)}${h.in_list ? ' <span class="chip g sm">в списке</span>' : ""}</div>
        <div class="m"><span class="chip ${h.same_side ? "b" : "n"} sm">«${esc(h.outcome || "?")}»</span>${h.avg_cents !== null ? `<span>вход ${cents(h.avg_cents)}</span>` : ""}</div></div>
      <div class="n"><div class="tnum">${h.value_usd !== null ? compactMoney(h.value_usd) : fmtNum(Math.round(h.shares)) + " акц."}</div>
        ${h.pnl_usd !== null ? `<div class="sub ${tone(h.pnl_usd)}">${compactMoney(h.pnl_usd, { sign: true })}</div>` : ""}</div></button>`).join("")}</div>`;
}
function whaleNowHtml(m) {
  const ps = m.whale_positions || [];
  if (!m.whale) return "";
  if (!ps.length) return `<div class="card col" style="gap:6px"><h3>Кит ${esc(m.whale)} сейчас</h3><div class="sub">Позиции в этом рынке у кита больше нет — он вышел или ещё не появился в данных Polymarket.</div></div>`;
  return `<div class="card col" style="gap:10px"><h3>Кит ${esc(m.whale)} сейчас</h3>${ps.map((p) => `
    <div class="kpis k3">${kpi("Держит", `${fmtNum(Math.round(p.shares))} акц.`, `«${esc(p.outcome)}»`)}${kpi("Средний вход", cents(p.avg_cents), `сейчас ${cents(p.now_cents)}`)}${kpi("Его прибыль", compactMoney(p.pnl_usd, { sign: true }), `стоит ${compactMoney(p.value_usd)}`, tone(p.pnl_usd))}</div>`).join("")}
    <div class="sub">Готовые данные Polymarket по позиции кита: средняя цена и прибыль уже посчитаны.</div></div>`;
}
function marketPanelHtml(m, opts = {}) {
  const compact = !!opts.compact;
  if (!m || m.ok === false) return `<div class="card sub">${esc((m && m.error) || "Нет данных о рынке")}</div>`;
  const pts = (m.chart || []).map((p) => ({ x: p.ts, y: p.price * 100, label: dateLabel(p.ts) }));
  const marks = m.whale_mark && m.whale_mark.ts && m.whale_mark.price !== null ? [{ x: m.whale_mark.ts, y: m.whale_mark.price * 100, color: "#C8A464", label: "кит купил", row: 0 }] : [];
  const facts = `<div class="kpis ${compact ? "k2" : "k4"}">
      ${kpi("Закроется", m.end_ts ? new Date(m.end_ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) : "—", untilText(m.end_ts))}
      ${kpi("Объём торгов", compactMoney(m.volume_usd), m.volume_24h_usd ? `за сутки ${compactMoney(m.volume_24h_usd)}` : "")}
      ${kpi("Ликвидность", compactMoney(m.liquidity_usd), m.spread !== null && m.spread !== undefined ? `разница цен ${cents(m.spread * 100)}` : "")}
      ${kpi("Комиссия", m.fee_rate ? `${Math.round(m.fee_rate * 100)}%` : "нет", "от p·(1−p) на акцию")}</div>`;
  return `<div class="mpanel col" style="gap:16px">
    ${m.event_title && m.event_title !== m.question ? `<div class="sub">Событие: ${esc(m.event_title)}</div>` : ""}
    ${facts}
    ${outcomesBar(m.outcomes)}
    ${!compact && !opts.noChart && pts.length > 1 ? `<div class="card"><div class="top"><h3>Цена «${esc(m.side)}» за месяц</h3><span class="sub">в центах</span></div>${lineChart(pts, { height: 170, label: "Цена исхода", fmtY: (v) => Math.round(v) + "¢", marks })}</div>` : ""}
    <div class="${compact ? "col" : "grid2"}" style="gap:16px">
      ${whaleNowHtml(m)}
      <div class="card col" style="gap:10px"><div class="top"><h3>Кто ещё держит</h3><span class="sub">крупнейшие позиции</span></div>${holdersHtml(m.holders)}</div></div>
    ${m.rules ? `<details class="more card"><summary class="link">Правила рынка — как он разрешится</summary><div class="rules">${esc(m.rules).replace(/\n/g, "<br>")}</div></details>` : ""}
    <div class="row">${m.url ? `<a class="btn" href="${safeUrl(m.url)}" target="_blank" rel="noopener">Открыть на Polymarket</a>` : ""}</div></div>`;
}
async function loadMarketPanel(boxId, token, opts = {}) {
  const box = $("#" + boxId);
  if (!box) return;
  box.innerHTML = `<div class="card skeleton">Загружаем рынок: держатели, объём, правила…</div>`;
  try {
    const m = await api("/markets/" + encodeURIComponent(token));
    if (!document.body.contains(box)) return;
    box.innerHTML = (opts.before ? opts.before(m) : "") + marketPanelHtml(m, opts);
  } catch (e) { box.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}

// сценарии по открытой ставке: сбудется / нет / продать сейчас (с комиссией Polymarket)
function scenariosHtml(b, m) {
  if (!b.is_open) return "";
  const rate = (m && m.fee_rate) || 0;
  const bid = b.now_cents !== null ? b.now_cents / 100 : null;
  const sh = b.shares_exact || b.shares || 0, cost = b.cost_exact || b.stake_usd || 0, paid = b.fees_usd || 0;
  const sellFee = bid !== null ? rate * sh * bid * (1 - bid) : 0;
  const sellNow = bid !== null ? sh * bid - sellFee - cost - paid : null;
  return `<div class="scen">
    <div class="sc g"><div class="label">Если сбудется</div><b>${money(sh - cost - paid, { sign: true })}</b><div class="sub">получите ${money(sh)} за ${fmtNum(Math.round(sh))} акц.</div></div>
    <div class="sc r"><div class="label">Если не сбудется</div><b>${money(-cost - paid, { sign: true })}</b><div class="sub">ставка сгорит целиком</div></div>
    <div class="sc"><div class="label">Если продать сейчас</div><b>${sellNow === null ? "—" : money(sellNow, { sign: true })}</b><div class="sub">${bid === null ? "нет цены покупателя" : `по ${cents(b.now_cents)}, комиссия ≈ ${money(sellFee, { cents: true })}`}</div></div></div>`;
}

// ---------------------------------------------------------------- профиль кита: блок Polymarket
async function loadPlayerBox(boxId, wallet) {
  const box = $("#" + boxId);
  if (!box) return;
  box.innerHTML = `<div class="card skeleton">Загружаем готовую статистику Polymarket…</div>`;
  try {
    const c = await api("/players/" + encodeURIComponent(wallet));
    if (!document.body.contains(box)) return;
    if (c.ok === false) { box.innerHTML = `<div class="banner gray"><div class="grow">Статистика Polymarket недоступна: ${esc(c.error || "")}</div></div>`; return; }
    const id = "pc" + c.wallet.slice(2, 10);
    state.cards = state.cards || {}; state.cards[id] = c;
    state.pnlPeriod = state.pnlPeriod || {};
    const period = state.pnlPeriod[id] || "90";
    const title = $("#whale-title"), seal0 = $("#whale-seal");
    if (title && c.name) title.innerHTML = `${esc(c.name)}${c.verified ? ' <span class="chip b sm" style="vertical-align:middle">проверен Polymarket</span>' : ""}`;
    if (seal0 && c.image) seal0.innerHTML = avatar(c.image, c.short, { lg: true });
    box.innerHTML = `<section class="card col" style="gap:18px" aria-label="Статистика Polymarket">
      <div class="top"><h2>По данным Polymarket</h2><a class="link" href="${safeUrl(c.profile_url)}" target="_blank" rel="noopener">Профиль на Polymarket</a></div>
      <div class="kpis">
        ${kpi("Прибыль за всё время", compactMoney(c.pnl_all_usd, { sign: true }), c.rank_pnl ? `место ${fmtNum(c.rank_pnl)} в рейтинге` : "вне рейтинга", tone(c.pnl_all_usd))}
        ${kpi("За 30 дней", compactMoney(c.pnl_30d_usd, { sign: true }), `90 дней ${compactMoney(c.pnl_90d_usd, { sign: true })}`, tone(c.pnl_30d_usd))}
        ${kpi("Объём за 30 дней", compactMoney(c.volume_30d_usd), c.trades_30d ? `${fmtNum(c.trades_30d)} сделок` : "")}
        ${kpi("В рынках сейчас", compactMoney(c.value_usd), c.markets ? `рынков всего ${fmtNum(c.markets)}` : "")}
        ${kpi("Крупнейший выигрыш", compactMoney(c.biggest_win_usd))}
        ${kpi("На Polymarket", monthsSince(c.joined_ts), c.joined_ts ? "с " + new Date(c.joined_ts * 1000).toLocaleDateString("ru-RU", { month: "long", year: "numeric" }) : "")}</div>
      <div class="pnl-box" id="${id}">${pnlChartHtml(c, period, { id })}</div>
      <div class="col" style="gap:8px"><div class="top"><h3>Форма по неделям</h3>${formLine(c.form)}</div>${formStrip(c.weeks)}</div>
      <div class="col" style="gap:8px"><h3>Открытые позиции сейчас</h3>${positionsHtml(c.positions, { limit: 10 })}</div></section>`;
  } catch (e) { box.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- события
document.addEventListener("click", async (e) => {
  if (e.target.id === "drawer-back") { closeDrawer(); return; }
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act, id = el.dataset.id;
  try {
    if (act === "drawer-close") { closeDrawer(); return; }
    if (act === "player") { e.preventDefault(); e.stopPropagation(); return drawerPlayer(id); }
    if (act === "market") { e.preventDefault(); e.stopPropagation(); return drawerMarket(id); }
    if (act === "lb-open") { if (e.target.closest("a,input,label")) return; return drawerPlayer(id); }
    if (act === "pshow") return showPlayer(id);
    if (act === "pnl-period") {
      state.pnlPeriod[id] = el.dataset.v;
      const box = $("#" + id), c = state.cards[id];
      if (box && c) box.innerHTML = pnlChartHtml(c, el.dataset.v, { id, height: box.closest(".in-drawer") ? 140 : 180 });
      return;
    }
    if (act === "padd") {
      el.disabled = true;
      const r = await addPlayers([id], el.dataset.v);
      if (r.ok && $("#drawer") && !$("#drawer").hidden) drawerPlayer(id);
      if ($("#finder-out")) {
        await showPlayer(id);
        const job = (r.added || [])[0];
        if (job && job.job_id) followCheck(job.job_id);
      } else if (location.hash.startsWith("#/whales")) render();
      return;
    }
    if (act === "bulk-add") {
      const ws = [...state.picked];
      if (!ws.length) return;
      if (ws.length > 10) {
        const ok = await confirmDialog({ title: `Добавить ${ws.length} ${plural(ws.length, "игрока", "игроков", "игроков")}?`, ok: "Добавить",
          text: "Каждого проверим в фоне — по два одновременно, так что вся пачка займёт время. Данные начнут собираться только по этим игрокам." });
        if (!ok) return;
      }
      await addPlayers(ws, el.dataset.v);
      state.picked.clear(); syncBulk();
      return loadLeaderboard(0);
    }
    if (act === "bulk-clear") { state.picked.clear(); $$("[data-act=lb-pick]").forEach((c) => { c.checked = false; }); return syncBulk(); }
  } catch (err) { toast(err.message); el.disabled = false; }
}, true);

document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset.act === "lb-pick") { el.checked ? state.picked.add(el.dataset.id) : state.picked.delete(el.dataset.id); syncBulk(); }
  if (el.dataset.act === "lb-pick-page") {
    $$("[data-act=lb-pick]").forEach((c) => { c.checked = el.checked; el.checked ? state.picked.add(c.dataset.id) : state.picked.delete(c.dataset.id); });
    syncBulk();
  }
});

document.addEventListener("submit", (e) => {
  if (e.target.id === "finder") { e.preventDefault(); findPlayer($("#finder-q").value); }
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && closeDrawer()) { e.preventDefault(); e.stopImmediatePropagation(); }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".lb-row[data-act=lb-open]")) { e.preventDefault(); drawerPlayer(e.target.dataset.id); }
}, true);
window.addEventListener("hashchange", () => { closeDrawer(); const p = $("#pop"); if (p) p.hidden = true; });
