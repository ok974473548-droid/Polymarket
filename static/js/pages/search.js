// Search results page: the phrase as removable filter chips, tabs by kind, facets, sorting, "did you mean" notes,
// saved searches with alerts.
import { $, $$, api, avatar, CATEGORY_NAMES, compact, date, emptyBox, errorBox, esc, go, icon, modal, pct, price, route, short,
  starBtn, state, tagChips, thumb, toast, usd, withQuery } from "../core.js";
import { BEHAVIOUR, KIND_LABELS, badges, invalidateHome, itemIcon, itemMeta, leadLine, openPalette, trWr, visit } from "../palette.js";
import { tradesTable } from "./trader.js";

const TABS = [["all", "Всё"], ["events", "Рынки"], ["traders", "Трейдеры"], ["trades", "Сделки"], ["tags", "Теги"], ["mine", "Моё"]];
const CATS = ["politics", "sports", "crypto", "economy", "finance", "tech", "culture", "geopolitics", "elections", "esports", "weather"];

// Facet definitions per tab: [param, label, options [[value, text]]]; "" = from the phrase / any.
const FACETS = {
  events: [
    ["category", "Категория", CATS.map((c) => [c, CATEGORY_NAMES[c] || c])],
    ["end_days", "Окончание", [["1", "В ближайшие сутки"], ["7", "За неделю"], ["30", "За месяц"], ["90", "За 3 месяца"]]],
    ["price_max", "Цена лидера", [["0.2", "до $0,20"], ["0.5", "до $0,50"]], ["price_min", [["0.8", "от $0,80"], ["0.95", "от $0,95"]]]],
    ["volume_min", "Объём", [["10000", "от $10 тыс."], ["100000", "от $100 тыс."], ["1000000", "от $1 млн"]]],
    ["new", "Новые", [["1", "За последние 7 дней"]]],
  ],
  traders: [
    ["category", "Рейтинг категории", CATS.slice(0, 5).map((c) => [c, CATEGORY_NAMES[c] || c])],
    ["wr_min", "Винрейт", [["55", "от 55%"], ["65", "от 65%"], ["75", "от 75%"]]],
    ["pnl_min", "Прибыль", [["10000", "от $10 тыс."], ["100000", "от $100 тыс."], ["1000000", "от $1 млн"]]],
    ["networth_min", "Капитал", [["100000", "от $100 тыс."], ["1000000", "от $1 млн (киты)"]]],
    ["active", "Активность", [["1", "Есть открытые позиции"]]],
    ["behaviour", "Стиль", Object.entries(BEHAVIOUR)],
    ["tag", "Метки", [["sniper", "Снайпер"], ["hot", "В ударе"], ["rookie", "Новичок"], ["top10", "Топ-10"]]],
  ],
  trades: [
    ["amount_min", "Сумма сделки", [["1000", "от $1 тыс."], ["10000", "от $10 тыс. (киты)"], ["50000", "от $50 тыс."]]],
    ["side", "Сторона", [["BUY", "Покупки"], ["SELL", "Продажи"]]],
    ["outcome", "Исход", [["Yes", "Да"], ["No", "Нет"]]],
    ["period_h", "Период", [["1", "Час"], ["24", "Сутки"], ["168", "Неделя"]]],
    ["category", "Категория", CATS.slice(0, 6).map((c) => [c, CATEGORY_NAMES[c] || c])],
  ],
  mine: [["mine", "Раздел", [["positions", "Позиции"], ["orders", "Заявки"], ["watchlist", "Избранное"], ["copies", "Копирование"], ["autopilot", "Автопилот"]]]],
};
const SORTS = {
  events: [["", "По релевантности"], ["hot", "Горячие (24 ч)"], ["volume", "По объёму"], ["liquidity", "По ликвидности"], ["end", "Скоро закончатся"], ["new", "Новые"]],
  traders: [["", "По релевантности"], ["pnl", "По прибыли"], ["wr", "По винрейту"], ["networth", "По капиталу"]],
};
const FACET_KEYS = ["category", "end_days", "price_min", "price_max", "volume_min", "new", "wr_min", "pnl_min", "networth_min", "active",
  "behaviour", "tag", "amount_min", "side", "outcome", "period_h", "mine", "sort"];

let recorded = "";

