"""CV-screen decisions for the Riverside Screening plugin.

The Comeet payloads below are pinned to what the recruiter app sent in the HAR
captures (2026-10-20). Changing them can send rejection emails or write to the
wrong step, so a change here should come from a new capture, not a guess.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import screening_actions as sa
from app.config import settings
from app.routes import screening as routes

REASONS = [
    {"id": 33272, "reason": "Incomplete application", "status": 1, "order": 1, "company_id": 1641},
    {"id": 33278, "reason": "Not Interested in Position", "status": 2, "order": 1, "company_id": 1641},
    {"id": 33273, "reason": "Doesn't Meet Minimum Qualifications", "status": 1, "order": 2, "company_id": 1641},
    {"id": 33296, "reason": "Professional gaps", "status": 1, "order": 50, "company_id": 1641},
]
STEPS = [
    {"id": 1, "name": "Application", "is_completed": True},
    {"id": 832200946, "name": "CV Screen / Recruiter", "is_completed": False},
    {"id": 832200988, "name": "Send rejection", "is_completed": False},
]


class FakeApp:
    def __init__(self, steps=STEPS):
        self.calls = []
        self.steps = steps

    def _request(self, method, path, *, json_body=None, **_):
        self.calls.append((method, path, json_body))
        if "disposition-types" in path:
            return 200, REASONS
        if path.endswith("/steps"):
            return 200, self.steps
        if path.endswith("/submit?parent_id=63429397"):
            return 200, {"id": 70996612, "is_completed": True}
        if path == "/api/v1/users":
            return 200, [{"id": 131480, "email": "yoni.portnoy@riverside.fm"}, {"id": 160785, "email": "comeetadmin@riverside.fm"}]
        if path == "/api/v1/sendmessages":
            return 200, [{"id": 56276989, "type": "discussion"}]
        raise AssertionError(f"unexpected {method} {path}")

    def get_candidate(self, uid):
        return {"id": 63429397}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(sa, "_reasons_cache", None)
    monkeypatch.setattr(sa, "_bot_user_id", None)
    monkeypatch.setattr(settings, "comeet_app_email", "comeetadmin@riverside.fm")
    monkeypatch.setattr(settings, "screener_step_substrings", "cv screen / recruiter")


def test_only_company_side_reasons_in_comeet_order():
    assert sa.reject_reasons(FakeApp()) == [
        {"id": 33272, "reason": "Incomplete application"},
        {"id": 33273, "reason": "Doesn't Meet Minimum Qualifications"},
        {"id": 33296, "reason": "Professional gaps"},
    ]


def test_pass_payload_matches_the_capture():
    app = FakeApp()
    sa.submit_decision(63429397, 832200946, "pass", None, app)
    assert app.writes() == [("POST", "/api/v1/candidates/63429397/steps/832200946/submit?parent_id=63429397",
                             {"proceed": True, "further_review": ""})]


def test_reject_payload_matches_the_capture():
    app = FakeApp()
    sa.submit_decision(63429397, 832200946, "reject", 33273, app)
    assert app.writes() == [("POST", "/api/v1/candidates/63429397/steps/832200946/submit?parent_id=63429397",
                             {"proceed": False, "further_review": "", "disposition_type_id": 33273})]


def test_reject_needs_a_company_side_reason():
    app = FakeApp()
    for bad in (None, 33278, 99999):  # missing, candidate-side, unknown
        with pytest.raises(sa.ScreeningError):
            sa.submit_decision(63429397, 832200946, "reject", bad, app)
    assert app.writes() == []


def test_comment_is_internal_never_an_email():
    app = FakeApp()
    sa.post_comment(63429397, "Passed CV screen.", app)
    (method, path, body), = app.writes()
    assert (method, path) == ("POST", "/api/v1/sendmessages")
    assert body[0]["action"] == 2 and body[0]["discussion_context"] == {"id": 63429397, "type": "candidate"}
    assert body[0]["participants"] == [{"id": 160785, "type": "user"}]  # the bot itself


def test_current_step_must_be_the_cv_screen():
    assert sa.current_cv_screen_step(63429397, FakeApp())["id"] == 832200946
    moved_on = [dict(s, is_completed=True) if s["id"] == 832200946 else s for s in STEPS]
    with pytest.raises(sa.ScreeningError, match="no longer at CV screen"):
        sa.current_cv_screen_step(63429397, FakeApp(moved_on))
    done = [dict(s, is_completed=True) for s in STEPS]
    with pytest.raises(sa.ScreeningError, match="no open step"):
        sa.current_cv_screen_step(63429397, FakeApp(done))


def test_comment_text_names_the_recruiter():
    assert sa.comment_text("Noga Kaufman", "pass", None, "") == "Passed CV screen. Screened by Noga Kaufman with Riverside Screening."
    t = sa.comment_text("Noga Kaufman", "reject", "Professional gaps", "No B2B sales")
    assert t.startswith("Rejected at CV screen (Professional gaps). Screened by Noga Kaufman") and t.endswith("\nNo B2B sales")


# ─── Routes ──────────────────────────────────────────────────────────────────
CANDS = [
    {"uid": "AB.1", "first_name": "Yoni", "last_name": "Test", "URL": "https://app.comeet.co/app/req/473976/can/63429397",
     "time_created": "2026-10-19T10:00:00Z", "status": "In progress", "resume": {"url": "https://x/cv.pdf"},
     "current_steps": [{"name": "CV Screen / Recruiter", "type": "Go/No-go"}], "source": {"name": "LinkedIn"}},
    {"uid": "AB.2", "first_name": "Old", "last_name": "One", "URL": "https://app.comeet.co/app/req/473976/can/2",
     "time_created": "2026-10-01T10:00:00Z", "status": "In progress", "resume": {},
     "current_steps": [{"name": "CV Screen / Recruiter"}]},
    {"uid": "AB.3", "first_name": "Further", "last_name": "Along", "URL": "https://app.comeet.co/app/req/473976/can/3",
     "time_created": "2026-09-01T10:00:00Z", "status": "In progress", "current_steps": [{"name": "Interview / Manager"}]},
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "screener_gateway_token", "gw-secret")
    monkeypatch.setattr(routes, "_position_candidates", lambda pos, fresh=False: CANDS)
    fake = FakeApp()
    monkeypatch.setattr(sa, "app_client", lambda: fake)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/screening")
    c = TestClient(app)
    c.fake = fake
    return c


H = {"X-Gateway-Token": "gw-secret"}


def test_routes_need_the_gateway_token(client):
    assert client.get("/api/screening/queue?position_uid=P1").status_code == 401
    assert client.get("/api/screening/queue?position_uid=P1", headers={"X-Gateway-Token": "nope"}).status_code == 401


def test_queue_is_cv_screen_only_oldest_first(client):
    r = client.get("/api/screening/queue?position_uid=P1", headers=H).json()
    assert r["waiting"] == 2 and [c["uid"] for c in r["candidates"]] == ["AB.2", "AB.1"]
    assert r["candidates"][1]["numeric_id"] == 63429397 and r["candidates"][1]["has_cv"]


def test_reject_writes_decision_then_comment(client):
    body = {"position_uid": "P1", "candidate_uid": "AB.1", "decision": "reject", "reason_id": 33273,
            "comment": "No real closing experience", "recruiter": {"name": "Noga Kaufman", "email": "noga@riverside.fm"}}
    r = client.post("/api/screening/decision", json=body, headers=H).json()
    assert r["ok"] and r["comment_posted"] and r["reason"] == "Doesn't Meet Minimum Qualifications"
    writes = client.fake.writes()
    assert writes[0][2] == {"proceed": False, "further_review": "", "disposition_type_id": 33273}
    assert writes[1][1] == "/api/v1/sendmessages" and "Screened by Noga Kaufman" in writes[1][2][0]["body"]
    assert len(writes) == 2  # nothing else: no email step


def test_decision_refused_for_someone_not_at_cv_screen(client):
    client.fake.steps = [dict(s, is_completed=True) if s["id"] == 832200946 else s for s in STEPS]
    body = {"position_uid": "P1", "candidate_uid": "AB.1", "decision": "pass", "recruiter": {"name": "Noga"}}
    r = client.post("/api/screening/decision", json=body, headers=H)
    assert r.status_code == 409 and client.fake.writes() == []


def test_decision_refused_for_unknown_candidate(client):
    body = {"position_uid": "P1", "candidate_uid": "ZZ.9", "decision": "pass", "recruiter": {"name": "Noga"}}
    assert client.post("/api/screening/decision", json=body, headers=H).status_code == 404


# ─── Rejection email (from the send-rejection capture) ───────────────────────
# Order as Comeet returns it after a reject: the added notification step comes AFTER
# the next interview step, which is still open; an older notification is already done.
NOTIFY_STEPS = [
    {"id": 832200946, "name": "CV Screen / Recruiter", "is_completed": True, "step_type": 1},
    {"id": 832200947, "name": "Video Interview/Recruiter", "is_completed": False, "step_type": 2},
    {"id": 832200988, "name": "Send notification", "is_completed": True, "step_type": 5},
    {"id": 832271328, "name": "Send notification", "is_completed": False, "step_type": 5},
]
CANDIDATE_EMAIL = {"subject": "Your Application for Full Stack Engineer at Riverside", "body": "<p>Dear test,</p>",
                   "to": [{"email": "test@example.com", "type": "candidate", "id": 63429397}], "cc": [], "bcc": [],
                   "action": 2, "event": 1, "template_id": 113768, "delay_hours": 24.0, "is_delayed": True,
                   "is_delayed_until_business_hours": True, "discussion_type": 2}
TEAM_NOTICE = {"subject": "Candidate test for Full Stack Engineer", "body": "<p>fyi</p>", "to": [{"type": None}],
               "action": 2, "event": 1, "template_id": 113777, "delay_hours": 0.0, "is_delayed": False}


class EmailApp(FakeApp):
    def __init__(self, status=3, steps=NOTIFY_STEPS, msgs=(CANDIDATE_EMAIL, TEAM_NOTICE)):
        super().__init__(steps)
        self.status, self.msgs = status, [dict(m) for m in msgs]

    def get_candidate(self, uid):
        return {"id": 63429397, "status": self.status}

    def _request(self, method, path, *, json_body=None, **kw):
        if "/sendmessages?parent_id=" in path:
            self.calls.append((method, path, json_body))
            return 200, self.msgs
        if path.endswith("/steps/832271328/submit?parent_id=63429397"):
            self.calls.append((method, path, json_body))
            return 200, {"id": 71003831, "is_completed": True}
        return super()._request(method, path, json_body=json_body, **kw)


def test_rejection_email_sends_only_the_candidate_email():
    app = EmailApp()
    out = sa.send_rejection_email(63429397, app)
    (method, path, body), = app.writes()
    assert (method, path) == ("POST", "/api/v1/candidates/63429397/steps/832271328/submit?parent_id=63429397")
    assert body["proceed"] is True and body["further_review"] == ""
    email, notice = body["sendmessages"]
    assert email["send"] is True and email["index"] == 1 and email["missing_email"] is False
    assert email["delay_hours"] == 24.0 and email["template_id"] == 113768   # Comeet's own template and delay
    assert notice["send"] is False and notice["subject"] == TEAM_NOTICE["subject"]
    assert out == {"subject": CANDIDATE_EMAIL["subject"], "to": "test@example.com", "delay_hours": 24.0, "business_hours": True}


def test_rejection_email_refused_when_not_rejected_or_not_waiting():
    with pytest.raises(sa.ScreeningError, match="isn't rejected"):
        sa.send_rejection_email(63429397, EmailApp(status=0))
    done = [dict(s, is_completed=True) if s["step_type"] == 5 else s for s in NOTIFY_STEPS]
    with pytest.raises(sa.ScreeningError, match="no rejection email waiting"):
        sa.send_rejection_email(63429397, EmailApp(steps=done))
    for msgs in ((TEAM_NOTICE,), (CANDIDATE_EMAIL, CANDIDATE_EMAIL)):
        with pytest.raises(sa.ScreeningError, match="exactly one"):
            sa.send_rejection_email(63429397, EmailApp(msgs=msgs))
    no_address = dict(CANDIDATE_EMAIL, to=[{"email": "", "type": "candidate"}])
    app = EmailApp(msgs=(no_address, TEAM_NOTICE))
    with pytest.raises(sa.ScreeningError, match="no email address"):
        sa.send_rejection_email(63429397, app)
    assert app.writes() == []


def test_decision_without_the_email_flag_never_emails(client):
    body = {"position_uid": "P1", "candidate_uid": "AB.1", "decision": "reject", "reason_id": 33273,
            "recruiter": {"name": "Noga Kaufman"}}
    r = client.post("/api/screening/decision", json=body, headers=H).json()
    assert r["rejection_email"] is None
    assert not any("sendmessages?parent_id" in c[1] or "832271328" in c[1] for c in client.fake.calls)
