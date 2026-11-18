<script lang="ts">
  import { loader } from "../lib/load.svelte";

  let { params: _ }: { params: string[] } = $props();
  const j = loader(() => "/journal?limit=200");
  const LABEL: Record<string, string> = {
    executed: "Куплено",
    pending: "Ждёт решения",
    skipped: "Пропущено",
    rejected: "Пропущено вами",
    expired: "Время вышло",
  };
</script>

<a class="back" href="#/">← Сигналы</a>
<h1>Журнал решений</h1>
<div class="sub">Каждый сигнал, включая пропущенные, с причиной.</div>
{#if j.error}
  <div class="card err" role="alert">{j.error}</div>
{:else if j.data}
  {#each j.data.items as r (r.signal_id)}
    <a class="card item" style="text-decoration:none;color:inherit" href="#/signal/{encodeURIComponent(r.signal_id)}">
      <div class="grow">
        <b>{r.question}</b>
        <div class="sub">
          Кит {r.whale} · {new Date(r.ts * 1000).toLocaleString("ru-RU")}{r.kind === "whale_exit" ? " · кит вышел" : ""}{r.reason ? " · " + r.reason : ""}
        </div>
      </div>
      <span class="chip {r.status === 'executed' ? 'g' : r.status === 'pending' ? 'a' : 'n'}">{LABEL[r.status] || r.status}</span>
    </a>
  {:else}
    <div class="card empty">Решений пока не было</div>
  {/each}
{/if}
