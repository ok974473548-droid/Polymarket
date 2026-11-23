// Markets, tags (+ one tag), watchlist and search pages.
import { $, api, avatar, CATEGORY_NAMES, compact, emptyBox, errorBox, esc, go, icon, num, route, short, starBtn, state, thumb,
  unwrap, withQuery } from "../core.js";
import { marketCard } from "../cards.js";
import { marketsList } from "./home.js";
import { needLogin } from "../layout.js";

const MARKET_TABS = ["politics", "sports", "crypto", "esports", "finance", "geopolitics", "tech", "culture", "economy", "weather", "ai", "elections", "mentions"];

// ------------------------------------------------------------------ /markets
export function marketsPage(main) {
  const q = route().q;
  const tag = MARKET_TABS.includes(q.get("category")) ? q.get("category") : "";
  const order = q.get("sort") === "new" ? "new" : q.get("sort") === "volume" ? "volume" : q.get("sort") === "liquidity" ? "liquidity" : "oi";
  const titles = { oi: "Топ рынков по открытому интересу", new: "Новые рынки", volume: "Топ рынков по объёму за 24 ч", liquidity: "Топ рынков по ликвидности" };
  main.innerHTML = `<div class="page-head"><h1>Рынки Polymarket</h1><p>Активные события с ценами исходов, объёмом и открытым интересом. Нажмите «Да» или «Нет», чтобы открыть карточку события с тикетом.</p></div>
    <nav class="cat-tabs" aria-label="Категории">
      <a href="${withQuery({ sort: null, category: null })}" class="${!tag && order === "oi" ? "cur" : ""}">${icon("trend", 14)} Обзор</a>
      <a href="${withQuery({ sort: "new", category: null })}" class="${!tag && order === "new" ? "cur" : ""}">${icon("plus", 14)} Новые</a>
      ${MARKET_TABS.map((t) => `<a href="${withQuery({ category: t })}" class="${tag === t ? "cur" : ""}">${esc(CATEGORY_NAMES[t] || t)}</a>`).join("")}</nav>
    <div class="list-head"><h2>${tag ? esc(CATEGORY_NAMES[tag] || tag) + " · " : ""}${titles[order]}</h2>
      <select id="m-sort" class="input-sm" aria-label="Сортировка"><option value="oi">По открытому интересу</option><option value="volume">По объёму 24 ч</option><option value="liquidity">По ликвидности</option><option value="new">Сначала новые</option></select></div>
    <div id="m-list"></div>`;
  $("#m-sort").value = order;
  $("#m-sort").onchange = (e) => go(withQuery({ sort: e.target.value === "oi" ? null : e.target.value }));
  marketsList($("#m-list"), order, tag);
}

// ------------------------------------------------------------------ /tags and /tags/<slug>
export async function tagsPage(main) {
  const q = route().q;
  const phrase = q.get("q") || "";
  main.innerHTML = `<div class="page-head"><h1>Теги</h1><p>Темы Polymarket. Число событий считается по 2 000 самых активных событий за сутки и обновляется в фоне.</p></div>
    <div class="filters-row"><div class="search-inline">${icon("search", 15)}<input id="tag-q" type="search" placeholder="Найти тег" value="${esc(phrase)}" aria-label="Найти тег"></div><span class="muted small" id="tag-meta"></span></div>
    <div id="tag-grid" class="tag-grid"><div class="sk-block tall"></div></div>`;
  let t = 0;
  $("#tag-q").oninput = (e) => {
    clearTimeout(t);
    t = setTimeout(() => go(withQuery({ q: e.target.value.trim() || null }), { replace: true }), 400);
  };
  try {
    const d = await api("/api/tags?q=" + encodeURIComponent(phrase));
    $("#tag-meta").textContent = d.at ? `Просмотрено событий: ${num(d.events)}` : "Подсчёт тегов идёт в фоне…";
    $("#tag-grid").innerHTML = d.rows.length ? d.rows.slice(0, 300).map((r) => `<div class="tag-card"><a href="/tags/${esc(r.slug)}">${icon("tag", 15)}<b>${esc(r.label)}</b></a>
      <small>${r.count != null ? num(r.count) + " событ." : ""}</small>${starBtn("tag", r.slug, r.label)}</div>`).join("")
      : emptyBox(phrase ? "Теги не найдены" : "Теги ещё считаются", phrase ? "Попробуйте другое слово." : "Обновите страницу через пару минут.");
  } catch (e) {
    $("#tag-grid").innerHTML = errorBox(e);
  }
}

