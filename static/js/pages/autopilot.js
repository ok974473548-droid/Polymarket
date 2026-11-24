// Autopilot: auto-selected roster copied from one budget, bench with probation, automatic replacement.
import { $, $$, delaySummary, ago, api, avatar, cents, confirmBox, dateTime, emptyBox, errorBox, esc, go, icon, loadSession, modal, num, route,
  short, signedUsd, state, toast, usd, withQuery, yesNo } from "../core.js";
import { activityTable, betsTable, bindRows, loadExit } from "../trades.js";
import { header, needLogin } from "../layout.js";

const TABS = [["roster", "Состав"], ["bench", "Скамейка"], ["candidates", "Кандидаты"], ["bets", "Ставки"], ["activity", "Активность"],
  ["log", "Журнал"], ["history", "Проверка на истории"]];
let refresh = 0;

const p100 = (v, d = 0) => (v == null || !isFinite(v) ? "—" : (v * 100).toFixed(d).replace(".", ",") + "%");
const sp100 = (v, d = 1) => (v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + (v * 100).toFixed(d).replace(".", ",") + "%");
const days = (ts) => (ts ? Math.max(0, Math.floor((Date.now() / 1000 - ts) / 86400)) : 0);
const playerName = (p) => p.name || short(p.wallet);
const tone = (v, good, bad) => (v == null ? "" : v >= good ? "up" : v < bad ? "down" : "");

export async function autopilotPage(main) {
  clearInterval(refresh);
  if (!state.user) return needLogin(main, "Автопилот", "Войдите, чтобы автопилот подбирал игроков и копировал их ставки на виртуальный счёт.");
  const q = route().q;
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "roster";
  main.innerHTML = '<div class="sk-block tall"></div>';
  let d;
  try {
    d = await api("/api/autopilot");
  } catch (e) {
    main.innerHTML = errorBox(e);
    return;
  }
  if (!d.account) return intro(main, d);
  const s = d.settings, a = d.account, st = d.stats;
  const copySize = d.share * s.copy_pct / 100;
  main.innerHTML = `<div class="ap-head"><div><h1>${icon("pilot", 22)} Автопилот</h1>
      <p class="muted">Сам подбирает игроков, копирует их ставки из общего бюджета и заменяет отстающих лучшими со скамейки.</p></div>
      <div class="ap-controls"><label class="toggle ap-switch"><input type="checkbox" id="ap-on" ${d.enabled ? "checked" : ""}><span></span><b>${d.enabled ? "Работает" : "Выключен"}</b></label>
        <button class="btn" id="ap-settings">${icon("sliders", 15)} Настройки</button></div></div>
    <section class="card ap-summary">
      <div class="ap-budget"><small>Бюджет автопилота</small><b class="ap-total">${usd(a.total)}</b>
        <span class="${a.profit >= 0 ? "up" : "down"} small">${signedUsd(a.profit)} за всё время</span>
        <div class="cb-actions"><button class="btn btn-sm" id="ap-transfer">${icon("arrow", 14)} Перевести с основного</button>
          <button class="btn btn-sm" data-deposit data-account="${a.id}">${icon("plus", 14)} Пополнить</button>
          <button class="btn btn-sm" data-withdraw data-account="${a.id}">${icon("bank", 14)} Вывести</button></div></div>
      <dl class="ap-kpis">
        <div><dt>Выиграно копий</dt><dd class="${tone(st.wr, s.min_wr / 100, 0.5)}">${p100(st.wr)}</dd><small>${st.closed ? `${st.wins} из ${st.closed} решённых` : "пока нет решённых"} · цель ${s.min_wr}–${Math.min(95, s.min_wr + 10)}%</small></div>
        <div><dt>Ожидание по составу</dt><dd>${p100(st.expected_wr)}</dd><small>средняя доля выигрышей игроков по истории</small></div>
        <div><dt>Свободно / в ставках</dt><dd>${usd(a.cash)}</dd><small>${usd(a.positions)} в ${num(st.open)} открытых копиях</small></div>
        <div><dt>Доля игрока · копия</dt><dd>${usd(d.share)}</dd><small>одна копия ${usd(copySize)} (${num(s.copy_pct)}% доли)</small></div>
      </dl>
      <p class="ap-status small muted">${icon("clock", 13)} ${d.reviewed ? "Пересмотр состава " + ago(d.reviewed) : "Первый пересмотр начнётся в течение минуты"} ·
        проверено игроков ${num(d.scout.profiled)} из ${num(d.scout.total)} · замен за сутки ${d.swaps_today} из ${s.max_swaps_per_day}
        <button class="link-btn" id="ap-review">Пересмотреть сейчас</button></p></section>
    <nav class="tabs">${TABS.map(([k, l]) => `<a href="${withQuery({ tab: k === "roster" ? null : k, status: null })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${l}${
      k === "roster" ? ` <span class="count">${d.players.active.length}/${s.active_count}</span>` : k === "bench" ? ` <span class="count">${d.players.bench.length}/${s.bench_size}</span>` : ""}</a>`).join("")}</nav>
    <div id="ap-tab"></div>`;
  $("#ap-on").onchange = async (e) => {
    try {
      await api("/api/autopilot/enable", { on: e.target.checked });
      toast(e.target.checked ? "Автопилот включён" : "Автопилот выключен: новые копии не делаются");
      autopilotPage(main);
    } catch (err) {
      toast(err.message, "bad");
    }
  };
  $("#ap-settings").onclick = () => settingsDialog(d, () => autopilotPage(main));
  $("#ap-transfer").onclick = () => transferDialog(a, () => autopilotPage(main));
  $("#ap-review").onclick = async () => {
    await api("/api/autopilot/review", {});
    toast("Пересмотр запущен, обновлю страницу через полминуты");
    setTimeout(() => location.pathname === "/autopilot" && autopilotPage(main), 30000);
  };
  const host = $("#ap-tab");
  ({ roster: rosterTab, bench: benchTab, candidates: candidatesTab, bets: betsTab, activity: activityTab, log: logTab, history: historyTab })[tab](host, d, main);
  refresh = setInterval(() => {
    if (location.pathname !== "/autopilot") return clearInterval(refresh);
    if (!document.hidden && !document.querySelector("dialog[open]") && ["roster", "bench"].includes(tab)) autopilotPage(main);
  }, 60000);
  host.addEventListener("click", (e) => onPlayerAction(e, main));
}

