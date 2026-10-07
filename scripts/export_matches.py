"""
Exports the TOP 10 highest-scoring, not-yet-applied `jobs`+`matches` into a
single JSON file, so you can upload it here (or anywhere) to review match
score accuracy or decide what to apply to next.

Every exported job is verified against LinkedIn first: a posting that is no
longer accepting applications is marked applied in the database — a discard,
not filed to Notion — and another candidate takes its place. So the ten in
the file are ten you can actually apply to, not ten that merely scored well
whenever the matcher last ran.

Verification is the guest tier only: LinkedIn says whether a posting is open
without anyone being signed in, so no account traffic is involved.

Run: python3 scripts/export_matches.py
     SKIP_LINKEDIN_CHECK=1 python3 scripts/export_matches.py   # old behaviour
Output: results/matches_export.json.
"""

import os
import sys
import json
import importlib.util
from datetime import datetime

# db.py lives in data/, a sibling of scripts/; exports land in results/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db

RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
OUTPUT_PATH = os.path.join(RESULTS_DIR, "matches_export.json")

TOP_N = 10

# Only a fraction of candidates turn out closed, but over-fetching means one
# query instead of topping up repeatedly as jobs get discarded.
CANDIDATE_MULTIPLIER = 3

SKIP_CHECK = os.environ.get("SKIP_LINKEDIN_CHECK", "").lower() in ("1", "true", "yes")


def load_linkedin():
    """The LinkedIn MCP server's guest-tier logic, loaded by path.

    Imported rather than reimplemented so page classification has one
    definition. Loaded by path because the module is mcp/linkedin/server.py
    and 'server' is not importable by name from here.
    """
    path = os.path.join(PROJECT_ROOT, "mcp", "linkedin", "server.py")
    spec = importlib.util.spec_from_file_location("linkedin_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["linkedin_server"] = module
    spec.loader.exec_module(module)
    return module


def fetch_candidates(limit):
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            j.job_id,
            j.title,
            j.company,
            j.url,
            j.jd_text,
            j.source_keyword,
            m.resume_version,
            m.score,
            m.missing_skills,
            m.reasoning,
            m.applied,
            m.scored_at
        FROM matches m
        JOIN jobs j ON j.job_id = m.job_id
        WHERE m.applied = FALSE
        ORDER BY m.score DESC, m.scored_at DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    cur.close()
    conn.close()

    records = []
    for row in rows:
        record = dict(zip(columns, row))
        # datetime isn't JSON-serializable by default — convert to ISO string.
        if isinstance(record.get("scored_at"), datetime):
            record["scored_at"] = record["scored_at"].isoformat()
        records.append(record)
    return records


def take_active(candidates, wanted, linkedin):
    """First `wanted` candidates still accepting applications.

    Closed postings are marked applied so they stop reappearing here and in
    the dashboard queue. Anything the guest page doesn't classify cleanly —
    unknown, or the sign-in wall — is kept rather than discarded: a job is
    only ever written off on a definite "no longer accepting applications",
    never on an inconclusive read.
    """
    active, discarded, unverified = [], [], 0

    for record in candidates:
        if len(active) >= wanted:
            break

        url = record["url"]
        verdict = linkedin.classify_many([url]).get(url, "error")

        if verdict == "closed":
            db.mark_applied(record["job_id"], applied=True)
            discarded.append(record)
            print(f"  [closed]  {record['title'][:52]} @ {record['company'][:24]} "
                  f"— marked applied (discarded)")
            continue

        if verdict in ("external", "easy_apply"):
            print(f"  [{verdict:10}] {record['title'][:52]} @ {record['company'][:24]}")
        else:
            unverified += 1
            print(f"  [{verdict:10}] {record['title'][:52]} @ {record['company'][:24]} "
                  f"— kept, could not verify")

        record["apply_type"] = verdict
        active.append(record)

    return active, discarded, unverified


def export():
    if SKIP_CHECK:
        results = fetch_candidates(TOP_N)
        discarded, unverified = [], 0
        print(f"Skipping the LinkedIn check (SKIP_LINKEDIN_CHECK set).")
    else:
        candidates = fetch_candidates(TOP_N * CANDIDATE_MULTIPLIER)
        print(f"Checking up to {len(candidates)} candidates for {TOP_N} still-open jobs...")
        linkedin = load_linkedin()
        results, discarded, unverified = take_active(candidates, TOP_N, linkedin)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nExported {len(results)} open matches to {OUTPUT_PATH}")
    if discarded:
        print(f"  {len(discarded)} no longer accepting applications, marked applied:")
        for record in discarded:
            print(f"    {record['job_id']}  {record['title'][:46]} @ {record['company'][:24]}")
    if unverified:
        print(f"  {unverified} could not be verified and were kept anyway")
    if len(results) < TOP_N and not SKIP_CHECK:
        print(f"  Only {len(results)} of {TOP_N} filled — the candidate pool ran out.")


if __name__ == "__main__":
    export()
