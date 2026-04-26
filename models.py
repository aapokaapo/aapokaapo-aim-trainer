"""SQLModel ORM models and database engine for the Halo Aim Trainer app."""

import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlmodel import Field, SQLModel, create_engine

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///matches.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Player(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    xuid: str = Field(index=True, unique=True)
    gamertag: str = Field(index=True)
    # UUID of the newest match we have fetched (not necessarily stored) for
    # this player.  Used as a cursor so we only fetch new history on subsequent
    # imports.
    latest_match_id: Optional[str] = Field(default=None)


class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: str = Field(index=True, unique=True)
    player_id: int = Field(foreign_key="player.id")
    gamemode: int = Field(default=0)
    valid: bool = Field(default=True)
    duration: str
    played_at: datetime
    raw_match_stats: str  # JSON text


def create_db_and_tables() -> None:
    """Create all database tables defined in the SQLModel metadata."""
    SQLModel.metadata.create_all(engine)
