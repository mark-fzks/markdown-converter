from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from app.config import get_settings
from app.downloads import DownloadStore
from app.mcp_server import build_mcp_server
from app.oauth import SingleUserOAuthProvider

from .conftest import pandoc_required

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every async test in this module runs on asyncio via the anyio plugin.
pytestmark = pytest.mark.anyio


@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("THEMES_DIR", str(REPO_ROOT / "themes"))
    monkeypatch.setenv("MERMAID_ENABLED", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://md.example.com")
    monkeypatch.setenv("AUTH_USERNAME", "mark")
    monkeypatch.setenv("AUTH_PASSWORD", "hunter2-and-then-some")
    monkeypatch.setenv("OAUTH_CLIENT_ID", "claude-web")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "shhh")
    monkeypatch.setenv("OAUTH_REDIRECT_URIS", "https://claude.ai/api/mcp/auth_callback")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def store():
    s = DownloadStore(ttl_seconds=60)
    yield s
    s.shutdown()


@pytest.fixture
def server(mcp_env, store):
    return build_mcp_server(store, mcp_env)


# --- tool surface ----------------------------------------------------------


async def test_exposes_the_expected_tools(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {"list_formats", "convert", "convert_file_from", "preview"}


async def test_tools_document_their_arguments(server):
    tools = {t.name: t for t in await server.list_tools()}
    convert = tools["convert"]
    props = convert.input_schema["properties"]
    assert "markdown" in props and "to" in props
    # Descriptions are what Claude reads to decide how to call the tool.
    assert props["to"]["description"]
    assert convert.description


async def test_list_formats_reports_capabilities(server):
    result = await server.call_tool("list_formats", {})
    data = result.structured_content
    keys = {f["key"] for f in data["formats"]}
    assert {"pdf", "docx", "html"} <= keys
    assert data["pdf_engines"] == ["typst", "weasyprint"]
    assert "default" in data["themes"]


# --- conversion ------------------------------------------------------------


@pandoc_required
async def test_text_format_comes_back_inline(server):
    result = await server.call_tool(
        "convert", {"markdown": "# Hi\n\nSome *text*.", "to": "html-fragment"}
    )
    data = result.structured_content
    assert "<h1" in data["content"]
    assert "download_url" not in data


@pandoc_required
async def test_preview_returns_html(server):
    result = await server.call_tool("preview", {"markdown": "# Title\n\ntext"})
    assert "<h1" in result.structured_content["content"]


@pandoc_required
async def test_binary_format_comes_back_as_a_link(server, store):
    result = await server.call_tool(
        "convert", {"markdown": "# Report\n\nBody.", "to": "docx", "filename": "report"}
    )
    data = result.structured_content

    assert data["download_url"].startswith("https://md.example.com/download/")
    assert data["filename"] == "report.docx"
    assert data["bytes"] > 0
    assert "content" not in data

    token = data["download_url"].split("/download/")[1].split("/")[0]
    entry = store.get(token)
    assert entry is not None and entry.path.is_file()


@pandoc_required
async def test_unknown_format_is_a_helpful_error(server):
    # In-process a failing tool raises; the protocol layer turns this into a
    # CallToolResult with isError set, which is what Claude actually sees.
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("convert", {"markdown": "# x", "to": "wingdings"})
    assert "list_formats" in str(excinfo.value)


async def test_convert_file_rejects_relative_paths(server):
    with pytest.raises(ToolError, match="absolute path"):
        await server.call_tool("convert_file_from", {"source": "notes.md"})


@pandoc_required
async def test_convert_file_reads_a_container_path(server, tmp_path):
    doc = tmp_path / "notes.md"
    doc.write_text("# From disk\n\nbody\n", encoding="utf-8")
    result = await server.call_tool(
        "convert_file_from", {"source": str(doc), "to": "html-fragment"}
    )
    assert "From disk" in result.structured_content["content"]


# --- download store --------------------------------------------------------


def test_store_serves_then_expires(tmp_path):
    store = DownloadStore(ttl_seconds=0)
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF-1.4")
    entry = store.add(source, "a.pdf", "application/pdf")

    time.sleep(0.01)
    assert store.get(entry.token) is None  # swept on access
    store.shutdown()


def test_store_rejects_unknown_tokens(store, tmp_path):
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF-1.4")
    store.add(source, "a.pdf", "application/pdf")
    assert store.get("not-a-real-token") is None


def test_store_keeps_files_separate(store, tmp_path):
    source = tmp_path / "a.md"
    source.write_text("x")
    one = store.add(source, "a.md", "text/markdown")
    two = store.add(source, "a.md", "text/markdown")
    assert one.token != two.token
    assert one.path != two.path


# --- oauth -----------------------------------------------------------------


@pytest.fixture
def provider(mcp_env):
    return SingleUserOAuthProvider(mcp_env)


async def test_static_client_is_pre_registered(provider):
    client = await provider.get_client("claude-web")
    assert client is not None
    assert client.client_secret == "shhh"
    assert str(client.redirect_uris[0]).startswith("https://claude.ai/")


async def test_unknown_client_is_rejected(provider):
    assert await provider.get_client("someone-else") is None


async def _start(provider):
    from mcp.server.auth.provider import AuthorizationParams

    client = await provider.get_client("claude-web")
    params = AuthorizationParams(
        state="xyz",
        scopes=["convert"],
        code_challenge="challenge",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
    )
    url = await provider.authorize(client, params)
    return client, url.split("request=")[1]


async def test_authorize_sends_the_user_to_the_login_form(provider):
    _, handle = await _start(provider)
    assert provider.pending(handle) is not None


async def test_wrong_password_does_not_issue_a_code(provider):
    _, handle = await _start(provider)
    assert provider.complete_login(handle, "mark", "wrong") is None
    # The request survives so the user can retry.
    assert provider.pending(handle) is not None


async def test_full_authorization_code_flow(provider):
    client, handle = await _start(provider)

    redirect = provider.complete_login(handle, "mark", "hunter2-and-then-some")
    assert redirect.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert "state=xyz" in redirect

    code_value = redirect.split("code=")[1].split("&")[0]
    code = await provider.load_authorization_code(client, code_value)
    assert code is not None and code.code_challenge == "challenge"

    token = await provider.exchange_authorization_code(client, code)
    assert token.token_type == "Bearer"
    assert await provider.load_access_token(token.access_token) is not None

    # Codes are single use.
    assert await provider.load_authorization_code(client, code_value) is None


async def test_refresh_token_rotates(provider):
    client, handle = await _start(provider)
    redirect = provider.complete_login(handle, "mark", "hunter2-and-then-some")
    code = await provider.load_authorization_code(
        client, redirect.split("code=")[1].split("&")[0]
    )
    first = await provider.exchange_authorization_code(client, code)

    refresh = await provider.load_refresh_token(client, first.refresh_token)
    second = await provider.exchange_refresh_token(client, refresh, ["convert"])
    assert second.access_token != first.access_token
    # The spent refresh token is gone.
    assert await provider.load_refresh_token(client, first.refresh_token) is None


async def test_revoking_an_access_token_invalidates_it(provider):
    client, handle = await _start(provider)
    redirect = provider.complete_login(handle, "mark", "hunter2-and-then-some")
    code = await provider.load_authorization_code(
        client, redirect.split("code=")[1].split("&")[0]
    )
    token = await provider.exchange_authorization_code(client, code)

    access = await provider.load_access_token(token.access_token)
    await provider.revoke_token(access)
    assert await provider.load_access_token(token.access_token) is None


# --- the MCP endpoint is guarded ------------------------------------------
#
# Regressions from the first MCP release: /mcp (no trailing slash) answered a
# 307 that MCP clients will not follow on POST, and the mounted app bypassed
# Basic auth entirely because FastAPI dependencies do not reach a sub-app.
# These use a bare TestClient so the lifespan (and the single-run MCP session
# manager) never starts; a 401 is decided in middleware, before the mount.

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def guarded_client(mcp_env):
    from fastapi.testclient import TestClient

    import app.main as main

    # No `with`: the lifespan, and the MCP session manager, stay unstarted.
    return TestClient(main.app)


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_rejects_anonymous_callers(guarded_client, path):
    response = guarded_client.post(path, json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_rejects_wrong_credentials(guarded_client, path):
    response = guarded_client.post(
        path, json=INITIALIZE, headers=MCP_HEADERS, auth=("mark", "wrong")
    )
    assert response.status_code == 401


def test_mcp_without_a_slash_is_not_a_redirect(guarded_client):
    """MCP clients do not follow a redirect on POST, so /mcp must be terminal."""
    response = guarded_client.post(
        "/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False
    )
    assert response.status_code != 307


def test_challenge_advertises_oauth_when_configured(guarded_client):
    response = guarded_client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
    challenge = response.headers["www-authenticate"]
    assert "Bearer" in challenge and "resource_metadata=" in challenge
    assert "Basic" in challenge
