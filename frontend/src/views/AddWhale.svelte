<script lang="ts">
  // «Добавить игрока»: поиск по ссылке/адресу/имени, рейтинг Polymarket, пачечное добавление
  import { untrack } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import Avatar from "../components/Avatar.svelte";
  import CheckJob from "../components/CheckJob.svelte";
  import PlayerCard from "../components/PlayerCard.svelte";
  import Seal from "../components/Seal.svelte";
  import { api, type Json } from "../lib/api";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { compactMoney, plural, shortAddr, signedPct, tone, TOPICS } from "../lib/format";
  import { addPlayers } from "../lib/players.svelte";
  import { app, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();

  let w = $state<Json>(null);
  api("/whales")
    .then((x) => (w = x))
    .catch(() => (w = { candidates: [] }));

  // ---- поиск игрока
  let q = $state(untrack(() => params[0]) || "");
  let found = $state<{ kind: "none" | "busy" | "err" | "empty" | "list" | "card"; text?: string; items?: Json[]; card?: Json }>({ kind: "none" });
  let job = $state(""); // идущая проверка игрока
  let seq = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function find(text: string) {
    text = (text || "").trim();
    if (text.length < 2) {
      found = { kind: "none" };
      return;
    }
    const my = ++seq;
    found = { kind: "busy", text: "Ищем игрока…" };
    let r: Json;
    try {
      r = await api("/players/resolve?q=" + encodeURIComponent(text));
    } catch (e) {
      r = { ok: false, error: (e as Error).message, items: [] };
    }
    if (my !== seq) return;
    if (!r.ok) found = { kind: "err", text: r.error };
    else if (!r.items.length) found = { kind: "empty" };
    else if (r.items.length === 1 || r.items[0].exact) await show(r.items[0].wallet);
    else found = { kind: "list", items: r.items };
  }
  async function show(wallet: string) {
    const my = ++seq;
    found = { kind: "busy", text: "Загружаем статистику Polymarket…" };
    try {
      const c = await api("/players/" + encodeURIComponent(wallet));
      if (my !== seq) return;
      found = { kind: "card", card: c };
      if (c.checking) job = c.checking;
    } catch (e) {
      found = { kind: "err", text: (e as Error).message };
    }
  }
  function onInput() {
    clearTimeout(timer);
    timer = setTimeout(() => find(q), 450);
  }
  if (untrack(() => q)) void find(untrack(() => q));

  // ---- рейтинг Polymarket
  let cat = $state("overall");
  let period = $state("month");
  let order = $state("pnl");
  let lb = $state<Json[]>([]);
  let lbErr = $state("");
  let lbLoading = $state(true);
  let hasMore = $state(false);
  let offset = $state(0);
  const picked = new SvelteSet<string>();

  async function loadLb(off = 0) {
    if (!off) lbLoading = true;
    let r: Json;
    try {
      r = await api(`/leaderboard?category=${cat}&period=${period}&order=${order}&offset=${off}`);
    } catch (e) {
      r = { ok: false, error: (e as Error).message, items: [] };
    }
    lbLoading = false;
    if (!r.ok) {
      lbErr = r.error;
      return;
    }
    lbErr = "";
    lb = off ? [...lb, ...r.items] : r.items;
    offset = off;
    hasMore = !!r.has_more && off + 50 <= 1000;
  }
  $effect(() => {
    void cat;
    void period;
    void order;
    void loadLb(0);
  });

  const pageWallets = $derived(lb.filter((x) => !x.in_list).map((x) => x.wallet as string));
  const allPicked = $derived(pageWallets.length > 0 && pageWallets.every((x) => picked.has(x)));
  function pickPage(on: boolean) {
    for (const x of pageWallets) on ? picked.add(x) : picked.delete(x);
  }
  async function bulkAdd(mode: string) {
    const ws = [...picked];
    if (!ws.length) return;
    if (ws.length > 10) {
      const ok = await confirmDialog({
        title: `Добавить ${ws.length} ${plural(ws.length, "игрока", "игроков", "игроков")}?`,
        ok: "Добавить",
        text: "Каждого проверим в фоне — по два одновременно, так что вся пачка займёт время. Данные начнут собираться только по этим игрокам.",
      });
      if (!ok) return;
    }
    try {
      await addPlayers(ws, mode);
      picked.clear();
      void loadLb(0);
    } catch (e) {
      toast((e as Error).message);
    }
  }
  function openRow(e: Event, wallet: string) {
    if ((e.target as HTMLElement).closest("a,input,label")) return;
    app.panel = { kind: "player", id: wallet };
  }
  function rowKey(e: KeyboardEvent, wallet: string) {
    if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
      e.preventDefault();
      app.panel = { kind: "player", id: wallet };
    }
  }
  async function addCandidate(id: string) {
    try {
      const r = await api("/whales", { method: "POST", body: { entity_id: id, mode: "copy" } });
      toast(r.message || (r.mode === "copy" ? "Кит добавлен, копирование включено" : "Кит добавлен в режиме «Только следить»"));
      location.hash = "#/whales";
    } catch (e) {
      toast((e as Error).message);
    }
  }
  function afterAdd(r: Json) {
    const j = (r.added || [])[0];
    if (found.kind === "card" && found.card) void show(found.card.wallet);
    if (j && j.job_id) job = j.job_id;
  }
