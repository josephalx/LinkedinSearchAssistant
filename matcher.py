"""
Matcher — scores unscored (job, resume) pairs from `jobs` using NVIDIA
Nemotron 3 Ultra (free) via OpenRouter, writing results into `matches`.

Every job is scored against BOTH resumes, regardless of which search found
it — keyword-based routing was tried and dropped, since which search
surfaced a job isn't a reliable signal of what type of role it actually is
(an RN search can surface a general SDE role that just mentions React
Native, and vice versa). Letting the actual JD content get judged against
both resumes avoids that misclassification risk entirely.

Queue logic:
  - db.pending_pairs(resume_versions) returns (job_id, resume_version)
    pairs that don't have a `matches` row yet — the real "pending" list.
  - Each pair scored gets exactly one row inserted into `matches`.
  - Interrupting and rerunning this script is safe: already-scored pairs
    won't reappear in the queue, so it always resumes where it left off.
  - MAX_REQUESTS_PER_RUN caps a single run below Nemotron's daily limit;
    anything left over just rolls into the next run's queue untouched.
"""

import os
import re
import json
import time
import random
import keyring


import requests
from docx import Document

import db


OPENROUTER_API_KEY = keyring.get_password("openrouter", "api_key") or os.environ.get("OPENROUTER_API_KEY")

# Dry-run mode: set MATCHER_DRY_RUN=1 to score jobs normally (still calls both
# models, still costs API quota) but skip writing anything to `matches` —
# useful for demoing/testing the run without touching real backlog data.
# Unset (the default) is the normal "prod" behavior: scores get saved.
DRY_RUN = os.environ.get("MATCHER_DRY_RUN", "").lower() in ("1", "true", "yes")


# Two models, two separate OpenRouter rate-limit pools — classification calls
# don't eat into the scoring model's daily quota, and vice versa.
CLASSIFIER_MODEL = "nvidia/nemotron-3.5-lightning:free"
SCORER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

RESUMES = {
    "software_engineering_v1": "/Users/joseph/Documents/Resume/Chakola_Joseph_Resume_Software_Engineering.docx",
    "mobile_v1": "/Users/joseph/Documents/Resume/Chakola_Joseph_Resume_Mobile_App_Developer.docx",
}
DEFAULT_RESUME_VERSION = "software_engineering_v1"  # fallback if classification fails/is ambiguous

MAX_REQUESTS_PER_RUN = 950  # stay under Ultra's 1000/day cap with a safety buffer
MATCH_THRESHOLD = 70


def load_resume_text(path):
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def load_all_resumes():
    """version -> resume text, loaded once per run."""
    return {version: load_resume_text(path) for version, path in RESUMES.items()}


def build_classifier_prompt(jd_text):
    return f"""Classify this job description as exactly one of: mobile, software_engineering.

"mobile" = React Native, Android, iOS, Flutter, Swift, Kotlin, or other mobile app development roles.
"software_engineering" = general backend, full-stack, web, or other non-mobile software engineering roles.

Job Description:
{jd_text}

Return ONLY one word, no punctuation, no explanation: mobile OR software_engineering
"""


def build_prompt(resume_text, jd_text):
    return f"""You are scoring how well a candidate's resume matches a job description.

Resume:
{resume_text}

Job Description:
{jd_text}

Scoring rules:
- Identify the required years of professional experience stated in the JD, if any.
- If the JD requires 5+ years of experience (or more) and the candidate's actual
  professional experience falls meaningfully short of that requirement, treat this
  as a significant gap: cap the score at 50 or below, even if the candidate's
  technical skills otherwise align well with the role.
- If the JD requires fewer than 5 years, or does not specify a minimum, do not
  apply this cap — score based on overall fit as usual.
- A hard, explicitly stated year requirement ("X+ years required") should be
  weighted more heavily than a "nice to have" experience mention.

Return ONLY a JSON object, no markdown fences, no extra commentary, in exactly this shape:
{{"score": <integer 0-100>, "missing_skills": "<comma-separated list>", "reasoning": "<1-2 sentence explanation>"}}
"""


