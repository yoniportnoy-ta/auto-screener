"""FastAPI entrypoint.

The web app exposes:
  - /          — recruiter UI (Jinja-rendered Index template)
  - /healthz   — health probe for Render
  - /api/*     — JSON endpoints called by the UI (positions, scan, score, feedback)
  - /webhook/* — placeholder for Comeet evaluation webhook (later)

Routes are organised under app/routes/ and registered here.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .logging_config import configure_logging

log = logging.getLogger(__name__)


def _run_pending_migrations() -> None:
    """Run alembic upgrade head on app startup so the DB schema matches the
    code we're booting. Idempotent — alembic skips already-applied revisions.

    This used to require manual SSH or a render.yaml startCommand hook; doing
    it inside the app process means new migrations apply automatically on
    every deploy, no infra change required.
    """
    try:
        from pathlib import Path
        from alembic import command
        from alembic.config import Config
        ini = Path(__file__).parent.parent / "alembic.ini"
        if not ini.exists():
            log.warning("alembic.ini not found at %s; skipping migrations", ini)
            return
        cfg = Config(str(ini))
        # Make sure alembic uses the same DATABASE_URL the app does.
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
        log.info("alembic upgrade head: OK")
    except Exception as exc:  # noqa: BLE001
        log.exception("alembic upgrade failed: %s", exc)


def _prewarm_unscreened_counts() -> None:
    """Keep the unscreened-counts cache warm so position dropdowns always
    show "(N)" badges without making the recruiter wait. Runs forever in a
    daemon thread, refreshing one position at a time on a slow cadence.

    History — what was tried and why this shape:
      - v1: prewarm-once at startup with ThreadPoolExecutor(12). OOM-killed
        the 512 MB starter instance.
      - v2: ThreadPoolExecutor(4) + 20s delay. Container still got
        health-check-restarted around the 5 min mark — the burst of parallel
        Comeet calls (each constructing httpx clients + parsing big JSON
        responses) starved /healthz long enough for Render to give up.
      - v3 (this): sequential, one position every few seconds, with a long
        initial wait so the app + alembic + first health checks finish first.
        No bursts, no parallelism, no OOM, /healthz stays responsive.

    Set SKIP_PREWARM=1 to disable entirely (useful for local dev).
    """
    import os
    import threading

    if os.environ.get("SKIP_PREWARM") == "1":
        log.info("prewarm: disabled via SKIP_PREWARM=1")
        return

    # Tunables. Conservative defaults; override via env if needed.
    initial_delay = int(os.environ.get("PREWARM_INITIAL_DELAY_SECONDS", "60"))
    per_position_gap = float(os.environ.get("PREWARM_PER_POSITION_GAP_SECONDS", "3"))

    def _runner():
        # Let the app fully boot first. /healthz needs to be answering 200s
        # before we start any Comeet work, otherwise Render restarts us.
        time.sleep(initial_delay)
        # One-shot prewarm. We used to loop here every 50 min to keep the
        # cache always warm, but the second iteration consistently killed
        # the container (likely httpx pool / fd leak between iterations).
        # One-shot is reliable; users after the 60-min TTL pay one slow
        # fetch (~90s) but the container stays up indefinitely.
        try:
            from .comeet_client import ComeetClient, candidate_in_allowed_step
            from .routes.api import _UNSCREENED_CACHE
            t0 = time.monotonic()
            with ComeetClient() as pub:
                positions = pub.list_open_positions()
                pos_uids = [str(p["uid"]) for p in positions if p.get("uid")]
            ok = 0
            fail = 0
            for u in pos_uids:
                try:
                    with ComeetClient() as client:
                        cands = client.list_candidates_for_position(u)
                    cnt = sum(1 for c in cands if c.get("uid") and candidate_in_allowed_step(c))
                    _UNSCREENED_CACHE[u] = (cnt, time.time())
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.info("prewarm: %s: %s", u, exc)
                    fail += 1
                # Stagger so we don't ever burst Comeet or steal the GIL
                # from /healthz responses for too long.
                time.sleep(per_position_gap)
            took = int(time.monotonic() - t0)
            log.info(
                "prewarm: warmed cache (%d ok, %d failed, %ds)",
                ok, fail, took,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("prewarm: failed: %s", exc)

    threading.Thread(target=_runner, name="prewarm-unscreened", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info(
        "starting auto-screener env=%s log_level=%s scoring_v2=%s auto_tag=%s",
        settings.app_env, settings.log_level, settings.scoring_use_v2, settings.auto_tag_enabled,
    )
    _run_pending_migrations()
    _prewarm_unscreened_counts()
    yield
    log.info("shutting down")


app = FastAPI(
    title="Auto Screener",
    description="Comeet candidate auto-screener with Claude scoring + recruiter feedback learning.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
)


# CORS — the Chrome extension content script runs on app.comeet.co and calls
# our /api/extension/* endpoints. Chrome also issues an Origin: chrome-extension://<id>
# preflight, which we allow via regex. Token auth gates the actual writes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.comeet.co"],
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "%s %s -> %d (%dms)",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response


@app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
async def healthz() -> str:
    """Render's health-check endpoint. Cheap; no DB hit."""
    return "ok"


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness probe — checks DB connectivity."""
    from sqlalchemy import text
    from .db import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return JSONResponse({"ready": True})
    except Exception as exc:  # noqa: BLE001
        log.exception("readiness check failed")
        return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)


# ─── Routes ──────────────────────────────────────────────────────────────────
# Will be wired as we port modules. Stubbed for now so the deployment works.
try:
    from .routes import api as api_routes  # noqa: F401
    app.include_router(api_routes.router, prefix="/api", tags=["api"])
except ImportError:
    log.warning("routes/api.py not yet implemented")

try:
    from .routes import ui as ui_routes  # noqa: F401
    app.include_router(ui_routes.router, tags=["ui"])
except ImportError:
    log.warning("routes/ui.py not yet implemented")


# Static assets (CSS / JS extracted from the Apps Script Index.html, when ported).
try:
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
except RuntimeError:
    # static/ dir may not exist yet during early scaffolding
    log.debug("app/static missing; static mount skipped")
