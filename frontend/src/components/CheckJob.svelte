<script lang="ts">
  // Проверка игрока в фоне (задача worker): ход проверки, затем итог и «что было бы»
  import { onDestroy } from "svelte";
  import { api, type Json } from "../lib/api";
  import { fmtNum, plural } from "../lib/format";
  import { toast } from "../lib/state.svelte";
  import SimCard from "./SimCard.svelte";

  let { job }: { job: string } = $props();
  let r = $state<Json>(null);
  let err = $state("");
  let alive = true;
  onDestroy(() => (alive = false)); // ушли со страницы — проверка идёт в фоне, придёт уведомление

  $effect(() => {
    const id = job;
    r = null;
    err = "";
    void (async () => {
      try {
        let x = await api("/whales/check/" + encodeURIComponent(id));
        while (alive && id === job && x.ok && x.state === "running") {
          r = x;
          await new Promise((ok) => setTimeout(ok, 900));
          x = await api("/whales/check/" + encodeURIComponent(id));
        }
        if (alive && id === job) {
          if (x.ok) r = x;
          else err = x.error || "Проверка не удалась";
        }
      } catch (e) {
        if (alive) err = (e as Error).message;
      }
    })();
  });

  const pct = $derived(r && r.state === "running" && r.total ? Math.round((r.done / r.total) * 100) : null);
  const count = $derived(
    !r ? "" : r.stage === "trades" ? (r.done ? `загружено ${fmtNum(r.done)} ставок` : "") : r.total ? `${fmtNum(r.done)} из ${fmtNum(r.total)}` : "",
  );
  const NAMES: Record<string, string> = { profile: "статистика", trades: "ставки", activity: "операции", markets: "рынки", prices: "цены", score: "расчёт" };
  function timingText(t: Json) {
    if (!t) return "";
    const parts = Object.keys(NAMES)
      .filter((k) => t[k] >= 1)
      .map((k) => `${NAMES[k]} ${Math.round(t[k])} с`);
    return parts.length ? ` (${parts.join(" · ")})` : "";
  }
  async function add(mode: string) {
    try {
      const x = await api("/whales", { method: "POST", body: { entity_id: r.entity_id, mode } });
      toast(x.message || (x.mode === "copy" ? "Кит добавлен, копирование включено" : "Кит добавлен в режиме «Только следить»"));
      location.hash = "#/whales";
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if err}
  <div class="card err" role="alert">{err}</div>
{:else if !r}
  <div class="card skeleton">Начинаем проверку…</div>
{:else if r.state === "running"}
  <div class="card col" style="gap:12px" aria-live="polite">
    <div class="top"><b>Проверяем игрока</b><span class="sub">{Math.round(r.elapsed_s || 0)} с</span></div>
    {#each r.stages as st, i (i)}
      <div class="row" style="gap:10px">
        <span class="chip {st.status === 'done' ? 'g' : 'n'}" style="min-width:64px;text-align:center">{st.status === "done" ? "готово" : st.status === "active" ? "идёт" : "ждёт"}</span>
        <span style={st.status === "wait" ? "color:var(--muted)" : ""}>{st.text}{#if st.status === "active" && count} · <span class="sub">{count}</span>{/if}</span>
      </div>
    {/each}
    {#if pct !== null}<div class="bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><span style="width:{pct}%"></span></div>{/if}
    <div class="sub">Можно уйти с этой страницы — проверка продолжится, а когда закончится, придёт уведомление.</div>
  </div>
{:else}
  {@const st = r.stats || {}}
  <h2>Результат проверки</h2>
  <div class="card col" style="border-color:{r.fits ? 'var(--accent)' : 'var(--line)'};gap:12px">
    <div class="row">
      <b style="font-size:20px">Игрок {r.name}</b><span class="chip {r.fits ? 'g' : 'n'}">{r.fits ? "Подходит" : "Не подходит"}</span>
      {#if r.risk}<span class="sub">Риск: {r.risk}{r.strong_topics && r.strong_topics.length ? " · сильные темы: " + r.strong_topics.join(", ") : ""}</span>{/if}
    </div>
    {#if st.trades}
      <div class="sub">
        Проверено {fmtNum(st.trades)} {plural(st.trades, "ставка", "ставки", "ставок")} в {fmtNum(st.markets)} {plural(st.markets, "рынке", "рынках", "рынках")}{st.seconds != null
          ? ` за ${Math.round(st.seconds)} с`
          : ""}{timingText(r.timings)}
      </div>
    {/if}
    {#each r.checks as c, i (i)}
      <div class="check-row"><span class="chip {c.ok ? 'g' : 'r'}">{c.ok ? "Да" : "Нет"}</span><div><b>{c.title}</b><div class="sub">{c.text}</div></div></div>
    {/each}
    <div class="row">
      {#if r.fits}<button class="btn p" data-act="add" onclick={() => add("copy")}>Добавить и копировать</button>{/if}
      <button class="btn" data-act="add" onclick={() => add("watch")}>Только следить</button>
      <a class="link" href="#/whales">Отмена</a>
    </div>
    {#if r.hint}<div class="sub">{r.hint}</div>{/if}
  </div>
  {#if r.entity_id}<div style="margin-top:16px"><SimCard id={r.entity_id} /></div>{/if}
{/if}
