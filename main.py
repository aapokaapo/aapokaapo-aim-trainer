import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, List

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, select

from database import engine
from haloclient import get_client
from models import Match, Player

logger = logging.getLogger(__name__)

def create_db_and_tables():
    # Because models.py is imported, SQLModel knows about the tables
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    from updater import start_background_task
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
    """Serves the index.html file directly from the root URL."""
    # This assumes index.html is in the exact same folder as main.py
    return FileResponse("index.html")


@app.get("/leaderboard", response_class=FileResponse)
async def serve_leaderboard():
    """Serves the leaderboard HTML page."""
    return FileResponse("leaderboard.html")
    

@app.post("/players/", response_model=PlayerOut, status_code=201)
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
        raise HTTPException(status_code=404, detail=f"Gamertag '{request.gamertag}' not found on Xbox Live.")

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
    with Session(engine) as db:
        # ONLY select matches that have been flagged as valid (>= 100 kills)
        statement = select(Match, Player).join(Player).where(Match.is_valid == True)
        results = db.exec(statement).all()
        # Group by player to find their absolute best time
        player_bests = {}
        for match, player in results:
            gt = player.gamertag
            # String comparison works safely here because spnkr timedelta strings 
            # are consistently formatted (e.g. "0:01:23.450" < "0:02:10.000")
            if gt not in player_bests or match.duration < player_bests[gt].duration:
                player_bests[gt] = match

        # Sort all players by their best duration (fastest first)
        sorted_bests = sorted(player_bests.items(), key=lambda x: x[1].duration)

        # Build the final leaderboard list
        leaderboard = []
        for rank, (gamertag, match) in enumerate(sorted_bests, start=1):
            leaderboard.append(LeaderboardEntry(
                rank=rank,
                gamertag=gamertag,
                duration=match.duration,
                played_at=match.played_at
            ))
            
        return leaderboard[:100]

@app.get("/players/{gamertag}/history/live")
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
def get_players():
    with Session(engine) as db:
        players = db.exec(select(Player)).all()
        return [_player_out(db, p) for p in players]


@app.get("/matches/", response_model=list[MatchOut])
def get_matches():
    with Session(engine) as db:
        matches = db.exec(select(Match)).all()
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
