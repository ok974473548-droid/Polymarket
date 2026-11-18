// Клиент API: изменяющие запросы — всегда JSON (сервер отклоняет другие — защита от подделки запросов).
import { app } from "./state.svelte";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// Ответы API описаны моделями pydantic на сервере (/docs); здесь — свободная форма.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = any;

export async function api(path: string, { method = "GET", body }: { method?: string; body?: unknown } = {}): Promise<Json> {
  const res = await fetch("/api" + path, {
    method,
    credentials: "same-origin",
    headers: method !== "GET" ? { "Content-Type": "application/json" } : {},
    body: method !== "GET" ? JSON.stringify(body || {}) : undefined,
  });
  let data: Json = null;
  try {
    data = await res.json();
  } catch {
    /* пусто */
  }
  if (res.status === 401 && data && data.auth && !path.startsWith("/login")) {
    app.locked = true;
    throw new ApiError("Нужен вход", 401);
  }
  if (!res.ok) throw new ApiError((data && data.error) || `Ошибка ${res.status}`, res.status);
  return data;
}
