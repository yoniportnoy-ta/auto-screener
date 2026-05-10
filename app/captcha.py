"""2captcha helper — solves the reCAPTCHA on Comeet's /signin form.

Direct port of `_solve_recaptcha_for_login` from referral-bot's comeet_app_client.py.
Same submit-then-poll pattern; same v3 + action + min_score config (matches the
parameters the referral bot is already running successfully against Comeet).
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import settings

log = logging.getLogger(__name__)


class CaptchaError(RuntimeError):
    """Raised when 2captcha rejects the submission or polling times out."""


def solve_recaptcha_for_login(
    *,
    api_key: str | None = None,
    site_key: str | None = None,
    page_url: str = "https://app.comeet.co/",
    enterprise: bool = False,
    action: str | None = None,
    min_score: float | None = None,
    poll_interval_s: float = 5.0,
    timeout_s: float = 240.0,
) -> str:
    """Submit a reCAPTCHA job to 2captcha, poll until ready, return the solved token.

    Raises CaptchaError on failure (submit rejected, polling timeout, malformed
    response). Defaults to the same v3+login+min_score 0.3 config the referral
    bot uses in production.
    """
    api_key = api_key or settings.captcha_api_key
    site_key = site_key or settings.recaptcha_site_key
    action = action or settings.recaptcha_action
    min_score = min_score if min_score is not None else settings.recaptcha_min_score

    if not api_key:
        raise CaptchaError("CAPTCHA_API_KEY not configured")
    if not site_key:
        raise CaptchaError("RECAPTCHA_SITE_KEY not configured")

    submit_payload: dict[str, str | int | float] = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    if enterprise:
        submit_payload["enterprise"] = 1
    else:
        submit_payload["version"] = "v3"
        submit_payload["action"] = action
        submit_payload["min_score"] = min_score

    log.info(
        "2captcha submit: site_key=%s pageurl=%s enterprise=%s action=%s min_score=%s",
        site_key, page_url, enterprise, action, min_score,
    )
    start = time.time()
    with httpx.Client(timeout=20.0) as client:
        try:
            sub = client.post("https://2captcha.com/in.php", data=submit_payload)
        except httpx.HTTPError as exc:
            raise CaptchaError(f"submit network error: {exc}") from exc

        try:
            sub_json = sub.json()
        except Exception as exc:  # noqa: BLE001
            raise CaptchaError(f"submit non-JSON HTTP {sub.status_code}: {sub.text[:300]}") from exc

        if sub_json.get("status") != 1:
            raise CaptchaError(f"submit rejected: {sub_json.get('request') or sub_json}")

        captcha_id = str(sub_json.get("request"))
        log.info("2captcha accepted; id=%s polling…", captcha_id)

        deadline = start + timeout_s
        while time.time() < deadline:
            time.sleep(poll_interval_s)
            try:
                poll = client.get(
                    "https://2captcha.com/res.php",
                    params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
                )
            except httpx.HTTPError as exc:
                log.warning("2captcha poll error: %s", exc)
                continue
            try:
                poll_json = poll.json()
            except Exception:  # noqa: BLE001
                log.warning("2captcha poll non-JSON HTTP %d: %s", poll.status_code, poll.text[:200])
                continue
            if poll_json.get("status") == 1:
                token = str(poll_json.get("request") or "")
                log.info("2captcha solved in %.1fs (id=%s, len=%d)", time.time() - start, captcha_id, len(token))
                return token
            req = str(poll_json.get("request") or "")
            if req != "CAPCHA_NOT_READY":
                raise CaptchaError(f"poll rejected: {req}")

    raise CaptchaError(f"timeout after {timeout_s}s (captcha_id={captcha_id})")
