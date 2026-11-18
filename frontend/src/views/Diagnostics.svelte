<script lang="ts">
  // Диагностика: проверки источников, процессы, потоки, данные, задачи, текст отчёта
  import { api, type Json } from "../lib/api";
  import { ago, fmtNum } from "../lib/format";
  import { loader } from "../lib/load.svelte";
  import { toast } from "../lib/state.svelte";

  const d = loader(() => "/diagnostics");
  let running = $state(false);
  let reportEl: HTMLPreElement | undefined = $state();

  async function runLive() {
    running = true;
    try {
      const x = await api("/diagnostics?live=1");
      d.data = x;
      toast(x.probe && x.probe.ok ? "Все источники работают" : "Есть проблемы — см. список");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      running = false;
    }
  }
  async function copyReport() {
    const txt = d.data?.report || "";
    try {
      await navigator.clipboard.writeText(txt);
      toast("Отчёт скопирован");
    } catch {
      if (reportEl) {
        const r = document.createRange();
        r.selectNodeContents(reportEl);
        getSelection()?.removeAllRanges();
        getSelection()?.addRange(r);
      }
      toast("Выделено — скопируйте сочетанием клавиш");
    }
  }
  const GROUPS: [string, string][] = [
    ["polymarket", "Polymarket"],
    ["stream", "Потоки реального времени"],
    ["kalshi", "Другие рынки"],
    ["news", "Новости и прогнозы"],
    ["telegram", "Уведомления"],
    ["chain", "Блокчейн"],
    ["odds", "Букмекеры"],
  ];
  const SPEED: Record<string, string> = { мгновенно: "g", быстро: "g", нормально: "n", медленно: "a" };
  const upFor = (sec: number) => (sec < 3600 ? `${Math.max(1, Math.round(sec / 60))} мин` : sec < 172800 ? `${Math.round(sec / 3600)} ч` : `${Math.round(sec / 86400)} дн`);
</script>

