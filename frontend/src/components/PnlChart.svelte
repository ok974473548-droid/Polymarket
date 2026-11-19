<script lang="ts">
  // Кривая прибыли игрока по готовым данным Polymarket, с выбором периода.
  import { compactMoney, tone } from "../lib/format";
  import Plot from "./Plot.svelte";

  const PERIODS: [string, string][] = [
    ["7", "7 дн"],
    ["30", "30 дн"],
    ["90", "90 дн"],
    ["all", "Всё время"],
  ];
  let { series, height = 170 }: { series: { ts: number; pnl: number }[]; height?: number } = $props();
  let period = $state("90");

  const pts = $derived.by(() => {
    if (!series || !series.length) return [];
    if (period === "all") return series;
    const cut = Date.now() / 1000 - Number(period) * 86400;
    const i = series.findIndex((p) => p.ts >= cut);
    return series.slice(Math.max(0, i - 1));
  });
  const base = $derived(pts.length ? pts[0].pnl : 0);
  const delta = $derived(pts.length ? pts[pts.length - 1].pnl - base : null);
</script>

<div class="pnl-head">
  <div><div class="label">Прибыль за период</div><b class="pnl-delta {tone(delta)}">{compactMoney(delta, { sign: true })}</b></div>
  <div class="seg sm" role="group" aria-label="Период графика">
    {#each PERIODS as [k, l] (k)}<button type="button" aria-pressed={k === period} onclick={() => (period = k)}>{l}</button>{/each}
  </div>
</div>
{#if pts.length > 1}
  {#key period}
    <Plot {height} xs={pts.map((p) => p.ts)} lines={[{ label: "Прибыль", values: pts.map((p) => p.pnl - base), color: "#8B7CFF", fill: true }]}
      fmtY={(v) => compactMoney(v)} label="Прибыль игрока по данным Polymarket" />
  {/key}
{:else}
  <div class="sub">Недостаточно данных для графика</div>
{/if}
