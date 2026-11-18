<script lang="ts">
  import type { Json } from "../lib/api";
  import { cents, pct, safeUrl, tone } from "../lib/format";

  let { s }: { s: Json } = $props();
  const w = $derived(s.whale_stats || {});
  const pr = $derived(s.prices || {});
</script>

{#if s.ai}
  {@const a = s.ai}
  <div class="card col" style="gap:10px">
    <div class="top"><h2 style="margin:0">Оценка ИИ</h2><span class="sub">ИИ не видел ни цену рынка, ни ставку кита</span></div>
    <div class="row" style="gap:28px">
      <div><div class="sub">Вероятность по оценке ИИ</div><div class="big">{a.probability_pct}%</div></div>
      <div><div class="sub">Уверенность</div><div style="font-size:18px;font-weight:600">{a.confidence}</div></div>
      {#if a.base_rate_pct !== null}<div><div class="sub">Обычно такие события</div><div style="font-size:18px;font-weight:600">{a.base_rate_pct}%</div></div>{/if}
    </div>
    <div class="grid2">
      {#each [["За", a.for], ["Против", a.against]] as [title, arr] (title)}
        <div>
          <b>{title}</b>
          {#if arr.length}<ul style="margin:6px 0 0;padding-left:20px">{#each arr as x, i (i)}<li>{x}</li>{/each}</ul>{:else}<div class="sub">—</div>{/if}
        </div>
      {/each}
    </div>
    {#if a.uncertain.length}<div class="sub">Что может изменить оценку: {a.uncertain.join("; ")}</div>{/if}
    {#if !a.trusted}<div class="sub">ИИ ещё не прошёл проверку точности на 300 рынках, поэтому его вес в решении снижен.</div>{/if}
  </div>
{/if}
<div class="card col" style="gap:8px">
  <h2 style="margin:0">Данные для решения</h2>
  <div class="grid3" style="gap:10px">
    <div><div class="sub">Кит купил по</div><b style="font-size:18px">{cents(pr.whale_cents)}</b></div>
    <div><div class="sub">Сейчас можно купить по</div><b style="font-size:18px">{cents(pr.now_cents)}</b></div>
    <div><div class="sub">Шанс по истории кита</div><b style="font-size:18px">{pr.model_cents ?? "—"}%</b></div>
    <div><div class="sub">Кит за 90 дней</div><b class={tone(w.return_90d_pct)}>{pct(w.return_90d_pct)}</b></div>
    <div><div class="sub">Событий в истории (в этой теме)</div><b>{w.events ?? "—"} ({w.events_in_topic ?? "—"})</b></div>
    <div><div class="sub">Держит до закрытия рынка</div><b>{w.hold_pct ?? "—"}% ставок</b></div>
  </div>
  <div class="sub">Если цена «сейчас» заметно выше цены кита, выгода уже частично ушла.</div>
</div>
{#if s.forecasts && s.forecasts.length}
  <div class="card col" style="gap:10px">
    <div class="top"><h2 style="margin:0">Другие рынки прогнозов</h2><span class="sub">Похожие вопросы — проверьте, что речь о том же</span></div>
    {#each s.forecasts as f, i (i)}
      <div class="bet">
        <a class="link" style="font-weight:500" href={safeUrl(f.url)} target="_blank" rel="noopener">{f.title}</a>
        <span class="chip i">{f.source} · {f.probability_pct}%</span>
      </div>
    {/each}
  </div>
{/if}
{#if s.warnings && s.warnings.length}
  <div class="banner warn">
    <div class="grow"><b>Осторожно с новостями</b><ul style="margin:6px 0 0;padding-left:20px">{#each s.warnings as x, i (i)}<li>{x}</li>{/each}</ul></div>
  </div>
{/if}
<div class="card col" style="gap:10px">
  <h2 style="margin:0">Что пишут</h2>
  {#each s.news || [] as n, i (i)}
    <div class="bet">
      <div>
        <a class="link" style="font-weight:500" href={safeUrl(n.url)} target="_blank" rel="noopener">{n.title}</a>
        <div class="sub">{n.source} · {n.kind} · {n.when}</div>
      </div>
      <span class="chip {n.reliable ? 'g' : 'a'}">{n.reliable ? "надёжный источник" : "непроверенный"}</span>
    </div>
  {:else}
    <div class="sub">Свежих новостей по этому вопросу не нашлось.</div>
  {/each}
</div>
