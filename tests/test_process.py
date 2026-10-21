"""Any lead or candidate: lead steps, not interested, convert, pass a review step, comment.

Payloads are pinned to Yoni's recording of a test lead (2026-10-21). A change here should
come from a new capture, not a guess.
"""
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


def test_comment_is_internal_and_names_the_recruiter():
    app = FakeApp()
    pa.comment(63470107, "Dana Levi", "Called, interested in March", app)
    (method, path, body), = app.writes()
    assert path == "/api/v1/sendmessages" and body[0]["action"] == 2 and body[0]["discussion_type"] == 1
    assert "to" not in body[0] and "Dana Levi" in body[0]["body"]


def test_reminder_payload_matches_the_l2c_capture():
    app = FakeApp()
    res = pa.reminder(63470107, "Dana@riverside.com", "Call back <today>", "2099-01-02T10:00:00+02:00", app)
    assert res == {"for": "Dana Levi", "at_utc": "2099-01-02T08:00:00.000Z", "text": "Call back <today>"}
    (method, path, body), = app.writes()
    assert path == "/api/v1/sendmessages"
    m = body[0]
    assert (m["action"], m["event"], m["discussion_type"]) == (2, 32, 3)
    assert m["participants"] == [{"id": 160785, "type": "user"}, {"id": 222, "type": "user"}]
    assert m["body"] == '<b class="bolder mentionItem">Dana Levi</b> Call back &lt;today&gt;'
    assert m["discussion_context"] == {"id": 63470107, "type": "candidate"} and m["time_to_send_message"] == "2099-01-02T08:00:00.000Z"


def test_reminder_refuses_past_naive_or_unknown_recruiter():
    for at in ("2020-01-01T10:00:00+02:00", "2099-01-02T10:00:00", "tomorrow"):
        with pytest.raises(sa.ScreeningError):
            pa.reminder(1, "dana@riverside.fm", "x", at, FakeApp())
    with pytest.raises(sa.ScreeningError):
        pa.reminder(1, "nobody@riverside.fm", "x", "2099-01-02T10:00:00+02:00", FakeApp())


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
