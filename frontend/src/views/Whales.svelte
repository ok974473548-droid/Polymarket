<script lang="ts">
  // Список китов: статус, выигрыши, прибыль, динамика, переключатель копирования
  import Avatar from "../components/Avatar.svelte";
  import Banners from "../components/Banners.svelte";
  import Icon from "../components/Icon.svelte";
  import Seal from "../components/Seal.svelte";
  import Spark from "../components/Spark.svelte";
  import { api, type Json } from "../lib/api";
  import { compactMoney, FORM_TEXT, money, pctText, plural, signedPct, statusChip, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { softRefresh, toast } from "../lib/state.svelte";

  const d = loader(() => "/whales");
  const items = $derived((d.data?.items || []) as Json[]);
  const copying = $derived(items.filter((r) => r.copy).length);

  // поиск, фильтр и сортировка — в браузере, список китов небольшой
  let q = $state("");
  let show = $state<"all" | "copy" | "watch">("all");
  let sortKey = $state<"" | "win" | "bets" | "pnl">("");
  let sortDir = $state(-1);
  const SORT: Record<string, (r: Json) => number> = {
    win: (r) => (r.stats || {}).win_pct ?? -1,
    bets: (r) => (r.stats || {}).bets ?? 0,
    pnl: (r) => (r.stats || {}).pnl_usd ?? -1e12,
  };
  const shown = $derived.by(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    let out = items.filter((r) => (show === "all" ? true : show === "copy" ? r.copy : !r.copy));
    if (words.length) {
      out = out.filter((r) => {
        const t = `${view(r).name || ""} ${r.name} ${r.entity_id} ${(r.categories || []).join(" ")}`.toLowerCase();
        return words.every((w) => t.includes(w));
      });
    }
    if (sortKey) out = [...out].sort((a, b) => sortDir * (SORT[sortKey](a) - SORT[sortKey](b)));
    return out;
  });
  const filtered = $derived(q.trim() !== "" || show !== "all");
  function sortBy(k: "win" | "bets" | "pnl") {
    if (sortKey === k) sortDir = -sortDir;
    else (sortKey = k), (sortDir = -1);
  }
  const arr = (k: string) => (sortKey === k ? (sortDir < 0 ? "▼" : "▲") : "");
  const openSum = $derived(items.reduce((a, r) => a + ((r.stats || {}).open || 0), 0));

  // имена и аватары из готовых данных Polymarket — догружаются после показа списка, по 4 за раз
  let mini = $state<Record<string, Json>>({});
  let filling = false;
  $effect(() => {
    const need = items.filter((r) => !r.pm && !(r.entity_id in mini)).map((r) => r.entity_id as string);
    if (!need.length || filling) return;
    filling = true;
    void (async () => {
      for (let i = 0; i < need.length; i += 4) {
        await Promise.all(
          need.slice(i, i + 4).map(async (eid) => {
            try {
              const c = await api(`/players/${encodeURIComponent(eid)}?mini=1`);
              mini[eid] = c && c.ok !== false ? c : {};
            } catch {
              mini[eid] = {}; // без имени — останется короткий код
            }
          }),
        );
      }
      filling = false;
    })();
  });

  function view(r: Json) {
    if (r.pm) return { name: r.pm.name, image: r.pm.image, form: r.pm.form, pnl30: r.pm.pnl_30d_usd };
    const c = mini[r.entity_id] || {};
    return { name: c.name, image: c.image, form: c.form && c.form.state, pnl30: c.pnl_30d_usd };
  }

  async function setCopy(r: Json, on: boolean) {
    try {
      const x = await api("/whales/" + encodeURIComponent(r.entity_id), { method: "PATCH", body: { mode: on ? "copy" : "watch" } });
      toast(x.message || (x.mode === "copy" ? "Копирование включено" : "Копирование выключено — только следим"));
    } catch (e) {
      toast((e as Error).message);
    }
    softRefresh();
  }
  async function removeAll(r: Json) {
    const ok = await confirmDialog({
      title: "Удалить кита и все его данные?",
      ok: "Удалить",
      danger: true,
      text: "Кит уйдёт из списка, а его сделки, позиции, оценки и сигналы без ставок будут удалены из базы. Ваши ставки по нему останутся. Отменить нельзя.",
    });
    if (!ok) return;
    try {
      const x = await api("/whales/" + encodeURIComponent(r.entity_id), { method: "DELETE" });
      toast(`Кит удалён: записей — ${x.rows}`);
    } catch (e) {
      toast((e as Error).message);
    }
    softRefresh();
  }
  function go(e: Event, r: Json) {
    if ((e.target as HTMLElement).closest("label,input,a,button")) return;
    location.hash = "#/whales/" + encodeURIComponent(r.entity_id);
  }
  function key(e: KeyboardEvent, r: Json) {
    if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
      e.preventDefault();
      location.hash = "#/whales/" + encodeURIComponent(r.entity_id);
    }
  }
