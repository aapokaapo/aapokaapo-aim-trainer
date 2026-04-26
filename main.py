import os
from contextlib import asynccontextmanager
from typing import Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from spnkr.client import HaloInfiniteClient
from sqlmodel import Field, Session, SQLModel, create_engine, select

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///matches.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: str = Field(index=True, unique=True)
    start_time: str
    end_time: str
    game_mode: int


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Halo Aim Trainer Match Importer", lifespan=lifespan)


class ImportRequest(BaseModel):
    gamertag: str
    gamemode: int


class MatchOut(BaseModel):
    match_id: str
    start_time: str
    end_time: str
    game_mode: int


class ImportResponse(BaseModel):
    imported: int
    matches: list[MatchOut]


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

        all_results = []
        start = 0
        batch_size = 25
        while True:
            response = await client.stats.get_match_history(
                body.gamertag, start=start, count=batch_size
            )
            history = await response.parse()
            all_results.extend(history.results)
            if history.result_count < batch_size:
                break
            start += batch_size

        filtered = [
            m
            for m in all_results
            if int(m.match_info.game_variant_category) == body.gamemode
        ]

    imported_matches: list[MatchOut] = []
    with Session(engine) as db:
        for m in filtered:
            mid = str(m.match_id)
            existing = db.exec(select(Match).where(Match.match_id == mid)).first()
            if existing:
                imported_matches.append(
                    MatchOut(
                        match_id=existing.match_id,
                        start_time=existing.start_time,
                        end_time=existing.end_time,
                        game_mode=existing.game_mode,
                    )
                )
                continue
            match_obj = Match(
                match_id=mid,
                start_time=str(m.match_info.start_time),
                end_time=str(m.match_info.end_time),
                game_mode=int(m.match_info.game_variant_category),
            )
            db.add(match_obj)
            imported_matches.append(
                MatchOut(
                    match_id=match_obj.match_id,
                    start_time=match_obj.start_time,
                    end_time=match_obj.end_time,
                    game_mode=match_obj.game_mode,
                )
            )
        db.commit()

    return ImportResponse(imported=len(imported_matches), matches=imported_matches)


@app.get("/matches/", response_model=list[MatchOut])
def get_matches():
    with Session(engine) as db:
        matches = db.exec(select(Match)).all()
    return [
        MatchOut(
            match_id=m.match_id,
            start_time=m.start_time,
            end_time=m.end_time,
            game_mode=m.game_mode,
        )
        for m in matches
    ]


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
