"""Pre-flight: replay refuses a surface that cannot resolve what it recorded.

``SurfaceBinding.required_strategies`` is the union of every targeting strategy
the recording used anywhere, so the check is conservative by construction. These
tests pin the two things that matter: it fires *before* the first step, and it
does not fire on a surface that supports everything.

No browser: a run that gets past pre-flight on ``DesktopSurface`` would raise
``DesktopSurfaceNotImplemented`` from ``open()`` instead, which is exactly the
distinction being tested.
"""

from __future__ import annotations

from cua.artifact.schema import StrategyKind
from cua.handoff.control import SessionControl
from cua.observability.evidence import EvidenceWriter
from cua.observability.logging import RunLogger
from cua.replay.engine import ReplayEngine
from cua.replay.result import ReplayStatus
from cua.safety.allowlist import load_profile
from cua.safety.policy import PolicyGate
from cua.safety.redact import Redactor
from cua.surface.desktop import DesktopSurface
from fixture_artifact import build_artifact


def _artifact_requiring(*strategies: StrategyKind):
    """The fixture artifact, re-signed after declaring what a surface must support."""
    artifact = build_artifact()
    artifact.surface.required_strategies = list(strategies)
    return artifact.with_checksum()


def _engine(artifact, surface, tmp_path):
    redactor = Redactor()
    run_id = "preflight_" + tmp_path.name
    evidence = EvidenceWriter("replay", run_id, redactor, root=tmp_path / "evidence")
    logger = RunLogger(evidence.log_path, run_id=run_id, phase="replay", redactor=redactor,
                       echo=False)
    engine = ReplayEngine(
        artifact, surface, PolicyGate(load_profile("default")), redactor=redactor,
        logger=logger, evidence=evidence, control=SessionControl(run_id=run_id),
        config={"base_url": "http://127.0.0.1:8799"}, run_id=run_id,
    )
    return engine, logger


def test_unsupported_strategy_fails_before_the_first_step(tmp_path):
    artifact = _artifact_requiring(StrategyKind.role_name, StrategyKind.dom_hint)

    engine, logger = _engine(artifact, DesktopSurface("MeridianCore.app"), tmp_path)
    result = engine.run({"member_id": "100244"})
    logger.close()

    assert result.status is ReplayStatus.failure
    assert result.failure is not None
    assert result.failure.error_class == "surface_unsupported"
    # The one strategy a desktop surface genuinely cannot resolve, named.
    assert "dom_hint" in result.failure.message
    assert "role_name" not in result.failure.message
    # Loudly, and up front: the application was never touched.
    assert result.steps == []


def test_portable_artifact_passes_preflight_on_desktop(tmp_path):
    """Every strategy a desktop accessibility API has an analogue for."""
    artifact = _artifact_requiring(
        StrategyKind.role_name,
        StrategyKind.label_proximity,
        StrategyKind.table_cell,
    )

    engine, logger = _engine(artifact, DesktopSurface("MeridianCore.app"), tmp_path)
    engine._check_surface_support()  # does not raise
    logger.close()


def test_artifact_declaring_nothing_is_not_gated(tmp_path):
    """An empty list means "not recorded", which must not be read as "needs nothing forbidden"."""
    artifact = _artifact_requiring()

    engine, logger = _engine(artifact, DesktopSurface("MeridianCore.app"), tmp_path)
    engine._check_surface_support()  # does not raise
    logger.close()
