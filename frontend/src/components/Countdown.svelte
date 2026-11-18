<script lang="ts">
  import { app } from "../lib/state.svelte";

  let { deadline }: { deadline: number } = $props();
  const left = $derived(Math.max(0, Math.round((deadline - app.now) / 1000)));
</script>

<span class:expired={left === 0}>{left > 0 ? `актуален ещё ${left} с` : "время вышло"}</span>
