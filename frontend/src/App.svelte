<script lang="ts">
  import { onMount, tick, type Component } from "svelte";
  import Banners from "./components/Banners.svelte";
  import ConfirmDialog from "./components/ConfirmDialog.svelte";
  import Login from "./components/Login.svelte";
  import ShortcutsDialog from "./components/ShortcutsDialog.svelte";
  import SidePanel from "./components/SidePanel.svelte";
  import { ago } from "./lib/format";
  import { hotkey } from "./lib/hotkeys";
  import { refreshStatus, startLive, startPolling } from "./lib/live";
  import { match, route } from "./lib/router.svelte";
  import { app } from "./lib/state.svelte";
  import Dashboard from "./views/Dashboard.svelte";
  import Signal from "./views/Signal.svelte";

  // экраны без параметров тоже получают params — лишнее свойство им не мешает
  type View = Component<{ params: string[] }> | Component<Record<string, never>>;
  type Loader = () => Promise<{ default: View }>;
  // первый экран (сигналы и карточка сигнала) — в основной части; остальное подгружается по требованию
  const LAZY: Record<string, Loader> = {
    journal: () => import("./views/Journal.svelte"),
    positions: () => import("./views/Positions.svelte"),
    bet: () => import("./views/Bet.svelte"),
    whales: () => import("./views/Whales.svelte"),
    add: () => import("./views/AddWhale.svelte"),
    rotation: () => import("./views/Rotation.svelte"),
    whale: () => import("./views/Whale.svelte"),
    results: () => import("./views/Results.svelte"),
    settings: () => import("./views/Settings.svelte"),
    diagnostics: () => import("./views/Diagnostics.svelte"),
    plugins: () => import("./views/Plugins.svelte"),
    events: () => import("./views/Events.svelte"),
    event: () => import("./views/Event.svelte"),
    trial: () => import("./views/Trial.svelte"),
    welcome: () => import("./views/Welcome.svelte"),
  };
  const STATIC: Record<string, Component<{ params: string[] }>> = { dashboard: Dashboard, signal: Signal };

  const current = $derived(match(route.hash));
  let mainEl: HTMLElement | undefined = $state();
  let ready = $state(false);

  const st = $derived(app.status);
  const dotClass = $derived(
    app.statusError ? "bad" : !st ? "" : st.ok ? "ok" : st.stopped || st.paused ? "bad" : "warn",
  );
  const sysTitle = $derived(
    app.statusError
      ? "Нет связи с сервером"
      : !st
        ? "Подключаемся…"
        : st.stopped
          ? "Система остановлена"
          : st.paused
            ? "Система на паузе"
            : st.ok
              ? "Система работает"
              : "Нет свежих данных",
  );
  const sysSub = $derived(
    app.statusError || !st ? "" : st.data_age_s == null ? "Данных пока нет" : `Данные обновлены ${ago(st.data_age_s)}`,
  );

  // первый запуск: без согласия с условиями — на приветствие
  $effect(() => {
    if (st && !st.onboarded && current.name !== "welcome") location.hash = "#/welcome";
  });

  $effect(() => {
    void route.seq;
    app.notifOpen = false;
    app.panel = null;
    void refreshStatus();
    tick().then(() => {
      mainEl?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    });
  });

  $effect(() => {
    document.body.classList.toggle("bare", current.nav === null);
    document.body.classList.toggle("locked", app.locked);
  });

  function onKey(e: KeyboardEvent) {
    if (hotkey(e)) {
      e.preventDefault();
      return;
    }
    if (e.key === "Escape" && (app.notifOpen || app.panel)) {
      app.notifOpen = false;
      app.panel = null;
    }
  }

  onMount(async () => {
    try {
      const a = await (await fetch("/api/auth", { credentials: "same-origin" })).json();
      if (a.required && !a.ok) app.locked = true;
    } catch {
      /* сервер недоступен — покажет баннер */
    }
    ready = true;
    if (!app.locked) {
      await refreshStatus();
      startLive();
    }
    startPolling();
    setInterval(() => (app.now = Date.now()), 1000);
  });
</script>

<svelte:window onkeydown={onKey} />

<div class="app" id="app">
  <nav class="side" id="side" aria-label="Разделы" hidden={current.nav === null || app.locked}>
    <p class="logo"><span class="seal" aria-hidden="true">W</span>WhaleFollow</p>
    <a href="#/" class:on={current.nav === "signals"} data-nav="signals">Сигналы</a>
    <a href="#/events" class:on={current.nav === "events"} data-nav="events">События</a>
    <a href="#/positions" class:on={current.nav === "positions"} data-nav="positions"
      >Мои ставки{#if st?.need_decision}<span class="badge" id="badge-decide">{st.need_decision}</span>{/if}</a
    >
    <a href="#/whales" class:on={current.nav === "whales"} data-nav="whales">Киты</a>
    <a href="#/results" class:on={current.nav === "results"} data-nav="results">Итоги</a>
    <a href="#/settings" class:on={current.nav === "settings"} data-nav="settings">Настройки</a>
    <a class="sys" id="sys" href="#/diagnostics" aria-live="polite" title="Открыть диагностику">
      <div class="sys-row"><span class="dot {dotClass}" id="sys-dot" aria-hidden="true"></span><span id="sys-title">{sysTitle}</span></div>
      <div class="sys-sub" id="sys-sub">{sysSub}</div>
    </a>
  </nav>
  <main class="main" id="main" tabindex="-1" bind:this={mainEl}>
    {#if app.locked}
      <Login />
    {:else if ready}
      {#key route.hash + ":" + route.seq}
        {#if STATIC[current.name]}
          {@const C = STATIC[current.name]}
          <C params={current.params} />
        {:else if LAZY[current.name]}
          {#await LAZY[current.name]()}
            <div class="sub" style="padding:40px 0">Загрузка…</div>
          {:then mod}
            {@const V = mod.default as Component<{ params: string[] }>}
            <V params={current.params} />
          {:catch e}
            <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{e.message}</div></div>
          {/await}
        {:else}
          <Banners />
          <div class="card empty"><b>Такой страницы нет</b><a class="btn" href="#/">К сигналам</a></div>
        {/if}
      {/key}
    {/if}
  </main>
</div>
<SidePanel />
{#if app.toast}
  {#key app.toastSeq}<div class="toast" id="toast" role="status" aria-live="polite">{app.toast}</div>{/key}
{/if}
<ConfirmDialog />
<ShortcutsDialog />
