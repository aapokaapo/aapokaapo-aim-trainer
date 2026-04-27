"""Periodic background task: update match history for all players in the DB.

Every ``UPDATE_INTERVAL_SECONDS`` seconds (default: 300 / 5 minutes) this
module iterates every :class:`~main.Player` row in the database, fetches new
Halo Infinite matches for that player via :mod:`haloclient`, applies selection
criteria, and persists only the matches that pass those criteria.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp
from sqlmodel import Session, select
from haloclient import get_client

# Clean, top-level imports! No circular dependency risks.
from database import engine
from models import Player, Match

from datetime import datetime, timezone


LAST_UPDATE_TIMESTAMP: Optional[datetime] = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UPDATE_INTERVAL_SECONDS: int = int(os.getenv("UPDATE_INTERVAL_SECONDS", "300"))
"""Seconds between full DB update cycles (default: 300 = 5 minutes)."""

INTER_PLAYER_DELAY_SECONDS: float = float(os.getenv("INTER_PLAYER_DELAY_SECONDS", "2"))
"""Seconds to pause between updating consecutive players (default: 2).

Prevents burst-firing requests when multiple players are in the database,
which would otherwise exceed the Halo Infinite API rate limit.
"""

HISTORY_PAGE_DELAY_SECONDS: float = 0.5
"""Seconds to pause between fetching consecutive pages of match history (0.5s).

