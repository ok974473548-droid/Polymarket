// Живые обновления: состояние системы, поток событий сервера (SSE) и опрос раз в 10 с как страховка.
import { api } from "./api";
import { route } from "./router.svelte";
import { chime } from "./sound";
import { app, softRefresh, toast } from "./state.svelte";

export async function refreshStatus(): Promise<unknown> {
  try {
    const st = await api("/status");
    app.status = st;
    app.statusError = false;
    return st;
  } catch {
    app.statusError = true;
    return null;
  }
}

let es: EventSource | null = null;

export function startLive(): void {
  if (!window.EventSource || es) return;
  es = new EventSource("/api/events");
  const on = () => (app.live = true);
  es.addEventListener("hello", on);
  es.addEventListener("ping", on);
  es.addEventListener("change", (e) => {
    on();
    const c = JSON.parse((e as MessageEvent).data) as { what: string[]; new_pending?: string[] };
    const hash = route.hash;
    if (c.new_pending && c.new_pending.length) {
      chime();
      if (hash !== "#/" && !hash.startsWith("#/signal/"))
        toast(c.new_pending.length > 1 ? `Новых сигналов: ${c.new_pending.length}` : "Новый сигнал — откройте «Сигналы»");
    }
    const has = (k: string) => c.what.includes(k);
    if (hash === "#/" && (has("signals") || has("bets"))) return softRefresh();
    if (hash.startsWith("#/signal/") && has("signals")) {
      // по открытому сигналу — только если изменился он сам (истёк, куплен), а не пришёл другой
      const sig = app.sig;
      if (!sig) return;
      api("/signals/" + encodeURIComponent(sig.id))
        .then((s) => {
          if (s.status !== "pending" || (s.pending_ids || []).length !== (sig.pending || []).length) softRefresh();
        })
        .catch(() => {});
      return;
    }
    if ((hash.startsWith("#/positions") || hash.startsWith("#/bet/")) && has("bets")) return softRefresh();
    void refreshStatus();
  });
  es.addEventListener("price", (e) => {
    const q = JSON.parse((e as MessageEvent).data);
    app.quotes[q.token_id] = q;
  });
  es.onerror = () => (app.live = false); // браузер переподключится сам
}

export function startPolling(): void {
  setInterval(async () => {
    if (document.hidden || app.notifOpen || app.locked || document.querySelector("dialog[open]")) return;
    if (app.live) {
      await refreshStatus(); // поток событий жив — экран обновляется сам
      return;
    }
    const h = route.hash;
    if (h === "#/" || h.startsWith("#/positions")) softRefresh();
    else await refreshStatus();
  }, 10000);
}
