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
import sys
import json
import time
import random
import keyring


from docx import Document
from openai import OpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError

# db.py lives in data/, a sibling of scripts/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db


# ---- Routers ------------------------------------------------------------
#
# Any OpenAI-compatible gateway works. A router is just a base URL plus where
# to find its key; add an entry here and it becomes usable by name everywhere
# (matcher, dedupe_agent, both benchmarks).
#
# Pick one with LLM_ROUTER=orcarouter, or point somewhere unlisted with
# LLM_BASE_URL=https://… (plus LLM_API_KEY). comparison_benchmark.py can also
# pass a client straight to call_model(), so a single run can compare routers.
ROUTERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "keyring": ("openrouter", "api_key"),
    },
    "orcarouter": {
        "base_url": "https://api.orcarouter.ai/v1",
        "env_key": "ORCAROUTER_API_KEY",
        "keyring": ("orcarouter", "api_key"),
    },
}

DEFAULT_ROUTER = os.environ.get("LLM_ROUTER", "openrouter").strip().lower()

# Optional attribution header — names this app in OpenRouter's activity view
# and public app rankings. The documented header is "X-Title"; anything else
# is ignored, so the name has to be exact. Its companion is "HTTP-Referer"
# (a URL), only worth setting if this ever gets a public page.
APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "LinkedinBot")
DEFAULT_HEADERS = {"X-Title": APP_TITLE}

# Reasoning tokens. Off by default: enabling it changes what the model
# produces and what it costs, so turning it on mid-benchmark would make runs
# non-comparable. MATCHER_REASONING=1 switches it on for every call.
REASONING_ENABLED = os.environ.get("MATCHER_REASONING", "").lower() in ("1", "true", "yes")

# Streaming stays on: tokens arriving incrementally reset the read-timeout
# clock, which is what stopped slower models timing out mid-response.
STREAMING = os.environ.get("MATCHER_STREAM", "1").lower() in ("1", "true", "yes")

REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))

# Requests-per-minute ceiling, applied per REQUEST rather than per job. This
# matters because scoring a job costs two calls (classify + score): a per-job
# sleep still lets those two fire back to back, so a 15 RPM account gets
# tripped even with a generous gap between jobs.
#
# 0 (the default) disables it, leaving the existing per-job sleeps as the only
# pacing. Set LLM_RPM=15 for a model with that limit.
RPM_LIMIT = float(os.environ.get("LLM_RPM", "0"))
MIN_REQUEST_INTERVAL = 60.0 / RPM_LIMIT if RPM_LIMIT > 0 else 0.0

_last_request_at = 0.0


def _throttle():
    """Hold off until MIN_REQUEST_INTERVAL has passed since the last request.

    Single-threaded by assumption — matcher, dedupe_agent and the benchmarks
    all run their calls sequentially, so a plain module-level timestamp is
    enough and avoids dragging a lock into the hot path.
    """
    global _last_request_at
    if MIN_REQUEST_INTERVAL <= 0:
        return
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def resolve_api_key(router_name):
    """Key for a router: its own env var first, then keyring, then the
    generic LLM_API_KEY. Returns None if nothing is configured."""
    spec = ROUTERS.get(router_name, {})
    env_key = spec.get("env_key")
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    kr = spec.get("keyring")
    if kr:
        for username in (kr[1], f"{kr[1]}_backup"):
            value = keyring.get_password(kr[0], username)
            if value:
                return value
    return os.environ.get("LLM_API_KEY")


_client_cache = {}


def make_client(base_url=None, api_key=None, router=None):
    """An OpenAI client for a given gateway, cached per (base_url, key).

    Cached because the SDK holds a connection pool — building a fresh client
    per call would discard keep-alive and slow every request down.
    """
    router = (router or DEFAULT_ROUTER).strip().lower()
    if base_url is None:
        base_url = os.environ.get("LLM_BASE_URL") or ROUTERS.get(router, {}).get(
            "base_url", ROUTERS["openrouter"]["base_url"])
    if api_key is None:
        api_key = resolve_api_key(router)

    cache_key = (base_url, api_key)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = OpenAI(
            base_url=base_url,
            api_key=api_key or "missing",
            default_headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
            max_retries=0,  # we do our own, with router-aware error classification
        )
    return _client_cache[cache_key]


