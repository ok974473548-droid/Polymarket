<script lang="ts">
  // Совет модели «что купить, по какой цене и сколько» + «Следить». Сценарии: если сбудется / если нет / в среднем.
  import type { Json } from "../lib/api";
  import { centsOf, pctOf } from "../lib/events";
  import { money } from "../lib/format";

  let {
    a,
    single = false,
    onBuy,
    onWatch,
  }: { a: Json; single?: boolean; onBuy: (x: Json) => void; onWatch: (on: boolean) => void } = $props();

  const adv = $derived(a?.advice);
  const best = $derived(adv?.best as Json | null);
  const others = $derived(((adv?.buys || []) as Json[]).slice(1, 4));
  const close = $derived(adv?.closest as Json | null);
  const watching = $derived(!!a?.watch);
  const pp = (x: number) => (Math.round(x * 1000) / 10).toString().replace(".", ",") + " п.п.";
  const what = (x: Json) => (single ? `«${x.label}»` : `«${x.label}» в «${x.title}»`);
  const noInfo = $derived(((adv?.items || []) as Json[]).every((x) => x.verdict === "no_info"));
</script>

{#if adv}
  <section class="card col adv-card" class:has={!!best} aria-label="Совет модели" data-advice>
    <div class="row" style="justify-content:space-between;gap:10px;flex-wrap:wrap">
      <div class="label">Совет модели{a.mode === "ai" ? " (с ИИ)" : ""}</div>
      <button class="btn" aria-pressed={watching} data-act="watch" onclick={() => onWatch(!watching)} title="Проверять раз в 15 минут и прислать уведомление, когда появится выгода">
        {watching ? "★ Слежу" : "☆ Следить"}
      </button>
    </div>
    {#if adv.caps?.paused?.length}
      <div class="sub" role="status">Ставки на паузе: {adv.caps.paused.join("; ")}</div>
    {/if}
    {#if best}
      <h2 class="adv-what">
        Купить {what(best)} на {money(best.size_usd, { cents: !Number.isInteger(best.size_usd) })} <span class="sub">по цене до {centsOf(best.limit_price)}</span>
      </h2>
      <div class="sub">
        Оценка модели {pctOf(best.p)}, цена {centsOf(best.ask)} + комиссия → {centsOf(best.cost)} за акцию; выгода {pp(best.edge)} при пороге {pp(best.need)};
        {best.shares.toFixed(1).replace(".", ",")} акций, средняя цена {centsOf(best.avg_price)}{best.capped ? " · сумма урезана лимитами" : ""}{best.partial ? " · дешевле предельной цены в стакане больше нет" : ""}.
      </div>
      <div class="row adv-sc" style="gap:22px">
        <div class="fact"><div class="label">Если сбудется</div><div class="v up">{money(best.win_usd, { sign: true, cents: true })}</div></div>
        <div class="fact"><div class="label">Если нет</div><div class="v down">{money(best.lose_usd, { sign: true, cents: true })}</div></div>
        <div class="fact"><div class="label">В среднем по оценке</div><div class="v">{money(best.ev_usd, { sign: true, cents: true })}</div></div>
      </div>
      <div class="row" style="gap:10px">
        <button class="btn p" data-act="advice-buy" onclick={() => onBuy(best)} disabled={!!adv.caps?.paused?.length}>Поставить по совету</button>
        <span class="sub">Ставка виртуальная; сумма — ¼ Келли с комиссией в пределах лимитов «Событий».</span>
      </div>
      {#if others.length}
        <div class="sub">Ещё выгодно: {others.map((x) => `${what(x)} до ${centsOf(x.limit_price)} (выгода ${pp(x.edge)})`).join("; ")}.</div>
      {/if}
    {:else}
      <h2 class="adv-what">Сейчас выгодных ставок нет</h2>
      <div class="sub">
        {#if noInfo}
          По событию нет независимых оценок — модель согласна с ценой, обыграть её нечем.
        {:else if close}
          Ближе всего: {what(close)} — оценка {pctOf(close.p)}, цена с комиссией {centsOf(close.cost)}. Станет выгодно по цене до {close.limit_price !== null ? centsOf(close.limit_price) : "—"} (сейчас {centsOf(close.ask)}). {close.reason}.
        {:else}
          Цены близки к оценке модели или стакан пуст.
        {/if}
        {watching ? "Пришлём уведомление, когда появится выгода." : "Нажмите «Следить» — пришлём уведомление, когда появится выгода."}
      </div>
    {/if}
  </section>
{/if}

<style>
  .adv-card{gap:10px;border-color:var(--line)}
  .adv-card.has{border-color:rgba(200,164,100,.45);box-shadow:0 0 0 1px rgba(200,164,100,.15) inset}
  .adv-what{font-size:24px;margin:0}
  .adv-what .sub{font:400 15px/1.3 var(--sans, inherit)}
  @media (max-width:600px){ .adv-what{font-size:20px} .adv-sc{gap:14px!important} }
</style>
