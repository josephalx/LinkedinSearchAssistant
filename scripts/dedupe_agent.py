"""
dedupe_agent.py — LLM-judged duplicate cleanup, run automatically at the end
of every scrapper.py run (not matcher.py — matcher.py is untouched).

Complements the exact-hash dedup already done at insert time (db.insert_job):
that catches true reposts (identical JD content). This catches the harder,
ambiguous case — same normalized (title, company), but DIFFERENT jd_hash.
That could mean:
  - A trivial repost (extra tracking text, minor formatting change) — same
    underlying job, worth merging.
  - A genuinely different opening that happens to share a generic title —
    especially common at staffing agencies (Motion Recruitment, Apex
    Systems, Mastech Digital, etc.) who routinely post many distinct client
    roles under one boilerplate title. These must NOT be merged.

Only a model reading both JDs can tell these apart, so this is the one place
in the pipeline an LLM call is used for dedup, and only for jobs where a
cheap SQL check finds an actual candidate — most jobs never trigger any
API call at all.

Durable flag / no wasted reruns:
  - jobs.dedup_checked marks a job as handled (kept as distinct, or already
    removed as a duplicate). Once TRUE, it's never re-examined.
  - Only unscored jobs are touched — anything already in `matches` is left
    completely alone, never deleted, never re-evaluated.
  - Re-running this (e.g. because a scrape got interrupted) just re-queries
    for dedup_checked = FALSE; if the previous run already finished, that's
    an empty result and this becomes a fast no-op.

Respects SCRAPER_DRY_RUN (same flag scrapper.py itself uses) — a dashboard
dry-run scrape won't have this step delete anything either, just report
what it would have done.
"""

import os
import sys

# db.py lives in data/, a sibling of scripts/. This is normally already on
# sys.path by the time scrapper.py imports this module — but adding it here
# too means dedupe_agent.py also works standalone (python3 dedupe_agent.py),
# not just as an import.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
import db

DRY_RUN = os.environ.get("SCRAPER_DRY_RUN", "").lower() in ("1", "true", "yes")

# Reused from matcher.py: same OpenRouter call_model() (streaming, retries,
# error handling). This is a simple same/different judgment call, well within
# what a smaller/free model handles reliably.
CLASSIFIER_MODEL = "inclusionai/ling-3.0-flash-fin:free"


def build_dedup_prompt(title, company, jd_a, jd_b):
    return f"""Two job postings share the same title and company, but their
descriptions differ. Decide whether they are the SAME underlying job
posting (e.g. a trivial repost with minor text differences), or GENUINELY
DIFFERENT openings.

Important context: staffing/recruiting agencies (e.g. Motion Recruitment,
Apex Systems, Mastech Digital, TechLink Resources, NTT DATA, and similar)
routinely post many distinct client roles under one generic, boilerplate
title. For company names that look like a staffing/recruiting agency rather
than a direct employer, lean toward "different" unless the job descriptions
are clearly describing the exact same role (same client, same team, same
responsibilities) — a shared generic title alone is NOT sufficient evidence
of duplication.

Title: {title}
Company: {company}

Job Description A:
{jd_a}

Job Description B:
{jd_b}

Return ONLY one word, no punctuation, no explanation: same OR different
"""


def judge_same_posting(title, company, jd_a, jd_b):
    """Returns True (same posting, safe to merge) or False (distinct,
    leave both). Defaults to False (distinct) on any failure — an
    unresolved case should never risk deleting a real, distinct posting.
    """
    import matcher  # deferred import: avoids any load-order assumptions,
                     # and this module is only ever called from scrapper.py

    content = matcher.call_model(CLASSIFIER_MODEL, build_dedup_prompt(title, company, jd_a, jd_b))
    if content is None:
        return False
    normalized = content.strip().lower()
    return "same" in normalized and "different" not in normalized


def run_cleanup():
    candidates = db.unchecked_unscored_jobs()

    if not candidates:
        print("=== Dedup check: up to date, nothing new to check ===")
        return

    print(f"=== Dedup check: {len(candidates)} job(s) need checking ===")
    if DRY_RUN:
        print("(dry-run: will report findings, but won't remove or mark anything)")

    examined = 0
    removed = 0
    kept_distinct = 0

    for job_id, title, company, jd_text, jd_hash in candidates:
        examined += 1
        partner = db.find_dedup_candidate(job_id, title, company, jd_hash)

        if partner is None:
            # No ambiguous candidate at all — nothing to judge, mark done.
            print(f"  [ok] {title} @ {company} — job_id {job_id}, no candidate found")
            if not DRY_RUN:
                db.mark_dedup_checked(job_id)
            kept_distinct += 1
            continue

        partner_id, partner_jd, partner_is_scored = partner
        is_same = judge_same_posting(title, company, jd_text, partner_jd)

        if is_same:
            # job_id (the one being examined) is always unscored, by
            # construction of unchecked_unscored_jobs() — so it's always
            # safe to remove it and keep the partner, whether or not the
            # partner itself has already been scored.
            print(f"  [duplicate] {title} @ {company} — job_id {job_id} matches {partner_id}, removing {job_id}")
            if not DRY_RUN:
                db.remove_job_if_unscored(job_id)
            removed += 1
        else:
            print(f"  [distinct] {title} @ {company} — job_id {job_id} vs {partner_id}, keeping both")
            if not DRY_RUN:
                db.mark_dedup_checked(job_id)
            kept_distinct += 1

    print(f"=== Dedup check finished: {examined} examined, {removed} removed, {kept_distinct} kept as distinct ===")


if __name__ == "__main__":
    run_cleanup()
