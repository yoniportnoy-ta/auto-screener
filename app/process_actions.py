"""Working with any existing Comeet lead or candidate, for the RiverTalent plugin.

Same path as screening (gateway → here → recruiter-app session as the bot). Decoded from
Yoni's recording of a test lead (2026-10-21):
  lead step      PATCH /api/v1/candidates/{num}/outreach-status  {"lead_details": {"outreach_status": N}}
                 N: 1 Not Contacted, 2 Contacted, 3 Responded
  not interested PATCH /api/v1/candidates/{num}  {"status": 5, "disposition_type_id": <withdrawn reason>,
                 "step_ids_completed_by": []}   (reasons with disposition status 2)
  convert        POST /api/v1/candidates/{num}/lead_to_candidate
                 {"new_requisition_id": <req>, "source_id": <the lead's source>, "step_to_start_id": <req step>}
  process        GET /api/v2/candidates/{num}: process_type 2 = lead, 1 = candidate
Moving a candidate forward is only "pass the current step", and only on steps that need no
interview summary (step_type 1 review, 3 assignment): interviews and offers stay with people.
Comments are internal (never emails) and name the recruiter.
Availability (read; the recruiter app calls it just to show times):
  POST /api/v1/candidates/{num}/steps/{step}/interview-availability?parent_id={num}
       {"id": step, "start": <now Z>, "days": N, "parent_id": num, "meeting_room": [], "duration_minutes": M}
  -> each assignee's free slots (their Comeet working hours, notice, daily/weekly caps, minus Google busy)
     and availability_intersection (slots they all share), all in UTC.
Reminders use the envelope linkedin-to-comeet decoded from the recruiter UI's "Add reminder":
  POST /api/v1/sendmessages [{"action": 2, "event": 32, "discussion_type": 3, "body": <mention chip + text>,
       "participants": [<bot>, <recruiter>], "time_to_send_message": "...Z", ...}]
The recruiter's mention chip plus their id in participants is what makes Comeet notify them.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from . import screening_actions as sa
from .comeet_app_client import ComeetAppClient

LEAD, CANDIDATE = 2, 1
OUTREACH = {1: "Not Contacted", 2: "Contacted", 3: "Responded"}
OUTREACH_BY_NAME = {v.lower(): k for k, v in OUTREACH.items()}
WITHDRAWN_BY_CANDIDATE = 2
NOT_INTERESTED_STATUS = 5
PASSABLE_STEP_TYPES = {1, 3}          # review / approve, send assignment
# Comeet also gives an "Offer" step type 3 (seen on AI Agents, 2026-10-22): never passable from here.
NEVER_PASS = re.compile(r"(?i)\boffer")


def _passable(step: dict[str, Any]) -> bool:
    return step.get("step_type") in PASSABLE_STEP_TYPES and not NEVER_PASS.search(str(step.get("name") or ""))
STEP_TYPE_NAMES = {1: "review", 2: "interview", 3: "assignment", 5: "notification", 8: "offer"}


def _app(app: ComeetAppClient | None) -> ComeetAppClient:
    return app or sa.app_client()


def numeric_id(ref: str, app: ComeetAppClient | None = None) -> int:
    """A Comeet candidate link (…/can/12345), a numeric id, or an alphanumeric uid."""
    ref = (ref or "").strip()
    m = re.search(r"/can/(\d+)", ref) or re.fullmatch(r"(\d{5,})", ref)
    if m:
        return int(m.group(1))
    if not ref:
        raise sa.ScreeningError("pass a Comeet link, id or uid", status=400)
    return sa.resolve_numeric_id(ref, _app(app))


def _get(app: ComeetAppClient, path: str) -> Any:
    return sa._body(app._request("GET", path))


def withdrawal_reasons(app: ComeetAppClient | None = None) -> list[dict[str, Any]]:
    app = _app(app)
    items = _get(app, f"/api/v1/companies/{sa.COMEET_COMPANY_ID}/disposition-types?parent_id={sa.COMEET_COMPANY_ID}")
    if isinstance(items, dict):
        items = items.get("results") or []
    out = [{"id": int(x["id"]), "reason": str(x.get("reason") or "").strip(), "order": x.get("order") or 0}
           for x in (items or []) if isinstance(x, dict) and x.get("id") and x.get("status") == WITHDRAWN_BY_CANDIDATE]
    out.sort(key=lambda x: (x["order"], x["reason"].lower()))
    return [{"id": x["id"], "reason": x["reason"]} for x in out]


def position_steps(req_id: int, app: ComeetAppClient | None = None) -> list[dict[str, Any]]:
    steps = _get(_app(app), f"/api/v1/requisitions/{req_id}/steps?parent_id={req_id}") or []
    steps = [s for s in steps if isinstance(s, dict) and s.get("id")]
    steps.sort(key=lambda s: float(s.get("group_number") or 0))
    return [{"id": int(s["id"]), "name": s.get("name") or "", "type": STEP_TYPE_NAMES.get(s.get("step_type"), "other")}
            for s in steps]


def _who(u: Any) -> str:
    if not isinstance(u, dict):
        return ""
    return (u.get("full_name") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}").strip()


def _israel(ts: str | None) -> str | None:
    """Comeet UTC time as Israel wall time, e.g. "Sun 25 Oct 10:00"."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Jerusalem"))
    except ValueError:
        return None
    return t.strftime("%a %d %b %H:%M")


