<script lang="ts">
  // Настройки: режим, ИИ, лимиты, сбор данных, темы, источники, уведомления (в т.ч. push), защита
  import PushToggle from "../components/PushToggle.svelte";
  import { api, type Json } from "../lib/api";
  import { confirmDialog } from "../lib/dialog.svelte";
  import { ago, days, fmtNum, plural, TOPICS } from "../lib/format";
  import { go } from "../lib/router.svelte";
  import { toast } from "../lib/state.svelte";

  // загружается один раз: фоновое обновление не должно затирать несохранённые правки
  let s = $state<Json>(null);
  let err = $state("");
  let usage = $state<Json>(null);
  let usageErr = $state("");
  let saving = $state(false);
  api("/settings")
    .then((x) => (s = x))
    .catch((e: Error) => (err = e.message));
  async function loadUsage() {
    try {
      usage = await api("/data");
    } catch (e) {
      usageErr = (e as Error).message;
    }
  }
  void loadUsage();

  const CATS: [string, string][] = [...TOPICS, ["other", "Разное (тема не указана)"]];
  const WHERE: [string, string][] = [["telegram", "Telegram"], ["push", "Push"], ["discord", "Discord"], ["email", "Email"]];
  const WHAT: [string, string][] = [["signals", "Новые сигналы"], ["whale_exit", "Кит вышел из позиции"], ["limits", "Лимиты и паузы"], ["weekly", "Итоги недели"], ["system", "Сбои сбора данных"]];
  const MONEY: [string, string, string][] = [
    ["bankroll_usd", "Сумма для торговли", "$"],
    ["max_bet_usd", "Максимум на одну ставку", "$"],
    ["daily_limit_usd", "Лимит в день", "$"],
    ["daily_loss_stop_frac", "Пауза при убытке за день", "%"],
    ["monthly_loss_stop_frac", "Пауза при убытке за месяц", "%"],
    ["events_max_bet_usd", "«События»: максимум на одну ставку", "$"],
    ["events_daily_limit_usd", "«События»: лимит в день", "$"],
  ];
  const src = (k: string, def = true) => {
    const v = (s.news_sources || {})[k];
    return def ? v !== false : !!v;
  };

  async function save(e: SubmitEvent) {
    e.preventDefault();
    const form = e.currentTarget as HTMLFormElement;
    const f = new FormData(form);
    const v = (id: string) => Number((form.querySelector("#" + id) as HTMLInputElement).value);
    const notify: Record<string, boolean> = {};
    for (const [k] of [...WHERE, ...WHAT]) notify[k] = f.has("n_" + k);
    const patch = {
      mode: f.get("mode") || "paper",
      require_confirmation: f.has("require_confirmation"),
      bankroll_usd: v("bankroll_usd"),
      max_bet_usd: v("max_bet_usd"),
      daily_limit_usd: v("daily_limit_usd"),
      events_max_bet_usd: v("events_max_bet_usd"),
      events_daily_limit_usd: v("events_daily_limit_usd"),
      events_ai_daily_limit: v("events_ai_daily_limit"),
      daily_loss_stop_frac: v("daily_loss_stop_frac") / 100,
      monthly_loss_stop_frac: v("monthly_loss_stop_frac") / 100,
      categories: f.getAll("cat"),
      notify,
      ai_mode: f.get("ai_mode") || "manual",
      news_sources: {
        google_news: f.has("src_google_news"),
        manifold: f.has("src_manifold"),
        reddit: f.has("src_reddit"),
        kalshi: f.has("src_kalshi"),
        // без ключа флажок неактивен — сохраняем «включено», чтобы источник заработал сразу после добавления ключа
        bookmakers: f.has("src_bookmakers") || Boolean((form.querySelector("[name=src_bookmakers]") as HTMLInputElement | null)?.disabled),
      },
      telegram_channels: String(f.get("telegram_channels") || "")
        .split(/[\s,;]+/)
        .filter(Boolean),
      auto_discover: f.has("auto_discover"),
    };
    saving = true;
    try {
      const r = await api("/settings", { method: "PUT", body: patch });
      const errs = Object.entries(r.errors || {});
      toast(errs.length ? "Не сохранено: " + errs.map(([k, x]) => `${k} — ${x}`).join("; ") : "Настройки сохранены");
    } catch (x) {
      toast((x as Error).message);
    } finally {
      saving = false;
    }
  }

  async function purge() {
    const ok = await confirmDialog({
      title: "Удалить данные по другим кошелькам?",
      ok: "Удалить",
      danger: true,
      text: "Будут удалены сделки, позиции и оценки игроков, которых нет в вашем списке китов. Данные китов из списка и ваши ставки останутся. Отменить нельзя.",
    });
    if (!ok) return;
    try {
      const r = await api("/data/purge", { method: "POST" });
      toast(`Удалено сделок: ${fmtNum((r.removed || {}).trades || 0)}`);
      void loadUsage();
    } catch (e) {
      toast((e as Error).message);
    }
  }
