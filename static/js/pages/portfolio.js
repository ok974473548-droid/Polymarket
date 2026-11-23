// Virtual account: value, day change, available cash, profit, Deposit/Withdraw, equity chart,
// Positions (search, sort, sell) / Open orders (cancel) / History.
import { $, api, cents, confirmBox, dateTime, delta, emptyBox, errorBox, esc, go, icon, loadSession, modal, route, signedUsd,
  state, thumb, toast, usd, withQuery, yesNo, shares as fmtShares } from "../core.js";
import { lineChart } from "../chart.js";
import { header, needLogin } from "../layout.js";

const TABS = [["positions", "Позиции"], ["orders", "Открытые заявки"], ["history", "История"]];
const WINDOWS = [["1d", "1Д"], ["1w", "7Д"], ["1m", "1М"], ["ytd", "YTD"], ["all", "Всё"]];
let chart = null;

export async function portfolioPage(main) {
  if (!state.user) return needLogin(main, "Портфель", "Войдите, чтобы видеть виртуальный счёт, позиции и заявки.");
  const q = route().q;
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "positions";
  const win = WINDOWS.some((w) => w[0] === q.get("w")) ? q.get("w") : "1w";
  let a;
  try {
    a = await api("/api/account");
  } catch (e) {
    main.innerHTML = errorBox(e);
    return;
  }
  state.account = a;
  main.innerHTML = `<div class="pf-grid">
      <section class="card pf-value"><div class="pf-top"><div><small>Стоимость портфеля</small><div class="pf-big">${usd(a.total)}</div>
        <div class="${a.day_change >= 0 ? "up" : "down"} small">${signedUsd(a.day_change)} ${delta(a.day_change_pct, 2)} <span class="muted">за сутки</span></div></div>
        <div class="pf-cash"><small>Доступно для торговли</small><b>${usd(a.cash)}</b>${a.reserved ? `<small class="muted">${usd(a.reserved)} в заявках</small>` : ""}</div></div>
        <div class="pf-actions"><button class="btn btn-primary" data-deposit>${icon("plus", 15)} Пополнить</button><button class="btn" data-withdraw>${icon("bank", 15)} Вывести</button></div>
        <p class="muted small">Виртуальный счёт в долларах. Цены и комиссии — с живых рынков Polymarket.</p></section>
      <section class="card pf-chart"><div class="pf-top"><div><small>Прибыль/убыток</small><div class="pf-big ${a.profit >= 0 ? "up" : "down"}">${signedUsd(a.profit)}</div>
        <small class="muted">В позициях ${usd(a.positions)} · нереализовано <span class="${a.unrealized >= 0 ? "up" : "down"}">${signedUsd(a.unrealized)}</span></small></div>
        <div class="seg">${WINDOWS.map(([k, l]) => `<a href="${withQuery({ w: k === "1w" ? null : k })}" class="${win === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div></div>
        <div id="eq-plot"></div></section></div>
    <nav class="tabs">${TABS.map(([k, l]) => `<a href="${withQuery({ tab: k === "positions" ? null : k })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</nav>
    <div id="pf-tab"></div>`;
  api(`/api/account/history?window=${win}`).then((pts) => {
    chart?.destroy();
    const host = $("#eq-plot");
    if (host) chart = lineChart(host, [{ name: "Стоимость", color: "#1b60d6", area: true, points: pts }], { unit: "$", height: 200, intraday: win === "1d" || win === "1w", empty: "История появится после первых сделок" });
  }).catch(() => {});
  const host = $("#pf-tab");
  if (tab === "positions") positionsTab(host, a.id);
  else if (tab === "orders") ordersTab(host, a.id);
  else historyTab(host, a.id);
}

export function positionsTable(rows, accountId) {
  return `<table class="table"><thead><tr><th>Рынок</th><th class="num">Ср. → текущая</th><th class="num">Вложено</th><th class="num">Стоимость</th><th class="num">К выигрышу</th><th></th></tr></thead><tbody>
    ${rows.map((p) => {
      const ret = p.pnl != null && p.cost ? (p.pnl / p.cost) * 100 : null;
      return `<tr><td><a class="ev-cell" href="/event/${esc(p.event_slug)}?contract=${p.condition}">${thumb(p.image, 32)}<span>${esc(p.title)}<small class="block"><span class="pill ${/^no$/i.test(p.outcome) ? "pill-red" : "pill-green"}">${esc(yesNo(p.outcome))}</span> ${fmtShares(p.shares)} акц.</small></span></a></td>
      <td class="num">${cents(p.avg)} → ${cents(p.price)}</td><td class="num">${usd(p.cost)}</td>
      <td class="num">${usd(p.value)}<small class="block ${(p.pnl || 0) >= 0 ? "up" : "down"}">${p.pnl != null ? signedUsd(p.pnl) : ""} ${ret != null ? "(" + ret.toFixed(1) + "%)" : ""}</small></td>
      <td class="num">${usd(p.to_win)}</td>
      <td class="num"><button class="btn btn-sm" data-sell="${p.token}" data-cond="${p.condition}" data-acc="${accountId}" data-max="${p.shares}" data-title="${esc(p.title)}" data-outcome="${esc(yesNo(p.outcome))}">Продать</button></td></tr>`;
    }).join("")}</tbody></table>`;
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-sell]");
  if (b) sellDialog(b.dataset);
});

