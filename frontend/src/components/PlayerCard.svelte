<script lang="ts">
  // Карточка игрока по готовым данным Polymarket (полная или в боковой панели)
  import type { Json } from "../lib/api";
  import { compactMoney, fmtNum, monthsSince, playerName, plural, safeUrl, shortAddr, tone } from "../lib/format";
  import { addPlayers } from "../lib/players.svelte";
  import { app, toast } from "../lib/state.svelte";
  import Avatar from "./Avatar.svelte";
  import Form from "./Form.svelte";
  import Kpi from "./Kpi.svelte";
  import PnlChart from "./PnlChart.svelte";
  import PositionsList from "./PositionsList.svelte";
  import Seal from "./Seal.svelte";

  let { c, inDrawer = false, onadded }: { c: Json; inDrawer?: boolean; onadded?: (r: Json) => void } = $props();
  let busy = $state(false);

  async function add(mode: string) {
    busy = true;
    try {
      const r = await addPlayers([c.wallet], mode);
      onadded?.(r);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      busy = false;
    }
  }
  const profile = $derived(safeUrl(c?.profile_url || "https://polymarket.com/profile/" + c?.wallet));
</script>

{#snippet chips()}
  {#if c.in_list}<span class="chip {c.mode === 'copy' ? 'g' : 'i'}">{c.mode === "copy" ? "копируем" : "в списке · следим"}</span>{/if}
  {#if c.checking}<span class="chip a">идёт проверка</span>{:else if c.checked}<span class="chip n">{c.checked.toLowerCase()}</span>{/if}
  {#if c.copy_after_check}<span class="chip a">копирование — после проверки</span>{/if}
{/snippet}

{#snippet actions()}
  {#if c.in_list}
    <div class="row">
      {#if inDrawer}<a class="btn p" href="#/whales/{encodeURIComponent(c.entity_id || c.wallet)}" onclick={() => (app.panel = null)}>Открыть профиль</a>{/if}
      <a class="btn" href={profile} target="_blank" rel="noopener">На Polymarket</a>
    </div>
  {:else}
    <div class="row">
      <button class="btn p" data-act="padd" disabled={busy} onclick={() => add("copy")} title="Добавим сразу; копирование включится, если игрок пройдёт проверку">Добавить и копировать</button>
      <button class="btn" disabled={busy} onclick={() => add("watch")}>Только следить</button>
      <a class="btn" href={profile} target="_blank" rel="noopener">На Polymarket</a>
    </div>
  {/if}
{/snippet}

{#if !c || c.ok === false}
  <div class="card col" style="gap:10px">
    <div class="row"><Seal code={c && c.short} /><b>{c && c.wallet ? shortAddr(c.wallet) : "Игрок"}</b>{#if c}{@render chips()}{/if}</div>
    <div class="banner warn"><div class="grow">{(c && c.error) || "Нет данных"}</div></div>
    {#if c && c.wallet}{@render actions()}{/if}
  </div>
{:else}
  <section class="pcard" class:in-drawer={inDrawer} aria-label="Игрок {playerName(c)}">
    <header class="pcard-head">
      <Avatar img={c.image} code={c.short} lg />
      <div class="grow">
        <div class="row" style="gap:8px"><h2 class="pname">{playerName(c)}</h2>{#if c.verified}<span class="chip b sm">проверен Polymarket</span>{/if}</div>
        <div class="m">
          <span>{shortAddr(c.wallet)}</span>
          {#if c.x}<a class="link" href="https://x.com/{encodeURIComponent(c.x)}" target="_blank" rel="noopener">@{c.x}</a>{/if}
          <span>на Polymarket {monthsSince(c.joined_ts)}</span>
        </div>
        <div class="row" style="gap:6px;margin-top:8px">{@render chips()}</div>
      </div>
    </header>
    <div class="kpis">
      <Kpi label="Прибыль за всё время" value={compactMoney(c.pnl_all_usd, { sign: true })} sub={c.rank_pnl ? `место в рейтинге: ${fmtNum(c.rank_pnl)}` : "вне рейтинга"} cls={tone(c.pnl_all_usd)} />
      <Kpi label="За 30 дней" value={compactMoney(c.pnl_30d_usd, { sign: true })} sub="за 7 дней {compactMoney(c.pnl_7d_usd, { sign: true })}" cls={tone(c.pnl_30d_usd)} />
      <Kpi label="Объём за 30 дней" value={compactMoney(c.volume_30d_usd)} sub={c.trades_30d ? `${fmtNum(c.trades_30d)} ${plural(c.trades_30d, "сделка", "сделки", "сделок")}` : ""} />
      <Kpi label="В рынках сейчас" value={compactMoney(c.value_usd)} sub={c.positions ? `${c.positions.length}+ ${plural(c.positions.length, "позиция", "позиции", "позиций")}` : ""} />
      <Kpi label="Рынков всего" value={c.markets ? fmtNum(c.markets) : "—"} sub={c.biggest_win_usd ? `крупнейший выигрыш ${compactMoney(c.biggest_win_usd)}` : ""} />
      <Kpi label="Наибольшая просадка" value={c.max_drawdown_usd ? compactMoney(-c.max_drawdown_usd) : "—"} sub="от пика кривой прибыли" />
    </div>
    <div class="pnl-box"><PnlChart series={c.series} height={inDrawer ? 140 : 180} /></div>
    <Form form={c.form} weeks={c.weeks} />
    {#if c.positions}<div class="col" style="gap:8px"><h3>Открытые позиции</h3><PositionsList list={c.positions} limit={inDrawer ? 6 : 10} /></div>{/if}
    {@render actions()}
    <div class="sub">Цифры — готовые данные Polymarket. Они показывают, сколько заработал игрок; можно ли повторить его ставки с задержкой — покажет проверка.</div>
  </section>
{/if}
