<script lang="ts">
  import type { Json } from "../lib/api";

  let { checks }: { checks: Json[] } = $props();
</script>

<div class="card checks">
  {#each checks as c, i (i)}
    <div class="check-row">
      <span class="chip {c.ok === true ? 'g' : c.ok === false ? 'r' : 'a'}">{c.ok === true ? "Да" : c.ok === false ? "Нет" : "Учтите"}</span>
      <div><b>{c.title}</b><div class="sub">{c.text}</div></div>
    </div>
  {/each}
</div>