def parse_json_response(content):
    # response_format isn't enforced on the free tier, so strip markdown
    # fences defensively before parsing.
    cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    return json.loads(cleaned)


class FatalAPIError(Exception):
    """Raised for errors that retrying won't fix (bad key, out of credits) —
    signals the whole run should stop, not just this one call."""
    pass


def extract_error(data):
    """Find an error object wherever OpenRouter put it — either top-level
    (request-stage failures) or nested in choices[0] (provider failures that
    happen after a 200 has already been sent, per OpenRouter's docs)."""
    if "error" in data:
        return data["error"]
    choices = data.get("choices") or []
    if choices and choices[0].get("finish_reason") == "error" and "error" in choices[0]:
        return choices[0]["error"]
    return None


def call_model(model, prompt, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  Attempt {attempt + 1} failed ({model}): network error: {e}")
            time.sleep(2 ** (attempt + 1))
            continue
        except ValueError as e:
            print(f"  Attempt {attempt + 1} failed ({model}): invalid JSON response: {e}")
            time.sleep(2 ** (attempt + 1))
            continue

        error = extract_error(data)
        if error is None and resp.status_code >= 400:
            # No parseable error body, but still a failing status — fall back
            # to the HTTP code alone.
            error = {"code": resp.status_code, "message": resp.text[:200], "metadata": {}}

        if error:
            error_type = error.get("metadata", {}).get("error_type")
            code = error.get("code", resp.status_code)

            if error_type == "rate_limit_exceeded" or code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 2 ** (attempt + 1)
                print(f"  Rate limited on {model}; waiting {wait}s before retry.")
                time.sleep(wait)
                continue

            if error_type == "payment_required" or code == 402:
                raise FatalAPIError(f"{model}: out of credits (402). Add credits and rerun.")

            if error_type == "authentication" or code == 401:
                raise FatalAPIError(f"{model}: invalid API key (401). Check OPENROUTER_API_KEY.")

            if error_type in ("provider_overloaded", "provider_unavailable", "server", "timeout", "unmapped") or code in (502, 503, 504) or code >= 500:
                # Provider-side/transient — honor Retry-After (sent on 429/503) if present.
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 2 ** (attempt + 1)
                print(f"  Transient error ({error_type or code}) from {model}; waiting {wait}s before retry.")
                time.sleep(wait)
                continue

            # Request-shape errors (bad prompt, content policy, context length, etc.)
            # — retrying the identical request won't help, so don't burn retries on it.
            print(f"  Non-retryable error from {model}: {error.get('message')} (error_type={error_type})")
            return None

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            print(f"  Unexpected response shape from {model}: {e}")
            time.sleep(2 ** (attempt + 1))
            continue

    return None


CLEARANCE_KEYWORDS = re.compile(
    r"security clearance|secret clearance|top secret|ts/sci|"
    r"public trust clearance|government clearance|active clearance|"
    r"must be able to obtain (a |and maintain )?(a )?(security )?clearance|"
    r"u\.?s\.? citizenship required|must be a u\.?s\.? citizen",
    re.IGNORECASE,
)


def requires_clearance(jd_text):
    """Cheap keyword check, run before spending any API call. Catches security
    clearance requirements and the citizenship-required roles that usually
    accompany them."""
    return bool(CLEARANCE_KEYWORDS.search(jd_text or ""))


def classify_resume_type(jd_text):
    """Ask the cheap/fast model which resume fits this JD. Falls back to the
    default resume if the classifier fails or returns something unexpected."""
    content = call_model(CLASSIFIER_MODEL, build_classifier_prompt(jd_text))
    if content is None:
        return DEFAULT_RESUME_VERSION
    normalized = content.strip().lower()
    if "mobile" in normalized:
        return "mobile_v1"
    if "software" in normalized:
        return "software_engineering_v1"
    return DEFAULT_RESUME_VERSION


def score_job(resume_text, jd_text):
    content = call_model(SCORER_MODEL, build_prompt(resume_text, jd_text))
    if content is None:
        return None
    try:
        return parse_json_response(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  Failed to parse scorer response: {e}")
        return None


def get_job_details(job_id):
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, company, jd_text FROM jobs WHERE job_id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row  # (title, company, jd_text)


def save_match(job_id, result, resume_version):
    if DRY_RUN:
        print(f"  [dry-run] would save: job_id={job_id} score={result.get('score')} resume_version={resume_version}")
        return
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO matches (job_id, score, missing_skills, reasoning, resume_version)
           VALUES (%s, %s, %s, %s, %s)""",
        (job_id, result.get("score"), result.get("missing_skills"), result.get("reasoning"), resume_version),
    )
    conn.commit()
    cur.close()
    conn.close()


def main():
    if not OPENROUTER_API_KEY:
        raise RuntimeError("Set OPENROUTER_API_KEY environment variable first.")

    if DRY_RUN:
        print("=== DRY RUN: scoring normally, but nothing will be written to the DB ===")

    resumes = load_all_resumes()  # resume_version -> resume text, loaded once
    scored = 0
    max_passes = 1 if DRY_RUN else 5  # dry-run: one pass only — nothing gets
    # marked scored in the DB, so a second pass would just re-fetch and
    # re-score the exact same jobs again for no reason.
    seen_this_run = set()  # extra safety even within a single dry-run pass

    try:
        for pass_num in range(1, max_passes + 1):
            pending = [j for j in db.unscored_job_ids() if j not in seen_this_run]
            if not pending:
                break

            print(f"\n--- Pass {pass_num}: {len(pending)} jobs pending ---")
            progressed_this_pass = 0

            for job_id in pending:
                if scored >= MAX_REQUESTS_PER_RUN:
                    print(f"Hit run cap ({MAX_REQUESTS_PER_RUN}); stopping.")
                    print(f"\nDone. Scored {scored} jobs this run.")
                    return

                title, company, jd_text = get_job_details(job_id)

                if requires_clearance(jd_text):
                    save_match(
                        job_id,
                        {"score": 0, "missing_skills": "N/A", "reasoning": "Excluded: requires security clearance / US citizenship."},
                        "excluded_clearance",
                    )
                    seen_this_run.add(job_id)
                    progressed_this_pass += 1
                    print(f"[excluded] {title} @ {company} — requires clearance/citizenship, skipped without scoring")
                    continue

                resume_version = classify_resume_type(jd_text)  # cheap model, separate quota
                resume_text = resumes[resume_version]

                result = score_job(resume_text, jd_text)  # heavy model, separate quota

                if result is None:
                    print(f"Skipping {job_id} ({title} @ {company}) after failed retries — will retry this pass or next run.")
                    continue

                save_match(job_id, result, resume_version)
                seen_this_run.add(job_id)
                scored += 1
                progressed_this_pass += 1
                score = result.get("score", 0)
                status = "MATCH" if score >= MATCH_THRESHOLD else "below threshold"
                print(f"[{score}%] {title} @ {company} ({resume_version}) — {status}")

                time.sleep(random.uniform(2, 4))  # stay comfortably under 20 RPM

            if progressed_this_pass == 0:
                print("No progress made this pass — remaining jobs appear to be failing persistently. Stopping here rather than looping.")
                break

    except FatalAPIError as e:
        print(f"\nStopping run early — {e}")
        print(f"Scored {scored} jobs before stopping; the rest remain queued for next run.")
        return

    remaining = len(db.unscored_job_ids())
    print(f"\nDone. Scored {scored} jobs this run. {remaining} jobs still pending.")


if __name__ == "__main__":
    main()
