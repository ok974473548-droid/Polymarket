// Главные сценарии: «диагностика», «добавить игрока», «решить по сигналу», «продать ставку».
// Демо-сервер: 4 сигнала ждут решения, одна ставка уже куплена, три кита на копировании.
import { expect, test, type Page } from "@playwright/test";

async function toast(page: Page, re: RegExp) {
  await expect(page.locator("#toast")).toHaveText(re);
}

test.describe.configure({ mode: "serial" });

test("первый экран: сигналы, меню и живое состояние @phone", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#pending-list .sig-row").first()).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  // меню разделов и индикатор системы
  await expect(page.locator("[data-nav]").first()).toBeVisible();
});

test("диагностика: сводка, процессы, данные и отчёт", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/#/settings");
  await page.getByRole("link", { name: /Диагностика/ }).click();
  await expect(page).toHaveURL(/#\/diagnostics$/);
  await expect(page.getByRole("heading", { name: "Диагностика" })).toBeVisible();
  await expect(page.locator(".statement .figure")).toHaveText("4 из 4");
  await expect(page.locator("#diag-services")).toContainText("работает");
  await expect(page.getByText("Сделок китов")).toBeVisible();
  await page.locator("[data-act=diag-copy]").click();
  await toast(page, /Отчёт скопирован|Выделено/);
  await page.getByText("Текст отчёта").click();
  await expect(page.locator("#diag-report")).not.toBeEmpty();
});

