<script lang="ts">
  import { onMount, type Snippet } from "svelte";
  import { api, type Json } from "../lib/api";
  import type { Mark } from "../lib/chart";
  import MarketPanel from "./MarketPanel.svelte";

  let {
    token,
    noChart = false,
    ourMark = null,
    before,
    onloaded,
  }: { token: string; noChart?: boolean; ourMark?: Mark | null; before?: Snippet<[Json]>; onloaded?: (m: Json) => void } = $props();
  let m = $state<Json>(null);
  let err = $state("");

  onMount(async () => {
    try {
      m = await api("/markets/" + encodeURIComponent(token));
      onloaded?.(m);
    } catch (e) {
      err = (e as Error).message;
    }
  });
</script>

{#if err}
  <div class="card err">{err}</div>
{:else if !m}
  <div class="card skeleton">Загружаем рынок: держатели, объём, правила…</div>
{:else}
  {#if before}{@render before(m)}{/if}
  <MarketPanel {m} {noChart} {ourMark} />
{/if}
