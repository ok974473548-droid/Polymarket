// Сценарии интерфейса: демо-сервер на синтетических данных (tests/demo_server.py), без сети.
// Сценарии меняют состояние (покупают и продают виртуальные ставки), поэтому идут по одному.
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.WF_E2E_PORT || 8899);
const PY = process.env.WF_PYTHON || "python";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: "ru-RU",
    trace: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 900 } } },
    { name: "phone", use: { ...devices["Pixel 7"] }, grep: /@phone/ },
  ],
  webServer: {
    command: `${PY} ../tests/demo_server.py --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/status`,
    timeout: 180_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
  },
});
