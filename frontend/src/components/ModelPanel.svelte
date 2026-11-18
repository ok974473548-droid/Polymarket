<script lang="ts">
  // Оценка модели по исходам: «лестница» — цена рынка → независимые оценки (вес, основание) → сводная оценка →
  // итог со сжатием к цене, интервал и уверенность. Переключатель «С ИИ / Без ИИ», ход анализа ИИ, объяснение.
  import type { Json } from "../lib/api";
  import { pctOf } from "../lib/events";
  import { ago } from "../lib/format";

  let { a, err = "", mode, onMode }: { a: Json; err?: string; mode: string; onMode: (m: string) => void } = $props();

  const CONF: Record<string, string> = {
    высокая: "уверенность высокая",
    средняя: "уверенность средняя",
    низкая: "уверенность низкая",
    "только цена": "только цена рынка",
  };
  const pp = (d: number | null | undefined) =>
    d === null || d === undefined ? "" : (d > 0 ? "+" : d < 0 ? "−" : "") + (Math.round(Math.abs(d) * 1000) / 10).toString().replace(".", ",") + " п.п.";
  const rows = $derived(((a?.outcomes || []) as Json[]).slice().sort((x, y) => (y.final ?? 0) - (x.final ?? 0)));
  const errs = $derived(Object.entries(a?.estimators || {}).filter(([, v]) => (v as Json).error) as [string, Json][]);
  const job = $derived(a?.ai?.job);
  const JOB: Record<string, string> = {
    running: "Claude оценивает исходы вслепую (без цен) — это займёт до минуты…",
    wait_dossier: "ИИ начнёт, когда соберётся досье",
    limit: "",
    error: "ИИ не ответил",
  };
</script>

<div class="col" style="gap:12px" data-model-panel>
  <div class="row" style="justify-content:space-between;gap:10px;flex-wrap:wrap">
    <div class="seg" role="group" aria-label="Режим оценки">
      <button aria-pressed={mode === "ai"} data-mode="ai" onclick={() => onMode("ai")} disabled={a && !a.ai?.available} title={a && !a.ai?.available ? "Нужен ANTHROPIC_API_KEY в .env" : ""}>С ИИ</button>
      <button aria-pressed={mode === "manual"} data-mode="manual" onclick={() => onMode("manual")}>Без ИИ</button>
    </div>
    {#if a?.ts}<span class="sub">посчитано {ago(Math.max(0, Math.round(Date.now() / 1000 - a.ts)))}{a.ai?.limit && mode === "ai" ? ` · анализов с ИИ сегодня ${a.ai.used_today} из ${a.ai.limit}` : ""}</span>{/if}
  </div>
  {#if err && !a}
    <div class="sub" role="alert">{err}</div>
  {:else if !a}
    <div class="skeleton" style="height:160px"></div>
  {:else}
    {#if a.ai?.note}<div class="sub">{a.ai.note}</div>{/if}
    {#if a.dossier_job === "running"}<div class="sub">Собираем досье события — оценка обновится сама…</div>{/if}
    {#if mode === "ai" && job && job.state !== "done"}
      <div class="sub" class:skeleton={job.state === "running"}>{job.state === "limit" ? job.error : JOB[job.state] || ""}{job.state === "error" && job.error ? `: ${job.error}` : ""}</div>
    {/if}
    <ul class="col" style="gap:4px;margin:0;padding-left:18px">
      {#each a.explanation as t, i (i)}<li class="sub" style="color:var(--ink-2)">{t}</li>{/each}
    </ul>
    {#if mode === "ai" && a.ai?.key_factors?.length}
      <div class="col" style="gap:4px">
        <div class="label">Главное по мнению ИИ</div>
        {#each a.ai.key_factors as f, i (i)}<div class="sub">• {f}</div>{/each}
        {#if a.ai.what_would_change?.length}
          <div class="label" style="margin-top:6px">Что изменит оценку</div>
          {#each a.ai.what_would_change as f, i (i)}<div class="sub">• {f}</div>{/each}
        {/if}
      </div>
    {/if}

    <div class="ledger">
      {#each rows as r (r.key)}
        <details class="md-row" data-model-outcome={r.key}>
          <summary class="md-sum">
            <b class="grow">{r.title}</b>
            <span class="sub tnum">рынок {pctOf(r.market)}</span>
            <span class="tnum md-final">модель <b>{pctOf(r.final)}</b></span>
            <span class="tnum md-diff {r.diff > 0.005 ? 'up' : r.diff < -0.005 ? 'down' : ''}">{pp(r.diff)}</span>
          </summary>
          <div class="md-ladder">
            <div class="md-step"><span>Цена Polymarket сейчас</span><b class="tnum">{pctOf(r.market)}</b></div>
            {#if r.anchor !== r.market && r.anchor !== null}<div class="md-step"><span>Цена с поправкой по истории</span><b class="tnum">{pctOf(r.anchor)}</b></div>{/if}
            {#each r.components.filter((c: Json) => c.role !== "anchor") as c (c.name)}
              <div class="md-step">
                <span>{c.title}<span class="sub" style="display:block">{c.note}{c.w === 0 ? " · пока не учитывается (вес 0)" : ` · вес ${String(c.w).replace(".", ",")}`}</span></span>
                <b class="tnum">{pctOf(c.p)}</b>
              </div>
            {:else}
              <div class="sub">Независимых оценок нет — итог равен цене рынка.</div>
            {/each}
            {#if r.model !== null}<div class="md-step"><span>Сводная оценка (среднее с весами)</span><b class="tnum">{pctOf(r.model)}</b></div>{/if}
            <div class="md-step md-total">
              <span>Итог: цена + {Math.round((r.lam || 0) * 100)}% разницы<span class="sub" style="display:block">80% интервал {pctOf(r.lo)}–{pctOf(r.hi)} · {CONF[r.confidence] || r.confidence}</span></span>
              <b class="tnum">{pctOf(r.final)}</b>
            </div>
          </div>
        </details>
      {/each}
    </div>
    {#if errs.length}
      <div class="sub" role="status">Ошибки моделей: {errs.map(([, v]) => `${v.title} — ${v.error}`).join("; ")}</div>
    {/if}
    <div class="sub">
      Модель осторожна: цена Polymarket обычно точна (проверено на 2432 закрытых рынках), поэтому итог отходит от неё не больше чем на {Math.round((a.lambda0 || 0.3) * 100)}% разницы с независимыми оценками. Пунктир на графике — итог модели во времени.
    </div>
  {/if}
</div>

<style>
  .md-row{border-top:1px solid var(--line)}
  .md-row:first-child{border-top:0}
  .md-sum{display:flex;gap:12px;align-items:center;padding:12px 16px;cursor:pointer;list-style:none;flex-wrap:wrap}
  .md-sum::-webkit-details-marker{display:none}
  .md-final b{font:500 20px/1 var(--serif);color:var(--pearl)}
  .md-diff{min-width:76px;text-align:right;font-size:13.5px}
  .md-ladder{display:flex;flex-direction:column;gap:2px;padding:0 16px 14px 28px}
  .md-step{display:flex;justify-content:space-between;gap:16px;padding:6px 0;border-bottom:1px dashed var(--line);font-size:14px}
  .md-step b{font-weight:500;white-space:nowrap}
  .md-total{border-bottom:0;color:var(--brass-2)}
  .md-total b{font:500 20px/1 var(--serif)}
  @media (max-width:600px){
    .md-sum{gap:6px 12px}
    .md-sum > b{flex-basis:100%}
    .md-ladder{padding-left:16px}
  }
</style>
