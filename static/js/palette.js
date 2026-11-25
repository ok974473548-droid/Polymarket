// Smart search palette (Ctrl/Cmd+K, "/" or the header search): one box for markets, traders, tags, the user's
// own data and commands, with a preview of the highlighted item and quick actions.
import { $, $$, api, avatar, compact, date, esc, go, icon, pct, price, short, starBtn, state, tagChips, thumb, toast, unwrap, usd } from "./core.js";

export const KIND_LABELS = { events: "Рынки", traders: "Трейдеры", tags: "Теги", mine: "Моё", commands: "Команды" };
export const BEHAVIOUR = { directional: "Направленный", short_crypto: "Короткая крипта", market_maker: "Маркет-мейкер", sniper: "Снайпер",
  hft: "Высокочастотный", scalper: "Скальпер", thin: "Мало данных" };
const MINE_LABELS = { position: "Позиция", order: "Заявка", watch: "Избранное", copy: "Копирование", player: "Автопилот", saved: "Сохранённый поиск" };

const COMMANDS = [
  ["Главная", "/", "home", "главная home"], ["Трейдеры — рейтинг", "/traders", "users", "трейдеры рейтинг лидеры traders"],
  ["Рынки", "/markets", "chart", "рынки события markets"], ["Автопилот", "/autopilot", "pilot", "автопилот autopilot"],
  ["Кандидаты автопилота", "/autopilot?tab=candidates", "pilot", "кандидаты автопилот"], ["Портфель", "/portfolio", "wallet", "портфель позиции"],
  ["Копирование", "/copy-trading", "copy", "копирование копитрейдинг copy"], ["Новое копирование", "/copy-trading/new", "copy", "новое копирование добавить"],
  ["Избранное", "/watchlist", "star", "избранное watchlist"], ["Теги", "/tags", "tag", "теги tags"],
  ["Расширенный поиск", "/search", "search", "поиск фильтры search"], ["API", "/api", "code", "api"],
];

// ------------------------------------------------------------ shared item rendering (palette and /search)
export function trWr(d) {
  return d.copy_wr != null && (d.copy_n || 0) >= 15 ? d.copy_wr * 100 : d.win_rate;
}
export function leadLine(d) {
  const top = (d.top || [])[0];
  if (!top) return "";
  return `<span class="lead">${esc(top.name === "Yes" ? "Да" : top.name === "No" ? "Нет" : top.name)} <b>${price(top.price)}</b></span>`;
}
export function itemIcon(it, size = 30) {
  if (it.kind === "event") return thumb(it.image, size);
  if (it.kind === "trader" || it.kind === "player") return avatar(it.key, it.image, it.title, size);
  if (it.kind === "tag") return `<span class="it-ic">${icon("tag", 15)}</span>`;
  if (it.kind === "command") return `<span class="it-ic">${icon(it.icon || "arrow", 15)}</span>`;
  if (it.kind === "query") return `<span class="it-ic">${icon("clock", 15)}</span>`;
  return `<span class="it-ic">${icon({ position: "wallet", order: "clock", watch: "star", copy: "copy", saved: "bell" }[it.kind] || "search", 15)}</span>`;
}
export function itemMeta(it) {
  const d = it.data || {};
  if (it.kind === "event") {
    const bits = [leadLine(d)];
    if (d.volume24h) bits.push(`${compact(d.volume24h)} за 24 ч`);
    if (d.end) bits.push(`до ${date(d.end)}`);
    return bits.filter(Boolean).join(" · ");
  }
  if (it.kind === "trader") {
    const wr = trWr(d);
    const bits = [];
    if (d.pnl != null) bits.push(`<b class="${d.pnl >= 0 ? "up" : "down"}">${compact(d.pnl, true)}</b>`);
    if (wr != null) bits.push(`винрейт ${pct(wr / 100)}`);
    if (d.rank) bits.push(`#${d.rank}`);
    return bits.join(" · ") || short(it.key);
  }
  if (it.kind === "tag") return d.count != null ? `${d.count} событ.` : "Тег";
  if (it.kind === "position") return `${MINE_LABELS.position} · ${esc(d.outcome === "Yes" ? "Да" : d.outcome === "No" ? "Нет" : d.outcome)} · ${usd(d.value)} · ${esc(d.account)}`;
  if (it.kind === "order") return `${MINE_LABELS.order} · ${d.side === "buy" ? "покупка" : "продажа"}${d.price != null ? " по " + price(d.price) : ""}`;
  if (it.kind === "player") return d.state === "bench" ? "Автопилот · на скамейке" : "Автопилот · в составе";
  if (it.kind === "saved") return `Сохранённый поиск${d.notify ? " · с оповещениями" : ""}`;
  if (it.kind === "copy") return `Копирование · ${esc(d.profile)}${d.paused ? " · на паузе" : ""}`;
  if (it.kind === "watch") return "Избранное";
  return esc(it.subtitle || "");
}
export function badges(it) {
  return (it.watched ? `<span class="it-badge">${icon("star", 11)}</span>` : "") + (it.mine ? `<span class="it-badge mine">моё</span>` : "")
    + (it.external ? `<span class="it-badge">Polymarket</span>` : "");
}

