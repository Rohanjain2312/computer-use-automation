"""Desktop surface — the seam, deliberately not implemented.

This file exists so the extension point is concrete rather than a paragraph in a
design document. It is an explicit cut: see REPORT.md § Cuts.

## Why the artifact already ports

Nothing above ``cua.surface`` knows what a browser is. Replay consumes
``UiNode`` objects — role, accessible name, value, bounding box, table context —
and issues physical actions: click a point, type into focus, press a key. Those
are exactly the primitives a desktop accessibility API provides, which is why the
targeting strategies in the artifact were chosen the way they were:

| Strategy            | Web (implemented)             | macOS AXUIElement            | Windows UI Automation          |
|---------------------|-------------------------------|------------------------------|--------------------------------|
| ``role_name``       | computed role + accname       | ``AXRole`` + ``AXTitle``/``AXDescription`` | ``ControlType`` + ``Name``     |
| ``label_proximity`` | nearest labelling cell/text   | ``AXTitleUIElement``, else geometry | ``LabeledBy``, else geometry   |
| ``text_anchor``     | visible text                  | ``AXValue`` / ``AXStaticText``| ``Text`` pattern               |
| ``table_cell``      | table headers + row label     | ``AXTable``/``AXRow``/``AXColumn`` | ``Grid``/``Table`` patterns    |
| ``dom_hint``        | **web only** — skipped        | not applicable               | not applicable                 |
| ``viewport_ratio``  | normalized coordinates        | screen coordinates           | screen coordinates             |

``dom_hint`` is the only row with no desktop analogue at all; ``viewport_ratio``
ports, but as screen coordinates it is a last resort on either surface.

``Surface.capabilities()`` is what makes that safe, and it is enforced at two
different granularities:

* **Before step 0.** ``ReplayEngine._check_surface_support`` compares the
  artifact's ``surface.required_strategies`` against ``capabilities()`` and
  fails with ``error_class="surface_unsupported"`` naming the missing
  strategies, without touching the application.
* **Within a step.** ``locate.strategies.resolve`` skips an unsupported
  strategy and records the skip in the step's locator attempts, so a plan that
  has a portable fallback still resolves.

Be precise about what the first of those actually buys, because it is coarser
than it looks. ``required_strategies`` is synthesized as the *union* of every
strategy on every target, and candidate generation always appends a
``dom_hint`` and a ``viewport_ratio`` fallback — so every web-recorded artifact
lists ``dom_hint``, and the pre-flight therefore refuses web recordings on a
desktop surface **as a class**. That is a defensible default (refusing up front
beats discovering it with the application half-driven) but it is not a
per-capability portability verdict, and it does not distinguish a flow whose
every step has a portable primary from one that genuinely depends on markup.
Making it precise means recording the minimal set each target actually relies
on rather than everything generated for it; see REPORT.md § Cuts.

## What implementing this actually takes

1. ``observe()`` — walk the AX tree of the focused application, mapping each
   element to ``UiNode``. Table context comes from the row/column relationships
   the platform already exposes. This is the bulk of the work, and it is the
   direct analogue of ``perceive.js``.
2. Action primitives — synthesize events (macOS ``CGEvent``, Windows
   ``SendInput``), or use ``AXPress``/``Invoke`` patterns where clicking a point
   is unreliable.
3. ``screenshot()`` — platform capture, masking the rects of secret fields
   exactly as the web surface does.
4. Nothing else. The replay engine, artifact schema, safety gate, evidence and
   handoff machinery are unchanged.

The handoff story is also unchanged in shape but different in mechanism: the
"same live session" for a desktop app is the same OS window on the same host, so
control transfer means letting the operator drive that host (an RDP/VNC style
session) while automation stops synthesizing input — the ``SessionControl``
invariant is identical.
"""

from __future__ import annotations

from ..artifact.schema import StrategyKind
from .base import ActionOutcome, Observation, Rect, Surface, UiNode

SUPPORTED_STRATEGIES = {
    StrategyKind.role_name,
    StrategyKind.label_proximity,
    StrategyKind.text_anchor,
    StrategyKind.table_cell,
    StrategyKind.viewport_ratio,
}


class DesktopSurfaceNotImplemented(NotImplementedError):
    """Raised with a pointer to the mapping above rather than a bare stub error."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"the desktop surface does not implement {what!r}. This is a documented cut "
            "(REPORT.md § Cuts); see the module docstring for the AX/UIA mapping each "
            "method needs."
        )


class DesktopSurface(Surface):
    """Interface-complete placeholder for a native desktop surface."""

    kind = "desktop"

    def __init__(self, application: str) -> None:
        self.application = application

    def capabilities(self) -> set[StrategyKind]:
        # Declared honestly: a desktop surface can never resolve dom_hint, and an
        # artifact that depends on it must fail before it starts clicking.
        return set(SUPPORTED_STRATEGIES)

    def open(self, url: str) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("open")

    def current_url(self) -> str:
        raise DesktopSurfaceNotImplemented("current_url")

    def observe(self, *, screenshot: bool = True) -> Observation:
        raise DesktopSurfaceNotImplemented("observe")

    def click_point(self, x: float, y: float) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("click_point")

    def type_text(self, text: str) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("type_text")

    def press(self, key: str) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("press")

    def clear_focused(self) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("clear_focused")

    def set_combobox_value(self, node: UiNode, value: str) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("set_combobox_value")

    def read_node(self, node: UiNode, attribute: str = "text") -> str:
        raise DesktopSurfaceNotImplemented("read_node")

    def scroll_to(self, node: UiNode) -> ActionOutcome:
        raise DesktopSurfaceNotImplemented("scroll_to")

    def screenshot(self, *, mask: list[Rect] | None = None) -> bytes:
        raise DesktopSurfaceNotImplemented("screenshot")

    def close(self) -> None:
        return None
