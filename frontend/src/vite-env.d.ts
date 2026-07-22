/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Absolute backend origin, e.g. "http://localhost:8000" -- only needed in
   * dev, where the frontend and backend run on different ports (see
   * `vite.config.ts`'s proxy and `lib/config.ts`). Unset in production,
   * where the built SPA is served same-origin by the gateway/monolith and
   * `window.location.origin` is already correct. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
