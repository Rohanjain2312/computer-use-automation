"""Synthetic observations, so locator and predicate logic can be tested without a browser."""

from __future__ import annotations

from cua.surface.base import Observation, Rect, TableContext, UiNode


def node(ref: str, role: str, name: str, **kw) -> UiNode:
    table = kw.pop("table", None)
    return UiNode(
        ref=ref, role=role, name=name,
        name_source=kw.pop("name_source", "inner-text"),
        value=kw.pop("value", ""), text=kw.pop("text", name),
        rect=Rect(*kw.pop("rect", (10.0, 10.0, 80.0, 20.0))),
        frame_name=kw.pop("frame", ""), frame_path=kw.pop("frame_path", ""),
        label_candidates=kw.pop("label_candidates", []),
        label_left=kw.pop("label_left", ""), label_above=kw.pop("label_above", ""),
        dom_hint=kw.pop("dom_hint", {}), secret=kw.pop("secret", False),
        table=TableContext(**table) if table else None,
        **kw,
    )


def accounts_table_nodes() -> list[UiNode]:
    headers = ["Type", "Account", "Current Balance", "Status", "Opened"]
    rows = [
        ("Share Draft (Checking)", "****4417", "$1,208.55", "OPEN", "2014-03-19"),
        ("Regular Savings", "****9031", "$4,812.37", "OPEN", "2014-03-19"),
    ]
    out: list[UiNode] = []
    for c, header in enumerate(headers):
        out.append(node(f"h{c}", "cell", header, table={
            "headers": headers, "row_index": 0, "col_index": c, "row_label": headers[0],
            "col_header": header, "header_confident": True}, frame="acctframe"))
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row):
            out.append(node(f"c{r}{c}", "cell", cell, table={
                "headers": headers, "row_index": r, "col_index": c, "row_label": row[0],
                "col_header": headers[c], "header_confident": True}, frame="acctframe"))
    return out


def observation(nodes: list[UiNode], *, url: str = "http://127.0.0.1:8799/",
                text: str | None = None, frames: list[dict] | None = None,
                title: str = "MeridianCore") -> Observation:
    return Observation(
        url=url, title=title, nodes=nodes,
        text=text if text is not None else "\n".join(n.text for n in nodes),
        frames=frames if frames is not None else [], screenshot=None, captured_at="t0")
