"""Periodic background task: update match history for all players in the DB.

Every ``UPDATE_INTERVAL_SECONDS`` seconds (default: 300 / 5 minutes) this
module iterates every :class:`~main.Player` row in the database, fetches new
Halo Infinite matches for that player via :mod:`haloclient`, applies selection
criteria, and persists only the matches that pass those criteria.

Configuration (.env):
    UPDATE_INTERVAL_SECONDS – How often to run the full update cycle
                               (default: 300).
    DEFAULT_GAMEMODE         – Fallback ``game_variant_category`` integer used
                               when a player has no ``gamemode`` stored
                               (default: 9).  See the TODO in the variable
                               docstring below for how to find the right value.

Background task lifecycle:
    Call :func:`start_background_task` once inside the FastAPI lifespan to
    launch the updater as an asyncio background task.  It runs indefinitely
    until the event loop is cancelled (e.g. server shutdown).
"""

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp
from sqlmodel import Session, select

from haloclient import get_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UPDATE_INTERVAL_SECONDS: int = int(os.getenv("UPDATE_INTERVAL_SECONDS", "300"))
"""Seconds between full DB update cycles (default: 300 = 5 minutes)."""

DEFAULT_GAMEMODE: int = int(os.getenv("DEFAULT_GAMEMODE", "9"))
"""Fallback ``game_variant_category`` value used when the player row carries
no explicit gamemode.
# TODO: Verify this integer matches the game type used in your aim trainer
# sessions.  Run a manual import first and inspect the ``game_variant_category``
# field in the returned match history to find the right value.
"""

# ---------------------------------------------------------------------------
# Match selection criteria
# ---------------------------------------------------------------------------


def _passes_criteria(match_entry, gamemode: int) -> bool:
    """Return ``True`` if *match_entry* should be stored in the database.

    This function centralises all match-selection logic so that new criteria
    can be added in one place.

    Current criteria:
    1. The match's ``game_variant_category`` must equal *gamemode*.

    # TODO: Add additional aim-trainer specific criteria here, for example:
    #   - Minimum match duration
    #   - Specific map or playlist
    #   - Minimum number of kills / accuracy threshold
    #   - Exclude unfinished / early-quit matches

    Args:
        match_entry: A single result from :meth:`spnkr.stats.get_match_history`.
        gamemode:    The expected ``game_variant_category`` integer.

    Returns:
        ``True`` when all criteria are satisfied, ``False`` otherwise.
    """
    return int(match_entry.match_info.game_variant_category) == gamemode


# ---------------------------------------------------------------------------
# Per-player update
# ---------------------------------------------------------------------------


