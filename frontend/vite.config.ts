import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// Сборка кладётся в пакет Python (whalefollow/web): сервер FastAPI раздаёт её как статику.
// В режиме разработки (npm run dev) запросы /api уходят на запущенный whalefollow (порт 8787).
export default defineConfig({
  plugins: [svelte()],
  base: "/",
  build: {
    outDir: "../whalefollow/web",
    emptyOutDir: true,
    target: "es2022",
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 400,
    rollupOptions: {
      output: {
        // графики — отдельными частями: первый экран (сигналы) их не грузит
        manualChunks: (id) =>
          id.includes("lightweight-charts") || id.includes("fancy-canvas") ? "charts" : id.includes("uplot") ? "uplot" : undefined,
      },
    },
  },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8787", changeOrigin: false } } },
});