export function tagPage(main, slug) {
  const q = route().q;
  const order = q.get("sort") === "new" ? "new" : q.get("sort") === "volume" ? "volume" : "oi";
  const label = state.watch.get("tag:" + slug)?.label || slug.replace(/-/g, " ");
  main.innerHTML = `<div class="page-head row"><div><a class="back" href="/tags">${icon("back", 14)} Все теги</a><h1>${icon("tag", 20)} ${esc(label)}</h1></div>${starBtn("tag", slug, label, "", true)}</div>
    <div class="list-head"><h2>События</h2><select id="tg-sort" class="input-sm" aria-label="Сортировка"><option value="oi">По открытому интересу</option><option value="volume">По объёму 24 ч</option><option value="new">Сначала новые</option></select></div>
    <div id="tg-list"></div>`;
  $("#tg-sort").value = order;
  $("#tg-sort").onchange = (e) => go(withQuery({ sort: e.target.value === "oi" ? null : e.target.value }));
  marketsList($("#tg-list"), order, slug);
}

// ------------------------------------------------------------------ /watchlist
export async function watchlistPage(main) {
  if (!state.user) return needLogin(main, "Избранное", "Войдите, чтобы отмечать трейдеров, рынки и теги звёздочкой и видеть их здесь.");
  const q = route().q;
  const tab = ["traders", "markets", "tags"].includes(q.get("tab")) ? q.get("tab") : "traders";
  const kind = { traders: "trader", markets: "event", tags: "tag" }[tab];
  const counts = { trader: 0, event: 0, tag: 0 };
  for (const r of state.watch.values()) counts[r.kind]++;
  main.innerHTML = `<div class="page-head"><h1>${icon("star", 20)} Избранное</h1><p>Трейдеры, рынки и теги, отмеченные звёздочкой.</p></div>
    <nav class="tabs">${[["traders", "Трейдеры", "trader"], ["markets", "Рынки", "event"], ["tags", "Теги", "tag"]].map(([k, l, kd]) =>
      `<a href="${withQuery({ tab: k === "traders" ? null : k })}" class="${tab === k ? "cur" : ""}">${l} <span class="count">${counts[kd]}</span></a>`).join("")}</nav>
    <div id="w-list"></div>`;
  const items = [...state.watch.values()].filter((r) => r.kind === kind);
  const host = $("#w-list");
  if (!items.length) {
    const action = { trader: '<a class="btn btn-primary" href="/traders">Найти трейдеров</a>', event: '<a class="btn btn-primary" href="/markets">Открыть рынки</a>', tag: '<a class="btn btn-primary" href="/tags">Открыть теги</a>' }[kind];
    host.innerHTML = emptyBox("Здесь пока пусто", "Нажмите на звёздочку рядом с трейдером, рынком или тегом.", action);
    return;
  }
  if (kind === "tag") {
    host.innerHTML = `<div class="tag-grid">${items.map((r) => `<div class="tag-card"><a href="/tags/${esc(r.key)}">${icon("tag", 15)}<b>${esc(r.label || r.key)}</b></a>${starBtn("tag", r.key, r.label)}</div>`).join("")}</div>`;
    return;
  }
  if (kind === "trader") {
    host.innerHTML = '<div class="tablewrap"><div class="sk-block tall"></div></div>';
    const rows = await Promise.all(items.map(async (r) => {
      try {
        const d = await api("/api/trader?wallet=" + r.key);
        return { r, d };
      } catch {
        return { r, d: null };
      }
    }));
    host.innerHTML = `<div class="tablewrap"><table class="table"><thead><tr><th></th><th>Трейдер</th><th class="num">Ранг</th><th class="num">PnL</th><th class="num">Стоимость</th><th class="num">Win rate</th><th></th></tr></thead><tbody>
      ${rows.map(({ r, d }) => {
        const s = d?.stats || {};
        const pnl = d?.overall?.pnl ?? s.pnl;
        return `<tr><td>${starBtn("trader", r.key, r.label, r.image)}</td><td><a class="trader-cell" href="/traders/${r.key}">${avatar(r.key, s.image || r.image, s.name || r.label, 28)}<span>${esc(s.name || r.label || short(r.key))}</span></a></td>
          <td class="num">${d?.overall?.rank ? "#" + num(d.overall.rank) : "—"}</td><td class="num ${(pnl || 0) >= 0 ? "up" : "down"}">${pnl != null ? compact(pnl) : "—"}</td>
          <td class="num">${d?.networth != null ? compact(d.networth) : "—"}</td><td class="num">${s.win_rate != null ? s.win_rate.toFixed(1) + "%" : "—"}</td>
          <td class="num"><a class="btn btn-sm" href="/copy-trading/new?leader=${r.key}">${icon("copy", 14)} Копировать</a></td></tr>`;
      }).join("")}</tbody></table></div>`;
    return;
  }
  host.innerHTML = '<div class="mgrid" id="w-grid"></div>';
  const grid = $("#w-grid");
  for (const r of items) {
    try {
      const e = unwrap(await api("/api/event?slug=" + encodeURIComponent(r.key)));
      grid.insertAdjacentHTML("beforeend", marketCard({ ...e, contracts: e.contracts.slice(0, 2) }));
    } catch {
      grid.insertAdjacentHTML("beforeend", `<article class="mcard"><div class="mc-head">${thumb(r.image, 36)}<h3><a href="/event/${esc(r.key)}">${esc(r.label || r.key)}</a></h3></div><p class="muted small">Событие недоступно или завершено.</p><div class="mc-foot"><span></span>${starBtn("event", r.key, r.label, r.image, true)}</div></article>`);
    }
  }
}

