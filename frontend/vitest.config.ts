import { defineConfig } from 'vitest/config'
import path from 'node:path'

/**
 * Test config, separate from `vite.config.ts` so the dev-server proxy and the
 * app build stay untouched by it.
 *
 * `jsdom` rather than the default `node` environment: everything under test
 * here is DOM code. The four `lib/picker/vendor/**` suites came across from
 * the crawlPilot extension unchanged, and they are the regression net for the
 * selector-stability and schema-inference heuristics -- if the vendoring
 * altered behaviour, those fail, which is exactly what they are here for.
 */
export default defineConfig({
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
})
