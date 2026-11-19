// Service worker WhaleFollow: push-уведомления и быстрый запуск установленного приложения.
// Кэш: файлы сборки /assets/* (их имена меняются с каждой версией) и оболочка index.html на случай
// плохой связи. Данные /api/* никогда не кэшируются — на экране всегда свежие цифры.
const SHELL = "wf-shell-v2";
const ASSETS = "wf-assets-v2";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(
    (async () => {
      const keep = new Set([SHELL, ASSETS]);
      for (const k of await caches.keys()) if (!keep.has(k)) await caches.delete(k);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (url.pathname.startsWith("/assets/")) {
    // имя файла содержит хэш — содержимое не меняется, берём из кэша
    e.respondWith(
      caches.open(ASSETS).then(async (c) => {
        const hit = await c.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) c.put(req, res.clone());
        return res;
      }),
    );
    return;
  }
  if (req.mode === "navigate") {
    // оболочка: сначала сеть (новая версия), без сети — последняя сохранённая
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) caches.open(SHELL).then((c) => c.put("/", res.clone()));
          return res;
        })
        .catch(() => caches.match("/").then((r) => r || Response.error())),
    );
  }
});

// ---------------------------------------------------------------- push
self.addEventListener("push", (e) => {
  let m = {};
  try {
    m = e.data ? e.data.json() : {};
  } catch {
    m = { title: "WhaleFollow", body: e.data ? e.data.text() : "" };
  }
  const opts = {
    body: m.body || "",
    tag: m.tag || undefined,
    renotify: !!m.tag,
    icon: "/icon-192.png",
    badge: "/badge-72.png",
    data: { url: m.url || "/#/", signal: m.signal || null },
    requireInteraction: m.kind === "signal",
  };
  if (m.signal) {
    opts.actions = [
      { action: "confirm", title: "Купить" },
      { action: "skip", title: "Пропустить" },
    ];
  }
  e.waitUntil(self.registration.showNotification(m.title || "WhaleFollow", opts));
});

async function openApp(url) {
  const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of all) {
    if (new URL(c.url).origin === location.origin) {
      await c.focus();
      if ("navigate" in c) return c.navigate(url).catch(() => c.postMessage({ go: url }));
      return c.postMessage({ go: url });
    }
  }
  return self.clients.openWindow(url);
}

async function decide(id, action) {
  // решение прямо из уведомления: тот же запрос, что и кнопка на экране (cookie входа — от браузера)
  try {
    const r = await fetch(`/api/signals/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) return openApp(`/#/signal/${encodeURIComponent(id)}`);
    const text =
      action === "skip"
        ? "Сигнал пропущен"
        : j.status === "executed"
          ? `Куплено на $${Math.round(j.usd)} по ${Math.round(j.avg_price * 100)}¢`
          : j.error || "Не выполнено — откройте сигнал";
    return self.registration.showNotification("WhaleFollow", { body: text, tag: "decision:" + id, icon: "/icon-192.png", data: { url: `/#/signal/${encodeURIComponent(id)}` } });
  } catch {
    return openApp(`/#/signal/${encodeURIComponent(id)}`);
  }
}

self.addEventListener("notificationclick", (e) => {
  const d = e.notification.data || {};
  e.notification.close();
  if (d.signal && (e.action === "confirm" || e.action === "skip")) {
    e.waitUntil(decide(d.signal, e.action));
    return;
  }
  e.waitUntil(openApp(d.url || "/#/"));
});