# The default client every caller gets unless one is passed explicitly.
client = make_client()

# Kept for callers that still read these names.
OPENROUTER_BASE_URL = str(client.base_url)
OPENROUTER_API_KEY = resolve_api_key(DEFAULT_ROUTER)

# Dry-run mode: set MATCHER_DRY_RUN=1 to score jobs normally (still calls both
# models, still costs API quota) but skip writing anything to `matches` —
# useful for demoing/testing the run without touching real backlog data.
# Unset (the default) is the normal "prod" behavior: scores get saved.
DRY_RUN = os.environ.get("MATCHER_DRY_RUN", "").lower() in ("1", "true", "yes")


# Two models, two separate OpenRouter rate-limit pools — classification calls
# don't eat into the scoring model's daily quota, and vice versa.
CLASSIFIER_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
SCORER_MODEL = "inclusionai/ling-3.0-flash-vl:free"

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


def dispatch_error(error, status, retry_after, model, attempt):
    """Shared error-handling logic for both request-stage failures and
    mid-stream error events — same dispatch either way, since OpenRouter uses
    the same error_type vocabulary in both places.

    `status` is the HTTP status (or None mid-stream) and `retry_after` the
    Retry-After header value (or None). Taking them as plain arguments keeps
    this independent of whichever HTTP client produced them.

    Returns "retry" (caller should continue the retry loop) or "stop"
    (caller should give up on this call and return None). Raises
    FatalAPIError directly for run-ending problems.
    """
    metadata = error.get("metadata") or {}
    error_type = metadata.get("error_type")
    code = error.get("code", status)

    def backoff(label):
        wait = 2 ** (attempt + 1)
        if retry_after:
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                pass
        print(f"  {label} on {model}; waiting {wait}s before retry.")
        time.sleep(wait)
        return "retry"

    # Checked before the 429 branch on purpose: gateways return 429 both for
    # genuine rate limiting and for "this model isn't available to your
    # account", and only this flag tells them apart. Without it a permanent
    # denial burns every retry on every pass before the run gives up.
    if metadata.get("retryable") is False:
        raise FatalAPIError(f"{model}: {error.get('message')} (marked non-retryable)")

    if error_type == "rate_limit_exceeded" or code == 429:
        return backoff("Rate limited")

    if error_type == "payment_required" or code == 402:
        raise FatalAPIError(f"{model}: out of credits (402). Add credits and rerun.")

    if error_type == "authentication" or code in (401, 403):
        raise FatalAPIError(f"{model}: invalid API key ({code}). Check OPENROUTER_API_KEY.")

    if (error_type in ("provider_overloaded", "provider_unavailable", "server", "timeout", "unmapped")
            or (isinstance(code, int) and code >= 500)):
        return backoff(f"Transient error ({error_type or code})")

    print(f"  Non-retryable error from {model}: {error.get('message')} (error_type={error_type})")
    return "stop"


# Tally of how each call was billed, filled in by record_usage(). A caller can
# print this at the end of a run — see comparison_benchmark.py.
BYOK_STATS = {"byok": 0, "shared": 0, "unknown": 0}


def record_usage(model, usage):
    """Log how a completed call was billed.

    OpenRouter's is_byok is True when the call went through your own provider
    key, False when it silently fell back to OpenRouter's shared credits.
    Only the fallback is worth printing per-call — that's the case that costs
    credits you didn't intend to spend.
    """
    if not usage:
        BYOK_STATS["unknown"] += 1
        return

    is_byok = usage.get("is_byok")
    if is_byok is True:
        BYOK_STATS["byok"] += 1
    elif is_byok is False:
        BYOK_STATS["shared"] += 1
        cost = usage.get("cost")
        print(f"  NOTE: {model} call was NOT billed to your key "
              f"(is_byok=false, fell back to OpenRouter credits"
              f"{f', cost={cost}' if cost is not None else ''})")
    else:
        # Field absent — older endpoint, or usage accounting not honoured.
        BYOK_STATS["unknown"] += 1


