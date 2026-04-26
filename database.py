"""CRUD (Create / Read / Update / Delete) helpers for Player and Match.

All functions accept an already-open :class:`sqlmodel.Session` so callers
control transaction boundaries.  No session or engine is created here;
pass one in from the caller (e.g. ``with Session(engine) as db: ...``).
"""

from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from models import Match, Player


# ---------------------------------------------------------------------------
# Player CRUD
# ---------------------------------------------------------------------------


def add_player(db: Session, xuid: str, gamertag: str) -> Player:
    """Return the existing Player row or create and flush a new one.

    Does **not** commit — the caller is responsible for committing the session.
    """
    player = db.exec(select(Player).where(Player.xuid == xuid)).first()
    if player:
        return player
    player = Player(xuid=xuid, gamertag=gamertag)
    db.add(player)
    db.flush()
    return player


def get_player_by_xuid(db: Session, xuid: str) -> Optional[Player]:
    """Return the Player with the given *xuid*, or ``None``."""
    return db.exec(select(Player).where(Player.xuid == xuid)).first()


def get_player_by_gamertag(db: Session, gamertag: str) -> Optional[Player]:
    """Return the Player with the given *gamertag* (exact match), or ``None``."""
    return db.exec(select(Player).where(Player.gamertag == gamertag)).first()


def search_players_by_gamertag(db: Session, query: str) -> List[Player]:
    """Return all Players whose gamertag contains *query* (case-insensitive)."""
    pattern = f"%{query}%"
    return list(
        db.exec(select(Player).where(Player.gamertag.ilike(pattern))).all()  # type: ignore[attr-defined]
    )


def get_all_players(db: Session) -> List[Player]:
    """Return every Player row in the database."""
    return list(db.exec(select(Player)).all())


def update_player_latest_match(
    db: Session, player: Player, latest_match_id: str
) -> Player:
    """Update the *latest_match_id* cursor for *player*.

    Stages the change — the caller must commit.
    """
    player.latest_match_id = latest_match_id
    db.add(player)
    return player


def delete_player(db: Session, xuid: str) -> bool:
    """Delete the Player with the given *xuid*.

    Returns ``True`` if a row was deleted, ``False`` if not found.
    """
    player = db.exec(select(Player).where(Player.xuid == xuid)).first()
    if player is None:
        return False
    db.delete(player)
    return True


# ---------------------------------------------------------------------------
# Match CRUD
# ---------------------------------------------------------------------------


def add_match(
    db: Session,
    *,
    match_id: str,
    player_id: int,
    gamemode: int,
    duration: str,
    played_at: datetime,
    raw_match_stats: str,
    valid: bool = True,
) -> Match:
    """Return the existing Match row or create and stage a new one.

    Guarantees uniqueness on *match_id* — no duplicate rows will be inserted.
    Does **not** commit — the caller controls the transaction.
    """
    existing = db.exec(select(Match).where(Match.match_id == match_id)).first()
    if existing:
        return existing
    match = Match(
        match_id=match_id,
        player_id=player_id,
        gamemode=gamemode,
        valid=valid,
        duration=duration,
        played_at=played_at,
        raw_match_stats=raw_match_stats,
    )
    db.add(match)
    return match


def get_match_by_id(db: Session, match_id: str) -> Optional[Match]:
    """Return the Match with the given *match_id*, or ``None``."""
    return db.exec(select(Match).where(Match.match_id == match_id)).first()


def get_all_matches(db: Session) -> List[Match]:
    """Return every Match row in the database."""
    return list(db.exec(select(Match)).all())


def get_player_matches(db: Session, player_id: int) -> List[Match]:
    """Return all Match rows linked to *player_id*, ordered by date descending."""
    return list(
        db.exec(
            select(Match)
            .where(Match.player_id == player_id)
            .order_by(Match.played_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


def delete_match(db: Session, match_id: str) -> bool:
    """Delete the Match with the given *match_id*.

    Returns ``True`` if a row was deleted, ``False`` if not found.
    """
    match = db.exec(select(Match).where(Match.match_id == match_id)).first()
    if match is None:
        return False
    db.delete(match)
    return True
