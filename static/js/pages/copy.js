// Copy trading, as on polymarketanalytics.com/copy-trading: profiles with their own balance, Deposit / Transfer
// from main / Withdraw, Pause all, tabs Copies / Positions / Activity / Settings and a 3-step wizard.
import { $, $$, delaySummary, api, avatar, cents, confirmBox, dateTime, emptyBox, errorBox, esc, go, icon, loadSession, modal, num, route,
  short, signedUsd, state, toast, usd, withQuery } from "../core.js";
import { activityTable, bindRows } from "../trades.js";
import { header, needLogin } from "../layout.js";
import { positionsTable } from "./portfolio.js";

const TABS = [["copies", "Копии"], ["positions", "Позиции"], ["activity", "Активность"], ["settings", "Настройки"]];
let hideBalance = false;
try {
  hideBalance = localStorage.getItem("kontur.hideCopy") === "1";
} catch {
  /* storage unavailable */
}

export async function copyPage(main) {
  if (!state.user) return needLogin(main, "Копирование сделок", "Войдите, чтобы копировать сделки трейдеров на виртуальный счёт.");
  const q = route().q;
  main.innerHTML = '<div class="sk-block tall"></div>';
  let profiles;
  try {
    profiles = await api("/api/copy/profiles");
  } catch (e) {
    main.innerHTML = errorBox(e);
    return;
  }
  if (!profiles.length) {
    main.innerHTML = `<div class="page-head"><h1>${icon("copy", 20)} Копирование сделок</h1><p>Автоматически повторяйте сделки выбранных трейдеров Polymarket на виртуальном счёте. Сделки лидеров проверяются каждые 30 секунд.</p></div>
      <div class="gate"><span class="gate-ic">${icon("users", 22)}</span><h2>Создайте профиль копирования</h2>
      <p>У профиля свой баланс и свои правила. Переведите на него деньги с основного счёта и добавьте трейдеров.</p>
      <button class="btn btn-primary" id="new-prof">${icon("plus", 15)} Новый профиль</button></div>
      ${howItWorks()}`;
    $("#new-prof").onclick = newProfile;
    return;
  }
  const p = profiles.find((x) => String(x.id) === q.get("profile")) || profiles[0];
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "copies";
  main.innerHTML = `<div class="copy-head"><h1>${icon("copy", 20)} Копирование</h1>
      <div class="dropdown prof-switch"><button class="btn" aria-haspopup="true">${icon("wallet", 15)} ${esc(p.name)} ${icon("down", 13)}</button>
        <div class="menu" role="menu">${profiles.map((x) => `<a href="${withQuery({ profile: x.id, tab: null })}" role="menuitem" class="${x.id === p.id ? "cur" : ""}">${esc(x.name)} <small>${usd(x.total)}</small></a>`).join("")}
          <button role="menuitem" id="new-prof">${icon("plus", 15)} Новый профиль</button></div></div>
      <a class="btn btn-primary" href="/copy-trading/new?account=${p.id}">${icon("plus", 15)} Копировать трейдера</a></div>
    <section class="card copy-balance">
      <div class="cb-main"><small>Баланс профиля ${p.paused ? '<span class="badge badge-red">Пауза</span>' : '<span class="badge badge-green">Активен</span>'}</small>
        <div class="cb-total"><b>${hideBalance ? "••••••" : usd(p.total)}</b><button class="icon-btn" id="cb-eye" aria-label="${hideBalance ? "Показать" : "Скрыть"} баланс">${icon(hideBalance ? "eye" : "eyeoff", 16)}</button></div>
        <div class="cb-actions"><button class="btn btn-sm btn-primary" data-deposit data-account="${p.id}">${icon("plus", 14)} Пополнить</button>
          <button class="btn btn-sm" id="cb-transfer">${icon("arrow", 14)} Перевести с основного</button>
          <button class="btn btn-sm" data-withdraw data-account="${p.id}">${icon("bank", 14)} Вывести</button></div></div>
      <dl class="cb-stats"><div><dt>Доступно</dt><dd>${hide(p.cash)}</dd></div><div><dt>В позициях</dt><dd>${hide(p.positions)}</dd></div>
        <div><dt>Нереализованный P&amp;L</dt><dd class="${p.unrealized >= 0 ? "up" : "down"}">${hideBalance ? "••••" : signedUsd(p.unrealized)}</dd></div>
        <div><dt>Прибыль профиля</dt><dd class="${p.profit >= 0 ? "up" : "down"}">${hideBalance ? "••••" : signedUsd(p.profit)}</dd></div></dl>
      <label class="toggle pause-all"><input type="checkbox" id="cb-pause" ${p.paused ? "checked" : ""}><span></span> Пауза для всех копий</label></section>
    <nav class="tabs">${TABS.map(([k, l]) => `<a href="${withQuery({ tab: k === "copies" ? null : k, status: null })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</nav>
    <div id="c-tab"></div>`;
  $("#new-prof").onclick = newProfile;
  $("#cb-eye").onclick = () => {
    hideBalance = !hideBalance;
    try {
      localStorage.setItem("kontur.hideCopy", hideBalance ? "1" : "0");
    } catch {
      /* ignore */
    }
    copyPage(main);
  };
  $("#cb-transfer").onclick = () => transferDialog(p, () => copyPage(main));
  $("#cb-pause").onchange = async (e) => {
    try {
      await api("/api/copy/profile/pause", { id: p.id, paused: e.target.checked });
      toast(e.target.checked ? "Копирование профиля на паузе" : "Копирование возобновлено");
      copyPage(main);
    } catch (err) {
      toast(err.message, "bad");
    }
  };
  const host = $("#c-tab");
  if (tab === "copies") copiesTab(host, p, main);
  else if (tab === "positions") positionsTabCopy(host, p);
  else if (tab === "activity") activityTab(host, p);
  else settingsTab(host, p, profiles);
}

const hide = (v) => (hideBalance ? "••••" : usd(v));

function howItWorks() {
  return `<section class="how"><h2>Как это работает</h2><ol>
    <li><b>Выберите трейдера</b><span>Адрес кошелька или трейдер из рейтинга.</span></li>
    <li><b>Задайте размер</b><span>Фиксированная сумма на сделку или процент от сделки лидера.</span></li>
    <li><b>Настройте правила</b><span>Лимиты, диапазон цен, проскальзывание и уведомления.</span></li></ol>
    <p class="muted small">Копируются только новые сделки лидера после запуска. Продажи лидера повторяются пропорционально. Комиссии — как на Polymarket.</p></section>`;
}

async function newProfile() {
  const d = modal("Новый профиль копирования", `<form class="form" id="np-form"><label>Название<input name="name" maxlength="40" required placeholder="Например, Политика"></label>
    <label>Перевести с основного счёта, $<input name="amount" type="number" min="0" step="0.01" placeholder="0"></label>
    <p class="muted small">Свободно на основном счёте: ${usd(state.account?.cash || 0)}</p><p class="form-error" hidden></p>
    <button class="btn btn-primary btn-block">Создать профиль</button></form>`);
  const f = $("#np-form", d);
  f.name.focus();
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const p = await api("/api/copy/profile", { name: f.name.value });
      if (Number(f.amount.value) > 0) await api("/api/account/transfer", { id: p.id, amount: Number(f.amount.value) });
      d.close();
      await loadSession();
      header();
      toast("Профиль создан");
      go("/copy-trading?profile=" + p.id);
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}

function transferDialog(p, done) {
  const d = modal("Перевод с основного счёта", `<form class="form" id="tr-form"><p class="muted">Свободно на основном счёте: <b>${usd(state.account?.cash || 0)}</b></p>
    <label>Сумма, $<input name="amount" type="number" min="0.01" step="0.01" required></label>
    <div class="chips">${[25, 50, 100].map((v) => `<button type="button" class="chip" data-p="${v}">${v === 100 ? "Всё" : v + "%"}</button>`).join("")}</div>
    <p class="form-error" hidden></p><button class="btn btn-primary btn-block">Перевести в «${esc(p.name)}»</button></form>`);
  const f = $("#tr-form", d);
  f.amount.focus();
  f.addEventListener("click", (e) => {
    const c = e.target.closest("[data-p]");
    if (c) f.amount.value = Math.floor(((state.account?.cash || 0) * Number(c.dataset.p)) / 100 * 100) / 100;
  });
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/account/transfer", { id: p.id, amount: Number(f.amount.value) });
      d.close();
      await loadSession();
      header();
      toast("Средства переведены");
      done();
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}

// ------------------------------------------------------------------ tabs
async function copiesTab(host, p, main) {
  host.innerHTML = '<div class="sk-block tall"></div>';
  let rows;
  try {
    rows = await api("/api/copy/trades?account=" + p.id);
  } catch (e) {
    host.innerHTML = errorBox(e, false);
    return;
  }
  if (!rows.length) {
    host.innerHTML = emptyBox("Нет копий", "Добавьте трейдера, чтобы начать копировать его сделки.", `<a class="btn btn-primary" href="/copy-trading/new?account=${p.id}">Копировать трейдера</a>`) + howItWorks();
    return;
  }
  host.innerHTML = `<div class="copy-list">${rows.map((c) => `<article class="card copy-item ${c.paused ? "paused" : ""}">
      <div class="ci-head"><a class="trader-cell" href="/traders/${c.leader}">${avatar(c.leader, c.leader_image, c.nickname || c.leader_name, 36)}<span><b>${esc(c.nickname || c.leader_name || short(c.leader))}</b><small>${short(c.leader)}</small></span></a>
        <span class="badge ${c.paused ? "badge-red" : "badge-green"}">${c.paused ? "Пауза" : "Активна"}</span></div>
      <dl class="ci-stats"><div><dt>Размер</dt><dd>${c.sizing === "fixed" ? usd(c.amount) + " на сделку" : num(c.percent) + "% от сделки"}</dd></div>
        <div><dt>Лимит на сделку</dt><dd>${c.max_per_trade ? usd(c.max_per_trade) : "Без лимита"}</dd></div>
        <div><dt>Дневной лимит</dt><dd>${c.daily_limit ? `${usd(c.spent_today)} / ${usd(c.daily_limit)}` : "Без лимита"}</dd></div>
        <div><dt>Цены</dt><dd>${c.price_min != null || c.price_max != null ? `${cents(c.price_min ?? 0)}–${cents(c.price_max ?? 1)}` : "Любые"}</dd></div>
        <div><dt>Проскальзывание</dt><dd>${c.slippage == null ? "Любое" : c.slippage === 0 ? "Точная цена" : (c.slippage * 100).toFixed(0) + "%"}</dd></div>
        <div><dt>Исполнено</dt><dd>${num(c.filled)} из ${num(c.events)}</dd></div>
        <div><dt>Потрачено / получено</dt><dd>${usd(c.spent)} / ${usd(c.received)}</dd></div>
        <div><dt>Проверка</dt><dd>${c.last_check ? dateTime(c.last_check) : "ожидает"}</dd></div></dl>
      ${c.last_error ? `<p class="form-error small">${icon("alert", 13)} ${esc(c.last_error)}</p>` : ""}
      <div class="ci-actions"><a class="btn btn-sm" href="/copy-trading/new?edit=${c.id}">${icon("sliders", 14)} Изменить</a>
        <button class="btn btn-sm" data-pause="${c.id}" data-on="${c.paused ? 0 : 1}">${icon(c.paused ? "play" : "pause", 14)} ${c.paused ? "Возобновить" : "Пауза"}</button>
        <button class="btn btn-sm btn-danger-ghost" data-del="${c.id}">${icon("trash", 14)} Удалить</button></div></article>`).join("")}</div>`;
  host.onclick = async (e) => {
    const pb = e.target.closest("[data-pause]");
    const db = e.target.closest("[data-del]");
    try {
      if (pb) {
        await api("/api/copy/trade/pause", { id: Number(pb.dataset.pause), paused: pb.dataset.on === "1" });
        toast(pb.dataset.on === "1" ? "Копия на паузе" : "Копия возобновлена");
        copyPage(main);
      } else if (db && (await confirmBox("Удалить копию?", "Новые сделки трейдера больше не будут копироваться. Открытые позиции останутся в профиле.", "Удалить", true))) {
        await api("/api/copy/trade/delete", { id: Number(db.dataset.del) });
        toast("Копия удалена");
        copyPage(main);
      }
    } catch (err) {
      toast(err.message, "bad");
    }
  };
}

async function positionsTabCopy(host, p) {
  host.innerHTML = '<div class="sk-block tall"></div>';
  try {
    const rows = await api(`/api/account/positions?id=${p.id}&status=open`);
    host.innerHTML = rows.length ? `<div class="tablewrap">${positionsTable(rows, p.id)}</div>` : emptyBox("Открытых позиций нет", "Позиции появятся, когда трейдер совершит сделку, которую профиль скопирует.");
  } catch (e) {
    host.innerHTML = errorBox(e, false);
  }
}

async function activityTab(host, p) {
  const q = route().q;
  const status = ["filled", "skipped", "failed"].includes(q.get("status")) ? q.get("status") : "";
  host.innerHTML = `<div class="filters-row"><div class="seg">${[["", "Все"], ["filled", "Исполнено"], ["skipped", "Пропущено"], ["failed", "Ошибка"]].map(([k, l]) =>
    `<a href="${withQuery({ status: k || null })}" class="${status === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div></div><div class="tablewrap" id="act"><div class="sk-block tall"></div></div>`;
  try {
    const rows = await api(`/api/copy/events?account=${p.id}&status=${status}`);
    const box = $("#act");
    box.innerHTML = rows.length ? `${delaySummary(rows) ? `<div class="pad-x">${delaySummary(rows)}</div>` : ""}${activityTable(rows, {
      who: "Трейдер", filled: "Исполнено",
      settings: (r) => `<a class="btn btn-sm" href="/copy-trading/new?edit=${r.copy_trade_id}">${icon("sliders", 14)} Изменить настройки копии</a>` })}`
      : '<p class="muted pad">Событий пока нет. Здесь появятся скопированные, пропущенные и неудачные сделки.</p>';
    bindRows(box);
  } catch (e) {
    $("#act").innerHTML = errorBox(e, false);
  }
}

function settingsTab(host, p) {
  host.innerHTML = `<section class="card settings"><h2>Название профиля</h2>
      <form class="form row-form" id="rn-form"><input name="name" maxlength="40" value="${esc(p.name)}" required aria-label="Название профиля"><button class="btn btn-primary">Сохранить</button></form></section>
    <section class="card settings"><h2>Вывести средства</h2><p class="muted">Свободные средства профиля (${usd(p.cash)}) вернутся на основной счёт.</p>
      <button class="btn" data-withdraw data-account="${p.id}">${icon("bank", 14)} Вывести на основной счёт</button></section>
    <section class="card settings danger-zone"><h2>Удалить профиль</h2><p class="muted">Все копии профиля будут остановлены, остаток вернётся на основной счёт. Сначала продайте открытые позиции.</p>
      <button class="btn btn-danger" id="del-prof">${icon("trash", 14)} Удалить профиль</button></section>`;
  $("#rn-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/copy/profile/rename", { id: p.id, name: e.target.name.value });
      toast("Профиль переименован");
      go(withQuery({}));
    } catch (err) {
      toast(err.message, "bad");
    }
  };
  $("#del-prof").onclick = async () => {
    if (!(await confirmBox("Удалить профиль?", `Профиль «${p.name}» и все его копии будут удалены.`, "Удалить", true))) return;
    try {
      await api("/api/copy/profile/delete", { id: p.id });
      await loadSession();
      header();
      toast("Профиль удалён");
      go("/copy-trading");
    } catch (err) {
      toast(err.message, "bad");
    }
  };
}

