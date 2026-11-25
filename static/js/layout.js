// Header, hot-traders strip, footer, mobile bar, search box, notifications, account menu, sign-in.
import "./palette.js";
import { $, $$, api, ago, avatar, compact, esc, go, icon, loadSession, modal, render, route, state, thumb, toast, unwrap, usd } from "./core.js";

export function brand() {
  return `<a href="/" class="brand" aria-label="Контур рынка — на главную"><span class="brand-mark">${icon("logo", 20)}</span><span class="brand-text"><small>Polymarket</small><b>Контур</b></span></a>`;
}

const NAV = [
  ["/traders", "Трейдеры"],
  ["/markets", "Рынки"],
  ["/copy-trading", "Копирование", '<span class="hot-badge">ХИТ</span>'],
  ["/autopilot", "Автопилот", '<span class="hot-badge new">NEW</span>'],
  ["/api", "API"],
];

export function header() {
  const cur = "/" + (route().parts[0] || "");
  const u = state.user, a = state.account;
  $("#top").innerHTML = `<div class="wrap top-row">
    <button class="icon-btn nav-burger" data-mobile-menu aria-label="Меню">${icon("menu", 20)}</button>
    ${brand()}
    <button class="search-trigger" data-palette aria-label="Умный поиск: рынки, трейдеры, теги">${icon("search", 16)}<span>Поиск рынков и трейдеров</span></button>
    <nav class="main-nav" aria-label="Основная навигация">
      ${NAV.map(([href, label, extra]) => `<a href="${href}" class="${cur === href ? "cur" : ""}">${label}${extra || ""}</a>`).join("")}
      <div class="dropdown more-menu">
        <button class="nav-btn" aria-haspopup="true" aria-expanded="false">Ещё ${icon("down", 13)}</button>
        <div class="menu" role="menu"><a href="/portfolio" role="menuitem">${icon("wallet", 15)} Портфель</a><a href="/tags" role="menuitem">${icon("tag", 15)} Теги</a></div>
      </div>
    </nav>
    <div class="top-actions">
      <a href="/watchlist" class="watch-link ${cur === "/watchlist" ? "cur" : ""}">${icon("star", 15)}<span>Избранное</span></a>
      <button class="search-box" data-palette aria-label="Умный поиск (Ctrl+K)">
        ${icon("search", 15)}<span>Поиск рынков, трейдеров…</span><kbd>${/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl"} K</kbd>
      </button>
      ${u ? `<a href="/portfolio" class="pf-chip"><small>Портфель</small><b>${usd(a?.total ?? 0)}</b></a>
        <button class="btn btn-primary btn-sm" data-deposit>Пополнить</button>
        <div class="dropdown bell">
          <button class="icon-btn bell-btn" aria-label="Уведомления" aria-haspopup="true">${icon("bell", 18)}${state.unread ? `<span class="dot">${state.unread > 9 ? "9+" : state.unread}</span>` : ""}</button>
          <div class="menu bell-menu" role="menu"></div>
        </div>
        <div class="dropdown acct">
          <button class="acct-btn" aria-haspopup="true" aria-label="Меню аккаунта">${avatar("0x" + (u.username || "").padEnd(4, "0").slice(0, 4).split("").map((c) => c.charCodeAt(0).toString(16)).join("").slice(0, 4), "", u.username, 26)}${icon("down", 13)}</button>
          <div class="menu" role="menu">
            <div class="menu-head"><b>${esc(u.username)}</b><small>Виртуальный счёт · ${usd(a?.cash ?? 0)} свободно</small></div>
            <a href="/portfolio" role="menuitem">${icon("wallet", 15)} Портфель</a>
            <a href="/copy-trading" role="menuitem">${icon("copy", 15)} Копирование</a>
            <a href="/autopilot" role="menuitem">${icon("pilot", 15)} Автопилот</a>
            ${u.is_admin ? `<a href="/admin" role="menuitem">${icon("users", 15)} Пользователи</a>` : ""}
            <button role="menuitem" data-password>${icon("key", 15)} Сменить пароль</button>
            <button role="menuitem" class="danger" data-logout>${icon("logout", 15)} Выйти</button>
          </div>
        </div>`
      : `<button class="btn btn-primary btn-sm" data-login>Войти</button>`}
    </div>
  </div>`;
}

