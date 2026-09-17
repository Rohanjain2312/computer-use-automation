from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("MERIDIAN_OPERATOR_ID", "ops.demo")
os.environ.setdefault("MERIDIAN_OPERATOR_PASSCODE", "demo-not-a-real-password")
os.environ.setdefault("MERIDIAN_OVERRIDE_CODE", "OVR-4417")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_app():
    """The MeridianCore mock console, on its own port, for the whole session."""
    from werkzeug.serving import make_server

    from mockapp.app import create_app

    port = _free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    from cua.surface.web import wait_for_app

    assert wait_for_app(base + "/healthz", timeout_s=15), "mock app did not start"
    yield base
    server.shutdown()


@pytest.fixture
def reset_faults(mock_app):
    import urllib.request

    def _set(mode: str) -> None:
        urllib.request.urlopen(f"{mock_app}/admin/inject?mode={mode}", timeout=5).read()

    _set("none")
    yield _set
    _set("none")


@pytest.fixture
def allowlist(mock_app, tmp_path):
    """An allowlist profile bound to the test app's port."""
    import yaml

    from cua.safety.allowlist import DEFAULT_CONFIG, load_profile

    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    data["profiles"]["default"]["allowed_origins"] = [mock_app]
    path = tmp_path / "allowlist.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_profile("default", path)


@pytest.fixture
def surface():
    from cua.surface.web import PlaywrightWebSurface

    s = PlaywrightWebSurface(headless=True)
    yield s
    s.close()
