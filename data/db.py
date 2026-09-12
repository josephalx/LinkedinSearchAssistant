"""
Storage layer for LinkedinBot — Postgres version.

`jobs` holds every scraped posting, written once by the scraper.
`matches` (used once the scorer script is built) holds scores,
keyed back to jobs.job_id — kept separate so scraping and scoring
never write to the same row.
"""

import psycopg2
import psycopg2.errors

DB_CONFIG = {
    "dbname": "linkedinbot",
    "host": "localhost",
    "port": 5432,
    # user/password default to your local Postgres setup (peer auth via
    # your macOS username, no password needed for a local `createdb`).
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            url TEXT,
            jd_text TEXT,
            jd_hash TEXT,
            source_keyword TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # In case jobs already exists from before source_keyword/jd_hash were added.
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_keyword TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_hash TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedup_checked BOOLEAN DEFAULT FALSE")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_jd_hash ON jobs (jd_hash)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            job_id TEXT REFERENCES jobs(job_id),
            score INTEGER,
            missing_skills TEXT,
            reasoning TEXT,
            resume_version TEXT,
            applied BOOLEAN DEFAULT FALSE,
            scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # In case matches already exists from before applied was added.
    cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS applied BOOLEAN DEFAULT FALSE")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS match_benchmark (
            id SERIAL PRIMARY KEY,
            job_id TEXT REFERENCES jobs(job_id),
            classifier_model TEXT,
            scorer_model TEXT,
            resume_version TEXT,
            score INTEGER,
            missing_skills TEXT,
            reasoning TEXT,
            scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def _normalize_for_dedup(text: str) -> str:
    """Collapse whitespace and lowercase, so trivial formatting differences
    (extra spaces, capitalization) don't defeat duplicate detection."""
    return " ".join((text or "").split()).lower()


def _jd_hash(jd_text: str) -> str:
    import hashlib
    return hashlib.sha256(_normalize_for_dedup(jd_text).encode("utf-8")).hexdigest()


def insert_job(job: dict) -> bool:
    """Insert a scraped job. Returns False if job_id already existed, OR if
    the JD text content itself is an exact duplicate (by hash) — the only
    reliable signal that two postings are genuinely the same. Title+company
    matching alone is NOT used to skip: companies (especially staffing
    agencies — Motion Recruitment, Apex Systems, Mastech Digital, etc. —
    routinely post many genuinely different roles under one generic title),
    so treating that alone as duplication would silently drop real postings.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        jd_hash = _jd_hash(job["jd_text"])

        cur.execute(
            "SELECT 1 FROM jobs WHERE jd_hash = %s LIMIT 1",
            (jd_hash,),
        )
        if cur.fetchone():
            return False  # exact content duplicate, skip

        cur.execute(
            """INSERT INTO jobs (job_id, title, company, url, jd_text, jd_hash, source_keyword)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (job["job_id"], job["title"], job["company"], job["url"], job["jd_text"], jd_hash, job.get("source_keyword")),
        )
        conn.commit()
        return True
    except psycopg2.errors.UniqueViolation:
        # job_id already present — already scraped, skip silently.
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def mark_applied(job_id: str, applied: bool = True):
    """Flip the applied flag on every matches row for this job_id — in
    practice there's one row per job, but this covers it either way."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE matches SET applied = %s WHERE job_id = %s", (applied, job_id))
    conn.commit()
    cur.close()
    conn.close()


def save_benchmark_match(job_id, result, resume_version, classifier_model, scorer_model):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO match_benchmark (job_id, classifier_model, scorer_model, resume_version, score, missing_skills, reasoning)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (job_id, classifier_model, scorer_model, resume_version, result.get("score"), result.get("missing_skills"), result.get("reasoning")),
    )
    conn.commit()
    cur.close()
    conn.close()


def unscored_job_ids() -> list[str]:
    """Job IDs in `jobs` that don't yet have ANY row in `matches`. Kept for
    reference/backward compat — pending_pairs() is what the matcher actually
    uses now that a job can need scoring against more than one resume."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT job_id FROM jobs
        WHERE job_id NOT IN (SELECT job_id FROM matches)
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


def unchecked_unscored_jobs():
    """Jobs that are (a) unscored and (b) haven't been through the dedup
    cleanup agent yet. This is the durable "flag" the cleanup step uses —
    once a job is marked checked, it's never re-examined, so a restart of
    the scraper (or the cleanup step itself) just re-queries this and finds
    nothing new to do if the prior run already finished.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT job_id, title, company, jd_text, jd_hash FROM jobs
        WHERE dedup_checked = FALSE
          AND job_id NOT IN (SELECT job_id FROM matches)
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows  # list of (job_id, title, company, jd_text, jd_hash)


def find_dedup_candidate(job_id, title, company, jd_hash):
    """Find another job sharing normalized (title, company) but a DIFFERENT
    jd_hash — i.e. looks like the same posting on the surface, but isn't an
    exact-content duplicate (those are already caught at insert time). This
    is the ambiguous case the cleanup agent actually has to judge.
    Returns (job_id, jd_text, is_scored) for the first candidate found, or
    None.

    Normalization here must match _normalize_for_dedup() exactly (lowercase
    + collapse ALL internal whitespace runs to a single space) — plain
    LOWER(TRIM(...)) only strips the edges, so scraped titles with any
    inconsistent internal spacing would silently fail to match even when
    Python's normalized comparison would consider them identical.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.job_id, j.jd_text,
               EXISTS(SELECT 1 FROM matches m WHERE m.job_id = j.job_id) AS is_scored
        FROM jobs j
        WHERE regexp_replace(LOWER(TRIM(j.title)), '\s+', ' ', 'g') = %s
          AND regexp_replace(LOWER(TRIM(j.company)), '\s+', ' ', 'g') = %s
          AND j.jd_hash != %s
          AND j.job_id != %s
        LIMIT 1
    """, (_normalize_for_dedup(title), _normalize_for_dedup(company), jd_hash, job_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row  # (job_id, jd_text, is_scored) or None


def mark_dedup_checked(job_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE jobs SET dedup_checked = TRUE WHERE job_id = %s", (job_id,))
    conn.commit()
    cur.close()
    conn.close()


def remove_job_if_unscored(job_id) -> bool:
    """Deletes a job row, but only if it has no matches/match_benchmark rows
    attached — never removes anything already scored, regardless of what
    the cleanup agent concluded."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM matches WHERE job_id = %s LIMIT 1", (job_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return False
    cur.execute("SELECT 1 FROM match_benchmark WHERE job_id = %s LIMIT 1", (job_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return False
    cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
    conn.commit()
    cur.close()
    conn.close()
    return True


def pending_pairs(resume_versions: list[str]) -> list[tuple[str, str]]:
    """(job_id, resume_version) pairs that don't have a `matches` row yet.

    This is the real queue: since a job may need scoring against multiple
    resumes (not just once), "pending" is tracked per (job, resume) pair
    rather than per job. Safe to interrupt/rerun — any pair already scored
    won't be returned again.
    """
    conn = get_connection()
    cur = conn.cursor()
    pairs = []
    for version in resume_versions:
        cur.execute("""
            SELECT job_id FROM jobs
            WHERE job_id NOT IN (
                SELECT job_id FROM matches WHERE resume_version = %s
            )
        """, (version,))
        pairs.extend((row[0], version) for row in cur.fetchall())
    cur.close()
    conn.close()
    return pairs
