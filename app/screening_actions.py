"""CV-screen decisions written to Comeet for the Riverside Screening plugin.

The plugin runs in each recruiter's own Claude; the sourcing gateway authenticates
them and calls /api/screening/* here. Writes go through the recruiter-app session
(ComeetAppClient, logged in as COMEET_APP_EMAIL = comeetadmin, "Yoni Admin (Bot)").

Payloads were decoded from recruiter-app HAR captures (2026-10-20):
  pass     POST /api/v1/candidates/{num}/steps/{step}/submit?parent_id={num}
           {"proceed": true, "further_review": ""}
  reject   same call, {"proceed": false, "further_review": "", "disposition_type_id": <id>}
           Sends NO email: the candidate moves on to a "send notification" step that
           waits for a person to send or skip it. Nothing here touches that step.
  comment  POST /api/v1/sendmessages  [{"action": 2, ...}]
           action 2 is an internal comment. Other actions send email, so never change it.
  reasons  GET /api/v1/companies/{company}/disposition-types?parent_id={company}
           status 1 = rejected by the company, status 2 = withdrawn by the candidate.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time
from typing import Any

from .comeet_app_client import ComeetAppClient, ComeetAppError
from .config import settings

log = logging.getLogger(__name__)

COMEET_COMPANY_ID = 1641          # Riverside's company id in the recruiter app
REJECTED_BY_COMPANY = 1           # disposition-type status for company-side rejections
CV_TEXT_LIMIT = 25_000            # characters handed to the plugin per CV

_lock = threading.Lock()
_app: ComeetAppClient | None = None
_bot_user_id: int | None = None


class ScreeningError(RuntimeError):
    """A decision that must not be written (wrong step, unknown reason, ...)."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def app_client() -> ComeetAppClient:
    """One recruiter-app client per process, so the cached session is reused."""
    global _app
    with _lock:
        if _app is None:
            _app = ComeetAppClient()
        return _app


def _body(resp: tuple[int, Any]) -> Any:
    return resp[1]


# ─── Reasons ─────────────────────────────────────────────────────────────────
_reasons_cache: tuple[float, list[dict[str, Any]]] | None = None


def reject_reasons(app: ComeetAppClient | None = None) -> list[dict[str, Any]]:
    """Company-side rejection reasons, in Comeet's order. Cached for 10 minutes."""
    global _reasons_cache
    if _reasons_cache and time.time() - _reasons_cache[0] < 600:
        return _reasons_cache[1]
    app = app or app_client()
    path = f"/api/v1/companies/{COMEET_COMPANY_ID}/disposition-types?parent_id={COMEET_COMPANY_ID}"
    items = _body(app._request("GET", path))
    if isinstance(items, dict):
        items = items.get("results") or []
    out = [
        {"id": int(x["id"]), "reason": str(x.get("reason") or "").strip(), "order": x.get("order") or 0}
        for x in (items or [])
        if isinstance(x, dict) and x.get("id") and x.get("status") == REJECTED_BY_COMPANY
    ]
    out.sort(key=lambda x: (x["order"], x["reason"].lower()))
    reasons = [{"id": x["id"], "reason": x["reason"]} for x in out]
    _reasons_cache = (time.time(), reasons)
    return reasons


# ─── Candidate + step ────────────────────────────────────────────────────────
def numeric_id_from_url(url: str | None) -> int | None:
    m = re.search(r"/can/(\d+)", url or "")
    return int(m.group(1)) if m else None


def resolve_numeric_id(candidate_uid: str, app: ComeetAppClient | None = None) -> int:
    app = app or app_client()
    body = app.get_candidate(candidate_uid)
    num = body.get("id") if isinstance(body, dict) else None
    if not num:
        raise ScreeningError(f"couldn't find candidate {candidate_uid} in Comeet", status=404)
    return int(num)


def _matches_cv_screen(step: dict[str, Any]) -> bool:
    patterns = settings.step_substrings_list
    name = str(step.get("name") or "").lower()
    return patterns == ["*"] or any(p in name for p in patterns)


