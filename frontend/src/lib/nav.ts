// Разделы приложения: меню (боковое на компьютере, нижнее на телефоне), заголовок верхней панели и палитра команд.
export interface NavItem {
  key: string; // data-nav
  label: string;
  hash: string;
  icon: string;
  routes: string[]; // какие экраны подсвечивают пункт
  primary?: boolean; // пункт нижнего меню на телефоне
  keys?: string; // горячая клавиша (подсказка)
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  {
    title: "Торговля",
    items: [
      { key: "signals", label: "Сигналы", hash: "#/", icon: "bolt", routes: ["dashboard", "signal", "journal"], primary: true, keys: "G S" },
      { key: "events", label: "События", hash: "#/events", icon: "globe", routes: ["events", "event"], primary: true },
      { key: "positions", label: "Мои ставки", hash: "#/positions", icon: "wallet", routes: ["positions", "bet"], primary: true, keys: "G B" },
    ],
  },
  {
    title: "Аналитика",
    items: [
      { key: "whales", label: "Киты", hash: "#/whales", icon: "whale", routes: ["whales", "whale", "add"], primary: true, keys: "G W" },
      { key: "rotation", label: "Ротация", hash: "#/rotation", icon: "rotate", routes: ["rotation"] },
      { key: "results", label: "Итоги", hash: "#/results", icon: "chart", routes: ["results", "trial"] },
      { key: "accuracy", label: "Точность модели", hash: "#/events-accuracy", icon: "target", routes: ["accuracy"] },
    ],
  },
  {
    title: "Система",
    items: [
      { key: "settings", label: "Настройки", hash: "#/settings", icon: "sliders", routes: ["settings"] },
      { key: "plugins", label: "Плагины", hash: "#/plugins", icon: "puzzle", routes: ["plugins"] },
      { key: "diagnostics", label: "Диагностика", hash: "#/diagnostics", icon: "pulse", routes: ["diagnostics"] },
    ],
  },
];

export const ALL_NAV: NavItem[] = NAV.flatMap((g) => g.items);

/** Заголовок экрана для верхней панели: раздел и (если глубже) сам экран. */
const SCREEN: Record<string, string> = {
  signal: "Сигнал",
  journal: "Журнал",
  event: "Событие",
  bet: "Ставка",
  whale: "Кит",
  add: "Новый игрок",
  trial: "Пробный режим",
};

export function crumbs(name: string): { section: NavItem | null; screen: string | null } {
  const section = ALL_NAV.find((i) => i.routes.includes(name)) || null;
  return { section, screen: SCREEN[name] || null };
}

/** SVG-пути иконок (24×24, обводка). */
export const ICONS: Record<string, string> = {
  bolt: "M13 2 4 14h7l-1 8 9-12h-7l1-8z",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3.6 9h16.8M3.6 15h16.8M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z",
  wallet: "M3 7.5A2.5 2.5 0 0 1 5.5 5H18a2 2 0 0 1 2 2v1M3 7.5V17a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1v-3M3 7.5A2.5 2.5 0 0 0 5.5 10H20a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-3a2 2 0 0 1 0-4",
  whale: "M3 13c0 4 3.6 6 8 6 5 0 9-3 10-8l-2.5 1.5L17 10c-1 2-3 3-6 3H3zM3 13c-.5-2 .2-3.6 1.6-4.5M7 16h.01M17 10l1-3m1.5 4.5 2-1.5",
  rotate: "M4 12a8 8 0 0 1 13.7-5.6L20 9M20 4v5h-5M20 12a8 8 0 0 1-13.7 5.6L4 15m0 5v-5h5",
  chart: "M4 20V10m6 10V4m6 16v-7m4 7H2",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zm0-4a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm0-4a1 1 0 1 0 0-2 1 1 0 0 0 0 2z",
  sliders: "M4 6h10m4 0h2M4 12h4m4 0h8M4 18h12m4 0h0M14 4v4M8 10v4M16 16v4",
  puzzle: "M10 4a2 2 0 1 1 4 0v1h4a1 1 0 0 1 1 1v4h-1a2 2 0 1 0 0 4h1v4a1 1 0 0 1-1 1h-4v-1a2 2 0 1 0-4 0v1H6a1 1 0 0 1-1-1v-4h1a2 2 0 1 0 0-4H5V6a1 1 0 0 1 1-1h4V4z",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm10 2-4.3-4.3",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  plus: "M12 5v14M5 12h14",
  keyboard: "M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7zm4 3h.01M11 10h.01M15 10h.01M7 14h10",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  collapse: "M15 6l-6 6 6 6",
  expand: "M9 6l6 6-6 6",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  close: "M6 6l12 12M18 6 6 18",
};
