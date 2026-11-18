<script lang="ts">
  // Страница события: график вероятностей исходов (живой), исходы с «Купить Да / Нет», стакан, правила,
  // наши ставки в этом событии. Всё обновляется само: цены — раз в 3 с (или сразу из потока), событие — раз в 15 с.
  import { onDestroy, untrack } from "svelte";
  import BuyDialog from "../components/BuyDialog.svelte";
  import Dossier from "../components/Dossier.svelte";
  import EventChart from "../components/EventChart.svelte";
  import { api, type Json } from "../lib/api";
  import { centsOf, type ChartLine, changeText, endText, LINE_COLORS, mid, pctOf, type Quote, TOPIC_LABEL } from "../lib/events";
  import { compactMoney, money, safeUrl, tone } from "../lib/format";
  import { app, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const id = $derived(params[0]);

  let ev = $state<Json>(null);
  let err = $state("");
  let period = $state("1w");
  let keys = $state<string[]>([]); // исходы на графике
  let hist = $state<Json>(null);
  let histErr = $state("");
  let quotes = $state<Record<string, Quote>>({});
  let open = $state<string | null>(null); // раскрытый исход (стакан)
  let showAll = $state(false);
  let order = $state<{ token: string; title: string; side: string; ask: number | null } | null>(null);
  let tab = $state("dossier");

  async function loadEvent() {
    try {
      const r = await api("/catalog/" + encodeURIComponent(untrack(() => id)));
      if (!r.ok) {
        err = r.error;
        return;
      }
      err = "";
      ev = r;
      if (!untrack(() => keys).length) keys = r.chart_keys;
    } catch (e) {
      err = (e as Error).message;
    }
  }
  async function loadHistory() {
    const k = untrack(() => keys);
    if (!k.length) return;
    try {
      const r = await api(`/catalog/${encodeURIComponent(untrack(() => id))}/history?period=${untrack(() => period)}&keys=${k.join(",")}`);
      if (r.ok) {
        hist = r;
        histErr = "";
      } else histErr = r.error;
    } catch (e) {
      histErr = (e as Error).message;
    }
  }
  const liveTokens = $derived.by(() => {
    if (!ev) return [] as string[];
    const rows = (ev.outcomes as Json[]).filter((r) => !r.closed);
    const vis = rows.slice(0, showAll ? 12 : 8);
    const toks: string[] = [];
    for (const r of vis) {
      toks.push(r.yes_token);
      if (r.no_token && r.single === false) toks.push(r.no_token);
    }
    if (rows.length && rows[0].single && rows[0].no_token) toks.push(rows[0].no_token);
    for (const k of keys) if (!toks.includes(k)) toks.push(k);
    return [...new Set(toks)].slice(0, 24);
  });
  async function loadLive() {
    const toks = untrack(() => liveTokens);
    if (!toks.length || document.hidden) return;
    try {
      const r = await api(`/catalog/${encodeURIComponent(untrack(() => id))}/live?tokens=${toks.join(",")}`);
      if (r.ok) quotes = { ...untrack(() => quotes), ...r.quotes };
    } catch {
      /* временная ошибка сети — следующий опрос через 3 с */
    }
  }

  $effect(() => {
    void id;
    ev = null;
    keys = [];
    hist = null;
    untrack(() => loadEvent()).then(() => {
      void loadHistory();
      void loadLive();
    });
  });
  $effect(() => {
    void period;
    void keys.length;
    void keys.join();
    untrack(() => loadHistory());
  });

  // после покупки и при изменениях из потока событий — перечитать событие (наши ставки, лимиты)
  let seenRefresh = app.refresh;
  $effect(() => {
    if (app.refresh !== seenRefresh) {
      seenRefresh = app.refresh;
      untrack(() => loadEvent());
    }
  });

  // живой режим
  const tLive = setInterval(() => void loadLive(), 3000);
  const tEvent = setInterval(() => {
    if (!document.hidden) void loadEvent();
  }, 15000);
  const tHist = setInterval(() => {
    if (!document.hidden && (period === "1h" || period === "6h" || period === "1d")) void loadHistory();
  }, 60000);
  onDestroy(() => {
    clearInterval(tLive);
    clearInterval(tEvent);
    clearInterval(tHist);
  });

  // цена из потока (SSE) новее опроса — берём её
  function q(tok: string | null | undefined): Quote | undefined {
    if (!tok) return undefined;
    const sse = app.quotes[tok];
    const polled = quotes[tok];
    if (sse && (sse.bid !== null || sse.ask !== null)) return { ...polled, bid: sse.bid, ask: sse.ask, live: true };
    return polled;
  }
  function priceOf(r: Json): number | null {
    const m = mid(q(r.yes_token));
    return m !== null ? m : r.price;
  }
  function askOf(tok: string | null, fallback: number | null): number | null {
    const x = q(tok);
    return x && x.ask !== null && x.ask !== undefined ? x.ask : fallback;
  }
  function noAsk(r: Json): number | null {
    const yes = q(r.yes_token);
    const fb = yes && yes.bid !== null && yes.bid !== undefined ? 1 - yes.bid : r.price !== null ? 1 - r.price : null;
    return askOf(r.no_token, fb !== null ? Math.round(fb * 1000) / 1000 : null);
  }

  const lines = $derived.by<ChartLine[]>(() => {
    if (!hist) return [];
    return (hist.lines as Json[]).map((l, i) => ({ key: l.key, title: l.title, color: colorOf(l.key, i), points: l.points }));
  });
  function colorOf(key: string, i: number): string {
    const idx = keys.indexOf(key);
    return LINE_COLORS[(idx >= 0 ? idx : i) % LINE_COLORS.length];
  }
  const liveMid = $derived.by(() => {
    const out: Record<string, number | null> = {};
    for (const k of keys) out[k] = mid(q(k));
    return out;
  });
  function toggleLine(key: string) {
    if (keys.includes(key)) {
      if (keys.length > 1) keys = keys.filter((k) => k !== key);
    } else if (keys.length < 8) keys = [...keys, key];
    else toast("На графике не больше 8 исходов");
  }
  function buy(r: Json, side: "yes" | "no") {
    const yes = side === "yes";
    order = {
      token: yes ? r.yes_token : r.no_token,
      title: r.single ? ev.title : r.title,
      side: yes ? r.yes_label : r.no_label,
      ask: yes ? askOf(r.yes_token, r.best_ask ?? r.price) : noAsk(r),
    };
  }
  const PERIODS: [string, string][] = [
    ["1h", "1Ч"],
    ["6h", "6Ч"],
    ["1d", "1Д"],
    ["1w", "1Н"],
    ["1m", "1М"],
    ["all", "Всё"],
  ];
  const rows = $derived(ev ? ((ev.outcomes as Json[]) || []) : []);
  const openRows = $derived(rows.filter((r) => !r.closed));
  const visible = $derived(showAll ? rows : openRows.slice(0, 10));
  const anyLive = $derived(liveTokens.some((t) => q(t)?.live));
</script>

{#if err && !ev}
  <a class="back" href="#/events">← События</a>
  <div class="card err" role="alert"><b>Событие не загрузилось</b><div>{err}</div></div>
{:else if !ev}
  <div class="card skeleton" style="min-height:420px">Загружаем событие…</div>
{:else}
  <a class="back" href="#/events">← События</a>
  <div class="page-head ev-head">
    <div class="who" style="align-items:flex-start">
      {#if ev.image}<img class="ev-img lg" src={ev.image} alt="" referrerpolicy="no-referrer" />{/if}
      <div>
        <div class="sub">{TOPIC_LABEL[ev.topic] || "Разное"}{ev.tags.length ? " · " + ev.tags.slice(0, 3).join(" · ") : ""}</div>
        <h1 style="font-size:34px;margin-top:4px">{ev.title}</h1>
        <div class="row sub" style="gap:14px;margin-top:8px">
          <span>{compactMoney(ev.volume)} объём</span>
          {#if ev.volume24}<span>{compactMoney(ev.volume24)} за сутки</span>{/if}
          {#if ev.end_ts}<span>{endText(ev.end_ts)}</span>{/if}
          <span class="live-dot" class:on={anyLive} title={anyLive ? "Цены приходят потоком" : "Цены обновляются раз в 3 с"}>{anyLive ? "в реальном времени" : "обновляется каждые 3 с"}</span>
        </div>
      </div>
    </div>
    {#if ev.url}<a class="btn" href={safeUrl(ev.url)} target="_blank" rel="noopener">Открыть на Polymarket</a>{/if}
  </div>

  <section class="card col" style="gap:10px" aria-label="График вероятностей">
    {#if lines.length}
      {#key hist}
        <EventChart {lines} live={liveMid} height={320} />
      {/key}
    {:else if histErr}
      <div class="sub">{histErr}</div>
    {:else}
      <div class="skeleton" style="height:320px"></div>
    {/if}
    <div class="row" style="justify-content:space-between">
      <span class="sub">{ev.neg_risk ? "Исходы взаимоисключающие: сбудется только один." : ""} Колёсиком — масштаб; линии — флажками у исходов.</span>
      <div class="seg" role="group" aria-label="Период графика">
        {#each PERIODS as [k, l] (k)}<button aria-pressed={period === k} data-period={k} onclick={() => (period = k)}>{l}</button>{/each}
      </div>
    </div>
  </section>

  {#if ev.mine.length}
    <section class="card col" style="gap:8px" aria-label="Наши ставки в событии">
      <h2>Ваши ставки в этом событии</h2>
      {#each ev.mine as m (m.token_id)}
        <a class="bet" href="#/bet/{encodeURIComponent(m.token_id)}" style="text-decoration:none;color:inherit">
          <span><b style="font-weight:500">{m.title}</b> <span class="sub">«{m.side}» · вход {m.entry_cents}¢ · {m.is_open ? "открыта" : "закрыта"}</span></span>
          <b class="tnum {tone(m.pnl_usd)}">{money(m.pnl_usd, { sign: true })}</b>
        </a>
      {/each}
    </section>
  {/if}

  <section class="ledger ev-outcomes" aria-label="Исходы">
    <div class="ledger-head ev-row"><span></span><span>Исход</span><span class="n">Шанс</span><span class="ev-buy-head">Купить</span></div>
    {#each visible as r, i (r.key)}
      {@const p = priceOf(r)}
      {@const ya = askOf(r.yes_token, r.best_ask ?? r.price)}
      {@const na = noAsk(r)}
      {@const ki = keys.indexOf(r.key)}
      <div class="ev-row ledger-row" class:closed={r.closed} data-outcome={r.key}>
        <label class="ck ev-line" title="Показать на графике">
          <input type="checkbox" checked={ki >= 0} onchange={() => toggleLine(r.key)} aria-label="На графике: {r.title}" disabled={r.closed} />
          <i style="background:{ki >= 0 ? LINE_COLORS[ki % LINE_COLORS.length] : 'transparent'}"></i>
        </label>
        <button class="ev-name" onclick={() => (open = open === r.key ? null : r.key)} aria-expanded={open === r.key}>
          {#if r.image && !r.single && r.image !== ev.image}<img class="ev-img sm" src={r.image} alt="" loading="lazy" referrerpolicy="no-referrer" />{/if}
          <span class="grow">
            <b>{r.title}</b>
            <span class="sub">{r.volume ? compactMoney(r.volume) + " объём" : ""}{r.closed ? " · закрыт" : ""}</span>
          </span>
        </button>
        <div class="n ev-pct">
          <b class="tnum">{r.single && r.yes_label !== "Да" ? centsOf(p) : pctOf(p)}</b>
          {#if changeText(r.change_1d)}<span class="ev-chg {r.change_1d > 0 ? 'up' : 'down'}">{changeText(r.change_1d)}</span>{/if}
        </div>
        <div class="ev-buy">
          {#if !r.closed}
            <button class="btn ev-yes-btn" data-act="buy-yes" onclick={() => buy(r, "yes")} disabled={ya === null}>Купить {r.yes_label} {centsOf(ya)}</button>
            {#if !r.single || r.yes_label === "Да"}
              <button class="btn ev-no-btn" data-act="buy-no" onclick={() => buy(r, "no")} disabled={na === null}>Купить {r.no_label} {centsOf(na)}</button>
            {/if}
          {/if}
        </div>
      </div>
      {#if open === r.key}
        {@const bk = q(r.yes_token)}
        <div class="ev-book">
          <div class="col" style="gap:4px">
            <div class="label">Покупатели «{r.yes_label}»</div>
            {#each (bk?.bids || []).slice(0, 6) as [px, sz], j (j)}<div class="ev-lvl up"><span>{centsOf(px)}</span><span class="sub">{Math.round(sz).toLocaleString("ru-RU")} акц.</span></div>{:else}<div class="sub">нет заявок</div>{/each}
          </div>
          <div class="col" style="gap:4px">
            <div class="label">Продавцы «{r.yes_label}»</div>
            {#each (bk?.asks || []).slice(0, 6) as [px, sz], j (j)}<div class="ev-lvl down"><span>{centsOf(px)}</span><span class="sub">{Math.round(sz).toLocaleString("ru-RU")} акц.</span></div>{:else}<div class="sub">нет заявок</div>{/each}
          </div>
          <div class="col" style="gap:6px">
            {#if r.question && r.question !== ev.title}<div class="sub">{r.question}</div>{/if}
            <button class="btn" onclick={() => (app.panel = { kind: "market", id: r.yes_token })}>Держатели и подробности</button>
            {#if i === 0 && !bk}<div class="sub">Стакан появится через несколько секунд</div>{/if}
          </div>
        </div>
      {/if}
    {/each}
    {#if openRows.length > 10 || rows.length > openRows.length}
      <button class="ev-more" onclick={() => (showAll = !showAll)}>{showAll ? "Свернуть" : `Показать все исходы (${rows.length})`}</button>
    {/if}
  </section>

  <section class="card col" style="gap:10px">
    <div class="seg" role="group" aria-label="Подробности">
      <button aria-pressed={tab === "dossier"} data-tab="dossier" onclick={() => (tab = "dossier")}>Досье</button>
      <button aria-pressed={tab === "rules"} onclick={() => (tab = "rules")}>Правила</button>
      <button aria-pressed={tab === "about"} onclick={() => (tab = "about")}>О ставках</button>
    </div>
    {#if tab === "dossier"}
      <Dossier eventId={id} outcomes={openRows} {priceOf} />
    {:else if tab === "rules"}
      <div class="ev-rules">{ev.description || "Правила не указаны"}</div>
      {#if ev.resolution_source}<div class="sub">Источник исхода: <a class="link" href={safeUrl(ev.resolution_source)} target="_blank" rel="noopener">{ev.resolution_source}</a></div>{/if}
    {:else}
      <div class="sub">
        Ставки здесь виртуальные, из общего банка, с отдельным лимитом: до {money(ev.limits.max_bet_usd)} на одну, {money(ev.limits.daily_limit_usd)} в день (осталось сегодня {money(ev.limits.left_today_usd)}).
        Покупка проходит по стакану с комиссией Polymarket; продать можно в «Моих ставках». Совет «что и сколько покупать» появится на следующих этапах модуля.
      </div>
    {/if}
  </section>

  <BuyDialog eventId={id} bind:order limits={ev.limits} feeRate={ev.fee_rate} />
{/if}
