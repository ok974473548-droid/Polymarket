<script lang="ts">
  import { api, type Json } from "../lib/api";
  import { refreshStatus } from "../lib/live";
  import { app, toast } from "../lib/state.svelte";

  let { unread = 0 }: { unread?: number } = $props();
  let items = $state<Json[] | null>(null);
  const count = $derived(app.status?.unread ?? unread);

  function href(i: Json): string | null {
    if (i.kind === "signal" && i.ref) return "#/signal/" + encodeURIComponent(i.ref);
    if ((i.kind === "whale_check" || i.kind === "whale_status") && i.ref) return "#/whales/" + encodeURIComponent(i.ref);
    if (i.kind === "whale_exit" || i.kind === "settled") return "#/positions";
    if (i.kind === "weekly") return "#/results/week";
    if (i.link) return String(i.link).replace(/^\//, "");
    return null;
  }

  async function toggle() {
    if (app.notifOpen) {
      app.notifOpen = false;
      return;
    }
    try {
      items = (await api("/notifications")).items;
      app.notifOpen = true;
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function readAll() {
    await api("/notifications/read", { method: "POST" });
    app.notifOpen = false;
    void refreshStatus();
  }
</script>

<button class="btn icon" aria-label="Уведомления" aria-expanded={app.notifOpen} onclick={toggle} data-act="bell">
  <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
    ><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" /></svg
  >
  {#if count}<span class="count bell-count">{count}</span>{/if}
</button>
{#if app.notifOpen && items}
  <div class="card notif" role="region" aria-label="Уведомления">
    <div class="top" style="padding:14px 20px;border-bottom:1px solid var(--line)">
      <b style="font-size:16px">Уведомления</b>
      <button class="link" style="background:none;border:0;cursor:pointer;min-height:44px" onclick={readAll}>Прочитать все</button>
    </div>
    {#each items as i, k (k)}
      {@const h = href(i)}
      {#if h}
        <a class="nt" href={h} style="color:inherit;text-decoration:none">
          <span class="dot {i.unread ? 'ok' : 'read'}" aria-hidden="true"></span>
          <div>{#if i.unread}<b>{i.title}</b>{:else}{i.title}{/if}<div class="sub">{i.body}{i.body ? " · " : ""}{i.when}</div></div>
        </a>
      {:else}
        <div class="nt">
          <span class="dot {i.unread ? 'ok' : 'read'}" aria-hidden="true"></span>
          <div>{#if i.unread}<b>{i.title}</b>{:else}{i.title}{/if}<div class="sub">{i.body}{i.body ? " · " : ""}{i.when}</div></div>
        </div>
      {/if}
    {:else}
      <div class="nt">Уведомлений пока нет</div>
    {/each}
  </div>
{/if}
