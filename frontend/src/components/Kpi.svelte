<script lang="ts">
  let { label, value, sub = "", cls = "" }: { label: string; value: string; sub?: string; cls?: string } = $props();
</script>

<div class="kpi"><div class="label">{label}</div><div class="v {cls}">{value}</div>{#if sub}<div class="sub">{sub}</div>{/if}</div>
