// Fuller interactive demo of the exposed CDP endpoint, beyond
// cdp_puppeteer_check.mjs's one-shot smoke test: connects real Puppeteer
// and drives the session through actual navigation, typed user input, a
// click-triggered navigation, and scrolling -- proof (not just an
// assertion) that the browser behind the endpoint is genuinely
// interactive, not a static capture. Each step logs what it observed and
// saves a screenshot to scripts/cdp_demo_screenshots/ so you can eyeball it.
//
// Usage (endpoint you already have, e.g. from the sessions table's
// "Connect" dialog or scripts/validate_cdp.py's output):
//   node scripts/cdp_puppeteer_demo.mjs 'ws://localhost:8000/v1/sessions/<id>/cdp?api_key=<key>'
//
// Or let it open its own session (same flags as validate_cdp.py):
//   node scripts/cdp_puppeteer_demo.mjs --base-url http://localhost:8000 --admin-token dev-admin-token --tenant demo
//   node scripts/cdp_puppeteer_demo.mjs --base-url http://localhost:8000 --api-key bk_live_... --tenant demo

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCREENSHOT_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "cdp_demo_screenshots");

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      flags[argv[i].slice(2)] = argv[i + 1];
      i += 1; // consume the value too -- it is not a positional
    } else {
      positional.push(argv[i]);
    }
  }
  return { wsUrl: positional[0], flags };
}

async function openOwnSession(flags) {
  const baseUrl = flags["base-url"] ?? "http://localhost:8000";
  const tenant = flags.tenant ?? "cdp-demo";
  const domain = flags.domain ?? "example.com";

  let apiKey = flags["api-key"];
  if (!apiKey) {
    if (!flags["admin-token"]) {
      throw new Error("no ws endpoint given, and no --api-key or --admin-token to mint one");
    }
    const resp = await fetch(`${baseUrl}/v1/api-keys`, {
      method: "POST",
      headers: { Authorization: `Bearer ${flags["admin-token"]}`, "Content-Type": "application/json" },
      body: JSON.stringify({ tenant, name: `cdp-demo-${Math.random().toString(16).slice(2, 8)}` }),
    });
    if (!resp.ok) throw new Error(`api-key mint failed: ${resp.status} ${await resp.text()}`);
    apiKey = (await resp.json()).api_key;
  }

  const openResp = await fetch(`${baseUrl}/v1/sessions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      tenant,
      domain,
      name: `cdp-demo-${Math.random().toString(16).slice(2, 8)}`,
      headful: false,
      enable_cdp: true,
    }),
  });
  if (!openResp.ok) throw new Error(`session open failed: ${openResp.status} ${await openResp.text()}`);
  const sessionId = (await openResp.json()).session_id;

  const discResp = await fetch(`${baseUrl}/v1/sessions/${sessionId}/cdp/json/version`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!discResp.ok) throw new Error(`cdp discovery failed: ${discResp.status} ${await discResp.text()}`);
  const { webSocketDebuggerUrl } = await discResp.json();

  return {
    wsUrl: webSocketDebuggerUrl,
    async release() {
      await fetch(`${baseUrl}/v1/sessions/${sessionId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${apiKey}` },
      });
    },
  };
}

async function screenshot(page, step, name) {
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  const file = path.join(SCREENSHOT_DIR, `${String(step).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path: file });
  console.log(`    saved ${path.relative(process.cwd(), file)}`);
}

async function main() {
  const { wsUrl: argWsUrl, flags } = parseArgs(process.argv.slice(2));

  let puppeteer;
  try {
    ({ default: puppeteer } = await import("puppeteer-core"));
  } catch {
    console.error("puppeteer-core not installed -- run `npm install` in scripts/ first");
    process.exit(1);
  }

  let wsUrl = argWsUrl;
  let release = async () => {};
  if (!wsUrl) {
    console.log("no ws endpoint given -- opening a fresh session...");
    const opened = await openOwnSession(flags);
    wsUrl = opened.wsUrl;
    release = opened.release;
    console.log(`opened: ${wsUrl.split("?")[0]}...`);
  }

  console.log("connecting puppeteer...");
  const browser = await puppeteer.connect({ browserWSEndpoint: wsUrl });
  let step = 0;

  try {
    const page = (await browser.pages())[0] ?? (await browser.newPage());
    await page.setViewport({ width: 1280, height: 900 });

    // 1. Plain navigation.
    step += 1;
    console.log(`\n[${step}] navigate -> https://example.com`);
    await page.goto("https://example.com", { waitUntil: "networkidle2" });
    console.log(`    title: ${await page.title()}`);
    console.log(`    url:   ${page.url()}`);
    await screenshot(page, step, "navigate");

    // 2. User action: navigate to Wikipedia and type into the real search
    // box (a typed, dispatched keyboard input -- not `page.goto` -- so this
    // exercises Input.dispatchKeyEvent over CDP, same as a human typing).
    step += 1;
    console.log(`\n[${step}] navigate -> https://en.wikipedia.org, then type into search box`);
    await page.goto("https://en.wikipedia.org/", { waitUntil: "networkidle2" });
    await page.click("#searchInput");
    await page.type("#searchInput", "Chrome DevTools Protocol", { delay: 30 });
    await screenshot(page, step, "typed-search");

    // 3. User action: press Enter to submit the search (real keyboard
    // event, triggers a real navigation) -- proves click+type+navigate all
    // chain correctly over the same CDP session.
    step += 1;
    console.log(`\n[${step}] press Enter -> submit search`);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle2" }),
      page.keyboard.press("Enter"),
    ]);
    console.log(`    title: ${await page.title()}`);
    console.log(`    url:   ${page.url()}`);
    await screenshot(page, step, "search-result");

    // 4. Scroll: real wheel/scroll input, not `window.scrollTo` via JS --
    // confirms Input domain mouse-wheel events land, same mechanism the
    // live-view's `interact` mode uses.
    step += 1;
    console.log(`\n[${step}] scroll down the article`);
    const beforeY = await page.evaluate(() => window.scrollY);
    await page.mouse.wheel({ deltaY: 1600 });
    await new Promise((r) => setTimeout(r, 300));
    const afterY = await page.evaluate(() => window.scrollY);
    console.log(`    scrollY: ${beforeY} -> ${afterY}`);
    if (afterY <= beforeY) throw new Error("scroll did not move the page");
    await screenshot(page, step, "scrolled");

    // 5. User action: click the first in-article link -- another real
    // navigation, this time click-triggered rather than Enter-triggered.
    step += 1;
    console.log(`\n[${step}] click first article link -> navigate again`);
    const linkHref = await page.evaluate(() => {
      const link = document.querySelector("#mw-content-text p a[href^='/wiki/']");
      return link ? link.getAttribute("href") : null;
    });
    if (!linkHref) throw new Error("couldn't find an in-article link to click");
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle2" }),
      page.click(`#mw-content-text p a[href='${linkHref}']`),
    ]);
    console.log(`    title: ${await page.title()}`);
    console.log(`    url:   ${page.url()}`);
    await screenshot(page, step, "clicked-link");

    console.log("\nAll steps completed -- CDP navigation, typed input, click, and scroll all confirmed working.");
  } finally {
    await browser.disconnect(); // the remote session stays up
    await release();
  }
}

main().catch((err) => {
  console.error("\nFAILED:", err);
  process.exit(1);
});