async def _update_player(
    client,
    player,
    engine,
) -> int:
    """Fetch and persist new matches for a single *player*.

    The fetch is *incremental*: it stops as soon as it encounters the match
    stored in ``player.latest_match_id``, so only genuinely new matches are
    downloaded on each run.

    Args:
        client:  An authenticated :class:`~spnkr.HaloInfiniteClient`.
        player:  A :class:`~main.Player` ORM instance.
        engine:  The SQLAlchemy engine used for DB writes.

    Returns:
        The number of newly inserted match rows.
    """
    # Deferred imports avoid a circular-import between main.py and this module.
    from main import Match  # noqa: PLC0415

    logger.info("Updating player: %s (xuid=%s)", player.gamertag, player.xuid)

    # Determine which gamemode to filter.
    # TODO: If you add a per-player ``gamemode`` column in the future, read it
    # here and fall back to DEFAULT_GAMEMODE only when it is None.
    gamemode: int = DEFAULT_GAMEMODE

    # ------------------------------------------------------------------
    # Incremental fetch: stop at the previously recorded latest match.
    # ------------------------------------------------------------------
    stop_at_match_id: Optional[str] = player.latest_match_id
    all_results = []
    start = 0
    batch_size = 25
    done = False

    while not done:
        response = await client.stats.get_match_history(
            player.gamertag, start=start, count=batch_size
        )
        history = await response.parse()

        for result in history.results:
            if stop_at_match_id and str(result.match_id) == stop_at_match_id:
                done = True
                break
            all_results.append(result)

        if history.result_count < batch_size:
            break
        if done:
            break
        start += batch_size

    if not all_results:
        logger.info("No new matches found for %s.", player.gamertag)
        return 0

    # ------------------------------------------------------------------
    # Apply selection criteria.
    # ------------------------------------------------------------------
    filtered = [m for m in all_results if _passes_criteria(m, gamemode)]
    logger.debug(
        "%s: %d total new matches, %d pass criteria.",
        player.gamertag,
        len(all_results),
        len(filtered),
    )

    # ------------------------------------------------------------------
    # Fetch full match stats for each match that passed the filter.
    # ------------------------------------------------------------------
    match_stats_list = []
    for m in filtered:
        stats_resp = await client.stats.get_match_stats(m.match_id)
        raw_json = await stats_resp.json()
        match_stats_list.append((m, raw_json))

    # ------------------------------------------------------------------
    # Persist inside a single transaction.
    # ------------------------------------------------------------------
    new_count = 0
    newest_match_id: Optional[str] = str(all_results[0].match_id)

    with Session(engine) as db:
        # Re-fetch inside this session to avoid detached-instance issues.
        from main import Player  # noqa: PLC0415
        db_player = db.exec(select(Player).where(Player.xuid == player.xuid)).first()
        if db_player is None:
            logger.error("Player %s not found in DB during update – skipping.", player.xuid)
            return 0

        db_player.latest_match_id = newest_match_id
        db.add(db_player)

        for history_entry, raw_json in match_stats_list:
            mid = str(history_entry.match_id)
            if db.exec(select(Match).where(Match.match_id == mid)).first():
                # Already stored (e.g. imported manually via the API endpoint).
                continue
            match_obj = Match(
                match_id=mid,
                player_id=db_player.id,
                duration=str(history_entry.match_info.duration),
                played_at=history_entry.match_info.start_time,
                raw_match_stats=json.dumps(raw_json),
            )
            db.add(match_obj)
            new_count += 1

        db.commit()

    logger.info(
        "Player %s updated: %d new match(es) stored.", player.gamertag, new_count
    )
    return new_count


# ---------------------------------------------------------------------------
# Full-DB update cycle
# ---------------------------------------------------------------------------


async def _run_update_cycle(engine) -> None:
    """Run one full update cycle: refresh all players in the database.

    Acquires a fresh Spartan token at the start of every cycle so the
    background task remains authenticated across long intervals.

    Args:
        engine: The SQLAlchemy engine used for DB reads and writes.
    """
    # Deferred import to avoid circular dependency with main.py.
    from main import Player  # noqa: PLC0415

    logger.info("Background update cycle starting…")

    with Session(engine) as db:
        players = db.exec(select(Player)).all()

    if not players:
        logger.info("No players in database – nothing to update.")
        return

    async with aiohttp.ClientSession() as session:
        async with get_client(session) as client:
            total_new = 0
            for player in players:
                try:
                    new = await _update_player(client, player, engine)
                    total_new += new
                except Exception:
                    # Log and continue so one bad player doesn't abort the cycle.
                    logger.exception(
                        "Failed to update player %s – continuing with next player.",
                        player.gamertag,
                    )

    logger.info(
        "Background update cycle complete. Total new matches stored: %d.", total_new
    )


# ---------------------------------------------------------------------------
# Asyncio background task entry point
# ---------------------------------------------------------------------------


async def _background_loop(engine) -> None:
    """Infinite asyncio loop that runs :func:`_run_update_cycle` periodically.

    Sleeps for ``UPDATE_INTERVAL_SECONDS`` between each cycle.  Any unhandled
    exception inside a cycle is caught and logged so the loop never dies
    silently.

    Args:
        engine: The SQLAlchemy engine passed through to the update cycle.
    """
    logger.info(
        "Background match updater started (interval: %ds).",
        UPDATE_INTERVAL_SECONDS,
    )
    while True:
        try:
            await _run_update_cycle(engine)
        except Exception:
            logger.exception("Unhandled error in background update cycle.")
        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)


def start_background_task(engine) -> asyncio.Task:
    """Schedule the periodic match-updater as an asyncio background task.

    Call this once from the FastAPI ``lifespan`` context manager **after**
    the database tables have been created.  The returned task runs until the
    event loop is cancelled (i.e. server shutdown).

    Args:
        engine: The SQLAlchemy engine to pass to the update loop.

    Returns:
        The scheduled :class:`asyncio.Task`.

    Example (in ``main.py``)::

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            create_db_and_tables()
            task = start_background_task(engine)
            yield
            task.cancel()
    """
    return asyncio.create_task(
        _background_loop(engine),
        name="match_updater",
    )