// ------------------------------------------------------------------ /search
export async function searchPage(main) {
  const q = route().q;
  const phrase = (q.get("q") || "").trim();
  const tab = ["markets", "traders", "tags"].includes(q.get("tab")) ? q.get("tab") : "markets";
  main.innerHTML = `<div class="page-head"><h1>Поиск</h1></div>
    <form class="search-big" id="s-form">${icon("search", 18)}<input name="q" type="search" value="${esc(phrase)}" placeholder="Трейдер, адрес 0x…, событие или тег" aria-label="Поиск" autofocus><button class="btn btn-primary">Найти</button></form>
    <nav class="tabs">${[["markets", "Рынки"], ["traders", "Трейдеры"], ["tags", "Теги"]].map(([k, l]) =>
      `<a href="${withQuery({ tab: k === "markets" ? null : k })}" class="${tab === k ? "cur" : ""}" data-keep-scroll>${l} <span class="count" data-count="${k}"></span></a>`).join("")}</nav>
    <div id="s-res"></div>`;
  $("#s-form").onsubmit = (e) => {
    e.preventDefault();
    const v = e.target.q.value.trim();
    if (/^0x[0-9a-fA-F]{40}$/.test(v)) return go("/traders/" + v.toLowerCase());
    go(withQuery({ q: v || null }));
  };
  const host = $("#s-res");
  if (!phrase) {
    host.innerHTML = emptyBox("Введите запрос", "Ищите события, трейдеров по нику или адресу и теги.");
    return;
  }
  host.innerHTML = '<div class="sk-block tall"></div>';
  try {
    const d = unwrap(await api("/api/search?q=" + encodeURIComponent(phrase)));
    const local = await api("/api/traders?search=" + encodeURIComponent(phrase) + "&size=20").catch(() => ({ rows: [] }));
    const known = new Set(d.profiles.map((p) => p.wallet));
    const traders = d.profiles.concat((local.rows || []).filter((r) => !known.has(r.wallet)).map((r) => ({ wallet: r.wallet, name: r.name, image: r.image, pnl: r.pnl })));
    $('[data-count="markets"]').textContent = d.events.length;
    $('[data-count="traders"]').textContent = traders.length;
    $('[data-count="tags"]').textContent = d.tags.length;
    if (tab === "markets") host.innerHTML = d.events.length ? `<div class="mgrid">${d.events.map(marketCard).join("")}</div>` : emptyBox("Рынки не найдены");
    else if (tab === "traders") host.innerHTML = traders.length ? `<div class="list-cards">${traders.map((p) => `<a class="row-card" href="/traders/${p.wallet}">${avatar(p.wallet, p.image, p.name, 36)}<span><b>${esc(p.name || short(p.wallet))}</b><small>${short(p.wallet)}</small></span>${p.pnl != null ? `<b class="${p.pnl >= 0 ? "up" : "down"}">${compact(p.pnl)}</b>` : ""}</a>`).join("")}</div>` : emptyBox("Трейдеры не найдены");
    else host.innerHTML = d.tags.length ? `<div class="tag-grid">${d.tags.map((r) => `<div class="tag-card"><a href="/tags/${esc(r.slug)}">${icon("tag", 15)}<b>${esc(r.label)}</b></a><small>${r.count != null ? num(r.count) + " событ." : ""}</small>${starBtn("tag", r.slug, r.label)}</div>`).join("")}</div>` : emptyBox("Теги не найдены");
  } catch (e) {
    host.innerHTML = errorBox(e);
  }
}
