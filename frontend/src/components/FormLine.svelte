<script lang="ts">
  import type { Json } from "../lib/api";
  import { FORM_TEXT, plural } from "../lib/format";

  let { form }: { form: Json } = $props();
  const ft = $derived(FORM_TEXT[form?.state] || FORM_TEXT.neutral);
  const parts = $derived.by(() => {
    const f = form || {};
    const p: string[] = [];
    if (f.streak_now) p.push(`${f.streak_now} ${plural(f.streak_now, "неделя", "недели", "недель")} подряд в плюсе`);
    if (f.green_share !== null && f.green_share !== undefined) p.push(`плюсовых недель ${Math.round(f.green_share * 100)}%`);
    if (f.streak_best) p.push(`лучшая серия ${f.streak_best}`);
    return p.join(" · ");
  });
</script>

{#if form && form.weeks}<div class="row" style="gap:10px"><span class="chip {ft[0]}">{ft[1]}</span><span class="sub">{parts}</span></div>{/if}
