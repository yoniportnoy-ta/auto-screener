"""What this screener needs from the Anthropic SDK, asserted.

The 1.x SDK removed `temperature` from messages.create(). Not a 400 from the
API -- a TypeError raised before the request is built. An unpinned
`pip install -e .` picked it up on a routine redeploy on 2026-10-04 and every
score_candidate call raised for three days, visible only as an ERROR line in
a cron log.

A dependency that can take scoring down deserves a red test rather than a
quiet production outage, so these assert the call shapes the app actually
uses against whatever version is installed.
"""
from __future__ import annotations

import inspect

import anthropic
import pytest
from anthropic.resources.messages import Messages

# Every sampling value the app passes. Scoring's thresholds were measured at
# these settings, so losing the parameter is a recalibration, not a port.
USED_BY_APP = ("temperature",)


@pytest.mark.parametrize("param", USED_BY_APP)
def test_messages_create_still_takes_what_we_pass(param):
    sig = inspect.signature(Messages.create)
    assert param in sig.parameters, (
        f"The installed anthropic ({anthropic.__version__}) dropped "
        f"`{param}` from messages.create(). Every call site passing it now "
        f"raises TypeError before the request is sent. Either pin the SDK "
        f"below the version that removed it, or remove the parameter "
        f"everywhere and re-measure the screener's thresholds."
    )


def test_the_sdk_major_is_one_we_have_tested_against():
    """Pinned in pyproject; asserted here so an override in a container or a
    stale lockfile is a test failure rather than a silent change."""
    major = int(anthropic.__version__.split(".")[0])
    assert major == 0, (
        f"anthropic {anthropic.__version__} is a major the app has not been "
        f"ported to. 1.x removes sampling parameters, moves to httpx2, and "
        f"requires awaiting async .with_raw_response."
    )


def test_every_create_call_in_the_app_is_still_valid():
    """Bind the app's actual keyword sets against the installed signature.

    Catches a removed parameter at the exact call shape the app uses, which
    is what the version check above cannot do on its own.
    """
    sig = inspect.signature(Messages.create)
    call_shapes = [
        # app/scoring.py _single_pass -- the per-candidate score
        dict(model="m", max_tokens=2048, temperature=0.2, system=[],
             messages=[]),
        # app/scoring.py -- JD capability extraction
        dict(model="m", max_tokens=800, temperature=0.0, system="",
             messages=[]),
        # app/enrichment.py _claude_extract
        dict(model="m", max_tokens=2000, temperature=0.0, messages=[]),
        # app/rubrics.py _synthesise_rubric_text
        dict(model="m", max_tokens=4096, temperature=0.3, system="",
             messages=[]),
        # app/routes/api.py -- position class pick
        dict(model="m", max_tokens=40, temperature=0.0, messages=[]),
    ]
    for kwargs in call_shapes:
        # `self` is unbound here, so supply a placeholder for it.
        sig.bind(None, **kwargs)
