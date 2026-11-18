<script lang="ts">
  // Ротация китов по форме: кривые, «держится ли форма», правило «в форме»
  import Plot from "../components/Plot.svelte";
  import { api, type Json } from "../lib/api";
  import { compactMoney, dateLabel, signedPct, tone } from "../lib/format";
  import { toast } from "../lib/state.svelte";

  let r = $state<Json>(null);
  let err = $state("");
  let loading = $state(true);
  let rule = $state({ window_weeks: 8, min_bets: 5, min_result_usd: 0, dd_pct: 50 });

  function ruleQuery() {
    return {
      window_weeks: Number(rule.window_weeks),
      min_bets: Number(rule.min_bets),
      min_result_usd: Number(rule.min_result_usd),
      max_drawdown_share: Number(rule.dd_pct || 0) / 100,
    };
  }
  async function load(withRule = false) {
    loading = true;
    err = "";
    try {
      const qs = withRule ? "?" + new URLSearchParams(Object.entries(ruleQuery()).map(([k, v]) => [k, String(v)])).toString() : "";
      r = await api("/rotation" + qs);
      const ru = r.rule || {};
      rule = {
        window_weeks: ru.window_weeks ?? 8,
        min_bets: ru.min_bets ?? 5,
        min_result_usd: ru.min_result_usd ?? 0,
        dd_pct: Math.round((ru.max_drawdown_share ?? 0.5) * 100),
      };
    } catch (e) {
      err = (e as Error).message;
    } finally {
      loading = false;
    }
  }
  void load();

  async function save() {
    try {
      await api("/rotation/rule", { method: "PUT", body: ruleQuery() });
      toast("Правило сохранено");
      void load();
    } catch (e) {
      toast((e as Error).message);
    }
  }
  const pctOrDash = (x: number | null) => (x === null || x === undefined ? "—" : Math.round(x * 100) + "%");
  const narrow = typeof window !== "undefined" && window.innerWidth < 600;
</script>