export async function searchPage(main) {
  const q = route().q;
  if (q.get("saved")) return openSaved(main, q.get("saved"));
  const phrase = (q.get("q") || "").trim();
  const kindParam = TABS.some(([k]) => k === q.get("kind")) ? q.get("kind") : "";
  const params = new URLSearchParams();
  for (const k of FACET_KEYS) if (q.get(k)) params.set(k, q.get(k));
  main.innerHTML = `<div class="page-head"><h1>Поиск</h1><p class="muted">Пишите как думаете: по-русски, с опечатками, в любой раскладке, с условиями — «трейдеры политика винрейт &gt; 70%».</p></div>
    <form class="search-big" id="s-form">${icon("search", 18)}<input name="q" type="search" value="${esc(phrase)}" placeholder="Трейдер, адрес 0x…, ссылка, рынок или условие" aria-label="Поиск" autocomplete="off">
      <button class="btn btn-primary">Найти</button></form>
    <div class="s-chips" id="s-chips"></div>
    <nav class="tabs" id="s-tabs"></nav>
    <div class="s-layout"><aside class="s-facets" id="s-facets"></aside><section id="s-res"><div class="sk-block tall"></div></section></div>`;
  $("#s-form").onsubmit = (e) => {
    e.preventDefault();
    const v = e.target.q.value.trim();
    go(withQuery({ q: v || null, offset: null }));
  };
  const host = $("#s-res");
  if (!phrase && !kindParam && ![...params.keys()].length) return emptyHome(host);
  const record = phrase && recorded !== phrase && state.user ? "&record=1" : "";
  recorded = phrase;
  if (record) invalidateHome();
  const offset = Number(q.get("offset")) || 0;
  let r;
  try {
    r = await api(`/api/search/full?q=${encodeURIComponent(phrase)}${kindParam ? "&kind=" + kindParam : ""}&${params}&offset=${offset}${record}`);
  } catch (e) {
    host.innerHTML = errorBox(e);
    return;
  }
  if (r.go) return go(r.go, { replace: true });
  const kind = r.kind;
  drawTabs(kind, phrase, params);
  drawChips(r, phrase, kind, params);
  drawFacets(kind, r.filters, params);
  const head = `<div class="s-bar">${resultCount(r)}${sortSelect(kind, r.sort)}<button class="btn btn-sm" id="s-save">${icon("bell", 14)} Сохранить поиск</button></div>
    ${notesHtml(r)}`;
  if (kind === "all") host.innerHTML = head + allHtml(r, phrase);
  else if (kind === "trades") host.innerHTML = head + tradesHtml(r);
  else host.innerHTML = head + listHtml(r.rows || [], kind) + pagerHtml(r, offset);
  bind(host, r, phrase, kind, params);
}

function resultCount(r) {
  if (r.kind === "all") {
    const n = Object.values(r.groups || {}).reduce((s, g) => s + (g.total ?? g.rows.length), 0);
    return `<span class="s-count">Найдено: <b>${n}</b></span>`;
  }
  if (r.kind === "trades") return `<span class="s-count">Сделок: <b>${r.total || 0}</b>${r.scanned?.length ? ` на ${r.scanned.length} рынках` : ""}</span>`;
  return `<span class="s-count">Найдено: <b>${r.total ?? (r.rows || []).length}</b></span>`;
}

function notesHtml(r) {
  const notes = r.notes || [];
  const bits = [];
  if (notes.length) bits.push(`${icon("sparkle", 14)} Понял как: ${notes.slice(0, 4).map((n) => `<b>${esc(n)}</b>`).join(", ")}`);
  const relaxed = r.relaxed || Object.values(r.groups || {}).some((g) => g.relaxed);
  if (relaxed) bits.push(`${icon("alert", 14)} В выбранной категории ничего нет — показаны результаты без неё.`);
  return bits.length ? `<div class="s-notes">${bits.map((b) => `<p>${b}</p>`).join("")}</div>` : "";
}

function drawTabs(kind, phrase, params) {
  $("#s-tabs").innerHTML = TABS.filter(([k]) => k !== "mine" || state.user).map(([k, l]) =>
    `<a href="${withQuery({ kind: k, offset: null })}" class="${kind === k ? "cur" : ""}">${l}</a>`).join("");
}

