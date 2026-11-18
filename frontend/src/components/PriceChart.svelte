<script lang="ts">
  // График цены исхода (lightweight-charts от TradingView): масштаб колёсиком/жестом, перекрестие,
  // отметки входа кита и нашей покупки. Цена — в центах.
  import { onMount } from "svelte";
  import type { Mark } from "../lib/chart";

  let {
    points,
    marks = [],
    height = 220,
    label = "Цена исхода",
  }: { points: { ts: number; price: number }[]; marks?: Mark[]; height?: number; label?: string } = $props();

  let box: HTMLDivElement | undefined = $state();
  let failed = $state(false);

  onMount(() => {
    let dispose = () => {};
    let alive = true;
    (async () => {
      try {
        const lc = await import("lightweight-charts");
        if (!alive || !box) return;
        const css = getComputedStyle(document.documentElement);
        const ink = css.getPropertyValue("--ink-3").trim() || "#93A7AA";
        const chart = lc.createChart(box, {
          height,
          autoSize: true,
          layout: {
            background: { type: lc.ColorType.Solid, color: "transparent" },
            textColor: ink,
            fontFamily: "Golos Text, system-ui, sans-serif",
            fontSize: 12,
            attributionLogo: true, // лицензия Apache-2.0 библиотеки: ссылка на TradingView
          },
          grid: { vertLines: { color: "rgba(236,230,218,.05)" }, horzLines: { color: "rgba(236,230,218,.07)" } },
          rightPriceScale: { borderVisible: false },
          timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
          crosshair: { mode: lc.CrosshairMode.Normal },
          localization: {
            locale: "ru-RU",
            priceFormatter: (v: number) => `${String(Math.round(v * 10) / 10).replace(".", ",")}¢`,
          },
        });
        const series = chart.addSeries(lc.AreaSeries, {
          lineColor: "#C8A464",
          topColor: "rgba(200,164,100,.22)",
          bottomColor: "rgba(200,164,100,0)",
          lineWidth: 2,
          priceLineVisible: false,
        });
        // точки с одинаковым временем lightweight-charts не принимает — оставляем последнюю
        const byTs = new Map<number, number>();
        for (const p of points) byTs.set(Math.floor(p.ts), p.price * 100);
        const data = [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ time: t as never, value: v }));
        series.setData(data);
        const ms = marks
          .filter((m) => m.ts && m.price !== null && m.price !== undefined && data.length)
          .map((m) => {
            // отметка — на ближайшей точке ряда не позже момента сделки
            let t = data[0].time as unknown as number;
            for (const d of data) if ((d.time as unknown as number) <= m.ts) t = d.time as unknown as number;
            return { time: t as never, position: "belowBar" as const, color: m.color, shape: "arrowUp" as const, text: m.label };
          })
          .sort((a, b) => (a.time as number) - (b.time as number));
        if (ms.length) lc.createSeriesMarkers(series, ms);
        chart.timeScale().fitContent();
        dispose = () => chart.remove();
      } catch {
        failed = true;
      }
    })();
    return () => {
      alive = false;
      dispose();
    };
  });
</script>

<div class="lwc" bind:this={box} style="height:{height}px" role="img" aria-label={label}></div>
{#if failed}<div class="sub">График не загрузился</div>{/if}
