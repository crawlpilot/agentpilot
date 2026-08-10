"""Paint-order occlusion filtering -- decides which interactive elements are
visually *covered* by later-painted opaque elements and should be dropped from
the LLM view (a button hidden behind a modal overlay isn't clickable). Ported
from browser-use's `dom/serializer/paint_order.py` (`RectUnionPure` /
`PaintOrderRemover`).

Decoupled from the node type: `compute_occluded` takes lightweight `PaintEntry`
records (key + bounds + paint order + opacity/background) so the rectangle-union
geometry is pure and unit-testable. The serializer builds the entries from
`SimplifiedNode`s and marks the returned keys `ignored_by_paint_order`.

Algorithm: process elements from highest paint order (front) to lowest (back),
maintaining a union of already-covered rectangles per document context; an
element whose rectangle is already fully covered is occluded. Translucent /
transparent elements cover nothing (they don't hide what's behind them).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle, (x1,y1) top-left, (x2,y2) bottom-right."""

    x1: float
    y1: float
    x2: float
    y2: float

    def intersects(self, other: Rect) -> bool:
        return not (
            self.x2 <= other.x1 or other.x2 <= self.x1 or self.y2 <= other.y1 or other.y2 <= self.y1
        )

    def contains(self, other: Rect) -> bool:
        return (
            self.x1 <= other.x1
            and self.y1 <= other.y1
            and self.x2 >= other.x2
            and self.y2 >= other.y2
        )


class RectUnionPure:
    """A disjoint set of rectangles supporting "is this rect fully covered?".
    Capped to avoid exponential fragmentation on pages with many translucent
    layers -- past the cap `contains` conservatively returns False (nothing
    hidden), trading filtering aggressiveness for correctness/safety."""

    __slots__ = ("_rects",)
    _MAX_RECTS = 5000

    def __init__(self) -> None:
        self._rects: list[Rect] = []

    @staticmethod
    def _split_diff(a: Rect, b: Rect) -> list[Rect]:
        r"""Up to four rectangles making up ``a \ b`` (assumes a intersects b)."""

        parts: list[Rect] = []
        if a.y1 < b.y1:
            parts.append(Rect(a.x1, a.y1, a.x2, b.y1))
        if b.y2 < a.y2:
            parts.append(Rect(a.x1, b.y2, a.x2, a.y2))
        y_lo = max(a.y1, b.y1)
        y_hi = min(a.y2, b.y2)
        if a.x1 < b.x1:
            parts.append(Rect(a.x1, y_lo, b.x1, y_hi))
        if b.x2 < a.x2:
            parts.append(Rect(b.x2, y_lo, a.x2, y_hi))
        return parts

    def contains(self, r: Rect) -> bool:
        """True iff `r` is fully covered by the current union."""

        if not self._rects:
            return False
        stack = [r]
        for s in self._rects:
            new_stack: list[Rect] = []
            for piece in stack:
                if s.contains(piece):
                    continue
                if piece.intersects(s):
                    new_stack.extend(self._split_diff(piece, s))
                else:
                    new_stack.append(piece)
            if not new_stack:
                return True
            stack = new_stack
        return False

    def add(self, r: Rect) -> bool:
        """Insert `r` unless already covered. Returns whether the union grew."""

        if len(self._rects) >= self._MAX_RECTS:
            return False
        if self.contains(r):
            return False
        pending = [r]
        for s in self._rects:
            new_pending: list[Rect] = []
            for piece in pending:
                if piece.intersects(s):
                    new_pending.extend(self._split_diff(piece, s))
                else:
                    new_pending.append(piece)
            pending = new_pending
        self._rects.extend(pending)
        return True


@dataclass(frozen=True, slots=True)
class PaintEntry:
    """One candidate element for occlusion analysis."""

    key: int
    """Caller's identifier (e.g. backend_node_id) returned when occluded."""
    x: float
    y: float
    width: float
    height: float
    paint_order: int
    opacity: float = 1.0
    background_transparent: bool = False
    context: tuple = ()
    """Document/session context -- rectangles only occlude within the same
    context (an element in one iframe can't cover one in another)."""

    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.x + self.width, self.y + self.height)

    def is_opaque_cover(self) -> bool:
        """Whether this element actually hides what's behind it. Transparent
        background or low opacity -> covers nothing."""

        return not self.background_transparent and self.opacity >= 0.8


def compute_occluded(entries: list[PaintEntry]) -> set[int]:
    """Keys of entries fully covered by higher-paint-order opaque elements in
    the same context. Front-to-back sweep building a per-context rect union."""

    grouped: defaultdict[int, list[PaintEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.paint_order].append(entry)

    unions: defaultdict[tuple, RectUnionPure] = defaultdict(RectUnionPure)
    occluded: set[int] = set()

    for paint_order in sorted(grouped, reverse=True):
        to_add: defaultdict[tuple, list[Rect]] = defaultdict(list)
        for entry in grouped[paint_order]:
            rect = entry.rect()
            if unions[entry.context].contains(rect):
                occluded.add(entry.key)
            if entry.is_opaque_cover():
                to_add[entry.context].append(rect)
        # Add this layer's covers only after the whole layer is tested, so
        # same-layer siblings don't occlude each other.
        for context, rects in to_add.items():
            for rect in rects:
                unions[context].add(rect)

    return occluded
