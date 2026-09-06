// Turning what somebody pasted into a job submission.
//
// Kept out of the page so fast refresh keeps working there, and so the parsing
// is testable without rendering a form.

/**
 * Split a textarea into URLs the way people actually paste them.
 *
 * Newlines, commas and stray whitespace all show up in real pastes -- out of a
 * spreadsheet column, out of a CSV cell, out of a chat message. Splitting on
 * all three costs nothing and removes the "one per line" instruction that gets
 * ignored anyway.
 *
 * De-duplication happens here *and* on the server. Here so the count above the
 * submit button is the truth before anything is sent; there because the API is
 * reachable without this page and cannot assume it ran.
 */
export function parseUrls(raw: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const piece of raw.split(/[\s,]+/)) {
    const url = piece.trim()
    if (!url || seen.has(url)) continue
    seen.add(url)
    out.push(url)
  }
  return out
}

/**
 * What will certainly fail, without pretending to validate a URL.
 *
 * A missing scheme is the one mistake worth flagging before submitting: the
 * driver cannot navigate to `example.com/p/1`, so every such row would burn a
 * browser session to report the same error. Everything else -- a typo'd host,
 * a 404, the wrong page type -- is only knowable by fetching it, and guessing
 * at those would reject URLs that work.
 */
export function suspiciousUrls(urls: string[]): string[] {
  return urls.filter((u) => !/^https?:\/\//i.test(u))
}

/** How many pieces the raw text held, so "duplicates removed" is only said when true. */
export function rawPieceCount(raw: string): number {
  const trimmed = raw.trim()
  return trimmed === '' ? 0 : trimmed.split(/[\s,]+/).length
}