def current_cv_screen_step(numeric_id: int, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """The candidate's current step, which must be the CV screen and not yet decided.

    The current step is the first one that isn't completed. Anything else (already
    decided, or sitting further along) is refused so a decision is never written twice
    or on the wrong step.
    """
    app = app or app_client()
    steps = _body(app._request("GET", f"/api/v1/candidates/{numeric_id}/steps"))
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    current = next((s for s in steps if not s.get("is_completed")), None)
    if current is None:
        raise ScreeningError("this candidate has no open step (the process is finished)")
    if not _matches_cv_screen(current):
        raise ScreeningError(f"this candidate is no longer at CV screen (now at \"{current.get('name')}\")")
    return current


# ─── Writes ──────────────────────────────────────────────────────────────────
def submit_decision(numeric_id: int, step_id: int, decision: str, reason_id: int | None,
                    app: ComeetAppClient | None = None) -> dict[str, Any]:
    app = app or app_client()
    if decision == "pass":
        body: dict[str, Any] = {"proceed": True, "further_review": ""}
    elif decision == "reject":
        valid = {r["id"] for r in reject_reasons(app)}
        if reason_id not in valid:
            raise ScreeningError("pick one of Comeet's rejection reasons", status=400)
        body = {"proceed": False, "further_review": "", "disposition_type_id": reason_id}
    else:
        raise ScreeningError("decision must be 'pass' or 'reject'", status=400)
    path = f"/api/v1/candidates/{numeric_id}/steps/{step_id}/submit?parent_id={numeric_id}"
    resp = _body(app._request("POST", path, json_body=body))
    return resp if isinstance(resp, dict) else {}


def bot_user_id(app: ComeetAppClient | None = None) -> int:
    """Comeet user id of the account the app is logged in as (comment author)."""
    global _bot_user_id
    if _bot_user_id:
        return _bot_user_id
    app = app or app_client()
    users = _body(app._request("GET", "/api/v1/users")) or []
    me = (settings.comeet_app_email or "").strip().lower()
    for u in users:
        if isinstance(u, dict) and str(u.get("email") or "").strip().lower() == me:
            _bot_user_id = int(u["id"])
            return _bot_user_id
    raise ScreeningError("couldn't find the bot's own Comeet user", status=500)


def post_comment(numeric_id: int, text: str, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """Internal comment on the candidate (action 2: never an email)."""
    app = app or app_client()
    payload = [{
        "action": 2,
        "body": text,
        "files": [],
        "groups": [],
        "participants": [{"id": bot_user_id(app), "type": "user"}],
        "visibility": 1,
        "discussion_context": {"id": numeric_id, "type": "candidate"},
        "discussion_type": 1,
        "is_new": True,
    }]
    resp = _body(app._request("POST", "/api/v1/sendmessages", json_body=payload))
    first = resp[0] if isinstance(resp, list) and resp else resp
    return first if isinstance(first, dict) else {}


def comment_text(recruiter_name: str, decision: str, reason: str | None, note: str | None) -> str:
    verdict = "Passed CV screen" if decision == "pass" else f"Rejected at CV screen ({reason})" if reason else "Rejected at CV screen"
    text = f"{verdict}. Screened by {recruiter_name} with Riverside Screening."
    note = (note or "").strip()
    return f"{text}\n{note}" if note else text


# ─── CV text ─────────────────────────────────────────────────────────────────
def cv_text(resume_url: str | None) -> tuple[str, str]:
    """Readable CV text and a status: ok / none / unreadable."""
    if not resume_url:
        return "", "none"
    from .scan import _maybe_fetch_resume

    pdf_b64, text, failed = _maybe_fetch_resume(resume_url)
    if pdf_b64:
        text = _pdf_text(base64.b64decode(pdf_b64))
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        return "", "unreadable" if (failed or pdf_b64) else "none"
    return text[:CV_TEXT_LIMIT], "ok"


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:15])
    except Exception as exc:  # noqa: BLE001
        log.info("pdf text extraction failed: %s", exc)
        return ""


__all__ = [
    "ScreeningError", "app_client", "reject_reasons", "numeric_id_from_url", "resolve_numeric_id",
    "current_cv_screen_step", "submit_decision", "post_comment", "comment_text", "cv_text",
    "ComeetAppError",
]