export function footer() {
  $("#foot").innerHTML = `<div class="wrap foot-grid">
    <div class="foot-brand">${brand()}<p>Практическая аналитика рынков прогнозов.</p></div>
    <div><h3>Данные Polymarket</h3><a href="/traders">Трейдеры</a><a href="/markets">Рынки</a><a href="/tags">Теги</a></div>
    <div><h3>Ресурсы</h3><a href="/api">API</a><a href="/search">Поиск</a></div>
    <div><h3>Аккаунт</h3><a href="/portfolio">Портфель</a><a href="/copy-trading">Копирование</a><a href="/autopilot">Автопилот</a><a href="/watchlist">Избранное</a></div>
  </div>
  <div class="wrap foot-copy">© ${new Date().getFullYear()} Контур рынка · Данные Polymarket и DefiLlama. Торговля виртуальная, ключи кошелька не нужны.</div>`;
}

export function mobileBar() {
  const cur = "/" + (route().parts[0] || "");
  const items = [["/", "Главная", "home"], ["/markets", "Рынки", "chart"], ["/traders", "Трейдеры", "users"],
    ["/autopilot", "Автопилот", "pilot"], ["/portfolio", "Портфель", "wallet"], ["#menu", "Меню", "menu"]];
  $("#mobile").innerHTML = items.map(([href, label, ic]) => href === "#menu"
    ? `<button data-mobile-menu>${icon(ic, 20)}<span>${label}</span></button>`
    : `<a href="${href}" class="${cur === href ? "cur" : ""}">${icon(ic, 20)}<span>${label}</span></a>`).join("");
}

// ------------------------------------------------------------ hot traders strip
const HOT_PAGES = new Set(["", "traders", "watchlist", "portfolio"]);
let hotCache = null, hotAt = 0;
export async function hotStrip() {
  const host = $("#hotstrip");
  const page = route().parts[0] || "";
  if (!HOT_PAGES.has(page)) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  if (!hotCache || Date.now() - hotAt > 120000) {
    try {
      hotCache = await api("/api/hot");
      hotAt = Date.now();
    } catch {
      hotCache = hotCache || [];
    }
  }
  if (!hotCache.length) {
    host.innerHTML = `<div class="wrap hs-row"><a class="hs-label" href="/traders">${icon("fire", 14)} Популярные трейдеры ${icon("right", 12)}</a><span class="muted small">Рейтинг загружается…</span></div>`;
    return;
  }
  const item = (t) => `<a class="hs-item" href="/traders/${t.wallet}"><small>#${t.hot_rank}</small>${avatar(t.wallet, t.image, t.name, 18)}<b>${esc(t.name || t.wallet.slice(0, 6) + "…")}</b>${t.pct_15d != null ? `<span class="${t.pct_15d >= 0 ? "up" : "down"}">${t.pct_15d >= 0 ? "▲" : "▼"} ${Math.abs(t.pct_15d).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%</span>` : `<span class="up">${compact(t.pnl)}</span>`}</a>`;
  const run = hotCache.map(item).join("");
  const sig = hotCache.map((t) => t.wallet + t.pct_15d).join();
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.innerHTML = `<div class="wrap hs-row"><a class="hs-label" href="/traders">${icon("fire", 14)} Популярные трейдеры ${icon("right", 12)}</a><div class="hs-viewport"><div class="hs-track"><div class="hs-run">${run}</div><div class="hs-run" aria-hidden="true">${run}</div></div></div></div>`;
  $$('.hs-run[aria-hidden="true"] a', host).forEach((a) => (a.tabIndex = -1));
}

// ------------------------------------------------------------ dropdowns
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".dropdown > button");
  const open = $$(".dropdown.open");
  for (const d of open) if (!btn || d !== btn.parentElement) {
    d.classList.remove("open");
    d.querySelector("button")?.setAttribute("aria-expanded", "false");
  }
  if (btn) {
    const d = btn.parentElement;
    const on = d.classList.toggle("open");
    btn.setAttribute("aria-expanded", on);
    if (on && d.classList.contains("bell")) loadBell(d.querySelector(".bell-menu"));
  } else if (e.target.closest(".menu a, .menu button")) {
    for (const d of $$(".dropdown.open")) d.classList.remove("open");
  }
});

