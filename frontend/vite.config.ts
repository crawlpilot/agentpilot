import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // `VITE_API_BASE_URL` (see `.env.development`) is the single source of
  // truth for "where's the backend in dev" -- read here via `loadEnv` (env
  // files aren't auto-applied to `process.env` inside this config file,
  // only to client code) so the proxy target and the code-snippet base URL
  // in `src/lib/config.ts` can never drift apart.
  const env = loadEnv(mode, process.cwd(), '')
  const apiBaseUrl = env.VITE_API_BASE_URL || 'http://localhost:8000'

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      proxy: {
        '/v1': { target: apiBaseUrl, ws: true },
        '/healthz': apiBaseUrl,
        '/readyz': apiBaseUrl,
      },
    },
  }
})
