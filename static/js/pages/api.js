// Reference of the local JSON API with a "try it" runner for public GET routes.
import { $, esc, icon } from "../core.js";

const GROUPS = [
  ["Рынки", [
    ["GET", "/api/markets", "order=oi|volume|new|liquidity, tag, offset", "Активные события (по 20), как на странице «Рынки»", "/api/markets?order=oi"],
    ["GET", "/api/event", "slug", "Событие со всеми рынками, исходами, токенами, комиссиями и правилами", "/api/event?slug=presidential-election-winner-2028"],
    ["GET", "/api/prices", "token, window=1d|1w|1m|3m|ytd|1y|max, start, end", "История цены исхода: [[unix, цена 0..1], …]", ""],
    ["GET", "/api/holders", "condition", "Крупнейшие держатели по каждому исходу рынка", ""],
    ["GET", "/api/event-trades", "event_id, side=BUY|SELL, cursor", "Сделки события с метками трейдеров", ""],
    ["GET", "/api/news", "title", "Новости Google News и Bing News по событию", "/api/news?title=Bitcoin"],
    ["GET", "/api/search", "q", "Поиск событий, профилей и тегов", "/api/search?q=bitcoin"],
    ["GET", "/api/tags", "q", "Теги с числом активных событий", "/api/tags"],
    ["GET", "/api/home", "", "Объём за 24 ч, число рынков, популярные трейдеры, избранные события", "/api/home"],
  ]],
  ["Трейдеры", [
    ["GET", "/api/traders", "tab=leaderboard|whales|sports|crypto|politics, category, sort, dir=asc|desc, search, active=1, tag, *_min/*_max, page, size", "Рейтинг с фильтрами и сортировкой", "/api/traders?size=5"],
    ["GET", "/api/hot", "", "Популярные трейдеры по изменению PnL за 15 дней", "/api/hot"],
    ["GET", "/api/trader", "wallet", "Профиль: статистика, ранг, PnL, стоимость позиций", ""],
    ["GET", "/api/trader/pnl", "wallet, window=7d|1m|max", "Ряд PnL: общий и реализованный", ""],
    ["GET", "/api/trader/positions", "wallet, status=OPEN|CLOSED, sort, dir, cursor, title", "Позиции трейдера", ""],
    ["GET", "/api/trader/trades", "wallet, side, cursor", "Сделки трейдера", ""],
    ["GET", "/api/trader/categories", "wallet", "Ранги и статистика по категориям", ""],
  ]],
  ["Счёт и торговля (нужен вход)", [
    ["GET", "/api/account", "id", "Сводка счёта: деньги, позиции, прибыль, изменение за сутки", ""],
    ["GET", "/api/account/positions", "id, status=open|closed", "Позиции виртуального счёта", ""],
    ["GET", "/api/account/orders", "id, status=open|done", "Заявки", ""],
    ["POST", "/api/quote", "{condition, token, side, type, amount | shares, price}", "Предпросмотр исполнения по живому стакану", ""],
    ["POST", "/api/order", "{condition, token, side: buy|sell, type: market|limit, amount | shares, price, expires, account}", "Разместить заявку", ""],
    ["POST", "/api/order/cancel", "{id}", "Отменить лимитную заявку", ""],
    ["POST", "/api/account/deposit | withdraw | transfer", "{id, amount}", "Пополнение, вывод, перевод с основного счёта", ""],
  ]],
  ["Копирование (нужен вход)", [
    ["GET", "/api/copy/profiles", "", "Профили копирования с балансами", ""],
    ["GET", "/api/copy/trades", "account", "Копии профиля", ""],
    ["GET", "/api/copy/events", "account, status=filled|skipped|failed", "Журнал копирования", ""],
    ["POST", "/api/copy/trade", "{account, leader, sizing: fixed|percent, amount | percent, max_per_trade, daily_limit, price_min, price_max, slippage, notify}", "Начать копирование", ""],
    ["POST", "/api/copy/trade/update | pause | delete", "{id, …}", "Изменить, приостановить или удалить копию", ""],
  ]],
];

export function apiPage(main) {
  main.innerHTML = `<div class="page-head"><h1>${icon("api", 20)} API</h1>
    <p>Все данные приложения доступны локальным JSON API на этом же адресе. Ключ не нужен: публичные маршруты открыты, личные работают с cookie сеанса после входа. POST-запросы принимают JSON и проверяют заголовок Origin.</p></div>
    ${GROUPS.map(([title, rows]) => `<section class="card api-group"><h2>${esc(title)}</h2><div class="tablewrap"><table class="table api-table"><thead><tr><th>Метод</th><th>Маршрут</th><th>Параметры</th><th>Описание</th><th></th></tr></thead><tbody>
      ${rows.map(([m, path, params, text, sample]) => `<tr><td><span class="method ${m.toLowerCase()}">${m}</span></td><td><code>${esc(path)}</code></td><td class="small muted">${esc(params)}</td><td class="small">${esc(text)}</td>
        <td>${sample ? `<button class="btn btn-sm" data-try="${esc(sample)}">Попробовать</button>` : ""}</td></tr>`).join("")}</tbody></table></div></section>`).join("")}
    <section class="card" id="try-box" hidden><div class="list-head"><h2>Ответ <code id="try-url"></code></h2><button class="icon-btn" id="try-close" aria-label="Закрыть">${icon("x", 16)}</button></div><pre class="json" id="try-out"></pre></section>`;
  main.onclick = async (e) => {
    const b = e.target.closest("[data-try]");
    if (!b) return;
    const box = $("#try-box");
    box.hidden = false;
    $("#try-url").textContent = b.dataset.try;
    $("#try-out").textContent = "Загрузка…";
    box.scrollIntoView({ behavior: "smooth" });
    try {
      const r = await fetch(b.dataset.try);
      const text = JSON.stringify(await r.json(), null, 2);
      $("#try-out").textContent = text.length > 20000 ? text.slice(0, 20000) + "\n…" : text;
    } catch (err) {
      $("#try-out").textContent = String(err);
    }
  };
  $("#try-close").onclick = () => ($("#try-box").hidden = true);
}
