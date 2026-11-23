import { $, api, avatar, bar, CATEGORY_NAMES, cents, compact, dateTime, errorBox, esc, go, icon, minibar, num, pctRaw,
  pnlBadge, route, short, signedUsd, starBtn, state, tagChips, thumb, toast, unwrap, usd, winBadge, withQuery, shares as fmtShares } from "../core.js";
import { lineChart } from "../chart.js";
import { playerDialog } from "./autopilot.js";

const TABS = [["chart", "График"], ["positions", "Позиции"], ["categories", "Категории"], ["trades", "Сделки"]];
let chart = null;

const info = (t) => `<span class="info" title="${esc(t)}">${icon("info", 13)}</span>`;

export async function traderPage(main, wallet) {
  wallet = wallet.toLowerCase();
  const q = route().q;
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "chart";
  main.innerHTML = `<div class="profile"><aside class="pside" id="pside">${'<div class="sk-block tall"></div>'}</aside>
    <section class="pmain"><nav class="tabs" aria-label="Разделы профиля">${TABS.map(([k, l]) =>
      `<a href="${withQuery({ tab: k === "chart" ? null : k, cursor: null })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</nav>
    <div id="ptab"></div></section></div>`;
  let data;
  try {
    data = await api("/api/trader?wallet=" + wallet);
  } catch (e) {
    $("#pside").innerHTML = errorBox(e);
    return;
  }
  side(data);
  apBlock(wallet);
  const host = $("#ptab");
  if (tab === "chart") chartTab(host, wallet);
  else if (tab === "positions") positionsTab(host, wallet);
  else if (tab === "categories") categoriesTab(host, wallet);
  else tradesTab(host, wallet);
}

function side(d) {
  const s = d.stats || {};
  const name = s.name || short(d.wallet);
  document.title = name + " · Контур рынка";
  const pnl = d.overall?.pnl ?? s.pnl ?? d.profile?.pnl;
  const ready = s.stats_at != null;
  const calc = '<span class="calc">считается</span>';
  const streak = s.streak;
  const streakTxt = streak == null || streak === 0 ? "—" : `<span class="badge ${streak > 0 ? "badge-green" : "badge-red"}">${Math.abs(streak)} дн. ${streak > 0 ? "прибыли" : "убытка"}</span>`;
  $("#pside").innerHTML = `
    <div class="p-id">${avatar(d.wallet, s.image, name, 40)}<h1>${esc(name)}</h1>${d.overall?.rank ? `<span class="rank-chip">#${num(d.overall.rank)}</span>` : ""}
      <button class="icon-btn" data-copyaddr="${d.wallet}" title="Скопировать адрес">${icon("copy", 15)}</button></div>
    ${s.tags?.length ? `<div class="ttags">${tagChips(s.tags)}</div>` : ""}
    <div class="p-pnl ${pnl >= 0 ? "up" : "down"}">${usd(pnl)}</div><small class="muted">Общий PnL</small>
    ${ready ? `<div class="p-bar">${bar(s.gains, s.losses).replace('class="gl"', 'class="gl gl-wide"')}<small>PnL за всё время</small></div>` : ""}
    <div class="p-actions"><a class="btn btn-primary" href="/copy-trading/new?leader=${d.wallet}">${icon("copy", 15)} Копировать</a>${starBtn("trader", d.wallet, name, s.image, true).replace("star-btn", "star-btn btn")}</div>
    <dl class="p-stats">
      <div><dt>Общий PnL ${info("Прибыль и убыток по всем позициям (Polymarket)")}</dt><dd>${pnlBadge(pnl, 2)}</dd></div>
      <div><dt>Стоимость позиций ${info("Текущая стоимость открытых позиций")}</dt><dd>${d.networth != null ? `<span class="badge badge-green">${compact(d.networth)}</span>` : "—"}</dd></div>
      <div><dt>Сумма прибыли ${info("Сумма PnL прибыльных позиций")}</dt><dd class="up">${ready ? "+" + compact(s.gains).slice(1) : calc}</dd></div>
      <div><dt>Сумма убытков ${info("Сумма PnL убыточных позиций")}</dt><dd class="down">${ready ? "-" + compact(Math.abs(s.losses || 0)).slice(1) : calc}</dd></div>
      <div><dt>Win rate ${info("Доля позиций с положительным результатом")}</dt><dd>${ready ? winBadge(s.win_rate) : calc}</dd></div>
      <div><dt>Активные позиции ${info("Открытые сейчас позиции")}</dt><dd>${ready ? num(s.active) : calc}</dd></div>
      <div><dt>Всего позиций ${info("Все позиции кошелька" + (s.partial ? "; у очень крупных кошельков считается не больше 35 тыс." : ""))}</dt><dd>${ready ? num(s.positions) + (s.partial ? "+" : "") : calc}</dd></div>
    </dl>
    <a class="btn btn-block" href="https://polymarket.com/profile/${d.wallet}" target="_blank" rel="noopener noreferrer">${icon("ext", 14)} Профиль на Polymarket</a>
    <dl class="p-stats">
      <div><dt>Эффективность капитала (30д) ${info("PnL за 30 дней к объёму торгов за 30 дней")}</dt><dd>${s.cap_eff_30d != null ? pctRaw(s.cap_eff_30d, 2) : "—"}</dd></div>
      <div><dt>Текущая серия ${info("Дни подряд с ростом или падением PnL")}</dt><dd>${streakTxt}</dd></div>
      ${d.profile?.join_date ? `<div><dt>На Polymarket с</dt><dd>${new Date(d.profile.join_date * 1000).toLocaleDateString("ru-RU")}</dd></div>` : ""}
      ${d.profile?.volume != null ? `<div><dt>Объём торгов</dt><dd>${compact(d.profile.volume)}</dd></div>` : ""}
    </dl>`;
  $("[data-copyaddr]").onclick = async () => {
    try {
      await navigator.clipboard.writeText(d.wallet);
      toast("Адрес скопирован");
    } catch {
      toast(d.wallet);
    }
  };
}

async function chartTab(host, wallet) {
  const q = route().q;
  const series = q.get("series") === "realized" ? "realized" : "total";
  const win = ["7d", "1m", "3m", "ytd", "1y", "max"].includes(q.get("w")) ? q.get("w") : "max";
  host.innerHTML = `<div class="chart-tools"><div class="seg">${[["total", "Общий PnL"], ["realized", "Реализованный PnL"]].map(([k, l]) =>
    `<a href="${withQuery({ series: k === "total" ? null : k })}" class="${series === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div>
    <div class="seg">${[["7d", "7Д"], ["1m", "1М"], ["3m", "3М"], ["ytd", "YTD"], ["1y", "1Г"], ["max", "Всё"]].map(([k, l]) =>
      `<a href="${withQuery({ w: k === "max" ? null : k })}" class="${win === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div></div>
    <div id="pplot" class="pplot"></div><div id="returns"></div>`;
  try {
    let pts = unwrap(await api(`/api/trader/pnl?wallet=${wallet}&window=${win}`));
    const now = Date.now() / 1000;
    const since = { "3m": now - 92 * 86400, "1y": now - 366 * 86400, ytd: Date.UTC(new Date().getUTCFullYear(), 0, 1) / 1000 }[win];
    if (since) pts = pts.filter((p) => p.t >= since);
    const key = series === "realized" ? "realized" : "pnl";
    chart?.destroy();
    chart = lineChart($("#pplot"), [{ name: series === "realized" ? "Реализованный PnL" : "Общий PnL", color: "#34ca5b", area: true,
      points: pts.filter((p) => p[key] != null).map((p) => [p.t, p[key]]) }], { unit: "$", height: 300, intraday: win === "7d", empty: "Нет истории PnL" });
    const all = unwrap(await api(`/api/trader/pnl?wallet=${wallet}&window=1m`)).filter((p) => p.pnl != null);
    const last = all[all.length - 1];
    const change = (days) => {
      if (!last) return null;
      const target = last.t - days * 86400;
      const older = all.filter((p) => p.t <= target);
      return last.pnl - (older.length ? older[older.length - 1] : all[0]).pnl;
    };
    $("#returns").innerHTML = `<div class="returns">${[[2, "2 дня"], [7, "7 дней"], [14, "14 дней"], [30, "30 дней"]].map(([d, l]) => {
      const v = change(d);
      return `<div><span>${l}</span><b class="${v >= 0 ? "up" : "down"}">${v == null ? "—" : (v >= 0 ? "▲ " : "▼ ") + signedUsd(v, 0).slice(1)}</b></div>`;
    }).join("")}</div>`;
  } catch (e) {
    $("#pplot").innerHTML = errorBox(e, false);
  }
}

async function positionsTab(host, wallet) {
  const q = route().q;
  const status = q.get("status") === "closed" ? "CLOSED" : "OPEN";
  const sort = ["CURRENT_VALUE", "TOTAL_PNL", "TIMESTAMP"].includes(q.get("sort")) ? q.get("sort") : status === "OPEN" ? "CURRENT_VALUE" : "TIMESTAMP";
  host.innerHTML = `<div class="filters-row">
    <input type="search" id="pos-title" class="input-sm" placeholder="Поиск по событию…" value="${esc(q.get("title") || "")}">
    <select id="pos-status" class="input-sm"><option value="open">Активные позиции</option><option value="closed" ${status === "CLOSED" ? "selected" : ""}>Закрытые позиции</option></select>
    <select id="pos-sort" class="input-sm"><option value="CURRENT_VALUE">По стоимости</option><option value="TOTAL_PNL">По PnL</option><option value="TIMESTAMP">По дате</option></select>
    <select id="pos-event" class="input-sm"><option value="">Все события</option></select></div>
    <div class="tablewrap" id="pos-table">${'<div class="sk-block tall"></div>'}</div><div class="center"><button class="btn" id="pos-more" hidden>Загрузить ещё</button></div>`;
  $("#pos-sort").value = sort;
  $("#pos-status").onchange = (e) => go(withQuery({ status: e.target.value === "closed" ? "closed" : null, sort: null }));
  $("#pos-sort").onchange = (e) => go(withQuery({ sort: e.target.value }));
  let t = 0;
  $("#pos-title").oninput = (e) => {
    clearTimeout(t);
    t = setTimeout(() => go(withQuery({ title: e.target.value.trim() || null })), 450);
  };
  let rows = [], cursor = "";
  const evSel = $("#pos-event");
  evSel.onchange = draw;
  async function load() {
    try {
      const res = unwrap(await api(`/api/trader/positions?wallet=${wallet}&status=${status}&sort=${sort}&limit=50&cursor=${encodeURIComponent(cursor)}&title=${encodeURIComponent(q.get("title") || "")}`));
      rows = rows.concat(res.rows);
      cursor = res.next || "";
      $("#pos-more").hidden = !cursor;
      const events = [...new Map(rows.map((r) => [r.event_slug, r.title])).entries()];
      const cur = evSel.value;
      evSel.innerHTML = '<option value="">Все события</option>' + events.map(([s, t2]) => `<option value="${esc(s)}">${esc(t2.slice(0, 70))}</option>`).join("");
      evSel.value = cur;
      draw();
    } catch (e) {
      $("#pos-table").innerHTML = errorBox(e, false);
    }
  }
  function draw() {
    const list = rows.filter((r) => !evSel.value || r.event_slug === evSel.value);
    if (!list.length) {
      $("#pos-table").innerHTML = '<p class="muted pad">Позиций нет.</p>';
      return;
    }
    const maxPnl = Math.max(...list.map((r) => Math.abs(r.total_pnl || 0)), 1);
    $("#pos-table").innerHTML = `<table class="table"><thead><tr><th>Событие</th><th>Позиция</th><th class="num">Текущая стоимость</th><th class="num">Вложено</th><th class="num">PnL</th></tr></thead><tbody>
      ${list.map((r) => `<tr><td><a class="ev-cell" href="/event/${esc(r.event_slug)}">${thumb(r.icon, 28)}<span>${esc(r.title)}</span></a></td>
        <td><span class="pill ${String(r.outcome).toLowerCase() === "no" ? "pill-red" : "pill-green"}">${esc(r.outcome)}</span> <small class="muted">${cents(r.avg_price)} → ${cents(r.current_price)}</small></td>
        <td class="num">${usd(r.current_value)}</td><td class="num">${usd(r.total_cost_usdc)}</td>
        <td class="num"><div class="pnl-cell">${minibar(Math.abs(r.total_pnl || 0), maxPnl, (r.total_pnl || 0) >= 0 ? "green" : "red")}<span class="${(r.total_pnl || 0) >= 0 ? "up" : "down"}">${signedUsd(r.total_pnl || 0)}</span></div></td></tr>`).join("")}</tbody></table>`;
  }
  $("#pos-more").onclick = load;
  load();
}

async function categoriesTab(host, wallet) {
  host.innerHTML = '<div class="tablewrap" id="cat-table"><div class="sk-block tall"></div></div>';
  try {
    const d = await api("/api/trader/categories?wallet=" + wallet);
    if (!d.rows.length) {
      $("#cat-table").innerHTML = '<p class="muted pad">Нет данных по категориям.</p>';
      return;
    }
    $("#cat-table").innerHTML = `${d.detail_ready ? "" : `<p class="calc-note">${icon("clock", 14)} Позиции по категориям считаются в фоне. Ранг и PnL уже доступны.</p>`}
      <table class="table"><thead><tr><th>Категория</th><th class="num">Ранг</th><th class="num">Всего позиций</th><th class="num">Активные</th><th class="num">Рынки</th><th class="num">Win rate</th><th class="num">Прибыль/убыток</th><th class="num">PnL</th><th class="num">Объём</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr><td><b>${esc(CATEGORY_NAMES[r.category] || r.category)}</b></td><td class="num">${r.rank ? "#" + num(r.rank) : "—"}</td>
        <td class="num">${num(r.positions)}</td><td class="num">${num(r.active)}</td><td class="num">${num(r.markets)}</td>
        <td class="num">${r.win_rate != null ? winBadge(r.win_rate) : "—"}</td><td class="num">${r.gains != null ? bar(r.gains, r.losses) : "—"}</td>
        <td class="num">${pnlBadge(r.pnl)}</td><td class="num">${compact(r.volume)}</td></tr>`).join("")}</tbody></table>`;
  } catch (e) {
    $("#cat-table").innerHTML = errorBox(e, false);
  }
}

export function tradesTable(rows, { withTrader = false, showTags = false } = {}) {
  const maxV = Math.max(...rows.map((r) => (r.size || 0) * (r.price || 0)), 1);
  const maxS = Math.max(...rows.map((r) => r.size || 0), 1);
  return `<table class="table trades-table"><thead><tr><th>Время</th>${withTrader ? "<th>Трейдер</th>" : ""}<th>Сторона</th><th>Рынок</th><th>Исход</th><th class="num">Сумма</th><th class="num">Цена</th><th class="num">Акции</th></tr></thead><tbody>
    ${rows.map((r) => {
      const v = (r.size || 0) * (r.price || 0);
      return `<tr><td class="nowrap small">${dateTime(r.timestamp)}</td>
      ${withTrader ? `<td><a class="trader-cell" href="/traders/${r.proxy_wallet}">${avatar(r.proxy_wallet, r.profile_image, r.name, 22)}<span>${esc(r.name || r.pseudonym || short(r.proxy_wallet))}</span></a>${showTags && r.tags?.length ? `<div class="ttags">${tagChips(r.tags)}</div>` : ""}</td>` : ""}
      <td><span class="side ${r.side === "BUY" ? "buy" : "sell"}">${r.side === "BUY" ? "Покупка" : "Продажа"}</span></td>
      <td><a class="ev-cell" href="/event/${esc(r.event_slug)}">${thumb(r.icon, 24)}<span>${esc(r.title)}</span></a></td>
      <td>${esc(r.outcome)}</td>
      <td class="num"><div class="val-cell">${minibar(v, maxV)}<b>${usd(v, v < 10 ? 2 : 0)}</b></div></td>
      <td class="num"><div class="val-cell">${minibar(r.price || 0, 1)}<b>${cents(r.price)}</b></div></td>
      <td class="num"><div class="val-cell">${minibar(r.size || 0, maxS)}<b>${fmtShares(r.size)}</b></div></td></tr>`;
    }).join("")}</tbody></table>`;
}

async function tradesTab(host, wallet) {
  const q = route().q;
  const side = ["BUY", "SELL"].includes(q.get("side")) ? q.get("side") : "";
  host.innerHTML = `<div class="filters-row"><div class="seg">${[["", "Все"], ["BUY", "Покупки"], ["SELL", "Продажи"]].map(([k, l]) =>
    `<a href="${withQuery({ side: k || null })}" class="${side === k ? "cur" : ""}" data-keep-scroll>${l}</a>`).join("")}</div></div>
    <div class="tablewrap" id="tr-table"><div class="sk-block tall"></div></div><div class="center"><button class="btn" id="tr-more" hidden>Загрузить ещё</button></div>`;
  let rows = [], cursor = "";
  async function load() {
    try {
      const res = unwrap(await api(`/api/trader/trades?wallet=${wallet}&side=${side}&cursor=${encodeURIComponent(cursor)}`));
      rows = rows.concat(res.rows);
      cursor = res.next || "";
      $("#tr-more").hidden = !cursor;
      $("#tr-table").innerHTML = rows.length ? tradesTable(rows) : '<p class="muted pad">Сделок нет.</p>';
    } catch (e) {
      $("#tr-table").innerHTML = errorBox(e, false);
    }
  }
  $("#tr-more").onclick = load;
  load();
}

// Autopilot verdict on this trader, for signed-in users.
async function apBlock(wallet) {
  if (!state.user) return;
  const side = $("#pside");
  let p;
  try {
    p = await api("/api/autopilot/player?wallet=" + wallet);
  } catch {
    return;
  }
  if (!side.isConnected) return;
  const states = { active: ["В составе автопилота", "badge-green"], bench: ["На скамейке автопилота", "badge-gold"],
    retired: ["Выбыл из автопилота", "badge-gray"], banned: ["Исключён из автопилота", "badge-red"] };
  const st = p.card ? states[p.card.state] : null;
  const v = p.verdict, m = p.metrics;
  side.insertAdjacentHTML("beforeend", `<div class="ap-mini">
    <div class="ap-mini-head">${icon("pilot", 16)} <b>Автопилот</b>${st ? `<span class="badge ${st[1]}">${st[0]}</span>` : ""}</div>
    ${m ? `<p class="small">${v.passed ? '<span class="up">Проходит отбор</span>' : `<span class="muted">Не проходит: ${esc(v.failed.slice(0, 3).join(", ").toLowerCase())}</span>`} · балл ${Math.round(v.score)}</p>
      <p class="small muted">Копии его ставок за 180 дней: ${m.wins} из ${m.n} выиграно (${m.wr == null ? "—" : Math.round(m.wr * 100) + "%"}), доходность ${m.roi == null ? "—" : (m.roi * 100).toFixed(1).replace(".", ",") + "%"} на ставку.</p>`
      : '<p class="small muted">Игрок ещё не проверен. Добавьте его на скамейку — история проверится в фоне.</p>'}
    <div class="ap-mini-actions">${m ? '<button class="btn btn-sm" id="ap-why">Подробнее</button>' : ""}${!p.card ? '<button class="btn btn-sm" id="ap-bench">На скамейку</button>' : '<a class="btn btn-sm" href="/autopilot">Открыть автопилот</a>'}</div></div>`);
  $("#ap-why")?.addEventListener("click", () => playerDialog(wallet));
  $("#ap-bench")?.addEventListener("click", async () => {
    try {
      await api("/api/autopilot/action", { wallet, action: "bench" });
      toast("Игрок на скамейке автопилота: начато испытание");
      $(".ap-mini")?.remove();
      apBlock(wallet);
    } catch (err) {
      toast(err.message, "bad");
    }
  });
}
