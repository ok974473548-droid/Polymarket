// Entry point: session, layout and router.
import { $, loadSession, onRoute, render, route, state, toast } from "./core.js";
import { footer, header, hotStrip, mobileBar, setupPage } from "./layout.js";
import { homePage } from "./pages/home.js";
import { tradersPage } from "./pages/traders.js";
import { traderPage } from "./pages/trader.js";
import { eventPage } from "./pages/event.js";
import { marketsPage, searchPage, tagPage, tagsPage, watchlistPage } from "./pages/browse.js";
import { copyPage, copyWizard } from "./pages/copy.js";
import { portfolioPage } from "./pages/portfolio.js";
import { apiPage } from "./pages/api.js";
import { autopilotPage } from "./pages/autopilot.js";
import { adminPage, joinPage } from "./pages/admin.js";

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