async function loadBell(box) {
  box.innerHTML = '<div class="sp-note">Загрузка…</div>';
  try {
    const n = await api("/api/notifications");
    box.innerHTML = `<div class="menu-head"><b>Уведомления</b></div>` + (n.rows.length
      ? n.rows.slice(0, 20).map((r) => `<a href="${esc(r.link || "/copy-trading")}" class="note ${r.read ? "" : "unread"}"><b>${esc(r.title)}</b><span>${esc(r.body)}</span><small>${ago(r.at)}</small></a>`).join("")
      : '<div class="sp-note">Пока нет уведомлений. Здесь появятся сработавшие копии сделок.</div>');
    if (n.unread) {
      await api("/api/notifications/read", {});
      state.unread = 0;
      $(".bell-btn .dot")?.remove();
    }
  } catch (err) {
    box.innerHTML = `<div class="sp-note">${esc(err.message)}</div>`;
  }
}

// Notifications: pushed by the server over /api/stream (SSE) the moment they appear; a slow poll stays as a fallback.
let lastSeen = 0;
let live = null;
let refreshTimer = 0;
const USER_PAGES = ["portfolio", "copy-trading", "autopilot"];
// Public pages read prices that change all the time: refreshed on a timer while the tab is visible (seconds).
const PUBLIC_EVERY = { "": 60, markets: 60, traders: 60, tags: 60, watchlist: 60 };   // an event page is live by itself (/api/live)
let lastQuiet = Date.now();

function refreshSoon(pages = USER_PAGES) {
  // The person's data changed (a copy, a fill, new prices, a payout, the roster): redraw the open page in place.
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    updateChip();
    if (!pages.includes(route().parts[0] || "")) return;
    lastQuiet = Date.now();
    state.quietRefresh?.();
  }, 700);
}

async function updateChip() {
  // Portfolio total in the header, without redrawing the header (an open menu stays open).
  try {
    await loadSession();
    const chip = document.querySelector(".pf-chip b");
    if (chip && state.account) chip.textContent = usd(state.account.total ?? 0);
  } catch {
    /* next event */
  }
}

function connectLive() {
  if (live || !state.user || !window.EventSource) return;
  live = new EventSource("/api/stream");
  live.addEventListener("notify", () => checkNotifications());
  for (const kind of ["copy", "fill", "account", "prices"]) live.addEventListener(kind, () => refreshSoon());
  live.addEventListener("autopilot", () => refreshSoon(["autopilot"]));
  live.onerror = () => {
    if (!state.user && live) {
      live.close();
      live = null;
    }
  };
}

setInterval(() => {
  // public pages on a timer; personal pages too when the live connection is down
  const page = route().parts[0] || "";
  const every = PUBLIC_EVERY[page] ?? (USER_PAGES.includes(page) && (!live || live.readyState !== 1) ? 30 : null);
  if (every && Date.now() - lastQuiet >= every * 1000 && !document.hidden) {
    lastQuiet = Date.now();
    state.quietRefresh?.();
  }
}, 5000);

document.addEventListener("visibilitychange", () => {
  // back to the tab: catch up at once instead of waiting for the next change
  if (document.hidden || Date.now() - lastQuiet < 10000) return;
  lastQuiet = Date.now();
  updateChip();
  state.quietRefresh?.();
});

