"""Tests for tagging name conventions + person-id extraction.

Network-touching paths (login, tag CRUD) are covered by integration smoke tests
that run against the real Comeet test user — not in this file.
"""
from __future__ import annotations

import pytest

from app.comeet_app_client import _coerce_int, _extract_person_id
from app.tagging import (
    RATING_TAG_NAMES,
    numeric_candidate_id_from_url,
    rating_tag_color,
    rating_tag_name,
)


def test_numeric_candidate_id_from_url() -> None:
    assert numeric_candidate_id_from_url("https://app.comeet.co/app/req/423848/can/57511356") == "57511356"
    assert numeric_candidate_id_from_url("https://app.comeet.co/app/req/423848/can/57511356?reqStatus=1") == "57511356"
    assert numeric_candidate_id_from_url("https://app.comeet.co/app/req/423848/can/57511356/") == "57511356"
    assert numeric_candidate_id_from_url(None) is None
    assert numeric_candidate_id_from_url("") is None
    assert numeric_candidate_id_from_url("https://example.com") is None


def test_rating_tag_name_matches_comeet_tags() -> None:
    """Tag names match the human-created Comeet tags (incl. the space typo in 'Way off')."""
    assert rating_tag_name(5) == "Superstar (AI Screener)"
    assert rating_tag_name(4) == "Great (AI Screener)"
    assert rating_tag_name(3) == "OK (AI Screener)"
    assert rating_tag_name(2) == "Not a fit (AI Screener)"
    assert rating_tag_name(1) == "Way off ( AI Screener)"


def test_rating_tag_name_unknown_rating_raises() -> None:
    with pytest.raises(ValueError):
        rating_tag_name(0)
    with pytest.raises(ValueError):
        rating_tag_name(6)


def test_rating_tag_names_covers_full_range() -> None:
    assert set(RATING_TAG_NAMES.keys()) == {1, 2, 3, 4, 5}


def test_rating_tag_color_is_none_for_all_ratings() -> None:
    """Colors are owned by Comeet's tag-management UI; we never overwrite."""
    for rating in (1, 2, 3, 4, 5):
        assert rating_tag_color(rating) is None


def test_extract_person_id_from_modern_top_level_person_field() -> None:
    """The modern Comeet shape: `person` is a top-level numeric field."""
    payload = {"id": 57511356, "person": 57144261, "person_uid": "C3.F7635"}
    assert _extract_person_id(payload) == 57144261


def test_extract_person_id_from_nested_person_object() -> None:
    payload = {"id": 9999, "person": {"id": 57144261, "name": "Ada"}}
    assert _extract_person_id(payload) == 57144261


def test_extract_person_id_falls_back_to_top_level_field() -> None:
    payload = {"person_id": 57144261}
    assert _extract_person_id(payload) == 57144261


def test_extract_person_id_ignores_alphanumeric_person_uid() -> None:
    """`person_uid` is alphanumeric (e.g. 'C3.F7635') and unusable for tagging — never accept it."""
    payload = {"person_uid": "C3.F7635"}
    assert _extract_person_id(payload) is None


def test_extract_person_id_returns_none_when_absent() -> None:
    assert _extract_person_id({"foo": "bar"}) is None
    assert _extract_person_id(None) is None
    assert _extract_person_id([]) is None


def test_coerce_int_accepts_numeric_strings_only() -> None:
    assert _coerce_int(42) == 42
    assert _coerce_int("42") == 42
    assert _coerce_int("abc") is None
    assert _coerce_int(None) is None
    assert _coerce_int(True) is None  # bools rejected to avoid confusion
