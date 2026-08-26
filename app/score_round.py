"""Run a scoring round for one position.

Reads the position's LEARNED brief (position_briefs — written by the Comeet
Helper teaching flow), lists candidates currently in the CV-screen step, scores
the ones not yet scored at the current brief version, and writes results to
`candidate_scores`. A position MUST be taught first (phase ①) — no brief, no round.

Cost note: this calls Claude per candidate on the SHARED Anthropic account.
Respect the daily cap; pass --limit to bound a run. Prefer running it off-peak /
via a capped cron once wired.

CLI:  python -m app.score_round <position_uid> [--limit N]
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .db import engine
from .comeet_client import ComeetClient, candidate_in_allowed_step, candidate_full_name
from .screen_judge import score_candidate

log = logging.getLogger(__name__)

_DDL = (
    "CREATE TABLE IF NOT EXISTS candidate_scores ("
    " candidate_uid text, position_uid text, candidate_name text, fit int,"
    " recommendation text, confidence real, dims_json text, rationale text,"
    " model text, brief_built_from_n int, in_tokens int, out_tokens int,"
    " scored_at timestamptz default now(),"
    " PRIMARY KEY (candidate_uid, position_uid))"
)
_UPSERT = (
    "INSERT INTO candidate_scores (candidate_uid, position_uid, candidate_name, fit,"
    " recommendation, confidence, dims_json, rationale, model, brief_built_from_n,"
    " in_tokens, out_tokens) VALUES (:candidate_uid,:position_uid,:candidate_name,:fit,"
    " :recommendation,:confidence,:dims_json,:rationale,:model,:brief_built_from_n,"
    " :in_tokens,:out_tokens)"
    " ON CONFLICT (candidate_uid, position_uid) DO UPDATE SET fit=EXCLUDED.fit,"
    " recommendation=EXCLUDED.recommendation, confidence=EXCLUDED.confidence,"
    " dims_json=EXCLUDED.dims_json, rationale=EXCLUDED.rationale, model=EXCLUDED.model,"
    " brief_built_from_n=EXCLUDED.brief_built_from_n, scored_at=now()"
)


def get_brief(position_uid: str) -> Optional[Dict[str, Any]]:
    with engine.connect() as c:
        try:
            row = c.execute(text(
                "SELECT position_name, brief, built_from_n FROM position_briefs WHERE position_uid=:p"),
                {"p": position_uid}).first()
        except Exception as exc:  # noqa: BLE001 — table may not exist until first teaching
            log.info("get_brief: %s", exc)
            return None
    if not row:
        return None
    return {"position_name": row[0], "brief": row[1], "built_from_n": row[2]}


def _already_scored(position_uid: str) -> set:
    with engine.begin() as c:
        c.execute(text(_DDL))
        rows = c.execute(text(
            "SELECT candidate_uid FROM candidate_scores WHERE position_uid=:p"), {"p": position_uid}).all()
    return {r[0] for r in rows}


def run_round(position_uid: str, limit: Optional[int] = None) -> Dict[str, Any]:
    brief = get_brief(position_uid)
    if not brief:
        return {"error": "no_brief",
                "message": f"No learned brief for {position_uid} — teach it first "
                           f"(Comeet Helper: `screen <position>`)."}

    with ComeetClient() as cc:
        position = cc.get_position(position_uid)
        cands = cc.list_candidates_for_position(position_uid)

    in_step = [c for c in cands
               if c.get("uid") and not c.get("deleted")
               and candidate_in_allowed_step(c)
               and isinstance(c.get("resume"), dict) and (c["resume"].get("url"))]
    done = _already_scored(position_uid)
    todo = [c for c in in_step if str(c["uid"]) not in done]
    if limit:
        todo = todo[:limit]

    log.info("score_round %s: %d in-step, %d already scored, scoring %d",
             position_uid, len(in_step), len(done), len(todo))

    scored, skipped, tin, tout = 0, 0, 0, 0
    buckets = {"advance": 0, "borderline": 0, "reject": 0}
    for c in todo:
        try:
            res = score_candidate(c, position, brief["brief"])
        except Exception as exc:  # noqa: BLE001
            log.warning("score_round: %s: %s", c.get("uid"), exc)
            skipped += 1
            continue
        if not res:
            skipped += 1
            continue
        row = {"candidate_uid": str(c["uid"]), "position_uid": position_uid,
               "candidate_name": candidate_full_name(c), "fit": res["fit"],
               "recommendation": res["recommendation"], "confidence": res["confidence"],
               "dims_json": json.dumps(res["dims"]), "rationale": res["rationale"],
               "model": res["model"], "brief_built_from_n": brief["built_from_n"],
               "in_tokens": res["in_tokens"], "out_tokens": res["out_tokens"]}
        with engine.begin() as conn:
            conn.execute(text(_UPSERT), row)
        scored += 1
        tin += res["in_tokens"]; tout += res["out_tokens"]
        buckets[res["recommendation"]] = buckets.get(res["recommendation"], 0) + 1

    cost = tin * 2.0 / 1_000_000 + tout * 10.0 / 1_000_000  # Sonnet-5 intro rate
    return {"position_uid": position_uid, "position_name": brief["position_name"],
            "in_step": len(in_step), "already_scored": len(done), "scored": scored,
            "skipped": skipped, "recommend": buckets, "cost_usd": round(cost, 2)}


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m app.score_round <position_uid> [--limit N]"); sys.exit(2)
    pos = sys.argv[1]
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    t0 = time.time()
    out = run_round(pos, limit=limit)
    out["seconds"] = int(time.time() - t0)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
