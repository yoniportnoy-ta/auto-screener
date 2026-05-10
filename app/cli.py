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
    """Hourly cron entrypoint. Walks all positions, scores new candidates, posts notes/tags."""
    log.info("scan-all: not yet wired up; will be implemented after scoring port lands")
    return 0


async def cmd_refresh_rubrics() -> int:
    log.info("refresh-rubrics: not yet wired up")
    return 0


async def cmd_refresh_comeet_session() -> int:
    log.info("refresh-comeet-session: not yet wired up")
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
