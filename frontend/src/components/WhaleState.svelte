<script lang="ts">
  let { st }: { st: string | null | undefined } = $props();
</script>

{#if st === "держит"}<span class="chip g">кит держит</span>{:else if st === "вышел"}<span class="chip a">кит вышел</span>{/if}
