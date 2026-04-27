import logging
import os
import sys
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, List
from urllib.parse import urlencode, quote

import aiohttp
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Security, status
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, SQLModel, select

from database import engine
from haloclient import get_client
from models import Match, Player

import json # Make sure this is imported!
import updater
from updater import start_background_task, _run_update_cycle, _check_if_match_valid

logging.basicConfig(
    level=logging.DEBUG, # Change to DEBUG if you want to see absolutely everything
    format="%(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Microsoft / Xbox OAuth configuration
# ---------------------------------------------------------------------------

AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
API_KEY = os.environ.get("API_KEY", "")
# PUBLIC_BASE_URL is used to build the OAuth redirect URI.
# For local dev: http://127.0.0.1:8045  |  for prod: https://aimtrainer.aapokaapostats.site
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8045").rstrip("/")
PORT = int(os.environ.get("PORT", 8045))

_MICROSOFT_TENANT = "consumers"  # Xbox Live accounts are Microsoft consumer accounts
_MICROSOFT_AUTH_URL = f"https://login.microsoftonline.com/{_MICROSOFT_TENANT}/oauth2/v2.0/authorize"
_MICROSOFT_TOKEN_URL = f"https://login.microsoftonline.com/{_MICROSOFT_TENANT}/oauth2/v2.0/token"
_XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
_XBOX_PROFILE_URL_TEMPLATE = "https://profile.xboxlive.com/users/xuid({xuid})/profile/settings"

# In-memory CSRF state store: state_token -> created_at (unix timestamp)
_oauth_states: dict[str, float] = {}
_STATE_TTL = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Leaderboard TTL cache
# ---------------------------------------------------------------------------

_leaderboard_cache: dict = {"data": None, "expires_at": 0.0}
_LEADERBOARD_CACHE_TTL = 60  # seconds — leaderboard is refreshed every 5 min anyway

def invalidate_leaderboard_cache() -> None:
    """Reset the leaderboard TTL cache. Called by the updater after each cycle."""
    _leaderboard_cache["expires_at"] = 0.0

def _purge_expired_states() -> None:
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if now - v > _STATE_TTL]
    for k in expired:
        del _oauth_states[k]

# ---------------------------------------------------------------------------
# Xbox token-exchange helpers
# ---------------------------------------------------------------------------

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(_api_key_header)) -> None:
    """Dependency that validates the X-API-Key header for write endpoints."""
    if not API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key is not configured on the server.",
        )
    if not api_key or not secrets.compare_digest(api_key, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

async def _exchange_code_for_token(session: aiohttp.ClientSession, code: str) -> dict:
    """Exchange an OAuth authorization code for a Microsoft access token."""
    redirect_uri = f"{PUBLIC_BASE_URL}/auth/microsoft/callback"
    data = {
        "client_id": AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "scope": "XboxLive.signin",
    }
    async with session.post(_MICROSOFT_TOKEN_URL, data=data) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"Token exchange failed ({resp.status}): {body.get('error_description', body)}")
        return body

async def _get_xbl_token(session: aiohttp.ClientSession, ms_access_token: str) -> dict:
    """Exchange a Microsoft access token for an Xbox Live (XBL) user token."""
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    async with session.post(_XBL_AUTH_URL, json=payload, headers=headers) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"XBL auth failed ({resp.status}): {body}")
        return body

async def _get_xsts_token(session: aiohttp.ClientSession, xbl_token: str) -> dict:
    """Exchange an XBL token for an XSTS token required by Xbox Live APIs."""
    payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": "http://xboxlive.com",
        "TokenType": "JWT",
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    async with session.post(_XSTS_AUTH_URL, json=payload, headers=headers) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"XSTS auth failed ({resp.status}): {body}")
        return body

async def _get_xbox_gamertag(
    session: aiohttp.ClientSession, xuid: str, uhs: str, xsts_token: str
) -> str:
    """Fetch the gamertag for a given XUID using the Xbox Live profile API."""
    auth_header = f"XBL3.0 x={uhs};{xsts_token}"
    headers = {
        "Authorization": auth_header,
        "x-xbl-contract-version": "2",
        "Accept": "application/json",
    }
    url = _XBOX_PROFILE_URL_TEMPLATE.format(xuid=xuid)
    async with session.get(url, headers=headers, params={"settings": "Gamertag"}) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"Xbox profile fetch failed ({resp.status}): {body}")
    try:
        for setting in body["profileUsers"][0]["settings"]:
            if setting["id"] == "Gamertag":
                return setting["value"]
        raise RuntimeError("Gamertag not found in Xbox profile response")
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Failed to parse Xbox profile response: {exc}") from exc