def byok_summary():
    """One-line billing summary for the end of a run."""
    s = BYOK_STATS
    total = s["byok"] + s["shared"] + s["unknown"]
    if not total:
        return "No model calls made."
    parts = [f"{s['byok']} on your key"]
    if s["shared"]:
        parts.append(f"{s['shared']} on OpenRouter credits")
    if s["unknown"]:
        parts.append(f"{s['unknown']} unreported")
    return f"Billing: {total} call(s) — " + ", ".join(parts)


def _chunk_dict(obj):
    """SDK objects as plain dicts, including fields the SDK doesn't model.

    OpenRouter puts provider errors and is_byok in places the OpenAI schema
    doesn't declare; pydantic keeps those in model_extra rather than dropping
    them, and model_dump() surfaces the lot.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    return getattr(obj, "__dict__", {}) or {}


def _usage_dict(obj):
    """The usage object off a chunk or a completion, as a plain dict."""
    return _chunk_dict(getattr(obj, "usage", None))


def call_model(model, prompt, retries=2, client=None):
    """One chat completion, with retry and OpenRouter-aware error handling.

    Goes through the OpenAI SDK pointed at OpenRouter's base URL. Streaming
    stays on by default (MATCHER_STREAM=0 disables it): tokens arriving
    incrementally reset the read-timeout clock, which is what stopped "Read
    timed out" failures on slower models — Nemotron 3 Ultra measured ~3
    tokens/sec, 50-65s for a full response, right up against a 60s timeout
    even with zero queue delay.

    Errors arrive two ways: as an HTTP status before any tokens (raised by
    the SDK as APIStatusError), or as an error object inside the stream after
    the 200 is already committed. Both go through dispatch_error(), so the
    retry/stop/fatal behaviour is identical either way.

    Pass `client` to send this call through a different gateway — see
    make_client(). Defaults to the module-level client (LLM_ROUTER, or
    OpenRouter).

    Returns the response text, or None once the attempts are spent. Raises
    FatalAPIError for problems no retry will fix.
    """
    if client is None:
        client = globals()["client"]
    extra_body = {
        # Makes OpenRouter append a usage object to the final chunk,
        # including is_byok — see record_usage().
        "usage": {"include": True},
    }
    if REASONING_ENABLED:
        extra_body["reasoning"] = {"enabled": True}

    for attempt in range(retries + 1):
        try:
            _throttle()
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=STREAMING,
                extra_body=extra_body,
            )

            if not STREAMING:
                record_usage(model, _usage_dict(stream))
                return stream.choices[0].message.content

            content_parts = []
            usage = None  # arrives on the final chunk, which carries no choices
            mid_stream_error = None

            for chunk in stream:
                # OpenRouter can report a provider failure inside the stream
                # after the 200 is already committed. The SDK keeps unknown
                # fields in model_extra, so check there as well as in choices.
                error = extract_error(_chunk_dict(chunk))
                if error:
                    mid_stream_error = error
                    break

                if getattr(chunk, "usage", None):
                    usage = _usage_dict(chunk)

                if chunk.choices:
                    content_parts.append(chunk.choices[0].delta.content or "")

            if mid_stream_error:
                outcome = dispatch_error(mid_stream_error, None, None, model, attempt)
                if outcome == "retry":
                    continue
                return None  # "stop"

            record_usage(model, usage)
            return "".join(content_parts)

        except (APIConnectionError, APITimeoutError) as e:
            print(f"  Attempt {attempt + 1} failed ({model}): network error: {e}")
            time.sleep(2 ** (attempt + 1))

        except APIStatusError as e:
            body = e.body if isinstance(e.body, dict) else {}
            error = body.get("error") if isinstance(body.get("error"), dict) else None
            if error is None:
                error = {"code": e.status_code, "message": str(e)[:200], "metadata": {}}
            retry_after = None
            if getattr(e, "response", None) is not None:
                retry_after = e.response.headers.get("Retry-After")

            outcome = dispatch_error(error, e.status_code, retry_after, model, attempt)
            if outcome == "retry":
                continue
            return None  # "stop"

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