{#snippet ruleForm()}
  <div class="card col" style="gap:12px">
    <h2>Правило «в форме»</h2>
    <div class="sub">Каждую неделю кит считается «в форме», если за последние недели окна выполнены все условия. Решение — только по прошлым данным.</div>
    <div class="rot-form">
      <label class="fld"><span>Окно, недель</span><input class="inp" type="number" min="2" max="52" id="rot-window" bind:value={rule.window_weeks} /></label>
      <label class="fld"><span>Ставок в окне, не меньше</span><input class="inp" type="number" min="0" id="rot-bets" bind:value={rule.min_bets} /></label>
      <label class="fld"><span>Результат за окно, $, не ниже</span><input class="inp" type="number" step="100" id="rot-result" bind:value={rule.min_result_usd} /></label>
      <label class="fld"><span>Просадка в окне, % заработанного, не больше</span><input class="inp" type="number" min="0" max="100" step="5" id="rot-dd" bind:value={rule.dd_pct} /></label>
    </div>
    <div class="row"><button class="btn" data-act="rot-try" onclick={() => load(true)}>Примерить</button><button class="btn p" data-act="rot-save" onclick={save}>Сохранить правило</button></div>
  </div>
{/snippet}

<a class="back" href="#/whales">← Киты</a>
<div class="page-head">
  <div>
    <h1>Ротация по форме</h1>
    <div class="lede">
      Каждую неделю копируем только тех китов из списка, кто «в форме», и проверяем главное: держится ли форма — помогает ли прошлое предсказать следующее.
    </div>
  </div>
</div>
{#if loading && !r}
  <div class="sub" style="padding:40px 0">Считаю ротацию по готовым кривым прибыли Polymarket…</div>
{:else if err}
  <div class="card err" role="alert">{err}</div>
{:else if r && !r.ok}
  <div class="card">
    <b>{r.error || "Нет данных"}</b>
    <div class="sub" style="margin-top:6px">Нужны киты в списке с историей дольше окна. {r.pool === 0 ? "Список китов пуст — добавьте игроков." : ""}</div>
  </div>
  {@render ruleForm()}
{:else if r}
  {@const v = r.verdict}
  {@const p = r.persistence}
  {@const t = r.total_pct}
  {@const c = r.curves}
  {@const h = r.hit_rate}
  <section class="statement" aria-label="Держится ли форма" style:opacity={loading ? 0.6 : 1}>
    <div>
      <div class="label">Держится ли форма</div>
      <div class="figure {v.holds === true ? 'up' : v.holds === false ? 'down' : ''}" style="font-size:34px">{v.title}</div>
      <div class="sub" style="margin-top:10px;max-width:60ch">{v.text}</div>
    </div>
    <div class="facts">
      <div class="fact">
        <div class="label">Связь прошлого и будущего</div>
        <div class="v" style="font-size:20px">{p.rho === null ? "—" : (p.rho > 0 ? "+" : "") + String(p.rho).replace(".", ",")}</div>
        <div class="sub">от −1 до +1; пар {p.pairs}</div>
      </div>
      <div class="fact">
        <div class="label">Вероятность случайности</div>
        <div class="v" style="font-size:20px">{p.p_value === null ? "—" : (p.p_value * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + "%"}</div>
      </div>
      <div class="fact">
        <div class="label">Неделя в плюсе: «в форме» / остальные</div>
        <div class="v" style="font-size:20px">{pctOrDash(h.in_form)} / {pctOrDash(h.not_in_form)}</div>
      </div>
      <div class="fact">
        <div class="label">Китов · недель</div>
        <div class="v" style="font-size:20px">{r.whales} · {r.weeks}</div>
        <div class="sub">{r.weeks_in_cash ? `${r.weeks_in_cash} нед. никто не был в форме` : ""}</div>
      </div>
    </div>
  </section>
  <div class="card col" style="gap:10px">
    <h2>Если бы копировали так</h2>
    <div class="sub">Доход на вложенный доллар, сумма по неделям (на вложенное ≈ 4 недели обычных покупок кита). Деньги делятся поровну между выбранными китами.</div>
    {#if c.ts.length > 1}
      {#key r}
        <Plot
          xs={c.ts}
          height={narrow ? 300 : 240}
          band={{ lo: c.random_p10, hi: c.random_p90, color: "#93A7AA" }}
          lines={[
            { label: "Идеал задним числом", values: c.ideal, color: "#E58C7A", dash: [5, 5], width: 1.6 },
            { label: "Случайный выбор", values: c.random_median, color: "#93A7AA", width: 1.6 },
            { label: "Копировать всех", values: c.all, color: "#86C9A8", width: 1.8 },
            { label: "Ротация", values: c.rotation, color: "#C8A464", width: 2.6 },
          ]}
          fmtY={(x) => signedPct(x)}
          label="Кривые ротации"
        />
      {/key}
    {:else}
      <div class="sub">Недостаточно данных для графика</div>
    {/if}
    <div class="rot-legend">
      {#each [["#C8A464", "Ротация по форме", t.rotation], ["#86C9A8", "Копировать всех", t.all], ["#93A7AA", "Случайный выбор (медиана)", t.random_median], ["#E58C7A", "Идеал задним числом — недостижимо", t.ideal]] as [col, name, val] (name)}
        <span><i style="background:{col}"></i>{name} <b class={tone(val as number)}>{signedPct(val as number)}</b></span>
      {/each}
    </div>
    <div class="sub">
      Серая полоса — разброс случайного выбора (80% розыгрышей). Идеал задним числом каждую неделю берёт лучших по уже известному итогу — так выбрать нельзя, это только потолок.
    </div>
    <div class="sub" style="color:var(--warn-ink)">
      Пул — киты из вашего списка сегодня. Если вы добавили их за хорошие результаты, все кривые выглядят лучше, чем было бы на самом деле. Сравнивайте кривые между собой, а не с нулём.
    </div>
  </div>
  <div class="grid2">
    <div class="card col" style="gap:6px">
      <h2>Кто в форме сейчас</h2>
      {#each r.current as w, i (i)}
        <div class="bet">
          <span class="row rot-name" style="gap:8px;flex-wrap:nowrap" title={w.name}><span class="dot" class:ok={w.in_form} aria-hidden="true"></span><span class="ell">{w.name}</span></span>
          <span class="sub" style="text-align:right">
            {#if w.in_form}<span class="up">в форме</span>{:else}{w.why}{/if}{w.gain_usd !== undefined ? ` · за окно ${compactMoney(w.gain_usd, { sign: true })}, ставок ${w.bets}` : ""}
          </span>
        </div>
      {:else}
        <div class="sub">Список китов пуст</div>
      {/each}
    </div>
    <div class="card col" style="gap:6px">
      <h2>Последние пересмотры</h2>
      {#each r.history.slice().reverse().slice(0, 12) as w, i (i)}
        <div class="bet">
          <span>{dateLabel(w.ts)}</span>
          <span class="sub">{w.n_in_form ? w.in_form.join(", ") : "никого — неделя в деньгах"} · ротация <b class={tone(w.rotation_pct)}>{signedPct(w.rotation_pct)}</b>, все {signedPct(w.all_pct)}</span>
        </div>
      {/each}
    </div>
  </div>
  {@render ruleForm()}
  <details>
    <summary class="link" style="cursor:pointer;display:inline-block">Как считается</summary>
    <div class="card col sub" style="gap:6px;margin-top:10px">
      <div>Кривые прибыли китов — готовые из Polymarket (накопленная прибыль по дням). Объём и число ставок — из сохранённых сделок.</div>
      <div>Пересмотр раз в неделю. «В форме» решается только по данным до момента пересмотра; результат выбора — следующая неделя.</div>
      <div>
        Доход недели = прибыль кита за неделю / вложенное, где вложенное ≈ 4 недели его обычных покупок (среднее за 12 недель). Крупные киты ставят рывками, поэтому так, а не по
        покупкам одной недели. Неделя кита ограничена ±100%: потерять больше вложенного нельзя.
      </div>
      <div>Недели, за которые у нас нет сделок кита, не учитываются.</div>
      <div>
        Вычитается наша комиссия Polymarket за повторённые покупки (мы всегда тейкер). Задержку входа и проскальзывание здесь не учитываем — их показывает «Что было бы» в профиле
        кита.
      </div>
      <div>
        Главная цифра — ранговая связь результата кита в прошлом окне и в следующем таком же окне (окна разных пар не пересекаются); вероятность случайности — проверкой
        перестановками.
      </div>
    </div>
  </details>
{/if}
