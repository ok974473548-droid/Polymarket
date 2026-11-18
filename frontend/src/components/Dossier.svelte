<script lang="ts">
  // Досье события: другие площадки, данные по теме, сделки и держатели Polymarket, новости — и что с источниками.
  // Собирается само при открытии (если старше 30 мин), идёт в фоне; ход сбора виден; раз в 5 мин проверяется свежесть.
  import { onDestroy } from "svelte";
  import { api, type Json } from "../lib/api";
  import { centsOf, pctOf } from "../lib/events";
  import { ago, plural, safeUrl } from "../lib/format";

  let { eventId, outcomes, priceOf }: { eventId: string; outcomes: Json[]; priceOf: (r: Json) => number | null } = $props();

  let d = $state<Json>(null);
  let err = $state("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  let alive = true;
  onDestroy(() => {
    alive = false;
    clearTimeout(timer);
  });

  async function load(collect: "auto" | "force" | "no" = "auto") {
    clearTimeout(timer);
    try {
      d = await api(`/catalog/${encodeURIComponent(eventId)}/dossier?collect=${collect}`);
      err = d.ok ? "" : d.error;
    } catch (e) {
      err = (e as Error).message;
    }
    if (!alive) return;
    // пока идёт сбор — смотрим каждые 2 с; потом — раз в 5 мин (собрать заново, если устарело)
    timer = setTimeout(() => load(d?.job?.state === "running" ? "no" : "auto"), d?.job?.state === "running" ? 2000 : 300000);
  }
  void load();

  const items = $derived((d?.items || []) as Json[]);
  const byKind = (k: string) => items.filter((x) => x.kind === k);
  const titleOf = (key: string) => outcomes.find((o) => o.key === key)?.title || "";
  const rowOf = (key: string) => outcomes.find((o) => o.key === key);
  const running = $derived(d?.job?.state === "running");
  const sources = $derived(Object.entries(d?.sources || {}) as [string, Json][]);
  const FLAG: Record<string, string> = {
    coordinated_text: "Одинаковые тексты в нескольких источниках — похоже на вброс",
    news_after_price_move: "Новости вышли уже после движения цены — это следствие, а не причина",
    already_priced: "Цена уже сдвинулась после новостей — информация, скорее всего, учтена",
  };
  const flags = $derived(Object.entries(FLAG).filter(([k]) => d?.flags?.[k]));
  function fmtValue(x: Json): string {
    if (x.value === null || x.value === undefined) return "";
    const v = Number(x.value);
    const s = Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("ru-RU") : v.toLocaleString("ru-RU", { maximumFractionDigits: 3 });
    return x.unit === "USD" ? `$${s}` : `${s} ${x.unit || ""}`.trim();
  }
  function since(ts: number | null): string {
    return ts ? ago(Math.max(0, Math.round(Date.now() / 1000 - ts))) : "";
  }
</script>

<div class="col" style="gap:16px" id="dossier">
  <div class="row" style="justify-content:space-between">
    <div>
      {#if running}
        <b>Собираем досье…</b>
        <span class="sub">источников готово {d.job.done} из {d.job.total || "…"}</span>
      {:else if d?.collected_ts}
        {@const ns = sources.filter(([, s]) => s.n).length}
        <b>{items.length} {plural(items.length, "факт", "факта", "фактов")} из {ns} {plural(ns, "источника", "источников", "источников")}</b>
        <span class="sub">собрано {since(d.collected_ts)}</span>
      {:else if err}
        <span class="down">{err}</span>
      {:else}
        <span class="sub">Загружаем…</span>
      {/if}
    </div>
    <button class="btn" data-act="dossier-refresh" disabled={running} onclick={() => load("force")}>{running ? "Идёт сбор…" : "Собрать заново"}</button>
  </div>
  {#if running}<div class="bar" role="progressbar" aria-label="Сбор досье" aria-valuemin={0} aria-valuemax={d.job.total || 1} aria-valuenow={d.job.done}><span style="width:{d.job.total ? (100 * d.job.done) / d.job.total : 8}%"></span></div>{/if}

  {#if byKind("forecast").length}
    <section class="col" style="gap:8px" aria-label="Другие площадки">
      <h3>Другие площадки прогнозов</h3>
      <div class="sub">Вероятность того же события в другом месте. Вопросы сопоставлены по словам — проверьте формулировку.</div>
      <div class="ledger">
        {#each byKind("forecast") as x, i (i)}
          {@const r = rowOf(x.outcome_key)}
          {@const pm = r ? priceOf(r) : null}
          <div class="ledger-row dz-fc">
            <div>
              <div class="t"><a class="link" href={safeUrl(x.url)} target="_blank" rel="noopener">{x.title}</a></div>
              <div class="m"><span>{x.source}</span><span>к исходу «{titleOf(x.outcome_key)}»</span>{#if x.meta?.similarity}<span>совпадение слов {Math.round(x.meta.similarity * 100)}%</span>{/if}</div>
            </div>
            <div class="n"><b class="tnum">{pctOf(x.value)}</b><div class="sub">у них</div></div>
            <div class="n"><div class="tnum">{pctOf(pm)}</div><div class="sub">Polymarket</div></div>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  {#if byKind("data").length}
    <section class="col" style="gap:8px" aria-label="Данные по теме">
      <h3>Данные по теме</h3>
      <div class="dz-cards">
        {#each byKind("data") as x, i (i)}
          <div class="card dz-card">
            <div class="label">{x.title}</div>
            <div class="big tnum">{fmtValue(x)}</div>
            {#if x.note}<div class="sub">{x.note}</div>{/if}
            <div class="sub">{#if x.url}<a class="link" href={safeUrl(x.url)} target="_blank" rel="noopener">{x.source}</a>{:else}{x.source}{/if}{x.ts_pub ? " · " + since(x.ts_pub) : ""}</div>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  {#if byKind("flow").length}
    <section class="col" style="gap:6px" aria-label="Сделки и держатели">
      <h3>Сделки и держатели на Polymarket</h3>
      {#each byKind("flow") as x, i (i)}
        <div class="bet">
          <span>{x.title}{#if x.note}<span class="sub"> · {x.note}</span>{/if}</span>
          <span class="sub">{x.ts_pub ? since(x.ts_pub) : ""}</span>
        </div>
      {/each}
    </section>
  {/if}

  {#if byKind("news").length}
    <section class="col" style="gap:6px" aria-label="Новости">
      <h3>Новости и сообщения</h3>
      {#each flags as [k, t] (k)}<div class="banner warn"><div class="grow">{t}</div></div>{/each}
      {#each byKind("news") as x, i (i)}
        <div class="bet">
          <span><a class="link" style="color:var(--ink)" href={safeUrl(x.url)} target="_blank" rel="noopener">{x.title}</a></span>
          <span class="sub" style="white-space:nowrap">{x.source}{x.ts_pub ? " · " + since(x.ts_pub) : ""}{#if x.weight !== null && x.weight !== undefined && x.weight < 0.5} · <span title="Источник пока мало проверен">надёжность {Math.round(x.weight * 100)}%</span>{/if}</span>
        </div>
      {/each}
    </section>
  {/if}

  {#if d?.collected_ts && !items.length && !running}
    <div class="sub">По этому событию источники ничего не нашли. Включите другие источники на экране «Плагины».</div>
  {/if}

  {#if sources.length}
    <div class="dz-src" aria-label="Источники досье">
      {#each sources as [name, s] (name)}
        <span class="chip sm {s.error ? 'r' : s.n ? 'g' : 'n'}" title={s.error || ""}>{s.title || name}: {s.error ? "ошибка" : s.n}</span>
      {/each}
      {#if sources.some(([, s]) => s.error)}<a class="link sub" href="#/plugins">Подробнее об ошибках — «Плагины»</a>{/if}
    </div>
  {/if}
  <div class="sub">Цены Polymarket для сравнения — живые: {outcomes.slice(0, 3).map((o) => `${o.title} ${centsOf(priceOf(o))}`).join(" · ")}.</div>
</div>
