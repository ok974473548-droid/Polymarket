<script lang="ts">
  // Мини-карточка игрока при наведении на имя (только на устройствах с мышью)
  import { onMount } from "svelte";
  import { api, type Json } from "../lib/api";
  import { compactMoney, playerName, shortAddr, tone } from "../lib/format";
  import { miniCache } from "../lib/players.svelte";
  import { route } from "../lib/router.svelte";
  import Avatar from "./Avatar.svelte";
  import FormLine from "./FormLine.svelte";
  import Spark from "./Spark.svelte";

  let c = $state<Json>(null);
  let shown = $state(false);
  let left = $state(0);
  let top = $state(0);
  let hoverEl: Element | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function show(el: HTMLElement) {
    const w = el.dataset.player as string;
    const r = el.getBoundingClientRect();
    left = Math.min(window.innerWidth - 300, Math.max(8, r.left));
    top = r.bottom + 8 + window.scrollY;
    c = miniCache[w] || null;
    shown = true;
    if (!miniCache[w]) {
      try {
        miniCache[w] = await api(`/players/${encodeURIComponent(w)}?mini=1`);
      } catch (e) {
        miniCache[w] = { ok: false, error: (e as Error).message };
      }
      if (hoverEl === el) c = miniCache[w];
    }
  }

  $effect(() => {
    void route.hash;
    shown = false;
  });

  onMount(() => {
    if (!window.matchMedia || !matchMedia("(hover:hover) and (pointer:fine)").matches) return;
    const over = (e: MouseEvent) => {
      const el = (e.target as Element).closest?.("[data-player]") as HTMLElement | null;
      if (el === hoverEl) return;
      clearTimeout(timer);
      hoverEl = el;
      if (!el) {
        shown = false;
        return;
      }
      timer = setTimeout(() => show(el), 420);
    };
    document.addEventListener("mouseover", over);
    return () => document.removeEventListener("mouseover", over);
  });

  const cum = $derived(
    ((c && c.weeks) || []).slice(-12).reduce((a: number[], w: { pnl: number }) => (a.push((a.length ? a[a.length - 1] : 0) + w.pnl), a), []),
  );
</script>

{#if shown}
  <div class="pop" id="pop" role="tooltip" style="left:{left}px;top:{top}px">
    {#if !c}
      <div class="sub">Загружаем…</div>
    {:else if c.ok === false}
      <div class="sub">{c.error || "Нет данных"}</div>
    {:else}
      <div class="row" style="gap:10px"><Avatar img={c.image} code={c.short} /><div><b>{playerName(c)}</b><div class="sub">{shortAddr(c.wallet)}</div></div></div>
      <div class="mini-kpis">
        <div><div class="label">Всё время</div><b class={tone(c.pnl_all_usd)}>{compactMoney(c.pnl_all_usd, { sign: true })}</b></div>
        <div><div class="label">30 дней</div><b class={tone(c.pnl_30d_usd)}>{compactMoney(c.pnl_30d_usd, { sign: true })}</b></div>
        <div><div class="label">В рынках</div><b>{compactMoney(c.value_usd)}</b></div>
      </div>
      <Spark values={cum} w={220} h={34} />
      <FormLine form={c.form} />
      <div class="sub">Нажмите — подробнее</div>
    {/if}
  </div>
{/if}