setInterval(() => {
  connectLive();
  if (!live || live.readyState !== 1) checkNotifications();
}, 30000);
setTimeout(connectLive, 1500);

async function checkNotifications() {
  if (!state.user || document.hidden) return;
  try {
    const n = await api("/api/notifications");
    const fresh = n.rows.filter((r) => !r.read && r.id > lastSeen);
    if (lastSeen) for (const r of fresh.slice(0, 3).reverse()) toast(r.title + " — " + r.body);
    lastSeen = Math.max(lastSeen, ...n.rows.map((r) => r.id), 0);
    if (n.unread !== state.unread) {
      state.unread = n.unread;
      const btn = $(".bell-btn");
      btn?.querySelector(".dot")?.remove();
      if (btn && n.unread) btn.insertAdjacentHTML("beforeend", `<span class="dot">${n.unread > 9 ? "9+" : n.unread}</span>`);
    }
  } catch { /* offline */ }
}

// ------------------------------------------------------------ account actions
document.addEventListener("click", async (e) => {
  if (e.target.closest("[data-login]")) return loginDialog();
  if (e.target.closest("[data-logout]")) {
    await api("/api/auth/logout", {});
    await loadSession();
    toast("Вы вышли из аккаунта");
    go("/");
  }
  if (e.target.closest("[data-password]")) return passwordDialog();
  const dep = e.target.closest("[data-deposit]");
  if (dep) return moneyDialog("deposit", dep.dataset.account);
  const wd = e.target.closest("[data-withdraw]");
  if (wd) return moneyDialog("withdraw", wd.dataset.account);
  if (e.target.closest("[data-mobile-menu]")) return mobileMenu();
});

export function loginDialog(reason = "") {
  if (state.setup) return go("/setup");
  const d = modal("Вход в Контур рынка", `${reason ? `<p class="muted">${esc(reason)}</p>` : ""}
    <form class="form" id="login-form">
      <label>Логин<input name="username" autocomplete="username" required></label>
      <label>Пароль<input name="password" type="password" autocomplete="current-password" required></label>
      <p class="form-error" hidden></p>
      <button class="btn btn-primary btn-block">Войти</button>
      <p class="muted small">Нет аккаунта? Попросите у администратора ссылку-приглашение.</p>
    </form>`);
  const f = $("#login-form", d);
  f.username.focus();
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/auth/login", { username: f.username.value, password: f.password.value });
      await loadSession();
      d.close();
      toast("Добро пожаловать, " + state.user.username);
      render();
    } catch (err) {
      const p = $(".form-error", f);
      p.textContent = err.message;
      p.hidden = false;
    }
  };
}
state.requireLogin = loginDialog;

function passwordDialog() {
  const d = modal("Смена пароля", `<form class="form" id="pw-form">
    <label>Текущий пароль<input name="current" type="password" autocomplete="current-password" required></label>
    <label>Новый пароль<input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
    <label>Повторите новый пароль<input name="again" type="password" autocomplete="new-password" minlength="8" required></label>
    <p class="form-error" hidden></p>
    <button class="btn btn-primary btn-block">Сохранить пароль</button></form>`);
  const f = $("#pw-form", d);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = $(".form-error", f);
    if (f.password.value !== f.again.value) {
      err.textContent = "Пароли не совпадают";
      err.hidden = false;
      return;
    }
    try {
      await api("/api/auth/password", { current: f.current.value, password: f.password.value });
      d.close();
      toast("Пароль изменён. Другие сеансы завершены.");
    } catch (e2) {
      err.textContent = e2.message;
      err.hidden = false;
    }
  };
}

