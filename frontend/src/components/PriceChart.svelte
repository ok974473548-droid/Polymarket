<script lang="ts">
  // График цены исхода: ступенчатая линия (цена держится до следующей сделки), отметки входа кита и нашей
  // покупки, шапка — текущая цена и изменение; при наведении (или пальцем) — цена в выбранный момент. Цена — в центах.
  import type { Mark } from "../lib/chart";
  import Chart, { type ChartHover } from "./Chart.svelte";

  let {
    points,
    marks = [],
    height = 220,
    label = "Цена исхода",
  }: { points: { ts: number; price: number }[]; marks?: Mark[]; height?: number; label?: string } = $props();

  let hover = $state<ChartHover | null>(null);
  const pts = $derived(points.filter((p) => p && p.price !== null && p.price !== undefined).map((p) => ({ t: p.ts, v: p.price * 100 })));
  const last = $derived(pts.length ? (pts[pts.length - 1].v as number) : null);
  const first = $derived(pts.length ? (pts[0].v as number) : null);
  const shown = $derived(hover?.values.p ?? last);
  const delta = $derived(shown !== null && first !== null ? shown - first : null);
  const c = (v: number) => `${String(Math.round(v * 10) / 10).replace(".", ",")}¢`;
  const up = $derived((delta ?? 0) >= 0);
  const color = $derived(up ? "#2EE59D" : "#FF5C7A");
</script>

{#if pts.length > 1}
  <div class="pc-head">
    <b class="pc-now">{shown !== null ? c(shown) : "—"}</b>
    {#if delta !== null}<span class="pc-d" class:dn={!up}>{up ? "▲" : "▼"} {c(Math.abs(delta))}</span>{/if}
    <span class="sub">{hover ? new Date(hover.t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "за период"}</span>
  </div>
  <Chart
    bind:hover
    {height}
    {label}
    curve="auto"
      clamp={[0, 100]}
    minSpan={4}
    series={[{ key: "p", label: "Цена", color, area: true, points: pts }]}
    marks={marks.filter((m) => m.ts && m.price !== null && m.price !== undefined).map((m) => ({ t: m.ts, v: (m.price as number) * 100, label: m.label, color: m.color }))}
    yFormat={(v) => `${Math.round(v)}¢`}
    tooltip={false}
  />
{:else}
  <div class="sub">Истории цены пока нет</div>
{/if}

<style>
  .pc-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }
  .pc-now {
    font: 600 26px/1 var(--mono);
    letter-spacing: -0.04em;
    color: var(--pearl);
  }
  .pc-d {
    font: 600 13px var(--mono);
    color: var(--good-ink);
  }
  .pc-d.dn {
    color: var(--bad-ink);
  }
</style>
