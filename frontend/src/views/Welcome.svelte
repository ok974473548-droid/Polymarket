<script lang="ts">
  // Первый запуск: 3 шага (подтверждения, режим и сумма, лимиты)
  import { api, type Json } from "../lib/api";
  import { refreshStatus } from "../lib/live";
  import { toast } from "../lib/state.svelte";

  let { params }: { params: string[] } = $props();
  const step = $derived(Number(params[0] || "1") || 1);
  const STEPS = ["Проверка доступа", "Режим и сумма", "Лимиты и уведомления"];

  let s = $state<Json>(null);
  $effect(() => {
    if (step > 1 && !s) api("/settings").then((x) => (s = x)).catch((e: Error) => toast(e.message));
  });

  let j = $state(false);
  let r = $state(false);
  let bank = $state<number | null>(null);
  let confirmEach = $state<boolean | null>(null);
  let maxBet = $state<number | null>(null);
  let dayLim = $state<number | null>(null);

  async function step1(e: SubmitEvent) {
    e.preventDefault();
    try {
      const x = await api("/onboarding", { method: "POST", body: { jurisdiction_ok: j, risk_ack: r } });
      if (!x.ok) return toast(x.error);
      location.hash = "#/welcome/2";
    } catch (x) {
      toast((x as Error).message);
    }
  }
  async function step2(e: SubmitEvent) {
    e.preventDefault();
    try {
      await api("/settings", { method: "PUT", body: { bankroll_usd: Number(bank ?? s.bankroll_usd), require_confirmation: confirmEach ?? s.require_confirmation } });
      s = null;
      location.hash = "#/welcome/3";
    } catch (x) {
      toast((x as Error).message);
    }
  }
  async function step3(e: SubmitEvent) {
    e.preventDefault();
    try {
      const x = await api("/settings", { method: "PUT", body: { max_bet_usd: Number(maxBet ?? s.max_bet_usd), daily_limit_usd: Number(dayLim ?? s.daily_limit_usd) } });
      if (Object.keys(x.errors || {}).length) return toast("Проверьте значения лимитов");
      toast("Готово! Система работает в пробном режиме");
      await refreshStatus();
      location.hash = "#/";
    } catch (x) {
      toast((x as Error).message);
    }
  }
</script>

<div class="ob">
  <div class="col">
    <p style="margin:0;font-weight:700;font-size:18px">WhaleFollow</p>
    <ol class="steps">
      {#each STEPS as name, i (i)}
        <li class:on={i + 1 === step} class:done={i + 1 < step} aria-current={i + 1 === step ? "step" : undefined}><span></span>{i + 1}. {name}</li>
      {/each}
    </ol>
    {#if step === 1}
      <h1 style="font-size:30px">Добро пожаловать</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2)">WhaleFollow следит за сильными игроками на Polymarket и предлагает повторить их ставку, только когда это ещё выгодно.</p>
      <div class="card col" style="gap:12px">
        <b style="font-size:16px">Как это работает</b>
        {#each ["Находим игроков, которые выигрывают стабильно, а не случайно", "Когда такой игрок делает ставку, проверяем цену и новости", "Предлагаем ставку с понятным расчётом — решение за вами"] as t, i (i)}
          <div class="row" style="flex-wrap:nowrap"><span class="num-dot">{i + 1}</span><span>{t}</span></div>
        {/each}
      </div>
      <form class="card" id="ob1" onsubmit={step1}>
        <fieldset>
          <legend>Перед началом подтвердите</legend>
          <label class="ck" style="align-items:flex-start">
            <input type="checkbox" name="j" required bind:checked={j} /><span>Торговля на Polymarket разрешена в моей стране<span class="sub" style="display:block">Правила различаются по странам — проверьте до реальных ставок</span></span>
          </label>
          <label class="ck"><input type="checkbox" name="r" required bind:checked={r} /><span>Я понимаю, что часть ставок проиграет и можно потерять деньги</span></label>
          <label class="ck" style="align-items:flex-start">
            <input type="checkbox" checked disabled /><span>Начну с пробного режима на виртуальных деньгах<span class="sub" style="display:block">В этой версии доступен только он</span></span>
          </label>
        </fieldset>
        <div class="row" style="margin-top:12px"><button class="btn p" style="min-width:160px">Далее</button><span class="sub">Шаг 1 из 3 · займёт около минуты</span></div>
      </form>
    {:else if !s}
      <div class="card skeleton">Загружаем настройки…</div>
    {:else if step === 2}
      <h1>Режим и сумма</h1>
      <form class="card col" id="ob2" onsubmit={step2}>
        <label class="opt">
          <input type="radio" checked disabled /><span><b>Пробный режим</b><span class="sub" style="display:block">Все ставки на виртуальные деньги. Так вы увидите, как работает система, без риска.</span></span>
        </label>
        <div class="fld">
          <label for="ob-bank">Виртуальная сумма для торговли, $</label>
          <input id="ob-bank" class="inp" type="number" min="100" step="100" value={bank ?? s.bankroll_usd} oninput={(e) => (bank = Number(e.currentTarget.value))} />
        </div>
        <label class="ck">
          <input type="checkbox" id="ob-confirm" checked={confirmEach ?? s.require_confirmation} onchange={(e) => (confirmEach = e.currentTarget.checked)} />Подтверждать каждую ставку вручную (рекомендуем)
        </label>
        <div class="row"><button class="btn p" style="min-width:160px">Далее</button><a class="link" href="#/welcome/1">Назад</a></div>
      </form>
    {:else}
      <h1>Лимиты и уведомления</h1>
      <form class="card col" id="ob3" onsubmit={step3}>
        <div class="fld">
          <label for="ob-max">Максимум на одну ставку, $</label>
          <input id="ob-max" class="inp" type="number" min="1" value={maxBet ?? s.max_bet_usd} oninput={(e) => (maxBet = Number(e.currentTarget.value))} />
        </div>
        <div class="fld">
          <label for="ob-day">Лимит в день, $</label>
          <input id="ob-day" class="inp" type="number" min="1" value={dayLim ?? s.daily_limit_usd} oninput={(e) => (dayLim = Number(e.currentTarget.value))} />
        </div>
        <div class="sub">Дополнительно система ограничивает ставку 2% от суммы и ставит паузу при убытке 5% за день или 10% за месяц.</div>
        <div class="row"><button class="btn p" style="min-width:160px">Начать</button><a class="link" href="#/welcome/2">Назад</a></div>
      </form>
    {/if}
  </div>
</div>
