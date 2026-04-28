"""Unit tests for the _check_if_match_valid function in updater.py."""

import pytest
from updater import _check_if_match_valid, TARGET_ASSET_ID, TARGET_VERSION_ID

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

MAP_AUTHOR_XUID = "xuid(2814672600485177)"
PLAYER_XUID = "12345678901234567"
PLAYER_TEAM_ID = 0


def _make_raw_json(
    asset_id: str = TARGET_ASSET_ID,
    version_id: str = TARGET_VERSION_ID,
    kills: int = 120,
    team_score: int = 120,
    extra_teammate: bool = False,
) -> dict:
    """Build a minimal raw match-stats dict that satisfies all validity checks
    unless the caller overrides individual fields."""
    players = [
        {
            "PlayerId": f"xuid({PLAYER_XUID})",
            "PlayerTeamStats": [
                {
                    "TeamId": PLAYER_TEAM_ID,
                    "Stats": {"CoreStats": {"Kills": kills}},
                }
            ],
        }
    ]

    if extra_teammate:
        players.append(
            {
                "PlayerId": "xuid(99999999999999999)",
                "PlayerTeamStats": [
                    {
                        "TeamId": PLAYER_TEAM_ID,
                        "Stats": {"CoreStats": {"Kills": 10}},
                    }
                ],
            }
        )

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
                "TeamId": PLAYER_TEAM_ID,
                "Stats": {"CoreStats": {"Score": team_score}},
            }
        ],
    }


def _make_valid_map() -> dict:
    return {
        "PublicName": "Live Fire - Ranked",
        "Admin": MAP_AUTHOR_XUID,
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_happy_path_returns_true():
    """All criteria satisfied → valid."""
    assert _check_if_match_valid(_make_raw_json(), PLAYER_XUID, _make_valid_map()) is True


def test_wrong_asset_id_returns_false():
    """Wrong UgcGameVariant asset_id → invalid."""
    raw = _make_raw_json(asset_id="00000000-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_wrong_version_id_returns_false():
    """Wrong UgcGameVariant version_id → invalid."""
    raw = _make_raw_json(version_id="00000000-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_wrong_map_name_returns_false():
    """Map public name doesn't match → invalid."""
    raw_map = {"PublicName": "Wrong Map Name", "Admin": MAP_AUTHOR_XUID}
    assert _check_if_match_valid(_make_raw_json(), PLAYER_XUID, raw_map) is False


def test_wrong_map_author_xuid_returns_false():
    """Map author XUID doesn't match → invalid."""
    raw_map = {"PublicName": "Live Fire - Ranked", "Admin": "xuid(0000000000000000)"}
    assert _check_if_match_valid(_make_raw_json(), PLAYER_XUID, raw_map) is False


def test_no_team_reaches_100_points_returns_false():
    """No team score reaches 100 → invalid."""
    raw = _make_raw_json(team_score=99)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_exactly_100_points_is_valid():
    """A score of exactly 100 is accepted (≥100)."""
    raw = _make_raw_json(team_score=100)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is True


def test_player_below_100_kills_returns_false():
    """Player has fewer than 100 kills → invalid."""
    raw = _make_raw_json(kills=99)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_exactly_100_kills_is_valid():
    """Exactly 100 kills is accepted (≥100)."""
    raw = _make_raw_json(kills=100)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is True


def test_player_has_teammate_returns_false():
    """Another player on the same team → invalid (must be solo)."""
    raw = _make_raw_json(extra_teammate=True)
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_player_not_found_returns_false():
    """XUID not present in Players list → invalid."""
    raw = _make_raw_json()
    assert _check_if_match_valid(raw, "00000000000000000", _make_valid_map()) is False


def test_missing_match_info_returns_false():
    """MatchInfo key completely absent → invalid (safe missing-field handling)."""
    raw = {
        "Players": [
            {
                "PlayerId": f"xuid({PLAYER_XUID})",
                "PlayerTeamStats": [
                    {
                        "TeamId": PLAYER_TEAM_ID,
                        "Stats": {"CoreStats": {"Kills": 120}},
                    }
                ],
            }
        ],
        "Teams": [
            {"TeamId": PLAYER_TEAM_ID, "Stats": {"CoreStats": {"Score": 120}}}
        ],
    }
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is False


def test_empty_raw_map_returns_false():
    """Empty map dict → public name and admin are missing → invalid."""
    assert _check_if_match_valid(_make_raw_json(), PLAYER_XUID, {}) is False


def test_opponent_on_different_team_does_not_count_as_teammate():
    """A second player on a *different* team must not trigger the solo check."""
    raw = _make_raw_json()
    # Add an opponent on team 1 (player is on team 0)
    raw["Players"].append(
        {
            "PlayerId": "xuid(88888888888888888)",
            "PlayerTeamStats": [
                {
                    "TeamId": 1,
                    "Stats": {"CoreStats": {"Kills": 80}},
                }
            ],
        }
    )
    assert _check_if_match_valid(raw, PLAYER_XUID, _make_valid_map()) is True
