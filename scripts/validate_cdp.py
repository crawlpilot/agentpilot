"""Validates the tenant-facing CDP surface (`GET .../cdp/json/version` +
`WS .../cdp`, `routes/cdp.py`/`routes/cdp_proxy.py`) end-to-end against a
running gateway: opens a real `enable_cdp=True` session, checks the
discovery document against Chrome's own `/json/version` shape (the same
shape any hosted-CDP provider -- browser-use.com, Browserless, etc. --
returns, since we're proxying the browser's real endpoint rather than
inventing our own), then drives the returned `webSocketDebuggerUrl` with
three independent clients: a raw CDP session (protocol-level, no library
assumptions), Playwright (both the direct-websocket and the
http-auto-discovery connect styles), and Puppeteer (via a companion Node
script, `cdp_puppeteer_check.mjs`) -- proving the endpoint is genuinely
standard CDP, not something only our own driver can talk to.

Usage:
    python scripts/validate_cdp.py --admin-token dev-admin-token --tenant cdp-check
    python scripts/validate_cdp.py --api-key bk_live_... --tenant existing-tenant

Env var fallbacks: AGENTPILOT_BASE_URL, AGENTPILOT_API_KEY,
AGENTPILOT_ADMIN_TOKEN, AGENTPILOT_TENANT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websockets

_REQUIRED_DISCOVERY_FIELDS = (
    "Browser",
    "Protocol-Version",
    "User-Agent",
    "V8-Version",
    "WebKit-Version",
    "webSocketDebuggerUrl",
)

_NODE_SCRIPT = Path(__file__).parent / "cdp_puppeteer_check.mjs"


@dataclass
class Check:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("AGENTPILOT_BASE_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("AGENTPILOT_API_KEY"))
    p.add_argument("--admin-token", default=os.environ.get("AGENTPILOT_ADMIN_TOKEN"))
    p.add_argument("--tenant", default=os.environ.get("AGENTPILOT_TENANT", "cdp-validate"))
    p.add_argument("--domain", default="example.com")
    p.add_argument("--keep-session", action="store_true", help="don't release the session on exit")
    return p.parse_args()


def _mint_api_key(client: httpx.Client, base_url: str, admin_token: str, tenant: str) -> str:
    resp = client.post(
        f"{base_url}/v1/api-keys",
        json={"tenant": tenant, "name": f"cdp-validate-{uuid.uuid4().hex[:8]}"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp.raise_for_status()
    return resp.json()["api_key"]


def _open_session(client: httpx.Client, base_url: str, api_key: str, tenant: str, domain: str) -> str:
    resp = client.post(
        f"{base_url}/v1/sessions",
        json={
            "tenant": tenant,
            "domain": domain,
            "name": f"cdp-validate-{uuid.uuid4().hex[:8]}",
            "headful": False,
            "enable_cdp": True,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _release_session(client: httpx.Client, base_url: str, api_key: str, session_id: str) -> None:
    with_suppress = client.delete(
        f"{base_url}/v1/sessions/{session_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    with_suppress.raise_for_status()


def check_discovery(client: httpx.Client, base_url: str, api_key: str, session_id: str) -> tuple[Check, dict[str, Any] | None]:
    try:
        resp = client.get(
            f"{base_url}/v1/sessions/{session_id}/cdp/json/version",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        doc = resp.json()
    except Exception as exc:  # noqa: BLE001
        return Check("discovery (GET .../cdp/json/version)", "FAIL", str(exc)), None

    missing = [f for f in _REQUIRED_DISCOVERY_FIELDS if not doc.get(f)]
    if missing:
        return (
            Check(
                "discovery (GET .../cdp/json/version)",
                "FAIL",
                f"missing/empty required field(s): {missing}; got keys={sorted(doc.keys())}",
            ),
            doc,
        )
    ws_url = doc["webSocketDebuggerUrl"]
    if not (ws_url.startswith("ws://") or ws_url.startswith("wss://")):
        return Check(
            "discovery (GET .../cdp/json/version)",
            "FAIL",
            f"webSocketDebuggerUrl is not a ws(s):// url: {ws_url!r}",
        ), doc

    detail = ", ".join(f"{k}={doc[k]!r}" for k in _REQUIRED_DISCOVERY_FIELDS if k != "webSocketDebuggerUrl")
    return Check("discovery (GET .../cdp/json/version)", "PASS", detail), doc


async def check_raw_cdp(ws_url: str) -> Check:
    """Speaks bare CDP JSON-RPC over the websocket -- no client library at
    all, so this is the ground truth for "is this really CDP" independent
    of any single tool's own quirks/version."""

    try:
        async with websockets.connect(ws_url, max_size=None) as ws:

            async def _send(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(raw)
                    if data.get("id") == msg_id:
                        return data

            targets = await _send(1, "Target.getTargets")
            page_targets = [
                t for t in targets["result"]["targetInfos"] if t["type"] == "page"
            ]
            if not page_targets:
                return Check("raw CDP protocol (Target/Page/Runtime)", "FAIL", "no 'page' target found")
            target_id = page_targets[0]["targetId"]

            attach = await _send(2, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id = attach["result"]["sessionId"]

            async def _send_sess(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                await ws.send(
                    json.dumps(
                        {"id": msg_id, "method": method, "params": params or {}, "sessionId": session_id}
                    )
                )
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(raw)
                    if data.get("id") == msg_id and data.get("sessionId") == session_id:
                        return data

            marker = f"cdp-validate-{uuid.uuid4().hex[:8]}"
            await _send_sess(3, "Page.navigate", {"url": f"data:text/html,<title>{marker}</title>"})
            await asyncio.sleep(0.5)
            evald = await _send_sess(4, "Runtime.evaluate", {"expression": "document.title"})
            title = evald["result"]["result"]["value"]
            if title != marker:
                return Check(
                    "raw CDP protocol (Target/Page/Runtime)",
                    "FAIL",
                    f"navigated page title mismatch: got {title!r}, want {marker!r}",
                )

            shot = await _send_sess(5, "Page.captureScreenshot", {"format": "png"})
            png_b64 = shot["result"]["data"]
            if len(png_b64) < 100:
                return Check(
                    "raw CDP protocol (Target/Page/Runtime)", "FAIL", "captureScreenshot returned suspiciously little data"
                )

            return Check(
                "raw CDP protocol (Target/Page/Runtime)",
                "PASS",
                f"attached session_id={session_id}, navigated+read title, captured {len(png_b64)}b64 screenshot",
            )
    except Exception as exc:  # noqa: BLE001
        return Check("raw CDP protocol (Target/Page/Runtime)", "FAIL", repr(exc))


async def check_playwright(ws_url: str, cdp_base_url: str, api_key: str) -> list[Check]:
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError:
        skip = Check(
            "playwright (direct ws)", "SKIP", "playwright not installed -- `pip install playwright` (no `playwright install` needed, we only connect_over_cdp)"
        )
        return [skip, Check("playwright (http auto-discovery)", "SKIP", skip.detail)]

    results: list[Check] = []
    marker_a = f"cdp-validate-pw-{uuid.uuid4().hex[:8]}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                await page.goto(f"data:text/html,<title>{marker_a}</title>")
                title = await page.title()
                if title != marker_a:
                    raise AssertionError(f"title mismatch: got {title!r} want {marker_a!r}")
                await page.close()
                results.append(Check("playwright (direct ws)", "PASS", f"connect_over_cdp({ws_url.split('?')[0]}...)"))
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        results.append(Check("playwright (direct ws)", "FAIL", repr(exc)))

    marker_b = f"cdp-validate-pw-{uuid.uuid4().hex[:8]}"
    try:
        async with async_playwright() as p:
            # Base "cdp" URL, no /json/version suffix -- playwright fetches
            # that itself, headers included, exactly like a real integrator
            # who was only ever handed one base endpoint would.
            browser = await p.chromium.connect_over_cdp(
                cdp_base_url, headers={"Authorization": f"Bearer {api_key}"}
            )
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                await page.goto(f"data:text/html,<title>{marker_b}</title>")
                title = await page.title()
                if title != marker_b:
                    raise AssertionError(f"title mismatch: got {title!r} want {marker_b!r}")
                await page.close()
                results.append(
                    Check("playwright (http auto-discovery)", "PASS", f"connect_over_cdp({cdp_base_url}, headers=Bearer ...)")
                )
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        results.append(Check("playwright (http auto-discovery)", "FAIL", repr(exc)))

    return results


def check_puppeteer(ws_url: str) -> Check:
    name = "puppeteer (direct ws, via companion Node script)"
    node = shutil.which("node")
    if node is None:
        return Check(name, "SKIP", "no `node` on PATH")
    if not _NODE_SCRIPT.exists():
        return Check(name, "SKIP", f"missing {_NODE_SCRIPT}")

    try:
        proc = subprocess.run(
            [node, str(_NODE_SCRIPT), ws_url], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return Check(name, "FAIL", "node script timed out after 30s")

    result_line = next(
        (line for line in reversed(proc.stdout.splitlines()) if line.startswith("RESULT:")), None
    )
    if result_line is None:
        return Check(name, "FAIL", f"no RESULT line from node script; stderr={proc.stderr.strip()[-500:]}")
    _, _, rest = result_line.partition(" ")
    status, _, detail = rest.partition(" ")
    if status not in ("PASS", "FAIL", "SKIP"):
        return Check(name, "FAIL", f"unparseable result line: {result_line!r}")
    return Check(name, status, detail)


def _print_report(checks: list[Check]) -> bool:
    width = max(len(c.name) for c in checks)
    print("\n--- CDP endpoint validation report ---")
    ok = True
    for c in checks:
        marker = {"PASS": "\033[32mPASS\033[0m", "FAIL": "\033[31mFAIL\033[0m", "SKIP": "\033[33mSKIP\033[0m"}[c.status]
        print(f"[{marker}] {c.name.ljust(width)}  {c.detail}")
        if c.status == "FAIL":
            ok = False
    print()
    return ok


async def _amain(args: argparse.Namespace) -> int:
    if not args.api_key and not args.admin_token:
        print("error: need --api-key or --admin-token (or AGENTPILOT_API_KEY / AGENTPILOT_ADMIN_TOKEN)", file=sys.stderr)
        return 2

    with httpx.Client(timeout=30) as client:
        api_key = args.api_key or _mint_api_key(client, args.base_url, args.admin_token, args.tenant)

        print(f"opening session (tenant={args.tenant!r}, domain={args.domain!r}, enable_cdp=True)...")
        session_id = _open_session(client, args.base_url, api_key, args.tenant, args.domain)
        print(f"session_id={session_id}")

        checks: list[Check] = []
        try:
            discovery_check, doc = check_discovery(client, args.base_url, api_key, session_id)
            checks.append(discovery_check)
            if doc is None:
                return 0 if _print_report(checks) else 1

            ws_url = doc["webSocketDebuggerUrl"]
            cdp_base_url = f"{args.base_url}/v1/sessions/{session_id}/cdp"

            checks.append(await check_raw_cdp(ws_url))
            checks.extend(await check_playwright(ws_url, cdp_base_url, api_key))
            checks.append(check_puppeteer(ws_url))
        finally:
            if not args.keep_session:
                _release_session(client, args.base_url, api_key, session_id)
                print(f"released session {session_id}")
            else:
                print(f"leaving session {session_id} open (--keep-session)")

    return 0 if _print_report(checks) else 1


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
