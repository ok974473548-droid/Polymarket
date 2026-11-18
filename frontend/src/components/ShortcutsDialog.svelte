<script lang="ts">
  import { SHORTCUTS } from "../lib/hotkeys";
  import { app } from "../lib/state.svelte";

  let el: HTMLDialogElement | undefined = $state();
  $effect(() => {
    if (!el) return;
    if (app.shortcuts && !el.open) el.showModal();
    if (!app.shortcuts && el.open) el.close();
  });
</script>

<dialog bind:this={el} onclose={() => (app.shortcuts = false)}>
  <form method="dialog" style="display:flex;flex-direction:column;gap:12px;min-width:320px">
    <b style="font-size:20px">Горячие клавиши</b>
    <div class="col" style="gap:8px">
      {#each SHORTCUTS as [k, t] (k)}
        <div class="bet">
          <span>{t}</span>
          <span>
            {#each k.split(" ") as x, i (i)}
              {#if x === "/" || x === "затем"}<span class="sub">{x}</span>{:else}<kbd>{x}</kbd>{/if}{" "}
            {/each}
          </span>
        </div>
      {/each}
    </div>
    <div class="sub">Работают при любой раскладке. В полях ввода отключены.</div>
    <div class="row"><button class="btn p">Понятно</button></div>
  </form>
</dialog>
