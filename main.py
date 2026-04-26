import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from spnkr.client import HaloInfiniteClient
from sqlmodel import Session, select

from database import (
    add_match,
    add_player,
    get_all_matches,
    get_all_players,
    get_match_by_id,
    get_player_by_xuid,
    get_player_matches,
    search_players_by_gamertag,
)
from models import Match, Player, create_db_and_tables, engine

# Load environment variables from .env (no-op when already set, e.g. in CI).
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s \u2013 %(message)s",
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: initialise DB tables then start the background updater."""
    create_db_and_tables()

    from updater import start_background_task  # noqa: PLC0415

    task = start_background_task(engine)
    yield
    task.cancel()


app = FastAPI(title="Halo Aim Trainer Match Importer", lifespan=lifespan)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))


# ---------------------------------------------------------------------------
# Request / Response schemas (JSON API)
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
# Template helpers
# ---------------------------------------------------------------------------


def _fmt_duration(duration_str: str) -> str:
    """Convert an ISO 8601 duration string (e.g. ``PT2M30S``) to ``MM:SS``."""
    m = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        duration_str or "",
    )
    if not m:
        return duration_str
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(float(m.group(3) or 0))
    total_minutes = hours * 60 + minutes
    return f"{total_minutes}:{seconds:02d}"


def _parse_core_stats(raw_match_stats: str, player_xuid: str) -> dict:
    """Extract the CoreStats block for *player_xuid* from raw match stats JSON."""
    try:
        data = json.loads(raw_match_stats)
    except (json.JSONDecodeError, TypeError):
        return {}

    # Try to find the stats for the specific player first.
    for player in data.get("Players", []):
        pid = str(player.get("PlayerId", ""))
        if player_xuid and player_xuid not in pid:
            continue
        team_stats = player.get("PlayerTeamStats", [])
        if team_stats:
            return team_stats[0].get("Stats", {}).get("CoreStats", {})

    # Fallback: return first player's CoreStats.
    for player in data.get("Players", []):
        team_stats = player.get("PlayerTeamStats", [])
        if team_stats:
            return team_stats[0].get("Stats", {}).get("CoreStats", {})

    return {}


def _build_match_context(match: Match, player: Player) -> dict:
    """Build a template-ready dict for a single *match*."""
    core = _parse_core_stats(match.raw_match_stats, player.xuid)
    accuracy = core.get("Accuracy")
    return {
        "match": match,
        "player": player,
        "duration_fmt": _fmt_duration(match.duration),
        "kills": core.get("Kills", "\u2013"),
        "deaths": core.get("Deaths", "\u2013"),
        "assists": core.get("Assists", "\u2013"),
        "accuracy": f"{accuracy:.1f}%" if isinstance(accuracy, (int, float)) else "\u2013",
        "score": core.get("Score", "\u2013"),
    }


def _player_out(db: Session, player: Player) -> PlayerOut:
    matches = get_player_matches(db, player.id)
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
# HTML views
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def view_index(request: Request):
    with Session(engine) as db:
        players = get_all_players(db)
        leaderboard = []
        for p in players:
            matches = get_player_matches(db, p.id)
            leaderboard.append({"player": p, "match_count": len(matches)})

    leaderboard.sort(key=lambda e: e["match_count"], reverse=True)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"leaderboard": leaderboard},
    )


@app.get("/player/{xuid}", response_class=HTMLResponse)
def view_player(request: Request, xuid: str):
    with Session(engine) as db:
        player = get_player_by_xuid(db, xuid)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        matches = get_player_matches(db, player.id)
        match_contexts = [_build_match_context(m, player) for m in matches]

    best = None
    if match_contexts:
        def _sort_key(ctx: dict):
            k = ctx["kills"]
            return k if isinstance(k, int) else -1

        best = max(match_contexts, key=_sort_key)

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "player": player,
            "matches": match_contexts,
            "best_match": best,
        },
    )


@app.get("/match/{match_id:path}", response_class=HTMLResponse)
def view_match(request: Request, match_id: str):
    with Session(engine) as db:
        match = get_match_by_id(db, match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Match not found")
        player_row = db.exec(select(Player).where(Player.id == match.player_id)).first()
        if player_row is None:
            raise HTTPException(status_code=404, detail="Player not found")
        ctx = _build_match_context(match, player_row)

    return templates.TemplateResponse(
        request,
        "match.html",
        ctx,
    )


@app.get("/search", response_class=HTMLResponse)
def view_search(request: Request, q: str = ""):
    q = q.strip()
    if not q:
        return RedirectResponse("/")

    with Session(engine) as db:
        results = search_players_by_gamertag(db, q)

    if len(results) == 1:
        return RedirectResponse(f"/player/{results[0].xuid}")

    return templates.TemplateResponse(
        request,
        "search.html",
        {"query": q, "results": results},
    )


# ---------------------------------------------------------------------------
# JSON API endpoints (kept for backwards compatibility)
# ---------------------------------------------------------------------------


@app.post("/import-matches/", response_model=ImportResponse)
async def import_matches(body: ImportRequest):
    spartan_token = os.getenv("SPARTAN_TOKEN")
    clearance_token = os.getenv("CLEARANCE_TOKEN")

    if not spartan_token or not clearance_token:
        raise HTTPException(
            status_code=500,
            detail="SPARTAN_TOKEN and CLEARANCE_TOKEN environment variables must be set.",
        )

    async with aiohttp.ClientSession() as session:
        client = HaloInfiniteClient(
            session=session,
            spartan_token=spartan_token,
            clearance_token=clearance_token,
        )

        profile_resp = await client.profile.get_user_by_gamertag(body.gamertag)
        user = await profile_resp.parse()
        xuid = str(user.xuid)

        with Session(engine) as db:
            player = add_player(db, xuid, body.gamertag)
            db.commit()
            db.refresh(player)
            stop_at_match_id = player.latest_match_id

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

        filtered = [
            m
            for m in all_results
            if int(m.match_info.game_variant_category) == body.gamemode
        ]

        match_stats_list = []
        for m in filtered:
            stats_resp = await client.stats.get_match_stats(m.match_id)
            raw_json = await stats_resp.json()
            match_stats_list.append((m, raw_json))

    new_count = 0
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
            existing = get_match_by_id(db, mid)
            if existing:
                continue
            add_match(
                db,
                match_id=mid,
                player_id=player.id,
                gamemode=body.gamemode,
                valid=True,
                duration=str(history_entry.match_info.duration),
                played_at=history_entry.match_info.start_time,
                raw_match_stats=json.dumps(raw_json),
            )
            new_count += 1

        db.commit()
        db.refresh(player)
        player_out = _player_out(db, player)

    return ImportResponse(imported=new_count, player=player_out)


@app.get("/players/", response_model=list[PlayerOut])
def get_players():
    with Session(engine) as db:
        players = get_all_players(db)
        return [_player_out(db, p) for p in players]


@app.get("/matches/", response_model=list[MatchOut])
def get_matches():
    with Session(engine) as db:
        matches = get_all_matches(db)
    return [
        MatchOut(match_id=m.match_id, duration=m.duration, played_at=m.played_at)
        for m in matches
    ]


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