// Phrase chips: kind and every recognised condition; × removes that piece of the phrase.
function drawChips(r, phrase, kind, params) {
  const p = r.parsed || {};
  const chips = (p.chips || []).map((c, i) => `<span class="chip chip-f">${esc(c.label)}<button data-chip="${i}" aria-label="Убрать условие">${icon("x", 11)}</button></span>`);
  for (const [k, v] of params) {
    if (k === "sort") continue;
    const facet = (FACETS[kind] || []).find((f) => f[0] === k || f[3]?.[0] === k);
    const opt = facet ? (facet[0] === k ? facet[2] : facet[3][1]).find(([val]) => val === v) : null;
    if (v !== "none") chips.push(`<span class="chip chip-p">${esc(facet ? facet[1] + ": " : "")}${esc(opt ? opt[1] : v)}<button data-param="${k}" aria-label="Убрать фильтр">${icon("x", 11)}</button></span>`);
  }
  const words = (p.words || []).filter((w) => w.en).map((w) => `<span class="chip chip-note">${esc(w.word)} → ${esc(w.en)}</span>`);
  $("#s-chips").innerHTML = chips.concat(words).join("");
  $("#s-chips").onclick = (e) => {
    const c = e.target.closest("[data-chip]");
    if (c) {
      const span = p.chips[Number(c.dataset.chip)].span;
      const next = span ? (phrase.slice(0, span[0]) + " " + phrase.slice(span[1])).replace(/\s+/g, " ").trim() : phrase;
      const key = p.chips[Number(c.dataset.chip)].key;
      go(withQuery({ q: next || null, offset: null, [key]: null }));
    }
    const pr = e.target.closest("[data-param]");
    if (pr) go(withQuery({ [pr.dataset.param]: null, offset: null }));
  };
}

