import { defineConfig } from 'vite'
import path from 'node:path'

/**
 * Builds the injectable picker bundle -- a second, separate build from the
 * app's own.
 *
 * `src/lib/picker/entry.ts` and its ~5.2k lines of vendored DOM code never
 * run in the studio's tab; they run inside the *remote* page, injected as a
 * string through `execute_js`. That means the output has to be a single
 * self-contained script with no imports and no module wrapper, which is what
 * `formats: ['iife']` gives us. `name: '__cpPicker'` makes the bundle assign
 * its default export to `window.__cpPicker`, which is the handle
 * `usePagePicker` calls into.
 *
 * The output is committed. `npm run build` must not depend on this having
 * been run -- the app imports the artefact with `?raw`, so a missing file
 * would be a build error rather than a silently stale bundle. Re-run
 * `npm run build:picker` after touching anything under `src/lib/picker/`.
 */
export default defineConfig({
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    lib: {
      entry: path.resolve(__dirname, 'src/lib/picker/entry.ts'),
      formats: ['iife'],
      name: '__cpPicker',
      fileName: () => 'picker.iife.js',
    },
    outDir: 'src/lib/picker/generated',
    emptyOutDir: true,
    // The bundle travels as a JSON string in an HTTP body on every install,
    // so size is worth minding, but readable output is worth more the first
    // time this misbehaves inside someone else's page. Minify, keep no map.
    minify: 'esbuild',
    sourcemap: false,
    // The picker targets whatever Chrome the browser pool runs, not the
    // studio's browser. Chrome 111 is comfortably below any CDP-driven build.
    target: 'chrome111',
  },
})
