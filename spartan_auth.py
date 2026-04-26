"""
spartan_auth.py – Azure OAuth → Spartan token flow for Halo Infinite.

The module implements a multi-step authentication chain:

  1. Exchange the stored Azure refresh token for a fresh Azure access token.
  2. Exchange the Azure access token for an Xbox Live (XBL3.0) user token.
  3. Exchange the XBL3.0 token for an XSTS token scoped to the Halo Waypoint
     relying-party.
  4. Exchange the XSTS token for a Spartan v4 access token (and refresh token).

Environment variables (loaded from .env via python-dotenv if available):
  AZURE_CLIENT_ID      – Azure AD application (client) ID.
  AZURE_CLIENT_SECRET  – Azure AD client secret.
  AZURE_REFRESH_TOKEN  – Long-lived Azure OAuth refresh token.
  REDIRECT_URI         – OAuth redirect URI registered with the Azure app.

Assumptions / TODOs
-------------------
* The Spartan token endpoint and its exact request/response schema are based
  on publicly documented Halo Waypoint reverse-engineering.  Verify the URL
  and payload format against the current API before deploying.
* The module is designed to be called at startup or on-demand; extend
  `SpartanTokenManager` to add automatic refresh-before-expiry logic.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is not installed; fall back to whatever is already in
    # os.environ (e.g. variables exported by the shell or a container runtime).
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Microsoft identity platform token endpoint (personal / consumer accounts).
_MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# Xbox Live user-authentication endpoint.
_XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"

# Xbox Secure Token Service (XSTS) authorization endpoint.
_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

# Halo Waypoint XSTS relying-party used when requesting an XSTS token that
# can subsequently be traded for a Spartan token.
_HALO_XSTS_RELYING_PARTY = "https://prod.xsts.halowaypoint.com/"

# TODO: Confirm the exact Spartan v4 token endpoint.  The URL below is based
#       on community documentation; verify against the current Halo Waypoint
#       service contract before deploying.
_SPARTAN_TOKEN_URL = "https://settings.svc.halowaypoint.com/spartan-token"

# Halo clearance-token endpoint (needed by the HaloInfiniteClient alongside
# the Spartan token).
# TODO: Confirm the exact clearance endpoint.
_CLEARANCE_TOKEN_URL = (
    "https://settings.svc.halowaypoint.com/settings/hipc/production"
)

# OAuth scope required for Xbox Live authentication.
_XBOX_SCOPE = "Xboxlive.signin Xboxlive.offline_access"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AzureTokens:
    """Tokens returned by the Microsoft identity platform."""

    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass
class SpartanTokens:
    """Spartan token bundle returned by the Halo Waypoint token service."""

    spartan_token: str
    # The refresh token for renewing the Spartan token directly (if the API
    # provides one).  Set to None when the service does not return one; in
    # that case re-run the full Azure → Spartan chain with the Azure refresh
    # token instead.
    spartan_refresh_token: Optional[str]
    expires_at: datetime  # always set; defaults to 1 hour from now when unknown


# ---------------------------------------------------------------------------
# Step 1 – Azure access token via refresh-token grant
# ---------------------------------------------------------------------------


async def fetch_azure_tokens(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    redirect_uri: str,
) -> AzureTokens:
    """Exchange an Azure refresh token for a fresh access token.

    Uses the OAuth 2.0 refresh-token grant against the Microsoft identity
    platform consumers endpoint.

    Args:
        session: An active :class:`aiohttp.ClientSession`.
        client_id: Azure AD application (client) ID.
        client_secret: Azure AD client secret.
        refresh_token: A valid Azure OAuth refresh token.
        redirect_uri: The redirect URI registered with the Azure application.

    Returns:
        :class:`AzureTokens` containing the new access token, a potentially
        updated refresh token, and the expiry timestamp.

    Raises:
        RuntimeError: If the token endpoint returns a non-2xx response.
    """
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "redirect_uri": redirect_uri,
        "scope": _XBOX_SCOPE,
    }

    async with session.post(_MS_TOKEN_URL, data=payload) as resp:
        body = await resp.json(content_type=None)
        if not resp.ok:
            raise RuntimeError(
                f"Azure token refresh failed ({resp.status}): {body}"
            )

    expires_in: int = body.get("expires_in", 3600)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    return AzureTokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", refresh_token),
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Step 2 – Xbox Live (XBL3.0) user token
# ---------------------------------------------------------------------------


async def _fetch_xbl_token(
    session: aiohttp.ClientSession, azure_access_token: str
) -> tuple[str, str]:
    """Exchange an Azure access token for an Xbox Live user token.

    Args:
        session: An active :class:`aiohttp.ClientSession`.
        azure_access_token: A valid Microsoft/Azure access token.

    Returns:
        A tuple of ``(xbl_token, user_hash)`` where *user_hash* (``uhs``) is
        required when building the XSTS authorisation header.

    Raises:
        RuntimeError: If the XBL endpoint returns a non-2xx response.
    """
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={azure_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    async with session.post(_XBL_AUTH_URL, json=payload, headers=headers) as resp:
        body = await resp.json(content_type=None)
        if not resp.ok:
            raise RuntimeError(f"XBL authentication failed ({resp.status}): {body}")

    xbl_token: str = body["Token"]
    # The user hash lives inside DisplayClaims → xui[0].uhs.
    user_hash: str = body["DisplayClaims"]["xui"][0]["uhs"]
    return xbl_token, user_hash


# ---------------------------------------------------------------------------
# Step 3 – XSTS token scoped to the Halo Waypoint relying-party
# ---------------------------------------------------------------------------


async def _fetch_xsts_token(
    session: aiohttp.ClientSession, xbl_token: str
) -> tuple[str, str]:
    """Exchange an XBL3.0 user token for an XSTS token for Halo Waypoint.

    Args:
        session: An active :class:`aiohttp.ClientSession`.
        xbl_token: A valid Xbox Live user token.

    Returns:
        A tuple of ``(xsts_token, user_hash)``.

    Raises:
        RuntimeError: If the XSTS endpoint returns a non-2xx response.
    """
    payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": _HALO_XSTS_RELYING_PARTY,
        "TokenType": "JWT",
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    async with session.post(_XSTS_AUTH_URL, json=payload, headers=headers) as resp:
        body = await resp.json(content_type=None)
        if not resp.ok:
            raise RuntimeError(
                f"XSTS authorisation failed ({resp.status}): {body}"
            )

    xsts_token: str = body["Token"]
    user_hash: str = body["DisplayClaims"]["xui"][0]["uhs"]
    return xsts_token, user_hash


# ---------------------------------------------------------------------------
# Step 4 – Spartan v4 token
# ---------------------------------------------------------------------------


async def _fetch_spartan_tokens(
    session: aiohttp.ClientSession,
    xsts_token: str,
    user_hash: str,
) -> SpartanTokens:
    """Exchange an XSTS token for a Spartan v4 access token.

    TODO: The endpoint URL and exact payload/response structure below are
          based on community-documented Halo Waypoint reverse-engineering.
          Verify against the live service and update as needed.

    Args:
        session: An active :class:`aiohttp.ClientSession`.
        xsts_token: A valid XSTS token scoped to the Halo Waypoint
            relying-party (``https://prod.xsts.halowaypoint.com/``).
        user_hash: The ``uhs`` claim from the XSTS token.

    Returns:
        :class:`SpartanTokens` containing the Spartan access token and,
        where provided by the service, a refresh token and expiry time.

    Raises:
        RuntimeError: If the Spartan token endpoint returns a non-2xx
            response.
    """
    # TODO: Verify the request payload structure with the current Halo
    #       Waypoint API contract.  The ``Proof`` array and ``MinVersion``
    #       field are taken from publicly available documentation.
    payload = {
        "Audience": "urn:343:s3:services",
        "MinVersion": "4",
        "Proof": [
            {
                "Token": xsts_token,
                "TokenType": "Xbox_XSTSv3",
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # The XBL 3.0 authorisation header format expected by Halo services.
        "Authorization": f"XBL3.0 x={user_hash};{xsts_token}",
    }

    async with session.post(
        _SPARTAN_TOKEN_URL, json=payload, headers=headers
    ) as resp:
        body = await resp.json(content_type=None)
        if not resp.ok:
            raise RuntimeError(
                f"Spartan token request failed ({resp.status}): {body}"
            )

    # TODO: Confirm the response field names used by the Halo Waypoint service.
    spartan_token: str = body["SpartanToken"]
    spartan_refresh_token: Optional[str] = body.get("RefreshToken")

    expires_at: Optional[datetime] = None
    if "ExpiresUtc" in body:
        try:
            # TODO: Verify the exact timestamp format returned by the service.
            expires_at = datetime.fromisoformat(
                body["ExpiresUtc"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            expires_at = None

    if expires_at is None:
        # The service did not return an expiry; assume a conservative 1-hour
        # validity so cached tokens are eventually refreshed.
        expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)

    return SpartanTokens(
        spartan_token=spartan_token,
        spartan_refresh_token=spartan_refresh_token,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


class SpartanTokenManager:
    """Manages the full Azure OAuth → Spartan token chain.

    Loads credentials from environment variables (populated from ``.env`` by
    :func:`dotenv.load_dotenv` at module import time).

    Cached tokens are stored in-memory and re-used until they expire.  Call
    :meth:`get_spartan_token` to obtain (and cache) a valid Spartan token.

    Example::

        async with aiohttp.ClientSession() as session:
            manager = SpartanTokenManager()
            tokens = await manager.get_spartan_token(session)
            print(tokens.spartan_token)
    """

    def __init__(self) -> None:
        self._client_id: str = self._require_env("AZURE_CLIENT_ID")
        self._client_secret: str = self._require_env("AZURE_CLIENT_SECRET")
        self._azure_refresh_token: str = self._require_env("AZURE_REFRESH_TOKEN")
        self._redirect_uri: str = self._require_env("REDIRECT_URI")

        # In-memory cache for the most recently obtained tokens.
        self._azure_tokens: Optional[AzureTokens] = None
        self._spartan_tokens: Optional[SpartanTokens] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_spartan_token(
        self, session: aiohttp.ClientSession
    ) -> SpartanTokens:
        """Return a valid Spartan token, refreshing it when necessary.

        Executes the full Azure → XBL → XSTS → Spartan chain on first call,
        then returns the cached result on subsequent calls until the token is
        close to expiry.

        Args:
            session: An active :class:`aiohttp.ClientSession`.

        Returns:
            :class:`SpartanTokens` with a valid Spartan access token.
        """
        if self._spartan_tokens is not None and not self._is_expired(
            self._spartan_tokens.expires_at
        ):
            return self._spartan_tokens

        self._spartan_tokens = await self._run_full_chain(session)
        return self._spartan_tokens

    async def refresh_azure_token(
        self, session: aiohttp.ClientSession
    ) -> AzureTokens:
        """Explicitly refresh the Azure access token.

        Updates the cached refresh token with the one returned by Microsoft
        (Microsoft may rotate refresh tokens on each use).

        Args:
            session: An active :class:`aiohttp.ClientSession`.

        Returns:
            Fresh :class:`AzureTokens`.
        """
        tokens = await fetch_azure_tokens(
            session,
            client_id=self._client_id,
            client_secret=self._client_secret,
            refresh_token=self._azure_refresh_token,
            redirect_uri=self._redirect_uri,
        )
        # Persist any rotated refresh token for the lifetime of this instance.
        self._azure_refresh_token = tokens.refresh_token
        self._azure_tokens = tokens
        return tokens

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_full_chain(
        self, session: aiohttp.ClientSession
    ) -> SpartanTokens:
        """Execute the complete Azure → Spartan token chain."""
        azure = await self.refresh_azure_token(session)
        xbl_token, _uhs = await _fetch_xbl_token(session, azure.access_token)
        xsts_token, uhs = await _fetch_xsts_token(session, xbl_token)
        return await _fetch_spartan_tokens(session, xsts_token, uhs)

    @staticmethod
    def _is_expired(expires_at: datetime, buffer_seconds: int = 60) -> bool:
        """Return True if *expires_at* is within *buffer_seconds* of now."""
        return datetime.now(tz=timezone.utc) >= expires_at - timedelta(
            seconds=buffer_seconds
        )

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise EnvironmentError(
                f"Required environment variable '{name}' is not set. "
                "Add it to your .env file."
            )
        return value


# ---------------------------------------------------------------------------
# Entry point – standalone usage example
# ---------------------------------------------------------------------------


async def _main() -> None:
    """Obtain and print a Spartan token using credentials from .env."""
    manager = SpartanTokenManager()
    async with aiohttp.ClientSession() as session:
        tokens = await manager.get_spartan_token(session)
    print("Spartan token:", tokens.spartan_token)
    if tokens.spartan_refresh_token:
        print("Spartan refresh token:", tokens.spartan_refresh_token)
    print("Expires at:", tokens.expires_at.isoformat())


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