def _step_view(s: dict[str, Any]) -> dict[str, Any]:
    """One candidate step with who it's assigned to, who actually did it and when.
    The actor is assigned_to[].filled_by; null means unknown, never "the assignee"."""
    slots = [a for a in (s.get("assigned_to") or []) if isinstance(a, dict)]
    did = [n for n in dict.fromkeys(_who(a.get("filled_by")) for a in slots) if n] or (
        [_who(s.get("filled_by"))] if _who(s.get("filled_by")) else [])
    done = bool(s.get("is_completed"))
    return {"name": s.get("name"), "done": done, "type": STEP_TYPE_NAMES.get(s.get("step_type"), "other"),
            "assignees": [n for n in dict.fromkeys(_who(a) for a in slots) if n],
            "completed_by": did if done else [],
            "completed": _israel(s.get("time_completed")) if done else None,
            "scheduled": _israel(s.get("time_scheduled"))}


def process(num: int, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """Who this is, lead or candidate, which position, and where they are."""
    app = _app(app)
    c = _get(app, f"/api/v2/candidates/{num}") or {}
    if not isinstance(c, dict) or not c.get("id"):
        raise sa.ScreeningError("no such lead or candidate in Comeet", status=404)
    req = c.get("requisition") or {}
    is_lead = c.get("process_type") == LEAD
    out: dict[str, Any] = {
        "numeric_id": num, "name": c.get("full_name") or "", "email": c.get("email") or "",
        "linkedin_url": c.get("linkedin_url") or "", "comeet_url": f"https://app.comeet.co/app/can/{num}",
        "position": {"id": req.get("id"), "name": req.get("name")}, "kind": "lead" if is_lead else "candidate",
        "status_code": c.get("status"), "source": (c.get("source") or {}).get("name"),
    }
    if is_lead:
        ld = c.get("lead_details") or {}
        out["lead_step"] = OUTREACH.get(ld.get("outreach_status"), str(ld.get("outreach_status")))
        out["not_interested"] = c.get("status") == NOT_INTERESTED_STATUS
        if req.get("id"):
            out["position_steps"] = position_steps(int(req["id"]), app)
        return out
    steps = [s for s in (_get(app, f"/api/v1/candidates/{num}/steps") or []) if isinstance(s, dict)]
    current = next((s for s in steps if not s.get("is_completed")), None)
    out["steps"] = [_step_view(s) for s in steps]
    out["current_step"] = None if current is None else {
        **_step_view(current), "can_pass_here": _passable(current)}
    out["times_in_israel_time"] = True
    return out


def _require(num: int, kind: str, app: ComeetAppClient) -> dict[str, Any]:
    c = _get(app, f"/api/v2/candidates/{num}") or {}
    is_lead = isinstance(c, dict) and c.get("process_type") == LEAD
    if kind == "lead" and not is_lead:
        raise sa.ScreeningError("this is a candidate, not a lead")
    if kind == "candidate" and is_lead:
        raise sa.ScreeningError("this is a lead, not a candidate: convert it first")
    return c


def set_lead_step(num: int, step: str, app: ComeetAppClient | None = None) -> str:
    app = _app(app)
    _require(num, "lead", app)
    code = OUTREACH_BY_NAME.get((step or "").strip().lower())
    if not code:
        raise sa.ScreeningError("lead step must be Not Contacted, Contacted or Responded", status=400)
    app._request("PATCH", f"/api/v1/candidates/{num}/outreach-status", json_body={"lead_details": {"outreach_status": code}})
    return OUTREACH[code]


def lead_not_interested(num: int, reason_id: int, app: ComeetAppClient | None = None) -> str:
    app = _app(app)
    _require(num, "lead", app)
    reasons = {r["id"]: r["reason"] for r in withdrawal_reasons(app)}
    if reason_id not in reasons:
        raise sa.ScreeningError("pick one of Comeet's withdrawal reasons", status=400)
    app._request("PATCH", f"/api/v1/candidates/{num}",
                 json_body={"status": NOT_INTERESTED_STATUS, "disposition_type_id": reason_id, "step_ids_completed_by": []})
    return reasons[reason_id]


def convert_lead(num: int, step_name: str, position_id: int | None = None, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """Turn a lead into a candidate on its position (or another), landing on the named step."""
    app = _app(app)
    c = _require(num, "lead", app)
    req_id = int(position_id or (c.get("requisition") or {}).get("id") or 0)
    if not req_id:
        raise sa.ScreeningError("this lead has no position; pass one", status=400)
    steps = position_steps(req_id, app)
    want = (step_name or "").strip().lower()
    step = next((s for s in steps if s["name"].strip().lower() == want), None) if want else (steps[0] if steps else None)
    if step is None:
        raise sa.ScreeningError(f"no step \"{step_name}\" on that position (steps: {', '.join(s['name'] for s in steps)})", status=400)
    source_id = (c.get("source") or {}).get("id")
    if not source_id:
        raise sa.ScreeningError("this lead has no source in Comeet", status=400)
    app._request("POST", f"/api/v1/candidates/{num}/lead_to_candidate",
                 json_body={"new_requisition_id": req_id, "source_id": int(source_id), "step_to_start_id": step["id"]})
    return {"position_id": req_id, "step": step["name"]}


def pass_current_step(num: int, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """Pass the candidate's current step, only when it needs no interview summary."""
    app = _app(app)
    _require(num, "candidate", app)
    steps = [s for s in (_get(app, f"/api/v1/candidates/{num}/steps") or []) if isinstance(s, dict)]
    current = next((s for s in steps if not s.get("is_completed")), None)
    if current is None:
        raise sa.ScreeningError("this candidate has no open step (the process is finished)")
    if NEVER_PASS.search(str(current.get("name") or "")):
        raise sa.ScreeningError(f"\"{current.get('name')}\" is an offer step: offers are handled in Comeet, never passed from here")
    if current.get("step_type") not in PASSABLE_STEP_TYPES:
        kind = STEP_TYPE_NAMES.get(current.get("step_type"), "this kind of")
        raise sa.ScreeningError(f"\"{current.get('name')}\" is {'an' if kind[0] in 'aeiou' else 'a'} {kind} step: it needs the "
                                f"interviewer's summary in Comeet, so it can't be passed from here")
    sa.submit_decision(num, int(current["id"]), "pass", None, app)
    nxt = next((s for s in steps if not s.get("is_completed") and s is not current), None)
    return {"passed": current.get("name"), "now_at": nxt.get("name") if nxt else None}


def comment(num: int, recruiter_name: str, text: str, app: ComeetAppClient | None = None) -> None:
    text = (text or "").strip()
    if not text:
        raise sa.ScreeningError("the comment is empty", status=400)
    sa.post_comment(num, f"{text}\n({recruiter_name}, via RiverTalent)", _app(app))


def comeet_user(email: str, app: ComeetAppClient | None = None) -> dict[str, Any]:
    """The recruiter's Comeet user (id and name) by work email."""
    want = (email or "").strip().lower().replace("@riverside.com", "@riverside.fm")
    if not want:
        raise sa.ScreeningError("the recruiter's email is missing", status=400)
    for u in _get(_app(app), "/api/v1/users") or []:
        if isinstance(u, dict) and str(u.get("email") or "").strip().lower() == want:
            name = (u.get("full_name") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}").strip()
            return {"id": int(u["id"]), "name": name or want}
    raise sa.ScreeningError(f"{email} isn't a Comeet user, so the reminder has nobody to notify", status=400)


def reminder(num: int, recruiter_email: str, text: str, at: str, tz: str = "", app: ComeetAppClient | None = None) -> dict[str, Any]:
    """A Comeet reminder on this lead or candidate for the recruiter at `at`: local wall time in `tz`
    (IANA name, default Asia/Jerusalem), or ISO 8601 with an offset."""
    app = _app(app)
    try:
        when = datetime.fromisoformat((at or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise sa.ScreeningError("pass the reminder time like 2026-10-22T10:00 (the recruiter's local time)", status=400)
    if when.tzinfo is None:
        try:
            when = when.replace(tzinfo=ZoneInfo((tz or "").strip() or "Asia/Jerusalem"))
        except (ZoneInfoNotFoundError, ValueError):
            raise sa.ScreeningError(f"unknown time zone \"{tz}\" (use a name like Asia/Jerusalem or America/Toronto)", status=400)
    when = when.astimezone(timezone.utc)
    if when <= datetime.now(timezone.utc):
        raise sa.ScreeningError("the reminder time is in the past", status=400)
    who = comeet_user(recruiter_email, app)
    text = (text or "").strip() or "Follow up"
    iso = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    app._request("POST", "/api/v1/sendmessages", json_body=[{
        "action": 2, "body": f'<b class="bolder mentionItem">{html.escape(who["name"])}</b> {html.escape(text)}',
        "event": 32, "files": [], "groups": [],
        "participants": [{"id": sa.bot_user_id(app), "type": "user"}, {"id": who["id"], "type": "user"}],
        "visibility": 1, "discussion_context": {"id": num, "type": "candidate"}, "discussion_type": 3,
        "is_new": True, "time_to_send_message": iso}])
    return {"for": who["name"], "at_utc": iso, "text": text}


def _by_day(slots: list[str]) -> dict[str, list[str]]:
    """UTC slot starts grouped by Israel day: {"Sun 25 Oct": ["10:00", "14:30"]}."""
    out: dict[str, list[str]] = {}
    for ts in sorted(slots or []):
        local = _israel(ts)
        if local:
            day, hm = local.rsplit(" ", 1)
            out.setdefault(day, []).append(hm)
    return out


def availability(num: int, step_name: str = "", days: int = 7, duration: int | None = None,
                 app: ComeetAppClient | None = None) -> dict[str, Any]:
    """Free slots of the interviewers on one of a candidate's steps: the named step, else the
    current step if it's an interview, else the next open interview step."""
    app = _app(app)
    _require(num, "candidate", app)
    steps = [s for s in (_get(app, f"/api/v1/candidates/{num}/steps") or []) if isinstance(s, dict) and s.get("id")]
    open_steps = [s for s in steps if not s.get("is_completed")]
    want = (step_name or "").strip().lower()
    if want:
        step = next((s for s in steps if str(s.get("name") or "").strip().lower() == want), None)
        if step is None:
            raise sa.ScreeningError(f"no step \"{step_name}\" (steps: {', '.join(str(s.get('name')) for s in steps)})", status=400)
    else:
        step = next((s for s in open_steps if s.get("step_type") == 2), None)
        if step is None:
            raise sa.ScreeningError("this candidate has no interview step ahead")
    days = max(1, min(int(days or 7), 14))
    minutes = int(duration or step.get("duration_minutes") or 30)
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = sa._body(app._request(
        "POST", f"/api/v1/candidates/{num}/steps/{int(step['id'])}/interview-availability?parent_id={num}",
        json_body={"id": int(step["id"]), "start": start, "days": days, "parent_id": num,
                   "meeting_room": [], "duration_minutes": minutes})) or {}
    inter = res.get("availability_intersection")
    if isinstance(inter, dict) and inter.get("is_failed"):
        # The answer comes from the signed-in Comeet user's Google connection; the bot has none.
        raise sa.ScreeningError("Comeet couldn't read the interviewers' calendars for the RiverTalent account "
                                f"({inter.get('message') or 'schedule error'}): availability isn't available yet", status=503)
    people = []
    for a in res.get("assigned_to") or []:
        if not isinstance(a, dict):
            continue
        sch = a.get("scheduling") or {}
        people.append({
            "name": _who(a), "slots": _by_day(sch.get("availability") or []),
            "calendar_problem": bool(sch.get("is_failed")),
            "minimum_notice_hours": a.get("minimum_scheduling_notice_hours"),
            "max_interview_hours_per_day": a.get("max_interviews_hours_per_day"),
            "out_of_office": [_israel(a.get("ooo_starttime")), _israel(a.get("ooo_endtime"))] if a.get("ooo_starttime") else None,
        })
    return {"step": step.get("name"), "type": STEP_TYPE_NAMES.get(step.get("step_type"), "other"),
            "is_current": step is (open_steps[0] if open_steps else None),
            "already_scheduled": _israel(step.get("time_scheduled")), "duration_minutes": minutes, "days": days,
            "interviewers": people, "common_slots": _by_day(res.get("availability_intersection") or []),
            "times_in_israel_time": True}
