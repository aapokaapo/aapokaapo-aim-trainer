"""Halo Infinite client factory with Azure OAuth token management.

This module mirrors the pattern used in HaloDashApp/spnkr_app/__init__.py,
adapted for a FastAPI async context with .env-based configuration.

The Azure OAuth tokens are refreshed before every client use so long-running
processes (e.g. the background updater) stay authenticated without a restart.

Configuration (all read from .env):
    AZURE_CLIENT_ID      – Azure AD application client ID
    AZURE_CLIENT_SECRET  – Azure AD application client secret
    AZURE_REFRESH_TOKEN  – Initial OAuth refresh token
    REDIRECT_URI         – OAuth redirect URI (e.g. https://localhost)

See https://acurtis166.github.io/SPNKr/getting-started/ for instructions on
creating an Azure AD app and obtaining the initial refresh token.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiohttp
from spnkr import AzureApp, HaloInfiniteClient, refresh_player_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Azure app singleton (built once from env; safe because the values never
# change at runtime even if the tokens are rotated).
# ---------------------------------------------------------------------------

_azure_app = AzureApp(
    client_id=os.getenv("AZURE_CLIENT_ID", ""),
    client_secret=os.getenv("AZURE_CLIENT_SECRET", ""),
    redirect_uri=os.getenv("REDIRECT_URI", "https://localhost"),
)


@asynccontextmanager
async def get_client(
    session: aiohttp.ClientSession,
) -> AsyncGenerator[HaloInfiniteClient, None]:
    """Async context manager that yields an authenticated HaloInfiniteClient.

    Refreshes the Azure/Spartan tokens on each call so long-running background
    tasks remain authenticated without restarting the server.

    Args:
        session: An active :class:`aiohttp.ClientSession` to reuse.

    Yields:
        An authenticated :class:`~spnkr.HaloInfiniteClient` instance.

    Raises:
        RuntimeError: If ``AZURE_REFRESH_TOKEN`` is not set in the environment.

    Example::

        async with aiohttp.ClientSession() as session:
            async with get_client(session) as client:
                resp = await client.stats.get_match_history(gamertag)
    """
    refresh_token = os.getenv("AZURE_REFRESH_TOKEN", "")
    if not refresh_token:
        raise RuntimeError(
            "AZURE_REFRESH_TOKEN is not set. "
            "Add it to your .env file before starting the server."
        )

    logger.debug("Refreshing Spartan/Azure player tokens…")
    player = await refresh_player_tokens(session, _azure_app, refresh_token)

    if not player.is_valid:
        logger.warning("Refreshed player token is not valid – API calls may fail.")
    else:
        logger.debug("Player tokens refreshed successfully.")

    client = HaloInfiniteClient(
        session=session,
        spartan_token=player.spartan_token.token,
        clearance_token=player.clearance_token.token,
        # Respect the Halo API rate limit; 5 r/s is the safe default.
        requests_per_second=5,
    )
    yield client
