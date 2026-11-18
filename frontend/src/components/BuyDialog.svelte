<script lang="ts">
  // Виртуальная покупка исхода со страницы события: сумма, сколько акций, комиссия, что получите.
  import { api, type Json } from "../lib/api";
  import { centsOf } from "../lib/events";
  import { money } from "../lib/format";
  import { softRefresh, toast } from "../lib/state.svelte";

  let {
    eventId,
    order = $bindable(null),
    limits,
    feeRate = 0,
  }: {
    eventId: string;
    order: { token: string; title: string; side: string; ask: number | null } | null;
    limits: Json;
    feeRate?: number | null;
  } = $props();

  let el: HTMLDialogElement | undefined = $state();
  let usd = $state(10);
  let busy = $state(false);

  $effect(() => {
    if (order && el && !el.open) {
      usd = Math.min(10, limits?.max_bet_usd || 10, Math.max(1, limits?.left_today_usd || 10));
      el.showModal();
    } else if (!order && el?.open) el.close();
  });

  const p = $derived(order?.ask ?? null);
  const shares = $derived(p ? usd / p : 0);
  const fee = $derived(p ? (feeRate || 0) * shares * p * (1 - p) : 0);
  const max = $derived(Math.min(limits?.max_bet_usd || 0, limits?.left_today_usd || 0));

  async function buy(e: SubmitEvent) {
    e.preventDefault();
    if (!order) return;
    busy = true;
    try {
      const r = await api(`/catalog/${encodeURIComponent(eventId)}/buy`, { method: "POST", body: { token_id: order.token, usd } });
      toast(r.ok ? r.message : r.error);
      if (r.ok) {
        order = null;
        softRefresh();
      }
    } catch (x) {
      toast((x as Error).message);
    } finally {
      busy = false;
    }
  }
</script>

<dialog bind:this={el} class="buy-dlg" onclose={() => (order = null)} aria-labelledby="buy-title">
  {#if order}
    <form method="dialog" class="col" style="gap:14px" onsubmit={buy}>
      <div>
        <div class="label">Виртуальная ставка</div>
        <h2 id="buy-title" style="margin-top:4px">{order.title} — «{order.side}»</h2>
        <div class="sub">Лучшая цена продавца сейчас {centsOf(p)}. Покупка проходит по стакану, не дороже {centsOf(p !== null ? Math.min(p + 0.02, 0.99) : null)}.</div>
      </div>
      <label class="fld">
        <span>Сумма, $</span>
        <input class="inp" type="number" min="1" step="1" max={max || undefined} bind:value={usd} required data-act="buy-usd" />
      </label>
      <div class="row" style="gap:24px">
        <div class="fact"><div class="label">Акций</div><div class="v">{shares ? shares.toFixed(1).replace(".", ",") : "—"}</div></div>
        <div class="fact"><div class="label">Если сбудется</div><div class="v up">{money(shares - usd - fee, { sign: true })}</div></div>
        <div class="fact"><div class="label">Комиссия</div><div class="v">{money(fee, { cents: true })}</div></div>
      </div>
      <div class="sub">
        Лимит ставок по событиям: до {money(limits?.max_bet_usd)} на одну, осталось на сегодня {money(limits?.left_today_usd)} (общий банк). Если не сбудется — ставка сгорит целиком.
      </div>
      <div class="row">
        <button class="btn p" type="submit" disabled={busy || !p || max < 1} data-act="buy-confirm">{busy ? "Покупаем…" : `Купить на ${money(usd)}`}</button>
        <button class="btn" type="button" onclick={() => (order = null)}>Отмена</button>
      </div>
    </form>
  {/if}
</dialog>
