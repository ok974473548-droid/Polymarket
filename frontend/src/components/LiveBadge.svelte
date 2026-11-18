<script lang="ts">
  import { app } from "../lib/state.svelte";
</script>

<span class="live-badge" class:on={app.live} title={app.live ? "Новые сигналы и цены приходят сразу" : "Обновление раз в 10 секунд"}>
  <span class="pulse" aria-hidden="true"></span>{app.live ? "в реальном времени" : "обновление раз в 10 с"}
</span>