Avoids rapid-fire pagination requests that contribute to rate limiting when
a player has many pages of unprocessed match history.
"""

# The specific Aim Trainer variant we want to track
TARGET_ASSET_ID = "ccde9ea1-200d-4017-98be-affc41460bae"
TARGET_VERSION_ID = "f478dc12-f455-46c5-9d04-fe477dbc88f2"

# ---------------------------------------------------------------------------
# Match selection criteria
# ---------------------------------------------------------------------------

def _passes_criteria(match_entry) -> bool:
    """Return ``True`` if *match_entry* should be stored in the database.
    
    This explicitly filters for our specific Target Asset and Version ID.
    """
    variant = getattr(match_entry.match_info, "ugc_game_variant", None)
    
    # 1. Reject matches that have no game variant attached
    if not variant:
        return False
        
    # 2. Reject matches that don't match our exact Aim Trainer IDs
    if str(variant.asset_id) != TARGET_ASSET_ID or str(variant.version_id) != TARGET_VERSION_ID:
        return False
        
    # 3. If it makes it here, it's the correct match!
    return True


def _check_if_match_valid(raw_json: dict, xuid: str, raw_map: dict) -> bool:
    """
    Parses the raw match stats JSON to verify the team reached 100 points 
    AND the player got at least 100 kills.
    """
    target_id = f"xuid({xuid})"
    
    try:
        player_team_id = None
        player_kills = 0
        
        # 1. Find the player to get their Kills and their TeamId
        players = raw_json.get("Players", [])

        for p in players:
            
            # Extract whatever the API gave us, convert to lowercase string
            api_player_id = str(p.get("PlayerId", "")).lower()
            
            # If the raw XUID numbers exist anywhere inside that string, it's a match!
            
            if target_id == api_player_id:
                player_team_stats = p.get("PlayerTeamStats", [])
                for t in player_team_stats:
                    player_team_id = t.get("TeamId")
                
                # Get kills and personal score
                    core_stats = t.get("Stats", {}).get("CoreStats", {})
                    player_kills = core_stats.get("Kills", 0)
                break
        # If we couldn't find the player or they have no team, it's invalid
        if player_team_id is None:
            return False

        players_on_this_team = []
        for p in players:
            if p.get("PlayerTeamStats", []):
                for t in player_team_stats:
                    if t.get("TeamId") == player_team_id:
                        players_on_this_team.append(p)
        if len(players_on_this_team) > 1:
            return False

        team_score = 0
        if player_team_id is not None:
            for t in raw_json.get("Teams", []):
                if t.get("TeamId") == player_team_id:
                    team_score = t.get("Stats", {}).get("CoreStats", {}).get("Score", 0)
                    break
            
                
        # 3. Check both conditions! 
        # (Using >= 100 is safer than == 100 just in case a multikill ends the game at 101)
        if raw_map.get("PublicName", "") != "Live Fire - Ranked" or raw_map.get("Admin", "") != 'xuid(2814672600485177)':
            return False
        
        
        return team_score >= 100 and player_kills >= 100
                
    except Exception as e:
        logger.error(f"Failed to parse match validation: {e}")
        
    return False


async def fetch_with_backoff(func, *args, **kwargs):
    """
    Executes a SPNKr API call and automatically handles 429 'Too Many Requests'.
    """
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                # 1. Start with your default 60s wait
                wait = 60 
                
                # 2. Try to safely parse the Retry-After header
                if e.headers and "Retry-After" in e.headers:
                    try:
                        wait = int(e.headers["Retry-After"])
                    except ValueError:
                        # Header was likely a Date string instead of an integer. 
                        # We ignore it and stick to the 60s default.
                        pass 
                
                logger.warning(
                    "Rate limited (429). Waiting %ds before retry %d/%d…",
                    wait, attempt + 1, max_attempts,
                )
                await asyncio.sleep(wait)
            else:
                # If it's a 401 (Token Expired) or 404, let the main logic handle it
                raise e
                
    raise Exception("Failed to fetch data after multiple retries due to rate limiting.")

# ---------------------------------------------------------------------------
# Per-player update
# ---------------------------------------------------------------------------

async def _update_player(
    client,
    player,
    engine,
) -> int:
    """Fetch and persist new matches for a single *player*."""

    logger.info("Updating player: %s (xuid=%s)", player.gamertag, player.xuid)

    # ------------------------------------------------------------------
    # Incremental fetch: stop at the previously recorded latest match.
    # ------------------------------------------------------------------
    stop_at_match_id: Optional[str] = player.latest_match_id
    all_results = []
    start = 0
    batch_size = 25
    done = False

    while not done:
        # 1. Format the XUID exactly how the API requires it
        target_id = f"xuid({player.xuid})"
        
        # 2. Pass the target_id INSTEAD of the gamertag
        response = await fetch_with_backoff(
            client.stats.get_match_history, 
            target_id,
            start=start, 
            count=batch_size,
            match_type="custom"
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
        # Throttle between history pages to avoid bursting the API.
        await asyncio.sleep(HISTORY_PAGE_DELAY_SECONDS)

    if not all_results:
        logger.info("No new matches found for %s.", player.gamertag)
        return 0

    # ------------------------------------------------------------------
    # Apply selection criteria.
    # ------------------------------------------------------------------
    # Now this cleanly filters out anything that isn't the Aim Trainer!
    filtered = [m for m in all_results if _passes_criteria(m)]
    
    logger.debug(
        "%s: %d total new matches, %d passed criteria",
        player.gamertag,
        len(all_results),
        len(filtered)
    )

    if not filtered:
        # We need to update their latest_match_id so we don't scan these again next time
        with Session(engine) as db:
            db_player = db.exec(select(Player).where(Player.xuid == player.xuid)).first()
            if db_player:
                db_player.latest_match_id = str(all_results[0].match_id)
                db.add(db_player)
                db.commit()
        return 0
        
        
    filtered_match_ids = [str(m.match_id) for m in filtered]
    
    with Session(engine) as db:
        # Fetch only the IDs of matches that already exist in our DB
        existing_matches = db.exec(
            select(Match.match_id).where(Match.match_id.in_(filtered_match_ids))
        ).all()
        
    existing_match_ids = set(existing_matches)
    
    # Filter down to ONLY the matches we don't have yet
    matches_to_fetch = [m for m in filtered if str(m.match_id) not in existing_match_ids]
    
    logger.info(
        "Player %s: %d aim trainer matches found, %d already in DB, fetching stats for %d...", 
        player.gamertag, len(filtered), len(existing_match_ids), len(matches_to_fetch)
    )

    if not matches_to_fetch:
        return 0

    newest_match_id: Optional[str] = str(all_results[0].match_id)

    # ------------------------------------------------------------------
    # 1. Update the player's latest_match_id FIRST. 
    # This ensures that even if the loop below crashes halfway through,
    # we don't scan the same pages of history again next time.
    # ------------------------------------------------------------------
    with Session(engine) as db:
        db_player = db.exec(select(Player).where(Player.xuid == player.xuid)).first()
        if db_player is None:
            logger.error("Player %s not found in DB during update – skipping.", player.xuid)
            return 0

        db_player.latest_match_id = newest_match_id
        db.add(db_player)
        db.commit()
        
        # Save the database's internal player ID so we can assign it to the matches
        internal_player_id = db_player.id

    # ------------------------------------------------------------------
    # 2. Fetch stats and build match objects; commit in a single batch.
    # ------------------------------------------------------------------
    new_matches: list[Match] = []
    for m in matches_to_fetch:
        
        # 1. Fetch the match stats from the Halo API
        stats_resp = await fetch_with_backoff(
            client.stats.get_match_stats, m.match_id
        )
        raw_json = await stats_resp.json()
        
        map_resp = await fetch_with_backoff(
            client.discovery_ugc.get_map, m.match_info.map_variant.asset_id, m.match_info.map_variant.version_id
        )
        raw_map = await map_resp.json()
        
        mid = str(m.match_id)
        
        # 2. Run our validation check
        is_valid = _check_if_match_valid(raw_json, player.xuid, raw_map)
        
        new_matches.append(Match(
            match_id=mid,
            player_id=internal_player_id,
            duration=str(m.match_info.duration),
            played_at=m.match_info.start_time,
            raw_match_stats=json.dumps(raw_json),
            is_valid=is_valid,
        ))

        # 3. Wait to respect rate limits before fetching the next one
        await asyncio.sleep(1)

    # Bulk-insert, skipping any that were concurrently added
    new_count = 0
    if new_matches:
        with Session(engine) as db:
            new_match_ids = [m.match_id for m in new_matches]
            existing_ids = set(
                db.exec(
                    select(Match.match_id).where(
                        Match.match_id.in_(new_match_ids)
                    )
                ).all()
            )
            to_add = [m for m in new_matches if m.match_id not in existing_ids]
            for obj in to_add:
                db.add(obj)
            db.commit()
            new_count = len(to_add)

    return new_count


# ---------------------------------------------------------------------------
# Full-DB update cycle
# ---------------------------------------------------------------------------

async def _run_update_cycle(engine) -> None:
    global LAST_UPDATE_TIMESTAMP
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
                    logger.exception(
                        "Failed to update player %s – continuing with next player.",
                        player.gamertag,
                    )
                # Pause between players to avoid bursting the Halo API rate limit.
                await asyncio.sleep(INTER_PLAYER_DELAY_SECONDS)

    logger.info(
        "Background update cycle complete. Total new matches stored: %d.", total_new
    )
    LAST_UPDATE_TIMESTAMP = datetime.now(timezone.utc)
    logger.info("Timestamp updated: %s", LAST_UPDATE_TIMESTAMP)

    try:
        import main as _main
        _main.invalidate_leaderboard_cache()
    except Exception:
        logger.debug("Could not invalidate leaderboard cache", exc_info=True)


# ---------------------------------------------------------------------------
# Asyncio background task entry point
# ---------------------------------------------------------------------------

async def _background_loop(engine) -> None:
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
    return asyncio.create_task(
        _background_loop(engine),
        name="match_updater",
    )