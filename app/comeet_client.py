"""Public Comeet Recruit API client (api.comeet.co, JWT auth).

Port of the JWT + UrlFetch helpers from `Code.gs`. Same endpoints, same retry
semantics — translated to httpx. The internal `app.comeet.co` API lives in
`comeet_app_client.py`; this module is purely for the public Recruit API.

Endpoints used:
    GET  /positions?status=open&limit=N         — list open positions
    GET  /positions/{position_uid}              — position detail
    GET  /positions/{position_uid}/candidates   — candidates on a position
    GET  /candidates/{candidate_uid}            — candidate detail
    GET  /sourcing/candidates/find_duplicates   — past hiring processes lookup

Auth: HS256-signed JWT, valid 10 minutes per request batch. We mint a fresh
token per ComeetClient instance on construction; callers that span > 10 min
should call `refresh_token()` between batches.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx
import jwt
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings

log = logging.getLogger(__name__)


class ComeetError(RuntimeError):
    """Non-2xx response from api.comeet.co. Carries status + body for debugging."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ComeetBandwidthError(ComeetError):
    """429 / 'Bandwidth quota exceeded'. Caller should back off."""


class ComeetTransientError(ComeetError):
    """5xx / 302 splash / network blip. Caller should retry."""


def _is_transient_status(code: int) -> bool:
    return code in (302, 502, 503, 504)


def _is_bandwidth_response(code: int, body_text: str) -> bool:
    if code == 429:
        return True
    lower = (body_text or "").lower()
    return "bandwidth quota" in lower or "rate of data transfer" in lower


# ─── JWT minting ─────────────────────────────────────────────────────────────
def mint_token(api_key: str, api_secret: str, *, ttl_seconds: int = 600) -> str:
    """Mint an HS256 JWT for api.comeet.co. Same algorithm as Code.gs."""
    now = int(time.time())
    payload = {"iss": api_key, "exp": now + ttl_seconds}
    return jwt.encode(payload, api_secret, algorithm="HS256")