export function visit(it) {
  if (!state.user || !it.url || it.kind === "command" || it.kind === "query") return;
  const sub = it.kind === "event" ? "Рынок" : it.kind === "trader" ? "Трейдер" : it.kind === "tag" ? "Тег" : MINE_LABELS[it.kind] || "";
  api("/api/search/visit", { kind: it.kind, key: it.key, title: it.title, subtitle: sub, image: it.image || "", url: it.url }).catch(() => {});
}

// ------------------------------------------------------------ palette
let dlg = null, items = [], cur = 0, timer = 0, ctl = null, homeData = null;

export function openPalette(initial = "") {
  if (dlg) {
    $("input", dlg).focus();
    return;
  }
  dlg = document.createElement("dialog");
  dlg.className = "pal";
  dlg.setAttribute("aria-label", "Умный поиск");
  dlg.innerHTML = `<div class="pal-box">
    <div class="pal-in">${icon("search", 18)}<input type="text" placeholder="Рынок, трейдер, адрес, ссылка или «киты купили да трамп за сутки»" autocomplete="off" spellcheck="false" aria-label="Поиск" role="combobox" aria-expanded="true" aria-controls="pal-list"><button class="pal-close" data-pal-close aria-label="Закрыть"><kbd>Esc</kbd></button></div>
    <div class="pal-chips" hidden></div>
    <div class="pal-body"><div class="pal-list" id="pal-list" role="listbox"></div><aside class="pal-prev" aria-live="polite"></aside></div>
    <div class="pal-foot"><span><kbd>↑</kbd><kbd>↓</kbd> выбор</span><span><kbd>Enter</kbd> открыть</span><span><kbd>Ctrl</kbd><kbd>Enter</kbd> все результаты</span>
      <span class="pal-pref"><b>@</b> трейдеры <b>#</b> теги <b>$</b> рынки <b>&gt;</b> команды</span></div>
  </div>`;
  document.body.append(dlg);
  const input = $("input", dlg);
  input.value = initial;
  dlg.addEventListener("close", () => {
    dlg.remove();
    dlg = null;
  });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg || e.target.closest("[data-pal-close]")) return dlg.close();
    const row = e.target.closest("[data-i]");
    if (row && !e.target.closest("button, .star-btn")) {
      e.preventDefault();
      choose(Number(row.dataset.i), e.ctrlKey || e.metaKey);
    }
    const act = e.target.closest("[data-act]");
    if (act) action(act.dataset.act, items[cur]);
  });
  dlg.addEventListener("mousemove", (e) => {
    const row = e.target.closest("[data-i]");
    if (row && Number(row.dataset.i) !== cur) select(Number(row.dataset.i), false);
  });
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => lookup(input.value), 140);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) select((cur + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if ((e.ctrlKey || e.metaKey) || !items.length) return all(input.value);
      choose(cur, false);
    } else if (e.key === "Tab" && items[cur]?.kind === "query") {
      e.preventDefault();
      input.value = items[cur].title;
      lookup(input.value);
    }
  });
  dlg.showModal();
  input.focus();
  lookup(initial);
}

function all(q) {
  dlg?.close();
  go("/search" + (q.trim() ? "?q=" + encodeURIComponent(q.trim()) : ""));
}

