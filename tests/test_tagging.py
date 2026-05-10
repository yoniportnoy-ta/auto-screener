"""Tests for tagging name conventions + person-id extraction.

Network-touching paths (login, tag CRUD) are covered by integration smoke tests
that run against the real Comeet test user — not in this file.
"""
from __future__ import annotations

import pytest

from app.comeet_app_client import _coerce_int, _extract_person_id
from app.tagging import RATING_TAG_SUFFIX, rating_tag_name


def test_rating_tag_name_for_each_rating() -> None:
    assert rating_tag_name(5) == "AI: Superstar"
    assert rating_tag_name(4) == "AI: Great"
    assert rating_tag_name(3) == "AI: OK"
    assert rating_tag_name(2) == "AI: Not a fit"
    assert rating_tag_name(1) == "AI: Way off"


def test_rating_tag_name_unknown_rating_raises() -> None:
    with pytest.raises(ValueError):
        rating_tag_name(0)
    with pytest.raises(ValueError):
        rating_tag_name(6)


def test_rating_tag_suffix_covers_full_range() -> None:
    assert set(RATING_TAG_SUFFIX.keys()) == {1, 2, 3, 4, 5}


def test_extract_person_id_from_nested_person_object() -> None:
    payload = {"id": 9999, "person": {"id": 57144261, "name": "Ada"}}
    assert _extract_person_id(payload) == 57144261


def test_extract_person_id_falls_back_to_top_level_field() -> None:
    payload = {"person_id": 57144261}
    assert _extract_person_id(payload) == 57144261


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
