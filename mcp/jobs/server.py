"""
Job-queue MCP server — exposes the match queue and the Notion hand-off as
tools an agent can drive.

Three tools, designed to be used in a loop:

    get_jobs(...)              the next batch of strong matches, best first
    add_job_to_notion(job_id)  file one in the Notion tracker (and mark it handled)
    discard_job(job_id)        drop one without filing it

Together they form a draining queue: both write actions take a job out of the
next get_jobs() call, so an agent can triage until get_jobs() reports the queue
is empty. The only difference between them is whether the job reaches Notion.

Reads Postgres through data/db.py and writes Notion through data/notion.py, so
the credentials, the schema-adaptive property mapping and the duplicate guard
are all the same ones dashboard/api.py uses. No new config.

Transport is stdio: stdout carries the JSON-RPC protocol, so nothing here may
print to it. Diagnostics go to stderr.

Setup:
    pip install "mcp[cli]"
    Postgres running, and a Notion key in keyring ('notion', 'api_key').

Register:
    claude mcp add jobs -- /path/to/.venv/bin/python /path/to/mcp/jobs/server.py
    agy mcp add jobs /path/to/.venv/bin/python /path/to/mcp/jobs/server.py

Run standalone (smoke test, no MCP client):
    python3 mcp/jobs/server.py --selftest
"""

import os
import sys
import json
from datetime import datetime, date

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db
import notion

# The LinkedIn logic lives in the sibling server rather than being duplicated
# here: one definition of how a job page is classified and how the employer
# URL is read. Loaded by path because both files are called server.py.
import importlib.util as _importlib_util
_linkedin_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "linkedin", "server.py")
_spec = _importlib_util.spec_from_file_location("linkedin_server", _linkedin_path)
linkedin = _importlib_util.module_from_spec(_spec)
sys.modules["linkedin_server"] = linkedin
_spec.loader.exec_module(linkedin)

from mcp.server.mcpserver import MCPServer

DEFAULT_LIMIT = 5          # each returned job costs two LinkedIn page loads
DEFAULT_MIN_SCORE = 70

# How many candidates to consider per requested job. Roughly a third of
# candidates turn out to be external applications, and closed ones are
# discarded rather than returned, so the pool has to be several times the
# limit to fill it.
CANDIDATE_MULTIPLIER = 6
MAX_CANDIDATES = 40

# Employers to keep out of the queue entirely. Matched case-insensitively on
# whole words, so "Amazon Web Services (AWS)", "Amazon Lab126", "Prime Video &
# Amazon MGM Studios" and "Google DeepMind" are all caught, while "Metabase"
# and "Amazonia Labs" are not — a plain substring match on "meta" would have
# swept those up too.
# "audible" is listed separately because Amazon's subsidiaries don't carry the
# parent name — "Audible" alone never matches \yamazon\y.
EXCLUDED_COMPANIES = ("amazon", "audible", "google", "microsoft", "meta")
EXCLUDED_COMPANY_PATTERN = r"\y(" + "|".join(EXCLUDED_COMPANIES) + r")\y"


def log(message):
    """stderr only — stdout belongs to the MCP protocol."""
    print(f"[jobs-mcp] {message}", file=sys.stderr, flush=True)


def jsonable(value):
    """Postgres hands back datetimes; JSON can't serialise them."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def fetch_jobs(limit, min_score, include_applied, include_in_notion, include_jd_text,
               exclude_companies=True):
    """Rows from `matches` joined to `jobs`, in export_matches.py's shape.

    Ordered by score then recency, so the best match is always first.
    """
    # In practice `applied = FALSE` already excludes everything in Notion —
    # filing a job marks it applied, so added_to_notion implies applied. The
    # second filter is a belt-and-braces guard against any future path that
    # writes one flag without the other.
    where = ["m.score >= %s"]
    params = [min_score]
    if not include_applied:
        where.append("m.applied = FALSE")
    if not include_in_notion:
        where.append("m.added_to_notion = FALSE")
    if exclude_companies:
        where.append("j.company !~* %s")
        params.append(EXCLUDED_COMPANY_PATTERN)

    # jd_text is ~8 KB per row; ten of those is a lot of agent context to
    # spend when the caller only wanted titles and scores.
    jd_column = "j.jd_text," if include_jd_text else ""

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            j.job_id,
            j.title,
            j.company,
            j.url,
            {jd_column}
            j.source_keyword,
            m.resume_version,
            m.score,
            m.missing_skills,
            m.reasoning,
            m.applied,
            m.added_to_notion,
            m.scored_at
        FROM matches m
        JOIN jobs j ON j.job_id = m.job_id
        WHERE {' AND '.join(where)}
        ORDER BY m.score DESC, m.scored_at DESC
        LIMIT %s
    """, (*params, limit))
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    cur.close()
    conn.close()

    return [{col: jsonable(val) for col, val in zip(columns, row)} for row in rows]