function choose(i, newTab) {
  const it = items[i];
  if (!it) return;
  if (it.kind === "query") {
    const input = $("input", dlg);
    input.value = it.title;
    return lookup(it.title);
  }
  if (it.kind === "all") return all(it.q);
  visit(it);
  if (newTab) return open(it.url, "_blank");
  dlg.close();
  go(it.url);
}

async function action(what, it) {
  if (!it) return;
  if (what === "open") return choose(cur, false);
  if (what === "copy") {
    dlg.close();
    return go("/copy-trading/new?leader=" + it.key);
  }
  if (what === "bench") {
    if (!state.user) return state.requireLogin?.("Войдите, чтобы пользоваться автопилотом");
    try {
      await api("/api/autopilot/action", { wallet: it.key, action: "bench" });
      toast(`${it.title} — на скамейке автопилота`, "good");
    } catch (e) {
      toast(e.message, "bad");
    }
  }
  if (what === "buy-yes" || what === "buy-no") {
    visit(it);
    dlg.close();
    go(`/event/${it.key}${what === "buy-no" ? "?outcome=1" : ""}`);
  }
}

function rowHtml(it, i) {
  return `<a href="${esc(it.url || "#")}" class="pal-row ${i === cur ? "cur" : ""}" data-i="${i}" role="option" aria-selected="${i === cur}" id="pal-o${i}">
    ${itemIcon(it, 28)}<span class="pal-t"><b>${esc(it.title)}</b><small>${itemMeta(it)}</small></span>${badges(it)}
    ${it.kind === "query" ? `<kbd class="pal-tab">Tab</kbd>` : ""}</a>`;
}

function draw(sections, note = "") {
  const list = $(".pal-list", dlg);
  items = [];
  let html = note;
  for (const [title, rows, extra] of sections) {
    if (!rows.length) continue;
    html += `<div class="pal-head">${esc(title)}${extra || ""}</div>`;
    for (const it of rows) {
      html += rowHtml(it, items.length);
      items.push(it);
    }
  }
  list.innerHTML = html || `<div class="pal-empty">${icon("search", 20)}<b>Ничего не нашлось</b><span>Попробуйте другое слово или откройте <a href="/search">расширенный поиск</a> с фильтрами.</span></div>`;
  cur = 0;
  select(0, false);
}

function select(i, scroll = true) {
  cur = i;
  for (const r of $$(".pal-row", dlg)) {
    const on = Number(r.dataset.i) === i;
    r.classList.toggle("cur", on);
    r.setAttribute("aria-selected", on);
    if (on && scroll) r.scrollIntoView({ block: "nearest" });
  }
  $("input", dlg).setAttribute("aria-activedescendant", "pal-o" + i);
  preview(items[i]);
}

