// Push-уведомления (Web Push): подписка этого устройства через service worker.
// Ключ VAPID выдаёт сервер; подписка хранится в базе и используется процессом realtime.
import { api } from "./api";

export function pushSupported(): string | null {
  if (!("serviceWorker" in navigator)) return "Этот браузер не поддерживает уведомления от сайтов.";
  if (!window.isSecureContext) return "Push работает только по HTTPS (или на этом компьютере через localhost).";
  if (!("PushManager" in window)) {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
    return ios
      ? "На iPhone push работает после установки: «Поделиться» → «На экран Домой», затем откройте приложение с экрана."
      : "Этот браузер не поддерживает push-уведомления.";
  }
  return null;
}

function keyBytes(b64: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function registration(): Promise<ServiceWorkerRegistration> {
  const reg = (await navigator.serviceWorker.getRegistration("/")) || (await navigator.serviceWorker.register("/sw.js"));
  return navigator.serviceWorker.ready.then(() => reg);
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

export async function subscribe(): Promise<{ ok: boolean; error?: string }> {
  const why = pushSupported();
  if (why) return { ok: false, error: why };
  const k = await api("/push/key");
  if (!k.ok) return { ok: false, error: k.error || "Сервер не выдал ключ для push" };
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return { ok: false, error: "Уведомления запрещены в браузере — разрешите их в настройках сайта." };
  const reg = await registration();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(k.key) });
  const r = await api("/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label: navigator.userAgent.slice(0, 120) } });
  return r.ok ? { ok: true } : { ok: false, error: r.error };
}

export async function unsubscribe(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await api("/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } }).catch(() => null);
  await sub.unsubscribe();
}
