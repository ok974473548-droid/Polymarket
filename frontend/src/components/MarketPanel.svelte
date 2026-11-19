<script lang="ts">
  // Панель рынка: сроки, объём, исходы, график цены с отметками, кит сейчас, держатели, правила
  import type { Json } from "../lib/api";
  import { cents, compactMoney, fmtNum, safeUrl, tone, untilText } from "../lib/format";
  import { app } from "../lib/state.svelte";
  import Avatar from "./Avatar.svelte";
  import Kpi from "./Kpi.svelte";
  import type { Mark } from "../lib/chart";
  import PriceChart from "./PriceChart.svelte";

  let { m, compact = false, noChart = false, ourMark = null }: { m: Json; compact?: boolean; noChart?: boolean; ourMark?: Mark | null } =
    $props();
  const marks = $derived.by(() => {
    const out: Mark[] = [];
    if (m?.whale_mark && m.whale_mark.ts && m.whale_mark.price !== null)
      out.push({ ts: m.whale_mark.ts, price: m.whale_mark.price, color: "#8B7CFF", label: "кит купил" });
    if (ourMark) out.push(ourMark);
    return out;
  });
</script>

{#if !m || m.ok === false}
  <div class="card sub">{(m && m.error) || "Нет данных о рынке"}</div>
{:else}
  <div class="mpanel col" style="gap:16px">
    {#if m.event_title && m.event_title !== m.question}<div class="sub">Событие: {m.event_title}</div>{/if}
    <div class="kpis {compact ? 'k2' : 'k4'}">
      <Kpi label="Закроется" value={m.end_ts ? new Date(m.end_ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) : "—"} sub={untilText(m.end_ts)} />
      <Kpi label="Объём торгов" value={compactMoney(m.volume_usd)} sub={m.volume_24h_usd ? `за сутки ${compactMoney(m.volume_24h_usd)}` : ""} />
      <Kpi label="Ликвидность" value={compactMoney(m.liquidity_usd)} sub={m.spread !== null && m.spread !== undefined ? `разница цен ${cents(m.spread * 100)}` : ""} />
      <Kpi label="Комиссия" value={m.fee_rate ? `${Math.round(m.fee_rate * 100)}%` : "нет"} sub="от p·(1−p) на акцию" />
    </div>
    {#if m.outcomes && m.outcomes.length}
      <div class="outs">
        {#each m.outcomes as o, i (i)}
          <div class="out" class:ours={o.ours}>
            <div class="top"><span>{o.label}{#if o.ours}{" "}<span class="chip b sm">наша сторона</span>{/if}</span><b class="tnum">{o.price === null ? "—" : cents(o.price * 100)}</b></div>
            <div class="obar"><span style="width:{Math.round((o.price || 0) * 100)}%"></span></div>
          </div>
        {/each}
      </div>
    {/if}
    {#if !compact && !noChart && (m.chart || []).length > 1}
      <div class="card">
        <div class="top"><h3>Цена «{m.side}» за месяц</h3><span class="sub">в центах · колёсиком — масштаб</span></div>
        <PriceChart points={m.chart} {marks} height={220} />
      </div>
    {/if}
    <div class={compact ? "col" : "grid2"} style="gap:16px">
      {#if m.whale}
        {@const ps = m.whale_positions || []}
        <div class="card col" style="gap:{ps.length ? 10 : 6}px">
          <h3>Кит {m.whale} сейчас</h3>
          {#if !ps.length}
            <div class="sub">Позиции в этом рынке у кита больше нет — он вышел или ещё не появился в данных Polymarket.</div>
          {:else}
            {#each ps as p, i (i)}
              <div class="kpis k3">
                <Kpi label="Держит" value="{fmtNum(Math.round(p.shares))} акц." sub="«{p.outcome}»" />
                <Kpi label="Средний вход" value={cents(p.avg_cents)} sub="сейчас {cents(p.now_cents)}" />
                <Kpi label="Его прибыль" value={compactMoney(p.pnl_usd, { sign: true })} sub="стоит {compactMoney(p.value_usd)}" cls={tone(p.pnl_usd)} />
              </div>
            {/each}
            <div class="sub">Готовые данные Polymarket по позиции кита: средняя цена и прибыль уже посчитаны.</div>
          {/if}
        </div>
      {/if}
      <div class="card col" style="gap:10px">
        <div class="top"><h3>Кто ещё держит</h3><span class="sub">крупнейшие позиции</span></div>
        {#if !m.holders || !m.holders.length}
          <div class="sub">Данных о держателях нет</div>
        {:else}
          <div class="hlist">
            {#each m.holders.slice(0, 10) as h, i (i)}
              <button class="hrow" data-player={h.wallet} onclick={() => (app.panel = { kind: "player", id: h.wallet })}>
                <Avatar img={h.image} code={h.short} sm />
                <div class="grow">
                  <div class="t">{h.name || h.short}{#if h.in_list}{" "}<span class="chip g sm">в списке</span>{/if}</div>
                  <div class="m"><span class="chip {h.same_side ? 'b' : 'n'} sm">«{h.outcome || "?"}»</span>{#if h.avg_cents !== null}<span>вход {cents(h.avg_cents)}</span>{/if}</div>
                </div>
                <div class="n">
                  <div class="tnum">{h.value_usd !== null ? compactMoney(h.value_usd) : fmtNum(Math.round(h.shares)) + " акц."}</div>
                  {#if h.pnl_usd !== null}<div class="sub {tone(h.pnl_usd)}">{compactMoney(h.pnl_usd, { sign: true })}</div>{/if}
                </div>
              </button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
    {#if m.rules}
      <details class="more card"><summary class="link">Правила рынка — как он разрешится</summary><div class="rules" style="white-space:pre-line">{m.rules}</div></details>
    {/if}
    <div class="row">{#if m.url}<a class="btn" href={safeUrl(m.url)} target="_blank" rel="noopener">Открыть на Polymarket</a>{/if}</div>
  </div>
{/if}
