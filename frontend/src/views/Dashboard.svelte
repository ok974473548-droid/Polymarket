<script lang="ts">
  import Banners from "../components/Banners.svelte";
  import Countdown from "../components/Countdown.svelte";
  import Flash from "../components/Flash.svelte";
  import LiveBadge from "../components/LiveBadge.svelte";
  import Seal from "../components/Seal.svelte";
  import { untrack } from "svelte";
  import { api } from "../lib/api";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { cents, money, signedPct, tone } from "../lib/format";
  import { refreshStatus } from "../lib/live";
  import { loader } from "../lib/load.svelte";
  import { chime, setSound, soundOn } from "../lib/sound";
  import { app, softRefresh, toast } from "../lib/state.svelte";

  let { params: _ }: { params: string[] } = $props();
  const d = loader(() => "/dashboard");
  let sound = $state(soundOn());
  // какие сигналы человек уже видел — новые подсвечиваются
  let seen = $state<Set<string> | null>(app.seenPending);
  const loadedAt = $derived(d.data ? Date.now() : 0);
  $effect(() => {
    if (!d.data) return;
    const ids = new Set<string>(d.data.pending.map((p: { signal_id: string }) => p.signal_id));
    seen = untrack(() => app.seenPending);
    app.seenPending = ids;
  });

  const lim = $derived(d.data?.daily_limit || { spent_usd: 0, limit_usd: 0 });
  const used = $derived(lim.limit_usd ? Math.min(100, (lim.spent_usd / lim.limit_usd) * 100) : 0);

  function toggleSound() {
    sound = !sound;
    setSound(sound);
    if (sound) chime(true);
    toast(sound ? "Звук при новом сигнале включён" : "Звук выключен");
  }

  async function stop() {
    const ok = await confirmDialog({
      title: "Остановить всё?",
      ok: "Остановить",
      danger: true,
      text: "Новые ставки создаваться не будут. Открытые ставки останутся до закрытия рынков — их можно продать вручную в разделе «Мои ставки».",
    });
    if (!ok) return;
    try {
      await api("/stop", { method: "POST" });
      toast("Система остановлена");
      await refreshStatus();
      softRefresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if d.error}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{d.error}</div></div>
{:else if d.data}
  {@const data = d.data}
  <Banners />
  <div class="page-head">
    <div>
      <h1>Сигналы</h1>
      <div class="lede">Ставки китов из вашего списка, которые прошли проверку. Решение за вами, пока не включено автоподтверждение.</div>
    </div>
    <div class="row" style="position:relative">
      <button class="btn icon" aria-pressed={sound} title="Звук при новом сигнале" aria-label="Звук при новом сигнале" onclick={toggleSound} data-act="sound">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M11 5 6 9H3v6h3l5 4z" />
          {#if sound}<path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" />{:else}<path d="m17 9 5 6" /><path d="m22 9-5 6" />{/if}
        </svg>
      </button>
      <button class="btn danger" onclick={stop} data-act="stop">Остановить всё</button>
    </div>
  </div>
  <section class="statement" aria-label="Баланс">
    <div>
      <div class="label">Баланс</div>
      <div class="figure">{money(data.balance_usd)}</div>
      <div class="sub" style="margin-top:10px">Лимит на сегодня: потрачено {money(lim.spent_usd)} из {money(lim.limit_usd)}</div>
      <div class="bar" style="margin-top:8px;max-width:320px" role="progressbar" aria-label="Потрачено от дневного лимита"
        aria-valuemin="0" aria-valuemax={lim.limit_usd} aria-valuenow={lim.spent_usd}><span style="width:{used}%"></span></div>
    </div>
    <div class="facts">
      <div class="fact"><div class="label">За 30 дней</div><div class="v {tone(data.pnl_30d_usd)}">{money(data.pnl_30d_usd, { sign: true })}<small>{signedPct(data.pnl_30d_pct)}</small></div></div>
      <div class="fact"><div class="label">Открытых ставок</div><div class="v">{data.open_bets}</div></div>
      <div class="fact"><div class="label">Ждут решения</div><div class="v">{data.pending.length}</div></div>
    </div>
  </section>
  <div class="top"><h2>Ждут вашего решения</h2><LiveBadge /></div>
  {#if data.pending.length}
    <div class="ledger" id="pending-list">
      {#each data.pending as p (p.signal_id)}
        {@const q = app.quotes[p.token_id]}
        <a class="ledger-row sig-row" class:fresh={seen && !seen.has(p.signal_id)} data-sid={p.signal_id}
          style="grid-template-columns:minmax(0,1fr) auto auto auto" href="#/signal/{encodeURIComponent(p.signal_id)}">
          <div class="who">
            <Seal code={p.whale} />
            <div class="grow">
              <div class="t">{p.question}</div>
              <div class="m">
                {#if p.side}<span>«{p.side}»</span>{/if}<span>Кит {p.whale}</span><span>{p.category}</span>
                <Countdown deadline={loadedAt + p.seconds_left * 1000} />
              </div>
            </div>
          </div>
          <div class="n hide-sm">
            <div class="tnum"><b><Flash text={q && q.ask != null ? cents(q.ask * 100) : "—"} /></b></div>
            <div class="sub">кит {cents(p.whale_cents)}</div>
          </div>
          <span class="chip b">{p.action} на {money(p.size_usd)}</span>
          <span class="btn p">Решить</span>
        </a>
      {/each}
    </div>
    <div class="sub kbd-hint"><kbd>J</kbd> <kbd>K</kbd> — по сигналам, <kbd>Enter</kbd> — открыть, <kbd>?</kbd> — все клавиши</div>
  {:else}
    <div class="card empty">
      <b>Новых сигналов нет</b>
      <div class="sub" style="font-size:15px;max-width:52ch">
        Киты из вашего списка не делали ставок, которые прошли проверку. Когда появится выгодная, придёт уведомление.
      </div>
      <a class="btn" href="#/settings">Настроить уведомления</a>
    </div>
  {/if}
  <div class="top" style="margin-top:8px"><h2>Недавние решения</h2><a class="link" href="#/journal">Журнал всех решений</a></div>
  {#if data.recent.length}
    <div class="ledger">
      {#each data.recent as r (r.signal_id)}
        <a class="ledger-row" style="grid-template-columns:minmax(0,1fr) auto" href="#/signal/{encodeURIComponent(r.signal_id)}">
          <div><div class="t">{r.question}</div><div class="m"><span>{r.text}</span></div></div>
          <span class="chip {r.label === 'Куплено' ? 'g' : 'n'}">{r.label}</span>
        </a>
      {/each}
    </div>
  {:else}
    <div class="sub">Решений пока не было.</div>
  {/if}
{/if}
