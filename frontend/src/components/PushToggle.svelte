<script lang="ts">
  // Push на этом устройстве: включить/выключить и прислать пробное уведомление
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { currentSubscription, pushSupported, subscribe, unsubscribe } from "../lib/push";
  import { toast } from "../lib/state.svelte";

  const why = pushSupported();
  let on = $state(false);
  let busy = $state(false);
  let devices = $state<number | null>(null);

  async function refresh() {
    on = !!(await currentSubscription().catch(() => null));
    try {
      const k = await api("/push/key");
      devices = k.ok ? k.devices : null;
    } catch {
      devices = null;
    }
  }
  onMount(refresh);

  async function toggle() {
    busy = true;
    try {
      if (on) {
        await unsubscribe();
        toast("Push на этом устройстве выключен");
      } else {
        const r = await subscribe();
        toast(r.ok ? "Push включён: сигналы и выходы китов придут на это устройство" : r.error || "Не удалось включить push");
      }
    } catch (e) {
      toast((e as Error).message);
    } finally {
      busy = false;
      void refresh();
    }
  }
  async function test() {
    try {
      const r = await api("/push/test", { method: "POST" });
      toast(r.ok ? `Отправлено на ${r.sent} ${r.sent === 1 ? "устройство" : "устройства"}` : r.error);
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

<div class="push-box" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">
  <div class="row" style="justify-content:space-between">
    <div>
      <b>Push на этом устройстве</b>
      <div class="sub">
        {#if why}{why}{:else if on}Включён. Новые сигналы и выход кита из позиции придут, даже если вкладка закрыта.{:else}Выключен.{/if}
        {#if devices}{" "}Подписано устройств: {devices}.{/if}
      </div>
    </div>
    {#if !why}
      <div class="row">
        <button type="button" class="btn {on ? '' : 'p'}" data-act="push" disabled={busy} onclick={toggle}>{on ? "Выключить" : "Включить"}</button>
        {#if on}<button type="button" class="btn" onclick={test}>Проверить</button>{/if}
      </div>
    {/if}
  </div>
</div>