def create_db_and_tables():
    # Because models.py is imported, SQLModel knows about the tables
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    task = start_background_task(engine)
    yield
    task.cancel()

app = FastAPI(title="Halo Aim Trainer Match Importer", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class MatchOut(BaseModel):
    match_id: str
    duration: str
    played_at: datetime

class PlayerOut(BaseModel):
    xuid: str
    gamertag: str
    latest_match_id: Optional[str]
    matches: list[MatchOut]

class PlayerSearchRequest(BaseModel):
    gamertag: str

class LeaderboardEntry(BaseModel):
    rank: int
    gamertag: str
    duration: str
    played_at: datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _player_out(db: Session, player: Player) -> PlayerOut:
    matches = db.exec(select(Match).where(Match.player_id == player.id)).all()
    return PlayerOut(
        xuid=player.xuid,
        gamertag=player.gamertag,
        latest_match_id=player.latest_match_id,
        matches=[
            MatchOut(match_id=m.match_id, duration=m.duration, played_at=m.played_at)
            for m in matches
        ],
    )

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=FileResponse)
async def serve_frontend():
    """Serves the leaderboard as the index page."""
    return FileResponse("leaderboard.html")

@app.get("/api/status")
async def get_system_status():
    """Returns the last time the background worker finished a cycle."""
    return {
        "last_update": updater.LAST_UPDATE_TIMESTAMP
    }

@app.get("/leaderboard")
async def serve_leaderboard():
    """Redirects to the leaderboard index page for backwards compatibility."""
    return RedirectResponse(url="/", status_code=301)

# ---------------------------------------------------------------------------
# Microsoft OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/microsoft/login")
async def microsoft_login():
    """Redirect the user to the Microsoft OAuth2 consent page."""
    if not AZURE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Microsoft OAuth is not configured (missing AZURE_CLIENT_ID).")

    _purge_expired_states()
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = time.time()

    redirect_uri = f"{PUBLIC_BASE_URL}/auth/microsoft/callback"
    params = {
        "client_id": AZURE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "XboxLive.signin",
        "state": state,
        "response_mode": "query",
        "prompt": "select_account",
    }
    return RedirectResponse(url=f"{_MICROSOFT_AUTH_URL}?{urlencode(params)}")

@app.get("/auth/microsoft/callback")
async def microsoft_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    """Handle the OAuth2 callback from Microsoft, fetch the Xbox profile, and upsert the player."""
    if error:
        logger.warning("OAuth error from Microsoft: %s – %s", error, error_description)
        return RedirectResponse(url="/?error=oauth_error")

    if not code or not state:
        return RedirectResponse(url="/?error=missing_params")

    # --- CSRF state validation ---
    if state not in _oauth_states:
        return RedirectResponse(url="/?error=invalid_state")
    created_at = _oauth_states[state]
    del _oauth_states[state]
    if time.time() - created_at > _STATE_TTL:
        return RedirectResponse(url="/?error=state_expired")

    # --- Xbox token-exchange chain ---
    try:
        async with aiohttp.ClientSession() as http:
            token_data = await _exchange_code_for_token(http, code)
            ms_access_token = token_data["access_token"]

            xbl_data = await _get_xbl_token(http, ms_access_token)
            xbl_token = xbl_data["Token"]

            xsts_data = await _get_xsts_token(http, xbl_token)
            xsts_token = xsts_data["Token"]
            xuid: str = xsts_data["DisplayClaims"]["xui"][0]["xid"]
            uhs: str = xsts_data["DisplayClaims"]["xui"][0]["uhs"]

            gamertag = await _get_xbox_gamertag(http, xuid, uhs, xsts_token)
    except Exception as exc:
        logger.error("Xbox auth / profile fetch failed: %s", exc)
        return RedirectResponse(url="/?error=xbox_auth_failed")

    # --- Upsert player ---
    try:
        with Session(engine) as db:
            existing = db.exec(select(Player).where(Player.xuid == xuid)).first()
            if existing:
                existing.gamertag = gamertag
                db.add(existing)
                db.commit()
            else:
                db.add(Player(xuid=xuid, gamertag=gamertag))
                db.commit()
    except Exception as exc:
        logger.error("DB upsert failed for xuid=%s: %s", xuid, exc)
        return RedirectResponse(url="/?error=db_error")

    return RedirectResponse(url=f"/?added={quote(gamertag)}")
    
