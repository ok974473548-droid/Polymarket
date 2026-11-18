// Игроки: добавление (одиночное и пачкой), кэш мини-карточек.
import { api, type Json } from "./api";
import { toast } from "./state.svelte";

export const miniCache: Record<string, Json> = {};

export async function addPlayers(wallets: string[], mode: string): Promise<Json> {
  const r = await api("/players/add", { method: "POST", body: { wallets, mode } });
  toast(r.message || r.error || "Готово");
  for (const k of Object.keys(miniCache)) delete miniCache[k];
  return r;
}
