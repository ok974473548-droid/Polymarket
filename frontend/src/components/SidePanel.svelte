<script lang="ts">
  // Боковая панель: карточка игрока или рынка поверх текущего экрана (Esc или фон — закрыть)
  import { api, type Json } from "../lib/api";
  import { playerName } from "../lib/format";
  import { app } from "../lib/state.svelte";
  import MarketPanel from "./MarketPanel.svelte";
  import PlayerCard from "./PlayerCard.svelte";
  import MiniPop from "./MiniPop.svelte";

  let data = $state<Json>(null);
  let err = $state("");
  let closeBtn: HTMLButtonElement | undefined = $state();
  let from: Element | null = null;

  async function load(p: NonNullable<typeof app.panel>) {
    data = null;
    err = "";
    try {
      data = await api(p.kind === "player" ? "/players/" + encodeURIComponent(p.id) : "/markets/" + encodeURIComponent(p.id));
    } catch (e) {
      err = (e as Error).message;
    }
  }

  $effect(() => {
    const p = app.panel;
    if (p) {
      from = from || document.activeElement;
      void load(p);
      document.body.classList.add("drawer-open");
      queueMicrotask(() => closeBtn?.focus({ preventScroll: true }));
    } else {
      document.body.classList.remove("drawer-open");
      if (from && document.body.contains(from)) (from as HTMLElement).focus({ preventScroll: true });
      from = null;
    }
  });

  const title = $derived(
    !app.panel ? "" : app.panel.kind === "player" ? (data ? playerName(data) : "Игрок") : data?.question || "Рынок",
  );
</script>

{#if app.panel}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="drawer-back" onclick={() => (app.panel = null)}></div>
  <div class="drawer on" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
    <div class="drawer-top">
      <b id="drawer-title">{title}</b>
      <button class="btn icon" aria-label="Закрыть" bind:this={closeBtn} onclick={() => (app.panel = null)}>✕</button>
    </div>
    <div class="drawer-body">
      {#if err}
        <div class="card err">{err}</div>
      {:else if !data}
        <div class="card skeleton">{app.panel.kind === "player" ? "Загружаем данные Polymarket…" : "Загружаем рынок…"}</div>
      {:else if app.panel.kind === "player"}
        <PlayerCard c={data} inDrawer onadded={() => app.panel && load(app.panel)} />
      {:else}
        <MarketPanel m={data} compact />
      {/if}
    </div>
  </div>
{/if}
<MiniPop />
