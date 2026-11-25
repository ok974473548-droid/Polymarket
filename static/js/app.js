// Entry point: session, layout and router.
import { $, loadSession, onRoute, render, route, state, toast } from "./core.js";
import { footer, header, hotStrip, mobileBar, setupPage } from "./layout.js";
import { homePage } from "./pages/home.js";
import { tradersPage } from "./pages/traders.js";
import { traderPage } from "./pages/trader.js";
import { eventPage } from "./pages/event.js";
import { marketsPage, tagPage, tagsPage, watchlistPage } from "./pages/browse.js";
import { searchPage } from "./pages/search.js";
import { copyPage, copyWizard } from "./pages/copy.js";
import { portfolioPage } from "./pages/portfolio.js";
import { apiPage } from "./pages/api.js";
import { autopilotPage } from "./pages/autopilot.js";
import { adminPage, joinPage } from "./pages/admin.js";
import { stopLive } from "./livechart.js";

const TITLES = { "": "Аналитика Polymarket", traders: "Трейдеры", markets: "Рынки", "copy-trading": "Копирование", portfolio: "Портфель",
  watchlist: "Избранное", tags: "Теги", search: "Поиск", api: "API", autopilot: "Автопилот",
  admin: "Пользователи", join: "Регистрация" };
let lastPath = "";

function page(main) {
  const { parts } = route();
  const [a, b] = parts;
  if (state.setup && a !== "setup") return setupPage(main);
  switch (a) {
    case undefined: return homePage(main);
    case "setup": return state.setup ? setupPage(main) : homePage(main);
    case "traders": return b && /^0x[0-9a-fA-F]{40}$/.test(b) ? traderPage(main, b) : tradersPage(main);
    case "event": return b ? eventPage(main, b) : marketsPage(main);
    case "markets": return marketsPage(main);
    case "tags": return b ? tagPage(main, b) : tagsPage(main);
    case "watchlist": return watchlistPage(main);
    case "search": return searchPage(main);
    case "copy-trading": return b === "new" ? copyWizard(main) : copyPage(main);
    case "portfolio": return portfolioPage(main);
    case "autopilot": return autopilotPage(main);
    case "api": return apiPage(main);
    case "admin": return adminPage(main);
    case "join": return joinPage(main);
    default:
      main.innerHTML = `<div class="gate"><h2>Страница не найдена</h2><p>Такого раздела нет.</p><a class="btn btn-primary" href="/">На главную</a></div>`;
  }
}

function draw() {
  dirty = false;
  stopLive();
  const { parts, path } = route();
  document.title = (TITLES[parts[0] || ""] || "Контур рынка") + " · Контур рынка";
  header();
  mobileBar();
  hotStrip();
  const main = $("#main");
  main.onclick = null;
  if (path !== lastPath) scrollTo(0, 0);
  lastPath = path;
  Promise.resolve(page(main)).catch((e) => {
    console.error(e);
    main.innerHTML = `<div class="error-box"><b>Ошибка страницы</b><p>${String(e.message || e)}</p></div>`;
  });
}

// ------------------------------------------------------------ live refresh without flicker
// The page is rebuilt out of sight under a snapshot of itself; once its data has arrived it takes the snapshot's
// place, at the same scroll position (rows opened under a trade stay open: trades.js remembers them).
let quietBusy = false;
// Someone who started typing or choosing on the page (an order ticket, a form) keeps it as it is until they leave.
let dirty = false;
const markDirty = (e) => {
  if (e.target.closest("#main") && (e.type !== "click" || e.target.closest(".ticket button, form button"))) dirty = true;
};
for (const type of ["input", "change", "click"]) document.addEventListener(type, markDirty, true);
const settled = (main, ms) => new Promise((resolve) => {
  const t0 = Date.now();
  const check = () => (!main.querySelector(".sk-block, .sk") && Date.now() - t0 > 120) || Date.now() - t0 > ms ? resolve() : setTimeout(check, 60);
  check();
});
async function quietDraw() {
  const main = $("#main");
  const busy = document.activeElement && main.contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
  if (quietBusy || document.hidden || busy || dirty || document.querySelector("dialog[open]") || state.setup) return;
  quietBusy = true;
  const y = scrollY;
  const lefts = [...main.querySelectorAll(".tablewrap, .tabs, .chips")].map((e) => e.scrollLeft);
  const shot = main.cloneNode(true);
  shot.removeAttribute("id");
  shot.querySelectorAll("[id]").forEach((e) => e.removeAttribute("id"));
  const from = main.querySelectorAll("canvas"), to = shot.querySelectorAll("canvas");
  to.forEach((c, i) => {
    try {
      c.getContext("2d").drawImage(from[i], 0, 0);
    } catch {
      /* an empty chart for a moment is fine */
    }
  });
  shot.classList.add("live-shot");
  shot.inert = true;
  shot.setAttribute("aria-hidden", "true");
  main.after(shot);
  main.style.top = shot.offsetTop + "px";
  main.classList.add("live-hidden");
  try {
    await Promise.race([Promise.resolve(page(main)).catch(() => {}), new Promise((r) => setTimeout(r, 8000))]);
    await settled(main, 6000);
  } finally {
    main.classList.remove("live-hidden");
    main.style.top = "";
    shot.remove();
    scrollTo(0, y);
    main.querySelectorAll(".tablewrap, .tabs, .chips").forEach((e, i) => lefts[i] && (e.scrollLeft = lefts[i]));
    quietBusy = false;
  }
}
state.quietRefresh = quietDraw;

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-retry]")) render();
});

state.onAuthLost = async () => {
  const had = !!state.user;
  await loadSession().catch(() => {});
  if (had && !state.user) {
    toast("Сеанс завершён. Войдите снова.", "bad");
    render();
  }
};

onRoute(draw);
footer();
loadSession()
  .catch(() => toast("Сервер недоступен", "bad"))
  .finally(draw);
