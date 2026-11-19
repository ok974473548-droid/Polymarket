// Мост Android-приложения WhaleFollow (WebView): есть только внутри приложения.
// saveFile — сохранить файл в «Загрузки» (WebView не скачивает blob-ссылки), changeServer — сменить адрес сервера,
// info — версия приложения и как приходят уведомления.
export interface AndroidBridge {
  saveFile: (name: string, mime: string, text: string) => string;
  changeServer: () => void;
  info: () => string;
}

export function android(): AndroidBridge | null {
  return (window as unknown as { WFAndroid?: AndroidBridge }).WFAndroid || null;
}