{#snippet streamLine(x: Json, name: string)}
  <div class="bet">
    <span class="row" style="gap:8px"><span class="dot {!x.started ? '' : x.live ? 'ok' : 'bad'}" aria-hidden="true"></span>{name}</span>
    <span class="sub">{!x.started ? "не запущен" : x.live ? "работает" : "нет связи"}{x.connect_ms && x.live ? `, подключение ${x.connect_ms} мс` : ""}</span>
  </div>
{/snippet}

<a class="back" href="#/settings">← Настройки</a>
<div class="page-head">
  <div>
    <h1>Диагностика</h1>
    <div class="lede">Проверка доступа ко всем источникам данных прямо из интерфейса. Если что-то не работает, скопируйте отчёт и пришлите его — в нём нет ключей и адресов кошельков.</div>
  </div>
  <div class="row">
    <button class="btn" data-act="diag-copy" onclick={copyReport} disabled={!d.data}>Скопировать отчёт</button>
    <button class="btn p" data-act="diag-run" onclick={runLive} disabled={running}>{running ? "Проверяем…" : "Проверить сейчас"}</button>
  </div>
</div>
{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  {@const x = d.data}
  {@const p = x.probe}
  {@const okN = p ? p.checks.filter((c: Json) => c.ok).length : 0}
  {@const u = x.usage}
  {@const cnt = x.counts}
  {@const st = x.streams}
  {@const svcs = (x.services || []) as Json[]}
  <section class="statement" aria-label="Сводка">
    <div>
      <div class="label">Источники</div>
      <div class="figure {p ? (p.ok ? 'up' : p.checks.some((c: Json) => !c.ok && c.group === 'polymarket') ? 'down' : 'warn-t') : ''}">{p ? `${okN} из ${p.checks.length}` : "—"}</div>
      <div class="sub" style="margin-top:10px">{p ? `работают, проверка ${ago(Math.max(0, Math.round(Date.now() / 1000 - p.ts)))}` : "ещё не проверялись"}</div>
    </div>
    <div class="facts">
      <div class="fact">
        <div class="label">Отклик Polymarket</div>
        <div class="v" style="font-size:20px">{p && p.polymarket_median_ms !== null && p.polymarket_median_ms !== undefined ? `${p.polymarket_median_ms} мс` : "—"}</div>
      </div>
      <div class="fact"><div class="label">Агент</div><div class="v" style="font-size:20px">{x.agent_ok ? "работает" : "не отвечает"}</div></div>
      <div class="fact"><div class="label">Свежесть данных</div><div class="v" style="font-size:20px">{x.data_age_s === null || x.data_age_s === undefined ? "нет данных" : ago(x.data_age_s)}</div></div>
      <div class="fact"><div class="label">Версия</div><div class="v" style="font-size:20px">{x.version}</div></div>
    </div>
  </section>
  <div class="ledger" id="diag-checks">
    {#if p}
      {#each GROUPS as [g, title] (g)}
        {@const items = p.checks.filter((c: Json) => c.group === g)}
        {#if items.length}
          <div class="ledger-head" style="grid-template-columns:1fr">{title}</div>
          {#each items as c, i (i)}
            <div class="ledger-row" style="grid-template-columns:minmax(0,1fr) 150px 96px">
              <div>
                <div class="t">{c.label}</div>
                <div class="m"><span>{c.detail}</span></div>
                {#if c.hint}<div class="sub" style="margin-top:4px;color:var(--warn-ink)">{c.hint}</div>{/if}
              </div>
              <div class="n">
                <b class="tnum">{c.ms} мс</b>
                {#if c.warm_ms !== null && c.warm_ms !== undefined && c.cold_ms}
                  <div class="sub" title="Первый запрос с установкой защищённого соединения. Приложение держит соединения открытыми, поэтому рабочие запросы идут быстрее.">с нуля {c.cold_ms} мс</div>
                {/if}
              </div>
              <div class="n"><span class="chip {c.ok ? SPEED[c.speed] || 'g' : 'r'}">{c.ok ? c.speed || "работает" : "ошибка"}</span></div>
            </div>
          {/each}
        {/if}
      {/each}
    {:else}
      <div class="ledger-empty">Проверка ещё не запускалась — нажмите «Проверить сейчас».</div>
    {/if}
  </div>
  {#if svcs.length}
    <div class="card col" style="gap:6px" id="diag-services">
      <h2>Процессы</h2>
      {#each svcs as s (s.name)}
        <div class="bet">
          <span class="row" style="gap:8px"><span class="dot {s.alive ? 'ok' : 'bad'}" aria-hidden="true"></span>{s.label}</span>
          <span class="sub">
            {#if s.alive}работает {upFor(s.uptime_s)}, пульс {ago(Math.round(s.last_seen_s))}{:else}<span class="down">остановлен — последний пульс {ago(Math.round(s.last_seen_s))}</span>{/if}
          </span>
        </div>
      {/each}
      {#if svcs.some((s) => !s.alive && s.name === "worker")}
        <div class="sub" style="color:var(--warn-ink)">Фоновые задачи стоят: сигналы приходят, но рынки, цены и пересчёт китов не обновляются. Перезапустите: docker compose up -d</div>
      {/if}
      {#if svcs.some((s) => !s.alive && s.name === "realtime")}
        <div class="sub" style="color:var(--warn-ink)">Сигналы не приходят: процесс realtime остановлен. Перезапустите: docker compose up -d</div>
      {/if}
    </div>
  {/if}
  {#if st}
    <div class="grid2">
      <div class="card col" style="gap:6px">
        <h2>Реальное время</h2>
        {@render streamLine(st.trades, "Поток сделок китов")}
        <div class="bet"><span>Китов в потоке</span><b class="tnum">{fmtNum(st.trades.wallets)}</b></div>
        <div class="bet"><span>Сделок китов пришло потоком</span><b class="tnum">{fmtNum(st.trades.matched)}</b></div>
        {#if st.trades.lag_s !== null && st.trades.lag_s !== undefined}
          <div class="bet"><span>Задержка последней сделки</span><b class="tnum">{String(Math.max(0, st.trades.lag_s)).replace(".", ",")} с</b></div>
        {/if}
        {@render streamLine(st.books, "Поток стаканов")}
        <div class="bet"><span>Рынков в потоке</span><b class="tnum">{fmtNum(st.books.subscribed)}</b></div>
        <div class="bet"><span>Стаканов для решений: из потока / запросом</span><b class="tnum">{fmtNum(st.agent_books.from_stream)} / {fmtNum(st.agent_books.by_request)}</b></div>
        {#if st.agent_books.request_median_ms}<div class="bet"><span>Запрос стакана (медиана)</span><b class="tnum">{st.agent_books.request_median_ms} мс</b></div>{/if}
        {#if (!st.trades.live && st.trades.error) || (!st.books.live && st.books.error)}
          <div class="sub" style="color:var(--warn-ink)">Поток недоступен — агент работает опросом. {(st.trades.error || st.books.error || "").slice(0, 120)}</div>
        {/if}
      </div>
      <div class="card col" style="gap:8px">
        <h2>Как устроена скорость</h2>
        <div class="sub">Сделки китов и стаканы приходят по постоянному соединению (WebSocket) сами — без ожидания следующего опроса. Экраны приложения читают локальную базу и открываются за миллисекунды.</div>
        <div class="sub">Время запроса к Polymarket — это в основном дорога до его серверов и обратно; её сокращают открытые соединения (без новой установки связи) и потоки, но не код приложения.</div>
      </div>
    </div>
  {/if}
  <div class="grid2">
    <div class="card col" style="gap:10px">
      <h2>Данные</h2>
      <div class="bet"><span>Кошельков китов в списке</span><b class="tnum">{fmtNum(u.tracked_wallets)}</b></div>
      <div class="bet"><span>Сделок китов</span><b class="tnum">{fmtNum(u.trades_tracked)}</b></div>
      <div class="bet"><span>Рынков</span><b class="tnum">{fmtNum(cnt.markets)}</b></div>
      <div class="bet"><span>Точек истории цен</span><b class="tnum">{fmtNum(cnt.prices)}</b></div>
      <div class="bet"><span>Сигналов / наших сделок</span><b class="tnum">{fmtNum(cnt.signals)} / {fmtNum(cnt.bets)}</b></div>
      {#if u.db_size_mb}<div class="bet"><span>Размер базы</span><b class="tnum">{String(u.db_size_mb).replace(".", ",")} МБ</b></div>{/if}
    </div>
    <div class="card col" style="gap:6px">
      <h2>Задачи сбора данных</h2>
      {#each x.jobs as j, i (i)}
        <div class="bet">
          <span class="row" style="gap:8px"><span class="dot {j.ok === false ? 'bad' : j.ok ? 'ok' : ''}" aria-hidden="true"></span>{j.label}</span>
          <span class="sub">{#if j.ok === false}<span class="down">{(j.error || "").slice(0, 80)}</span>{:else}{j.ago_s === null ? "ещё не запускалась" : ago(j.ago_s)}{/if}</span>
        </div>
      {:else}
        <div class="sub">Планировщик не запущен.</div>
      {/each}
    </div>
  </div>
  <details>
    <summary class="link" style="cursor:pointer;display:inline-block">Текст отчёта</summary>
    <pre id="diag-report" class="card" bind:this={reportEl} style="white-space:pre-wrap;font:13px/1.5 ui-monospace,Menlo,monospace;margin-top:10px">{x.report}</pre>
  </details>
{:else}
  <div class="card skeleton">Собираем диагностику…</div>
{/if}