export function moneyDialog(kind, accountId) {
  if (!state.user) return loginDialog();
  const deposit = kind === "deposit";
  const d = modal(deposit ? "Пополнить виртуальный счёт" : "Вывести с виртуального счёта", `
    <p class="muted">${deposit ? "Добавьте виртуальные доллары на счёт. Реальные деньги не используются." : "Свободные средства будут списаны с виртуального счёта."}</p>
    <form class="form" id="money-form">
      <label>Сумма, $<input name="amount" type="number" min="1" step="0.01" inputmode="decimal" required></label>
      <div class="chips">${[100, 500, 1000, 5000].map((v) => `<button type="button" class="chip" data-v="${v}">$${v.toLocaleString("ru-RU")}</button>`).join("")}</div>
      <p class="form-error" hidden></p>
      <button class="btn btn-primary btn-block">${deposit ? "Пополнить" : "Вывести"}</button>
    </form>`);
  const f = $("#money-form", d);
  f.amount.focus();
  f.addEventListener("click", (e) => {
    const c = e.target.closest("[data-v]");
    if (c) f.amount.value = c.dataset.v;
  });
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/account/" + kind, { id: accountId ? Number(accountId) : undefined, amount: Number(f.amount.value) });
      d.close();
      await loadSession();
      toast(deposit ? "Счёт пополнен" : "Средства выведены");
      render();
    } catch (err) {
      const p = $(".form-error", f);
      p.textContent = err.message;
      p.hidden = false;
    }
  };
}

function mobileMenu() {
  const u = state.user;
  const d = modal("Меню", `<nav class="mobile-menu">
    <a href="/markets">${icon("chart", 18)} Рынки</a><a href="/traders">${icon("users", 18)} Трейдеры</a>
    <a href="/copy-trading">${icon("copy", 18)} Копирование</a><a href="/autopilot">${icon("pilot", 18)} Автопилот</a>
    <a href="/portfolio">${icon("wallet", 18)} Портфель</a><a href="/search">${icon("search", 18)} Поиск</a>
    <a href="/watchlist">${icon("star", 18)} Избранное</a><a href="/tags">${icon("tag", 18)} Теги</a><a href="/api">${icon("api", 18)} API</a>
    ${u?.is_admin ? `<a href="/admin">${icon("users", 18)} Пользователи</a>` : ""}
    ${u ? `<button data-password>${icon("key", 18)} Сменить пароль</button><button data-logout class="danger">${icon("logout", 18)} Выйти</button>` : `<button data-login>${icon("user", 18)} Войти</button>`}
  </nav>`);
  d.addEventListener("click", (e) => e.target.closest("a, button[data-login], button[data-logout], button[data-password]") && d.close());
}

// ------------------------------------------------------------ setup screen (first start)
export function setupPage(main) {
  main.innerHTML = `<div class="auth-page">
    <div class="auth-card">
      ${brand()}
      <h1>Создайте учётную запись</h1>
      <p class="muted">Первый запуск. Придумайте логин и пароль — с ними вы будете входить в портфель, копирование и избранное. На счёт будет зачислено $1 000 виртуальных долларов.</p>
      <form class="form" id="setup-form">
        <label>Логин<input name="username" autocomplete="username" minlength="3" required placeholder="например, trader"></label>
        <label>Пароль<input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
        <label>Повторите пароль<input name="again" type="password" autocomplete="new-password" minlength="8" required></label>
        <p class="form-error" hidden></p>
        <button class="btn btn-primary btn-block">Создать и войти</button>
      </form>
    </div></div>`;
  const f = $("#setup-form", main);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = $(".form-error", f);
    if (f.password.value !== f.again.value) {
      err.textContent = "Пароли не совпадают";
      err.hidden = false;
      return;
    }
    try {
      await api("/api/auth/setup", { username: f.username.value, password: f.password.value });
      await loadSession();
      toast("Учётная запись создана");
      go("/");
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  };
}

export function needLogin(main, title, text) {
  main.innerHTML = `<div class="gate"><span class="gate-ic">${icon("user", 22)}</span><h2>${esc(title)}</h2><p>${esc(text)}</p><button class="btn btn-primary" data-login>Войти</button></div>`;
}
