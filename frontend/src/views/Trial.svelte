<script lang="ts">
  // Пробный режим: условия перехода, сигналы, калибровка, подстройка порога, скорость, ИИ, сбор данных
  import Plot from "../components/Plot.svelte";
  import { api, type Json } from "../lib/api";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { ago, money, signedPct, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { softRefresh, toast } from "../lib/state.svelte";

  const d = loader(() => "/trial");
  const VCLASS: Record<string, string> = { ready: "info", running: "gray", problem: "bad" };
  const GRID = "grid-template-columns:1fr 90px 100px 100px 110px 120px";
  const TGRID = "grid-template-columns:1fr 90px 110px 140px";

  async function tune(v: number) {
    const ok = await confirmDialog({
      title: `Порог выгоды ${Math.round(v * 100)}%?`,
      text: "Система будет покупать только сигналы с запасом выгоды не меньше этого порога. Решение принято по исходам пробного режима; вернуть можно так же.",
      ok: "Применить",
    });
    if (!ok) return;
    try {
      const r = await api("/trial/tune", { method: "POST", body: { edge_threshold: v } });
      toast(r.ok ? "Порог изменён" : r.error);
      softRefresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }
  const jobDot = (j: Json) => (j.ok === false ? "bad" : j.ok ? "ok" : "");
</script>

{#snippet chip(ok: boolean | null)}
  {#if ok === true}<span class="chip g">Выполнено</span>{:else if ok === false}<span class="chip r">Не выполнено</span>{:else}<span class="chip n">Идёт</span>{/if}
{/snippet}

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  {@const t = d.data}
  {@const v = t.verdict}
  {@const s = t.signals}
  <a class="back" href="#/results">← Итоги</a>
  <div>
    <h1>Пробный режим</h1>
    <div class="sub" style="margin-top:6px">
      День {Math.floor(t.days) + 1} · начат {new Date(t.started_ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" })} · все ставки на виртуальные деньги
    </div>
  </div>
  <div class="banner {VCLASS[v.status]}"><div class="grow"><b>{v.label}</b><div>{v.text}</div></div></div>
  <h2>Условия перехода к следующему этапу</h2>
  <div class="grid2">
    {#each t.criteria as c, i (i)}
      <div class="card col" style="gap:8px">
        <div class="top"><b>{c.title}</b>{@render chip(c.ok)}</div>
        <div class="sub" style="font-size:14px">{c.value}</div>
        {#if c.progress !== null && c.progress !== undefined}
          <div class="bar" role="progressbar" aria-label={c.title} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(c.progress * 100)}>
            <span style="width:{Math.round(c.progress * 100)}%"></span>
          </div>
        {/if}
      </div>
    {/each}
  </div>
  <h2>Сигналы</h2>
  <div class="grid3">
    <div class="card">
      <div class="sub">Всего сигналов</div><div class="big">{s.total}</div>
      <div style="font-size:14px;margin-top:4px">Рекомендаций «купить»: {s.buy_recommendations}, с известным исходом: {s.with_outcome}</div>
    </div>
    <div class="card"><div class="sub">Куплено</div><div class="big">{s.executed}</div><div style="font-size:14px;margin-top:4px">Ждут решения: {s.pending}</div></div>
    <div class="card">
      <div class="sub">Пропущено</div><div class="big">{s.skipped_by_system + s.skipped_by_you + s.expired}</div>
      <div style="font-size:14px;margin-top:4px">Системой: {s.skipped_by_system} · вами: {s.skipped_by_you} · время вышло: {s.expired}</div>
    </div>
  </div>
  {#if s.expired >= 5 && s.expired > s.executed}
    <div class="banner warn">
      <div class="grow">
        <b>Много сигналов сгорело по времени</b>
        <div>Подключите уведомления в Telegram или отключите ручное подтверждение в настройках — в пробном режиме это безопасно.</div>
      </div>
      <a class="btn" href="#/settings">Настройки</a>
    </div>
  {/if}
  <div class="card col" style="gap:10px">
    <div class="top">
      <h2 style="margin:0">Точность прогнозов</h2>
      <span class="sub">
        {t.calibration.better_than_market === null
          ? "сравнение с рынком появится после первых исходов"
          : t.calibration.better_than_market
            ? "прогнозы системы точнее рыночной цены"
            : "прогнозы системы пока не точнее рыночной цены"}
      </span>
    </div>
    <div class="sub">Когда система говорит «70%», такие события должны сбываться примерно в 70% случаев. Оценка — в корзинах, где не меньше 30 исходов.</div>
    {#if t.calibration.buckets.length}
      <div class="table-head" style="{GRID};padding:0"><span>Прогноз системы</span><span>Сигналов</span><span>В среднем</span><span>Сбылось</span><span>Отклонение</span><span></span></div>
      {#each t.calibration.buckets as b, i (i)}
        <div class="trow" style="{GRID};padding:6px 0;border-top:1px solid var(--line)">
          <span>{b.range}</span><span>{b.n}</span><span>{b.predicted_pct}%</span><span>{b.actual_pct}%</span>
          <span>{b.deviation_pp > 0 ? "+" : ""}{b.deviation_pp} п.п.</span>
          <span>{#if b.ok === null}<span class="chip n">Мало данных</span>{:else if b.ok}<span class="chip g">В допуске</span>{:else}<span class="chip r">Вне допуска</span>{/if}</span>
        </div>
      {/each}
    {:else}
      <div class="sub">Исходов пока нет — рынки ещё не закрылись.</div>
    {/if}
    {#if t.hypothetical_roi_pct !== null}
      <div style="font-size:14px">Если бы вы подтверждали все рекомендации, доходность была бы <b class={tone(t.hypothetical_roi_pct)}>{signedPct(t.hypothetical_roi_pct)}</b> на ставку.</div>
    {/if}
  </div>
  {#if t.tuning}
    {@const tu = t.tuning}
    {@const rows = tu.grid.filter((g: Json) => g.n > 0)}
    <div class="card col" style="gap:10px">
      <div class="top"><h2 style="margin:0">Подстройка порога выгоды</h2><span class="sub">сейчас {Math.round(tu.current * 100)}%</span></div>
      <div class="sub">Что было бы при другом пороге: по всем сигналам с известным исходом, включая пропущенные. Учитывается комиссия.</div>
      {#if rows.length}
        <div class="table-head" style="{TGRID};padding:0"><span>Порог выгоды</span><span>Сигналов</span><span>Выигрышей</span><span>Доход на ставку</span></div>
        {#each rows as g, i (i)}
          <div class="trow" style="{TGRID};padding:6px 0;border-top:1px solid var(--line)">
            <span>{Math.round(g.threshold * 100)}%{#if g.current}{" "}<span class="chip n">сейчас</span>{/if}{#if tu.recommended === g.threshold && !g.current}{" "}<span class="chip g">лучше</span>{/if}</span>
            <span>{g.n}</span><span>{g.win_pct ?? "—"}%</span><b class={tone(g.avg_return_pct)}>{signedPct(g.avg_return_pct)}</b>
          </div>
        {/each}
      {/if}
      <div class="row">
        <span style="font-size:14px">{tu.text}</span>
        {#if tu.recommended !== null && Math.abs(tu.recommended - tu.current) > 1e-9}
          <button class="btn" data-act="tune" onclick={() => tune(tu.recommended)}>Применить {Math.round(tu.recommended * 100)}%</button>
        {/if}
      </div>
    </div>
  {/if}
  <div class="card col" style="gap:8px">
    <div class="top"><h2 style="margin:0">Скорость сигналов</h2><span class="sub">от сделки кита до сигнала, за 7 дней</span></div>
    {#if t.latency && t.latency.n}
      <div class="row" style="gap:28px">
        <div><div class="sub">Обычно</div><div class="big">{Math.round(t.latency.median_s)} с</div></div>
        <div><div class="sub">9 из 10 сигналов быстрее</div><div class="big">{Math.round(t.latency.p90_s)} с</div></div>
        <div><div class="sub">Сигналов</div><div class="big">{t.latency.n}</div></div>
      </div>
      <div class="sub">Чем быстрее сигнал, тем ближе наша цена к цене кита.</div>
    {:else}
      <div class="sub">Появится после первых сигналов.</div>
    {/if}
  </div>
  <div class="card col" style="gap:8px">
    <div class="top">
      <h2 style="margin:0">ИИ</h2>
      <span class="chip {t.ai.mode.effective === 'ai' ? 'i' : 'n'}">{t.ai.mode.effective === "ai" ? "Работает" : t.ai.mode.available ? "Выключен вами" : "Не подключён"}</span>
    </div>
    <div style="font-size:14px">Оценок сделано: {t.ai.estimates} · с известным исходом: {t.ai.with_outcome} из {t.ai.needed} нужных для проверки</div>
    <div class="bar" role="progressbar" aria-label="Исходы для проверки ИИ" aria-valuemin={0} aria-valuemax={t.ai.needed} aria-valuenow={t.ai.with_outcome}>
      <span style="width:{Math.min(100, (t.ai.with_outcome / t.ai.needed) * 100)}%"></span>
    </div>
    <div class="sub">
      {t.ai.better_than_market === null
        ? "Сравнение с рынком появится после первых исходов."
        : t.ai.better_than_market
          ? "Оценки ИИ пока точнее рыночной цены."
          : "Оценки ИИ пока не точнее рыночной цены — он работает только как фильтр."}
      {t.ai.trusted ? " ИИ прошёл проверку и получает полный вес в решении." : ""}
    </div>
  </div>
  <div class="card">
    <div class="top"><h2 style="margin:0">Баланс</h2><span class="sub">Наибольшая просадка: {t.max_drawdown_pct}%</span></div>
    {#if t.series.length > 1}
      {#key t}
        <Plot xs={t.series.map((p: Json) => p.ts)} lines={[{ label: "Баланс", values: t.series.map((p: Json) => p.balance_usd), color: "#C8A464", fill: true }]}
          height={150} fmtY={(x) => money(x)} label="Баланс в пробном режиме" />
      {/key}
    {:else}
      <div class="sub" style="margin-top:8px">Недостаточно данных для графика</div>
    {/if}
  </div>
  <div class="card col" style="gap:8px">
    <h2 style="margin:0">Сбор данных</h2>
    {#each t.jobs as j, i (i)}
      <div class="top" style="font-size:14px">
        <span class="row" style="gap:8px"><span class="dot {jobDot(j)}" aria-hidden="true"></span>{j.label}</span>
        <span class="sub">{#if j.ok === false}<span class="down">ошибка: {j.error}</span>{:else}{j.ago_s === null ? "ещё не запускалось" : "был запуск " + ago(j.ago_s)}{/if}</span>
      </div>
    {:else}
      <div class="sub">Планировщик не запущен. Для пробного режима запустите <code>whalefollow run</code> или <code>docker compose up -d</code>.</div>
    {/each}
  </div>
{/if}