// ------------------------------------------------------------------ first run
function intro(main, d) {
  main.innerHTML = `<div class="ap-intro"><span class="gate-ic big">${icon("pilot", 28)}</span>
    <h1>Автопилот копирования</h1>
    <p>Система сама находит трейдеров Polymarket, чьи ставки выгодно повторять, держит из них состав и меняет тех, кто начал проигрывать. Цель — 70–80% выигранных копий при положительной доходности.</p>
    <ol class="ap-steps">
      <li><b>Отбор по истории</b><span>Для каждого кандидата считается, как часто выиграли бы ваши копии его ставок с задержкой и комиссией. Учитываются и проигрыши, которые Polymarket оставляет «открытыми».</span></li>
      <li><b>Испытание на скамейке</b><span>Новые игроки две недели проверяются без денег на своих свежих ставках.</span></li>
      <li><b>Состав и замены</b><span>${d.defaults.active_count} игроков делят общий бюджет. Серия проигрышей, падение доли выигрышей или убыток — и место занимает лучший со скамейки.</span></li></ol>
    <button class="btn btn-primary btn-lg" id="ap-start">${icon("bolt", 16)} Включить автопилот</button>
    <p class="muted small">Деньги виртуальные. Поиск проверяет тысячи кошельков из рейтингов Polymarket. Игроков с 70%+ выигранных копий немного, поэтому состав заполняется постепенно: первые — через полчаса-час, полностью — за несколько часов.</p></div>`;
  $("#ap-start").onclick = () => enableDialog(d, () => autopilotPage(main));
}

