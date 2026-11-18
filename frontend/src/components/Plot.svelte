<script lang="ts">
  // Кривые и мини-графики на uPlot: прибыль игрока, «что было бы», ротация, динамика в списке китов.
  import { onMount } from "svelte";
  import "uplot/dist/uPlot.min.css";

  export interface Line {
    label: string;
    values: (number | null)[];
    color: string;
    width?: number;
    dash?: number[];
    fill?: boolean;
  }

  let {
    xs,
    lines,
    height = 180,
    mini = false,
    band = null,
    fmtY = (v: number) => String(v),
    fmtX = (t: number) => new Date(t * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" }),
    label = "",
  }: {
    xs: number[];
    lines: Line[];
    height?: number;
    mini?: boolean;
    band?: { lo: number[]; hi: number[]; color: string } | null;
    fmtY?: (v: number) => string;
    fmtX?: (t: number) => string;
    label?: string;
  } = $props();

  let box: HTMLDivElement | undefined = $state();

  onMount(() => {
    let plot: { destroy: () => void; setSize: (s: { width: number; height: number }) => void } | null = null;
    let ro: ResizeObserver | null = null;
    let alive = true;
    (async () => {
      const { default: uPlot } = await import("uplot");
      if (!alive || !box) return;
      const ink = "#93A7AA";
      const grid = { stroke: "rgba(236,230,218,.07)", width: 1 };
      type S = Record<string, unknown>;
      // ось X: в подписи под графиком — дата точки под курсором (без служебного «Value»)
      const series: S[] = [{ label: "Дата", value: (_u: unknown, v: number | null) => (v === null || v === undefined ? "—" : fmtX(v)) }];
      const data: (number | null)[][] = [xs];
      if (band) {
        series.push({ stroke: "transparent", points: { show: false }, label: "низ" });
        series.push({ stroke: "transparent", points: { show: false }, label: "верх" });
        data.push(band.lo, band.hi);
      }
      for (const l of lines) {
        series.push({
          label: l.label,
          stroke: l.color,
          width: l.width ?? (mini ? 1.6 : 2),
          dash: l.dash,
          fill: l.fill ? l.color + "22" : undefined,
          points: { show: false },
          value: (_u: unknown, v: number | null) => (v === null || v === undefined ? "—" : fmtY(v)),
        });
        data.push(l.values);
      }
      const opts = {
        width: box.clientWidth || 300,
        height,
        legend: { show: !mini && lines.length > 1, live: true }, // одна линия — подпись не нужна
        cursor: mini ? { show: false } : { points: { size: 6 }, drag: { x: true, y: false } },
        scales: { x: { time: false } },
        bands: band ? [{ series: [2, 1], fill: band.color + "22" }] : [],
        axes: mini
          ? [{ show: false }, { show: false }]
          : [
              { stroke: ink, grid, ticks: { show: false }, values: (_u: unknown, vals: number[]) => vals.map((t) => fmtX(t)), font: "12px Golos Text" },
              { stroke: ink, grid, ticks: { show: false }, values: (_u: unknown, vals: number[]) => vals.map((v) => fmtY(v)), size: 64, font: "12px Golos Text" },
            ],
        series,
      };
      plot = new uPlot(opts as never, data as never, box);
      ro = new ResizeObserver(() => box && plot?.setSize({ width: box.clientWidth, height }));
      ro.observe(box);
    })();
    return () => {
      alive = false;
      ro?.disconnect();
      plot?.destroy();
    };
  });
</script>

<div class="uplot-box" class:mini class:has-band={!!band} bind:this={box} role="img" aria-label={label} style="min-height:{height}px"></div>
