<script lang="ts">
  // График вероятностей исходов события в стиле рынков прогнозов: ступенчатые линии, проценты справа,
  // пульсирующие точки «сейчас» (живые цены дописываются в конец), курсор со значениями всех исходов.
  // Шапка: у одного исхода — крупно «N% шанс» и изменение за период; у нескольких — легенда с текущими %.
  // «Подробно» — прежний график TradingView: масштаб колёсиком и перетаскивание.
  import { type ChartLine, pctOf } from "../lib/events";
  import Chart, { type ChartHover, type ChartSeries } from "./Chart.svelte";
  import EventChartPro from "./EventChartPro.svelte";

  let {
    lines,
    live = {},
    height = 300,
    label = "Вероятности исходов",
  }: { lines: ChartLine[]; live?: Record<string, number | null | undefined>; height?: number; label?: string } = $props();

  let hover = $state<ChartHover | null>(null);
  let pro = $state(false);
  const now = $derived(Math.floor(Date.now() / 1000));

  const series = $derived<ChartSeries[]>(
    lines.map((l) => {
      const pts = l.points.map((p) => ({ t: p.ts, v: p.p * 100 }));
      const lv = live[l.key];
      if (!l.dashed && lv !== null && lv !== undefined && pts.length) pts.push({ t: Math.max(now, pts[pts.length - 1].t), v: lv * 100 });
      return { key: l.key, label: l.title, color: l.color, dashed: l.dashed, step: l.dashed, points: pts, width: l.dashed ? 1.4 : 2.2 };
    }),
  );
  const current = $derived.by(() => {
    const out: Record<string, number | null> = {};
    for (const l of lines) {
      const lv = live[l.key];
      out[l.key] = lv !== null && lv !== undefined ? lv : l.points.length ? l.points[l.points.length - 1].p : null;
    }
    return out;
  });
  const shown = $derived.by(() => {
    if (!hover) return current;
    const out: Record<string, number | null> = {};
    for (const [k, v] of Object.entries(hover.values)) out[k] = v === null ? null : v / 100;
    return out;
  });
  const solid = $derived(lines.filter((x) => !x.dashed));
  const single = $derived(solid.length === 1 ? solid[0] : null);
  const delta = $derived.by(() => {
    if (!single || !single.points.length) return null;
    const v = shown[single.key];
    return v === null || v === undefined ? null : (v - single.points[0].p) * 100;
  });
</script>

<div class="ev-chart">
  {#if single}
    <div class="ev-big">
      <b class="tnum" style="color:{single.color}">{pctOf(shown[single.key])}</b><span class="sub">шанс</span>
      {#if delta !== null && Math.abs(delta) >= 0.05}
        <span class="ev-d" class:dn={delta < 0}>{delta >= 0 ? "▲" : "▼"} {Math.abs(delta).toFixed(1).replace(".", ",")} п.п.</span>
      {/if}
      <button class="link inline pro-btn" onclick={() => (pro = !pro)} aria-pressed={pro}>{pro ? "Обычный вид" : "Подробно"}</button>
    </div>
  {/if}
  <div class="ev-legend" aria-live="off" hidden={!!single}>
    {#each solid as l (l.key)}
      <span class="ev-leg"><i style="background:{l.color}"></i><b class="tnum">{pctOf(shown[l.key])}</b> {l.title}</span>
    {/each}
    {#if hover}<span class="sub ev-leg-time">{new Date(hover.t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
    {:else if !single}<button class="link inline pro-btn ev-leg-time" onclick={() => (pro = !pro)} aria-pressed={pro}>{pro ? "Обычный вид" : "Подробно"}</button>{/if}
  </div>
  {#if pro}
    <EventChartPro {lines} {live} {height} {label} legend={false} />
  {:else}
    <Chart
      bind:hover
      {series}
      {height}
      {label}
      curve="auto"
      clamp={[0, 100]}
      minSpan={6}
      yFormat={(v) => `${Math.round(v)}%`}
      tooltip={solid.length > 1}
    />
  {/if}
</div>

<style>
  .ev-big {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
  }
  .ev-big b {
    font: 600 34px/1 var(--mono);
    letter-spacing: -0.04em;
  }
  .ev-d {
    font: 600 13px var(--mono);
    color: var(--good-ink);
  }
  .ev-d.dn {
    color: var(--bad-ink);
  }
  .pro-btn {
    margin-left: auto;
    font-size: 13px;
  }
</style>
