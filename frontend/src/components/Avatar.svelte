<script lang="ts">
  import Seal from "./Seal.svelte";

  let { img = null, code = "?", lg = false, sm = false }: { img?: string | null; code?: string | null; lg?: boolean; sm?: boolean } =
    $props();
  let broken = $state(false);
  const ok = $derived(!!img && /^https:\/\//.test(img) && !broken);
</script>

{#if ok}
  <span class="ava" class:lg class:sm><img src={img} alt="" loading="lazy" referrerpolicy="no-referrer" onerror={() => (broken = true)} /></span>
{:else}
  <Seal {code} {lg} />
{/if}
