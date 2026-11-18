<script lang="ts">
  // Профиль кита: наш итог, готовые данные Polymarket, «что было бы», темы, сигналы
  import Avatar from "../components/Avatar.svelte";
  import Form from "../components/Form.svelte";
  import Kpi from "../components/Kpi.svelte";
  import PnlChart from "../components/PnlChart.svelte";
  import PositionsList from "../components/PositionsList.svelte";
  import Seal from "../components/Seal.svelte";
  import SimCard from "../components/SimCard.svelte";
  import { api, type Json } from "../lib/api";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { ago, compactMoney, fmtNum, money, monthsSince, pct, pctText, safeUrl, shortAddr, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { softRefresh, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const id = $derived(params[0]);
  const d = loader(() => "/whales/" + encodeURIComponent(id));
  const w = $derived(d.data);
  const st = $derived((w && w.stats) || {});

  // блок Polymarket грузится отдельно (внешний API медленнее нашего)
  let pm = $state<Json>(null);
  let pmErr = $state("");
  let pmFor = "";
  $effect(() => {
    const wallet = w && w.wallets && w.wallets[0];
    if (!wallet || wallet === pmFor) return;
    pmFor = wallet;
    pm = null;
    pmErr = "";
    api("/players/" + encodeURIComponent(wallet))
      .then((c) => (pm = c))
      .catch((e: Error) => (pmErr = e.message));
  });

  const since = $derived(
    w && w.tracked_since ? new Date(w.tracked_since * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }) : "—",
  );

  async function setCopy(on: boolean) {
    try {
      const r = await api("/whales/" + encodeURIComponent(w.entity_id), { method: "PATCH", body: { mode: on ? "copy" : "watch" } });
      toast(r.message || (r.mode === "copy" ? "Копирование включено" : "Копирование выключено — только следим"));
    } catch (e) {
      toast((e as Error).message);
    }
    softRefresh();
  }
  async function setTopic(cat: string, on: boolean) {
    try {
      await api("/whales/" + encodeURIComponent(w.entity_id), { method: "PATCH", body: { category: cat, enabled: on } });
      toast(on ? "Тема включена" : "Тема выключена для этого кита");
    } catch (e) {
      toast((e as Error).message);
    }
    softRefresh();
  }
  async function unfollow() {
    const ok = await confirmDialog({
      title: "Перестать следить?",
      ok: "Перестать",
      danger: true,
      text: "Сигналы по этому киту приходить не будут. Открытые ставки останутся.",
    });
    if (!ok) return;
    try {
      await api("/whales/" + encodeURIComponent(w.entity_id), { method: "PATCH", body: { mode: "off" } });
      location.hash = "#/whales";
    } catch (e) {
      toast((e as Error).message);
    }
  }
  const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const actionChip = (a: string) => (/Повторили/.test(a) ? "g" : /Ждёт|вышел/.test(a) ? "a" : "n");
</script>

{#snippet betRow(b: Json)}
  <div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 80px 100px 130px">
    <div>
      <div class="t">{#if b.url}<a href={safeUrl(b.url)} target="_blank" rel="noopener" style="color:inherit;text-decoration:none">{b.question}</a>{:else}{b.question}{/if}</div>
      <div class="m"><span>«{b.side || ""}»</span><span>{b.category}</span><span>{b.when}</span></div>
    </div>
    <div class="n tnum">{b.entry_cents}¢</div>
    <div class="n tnum">{money(b.stake_usd)}</div>
    <div class="n">
      {#if b.pnl_usd === null}<span class="chip n">{b.state}</span>{:else}<b class="tnum {tone(b.pnl_usd)}">{money(b.pnl_usd, { sign: true })}</b><div class="sub">{b.state}</div>{/if}
    </div>
  </div>
{/snippet}

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if w}
  <a class="back" href="#/whales">← Все киты</a>
  <div class="page-head">
    <div class="who">
      {#if pm && pm.ok !== false && pm.image}<Avatar img={pm.image} code={pm.short} lg />{:else}<Seal code={w.name} lg />{/if}
      <div>
        <h1 id="whale-title">
          {pm && pm.ok !== false && pm.name ? pm.name : "Кит " + w.name}
          {#if pm && pm.verified}<span class="chip b sm" style="vertical-align:middle">проверен Polymarket</span>{/if}
        </h1>
        <div class="row" style="margin-top:10px;gap:8px">
          <span class="chip {w.status === 'Надёжный' ? 'g' : 'n'}">{w.status}</span><span class="chip n">Риск: {w.risk}</span>
          <span class="sub">{w.wallets.length > 1 ? `${w.wallets.length} кошелька одного владельца` : "Кошелёк " + shortAddr(w.wallets[0])}, активен с {since}</span>
        </div>
      </div>
    </div>
    {#if w.can_copy}
      <label class="ck" style="font-weight:600"><input type="checkbox" data-act="copy" checked={w.copy} onchange={(e) => setCopy(e.currentTarget.checked)} />Копировать ставки</label>
    {:else}
      <span class="sub">{w.note || "Копирование недоступно"}</span>
    {/if}
  </div>
  <section class="statement" aria-label="Результат кита">
    <div>
      <div class="label">Прибыль кита по закрытым ставкам</div>
      <div class="figure {tone(st.pnl_usd)}">{money(st.pnl_usd, { sign: true })}</div>
      <div class="sub" style="margin-top:10px">{st.roi_pct === null || st.roi_pct === undefined ? "" : signedPct(st.roi_pct) + " к вложенному, "}за 90 дней {pct(w.return_90d_pct)}</div>
    </div>
    <div class="facts">
      <div class="fact"><div class="label">Выигрышей</div><div class="v">{pctText(st.win_pct)}<small>из {st.closed ?? 0}</small></div></div>
      <div class="fact"><div class="label">Средняя ставка</div><div class="v">{money(st.avg_bet_usd)}<small>вход {st.avg_entry_cents ?? "—"}¢</small></div></div>
      <div class="fact">
        <div class="label">Держит ставку</div>
        <div class="v">{st.hold_days === null || st.hold_days === undefined ? "—" : String(st.hold_days).replace(".", ",") + " дн"}<small>{w.hold_text}</small></div>
      </div>
      <div class="fact"><div class="label">Открыто сейчас</div><div class="v">{st.open ?? 0}<small>на {money(st.open_usd)}</small></div></div>
      <div class="fact"><div class="label">Мы повторили</div><div class="v">{w.copied_count}<small class={tone(w.copied_pnl_usd)}>{money(w.copied_pnl_usd, { sign: true })}</small></div></div>
      <div class="fact">
        <div class="label">Последняя ставка</div>
        <div class="v" style="font-size:20px">{st.last_trade_ago_s === null || st.last_trade_ago_s === undefined ? "—" : ago(st.last_trade_ago_s)}</div>
      </div>
    </div>
  </section>

  {#if pmErr}
    <div class="card err">{pmErr}</div>
  {:else if !pm}
    <div class="card skeleton">Загружаем готовую статистику Polymarket…</div>
  {:else if pm.ok === false}
    <div class="banner gray"><div class="grow">Статистика Polymarket недоступна: {pm.error || ""}</div></div>
  {:else}
    <section class="card col" style="gap:18px" aria-label="Статистика Polymarket">
      <div class="top"><h2>По данным Polymarket</h2><a class="link" href={safeUrl(pm.profile_url)} target="_blank" rel="noopener">Профиль на Polymarket</a></div>
      <div class="kpis">
        <Kpi label="Прибыль за всё время" value={compactMoney(pm.pnl_all_usd, { sign: true })} sub={pm.rank_pnl ? `место ${fmtNum(pm.rank_pnl)} в рейтинге` : "вне рейтинга"} cls={tone(pm.pnl_all_usd)} />
        <Kpi label="За 30 дней" value={compactMoney(pm.pnl_30d_usd, { sign: true })} sub="90 дней {compactMoney(pm.pnl_90d_usd, { sign: true })}" cls={tone(pm.pnl_30d_usd)} />
        <Kpi label="Объём за 30 дней" value={compactMoney(pm.volume_30d_usd)} sub={pm.trades_30d ? `${fmtNum(pm.trades_30d)} сделок` : ""} />
        <Kpi label="В рынках сейчас" value={compactMoney(pm.value_usd)} sub={pm.markets ? `рынков всего ${fmtNum(pm.markets)}` : ""} />
        <Kpi label="Крупнейший выигрыш" value={compactMoney(pm.biggest_win_usd)} />
        <Kpi
          label="На Polymarket"
          value={monthsSince(pm.joined_ts)}
          sub={pm.joined_ts ? "с " + new Date(pm.joined_ts * 1000).toLocaleDateString("ru-RU", { month: "long", year: "numeric" }) : ""}
        />
      </div>
      <div class="pnl-box"><PnlChart series={pm.series} height={180} /></div>
      <Form form={pm.form} weeks={pm.weeks} />
      <div class="col" style="gap:8px"><h3>Открытые позиции сейчас</h3><PositionsList list={pm.positions} limit={10} /></div>
    </section>
  {/if}

  {#key w.entity_id}<SimCard id={w.entity_id} withPeriods />{/key}

  <details class="more card">
    <summary class="link">
      История ставок кита: {(w.whale_bets || []).length} последних закрытых{w.whale_open && w.whale_open.length ? ` и ${w.whale_open.length} открытых` : ""}
    </summary>
    <div class="col" style="gap:12px;margin-top:14px">
      {#if w.whale_open && w.whale_open.length}
        <h3>Открытые</h3>
        <div class="ledger">{#each w.whale_open as b, i (i)}{@render betRow(b)}{/each}</div>
      {/if}
      <h3>Закрытые</h3>
      <div class="ledger">
        {#each w.whale_bets || [] as b, i (i)}{@render betRow(b)}{:else}<div class="ledger-empty">Закрытые ставки появятся после загрузки истории.</div>{/each}
      </div>
    </div>
  </details>
  <div class="grid2">
    <div class="card">
      <fieldset>
        <legend>Темы</legend>
        {#each w.topics as t (t.category)}
          <label class="ck">
            <input type="checkbox" data-act="topic" checked={t.enabled} disabled={!t.proven} onchange={(e) => setTopic(t.category, e.currentTarget.checked)} />
            <span><b style={t.proven ? "" : "color:var(--muted)"}>{cap(t.label)}</b><span class="sub" style="display:block">{t.text}</span></span>
          </label>
        {/each}
      </fieldset>
    </div>
    <div class="card">
      <h2 style="margin-bottom:6px">Наши сигналы по киту</h2>
      {#each w.last_bets as b, i (i)}
        <div class="bet">
          <div><b style="font-weight:500">{b.question}</b><div class="sub">«{b.side}» по {b.price_cents}¢, {b.when}</div></div>
          <span class="chip {actionChip(b.our_action)}">{b.our_action}</span>
        </div>
      {:else}
        <div class="sub" style="margin-top:8px">Сигналов ещё не было</div>
      {/each}
    </div>
  </div>
  {#if w.links && w.links.length}
    <div class="card col" style="gap:8px">
      <h2>Почему это один игрок</h2>
      <div class="sub">Система объединила {w.wallets.length} кошелька: так один человек не считается несколькими «умными» игроками.</div>
      {#each [...new Set(w.links.map((l: Json) => l.why))] as t (t)}<div class="row" style="gap:8px"><span class="chip n">связь</span>{t}</div>{/each}
    </div>
  {/if}
  <button
    class="link"
    style="color:var(--bad-ink);background:none;border:0;text-align:left;padding:0;min-height:44px;cursor:pointer;align-self:flex-start"
    data-act="unfollow"
    onclick={unfollow}>Перестать следить за китом</button
  >
{/if}
