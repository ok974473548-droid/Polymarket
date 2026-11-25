// Trades and bets in plain money: one row says who spent or received how much, the details open under the row.
// Used by the copy-trading and autopilot activity tabs and the autopilot bets tab. On a phone the rows become cards.
import { api, avatar, cents, dateTime, esc, icon, secs, shares as fmtShares, short, signedUsd, thumb, usd, yesNo } from "./core.js";

const WHEN = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const HM = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? HM.format(d) : WHEN.format(d);
}
function duration(s) {
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " мин";
  if (s < 2 * 86400) return Math.round(s / 3600) + " ч";
  return Math.round(s / 86400) + " дн.";
}
const pctOf = (v, base) => (base ? (v >= 0 ? "+" : "−") + Math.abs((100 * v) / base).toFixed(Math.abs(v / base) < 0.1 ? 1 : 0).replace(".", ",") + "%" : "");

// Short label for the reason a copy was skipped; `setting` marks reasons the person can change in the settings.
const REASONS = [
  [/^Размер копии меньше/, "Копия меньше $1", true],
  [/^Категория «/, "Категория", true],
  [/^Сделка (игрока|лидера) меньше/, "Мелкая сделка", true],
  [/без цены/, "Нет цены", false],
  [/диапазон/, "Цена вне диапазона", true],
  [/Краткосрочн/, "Быстрый крипторынок", false],
  [/^До разрешения/, "Долгий рынок", true],
  [/Доля игрока занята/, "Доля игрока занята", true],
  [/на одно событие/, "Лимит на событие", true],
  [/дневной лимит/i, "Дневной лимит", true],
  [/Недостаточно (средств|свободных денег)/, "Нет денег", false],
  [/Цена ушла/, "Цена ушла", true],
  [/нет (этой позиции|копии этой позиции)/i, "Нет нашей позиции", false],
  [/маленький объём продажи/, "Мелкая продажа", false],
  [/Рынок уже закрыт/, "Рынок закрыт", false],
  [/выведен из состава/, "Игрок вне состава", false],
  [/копировать поздно/, "Поздно", false],
  [/Цена не вернулась/, "Цена не вернулась", true],
  [/продал раньше|ждала цену игрока/, "Игрок уже продал", false],
  [/минимальной заявки/, "Мало для заявки", false],
];
export function reasonShort(r) {
  for (const [re, label, setting] of REASONS) if (re.test(r.reason || "")) return { label, setting };
  return { label: r.status === "failed" ? "Ошибка" : "Пропущено", setting: false };
}
function reasonFull(reason) {
  const m = /^До разрешения (\d+) дн\., лимит (\d+)/.exec(reason || "");
  if (m) return `Рынок разрешится через ${m[1]} дн., а в настройках лимит — ${m[2]} дн.`;
  return (reason || "Причина не записана").replace(/\.?$/, ".");
}

// ------------------------------------------------------------------ expand / collapse
const opened = new Set(); // rows the person opened stay open when a live update redraws the list

export function bindRows(box, onOpen) {
  const toggle = (row, open) => {
    const more = row.nextElementSibling;
    if (!more || !more.classList.contains("tx-detail")) return;
    more.hidden = !open;
    row.classList.toggle("open", open);
    row.querySelector(".tx-toggle")?.setAttribute("aria-expanded", String(open));
    if (open) opened.add(row.dataset.key);
    else opened.delete(row.dataset.key);
    if (open && onOpen) onOpen(row, more);
  };
  box.addEventListener("click", (e) => {
    const row = e.target.closest("tr.tx-row");
    if (!row || (e.target.closest("a, button, input, select") && !e.target.closest(".tx-toggle"))) return;
    toggle(row, row.classList.contains("open") ? false : true);
  });
  box.querySelectorAll("tr.tx-row").forEach((row) => opened.has(row.dataset.key) && toggle(row, true));
}

