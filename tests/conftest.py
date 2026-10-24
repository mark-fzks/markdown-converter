from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

pandoc_required = pytest.mark.skipif(
    shutil.which("pandoc") is None, reason="pandoc is not installed on this machine"
)
typst_required = pytest.mark.skipif(
    shutil.which("typst") is None, reason="typst is not installed on this machine"
)


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch):
    """Configure the service the way the container does, minus auth."""
    monkeypatch.setenv("THEMES_DIR", str(REPO_ROOT / "themes"))
    monkeypatch.setenv("MERMAID_ENABLED", "false")
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    get_settings.cache_clear()
    return monkeypatch


@pytest.fixture
def client(env):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def settings(env):
    return get_settings()


@pytest.fixture
def sample_markdown() -> str:
    return (
        "---\ntitle: Test Document\nauthor: Mark\n---\n\n"
        "# Heading\n\nSome *text* with a [link](https://example.com).\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "```python\nprint('hi')\n```\n"
    )


@pytest.fixture
def anyio_backend():
    """Run anyio-marked async tests on asyncio only, not trio."""
    return "asyncio"