</script>

{#snippet secRow(ok: boolean, title: string, text: string)}
  <div class="check-row"><span class="chip {ok ? 'g' : 'a'}">{ok ? "Да" : "Нет"}</span><div><b>{title}</b><div class="sub">{text}</div></div></div>
{/snippet}

{#if err}
  <div class="card err" role="alert"><b>Не удалось загрузить экран</b><div>{err}</div></div>
{:else if !s}
  <div class="card skeleton">Загружаем настройки…</div>
{:else}
  {@const n = s.notify || {}}
  {@const integ = s.integrations || {}}
  <form id="settings-form" class="col" onsubmit={save}>
    <div class="top">
      <h1>Настройки</h1>
      <div class="row"><button type="button" class="btn" onclick={() => go("#/settings")}>Отменить</button><button class="btn p" disabled={saving}>Сохранить</button></div>
    </div>
    <div class="grid2">
      <div class="col">
        <div class="card">
          <fieldset>
            <legend>Режим работы</legend>
            <label class="opt"><input type="radio" name="mode" value="paper" checked={s.mode === "paper"} /><span><b>Пробный</b><span class="sub" style="display:block">Ставки на виртуальные деньги — безопасно проверить систему</span></span></label>
            <label class="opt disabled">
              <input type="radio" name="mode" value="manual" disabled />
              <span><b style="color:var(--muted)">Ручной</b><span class="sub" style="display:block">Реальные ставки с подтверждением. Отключено в этой версии: сначала пробный режим и юридическая проверка</span></span>
            </label>
            <label class="opt disabled">
              <input type="radio" name="mode" value="auto" disabled />
              <span>
                <b style="color:var(--muted)">Автоматический</b>
                <span class="sub" style="display:block">Откроется после 2 месяцев пробного режима с хорошим результатом{s.auto_days_left ? ` · осталось ${days(s.auto_days_left)}` : ""}</span>
              </span>
            </label>
            <label class="ck"><input type="checkbox" name="require_confirmation" checked={s.require_confirmation} />Подтверждать каждую ставку вручную</label>
          </fieldset>
        </div>
        <div class="card">
          <fieldset>
            <legend>Кто принимает решение</legend>
            <label class="opt" class:disabled={!s.ai.available}>
              <input type="radio" name="ai_mode" value="ai" checked={s.ai_mode === "ai"} disabled={!s.ai.available} />
              <span>
                <b style={s.ai.available ? "" : "color:var(--muted)"}>С ИИ</b>
                <span class="sub" style="display:block">
                  {s.ai.available
                    ? "Claude оценивает вероятность по новостям вслепую — без цены рынка и ставки кита"
                    : "ИИ не подключён: добавьте ANTHROPIC_API_KEY в файл .env и перезапустите сервис"}
                </span>
              </span>
            </label>
            <label class="opt">
              <input type="radio" name="ai_mode" value="manual" checked={s.ai_mode === "manual" || !s.ai.available} />
              <span>
                <b>Без ИИ</b>
                <span class="sub" style="display:block">Данные собираются и анализируются так же, но каждую ставку решаете вы — система показывает историю кита, цены, другие рынки и новости. В «Событиях» вероятность считает аналитическая модель</span>
              </span>
            </label>
            <div class="fld">
              <label for="events_ai_daily_limit">«События»: анализов с ИИ в день</label>
              <input id="events_ai_daily_limit" class="inp" type="number" min="0" step="1" value={s.events_ai_daily_limit} />
            </div>
          </fieldset>
        </div>
        <div class="card">
          <fieldset>
            <legend>Деньги и лимиты</legend>
            {#each MONEY as [id, label, suffix] (id)}
              <div class="fld">
                <label for={id}>{label}, {suffix}</label>
                <input {id} class="inp" type="number" min="0" step="1" value={suffix === "%" ? Math.round(s[id] * 100) : s[id]} />
              </div>
            {/each}
            <div class="sub">Система никогда не превысит эти суммы, даже если сигнал очень выгодный. Ставки со страницы «События» — из того же банка, с отдельным лимитом; общие лимиты и паузы действуют и на них.</div>
          </fieldset>
        </div>
        <div class="card">
          <fieldset>
            <legend>Сбор данных</legend>
            <label class="opt">
              <input type="checkbox" name="auto_discover" checked={s.auto_discover} />
              <span>
                <b>Искать новых китов автоматически</b>
                <span class="sub" style="display:block">
                  Выключено — данные собираются только по китам из вашего списка. Включено — система просматривает всю ленту Polymarket и сама предлагает кандидатов: больше трафика и места на диске.
                </span>
              </span>
            </label>
            <div id="data-usage" class="sub">
              {#if usageErr}{usageErr}{:else if !usage}Считаем объём данных…{:else}
                <div>
                  Китов в списке: {usage.tracked_wallets ? fmtNum(usage.tracked_wallets) + " " + plural(usage.tracked_wallets, "кошелёк", "кошелька", "кошельков") : "нет"}; сделок по ним
                  {fmtNum(usage.trades_tracked)}.{usage.db_size_mb ? ` База занимает ${String(usage.db_size_mb).replace(".", ",")} МБ.` : ""}
                </div>
                {#if usage.trades_other}
                  <div class="row" style="margin-top:10px">
                    <span>Данные по другим кошелькам: {fmtNum(usage.trades_other)} сделок ({fmtNum(usage.other_wallets)} {plural(usage.other_wallets, "кошелёк", "кошелька", "кошельков")}).</span>
                    <button type="button" class="btn danger" data-act="purge" onclick={purge}>Удалить данные по другим кошелькам</button>
                  </div>
                {:else}
                  <div style="margin-top:6px">Лишних данных нет.</div>
                {/if}
              {/if}
            </div>
          </fieldset>
        </div>
      </div>
      <div class="col">
        <div class="card">
          <fieldset>
            <legend>Темы</legend>
            <div class="grid2" style="gap:0 12px">
              {#each CATS as [k, l] (k)}<label class="ck"><input type="checkbox" name="cat" value={k} checked={(s.categories || []).includes(k)} />{l}</label>{/each}
            </div>
            <div class="sub">Сигналы по выключенным темам не приходят.</div>
          </fieldset>
        </div>
        <div class="card">
          <fieldset>
            <legend>Источники данных</legend>
            <label class="ck"><input type="checkbox" name="src_google_news" checked={src("google_news")} /><span>Новости (Google News)<span class="sub" style="display:block">Поиск свежих статей по вопросу рынка</span></span></label>
            <label class="ck"><input type="checkbox" name="src_manifold" checked={src("manifold")} /><span>Другие рынки прогнозов (Manifold)<span class="sub" style="display:block">Вероятности по похожим вопросам</span></span></label>
            <label class="ck"><input type="checkbox" name="src_kalshi" checked={src("kalshi")} /><span>Kalshi<span class="sub" style="display:block">Цены по похожим вопросам на второй крупной бирже прогнозов</span></span></label>
            <label class="ck">
              <input type="checkbox" name="src_bookmakers" checked={src("bookmakers")} disabled={!integ.odds_api} />
              <span>Букмекеры (спорт)<span class="sub" style="display:block">{integ.odds_api ? "Вероятности по коэффициентам без маржи букмекера" : "Нужен ключ WF_ODDS_API_KEY в .env (the-odds-api.com)"}</span></span>
            </label>
            <label class="ck"><input type="checkbox" name="src_reddit" checked={src("reddit", false)} /><span>Соцсети (Reddit)<span class="sub" style="display:block">Обсуждения за неделю — как повод проверить, не как доказательство</span></span></label>
            <label for="tg-channels" style="font-weight:600;margin-top:6px">Telegram-каналы</label>
            <textarea
              id="tg-channels"
              name="telegram_channels"
              class="inp"
              rows="3"
              style="height:auto;padding:10px 12px;font-weight:500;resize:vertical"
              placeholder="@channel1, https://t.me/channel2"
              value={(s.telegram_channels || []).map((c: string) => "@" + c).join(", ")}
            ></textarea>
            <div class="sub">Только публичные каналы. Сообщения помечаются как непроверенные и служат поводом проверить новость.</div>
            <div class="sub" style="margin-top:4px">
              <b>Блокчейн (поиск связанных кошельков):</b>
              {s.chain.available
                ? `подключён (${s.chain.provider}) · проверено кошельков: ${s.chain.wallets_checked}, переводов: ${s.chain.transfers}${s.chain.errors ? `, ошибок: ${s.chain.errors}` : ""}`
                : "не подключён — добавьте WF_ETHERSCAN_API_KEY в .env (см. README)"}
            </div>
          </fieldset>
        </div>
        <div class="card">
          <div style="font-size:16px;font-weight:700;color:var(--ink-2);margin-bottom:10px">Уведомления</div>
          <div class="grid2" style="gap:0 12px">
            <fieldset style="gap:0">
              <legend class="sub" style="font-size:13px;margin-bottom:2px">Куда</legend>
              {#each WHERE as [k, l] (k)}<label class="ck"><input type="checkbox" name="n_{k}" checked={!!n[k]} />{l}</label>{/each}
            </fieldset>
            <fieldset style="gap:0">
              <legend class="sub" style="font-size:13px;margin-bottom:2px">О чём</legend>
              {#each WHAT as [k, l] (k)}<label class="ck"><input type="checkbox" name="n_{k}" checked={!!n[k]} />{l}</label>{/each}
            </fieldset>
          </div>
          <div class="sub">
            {integ.telegram
              ? "Telegram подключён: под сигналами — кнопки «Купить» и «Пропустить», команды /status, /stop, /resume."
              : "Telegram работает, если в файле .env указан бот (см. README)."} Discord и почта — по ключам в .env (см. «Плагины»); все уведомления видны в колокольчике.
          </div>
          <PushToggle />
        </div>
        <div class="card col" style="gap:6px">
          <div style="font-size:16px;font-weight:700;color:var(--ink-2)">Защита и сохранность</div>
          {@render secRow(
            !!integ.password,
            "Пароль на интерфейс",
            integ.password
              ? "Вход по паролю (WF_UI_PASSWORD)."
              : "Не задан. Пока интерфейс открыт только на этом компьютере, это допустимо; для доступа с телефона задайте WF_UI_PASSWORD в .env.",
          )}
          {@render secRow(
            !!integ.backup_ok,
            "Резервные копии базы",
            integ.backup_ok
              ? `Последняя ${ago(integ.backup_ago_s)}; хранятся 7 последних.`
              : integ.backup_ago_s === null || integ.backup_ago_s === undefined
                ? "Первая копия появится в течение суток после запуска."
                : "Последняя копия не удалась — см. «Пробный режим» → «Сбор данных».",
          )}
        </div>
        <a class="card item" style="text-decoration:none;color:inherit" href="#/plugins">
          <div class="grow"><b>Плагины</b><div class="sub">Источники данных, каналы уведомлений и стратегии: включить, выключить, состояние, ошибки, бэктест стратегий.</div></div>
          <span class="btn">Открыть</span>
        </a>
        <a class="card item" style="text-decoration:none;color:inherit" href="#/diagnostics">
          <div class="grow"><b>Диагностика</b><div class="sub">Проверить доступ к Polymarket и другим источникам, посмотреть ошибки и скопировать отчёт.</div></div>
          <span class="btn">Открыть</span>
        </a>
        <div class="card item">
          <div class="grow"><b>Кошелёк Polymarket</b><div class="sub">Не подключён. Нужен только для реальных ставок — в этой версии они отключены.</div></div>
          <button type="button" class="btn" disabled>Подключить</button>
        </div>
      </div>
    </div>
  </form>
{/if}
