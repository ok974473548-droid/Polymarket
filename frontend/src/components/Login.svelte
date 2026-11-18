<script lang="ts">
  import { api } from "../lib/api";

  let password = $state("");
  let err = $state("");

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    try {
      await api("/login", { method: "POST", body: { password } });
      location.reload();
    } catch (ex) {
      err = (ex as Error).message;
    }
  }
</script>

<div class="card col" style="max-width:380px;margin:12vh auto;gap:14px">
  <h1 style="margin:0">WhaleFollow</h1>
  <div class="sub">Введите пароль интерфейса (переменная WF_UI_PASSWORD).</div>
  <form class="col" style="gap:10px" onsubmit={submit}>
    <label for="login-pass">Пароль</label>
    <!-- svelte-ignore a11y_autofocus -->
    <input id="login-pass" type="password" autocomplete="current-password" required autofocus bind:value={password} />
    {#if err}<div class="err" role="alert">{err}</div>{/if}
    <button class="btn p" type="submit">Войти</button>
  </form>
</div>