@app.post("/players/", response_model=PlayerOut, status_code=201, dependencies=[Depends(verify_api_key)])
async def add_player_via_halo_api(request: PlayerSearchRequest):
    # 1. Check if they already exist in the local DB
    with Session(engine) as db:
        existing = db.exec(select(Player).where(Player.gamertag.ilike(request.gamertag))).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Player '{existing.gamertag}' is already in the database.")

    # Fetch the XUID using spnkr
    try:
        async with aiohttp.ClientSession() as session:
            async with get_client(session) as client:
                response = await client.profile.get_user_by_gamertag(request.gamertag)
                data = await response.json()
    except Exception as e:
        logger.error(f"Halo API Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to communicate with the Halo API.")

    if not data or "xuid" not in data:
        raise HTTPException(status_code=404, detail=f"Gamertag '{{request.gamertag}}' not found on Xbox Live.")

    xuid = data["xuid"]

    # Save the new player to the database
    with Session(engine) as db:
        # Double check XUID uniqueness just in case they changed their gamertag
        if db.exec(select(Player).where(Player.xuid == xuid)).first():
            raise HTTPException(status_code=400, detail="A player with this underlying XUID already exists.")

        new_player = Player(gamertag=request.gamertag, xuid=xuid)
        db.add(new_player)
        db.commit()
        db.refresh(new_player)
        return _player_out(db, new_player)

@app.get("/api/leaderboard", response_model=List[LeaderboardEntry])
def get_leaderboard():
    now = time.monotonic()
    if _leaderboard_cache["data"] is not None and now < _leaderboard_cache["expires_at"]:
        return _leaderboard_cache["data"]

    with Session(engine) as db:
        # Subquery: for each player find their best (minimum) duration among valid matches
        subq = (
            select(Match.player_id, func.min(Match.duration).label("best_duration"))
            .where(Match.is_valid == True)
            .group_by(Match.player_id)
            .subquery()
        )
        # Tiebreaker: among matches that tie on best duration, pick the one with the lowest id
        best_ids_subq = (
            select(func.min(Match.id).label("id"))
            .join(subq, (Match.player_id == subq.c.player_id) & (Match.duration == subq.c.best_duration))
            .group_by(Match.player_id)
            .subquery()
        )
        # Fetch those exact matches and their players, ordered fastest first
        stmt = (
            select(Match, Player)
            .join(Player, Match.player_id == Player.id)
            .where(Match.id.in_(select(best_ids_subq.c.id)))
            .order_by(Match.duration)
            .limit(100)
        )
        results = db.exec(stmt).all()

    leaderboard = [
        LeaderboardEntry(
            rank=rank,
            gamertag=player.gamertag,
            duration=match.duration,
            played_at=match.played_at,
        )
        for rank, (match, player) in enumerate(results, start=1)
    ]

    _leaderboard_cache["data"] = leaderboard
    _leaderboard_cache["expires_at"] = now + _LEADERBOARD_CACHE_TTL
    return leaderboard

