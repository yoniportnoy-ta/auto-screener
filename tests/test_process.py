"""Any lead or candidate: lead steps, not interested, convert, pass a review step, comment.

Payloads are pinned to Yoni's recording of a test lead (2026-10-21). A change here should
come from a new capture, not a guess.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import process_actions as pa
from app import screening_actions as sa
from app.config import settings
from app.routes import screening as routes

REASONS = [
    {"id": 33273, "reason": "Doesn't Meet Minimum Qualifications", "status": 1, "order": 2},
    {"id": 33278, "reason": "Not Interested in Position", "status": 2, "order": 1},
    {"id": 34938, "reason": "Accepted another offer", "status": 2, "order": 3},
]
REQ_STEPS = [
    {"id": 3238286, "name": "Video Interview/Recruiter", "step_type": 2, "group_number": 1.0},
    {"id": 3238285, "name": "CV Screen / Recruiter", "step_type": 1, "group_number": 0.0},
]
LEAD = {"id": 63470107, "full_name": "yoni test", "process_type": 2, "status": 0,
        "requisition": {"id": 403982, "name": "BDR - Israel"}, "source": {"id": 350131, "name": "Yoni Portnoy"},
        "lead_details": {"outreach_status": 2}}
CAND = {**LEAD, "process_type": 1}


class FakeApp:
    def __init__(self, record=LEAD, steps=None):
        self.calls, self.record = [], record
        self.steps = steps if steps is not None else [
            {"id": 9001, "name": "CV Screen / Recruiter", "step_type": 1, "is_completed": False},
            {"id": 9002, "name": "Video Interview/Recruiter", "step_type": 2, "is_completed": False}]

    def _request(self, method, path, *, json_body=None, **_):
        self.calls.append((method, path, json_body))
        if path.startswith("/api/v2/candidates/"):
            return 200, self.record
        if "disposition-types" in path:
            return 200, REASONS
        if path.startswith("/api/v1/requisitions/") and "/steps" in path:
            return 200, REQ_STEPS
        if path.endswith("/steps"):
            return 200, self.steps
        if "interview-availability" in path:
            return 200, {"assigned_to": [{"full_name": "Yoni Portnoy", "minimum_scheduling_notice_hours": 48,
                                          "max_interviews_hours_per_day": 2,
                                          "scheduling": {"availability": ["2026-10-27T12:00:00Z", "2026-10-25T08:00:00Z"]}},
                                         {"full_name": "Itay Schmidt", "scheduling": {"availability": ["2026-10-25T08:00:00Z"]}}],
                         "availability_intersection": ["2026-10-25T08:00:00Z"]}
        if path == "/api/v1/users":
            return 200, [{"id": 160785, "email": "comeetadmin@riverside.fm"},
                         {"id": 222, "email": "dana@riverside.fm", "first_name": "Dana", "last_name": "Levi"}]
        if method in ("PATCH", "POST"):
            return 200, {"id": 63470107}
        raise AssertionError(f"unexpected {method} {path}")

    def get_candidate(self, uid):
        return {"id": 63470107}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(sa, "_bot_user_id", None)
    monkeypatch.setattr(settings, "comeet_app_email", "comeetadmin@riverside.fm")
    monkeypatch.setattr(settings, "screener_gateway_token", "t")


def test_numeric_id_from_link_id_or_uid():
    app = FakeApp()
    assert pa.numeric_id("https://app.comeet.co/app/req/403982/can/63470107", app) == 63470107
    assert pa.numeric_id("https://app.comeet.co/app/?goto_url=/can/63470107", app) == 63470107
    assert pa.numeric_id("63470107", app) == 63470107
    assert pa.numeric_id("1B.D7C35", app) == 63470107


def test_process_shows_assignees_actor_and_times():
    steps = [
        {"id": 1, "name": "CV Screen / Recruiter", "step_type": 1, "is_completed": True, "time_completed": "2026-10-21T06:30:00Z",
         "assigned_to": [{"full_name": "Noga Kaufman", "filled_by": None},
                         {"full_name": "Yoni Portnoy", "filled_by": {"full_name": "Yoni Portnoy"}}]},
        {"id": 2, "name": "Video Interview/Recruiter", "step_type": 2, "is_completed": False,
         "time_scheduled": "2026-10-25T08:00:00Z", "assigned_to": [{"first_name": "Yoni", "last_name": "Portnoy"}]},
        {"id": 3, "name": "Go/No-go", "step_type": 1, "is_completed": True, "assigned_to": [{"full_name": "Itay Schmidt", "filled_by": None}]},
    ]
    out = pa.process(63470107, FakeApp(CAND, steps=steps))
    cv, video, gng = out["steps"]
    assert cv["assignees"] == ["Noga Kaufman", "Yoni Portnoy"] and cv["completed_by"] == ["Yoni Portnoy"]
    assert cv["completed"] == "Wed 21 Oct 09:30"
    assert video["scheduled"] == "Sun 25 Oct 10:00" and video["completed_by"] == []  # 2026-10-25 is after the clock change (+02:00)
    assert gng["completed_by"] == []  # unknown actor stays unknown, never "the assignee"
    assert out["current_step"]["name"] == "Video Interview/Recruiter" and out["current_step"]["assignees"] == ["Yoni Portnoy"]


def test_availability_next_interview_step_read_only():
    app = FakeApp(CAND)
    out = pa.availability(63470107, app=app)
    assert out["step"] == "Video Interview/Recruiter" and out["duration_minutes"] == 30
    assert out["interviewers"][0]["slots"] == {"Sun 25 Oct": ["10:00"], "Tue 27 Oct": ["14:00"]}
    assert out["common_slots"] == {"Sun 25 Oct": ["10:00"]}
    (method, path, body), = [c for c in app.calls if "interview-availability" in c[1]]
    assert method == "POST" and body["id"] == 9002 and body["days"] == 7 and body["meeting_room"] == []
    assert not [c for c in app.writes() if "interview-availability" not in c[1]]  # nothing else written


def test_availability_one_failed_calendar_keeps_the_others(monkeypatch):
    app = FakeApp(CAND)
    real = app._request
    def req(method, path, **kw):
        if "interview-availability" in path:
            app.calls.append((method, path, kw.get("json_body")))
            return 200, {"assigned_to": [
                {"full_name": "Yoni Portnoy", "working_hours": {"7": [{"start_time": "09:30", "end_time": "11:00"}], "1": [{"start_time": "08:30", "end_time": "12:00"}]},
                 "scheduling": {"availability": ["2026-10-27T07:30:00Z"]}},
                {"full_name": "Yahel Shiran", "scheduling": {"is_failed": True, "error_reason_code": 8}}],
                "availability_intersection": {"code": 524, "is_failed": True}}
        return real(method, path, **kw)
    app._request = req
    out = pa.availability(63470107, app=app)
    yoni, yahel = out["interviewers"]
    assert yoni["slots"] == {"Tue 27 Oct": ["09:30"]} and yoni["working_hours"] == {"Sun": ["09:30-11:00"], "Mon": ["08:30-12:00"]}
    assert yahel["calendar_problem"] is True and yahel["calendar_error_code"] == 8 and yahel["slots"] == {}
    assert out["common_slots"] == {}


def test_availability_reads_calendars_as_the_scheduling_account(monkeypatch):
    bot, scheduler = FakeApp(CAND), FakeApp(CAND)
    monkeypatch.setattr(sa, "app_client", lambda: bot)
    monkeypatch.setattr(sa, "scheduler_client", lambda: scheduler)
    pa.availability(63470107)
    assert not [c for c in bot.calls if "interview-availability" in c[1]]
    assert [c for c in scheduler.calls if "interview-availability" in c[1]]
    monkeypatch.setattr(sa, "scheduler_client", lambda: None)  # not configured: the bot asks
    pa.availability(63470107)
    assert [c for c in bot.calls if "interview-availability" in c[1]]


def test_free_slots_follow_hours_notice_busy_and_clock_change():
    from datetime import datetime, timezone
    hours = {"7": [{"start_time": "09:00", "end_time": "11:00"}]}  # Sundays only
    start = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)      # Thu 22 Oct 09:00 Israel
    busy = [(datetime(2026, 10, 25, 7, 30, tzinfo=timezone.utc), datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc))]
    slots = pa.free_slots(hours, 12, "Asia/Jerusalem", busy, start, 7, 30)
    # Sun 25 Oct is after the clock change (+02:00): 09:00 Israel = 07:00Z; 09:30 is busy.
    assert slots == ["2026-10-25T07:00:00Z", "2026-10-25T08:00:00Z", "2026-10-25T08:30:00Z"]
    assert pa.free_slots(hours, 24 * 4, "Asia/Jerusalem", [], start, 7, 30) == []  # notice pushes past Sunday


def test_failed_calendar_is_read_like_the_scheduling_screen(monkeypatch):
    bot, reader = FakeApp(CAND), FakeApp(CAND)
    def avail(method, path, **kw):
        if "interview-availability" in path:
            return 200, {"assigned_to": [{"full_name": "Yahel Shiran", "email": "yahel@riverside.fm", "timezone": "Asia/Jerusalem",
                                          "minimum_scheduling_notice_hours": 0,
                                          "working_hours": {str(d): [{"start_time": "09:00", "end_time": "10:00"}] for d in range(1, 8)},
                                          "scheduling": {"is_failed": True, "error_reason_code": 8}}],
                         "availability_intersection": {"code": 524, "is_failed": True}}
        if "google/calendar/events" in path:
            reader.calls.append((method, path, None))
            return 200, {"items": [{"summary": "secret 1:1", "start": {"dateTime": "2099-01-01T00:00:00Z"}, "end": {"dateTime": "2099-01-01T01:00:00Z"}}]}
        return FakeApp._request(reader, method, path, **kw)
    reader._request = avail
    monkeypatch.setattr(sa, "app_client", lambda: bot)
    monkeypatch.setattr(sa, "scheduler_client", lambda: reader)
    out = pa.availability(63470107)
    p = out["interviewers"][0]
    assert p["calendar_problem"] is False and p["computed_from_calendar"] is True and p["slots"]
    assert any("calendarId=yahel%40riverside.fm" in c[1] for c in reader.calls)
    assert "secret" not in json.dumps(out)  # meeting titles never leave


def test_default_hours_follow_location():
    assert sorted(pa.default_hours("Asia/Jerusalem")) == ["1", "2", "3", "4", "7"]      # Sun to Thu
    assert sorted(pa.default_hours("America/Toronto")) == ["1", "2", "3", "4", "5"]     # Mon to Fri
    assert pa.default_hours("America/Toronto")["1"] == [{"start_time": "09:00", "end_time": "17:00"}]


def test_canadian_interviewer_toronto_viewer(monkeypatch):
    from datetime import datetime, timezone
    pa._user_tz.clear()
    bot, reader = FakeApp(CAND), FakeApp(CAND)
    def bot_req(method, path, **kw):
        if path == "/api/v1/users":
            return 200, [{"id": 1, "email": "kourtney@riverside.fm", "timezone": "America/Toronto"}]
        return FakeApp._request(bot, method, path, **kw)
    bot._request = bot_req
    def reader_req(method, path, **kw):
        if "interview-availability" in path:
            return 200, {"assigned_to": [{"full_name": "Jade M", "email": "jade@riverside.fm", "timezone": "America/Toronto",
                                          "minimum_scheduling_notice_hours": 0, "scheduling": {"is_failed": True}}],
                         "availability_intersection": {"is_failed": True}}
        if "google/calendar/events" in path:
            return 200, {"items": []}
        return FakeApp._request(reader, method, path, **kw)
    reader._request = reader_req
    monkeypatch.setattr(sa, "app_client", lambda: bot)
    monkeypatch.setattr(sa, "scheduler_client", lambda: reader)
    out = pa.availability(63470107, viewer_email="Kourtney@riverside.com")
    jade = out["interviewers"][0]
    assert out["times_in"] == "America/Toronto" and jade["timezone"] == "America/Toronto"
    assert jade["hours_source"] == "default for their location" and set(jade["working_hours"]) == {"Mon", "Tue", "Wed", "Thu", "Fri"}
    for day, times in jade["slots"].items():   # shown in Toronto time: weekdays, 09:00 to 16:30 starts
        assert day[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri"} and all("09:00" <= t <= "16:30" for t in times)
    pa._user_tz.clear()


def test_two_interviewers_get_the_times_both_are_free(monkeypatch):
    bot, reader = FakeApp(CAND), FakeApp(CAND)
    def req(method, path, **kw):
        if "interview-availability" in path:
            return 200, {"assigned_to": [
                {"full_name": "Itay Schmidt", "scheduling": {"availability": ["2099-01-04T07:00:00Z", "2099-01-04T07:30:00Z"]}},
                {"full_name": "Roie Cohen", "scheduling": {"availability": ["2099-01-04T07:30:00Z", "2099-01-04T09:00:00Z"]}}],
                "availability_intersection": {"is_failed": True}}
        return FakeApp._request(reader, method, path, **kw)
    reader._request = req
    monkeypatch.setattr(sa, "app_client", lambda: bot)
    monkeypatch.setattr(sa, "scheduler_client", lambda: reader)
    out = pa.availability(63470107)
    assert out["common_slots"] == {"Sun 04 Jan": ["09:30"]} and out["common_slots_complete"] is True


def test_sourcing_sources_only_type_10():
    app = FakeApp()
    real = app._request
    def req(method, path, **kw):
        if path == "/api/v1/sources":
            return 200, [{"id": 1, "name": "Noga Kaufman (LinkedIn Recruiter)", "source_type": 10},
                         {"id": 2, "name": "Michal Milner", "source_type": 4},      # an employee referral source
                         {"id": 3, "name": "Katya - Sourcing", "source_type": 10}]
        return real(method, path, **kw)
    app._request = req
    assert [x["name"] for x in pa.sourcing_sources(app)] == ["Katya - Sourcing", "Noga Kaufman (LinkedIn Recruiter)"]


def test_availability_refuses_a_lead():
    with pytest.raises(sa.ScreeningError):
        pa.availability(63470107, app=FakeApp(LEAD))


def test_lead_step_payload_matches_the_capture():
    app = FakeApp()
    assert pa.set_lead_step(63470107, "responded", app) == "Responded"
    assert app.writes() == [("PATCH", "/api/v1/candidates/63470107/outreach-status", {"lead_details": {"outreach_status": 3}})]


def test_lead_step_refused_for_a_candidate_or_unknown_step():
    with pytest.raises(sa.ScreeningError):
        pa.set_lead_step(63470107, "Contacted", FakeApp(CAND))
    with pytest.raises(sa.ScreeningError):
        pa.set_lead_step(63470107, "Interviewing", FakeApp())


def test_not_interested_uses_a_withdrawal_reason_only():
    app = FakeApp()
    assert pa.lead_not_interested(63470107, 33278, app) == "Not Interested in Position"
    assert app.writes() == [("PATCH", "/api/v1/candidates/63470107",
                             {"status": 5, "disposition_type_id": 33278, "step_ids_completed_by": []})]
    with pytest.raises(sa.ScreeningError):
        pa.lead_not_interested(63470107, 33273, FakeApp())  # a company-side reject reason


def test_convert_payload_matches_the_capture():
    app = FakeApp()
    assert pa.convert_lead(63470107, "CV Screen / Recruiter", None, app) == {"position_id": 403982, "step": "CV Screen / Recruiter"}
    assert app.writes() == [("POST", "/api/v1/candidates/63470107/lead_to_candidate",
                             {"new_requisition_id": 403982, "source_id": 350131, "step_to_start_id": 3238285})]


def test_convert_refuses_unknown_step():
    with pytest.raises(sa.ScreeningError):
        pa.convert_lead(63470107, "Phone screen", None, FakeApp())


def test_pass_only_on_review_steps_never_interviews():
    app = FakeApp(CAND)
    assert pa.pass_current_step(63470107, app)["passed"] == "CV Screen / Recruiter"
    assert app.writes()[0][2] == {"proceed": True, "further_review": ""}
    interview_first = FakeApp(CAND, steps=[{"id": 9002, "name": "Video Interview/Recruiter", "step_type": 2, "is_completed": False}])
    with pytest.raises(sa.ScreeningError):
        pa.pass_current_step(63470107, interview_first)
    assert interview_first.writes() == []


def test_offer_step_is_never_passed_even_as_type_3():
    offer = FakeApp(CAND, steps=[{"id": 9009, "name": "Offer", "step_type": 3, "is_completed": False}])
    with pytest.raises(sa.ScreeningError):
        pa.pass_current_step(63470107, offer)
    assert offer.writes() == []
    assert pa.process(63470107, FakeApp(CAND, steps=[{"id": 9009, "name": "Offer", "step_type": 3, "is_completed": False}]))["current_step"]["can_pass_here"] is False


def test_comment_is_internal_and_names_the_recruiter():
    app = FakeApp()
    pa.comment(63470107, "Dana Levi", "Called, interested in March", app)
    (method, path, body), = app.writes()
    assert path == "/api/v1/sendmessages" and body[0]["action"] == 2 and body[0]["discussion_type"] == 1
    assert "to" not in body[0] and "Dana Levi" in body[0]["body"]


def test_reminder_payload_matches_the_l2c_capture():
    app = FakeApp()
    res = pa.reminder(63470107, "Dana@riverside.com", "Call back <today>", "2099-01-02T10:00:00+02:00", app=app)
    assert res == {"for": "Dana Levi", "at_utc": "2099-01-02T08:00:00.000Z", "text": "Call back <today>"}
    (method, path, body), = app.writes()
    assert path == "/api/v1/sendmessages"
    m = body[0]
    assert (m["action"], m["event"], m["discussion_type"]) == (2, 32, 3)
    assert m["participants"] == [{"id": 160785, "type": "user"}, {"id": 222, "type": "user"}]
    assert m["body"] == '<b class="bolder mentionItem">Dana Levi</b> Call back &lt;today&gt;'
    assert m["discussion_context"] == {"id": 63470107, "type": "candidate"} and m["time_to_send_message"] == "2099-01-02T08:00:00.000Z"


def test_reminder_local_time_follows_the_clock_change():
    # Israel: summer +03:00 until the last Sunday of October, then +02:00; Toronto in January is -05:00.
    assert pa.reminder(1, "dana@riverside.fm", "", "2099-07-01T10:00", app=FakeApp())["at_utc"] == "2099-07-01T07:00:00.000Z"
    assert pa.reminder(1, "dana@riverside.fm", "", "2099-12-01T10:00", app=FakeApp())["at_utc"] == "2099-12-01T08:00:00.000Z"
    assert pa.reminder(1, "dana@riverside.fm", "", "2099-01-05T10:00", "America/Toronto", app=FakeApp())["at_utc"] == "2099-01-05T15:00:00.000Z"


def test_reminder_refuses_past_naive_or_unknown_recruiter():
    for at in ("2020-01-01T10:00:00+02:00", "2020-01-01T10:00", "tomorrow"):
        with pytest.raises(sa.ScreeningError):
            pa.reminder(1, "dana@riverside.fm", "x", at, app=FakeApp())
    with pytest.raises(sa.ScreeningError):
        pa.reminder(1, "dana@riverside.fm", "x", "2099-01-02T10:00", "Mars/Base", app=FakeApp())
    with pytest.raises(sa.ScreeningError):
        pa.reminder(1, "nobody@riverside.fm", "x", "2099-01-02T10:00:00+02:00", app=FakeApp())


@pytest.fixture
def client(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(sa, "app_client", lambda: fake)
    api = FastAPI()
    api.include_router(routes.router, prefix="/api/screening")
    c = TestClient(api)
    c.fake = fake
    return c


def test_routes_need_the_gateway_token(client):
    assert client.get("/api/screening/process", params={"candidate": "63470107"}).status_code == 401


def test_lead_step_route_comments_with_recruiter(client):
    r = client.post("/api/screening/process/lead-step", headers={"X-Gateway-Token": "t"},
                    json={"candidate": "63470107", "step": "Contacted", "recruiter": {"name": "Dana Levi"}})
    assert r.status_code == 200 and r.json()["lead_step"] == "Contacted"
    assert [w[1] for w in client.fake.writes()] == ["/api/v1/candidates/63470107/outreach-status", "/api/v1/sendmessages"]
