// Модуль «События»: общие помощники экранов.
import { TOPICS } from "./format";

export const LINE_COLORS = ["#C8A464", "#6FB3E0", "#E58C4A", "#86C9A8", "#D98AA6", "#A99BE0", "#E3D27A", "#7FC4C0"];

export const TOPIC_LABEL: Record<string, string> = Object.fromEntries([...TOPICS, ["other", "Разное"]]);

/** 0,227 → «22,7%»; меньше 1% — «<1%», больше 99% — «>99%» (как на Polymarket). */
export function pctOf(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  const v = p * 100;
  if (v > 0 && v < 1) return "<1%";
  if (v > 99 && v < 100) return ">99%";
  return (v >= 10 ? Math.round(v) : Math.round(v * 10) / 10).toString().replace(".", ",") + "%";
}

/** Цена в центах с одной цифрой после запятой: 0,229 → «22,9¢». */
export function centsOf(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  const v = Math.round(p * 1000) / 10;
  return v.toString().replace(".", ",") + "¢";
}

export function changeText(d: number | null | undefined): string {
  if (!d) return "";
  const v = Math.round(d * 1000) / 10;
  if (!v) return "";
  return (v > 0 ? "▲ " : "▼ ") + Math.abs(v).toString().replace(".", ",");
}

export function endText(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const days = Math.round((ts * 1000 - Date.now()) / 86400000);
  const date = d.toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
  if (days < 0) return `закрылось ${date}`;
  if (days === 0) return "закроется сегодня";
  return `до ${date}`;
}

export interface Quote {
  bid: number | null;
  ask: number | null;
  bids?: [number, number][];
  asks?: [number, number][];
  live?: boolean;
}

export function mid(q: Quote | undefined | null): number | null {
  if (!q) return null;
  if (q.bid !== null && q.bid !== undefined && q.ask !== null && q.ask !== undefined) {
    // при широком спреде Polymarket показывает последнюю цену; здесь — середина, но не при пустом стакане
    if (q.ask - q.bid <= 0.1) return (q.bid + q.ask) / 2;
    return null;
  }
  return null;
}

export interface ChartLine {
  key: string;
  title: string;
  color: string;
  points: { ts: number; p: number }[];
}