test("добавить игрока: поиск, карточка, добавление и ход проверки", async ({ page }) => {
  await page.goto("/#/whales");
  const whales = page.locator(".whales-grid.click");
  await expect(whales.first()).toBeVisible();
  const before = await whales.count();
  await page.getByRole("link", { name: "Добавить игрока" }).click();
  await expect(page).toHaveURL(/#\/whales\/add$/);
  // рейтинг Polymarket загрузился, отметка пачкой показывает панель выбора
  const rows = page.locator("#lb-ledger .lb-row[data-id]");
  await expect(rows.first()).toBeVisible();
  await page.locator("#lb-ledger [data-act=lb-pick]").nth(3).check(); // киты из списка отмечены ✓, без флажка
  await expect(page.locator("#bulk")).toBeVisible();
  await expect(page.locator("#bulk-n")).toHaveText("Выбрано: 1");
  await page.locator("[data-act=bulk-clear]").click();
  await expect(page.locator("#bulk")).toBeHidden();
  // поиск по имени → карточка игрока
  await page.locator("#finder-q").fill("whale_one");
  await page.getByRole("button", { name: "Найти" }).click();
  const card = page.locator("#finder-out .pcard");
  const pick = page.locator("[data-act=pshow]");
  await expect(card.or(pick.first())).toBeVisible(); // точное совпадение имени — сразу карточка, иначе список
  if (await pick.count()) await pick.first().click();
  await expect(card).toBeVisible();
  await expect(card).toContainText("Прибыль за всё время");
  await card.locator("[data-act=padd]").click();
  await toast(page, /Добавлено: 1/);
  // проверка идёт в фоне: видно ход проверки или её итог
  await expect(page.locator("#check-result")).toContainText(/Проверяем игрока|Результат проверки/, { timeout: 30_000 });
  await page.goto("/#/whales");
  await expect(whales).toHaveCount(before + 1);
});

test("решить по сигналу: карточка, покупка, переход к следующему", async ({ page }) => {
  await page.goto("/");
  const first = page.locator("#pending-list .sig-row").first();
  const before = await page.locator("#pending-list .sig-row").count();
  expect(before).toBeGreaterThan(1);
  await first.click();
  await expect(page).toHaveURL(/#\/signal\//);
  const url = page.url();
  // карточка «решение за 5 секунд»: цена, отсчёт времени, кнопки
  await expect(page.locator("[data-act=confirm]")).toBeVisible();
  await expect(page.locator("[data-act=skip]")).toBeVisible();
  await page.locator("[data-act=confirm]").click();
  await toast(page, /Куплено на \$[\d\s ,]+ по \d+¢/);
  await expect(page).not.toHaveURL(url); // следующий сигнал или список
  // следующий сигнал — пропустить горячей клавишей S
  if (/#\/signal\//.test(page.url())) {
    await expect(page.locator("[data-act=skip]")).toBeVisible(); // карточка следующего сигнала загрузилась
    await page.keyboard.press("s");
    await toast(page, /Сигнал пропущен/);
  }
  await page.goto("/#/positions/open");
  await expect(page.locator("a.ledger-row.bets-grid")).toHaveCount(2);
});

test("продать ставку: подтверждение и закрытая позиция", async ({ page }) => {
  await page.goto("/#/positions/open");
  const rows = page.locator("a.ledger-row.bets-grid");
  const n = await rows.count();
  expect(n).toBeGreaterThan(0);
  await rows.first().click();
  await expect(page).toHaveURL(/#\/bet\//);
  await expect(page.getByText("Если продать сейчас")).toBeVisible();
  await page.locator("[data-act=sell-bet]").click();
  const dlg = page.locator("dialog[open]");
  await expect(dlg).toContainText("Продать ставку сейчас?");
  await dlg.getByRole("button", { name: "Продать" }).click();
  await toast(page, /Продано за \$/);
  await page.goto("/#/positions/open");
  await expect(rows).toHaveCount(n - 1);
  await page.goto("/#/positions/closed");
  await expect(page.locator("a.ledger-row.bets-grid").first()).toBeVisible();
});

test("горячие клавиши и боковая панель игрока", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("?");
  await expect(page.locator("dialog[open]")).toContainText(/Горячие клавиши|клавиш/i);
  await page.keyboard.press("Escape");
  await page.keyboard.press("g");
  await page.keyboard.press("w");
  await expect(page).toHaveURL(/#\/whales$/);
  await page.goto("/#/whales/add");
  await page.locator("#lb-ledger .lb-row[data-id]").first().click();
  await expect(page.locator(".drawer.on")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".drawer.on")).toBeHidden();
});

test("плагины: состояние, переключатель и бэктест стратегии", async ({ page }) => {
  await page.goto("/#/settings");
  await page.getByRole("link", { name: /Плагины/ }).click();
  await expect(page).toHaveURL(/#\/plugins$/);
  for (const h of ["Источники данных", "Уведомления", "Стратегии"]) await expect(page.getByRole("heading", { name: h })).toBeVisible();
  const cons = page.locator('[data-plugin="consensus"]');
  await expect(cons).toContainText("выключен");
  await cons.locator("[data-act=plugin-toggle]").check();
  await expect(page.locator("#toast")).toHaveText(/«Консенсус китов» включён/);
  await expect(cons).toContainText(/включён|работает/);
  await cons.locator("[data-act=plugin-toggle]").uncheck();
  await expect(page.locator("#toast")).toHaveText(/выключен/);
  const copy = page.locator('[data-plugin="copy_whale"]');
  await copy.locator("[data-act=backtest]").click();
  await expect(copy).toContainText("Итог закрытых ставок", { timeout: 30_000 });
  await expect(copy).toContainText("Повторили бы");
  await expect(page.locator('[data-plugin="bookmakers"]')).toContainText(/недоступен|выключен/);
});

test("события: список по темам, страница события с живым графиком и покупка", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-nav="events"]').click();
  await expect(page).toHaveURL(/#\/events$/);
  await page.locator('[data-topic="politics"]').click();
  const card = page.locator('[data-event="9001"]');
  await expect(card).toContainText("Кто победит на выборах мэра?");
  await expect(page.locator('[data-event="9002"]')).toHaveCount(0);
  await card.click();
  await expect(page).toHaveURL(/#\/events\/9001$/);
  await expect(page.locator(".ev-legend .ev-leg")).toHaveCount(4);
  await expect(page.locator(".ev-row[data-outcome]")).toHaveCount(4);
  await expect(page.locator(".ev-row[data-outcome]").first()).toContainText("Анна Смирнова");
  // живые цены: страница сама опрашивает цены исходов
  await page.waitForRequest((r) => r.url().includes("/live?tokens="));
  await page.locator('[data-period="1d"]').click();
  await expect(page.locator('[data-period="1d"]')).toHaveAttribute("aria-pressed", "true");
  // стакан исхода
  await page.locator(".ev-row[data-outcome] .ev-name").first().click();
  await expect(page.locator(".ev-book")).toContainText("Продавцы");
  // виртуальная покупка
  await page.locator("[data-act=buy-yes]").first().click();
  const dlg = page.locator("dialog[open]");
  await expect(dlg).toContainText("Анна Смирнова");
  await dlg.locator("[data-act=buy-usd]").fill("5");
  await dlg.locator("[data-act=buy-confirm]").click();
  await expect(page.locator("#toast")).toHaveText(/Куплено .* на \$5/);
  await expect(page.getByRole("heading", { name: "Ваши ставки в этом событии" })).toBeVisible();
  await page.goto("/#/positions/open");
  await expect(page.locator("a.ledger-row.bets-grid", { hasText: "своя ставка по событию" })).toHaveCount(1);
});

test("досье события: сбор в фоне, прогнозы других площадок, данные и новости", async ({ page }) => {
  await page.goto("/#/events/9001");
  await page.locator("[data-tab=dossier]").click();
  const dz = page.locator("#dossier");
  await expect(dz).toBeVisible();
  await expect(dz).toContainText(/факт(а|ов)? из \d/, { timeout: 20000 });
  await expect(dz).toContainText("Другие площадки прогнозов");
  await expect(dz).toContainText("Демо-площадка");
  await expect(dz).toContainText("Явка на прошлых выборах");
  await expect(dz).toContainText("Новость 1");
  await expect(dz.locator(".dz-src")).toContainText("Демо-источник");
  await dz.locator("[data-act=dossier-refresh]").click();
  await expect(dz).toContainText(/факт(а|ов)? из \d/, { timeout: 20000 });
});

test("модель события: оценка по исходам, лестница оценок, режим без ИИ", async ({ page }) => {
  await page.goto("/#/events/9001");
  const panel = page.locator("[data-model-panel]");
  await expect(page.locator("[data-tab=model]")).toHaveAttribute("aria-pressed", "true");
  // досье собирается само, оценка обновляется: прогноз демо-площадки 33% к каждому кандидату
  await expect(panel.locator("[data-model-outcome]")).toHaveCount(4, { timeout: 20000 });
  await expect(page.locator(".ev-row [data-model]").first()).toContainText(/модель \d/, { timeout: 20000 });
  const first = panel.locator("[data-model-outcome]").first();
  await first.locator("summary").click();
  await expect(first).toContainText("Другие площадки прогнозов");
  await expect(first).toContainText("Цена Polymarket сейчас");
  await expect(first).toContainText(/80% интервал/);
  await expect(panel.locator("[data-mode=manual]")).toHaveAttribute("aria-pressed", "true");
  await expect(panel.locator("[data-mode=ai]")).toBeDisabled(); // в демо нет ключа ИИ
});

test("советник: найти событие → совет → поставить по совету → продать", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-nav="events"]').click();
  await page.locator('[data-topic="politics"]').click();
  await page.locator('[data-event="9001"]').click();
  const card = page.locator("[data-advice]");
  // досье собирается само, модель и совет обновляются: две площадки дают «Вере Ли» 45% при цене 18%
  await expect(card).toContainText("Купить «Да» в «Вера Ли»", { timeout: 20000 });
  await expect(card).toContainText("Если сбудется");
  await expect(card).toContainText("Если нет");
  // «Следить»: уведомление, когда появится выгода
  await card.locator("[data-act=watch]").click();
  await toast(page, /Слежу/);
  await expect(card.locator("[data-act=watch]")).toHaveAttribute("aria-pressed", "true");
  // поставить по совету: сумма и предельная цена подставлены
  await card.locator("[data-act=advice-buy]").click();
  const dlg = page.locator("dialog[open]");
  await expect(dlg).toContainText("По совету модели");
  await expect(dlg).toContainText("Вера Ли");
  await dlg.locator("[data-act=buy-confirm]").click();
  await toast(page, /Куплено .* акций/);
  // в «Моих ставках» — отдельно от ставок за китами
  await page.goto("/#/positions/open");
  const row = page.locator("a.ledger-row.bets-grid", { hasText: "по совету модели" });
  await expect(row).toHaveCount(1);
  await row.click();
  await page.locator("[data-act=sell-bet]").click();
  await page.locator("dialog[open]").getByRole("button", { name: "Продать" }).click();
  await toast(page, /Продано за \$/);
  await page.goto("/#/results");
  await expect(page.locator("[data-results-events]")).toContainText("По совету модели");
  // в списке событий — «Слежу»
  await page.goto("/#/events");
  await expect(page.locator("[data-watched]")).toContainText("Кто победит на выборах мэра?");
});

test("точность модели событий: модель против рынка на закрытых событиях", async ({ page }) => {
  await page.goto("/#/events");
  await page.locator("[data-nav-acc]").click();
  await expect(page).toHaveURL(/#\/events-accuracy$/);
  const m = page.locator("[data-acc-mode=manual]");
  await expect(m).toContainText("Без ИИ");
  await expect(m).toContainText("Brier модели");
  await expect(m.locator("[data-verdict]")).toHaveText(/Лучше рынка|Пока не лучше рынка|Хуже рынка|Мало данных/);
  await expect(m).toContainText("Калибровка");
  const p = page.locator("[data-acc-params]");
  await expect(p).toContainText("λ сейчас");
  await expect(p).toContainText("по 80 закрытым исходам");
  await expect(p.locator("[data-est=platforms]")).toBeVisible();
});
