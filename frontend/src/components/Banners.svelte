<script lang="ts">
  import { api } from "../lib/api";
  import { app, softRefresh, toast } from "../lib/state.svelte";

  const st = $derived(app.status);
  const b = $derived((st && st.bootstrap) || {});

  async function resume() {
    try {
      await api("/resume", { method: "POST" });
      toast("Работа возобновлена");
      softRefresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#if app.statusError || !st}
  {#if app.statusError}
    <div class="banner bad" role="alert">
      <div class="grow"><b>Нет связи с сервером</b><div>Проверьте, что сервер запущен. Открытые ставки в безопасности.</div></div>
    </div>
  {/if}
{:else}
  {#if st.stopped}
    <div class="banner gray" role="alert">
      <div class="grow"><b>Система остановлена</b><div>Новые ставки не создаются. Открытые остаются до закрытия рынков.</div></div>
      <button class="btn" onclick={resume}>Возобновить</button>
    </div>
  {:else if st.paused}
    <div class="banner bad" role="alert">
      <div class="grow">
        <b>Система на паузе</b>
        <div>
          {st.paused_text || "Сработал защитный лимит."}
          {st.paused === "daily_loss_stop" ? "Работа продолжится завтра автоматически." : "Нужна ручная проверка."}
        </div>
      </div>
      <a class="btn" href="#/positions">Посмотреть ставки</a>
    </div>
  {/if}
  {#if !b.auto_discover && b.tracked === 0}
    <div class="banner info">
      <div class="grow">
        <b>Добавьте первого кита</b>
        <div>Система собирает данные только по игрокам из вашего списка. Пока список пуст, она ничего не загружает.</div>
      </div>
      <a class="btn p" href="#/whales/add">Добавить кита</a>
    </div>
  {:else if !b.auto_discover}
    <!-- данные только по списку — баннер первой загрузки не нужен -->
  {:else if b.running && !b.scored}
    <div class="banner info">
      <div class="grow">
        <b>Идёт первая загрузка данных Polymarket</b>
        <div>
          Найдено кандидатов: {b.candidates}, загружено сделок: {(b.trades || 0).toLocaleString("ru-RU")}. Список китов появится после
          первого пересчёта — обычно в течение часа.
        </div>
      </div>
    </div>
  {:else if b.scored && !b.whales}
    <div class="banner gray">
      <div class="grow">
        <b>Подходящих китов пока нет</b>
        <div>
          Проверено кандидатов: {b.candidates}. Требования строгие — система продолжает искать каждые 15 минут и пересчитывает раз в
          сутки. Можно добавить кита вручную.
        </div>
      </div>
      <a class="btn" href="#/whales/add">Добавить кита</a>
    </div>
  {:else if !b.running}
    <div class="banner gray">
      <div class="grow">
        <b>Сбор данных не запущен</b>
        <div>Запустите <code>docker compose up -d</code> или <code>whalefollow run</code> — иначе новые сигналы не появятся.</div>
      </div>
    </div>
  {/if}
  {#if b.error}
    <div class="banner warn">
      <div class="grow">
        <b>Ошибка при загрузке данных</b>
        <div>{b.error}</div>
        <div class="sub">Проверьте доступ: <code>docker compose run --rm api whalefollow check</code></div>
      </div>
    </div>
  {/if}
  {#if st.data_age_s != null && st.data_age_s > 120}
    <div class="banner gray">
      <div class="grow">
        <b>Нет связи с Polymarket уже {Math.floor(st.data_age_s / 60)} мин</b>
        <div>Новые сигналы не появятся, пока связь не вернётся. Открытые ставки в безопасности.</div>
      </div>
      <span class="chip n">Переподключаемся…</span>
    </div>
  {/if}
{/if}