function enableDialog(d, done) {
  const cash = state.account?.cash || 0;
  const dl = modal("Включить автопилот", `<form class="form" id="en-form">
    <p class="muted">Автопилот работает на отдельном бюджете. Переведите на него деньги с основного счёта (свободно ${usd(cash)}).</p>
    <label>Бюджет, $<input name="budget" type="number" min="0" step="0.01" value="${Math.floor(cash * 0.5)}"></label>
    <div class="chips">${[25, 50, 100].map((v) => `<button type="button" class="chip" data-p="${v}">${v === 100 ? "Всё" : v + "%"}</button>`).join("")}</div>
    <label>Игроков в составе<select name="count">${[3, 5, 8, 10, 12, 15, 20].map((v) => `<option ${v === d.settings.active_count ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <p class="muted small">Остальные параметры можно поменять в настройках. Копия ставки — ${d.settings.copy_pct}% доли игрока.</p>
    <p class="form-error" hidden></p><button class="btn btn-primary btn-block">Включить</button></form>`);
  const f = $("#en-form", dl);
  f.addEventListener("click", (e) => {
    const c = e.target.closest("[data-p]");
    if (c) f.budget.value = Math.floor(cash * Number(c.dataset.p)) / 100;
  });
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/autopilot/settings", { active_count: Number(f.count.value) });
      await api("/api/autopilot/enable", { on: true, budget: Number(f.budget.value) || 0 });
      dl.close();
      await loadSession();
      header();
      toast("Автопилот включён. Идёт поиск игроков", "good");
      done();
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}

function transferDialog(a, done) {
  const cash = state.account?.cash || 0;
  const dl = modal("Перевод в автопилот", `<form class="form" id="tr-form"><p class="muted">Свободно на основном счёте: <b>${usd(cash)}</b></p>
    <label>Сумма, $<input name="amount" type="number" min="0.01" step="0.01" required></label>
    <div class="chips">${[25, 50, 100].map((v) => `<button type="button" class="chip" data-p="${v}">${v === 100 ? "Всё" : v + "%"}</button>`).join("")}</div>
    <p class="form-error" hidden></p><button class="btn btn-primary btn-block">Перевести</button></form>`);
  const f = $("#tr-form", dl);
  f.addEventListener("click", (e) => {
    const c = e.target.closest("[data-p]");
    if (c) f.amount.value = Math.floor(cash * Number(c.dataset.p)) / 100;
  });
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/account/transfer", { id: a.id, amount: Number(f.amount.value) });
      dl.close();
      await loadSession();
      header();
      toast("Бюджет автопилота пополнен");
      done();
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}

// ------------------------------------------------------------------ settings
const GROUPS = [
  ["Состав и деньги", [
    ["active_count", "Игроков в составе", "", 1], ["bench_size", "Мест на скамейке", "", 1],
    ["copy_pct", "Копия ставки, % доли игрока", "%", 1], ["event_cap_pct", "Лимит на одно событие, % бюджета", "%", 1]]],
  ["Отбор игроков", [
    ["min_wr", "Доля выигрышей копий, от", "%", 1], ["min_lb", "Надёжная граница (Уилсон), от", "%", 1],
    ["min_bets", "Решённых ставок за 180 дней, от", "", 1], ["min_roi", "Доходность на ставку, от", "%", 0.5],
    ["min_edge", "Преимущество над ценой, от", "$", 0.005], ["max_top_share", "Доля прибыли от лучшей ставки, до", "%", 1],
    ["min_recent", "Новых ставок за 14 дней, от", "", 1], ["max_slip", "Проскальзывание через 60 с, до", "$", 0.005]]],
  ["Что копировать", [
    ["price_min", "Цена ставки, от", "$", 0.01], ["price_max", "Цена ставки, до", "$", 0.01],
    ["min_leader_trade", "Сделка игрока, от", "$", 1], ["max_days_to_end", "До разрешения рынка, дней (0 — без лимита)", "", 1],
    ["copy_slip", "Цена копии хуже цены игрока, не больше", "$", 0.005]]],
  ["Испытание на скамейке", [
    ["probation_days", "Срок испытания, дней", "", 1], ["probation_min_bets", "Новых ставок за испытание, от", "", 1],
    ["allow_early", "Брать досрочно, если готовых нет", "bool"], ["early_days", "Досрочно — после дней на скамейке", "", 1],
    ["bootstrap", "Первый набор состава без испытания", "bool"]]],
  ["Замена отстающих", [
    ["loss_streak", "Проигрышей подряд", "", 1], ["recent_min_wr", "Доля выигрышей последних 20 копий, ниже", "%", 1],
    ["max_drawdown_pct", "Убыток копий игрока, % его доли", "%", 1], ["inactive_days", "Нет новых ставок, дней", "", 1],
    ["rotation", "Ротация на более сильных со скамейки", "bool"], ["rotation_margin", "Ротация, если сильнее на баллов", "", 1],
    ["min_tenure_days", "Ротация не раньше, дней в составе", "", 1], ["max_swaps_per_day", "Замен в сутки, не больше", "", 1],
    ["cooldown_days", "Выбывший может вернуться через, дней", "", 1]]],
];

function settingsDialog(d, done) {
  const s = d.settings;
  const dl = modal("Настройки автопилота", `<form class="form ap-settings" id="set-form">
    ${GROUPS.map(([title, items]) => `<fieldset><legend>${title}</legend><div class="set-grid">${items.map(([key, label, unit, step]) => unit === "bool"
      ? `<label class="toggle set-bool"><input type="checkbox" name="${key}" ${s[key] ? "checked" : ""}><span></span> ${label}</label>`
      : `<label>${label}<div class="input-unit">${unit === "$" ? "<i>$</i>" : ""}<input name="${key}" type="number" step="${step}" value="${s[key]}"
          title="По умолчанию ${d.defaults[key]}">${unit === "%" ? "<i>%</i>" : ""}</div></label>`).join("")}</div></fieldset>`).join("")}
    <p class="form-error" hidden></p>
    <div class="modal-actions"><button type="button" class="btn" id="set-reset">Сбросить по умолчанию</button><button class="btn btn-primary">Сохранить</button></div></form>`, { wide: true });
  const f = $("#set-form", dl);
  const err = (m) => {
    const el = $(".form-error", f);
    el.textContent = m;
    el.hidden = false;
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const body = {};
    for (const [, items] of GROUPS) for (const [key, , unit] of items) body[key] = unit === "bool" ? f[key].checked : Number(f[key].value);
    try {
      await api("/api/autopilot/settings", body);
      dl.close();
      toast("Настройки сохранены. Состав пересмотрится в течение минуты");
      done();
    } catch (ex) {
      err(ex.message);
    }
  };
  $("#set-reset", f).onclick = async () => {
    if (!(await confirmBox("Сбросить настройки?", "Все параметры вернутся к значениям по умолчанию.", "Сбросить"))) return;
    await api("/api/autopilot/settings", { reset: true });
    dl.close();
    done();
  };
}

// ------------------------------------------------------------------ player cards
function scoreRing(score) {
  const v = Math.max(0, Math.min(100, score || 0));
  return `<span class="ring" style="--v:${v}" title="Балл игрока">${score == null ? "—" : Math.round(v)}</span>`;
}

function activeCard(c, s) {
  const pr = c.profile || {}, cp = c.copies || {};
  const used = c.share ? Math.min(100, (c.used / c.share) * 100) : 0;
  return `<article class="card pl-card ${c.warnings?.length ? "warn" : ""}">
    <div class="pl-head">${avatar(c.wallet, c.image, c.name, 40)}<div class="pl-id"><a href="/traders/${c.wallet}"><b>${esc(playerName(c))}</b></a>
      <small>${esc(c.kind_label || "")} · в составе ${days(c.active_since)} дн.</small>
      <div class="pl-badges">${c.pinned ? `<span class="badge badge-gold">${icon("pin", 12)} Закреплён</span>` : ""}${c.early ? '<span class="badge badge-gray">Досрочно</span>' : ""}${c.manual ? '<span class="badge badge-gray">Вручную</span>' : ""}</div></div>
      ${scoreRing(c.score)}</div>
    <dl class="pl-stats">
      <div><dt>Выигрыши копий по истории</dt><dd>${p100(pr.wr)} <small>надёжно ≥ ${p100(pr.lb)}</small></dd></div>
      <div><dt>Доходность на ставку</dt><dd class="${tone(pr.roi, 0.0001, 0)}">${sp100(pr.roi)}</dd></div>
      <div><dt>Решённых ставок</dt><dd>${num(pr.n)} <small>ср. вход ${cents(pr.avg_entry)}</small></dd></div>
      <div><dt>Копии автопилота</dt><dd>${cp.closed ? `${cp.wins}–${cp.losses} <small>${p100(cp.wr)}</small>` : '<small class="muted">ещё нет решённых</small>'}</dd></div>
      <div><dt>Результат копий</dt><dd class="${(cp.pnl || 0) >= 0 ? "up" : "down"}">${signedUsd(cp.pnl || 0)}</dd></div>
      <div><dt>Занято доли</dt><dd><div class="share-bar"><i style="width:${used}%"></i></div><small>${usd(c.used)} из ${usd(c.share)}</small></dd></div></dl>
    ${c.warnings?.length ? `<div class="pl-warn">${icon("alert", 14)} ${c.warnings.map(esc).join(" · ")}</div>` : ""}
    <div class="pl-actions"><button class="btn btn-sm" data-detail="${c.wallet}">${icon("info", 14)} Почему выбран</button>
      <button class="btn btn-sm" data-act="${c.pinned ? "unpin" : "pin"}" data-w="${c.wallet}">${icon("pin", 14)} ${c.pinned ? "Открепить" : "Закрепить"}</button>
      <button class="btn btn-sm" data-act="replace" data-w="${c.wallet}">${icon("swap", 14)} Заменить</button>
      <button class="btn btn-sm btn-danger-ghost" data-act="ban" data-w="${c.wallet}" title="Исключить навсегда">${icon("ban", 14)}</button></div></article>`;
}

function benchCard(c, s) {
  const pr = c.probation || {}, sh = pr.shadow || {}, pf = c.profile || {};
  const dayPct = Math.min(100, ((pr.days || 0) / (pr.need_days || s.probation_days)) * 100);
  const betPct = Math.min(100, ((sh.new || 0) / (pr.need_bets || s.probation_min_bets)) * 100);
  const label = pr.ready ? '<span class="badge badge-green">Готов войти в состав</span>' : pr.early_ok ? '<span class="badge badge-gold">Можно досрочно</span>' : '<span class="badge badge-gray">Испытание</span>';
  return `<article class="card pl-card bench">
    <div class="pl-head">${avatar(c.wallet, c.image, c.name, 40)}<div class="pl-id"><a href="/traders/${c.wallet}"><b>${esc(playerName(c))}</b></a>
      <small>${esc(c.kind_label || "")} · на скамейке ${days(c.bench_since)} дн.</small><div class="pl-badges">${label}${c.manual ? '<span class="badge badge-gray">Вручную</span>' : ""}</div></div>
      ${scoreRing(c.score)}</div>
    <div class="probation"><div><span>Срок</span><div class="share-bar"><i style="width:${dayPct}%"></i></div><small>${Math.floor(pr.days || 0)} из ${pr.need_days || s.probation_days} дн.</small></div>
      <div><span>Новые ставки</span><div class="share-bar"><i style="width:${betPct}%"></i></div><small>${sh.new || 0} из ${pr.need_bets || s.probation_min_bets}</small></div></div>
    <dl class="pl-stats">
      <div><dt>По истории</dt><dd>${p100(pf.wr)} <small>надёжно ≥ ${p100(pf.lb)}</small></dd></div>
      <div><dt>На испытании</dt><dd>${sh.decided ? `${sh.wins} из ${sh.decided} <small>${p100(sh.wr)}</small>` : '<small class="muted">нет решённых</small>'}</dd></div>
      <div><dt>Теневые копии</dt><dd class="${tone(sh.roi_all, 0.0001, 0)}">${sp100(sh.roi_all)} <small>${sh.open || 0} открытых</small></dd></div></dl>
    <div class="pl-actions"><button class="btn btn-sm" data-detail="${c.wallet}">${icon("info", 14)} Подробнее</button>
      <button class="btn btn-sm" data-act="promote" data-w="${c.wallet}">${icon("arrow", 14)} В состав сейчас</button>
      <button class="btn btn-sm" data-act="remove" data-w="${c.wallet}">${icon("x", 14)} Убрать</button>
      <button class="btn btn-sm btn-danger-ghost" data-act="ban" data-w="${c.wallet}" title="Исключить навсегда">${icon("ban", 14)}</button></div></article>`;
}

function rosterTab(host, d) {
  const s = d.settings, act = d.players.active, ret = d.players.retired;
  const empty = s.active_count - act.length;
  host.innerHTML = `${!d.enabled ? `<p class="note-line">${icon("pause", 14)} Автопилот выключен: состав не меняется, новые ставки не копируются.</p>` : ""}
    <div class="pl-grid">${act.map((c) => activeCard(c, s)).join("")}
      ${empty > 0 ? Array.from({ length: Math.min(empty, 4) }, () => `<article class="card pl-card empty"><span>${icon("users", 22)}</span><b>Свободное место</b>
        <small>${d.players.bench.some((b) => b.probation?.ready || b.probation?.early_ok) ? "Займёт лучший со скамейки при следующем пересмотре" :
          d.scout.profiled < 20 ? "Идёт проверка кандидатов…" : "Ждём, пока кто-то пройдёт испытание"}</small></article>`).join("") : ""}</div>
    ${ret.length ? `<h2 class="sub-h">Недавно выбыли</h2><div class="tablewrap"><table class="table"><thead><tr><th>Игрок</th><th>Причина</th><th class="num">Копии</th><th class="num">Результат</th><th>Может вернуться</th><th></th></tr></thead><tbody>
      ${ret.slice(0, 12).map((c) => `<tr><td><a class="trader-cell" href="/traders/${c.wallet}">${avatar(c.wallet, c.image, c.name, 24)}<span>${esc(playerName(c))}</span></a></td>
        <td class="small">${esc(c.reason)}</td><td class="num">${c.copies?.closed ? `${c.copies.wins}–${c.copies.losses}` : "—"}</td>
        <td class="num ${(c.copies?.pnl || 0) >= 0 ? "up" : "down"}">${c.copies ? signedUsd(c.copies.pnl) : "—"}</td>
        <td class="small">${c.until && c.until > Date.now() / 1000 ? new Date(c.until * 1000).toLocaleDateString("ru-RU") : "сейчас"}</td>
        <td class="num"><button class="btn btn-sm" data-detail="${c.wallet}">Подробнее</button></td></tr>`).join("")}</tbody></table></div>` : ""}`;
}

function benchTab(host, d) {
  const s = d.settings, bench = d.players.bench, banned = d.players.banned;
  host.innerHTML = `<p class="note-line">${icon("info", 14)} На скамейке игроки ${s.probation_days} дней проверяются без денег: их новые ставки оцениваются как копии. Прошедшие испытание занимают места выбывших.</p>
    ${bench.length ? `<div class="pl-grid">${bench.map((c) => benchCard(c, s)).join("")}</div>` : emptyBox("Скамейка пуста", d.scout.profiled < 20 ? "Фоновый поиск ещё проверяет кандидатов." : "Сейчас нет свободных кандидатов, прошедших отбор. Ослабьте пороги в настройках или добавьте игрока вручную во вкладке «Кандидаты».")}
    ${banned.length ? `<h2 class="sub-h">Исключённые вручную</h2><div class="list-cards">${banned.map((c) => `<div class="row-card">${avatar(c.wallet, c.image, c.name, 32)}<span><b>${esc(playerName(c))}</b><small>${short(c.wallet)}</small></span>
      <button class="btn btn-sm" data-act="unban" data-w="${c.wallet}">Вернуть</button></div>`).join("")}</div>` : ""}`;
}

// ------------------------------------------------------------------ candidates
const CAND_SORTS = { score: (r) => r.score, wr: (r) => r.wr ?? -1, lb: (r) => r.lb ?? -1, roi: (r) => r.roi ?? -9, n: (r) => r.n, new: (r) => r.new_14d, slip: (r) => -(r.slip ?? 9) };

async function candidatesTab(host, d) {
  const q = route().q;
  const passed = q.get("passed") !== "0";
  const sort = CAND_SORTS[q.get("sort")] ? q.get("sort") : "score";
  host.innerHTML = `<div class="filters-row"><div class="search-inline">${icon("search", 15)}<input id="c-q" type="search" placeholder="Имя или адрес" value="${esc(q.get("q") || "")}" aria-label="Поиск игрока"></div>
      <label class="toggle"><input type="checkbox" id="c-passed" ${passed ? "checked" : ""}><span></span> Только прошедшие отбор</label>
      <form class="add-wallet" id="c-add"><input name="w" placeholder="Добавить по адресу 0x…" pattern="0x[0-9a-fA-F]{40}" required spellcheck="false" aria-label="Адрес игрока"><button class="btn btn-sm">${icon("plus", 14)} На скамейку</button></form></div>
    <p class="muted small" id="c-meta"></p><div class="tablewrap" id="c-table"><div class="sk-block tall"></div></div>`;
  let t = 0;
  $("#c-q").oninput = (e) => {
    clearTimeout(t);
    t = setTimeout(() => go(withQuery({ q: e.target.value.trim() || null }), { replace: true }), 400);
  };
  $("#c-passed").onchange = (e) => go(withQuery({ passed: e.target.checked ? null : "0" }));
  $("#c-add").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/autopilot/action", { wallet: e.target.w.value.trim(), action: "bench" });
      toast("Игрок добавлен на скамейку. Его история проверяется в фоне");
      go(withQuery({ tab: "bench" }));
    } catch (err) {
      toast(err.message, "bad");
    }
  };
  try {
    const res = await api(`/api/autopilot/candidates?passed=${passed ? 1 : 0}&q=${encodeURIComponent(q.get("q") || "")}`);
    const dir = q.get("dir") === "asc" ? 1 : -1;
    const rows = res.rows.slice().sort((a, b) => dir * (CAND_SORTS[sort](a) - CAND_SORTS[sort](b)));
    $("#c-meta").textContent = `Проверено игроков: ${num(res.scout.profiled)} из ${num(res.scout.total)} найденных в рейтингах Polymarket. Отбор пересчитывается с вашими порогами.`;
    const th = (key, label) => {
      const cur = sort === key;
      return `<th class="num"><a class="sort ${cur ? "cur" : ""}" href="${withQuery({ sort: key, dir: cur && dir < 0 ? "asc" : null })}" data-keep-scroll>${label}${cur ? `<span class="sort-ic">${dir < 0 ? "▼" : "▲"}</span>` : ""}</a></th>`;
    };
    const stateLabel = { active: '<span class="badge badge-green">В составе</span>', bench: '<span class="badge badge-gold">На скамейке</span>',
      retired: '<span class="badge badge-gray">Выбыл</span>', banned: '<span class="badge badge-red">Исключён</span>' };
    $("#c-table").innerHTML = rows.length ? `<table class="table table-tight"><thead><tr><th>Игрок</th><th>Тип</th>${th("n", "Ставок")}${th("wr", "Выигрыши")}${th("lb", "Надёжно")}${th("roi", "Доходность")}${th("new", "Новых 14 дн")}${th("slip", "Проскальз.")}${th("score", "Балл")}<th>Отбор</th><th></th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td><a class="trader-cell" href="/traders/${r.wallet}">${avatar(r.wallet, r.image, r.name, 26)}<span>${esc(playerName(r))}</span></a></td>
        <td class="small">${esc(r.kind_label)}</td><td class="num">${num(r.n)}</td><td class="num ${tone(r.wr, d.settings.min_wr / 100, 0.55)}">${p100(r.wr)}</td>
        <td class="num">${p100(r.lb)}</td><td class="num ${tone(r.roi, 0.0001, 0)}">${sp100(r.roi)}</td><td class="num">${num(r.new_14d)}</td>
        <td class="num">${r.slip == null ? "—" : cents(r.slip)}</td><td class="num"><b>${Math.round(r.score)}</b></td>
        <td class="small">${r.state ? stateLabel[r.state] : r.passed ? '<span class="badge badge-green">Проходит</span>' : `<span class="muted" title="${esc(r.failed.join(", "))}">${esc(r.failed.slice(0, 2).join(", "))}${r.failed.length > 2 ? "…" : ""}</span>`}</td>
        <td class="num nowrap"><button class="btn btn-sm" data-detail="${r.wallet}" title="Подробнее">${icon("info", 14)}<span class="btn-label">Подробнее</span></button>${!r.state ? ` <button class="btn btn-sm" data-act="bench" data-w="${r.wallet}" title="На скамейку">${icon("plus", 14)}<span class="btn-label">На скамейку</span></button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<div class="pad">${emptyBox(res.scout.profiled ? "Никто не проходит отбор" : "Кандидаты ещё проверяются", res.scout.profiled ? "Снимите флажок «Только прошедшие отбор», чтобы увидеть всех, или ослабьте пороги в настройках." : "Фоновый поиск читает рейтинги Polymarket и историю игроков. Первые результаты появятся через несколько минут.")}</div>`;
  } catch (e) {
    $("#c-table").innerHTML = errorBox(e, false);
  }
}

// ------------------------------------------------------------------ bets, activity, log
async function betsTab(host) {
  const q = route().q;
  const status = ["open", "closed"].includes(q.get("status")) ? q.get("status") : "";
  host.innerHTML = `<div class="filters-row"><div class="seg">${[["", "Все"], ["open", "Открытые"], ["closed", "Закрытые"]].map(([k, l]) =>
    `<a href="${withQuery({ status: k || null })}" class="${status === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div>
    <span class="muted small">Стоимость — по цене рынка сейчас, под ней — результат. К выигрышу — сколько вернётся, если исход сыграет. Нажмите на строку — подробности.</span></div>
    <div class="tablewrap" id="b-table"><div class="sk-block tall"></div></div>`;
  try {
    const rows = await api("/api/autopilot/bets?status=" + status);
    const box = $("#b-table");
    box.innerHTML = rows.length ? betsTable(rows, status) : `<div class="pad">${emptyBox("Ставок пока нет", "Копии появятся, когда игроки из состава сделают новые ставки.")}</div>`;
    bindRows(box, loadExit(rows));
  } catch (e) {
    $("#b-table").innerHTML = errorBox(e, false);
  }
}

async function activityTab(host, d) {
  const q = route().q;
  const status = ["filled", "skipped", "failed"].includes(q.get("status")) ? q.get("status") : "";
  host.innerHTML = `<div class="filters-row"><div class="seg">${[["", "Все"], ["filled", "Скопировано"], ["skipped", "Пропущено"], ["failed", "Ошибка"]].map(([k, l]) =>
    `<a href="${withQuery({ status: k || null })}" class="${status === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div>
    <span class="muted small">Каждая сделка игроков из состава: сколько вложил игрок и сколько мы. Нажмите на строку — подробности.</span></div><div class="tablewrap" id="act"><div class="sk-block tall"></div></div>`;
  try {
    const rows = await api("/api/autopilot/events?status=" + status);
    const box = $("#act");
    const slip = d?.settings?.copy_slip;
    box.innerHTML = rows.length ? `${delaySummary(rows) ? `<div class="pad-x">${delaySummary(rows)}</div>` : ""}${activityTable(rows, {
      who: "Игрок", filled: "Скопировано", slip: slip != null ? `до ${cents(slip)} за акцию от цены игрока` : "",
      settings: () => `<button class="btn btn-sm" type="button" data-open-settings>${icon("sliders", 14)} Изменить в настройках</button>` })}`
      : `<div class="pad">${emptyBox("Событий пока нет", "Здесь появятся сделки игроков из состава.")}</div>`;
    bindRows(box);
    box.addEventListener("click", (e) => e.target.closest("[data-open-settings]") && $("#ap-settings")?.click());
  } catch (e) {
    $("#act").innerHTML = errorBox(e, false);
  }
}

const LOG_ICONS = { enable: "play", disable: "pause", bench: "users", promote: "arrow", bootstrap: "bolt", pass: "check", fail: "x",
  replace: "swap", rotate: "swap", retire: "logout", ban: "ban", unban: "check", pin: "pin", unpin: "pin", settings: "sliders", manual: "plus" };

async function logTab(host) {
  host.innerHTML = '<div class="card ap-log" id="log"><div class="sk-block tall"></div></div>';
  try {
    const rows = await api("/api/autopilot/log");
    $("#log").innerHTML = rows.length ? rows.map((r) => `<div class="log-row log-${r.kind}"><span class="log-ic">${icon(LOG_ICONS[r.kind] || "info", 15)}</span>
      <div><p>${esc(r.text)}</p><small>${dateTime(r.at)}${r.wallet ? ` · <a href="/traders/${r.wallet}">профиль</a> · <button class="link-btn" data-detail="${r.wallet}">оценка</button>` : ""}</small></div></div>`).join("")
      : '<p class="muted pad">Журнал пуст. Здесь появятся входы в состав, испытания и замены с причинами.</p>';
  } catch (e) {
    $("#log").innerHTML = errorBox(e, false);
  }
}

// ------------------------------------------------------------------ history check
async function historyTab(host, d) {
  host.innerHTML = '<div class="sk-block tall"></div>';
  let res;
  try {
    res = (await api("/api/autopilot/backtest")).result;
  } catch (e) {
    host.innerHTML = errorBox(e, false);
    return;
  }
  const block = (title, x, note) => `<div class="bt-col"><h3>${title}</h3>${x && x.bets ? `<dl>
      <div><dt>Решённых ставок</dt><dd>${num(x.bets)}</dd></div><div><dt>Выиграно</dt><dd class="${tone(x.wr, d.settings.min_wr / 100, 0.55)}">${p100(x.wr, 1)}</dd></div>
      <div><dt>Доходность на ставку</dt><dd class="${tone(x.roi, 0.0001, 0)}">${sp100(x.roi)}</dd></div><div><dt>Ещё открыто</dt><dd>${num(x.open)} <small>${sp100(x.open_roi)} по рынку</small></dd></div></dl>` : '<p class="muted">Нет данных</p>'}<small class="muted">${note}</small></div>`;
  host.innerHTML = `<section class="card bt"><div class="list-head"><h2>Что было бы, если бы автопилот выбрал состав ${res ? res.days : 60} дней назад</h2>
      <button class="btn btn-sm" id="bt-run">${icon("clock", 14)} Пересчитать</button></div>
    <p class="muted">Профили считаются только по ставкам, решённым до той даты. Затем по вашим правилам выбирается ${d.settings.active_count} игроков и оценивается результат их ставок, сделанных после, — как копий с задержкой и комиссией.</p>
    ${res ? `<div class="bt-grid">${block("Состав по правилам", res.roster, `${res.players.length} игроков из ${res.eligible} прошедших отбор на ${new Date(res.as_of * 1000).toLocaleDateString("ru-RU")}`)}
      ${block("Все кандидаты без отбора", res.baseline, "Для сравнения: все проверенные игроки")}</div>
      ${res.players.length ? `<div class="tablewrap"><table class="table"><thead><tr><th>Игрок</th><th class="num">Балл тогда</th><th class="num">До: ставок</th><th class="num">До: выигрыши</th><th class="num">После: ставок</th><th class="num">После: выигрыши</th><th class="num">После: доходность</th></tr></thead><tbody>
        ${res.players.map((p) => `<tr><td><a class="trader-cell" href="/traders/${p.wallet}">${avatar(p.wallet, p.image, p.name, 24)}<span>${esc(playerName(p))}</span></a></td>
          <td class="num">${Math.round(p.score)}</td><td class="num">${num(p.before.n)}</td><td class="num">${p100(p.before.wr)}</td>
          <td class="num">${num(p.after?.bets)}</td><td class="num ${tone(p.after?.wr, d.settings.min_wr / 100, 0.55)}">${p100(p.after?.wr)}</td>
          <td class="num ${tone(p.after?.roi, 0.0001, 0)}">${sp100(p.after?.roi)}</td></tr>`).join("")}</tbody></table></div>` : ""}
      <p class="note-line">${icon("alert", 14)} Оценка завышена: кандидаты берутся из сегодняшних рейтингов, куда трейдеры попали в том числе благодаря успехам после этой даты. Решающая проверка — работа автопилота на виртуальных деньгах. Посчитано ${ago(res.now)}.</p>`
      : `<p class="muted">Проверка ещё не считалась. Для неё нужно хотя бы 20 проверенных игроков (сейчас ${num(d.scout.profiled)}).</p>`}</section>`;
  $("#bt-run").onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Считаю…";
    try {
      await api("/api/autopilot/backtest", {});
      historyTab(host, d);
    } catch (err) {
      toast(err.message, "bad");
      e.target.disabled = false;
    }
  };
}

// ------------------------------------------------------------------ player detail and actions
const ACTIONS = {
  pin: ["Закрепить игрока?", "Автопилот не будет заменять этого игрока, даже если он отстаёт.", "Закрепить", false],
  replace: ["Заменить игрока?", "Новые ставки игрока перестанут копироваться, открытые копии закроются вместе с его продажами. Место займёт лучший со скамейки.", "Заменить", true],
  ban: ["Исключить навсегда?", "Автопилот больше не будет выбирать этого игрока. Если он в составе, его новые ставки перестанут копироваться.", "Исключить", true],
  promote: ["Перевести в состав сейчас?", "Игрок войдёт в состав без завершённого испытания.", "Перевести", false],
  remove: ["Убрать со скамейки?", "Игрок вернётся в общий список кандидатов.", "Убрать", false],
};

async function onPlayerAction(e, main = $("#main")) {
  const det = e.target.closest("[data-detail]");
  if (det) return playerDialog(det.dataset.detail, main);
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const what = b.dataset.act;
  const ask = ACTIONS[what];
  if (ask && !(await confirmBox(ask[0], ask[1], ask[2], ask[3]))) return;
  try {
    await api("/api/autopilot/action", { wallet: b.dataset.w, action: what });
    toast({ pin: "Игрок закреплён", unpin: "Игрок откреплён", replace: "Игрок выведен из состава, замена — в течение минуты", ban: "Игрок исключён",
      unban: "Игрок снова может быть выбран", promote: "Игрок переведён в состав", remove: "Игрок убран со скамейки", bench: "Игрок на скамейке" }[what] || "Готово");
    if (location.pathname === "/autopilot") autopilotPage(main);
  } catch (err) {
    toast(err.message, "bad");
  }
}

function checkValue(c) {
  if (c.value == null) return "—";
  if (["wr", "lb", "roi", "top"].includes(c.key)) return String(c.value).replace(".", ",") + "%";
  if (["edge", "slip"].includes(c.key)) return "$" + Number(c.value).toFixed(3).replace(".", ",");
  return esc(String(c.value));
}

async function playerDialog(wallet, main = $("#main")) {
  const dl = modal("Оценка игрока", '<div class="sk-block tall"></div>', { wide: true });
  let p;
  try {
    p = await api("/api/autopilot/player?wallet=" + wallet);
  } catch (e) {
    $(".modal-body", dl).innerHTML = errorBox(e, false);
    return;
  }
  const m = p.metrics, v = p.verdict, f = p.facts || {};
  const body = $(".modal-body", dl);
  if (!m) {
    body.innerHTML = `<div class="pl-head">${avatar(wallet, p.image, p.name, 44)}<div class="pl-id"><b>${esc(p.name || short(wallet))}</b><small>${short(wallet)}</small></div></div>
      <p class="muted">История игрока ещё проверяется в фоне. Обычно это занимает несколько минут.</p>`;
    return;
  }
  const months = m.monthly || [];
  body.innerHTML = `<div class="pl-head">${avatar(wallet, p.image, p.name, 44)}<div class="pl-id"><a href="/traders/${wallet}"><b>${esc(p.name || short(wallet))}</b></a>
      <small>${esc(p.kind_label)} · проверен ${ago(p.profiled)}${m.partial ? " · история прочитана частично" : ""}</small></div>${scoreRing(v.score)}</div>
    <div class="verdict ${v.passed ? "ok" : "no"}">${v.passed ? icon("check", 16) + " Проходит отбор по вашим правилам" : icon("x", 16) + " Не проходит: " + esc(v.failed.join(", ").toLowerCase())}</div>
    <div class="checks">${v.checks.map((c) => `<div class="${c.ok ? "ok" : "no"}">${icon(c.ok ? "check" : "x", 14)}<span>${esc(c.label)}</span><b>${checkValue(c)}</b><small>${c.key === "type" ? "" : esc(c.need.replace(".", ","))}</small></div>`).join("")}</div>
    <h3 class="sub-h">Копии его ставок за 180 дней</h3>
    <dl class="pl-stats wide">
      <div><dt>Выигрыши</dt><dd>${m.wins} из ${m.n} <small>${p100(m.wr, 1)}</small></dd></div>
      <div><dt>Надёжная граница</dt><dd>${p100(m.lb, 1)}</dd></div>
      <div><dt>Доходность на ставку</dt><dd class="${tone(m.roi, 0.0001, 0)}">${sp100(m.roi)}</dd></div>
      <div><dt>Средний вход</dt><dd>${cents(m.avg_entry)} <small>преимущество ${m.edge == null ? "—" : "$" + m.edge.toFixed(3).replace(".", ",")}</small></dd></div>
      <div><dt>Коэф. прибыли</dt><dd>${m.pf == null ? "—" : m.pf.toFixed(2).replace(".", ",")}</dd></div>
      <div><dt>Макс. просадка</dt><dd>${m.max_dd.toFixed(1).replace(".", ",")} ставки</dd></div>
      <div><dt>Последние ${m.recent_n}</dt><dd>${p100(m.recent_wr)}</dd></div>
      <div><dt>Серия сейчас</dt><dd class="${m.streak >= 0 ? "up" : "down"}">${m.streak > 0 ? m.streak + " выигр." : m.streak < 0 ? -m.streak + " проигр." : "—"}</dd></div>
      <div><dt>Удержание</dt><dd>${m.hold_days == null ? "—" : m.hold_days.toFixed(1).replace(".", ",") + " дн."}</dd></div>
      <div><dt>Новых за 14 / 30 дн.</dt><dd>${m.new_14d} / ${m.new_30d}</dd></div>
      <div><dt>Проскальзывание</dt><dd>${m.slip_measured == null ? "не измерено" : cents(m.slip_measured)} <small>${m.slip_samples || 0} замеров</small></dd></div>
      <div><dt>Открыто сейчас</dt><dd>${m.open_n} <small>${sp100(m.open_r)}</small></dd></div></dl>
    ${months.length ? `<h3 class="sub-h">По месяцам</h3><div class="months">${months.map((x) => `<div title="${x.n} ставок"><i style="height:${Math.max(4, x.wr * 100)}%" class="${x.wr >= 0.7 ? "g" : x.wr >= 0.55 ? "y" : "r"}"></i><b>${p100(x.wr)}</b><small>${x.month.slice(5)}.${x.month.slice(2, 4)}</small></div>`).join("")}</div>` : ""}
    ${f.bets_total ? `<p class="muted small">Тип: на краткосрочных крипторынках ${p100(f.short_share)} ставок, рынков с позициями на обе стороны ${p100(f.both_sides)}, входов дороже $0,95 — ${p100(f.sniper_share)}, сделок в день ${num(f.trades_day, 1)}, продаж ${p100(f.sell_share)}.</p>` : ""}
    ${p.recent_bets?.length ? `<h3 class="sub-h">Последние ставки игрока</h3><div class="tablewrap"><table class="table"><thead><tr><th>Вход</th><th>Рынок</th><th class="num">Цена</th><th class="num">Итог</th></tr></thead><tbody>
      ${p.recent_bets.slice(0, 12).map((b) => `<tr><td class="small nowrap">${dateTime(b.opened)}</td><td><a href="/event/${esc(b.event_slug)}">${esc(b.title)}</a> <small class="muted">${esc(yesNo(b.outcome))}</small></td>
        <td class="num">${cents(b.entry)}</td><td class="num">${b.state === "open" ? `<span class="muted">идёт · ${cents(b.mark)}</span>` : b.exit > b.entry ? `<span class="up">${cents(b.exit)}</span>` : `<span class="down">${cents(b.exit)}</span>`}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="modal-actions">${p.card ? "" : `<button class="btn" data-act="bench" data-w="${wallet}">${icon("plus", 14)} На скамейку</button>`}
      <a class="btn" href="/traders/${wallet}">${icon("user", 14)} Профиль</a><button class="btn btn-primary" data-close>Закрыть</button></div>`;
  body.addEventListener("click", (e) => {
    if (e.target.closest("[data-act]")) {
      dl.close();
      onPlayerAction(e, main);
    }
  });
}

export { playerDialog };