function sellDialog(d0) {
  const max = Number(d0.max);
  const d = modal("Продать позицию", `<p><b>${esc(d0.title)}</b> · ${esc(d0.outcome)}</p>
    <form class="form" id="sell-form"><label>Акции (у вас ${fmtShares(max)})<input name="shares" type="number" min="0.01" step="0.01" max="${max}" value="${max}" required></label>
    <div class="chips">${[25, 50, 75, 100].map((v) => `<button type="button" class="chip" data-part="${v}">${v === 100 ? "Макс." : v + "%"}</button>`).join("")}</div>
    <div class="tk-summary" id="sell-sum"></div><p class="form-error" hidden></p>
    <button class="btn btn-primary btn-block">Продать по рынку</button></form>`);
  const f = $("#sell-form", d);
  let timer = 0;
  const base = { condition: d0.cond, token: d0.sell, side: "sell", type: "market", account: Number(d0.acc) };
  const quote = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await api("/api/quote", { ...base, shares: Number(f.shares.value) || 1 });
        $("#sell-sum", d).innerHTML = `<div><span>Ср. цена</span><span>${cents(r.avg)}</span></div>${r.fees ? `<div><span>Комиссия</span><span>${usd(r.fees)}</span></div>` : ""}<div><span>Вы получите</span><span><b class="up">${usd(r.receive)}</b></span></div>`;
      } catch (err) {
        $("#sell-sum", d).innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
      }
    }, 250);
  };
  f.shares.oninput = quote;
  f.addEventListener("click", (e) => {
    const c = e.target.closest("[data-part]");
    if (!c) return;
    f.shares.value = c.dataset.part === "100" ? max : Math.floor((max * Number(c.dataset.part)) / 100 * 100) / 100;
    quote();
  });
  quote();
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const o = await api("/api/order", { ...base, shares: Math.min(max, Number(f.shares.value)) });
      d.close();
      toast(`Продано ${fmtShares(o.filled)} акц. по ${cents(o.avg)}`, "good");
      await loadSession();
      header();
      go(location.pathname + location.search);
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}

