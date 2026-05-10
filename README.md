# Auto Screener — Python / Render port

A Python rewrite of the Apps Script auto-screener. Scores Comeet candidates with Claude,
learns from recruiter feedback, applies rating tags back to Comeet via the internal API.

## Architecture

```
[Recruiter] → FastAPI UI (Jinja)            → Claude scoring → Postgres feedback
                  ↓                                              ↓
              Comeet public API (api.comeet.co, JWT)        Learned rubrics + anchors
                  ↓
              Comeet internal API (app.comeet.co, cookie)   ← session refresh via 2captcha
                  ↓
              Tag candidate (rating tag)
```

## Local development

```bash
git clone <this repo>
cd auto-screener-py

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# fill in COMEET_*, ANTHROPIC_API_KEY, CAPTCHA_API_KEY, COMEET_APP_*, DATABASE_URL

# spin up Postgres locally (or point DATABASE_URL at a remote one)
createdb auto_screener_dev
alembic upgrade head

uvicorn app.main:app --reload
# -> http://localhost:8000
```

## Render deploy

1. Push this repo to GitHub.
2. In Render: New → Blueprint → point at the repo root. Render reads `render.yaml`,
   provisions the Postgres database, and queues the web service + cron job builds.
3. After the first build, set the secret env vars in the dashboard:
   - `COMEET_API_KEY`, `COMEET_API_SECRET` — public Recruit API
   - `COMEET_APP_EMAIL`, `COMEET_APP_PASSWORD` — **dedicated low-permission user, not the same as the referral bot's**
   - `CAPTCHA_API_KEY` — 2captcha
   - `ANTHROPIC_API_KEY`
   - `SCREENER_API_TOKEN` — `python -c "import secrets; print(secrets.token_urlsafe(32))"`
4. SSH into the web service and run `alembic upgrade head` once. (Or push a small
   `release.sh` script if Render's release-command feature gets used.)

## Module map

| File | Replaces | Purpose |
|------|----------|---------|
| `app/main.py` | doGet / doPost in Code.gs | FastAPI entry, route registration |
| `app/config.py` | `SCREENER_CONFIG` + script properties | Settings via Pydantic |
| `app/db.py` + `app/models.py` | Locks/feedback sheets | Postgres connection + ORM |
| `app/comeet_client.py` | `Code.gs` Comeet helpers | Public API client (JWT) |
| `app/comeet_app_client.py` | (new — adapted from referral-bot) | Internal API client (cookie + 2captcha) |
| `app/scoring.py` | `ScoringV2.gs` | Claude scoring with criteria + rubric |
| `app/anchors.py` | `SimilarPastCandidates.gs` | Per-candidate calibration anchors |
| `app/rubrics.py` | `LearnedRubrics.gs` | Auto-synthesised class rubrics |
| `app/debug_log.py` | `DebugLog.gs` | Debug capture per scoring call |
| `app/tagging.py` | (new) | Apply rating tags via internal API |
| `app/feedback.py` | feedback sheet writes | Save/read recruiter feedback |
| `app/automation.py` | `AutomationScan.gs` | Scheduled scan logic |
| `app/cli.py` | manual triggers from editor | CLI for cron + ops |
| `app/routes/*` | doGet HTML / `google.script.run` | UI + JSON API |
| `app/templates/index.html` | `Index.html` | Recruiter UI (Jinja) |

## Status

Phase 1 (current): scaffold + DB schema (in progress)

Phase 2: port public + internal API clients

Phase 3: port scoring engine (criteria, rubrics, anchors, calibration)

Phase 4: port web UI

Phase 5: scheduled scan + tagging integration

Phase 6: deploy + smoke test