def count_remaining(min_score, include_applied, include_in_notion, exclude_companies=True):
    """How many jobs match the same filters, ignoring the limit.

    Must mirror fetch_jobs' WHERE clause exactly, or `remaining` reports a
    queue depth the agent can't actually reach.
    """
    where = ["m.score >= %s"]
    params = [min_score]
    if not include_applied:
        where.append("m.applied = FALSE")
    if not include_in_notion:
        where.append("m.added_to_notion = FALSE")
    if exclude_companies:
        where.append("j.company !~* %s")
        params.append(EXCLUDED_COMPANY_PATTERN)

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM matches m JOIN jobs j ON j.job_id = m.job_id "
                f"WHERE {' AND '.join(where)}", params)
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return total


def lookup_job(job_id):
    """(title, company, url, added_to_notion, applied) for a scored job.

    Returns None when the id matches no scored job — the join means an
    unscored job is reported the same as a nonexistent one, which is correct
    for this queue: you can't add something that was never matched.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.title, j.company, j.url, m.added_to_notion, m.applied
        FROM jobs j
        JOIN matches m ON m.job_id = j.job_id
        WHERE j.job_id = %s
    """, (str(job_id),))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


mcp = MCPServer(
    name="jobs",
    instructions=(
        "The LinkedIn job-match queue. Call get_jobs to pull the strongest "
        "unhandled matches, then for each one either add_job_to_notion to file "
        "it in the Notion tracker, or discard_job to drop it. Both take the job "
        "out of later get_jobs calls, so repeat until get_jobs reports the "
        "queue is empty. Neither can be undone from here — use the dashboard's "
        "Restore button if a job is handled by mistake."
    ),
)


