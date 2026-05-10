"""Smoke tests for the public Comeet API client.

Network calls are mocked via httpx-respx — no real Comeet hits during tests.
"""
from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.comeet_client import (
    ComeetBandwidthError,
    ComeetClient,
    ComeetError,
    ComeetTransientError,
    candidate_active_for_screening,
    candidate_full_name,
    candidate_in_allowed_step,
    candidate_max_activity_iso,
    mint_token,
    position_country,
    position_jd_text,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _stub_creds(monkeypatch):
    monkeypatch.setattr(settings, "comeet_api_key", "test-key")
    monkeypatch.setattr(settings, "comeet_api_secret", "test-secret")


def test_mint_token_returns_jwt_with_iss_and_exp() -> None:
    token = mint_token("k", "s", ttl_seconds=60)
    import jwt
    decoded = jwt.decode(token, "s", algorithms=["HS256"])
    assert decoded["iss"] == "k"
    assert decoded["exp"] > int(time.time())


@respx.mock
def test_list_open_positions_walks_pages() -> None:
    respx.get("https://api.comeet.co/positions?status=open&limit=500").mock(
        return_value=httpx.Response(
            200,
            json={
                "positions": [{"uid": "P1", "name": "Backend"}],
                "next_page": "https://api.comeet.co/positions?cursor=2",
            },
        )
    )
    respx.get("https://api.comeet.co/positions?cursor=2").mock(
        return_value=httpx.Response(200, json={"positions": [{"uid": "P2", "name": "PM"}]})
    )
    with ComeetClient() as c:
        out = c.list_open_positions()
    assert [p["uid"] for p in out] == ["P1", "P2"]


@respx.mock
def test_get_candidate_returns_none_on_404() -> None:
    respx.get("https://api.comeet.co/candidates/missing").mock(
        return_value=httpx.Response(404, text="not found")
    )
    with ComeetClient() as c:
        assert c.get_candidate("missing") is None


@respx.mock
def test_bandwidth_response_raises_after_retries() -> None:
    respx.get("https://api.comeet.co/positions/P1").mock(
        return_value=httpx.Response(429, text="Bandwidth quota exceeded")
    )
    with ComeetClient() as c, pytest.raises(ComeetBandwidthError):
        c.get_position("P1")


@respx.mock
def test_transient_502_eventually_succeeds() -> None:
    route = respx.get("https://api.comeet.co/candidates/abc")
    route.side_effect = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json={"uid": "abc"}),
    ]
    with ComeetClient() as c:
        out = c.get_candidate("abc")
    assert out and out["uid"] == "abc"


def test_candidate_active_for_screening_excludes_terminal_statuses(monkeypatch) -> None:
    monkeypatch.setattr(settings, "excluded_recruiting_statuses", "Rejected,Withdrawn,Hired")
    assert candidate_active_for_screening({"status": "In progress"}) is True
    assert candidate_active_for_screening({"status": "Rejected"}) is False
    assert candidate_active_for_screening({"status": ""}) is True  # blank → defer to profile


def test_candidate_in_allowed_step_matches_substring(monkeypatch) -> None:
    monkeypatch.setattr(settings, "screener_step_substrings", "cv screen / recruiter")
    cand = {"current_steps": [{"name": "CV Screen / Recruiter", "type": ""}]}
    assert candidate_in_allowed_step(cand) is True
    assert candidate_in_allowed_step({"current_steps": [{"name": "Interview"}]}) is False


def test_candidate_max_activity_picks_latest() -> None:
    out = candidate_max_activity_iso({
        "time_created": "2026-04-01T10:00:00Z",
        "time_last_status_changed": "2026-04-15T10:00:00Z",
    })
    assert out is not None and out.year == 2026 and out.month == 4 and out.day == 15


def test_candidate_full_name_handles_missing_parts() -> None:
    assert candidate_full_name({"first_name": "Yael", "last_name": "Maor"}) == "Yael Maor"
    assert candidate_full_name({"first_name": "", "last_name": "Maor"}) == "Maor"
    assert candidate_full_name({}) == ""


def test_position_country_falls_back_to_address_segments() -> None:
    assert position_country({"location": {"country": "US"}}) == "United States"
    assert position_country({"location": {"name": "Tel Aviv, Israel"}}) == "Israel"


def test_position_jd_text_contains_position_name_and_block() -> None:
    pos = {
        "name": "Senior Backend Engineer",
        "department": "Platform",
        "details": [{"name": "Requirements", "value": "<p>5+ years backend</p>"}],
    }
    text = position_jd_text(pos)
    assert "Senior Backend Engineer" in text
    assert "Platform" in text
    assert "5+ years backend" in text
    assert "<p>" not in text