const toggleCell = '<td class="c-more"><button class="tx-toggle" type="button" aria-expanded="false" title="Подробности" aria-label="Подробности">' + icon("down", 16) + "</button></td>";
const kv = (label, value, cls = "") => `<div class="${cls}"><span>${label}</span><b>${value}</b></div>`;
const playerCell = (wallet, name) => {
  const label = !name || /^0x[0-9a-f]{40}/i.test(name) ? short(wallet) : name; // Polymarket's default names are the address itself
  return `<a class="trader-cell" href="/traders/${esc(wallet)}" title="${esc(name || wallet)}">${avatar(wallet, "", name, 22)}<span>${esc(label)}</span></a>`;
};
const marketCell = (r, size, href, sub = "") =>
  `<a class="ev-cell tx-ev" href="${href}">${thumb(r.image, size)}<span><span class="tx-title">${esc(r.title || "Рынок")}</span>${sub ? `<small class="tx-sub">${sub}</small>` : ""}</span></a>`;
const outcomePill = (o) => `<span class="pill ${/^no$/i.test(o || "") ? "pill-red" : "pill-green"}">${esc(yesNo(o) || "—")}</span>`;
// Money with a quiet second line: "$13,72" / "к выигрышу $28,00".
const money = (main, sub = "", cls = "") => `<span class="tx-money ${cls}">${main}</span>${sub ? `<small class="tx-note">${sub}</small>` : ""}`;

// ------------------------------------------------------------------ activity: every leader trade and what we did
// opts: who ("Игрок" | "Трейдер"), filled ("Скопировано" | "Исполнено"), settings(r) -> html of a link or button, slip (text of the price tolerance)
export function activityTable(rows, opts) {
  const who = opts.who || "Игрок";
  const whoLow = who.toLowerCase();
  const whose = who === "Игрок" ? "игрока" : "трейдера";
  return `<table class="table tx-table tx-activity"><thead><tr><th>Время</th><th>${who}</th><th>Рынок и сделка</th>
    <th class="num">У ${whose}</th><th class="num">У нас</th><th>Итог</th><th class="c-more"><span class="sr">Подробности</span></th></tr></thead><tbody>
    ${rows.map((r) => {
      const buy = r.side === "buy";
      const out = (r.outcome && yesNo(r.outcome)) || "";
      const lead = r.leader_size != null && r.leader_price != null ? r.leader_size * r.leader_price : null;
      const filled = r.status === "filled" && r.amount != null;
      const why = r.status === "filled" ? null : reasonShort(r);
      const badge = r.status === "filled" ? `<span class="badge badge-green">${opts.filled || "Скопировано"}</span>`
        : r.status === "waiting" ? `<span class="badge badge-gold" title="${esc(r.reason || "")}">Ждёт цену</span>`
        : `<span class="badge ${r.status === "failed" ? "badge-red" : "badge-gray"}" title="${esc(r.reason || "")}">${esc(why.label)}</span>`;
      const side = `<span class="side ${buy ? "buy" : "sell"}">${buy ? "Купил" : "Продал"}${out ? ` «${esc(out)}»` : ""}</span>`;
      // A buy: money in and what it pays if the outcome wins ($1 a share). A sale: money received.
      const leadCell = lead == null ? "—" : money(usd(lead), buy ? `к выигрышу ${usd(r.leader_size)}` : "получено");
      const ourCell = !filled ? '<span class="muted">—</span>' : money(usd(r.amount), buy ? `к выигрышу ${usd(r.shares)}` : "получено", "strong");
      return `<tr class="tx-row" data-key="ev${r.id}">
        <td class="c-time nowrap small" title="${dateTime(r.at)}">${when(r.at)}</td>
        <td class="c-who">${playerCell(r.leader, "")}</td>
        <td class="c-mkt">${marketCell(r, 28, `/event/${esc(r.event_slug)}`, side)}</td>
        <td class="num c-a" data-label="У ${whose}">${leadCell}</td>
        <td class="num c-b" data-label="У нас">${ourCell}</td>
        <td class="c-status">${badge}</td>${toggleCell}</tr>
        <tr class="tx-detail" hidden><td colspan="7">${activityDetail(r, { ...opts, whoLow, lead, buy, out })}</td></tr>`;
    }).join("")}</tbody></table>`;
}