</script>

<a class="back" href="#/whales">← Все киты</a>
<div class="page-head">
  <div>
    <h1>Добавить игрока</h1>
    <div class="lede">
      Найдите игрока по ссылке на профиль, адресу кошелька или имени. Готовая статистика Polymarket появится сразу; проверка, можно ли повторять его ставки, идёт в фоне.
    </div>
  </div>
</div>
<form
  class="finder card"
  id="finder"
  autocomplete="off"
  role="search"
  onsubmit={(e) => {
    e.preventDefault();
    clearTimeout(timer);
    void find(q);
  }}
>
  <label for="finder-q">Ссылка, адрес или имя игрока</label>
  <div class="row">
    <input id="finder-q" class="inp" placeholder="polymarket.com/@имя, 0x… или имя" aria-describedby="finder-hint" bind:value={q} oninput={onInput} />
    <button class="btn p" type="submit">Найти</button>
  </div>
  <div class="sub" id="finder-hint">Подойдёт ссылка вида polymarket.com/profile/0x… или polymarket.com/@name, адрес 0x… или имя игрока.</div>
</form>
<div id="finder-out" aria-live="polite">
  {#if found.kind === "busy"}
    <div class="card skeleton">{found.text}</div>
  {:else if found.kind === "err"}
    <div class="banner warn"><div class="grow">{found.text}</div></div>
  {:else if found.kind === "empty"}
    <div class="card sub">Никого не нашли. Проверьте написание или вставьте ссылку на профиль.</div>
  {:else if found.kind === "list"}
    <div class="ledger">
      {#each found.items || [] as i (i.wallet)}
        <button class="ledger-row pick" style="grid-template-columns:minmax(0,1fr) auto" data-act="pshow" onclick={() => show(i.wallet)}>
          <div class="who">
            <Avatar img={i.image} code={i.short} />
            <div class="grow">
              <div class="t">{i.name || i.short}</div>
              <div class="m">
                <span>{shortAddr(i.wallet)}</span>
                {#if i.in_list}<span class="chip {i.mode === 'copy' ? 'g' : 'i'}">{i.mode === "copy" ? "копируем" : "в списке · следим"}</span>{/if}
              </div>
            </div>
          </div>
          <span class="btn">Показать</span>
        </button>
      {/each}
    </div>
  {:else if found.kind === "card" && found.card}
    <PlayerCard c={found.card} onadded={afterAdd} />
  {/if}
</div>
<div id="check-result">{#if job}{#key job}<CheckJob {job} />{/key}{/if}</div>

<div class="top" style="margin-top:6px">
  <div>
    <h2>Рейтинг Polymarket</h2>
    <div class="sub" style="margin-top:4px">
      Отметьте игроков и добавьте пачкой. Нажмите на строку — откроется карточка игрока. Высокая прибыль ещё не значит, что ставки можно повторить: это покажет проверка.
    </div>
  </div>
  <div class="row" id="lb-filters">
    <select class="inp" id="lb-cat" aria-label="Тема" bind:value={cat}>
      <option value="overall">Все темы</option>
      {#each TOPICS as [k, l] (k)}<option value={k}>{l}</option>{/each}
    </select>
    <select class="inp" id="lb-period" aria-label="Период" bind:value={period}>
      <option value="month">За месяц</option><option value="week">За неделю</option><option value="all">За всё время</option><option value="day">За день</option>
    </select>
    <select class="inp" id="lb-order" aria-label="Сортировка" bind:value={order}><option value="pnl">По прибыли</option><option value="vol">По объёму</option></select>
  </div>
</div>
<div id="lb-box">
  {#if lbLoading}
    <div class="card skeleton">Загружаем рейтинг…</div>
  {:else if lbErr}
    <div class="banner warn">
      <div class="grow">
        <b>Рейтинг не загрузился</b>
        <div>{lbErr}</div>
        <div class="sub">Проверьте доступ на экране <a class="link" href="#/diagnostics">«Диагностика»</a>.</div>
      </div>
    </div>
  {:else}
    <div class="ledger" id="lb-ledger">
      {#if lb.length}
        <div class="ledger-head lb-row">
          <label class="ck lb-ck"><input type="checkbox" data-act="lb-pick-page" aria-label="Отметить всех на странице" checked={allPicked} onchange={(e) => pickPage(e.currentTarget.checked)} /></label>
          <span>#</span><span>Игрок</span><span class="n">Прибыль</span><span class="n hide-sm">Объём</span><span class="n hide-md">К объёму</span>
        </div>
      {/if}
      {#each lb as x (x.wallet)}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div
          class="ledger-row lb-row"
          class:picked={picked.has(x.wallet)}
          data-act="lb-open"
          data-id={x.wallet}
          tabindex="0"
          role="button"
          aria-label="Карточка игрока {x.name || x.short}"
          onclick={(e) => openRow(e, x.wallet)}
          onkeydown={(e) => rowKey(e, x.wallet)}
        >
          <label class="ck lb-ck">
            {#if x.in_list}
              <span class="chip g sm" title="Уже в вашем списке" aria-label="в списке">✓</span>
            {:else}
              <input
                type="checkbox"
                data-act="lb-pick"
                checked={picked.has(x.wallet)}
                aria-label="Выбрать {x.name || x.short}"
                onchange={(e) => (e.currentTarget.checked ? picked.add(x.wallet) : picked.delete(x.wallet))}
              />
            {/if}
          </label>
          <div class="tnum sub rank">{x.rank || "—"}</div>
          <div class="who" data-player={x.wallet}>
            <Seal code={x.short} />
            <div class="grow">
              <div class="t">{x.name || x.short}{#if x.verified}{" "}<span class="chip b sm">проверен</span>{/if}</div>
              <div class="m"><span>{shortAddr(x.wallet)}</span>{#if x.checked && !x.in_list}<span class="chip n sm">{x.checked.toLowerCase()}</span>{/if}</div>
            </div>
          </div>
          <div class="n"><b class="tnum {tone(x.pnl)}">{compactMoney(x.pnl, { sign: true })}</b><div class="sub">прибыль</div></div>
          <div class="n hide-sm"><div class="tnum">{compactMoney(x.volume)}</div><div class="sub">объём</div></div>
          <div class="n hide-md"><div class="tnum">{x.roi_pct === null ? "—" : signedPct(x.roi_pct)}</div><div class="sub">к объёму</div></div>
        </div>
      {:else}
        <div class="ledger-empty">В этой теме за выбранный период рейтинг пуст.</div>
      {/each}
    </div>
    {#if hasMore}<button class="btn" id="lb-more" style="margin-top:12px" onclick={() => loadLb(offset + 50)}>Показать ещё 50</button>{/if}
  {/if}
</div>
<div class="bulk" id="bulk" hidden={picked.size === 0} role="region" aria-label="Выбранные игроки">
  <b id="bulk-n">Выбрано: {picked.size}</b>
  <button class="btn p" data-act="bulk-add" onclick={() => bulkAdd("copy")}>Добавить и копировать после проверки</button>
  <button class="btn" data-act="bulk-add" onclick={() => bulkAdd("watch")}>Только следить</button>
  <button class="link" data-act="bulk-clear" style="background:none;border:0;cursor:pointer;min-height:44px" onclick={() => picked.clear()}>Снять выбор</button>
</div>
{#if w && w.candidates && w.candidates.length}
  <h2>Найдены автопоиском</h2>
  <div class="ledger">
    {#each w.candidates as c (c.entity_id)}
      <div class="ledger-row" style="grid-template-columns:minmax(0,1fr) auto">
        <div class="who">
          <Seal code={c.name} />
          <div><div class="t nm">Кит {c.name}</div><div class="m"><span>{c.categories.join(", ")}</span><span>риск: {c.risk}</span></div></div>
        </div>
        <button class="btn" data-act="add" onclick={() => addCandidate(c.entity_id)}>Добавить и копировать</button>
      </div>
    {/each}
  </div>
{/if}
