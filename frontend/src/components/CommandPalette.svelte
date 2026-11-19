<script lang="ts">
  // Палитра команд (Ctrl+K или «/»): переход в любой раздел, действия, поиск китов и событий — с клавиатуры.
  import { tick } from "svelte";
  import { api, type Json } from "../lib/api";
  import { ALL_NAV } from "../lib/nav";
  import { app, savePref, softRefresh, toast } from "../lib/state.svelte";
  import Icon from "./Icon.svelte";

  interface Cmd {
    id: string;
    label: string;
    hint?: string;
    group: string;
    icon: string;
    run: () => void;
  }

  let dlg: HTMLDialogElement | undefined = $state();
  let input: HTMLInputElement | undefined = $state();
  let q = $state("");
  let sel = $state(0);
  let whales = $state<Json[]>([]);
  let events = $state<Json[]>([]);
  let timer: ReturnType<typeof setTimeout> | undefined;

  const go = (h: string) => () => (location.hash = h);

  const base = $derived<Cmd[]>([
    ...ALL_NAV.map((n) => ({ id: "nav:" + n.key, label: n.label, hint: n.keys, group: "Разделы", icon: n.icon, run: go(n.hash) })),
    { id: "a:add", label: "Добавить игрока", group: "Действия", icon: "plus", run: go("#/whales/add") },
    { id: "a:journal", label: "Журнал сигналов", group: "Действия", icon: "bolt", run: go("#/journal") },
    { id: "a:refresh", label: "Обновить данные экрана", group: "Действия", icon: "refresh", run: () => softRefresh() },
    {
      id: "a:hints",
      label: app.hints ? "Скрыть пояснения на экранах" : "Показывать пояснения на экранах",
      group: "Действия",
      icon: "eye",
      run: () => {
        app.hints = !app.hints;
        savePref("wf.hints", app.hints);
      },
    },
    {
      id: "a:rail",
      label: app.rail ? "Развернуть меню" : "Свернуть меню до иконок",
      group: "Действия",
      icon: app.rail ? "expand" : "collapse",
      run: () => {
        app.rail = !app.rail;
        savePref("wf.rail", app.rail);
      },
    },
    { id: "a:keys", label: "Горячие клавиши", hint: "?", group: "Действия", icon: "keyboard", run: () => (app.shortcuts = true) },
  ]);

  function fits(text: string, words: string[]): boolean {
    const t = text.toLowerCase();
    return words.every((w) => t.includes(w));
  }

  const items = $derived.by<Cmd[]>(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const out = base.filter((c) => fits(c.label, words));
    if (words.length) {
      for (const w of whales) {
        const name = String(w.pm?.name || w.name || w.entity_id);
        if (fits("кит " + name + " " + w.entity_id, words))
          out.push({
            id: "w:" + w.entity_id,
            label: name,
            hint: w.status,
            group: "Киты",
            icon: "whale",
            run: go("#/whales/" + encodeURIComponent(w.entity_id)),
          });
      }
      for (const e of events)
        out.push({ id: "e:" + e.id, label: e.title, group: "События", icon: "globe", run: go("#/events/" + encodeURIComponent(e.id)) });
    }
    return out.slice(0, 40);
  });

  $effect(() => {
    if (!dlg) return;
    if (app.palette && !dlg.open) {
      q = "";
      sel = 0;
      dlg.showModal();
      void tick().then(() => input?.focus());
      if (!whales.length) api("/whales").then((r) => (whales = r.items || []), () => {});
    } else if (!app.palette && dlg.open) dlg.close();
  });

  $effect(() => {
    const text = q.trim();
    clearTimeout(timer);
    if (text.length < 2) {
      events = [];
      return;
    }
    timer = setTimeout(async () => {
      try {
        const r = await api(`/catalog?q=${encodeURIComponent(text)}&sort=volume&offset=0`);
        if (q.trim() === text) events = (r.items || []).slice(0, 6);
      } catch {
        events = [];
      }
    }, 220);
  });

  $effect(() => {
    void items.length;
    if (sel >= items.length) sel = Math.max(0, items.length - 1);
  });

  function run(c: Cmd) {
    app.palette = false;
    try {
      c.run();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  function key(e: KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      sel = Math.min(items.length - 1, sel + 1);
      scrollSel();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, sel - 1);
      scrollSel();
    } else if (e.key === "Enter" && items[sel]) {
      e.preventDefault();
      run(items[sel]);
    }
  }

  function scrollSel() {
    void tick().then(() => dlg?.querySelector(".cmd.on")?.scrollIntoView({ block: "nearest" }));
  }
</script>

<dialog
  class="palette"
  bind:this={dlg}
  aria-label="Палитра команд"
  onclose={() => (app.palette = false)}
  onclick={(e) => e.target === dlg && (app.palette = false)}
>
  <div class="pal-in">
    <label class="pal-q">
      <Icon name="search" size={18} />
      <input
        bind:this={input}
        bind:value={q}
        onkeydown={key}
        oninput={() => (sel = 0)}
        placeholder="Раздел, кит, событие или команда…"
        aria-label="Поиск по командам"
        autocomplete="off"
        spellcheck="false"
      />
      <kbd>Esc</kbd>
    </label>
    <div class="pal-list" role="listbox" aria-label="Команды">
      {#each items as c, i (c.id)}
        {#if i === 0 || items[i - 1].group !== c.group}<div class="pal-group">{c.group}</div>{/if}
        <button
          class="cmd"
          class:on={i === sel}
          role="option"
          aria-selected={i === sel}
          onmousemove={() => (sel = i)}
          onclick={() => run(c)}
        >
          <span class="cmd-ic"><Icon name={c.icon} size={17} /></span>
          <span class="cmd-l">{c.label}</span>
          {#if c.hint}<span class="cmd-h">{c.hint}</span>{/if}
        </button>
      {:else}
        <div class="pal-empty">Ничего не нашлось</div>
      {/each}
    </div>
    <div class="pal-foot"><span><kbd>↑</kbd><kbd>↓</kbd> выбрать</span><span><kbd>Enter</kbd> открыть</span><span><kbd>Ctrl</kbd><kbd>K</kbd> палитра</span></div>
  </div>
</dialog>