function drawFacets(kind, filters, params) {
  const host = $("#s-facets");
  const defs = FACETS[kind];
  if (!defs) {
    host.innerHTML = `<div class="facet-tip">${icon("sparkle", 16)}<p>Выберите вкладку, чтобы уточнить поиск фильтрами. Условия можно писать и прямо в строке: «рынки до конца недели», «трейдеры капитал от 1 млн».</p></div>`;
    return;
  }
  const value = (k) => params.get(k) || (filters?.[k] != null && filters[k] !== false ? String(filters[k]) : "");
  host.innerHTML = defs.map(([k, label, opts, alt]) => {
    const all = opts.map(([v, t]) => [k, v, t]).concat(alt ? alt[1].map(([v, t]) => [alt[0], v, t]) : []);
    const curKey = all.find(([kk, v]) => value(kk) === v);
    const custom = !curKey && (value(k) && value(k) !== "none") ? `<option value="${k}=${esc(value(k))}" selected>${esc(value(k))}</option>` : "";
    return `<label class="facet"><span>${esc(label)}</span><select data-keys="${k}${alt ? "," + alt[0] : ""}">
      <option value="">Любой</option>${custom}${all.map(([kk, v, t]) => `<option value="${kk}=${esc(v)}" ${curKey && curKey[0] === kk && curKey[1] === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
  }).join("") + `<button class="btn btn-sm btn-ghost" id="f-reset">${icon("x", 13)} Сбросить фильтры</button>`;
  host.onchange = (e) => {
    const sel = e.target.closest("select[data-keys]");
    if (!sel) return;
    const changes = { offset: null };
    for (const k of sel.dataset.keys.split(",")) changes[k] = filters?.[k] != null ? "none" : null;
    if (sel.value) {
      const [k, v] = sel.value.split("=");
      changes[k] = v;
    }
    go(withQuery(changes));
  };
  $("#f-reset").onclick = () => {
    const changes = { offset: null };
    for (const kk of FACET_KEYS) changes[kk] = null;
    go(withQuery(changes));
  };
}

function sortSelect(kind, sort) {
  const opts = SORTS[kind];
  if (!opts) return "";
  return `<label class="s-sort">Сортировка <select id="s-sort">${opts.map(([v, t]) => `<option value="${v}" ${String(sort || "") === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>`;
}

function eventRow(it) {
  const d = it.data || {};
  return `<div class="s-item" data-key="${esc(it.key)}"><a href="${it.url}" class="s-main" data-visit>${thumb(it.image, 40)}
      <span class="s-t"><b>${esc(it.title)}</b><small>${(d.tag_labels || []).slice(0, 3).map(esc).join(" · ")}</small></span></a>
    <span class="s-col">${leadLine(d) || "—"}</span>
    <span class="s-col num"><small>24 ч</small>${compact(d.volume24h)}</span>
    <span class="s-col num"><small>Объём</small>${compact(d.volume)}</span>
    <span class="s-col num"><small>Окончание</small>${d.end ? date(d.end) : "—"}</span>
    <span class="s-acts">${badges(it)}${starBtn("event", it.key, it.title, it.image)}</span></div>`;
}
function traderRow(it) {
  const d = it.data || {};
  const wr = trWr(d);
  return `<div class="s-item" data-key="${esc(it.key)}"><a href="${it.url}" class="s-main" data-visit>${avatar(it.key, it.image, it.title, 40)}
      <span class="s-t"><b>${esc(it.title)}</b><small>${short(it.key)}${d.rank ? " · #" + d.rank : ""}</small><span class="ttags">${tagChips(d.tags)}</span></span></a>
    <span class="s-col num"><small>Прибыль</small><b class="${(d.pnl || 0) >= 0 ? "up" : "down"}">${compact(d.pnl, true)}</b></span>
    <span class="s-col num"><small>${d.copy_wr != null && (d.copy_n || 0) >= 15 ? "Винрейт копии" : "Винрейт"}</small>${wr != null ? pct(wr / 100) : "—"}</span>
    <span class="s-col num"><small>Капитал</small>${d.networth != null ? compact(d.networth) : "—"}</span>
    <span class="s-col"><small>Стиль</small>${d.behaviour ? esc(BEHAVIOUR[d.behaviour] || d.behaviour) : "—"}</span>
    <span class="s-acts">${badges(it)}<a class="btn btn-sm" href="/copy-trading/new?leader=${it.key}">${icon("copy", 13)}<span>Копировать</span></a>
      <button class="btn btn-sm" data-bench="${it.key}" data-name="${esc(it.title)}" title="На скамейку автопилота">${icon("pilot", 13)}</button>${starBtn("trader", it.key, it.title, it.image)}</span></div>`;
}
function plainRow(it) {
  return `<div class="s-item"><a href="${it.url}" class="s-main" data-visit>${itemIcon(it, 40)}<span class="s-t"><b>${esc(it.title)}</b><small>${itemMeta(it)}</small></span></a>
    <span class="s-acts">${badges(it)}${it.kind === "tag" ? starBtn("tag", it.key, it.title) : ""}</span></div>`;
}
function rowFor(it) {
  return it.kind === "event" ? eventRow(it) : it.kind === "trader" ? traderRow(it) : plainRow(it);
}
let lastRows = [];
function listHtml(rows, kind) {
  lastRows = rows;
  if (!rows.length) {
    const tip = kind === "trades" ? "" : "Попробуйте убрать часть условий или поискать во вкладке «Всё».";
    return emptyBox("Ничего не найдено", tip);
  }
  return `<div class="s-list s-${kind}">${rows.map((it, i) => rowFor(it).replace('data-visit', `data-visit="${i}"`)).join("")}</div>`;
}
function allHtml(r, phrase) {
  const g = r.groups || {};
  const out = [];
  lastRows = [];
  for (const k of ["mine", "events", "traders", "tags"]) {
    const grp = g[k];
    if (!grp || !grp.rows.length) continue;
    const start = lastRows.length;
    lastRows.push(...grp.rows);
    const total = grp.total ?? grp.rows.length;
    out.push(`<section class="s-group"><div class="s-ghead"><h3>${KIND_LABELS[k]} <span class="count">${total}</span></h3>
      ${total > grp.rows.length || k !== "mine" ? `<a href="${withQuery({ kind: k })}">Все ${KIND_LABELS[k].toLowerCase()} ${icon("arrow", 12)}</a>` : ""}</div>
      <div class="s-list s-${k}">${grp.rows.map((it, i) => rowFor(it).replace("data-visit", `data-visit="${start + i}"`)).join("")}</div></section>`);
  }
  out.push(`<a class="s-trades-hint" href="${withQuery({ kind: "trades" })}">${icon("bolt", 16)}<span><b>Сделки по запросу</b><small>Последние сделки на подходящих рынках — с фильтрами по сумме, стороне и периоду</small></span>${icon("arrow", 14)}</a>`);
  return out.length > 1 ? out.join("") : emptyBox("Ничего не найдено", `По «${phrase}» нет совпадений. Проверьте написание или упростите запрос.`) + out.join("");
}
function tradesHtml(r) {
  const scanned = (r.scanned || []).map((e) => `<a href="/event/${esc(e.slug)}" class="chip">${esc(e.title)}</a>`).join("");
  const rows = r.rows || [];
  return (scanned ? `<div class="s-scanned"><small>Рынки:</small> ${scanned}</div>` : "")
    + (rows.length ? `<div class="tablewrap">${tradesTable(rows.map((t) => ({ ...t, tags: t.tags || [] })), { withTrader: true, showTags: true })}</div>`
      : emptyBox("Сделок не найдено", "Ослабьте условия: уменьшите сумму или увеличьте период."));
}
function pagerHtml(r, offset) {
  const total = r.total || 0;
  if (total <= 50) return "";
  return `<div class="pager">${offset > 0 ? `<a class="btn btn-sm" href="${withQuery({ offset: Math.max(0, offset - 50) || null })}">${icon("left", 13)} Назад</a>` : ""}
    <span class="muted">${offset + 1}–${Math.min(total, offset + 50)} из ${total}</span>
    ${offset + 50 < total ? `<a class="btn btn-sm" href="${withQuery({ offset: offset + 50 })}">Дальше ${icon("right", 13)}</a>` : ""}</div>`;
}

function bind(host, r, phrase, kind, params) {
  $("#s-sort")?.addEventListener("change", (e) => go(withQuery({ sort: e.target.value || (r.parsed?.sort ? "none" : null), offset: null })));
  host.addEventListener("click", async (e) => {
    const v = e.target.closest("[data-visit]");
    if (v && v.dataset.visit !== "") visit(lastRows[Number(v.dataset.visit)] || {});
    const b = e.target.closest("[data-bench]");
    if (b) {
      e.preventDefault();
      if (!state.user) return state.requireLogin?.("Войдите, чтобы пользоваться автопилотом");
      try {
        await api("/api/autopilot/action", { wallet: b.dataset.bench, action: "bench" });
        toast(`${b.dataset.name} — на скамейке автопилота`, "good");
      } catch (err) {
        toast(err.message, "bad");
      }
    }
  });
  $("#s-save").onclick = () => saveDialog(phrase, kind, params);
}

function saveDialog(phrase, kind, params) {
  if (!state.user) return state.requireLogin?.("Войдите, чтобы сохранять поиски");
  const p = Object.fromEntries(params);
  const d = modal("Сохранить поиск", `<form id="sv-form" class="form">
      <label>Название<input name="name" maxlength="80" required value="${esc(phrase || KIND_LABELS[kind] || "Поиск")}"></label>
      <p class="muted small">Запрос: <b>${esc(phrase || "—")}</b>${kind !== "all" ? ` · ${esc(KIND_LABELS[kind] || "Сделки")}` : ""}${Object.keys(p).length ? ` · фильтров: ${Object.keys(p).length}` : ""}</p>
      <label class="check"><input type="checkbox" name="notify" ${kind === "trades" || kind === "events" ? "checked" : ""}> Оповещать о новых результатах
        <small>Проверка раз в 5 минут. Уведомление придёт в колокольчик, когда появится новый рынок, трейдер или сделка.</small></label>
      <label class="check"><input type="checkbox" name="pinned" checked> Показывать в быстром поиске</label>
      <div class="modal-actions"><button type="button" class="btn" data-close>Отмена</button><button class="btn btn-primary">Сохранить</button></div></form>`);
  $("#sv-form", d).onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/search/saved", { name: f.name.value, query: phrase, kind, params: p, notify: f.notify.checked, pinned: f.pinned.checked });
      invalidateHome();
      toast("Поиск сохранён", "good");
      d.close();
    } catch (err) {
      toast(err.message, "bad");
    }
  };
}

async function openSaved(main, id) {
  if (!state.user) return go("/search", { replace: true });
  try {
    const { rows } = await api("/api/search/saved");
    const s = rows.find((x) => String(x.id) === String(id));
    if (!s) {
      toast("Сохранённый поиск не найден", "bad");
      return go("/search", { replace: true });
    }
    const q = new URLSearchParams({ ...(s.query ? { q: s.query } : {}), ...(s.kind !== "all" ? { kind: s.kind } : {}), ...s.params });
    recorded = s.query;
    go("/search?" + q, { replace: true });
  } catch (e) {
    main.innerHTML = errorBox(e);
  }
}

async function emptyHome(host) {
  $("#s-tabs").innerHTML = "";
  $("#s-facets").innerHTML = `<div class="facet-tip">${icon("sparkle", 16)}<p>Нажмите <kbd>Ctrl</kbd> <kbd>K</kbd> на любой странице, чтобы искать, не уходя с неё.</p></div>`;
  const examples = ["трамп", "трейдеры политика винрейт > 70% активные", "рынки дешевле $0,20 до конца недели", "киты купили да за сутки",
    "биткоин горячие", "@", "мои позиции", "ьшфьш"];
  let h = { recent: [], saved: [], trending: [], top_traders: [] };
  try {
    h = await api("/api/search/home");
  } catch { /* examples are enough */ }
  host.innerHTML = `<section class="s-group"><div class="s-ghead"><h3>Попробуйте</h3></div><div class="s-examples">${examples.map((x) =>
      `<a class="chip" href="/search?q=${encodeURIComponent(x)}">${esc(x)}</a>`).join("")}</div></section>
    ${h.saved?.length ? `<section class="s-group"><div class="s-ghead"><h3>Сохранённые поиски</h3></div><div class="s-list">${h.saved.map(savedRow).join("")}</div></section>` : ""}
    ${h.recent?.length ? `<section class="s-group"><div class="s-ghead"><h3>Недавние запросы</h3><button class="btn btn-sm btn-ghost" id="s-clear">Очистить историю</button></div>
      <div class="s-examples">${h.recent.map((x) => `<a class="chip" href="/search?q=${encodeURIComponent(x)}">${icon("clock", 12)} ${esc(x)}</a>`).join("")}</div></section>` : ""}
    ${h.trending?.length ? `<section class="s-group"><div class="s-ghead"><h3>Сейчас популярно</h3></div><div class="s-list s-events">${h.trending.map(eventRow).join("")}</div></section>` : ""}
    <p class="muted small">В индексе: ${h.index ? `${h.index.events} рынков, ${h.index.traders} трейдеров, ${h.index.tags} тегов` : "—"}. Индекс обновляется в фоне.</p>`;
  $("#s-clear")?.addEventListener("click", async () => {
    await api("/api/search/recent/clear", {});
    invalidateHome();
    emptyHome(host);
  });
  host.onclick = async (e) => {
    const b = e.target.closest("[data-saved-act]");
    if (!b) return;
    e.preventDefault();
    const id = b.dataset.id;
    try {
      if (b.dataset.savedAct === "delete") await api("/api/search/saved/delete", { id });
      else await api("/api/search/saved/update", { id, notify: b.dataset.on !== "1" });
      invalidateHome();
      emptyHome(host);
    } catch (err) {
      toast(err.message, "bad");
    }
  };
}

function savedRow(s) {
  return `<div class="s-item"><a href="/search?saved=${s.id}" class="s-main"><span class="it-ic">${icon("search", 15)}</span>
      <span class="s-t"><b>${esc(s.name)}</b><small>${esc(s.query || "")}${s.kind !== "all" ? " · " + esc(KIND_LABELS[s.kind] || "Сделки") : ""}${s.hits ? ` · новых найдено: ${s.hits}` : ""}</small></span></a>
    <span class="s-acts"><button class="btn btn-sm ${s.notify ? "btn-on" : ""}" data-saved-act="notify" data-id="${s.id}" data-on="${s.notify ? 1 : 0}" title="Оповещения">${icon("bell", 13)}<span>${s.notify ? "Оповещения вкл." : "Оповещать"}</span></button>
      <button class="icon-btn" data-saved-act="delete" data-id="${s.id}" aria-label="Удалить">${icon("x", 15)}</button></span></div>`;
}

export { openPalette };
