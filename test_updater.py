"""Unit tests for match validation logic in updater.py."""

import pytest
from updater import (
    LIVE_FIRE_RANKED_MAP_ASSET_ID,
    TARGET_ASSET_ID,
    TARGET_VERSION_ID,
    is_live_fire_ranked,
    _check_if_match_valid,
    _passes_criteria,
)


# ---------------------------------------------------------------------------
# is_live_fire_ranked
# ---------------------------------------------------------------------------

def test_is_live_fire_ranked_returns_true_for_correct_id():
    # Intentionally uses the literal UUID (not the constant) so a typo in the
    # constant itself would be caught.
    assert is_live_fire_ranked("309253f8-7a75-48ff-83e1-e7fb3db2ac47") is True


def test_is_live_fire_ranked_returns_false_for_other_id():
    assert is_live_fire_ranked("00000000-0000-0000-0000-000000000000") is False


def test_is_live_fire_ranked_case_insensitive():
    # The comparison is normalised to lowercase so upper-case variants also match.
    assert is_live_fire_ranked("309253F8-7A75-48FF-83E1-E7FB3DB2AC47") is True


def test_live_fire_ranked_asset_id_constant():
    assert LIVE_FIRE_RANKED_MAP_ASSET_ID == "309253f8-7a75-48ff-83e1-e7fb3db2ac47"


# ---------------------------------------------------------------------------
# Helpers for building minimal match JSON payloads
# ---------------------------------------------------------------------------

def _make_match_json(
    *,
    ugc_asset_id=TARGET_ASSET_ID,
    ugc_version_id=TARGET_VERSION_ID,
    map_asset_id=LIVE_FIRE_RANKED_MAP_ASSET_ID,
    player_xuid="12345",
    player_team_id=0,
    player_kills=100,
    team_score=100,
    teammates=None,
):
    """Return a minimal raw match JSON dict that passes validation by default."""
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
    if teammates:
        for tm_xuid in teammates:
            players.append(
                {
                    "PlayerId": f"xuid({tm_xuid})",
                    "PlayerTeamStats": [
                        {
                            "TeamId": player_team_id,
                            "Stats": {"CoreStats": {"Kills": 50}},
                        }
                    ],
                }
            )

    return {
        "MatchInfo": {
            "UgcGameVariant": {
                "AssetId": ugc_asset_id,
                "VersionId": ugc_version_id,
            },
            "MapVariant": {
                "AssetId": map_asset_id,
            },
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
# _check_if_match_valid
# ---------------------------------------------------------------------------

def test_valid_match_passes():
    raw_json = _make_match_json(player_xuid="99")
    assert _check_if_match_valid(raw_json, "99") is True


def test_wrong_ugc_asset_id_fails():
    raw_json = _make_match_json(ugc_asset_id="aaaaaaaa-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw_json, "99") is False


def test_wrong_ugc_version_id_fails():
    raw_json = _make_match_json(ugc_version_id="aaaaaaaa-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw_json, "99") is False


def test_wrong_map_asset_id_fails():
    raw_json = _make_match_json(map_asset_id="00000000-0000-0000-0000-000000000000")
    assert _check_if_match_valid(raw_json, "99") is False


def test_correct_map_asset_id_passes():
    raw_json = _make_match_json(player_xuid="99", map_asset_id=LIVE_FIRE_RANKED_MAP_ASSET_ID)
    assert _check_if_match_valid(raw_json, "99") is True


def test_player_with_teammate_fails():
    raw_json = _make_match_json(player_xuid="99", teammates=["11111"])
    assert _check_if_match_valid(raw_json, "99") is False


def test_team_score_below_100_fails():
    raw_json = _make_match_json(player_xuid="99", team_score=99)
    assert _check_if_match_valid(raw_json, "99") is False


def test_team_score_exactly_100_passes():
    raw_json = _make_match_json(player_xuid="99", team_score=100)
    assert _check_if_match_valid(raw_json, "99") is True


def test_kills_below_100_fails():
    raw_json = _make_match_json(player_xuid="99", player_kills=99)
    assert _check_if_match_valid(raw_json, "99") is False


def test_kills_exactly_100_passes():
    raw_json = _make_match_json(player_xuid="99", player_kills=100)
    assert _check_if_match_valid(raw_json, "99") is True


def test_missing_player_fails():
    raw_json = _make_match_json(player_xuid="99")
    # Validate as a different xuid that's not in the players list
    assert _check_if_match_valid(raw_json, "00000") is False


def test_empty_json_fails():
    assert _check_if_match_valid({}, "99") is False
