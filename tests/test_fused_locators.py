"""Unit tests for `agentpilot.driver.fused_locators.candidate_selectors` and
`RefCache` fusion resolution -- the MatchLevel selector cascade for
`e<backendNodeId>` refs. A fake page stands in for Playwright (no browser)."""

from __future__ import annotations

import pytest

from agentpilot.driver.fused_locators import candidate_selectors
from agentpilot.driver.ref_cache import RefCache
from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.errors import StaleRefError


def _node(backend: int, *, attrs=None, role=None, name="") -> EnhancedDOMTreeNode:
    body = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    node = EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=NodeType.ELEMENT_NODE,
        node_name="BUTTON",
        attributes=attrs or {},
        ax_node=EnhancedAXNode(role=role, name=name) if role or name else None,
    )
    node.parent_node = body
    body.children_nodes.append(node)
    return node


def test_candidate_order_id_then_testid_then_xpath_then_role_then_attr() -> None:
    node = _node(
        5,
        attrs={"id": "buy", "data-testid": "cart", "name": "buyBtn", "aria-label": "Buy now"},
        role="button",
        name="Buy",
    )
    kinds = [k for k, _ in candidate_selectors(node)]
    assert kinds[0] == "css"  # id
    assert candidate_selectors(node)[0][1] == '[id="buy"]'
    assert ("css", '[data-testid="cart"]') in candidate_selectors(node)
    assert any(k == "xpath" for k, _ in candidate_selectors(node))
    assert ("role", "button\x1fBuy") in candidate_selectors(node)
    assert ("css", '[name="buyBtn"]') in candidate_selectors(node)


def test_candidate_quote_escaping() -> None:
    node = _node(6, attrs={"aria-label": 'say "hi"'})
    assert ("css", '[aria-label="say \\"hi\\""]') in candidate_selectors(node)


def test_no_identifiers_falls_back_to_xpath_only() -> None:
    node = _node(7)
    cands = candidate_selectors(node)
    assert all(k == "xpath" for k, _ in cands)
    assert cands and cands[0][1].startswith("xpath=/")


# --- RefCache fusion resolution against a fake page ------------------------------


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    """Resolves only the selectors registered in `unique`; everything else
    reports zero matches. `get_by_role` mirrors `locator`."""

    def __init__(self, unique: set[str]) -> None:
        self.unique = unique
        self.queried: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.queried.append(selector)
        return _FakeLocator(1 if selector in self.unique else 0)

    def get_by_role(self, role: str, name: str, exact: bool) -> _FakeLocator:  # noqa: FBT001
        key = f"role:{role}:{name}"
        self.queried.append(key)
        return _FakeLocator(1 if key in self.unique else 0)


async def test_ref_cache_resolves_first_unique_candidate() -> None:
    cache = RefCache()
    body = _node(5, attrs={"id": "buy"}).parent_node
    assert body is not None
    cache.reset(1)
    cache.record_fused(body)

    page = _FakePage(unique={'[id="buy"]'})
    loc = await cache.resolve(page, "e5")
    assert await loc.count() == 1
    assert page.queried[0] == '[id="buy"]'  # tried id first


async def test_ref_cache_skips_ambiguous_and_uses_next_tier() -> None:
    # id matches multiple (count 0 in fake == "not unique"); role+name is unique.
    node = _node(5, attrs={"id": "dup"}, role="button", name="Buy")
    cache = RefCache()
    cache.reset(1)
    cache.record_fused(node.parent_node)
    page = _FakePage(unique={"role:button:Buy"})
    loc = await cache.resolve(page, "e5")
    assert await loc.count() == 1


async def test_ref_cache_unknown_fused_ref_raises_stale() -> None:
    cache = RefCache()
    cache.reset(1)
    cache.record_fused(_node(5).parent_node)
    with pytest.raises(StaleRefError):
        await cache.resolve(_FakePage(unique=set()), "e999")


async def test_reset_clears_fused_refs() -> None:
    cache = RefCache()
    cache.reset(1)
    cache.record_fused(_node(5, attrs={"id": "buy"}).parent_node)
    cache.reset(2)  # new snapshot -> old backendNodeId must not resolve
    with pytest.raises(StaleRefError):
        await cache.resolve(_FakePage(unique={'[id="buy"]'}), "e5")
