import { $, api, avatar, bar, CATEGORY_NAMES, compact, delta, errorBox, esc, go, icon, num, pager, pct, pnlBadge, route,
  starBtn, tagChips, TAG_NAMES, thumb, unwrap, usd, winBadge, withQuery } from "../core.js";
import { sparkline } from "./home.js";

const TABS = [["leaderboard", "Лидерборд", "trophy"], ["whales", "Киты", "whale"], ["sports", "Спорт", "ball"],
  ["crypto", "Крипто", "coin"], ["politics", "Политика", "bank"]];
const COLS = [["positions", "Позиции"], ["active", "Активные"], ["gains", "Прибыль/убыток"], ["win", "Win %"],
  ["networth", "Стоимость"], ["pnl", "PnL"]];
const RANGES = [["pnl", "Общий PnL, $"], ["networth", "Стоимость позиций, $"], ["active", "Активные позиции"],
  ["gains", "Сумма прибыли, $"], ["losses", "Сумма убытков, $"], ["positions", "Всего позиций"], ["win", "Win rate, %"]];

export function traderRows(data, sortable = true) {
  const q = route().q;
  const showTags = q.get("tags") === "1";
  const head = (key, label) => {
    if (!sortable) return `<th class="num">${label}</th>`;
    const cur = data.sort === key;
    const next = cur && data.dir === "desc" ? "asc" : "desc";
    return `<th class="num"><a class="sort ${cur ? "cur" : ""}" href="${withQuery({ sort: key, dir: next, page: null })}" data-keep-scroll>${label}<span class="sort-ic">${cur ? (data.dir === "desc" ? "▼" : "▲") : "⇅"}</span></a></th>`;
  };
  if (!data.rows.length) return `<p class="muted pad">По этим условиям трейдеров нет.</p>`;
  return `<table class="table traders-table"><thead><tr><th class="w-star"></th><th class="w-rank">#</th><th>Трейдер</th>${COLS.map(([k, l]) => head(k, l)).join("")}</tr></thead><tbody>
    ${data.rows.map((r) => {
      const ready = r.stats_at != null;
      const dash = ready ? "—" : '<span class="calc">считается</span>';
      return `<tr>
      <td>${starBtn("trader", r.wallet, r.name, r.image)}</td>
      <td class="muted">${r.rank}</td>
      <td><a class="trader-cell" href="/traders/${r.wallet}">${avatar(r.wallet, r.image, r.name, 28)}<b>${esc(r.name || r.wallet.slice(0, 6) + "…" + r.wallet.slice(-4))}</b></a>${showTags && r.tags?.length ? `<div class="ttags">${tagChips(r.tags)}</div>` : ""}</td>
      <td class="num">${r.positions != null ? num(r.positions) + (r.partial ? "+" : "") : dash}</td>
      <td class="num">${r.active != null ? num(r.active) : dash}</td>
      <td class="num">${r.gains != null ? bar(r.gains, r.losses) : dash}</td>
      <td class="num">${r.win_rate != null ? winBadge(r.win_rate) : dash}</td>
      <td class="num">${r.networth != null ? compact(r.networth) : dash}</td>
      <td class="num">${pnlBadge(r.pnl)}</td></tr>`;
    }).join("")}</tbody></table>`;
}

