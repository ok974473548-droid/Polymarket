<script lang="ts">
  // «События»: события Polymarket по темам, поиск, сортировка. Цены в карточках обновляются сами.
  import { onDestroy, untrack } from "svelte";
  import { api, type Json } from "../lib/api";
  import { centsOf, changeText, endText, pctOf, TOPIC_LABEL } from "../lib/events";
  import { compactMoney, TOPICS } from "../lib/format";

  let topic = $state(sessionStorage.getItem("wf-ev-topic") || "");
  let sort = $state(sessionStorage.getItem("wf-ev-sort") || "volume24");
  let q = $state("");
  let items = $state<Json[]>([]);
  let hasMore = $state(false);
  let offset = $state(0);
  let loading = $state(true);
  let err = $state("");
  let seq = 0;

  async function load(off = 0, quiet = false) {
    const my = ++seq;
    if (!quiet) loading = !off;
    try {
      const r = await api(`/catalog?topic=${encodeURIComponent(topic)}&sort=${sort}&q=${encodeURIComponent(q.trim())}&offset=${off}`);
      if (my !== seq) return;
      if (!r.ok) {
        err = r.error;
        return;
      }
      err = "";
      if (off) items = [...items, ...r.items.filter((x: Json) => !items.some((y) => y.id === x.id))];
      else if (quiet && offset) {
        // тихое обновление: свежие цены для уже показанных карточек первой страницы
        const fresh = new Map(r.items.map((x: Json) => [x.id, x]));
        items = items.map((x) => fresh.get(x.id) || x);
      } else items = r.items;
      hasMore = r.has_more;
      if (!quiet) offset = off;
    } catch (e) {
      if (my === seq) err = (e as Error).message;
    } finally {
      if (my === seq) loading = false;
    }
  }

  $effect(() => {
    void topic;
    void sort;
    try {
      sessionStorage.setItem("wf-ev-topic", topic);
      sessionStorage.setItem("wf-ev-sort", sort);
    } catch {
      /* без хранилища — до перезагрузки */
    }
    untrack(() => load(0));
  });

  let timer: ReturnType<typeof setTimeout> | undefined;
  function onSearch() {
    clearTimeout(timer);
    timer = setTimeout(() => load(0), 400);
  }
  // живой режим: карточки обновляются каждые 30 с, пока вкладка на экране
  const live = setInterval(() => {
    if (!document.hidden) void load(0, true);
  }, 30000);
  onDestroy(() => clearInterval(live));

  const SORTS: [string, string][] = [
    ["volume24", "Объём за сутки"],
    ["volume", "Объём за всё время"],
    ["liquidity", "Ликвидность"],
    ["ending", "Скоро закроются"],
    ["new", "Новые"],
  ];
</script>

<div class="page-head">
  <div>
    <h1>События</h1>
    <div class="lede">Все события Polymarket по темам. Откройте событие — увидите вероятности исходов во времени, стакан и правила; ставки — виртуальные.</div>
  </div>
</div>
<div class="ev-filters">
  <div class="ev-topics" role="group" aria-label="Темы">
    <button class="chip-btn" aria-pressed={topic === ""} onclick={() => (topic = "")}>Все</button>
    {#each TOPICS as [k, l] (k)}<button class="chip-btn" aria-pressed={topic === k} data-topic={k} onclick={() => (topic = k)}>{l}</button>{/each}
  </div>
  <div class="row" style="gap:8px">
    <input class="inp" type="search" placeholder="Поиск события" aria-label="Поиск события" bind:value={q} oninput={onSearch} style="min-width:220px" />
    <select class="inp" aria-label="Сортировка" bind:value={sort}>
      {#each SORTS as [k, l] (k)}<option value={k}>{l}</option>{/each}
    </select>
  </div>
</div>
{#if err}
  <div class="banner warn"><div class="grow"><b>Список не загрузился</b><div>{err}</div></div><button class="btn" onclick={() => load(0)}>Повторить</button></div>
{:else if loading && !items.length}
  <div class="ev-grid">{#each Array(6) as _, i (i)}<div class="card skeleton ev-card" style="min-height:170px"></div>{/each}</div>
{:else if !items.length}
  <div class="card empty"><b>Ничего не нашли</b><div class="sub">Попробуйте другую тему или слова поиска.</div></div>
{:else}
  <div class="ev-grid" id="ev-grid">
    {#each items as e (e.id)}
      <a class="card ev-card" href="#/events/{encodeURIComponent(e.id)}" data-event={e.id}>
        <div class="ev-card-head">
          {#if e.image}<img class="ev-img" src={e.image} alt="" loading="lazy" referrerpolicy="no-referrer" />{/if}
          <div class="ev-title">{e.title}</div>
        </div>
        {#if e.single && e.top.length && (e.top[0].title === "Да" || e.outcomes_n === 1)}
          <div class="ev-yes"><b class="tnum">{pctOf(e.top[0].price)}</b><span class="sub">шанс «Да»</span>{#if changeText(e.top[0].change_1d)}<span class="ev-chg {e.top[0].change_1d > 0 ? 'up' : 'down'}">{changeText(e.top[0].change_1d)}</span>{/if}</div>
        {:else}
          <div class="ev-tops">
            {#each e.top as t, i (i)}
              <div class="ev-top"><span class="ell">{t.title}</span><b class="tnum">{e.single ? centsOf(t.price) : pctOf(t.price)}</b></div>
            {/each}
            {#if e.outcomes_n > 3}<div class="sub">ещё {e.outcomes_n - 3}</div>{/if}
          </div>
        {/if}
        <div class="ev-meta sub">
          <span>{TOPIC_LABEL[e.topic] || "Разное"}</span>
          <span>{compactMoney(e.volume)} объём</span>
          {#if e.end_ts}<span>{endText(e.end_ts)}</span>{/if}
        </div>
      </a>
    {/each}
  </div>
  {#if hasMore && !q.trim()}<button class="btn" style="align-self:center" onclick={() => load(offset + 24)}>Показать ещё</button>{/if}
{/if}