function preview(it) {
  const host = $(".pal-prev", dlg);
  if (!it || ["command", "query", "all"].includes(it.kind)) {
    host.innerHTML = `<div class="pp-tips"><b>Подсказки</b>
      <p><code>трамп</code> — по-русски, найдёт Trump</p><p><code>ьшфьш</code> — неверная раскладка → miami</p>
      <p><code>трейдеры политика винрейт &gt; 70%</code></p><p><code>рынки дешевле $0,20 до конца недели</code></p>
      <p><code>киты купили да франция за сутки</code></p><p><code>мои позиции биткоин</code></p><p>Адрес <code>0x…</code> или ссылка на Polymarket — сразу переход</p></div>`;
    return;
  }
  const d = it.data || {};
  if (it.kind === "event") {
    const rows = (d.top || []).map((o) => `<div class="pp-out"><span>${esc(o.name === "Yes" ? "Да" : o.name === "No" ? "Нет" : o.name)}</span><b>${price(o.price)}</b>${o.change ? `<small class="${o.change >= 0 ? "up" : "down"}">${o.change >= 0 ? "+" : "−"}$${Math.abs(o.change).toFixed(3).replace(".", ",")}</small>` : ""}</div>`).join("");
    const single = (d.top || []).length === 2 && d.top[0].name === "Yes";
    host.innerHTML = `<div class="pp-head">${thumb(it.image, 44)}<b>${esc(it.title)}</b></div>${rows}
      <dl class="pp-kv"><dt>Объём</dt><dd>${compact(d.volume)}</dd><dt>За 24 ч</dt><dd>${compact(d.volume24h)}</dd>${d.end ? `<dt>Окончание</dt><dd>${date(d.end)}</dd>` : ""}${d.count > 1 ? `<dt>Исходов</dt><dd>${d.count}</dd>` : ""}</dl>
      ${(d.tag_labels || []).length ? `<div class="pp-tags">${d.tag_labels.slice(0, 4).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>` : ""}
      <div class="pp-acts">${single ? `<button class="btn btn-sm btn-yes" data-act="buy-yes">Купить Да</button><button class="btn btn-sm btn-no" data-act="buy-no">Купить Нет</button>` : ""}
      <button class="btn btn-sm btn-primary" data-act="open">Открыть</button>${starBtn("event", it.key, it.title, it.image)}</div>`;
    return;
  }
  if (it.kind === "trader") {
    const wr = trWr(d);
    host.innerHTML = `<div class="pp-head">${avatar(it.key, it.image, it.title, 44)}<b>${esc(it.title)}<small>${short(it.key)}</small></b></div>
      <div class="pp-tags">${tagChips(d.tags)}${d.behaviour ? `<span class="chip">${esc(BEHAVIOUR[d.behaviour] || d.behaviour)}</span>` : ""}</div>
      <dl class="pp-kv"><dt>Прибыль</dt><dd class="${(d.pnl || 0) >= 0 ? "up" : "down"}">${compact(d.pnl, true)}</dd>
      ${wr != null ? `<dt>${d.copy_wr != null && (d.copy_n || 0) >= 15 ? "Винрейт копии" : "Винрейт"}</dt><dd>${pct(wr / 100)}</dd>` : ""}
      ${d.networth != null ? `<dt>Капитал</dt><dd>${compact(d.networth)}</dd>` : ""}${d.active != null ? `<dt>Открытых позиций</dt><dd>${d.active}</dd>` : ""}
      ${d.rank ? `<dt>Место в рейтинге</dt><dd>#${d.rank}</dd>` : ""}${d.ap_score != null ? `<dt>Оценка автопилота</dt><dd>${Math.round(d.ap_score)}</dd>` : ""}</dl>
      <div class="pp-acts"><button class="btn btn-sm btn-primary" data-act="open">Профиль</button><button class="btn btn-sm" data-act="copy">${icon("copy", 13)} Копировать</button>
      <button class="btn btn-sm" data-act="bench">${icon("pilot", 13)} На скамейку</button>${starBtn("trader", it.key, it.title, it.image)}</div>`;
    return;
  }
  host.innerHTML = `<div class="pp-head">${itemIcon(it, 44)}<b>${esc(it.title)}</b></div><p class="muted">${itemMeta(it)}</p>
    <div class="pp-acts"><button class="btn btn-sm btn-primary" data-act="open">Открыть</button>${it.kind === "tag" ? starBtn("tag", it.key, it.title) : ""}</div>`;
}

function commands(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return COMMANDS.filter(([, , , hay]) => words.every((w) => hay.includes(w)))
    .map(([title, url, ic]) => ({ kind: "command", key: url, title, url, icon: ic, subtitle: "Перейти" }));
}

function chipsHtml(parsed, notes) {
  const chips = (parsed?.chips || []).map((c) => `<span class="chip chip-f">${esc(c.label)}</span>`).join("");
  const hints = (notes || []).slice(0, 3).map((n) => `<span class="chip chip-note">${icon("sparkle", 11)} ${esc(n)}</span>`).join("");
  const kindLabel = KIND_LABELS[parsed?.kind] || "Сделки";
  const kind = parsed?.kind && !(parsed.chips || []).some((c) => c.label === kindLabel) ? `<span class="chip chip-k">${esc(KIND_LABELS[parsed.kind] || "Сделки")}</span>` : "";
  return kind + chips + hints;
}

async function lookup(raw) {
  if (!dlg) return;
  const q = raw.trim();
  const chips = $(".pal-chips", dlg);
  if (q.startsWith(">")) {
    chips.hidden = true;
    return draw([["Команды", commands(q.slice(1))]]);
  }
  ctl?.abort();
  ctl = new AbortController();
  if (!q) {
    chips.hidden = true;
    try {
      homeData = homeData || (await api("/api/search/home", undefined, { signal: ctl.signal }));
    } catch {
      homeData = { recent: [], opened: [], saved: [], trending: [], top_traders: [] };
    }
    if (!dlg) return;
    const h = homeData;
    return draw([
      ["Недавние запросы", h.recent.map((t) => ({ kind: "query", key: t, title: t, subtitle: "Повторить поиск" }))],
      ["Вы открывали", h.opened.map((o) => ({ kind: o.key.split(":")[0], key: o.key.split(":").slice(1).join(":"), title: o.title, image: o.image, url: o.url, subtitle: o.subtitle, data: {} }))],
      ["Сохранённые поиски", h.saved.slice(0, 5).map((s) => ({ kind: "saved", key: String(s.id), title: s.name, url: `/search?saved=${s.id}`, data: { notify: s.notify } }))],
      ["Сейчас популярно", h.trending],
      ["Трейдеры в ударе", h.top_traders.slice(0, 3)],
      ["Команды", commands("").slice(0, 4)],
    ]);
  }
  try {
    const r = unwrap(await api("/api/search?q=" + encodeURIComponent(q), undefined, { signal: ctl.signal }));
    if (!dlg) return;
    if (r.go) {
      chips.hidden = true;
      return draw([["Переход", [{ kind: "command", key: r.go, title: r.go.startsWith("/traders/") ? "Открыть профиль трейдера " + short(r.go.split("/")[2]) : "Открыть " + r.go, url: r.go, icon: "arrow", subtitle: "Распознана ссылка или адрес" }]]]);
    }
    const c = chipsHtml(r.parsed, r.notes);
    chips.innerHTML = c;
    chips.hidden = !c;
    const g = r.groups || {};
    const relaxed = Object.values(g).some((x) => x.relaxed) ? `<div class="pal-note">${icon("alert", 13)} С учётом категории ничего нет — показаны результаты без неё.</div>` : "";
    const sections = [];
    const order = r.parsed?.kind === "traders" ? ["traders", "events", "tags", "mine"] : ["events", "mine", "traders", "tags"];
    if (r.parsed?.kind === "mine") order.splice(0, order.length, "mine");
    for (const k of order) {
      if (!g[k]) continue;
      const more = g[k].total > g[k].rows.length ? `<a href="/search?q=${encodeURIComponent(q)}&kind=${k}" class="pal-more">ещё ${g[k].total - g[k].rows.length}</a>` : "";
      const rows = k === "mine" && r.parsed?.kind !== "mine" ? g[k].rows.slice(0, 3) : g[k].rows;
      sections.push([KIND_LABELS[k], rows, more]);
    }
    if (r.trades) sections.push(["Сделки", [{ kind: "all", q, key: "trades", title: "Показать сделки по запросу", url: "/search?q=" + encodeURIComponent(q), icon: "bolt", subtitle: "Поиск по последним сделкам на подходящих рынках" }]]);
    const cmds = commands(q);
    if (cmds.length) sections.push(["Команды", cmds.slice(0, 3)]);
    sections.push(["", [{ kind: "all", q, key: "all", title: `Все результаты по «${q}»`, url: "/search?q=" + encodeURIComponent(q), subtitle: "Фильтры, сортировка, сохранение поиска" }]]);
    draw(sections, relaxed);
  } catch (e) {
    if (e.name !== "AbortError" && dlg) $(".pal-list", dlg).innerHTML = `<div class="pal-empty">${icon("alert", 18)}<b>${esc(e.message)}</b></div>`;
  }
}

// ------------------------------------------------------------ global triggers
document.addEventListener("keydown", (e) => {
  const typing = /input|textarea|select/i.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" || (e.ctrlKey || e.metaKey) && e.code === "KeyK") {
    e.preventDefault();
    openPalette();
  } else if (e.key === "/" && !typing && !dlg) {
    e.preventDefault();
    openPalette();
  }
});
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-palette]");
  if (t) {
    e.preventDefault();
    openPalette(t.dataset.palette || "");
  }
});
export function invalidateHome() {
  homeData = null;
}
