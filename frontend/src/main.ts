// Шрифты — свои (без запросов к Google): кириллица и латиница, только нужные начертания.
import "@fontsource/golos-text/400.css";
import "@fontsource/golos-text/500.css";
import "@fontsource/golos-text/600.css";
import "@fontsource/golos-text/700.css";
import "@fontsource/cormorant-garamond/500.css";
import "@fontsource/cormorant-garamond/600.css";
import "./styles/app.css";
import { mount } from "svelte";
import App from "./App.svelte";

mount(App, { target: document.getElementById("root") as HTMLElement });

// PWA: установка на телефон и push-уведомления (сервис-воркер — только по HTTPS или на localhost)
if ("serviceWorker" in navigator && window.isSecureContext) {
  // нажатие на push-уведомление при открытом приложении: переход на нужный экран
  navigator.serviceWorker.addEventListener("message", (e) => {
    const go = e.data && e.data.go;
    if (typeof go === "string" && go.startsWith("/#/")) location.hash = go.slice(1);
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* без сервис-воркера всё работает, только без установки и push */
    });
  });
}
