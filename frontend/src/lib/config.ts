// In dev, the frontend (Vite, :5173) and backend (gateway/monolith, :8000)
// run on different ports -- `window.location.origin` there is the *frontend
// dev server's* origin, not the API's, so a code snippet built from it would
// be silently wrong for anyone who copy-pastes it into a terminal (it only
// happens to "work" through the browser's own fetches because of the dev
// proxy in `vite.config.ts`, which a standalone `curl` never goes through).
//
// In production, the built SPA is served same-origin by the gateway/
// monolith (a deliberate choice -- see the enterprise-UI plan's "gateway
// gap" section), so `window.location.origin` is already the right answer
// and `VITE_API_BASE_URL` is left unset.
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || window.location.origin
