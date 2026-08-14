"""A single-user OAuth 2.1 authorization server for the MCP endpoint.

Claude Code and Claude Desktop can send a fixed ``Authorization`` header, so
they need none of this. claude.ai on the web speaks OAuth, so this module backs
the SDK's authorization-server routes with the same username and password the
rest of the service already uses.

"Single-user" is the whole design: there is one account (AUTH_USERNAME /
AUTH_PASSWORD), consent is implicit, and codes and tokens live in memory. A
restart invalidates outstanding tokens and Claude reconnects.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings

log = logging.getLogger(__name__)

AUTH_CODE_TTL = 300           # 5 minutes, per OAuth 2.1 guidance
ACCESS_TOKEN_TTL = 3600       # 1 hour
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days
SCOPE = "convert"
SUBJECT = "owner"


@dataclass
class _PendingAuthorization:
    """An authorization request parked while the owner logs in."""

    client_id: str
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)


class SingleUserOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider protocol.

    The SDK builds ``/authorize``, ``/token``, ``/register`` and the discovery
    metadata on top of this; the only thing left to us is deciding who the user
    is, which for a self-hosted converter is "whoever knows the password".
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, _PendingAuthorization] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

        self._register_static_client(settings)

    # -- static client (what you paste into claude.ai) ---------------------

    def _register_static_client(self, settings: Settings) -> None:
        """Pre-register the client whose ID/secret you enter in claude.ai.

        Dynamically registered clients are forgotten on restart; this one is
        rebuilt from the environment every boot, so the connector keeps working.
        """
        client_id = settings.oauth_client_id
        if not client_id:
            return

        self._clients[client_id] = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=settings.oauth_client_secret or None,
            # Claude's callback is not known ahead of time, so accept the hosts
            # the operator listed and validate the exact URI at authorize time.
            redirect_uris=list(settings.oauth_redirect_uris),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
            token_endpoint_auth_method=(
                "client_secret_post" if settings.oauth_client_secret else "none"
            ),
        )
        log.info("pre-registered OAuth client %s from the environment", client_id)

    # -- client registry ---------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        log.info("dynamically registered OAuth client %s", client_info.client_id)

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to our login form."""
        self._expire()
        handle = secrets.token_urlsafe(24)
        self._pending[handle] = _PendingAuthorization(client.client_id, params)
        return f"/oauth/login?{urlencode({'request': handle})}"

    def pending(self, handle: str) -> _PendingAuthorization | None:
        self._expire()
        return self._pending.get(handle)

    def complete_login(self, handle: str, username: str, password: str) -> str | None:
        """Verify the owner's credentials and return the redirect back to Claude.

        Returns ``None`` when the credentials are wrong, so the caller can
        re-render the form without disclosing which half failed.
        """
        pending = self.pending(handle)
        if pending is None:
            return None
        if not self._credentials_match(username, password):
            return None

        self._pending.pop(handle, None)
        params = pending.params

        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=SUBJECT,
        )
        self._codes[code.code] = code

        query = {"code": code.code}
        if params.state:
            query["state"] = params.state
        separator = "&" if "?" in str(params.redirect_uri) else "?"
        return f"{params.redirect_uri}{separator}{urlencode(query)}"

    def _credentials_match(self, username: str, password: str) -> bool:
        settings = self._settings
        if not settings.auth_enabled:
            # With auth disabled the whole service is open; refusing here would
            # only be theatre, but it is worth a loud log line.
            log.warning("OAuth login accepted with authentication disabled")
            return True
        return secrets.compare_digest(
            username.encode(), settings.auth_username.encode()
        ) & secrets.compare_digest(password.encode(), settings.auth_password.encode())

    # -- token exchange ----------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._expire()
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: burn the code before minting anything.
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        self._expire()
        token = self._refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        self._expire()
        return self._access_tokens.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)

    # -- helpers -----------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = time.time()
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes or [SCOPE],
            expires_at=int(now + ACCESS_TOKEN_TTL),
            resource=resource,
            subject=SUBJECT,
        )
        refresh = RefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes or [SCOPE],
            expires_at=int(now + REFRESH_TOKEN_TTL),
            subject=SUBJECT,
        )
        self._access_tokens[access.token] = access
        self._refresh_tokens[refresh.token] = refresh

        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh.token,
            scope=" ".join(access.scopes),
        )

    def _expire(self) -> None:
        now = time.time()
        for store in (self._codes, self._access_tokens, self._refresh_tokens):
            for key in [k for k, v in store.items() if (v.expires_at or 0) <= now]:
                store.pop(key, None)
        for key in [
            k for k, v in self._pending.items() if v.created_at + AUTH_CODE_TTL <= now
        ]:
            self._pending.pop(key, None)