export async function tradersPage(main) {
  const q = route().q;
  const tab = TABS.some((t) => t[0] === q.get("tab")) ? q.get("tab") : "leaderboard";
  const params = new URLSearchParams(q);
  params.set("tab", tab);
  main.innerHTML = `<div class="page-head"><h1>Данные и аналитика Polymarket</h1><p>Данные, аналитика и рейтинг трейдеров Polymarket.</p></div>
    <div class="trio" id="trio"><div class="card sk-block"></div><div class="card sk-block"></div><div class="card sk-block"></div></div>
    <div class="section-head"><h2>Лучшие трейдеры</h2><p>Лидеры по разным категориям рынков.</p></div>
    <div class="tabs-row"><nav class="tabs" aria-label="Рейтинги">${TABS.map(([k, l, ic]) =>
      `<a href="${withQuery({ tab: k === "leaderboard" ? null : k, page: null, sort: null, dir: null, category: null })}" class="${tab === k ? "cur" : ""}">${icon(ic, 15)} ${l}</a>`).join("")}</nav>
      <div class="tabs-tools"><label class="search-mini">${icon("search", 14)}<input id="tr-search" type="search" placeholder="Поиск трейдера…" value="${esc(q.get("search") || "")}"></label>
      <div class="dropdown filters-dd"><button class="btn btn-sm">${icon("sliders", 14)} Фильтры ${icon("down", 13)}</button><div class="menu filters-panel" id="fpanel"></div></div></div></div>
    <div class="board-tools"><label class="field-sm">Категория<select id="tr-cat">${Object.entries(CATEGORY_NAMES).filter(([k]) =>
      ["overall", "politics", "sports", "crypto", "finance", "tech", "economy", "culture", "weather", "mentions", "esports"].includes(k)).map(([k, l]) =>
      `<option value="${k}">${k === "overall" ? "Все категории" : l}</option>`).join("")}</select></label>
      <label class="toggle"><input type="checkbox" id="tr-tags" ${q.get("tags") === "1" ? "checked" : ""}><span></span> Показать метки</label>
      <label class="toggle"><input type="checkbox" id="tr-active" ${q.get("active") === "1" ? "checked" : ""}><span></span> Только активные</label></div>
    <div id="board" class="tablewrap">${'<div class="sk-block tall"></div>'}</div>`;
  trio();
  let data;
  try {
    data = await api("/api/traders?" + params.toString());
  } catch (e) {
    $("#board").innerHTML = errorBox(e);
    return;
  }
  const cat = $("#tr-cat");
  cat.value = data.category;
  cat.onchange = () => go(withQuery({ category: cat.value === "overall" ? null : cat.value, page: null }));
  $("#tr-tags").onchange = (e) => go(withQuery({ tags: e.target.checked ? "1" : null }));
  $("#tr-active").onchange = (e) => go(withQuery({ active: e.target.checked ? "1" : null, page: null }));
  const s = $("#tr-search");
  let t = 0;
  s.oninput = () => {
    clearTimeout(t);
    t = setTimeout(() => go(withQuery({ search: s.value.trim() || null, page: null })), 400);
  };
  filtersPanel(data);
  const note = data.ready < data.total
    ? `<p class="calc-note">${icon("clock", 14)} Подробная статистика рассчитана для ${num(data.ready)} из ${num(data.total)} трейдеров. Остальные досчитываются в фоне — обновите страницу позже.</p>` : "";
  $("#board").innerHTML = note + traderRows(data) + `<div class="table-foot">${pager(data.page, data.size, data.total, (p) => withQuery({ page: p }))}
    <label class="rows-sel">Строк<select id="tr-size">${[10, 25, 50, 100].map((n) => `<option ${n === data.size ? "selected" : ""}>${n}</option>`).join("")}</select></label></div>`;
  $("#tr-size").onchange = (e) => go(withQuery({ size: e.target.value, page: null }));
}