@mcp.tool(description="Get job matches that are confirmed EXTERNAL applications, each with the employer's application URL. Verifies every job against LinkedIn at call time; jobs no longer accepting applications are auto-discarded. Excludes Amazon, Audible, Google, Microsoft and Meta. Takes ~1-2 minutes.")
def get_jobs(
    limit: int = DEFAULT_LIMIT,
    min_score: int = DEFAULT_MIN_SCORE,
    include_jd_text: bool = False,
    exclude_big_tech: bool = True,
) -> str:
    """Candidates from Postgres, filtered to externals by checking LinkedIn live.

    Deliberately slow. Two LinkedIn passes per returned job:

      1. Guest pass over candidates — no account involved — to sort them into
         external / easy_apply / closed. Paced, because unpaced requests get
         the sign-in wall instead of an answer.
      2. Signed-in pass over the externals only, to read the employer URL.
         This is the only account traffic, matching the rule that login
         happens just for external applications.

    Nothing is cached between calls beyond this process's memory, so a verdict
    is always fresh — which is the point: a job that closed yesterday must not
    come back as applicable.
    """
    limit = max(1, min(int(limit), 20))
    min_score = max(0, min(int(min_score), 100))

    # Over-fetch: only ~a third of candidates turn out external, and closed
    # ones get discarded along the way rather than counting toward the limit.
    pool_size = min(limit * CANDIDATE_MULTIPLIER, MAX_CANDIDATES)

    try:
        candidates = fetch_jobs(pool_size, min_score, False, False,
                                include_jd_text, exclude_big_tech)
        remaining = count_remaining(min_score, False, False, exclude_big_tech)
    except Exception as e:
        log(f"get_jobs failed reading the queue: {type(e).__name__}: {e}")
        return json.dumps({
            "error": f"Could not read the job queue: {type(e).__name__}: {e}",
            "hint": "Is Postgres running? data/db.py holds the connection settings.",
        }, indent=2)

    if not candidates:
        return json.dumps({
            "count": 0, "remaining": 0, "jobs": [],
            "message": (f"No jobs left: nothing scoring {min_score} or above is still "
                        f"unapplied and not already in Notion"
                        + (f" (excluding {', '.join(EXCLUDED_COMPANIES)})" if exclude_big_tech else "")
                        + ". Either the queue is drained, or try a lower min_score."),
        }, indent=2)

    by_url = {job["url"]: job for job in candidates}
    log(f"get_jobs: guest-checking up to {len(candidates)} candidates for {limit} external(s)")

    # --- pass 1: guest classification, stopping once `limit` externals found ---
    externals, discarded, skipped = [], [], {"easy_apply": 0, "unknown": 0, "error": 0}
    for url in list(by_url):
        if len(externals) >= limit:
            break
        verdict = linkedin.classify_many([url]).get(url, "error")

        if verdict == "external":
            externals.append(url)
        elif verdict == "closed":
            # No longer accepting applications: mark handled so it never
            # surfaces again. Not added to Notion — this is a discard.
            job_id = by_url[url]["job_id"]
            try:
                db.mark_applied(job_id, applied=True)
                discarded.append({"job_id": job_id, "title": by_url[url].get("title"),
                                  "company": by_url[url].get("company")})
                log(f"  discarded {job_id} — no longer accepting applications")
            except Exception as e:
                log(f"  could not discard {job_id}: {type(e).__name__}: {e}")
        else:
            skipped[verdict if verdict in skipped else "unknown"] += 1

    if not externals:
        return json.dumps({
            "count": 0,
            "remaining": remaining - len(discarded),
            "jobs": [],
            "discarded_closed": discarded,
            "skipped": skipped,
            "message": ("Checked every candidate and found no external applications — "
                        "they were Easy Apply, closed, or unreadable. Raise limit, lower "
                        "min_score, or try again later."),
        }, indent=2, ensure_ascii=False)

    # --- pass 2: signed-in, externals only, to read the employer URLs ---
    log(f"get_jobs: resolving {len(externals)} application URL(s)")
    resolved = linkedin.resolve_urls(externals)

    jobs = []
    for url in externals:
        job = dict(by_url[url])
        info = resolved.get(url, {})
        application_url = info.get("application_url")
        outcome = info.get("outcome")

        if outcome == "already_applied":
            # LinkedIn says this one is already done; take it out of the queue.
            try:
                db.mark_applied(job["job_id"], applied=True)
                discarded.append({"job_id": job["job_id"], "title": job.get("title"),
                                  "company": job.get("company"), "reason": "already applied"})
            except Exception:
                pass
            continue
        if not application_url:
            skipped["error"] += 1
            continue

        job["linkedin_url"] = job.pop("url")   # keep for reference
        job["url"] = application_url           # the URL the agent should use
        job["application_url"] = application_url
        job["apply_type"] = "external"
        jobs.append(job)

    return json.dumps({
        "count": len(jobs),
        "remaining": remaining - len(discarded),
        "checked": len(externals) + len(discarded) + sum(skipped.values()),
        "discarded_closed": discarded,
        "skipped": skipped,
        "jobs": jobs,
        "message": (f"{len(jobs)} external application(s), verified just now. "
                    f"`url` is the employer's application page."),
    }, indent=2, ensure_ascii=False)


