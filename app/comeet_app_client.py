"""Internal Comeet web-app client — app.comeet.co/api/v1, cookie session auth.

Adapted from referral-bot's comeet_app_client.py. Differences:
  - Session cache lives in Postgres (`comeet_app_session` table) instead of SQLite.
  - Adds the tagging endpoints (POST persontags, POST persons/{id}/tags, DELETE).
  - Adds candidate-UID → numeric person-ID resolution.

Auth (per ~3 days):
  1. GET https://app.comeet.co/  → mints csrftoken + Imperva cookies
  2. Solve reCAPTCHA via 2captcha
  3. POST /signin {email, password, grecaptcha_token}  → sets comeetsession

Per-request:
  All write methods send `x-csrftoken` + `Origin` + `Referer`. On 401 we drop the
  cached session and retry once with a fresh login.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .captcha import solve_recaptcha_for_login
from .config import settings
from .db import db_session
from .models import ComeetAppSession

log = logging.getLogger(__name__)

APP_BASE_URL = "https://app.comeet.co"
SIGNIN_PATH = "/signin"
LOGIN_PAGE_URL = f"{APP_BASE_URL}/"

# Sessions are observed at ~3 days; refresh proactively at 2.5d to avoid edge cases.
SESSION_LIFETIME = timedelta(days=2, hours=12)


class ComeetAppError(RuntimeError):
    """Non-success response from app.comeet.co."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ComeetAppAuthError(ComeetAppError):
    """401/403 — caller should drop the session and retry with a fresh login."""


# ─── Session persistence ─────────────────────────────────────────────────────
def load_cached_session() -> tuple[dict[str, str], str, datetime | None] | None:
    """Read the singleton session row from Postgres. Returns None if absent."""
    with db_session() as session:
        row = session.scalar(select(ComeetAppSession).where(ComeetAppSession.id == 1))
        if row is None:
            return None
        return dict(row.cookies_json or {}), row.csrf_token, row.expires_at


def save_session(cookies: dict[str, str], csrf_token: str, expires_at: datetime | None) -> None:
    """Upsert the singleton session row."""
    with db_session() as session:
        stmt = pg_insert(ComeetAppSession).values(
            id=1,
            cookies_json=cookies,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ComeetAppSession.id],
            set_={
                "cookies_json": stmt.excluded.cookies_json,
                "csrf_token": stmt.excluded.csrf_token,
                "expires_at": stmt.excluded.expires_at,
                "created_at": datetime.now(timezone.utc),
            },
        )
        session.execute(stmt)


def clear_session() -> None:
    with db_session() as session:
        session.query(ComeetAppSession).filter(ComeetAppSession.id == 1).delete()


