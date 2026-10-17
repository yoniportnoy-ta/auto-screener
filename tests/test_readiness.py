"""The gate that decides whether auto-screen may be switched on.

The rule lives in one pure function precisely so this file can pin it: the
Slack menu and the web UI both read it, and they must not disagree about what
"ready" means.
"""
from app.routes.api import MIN_TEACHING_RATINGS, assess_readiness


def r(**kw):
    base = dict(has_brief=True, ratings=MIN_TEACHING_RATINGS,
                class_id="backend", tau=None, enabled=False)
    return assess_readiness(**{**base, **kw})


def test_full_teaching_round_with_a_class_is_ready():
    v = r()
    assert v["ready"] and v["missing"] == []


def test_no_brief_is_not_ready():
    assert "brief" in r(has_brief=False)["missing"]


def test_short_of_a_full_round_says_how_many_are_left():
    v = r(ratings=4)
    assert not v["ready"]
    assert f"{MIN_TEACHING_RATINGS - 4} more rated CVs" in v["missing"]


def test_one_short_is_still_not_ready():
    assert not r(ratings=MIN_TEACHING_RATINGS - 1)["ready"]


def test_more_than_a_round_is_fine():
    assert r(ratings=40)["ready"]


def test_a_taught_position_with_no_class_is_not_ready():
    """set_auto_screen_enabled raises without a class, so the gate must catch
    it here. Four of the five taught positions are in exactly this state."""
    v = r(class_id=None)
    assert not v["ready"]
    assert "position class" in v["missing"]


def test_missing_reasons_are_reported_together():
    v = r(has_brief=False, ratings=0, class_id=None)
    assert len(v["missing"]) == 3


def test_calibration_is_reported_but_is_not_required():
    """A measured threshold is better; waiting for one would block every
    position, since only one has ever been calibrated."""
    assert r(tau=None)["ready"] and r(tau=None)["calibrated"] is False
    assert r(tau=33.0)["calibrated"] is True


def test_already_on_is_still_reported_as_ready():
    v = r(enabled=True)
    assert v["ready"] and v["enabled"] is True
