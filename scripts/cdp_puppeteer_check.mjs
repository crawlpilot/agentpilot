// Companion to validate_cdp.py -- proves the CDP endpoint works with a
// real, independent Puppeteer client, not just our own driver/Playwright.
// `puppeteer-core` (not `puppeteer`): we're connecting to an already-running
// remote browser, never launching a local one, so there's no reason to pull
// down a bundled Chromium just to run this check.
//
// Usage: node cdp_puppeteer_check.mjs <webSocketDebuggerUrl>
// Prints exactly one `RESULT: PASS|FAIL|SKIP ...` line (the parseable
// contract validate_cdp.py's check_puppeteer() reads) plus any deeper logs.

const wsUrl = process.argv[2];
if (!wsUrl) {
  console.log("RESULT: FAIL no websocket url provided as argv[1]");
  process.exit(1);
}

let puppeteer;
try {
  ({ default: puppeteer } = await import("puppeteer-core"));
} catch {
  console.log(
    "RESULT: SKIP puppeteer-core not installed -- run `npm install` in scripts/ first",
  );
  process.exit(0);
}

try {
  const browser = await puppeteer.connect({ browserWSEndpoint: wsUrl });
  try {
    const pages = await browser.pages();
    const page = pages[0] ?? (await browser.newPage());

    const marker = `cdp-validate-puppeteer-${Math.random().toString(16).slice(2, 10)}`;
    await page.goto(`data:text/html,<title>${marker}</title>`);
    const title = await page.title();
    if (title !== marker) {
      throw new Error(`title mismatch: got ${JSON.stringify(title)} want ${JSON.stringify(marker)}`);
    }

    const shot = await page.screenshot({ encoding: "base64" });
    if (!shot || shot.length < 100) {
      throw new Error("screenshot() returned suspiciously little data");
    }

    console.log(`RESULT: PASS connect({browserWSEndpoint}), navigated+read title, captured ${shot.length}b64 screenshot`);
  } finally {
    // disconnect(), not close(): this is someone else's shared browser --
    // close() would tear down the whole remote session out from under it.
    await browser.disconnect();
  }
} catch (err) {
  console.log(`RESULT: FAIL ${err && err.message ? err.message : String(err)}`);
  process.exit(1);
}
