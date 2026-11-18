<script lang="ts">
  import { sealTone } from "../lib/format";

  let { code = "?", lg = false }: { code?: string | null; lg?: boolean } = $props();
  const c = $derived(String(code || "?").slice(0, 4));
  const t = $derived(sealTone(c));
</script>

<span class="seal" class:lg style="--seal-hi:{t[0]};--seal-lo:{t[1]}" aria-hidden="true">{c}</span>
