import { defineConfig, devices } from '@playwright/test';

const externalUrl = process.env.ARCLIGHT_E2E_BASE_URL;

export default defineConfig({
  testDir: './tests', fullyParallel: false, workers: 1, timeout: 60000,
  use: { baseURL: externalUrl ?? 'http://127.0.0.1:4177', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], channel: process.env.CI ? undefined : 'chrome' } }],
  webServer: externalUrl ? undefined : [
    { command: process.platform === 'win32' ? '".venv/Scripts/python.exe" scripts/e2e_server.py' : '.venv/bin/python scripts/e2e_server.py', cwd: '../backend', url: 'http://127.0.0.1:8766/health/ready', reuseExistingServer: false, timeout: 60000 },
    { command: 'npm run dev -- --port 4177 --strictPort', url: 'http://127.0.0.1:4177', reuseExistingServer: false, env: { ARCLIGHT_API_PROXY: 'http://127.0.0.1:8766' } },
  ],
});
