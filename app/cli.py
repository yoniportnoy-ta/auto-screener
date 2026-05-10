"""CLI entrypoints used by the Render cron job (and for one-off ops).

Usage:
    python -m app.cli scan-all              # iterate all open positions, score new candidates
    python -m app.cli refresh-rubrics       # regenerate learned rubrics for all classes
    python -m app.cli refresh-comeet-session  # force-relogin to app.comeet.co
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .logging_config import configure_logging

log = logging.getLogger(__name__)


async def cmd_scan_all() -> int:
    """Hourly cron entrypoint. Walks open positions, scores new candidates, applies rating tags."""
    from .automation import run_autoscan

    result = run_autoscan()
    if result.error:
        log.error("scan-all: %s", result.error)
        return 1
    log.info(
        "scan-all done: positions=%d candidates_scored=%d tags_applied=%d duration=%.1fs",
        result.positions_scanned, result.candidates_scored, result.tags_applied, result.duration_s,
    )
    return 0


async def cmd_refresh_rubrics() -> int:
    """Force-regenerate learned rubrics for every class with enough feedback."""
    from .position_classes import list_all_classes
    from .rubrics import refresh_learned_rubric

    refreshed = 0
    for cls in list_all_classes():
        result = refresh_learned_rubric(cls["id"], cls["name"])
        if result.get("ok"):
            log.info("refreshed rubric for %s (samples=%s)", cls["id"], result.get("feedback_count"))
            refreshed += 1
        else:
            log.debug("rubric refresh skipped for %s: %s", cls["id"], result.get("error"))
    log.info("refresh-rubrics done: %d classes refreshed", refreshed)
    return 0


async def cmd_refresh_comeet_session() -> int:
    """Force a fresh app.comeet.co login (drops the cached session first)."""
    from .comeet_app_client import ComeetAppClient, clear_session

    clear_session()
    client = ComeetAppClient()
    client.login()
    summary = client.session_summary()
    log.info("refresh-comeet-session done: %s", summary)
    return 0


COMMANDS = {
    "scan-all": cmd_scan_all,
    "refresh-rubrics": cmd_refresh_rubrics,
    "refresh-comeet-session": cmd_refresh_comeet_session,
}


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("command", choices=COMMANDS.keys())
    args = parser.parse_args()
    return asyncio.run(COMMANDS[args.command]())


if __name__ == "__main__":
    sys.exit(main())
