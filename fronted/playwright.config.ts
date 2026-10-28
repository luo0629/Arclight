import { defineConfig, devices } from "@playwright/test";

const externalUrl = process.env.ARCLIGHT_E2E_BASE_URL;
const apiPort = process.env.ARCLIGHT_E2E_API_PORT ?? "8766";
const webPort = process.env.ARCLIGHT_E2E_WEB_PORT ?? "4177";
const production = process.env.ARCLIGHT_E2E_PRODUCTION === "1";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  use: {
    baseURL: externalUrl ?? `http://127.0.0.1:${webPort}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        channel: process.env.CI ? undefined : "chrome",
      },
    },
  ],
  webServer: externalUrl
    ? undefined
    : [
        {
          command:
            process.platform === "win32"
              ? '".venv/Scripts/python.exe" scripts/e2e_server.py'
              : ".venv/bin/python scripts/e2e_server.py",
          cwd: "../backend",
          url: `http://127.0.0.1:${apiPort}/health/ready`,
          reuseExistingServer: false,
          timeout: 60000,
        },
        {
          command: `npm run ${production ? "start" : "dev"} -- --port ${webPort}`,
          url: `http://127.0.0.1:${webPort}`,
          reuseExistingServer: false,
          env: { ARCLIGHT_API_PROXY: `http://127.0.0.1:${apiPort}` },
          timeout: 120000,
        },
      ],
});
