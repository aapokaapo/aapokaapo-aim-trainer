# models.py
from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel

class Player(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    xuid: str = Field(index=True, unique=True)
    gamertag: str = Field(index=True)
    latest_match_id: Optional[str] = Field(default=None)

class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: str = Field(index=True, unique=True)
    player_id: int = Field(foreign_key="player.id", index=True)
    duration: str
    played_at: datetime
    raw_match_stats: str
    is_valid: bool = Field(default=False, index=True)