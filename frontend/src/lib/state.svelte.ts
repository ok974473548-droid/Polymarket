// Общее состояние интерфейса (руны Svelte 5).
import type { Json } from "./api";

export interface Quote {
  token_id: string;
  bid: number | null;
  ask: number | null;
  live?: boolean;
}

export interface SigNav {
  id: string;
  pending: string[];
  live?: Json;
  size?: number;
}

function pref(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Запомнить настройку интерфейса этого устройства (без хранилища — просто не запоминаем). */
export function savePref(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* приватный режим */
  }
}

export const app = $state({
  status: null as Json,
  statusError: false,
  quotes: {} as Record<string, Quote>,
  live: false, // поток событий подключён
  refresh: 0, // растёт — открытый экран тихо перечитывает данные
  locked: false, // нужен вход по паролю
  notifOpen: false,
  sig: null as SigNav | null,
  seenPending: null as Set<string> | null,
  toast: "",
  toastSeq: 0,
  panel: null as null | { kind: "player" | "market"; id: string; token?: string },
  shortcuts: false,
  palette: false, // палитра команд (Ctrl+K)
  more: false, // телефон: лист «Ещё» со всеми разделами
  rail: pref("wf.rail") === "1", // компьютер: меню свёрнуто до иконок
  hints: pref("wf.hints") === "1", // показывать длинные пояснения на экранах
  now: Date.now(), // часы для обратных отсчётов (раз в секунду)
});

let toastTimer: ReturnType<typeof setTimeout> | undefined;
export function toast(msg: string): void {
  app.toast = msg;
  app.toastSeq++;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (app.toast = ""), 4000);
}

let refreshTimer: ReturnType<typeof setTimeout> | undefined;
/** Тихо перечитать данные открытого экрана — если человек ничего не вводит и не открыто окно. */
export function softRefresh(): void {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    const a = document.activeElement as HTMLElement | null;
    // флажок или переключатель в фокусе — это не набор текста: после него экран должен обновиться
    const input = a as HTMLInputElement | null;
    const toggle = !!input && input.tagName === "INPUT" && ["checkbox", "radio"].includes(input.type);
    const typing = !!a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) && !toggle;
    const dialogOpen = !!document.querySelector("dialog[open]");
    if (typing || dialogOpen || app.notifOpen || app.locked) return;
    app.refresh++;
  }, 120);
}
