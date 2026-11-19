<script lang="ts">
  import { onMount, tick, type Component } from "svelte";
  import Banners from "./components/Banners.svelte";
  import CommandPalette from "./components/CommandPalette.svelte";
  import ConfirmDialog from "./components/ConfirmDialog.svelte";
  import Icon from "./components/Icon.svelte";
  import Login from "./components/Login.svelte";
  import Notifications from "./components/Notifications.svelte";
  import ShortcutsDialog from "./components/ShortcutsDialog.svelte";
  import SidePanel from "./components/SidePanel.svelte";
  import { ago } from "./lib/format";
  import { hotkey } from "./lib/hotkeys";
  import { refreshStatus, startLive, startPolling } from "./lib/live";
  import { ALL_NAV, NAV, crumbs, type NavItem } from "./lib/nav";
  import { match, route } from "./lib/router.svelte";
  import { app, savePref } from "./lib/state.svelte";
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
    accuracy: () => import("./views/EventAccuracy.svelte"),
    trial: () => import("./views/Trial.svelte"),
    welcome: () => import("./views/Welcome.svelte"),
  };
  const STATIC: Record<string, Component<{ params: string[] }>> = { dashboard: Dashboard, signal: Signal };

  const current = $derived(match(route.hash));
  const here = $derived(crumbs(current.name));
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
    app.statusError || !st ? "" : st.data_age_s == null ? "Данных пока нет" : `Данные ${ago(st.data_age_s)}`,
  );
  const aiOn = $derived(st?.ai?.effective === "ai");
  const isActive = (i: NavItem) => i.routes.includes(current.name);
  const moreActive = $derived(ALL_NAV.some((i) => !i.primary && isActive(i)));

  // первый запуск: без согласия с условиями — на приветствие
  $effect(() => {
    if (st && !st.onboarded && current.name !== "welcome") location.hash = "#/welcome";
  });

  $effect(() => {
    void route.seq;
    app.notifOpen = false;
    app.panel = null;
    app.more = false;
    void refreshStatus();
    tick().then(() => {
      mainEl?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    });
  });

  $effect(() => {
    document.body.classList.toggle("bare", current.nav === null);
    document.body.classList.toggle("locked", app.locked);
    document.body.classList.toggle("rail", app.rail);
    document.body.classList.toggle("hints", app.hints);
  });

  function onKey(e: KeyboardEvent) {
    // палитра команд: Ctrl/⌘+K везде, «/» — когда не идёт набор текста
    const t = e.target as HTMLElement | null;
    const typing = !!t && (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable);
    if (!app.locked && (((e.ctrlKey || e.metaKey) && e.code === "KeyK") || (!typing && e.key === "/" && !document.querySelector("dialog[open]")))) {
      e.preventDefault();
      app.palette = !app.palette;
      return;
    }
    if (hotkey(e)) {
      e.preventDefault();
      return;
    }
    if (e.key === "Escape" && (app.notifOpen || app.panel || app.more)) {
      app.notifOpen = false;
      app.panel = null;
      app.more = false;
    }
  }

  function toggleRail() {
    app.rail = !app.rail;
    savePref("wf.rail", app.rail);
  }

  // мягкий свет под курсором на панелях (только на устройствах с мышью)
  function glow(e: PointerEvent) {
    if (e.pointerType !== "mouse") return;
    const el = (e.target as HTMLElement | null)?.closest<HTMLElement>(".card,.ledger,.pcard,.statement,.decide");
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - r.left}px`);
    el.style.setProperty("--my", `${e.clientY - r.top}px`);
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

<svelte:window onkeydown={onKey} onpointermove={glow} />

<div class="aura" aria-hidden="true"></div>
<div class="app" id="app">
  <nav class="side" id="side" aria-label="Разделы" hidden={current.nav === null || app.locked}>
    <div class="brand">
      <span class="mark" aria-hidden="true">W</span>
      <span class="brand-t">WhaleFollow</span>
    </div>
    <button class="pal-btn" onclick={() => (app.palette = true)} title="Поиск и команды (Ctrl+K)">
      <Icon name="search" size={16} /><span class="side-t">Поиск</span><kbd class="side-t">Ctrl K</kbd>
    </button>
    {#each NAV as g (g.title)}
      <div class="nav-group">
        <div class="nav-title side-t">{g.title}</div>
        {#each g.items as i (i.key)}
          <a href={i.hash} class="nav-a" class:on={isActive(i)} class:primary={i.primary} data-nav={i.key} data-tip={i.label} aria-current={isActive(i) ? "page" : undefined}>
            <Icon name={i.icon} />
            <span class="side-t">{i.label}</span>
            {#if i.key === "positions" && st?.need_decision}<span class="badge" id="badge-decide">{st.need_decision}</span>{/if}
          </a>
        {/each}
      </div>
    {/each}
    <button class="nav-a more-btn" class:on={moreActive} aria-expanded={app.more} onclick={() => (app.more = !app.more)}>
      <Icon name="more" /><span>Ещё</span>
    </button>
    <a class="sys" id="sys" href="#/diagnostics" aria-live="polite" title="Состояние системы — открыть диагностику">
      <div class="sys-row"><span class="dot {dotClass}" id="sys-dot" aria-hidden="true"></span><span id="sys-title" class="side-t">{sysTitle}</span></div>
      <div class="sys-sub side-t" id="sys-sub">{sysSub}</div>
    </a>
    <button class="rail-btn" onclick={toggleRail} aria-label={app.rail ? "Развернуть меню" : "Свернуть меню"} title={app.rail ? "Развернуть меню" : "Свернуть меню"}>
      <Icon name={app.rail ? "expand" : "collapse"} size={16} /><span class="side-t">Свернуть</span>
    </button>
  </nav>

  <div class="stage">
    {#if current.nav !== null && !app.locked}
      <header class="topbar">
        <div class="crumbs">
          <span class="m-mark" aria-hidden="true">W</span>
          {#if here.section}
            {#if here.screen}<a href={here.section.hash} class="crumb-sec">{here.section.label}</a><Icon name="expand" size={14} /><b>{here.screen}</b>
            {:else}<b>{here.section.label}</b>{/if}
          {:else}<b>WhaleFollow</b>{/if}
        </div>
        <div class="tb-right">
          <button class="search-pill" onclick={() => (app.palette = true)} aria-label="Поиск и команды">
            <Icon name="search" size={16} /><span>Кит, событие, раздел…</span><kbd>Ctrl K</kbd>
          </button>
          <div class="tb-stats">
            <span class="pill" class:live={app.live} title={app.live ? "Живые обновления подключены" : "Обновления — опросом"}>
              <i class="ldot" aria-hidden="true"></i>{app.live ? "Live" : "Опрос"}
            </span>
            {#if st}
              <a class="pill" class:ai={aiOn} href="#/settings" title="Режим принятия решений">{aiOn ? "ИИ" : "Без ИИ"}</a>
              {#if st.mode === "paper"}<a class="pill paper" href="#/trial" title="Ставки виртуальные">Paper</a>{/if}
              {#if st.need_decision}<a class="pill hot" href="#/positions" title="Ставки ждут вашего решения">Решить · {st.need_decision}</a>{/if}
            {/if}
          </div>
          <button class="btn icon m-search" aria-label="Поиск и команды" onclick={() => (app.palette = true)}><Icon name="search" size={19} /></button>
          <div class="bell-wrap"><Notifications /></div>
        </div>
      </header>
    {/if}
    <main class="main" id="main" tabindex="-1" bind:this={mainEl}>
      {#if app.locked}
        <Login />
      {:else if ready}
        {#key route.hash + ":" + route.seq}
          <div class="view">
            {#if STATIC[current.name]}
              {@const C = STATIC[current.name]}
              <C params={current.params} />
            {:else if LAZY[current.name]}
              {#await LAZY[current.name]()}
                <div class="skel" aria-label="Загрузка"><i></i><i></i><i></i></div>
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
          </div>
        {/key}
      {/if}
    </main>
  </div>
</div>

{#if app.more}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="sheet-back" onclick={() => (app.more = false)}></div>
  <div class="sheet" role="dialog" aria-label="Все разделы">
    <div class="sheet-grip" aria-hidden="true"></div>
    {#each NAV as g (g.title)}
      <div class="nav-title">{g.title}</div>
      <div class="sheet-grid">
        {#each g.items as i (i.key)}
          <a href={i.hash} class="sheet-a" class:on={isActive(i)} data-sheet-nav={i.key}><Icon name={i.icon} size={22} /><span>{i.label}</span></a>
        {/each}
      </div>
    {/each}
    <a class="sheet-sys" href="#/diagnostics"><span class="dot {dotClass}" aria-hidden="true"></span><span>{sysTitle}</span><span class="sub">{sysSub}</span></a>
  </div>
{/if}
<SidePanel />
{#if app.toast}
  {#key app.toastSeq}<div class="toast" id="toast" role="status" aria-live="polite">{app.toast}</div>{/key}
{/if}
<ConfirmDialog />
<ShortcutsDialog />
<CommandPalette />