async function positionsTab(host, aid) {
  const q = route().q;
  const status = q.get("status") === "closed" ? "closed" : "open";
  const sort = q.get("sort") || "value";
  host.innerHTML = `<div class="filters-row"><div class="search-inline">${icon("search", 15)}<input id="p-q" type="search" placeholder="Поиск по рынку" value="${esc(q.get("q") || "")}" aria-label="Поиск по рынку"></div>
    <select id="p-status" class="input-sm"><option value="open">Активные</option><option value="closed" ${status === "closed" ? "selected" : ""}>Закрытые</option></select>
    <select id="p-sort" class="input-sm" ${status === "closed" ? "hidden" : ""}><option value="value">По стоимости</option><option value="pnl">По прибыли, $</option><option value="ret">По прибыли, %</option><option value="cost">По вложенному</option><option value="name">По алфавиту</option><option value="recent">Недавние</option></select></div>
    <div class="tablewrap" id="p-table"><div class="sk-block tall"></div></div>`;
  $("#p-sort").value = sort;
  $("#p-status").onchange = (e) => go(withQuery({ status: e.target.value === "closed" ? "closed" : null }));
  $("#p-sort").onchange = (e) => go(withQuery({ sort: e.target.value === "value" ? null : e.target.value }));
  let rows;
  try {
    rows = await api(`/api/account/positions?id=${aid}&status=${status}`);
  } catch (e) {
    $("#p-table").innerHTML = errorBox(e, false);
    return;
  }
  const draw = () => {
    const phrase = $("#p-q").value.trim().toLowerCase();
    let list = rows.filter((r) => !phrase || r.title.toLowerCase().includes(phrase) || r.outcome.toLowerCase().includes(phrase));
    const key = { value: (r) => r.value ?? 0, pnl: (r) => r.pnl ?? 0, ret: (r) => (r.cost ? (r.pnl ?? 0) / r.cost : 0), cost: (r) => r.cost, recent: (r) => r.updated }[sort];
    if (sort === "name") list.sort((a, b) => a.title.localeCompare(b.title, "ru"));
    else if (key) list.sort((a, b) => key(b) - key(a));
    if (!list.length) {
      $("#p-table").innerHTML = emptyBox(status === "open" ? "Нет открытых позиций" : "Нет закрытых позиций", status === "open" ? "Откройте любой рынок и купите исход в тикете." : "", status === "open" ? '<a class="btn btn-primary" href="/markets">Открыть рынки</a>' : "");
      return;
    }
    $("#p-table").innerHTML = status === "open" ? positionsTable(list, aid)
      : `<table class="table"><thead><tr><th>Рынок</th><th>Итог</th><th class="num">Реализовано</th><th class="num">Закрыта</th></tr></thead><tbody>
        ${list.map((p) => `<tr><td><a class="ev-cell" href="/event/${esc(p.event_slug)}">${thumb(p.image, 28)}<span>${esc(p.title)} <small class="muted">${esc(yesNo(p.outcome))}</small></span></a></td>
          <td>${p.status === "redeemed" ? `<span class="badge ${p.payout > 0 ? "badge-green" : "badge-red"}">${p.payout > 0 ? "Выигрыш" : "Проигрыш"}</span>` : '<span class="badge badge-gray">Продана</span>'}</td>
          <td class="num ${p.realized >= 0 ? "up" : "down"}">${signedUsd(p.realized)}</td><td class="num small">${dateTime(p.updated)}</td></tr>`).join("")}</tbody></table>`;
  };
  $("#p-q").oninput = draw;
  draw();
}

async function ordersTab(host, aid) {
  host.innerHTML = '<div class="tablewrap" id="o-table"><div class="sk-block tall"></div></div>';
  try {
    const rows = await api(`/api/account/orders?id=${aid}&status=open`);
    $("#o-table").innerHTML = rows.length ? `<table class="table"><thead><tr><th>Рынок</th><th>Сторона</th><th class="num">Цена</th><th class="num">Исполнено</th><th class="num">Сумма</th><th>Срок</th><th></th></tr></thead><tbody>
      ${rows.map((o) => `<tr><td><a class="ev-cell" href="/event/${esc(o.event_slug)}?contract=${o.condition}">${thumb(o.image, 28)}<span>${esc(o.title)} <small class="muted">${esc(yesNo(o.outcome))}</small></span></a></td>
        <td><span class="side ${o.side === "buy" ? "buy" : "sell"}">${o.side === "buy" ? "Покупка" : "Продажа"}</span></td><td class="num">${cents(o.price)}</td>
        <td class="num">${fmtShares(o.filled)} / ${fmtShares(o.shares)}</td><td class="num">${usd(o.shares * o.price)}</td>
        <td class="small">${o.expires ? dateTime(o.expires) : "Без срока"}</td><td class="num"><button class="btn btn-sm btn-danger-ghost" data-cancel="${o.id}">Отменить</button></td></tr>`).join("")}</tbody></table>`
      : emptyBox("Открытых заявок нет", "Лимитные заявки, которые ещё не исполнились, появятся здесь.");
    host.onclick = async (e) => {
      const b = e.target.closest("[data-cancel]");
      if (!b || !(await confirmBox("Отменить заявку?", "Зарезервированные средства вернутся на счёт.", "Отменить заявку", true))) return;
      try {
        await api("/api/order/cancel", { id: Number(b.dataset.cancel) });
        toast("Заявка отменена");
        await loadSession();
        header();
        ordersTab(host, aid);
      } catch (err) {
        toast(err.message, "bad");
      }
    };
  } catch (e) {
    $("#o-table").innerHTML = errorBox(e, false);
  }
}

