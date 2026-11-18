<script lang="ts">
  // «Точность модели событий»: итог модели против цены рынка на закрытых событиях — честно, на одних и тех же
  // анализах; калибровка, темы, оценщики, выученные λ и веса, результат ставок по совету.
  import { type Json } from "../lib/api";
  import { pctOf, TOPIC_LABEL } from "../lib/events";
  import { money, plural, tone } from "../lib/format";
  import { loader } from "../lib/load.svelte";

  const d = loader(() => "/accuracy/events");
  const chip = (code: string) => (code === "better" ? "g" : code === "worse" ? "r" : "n");
  const num = (x: number | null | undefined, n = 4) => (x === null || x === undefined ? "—" : x.toFixed(n).replace(".", ","));
  const pctDiff = (a: number, b: number) => (b ? Math.round(((b - a) / b) * 1000) / 10 : 0);
  const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "ещё не было");
</script>

<a class="back" href="#/events">← События</a>
{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if !d.data}
  <div class="card skeleton" style="min-height:300px">Загружаем…</div>
{:else}
  {@const r = d.data}
  {@const modes = Object.entries(r.modes) as [string, Json][]}
  <div class="page-head">
    <div>
      <h1>Точность модели событий</h1>
      <div class="lede">
        Каждый анализ сохраняется с ценой рынка на тот момент. Когда рынок закрывается, мы сравниваем: чья вероятность
        была ближе к исходу — итога модели или цены рынка (Brier: чем меньше, тем точнее). Только анализы, сделанные до
        закрытия; оценки ИИ — только вперёд.
      </div>
    </div>
  </div>

  {#if !modes.length}
    <div class="card empty" data-acc-empty>
      <b>Пока нечего проверять</b>
      <div class="sub">
        Сохранено анализов по {r.analyses_outcomes} {plural(r.analyses_outcomes, "исходу", "исходам", "исходам")}; ждём закрытия рынков ({r.pending_markets}).
        Точность появится, когда закроется хотя бы одно событие из тех, что вы открывали или за которыми следите.
      </div>
    </div>
  {/if}

  {#each modes as [mode, m] (mode)}
    <section class="card col" style="gap:10px" data-acc-mode={mode}>
      <div class="row" style="justify-content:space-between;gap:10px;flex-wrap:wrap">
        <h2 style="margin:0">{m.title}</h2>
        <span class="chip {chip(m.verdict.code)}" data-verdict={m.verdict.code}>{m.verdict.label}</span>
      </div>
      <div class="sub">{m.verdict.text}.</div>
      <div class="row" style="gap:28px;flex-wrap:wrap">
        <div class="fact"><div class="label">Закрытых исходов</div><div class="v">{m.n}</div><div class="sub">{m.events} {plural(m.events, "событие", "события", "событий")}</div></div>
        <div class="fact"><div class="label">Brier модели</div><div class="v">{num(m.brier)}</div></div>
        <div class="fact"><div class="label">Brier рынка</div><div class="v">{num(m.brier_market)}</div></div>
        <div class="fact">
          <div class="label">{m.diff <= 0 ? "Модель точнее на" : "Модель менее точна на"}</div>
          <div class="v {m.diff < 0 ? 'up' : m.diff > 0 ? 'down' : ''}">{m.brier_market ? String(Math.abs(pctDiff(m.brier, m.brier_market))).replace(".", ",") + "%" : "—"}</div>
          <div class="sub">случайный разброс ±{m.se !== null && m.brier_market ? String(Math.round(((2 * m.se) / m.brier_market) * 1000) / 10).replace(".", ",") + "%" : "—"}</div>
        </div>
      </div>
      {#if m.with_info?.n && m.with_info.n < m.n}
        <div class="sub" data-with-info>Только где были независимые оценки ({m.with_info.n} {plural(m.with_info.n, "исход", "исхода", "исходов")}): Brier модели {num(m.with_info.brier)} против {num(m.with_info.brier_market)} у рынка — {m.with_info.verdict.label.toLowerCase()}. В остальных итог совпадал с ценой.</div>
      {/if}
      {#if m.model_only?.n}
        <div class="sub">Сводная оценка без сжатия к цене: Brier {num(m.model_only.brier)} против {num(m.model_only.brier_market)} у рынка — поэтому итог и держится близко к цене.</div>
      {/if}
      {#if m.calibration.length}
        <div class="label" style="margin-top:6px">Калибровка: когда модель говорит «X%», как часто это сбывается</div>
        <div class="ledger">
          <div class="ledger-head acc-row"><span>Оценка модели</span><span class="n">Исходов</span><span class="n">В среднем говорила</span><span class="n">Сбылось</span></div>
          {#each m.calibration as b (b.range)}
            <div class="ledger-row acc-row">
              <span>{b.range}</span><span class="n tnum">{b.n}</span><span class="n tnum">{pctOf(b.predicted)}</span><span class="n tnum">{pctOf(b.actual)}</span>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/each}

  <section class="card col" style="gap:8px" data-acc-params>
    <h2 style="margin:0">Чему модель научилась</h2>
    <div class="sub">
      λ — насколько итог может отойти от цены рынка в сторону независимых оценок. Подбирается раз в час только по закрытым
      событиям и сжимается к исходным 30%, пока данных мало. Выше 30% λ и веса поднимаются, только когда модель
      доказанно точнее рынка{r.params.proven ? " — сейчас это так" : " — пока нет, поэтому не выше 30%"}; снижаются всегда. Последний подбор: {when(r.params.fitted_ts)}.
    </div>
    <div class="row" style="gap:28px;flex-wrap:wrap">
      <div class="fact"><div class="label">λ сейчас</div><div class="v">{Math.round(r.params.lambda * 100)}%</div><div class="sub">по {r.params.n} закрытым исходам</div></div>
      {#if r.params.lambda_raw !== null && r.params.lambda_raw !== undefined && r.params.n >= r.min_outcomes}
        <div class="fact"><div class="label">Лучшее по истории</div><div class="v">{Math.round(r.params.lambda_raw * 100)}%</div><div class="sub">до сжатия</div></div>
      {/if}
      {#each Object.entries(r.params.by_topic) as [t, v] (t)}
        <div class="fact"><div class="label">λ: {TOPIC_LABEL[t] || t}</div><div class="v">{Math.round((v as Json).lambda * 100)}%</div><div class="sub">{(v as Json).n} исходов</div></div>
      {/each}
    </div>
    {#if r.estimators.length}
      <div class="label" style="margin-top:6px">Оценщики: помогала ли их поправка цене</div>
      <div class="ledger">
        <div class="ledger-head acc-est"><span>Оценщик</span><span class="n">Исходов</span><span class="n">Цена + 30% поправки</span><span class="n">Только цена</span><span class="n">Вес ×</span></div>
        {#each r.estimators as e (e.name)}
          <div class="ledger-row acc-est" data-est={e.name}>
            <span>{e.title}</span><span class="n tnum">{e.n}</span>
            <span class="n tnum {e.diff < 0 ? 'up' : e.diff > 0 ? 'down' : ''}">{num(e.brier)}</span>
            <span class="n tnum">{num(e.brier_market)}</span>
            <span class="n tnum">{String(e.mult).replace(".", ",")}</span>
          </div>
        {/each}
      </div>
      <div class="sub">Вес оценщика умножается на 0,2–2: если его поправка в прошлом улучшала цену — больше, ухудшала — меньше; пока исходов меньше 20 — без изменений.</div>
    {/if}
  </section>

  {#if r.topics.length}
    <section class="card col" style="gap:8px">
      <h2 style="margin:0">По темам</h2>
      <div class="ledger">
        <div class="ledger-head acc-row"><span>Тема</span><span class="n">Исходов</span><span class="n">Модель / рынок</span><span class="n">Вывод</span></div>
        {#each r.topics as t (t.topic)}
          <div class="ledger-row acc-row">
            <span>{TOPIC_LABEL[t.topic] || t.topic}</span><span class="n tnum">{t.n}</span>
            <span class="n tnum">{num(t.brier)} / {num(t.brier_market)}</span>
            <span class="n"><span class="chip {chip(t.verdict.code)} sm">{t.verdict.label}</span></span>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  <section class="card col" style="gap:8px">
    <h2 style="margin:0">Ставки по событиям за всё время</h2>
    {#each r.bets as g (g.kind)}
      <div class="top">
        <span><b>{g.title}</b> · {g.bets} {plural(g.bets, "ставка", "ставки", "ставок")} на {money(g.stake_usd)} · выиграно {g.won}, проиграно {g.lost}, ждут {g.waiting}</span>
        <b class={tone(g.pnl_usd)}>{money(g.pnl_usd, { sign: true })}</b>
      </div>
    {:else}
      <div class="sub">Ставок по событиям ещё не было.</div>
    {/each}
  </section>
{/if}

<style>
  .acc-row{display:grid;grid-template-columns:minmax(0,1fr) 90px 160px 120px;gap:12px;align-items:center}
  .acc-est{display:grid;grid-template-columns:minmax(0,1fr) 80px 150px 110px 70px;gap:12px;align-items:center}
  @media (max-width:700px){
    .acc-row{grid-template-columns:minmax(0,1fr) 60px 110px 80px}
    .acc-est{grid-template-columns:minmax(0,1fr) 50px 80px 80px 50px;font-size:13px}
  }
</style>
