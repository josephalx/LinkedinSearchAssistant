"""
Exports the TOP 10 highest-scoring, not-yet-applied `jobs`+`matches` into a
single JSON file, so you can upload it here (or anywhere) to review match
score accuracy or decide what to apply to next.

Run: python3 scripts/export_matches.py
Output: results/matches_export.json.
"""

import os
import sys
import json
from datetime import datetime

# db.py lives in data/, a sibling of scripts/; exports land in results/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db

RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
OUTPUT_PATH = os.path.join(RESULTS_DIR, "matches_export.json")

TOP_N = 10


def export():
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
    """, (TOP_N,))
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    cur.close()
    conn.close()

    results = []
    for row in rows:
        record = dict(zip(columns, row))
        # datetime isn't JSON-serializable by default — convert to ISO string.
        if isinstance(record.get("scored_at"), datetime):
            record["scored_at"] = record["scored_at"].isoformat()
        results.append(record)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported top {len(results)} not-yet-applied matches to {OUTPUT_PATH}")


if __name__ == "__main__":
    export()