function filtersPanel(data) {
  const q = route().q;
  const box = $("#fpanel");
  box.innerHTML = `<form id="fform"><div class="fgrid">
    <label class="field-sm">Метки трейдера<select name="tag"><option value="">Все метки</option>${Object.entries(data.tags).map(([k, l]) =>
      `<option value="${k}" ${q.get("tag") === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
    ${RANGES.map(([k, label]) => {
      const [lo, hi] = data.ranges[k] || [];
      return `<div class="range"><span>${label}</span><div class="range-in">
        <input name="${k}_min" type="number" step="any" placeholder="${lo != null ? Math.floor(lo).toLocaleString("ru-RU") : "от"}" value="${esc(q.get(k + "_min") || "")}">
        <input name="${k}_max" type="number" step="any" placeholder="${hi != null ? Math.ceil(hi).toLocaleString("ru-RU") : "до"}" value="${esc(q.get(k + "_max") || "")}"></div></div>`;
    }).join("")}</div>
    <div class="factions"><input name="search" type="search" placeholder="Поиск по имени или адресу…" value="${esc(q.get("search") || "")}">
      <button class="btn btn-primary btn-sm">${icon("sliders", 14)} Применить</button><button type="button" class="btn btn-sm" id="fclear">${icon("x", 14)} Сбросить</button></div></form>`;
  box.addEventListener("click", (e) => e.stopPropagation());
  const f = $("#fform");
  f.onsubmit = (ev) => {
    ev.preventDefault();
    const ch = { page: null, tag: f.tag.value || null, search: f.search.value.trim() || null };
    for (const [k] of RANGES) {
      ch[k + "_min"] = f[k + "_min"].value || null;
      ch[k + "_max"] = f[k + "_max"].value || null;
    }
    box.parentElement.classList.remove("open");
    go(withQuery(ch));
  };
  $("#fclear").onclick = () => {
    const ch = { page: null, tag: null, search: null };
    for (const [k] of RANGES) ch[k + "_min"] = ch[k + "_max"] = null;
    go(withQuery(ch));
  };
}

async function trio() {
  const host = $("#trio");
  try {
    const [home, markets] = await Promise.all([api("/api/home"), api("/api/markets?order=volume&offset=0")]);
    const v = home.volume?.data || {};
    const move = v.previous24h ? (v.total24h / v.previous24h - 1) * 100 : null;
    const movers = [];
    for (const e of unwrap(markets) || []) for (const c of e.contracts || []) {
      if (c.change24h != null && c.outcomes?.[0]?.price != null) movers.push({ e, c });
    }
    movers.sort((a, b) => Math.abs(b.c.change24h) - Math.abs(a.c.change24h));
    host.innerHTML = `
      <div class="card stack"><div class="kpi"><div><div class="kpi-val">${icon("trend", 16)} ${usd(v.total24h, 0)}</div><small>Объём Polymarket за 24 ч ${delta(move)}</small></div>${sparkline((v.chart || []).map((p) => p[1]))}</div>
        <div class="kpi"><div><div class="kpi-val">${icon("vol", 16)} ${home.markets?.count != null ? num(home.markets.count) : home.markets?.scanning ? num(home.markets.seen) + "…" : "—"}</div><small>${home.markets?.count == null && home.markets?.scanning ? "Подсчёт рынков идёт" : "Доступные рынки"}</small></div>${sparkline((home.markets?.series || []).map((p) => p[1]))}</div></div>
      <div class="card"><div class="card-head"><h3>${icon("chart", 15)} Рынки в движении</h3><a href="/markets?sort=volume" class="small">Все ${icon("right", 12)}</a></div>
        ${movers.slice(0, 3).map(({ e, c }) => `<a class="mover" href="/event/${esc(e.slug)}?contract=${c.condition}">${thumb(c.image || e.image, 24)}<span>${esc(e.title)}</span><em>${esc(c.label || "Да")}</em>${delta(c.change24h * 100, 1, " п.п.")}</a>`).join("") || '<p class="muted small">Нет данных</p>'}</div>
      <div class="card"><div class="card-head"><h3>Инструменты</h3></div>
        <div class="tool"><div><b>Копирование сделок</b><small>Повторяйте сделки лучших трейдеров на виртуальном счёте.</small></div><a class="btn btn-primary btn-sm" href="/copy-trading">${icon("copy", 14)} Начать</a></div>
        <div class="tool"><div><b>Данные и API</b><small>JSON-доступ к рейтингу, рынкам и сделкам.</small></div><a class="btn btn-gold btn-sm" href="/api">${icon("api", 14)} Открыть API</a></div></div>`;
  } catch (e) {
    host.innerHTML = `<div class="card">${errorBox(e, false)}</div>`;
  }
}
