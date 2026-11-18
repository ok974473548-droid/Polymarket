<script lang="ts">
  import Checks from "../components/Checks.svelte";
  import MarketBox from "../components/MarketBox.svelte";
  import Research from "../components/Research.svelte";
  import { api } from "../lib/api";
  import { cents, money } from "../lib/format";
  import { nextPendingAfter } from "../lib/hotkeys";
  import { loader } from "../lib/load.svelte";
  import { app, softRefresh, toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const id = $derived(params[0]);
  const d = loader(() => "/signals/" + encodeURIComponent(id));
  let loadedAt = $state(Date.now());
  let showAmount = $state(false);
  let amount = $state(0);
  let busy = $state(false);

  const s = $derived(d.data);
  const decide = $derived(!!s && s.status === "pending" && s.recommendation === "BUY" && !!s.live);
  $effect(() => {
    if (!s) return;
    loadedAt = Date.now();
    amount = Math.round(s.size_usd || 0);
    app.sig = decide ? { id, live: s.live, pending: s.pending_ids || [], size: s.size_usd } : { id, pending: s.pending_ids || [] };
  });

  const L = $derived(s?.live);
  // живая цена: из потока биржи (через сервер), иначе — из карточки
  const ask = $derived(L ? (app.quotes[L.token_id]?.ask ?? L.ask) : null);
  const fee = $derived(L && ask != null ? L.fee_rate * ask * (1 - ask) : 0);
  const edge = $derived(L && ask != null ? L.q_eff - ask - fee : 0);
  const ok = $derived(!!L && edge >= L.threshold);
  const delay = $derived(L && ask != null ? ask - L.whale_price : 0);

  // шкала цен: где купил кит, где цена сейчас, оценка исхода; закрашено — наш запас
  const scale = $derived.by(() => {
    if (!L) return { lo: 0, hi: 1 };
    const pts = [L.whale_price, L.ask, L.q_eff].filter((v: number | null) => v !== null && v !== undefined);
    let lo = Math.max(0, Math.min(...pts) - 0.06),
      hi = Math.min(1, Math.max(...pts) + 0.06);
    if (hi - lo < 0.15) {
      const c = (hi + lo) / 2;
      lo = Math.max(0, c - 0.075);
      hi = Math.min(1, c + 0.075);
    }
    return { lo, hi };
  });
  const x = (v: number) => ((Math.min(Math.max(v, scale.lo), scale.hi) - scale.lo) / (scale.hi - scale.lo)) * 100;

  // кольцо обратного отсчёта
  const R = 30,
    LEN = 2 * Math.PI * R;
  const left = $derived(s ? Math.max(0, (loadedAt + s.seconds_left * 1000 - app.now) / 1000) : 0);
  const frac = $derived(L ? Math.min(1, left / Math.max(1, L.window_s)) : 0);
  const ringNum = $derived(left >= 60 ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}` : String(Math.ceil(left)));

  const statusChip = $derived(
    s ? ({ executed: ["g", "Куплено"], skipped: ["n", "Пропущено системой"], rejected: ["n", "Пропущено вами"], expired: ["n", "Время вышло"] } as Record<string, string[]>)[s.status] : null,
  );
  const ids = $derived<string[]>(s?.pending_ids || []);
  const pos = $derived(ids.indexOf(id));
  const mark = (v: boolean | null) => (v === true ? ["g", "✓"] : v === false ? ["r", "✕"] : ["a", "!"]);

  async function confirm() {
    busy = true;
    try {
      const body = showAmount ? { size_usd: Number(amount) } : {};
      const r = await api(`/signals/${encodeURIComponent(id)}/confirm`, { method: "POST", body });
      toast(r.status === "executed" ? `Куплено на ${money(r.usd)} по ${Math.round(r.avg_price * 100)}¢` : r.error || "Не выполнено");
      if (r.status === "executed") location.hash = nextPendingAfter(id);
      else softRefresh();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      busy = false;
    }
  }

  async function skip() {
    try {
      await api(`/signals/${encodeURIComponent(id)}/skip`, { method: "POST" });
      toast("Сигнал пропущен");
      location.hash = nextPendingAfter(id);
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if s}
  <a class="back" href="#/">← Все сигналы</a>
  <div class="sig-head">
    <div class="grow">
      <h1>{s.question}</h1>
      <div class="sub" style="margin-top:6px">
        Кит <button class="link inline" data-player={s.entity_id} onclick={() => (app.panel = { kind: "player", id: s.entity_id })}>{s.whale}</button>
        · {s.category}{#if s.side}{" "}· ставка на «{s.side}»{/if}
      </div>
    </div>
    {#if pos >= 0 && ids.length > 1}
      <div class="sig-nav sub">
        Сигнал {pos + 1} из {ids.length}
        {#if pos > 0}<a class="link" href="#/signal/{encodeURIComponent(ids[pos - 1])}"><kbd>K</kbd> предыдущий</a>{/if}
        {#if pos < ids.length - 1}<a class="link" href="#/signal/{encodeURIComponent(ids[pos + 1])}">следующий <kbd>J</kbd></a>{/if}
      </div>
    {/if}
  </div>

  {#if decide && L}
    <section class="decide card" aria-label="Решение по сигналу">
      <div class="decide-main">
        <div class="scale-wrap">
          <div class="scale" role="img" aria-label="Цена кита {cents(L.whale_price * 100)}, сейчас {cents((ask ?? L.ask) * 100)}, наша оценка {cents(L.q_eff * 100)}">
            <div class="scale-track"></div>
            <div class="scale-zone {ok ? 'ok' : 'bad'}" style="left:{Math.min(x(ask), x(L.q_eff))}%;width:{Math.abs(x(ask) - x(L.q_eff))}%"></div>
            <div class="scale-mk whale" style="left:{x(L.whale_price)}%"><span>кит</span></div>
            <div class="scale-mk fair" style="left:{x(L.q_eff)}%"><span>оценка {cents(L.q_eff * 100)}</span></div>
            <div class="scale-mk now" style="left:{x(ask)}%"><span>сейчас</span></div>
            <div class="scale-ends sub"><span>{cents(scale.lo * 100)}</span><span>{cents(scale.hi * 100)}</span></div>
          </div>
        </div>
        <div class="decide-facts">
          <div><div class="sub">Кит купил по</div><b class="tnum">{cents(L.whale_price * 100)}</b></div>
          <div>
            <div class="sub">Сейчас можно купить по</div>
            <b class="tnum" id="live-ask">{cents(ask * 100)}</b>{#if L.live}<span class="live-dot" title="Цена из потока биржи, обновляется сама" aria-label="живая цена"></span>{/if}
          </div>
          <div><div class="sub">Задержка стоила</div><b class="tnum" id="delay-cost">{delay > 0.0005 ? cents(delay * 100) + " на акцию" : "ничего"}</b></div>
          <div>
            <div class="sub">Запас выгоды сейчас</div>
            <b class="tnum {ok ? 'up' : 'down'}" id="edge-now">{(edge >= 0 ? "+" : "−") + cents(Math.abs(edge) * 100).replace("¢", "") + "¢"}</b>
            <div class="sub">нужно не меньше {cents(L.threshold * 100)} на акцию</div>
          </div>
        </div>
      </div>
      <div class="ring" class:low={left < 15 && left > 0} class:over={left <= 0} role="timer" aria-label="Сколько осталось на решение">
        <svg viewBox="0 0 72 72" aria-hidden="true">
          <circle cx="36" cy="36" r={R} class="ring-track" />
          <circle cx="36" cy="36" r={R} class="ring-arc" stroke-dasharray={LEN} style="stroke-dashoffset:{LEN * (1 - frac)}" transform="rotate(-90 36 36)" />
        </svg>
        <div class="ring-num tnum">{ringNum}</div>
        <div class="ring-cap">на решение</div>
      </div>
    </section>
    <section class="decide-act">
      <div class="stake">
        <div class="sub">Ставка</div>
        <div class="num">{money(s.size_usd)}</div>
        <div class="sub">Сбудется: <b class="up">{money(s.if_win_usd, { sign: true })}</b> · Нет: <b class="down">{money(s.if_lose_usd, { sign: true })}</b></div>
      </div>
      <div class="row" style="gap:10px">
        <button class="btn p xl" class:dim={!ok} data-act="confirm" disabled={busy} onclick={confirm}>Купить на {money(showAmount ? amount : s.size_usd)} <kbd>B</kbd></button>
        <button class="btn xl" data-act="skip" onclick={skip}>Пропустить <kbd>S</kbd></button>
        <button class="link" style="background:none;border:0;cursor:pointer;min-height:44px" onclick={() => (showAmount = !showAmount)}>Изменить сумму</button>
      </div>
    </section>
    {#if showAmount}
      <div class="card row">
        <label for="amount-inp">Сумма ставки, $</label>
        <input id="amount-inp" class="inp" type="number" min="1" max={s.size_usd} step="1" bind:value={amount} style="width:120px" />
        <span class="sub">Не больше {money(s.size_usd)} — это расчёт системы с учётом лимитов</span>
      </div>
    {/if}
    {#if !ok}
      <div class="banner warn">
        <div class="grow"><b>Выгода ушла</b><div>Цена поднялась — при покупке сейчас запас меньше порога. Система не купит дороже безопасной цены.</div></div>
      </div>
    {/if}
    {#if s.mode === "manual"}
      <div class="banner {s.ai_failed ? 'warn' : 'gray'}">
        <div class="grow">
          <b>{s.ai_failed ? "ИИ не ответил — решение за вами" : "Режим без ИИ — решение за вами"}</b>
          <div>Ниже — всё, что собрала система: история кита, другие рынки прогнозов и новости.</div>
        </div>
      </div>
    {/if}
    <h2>Почему стоит купить</h2>
    <ul class="why">
      {#each s.checks.slice(0, 4) as c, i (i)}
        {@const m = mark(c.ok)}
        <li class={m[0]}><span class="ic" aria-hidden="true">{m[1]}</span><div><b>{c.title}</b><div class="sub">{c.text}</div></div></li>
      {/each}
    </ul>
    <div class="sub">
      Лимит на одну ставку — {money(s.limits.max_bet_usd)} · на сегодня осталось {money(s.limits.left_today_usd)}{#if s.fee_usd}{" "}· комиссия ≈ ${s.fee_usd.toFixed(2).replace(".", ",")} уже учтена{/if}{#if s.delay_s !== null && s.delay_s !== undefined}{" "}· сигнал пришёл через {Math.round(s.delay_s)} с после сделки кита{/if}
    </div>
    <h2>Рынок и кто в нём стоит</h2>
    <MarketBox token={s.token_id} />
    <details class="more">
      <summary class="link">Подробнее: все проверки, оценка ИИ, данные, новости</summary>
      <div class="col" style="gap:16px;margin-top:14px"><Checks checks={s.checks} /><Research {s} /></div>
    </details>
  {:else}
    {#if s.recommendation === "BUY"}
      <div class="card hero">
        <div style="flex:1">
          <span class="chip g">{s.mode === "ai" ? "Рекомендовали купить" : "Расчёт по истории кита: купить"} «{s.side}»</span>
          <div class="num">{money(s.size_usd)} по цене {s.price_cents}¢</div>
        </div>
        <div class="right"><div class="sub">Если сбудется</div><div style="font-size:22px;font-weight:700" class="up">{money(s.if_win_usd, { sign: true })}</div></div>
        <div class="right"><div class="sub">Если нет</div><div style="font-size:22px;font-weight:700" class="down">{money(s.if_lose_usd, { sign: true })}</div></div>
      </div>
      <p style="margin:0;color:var(--ink-2)">{s.plain}</p>
    {:else}
      <div class="card"><span class="chip n">Пропущено</span><div style="margin-top:8px;font-size:16px">{s.skip_reason || s.plain}</div></div>
    {/if}
    {#if statusChip && s.recommendation === "BUY"}<div><span class="chip {statusChip[0]}">{statusChip[1]}</span></div>{/if}
    <h2>{s.mode === "ai" ? "Почему мы так решили" : "Что проверила система"}</h2>
    <Checks checks={s.checks} />
    <h2>Рынок и кто в нём стоит</h2>
    {#if s.token_id}<MarketBox token={s.token_id} />{/if}
    <Research {s} />
  {/if}
{/if}
