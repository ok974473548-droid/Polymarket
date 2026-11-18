<script lang="ts">
  import Banners from "../components/Banners.svelte";
  import Flash from "../components/Flash.svelte";
  import Seal from "../components/Seal.svelte";
  import WhaleState from "../components/WhaleState.svelte";
  import { api, type Json } from "../lib/api";
  import { cents, days, money, plural, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { app, softRefresh, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const tab = $derived(params[0] || "open");
  const open = $derived(tab === "open");
  const p = loader(() => "/positions?status=" + tab);
  const sm = $derived(p.data?.summary || {});

  // живая цена покупателя (bid) пересчитывает стоимость и прибыль открытой ставки
  function live(r: Json) {
    const q = app.quotes[r.token_id];
    if (!open || !q || q.bid === null || q.bid === undefined) return null;
    const value = r.shares_exact * q.bid;
    return { bid: q.bid, value, pnl: value - r.cost_exact - (r.fees_usd || 0) };
  }

  async function exit(id: string, action: string) {
    try {
      const r = await api(`/exits/${encodeURIComponent(id)}`, { method: "POST", body: { action } });
      toast(r.status === "sold" ? `Продано за ${money(r.usd)}` : r.status === "held" ? "Держим до закрытия рынка" : r.error || "Готово");
      softRefresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if p.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{p.error}</div></div>
{:else if p.data}
  {@const d = p.data}
  <Banners />
  <div class="page-head">
    <div>
      <h1>Мои ставки</h1>
      <div class="lede">Повторённые ставки китов. Нажмите на ставку, чтобы увидеть цену входа, график и что делает кит.</div>
    </div>
    <div class="seg" role="group" aria-label="Какие ставки показать">
      <button aria-pressed={open} onclick={() => (location.hash = "#/positions/open")}>Открытые {d.counts.open}</button>
      <button aria-pressed={!open} onclick={() => (location.hash = "#/positions/closed")}>Закрытые {d.counts.closed}</button>
    </div>
  </div>
  <section class="statement" aria-label="Сводка по ставкам">
    <div>
      <div class="label">{open ? "Стоят сейчас" : "Итог закрытых ставок"}</div>
      <div class="figure {open ? '' : tone(sm.realized_pnl_usd)}">{open ? money(sm.open_value_usd) : money(sm.realized_pnl_usd, { sign: true })}</div>
      <div class="sub" style="margin-top:10px">
        {open ? `Вложено ${money(d.in_play_usd)} в ${d.items.length} ${plural(d.items.length, "ставку", "ставки", "ставок")}` : "Сумма прибыли и убытков с учётом комиссий"}
      </div>
    </div>
    <div class="facts">
      <div class="fact">
        <div class="label">{open ? "Изменение" : "Выигрышей"}</div>
        <div class="v {open ? tone(sm.open_pnl_usd) : ''}">{open ? money(sm.open_pnl_usd, { sign: true }) : sm.win_pct === null ? "—" : String(sm.win_pct).replace(".", ",") + "%"}</div>
      </div>
      <div class="fact">
        <div class="label">{open ? "Закрытые, итог" : "Закрытых ставок"}</div>
        <div class="v {open ? tone(sm.realized_pnl_usd) : ''}">{open ? money(sm.realized_pnl_usd, { sign: true }) : d.counts.closed}</div>
      </div>
      <div class="fact"><div class="label">Комиссии Polymarket</div><div class="v">{money(sm.fees_usd, { cents: true })}</div></div>
    </div>
  </section>
  {#each d.need_decision as x (x.signal_id)}
    <div class="banner warn" style="flex-direction:column;align-items:stretch;gap:12px">
      <div class="row">
        <Seal code={x.whale} />
        <div class="grow">
          <b style="font-size:16px">Кит {x.whale} продал свою позицию — нужно решение</b>
          <div>«{x.question}». Мы повторили эту ставку на {money(x.stake_usd)}. {x.text}</div>
        </div>
      </div>
      <div class="row">
        <button class="btn {x.recommendation === 'hold' ? 'p' : ''}" onclick={() => exit(x.signal_id, "hold")}>Держать до конца</button>
        <button class="btn {x.recommendation === 'sell' ? 'p' : ''}" onclick={() => exit(x.signal_id, "sell")}>Продать сейчас за {money(x.sell_now_usd)}</button>
        <span class="sub">Продажа зафиксирует {money(x.sell_pnl_usd, { sign: true })}</span>
      </div>
    </div>
  {/each}
  <div class="ledger">
    <div class="ledger-head bets-grid">
      <span>Ставка</span><span class="n">Вход</span><span class="n hide-md">{open ? "Цена" : "Итог"}</span><span class="n">Сумма</span><span class="n">Прибыль</span><span class="n hide-md">{open ? "Рынок" : "Результат"}</span>
    </div>
    {#each d.items as r (r.token_id)}
      {@const L = live(r)}
      <a class="ledger-row bets-grid" href="#/bet/{encodeURIComponent(r.token_id)}">
        <div class="who">
          <Seal code={r.whale} />
          <div class="grow">
            <div class="t">{r.question}</div>
            <div class="m">
              <span>«{r.side}»</span>
              {#if r.origin === "event_arb"}<span class="chip i sm">без риска</span>{:else if r.origin === "event_model"}<span class="chip i sm">по совету модели</span>{:else if r.origin === "event"}<span class="chip i sm">своя ставка по событию</span>{:else}<span>кит {r.whale}</span>{/if}
              <span>{r.category}</span>{#if open && r.origin === "whale"}<WhaleState st={r.whale_state} />{/if}
            </div>
          </div>
        </div>
        <div class="n"><div class="tnum">{cents(r.entry_cents)}</div><div class="sub">кит {cents(r.whale_cents)}</div></div>
        <div class="n hide-md">
          <div class="tnum">{#if open}<Flash text={cents(L ? L.bid * 100 : r.now_cents)} />{:else}{r.result || ""}{/if}</div>
          <div class="sub">{open ? "сейчас" : `держали ${String(r.held_days ?? "—").replace(".", ",")} дн`}</div>
        </div>
        <div class="n"><div class="tnum">{money(r.stake_usd)}</div><div class="sub">{open ? "стоит " + money(L ? L.value : r.value_usd) : "получено " + money(r.value_usd)}</div></div>
        <div class="n">
          <b class="tnum {tone(L ? L.pnl : r.pnl_usd)}"><Flash text={money(L ? L.pnl : r.pnl_usd, { sign: true })} /></b>
          <div class="sub">{signedPct(r.change_pct)}</div>
        </div>
        <div class="n hide-md">
          <div>{open ? (r.closes_in_days === 0 ? "сегодня" : "через " + days(r.closes_in_days)) : r.result || "закрыта"}</div>
          <div class="sub">{open ? "закроется" : ""}</div>
        </div>
      </a>
    {:else}
      <div class="ledger-empty">{open ? "Открытых ставок нет. Новые появятся, когда вы подтвердите сигнал." : "Закрытых ставок пока нет."}</div>
    {/each}
  </div>
  <p class="sub" style="margin:0">
    Стоимость открытой ставки меняется вместе с ценой на рынке{app.live ? " — здесь она обновляется сама" : ""}. Окончательный итог известен, когда рынок закроется.
  </p>
{/if}