function activityDetail(r, o) {
  const verb = o.buy ? "Купил" : "Продал";
  const leader = `<section><h4>Сделка ${o.whoLow === "игрок" ? "игрока" : "трейдера"}</h4><div class="tx-kv">
    ${kv("Действие", `${verb}${o.out ? ` «${esc(o.out)}»` : ""}`)}
    ${kv("Акций", r.leader_size != null ? fmtShares(r.leader_size) : "—")}
    ${kv("Цена", cents(r.leader_price))}
    ${kv(o.buy ? "Вложил" : "Получил", usd(o.lead), "tx-total")}
    ${o.buy && r.leader_size != null ? kv("К выигрышу", usd(r.leader_size)) : ""}
    ${r.leader_ts ? kv("Время", dateTime(r.leader_ts)) : ""}</div></section>`;
  let ours;
  if (r.status === "filled") {
    const gross = (r.shares || 0) * (r.price || 0);
    ours = `<section><h4>Наша копия</h4><div class="tx-kv">
      ${kv("Акций", fmtShares(r.shares))}
      ${kv("Средняя цена", cents(r.price))}
      ${kv("Сумма по цене", usd(gross))}
      ${kv("Комиссия", r.fee != null ? usd(r.fee) : "—")}
      ${kv(o.buy ? "Всего вложили" : "Получили на счёт", usd(r.amount), "tx-total")}
      ${o.buy ? kv("К выигрышу", usd(r.shares)) : ""}</div></section>`;
  } else {
    const plan = r.amount != null ? kv("Собирались вложить", usd(r.amount)) : r.shares != null ? kv("Собирались продать", fmtShares(r.shares) + " акций") : "";
    ours = `<section><h4>Наша копия</h4><div class="tx-kv">${kv("Копия", r.status === "failed" ? "не получилась" : "не делалась")}${plan}</div></section>`;
  }
  const rows = [];
  if (r.status === "filled" && r.price != null && r.leader_price) {
    const d = r.price - r.leader_price;
    const worse = o.buy ? d > 0.0005 : d < -0.0005;
    const better = o.buy ? d < -0.0005 : d > 0.0005;
    const txt = Math.abs(d) <= 0.0005 ? "та же, что у " + (o.whoLow === "игрок" ? "игрока" : "трейдера")
      : `${d > 0 ? "дороже" : "дешевле"} на ${cents(Math.abs(d))} за акцию (${pctOf(d, r.leader_price)})`;
    rows.push(kv("Наша цена", `<span class="${worse ? "down" : better ? "up" : ""}">${txt}</span>`));
    if (o.slip) rows.push(kv("Допуск по цене", o.slip));
  }
  if (r.leader_ts != null) {
    rows.push(kv("Задержка копии", secs(Math.max(0, r.at - r.leader_ts))));
    rows.push(kv("Источник", r.source === "stream" ? "живой поток сделок" : "запасной опрос Data API"));
  }
  const speed = rows.length ? `<section><h4>Цена и скорость</h4><div class="tx-kv">${rows.join("")}</div></section>` : "";
  const reason = r.status === "filled" ? "" : (() => {
    const s = reasonShort(r);
    return `<section class="tx-why"><h4>${r.status === "failed" ? "Что пошло не так" : "Почему пропущено"}</h4><p>${esc(reasonFull(r.reason))}</p>
      ${s.setting && o.settings ? o.settings(r) : ""}</section>`;
  })();
  const links = `<nav class="tx-links">${r.event_slug ? `<a href="/event/${esc(r.event_slug)}">${icon("chart", 14)} Рынок</a>` : ""}
    <a href="/traders/${esc(r.leader)}">${icon("user", 14)} Профиль ${o.whoLow === "игрок" ? "игрока" : "трейдера"}</a>
    ${r.leader_tx ? `<a href="https://polygonscan.com/tx/${esc(r.leader_tx)}" target="_blank" rel="noopener">${icon("ext", 14)} Сделка в блокчейне</a>` : ""}</nav>`;
  return `<div class="tx-more"><div class="tx-cols">${leader}${ours}${speed}${reason}</div>${links}</div>`;
}