# ─── Client ──────────────────────────────────────────────────────────────────
class ComeetClient:
    """Thin async-ready wrapper. We use sync httpx because the screener is
    not heavily concurrent — clarity beats async-everywhere here."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.comeet_api_key
        self.api_secret = api_secret or settings.comeet_api_secret
        self.base_url = (base_url or settings.comeet_base_url).rstrip("/")
        if not self.api_key or not self.api_secret:
            raise ComeetError("COMEET_API_KEY / COMEET_API_SECRET not set")
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._mint_if_needed()

    def __enter__(self) -> ComeetClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- token lifecycle ---------------------------------------------------
    def _mint_if_needed(self) -> None:
        now = time.time()
        # Refresh 60s before expiry to avoid edge races.
        if not self._token or now > (self._token_expires_at - 60):
            self._token = mint_token(self.api_key, self.api_secret, ttl_seconds=600)
            self._token_expires_at = now + 600
            log.debug("comeet: minted fresh token (expires in 10 min)")

    def refresh_token(self) -> None:
        self._token = ""
        self._mint_if_needed()

    # -- low-level request -------------------------------------------------
    @retry(
        retry=retry_if_exception_type((ComeetTransientError, ComeetBandwidthError, httpx.RequestError)),
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> httpx.Response:
        self._mint_if_needed()
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"
        log.debug("COMEET %s %s", method, path)
        try:
            resp = self._client.request(method, url, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("comeet network error %s %s: %s", method, path, exc)
            raise

        code = resp.status_code
        body = resp.text or ""
        if _is_bandwidth_response(code, body):
            raise ComeetBandwidthError(
                f"{method} {path} bandwidth quota / 429",
                status=code, body=body[:300],
            )
        if _is_transient_status(code):
            raise ComeetTransientError(
                f"{method} {path} transient {code}",
                status=code, body=body[:300],
            )
        if code == 401:
            # The JWT may have raced out; refresh and retry once via tenacity.
            log.info("comeet 401 — refreshing token")
            self.refresh_token()
            raise ComeetTransientError(f"{method} {path} 401 refresh", status=401, body=body[:300])
        if code >= 400:
            raise ComeetError(
                f"{method} {path} -> {code}: {body[:300]}",
                status=code, body=body[:300],
            )
        return resp

    # -- high-level methods ------------------------------------------------
    def list_open_positions(self) -> list[dict[str, Any]]:
        """Walk all pages of `/positions?status=open`. Returns raw position dicts."""
        out: list[dict[str, Any]] = []
        url: str | None = "/positions?status=open&limit=500"
        while url:
            resp = self._request("GET", url) if url.startswith("/") else self._request("GET", url)
            data = resp.json()
            positions = data.get("positions", []) if isinstance(data, dict) else []
            out.extend(p for p in positions if p and p.get("uid"))
            next_page = data.get("next_page") if isinstance(data, dict) else None
            url = next_page if next_page else None
        log.info("comeet: %d open positions", len(out))
        return out

    def get_position(self, position_uid: str) -> dict[str, Any] | None:
        try:
            resp = self._request("GET", f"/positions/{position_uid}")
        except ComeetError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.json() if resp.content else None

    def get_candidate(self, candidate_uid: str) -> dict[str, Any] | None:
        try:
            resp = self._request("GET", f"/candidates/{candidate_uid}")
        except ComeetError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.json() if resp.content else None

    def list_candidates_for_position(self, position_uid: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = f"/positions/{position_uid}/candidates?limit=1000"
        while url:
            resp = self._request("GET", url)
            data = resp.json()
            page = data.get("candidates", []) if isinstance(data, dict) else []
            out.extend(page)
            next_page = data.get("next_page") if isinstance(data, dict) else None
            url = next_page if next_page else None
        return out

    def find_duplicates(self, *, email: str = "", first_name: str = "", last_name: str = "",
                        linkedin_url: str = "", phone_number: str = "") -> list[dict[str, Any]]:
        """`/sourcing/candidates/find_duplicates` — used to surface past hiring processes."""
        params: list[tuple[str, str]] = []
        for key, value in (
            ("email", email),
            ("first_name", first_name),
            ("last_name", last_name),
            ("linkedin_url", linkedin_url),
            ("phone_number", phone_number),
        ):
            v = (value or "").strip()
            if v:
                params.append((key, v))
        if not params:
            return []
        try:
            resp = self._request("GET", "/sourcing/candidates/find_duplicates", params=params)
        except ComeetError as exc:
            if exc.status == 400:
                return []
            if exc.status in (401, 403):
                # Permissions issue — caller decides whether to ignore.
                raise
            raise
        data = resp.json()
        return data if isinstance(data, list) else []


# ─── Helpers ported from Code.gs ─────────────────────────────────────────────
def candidate_active_for_screening(candidate: dict[str, Any]) -> bool:
    """False when recruiting status is one of the excluded values (Rejected etc.)."""
    excluded = settings.excluded_statuses_list
    if not excluded:
        return True
    status = (candidate.get("status") or "").strip().lower()
    if not status:
        return True  # blank status: let the profile fetch decide
    return status not in excluded


def candidate_in_allowed_step(candidate: dict[str, Any]) -> bool:
    """True if any current step name/type substring-matches the configured patterns."""
    steps = candidate.get("current_steps") or []
    if not steps:
        return False
    patterns = settings.step_substrings_list
    if patterns == ["*"]:
        return True
    for step in steps:
        haystack = f"{step.get('name', '')} {step.get('type', '')}".lower()
        for pattern in patterns:
            if pattern in haystack:
                return True
    return False


def candidate_max_activity_iso(candidate: dict[str, Any]) -> datetime | None:
    """Latest of time_created / time_last_status_changed — used as the cursor.

    Equivalent of `maxCandidateActivityTime_` in Code.gs.
    """
    candidates = []
    for key in ("time_created", "time_last_status_changed"):
        raw = candidate.get(key)
        if not raw:
            continue
        try:
            # Comeet timestamps are ISO 8601 with trailing Z (UTC).
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            candidates.append(ts)
        except (ValueError, TypeError):
            pass
    return max(candidates) if candidates else None


def candidate_full_name(candidate: dict[str, Any]) -> str:
    parts = [
        (candidate.get("first_name") or "").strip(),
        (candidate.get("last_name") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def position_country(position: dict[str, Any]) -> str:
    """Mirrors `formatPositionCountry_` — country-level bucket for UI filters."""
    loc = position.get("location") or {}
    raw = (loc.get("country") or "").strip()
    if not raw:
        full = position_full_location(position)
        if not full:
            return ""
        segments = [seg.strip() for seg in full.split(",") if seg.strip()]
        raw = segments[-1] if segments else ""
    return _expand_country_display(raw)


def position_full_location(position: dict[str, Any]) -> str:
    loc = position.get("location") or {}
    if loc.get("name"):
        return str(loc["name"])
    parts = [loc.get("city"), loc.get("state"), loc.get("country")]
    return ", ".join(p for p in parts if p)


def position_lead_recruiter(position: dict[str, Any]) -> str:
    users = (position.get("users") or {}).get("lead_recruiter") or {}
    parts = [
        (users.get("first_name") or "").strip(),
        (users.get("last_name") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


_COUNTRY_EXPANSIONS = {
    # Israel — country codes and common cities
    "IL": "Israel",
    "ISR": "Israel",
    "ISRAEL": "Israel",
    "TELAVIV": "Israel",
    "TELAVIVYAFO": "Israel",
    "TLV": "Israel",
    "JERUSALEM": "Israel",
    "HAIFA": "Israel",
    "HERZLIYA": "Israel",
    "HERTZLIYA": "Israel",
    "RAMATGAN": "Israel",
    "BEERSHEVA": "Israel",
    "BEERSHEBA": "Israel",
    "REHOVOT": "Israel",
    "NETANYA": "Israel",
    "RAANANA": "Israel",
    "PETACHTIKVA": "Israel",
    "RISHONLEZION": "Israel",
    # United States
    "US": "United States",
    "USA": "United States",
    "UNITEDSTATES": "United States",
    "AMERICA": "United States",
    "NEWYORK": "United States",
    "NYC": "United States",
    "NEWYORKNY": "United States",
    "SANFRANCISCO": "United States",
    "SF": "United States",
    "LOSANGELES": "United States",
    "LA": "United States",
    "AUSTIN": "United States",
    "BOSTON": "United States",
    "CHICAGO": "United States",
    "SEATTLE": "United States",
    "MIAMI": "United States",
    "REMOTEUS": "United States",
    # Canada
    "CA": "Canada",       # note: this also matches California, but Comeet usually
    "CAN": "Canada",      #       writes "California" in full so we accept the risk
    "CANADA": "Canada",
    "TORONTO": "Canada",
    "MONTREAL": "Canada",
    "VANCOUVER": "Canada",
    "OTTAWA": "Canada",
    "CALGARY": "Canada",
    # United Kingdom
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "UNITEDKINGDOM": "United Kingdom",
    "LONDON": "United Kingdom",
    "MANCHESTER": "United Kingdom",
    # United Arab Emirates
    "UAE": "United Arab Emirates",
    "DUBAI": "United Arab Emirates",
    "ABUDHABI": "United Arab Emirates",
    # Germany
    "DE": "Germany",
    "DEU": "Germany",
    "GERMANY": "Germany",
    "BERLIN": "Germany",
    "MUNICH": "Germany",
    # France
    "FR": "France",
    "FRA": "France",
    "FRANCE": "France",
    "PARIS": "France",
    # India
    "IN": "India",
    "IND": "India",
    "INDIA": "India",
    "BANGALORE": "India",
    "BENGALURU": "India",
    "MUMBAI": "India",
    "HYDERABAD": "India",
    "PUNE": "India",
    "NEWDELHI": "India",
}


def _expand_country_display(raw: str) -> str:
    import re as _re
    s = (raw or "").strip()
    if not s:
        return ""
    # Strip everything but letters and uppercase, so "Tel-Aviv", "Tel Aviv",
    # "TEL.AVIV", "Tel_Aviv" all collapse to "TELAVIV".
    compact = _re.sub(r"[^A-Za-z]", "", s).upper()
    if compact in _COUNTRY_EXPANSIONS:
        return _COUNTRY_EXPANSIONS[compact]
    if len(s) == 2 and s.isalpha():
        try:
            from babel import Locale
            return Locale("en").territories.get(s.upper(), s)
        except ImportError:
            pass
    return s


_RECRUITER_NOTE_TOKENS = ("note", "internal", "recruiter", "comment", "memo", "extra")


def _looks_like_recruiter_note_block(name: str) -> bool:
    """Heuristic: does this details[] block hold recruiter-added notes
    rather than formal JD prose (description, requirements, etc.)?"""
    if not name:
        return False
    n = name.lower()
    return any(tok in n for tok in _RECRUITER_NOTE_TOKENS)


def _strip_html(value: str) -> str:
    import re
    # Strip HTML tags — same as Code.gs's `replace(/<[^>]+>/g, ' ')`.
    return re.sub(r"<[^>]+>", " ", value).strip()


def position_jd_text(position: dict[str, Any]) -> str:
    """Equivalent of `buildPositionJdText_` — prose JD passed to Claude.

    Returns only the "formal JD" pieces: name/department/location/level + any
    details[] block whose name doesn't look like a recruiter note. The recruiter
    notes are surfaced separately via `position_recruiter_notes()` so the prompt
    can weight them distinctly.
    """
    lines: list[str] = []
    lines.append(f"Position: {position.get('name') or ''}")
    if position.get("department"):
        lines.append(f"Department: {position['department']}")
    if (loc := (position.get("location") or {}).get("name")):
        lines.append(f"Location: {loc}")
    if position.get("experience_level"):
        lines.append(f"Experience level: {position['experience_level']}")
    if position.get("employment_type"):
        lines.append(f"Employment: {position['employment_type']}")
    for block in position.get("details") or []:
        if not block or not block.get("name"):
            continue
        if _looks_like_recruiter_note_block(block["name"]):
            continue  # surfaced separately via position_recruiter_notes
        value = _strip_html(block.get("value") or "")
        if not value:
            continue
        lines.append("")
        lines.append(f"--- {block['name']} ---")
        lines.append(value)
    text = "\n".join(lines).strip()
    return text or "No description provided; infer from title and department only."


def position_recruiter_notes(position: dict[str, Any]) -> str:
    """Pull out the recruiter-added 'Notes/Internal/etc' blocks from
    position.details[]. Returned as a single labeled text section so the
    scoring prompt can call them out distinctly from the formal JD.

    Returns "" when no such blocks exist.
    """
    parts: list[str] = []
    for block in position.get("details") or []:
        if not block or not block.get("name"):
            continue
        if not _looks_like_recruiter_note_block(block["name"]):
            continue
        value = _strip_html(block.get("value") or "")
        if not value:
            continue
        parts.append(f"--- {block['name']} ---")
        parts.append(value)
    if not parts:
        return ""
    return (
        "[POSITION-LEVEL RECRUITER NOTES — the hiring manager / recruiter added "
        "these to the role itself. Treat them as overrides on the formal JD when "
        "they conflict.]\n"
        + "\n".join(parts)
    )


__all__ = [
    "ComeetClient",
    "ComeetError",
    "ComeetBandwidthError",
    "ComeetTransientError",
    "candidate_active_for_screening",
    "candidate_in_allowed_step",
    "candidate_max_activity_iso",
    "candidate_full_name",
    "position_country",
    "position_full_location",
    "position_lead_recruiter",
    "position_jd_text",
    "mint_token",
]
