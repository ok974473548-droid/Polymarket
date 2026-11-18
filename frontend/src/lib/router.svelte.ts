// Роутер по hash (#/...): адреса — как в прежнем интерфейсе, ссылки из Telegram и уведомлений работают.
export interface Route {
  name: string;
  nav: string | null; // раздел меню; null — без меню (первый запуск)
  params: string[];
}

const ROUTES: [RegExp, string, string | null][] = [
  [/^#?\/?$/, "dashboard", "signals"],
  [/^#\/signal\/([^/]+)$/, "signal", "signals"],
  [/^#\/journal$/, "journal", "signals"],
  [/^#\/positions(?:\/(open|closed))?$/, "positions", "positions"],
  [/^#\/bet\/([^/]+)$/, "bet", "positions"],
  [/^#\/whales$/, "whales", "whales"],
  [/^#\/whales\/add(?:\?q=(.*))?$/, "add", "whales"],
  [/^#\/rotation$/, "rotation", "whales"],
  [/^#\/whales\/([^/]+)$/, "whale", "whales"],
  [/^#\/results(?:\/(week|month|all))?$/, "results", "results"],
  [/^#\/settings$/, "settings", "settings"],
  [/^#\/diagnostics$/, "diagnostics", "settings"],
  [/^#\/plugins$/, "plugins", "settings"],
  [/^#\/trial$/, "trial", "results"],
  [/^#\/welcome(?:\/(\d))?$/, "welcome", null],
];

export function match(hash: string): Route {
  for (const [re, name, nav] of ROUTES) {
    const m = hash.match(re);
    if (m) return { name, nav, params: m.slice(1).map((x) => (x === undefined ? "" : decodeURIComponent(x))) };
  }
  return { name: "notfound", nav: null, params: [] };
}

export const route = $state({ hash: location.hash || "#/", seq: 0 });

window.addEventListener("hashchange", () => {
  route.hash = location.hash || "#/";
  route.seq++;
});

export function go(hash: string): void {
  if (location.hash === hash) route.seq++;
  else location.hash = hash;
}
