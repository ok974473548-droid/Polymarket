<script lang="ts" module>
  export interface ChartPoint {
    t: number; // секунды
    v: number | null;
  }
  export interface ChartSeries {
    key: string;
    label: string;
    color: string;
    points: ChartPoint[];
    dashed?: boolean;
    width?: number;
    area?: boolean; // мягкая заливка под линией
    hideEnd?: boolean; // без точки и подписи на конце
    step?: boolean; // всегда ступенькой (значение держится до следующего)
  }
  export interface ChartMark {
    t: number;
    v: number;
    label: string;
    color: string;
  }
  export interface ChartBand {
    t: number[];
    lo: (number | null)[];
    hi: (number | null)[];
    color: string;
  }
  export interface ChartHover {
    t: number;
    values: Record<string, number | null>;
  }
</script>

<script lang="ts">
  // Общий график (SVG + D3-шкалы) в стиле рынков прогнозов: чистое поле, пунктирная сетка только по горизонтали,
  // шкала справа, пульсирующая точка на конце линии, курсор с подсказкой (мышь и палец), отметки сделок,
  // линия прорисовывается при смене данных. Значения — как есть; форматирование — yFormat.
  import { bisector, extent } from "d3-array";
  import { scaleLinear, scaleTime } from "d3-scale";
  import { area as d3area, curveLinear, curveMonotoneX, curveStepAfter, line as d3line } from "d3-shape";

  let {
    series,
    height = 220,
    mini = false,
    curve = "linear",
    yFormat = (v: number) => String(Math.round(v * 100) / 100),
    tFormat = defaultT,
    yDomain = null,
    clamp = null,
    minSpan = 0,
    zeroLine = false,
    band = null,
    marks = [],
    endLabels = true,
    tooltip = true,
    animKey = "",
    label = "График",
    hover = $bindable(null),
  }: {
    series: ChartSeries[];
    height?: number;
    mini?: boolean;
    curve?: "linear" | "step" | "smooth" | "auto";
    clamp?: [number, number] | null;
    yFormat?: (v: number) => string;
    tFormat?: (t: number, span: number) => string;
    yDomain?: [number, number] | null;
    minSpan?: number;
    zeroLine?: boolean;
    band?: ChartBand | null;
    marks?: ChartMark[];
    endLabels?: boolean;
    tooltip?: boolean;
    animKey?: string;
    label?: string;
    hover?: ChartHover | null;
  } = $props();

  let width = $state(0);
  const M = $derived(mini ? { t: 4, r: 6, b: 4, l: 2 } : { t: 12, r: 52, b: 24, l: 2 });
  const iw = $derived(Math.max(10, width - M.l - M.r));
  const ih = $derived(Math.max(10, height - M.t - M.b));

  const clean = $derived(
    series.map((s) => ({
      ...s,
      points: s.points.filter((p) => Number.isFinite(p.t) && p.v !== null && Number.isFinite(p.v)).sort((a, b) => a.t - b.t),
    })),
  );
  const tExt = $derived.by<[number, number]>(() => {
    const ts: number[] = [];
    for (const s of clean) if (s.points.length) ts.push(s.points[0].t, s.points[s.points.length - 1].t);
    if (band) ts.push(...band.t);
    const e = extent(ts) as [number | undefined, number | undefined];
    const a = e[0] ?? 0,
      b = e[1] ?? 1;
    return a === b ? [a - 3600, b + 3600] : [a, b];
  });
  const span = $derived(tExt[1] - tExt[0]);
  const vExt = $derived.by<[number, number]>(() => {
    if (yDomain) return yDomain;
    const vs: number[] = [];
    for (const s of clean) for (const p of s.points) vs.push(p.v as number);
    for (const m of marks) vs.push(m.v);
    if (band) for (const x of [...band.lo, ...band.hi]) if (x !== null && Number.isFinite(x)) vs.push(x);
    if (zeroLine) vs.push(0);
    let lo = vs.length ? Math.min(...vs) : 0,
      hi = vs.length ? Math.max(...vs) : 1;
    // честный минимальный размах: мелкое изменение не выглядит обрывом
    const need = Math.max(minSpan, (hi - lo) || Math.abs(hi) * 0.02 || 1);
    const mid = (lo + hi) / 2;
    if (hi - lo < need) (lo = mid - need / 2), (hi = mid + need / 2);
    const pad = (hi - lo) * (mini ? 0.12 : 0.08);
    lo -= pad;
    hi += pad;
    // границы по смыслу (вероятность не бывает меньше 0 % и больше 100 %)
    if (clamp) (lo = Math.max(clamp[0], lo)), (hi = Math.min(clamp[1], hi));
    return [lo, hi];
  });
  const x = $derived(scaleTime().domain([tExt[0] * 1000, tExt[1] * 1000]).range([M.l, M.l + iw]));
  const y = $derived.by(() => {
    const s = scaleLinear().domain(vExt).range([M.t + ih, M.t]);
    if (yDomain) return s;
    s.nice(mini ? 2 : 4);
    if (clamp) {
      const [a, b] = s.domain();
      s.domain([Math.max(clamp[0], a), Math.min(clamp[1], b)]);
    }
    return s;
  });
  const yTicks = $derived(mini ? [] : y.ticks(height < 180 ? 3 : 4));
  const xTicks = $derived(mini ? [] : x.ticks(Math.max(2, Math.min(6, Math.floor(iw / 110)))));

  // «auto»: редкие точки — ступенькой (цена держится до следующей сделки), плотные — линией (без «лесенки»)
  const dense = $derived(clean.some((s) => !s.dashed && s.points.length > iw / 25));
  const cv = $derived(
    curve === "step" || (curve === "auto" && !dense) ? curveStepAfter : curve === "smooth" ? curveMonotoneX : curveLinear,
  );
  const lineGen = $derived(
    d3line<ChartPoint>()
      .x((p) => x(p.t * 1000))
      .y((p) => y(p.v as number))
      .curve(cv),
  );
  const stepGen = $derived(
    d3line<ChartPoint>()
      .x((p) => x(p.t * 1000))
      .y((p) => y(p.v as number))
      .curve(curveStepAfter),
  );
  const pathOf = (s: ChartSeries) => (s.step ? stepGen : lineGen)(s.points) || "";
  const areaGen = $derived(
    d3area<ChartPoint>()
      .x((p) => x(p.t * 1000))
      .y0(M.t + ih)
      .y1((p) => y(p.v as number))
      .curve(cv),
  );
  const bandPath = $derived.by(() => {
    if (!band) return "";
    const pts = band.t.map((t, i) => ({ t, lo: band.lo[i], hi: band.hi[i] })).filter((p) => p.lo !== null && p.hi !== null);
    return (
      d3area<{ t: number; lo: number | null; hi: number | null }>()
        .x((p) => x(p.t * 1000))
        .y0((p) => y(p.lo as number))
        .y1((p) => y(p.hi as number))
        .curve(cv)(pts) || ""
    );
  });

  // концы линий и подписи справа (раздвинуты, чтобы не наезжали)
  const ends = $derived.by(() => {
    const out = clean
      .filter((s) => s.points.length && !s.hideEnd)
      .map((s) => {
        const p = s.points[s.points.length - 1];
        return { key: s.key, color: s.color, cx: x(p.t * 1000), cy: y(p.v as number), ly: y(p.v as number), text: yFormat(p.v as number), dashed: !!s.dashed };
      });
    // раздвигаем подписи вниз, а если упёрлись в низ поля — поднимаем всю стопку
    const sorted = [...out].filter((e) => !e.dashed).sort((a, b) => a.ly - b.ly);
    const GAP = 15,
      bottom = M.t + ih;
    for (let i = 1; i < sorted.length; i++) if (sorted[i].ly - sorted[i - 1].ly < GAP) sorted[i].ly = sorted[i - 1].ly + GAP;
    if (sorted.length && sorted[sorted.length - 1].ly > bottom) {
      sorted[sorted.length - 1].ly = bottom;
      for (let i = sorted.length - 2; i >= 0; i--) if (sorted[i + 1].ly - sorted[i].ly < GAP) sorted[i].ly = sorted[i + 1].ly - GAP;
    }
    return out;
  });

  // курсор: ближайшая точка по времени для каждой линии
  const by = bisector((p: ChartPoint) => p.t).right;
  function valueAt(s: ChartSeries, t: number): number | null {
    const i = by(s.points, t) - 1;
    if (i < 0) return null;
    if (curve !== "step" && i < s.points.length - 1) {
      const a = s.points[i],
        b = s.points[i + 1];
      const k = (t - a.t) / (b.t - a.t || 1);
      return (a.v as number) + ((b.v as number) - (a.v as number)) * k;
    }
    return s.points[i].v;
  }
  let hx = $state<number | null>(null);
  function move(e: PointerEvent) {
    if (mini) return;
    const r = (e.currentTarget as SVGElement).getBoundingClientRect();
    const px = Math.max(M.l, Math.min(M.l + iw, e.clientX - r.left));
    hx = px;
    const t = x.invert(px).getTime() / 1000;
    const values: Record<string, number | null> = {};
    for (const s of clean) values[s.key] = valueAt(s, t);
    hover = { t, values };
  }
  function leave() {
    hx = null;
    hover = null;
  }

  // подсказка: дата и значения всех линий
  const tip = $derived.by(() => {
    if (hx === null || !hover || !tooltip) return null;
    const rows = clean
      .filter((s) => hover!.values[s.key] !== null && hover!.values[s.key] !== undefined)
      .map((s) => ({ key: s.key, label: s.label, color: s.color, v: hover!.values[s.key] as number, cy: y(hover!.values[s.key] as number) }));
    const left = hx > width / 2;
    return { rows, left };
  });

  // подписи шкалы, рядом с которыми стоит подпись конца линии (или курсора), скрываем — не наезжают
  const busyY = $derived<number[]>(
    hx !== null && tip ? tip.rows.map((r) => r.cy) : endLabels ? ends.filter((e) => !e.dashed).map((e) => e.ly) : [],
  );

  function defaultT(t: number, sp: number): string {
    const d = new Date(t * 1000);
    if (sp <= 2 * 86400) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    if (sp <= 200 * 86400) return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }).replace(".", "");
    return d.toLocaleDateString("ru-RU", { month: "short", year: "2-digit" }).replace(".", "");
  }
  function tipTime(t: number): string {
    const d = new Date(t * 1000);
    return span <= 2 * 86400
      ? d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
  }

  const uid = `c${Math.random().toString(36).slice(2, 8)}`;
  const anim = $derived(animKey + "|" + clean.map((s) => s.key + ":" + (s.points[0]?.t ?? "")).join());
