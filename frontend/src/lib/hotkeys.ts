// Горячие клавиши — по физической клавише (e.code), поэтому работают при любой раскладке.
import { route } from "./router.svelte";
import { app } from "./state.svelte";

export const SHORTCUTS: [string, string][] = [
  ["B", "купить по открытому сигналу"],
  ["S", "пропустить сигнал"],
  ["J / K", "следующий / предыдущий сигнал"],
  ["Enter", "открыть выбранный сигнал"],
  ["Esc", "назад к списку сигналов"],
  ["G затем S", "к сигналам"],
  ["G затем B", "к моим ставкам"],
  ["G затем W", "к китам"],
  ["?", "эта подсказка"],
];

let gAt = 0;

function click(sel: string): void {
  const b = document.querySelector(sel) as HTMLButtonElement | null;
  if (b && !b.disabled) b.click();
}

export function hotkey(e: KeyboardEvent): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  const t = e.target as HTMLElement | null;
  if (t && (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable)) return false;
  if (document.querySelector("dialog[open]") || app.locked) return false;
  const hash = route.hash;
  const code = e.code;
  if (e.key === "?" || (code === "Slash" && e.shiftKey)) {
    app.shortcuts = true;
    return true;
  }
  if (gAt && Date.now() - gAt < 1200) {
    gAt = 0;
    if (code === "KeyS") return go("#/");
    if (code === "KeyB") return go("#/positions");
    if (code === "KeyW") return go("#/whales");
  }
  if (code === "KeyG") {
    gAt = Date.now();
    return true;
  }
  if (hash.startsWith("#/signal/")) {
    const ids = app.sig?.pending || [],
      i = ids.indexOf(app.sig?.id || "");
    if (code === "KeyB") return click("[data-act=confirm]"), true;
    if (code === "KeyS") return click("[data-act=skip]"), true;
    if (code === "KeyJ" && i >= 0 && i < ids.length - 1) return go("#/signal/" + encodeURIComponent(ids[i + 1]));
    if (code === "KeyK" && i > 0) return go("#/signal/" + encodeURIComponent(ids[i - 1]));
    if (e.key === "Escape") return go("#/");
  }
  if (hash === "#/" && (code === "KeyJ" || code === "KeyK")) {
    const rows = [...document.querySelectorAll<HTMLElement>("#pending-list .sig-row")];
    if (!rows.length) return true;
    const i = rows.indexOf(document.activeElement as HTMLElement);
    const next = code === "KeyJ" ? Math.min(rows.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
    rows[next].focus();
    return true;
  }
  return false;
}

function go(h: string): true {
  location.hash = h;
  return true;
}

export function nextPendingAfter(id: string): string {
  const ids = (app.sig?.pending || []).filter((x) => x !== id);
  return ids.length ? "#/signal/" + encodeURIComponent(ids[0]) : "#/";
}
