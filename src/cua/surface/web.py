"""Playwright-backed web surface.

This is the only module in the project that knows Playwright exists.

It deliberately acts the way a person acts: it resolves a control to a point on
screen and clicks that point, then types into whatever took focus. It does not
drive elements through CSS selectors, because the target class of applications
(framesets, table layouts, no test ids) does not reliably have usable ones, and
because coordinate-plus-accessible-name is the action model a desktop surface
would also use.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from ..artifact.schema import StrategyKind
from .base import ActionOutcome, Observation, Rect, Surface, TableContext, UiNode

_PERCEIVE_JS = (Path(__file__).parent / "perceive.js").read_text()

_MASK_JS = """(rects) => {
  const prev = document.getElementById('__cua_mask');
  if (prev) prev.remove();
  if (!rects || !rects.length) return;
  const host = document.createElement('div');
  host.id = '__cua_mask';
  host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none';
  for (const r of rects) {
    const d = document.createElement('div');
    d.style.cssText = `position:absolute;left:${r.x}px;top:${r.y}px;width:${r.width}px;` +
      `height:${r.height}px;background:#111;border:1px solid #444`;
    host.appendChild(d);
  }
  document.documentElement.appendChild(host);
}"""

_UNMASK_JS = "() => { const p = document.getElementById('__cua_mask'); if (p) p.remove(); }"

_READ_JS = """(a) => {
  const el = (window.__cua && window.__cua.nodes) ? window.__cua.nodes[a.i] : null;
  if (!el) return null;
  if (a.attribute === 'value') return String(el.value ?? '');
  return (el.innerText || el.textContent || '').trim();
}"""

_SET_SELECT_JS = """(a) => {
  const el = (window.__cua && window.__cua.nodes) ? window.__cua.nodes[a.i] : null;
  if (!el) return false;
  const want = String(a.value);
  let matched = null;
  for (const o of el.options || []) {
    if (o.value === want || (o.label || o.text || '').trim() === want) { matched = o; break; }
  }
  if (!matched) return false;
  el.value = matched.value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}"""

_SCROLL_JS = """(a) => {
  const el = (window.__cua && window.__cua.nodes) ? window.__cua.nodes[a.i] : null;
  if (!el) return false;
  el.scrollIntoView({ block: 'center', inline: 'center' });
  return true;
}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PlaywrightWebSurface(Surface):
    kind = "web"

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        slow_mo_ms: int = 0,
        nav_timeout_ms: int = 20_000,
    ) -> None:
        self._viewport = viewport or {"width": 1280, "height": 900}
        self._headless = headless
        self._slow_mo = slow_mo_ms
        self._nav_timeout = nav_timeout_ms
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.dialog_log: list[dict[str, Any]] = []
        self._start()

    # -- lifecycle --------------------------------------------------------

    def _start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            slow_mo=self._slow_mo,
            args=["--disable-features=Translate", "--no-first-run"],
        )
        self._context = self._browser.new_context(viewport=self._viewport)
        self._context.set_default_timeout(self._nav_timeout)
        self._page = self._context.new_page()
        self._page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        """Native alert/confirm/prompt. Recorded, then dismissed conservatively.

        A native dialog is an unexpected state, not a decision the surface is
        allowed to make, so it is always dismissed and surfaced to the engine,
        which decides via recovery/failure rules.
        """
        self.dialog_log.append(
            {"at": _now(), "type": dialog.type, "message": dialog.message, "action": "dismiss"}
        )
        try:
            dialog.dismiss()
        except Exception:  # pragma: no cover - dialog may already be gone
            pass

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._pw = self._browser = self._context = self._page = None

    # -- capabilities -----------------------------------------------------

    def capabilities(self) -> set[StrategyKind]:
        return {
            StrategyKind.role_name,
            StrategyKind.label_proximity,
            StrategyKind.text_anchor,
            StrategyKind.table_cell,
            StrategyKind.dom_hint,
            StrategyKind.viewport_ratio,
        }

    # -- perception -------------------------------------------------------

    def current_url(self) -> str:
        return self.page.url

    def _settle(self, settle_ms: int = 200) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self._nav_timeout)
        except Exception:
            pass
        if settle_ms:
            self.page.wait_for_timeout(settle_ms)

    def observe(self, *, screenshot: bool = True, settle_ms: int = 150) -> Observation:
        self._settle(settle_ms)
        raw = self.page.evaluate(_PERCEIVE_JS, {"maxNodes": 400, "maxText": 14000})
        nodes = [self._to_node(n) for n in raw["nodes"]]
        shot = None
        if screenshot:
            secret_rects = [n.rect for n in nodes if n.secret]
            shot = self.screenshot(mask=secret_rects)
        return Observation(
            url=raw["url"],
            title=raw["title"],
            nodes=nodes,
            text=raw["text"],
            frames=raw["frames"],
            screenshot=shot,
            captured_at=_now(),
        )

    @staticmethod
    def _to_node(n: dict[str, Any]) -> UiNode:
        tbl = n.get("table")
        return UiNode(
            ref=n["ref"],
            role=n["role"],
            name=n.get("name", ""),
            name_source=n.get("name_source", "none"),
            value=n.get("value", ""),
            text=n.get("text", ""),
            enabled=bool(n.get("enabled", True)),
            secret=bool(n.get("secret", False)),
            in_viewport=bool(n.get("in_viewport", True)),
            rect=Rect(**n["rect"]),
            frame_path=n.get("frame_path", ""),
            frame_name=n.get("frame_name", ""),
            label_candidates=list(n.get("label_candidates") or []),
            label_left=n.get("label_left", ""),
            label_above=n.get("label_above", ""),
            table=(
                TableContext(
                    headers=list(tbl.get("headers") or []),
                    row_index=int(tbl.get("row_index", -1)),
                    col_index=int(tbl.get("col_index", -1)),
                    row_label=tbl.get("row_label", ""),
                    col_header=tbl.get("col_header", ""),
                    header_confident=bool(tbl.get("header_confident", False)),
                )
                if tbl
                else None
            ),
            dom_hint=dict(n.get("dom_hint") or {}),
        )

    # -- action -----------------------------------------------------------

    def open(self, url: str) -> ActionOutcome:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout)
            return ActionOutcome(True, f"navigated to {url}", navigated=True)
        except Exception as exc:
            return ActionOutcome(False, f"navigation failed: {exc}")

    def click_point(self, x: float, y: float) -> ActionOutcome:
        before = self.page.url
        try:
            self.page.mouse.click(x, y)
        except Exception as exc:
            return ActionOutcome(False, f"click at ({x:.0f},{y:.0f}) failed: {exc}")
        self._settle(150)
        return ActionOutcome(
            True, f"clicked ({x:.0f},{y:.0f})", navigated=self.page.url != before
        )

    def type_text(self, text: str) -> ActionOutcome:
        try:
            self.page.keyboard.type(text, delay=12)
            return ActionOutcome(True, f"typed {len(text)} chars")
        except Exception as exc:
            return ActionOutcome(False, f"type failed: {exc}")

    def press(self, key: str) -> ActionOutcome:
        before = self.page.url
        try:
            self.page.keyboard.press(key)
        except Exception as exc:
            return ActionOutcome(False, f"press {key} failed: {exc}")
        self._settle(150)
        return ActionOutcome(True, f"pressed {key}", navigated=self.page.url != before)

    def clear_focused(self) -> ActionOutcome:
        try:
            self.page.keyboard.press("ControlOrMeta+a")
            self.page.keyboard.press("Delete")
            return ActionOutcome(True, "cleared focused field")
        except Exception as exc:
            return ActionOutcome(False, f"clear failed: {exc}")

    def set_combobox_value(self, node: UiNode, value: str) -> ActionOutcome:
        ok = self.page.evaluate(_SET_SELECT_JS, {"i": self._idx(node), "value": value})
        return ActionOutcome(bool(ok), f"set combobox {node.name!r} -> {value!r}")

    def read_node(self, node: UiNode, attribute: str = "text") -> str:
        out = self.page.evaluate(_READ_JS, {"i": self._idx(node), "attribute": attribute})
        return "" if out is None else str(out)

    def scroll_to(self, node: UiNode) -> ActionOutcome:
        ok = self.page.evaluate(_SCROLL_JS, {"i": self._idx(node)})
        if ok:
            self.page.wait_for_timeout(120)
        return ActionOutcome(bool(ok), f"scrolled to {node.name!r}")

    @staticmethod
    def _idx(node: UiNode) -> int:
        return int(node.ref[1:])

    # -- evidence ---------------------------------------------------------

    def screenshot(self, *, mask: list[Rect] | None = None) -> bytes:
        rects = [
            {"x": r.x, "y": r.y, "width": r.width, "height": r.height}
            for r in (mask or [])
            if r.width > 0 and r.height > 0
        ]
        try:
            if rects:
                self.page.evaluate(_MASK_JS, rects)
            return self.page.screenshot(type="png")
        except Exception:
            return b""
        finally:
            if rects:
                try:
                    self.page.evaluate(_UNMASK_JS)
                except Exception:
                    pass

    def is_alive(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def dom_snapshot(self) -> str:
        """Richer failure signal: the rendered markup of every reachable frame."""
        parts: list[str] = []
        try:
            for frame in self.page.frames:
                try:
                    parts.append(f"<!-- frame url={frame.url} name={frame.name} -->")
                    parts.append(frame.content())
                except Exception as exc:
                    parts.append(f"<!-- frame unavailable: {exc} -->")
        except Exception as exc:  # pragma: no cover
            parts.append(f"<!-- snapshot failed: {exc} -->")
        return "\n".join(parts)

    def wait_ms(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)


def wait_for_app(url: str, timeout_s: float = 20.0) -> bool:
    """Poll a health endpoint until the mock app answers."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False