</script>

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  <Banners />
  <div class="page-head">
    <div>
      <h1>Киты</h1>
      <div class="lede">Игроки, за которыми вы следите. «Надёжный» — преимущество подтверждено на многих независимых событиях, а не удачей.</div>
    </div>
    <div class="row"><a class="btn" href="#/rotation">Ротация по форме</a><a class="btn p" href="#/whales/add">Добавить игрока</a></div>
  </div>
  {#if items.length}
    <div class="row" style="gap:28px">
      <div class="fact"><div class="label">В списке</div><div class="v">{items.length}</div></div>
      <div class="fact"><div class="label">Копируем</div><div class="v">{copying}</div></div>
      <div class="fact"><div class="label">Открытых ставок у китов</div><div class="v">{openSum}</div></div>
    </div>
  {/if}
  {#if d.data.new_candidates}
    {@const n = d.data.new_candidates}
    <div class="banner info">
      <div class="grow">
        <b>Система нашла {n} {plural(n, "нового кандидата", "новых кандидата", "новых кандидатов")}</b>
        <div class="sub">Они прошли проверку. Решение за вами.</div>
      </div>
      <a class="btn" href="#/whales/add">Посмотреть</a>
    </div>
  {/if}
  {#if items.length > 1}
    <div class="toolbar">
      <label class="search"><Icon name="search" size={16} /><input class="inp" type="search" placeholder="Имя, код или тема" aria-label="Найти кита" bind:value={q} /></label>
      <div class="seg sm" role="group" aria-label="Каких китов показать">
        <button aria-pressed={show === "all"} onclick={() => (show = "all")}>Все {items.length}</button>
        <button aria-pressed={show === "copy"} onclick={() => (show = "copy")}>Копируем {copying}</button>
        <button aria-pressed={show === "watch"} onclick={() => (show = "watch")}>Следим {items.length - copying}</button>
      </div>
      {#if filtered}<span class="filter-note">Показано {shown.length} из {items.length}<button class="link inline" onclick={() => ((q = ""), (show = "all"))}>сбросить</button></span>{/if}
    </div>
  {/if}
  <div class="ledger">
    {#if items.length}
      <div class="ledger-head whales-grid">
        <span>Кит</span><span>Статус</span>
        <span class="n"><button class="sort-h" class:on={sortKey === "win"} onclick={() => sortBy("win")}>Выигрыши<span class="arr">{arr("win")}</span></button></span>
        <span class="n hide-md"><button class="sort-h" class:on={sortKey === "bets"} onclick={() => sortBy("bets")}>Ставок<span class="arr">{arr("bets")}</span></button></span>
        <span class="n"><button class="sort-h" class:on={sortKey === "pnl"} onclick={() => sortBy("pnl")}>Прибыль кита<span class="arr">{arr("pnl")}</span></button></span>
        <span class="hide-md">Динамика</span><span>Копирование</span>
      </div>
    {/if}
    {#if items.length && !shown.length}
      <div class="ledger-empty">Никого не нашлось. <button class="link inline" onclick={() => ((q = ""), (show = "all"))}>Показать всех</button></div>
    {/if}
    {#each shown as r (r.entity_id)}
      {@const st = r.stats || {}}
      {@const v = view(r)}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="ledger-row whales-grid click" role="link" tabindex="0" data-go="#/whales/{encodeURIComponent(r.entity_id)}" onclick={(e) => go(e, r)} onkeydown={(e) => key(e, r)}>
        <div class="who" data-player={r.entity_id}>
          {#if v.image}<Avatar img={v.image} code={r.name} />{:else}<Seal code={r.name} />{/if}
          <div class="grow">
            <div class="t nm">{v.name || "Кит " + r.name}</div>
            <div class="m">
              <span>{r.categories.join(", ") || "темы появятся после пересчёта"}</span>
              {#if v.form && FORM_TEXT[v.form]}<span class="chip {FORM_TEXT[v.form][0]} sm">{FORM_TEXT[v.form][1]}</span>{/if}
              {#if v.pnl30 !== null && v.pnl30 !== undefined}<span class={tone(v.pnl30)}>30 дн {compactMoney(v.pnl30, { sign: true })}</span>{/if}
            </div>
          </div>
        </div>
        <div><span class="chip {statusChip(r)}">{r.status}</span>{#if r.note}<div class="sub" style="margin-top:4px">{r.note}</div>{/if}</div>
        <div class="n"><div class="tnum">{pctText(st.win_pct)}</div><div class="sub">из {st.closed ?? 0}</div></div>
        <div class="n hide-md"><div class="tnum">{st.bets ?? 0}</div><div class="sub">{st.open ? st.open + " откр." : "ставок"}</div></div>
        <div class="n">
          <b class="tnum {tone(st.pnl_usd)}">{money(st.pnl_usd, { sign: true })}</b>
          <div class="sub">{st.roi_pct === null || st.roi_pct === undefined ? "" : signedPct(st.roi_pct) + " к вложенному"}</div>
        </div>
        <div class="hide-md hide-sm"><Spark values={st.spark} /><div class="sub">12 недель</div></div>
        <div>
          {#if r.can_copy || r.copy}
            <label class="ck" style="min-height:36px">
              <input type="checkbox" data-act="copy" checked={r.copy} onchange={(e) => setCopy(r, e.currentTarget.checked)} />{r.copy ? "Копируем" : "Выкл"}
            </label>
          {:else}
            <span class="sub">{r.note || "Только следить"}</span>
          {/if}
          <button class="del" data-act="delete-whale" title="Удалить кита и все его данные" aria-label="Удалить кита {v.name || r.name} и все его данные" onclick={() => removeAll(r)}>Удалить</button>
        </div>
      </div>
    {:else}
      {#if !items.length}
      <div class="ledger-empty">
        <div class="empty-ic" style="margin:0 auto 14px;width:56px;height:56px;border-radius:16px;display:flex;align-items:center;justify-content:center;color:#fff;background:var(--grad)"><Icon name="whale" size={26} /></div>
        <div style="font:700 20px/1.2 var(--sans);color:var(--pearl)">Список пуст</div>
        <div style="margin:8px auto 16px;max-width:50ch">Добавьте кита по адресу кошелька или ссылке на профиль. Данные собираются только по китам из этого списка.</div>
        <a class="btn p" href="#/whales/add">Добавить кита</a>
      </div>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .del {
    display: block;
    margin-top: 2px;
    padding: 0;
    min-height: 28px;
    background: none;
    border: 0;
    color: var(--muted);
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .del:hover,
  .del:focus-visible {
    color: var(--bad-ink);
    text-decoration: underline;
  }
</style>
