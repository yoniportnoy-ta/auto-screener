"""Phase 1 corpus miner — screen-decision labels from Comeet history.

READ-ONLY against Comeet (public Recruit API). Writes a labeled corpus into the
`corpus_screen_labels` table in our own DB. NO Claude calls.

Label = "did the recruiter advance the candidate PAST the CV Screen / Recruiter
Go/No-go?"  1 = advanced past screen, 0 = rejected at screen, NULL = excluded
(still in screen / withdrawn / rejected before any screen / undecided).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from app.comeet_client import ComeetClient, candidate_full_name
from app.db import engine

ADVANCE_TYPES = {"Phone Interview", "Video Interview"}


def _scrub(row: dict) -> dict:
    """Postgres text columns reject NUL (0x00). A few candidate records carry
    them (bad PDF-extracted names, mangled source strings), and one poisons the
    whole batch insert, so strip them at the edge."""
    return {k: (v.replace("\x00", "") if isinstance(v, str) else v) for k, v in row.items()}

# Per-position mine ledger. Lets the miner skip closed reqs it has already read
# (they never change) while still re-reading every open one.
_POSITIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS corpus_positions ("
    " position_uid text PRIMARY KEY, position_name text, status text,"
    " mined_at timestamptz default now())"
)


def _is_cv_screen(step: dict) -> bool:
    return "cv screen" in (step.get("name") or "").lower()


def _is_advance_step(step: dict) -> bool:
    """A completed step that means the candidate moved PAST the CV screen.
    Excludes 'Send notification' (often the rejection email)."""
    typ = (step.get("type") or "")
    name = (step.get("name") or "").lower()
    if typ in ADVANCE_TYPES:
        return True
    return any(k in name for k in ("interview", "phone screen", "technical", "hiring manager", "hm "))


def classify(c: dict) -> tuple[str, int | None, dict]:
    if c.get("deleted"):
        return ("deleted", None, {})
    status = (c.get("status") or "").strip()
    completed = c.get("completed_steps") or []
    current = c.get("current_steps") or []

    cv_completed = [s for s in completed if _is_cv_screen(s)]
    cv_current = [s for s in current if _is_cv_screen(s)]
    cv_time = cv_completed[0].get("time_completed") if cv_completed else None
    cv_assignee = None
    if cv_completed:
        ass = cv_completed[0].get("assignees") or []
        if ass:
            cv_assignee = (ass[0].get("email") or "").strip() or None

    post = [
        s for s in completed
        if _is_advance_step(s) and (s.get("time_completed") or "") > (cv_time or "")
    ]

    meta = {
        "status": status, "cv_time": cv_time, "cv_assignee": cv_assignee,
        "n_completed": len(completed), "has_post": bool(post),
    }

    if cv_current and not cv_completed:
        return ("in_screen", None, meta)
    if cv_completed:
        if post:
            return ("passed_screen" + ("_rej_later" if status == "Rejected" else ""), 1, meta)
        if status == "Rejected":
            return ("rejected_at_screen", 0, meta)
        if status in ("Hired", "In progress", "On hold"):
            return ("passed_screen_awaiting", 1, meta)
        if status == "Withdrawn":
            return ("withdrawn_at_screen", None, meta)
        return ("screen_done_unknown_status", None, meta)
    # no CV-screen step present
    if status == "Rejected":
        return ("rejected_pre_screen", None, meta)
    if status == "Withdrawn":
        return ("withdrawn_pre_screen", None, meta)
    if status == "":
        return ("no_step_blank", None, meta)
    return ("no_screen_step_other", None, meta)


def main() -> None:
    # 1) Load AI scores once (free features) — latest row per candidate.
    ai_by_uid: dict[str, dict] = {}
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT ON (candidate_uid) candidate_uid, final_rating, "
            "dim_company_domain, dim_profession_domain, dim_company_tier, "
            "dim_career_progression, dim_location_match, dim_university_tier "
            "FROM debug_scoring WHERE candidate_uid IS NOT NULL "
            "ORDER BY candidate_uid, timestamp DESC"
        )).mappings().all()
    for r in rows:
        ai_by_uid[r["candidate_uid"]] = dict(r)
    print(f"[ai] loaded {len(ai_by_uid)} scored candidates from debug_scoring", file=sys.stderr)

    # 2) Enumerate positions — OPEN *and* CLOSED.
    #
    # Closed reqs are where the completed hiring outcomes live (screened ->
    # interviewed -> offered -> hired). Mining only open ones kept ~5% of the
    # catalogue and silently erased a position's entire screening history the
    # day it was filled — i.e. exactly when it became most informative.
    full_refresh = "--full" in sys.argv
    include_closed = "--all" in sys.argv
    with ComeetClient() as client:
        positions = client.list_positions() if include_closed else client.list_positions(status="open")
    pos_meta = {
        str(p["uid"]): {"name": p.get("name") or "", "status": (p.get("status") or "").lower()}
        for p in positions if p.get("uid")
    }
    pos_names = {u: m["name"] for u, m in pos_meta.items()}
    n_open = sum(1 for m in pos_meta.values() if m["status"] == "open")
    print(f"[pos] {len(pos_meta)} positions ({n_open} open, {len(pos_meta) - n_open} closed)"
          f"{'' if include_closed else ' — active only; pass --all to include closed reqs'}",
          file=sys.stderr)

    # Incremental plan. A closed req is immutable, so it only needs mining once;
    # open ones are re-read every run. A position whose status changed since its
    # last mine is re-read too — that is the open->closed transition, which
    # carries the final round of decisions.
    with engine.begin() as conn:
        conn.execute(text(_POSITIONS_DDL))
        prior = {r[0]: r[1] for r in conn.execute(
            text("SELECT position_uid, status FROM corpus_positions")).all()}

    todo_pos = {
        uid: m for uid, m in pos_meta.items()
        if full_refresh or uid not in prior or prior[uid] != m["status"] or m["status"] == "open"
    }
    print(f"[pos] mining {len(todo_pos)}, skipping {len(pos_meta) - len(todo_pos)} "
          f"already-mined closed reqs{' (--full: forcing all)' if full_refresh else ''}",
          file=sys.stderr)

    corpus: list[dict] = []
    cat_counter: Counter = Counter()
    per_pos = defaultdict(lambda: {"n": 0, "pos": 0, "neg": 0})
    per_rec = defaultdict(lambda: {"screened": 0, "pos": 0, "neg": 0})
    deleted = blank = 0
    deleted_rows: list[dict] = []
    failed_pos: set[str] = set()

    for i, (uid, _meta) in enumerate(sorted(todo_pos.items()), 1):
        name = _meta["name"]
        try:
            with ComeetClient() as client:
                cands = client.list_candidates_for_position(uid)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {uid} {name[:30]}: {exc}", file=sys.stderr)
            failed_pos.add(uid)
            continue
        print(f"[{i}/{len(todo_pos)}] {uid} {name[:32]:32} -> {len(cands)} candidates", file=sys.stderr)
        for c in cands:
            cuid = str(c.get("uid") or "")
            if not cuid:
                continue
            cat, label, meta = classify(c)
            cat_counter[cat] += 1
            if cat == "deleted":
                deleted += 1
                # Upserts never remove rows, so a candidate deleted in Comeet
                # after an earlier mine has to be dropped explicitly.
                deleted_rows.append({"candidate_uid": cuid, "position_uid": uid})
                continue
            if meta.get("status") == "":
                blank += 1
            ai = ai_by_uid.get(cuid)
            dims = None
            ai_rating = None
            if ai:
                ai_rating = ai.get("final_rating")
                dims = {k: ai[k] for k in (
                    "dim_company_domain", "dim_profession_domain", "dim_company_tier",
                    "dim_career_progression", "dim_location_match", "dim_university_tier",
                ) if ai.get(k) is not None}
            resume = c.get("resume") or {}
            resume_url = resume.get("url") if isinstance(resume, dict) else None
            # Stable Comeet profile link (the S3 resume_url is a 15-min presigned
            # URL — useless once stored). This is what the teaching card links to.
            profile_url = c.get("URL") if isinstance(c.get("URL"), str) else None
            corpus.append({
                "candidate_uid": cuid,
                "position_uid": uid,
                "position_name": name,
                "candidate_name": candidate_full_name(c),
                "resume_url": resume_url,
                "profile_url": profile_url,
                "status": meta.get("status"),
                "category": cat,
                "screen_label": label,
                "cv_screen_assignee": meta.get("cv_assignee"),
                "cv_screen_time": meta.get("cv_time"),
                "n_completed_steps": meta.get("n_completed"),
                "has_resume": bool(c.get("resume")),
                "source": (c.get("source") or "")[:120] if isinstance(c.get("source"), str) else None,
                "ai_final_rating": ai_rating,
                "ai_dims_json": json.dumps(dims) if dims else None,
                "time_created": c.get("time_created"),
            })
            corpus[-1] = _scrub(corpus[-1])
            per_pos[uid]["n"] += 1
            if label == 1:
                per_pos[uid]["pos"] += 1
            elif label == 0:
                per_pos[uid]["neg"] += 1
            if meta.get("cv_assignee") and label in (0, 1):
                rec = per_rec[meta["cv_assignee"]]
                rec["screened"] += 1
                rec["pos" if label == 1 else "neg"] += 1

    # 3) Persist — UPSERT, never rebuild.
    #
    # The old path dropped the table every run, which meant any position the run
    # did not re-read lost its whole history. Upserting makes a partial or failed
    # run harmless: it can only add or refresh rows.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS corpus_screen_labels ("
            " candidate_uid text, position_uid text, position_name text,"
            " candidate_name text, resume_url text, profile_url text, status text,"
            " category text, screen_label int, cv_screen_assignee text, cv_screen_time timestamptz,"
            " n_completed_steps int, has_resume boolean, source text, ai_final_rating int,"
            " ai_dims_json text, time_created timestamptz, mined_at timestamptz default now(),"
            " PRIMARY KEY (candidate_uid, position_uid))"
        ))
        for _start in range(0, len(corpus), 5000):
            conn.execute(text(
                "INSERT INTO corpus_screen_labels "
                "(candidate_uid, position_uid, position_name, candidate_name, resume_url, profile_url, status,"
                " category, screen_label, cv_screen_assignee, cv_screen_time, n_completed_steps,"
                " has_resume, source, ai_final_rating, ai_dims_json, time_created, mined_at) VALUES "
                "(:candidate_uid, :position_uid, :position_name, :candidate_name, :resume_url, :profile_url, :status,"
                " :category, :screen_label, :cv_screen_assignee, :cv_screen_time, :n_completed_steps,"
                " :has_resume, :source, :ai_final_rating, :ai_dims_json, :time_created, now()) "
                "ON CONFLICT (candidate_uid, position_uid) DO UPDATE SET "
                " position_name=excluded.position_name, candidate_name=excluded.candidate_name,"
                " resume_url=excluded.resume_url, profile_url=excluded.profile_url,"
                " status=excluded.status, category=excluded.category,"
                " screen_label=excluded.screen_label, cv_screen_assignee=excluded.cv_screen_assignee,"
                " cv_screen_time=excluded.cv_screen_time, n_completed_steps=excluded.n_completed_steps,"
                " has_resume=excluded.has_resume, source=excluded.source,"
                " ai_final_rating=excluded.ai_final_rating, ai_dims_json=excluded.ai_dims_json,"
                " time_created=excluded.time_created, mined_at=now()"
            ), corpus[_start:_start + 5000])
        if deleted_rows:
            conn.execute(text(
                "DELETE FROM corpus_screen_labels "
                "WHERE candidate_uid=:candidate_uid AND position_uid=:position_uid"
            ), deleted_rows)
        # Record what was mined, so the next run can skip settled closed reqs.
        done_pos = [
            {"position_uid": u, "position_name": m["name"], "status": m["status"]}
            for u, m in todo_pos.items() if u not in failed_pos
        ]
        if done_pos:
            conn.execute(text(
                "INSERT INTO corpus_positions (position_uid, position_name, status, mined_at) "
                "VALUES (:position_uid, :position_name, :status, now()) "
                "ON CONFLICT (position_uid) DO UPDATE SET position_name=excluded.position_name,"
                " status=excluded.status, mined_at=now()"
            ), done_pos)

    # 4) Report.
    total = len(corpus)
    pos = sum(1 for r in corpus if r["screen_label"] == 1)
    neg = sum(1 for r in corpus if r["screen_label"] == 0)
    labeled = pos + neg
    with_ai = sum(1 for r in corpus if r["ai_final_rating"] is not None)
    labeled_with_ai = sum(1 for r in corpus if r["screen_label"] in (0, 1) and r["ai_final_rating"] is not None)

    print("\n================ CORPUS REPORT ================")
    print(f"candidates seen (excl deleted): {total}   deleted: {deleted}   blank-status: {blank}")
    print(f"screen-labeled (usable): {labeled}   positive(passed): {pos}   negative(rejected@screen): {neg}")
    if labeled:
        print(f"  positive rate: {pos/labeled:.1%}   base-rate balance ok for training: {min(pos,neg)>=100}")
    print(f"AI-scored candidates present: {with_ai}   labeled AND AI-scored: {labeled_with_ai}")
    print("\n--- category distribution ---")
    for k, v in cat_counter.most_common():
        print(f"  {v:6d}  {k}")
    print("\n--- per-position (labeled only, top 20 by volume) ---")
    for uid, d in sorted(per_pos.items(), key=lambda kv: -(kv[1]["pos"] + kv[1]["neg"]))[:20]:
        lab = d["pos"] + d["neg"]
        rate = f"{d['pos']/lab:.0%}" if lab else "-"
        print(f"  {uid:8} n={d['n']:5} labeled={lab:5} pass={d['pos']:5} rej={d['neg']:5} passrate={rate:>4}  {pos_names.get(uid,'')[:30]}")
    print("\n--- per-recruiter screen behavior (>=20 screens) ---")
    for rec, d in sorted(per_rec.items(), key=lambda kv: -kv[1]["screened"]):
        if d["screened"] < 20:
            continue
        print(f"  {d['screened']:5} screens  pass={d['pos']:5} rej={d['neg']:5} passrate={d['pos']/d['screened']:.0%}  {rec}")
    print("\nwrote table: corpus_screen_labels")
    print("=================================================")


if __name__ == "__main__":
    main()
