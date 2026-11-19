<script lang="ts">
  // Кривые и мини-графики: прибыль игрока, «что было бы», ротация, баланс, динамика в списке китов.
  // С 2.2 рисует общий график Chart (SVG + D3) — прежние параметры сохранены.
  import Chart, { type ChartSeries } from "./Chart.svelte";

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
    label = "",
    minSpan = 0,
    zeroLine = false,
  }: {
    xs: number[];
    lines: Line[];
    height?: number;
    mini?: boolean;
    band?: { lo: number[]; hi: number[]; color: string } | null;
    fmtY?: (v: number) => string;
    fmtX?: (t: number) => string;
    label?: string;
    minSpan?: number;
    zeroLine?: boolean;
  } = $props();

  const series = $derived<ChartSeries[]>(
    lines.map((l, i) => ({
      key: "s" + i,
      label: l.label,
      color: l.color,
      width: l.width,
      dashed: !!l.dash,
      area: !!l.fill,
      points: xs.map((t, k) => ({ t, v: l.values[k] ?? null })),
    })),
  );
</script>

<Chart
  {series}
  {height}
  {mini}
  {minSpan}
  {zeroLine}
  band={band ? { t: xs, lo: band.lo, hi: band.hi, color: band.color } : null}
  yFormat={fmtY}
  endLabels={lines.length <= 4}
  label={label || "График"}
/>
