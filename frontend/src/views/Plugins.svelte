<script lang="ts">
  // Экран «Плагины»: источники, уведомления, стратегии — включить/выключить, состояние, ошибки, бэктест стратегий
  import Plot from "../components/Plot.svelte";
  import { api, type Json } from "../lib/api";
  import { ago, money, pctText, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { softRefresh, toast } from "../lib/state.svelte";

  const d = loader(() => "/plugins");
  let bt = $state<Record<string, Json>>({});
  let btDays = $state<Record<string, number>>({});
  let busy = $state<Record<string, boolean>>({});

  const STATE: Record<string, [string, string]> = {
    ok: ["g", "работает"],
    idle: ["n", "включён · ещё не вызывался"],
    error: ["r", "ошибка"],
    unavailable: ["a", "недоступен"],
    off: ["n", "выключен"],
  };

  async function toggle(p: Json, on: boolean) {
    try {
      const r = await api("/plugins/" + encodeURIComponent(p.name), { method: "PATCH", body: { enabled: on } });
      toast(r.ok ? r.warning || (on ? `«${p.title}» включён` : `«${p.title}» выключен`) : r.error);
    } catch (e) {
      toast((e as Error).message);
    }
    softRefresh();
  }
  async function backtest(p: Json, days = 90) {
    busy[p.name] = true;
    btDays[p.name] = days;
    try {
      bt[p.name] = await api(`/plugins/${encodeURIComponent(p.name)}/backtest?days=${days}`);
    } catch (e) {
      bt[p.name] = { ok: false, error: (e as Error).message };
    } finally {
      busy[p.name] = false;
    }
  }
  const SKIP: Record<string, string> = {
    strategy: "стратегия не взяла",
    price_moved: "цена успела уйти",
    topic_off: "тема выключена у вас",
    day_limit: "дневной лимит",
    no_upside: "цена почти 100¢",
  };
</script>

{#snippet result(name: string, r: Json)}
  {#if !r.ok}
    <div class="banner warn"><div class="grow">{r.error}</div></div>
  {:else}
    {@const s = r.summary}
    <div class="col" style="gap:12px;margin-top:4px">
      <div class="row" style="gap:28px;align-items:flex-end">
        <div>
          <div class="label">Итог закрытых ставок</div>
          <div class="big {tone(s.closed_pnl_usd)}" style="font-size:30px">{money(s.closed_pnl_usd ?? 0, { sign: true })}</div>
          <div class="sub">{s.roi_pct === null || s.roi_pct === undefined ? "" : signedPct(s.roi_pct) + " к вложенному"}</div>
        </div>
        <div class="fact"><div class="label">Повторили бы</div><div class="v">{s.n}<small>закрыто {s.closed}</small></div></div>
        <div class="fact"><div class="label">Выигрышей</div><div class="v">{pctText(s.win_pct)}</div></div>
        <div class="fact"><div class="label">Наибольшая просадка</div><div class="v">{money(-(s.max_drawdown_usd || 0))}</div></div>
        <div class="fact"><div class="label">Китов</div><div class="v">{s.whales}</div></div>
      </div>
      {#if (s.curve || []).length > 1}
        {#key r}
          <Plot height={150} xs={s.curve.map((c: Json) => c.ts)} lines={[{ label: "Прибыль", values: s.curve.map((c: Json) => c.pnl_usd), color: "#C8A464", fill: true }]}
            fmtY={(v) => money(v)} label="Бэктест стратегии: прибыль закрытых ставок" />
        {/key}
      {/if}
      <div style="font-size:14px">{s.text}</div>
      {#if Object.values(s.skipped || {}).some((v) => v)}
        <div class="sub">
          Не повторили бы: {Object.entries(s.skipped || {}).filter(([, v]) => v).map(([k, v]) => `${SKIP[k] || k} — ${v}`).join("; ")}.
        </div>
      {/if}
      <div class="sub">
        Последние {s.days} дней, ваши киты на копировании; задержка {s.delay_s} с, ставка {money(s.stake_usd)}, проскальзывание 1¢ и комиссии учтены. Стратегия
        видит только то, что было известно на момент покупки кита.
      </div>
      {#if r.errors}<div class="sub" style="color:var(--warn-ink)">Ошибки стратегии в бэктесте: {r.errors.join("; ")}</div>{/if}
    </div>
  {/if}
{/snippet}

<a class="back" href="#/settings">← Настройки</a>
<div class="page-head">
  <div>
    <h1>Плагины</h1>
    <div class="lede">
      Источники данных, каналы уведомлений и стратегии подключаются как плагины. Здесь их можно включать и выключать, видеть состояние и ошибки, а для стратегий —
      проверить на истории ваших китов.
    </div>
  </div>
</div>
{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  {@const x = d.data}
  {#if x.load_errors.length}
    <div class="banner warn">
      <div class="grow">
        <b>Не все плагины загрузились</b>
        {#each x.load_errors as e, i (i)}<div><span class="sub">{e.origin}:</span> {e.error}</div>{/each}
      </div>
    </div>
  {/if}
  {#each x.kinds as k (k.kind)}
    {@const items = x.items.filter((p: Json) => p.kind === k.kind)}
    <section class="col" style="gap:10px" aria-label={k.title}>
      <h2>{k.title}</h2>
      {#if k.kind === "strategy"}
        <div class="sub">Включённые стратегии работают как «или»: сигнал появляется, если его берёт хотя бы одна. Дальше — обычные проверки цены, стакана, лимитов и ИИ.</div>
      {:else if k.kind === "notifier"}
        <div class="sub">О чём присылать — флажки «О чём» в <a class="link" href="#/settings">настройках</a>; они общие для всех каналов.</div>
      {/if}
      <div class="ledger">
        {#each items as p (p.name)}
          {@const st = STATE[p.state] || ["n", p.state]}
          <div class="plugin-row" data-plugin={p.name}>
            <div class="grow">
              <div class="row" style="gap:8px">
                <b>{p.title}</b>
                <span class="chip {st[0]} sm">{st[1]}</span>
                {#if p.origin !== "встроенный"}<span class="chip i sm">{p.origin}</span>{/if}
              </div>
              <div class="sub" style="margin-top:4px">{p.description}</div>
              {#if p.unavailable && p.enabled}<div class="sub" style="color:var(--warn-ink);margin-top:4px">{p.unavailable}</div>{/if}
              {#if p.error && p.state === "error"}<div class="sub down" style="margin-top:4px">Ошибка {p.err_ago_s !== null ? ago(p.err_ago_s) : ""}: {p.error}</div>{/if}
              {#if p.ok_ago_s !== null || p.calls}
                <div class="sub" style="margin-top:4px">
                  {p.ok_ago_s !== null ? `Работал ${ago(p.ok_ago_s)}` : ""}{p.note ? ` · ${p.note}` : ""}{p.calls ? ` · вызовов ${p.calls}` : ""}{p.errors ? `, ошибок ${p.errors}` : ""}
                </div>
              {/if}
              {#if p.has_backtest}
                <div class="row" style="gap:8px;margin-top:10px">
                  <button class="btn" data-act="backtest" disabled={busy[p.name]} onclick={() => backtest(p, btDays[p.name] || 90)}>
                    {busy[p.name] ? "Считаем…" : bt[p.name] ? "Пересчитать" : "Бэктест"}
                  </button>
                  {#if bt[p.name]}
                    <div class="seg" role="group" aria-label="Период бэктеста">
                      {#each [30, 90, 180] as dd (dd)}<button aria-pressed={(btDays[p.name] || 90) === dd} onclick={() => backtest(p, dd)}>{dd} дн</button>{/each}
                    </div>
                  {/if}
                </div>
                {#if bt[p.name]}{@render result(p.name, bt[p.name])}{/if}
              {/if}
            </div>
            <label class="ck plugin-toggle">
              <input type="checkbox" data-act="plugin-toggle" checked={p.enabled} onchange={(e) => toggle(p, e.currentTarget.checked)} aria-label="Включить «{p.title}»" />
              {p.enabled ? "Вкл" : "Выкл"}
            </label>
          </div>
        {/each}
      </div>
    </section>
  {/each}
  <details class="card">
    <summary class="link" style="cursor:pointer">Как добавить свой плагин</summary>
    <div class="col sub" style="gap:6px;margin-top:10px">
      <div>Положите файл <code>.py</code> с плагином в папку плагинов и перезапустите приложение — правка программы не нужна.</div>
      <div>Папка: <code>{x.dir || x.dir_hint}</code>.</div>
      <div>Пример и описание интерфейса — в README, раздел «Плагины», и в файле <code>examples/plugins/example_plugins.py</code>.</div>
      <div style="color:var(--warn-ink)">Плагин — это программа, которая работает с правами приложения. Ставьте только плагины, которым доверяете.</div>
    </div>
  </details>
{/if}
