<script lang="ts">
  // «Если бы вы копировали этого кита»: повтор его ставок с задержкой и комиссиями
  import { onMount } from "svelte";
  import { api, type Json } from "../lib/api";
  import { cents, money, pctText, signedPct, tone } from "../lib/format";
  import { toast as _t } from "../lib/state.svelte";
  import Plot from "./Plot.svelte";

  let { id, withPeriods = false }: { id: string; withPeriods?: boolean } = $props();
  let days = $state(90);
  let range = $state<[number, number] | null>(null);
  let showRange = $state(false);
  let r = $state<Json>(null);
  let err = $state("");
  let loading = $state(true);
  const isoDay = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 10);
  const nowS = Math.floor(Date.now() / 1000);
  let start = $state(isoDay(nowS - 60 * 86400));
  let end = $state(isoDay(nowS));

  async function load() {
    loading = true;
    err = "";
    try {
      const q = range ? `start=${range[0]}&end=${range[1]}` : `days=${days}`;
      r = await api(`/whales/${encodeURIComponent(id)}/simulate?${q}`);
    } catch (e) {
      err = (e as Error).message;
    } finally {
      loading = false;
    }
  }
  onMount(load);

  function period(d: number) {
    days = d;
    range = null;
    void load();
  }
  function submitRange(e: SubmitEvent) {
    e.preventDefault();
    const st = Math.floor(new Date(start + "T00:00:00Z").getTime() / 1000),
      en = Math.floor(new Date(end + "T23:59:59Z").getTime() / 1000);
    if (!st || !en || en <= st) return _t("Проверьте даты: конец должен быть позже начала");
    range = [st, Math.min(en, Math.floor(Date.now() / 1000))];
    void load();
  }
  const SIM_LABEL: Record<string, string> = {
    price_moved: "цена успела уйти",
    topic_off: "тема выключена у вас",
    day_limit: "дневной лимит",
    no_upside: "цена почти 100¢",
  };
  const STATUS: Record<string, string> = { resolved: "рынок закрылся", sold: "кит вышел — продали следом", open: "ещё открыта" };
  const s = $derived((r && r.summary) || {});
  const verdict = $derived(
    ({ good: ["g", "Было бы в плюсе"], bad: ["r", "Было бы в минусе"], few: ["a", "Мало ставок"], no_data: ["n", "Нет ставок"] } as Record<string, string[]>)[s.verdict] || ["n", ""],
  );
  const skipped = $derived(Object.entries(s.skipped || {}).filter(([, v]) => v) as [string, number][]);
</script>

{#if loading && !r}
  <div class="card skeleton">Считаем, что было бы при копировании…</div>
{:else if err}
  <div class="card err">{err}</div>
{:else if !r || !r.ok}
  <div class="card"><h2>Если бы вы копировали этого кита</h2><div class="sub" style="margin-top:8px">{(r && r.error) || "Нет данных"}</div></div>
{:else}
  <section class="card col" style="gap:16px" aria-label="Если бы вы копировали этого кита">
    <div class="top">
      <div>
        <h2>Если бы вы копировали этого кита</h2>
        <div class="sub" style="margin-top:4px">
          {range ? `Ставки с ${new Date(range[0] * 1000).toLocaleDateString("ru-RU")} по ${new Date(range[1] * 1000).toLocaleDateString("ru-RU")}` : `Последние ${s.days} дней`}, задержка
          {s.delay_s} с, ставка {money(s.stake_usd)} по вашим лимитам, с комиссиями
        </div>
      </div>
      <div class="row">
        {#if withPeriods}
          <div class="seg" role="group" aria-label="Период">
            {#each [30, 90, 180] as d (d)}<button type="button" aria-pressed={d === days && !range} data-act="sim" onclick={() => period(d)}>{d} дн</button>{/each}
            <button type="button" aria-pressed={!!range} aria-expanded={showRange} onclick={() => (showRange = !showRange)}>Свои даты</button>
          </div>
        {/if}
        <span class="chip {verdict[0]}">{verdict[1]}</span>
      </div>
    </div>
    {#if withPeriods && (showRange || range)}
      <form class="sim-range row" onsubmit={submitRange}>
        <label>С <input class="inp" type="date" bind:value={start} max={isoDay(nowS)} /></label>
        <label>по <input class="inp" type="date" bind:value={end} max={isoDay(nowS)} /></label>
        <button class="btn p">Посчитать</button>
        <span class="sub">Ставки, которые кит открыл в эти даты, и как они закрылись на самом деле</span>
      </form>
    {/if}
    {#if s.n}
      <div class="row" style="gap:36px;align-items:flex-end">
        <div>
          <div class="label">Итог закрытых ставок</div>
          <div class="big {tone(s.closed_pnl_usd)}" style="font-size:40px">{money(s.closed_pnl_usd, { sign: true })}</div>
          <div class="sub">{s.roi_pct === null ? "" : signedPct(s.roi_pct) + " к вложенному"}</div>
        </div>
        <div class="fact"><div class="label">Повторили бы</div><div class="v">{s.n}<small>закрыто {s.closed}</small></div></div>
        <div class="fact"><div class="label">Выигрышей</div><div class="v">{pctText(s.win_pct)}</div></div>
        <div class="fact"><div class="label">Сам кит на тех же ставках</div><div class="v {tone(s.whale_roi_pct)}">{s.whale_roi_pct === null ? "—" : signedPct(s.whale_roi_pct)}</div></div>
        <div class="fact"><div class="label">Наибольшая просадка</div><div class="v">{money(-s.max_drawdown_usd)}</div></div>
        <div class="fact"><div class="label">Комиссии</div><div class="v">{money(s.fees_usd, { cents: true })}</div></div>
      </div>
      {#if (s.curve || []).length > 1}
        {#key r}
          <Plot height={160} xs={s.curve.map((c: Json) => c.ts)} lines={[{ label: "Прибыль", values: s.curve.map((c: Json) => c.pnl_usd), color: "#8B7CFF", fill: true }]}
            fmtY={(v) => money(v)} label="Прибыль копирования по неделям" />
        {/key}
      {/if}
    {/if}
    <div style="font-size:14px">{s.text}</div>
    {#if skipped.length}<div class="sub">Не повторили бы: {skipped.map(([k, v]) => `${SIM_LABEL[k] || k} — ${v}`).join("; ")}.</div>{/if}
    {#if s.n}
      <div class="sub">
        Цена входа оценена по почасовой истории цен: не дешевле цены кита плюс 1¢ на проскальзывание. Это оценка, а не точный расчёт стакана.{s.estimated_share
          ? ` Для ${Math.round(s.estimated_share * 100)}% ставок истории цен нет — взята цена кита плюс 1¢.`
          : ""}
      </div>
    {/if}
    {#if (r.bets || []).length}
      <details>
        <summary class="link" style="cursor:pointer;display:inline-block">Ставки в расчёте</summary>
        <div class="ledger" style="margin-top:10px">
          {#each r.bets.slice(0, 12) as b, i (i)}
            <div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 90px 90px 110px">
              <div><div class="t">{b.question}</div><div class="m"><span>«{b.side || ""}»</span><span>{STATUS[b.status]}</span></div></div>
              <div class="n"><div class="tnum">{cents(b.whale_cents)}</div><div class="sub">кит</div></div>
              <div class="n"><div class="tnum">{cents(b.entry_cents)}</div><div class="sub">мы</div></div>
              <div class="n"><b class="tnum {tone(b.pnl_usd)}">{money(b.pnl_usd, { sign: true })}</b></div>
            </div>
          {/each}
        </div>
      </details>
    {/if}
  </section>
{/if}
