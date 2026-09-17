"""Filesystem artifact store.

Plain JSON files under ``artifacts/<capability_id>/<version>.json``, because the
artifact is meant to be *reviewed*: a JSON file in the repository diffs in a pull
request, is signed by a checksum, and needs no service to read. A database buys
nothing at this scale and would hide the thing the assignment cares about most.
"""

from __future__ import annotations

import json
from pathlib import Path

from .schema import CapabilityArtifact
from .validate import ArtifactInvalid, errors, validate_artifact

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_ROOT = REPO_ROOT / "artifacts"


class ArtifactStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else STORE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # -- write ------------------------------------------------------------

    def save(self, artifact: CapabilityArtifact, *, validate: bool = True) -> Path:
        if validate:
            issues = validate_artifact(artifact)
            if errors(issues):
                raise ArtifactInvalid(errors(issues))
        signed = artifact.with_checksum()
        path = self.path_for(signed.capability_id, signed.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(signed.model_dump(mode="json"), indent=2))
        self.reindex()
        return path

    def path_for(self, capability_id: str, version: str) -> Path:
        return self.root / capability_id / f"v{version}.json"

    # -- read -------------------------------------------------------------

    def load(self, path: Path | str) -> CapabilityArtifact:
        data = json.loads(Path(path).read_text())
        return CapabilityArtifact.model_validate(data)

    def get(self, capability_id: str, version: str | None = None) -> CapabilityArtifact:
        versions = self.versions(capability_id)
        if not versions:
            raise FileNotFoundError(f"no artifact stored for capability {capability_id!r}")
        if version in (None, "latest"):
            version = versions[-1]
        path = self.path_for(capability_id, version)
        if not path.exists():
            raise FileNotFoundError(
                f"capability {capability_id!r} has no version {version!r} (have {versions})"
            )
        return self.load(path)

    def versions(self, capability_id: str) -> list[str]:
        directory = self.root / capability_id
        if not directory.is_dir():
            return []
        found = [p.stem.lstrip("v") for p in directory.glob("v*.json")]
        return sorted(found, key=_version_key)

    def list_all(self) -> list[CapabilityArtifact]:
        out: list[CapabilityArtifact] = []
        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for version in self.versions(directory.name):
                try:
                    out.append(self.load(self.path_for(directory.name, version)))
                except Exception:  # pragma: no cover - a broken file must not hide the rest
                    continue
        return out

    # -- catalog ----------------------------------------------------------

    def reindex(self) -> Path:
        entries = []
        for artifact in self.list_all():
            entries.append(
                {
                    "capability_id": artifact.capability_id,
                    "version": artifact.version,
                    "name": artifact.name,
                    "status": artifact.status.value,
                    "description": artifact.description,
                    "surface": artifact.surface.kind,
                    "tenant": artifact.surface.tenant.tenant_id,
                    "variant": artifact.surface.tenant.variant,
                    "inputs": [
                        {"name": i.name, "type": i.type.value, "required": i.required,
                         "source": i.source}
                        for i in artifact.inputs
                    ],
                    "outputs": [{"name": o.name, "type": o.type.value} for o in artifact.outputs],
                    "business_outcomes": [o.name for o in artifact.business_outcomes],
                    "path": _repo_relative(self.path_for(artifact.capability_id, artifact.version)),
                    "checksum": artifact.checksum,
                }
            )
        index = self.root / "index.json"
        index.write_text(json.dumps({"capabilities": entries}, indent=2))
        return index


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _version_key(version: str) -> tuple:
    parts = []
    for chunk in version.split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)
