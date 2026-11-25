// Shared helpers: DOM, formatting, API, routing, dialogs, icons.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ------------------------------------------------------------ formatting
const nf = (digits, min = 0) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits, minimumFractionDigits: min });
const N0 = nf(0), N1 = nf(1), N2 = nf(2, 2);

export function num(v, digits = 0) {
  return v == null || !isFinite(v) ? "—" : nf(digits).format(v);
}
export function usd(v, digits = 2) {
  if (v == null || !isFinite(v)) return "—";
  const s = nf(digits, digits).format(Math.abs(v));
  return (v < 0 ? "-$" : "$") + s;
}
export function compact(v, sign = false) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const [d, suf] = a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "K"] : [1, ""];
  const body = suf ? N1.format(a / d) + suf : N0.format(a);
  return (v < 0 ? "-" : sign ? "+" : "") + "$" + body;
}
export function signedUsd(v, digits = 2) {
  if (v == null || !isFinite(v)) return "—";
  return (v < 0 ? "-$" : "+$") + nf(digits, digits).format(Math.abs(v));
}
export function pct(v, digits = 0) {
  if (v == null || !isFinite(v)) return "—";
  const p = v * 100;
  const d = p > 0 && p < 1 ? 1 : digits;
  return nf(d).format(p) + "%";
}
export function pctRaw(v, digits = 1) {
  return v == null || !isFinite(v) ? "—" : nf(digits).format(v) + "%";
}
// Prices of outcome shares, in dollars: $0,34 (three digits near $0 and $1).
export function cents(p) {
  if (p == null || !isFinite(p)) return "—";
  // Polymarket prices step by $0.001 near $0 and $1: show the third digit only when there is one (0,995, not 0,990 or 1,000).
  const r = Math.round(p * 1000) / 1000;
  if (p > 0 && r === 0) return "<$0,001";
  const d = (r < 0.04 || r > 0.96) && Math.round(r * 1000) % 10 !== 0 ? 3 : 2;
  return "$" + nf(d, d).format(r);
}
export const price = cents;
export function shares(v) {
  return v == null ? "—" : nf(v < 100 ? 2 : 0).format(v);
}
const DT = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" });
const DTT = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
export function date(v) {
  const t = typeof v === "number" ? v * 1000 : Date.parse(v);
  return isFinite(t) ? DT.format(t) : "—";
}
export function dateTime(ts) {
  return ts ? DTT.format(ts * 1000) : "—";
}
export function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "меньше минуты назад";
  if (s < 3600) return Math.floor(s / 60) + " мин назад";
  if (s < 86400) return Math.floor(s / 3600) + " ч назад";
  return Math.floor(s / 86400) + " дн назад";
}
export const short = (w) => (w ? w.slice(0, 6) + "…" + w.slice(-4) : "");
export const yesNo = (s) => ({ yes: "Да", no: "Нет" })[String(s || "").toLowerCase()] || s;

