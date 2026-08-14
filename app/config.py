"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


@dataclass(frozen=True)
class Settings:
    # --- auth -------------------------------------------------------------
    auth_username: str = ""
    auth_password: str = ""
    auth_realm: str = "markdown-converter"

    # --- limits -----------------------------------------------------------
    max_upload_bytes: int = 32 * 1024 * 1024
    max_unpacked_bytes: int = 256 * 1024 * 1024
    max_batch_files: int = 200
    conversion_timeout: int = 120
    max_concurrent_conversions: int = 2

    # --- conversion defaults ---------------------------------------------
    default_pdf_engine: str = "typst"
    default_theme: str = "default"
    themes_dir: Path = Path("/app/themes")
    revealjs_url: str = "https://unpkg.com/reveal.js@4"

    # --- features ---------------------------------------------------------
    mermaid_enabled: bool = True
    mermaid_scale: int = 3
    mermaid_theme: str = "default"

    # --- MCP server -------------------------------------------------------
    mcp_enabled: bool = True
    #: Absolute URL the service is reached at; download links are built from it.
    public_base_url: str = "http://localhost:8000"
    download_ttl_seconds: int = 900
    #: Pre-registered OAuth client, for the ID/secret fields in claude.ai.
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_uris: tuple[str, ...] = ()

    # --- binaries ---------------------------------------------------------
    pandoc_bin: str = "pandoc"
    typst_bin: str = "typst"
    mermaid_bin: str = "mmdc"
    puppeteer_config: str = "/app/puppeteer-config.json"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password)

    @property
    def oauth_enabled(self) -> bool:
        """OAuth is only worth serving once a client has been pre-registered."""
        return bool(self.mcp_enabled and self.oauth_client_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        auth_username=_env_str("AUTH_USERNAME"),
        auth_password=_env_str("AUTH_PASSWORD"),
        auth_realm=_env_str("AUTH_REALM", "markdown-converter"),
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 32) * 1024 * 1024,
        max_unpacked_bytes=_env_int("MAX_UNPACKED_MB", 256) * 1024 * 1024,
        max_batch_files=_env_int("MAX_BATCH_FILES", 200),
        conversion_timeout=_env_int("CONVERSION_TIMEOUT", 120),
        max_concurrent_conversions=_env_int("MAX_CONCURRENT_CONVERSIONS", 2),
        default_pdf_engine=_env_str("DEFAULT_PDF_ENGINE", "typst"),
        default_theme=_env_str("DEFAULT_THEME", "default"),
        themes_dir=Path(_env_str("THEMES_DIR", "/app/themes")),
        revealjs_url=_env_str("REVEALJS_URL", "https://unpkg.com/reveal.js@4"),
        mermaid_enabled=_env_bool("MERMAID_ENABLED", True),
        mermaid_scale=_env_int("MERMAID_SCALE", 3),
        mermaid_theme=_env_str("MERMAID_THEME", "default"),
        mcp_enabled=_env_bool("MCP_ENABLED", True),
        public_base_url=_env_str("PUBLIC_BASE_URL", "http://localhost:8000"),
        download_ttl_seconds=_env_int("DOWNLOAD_TTL_SECONDS", 900),
        oauth_client_id=_env_str("OAUTH_CLIENT_ID"),
        oauth_client_secret=_env_str("OAUTH_CLIENT_SECRET"),
        oauth_redirect_uris=tuple(
            uri.strip() for uri in _env_str("OAUTH_REDIRECT_URIS").split(",") if uri.strip()
        ),
        pandoc_bin=_env_str("PANDOC_BIN", "pandoc"),
        typst_bin=_env_str("TYPST_BIN", "typst"),
        mermaid_bin=_env_str("MERMAID_BIN", "mmdc"),
        puppeteer_config=_env_str("PUPPETEER_CONFIG", "/app/puppeteer-config.json"),
    )
