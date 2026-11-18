// Загрузка данных экрана: первая — с индикатором, повторные (живые обновления) — тихо, без мигания.
import { untrack } from "svelte";
import { api, type Json } from "./api";
import { app } from "./state.svelte";

export interface Loaded<T = Json> {
  data: T | null;
  error: string;
  loading: boolean;
  reload: () => Promise<void>;
}

export function loader<T = Json>(path: () => string | null, fetcher?: (p: string) => Promise<T>): Loaded<T> {
  const st = $state<Loaded<T>>({
    data: null,
    error: "",
    loading: true,
    reload: async () => {
      const p = untrack(path);
      if (!p) return;
      try {
        st.data = await (fetcher ? fetcher(p) : api(p));
        st.error = "";
      } catch (e) {
        if (!st.data) st.error = (e as Error).message;
      } finally {
        st.loading = false;
      }
    },
  });
  $effect(() => {
    void app.refresh;
    void path();
    void st.reload();
  });
  return st;
}