// ------------------------------------------------------------------ autopilot bets: our money in each copy
export function betsTable(rows, status = "") {
  // As the portfolio positions: entry -> current price, money in, value now (result under it), what it pays if it wins.
  // Closed copies: the value is what came back, the last column is the outcome.
  const closedOnly = status === "closed";
  return `<table class="table tx-table tx-bets"><thead><tr><th>Рынок</th><th>Игрок</th><th class="num">Ср. → ${closedOnly ? "выход" : "текущая"}</th>
    <th class="num">Вложено</th><th class="num">${closedOnly ? "Получено" : "Стоимость"}</th><th class="num">${closedOnly ? "Итог" : "К выигрышу"}</th>
    <th class="c-more"><span class="sr">Подробности</span></th></tr></thead><tbody>
    ${rows.map((r) => {
      const open = r.status === "open";
      const res = r.result;
      const resLine = res == null ? "" : `<span class="${res >= 0 ? "up" : "down"}">${signedUsd(res)} (${pctOf(res, r.cost)})</span>`;
      const exit = open ? r.price : r.bought ? r.proceeds / r.bought : null;
      const nowCell = open ? money(r.value == null ? "—" : usd(r.value), resLine) : money(usd(r.proceeds), resLine);
      const sold = open && r.proceeds > 1e-6 ? `продано на ${usd(r.proceeds)}` : ""; // keeps the row's sum readable: value + sold - in
      const last = open ? money(usd(r.shares)) : r.resolved
        ? ((r.pnl || 0) > 0 ? '<span class="badge badge-green">Выигрыш</span>' : '<span class="badge badge-red">Проигрыш</span>')
        : `<span class="badge ${(r.pnl || 0) >= 0 ? "badge-green" : "badge-red"}">Продана</span>`;
      const sub = `${outcomePill(r.outcome)} ${open ? fmtShares(r.shares) + " акц." : "закрыта " + when(r.closed)}`;
      return `<tr class="tx-row${open ? "" : " is-closed"}" data-key="bet${r.id}" data-bet="${r.id}">
        <td class="c-mkt">${marketCell(r, 32, `/event/${esc(r.event_slug)}?contract=${esc(r.condition_id)}`, sub)}</td>
        <td class="c-who">${playerCell(r.wallet, r.player)}</td>
        <td class="num c-price nowrap">${cents(r.entry)} → ${cents(exit)}</td>
        <td class="num c-a" data-label="Вложено">${money(usd(r.cost), sold)}</td>
        <td class="num c-b" data-label="${open ? "Стоимость" : "Получено"}">${nowCell}</td>
        <td class="num c-c" data-label="${open ? "К выигрышу" : "Итог"}">${last}</td>${toggleCell}</tr>
        <tr class="tx-detail" hidden><td colspan="7">${betDetail(r)}</td></tr>`;
    }).join("")}</tbody></table>`;
}

