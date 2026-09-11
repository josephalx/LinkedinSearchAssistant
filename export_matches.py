"""
Exports `jobs` joined with `matches` into a single JSON file, so you can
upload it here (or anywhere) to review match score accuracy.

Run: python3 export_matches.py
Output: matches_export.json in the same folder.
"""

import json
from datetime import datetime

import db


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
            m.scored_at
        FROM matches m
        JOIN jobs j ON j.job_id = m.job_id
        ORDER BY m.scored_at DESC
    """)
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

    with open("matches_export.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(results)} matches to matches_export.json")


if __name__ == "__main__":
    export()
