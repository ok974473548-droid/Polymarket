// Форматы чисел и дат — как в прежнем интерфейсе: ru-RU, запятая, деньги со стрелкой у знака.

type Num = number | null | undefined;

export function money(v: Num, { sign = false, cents = false } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const k = cents ? 100 : 1;
  v = Math.round(v * k) / k || 0; // без «−$0»
  const abs = Math.abs(v);
  const s = abs.toLocaleString("ru-RU", { minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
  if (!sign) return (v < 0 ? "−$" : "$") + s;
  if (v > 0) return "↑ +$" + s;
  if (v < 0) return "↓ −$" + s;
  return "→ $" + s;
}

export function compactMoney(v: Num, { sign = false } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v),
    sg = v < 0 ? "−" : sign && v > 0 ? "+" : "";
  if (a >= 1e6) return `${sg}$${(a / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн`;
  if (a >= 1e4) return `${sg}$${(a / 1e3).toLocaleString("ru-RU", { maximumFractionDigits: 0 })} тыс.`;
  return `${sg}$${Math.round(a).toLocaleString("ru-RU")}`;
}

export function pct(v: Num, { sign = true } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  v = Math.round(v * 10) / 10 || 0;
  const s = Math.abs(v).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + "%";
  if (!sign) return s;
  return v > 0 ? "↑ +" + s : v < 0 ? "↓ −" + s : "→ " + s;
}

export const tone = (v: Num): string => (Math.round(v || 0) > 0 ? "up" : Math.round(v || 0) < 0 ? "down" : "");

export function signedPct(v: Num): string {
  const r = Math.round((v || 0) * 10) / 10 || 0;
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toLocaleString("ru-RU") + "%";
}

export const pctText = (v: Num): string =>
  v === null || v === undefined ? "—" : String(Math.round(v * 10) / 10).replace(".", ",") + "%";

export const cents = (v: Num): string =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${String(Math.round(v * 10) / 10).replace(".", ",")}¢`;

export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10,
    m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
}

export const days = (n: number): string => `${n} ${plural(n, "день", "дня", "дней")}`;

export function ago(s: number): string {
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  return `${Math.floor(s / 3600)} ч назад`;
}

export const dateLabel = (ts: number): string =>
  new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

// дата с годом, если ряд длиннее ~10 месяцев
export const dateLabelY = (ts: number, xs: number[]): string =>
  xs[xs.length - 1] - xs[0] > 300 * 86400
    ? new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" })
    : dateLabel(ts);

export const fmtNum = (n: Num): string => Number(n || 0).toLocaleString("ru-RU");

export const safeUrl = (u: unknown): string => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "#");

export const shortAddr = (w: string | null | undefined): string => (w ? w.slice(0, 6) + "…" + w.slice(-4) : "");

export const playerName = (c: { name?: string | null; short?: string; wallet?: string }): string =>
  c.name || `Кит ${c.short || shortAddr(c.wallet)}`;

export function monthsSince(ts: Num): string {
  if (!ts) return "—";
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / (30.4 * 86400)));
  return m < 1 ? "меньше месяца" : m < 12 ? `${m} мес.` : `${Math.floor(m / 12)} г. ${m % 12 ? (m % 12) + " мес." : ""}`;
}

export function untilText(ts: Num): string {
  if (!ts) return "—";
  const d = Math.round((ts - Date.now() / 1000) / 86400);
  return d < 0 ? "срок прошёл" : d === 0 ? "сегодня" : `через ${days(d)}`;
}

export const FORM_TEXT: Record<string, [string, string]> = {
  hot: ["g", "в форме"],
  cold: ["r", "остыл"],
  neutral: ["n", "ровно"],
};

export const statusChip = (r: { status?: string }): string =>
  r.status === "Надёжный" ? "g" : r.status === "Проверяется" ? "a" : r.status === "Только следить" ? "i" : "n";

// темы — как в меню Polymarket (тот же список, что в categories.py)
export const TOPICS: [string, string][] = [
  ["politics", "Политика"],
  ["sports", "Спорт"],
  ["esports", "Киберспорт"],
  ["crypto", "Крипто"],
  ["finance", "Финансы"],
  ["culture", "Культура"],
  ["mentions", "Упоминания"],
  ["weather", "Погода"],
  ["economics", "Экономика"],
  ["tech", "Технологии"],
];

// печать кита: латунное кольцо с кодом; оттенок глубины зависит от кода
const SEAL_TONES = [
  ["#2A2560", "#121428"],
  ["#163A52", "#0C1A28"],
  ["#14443A", "#0A1F1B"],
  ["#3B2152", "#1A0F26"],
  ["#1F3557", "#0E1828"],
];
export function sealTone(code: string): [string, string] {
  let h = 0;
  for (const ch of code) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [hi, lo] = SEAL_TONES[h % SEAL_TONES.length];
  return [hi, lo];
}