// ------------------------------------------------------------------ wizard
export async function copyWizard(main) {
  if (!state.user) return needLogin(main, "Копирование сделок", "Войдите, чтобы настроить копирование.");
  const q = route().q;
  let profiles = await api("/api/copy/profiles").catch(() => []);
  let editing = null;
  if (q.get("edit")) {
    try {
      editing = await api("/api/copy/trade?id=" + encodeURIComponent(q.get("edit")));
    } catch (e) {
      main.innerHTML = errorBox(e, false);
      return;
    }
  }
  const s = editing
    ? { leader: editing.leader, nickname: editing.nickname, account: editing.account, sizing: editing.sizing, amount: editing.amount ?? 10,
      percent: editing.percent ?? 25, max_per_trade: editing.max_per_trade, daily_limit: editing.daily_limit,
      price_min: editing.price_min ?? null, price_max: editing.price_max ?? null,
      slippage: editing.slippage == null ? "any" : String(Math.round(editing.slippage * 100)), notify: editing.notify }
    : { leader: (q.get("leader") || "").toLowerCase(), nickname: "", account: Number(q.get("account")) || profiles[0]?.id || null, sizing: "fixed", amount: 10, percent: 25,
      max_per_trade: null, daily_limit: null, price_min: null, price_max: null, slippage: "2", notify: true };
  let step = editing ? 2 : s.leader ? 2 : 1;
  let leaderInfo = null;

  async function loadLeader() {
    if (!/^0x[0-9a-f]{40}$/.test(s.leader)) return (leaderInfo = null);
    try {
      leaderInfo = await api("/api/trader?wallet=" + s.leader);
    } catch {
      leaderInfo = null;
    }
  }
  await loadLeader();

  function draw() {
    main.innerHTML = `<div class="wizard"><a class="back" href="/copy-trading${s.account ? "?profile=" + s.account : ""}">${icon("back", 14)} Копирование</a>
      <h1>${editing ? "Изменить копию" : "Копировать трейдера"}</h1>
      <ol class="steps">${["Трейдер", "Размер", "Правила"].map((l, i) => `<li class="${step === i + 1 ? "cur" : step > i + 1 ? "done" : ""}"><span>${step > i + 1 ? icon("check", 13) : i + 1}</span>${l}</li>`).join("")}</ol>
      <div class="wz-grid"><section class="card wz-body" id="wz-body"></section><aside class="card wz-summary" id="wz-sum"></aside></div></div>`;
    const body = $("#wz-body");
    if (step === 1) stepTrader(body);
    else if (step === 2) stepSizing(body);
    else stepRules(body);
    summary();
  }

  function summary() {
    const name = s.nickname || leaderInfo?.stats?.name || (s.leader ? short(s.leader) : "—");
    const prof = profiles.find((x) => x.id === s.account);
    const slip = s.slippage === "any" ? "Любое" : s.slippage === "0" ? "Точная цена" : s.slippage + "%";
    $("#wz-sum").innerHTML = `<h2>Сводка</h2><dl class="sum-list">
      <div><dt>Трейдер</dt><dd>${s.leader ? `${avatar(s.leader, leaderInfo?.stats?.image, name, 22)} ${esc(name)}` : "—"}</dd></div>
      <div><dt>Профиль</dt><dd>${prof ? esc(prof.name) + ` <small>${usd(prof.cash)} свободно</small>` : "—"}</dd></div>
      <div><dt>Размер</dt><dd>${s.sizing === "fixed" ? usd(Number(s.amount)) + " на сделку" : num(Number(s.percent)) + "% от сделки лидера"}</dd></div>
      <div><dt>Лимит на сделку</dt><dd>${s.max_per_trade ? usd(s.max_per_trade) : "Без лимита"}</dd></div>
      <div><dt>Дневной лимит</dt><dd>${s.daily_limit ? usd(s.daily_limit) : "Без лимита"}</dd></div>
      <div><dt>Диапазон цен</dt><dd>${s.price_min != null || s.price_max != null ? `${cents(s.price_min ?? 0)} – ${cents(s.price_max ?? 1)}` : "Любой"}</dd></div>
      <div><dt>Проскальзывание</dt><dd>${slip}</dd></div><div><dt>Уведомления</dt><dd>${s.notify ? "Включены" : "Выключены"}</dd></div></dl>
      ${leaderInfo ? `<div class="sum-leader"><span>PnL трейдера</span><b class="${(leaderInfo.overall?.pnl || 0) >= 0 ? "up" : "down"}">${leaderInfo.overall?.pnl != null ? signedUsd(leaderInfo.overall.pnl, 0) : "—"}</b>
        <span>Win rate</span><b>${leaderInfo.stats?.win_rate != null ? leaderInfo.stats.win_rate.toFixed(1) + "%" : "—"}</b></div>` : ""}`;
  }

  function stepTrader(body) {
    body.innerHTML = `<h2>1. Выберите трейдера</h2>
      <form class="form" id="wz1"><label>Адрес кошелька<input name="leader" value="${esc(s.leader)}" placeholder="0x…" pattern="0x[0-9a-fA-F]{40}" required spellcheck="false"></label>
      <div class="wz-pick"><span class="muted small">или выберите из рейтинга</span><div id="wz-top" class="wz-top">${'<div class="sk-line"></div>'.repeat(3)}</div></div>
      <label>Имя для копии (необязательно)<input name="nickname" maxlength="60" value="${esc(s.nickname)}" placeholder="Как подписать трейдера"></label>
      <label>Профиль копирования<select name="account">${profiles.map((x) => `<option value="${x.id}" ${x.id === s.account ? "selected" : ""}>${esc(x.name)} — ${usd(x.cash)} свободно</option>`).join("")}<option value="new">+ Новый профиль…</option></select></label>
      <p class="form-error" hidden></p><div class="wz-nav"><span></span><button class="btn btn-primary">Далее ${icon("right", 14)}</button></div></form>`;
    const f = $("#wz1");
    if (!profiles.length) f.account.value = "new";
    api("/api/traders?size=8").then((d) => {
      $("#wz-top").innerHTML = (d.rows || []).map((r) => `<button type="button" class="wz-trader ${r.wallet === s.leader ? "cur" : ""}" data-w="${r.wallet}" data-n="${esc(r.name || "")}">${avatar(r.wallet, r.image, r.name, 26)}<span>${esc(r.name || short(r.wallet))}</span><b class="${(r.pnl || 0) >= 0 ? "up" : "down"}">${r.pnl != null ? signedUsd(r.pnl, 0) : ""}</b></button>`).join("") || '<p class="muted small">Рейтинг ещё загружается.</p>';
    }).catch(() => ($("#wz-top").innerHTML = ""));
    f.addEventListener("click", (e) => {
      const b = e.target.closest("[data-w]");
      if (!b) return;
      f.leader.value = b.dataset.w;
      $$(".wz-trader", f).forEach((x) => x.classList.toggle("cur", x === b));
    });
    f.onsubmit = async (e) => {
      e.preventDefault();
      const err = $(".form-error", f);
      s.leader = f.leader.value.trim().toLowerCase();
      s.nickname = f.nickname.value.trim();
      try {
        if (f.account.value === "new") {
          const p = await api("/api/copy/profile", { name: s.nickname ? "Копии: " + s.nickname : "Профиль копирования" });
          profiles = await api("/api/copy/profiles");
          s.account = p.id;
        } else s.account = Number(f.account.value);
        await loadLeader();
        if (!leaderInfo) throw new Error("Не удалось найти трейдера с таким адресом");
        step = 2;
        draw();
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      }
    };
  }

  function stepSizing(body) {
    body.innerHTML = `<h2>2. Размер сделки</h2>
      <div class="sizing-opts" role="radiogroup">
        <button class="opt ${s.sizing === "fixed" ? "cur" : ""}" data-sz="fixed" role="radio" aria-checked="${s.sizing === "fixed"}">${icon("dollar", 18)}<b>Фиксированная сумма</b><span>Одна и та же сумма на каждую покупку лидера.</span></button>
        <button class="opt ${s.sizing === "percent" ? "cur" : ""}" data-sz="percent" role="radio" aria-checked="${s.sizing === "percent"}">${icon("percent", 18)}<b>Процент</b><span>Доля от суммы сделки лидера.</span></button></div>
      ${s.sizing === "fixed"
        ? `<label class="tk-label">Сумма на сделку</label><div class="tk-amount"><span>$</span><input id="sz-v" type="number" min="1.1" step="0.01" value="${esc(s.amount)}"></div>
           <div class="chips">${[10, 25, 50, 100].map((v) => `<button class="chip ${Number(s.amount) === v ? "cur" : ""}" data-v="${v}">$${v}</button>`).join("")}</div><p class="muted small">Минимум $1,1 — у Polymarket минимальная покупка $1 плюс комиссия.</p>`
        : `<label class="tk-label">Процент от сделки лидера</label><div class="tk-amount"><input id="sz-v" type="number" min="1" max="1000" step="1" value="${esc(s.percent)}"><span>%</span></div>
           <div class="chips">${[10, 25, 50, 100].map((v) => `<button class="chip ${Number(s.percent) === v ? "cur" : ""}" data-v="${v}">${v}%</button>`).join("")}</div><p class="muted small">Лидер купил на $1 000, при 10% вы купите на $100. Покупки меньше $1 пропускаются.</p>`}
      <p class="form-error" hidden></p>
      <div class="wz-nav">${editing ? "<span></span>" : `<button class="btn" id="wz-back">${icon("left", 14)} Назад</button>`}<button class="btn btn-primary" id="wz-next">Далее ${icon("right", 14)}</button></div>`;
    $$("[data-sz]", body).forEach((b) => (b.onclick = () => {
      s.sizing = b.dataset.sz;
      draw();
    }));
    const inp = $("#sz-v");
    const put = (v) => (s.sizing === "fixed" ? (s.amount = v) : (s.percent = v));
    inp.oninput = () => {
      put(inp.value);
      summary();
    };
    $$("[data-v]", body).forEach((b) => (b.onclick = () => {
      put(b.dataset.v);
      draw();
    }));
    $("#wz-back")?.addEventListener("click", () => {
      step = 1;
      draw();
    });
    $("#wz-next").onclick = () => {
      const v = Number(inp.value);
      const err = $(".form-error", body);
      const bad = s.sizing === "fixed" ? !(v >= 1.1) : !(v >= 1 && v <= 1000);
      if (bad) {
        err.textContent = s.sizing === "fixed" ? "Минимальная сумма — $1,1" : "Процент — от 1 до 1000";
        err.hidden = false;
        return;
      }
      step = 3;
      draw();
    };
  }

  function stepRules(body) {
    const ranges = [["any", "Любые", null, null], ["safe", "$0,05–0,95", 0.05, 0.95], ["mid", "$0,20–0,80", 0.2, 0.8], ["fav", "$0,50–0,99", 0.5, 0.99], ["long", "$0,01–0,30", 0.01, 0.3]];
    const curRange = ranges.find((r) => r[2] === s.price_min && r[3] === s.price_max)?.[0] || "custom";
    body.innerHTML = `<h2>3. Правила</h2><form class="form" id="wz3">
      <div class="two"><label>Лимит на сделку, $<input name="max" type="number" min="1" step="0.01" placeholder="Без лимита" value="${s.max_per_trade ?? ""}"><small class="muted">Максимум покупок в одном рынке за день</small></label>
      <label>Дневной лимит, $<input name="daily" type="number" min="1" step="0.01" placeholder="Без лимита" value="${s.daily_limit ?? ""}"><small class="muted">Максимум всех покупок копии за сутки (UTC)</small></label></div>
      <label class="tk-label">Диапазон цен</label><div class="chips">${ranges.map(([k, l]) => `<button type="button" class="chip ${curRange === k ? "cur" : ""}" data-range="${k}">${l}</button>`).join("")}<button type="button" class="chip ${curRange === "custom" ? "cur" : ""}" data-range="custom">Свой</button></div>
      <div class="two" id="custom-range" ${curRange === "custom" ? "" : "hidden"}><label>От, $<input name="pmin" type="number" min="0" max="1" step="0.01" value="${s.price_min ?? ""}"></label><label>До, $<input name="pmax" type="number" min="0" max="1" step="0.01" value="${s.price_max ?? ""}"></label></div>
      <small class="muted">Покупки лидера по цене вне диапазона пропускаются.</small>
      <label class="tk-label">Проскальзывание</label><div class="chips">${[["1", "1%"], ["2", "2%"], ["5", "5%"], ["10", "10%"], ["0", "Точная"], ["any", "Любое"]].map(([v, l]) => `<button type="button" class="chip ${s.slippage === v ? "cur" : ""}" data-slip="${v}">${l}</button>`).join("")}</div>
      <small class="muted">Насколько ваша цена может быть хуже цены лидера.</small>
      <label class="toggle"><input type="checkbox" name="notify" ${s.notify ? "checked" : ""}><span></span> Уведомлять о скопированных и неудачных сделках</label>
      <p class="form-error" hidden></p>
      <div class="wz-nav"><button type="button" class="btn" id="wz-back">${icon("left", 14)} Назад</button><button class="btn btn-primary">${editing ? "Сохранить" : "Начать копирование"}</button></div></form>`;
    const f = $("#wz3");
    const sync = () => {
      s.max_per_trade = f.max.value ? Number(f.max.value) : null;
      s.daily_limit = f.daily.value ? Number(f.daily.value) : null;
      s.notify = f.notify.checked;
      if (!$("#custom-range").hidden) {
        s.price_min = f.pmin.value === "" ? null : Number(f.pmin.value);
        s.price_max = f.pmax.value === "" ? null : Number(f.pmax.value);
      }
      summary();
    };
    f.addEventListener("input", sync);
    f.addEventListener("click", (e) => {
      const r = e.target.closest("[data-range]"), sl = e.target.closest("[data-slip]");
      if (r) {
        sync();
        const found = ranges.find((x) => x[0] === r.dataset.range);
        if (found) [s.price_min, s.price_max] = [found[2], found[3]];
        stepRules(body);
        if (r.dataset.range === "custom") $("#custom-range").hidden = false;
        summary();
      }
      if (sl) {
        sync();
        s.slippage = sl.dataset.slip;
        stepRules(body);
        summary();
      }
    });
    $("#wz-back").onclick = () => {
      sync();
      step = 2;
      draw();
    };
    f.onsubmit = async (e) => {
      e.preventDefault();
      sync();
      const payload = { account: s.account, leader: s.leader, nickname: s.nickname, sizing: s.sizing, amount: Number(s.amount), percent: Number(s.percent),
        max_per_trade: s.max_per_trade, daily_limit: s.daily_limit, price_min: s.price_min, price_max: s.price_max,
        slippage: s.slippage === "any" ? "any" : Number(s.slippage), notify: s.notify };
      try {
        if (editing) await api("/api/copy/trade/update", { id: editing.id, ...payload });
        else await api("/api/copy/trade", payload);
        toast(editing ? "Настройки копии сохранены" : "Копирование запущено. Новые сделки трейдера будут повторяться.", "good");
        go("/copy-trading?profile=" + s.account);
      } catch (ex) {
        const el = $(".form-error", f);
        el.textContent = ex.message;
        el.hidden = false;
      }
    };
  }

  draw();
}

