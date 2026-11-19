<script lang="ts">
  import MarketBox from "../components/MarketBox.svelte";
  import PriceChart from "../components/PriceChart.svelte";
  import Seal from "../components/Seal.svelte";
  import WhaleState from "../components/WhaleState.svelte";
  import { api, type Json } from "../lib/api";
  import type { Mark } from "../lib/chart";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { cents, days, fmtNum, money, safeUrl, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { app, softRefresh, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const token = $derived(params[0]);
  const d = loader(() => "/bets/" + encodeURIComponent(token));
  let feeRate = $state(0); // ставка комиссии рынка — приходит с панелью рынка
  const nowSec = Date.now() / 1000;

  const b = $derived(d.data);
  const marks = $derived.by(() => {
    const out: Mark[] = [];
    if (!b) return out;
    if (b.marks.whale.ts && b.marks.whale.price !== null) out.push({ ts: b.marks.whale.ts, price: b.marks.whale.price, color: "#8B7CFF", label: "кит купил" });
    if (b.marks.ours.ts && b.marks.ours.price !== null) out.push({ ts: b.marks.ours.ts, price: b.marks.ours.price, color: "#5EE1FF", label: "мы купили" });
    return out;
  });

  // сценарии по открытой ставке: сбудется / нет / продать сейчас (с комиссией Polymarket)
  const scen = $derived.by(() => {
    if (!b || !b.is_open) return null;
    const q = app.quotes[b.token_id];
    const bid = q && q.bid != null ? q.bid : b.now_cents !== null ? b.now_cents / 100 : null;
    const sh = b.shares_exact || b.shares || 0,
      cost = b.cost_exact || b.stake_usd || 0,
      paid = b.fees_usd || 0;
    const sellFee = bid !== null ? feeRate * sh * bid * (1 - bid) : 0;
    return { sh, cost, paid, bid, sellFee, sellNow: bid !== null ? sh * bid - sellFee - cost - paid : null };
  });

  async function sell() {
    const ok = await confirmDialog({
      title: "Продать ставку сейчас?",
      ok: "Продать",
      danger: true,
      text: "Ставка продаётся целиком по лучшим ценам покупателей в стакане, с комиссией Polymarket. Отменить продажу нельзя.",
    });
    if (!ok) return;
    try {
      const r = await api(`/bets/${encodeURIComponent(token)}/sell`, { method: "POST" });
      toast(r.ok ? `Продано за ${money(r.usd)}` : r.error);
      softRefresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if b}
  <a class="back" href="#/positions/{b.is_open ? 'open' : 'closed'}">← Мои ставки</a>
  <div class="page-head">
    <div class="who" style="align-items:flex-start">
      <Seal code={b.whale} lg />
      <div>
        <h1 style="font-size:36px">{b.question}</h1>
        <div class="row" style="margin-top:12px;gap:8px">
          <span class="chip b">«{b.side}»</span><span class="chip n">{b.category}</span>
          {#if b.origin === "event"}
            <a class="chip i" href="#/events/{encodeURIComponent(b.event_id)}">своя ставка · к событию</a>
          {:else}
            <button class="chip n" data-player={b.entity_id} onclick={() => (app.panel = { kind: "player", id: b.entity_id })}>кит {b.whale}</button>
          {/if}
          {#if b.is_open}{#if b.origin !== "event"}<WhaleState st={b.whale_state} />{/if}{:else}<span class="chip {b.pnl_usd > 0 ? 'g' : 'r'}">{b.result || "закрыта"}</span>{/if}
        </div>
      </div>
    </div>
    <div class="row">
      {#if b.url}<a class="btn" href={safeUrl(b.url)} target="_blank" rel="noopener">Открыть на Polymarket</a>{/if}
      {#if b.is_open}<button class="btn danger" data-act="sell-bet" onclick={sell}>Продать сейчас</button>{/if}
    </div>
  </div>
  <section class="statement" aria-label="Итог ставки">
    <div>
      <div class="label">{b.is_open ? "Сейчас прибыль" : "Итог ставки"}</div>
      <div class="figure {tone(b.pnl_usd)}">{money(b.pnl_usd, { sign: true })}</div>
      <div class="sub" style="margin-top:10px">{signedPct(b.change_pct)} от вложенного, комиссия {money(b.fees_usd, { cents: true })} учтена</div>
    </div>
    <div class="facts">
      <div class="fact"><div class="label">Вложено</div><div class="v">{money(b.stake_usd)}</div></div>
      <div class="fact"><div class="label">{b.is_open ? "Стоит сейчас" : "Получено"}</div><div class="v">{money(b.value_usd)}</div></div>
      <div class="fact">
        <div class="label">{b.is_open ? "Если сбудется" : "Держали"}</div>
        <div class="v {b.is_open ? 'up' : ''}">{b.is_open ? money(b.if_win_usd, { sign: true }) : String(b.held_days ?? "—").replace(".", ",") + " дн"}</div>
      </div>
    </div>
  </section>
  <div class="grid3">
    <div class="card"><div class="label">Кит купил по</div><div class="big">{cents(b.whale_cents)}</div></div>
    <div class="card">
      <div class="label">Мы купили по</div>
      <div class="big">{cents(b.entry_cents)}</div>
      <div class="sub">{b.whale_cents && b.entry_cents ? (b.entry_cents > b.whale_cents ? `на ${cents(b.entry_cents - b.whale_cents)} дороже кита` : "не дороже кита") : ""}</div>
    </div>
    <div class="card">
      <div class="label">{b.is_open ? "Цена сейчас" : "Акций"}</div>
      <div class="big">{b.is_open ? cents(b.now_cents) : String(b.shares).replace(".", ",")}</div>
      <div class="sub">{b.is_open ? `${String(b.shares).replace(".", ",")} акций, закроется ${b.closes_in_days === 0 ? "сегодня" : "через " + days(b.closes_in_days)}` : ""}</div>
    </div>
  </div>
  {#if scen}
    <div class="scen">
      <div class="sc g"><div class="label">Если сбудется</div><b>{money(scen.sh - scen.cost - scen.paid, { sign: true })}</b><div class="sub">получите {money(scen.sh)} за {fmtNum(Math.round(scen.sh))} акц.</div></div>
      <div class="sc r"><div class="label">Если не сбудется</div><b>{money(-scen.cost - scen.paid, { sign: true })}</b><div class="sub">ставка сгорит целиком</div></div>
      <div class="sc">
        <div class="label">Если продать сейчас</div>
        <b>{scen.sellNow === null ? "—" : money(scen.sellNow, { sign: true })}</b>
        <div class="sub">{scen.bid === null ? "нет цены покупателя" : `по ${cents(scen.bid * 100)}, комиссия ≈ ${money(scen.sellFee, { cents: true })}`}</div>
      </div>
    </div>
  {/if}
  {#if (b.chart || []).length > 1}
    <div class="card">
      <div class="top"><h2>Цена «{b.side}»</h2><span class="sub">в центах за акцию; 100¢ — если событие произойдёт · колёсиком — масштаб</span></div>
      <PriceChart points={b.chart} {marks} height={240} label="Цена ставки" />
    </div>
  {/if}
  <h2>Рынок</h2>
  <MarketBox token={b.token_id} noChart={(b.chart || []).length > 1} onloaded={(m: Json) => (feeRate = (m && m.fee_rate) || 0)} />
  <div class="grid2">
    <div class="card">
      <h2 style="margin-bottom:16px">Как это было</h2>
      <ol class="timeline">
        {#each b.events as e, i (i)}
          <li class:whale={e.kind === "whale"} class:future={e.ts > nowSec}>
            <div>{e.text}</div>
            <div class="when">{new Date(e.ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}</div>
          </li>
        {/each}
      </ol>
    </div>
    <div class="card col" style="gap:12px">
      <h2>Почему купили</h2>
      <div class="check-row">
        <span class="chip b">{b.model.q_pct ?? "—"}%</span>
        <div><b>Шанс по расчёту системы</b><div class="sub">История кита в этой теме{b.model.ai_pct ? ` и оценка ИИ ${b.model.ai_pct}%` : ""}</div></div>
      </div>
      <div class="check-row">
        <span class="chip {b.model.edge_pct > 0 ? 'g' : 'n'}">{b.model.edge_pct === null ? "—" : String(b.model.edge_pct).replace(".", ",") + "%"}</span>
        <div><b>Запас выгоды при покупке</b><div class="sub">Шанс минус цена, комиссия и запас на неточность</div></div>
      </div>
      {#if b.signal_id}<a class="link" href="#/signal/{encodeURIComponent(b.signal_id)}">Открыть исходный сигнал с новостями</a>{/if}
    </div>
  </div>
{/if}
