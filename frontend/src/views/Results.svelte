<script lang="ts">
  // Итоги за период: прибыль, баланс, ставки, точность, откуда прибыль; отчёт CSV
  import Plot from "../components/Plot.svelte";
  import { android } from "../lib/android";
  import { api, type Json } from "../lib/api";
  import { money, plural, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const period = $derived(params[0] || "month");
  const d = loader(() => "/results?period=" + period);
  const NAMES: Record<string, string> = { week: "Неделя", month: "Месяц", all: "Всё время" };
  const PERIOD: Record<string, string> = { week: "За неделю", month: "За месяц", all: "За всё время" };

  async function downloadCsv() {
    try {
      const j = await api("/journal?limit=10000");
      const cols = ["signal_id", "ts", "whale", "question", "kind", "status", "decision", "size_usd", "q_whale", "q_llm", "q_final", "edge_net", "reason", "params_version", "user_action"];
      const q = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
      const csv = [cols.join(","), ...j.items.map((r: Json) => cols.map((c) => q(c === "ts" ? new Date(r.ts * 1000).toISOString() : r[c])).join(","))].join("\n");
      const name = `whalefollow-report-${new Date().toISOString().slice(0, 10)}.csv`;
      // Android-приложение: WebView не скачивает blob-ссылки — сохраняем через мост приложения в «Загрузки»
      const bridge = android();
      if (bridge) {
        toast(bridge.saveFile(name, "text/csv", "\ufeff" + csv));
        return;
      }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast((e as Error).message);
    }
  }
  const accChip = (l: string) => (l === "В норме" ? "g" : l === "Хуже рынка" ? "r" : "n");
</script>

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  {@const r = d.data}
  <div class="top">
    <h1>Итоги</h1>
    <div class="row">
      <div class="seg" role="group" aria-label="Период">
        {#each ["week", "month", "all"] as p (p)}<button aria-pressed={p === period} onclick={() => (location.hash = "#/results/" + p)}>{NAMES[p]}</button>{/each}
      </div>
      <button class="btn" data-act="csv" onclick={downloadCsv}>Скачать отчёт</button>
    </div>
  </div>
  <div class="card">
    <div class="row" style="gap:40px;align-items:flex-end">
      <div>
        <div class="sub">{PERIOD[period]}</div>
        <div class="big {tone(r.pnl_usd)}" style="font-size:32px">
          {money(r.pnl_usd, { sign: true })} <span style="font-size:16px;font-weight:600">({signedPct(r.pnl_pct)})</span>
        </div>
      </div>
      <div><div class="sub">Баланс сейчас</div><div class="big" style="font-size:24px">{money(r.balance_usd)}</div></div>
      {#if r.mode === "paper"}<a class="link" style="margin-left:auto" href="#/trial">Как идёт пробный режим →</a>{/if}
    </div>
    {#if r.series.length > 1}
      {#key r}
        <Plot xs={r.series.map((p: Json) => p.ts)} lines={[{ label: "Баланс", values: r.series.map((p: Json) => p.balance_usd), color: "#C8A464", fill: true }]}
          height={170} fmtY={(v) => money(v)} label="Баланс за период" />
      {/key}
    {:else}
      <div class="sub" style="margin-top:10px">Недостаточно данных для графика</div>
    {/if}
  </div>
  <div class="grid3">
    <div class="card">
      <div class="sub">Ставок сделано</div><div class="big">{r.bets.made}</div>
      <div style="font-size:14px;margin-top:4px">{r.bets.won} выиграли · {r.bets.lost} проиграли · {r.bets.waiting} ждут исхода</div>
    </div>
    <div class="card">
      <div class="sub">Сигналов пропущено</div><div class="big">{r.skipped_signals}</div>
      <div style="font-size:14px;margin-top:4px">Это нормально: система берёт только выгодные ставки</div>
    </div>
    <div class="card">
      <div class="sub">Точность прогнозов</div>
      <div style="margin-top:6px"><span class="chip {accChip(r.accuracy.label)}">{r.accuracy.label}</span></div>
      <div style="font-size:14px;margin-top:8px">{r.accuracy.text}</div>
    </div>
  </div>
  <div class="card col" style="gap:10px">
    <h2 style="margin:0">Откуда прибыль</h2>
    {#each r.by_whale as w, i (i)}
      <div class="top">
        <span><b>Кит {w.whale}</b> · {w.bets} {plural(w.bets, "ставка", "ставки", "ставок")}</span>
        <b class={tone(w.pnl_usd)}>{money(w.pnl_usd, { sign: true })}</b>
      </div>
    {:else}
      <div class="sub">Ставок за китами за период нет</div>
    {/each}
  </div>
  {#if r.events?.length}
    <div class="card col" style="gap:10px" data-results-events>
      <h2 style="margin:0">Ставки по событиям</h2>
      {#each r.events as g (g.kind)}
        <div class="top">
          <span><b>{g.title}</b> · {g.bets} {plural(g.bets, "ставка", "ставки", "ставок")} на {money(g.stake_usd)} · выиграно {g.won}, проиграно {g.lost}, ждут {g.waiting}</span>
          <b class={tone(g.pnl_usd)}>{money(g.pnl_usd, { sign: true })}</b>
        </div>
      {/each}
      <div class="sub">Учитываются отдельно от ставок за китами; итог периода выше — общий по банку. <a class="link" href="#/events-accuracy">Точность модели событий →</a></div>
    </div>
  {/if}
{/if}
