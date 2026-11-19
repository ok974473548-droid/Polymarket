<script lang="ts">
  import { ICONS } from "../lib/nav";

  let { name, size = 20 }: { name: string; size?: number } = $props();
</script>

<svg
  class="ico"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"><path d={ICONS[name] || ""} /></svg
>