@app.get("/players/{gamertag}/history/live", dependencies=[Depends(verify_api_key)])
async def fetch_live_match_history(gamertag: str):
    """
    Fetches the live match history directly from the Halo Infinite API 
    and returns the raw JSON response.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with get_client(session) as client:
                
                # 1. Look up the player's XUID from their gamertag
                profile_resp = await client.profile.get_user_by_gamertag(gamertag)
                profile_data = await profile_resp.json()
                
                if not profile_data or "xuid" not in profile_data:
                    raise HTTPException(
                        status_code=404, 
                        detail=f"Gamertag '{gamertag}' not found on Xbox Live."
                    )
                
                xuid = profile_data["xuid"]
                
                # 2. The Halo Stats API requires the XUID to be wrapped like "xuid(12345...)"
                target_id = f"xuid({xuid})"
                
                # 3. Fetch the match history
                # You can pass 'count=25' (or whatever number you want) to limit the results
                history_resp = await client.stats.get_match_history(target_id, count=10)
                history_data = await history_resp.json()
                
                # FastAPI will automatically convert this Python dictionary into a JSON response
                return history_data

    except HTTPException:
        # Re-raise the 404 so it doesn't get caught by the general Exception block below
        raise
    except Exception as e:
        logger.error(f"Live History Fetch Error: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Failed to fetch live match history from the Halo API."
        )

@app.get("/players/", response_model=list[PlayerOut])
def get_players(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    with Session(engine) as db:
        players = db.exec(select(Player).offset(skip).limit(limit)).all()
        if not players:
            return []
        player_ids = [p.id for p in players]
        # Single query to fetch all relevant matches
        all_matches = db.exec(
            select(Match).where(Match.player_id.in_(player_ids))
        ).all()
        matches_by_player: dict[int, list[Match]] = {}
        for m in all_matches:
            matches_by_player.setdefault(m.player_id, []).append(m)

        return [
            PlayerOut(
                xuid=p.xuid,
                gamertag=p.gamertag,
                latest_match_id=p.latest_match_id,
                matches=[
                    MatchOut(match_id=m.match_id, duration=m.duration, played_at=m.played_at)
                    for m in matches_by_player.get(p.id, [])
                ],
            )
            for p in players
        ]

@app.get("/matches/", response_model=list[MatchOut])
def get_matches(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    with Session(engine) as db:
        matches = db.exec(select(Match).offset(skip).limit(limit)).all()
    return [
        MatchOut(match_id=m.match_id, duration=m.duration, played_at=m.played_at)
        for m in matches
    ]
    
@app.get("/players/{gamertag}/matches", response_model=list[MatchOut])
def get_player_matches(gamertag: str = Path(..., description="The gamertag of the player")):
    with Session(engine) as db:
        # 1. Find the player (case-insensitive matching is recommended for Xbox gamertags)
        # Using .ilike() ensures "AapoKaapo" matches "aapokaapo"
        player_statement = select(Player).where(Player.gamertag.ilike(gamertag))
        player = db.exec(player_statement).first()

        if not player:
            raise HTTPException(status_code=404, detail=f"Player '{gamertag}' not found in database.")

        # 2. Fetch matches for this specific player, newest first
        match_statement = (
            select(Match)
            .where(Match.player_id == player.id)
            .order_by(Match.played_at.desc())
        )
        matches = db.exec(match_statement).all()

        # 3. Map to your existing MatchOut schema
        return [
            MatchOut(
                match_id=m.match_id,
                duration=m.duration,
                played_at=m.played_at,
                # Add any other fields your MatchOut schema expects
            )
            for m in matches
        ]
        
@app.post("/api/debug/force-update", dependencies=[Depends(verify_api_key)])
async def force_update_cycle():
    """DEBUG ONLY: Manually forces the background update cycle to run immediately.
    This will block the HTTP response until the entire cycle is complete.
    """
    logger.info("DEBUG: Manual update cycle triggered via API.")
    
    try:
        # Await the cycle directly using your global database engine
        await _run_update_cycle(engine)
        return {"status": "success", "message": "Manual update cycle completed."}
    except Exception as e:
        logger.error(f"DEBUG Update Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/debug/revalidate-matches", dependencies=[Depends(verify_api_key)])
async def revalidate_all_matches():
    """DEBUG ONLY: Iterates through every match in the database, re-parses the 
    raw JSON, and updates the is_valid flag based on the latest logic.
    """
    logger.info("Starting batch revalidation of all matches...")
    
    try:
        with Session(engine) as db:
            # Fetch all matches and their associated players (we need the player for the xuid)
            statement = select(Match, Player).join(Player)
            results = db.exec(statement).all()
            
            total_matches = len(results)
            valid_count = 0
            invalid_count = 0
            
            for match, player in results:
                try:
                    # 1. Parse the stored JSON string back into a Python dictionary
                    raw_json = json.loads(match.raw_match_stats)
                    asset_id = raw_json.get("MatchInfo", None).get("MapVariant", "").get("AssetId", "")
                    version_id = raw_json.get("MatchInfo", None).get("MapVariant", "").get("VersionId", "")
                    raw_map = await updater.get_raw_map(asset_id, version_id)


                    # 2. Re-run the validation logic
                    new_is_valid = _check_if_match_valid(raw_json, player.xuid, raw_map)
                    
                    # 3. Update the match flag
                    match.is_valid = new_is_valid
                    db.add(match)
                    
                    if new_is_valid:
                        valid_count += 1
                    else:
                        invalid_count += 1
                        
                except Exception as parse_err:
                    logger.error(f"Error parsing match {match.match_id}: {parse_err}")
                    continue
                    
            # Commit all the updates to the database in one big batch!
            db.commit()
            
        logger.info(f"Revalidation complete! {valid_count} valid, {invalid_count} invalid.")
        
        return {
            "status": "success",
            "message": "All matches revalidated.",
            "total_matches_checked": total_matches,
            "valid_matches": valid_count,
            "invalid_matches": invalid_count
        }
        
    except Exception as e:
        logger.error(f"Revalidation Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)