const LEDGER = { deposit: "Пополнение", withdraw: "Вывод", transfer_in: "Перевод на счёт", transfer_out: "Перевод со счёта", trade: "Сделка",
  reserve: "Резерв заявки", release: "Возврат резерва", redeem: "Погашение" };

async function historyTab(host, aid) {
  const q = route().q;
  const kind = q.get("kind") === "orders" ? "orders" : "ledger";
  host.innerHTML = `<div class="filters-row"><div class="seg"><a href="${withQuery({ kind: null })}" class="${kind === "ledger" ? "cur" : ""}" data-keep-scroll>Движение средств</a><a href="${withQuery({ kind: "orders" })}" class="${kind === "orders" ? "cur" : ""}" data-keep-scroll>Заявки</a></div></div>
    <div class="tablewrap" id="h-table"><div class="sk-block tall"></div></div>`;
  try {
    if (kind === "orders") {
      const rows = await api(`/api/account/orders?id=${aid}&status=done`);
      const st = { filled: ["Исполнена", "badge-green"], cancelled: ["Отменена", "badge-gray"], expired: ["Истекла", "badge-gray"], rejected: ["Отклонена", "badge-red"] };
      $("#h-table").innerHTML = rows.length ? `<table class="table"><thead><tr><th>Время</th><th>Рынок</th><th>Тип</th><th class="num">Акции</th><th class="num">Ср. цена</th><th class="num">Комиссия</th><th>Статус</th></tr></thead><tbody>
        ${rows.map((o) => `<tr><td class="small nowrap">${dateTime(o.created)}</td><td><a class="ev-cell" href="/event/${esc(o.event_slug)}">${thumb(o.image, 24)}<span>${esc(o.title)} <small class="muted">${esc(yesNo(o.outcome))}</small></span></a></td>
          <td><span class="side ${o.side === "buy" ? "buy" : "sell"}">${o.side === "buy" ? "Покупка" : "Продажа"}</span> <small class="muted">${o.type === "limit" ? "лимит" : "рынок"}${o.source === "copy" ? " · копия" : ""}</small></td>
          <td class="num">${fmtShares(o.filled)}</td><td class="num">${cents(o.avg)}</td><td class="num">${usd(o.fees)}</td>
          <td><span class="badge ${(st[o.status] || ["", "badge-gray"])[1]}">${(st[o.status] || [o.status])[0]}</span>${o.reason ? `<small class="muted block">${esc(o.reason)}</small>` : ""}</td></tr>`).join("")}</tbody></table>` : emptyBox("Заявок ещё не было");
    } else {
      const rows = await api(`/api/account/activity?id=${aid}`);
      $("#h-table").innerHTML = rows.length ? `<table class="table"><thead><tr><th>Время</th><th>Операция</th><th>Описание</th><th class="num">Сумма</th><th class="num">Баланс</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td class="small nowrap">${dateTime(r.at)}</td><td>${esc(LEDGER[r.kind] || r.kind)}</td><td class="small">${esc(r.note || "")}</td>
          <td class="num ${r.amount >= 0 ? "up" : "down"}">${signedUsd(r.amount)}</td><td class="num">${usd(r.balance_after)}</td></tr>`).join("")}</tbody></table>` : emptyBox("Операций нет");
    }
  } catch (e) {
    $("#h-table").innerHTML = errorBox(e, false);
  }
}
