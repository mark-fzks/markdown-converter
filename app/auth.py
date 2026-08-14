"""HTTP Basic auth, enabled whenever AUTH_USERNAME and AUTH_PASSWORD are set."""

from __future__ import annotations

import logging
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Settings, get_settings

log = logging.getLogger(__name__)

_basic = HTTPBasic(auto_error=False)


def require_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    settings: Settings = Depends(get_settings),
) -> str | None:
    """Guard a route. Returns the username, or None when auth is disabled."""
    if not settings.auth_enabled:
        return None

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{settings.auth_realm}"'},
    )

    if credentials is None:
        raise unauthorized

    # Both comparisons always run, so a wrong username costs the same as a
    # wrong password.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.auth_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), settings.auth_password.encode("utf-8")
    )
    if not (user_ok and password_ok):
        raise unauthorized

    return credentials.username


def warn_if_open(settings: Settings) -> None:
    if not settings.auth_enabled:
        log.warning(
            "AUTH_USERNAME/AUTH_PASSWORD are not set - this service is open to anyone "
            "who can reach it. Set both before exposing it to the internet."
        )
