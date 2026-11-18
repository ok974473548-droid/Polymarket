<script lang="ts">
  // Мини-график (uPlot) — динамика за последние недели; цвет по итогу.
  import Plot from "./Plot.svelte";

  let { values, w = 110, h = 30 }: { values: number[] | null | undefined; w?: number; h?: number } = $props();
  const flat = $derived(!values || values.length < 2 || values.every((v) => v === values[0]));
  const last = $derived(values && values.length ? values[values.length - 1] : 0);
  const color = $derived(last > 0 ? "#86C9A8" : last < 0 ? "#E58C7A" : "#93A7AA");
</script>

<div class="spark" style="width:{w}px;height:{h}px" aria-hidden="true">
  {#if flat}
    <svg viewBox="0 0 {w} {h}" width={w} height={h}><line x1="0" y1={h / 2} x2={w} y2={h / 2} stroke="rgba(236,230,218,.18)" stroke-dasharray="2 4" /></svg>
  {:else if values}
    <Plot mini height={h} xs={values.map((_, i) => i)} lines={[{ label: "", values, color }, { label: "", values: values.map(() => 0), color: "rgba(236,230,218,.14)", width: 1 }]} />
  {/if}
</div>
