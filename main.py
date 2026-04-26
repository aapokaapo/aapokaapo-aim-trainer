import json
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from spnkr.client import HaloInfiniteClient
from sqlmodel import Field, Session, SQLModel, create_engine, select

from spartan_auth import SpartanTokenManager

# Load .env variables (no-op when python-dotenv is unavailable; spartan_auth
# already attempts the same load at import time).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Shared token manager instance; initialised lazily on first request so that
# startup does not fail when Azure credentials are absent (e.g. when
# SPARTAN_TOKEN is provided directly instead).
_token_manager: Optional[SpartanTokenManager] = None
_token_manager_lock = asyncio.Lock()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///matches.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


# ---------------------------------------------------------------------------
# DB Models
# ---------------------------------------------------------------------------

class Player(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    xuid: str = Field(index=True, unique=True)
    gamertag: str = Field(index=True)
    # UUID of the newest match we have fetched (not necessarily stored) for
    # this player. Used as a cursor so we only fetch new history on subsequent
    # imports.
    latest_match_id: Optional[str] = Field(default=None)


class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: str = Field(index=True, unique=True)
    player_id: int = Field(foreign_key="player.id")
    duration: str
    played_at: datetime
    raw_match_stats: str  # JSON text


# ---------------------------------------------------------------------------
# DB initialisation
# ---------------------------------------------------------------------------

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Halo Aim Trainer Match Importer", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    gamertag: str
    gamemode: int


class MatchOut(BaseModel):
    match_id: str
    duration: str
    played_at: datetime


class PlayerOut(BaseModel):
    xuid: str
    gamertag: str
    latest_match_id: Optional[str]
    matches: list[MatchOut]


class ImportResponse(BaseModel):
    imported: int
    player: PlayerOut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_player(db: Session, xuid: str, gamertag: str) -> Player:
    player = db.exec(select(Player).where(Player.xuid == xuid)).first()
    if not player:
        player = Player(xuid=xuid, gamertag=gamertag)
        db.add(player)
        db.flush()
    return player


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

@app.post("/import-matches/", response_model=ImportResponse)
async def import_matches(body: ImportRequest):
    spartan_token = os.getenv("SPARTAN_TOKEN")
    clearance_token = os.getenv("CLEARANCE_TOKEN")

    async with aiohttp.ClientSession() as session:
        # If SPARTAN_TOKEN is not set, obtain it at runtime via the Azure OAuth
        # flow using credentials from .env (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
        # AZURE_REFRESH_TOKEN, REDIRECT_URI).
        if not spartan_token:
            global _token_manager
            async with _token_manager_lock:
                if _token_manager is None:
                    try:
                        _token_manager = SpartanTokenManager()
                    except EnvironmentError as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=(
                                "SPARTAN_TOKEN is not set and Azure OAuth credentials "
                                f"are incomplete: {exc}"
                            ),
                        )
            try:
                spartan_tokens = await _token_manager.get_spartan_token(session)
                spartan_token = spartan_tokens.spartan_token
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to obtain Spartan token via Azure OAuth: {exc}",
                )

        if not spartan_token or not clearance_token:
            raise HTTPException(
                status_code=500,
                detail="SPARTAN_TOKEN and CLEARANCE_TOKEN environment variables must be set.",
            )

        client = HaloInfiniteClient(
            session=session,
            spartan_token=spartan_token,
            clearance_token=clearance_token,
        )

        # Resolve XUID from the profile API before touching the DB.
        profile_resp = await client.profile.get_user_by_gamertag(body.gamertag)
        user = await profile_resp.parse()
        xuid = str(user.xuid)

        # Ensure the player row exists and retrieve the current cursor.
        with Session(engine) as db:
            player = _get_or_create_player(db, xuid, body.gamertag)
            db.commit()
            db.refresh(player)
            stop_at_match_id = player.latest_match_id

        # Fetch match history, stopping once we reach the previously stored
        # latest match (incremental fetch).
        all_results = []
        start = 0
        batch_size = 25
        done = False
        while not done:
            response = await client.stats.get_match_history(
                body.gamertag, start=start, count=batch_size
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

        # Filter by requested gamemode.
        filtered = [
            m
            for m in all_results
            if int(m.match_info.game_variant_category) == body.gamemode
        ]

        # Fetch raw match stats JSON for each match that passes the filter.
        match_stats_list = []
        for m in filtered:
            stats_resp = await client.stats.get_match_stats(m.match_id)
            raw_json = await stats_resp.json()
            match_stats_list.append((m, raw_json))

    # Persist everything inside a single transaction.
    new_count = 0
    # Track the newest match seen across ALL game modes as the new cursor.
    newest_match_id: Optional[str] = str(all_results[0].match_id) if all_results else None

    with Session(engine) as db:
        player = db.exec(select(Player).where(Player.xuid == xuid)).first()
        if player is None:
            raise HTTPException(status_code=500, detail="Player record missing after creation.")

        if newest_match_id is not None:
            player.latest_match_id = newest_match_id
        db.add(player)

        for history_entry, raw_json in match_stats_list:
            mid = str(history_entry.match_id)
            if db.exec(select(Match).where(Match.match_id == mid)).first():
                continue
            match_obj = Match(
                match_id=mid,
                player_id=player.id,
                duration=str(history_entry.match_info.duration),
                played_at=history_entry.match_info.start_time,
                raw_match_stats=json.dumps(raw_json),
            )
            db.add(match_obj)
            new_count += 1

        db.commit()
        db.refresh(player)
        player_out = _player_out(db, player)

    return ImportResponse(imported=new_count, player=player_out)


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


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
