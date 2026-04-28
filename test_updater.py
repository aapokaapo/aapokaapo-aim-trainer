"""Unit tests for _check_if_match_valid in updater.py."""

import pytest
from updater import _check_if_match_valid, TARGET_ASSET_ID, TARGET_VERSION_ID

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PLAYER_XUID = "1234567890"
PLAYER_ID = f"xuid({PLAYER_XUID})"
TEAM_ID = 0


def _make_raw_map(
    public_name: str = "Live Fire - Ranked",
    admin: str = "xuid(2814672600485177)",
) -> dict:
    return {"PublicName": public_name, "Admin": admin}


def _make_raw_json(
    asset_id: str = TARGET_ASSET_ID,
    version_id: str = TARGET_VERSION_ID,
    player_kills: int = 100,
    team_score: int = 100,
    extra_players: list | None = None,
    player_xuid: str = PLAYER_XUID,
    player_team_id: int = TEAM_ID,
) -> dict:
    """Build a minimal raw match stats dict that passes all validity checks by default."""
    players = [
        {
            "PlayerId": f"xuid({player_xuid})",
            "PlayerTeamStats": [
                {
                    "TeamId": player_team_id,
                    "Stats": {"CoreStats": {"Kills": player_kills}},
                }
            ],
        }
    ]
    if extra_players:
        players.extend(extra_players)

    return {
        "MatchInfo": {
            "UgcGameVariant": {
                "AssetId": asset_id,
                "VersionId": version_id,
            }
        },
        "Players": players,
        "Teams": [
            {
                "TeamId": player_team_id,
                "Stats": {"CoreStats": {"Score": team_score}},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_match_passes():
    """A match that satisfies every criterion should be valid."""
    assert _check_if_match_valid(
        _make_raw_json(), PLAYER_XUID, _make_raw_map()
    ) is True


# ---------------------------------------------------------------------------
# Game mode checks
# ---------------------------------------------------------------------------

def test_wrong_asset_id_fails():
    raw = _make_raw_json(asset_id="00000000-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_wrong_version_id_fails():
    raw = _make_raw_json(version_id="00000000-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_missing_ugc_game_variant_fails():
    raw = _make_raw_json()
    del raw["MatchInfo"]["UgcGameVariant"]
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


# ---------------------------------------------------------------------------
# Map checks
# ---------------------------------------------------------------------------

def test_wrong_map_name_fails():
    assert _check_if_match_valid(
        _make_raw_json(), PLAYER_XUID, _make_raw_map(public_name="Live Fire")
    ) is False


def test_wrong_map_author_xuid_fails():
    assert _check_if_match_valid(
        _make_raw_json(), PLAYER_XUID, _make_raw_map(admin="xuid(9999999999999999)")
    ) is False


def test_missing_map_name_fails():
    assert _check_if_match_valid(
        _make_raw_json(), PLAYER_XUID, {}
    ) is False


# ---------------------------------------------------------------------------
# Score / kills checks
# ---------------------------------------------------------------------------

def test_team_score_below_100_fails():
    raw = _make_raw_json(team_score=99)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_team_score_exactly_100_passes():
    raw = _make_raw_json(team_score=100)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is True


def test_team_score_above_100_passes():
    raw = _make_raw_json(team_score=101)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is True


def test_player_kills_below_100_fails():
    raw = _make_raw_json(player_kills=99)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_player_kills_exactly_100_passes():
    raw = _make_raw_json(player_kills=100)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is True


# ---------------------------------------------------------------------------
# Teammate check
# ---------------------------------------------------------------------------

def test_player_with_teammate_on_same_team_fails():
    """If another player shares the same team ID the match is invalid."""
    teammate = {
        "PlayerId": "xuid(9999999999)",
        "PlayerTeamStats": [
            {
                "TeamId": TEAM_ID,
                "Stats": {"CoreStats": {"Kills": 0}},
            }
        ],
    }
    raw = _make_raw_json(extra_players=[teammate])
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_player_with_opponent_on_different_team_passes():
    """A player on a different team is NOT a teammate; match should remain valid."""
    opponent = {
        "PlayerId": "xuid(9999999999)",
        "PlayerTeamStats": [
            {
                "TeamId": TEAM_ID + 1,  # different team
                "Stats": {"CoreStats": {"Kills": 50}},
            }
        ],
    }
    raw = _make_raw_json(extra_players=[opponent])
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_player_not_in_match_fails():
    """If the target XUID is not present in Players, return False."""
    raw = _make_raw_json(player_xuid="0000000000")  # different XUID in the data
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_empty_players_list_fails():
    raw = _make_raw_json()
    raw["Players"] = []
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_raw_map()) is False


def test_none_raw_map_fields_handled():
    """None values in raw_map should not crash and should fail validation."""
    assert _check_if_match_valid(_make_raw_json(), PLAYER_XUID, {"PublicName": None, "Admin": None}) is False
