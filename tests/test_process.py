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


def test_referral_sources_types_4_and_9_without_bot_duplicates():
    app = FakeApp()
    real = app._request
    def req(method, path, **kw):
        if path == "/api/v1/sources":
            return 200, [{"id": 1, "name": "Michal Milner", "source_type": 4},
                         {"id": 2, "name": "Michal Brosh", "source_type": 9},
                         {"id": 3, "name": "Kourtney Savona (Slack bot)", "source_type": 4},   # no plain one: keep
                         {"id": 4, "name": "Yoni Portnoy (Slack bot)", "source_type": 4},
                         {"id": 5, "name": "Yoni Portnoy", "source_type": 4},
                         {"id": 6, "name": "ZipRecruiter", "source_type": 2},
                         {"id": 7, "name": "Noga Kaufman (LinkedIn Recruiter)", "source_type": 10}]
        return real(method, path, **kw)
    app._request = req
    got = pa.referral_sources(app)
    assert [(x["name"], x["compensated"]) for x in got] == [
        ("Kourtney Savona (Slack bot)", True), ("Michal Brosh", False), ("Michal Milner", True), ("Yoni Portnoy", True)]


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


# ─── Interview feedback: text into the recruiter's own draft, never submitted ───
FB_STEPS = [
    {"id": 9001, "name": "CV Screen / Recruiter", "step_type": 1, "is_completed": True, "interview": {"id": 500},
     "assigned_to": [{"email": "dana@riverside.fm", "interview_started": True, "interview_submitted": True}]},
    {"id": 9002, "name": "Video Interview/Recruiter", "step_type": 2, "is_completed": False, "interview": {"id": 71303438},
     "assigned_to": [{"email": "dana@riverside.fm", "interview_started": True, "interview_submitted": False}]},
    {"id": 9003, "name": "Interview / Manager", "step_type": 2, "is_completed": False, "interview": None,
     "assigned_to": [{"email": "joel@riverside.fm", "interview_started": False, "interview_submitted": False}]}]


class FeedbackApp(FakeApp):
    def __init__(self, draft=None, steps=FB_STEPS):
        super().__init__(record=CAND, steps=steps)
        self.draft = {"id": 71303438, "candidate_step_id": 9002, "is_completed": False, "notes": "<p>.</p>",
                      "pros": "<p>Sharp</p>", "salary_expectations": "", **(draft or {})}

    def _request(self, method, path, *, json_body=None, **kw):
        if path.startswith("/api/v1/interviews/") and method == "GET":
            self.calls.append((method, path, json_body))
            return 200, self.draft
        return super()._request(method, path, json_body=json_body, **kw)


def test_feedback_patches_text_fields_only_never_submits():
    app = FeedbackApp()
    res = pa.feedback(63429397, "Dana@riverside.com", {"notes": "Strong <SaaS> background\nWants hybrid",
                      "pros": "Clear communicator", "salary_expectations": "20k NIS"}, app=app)
    assert res["step"] == "Video Interview/Recruiter" and res["submitted"] is False
    assert res["feedback_url"] == "https://app.comeet.co/app/interview/71303438?view=evaluate"
    w = app.writes()
    assert all(m == "PATCH" and p == "/api/v1/interviews/71303438" for m, p, _ in w)
    bodies = [b for *_, b in w]
    for b in bodies:
        assert not {"is_completed", "recommendation", "decision_candidate_status", "proceed"} & set(b)
        assert len(set(b) - {"id", "time_updated"}) == 1
    got = {k: v for b in bodies for k, v in b.items() if k not in ("id", "time_updated")}
    assert got["notes"] == "<p>Strong &lt;SaaS&gt; background</p><p>Wants hybrid</p>"   # placeholder dot replaced
    assert got["pros"] == "<p>Sharp</p><p>Clear communicator</p>"                       # added after
    assert got["salary_expectations"] == "20k NIS"


def test_feedback_refuses_decision_fields():
    for bad in ({"recommendation": "3"}, {"decision_candidate_status": "3"}, {"is_completed": "true"}):
        with pytest.raises(sa.ScreeningError):
            pa.feedback(63429397, "dana@riverside.fm", bad, app=FeedbackApp())


def test_feedback_needs_the_recruiters_own_open_draft():
    with pytest.raises(sa.ScreeningError, match="aren't an interviewer"):
        pa.feedback(63429397, "joel@riverside.fm", {"notes": "x"}, interview="https://app.comeet.co/app/interview/71303438?view=evaluate",
                    app=FeedbackApp())
    with pytest.raises(sa.ScreeningError, match="no feedback draft"):
        pa.feedback(63429397, "joel@riverside.fm", {"notes": "x"}, step_name="manager", app=FeedbackApp())
    with pytest.raises(sa.ScreeningError, match="already submitted"):
        pa.feedback(63429397, "dana@riverside.fm", {"notes": "x"}, app=FeedbackApp(draft={"is_completed": True}))
    two = [dict(FB_STEPS[1], assigned_to=FB_STEPS[1]["assigned_to"] + [{"email": "joel@riverside.fm", "interview_started": True}])]
    with pytest.raises(sa.ScreeningError, match="more than one"):
        pa.feedback(63429397, "dana@riverside.fm", {"notes": "x"}, app=FeedbackApp(steps=two))
    with pytest.raises(sa.ScreeningError, match="different candidate"):
        pa.feedback(63429397, "dana@riverside.fm", {"notes": "x"}, interview="71303438", app=FeedbackApp(draft={"candidate_step_id": 1}))


# ─── Email: only the email is sent, and the record is read back ───
class EmailApp(FakeApp):
    def __init__(self, after_change=None):
        super().__init__(record=CAND)
        self.rec = {"id": 63429397, "email": "old@gmail.com", "first_name": "yoni", "phone": "1", "custom_fields": [{"id": 1, "value": "a"}]}
        self.after_change = after_change or {}

    def _request(self, method, path, *, json_body=None, **kw):
        if path == "/api/v1/candidates/63429397":
            self.calls.append((method, path, json_body))
            if method == "PATCH":
                self.rec = {**self.rec, **json_body, **self.after_change}
            return 200, self.rec
        return super()._request(method, path, json_body=json_body, **kw)


def test_email_patch_sends_only_the_email_and_checks_the_rest():
    app = EmailApp()
    res = pa.update_email(63429397, " New.Me@Gmail.com ", app)
    assert res == {"email": "new.me@gmail.com", "previous": "old@gmail.com", "changed": True}
    assert app.writes() == [("PATCH", "/api/v1/candidates/63429397", {"email": "new.me@gmail.com"})]


def test_email_same_bad_or_side_effects():
    app = EmailApp()
    assert pa.update_email(63429397, "old@gmail.com", app)["changed"] is False and not app.writes()
    with pytest.raises(sa.ScreeningError, match="isn't an email"):
        pa.update_email(63429397, "not-an-email", EmailApp())
    with pytest.raises(sa.ScreeningError, match="also changed: phone"):
        pa.update_email(63429397, "x@gmail.com", EmailApp(after_change={"phone": ""}))