function betDetail(r) {
  const open = r.status === "open";
  const sold = Math.max(0, (r.bought || 0) - (r.shares || 0));
  const pos = `<section><h4>Покупка</h4><div class="tx-kv">
    ${kv("Куплено акций", fmtShares(r.bought))}
    ${kv("Средняя цена с комиссией", cents(r.entry))}
    ${kv("Вложено", usd(r.cost), "tx-total")}
    ${kv("Открыта", dateTime(r.opened))}</div></section>`;
  if (!open) {
    const how = r.resolved ? "выплата по итогу рынка" : "продажа";
    return `<div class="tx-more"><div class="tx-cols">${pos}<section><h4>Закрытие</h4><div class="tx-kv">
      ${kv("Как закрыта", r.resolved ? "рынок разрешился" : "продана")}
      ${kv("Получено", `${usd(r.proceeds)} <small class="muted">${how}</small>`)}
      ${kv("Результат", `<span class="${(r.pnl || 0) >= 0 ? "up" : "down"}">${signedUsd(r.pnl)} (${pctOf(r.pnl || 0, r.cost)})</span>`, "tx-total")}
      ${kv("Закрыта", dateTime(r.closed))}
      ${r.closed ? kv("Держали", duration(r.closed - r.opened)) : ""}</div></section></div>${betLinks(r)}</div>`;
  }
  const now = `<section><h4>Сейчас</h4><div class="tx-kv">
    ${kv("Акций", fmtShares(r.shares) + (sold > 1e-6 ? ` <small class="muted">продано ${fmtShares(sold)}</small>` : ""))}
    ${kv("Цена рынка (середина)", cents(r.price))}
    ${kv("Стоимость по рынку", r.value == null ? "—" : usd(r.value))}
    ${kv("Если продать сейчас", '<span class="tx-exit muted">считаю…</span>')}
    ${r.proceeds > 1e-6 ? kv("Уже получено от продаж", usd(r.proceeds)) : ""}
    ${kv("Результат", r.result == null ? "—" : `<span class="${r.result >= 0 ? "up" : "down"}">${signedUsd(r.result)} (${pctOf(r.result, r.cost)})</span>`, "tx-total")}</div></section>`;
  const how = `<section class="tx-why"><h4>Как считается результат</h4><p>Стоимость по рынку${r.proceeds > 1e-6 ? " + уже полученное от продаж" : ""} − вложенное.
    Стоимость — акции × середина между лучшими ценами покупки и продажи; обновляется каждые 15 секунд.
    Продажа прямо сейчас принесёт чуть меньше: по ценам покупателей и за вычетом комиссии — эта сумма в строке «Если продать сейчас».
    Когда рынок разрешится, ставка закроется по итогу ($1 или $0 за акцию) в течение 10 минут.</p></section>`;
  return `<div class="tx-more"><div class="tx-cols">${pos}${now}${how}</div>${betLinks(r)}</div>`;
}

const betLinks = (r) => `<nav class="tx-links">${r.event_slug ? `<a href="/event/${esc(r.event_slug)}?contract=${esc(r.condition_id)}">${icon("chart", 14)} Рынок</a>` : ""}
  <a href="/traders/${esc(r.wallet)}">${icon("user", 14)} Профиль игрока</a></nav>`;

// What selling the open copy right now would bring: the current bids, minus the taker fee (asked when the details open).
export function loadExit(rows) {
  const byId = new Map(rows.map((r) => [String(r.id), r]));
  return async (row, more) => {
    const r = byId.get(row.dataset.bet);
    const el = more.querySelector(".tx-exit");
    if (!r || !el || r.status !== "open" || el.dataset.done) return;
    el.dataset.done = "1";
    try {
      const q = await api("/api/quote", { side: "sell", type: "market", condition: r.condition_id, token: r.token_id, shares: r.shares });
      if (!q.shares) {
        el.textContent = "сейчас нет покупателей";
        return;
      }
      const part = q.shares < r.shares - 1e-6 ? ` <small class="muted">покупателей хватает на ${fmtShares(q.shares)} акций</small>` : "";
      const gap = r.value != null && q.shares >= r.shares - 1e-6 ? ` <small class="muted">на ${usd(Math.max(0, r.value - q.receive))} меньше оценки</small>` : "";
      el.classList.remove("muted");
      el.innerHTML = `${usd(q.receive)}${part}${gap}`;
    } catch {
      el.textContent = "не удалось получить стакан";
    }
  };
}
