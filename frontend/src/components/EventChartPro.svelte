<script lang="ts">
  // График вероятностей исходов события (lightweight-charts): несколько линий, легенда с текущими %,
  // перекрестие показывает значения всех исходов в эту минуту; живые цены дописываются в конец линий.
  import { onMount } from "svelte";
  import { type ChartLine, pctOf } from "../lib/events";

  let {
    lines,
    live = {},
    height = 300,
    label = "Вероятности исходов",
    legend = true,
  }: { lines: ChartLine[]; live?: Record<string, number | null | undefined>; height?: number; label?: string; legend?: boolean } = $props();

  let box: HTMLDivElement | undefined = $state();
  let hover = $state<Record<string, number> | null>(null);
  let hoverTs = $state<number | null>(null);
  type Api = { chart: any; series: Map<string, any>; last: Map<string, number> }; // eslint-disable-line
  let api: Api | null = null;

  const current = $derived.by(() => {
    const out: Record<string, number | null> = {};
    for (const l of lines) {
      const lv = live[l.key];
      out[l.key] = lv !== null && lv !== undefined ? lv : l.points.length ? l.points[l.points.length - 1].p : null;
    }
    return out;
  });

  onMount(() => {
    let alive = true;
    (async () => {
      const lc = await import("lightweight-charts");
      if (!alive || !box) return;
      const css = getComputedStyle(document.documentElement);
      const ink = css.getPropertyValue("--ink-3").trim() || "#8A93A6";
      const chart = lc.createChart(box, {
        height,
        autoSize: true,
        layout: {
          background: { type: lc.ColorType.Solid, color: "transparent" },
          textColor: ink,
          fontFamily: "JetBrains Mono, ui-monospace, monospace",
          fontSize: 12,
          attributionLogo: true, // лицензия Apache-2.0: ссылка на TradingView (режим «Подробно»)
        },
        grid: { vertLines: { visible: false }, horzLines: { color: "rgba(226,232,240,.07)", style: lc.LineStyle.Dotted } },
        rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.04 } },
        timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
        crosshair: { mode: lc.CrosshairMode.Magnet },
        localization: { locale: "ru-RU", priceFormatter: (v: number) => `${Math.round(v)}%` },
        handleScale: { axisPressedMouseMove: false },
      });
      api = { chart, series: new Map(), last: new Map() };
      chart.subscribeCrosshairMove((param: { time?: unknown; seriesData?: Map<unknown, unknown> }) => {
        if (!param.time || !api) {
          hover = null;
          hoverTs = null;
          return;
        }
        const vals: Record<string, number> = {};
        for (const [key, s] of api.series) {
          const d = param.seriesData?.get(s) as { value?: number } | undefined;
          if (d && typeof d.value === "number") vals[key] = d.value / 100;
        }
        hover = vals;
        hoverTs = param.time as number;
      });
      draw(lc);
    })();
    // перерисовка при смене линий (период, выбор исходов)
    return () => {
      alive = false;
      api?.chart.remove();
      api = null;
    };
  });

  let lcRef: typeof import("lightweight-charts") | null = null;
  function draw(lc?: typeof import("lightweight-charts")) {
    if (lc) lcRef = lc;
    if (!api || !lcRef) return;
    for (const s of api.series.values()) api.chart.removeSeries(s);
    api.series.clear();
    api.last.clear();
    for (const l of lines) {
      const s = api.chart.addSeries(lcRef.LineSeries, {
        color: l.color,
        lineWidth: l.dashed ? 1 : 2,
        lineStyle: l.dashed ? lcRef.LineStyle.Dashed : lcRef.LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerRadius: 4,
        title: "",
      });
      const byTs = new Map<number, number>();
      for (const p of l.points) byTs.set(Math.floor(p.ts), p.p * 100);
      const data = [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ time: t as never, value: v }));
      s.setData(data);
      api.series.set(l.key, s);
      api.last.set(l.key, data.length ? (data[data.length - 1].time as unknown as number) : 0);
    }
    // масштаб сбрасываем только при смене набора линий (а не при обновлении оценки модели раз в минуту)
    const sig = lines.map((l) => l.key).join();
    if (sig !== lastSig) api.chart.timeScale().fitContent();
    lastSig = sig;
  }
  let lastSig = "";

  // новые линии (период/выбор) — перерисовать
  $effect(() => {
    void lines;
    draw();
  });

  // живые цены — точка «сейчас» в конце каждой линии
  $effect(() => {
    const lv = live;
    if (!api) return;
    const now = Math.floor(Date.now() / 1000);
    for (const [key, s] of api.series) {
      const v = lv[key];
      if (v === null || v === undefined) continue;
      const last = api.last.get(key) || 0;
      const t = Math.max(now, last);
      try {
        s.update({ time: t as never, value: v * 100 });
        api.last.set(key, t);
      } catch {
        /* точка раньше последней — пропускаем */
      }
    }
  });

  const shown = $derived(hover || current);
</script>

<div class="ev-chart">
  {#if legend}
  <div class="ev-legend" aria-live="off">
    {#each lines.filter((x) => !x.dashed) as l (l.key)}
      <span class="ev-leg"><i style="background:{l.color}"></i><b class="tnum">{pctOf(shown[l.key])}</b> {l.title}</span>
    {/each}
    {#if hoverTs}<span class="sub ev-leg-time">{new Date(hoverTs * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>{/if}
  </div>
  {/if}
  <div class="lwc" bind:this={box} style="height:{height}px" role="img" aria-label={label}></div>
</div>
