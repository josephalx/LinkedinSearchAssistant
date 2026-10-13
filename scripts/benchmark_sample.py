"""
Draws a random sample of already-scored jobs from `matches` and freezes it to
a JSON file, for comparison_benchmark.py to run every model against.

The file is the freeze. Re-drawing per run would mean each model saw different
jobs, and at n=100 the sampling noise is large enough to swamp a real
difference between models — you would be measuring the dice. Pointing every
run at one file keeps the comparison honest.

Random rather than top-by-score on purpose: the top of the table is all jobs
the matcher already likes, so a model that inflates everything to 85 looks
fine there. A random draw spans the whole range, including the jobs where
misbehaviour shows.

Run: python3 scripts/benchmark_sample.py              # 100 jobs, default path
     python3 scripts/benchmark_sample.py 250          # a different size
     SAMPLE_OUT=... python3 scripts/benchmark_sample.py
     SAMPLE_FORCE=1 python3 scripts/benchmark_sample.py   # overwrite
"""

import os
import sys
import json
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db

DEFAULT_SIZE = 100

# Lives alongside the other benchmark data, NOT inside a per-gateway results
# folder: the same sample has to serve every gateway, or Bedrock results and
# OpenRouter results stop being comparable.
OUTPUT_PATH = os.environ.get("SAMPLE_OUT") or os.path.join(
    PROJECT_ROOT, "benchmarks_results", "benchmark_sample.json")

FORCE = os.environ.get("SAMPLE_FORCE", "").lower() in ("1", "true", "yes")

# Pre-filter exclusions are stored as matches with score 0 and a marker
# resume_version. Drawing them would waste sample slots: the benchmark's own
# pre-filters re-exclude them without ever calling a model, so they test
# nothing. Measured: 339 of 3,413 rows.
EXCLUDED_PREFIX = "excluded"


def draw(limit):
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.job_id, j.title, j.company, j.url,
               m.score, m.resume_version, m.missing_skills, m.reasoning,
               m.scored_at
        FROM matches m
        JOIN jobs j ON j.job_id = m.job_id
        WHERE m.resume_version NOT LIKE %s
        ORDER BY RANDOM()
        LIMIT %s
    """, (EXCLUDED_PREFIX + "%", limit))
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]

    cur.execute("""
        SELECT COUNT(*) FROM matches WHERE resume_version NOT LIKE %s
    """, (EXCLUDED_PREFIX + "%",))
    eligible = cur.fetchone()[0]
    cur.close()
    conn.close()

    records = []
    for row in rows:
        record = dict(zip(columns, row))
        if isinstance(record.get("scored_at"), datetime):
            record["scored_at"] = record["scored_at"].isoformat()
        records.append(record)
    return records, eligible


def summarise(records):
    """Score spread, so it is obvious whether the draw covers the range."""
    bands = [("0-29", 0, 29), ("30-49", 30, 49), ("50-69", 50, 69),
             ("70-89", 70, 89), ("90-100", 90, 100)]
    for label, lo, hi in bands:
        n = sum(1 for r in records if r.get("score") is not None and lo <= r["score"] <= hi)
        if n:
            print(f"     {label:7} {n:4}  {'#' * n}")


def main():
    size = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SIZE

    if os.path.exists(OUTPUT_PATH) and not FORCE:
        print(f"{OUTPUT_PATH} already exists.")
        print("Refusing to overwrite — a frozen sample is what makes separate "
              "model runs comparable, and replacing it silently invalidates "
              "every result already measured against it.")
        print("Re-draw deliberately with SAMPLE_FORCE=1, or set SAMPLE_OUT to "
              "a new path.")
        return 1

    records, eligible = draw(size)
    if not records:
        print("No eligible rows in `matches` — nothing to sample.")
        return 1

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)

    print(f"Drew {len(records)} of {eligible} eligible scored jobs "
          f"(excluding {EXCLUDED_PREFIX}_* rows)")
    print(f"Wrote {OUTPUT_PATH}\n")
    print("  score spread:")
    summarise(records)
    if len(records) < size:
        print(f"\n  Only {len(records)} available, asked for {size}.")
    print(f"\nUse it with:  ./run_benchmark_sweep.sh {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
