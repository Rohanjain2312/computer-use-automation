"""Predicate evaluation.

One evaluator serves checkpoints, business-outcome detectors, recovery triggers
and hard-failure detectors, so all four have identical semantics and one place
to be wrong. Each evaluation returns *why* it decided, which is what makes a
failed checkpoint debuggable instead of merely false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..artifact.schema import Predicate, TargetPlan, TargetStrategy, StrategyKind
from ..locate.strategies import _cmp, _norm, match_strategy
from ..surface.base import Observation


@dataclass(frozen=True)
class Verdict:
    ok: bool
    detail: str
    observed: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _urls(obs: Observation) -> list[str]:
    urls = [obs.url]
    for frame in obs.frames:
        u = frame.get("url")
        if u:
            urls.append(u)
    return urls


def _plan_from(params: dict) -> TargetPlan:
    kind = StrategyKind(params.get("strategy", "role_name"))
    return TargetPlan(
        description=params.get("description", "checkpoint target"),
        strategies=[
            TargetStrategy(kind=kind, params=params.get("params", {}), confidence=1.0,
                           rationale="checkpoint predicate")
        ],
        frame=params.get("frame"),
        require_unique=False,
    )


def evaluate(predicate: Predicate, obs: Observation) -> Verdict:
    kind = predicate.kind
    p = predicate.params

    if kind == "all_of":
        results = [evaluate(c, obs) for c in predicate.children]
        failed = [r for r in results if not r.ok]
        return Verdict(not failed, "all conditions held" if not failed
                       else "; ".join(r.detail for r in failed))

    if kind == "any_of":
        results = [evaluate(c, obs) for c in predicate.children]
        passed = next((r for r in results if r.ok), None)
        return Verdict(passed is not None, passed.detail if passed
                       else "no alternative held: " + "; ".join(r.detail for r in results))

    if kind == "not":
        inner = evaluate(predicate.children[0], obs)
        return Verdict(not inner.ok, f"negation of ({inner.detail})")

    if kind in {"text_present", "text_absent"}:
        needle = p.get("text", "")
        mode = p.get("match", "contains")
        if mode == "regex":
            present = bool(re.search(needle, obs.text, re.I))
        else:
            present = _norm(needle) in _norm(obs.text)
        want = kind == "text_present"
        return Verdict(
            present == want,
            f"text {needle!r} {'found' if present else 'not found'} on screen "
            f"(expected {'present' if want else 'absent'})",
            observed=_excerpt(obs.text, needle),
        )

    if kind in {"element_present", "element_absent"}:
        plan = _plan_from(p)
        matches = match_strategy(plan.strategies[0], obs, plan.frame)
        present = bool(matches)
        want = kind == "element_present"
        return Verdict(
            present == want,
            f"{plan.strategies[0].kind.value} {plan.strategies[0].params} matched {len(matches)} "
            f"control(s) (expected {'>=1' if want else '0'})",
            observed=", ".join(f"{m.role}:{m.name}" for m in matches[:5]),
        )

    if kind == "url_matches":
        pattern = p.get("pattern", "")
        scope = p.get("scope", "any")
        candidates = _urls(obs) if scope == "any" else [obs.url]
        hit = next((u for u in candidates if re.search(pattern, u)), None)
        return Verdict(hit is not None,
                       f"url pattern {pattern!r} {'matched ' + hit if hit else 'matched no url'}",
                       observed=" | ".join(candidates))

    if kind == "table_cell_matches":
        plan = TargetPlan(
            description="table cell",
            strategies=[TargetStrategy(kind=StrategyKind.table_cell, params={
                "row_label": p.get("row_label", ""),
                "column": p.get("column", ""),
                "headers": p.get("headers"),
                "row_match": p.get("row_match", "ci_exact"),
            }, confidence=1.0, rationale="checkpoint predicate")],
            frame=p.get("frame"), require_unique=False,
        )
        matches = match_strategy(plan.strategies[0], obs, plan.frame)
        expected = p.get("equals")
        contains = p.get("contains")
        if not matches:
            return Verdict(False, f"no cell found for row {p.get('row_label')!r} / "
                                  f"column {p.get('column')!r}")
        text = matches[0].text
        if expected is not None:
            return Verdict(_cmp(text, expected, "ci_exact"),
                           f"cell text {text!r} vs expected {expected!r}", observed=text)
        if contains is not None:
            return Verdict(_cmp(text, contains, "contains"),
                           f"cell text {text!r} should contain {contains!r}", observed=text)
        return Verdict(bool(text.strip()), f"cell has text {text!r}", observed=text)

    return Verdict(False, f"unknown predicate kind {kind!r}")  # pragma: no cover


def _excerpt(text: str, needle: str, width: int = 90) -> str:
    idx = _norm(text).find(_norm(needle))
    if idx < 0:
        return text[:width].replace("\n", " ")
    start = max(0, idx - width // 3)
    return text[start : start + width].replace("\n", " ")
