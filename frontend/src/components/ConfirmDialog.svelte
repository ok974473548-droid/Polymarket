<script lang="ts">
  import { closeConfirm, confirmState } from "../lib/dialog.svelte";

  let el: HTMLDialogElement | undefined = $state();
  $effect(() => {
    if (!el) return;
    if (confirmState.open && !el.open) el.showModal();
    if (!confirmState.open && el.open) el.close();
  });
</script>

<dialog bind:this={el} onclose={() => confirmState.open && closeConfirm(false)}>
  <form method="dialog" style="display:flex;flex-direction:column;gap:12px" onsubmit={(e) => { e.preventDefault(); closeConfirm(true); }}>
    <b style="font-size:20px">{confirmState.title}</b>
    <div>{confirmState.text}</div>
    <div class="row">
      <button class="btn {confirmState.danger ? 'danger solid' : 'p'}" type="submit">{confirmState.ok}</button>
      <button class="btn" type="button" onclick={() => closeConfirm(false)}>Отмена</button>
    </div>
  </form>
</dialog>
