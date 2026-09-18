"""The seam between "how we perceive and act" and "what we recorded".

Everything above this module (locators, replay, discovery, handoff) speaks only
in terms of :class:`UiNode` / :class:`Observation` / :class:`Surface`. Nothing
above this module imports Playwright. That is what makes the artifact portable:
a desktop surface backed by macOS ``AXUIElement`` or Windows UI Automation
produces the same ``UiNode`` shape (role, name, value, bounding box, table
context) and therefore executes the same artifact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..artifact.schema import StrategyKind


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class TableContext:
    """Where a node sits inside a table, in header terms rather than indices.

    Legacy back-office screens are tables. Identifying "the Current Balance cell
    on the Regular Savings row" survives added columns, reordered rows and
    restyled markup in a way that ``tr:nth-child(3) td:nth-child(3)`` does not.
    """

    headers: list[str]
    row_index: int
    col_index: int
    row_label: str
    col_header: str
    header_confident: bool = False


@dataclass(frozen=True)
class UiNode:
    """One perceivable control or text cell.

    ``ref`` is valid only for the observation it came from; every navigation
    invalidates it. Callers re-observe rather than caching refs.
    """

    ref: str
    role: str
    name: str
    name_source: str
    value: str = ""
    text: str = ""
    enabled: bool = True
    secret: bool = False
    in_viewport: bool = True
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    frame_path: str = ""
    frame_name: str = ""
    label_candidates: list[str] = field(default_factory=list)
    label_left: str = ""
    label_above: str = ""
    table: TableContext | None = None
    dom_hint: dict[str, Any] = field(default_factory=dict)

    @property
    def interactive(self) -> bool:
        return self.role in {
            "button",
            "link",
            "textbox",
            "combobox",
            "checkbox",
            "radio",
            "menuitem",
            "tab",
        }


@dataclass
class Observation:
    """A single perception of the surface: what a human would see right now."""

    url: str
    title: str
    nodes: list[UiNode]
    text: str
    frames: list[dict[str, Any]]
    screenshot: bytes | None = None
    captured_at: str = ""

    def by_ref(self, ref: str) -> UiNode | None:
        return next((n for n in self.nodes if n.ref == ref), None)

    def interactive_nodes(self) -> list[UiNode]:
        return [n for n in self.nodes if n.interactive]


@dataclass(frozen=True)
class ActionOutcome:
    ok: bool
    detail: str = ""
    navigated: bool = False


class Surface(ABC):
    """Perception + physical action for one application surface.

    The action primitives are intentionally the ones a human has: move to a
    point and click it, type into whatever has focus, press a key. The only
    non-physical primitives are ``read_node`` and ``set_combobox_value``, both
    of which have direct desktop-accessibility equivalents (``AXValue`` /
    ``UIA ValuePattern``) and neither of which leaks markup into the artifact.
    """

    kind: str = "abstract"

    @abstractmethod
    def capabilities(self) -> set[StrategyKind]:
        """Targeting strategies this surface can resolve."""

    @abstractmethod
    def open(self, url: str) -> ActionOutcome: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def observe(self, *, screenshot: bool = True) -> Observation: ...

    @abstractmethod
    def click_point(self, x: float, y: float) -> ActionOutcome: ...

    @abstractmethod
    def type_text(self, text: str) -> ActionOutcome:
        """Type into whatever currently has focus."""

    @abstractmethod
    def press(self, key: str) -> ActionOutcome: ...

    @abstractmethod
    def clear_focused(self) -> ActionOutcome: ...

    @abstractmethod
    def set_combobox_value(self, node: UiNode, value: str) -> ActionOutcome: ...

    @abstractmethod
    def read_node(self, node: UiNode, attribute: str = "text") -> str: ...

    @abstractmethod
    def scroll_to(self, node: UiNode) -> ActionOutcome: ...

    @abstractmethod
    def screenshot(self, *, mask: list[Rect] | None = None) -> bytes: ...

    def is_alive(self) -> bool:
        """Is the session still usable?

        Not abstract: a surface that cannot lose its session inherits ``True``.
        A browser can — the operator can close the window mid-handoff — and the
        engine needs to say so plainly rather than failing on the next action.
        """
        return True

    @abstractmethod
    def close(self) -> None: ...