export function img(url) {
  if (!url) return "";
  if (/^https:\/\/(polymarket-upload\.s3[\w.-]*\.amazonaws\.com|[\w-]+\.polymarket\.com)\//.test(url))
    return "/img?u=" + encodeURIComponent(url);
  return "";
}
const HUES = [210, 150, 280, 20, 330, 190, 45, 100];
export function avatar(wallet, image, name, size = 28) {
  const src = img(image);
  if (src) return `<img class="av" src="${esc(src)}" width="${size}" height="${size}" alt="" loading="lazy">`;
  const h = HUES[parseInt((wallet || "0x0").slice(2, 6), 16) % HUES.length];
  const h2 = (h + 60) % 360;
  return `<span class="av av-grad" style="width:${size}px;height:${size}px;background:linear-gradient(135deg,hsl(${h} 70% 55%),hsl(${h2} 75% 45%))" aria-hidden="true"></span>`;
}
export function thumb(url, size = 40, cls = "") {
  const src = img(url);
  return src
    ? `<img class="thumb ${cls}" src="${esc(src)}" width="${size}" height="${size}" alt="" loading="lazy">`
    : `<span class="thumb thumb-empty ${cls}" style="width:${size}px;height:${size}px">${icon("chart", Math.round(size / 2))}</span>`;
}

// ------------------------------------------------------------ icons (lucide-style strokes)
const P = {
  logo: '<path d="M3 17l5-6 4 4 7-9"/><path d="M14 6h5v5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
  down: '<path d="m6 9 6 6 6-6"/>', up: '<path d="m18 15-6-6-6 6"/>',
  right: '<path d="m9 6 6 6-6 6"/>', left: '<path d="m15 6-6 6 6 6"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>', back: '<path d="M19 12H5m6 6-6-6 6-6"/>',
  fire: '<path d="M12 3c1 4 6 5 6 11a6 6 0 0 1-12 0c0-3 2-4 3-6 0 3 2 3 2 3 1-2 1-5 1-8z"/>',
  trend: '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  chart: '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>',
  bell: '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2 .8 3.5 2.8 3.5 6"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  wallet: '<path d="M3 7a2 2 0 0 1 2-2h13v4"/><path d="M3 7v11a2 2 0 0 0 2 2h15V9H5a2 2 0 0 1-2-2z"/><circle cx="16" cy="14.5" r="1"/>',
  home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
  more: '<circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  vol: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  ext: '<path d="M14 3h7v7"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  play: '<path d="M7 5v14l12-7z"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeoff: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.1 3.9M6.6 6.6C3.8 8.5 2 12 2 12s4 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  dollar: '<path d="M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  percent: '<path d="M19 5 5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  alert: '<path d="M12 3 2 21h20z"/><path d="M12 10v5M12 18v.5"/>',
  logout: '<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M16 7l3 3"/>',
  news: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h6v4H7zM16 8h2M16 12h2M7 16h11"/>',
  tag: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="8" cy="8" r="1.5"/>',
  trophy: '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M17 6h3a3 3 0 0 1-3 4M7 6H4a3 3 0 0 0 3 4"/>',
  whale: '<path d="M3 13c0 4 4 6 8 6s9-3 10-9c-2 1-3 1-4 0 0 3-3 5-6 5-3 0-5-1-6-2"/><path d="M17 6c1-2 3-2 4-1-1 0-2 1-2 2"/>',
  ball: '<circle cx="12" cy="12" r="9"/><path d="M12 3a15 15 0 0 1 0 18M3 12h18"/>',
  coin: '<circle cx="12" cy="12" r="9"/><path d="M9 9h4.5a2 2 0 0 1 0 4H9V7v10h5a2 2 0 0 0 0-4"/>',
  bank: '<path d="M3 10 12 4l9 6M5 10v8M9 10v8M15 10v8M19 10v8M3 20h18"/>',
  api: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>',
  pilot: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3v6.5M4.5 15.5l5.3-2.2M19.5 15.5l-5.3-2.2"/>',
  swap: '<path d="M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7"/>',
  pin: '<path d="M9 4h6l-1 6 3 3H7l3-3zM12 13v8"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m6 6 12 12"/>',
  bolt: '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>',
};
export function icon(name, size = 16, cls = "") {
  return `<svg class="ic ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
}

// ------------------------------------------------------------ API
export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}
export async function api(path, body, { signal } = {}) {
  const opts = body === undefined ? { signal } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal };
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "Сервер недоступен. Проверьте, что приложение запущено.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty */
  }
  if (!res.ok) {
    if (res.status === 401 && body === undefined) state.onAuthLost?.();
    throw new ApiError(res.status, (data && data.error) || "Ошибка " + res.status);
  }
  return data;
}
// Many public routes wrap their payload: {data, fetched, stale, error}.
export const unwrap = (r) => (r && "data" in r && "fetched" in r ? r.data : r);

// ------------------------------------------------------------ app state
export const state = {
  user: null,
  account: null,
  setup: false,
  unread: 0,
  watch: new Map(),
  onAuthLost: null,
};

export async function loadSession() {
  const s = await api("/api/session");
  state.user = s.user;
  state.setup = s.setup;
  state.account = s.account || null;
  state.unread = s.unread || 0;
  if (state.user) {
    try {
      const rows = await api("/api/watchlist");
      state.watch = new Map(rows.map((r) => [r.kind + ":" + r.key, r]));
    } catch {
      state.watch = new Map();
    }
  } else state.watch = new Map();
  return s;
}

// ------------------------------------------------------------ routing
const listeners = [];
export function onRoute(fn) {
  listeners.push(fn);
}
export function go(url, { replace = false } = {}) {
  if (url === location.pathname + location.search) return render();
  history[replace ? "replaceState" : "pushState"]({}, "", url);
  render();
}
export function render() {
  for (const fn of listeners) fn();
}
export function route() {
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, q: new URLSearchParams(location.search), path: location.pathname };
}
export function withQuery(changes, base = location.pathname + location.search) {
  const u = new URL(base, location.origin);
  for (const [k, v] of Object.entries(changes)) {
    if (v == null || v === "") u.searchParams.delete(k);
    else u.searchParams.set(k, v);
  }
  return u.pathname + (u.search || "");
}
document.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[href]");
  if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || a.target === "_blank") return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/")) return;
  ev.preventDefault();
  const keepScroll = a.hasAttribute("data-keep-scroll");
  const y = scrollY;
  go(url.pathname + url.search);
  if (keepScroll) requestAnimationFrame(() => scrollTo(0, y));
});
addEventListener("popstate", render);

// ------------------------------------------------------------ UI bits
export function toast(text, kind = "") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", "status");
  el.textContent = text;
  box.append(el);
  setTimeout(() => el.classList.add("out"), 4200);
  setTimeout(() => el.remove(), 4700);
}

export function modal(title, html, { wide = false, onClose } = {}) {
  const d = document.createElement("dialog");
  d.className = "modal" + (wide ? " wide" : "");
  d.innerHTML = `<div class="modal-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Закрыть">${icon("x", 18)}</button></div><div class="modal-body">${html}</div>`;
  document.body.append(d);
  d.addEventListener("click", (e) => {
    if (e.target === d || e.target.closest("[data-close]")) d.close();
  });
  d.addEventListener("close", () => {
    onClose?.();
    d.remove();
  });
  d.showModal();
  return d;
}

export function confirmBox(title, text, okText = "Подтвердить", danger = false) {
  return new Promise((resolve) => {
    let ok = false;
    const d = modal(title, `<p class="muted">${esc(text)}</p><div class="modal-actions"><button class="btn" data-close>Отмена</button><button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-ok>${esc(okText)}</button></div>`, {
      onClose: () => resolve(ok),
    });
    $("[data-ok]", d).onclick = () => {
      ok = true;
      d.close();
    };
  });
}

export function loading(rows = 3) {
  return `<div class="skeleton">${'<div class="sk-line"></div>'.repeat(rows)}</div>`;
}
export function errorBox(e, retry = true) {
  return `<div class="error-box">${icon("alert", 18)}<div><b>Не удалось загрузить данные</b><p>${esc(e?.message || e)}</p></div>${retry ? '<button class="btn btn-sm" data-retry>Повторить</button>' : ""}</div>`;
}
export function emptyBox(title, text = "", action = "") {
  return `<div class="empty-box"><span class="empty-ic">${icon("search", 20)}</span><b>${esc(title)}</b>${text ? `<p>${esc(text)}</p>` : ""}${action}</div>`;
}

export function starBtn(kind, key, label = "", image = "", withText = false) {
  const on = state.watch.has(kind + ":" + key);
  return `<button class="star-btn ${on ? "on" : ""}" data-star="${kind}" data-key="${esc(key)}" data-label="${esc(label)}" data-image="${esc(image)}" aria-pressed="${on}" aria-label="${on ? "Убрать из избранного" : "В избранное"}">${icon("star", 15)}${withText ? `<span>${on ? "В избранном" : "Избранное"}</span>` : ""}</button>`;
}
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-star]");
  if (!b) return;
  ev.preventDefault();
  if (!state.user) return state.requireLogin?.("Войдите, чтобы добавлять в избранное");
  const kind = b.dataset.star, key = b.dataset.key;
  try {
    const r = await api("/api/watchlist", { kind, key, label: b.dataset.label, image: b.dataset.image });
    if (r.on) state.watch.set(kind + ":" + key, { kind, key, label: b.dataset.label, image: b.dataset.image });
    else state.watch.delete(kind + ":" + key);
    for (const el of $$(`[data-star="${kind}"][data-key="${CSS.escape(key)}"]`)) {
      el.classList.toggle("on", r.on);
      el.setAttribute("aria-pressed", r.on);
      const t = el.querySelector("span");
      if (t) t.textContent = r.on ? "В избранном" : "Избранное";
    }
  } catch (e) {
    toast(e.message, "bad");
  }
});

export function bar(gains, losses) {
  if (gains == null || losses == null) return '<span class="muted">—</span>';
  const g = Math.max(0, gains), l = Math.abs(losses), t = g + l;
  const share = t ? (g / t) * 100 : 50;
  return `<div class="gl"><div class="gl-track"><i style="width:${share.toFixed(1)}%"></i></div><div class="gl-nums"><span class="up">+${compact(g).slice(1)}</span><span class="down">-${compact(l).slice(1)}</span></div></div>`;
}
export function minibar(value, max, tone = "green") {
  const w = max > 0 ? Math.max(2, Math.min(100, (value / max) * 100)) : 0;
  return `<div class="mb mb-${tone}"><i style="width:${w}%"></i></div>`;
}
export function winBadge(v) {
  if (v == null) return '<span class="muted">—</span>';
  return `<span class="badge ${v >= 50 ? "badge-green" : "badge-red"}">${pctRaw(v)}</span>`;
}
export function pnlBadge(v, digits = 0) {
  if (v == null) return '<span class="muted">—</span>';
  return `<span class="badge-solid ${v >= 0 ? "pos" : "neg"}">${usd(v, digits)}</span>`;
}
export function delta(v, digits = 1, suffix = "%") {
  if (v == null || !isFinite(v)) return "";
  const cls = v > 0 ? "up" : v < 0 ? "down" : "flat";
  return `<span class="delta ${cls}">${v > 0 ? "▲" : v < 0 ? "▼" : ""}${nf(digits).format(Math.abs(v))}${suffix}</span>`;
}
export function pager(page, size, total, onUrl) {
  const pages = Math.max(1, Math.ceil(total / size));
  if (total <= size) return `<div class="pager"><span class="muted">Показано ${num(total)} из ${num(total)}</span></div>`;
  const list = [];
  for (let i = 1; i <= pages; i++) if (i === 1 || i === pages || Math.abs(i - page) <= 1) list.push(i);
  let html = "", prev = 0;
  for (const i of list) {
    if (i - prev > 1) html += '<span class="pg-gap">…</span>';
    html += `<a class="pg ${i === page ? "cur" : ""}" href="${onUrl(i)}" data-keep-scroll>${i}</a>`;
    prev = i;
  }
  return `<div class="pager"><span class="muted">Показано ${num((page - 1) * size + 1)}–${num(Math.min(total, page * size))} из ${num(total)}</span><div class="pg-list">${page > 1 ? `<a class="pg" href="${onUrl(page - 1)}" data-keep-scroll aria-label="Назад">${icon("left", 14)}</a>` : ""}${html}${page < pages ? `<a class="pg" href="${onUrl(page + 1)}" data-keep-scroll aria-label="Вперёд">${icon("right", 14)}</a>` : ""}</div></div>`;
}

export const TAG_NAMES = {
  whale: "Кит", sniper: "Снайпер", active: "Активный", rookie: "Новичок", hot: "В ударе", top10: "Топ-10", loser: "Серия убытков", big: "Крупная сделка",
};
export function tagChips(tags) {
  return (tags || []).map((t) => `<span class="ttag ttag-${t}">${esc(TAG_NAMES[t] || t)}</span>`).join("");
}

export const CATEGORY_NAMES = {
  overall: "Все", politics: "Политика", sports: "Спорт", crypto: "Крипто", finance: "Финансы", tech: "Технологии",
  economy: "Экономика", culture: "Культура", weather: "Погода", mentions: "Упоминания", esports: "Киберспорт",
  geopolitics: "Геополитика", ai: "ИИ", elections: "Выборы",
};

// Images come through the local /img proxy; when one cannot be loaded put a neutral placeholder in its place.
document.addEventListener("error", (e) => {
  const el = e.target;
  if (el.tagName !== "IMG" || !(el.classList.contains("thumb") || el.classList.contains("av"))) return;
  const ph = document.createElement("span");
  ph.className = el.className + " broken";
  ph.style.width = el.width + "px";
  ph.style.height = el.height + "px";
  ph.style.display = "inline-block";
  ph.setAttribute("aria-hidden", "true");
  el.replaceWith(ph);
}, true);

// ------------------------------------------------------------ copy delay (leader's fill -> our copy)
export function secs(v) {
  return v < 10 ? v.toFixed(1).replace(".", ",") + " с" : v < 120 ? Math.round(v) + " с" : Math.round(v / 60) + " мин";
}
export function delaySummary(rows) {
  const d = rows.filter((r) => r.leader_ts != null).map((r) => Math.max(0, r.at - r.leader_ts)).sort((a, b) => a - b);
  if (!d.length) return "";
  const live = rows.filter((r) => r.leader_ts != null && r.source === "stream").length;
  return `<span class="muted small">Задержка копии: медиана ${secs(d[Math.floor(d.length / 2)])}, 9 из 10 — быстрее ${secs(d[Math.floor(d.length * 0.9)])} · из живого потока ${Math.round((100 * live) / d.length)}%</span>`;
}
