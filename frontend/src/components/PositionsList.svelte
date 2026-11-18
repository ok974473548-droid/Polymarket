<script lang="ts">
  import type { Json } from "../lib/api";
  import { cents, compactMoney, safeUrl, tone, untilText } from "../lib/format";

  let { list, limit = 8 }: { list: Json[] | null | undefined; limit?: number } = $props();
  let broken = $state<Record<number, boolean>>({});
</script>

{#if !list || !list.length}
  <div class="sub">Открытых позиций нет</div>
{:else}
  <div class="plist">
    {#each list.slice(0, limit) as p, i (i)}
      <a class="prow" href={p.url ? safeUrl(p.url) : undefined} target={p.url ? "_blank" : undefined} rel={p.url ? "noopener" : undefined}>
        {#if p.icon && !broken[i]}
          <img class="picon" src={p.icon} alt="" loading="lazy" referrerpolicy="no-referrer" onerror={() => (broken[i] = true)} />
        {:else}
          <span class="picon"></span>
        {/if}
        <div class="grow">
          <div class="t">{p.title}</div>
          <div class="m">
            <span class="chip b sm">«{p.outcome}»</span><span>{cents(p.avg_cents)} → {cents(p.now_cents)}</span><span>{untilText(p.end_ts)}</span>
            {#if p.we_hold}<span class="chip g sm">у нас тоже</span>{/if}
          </div>
        </div>
        <div class="n"><div class="tnum">{compactMoney(p.value_usd)}</div><div class="sub {tone(p.pnl_usd)}">{compactMoney(p.pnl_usd, { sign: true })}</div></div>
      </a>
    {/each}
  </div>
{/if}
