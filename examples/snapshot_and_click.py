"""Snapshot a page, then click things -- both ways of addressing an element.

    uv run python examples/snapshot_and_click.py

Runs against an HTML fixture this script writes to a temp file, so it is
deterministic and needs no network.

There are two ways to say *which* element, and the difference is the thing
worth understanding:

  * **CSS selector** -- `page.click("#add-to-cart")`. What you reach for when
    you know the page. Resolved in the browser by `DOM.querySelector`, which is
    document-scoped: it cannot see inside a cross-origin iframe or a shadow
    root, and an ambiguous selector silently takes the first match.
  * **Snapshot ref** -- `page.click(ref="e42")`. A token minted by `snapshot()`
    for a specific captured node. Unambiguous, and it reaches into iframes and
    shadow DOM, because the driver acts on the node's `(session, backendNodeId)`
    rather than re-querying. This is what an agent uses.

**The parameter decides which you mean, never the string.** A positional
argument is always a selector, `ref=` is always a ref, and passing both is a
`ValueError`. There is no sniffing and there cannot be: `e42` is itself a valid
CSS type selector (it matches an `<e42>` element), so any rule based on the
shape of the string would be a guess -- and a wrong guess clicks the wrong
element without telling you.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from crawlpilot import Browser
from crawlpilot.dom.serializer import serialize

FIXTURE = """<!doctype html>
<html><head><title>Widget Shop</title></head><body>
  <h1 id="heading">Widget Shop</h1>

  <button id="add-to-cart">Add to cart</button>
  <button id="wishlist">Save for later</button>

  <div id="cart-count">0</div>
  <div id="status">idle</div>

  <label><input type="checkbox" id="gift"> Gift wrap</label>
  <input type="text" id="promo" placeholder="Promo code">

  <ul>
    <li class="review">Great widget</li>
    <li class="review">Does widget things</li>
    <li class="review">Would widget again</li>
  </ul>

  <p id="secret" style="display:none">hidden text</p>

  <script>
    let n = 0;
    document.getElementById('add-to-cart').onclick = () => {
      document.getElementById('cart-count').textContent = String(++n);
      document.getElementById('status').textContent = 'added';
    };
    document.getElementById('wishlist').onclick = () => {
      document.getElementById('status').textContent = 'saved for later';
    };
  </script>
</body></html>
"""


def _find_ref(dom, **attrs: str) -> str:
    """The ref of the first snapshot node matching every given attribute.

    Refs are `e<selector_index>`, and `serialize()` hands back the
    `selector_map` that pairs each index with the node it was minted for -- so
    finding one is a dict scan, not a tree walk. This helper exists because
    picking an element out of a snapshot is what every caller does first.
    """

    for index, node in dom.selector_map.items():
        node_attrs = node.attributes or {}
        if all(node_attrs.get(k) == v for k, v in attrs.items()):
            return f"e{index}"
    raise LookupError(f"no snapshot node matching {attrs}")


async def main() -> None:
    fixture = Path(tempfile.mkdtemp()) / "shop.html"
    fixture.write_text(FIXTURE)
    shots = Path(tempfile.mkdtemp())

    async with Browser(headful=True) as browser:
        async with browser.session() as page:
            await page.navigate(fixture.as_uri())

            # --- 1. What the page looks like to the library -----------------
            #
            # `snapshot()` gives you that compact text directly, with a `[ref]`
            # on every interactive node, and works the same against a remote
            # browser. Printing it is the fastest way to understand what refs
            # are:
            #
            #     print((await page.snapshot()).llm_text)
            #
            # This example goes one level lower instead, because `_find_ref`
            # below matches on `id` attributes -- and attributes live on the
            # fused tree, which `tree()` returns and only a local session has.
            tree = await page.tree()
            assert tree is not None
            dom = serialize(tree)
            print("=== page as the library sees it ===")
            print(dom.llm_text[:700])

            (shots / "before.png").write_bytes(await page.screenshot())

            # --- 2. Click by CSS selector ----------------------------------
            #
            # No snapshot needed for this at all -- the two are independent.
            print("\n=== click by selector ===")
            await page.click("#add-to-cart")
            print("cart-count:", await page.get_text(selector="#cart-count"))
            print("status:    ", await page.get_text(selector="#status"))

            # --- 3. Click by ref -------------------------------------------
            #
            # The same button, addressed through the snapshot instead.
            ref = _find_ref(dom, id="add-to-cart")
            print(f"\n=== click by ref ({ref}) ===")
            await page.click(ref=ref)
            print("cart-count:", await page.get_text(selector="#cart-count"))

            # A *different* element, to show the ref really identifies a node.
            wishlist = _find_ref(dom, id="wishlist")
            await page.click(ref=wishlist)
            print(f"after clicking {wishlist}, status:", await page.get_text(selector="#status"))

            # --- 4. The getters return values, not sentences ---------------
            #
            # Before 0.2 every one of these came back as prose written for an
            # LLM: `get_count()` gave "count: 3 element(s) match '.review'",
            # and `is_visible()` gave "#secret is not visible" -- a non-empty,
            # therefore truthy, string.
            print("\n=== typed getters ===")
            count = await page.get_count(".review")
            visible = await page.is_visible(selector="#heading")
            hidden = await page.is_visible(selector="#secret")
            print(f"get_count('.review')       -> {count!r}   ({type(count).__name__})")
            print(f"is_visible('#heading')     -> {visible!r} ({type(visible).__name__})")
            print(f"is_visible('#secret')      -> {hidden!r} ({type(hidden).__name__})")
            assert count == 3 and visible is True and hidden is False
            # The line that was a bug in every caller's code until 0.2:
            if not await page.is_visible(selector="#secret"):
                print("...so `if not await page.is_visible(...)` now works")

            # --- 5. Fill and check, by selector ----------------------------
            await page.fill("#promo", "WIDGET10")
            await page.check("#gift")
            print("\npromo value:", repr(await page.get_value(selector="#promo")))
            print("gift checked:", await page.is_checked(selector="#gift"))

            (shots / "after.png").write_bytes(await page.screenshot())
            print(f"\nscreenshots written to {shots}")

            # --- 6. Both at once is an error, not a preference -------------
            try:
                await page.click("#add-to-cart", ref=ref)
            except ValueError as exc:
                print(f"\npassing both -> ValueError: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