@mcp.tool(description="File one job into the Notion tracker by job_id.")
def add_job_to_notion(job_id: str) -> str:
    """Returns JSON describing what happened. Never raises at the agent."""
    job_id = str(job_id).strip()
    if not job_id:
        return json.dumps({"ok": False, "error": "job_id is required."}, indent=2)

    try:
        row = lookup_job(job_id)
    except Exception as e:
        log(f"lookup failed for {job_id}: {type(e).__name__}: {e}")
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": f"Could not reach the database: {type(e).__name__}: {e}",
        }, indent=2)

    if row is None:
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": f"No scored job with id {job_id!r}.",
            "hint": "Use a job_id from get_jobs(); ids not in `matches` can't be filed.",
        }, indent=2)

    title, company, url, already_added, _applied = row

    if already_added:
        # Not a failure — the desired end state already holds. Said plainly so
        # the agent moves on instead of retrying.
        return json.dumps({
            "ok": True,
            "job_id": job_id,
            "already_added": True,
            "title": title,
            "company": company,
            "message": "Already in Notion; nothing to do.",
        }, indent=2, ensure_ascii=False)

    try:
        page = notion.add_job_to_notion(company=company, role=title, url=url)
    except Exception as e:
        log(f"notion write failed for {job_id}: {type(e).__name__}: {e}")
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": f"Notion rejected the write: {type(e).__name__}: {e}",
            "hint": "Check the 'notion' keyring entry, and that the database is "
                    "shared with the integration (... -> Connections).",
        }, indent=2)

    # Both flags, only after Notion has accepted the page — a failed write
    # leaves the job in the queue instead of silently dropping it.
    #
    # `applied` matters as much as `added_to_notion`: the dashboard's Notion
    # button sets both (filing a job *is* applying to it), so every one of the
    # 140 rows currently in Notion is also applied. Setting only the Notion
    # flag here would make this tool the one path that breaks that invariant,
    # and the job would keep showing up as un-applied everywhere else.
    db.mark_added_to_notion(job_id)
    db.mark_applied(job_id, applied=True)
    log(f"add_job_to_notion -> {job_id} ({title} @ {company})")

    return json.dumps({
        "ok": True,
        "job_id": job_id,
        "title": title,
        "company": company,
        "notion_page_id": page.get("id"),
        "marked_applied": True,
        "message": f"Filed {title} @ {company} in Notion and marked it applied.",
    }, indent=2, ensure_ascii=False)


@mcp.tool(description="Discard a job: take it out of the queue without filing it in Notion.")
def discard_job(job_id: str) -> str:
    """Mark a job handled without touching Notion.

    `applied` is really a "handled" flag — the dashboard labels it
    "Mark Applied / Discard" and filters on "Show applied/discarded" — so
    discarding and applying are the same database write. What separates them
    is whether the job also went to Notion, which this tool deliberately
    does not do.

    Returns JSON describing what happened. Never raises at the agent.
    """
    job_id = str(job_id).strip()
    if not job_id:
        return json.dumps({"ok": False, "error": "job_id is required."}, indent=2)

    try:
        row = lookup_job(job_id)
    except Exception as e:
        log(f"lookup failed for {job_id}: {type(e).__name__}: {e}")
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": f"Could not reach the database: {type(e).__name__}: {e}",
        }, indent=2)

    if row is None:
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": f"No scored job with id {job_id!r}.",
            "hint": "Use a job_id from get_jobs(); ids not in `matches` can't be discarded.",
        }, indent=2)

    title, company, _url, in_notion, already_handled = row

    if already_handled:
        # Already out of the queue — say so plainly so the agent moves on.
        return json.dumps({
            "ok": True,
            "job_id": job_id,
            "already_handled": True,
            "in_notion": bool(in_notion),
            "title": title,
            "company": company,
            "message": "Already applied/discarded; nothing to do.",
        }, indent=2, ensure_ascii=False)

    db.mark_applied(job_id, applied=True)
    log(f"discard_job -> {job_id} ({title} @ {company})")

    return json.dumps({
        "ok": True,
        "job_id": job_id,
        "title": title,
        "company": company,
        "discarded": True,
        "message": f"Discarded {title} @ {company}; it will not appear in get_jobs again.",
    }, indent=2, ensure_ascii=False)


def selftest():
    """Exercise both tools without an MCP client. Read-only except where noted."""
    print("get_jobs(limit=3, include_jd_text=False):")
    print(get_jobs(limit=3, include_jd_text=False)[:1200])

    print("\nadd_job_to_notion('not-a-real-id'):")
    print(add_job_to_notion("not-a-real-id"))

    print("\ndiscard_job('not-a-real-id'):")
    print(discard_job("not-a-real-id"))

    print("\nadd_job_to_notion('') :")
    print(add_job_to_notion(""))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mcp.run(transport="stdio")
