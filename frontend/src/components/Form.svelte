<script lang="ts">
  // Форма по неделям: строка с серией и лента недель (зелёная — в плюсе, красная — в минусе).
  import type { Json } from "../lib/api";
  import { compactMoney, dateLabel } from "../lib/format";
  import FormLine from "./FormLine.svelte";

  let { form, weeks, h = 46 }: { form: Json; weeks: { ts: number; pnl: number }[] | null; h?: number } = $props();
  const mx = $derived(Math.max(...(weeks || []).map((w) => Math.abs(w.pnl)), 1));
</script>

<div class="col" style="gap:8px">
  <div class="top"><h3>Форма по неделям</h3><FormLine {form} /></div>
  {#if weeks && weeks.length}
    <div class="form-strip" role="img" aria-label="Результат по неделям за полгода">
      {#each weeks as w (w.ts)}
        <span class="wk {w.pnl > 0.5 ? 'g' : w.pnl < -0.5 ? 'r' : 'z'}" style="--h:{Math.max(3, Math.round((Math.abs(w.pnl) / mx) * (h / 2)))}px"
          title="Неделя с {dateLabel(w.ts)}: {compactMoney(w.pnl, { sign: true })}"></span>
      {/each}
    </div>
    <div class="form-axis sub"><span>{dateLabel(weeks[0].ts)}</span><span>эта неделя</span></div>
  {:else}
    <div class="sub">Недостаточно истории для ленты формы</div>
  {/if}
</div>