# ─── Client ──────────────────────────────────────────────────────────────────
class ComeetAppClient:
    """Authenticated wrapper around app.comeet.co's internal API.

    Uses curl_cffi for Chrome TLS impersonation — Imperva inspects TLS
    fingerprints and rejects standard urllib/requests/httpx fingerprints.
    """

    def __init__(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        captcha_api_key: str | None = None,
        impersonate: str = "chrome124",
    ) -> None:
        self.email = email or settings.comeet_app_email
        self.password = password or settings.comeet_app_password
        self.captcha_api_key = captcha_api_key or settings.captcha_api_key
        self.impersonate = impersonate
        self._cookies: dict[str, str] = {}
        self._csrf_token: str = ""
        self._expires_at: datetime | None = None

    # -- public diagnostics ------------------------------------------------
    @property
    def has_session(self) -> bool:
        return bool(self._cookies and self._csrf_token)

    def session_summary(self) -> dict[str, Any]:
        if not self.has_session:
            return {"present": False}
        return {
            "present": True,
            "csrf_prefix": self._csrf_token[:8] + "…",
            "cookie_names": list(self._cookies.keys()),
            "cookies_redacted": {k: f"{v[:6]}…(len={len(v)})" for k, v in self._cookies.items()},
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
        }

    # -- session lifecycle -------------------------------------------------
    def adopt_cached_session(self) -> bool:
        """Restore the most recent session from Postgres. Returns True on success."""
        cached = load_cached_session()
        if not cached:
            return False
        cookies, csrf, expires_at = cached
        if expires_at and expires_at <= datetime.now(timezone.utc):
            log.info("comeet-app: cached session past expiry; ignoring")
            return False
        self._cookies = cookies
        self._csrf_token = csrf
        self._expires_at = expires_at
        log.info(
            "comeet-app: adopted cached session (cookies=%d, csrf_len=%d, expires=%s)",
            len(cookies), len(csrf), expires_at.isoformat() if expires_at else "?",
        )
        return True

    def drop_session(self) -> None:
        self._cookies = {}
        self._csrf_token = ""
        self._expires_at = None
        clear_session()

    def _ensure_session(self) -> None:
        if not self.has_session and not self.adopt_cached_session():
            self.login()
        elif self._expires_at and self._expires_at <= datetime.now(timezone.utc):
            log.info("comeet-app: session past expiry; re-login")
            self.login()

    def login(self) -> None:
        """Run the full GET / → 2captcha → POST /signin flow."""
        if not self.email or not self.password:
            raise ComeetAppError("COMEET_APP_EMAIL / COMEET_APP_PASSWORD not configured")
        if not self.captcha_api_key:
            raise ComeetAppError("CAPTCHA_API_KEY not configured")

        # Lazy import — curl_cffi adds ~30 MB to the image; only load when actually logging in.
        from curl_cffi import requests as curl_requests

        sess = curl_requests.Session()
        log.info("comeet-app login step 1: GET %s", LOGIN_PAGE_URL)
        try:
            r = sess.get(
                LOGIN_PAGE_URL,
                impersonate=self.impersonate,
                timeout=30,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9",
                },
            )
            log.info(
                "comeet-app GET / -> %d (cookies_set=%d)",
                r.status_code, len(sess.cookies.get_dict()),
            )
        except Exception as exc:
            raise ComeetAppError(f"login GET / failed: {exc}") from exc

        initial_cookies = sess.cookies.get_dict()
        csrf = initial_cookies.get("csrftoken", "")
        if not csrf:
            log.warning(
                "comeet-app: no csrftoken cookie after initial GET; cookies=%s",
                list(initial_cookies.keys()),
            )

        log.info("comeet-app login step 2: solving reCAPTCHA…")
        token = solve_recaptcha_for_login(
            api_key=self.captcha_api_key,
            site_key=settings.recaptcha_site_key,
            page_url=LOGIN_PAGE_URL,
        )

        log.info("comeet-app login step 3: POST %s%s", APP_BASE_URL, SIGNIN_PATH)
        signin_body = {"email": self.email, "password": self.password, "grecaptcha_token": token}
        signin_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json;charset=UTF-8",
            "origin": APP_BASE_URL,
            "referer": LOGIN_PAGE_URL,
            "x-csrftoken": csrf,
        }
        try:
            resp = sess.post(
                f"{APP_BASE_URL}{SIGNIN_PATH}",
                json=signin_body,
                headers=signin_headers,
                impersonate=self.impersonate,
                timeout=45,
            )
        except Exception as exc:
            raise ComeetAppError(f"login POST /signin failed: {exc}") from exc

        if resp.status_code != 200:
            body_preview = (resp.text or "")[:300]
            raise ComeetAppError(
                f"/signin returned {resp.status_code}: {body_preview}",
                status=resp.status_code,
                body=body_preview,
            )

        final_cookies = sess.cookies.get_dict()
        if "comeetsession" not in final_cookies:
            log.warning(
                "comeet-app /signin 200 but comeetsession missing; cookies=%s",
                list(final_cookies.keys()),
            )
            raise ComeetAppError(
                "/signin 200 but comeetsession cookie not set",
                status=200, body=(resp.text or "")[:300],
            )

        self._cookies = final_cookies
        self._csrf_token = final_cookies.get("csrftoken", csrf)
        self._expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME

        save_session(self._cookies, self._csrf_token, self._expires_at)
        log.info(
            "comeet-app login success; cookies=%d csrf_len=%d expires=%s",
            len(self._cookies), len(self._csrf_token), self._expires_at.isoformat(),
        )

    # -- generic request ---------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: int = 30,
        _retried: bool = False,
    ) -> tuple[int, Any]:
        from curl_cffi import requests as curl_requests

        self._ensure_session()
        url = f"{APP_BASE_URL}{path}"

        sess = curl_requests.Session()
        for k, v in self._cookies.items():
            sess.cookies.set(k, v, domain=".comeet.co")

        headers: dict[str, str] = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "origin": APP_BASE_URL,
            "referer": f"{APP_BASE_URL}/app/",
            "x-csrftoken": self._csrf_token,
        }
        if method.upper() in ("POST", "PUT", "PATCH"):
            headers["content-type"] = "application/json;charset=UTF-8"

        log.info("comeet-app %s %s", method.upper(), path)
        try:
            resp = sess.request(
                method.upper(), url,
                json=json_body,
                headers=headers,
                impersonate=self.impersonate,
                timeout=timeout,
            )
        except Exception as exc:
            raise ComeetAppError(f"{method} {path} network error: {exc}") from exc

        log.info(
            "comeet-app %s %s -> %d (body_len=%d)",
            method.upper(), path, resp.status_code, len(resp.text or ""),
        )

        if resp.status_code in (401, 403):
            if _retried:
                raise ComeetAppAuthError(
                    f"{method} {path} -> {resp.status_code} after re-login retry",
                    status=resp.status_code, body=(resp.text or "")[:300],
                )
            log.info("comeet-app: auth-failed, dropping session and retrying once")
            self.drop_session()
            return self._request(method, path, json_body=json_body, timeout=timeout, _retried=True)

        if resp.status_code == 204:
            return resp.status_code, None

        if resp.status_code >= 400:
            raise ComeetAppError(
                f"{method} {path} -> {resp.status_code}: {(resp.text or '')[:300]}",
                status=resp.status_code, body=(resp.text or "")[:300],
            )

        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, resp.text

    # -- candidate / person resolution -------------------------------------
    def get_candidate(self, candidate_uid_or_id: str | int) -> dict[str, Any]:
        """GET /api/v1/candidates/{uid}. Accepts either alphanumeric UID or numeric ID."""
        _, body = self._request("GET", f"/api/v1/candidates/{candidate_uid_or_id}")
        if not isinstance(body, dict):
            raise ComeetAppError(f"candidates/{candidate_uid_or_id} non-dict: {body!r}")
        return body

    def get_candidate_v2(self, candidate_uid_or_id: str | int) -> dict[str, Any]:
        _, body = self._request("GET", f"/api/v2/candidates/{candidate_uid_or_id}")
        if not isinstance(body, dict):
            raise ComeetAppError(f"v2/candidates/{candidate_uid_or_id} non-dict: {body!r}")
        return body

    def resolve_person_id(self, candidate_uid: str) -> int | None:
        """Resolve an alphanumeric candidate UID to the numeric person ID used by tagging.

        Strategy:
          1. GET /api/v1/candidates/{uid}; look for a person.id-shaped field.
          2. Fall back to v2 endpoint which has a richer payload.
        """
        for fetcher in (self.get_candidate, self.get_candidate_v2):
            try:
                payload = fetcher(candidate_uid)
            except ComeetAppError as exc:
                log.debug("resolve_person_id %s via %s: %s", candidate_uid, fetcher.__name__, exc)
                continue
            person_id = _extract_person_id(payload)
            if person_id is not None:
                return person_id
        log.warning("resolve_person_id: could not extract person_id for candidate %s", candidate_uid)
        return None

    # -- tag CRUD ----------------------------------------------------------
    def list_persontags(self) -> list[dict[str, Any]]:
        _, body = self._request("GET", "/api/v1/persontags")
        return body if isinstance(body, list) else []

    def create_persontag(self, name: str, *, color: str | None = None) -> dict[str, Any]:
        """POST /api/v1/persontags. Returns the created tag dict (with id).

        `color` is a Comeet palette token (e.g. "green", "darkOrange", "red");
        omit/None for the default uncolored tag.
        """
        body: dict[str, Any] = {"name": name, "is_new_tag": True}
        if color:
            body["color"] = color
        _, resp = self._request("POST", "/api/v1/persontags", json_body=body)
        if not isinstance(resp, dict) or "id" not in resp:
            raise ComeetAppError(f"create_persontag unexpected response: {resp!r}")
        return resp

    def update_persontag_color(self, tag_id: int, color: str | None) -> dict[str, Any]:
        """PATCH the color of an existing tag. Pass None to clear."""
        _, body = self._request(
            "PATCH", f"/api/v1/persontags/{tag_id}",
            json_body={"color": color},
        )
        if not isinstance(body, dict):
            raise ComeetAppError(f"update_persontag_color unexpected response: {body!r}")
        return body

    def get_or_create_persontag(self, name: str, *, color: str | None = None) -> dict[str, Any]:
        """Idempotent: find a tag by exact name; create with color if absent.

        If the tag already exists but has no color (or a different color) and
        `color` was requested, we PATCH it to match — useful when migrating
        existing AI: tags to the new colored scheme.
        """
        target = name.strip()
        for tag in self.list_persontags():
            if not isinstance(tag, dict) or (tag.get("name") or "").strip() != target:
                continue
            if color and (tag.get("color") or "") != color:
                try:
                    return self.update_persontag_color(int(tag["id"]), color)
                except ComeetAppError as exc:
                    log.info("could not update color on tag %s: %s", target, exc)
            return tag
        return self.create_persontag(target, color=color)

    def assign_tag_to_person(self, person_id: int, tag_id: int) -> dict[str, Any]:
        """POST /api/v1/persons/{person_id}/tags  body={tag_id}"""
        _, body = self._request(
            "POST", f"/api/v1/persons/{person_id}/tags",
            json_body={"tag_id": tag_id},
        )
        if not isinstance(body, dict):
            raise ComeetAppError(f"assign_tag unexpected response: {body!r}")
        return body

    def remove_tag_from_person(self, person_id: int, tag_id: int) -> bool:
        """DELETE /api/v1/persons/{person_id}/tags/{tag_id}. Returns True on 204."""
        status, _ = self._request("DELETE", f"/api/v1/persons/{person_id}/tags/{tag_id}")
        return status == 204


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _extract_person_id(payload: Any) -> int | None:
    """Best-effort search for a numeric person identifier in a candidate payload.

    Observed Comeet shape (api/v1/candidates/{id}):
        { ..., "person": 57144261, "person_uid": "C3.F7635", ... }

    where `person` is the numeric ID we need for /persons/{id}/tags. We also
    handle older shapes: `person` as a nested object containing `.id`, plus
    top-level `person_id`. `person_uid` is alphanumeric and not what tagging
    expects, so we never use it for the numeric ID.
    """
    if not isinstance(payload, dict):
        return None

    # Modern shape: top-level numeric "person" field.
    person_value = payload.get("person")
    if person_value is not None and not isinstance(person_value, dict):
        converted = _coerce_int(person_value)
        if converted is not None:
            return converted

    # Older shape: nested {"person": {"id": N, ...}}.
    if isinstance(person_value, dict):
        for key in ("id", "person_id", "uid"):
            converted = _coerce_int(person_value.get(key))
            if converted is not None:
                return converted

    # Direct top-level person_id (rare).
    return _coerce_int(payload.get("person_id"))


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


__all__ = [
    "ComeetAppClient",
    "ComeetAppError",
    "ComeetAppAuthError",
    "load_cached_session",
    "save_session",
    "clear_session",
]