</script>

<div class="chart-box" class:mini bind:clientWidth={width} style="height:{height}px">
  {#if width > 0}
    <svg
      {width}
      {height}
      role="img"
      aria-label={label}
      onpointermove={move}
      onpointerdown={move}
      onpointerleave={leave}
      onpointercancel={leave}
    >
      <defs>
        {#each clean as s (s.key)}
          {#if s.area}
            <linearGradient id="{uid}-{s.key}" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stop-color={s.color} stop-opacity={mini ? 0.18 : 0.22} />
              <stop offset="1" stop-color={s.color} stop-opacity="0" />
            </linearGradient>
          {/if}
        {/each}
      </defs>
      <!-- сетка и шкала справа -->
      {#each yTicks as tv (tv)}
        <line class="grid" x1={M.l} x2={M.l + iw} y1={y(tv)} y2={y(tv)} />
        {#if !busyY.some((b) => Math.abs(b - y(tv)) < 13)}<text class="ax" x={M.l + iw + 10} y={y(tv)} dy="0.32em">{yFormat(tv)}</text>{/if}
      {/each}
      {#each xTicks as d (+d)}
        <text class="ax" x={x(d)} y={height - 6} text-anchor="middle">{tFormat(+d / 1000, span)}</text>
      {/each}
      {#if zeroLine && vExt[0] < 0 && vExt[1] > 0}
        <line class="zero" x1={M.l} x2={M.l + iw} y1={y(0)} y2={y(0)} />
      {/if}
      {#if bandPath}<path d={bandPath} fill={band?.color} fill-opacity="0.12" />{/if}

      {#key anim}
        <g class="draw">
          {#each clean as s (s.key)}
            {#if s.points.length > 1 && s.area}
              <path class="area" d={areaGen(s.points) || ""} fill="url(#{uid}-{s.key})" />
            {/if}
          {/each}
          {#each clean as s (s.key)}
            {#if s.points.length > 1}
              <path
                class="ln"
                class:dashed={s.dashed}
                d={pathOf(s)}
                stroke={s.color}
                stroke-width={s.width ?? (mini ? 1.6 : s.dashed ? 1.4 : 2)}
                pathLength={s.dashed ? undefined : 1}
              />
            {/if}
          {/each}
        </g>
      {/key}

      {#each marks as m, i (i)}
        {#if m.t >= tExt[0] && m.t <= tExt[1]}
          <g class="mark" transform="translate({x(m.t * 1000)},{y(m.v)})">
            <circle r="9" fill={m.color} fill-opacity="0.16" />
            <circle r="4.5" fill="var(--bg)" stroke={m.color} stroke-width="2" />
            {#if !mini}<text y="-14" text-anchor="middle" fill={m.color}>{m.label}</text>{/if}
          </g>
        {/if}
      {/each}

      {#each ends as e (e.key)}
        {#if !e.dashed}
          <circle class="pulse" cx={e.cx} cy={e.cy} r={mini ? 5 : 8} fill={e.color} />
          <circle cx={e.cx} cy={e.cy} r={mini ? 2.6 : 4} fill={e.color} stroke="var(--bg)" stroke-width="1.5" />
        {/if}
        {#if e.dashed}
          <circle cx={e.cx} cy={e.cy} r="3.5" fill="var(--bg)" stroke={e.color} stroke-width="1.6" />
        {/if}
        {#if !mini && endLabels && hx === null && !e.dashed}
          <text class="endlab" x={M.l + iw + 10} y={e.ly} dy="0.32em" fill={e.color}>{e.text}</text>
        {/if}
      {/each}

      {#if hx !== null && tip}
        <line class="cursor" x1={hx} x2={hx} y1={M.t} y2={M.t + ih} />
        {#each tip.rows as r (r.key)}
          <circle cx={hx} cy={r.cy} r="4.5" fill={r.color} stroke="var(--bg)" stroke-width="2" />
          <text class="endlab" x={M.l + iw + 10} y={r.cy} dy="0.32em" fill={r.color}>{yFormat(r.v)}</text>
        {/each}
      {/if}
      <rect class="hit" x={M.l} y="0" width={iw} {height} fill="transparent" />
    </svg>
    {#if hx !== null && tip && hover && tip.rows.length}
      <div class="tip" style="{tip.left ? `right:${width - hx + 12}px` : `left:${hx + 12}px`};top:{M.t}px">
        <div class="tip-t">{tipTime(hover.t)}</div>
        {#each tip.rows as r (r.key)}
          <div class="tip-r"><i style="background:{r.color}"></i><span>{r.label}</span><b>{yFormat(r.v)}</b></div>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .chart-box {
    position: relative;
    width: 100%;
    user-select: none;
    -webkit-user-select: none;
  }
  svg {
    display: block;
    overflow: visible;
    touch-action: pan-y; /* вертикально — прокрутка страницы, вбок — курсор */
    cursor: crosshair;
  }
  .mini svg {
    cursor: default;
  }
  .grid {
    stroke: rgba(226, 232, 240, 0.08);
    stroke-dasharray: 2 4;
  }
  .zero {
    stroke: rgba(226, 232, 240, 0.22);
    stroke-dasharray: 3 3;
  }
  .ax {
    font: 500 11px var(--mono);
    fill: var(--faint);
    letter-spacing: -0.02em;
  }
  .endlab {
    font: 600 11.5px var(--mono);
    letter-spacing: -0.02em;
  }
  .ln {
    fill: none;
    stroke-linejoin: round;
    stroke-linecap: round;
  }
  .ln.dashed {
    stroke-dasharray: 5 5;
    opacity: 0.9;
  }
  .mark text {
    font: 600 10.5px var(--sans);
  }
  .cursor {
    stroke: rgba(226, 232, 240, 0.35);
    stroke-dasharray: 3 3;
  }
  .pulse {
    opacity: 0.25;
    transform-box: fill-box;
    transform-origin: center;
  }
  .tip {
    position: absolute;
    z-index: 3;
    pointer-events: none;
    min-width: 150px;
    max-width: 260px;
    padding: 9px 11px;
    border-radius: 10px;
    background: rgba(17, 20, 27, 0.94);
    border: 1px solid var(--line-strong);
    box-shadow: 0 14px 34px -10px rgba(0, 0, 0, 0.8);
    backdrop-filter: blur(10px);
    font-size: 12.5px;
  }
  .tip-t {
    color: var(--muted);
    font-size: 11.5px;
    margin-bottom: 4px;
  }
  .tip-r {
    display: flex;
    align-items: center;
    gap: 7px;
    line-height: 1.6;
  }
  .tip-r i {
    width: 8px;
    height: 8px;
    border-radius: 99px;
    flex: none;
  }
  .tip-r span {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--ink-2);
  }
  .tip-r b {
    font: 600 12.5px var(--mono);
    color: var(--pearl);
  }
  @media (prefers-reduced-motion: no-preference) {
    .draw .ln:not(.dashed) {
      stroke-dasharray: 1;
      stroke-dashoffset: 1;
      animation: draw 0.9s cubic-bezier(0.3, 0.7, 0.2, 1) forwards;
    }
    .draw .ln.dashed,
    .draw .area {
      animation: fade 0.9s ease both;
    }
    .pulse {
      animation: pulse 2s ease-out infinite;
    }
    @keyframes draw {
      to {
        stroke-dashoffset: 0;
      }
    }
    @keyframes fade {
      from {
        opacity: 0;
      }
    }
    @keyframes pulse {
      0% {
        transform: scale(0.6);
        opacity: 0.45;
      }
      100% {
        transform: scale(2.2);
        opacity: 0;
      }
    }
  }
</style>
