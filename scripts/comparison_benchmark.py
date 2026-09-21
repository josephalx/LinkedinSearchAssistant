"""
comparison_benchmark.py — reruns a fixed set of job_ids (from a prior
matches_export.json) through NEW classifier/scorer models, storing results
in `match_benchmark` (never touches the real `matches` table or pending
queue) and writing a side-by-side comparison JSON.

This version uses matcher.py's REAL production prompts (5+ year experience
cap, security clearance pre-filter) — matching exactly what a real matcher.py
run would do, for the fairest possible comparison between scorer candidates.

Reuses matcher.py's prompts, call_model(), and error handling directly —
only the model slugs differ.

Usage:
    python3 scripts/comparison_benchmark.py [path/to/matches_export.json]
    (defaults to results/matches_export.json)

Output:
    benchmarks_results/benchmark_comparison_<model>.json — old score/reasoning
    next to new, per job.
"""

import os
import re
import sys
import json
import time
import random

# db.py lives in data/, a sibling of scripts/
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
import db
import matcher  # reuse build_classifier_prompt, build_prompt, call_model,
                 # parse_json_response, requires_clearance, get_job_details,
                 # load_all_resumes, FatalAPIError, DEFAULT_RESUME_VERSION

# The two models being benchmarked — Ultra scorer, with streaming now applied
# to call_model() to address the earlier read-timeout issue on this model.
# Overridable, because a router swap usually needs a model swap too — model
# slugs aren't portable between gateways.
#   BENCHMARK_SCORER=deepseek/deepseek-v4-flash-free BENCHMARK_ROUTER=orcarouter ...
BENCHMARK_CLASSIFIER_MODEL = os.environ.get("BENCHMARK_CLASSIFIER", "inclusionai/ling-3.0-flash-vl:free")
BENCHMARK_SCORER_MODEL = os.environ.get("BENCHMARK_SCORER", "nvidia/nemotron-3-ultra-550b-a55b:free")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCHMARKS_DIR = os.path.join(PROJECT_ROOT, "benchmarks_results")

# Input and output both live in benchmarks_results/. Deliberately NOT
# results/matches_export.json: export_matches.py writes a 10-job "what to apply
# to next" shortlist there, which would silently shrink every benchmark to 10
# jobs. This is a separate, larger, stable sample so runs stay comparable
# across models.
DEFAULT_INPUT = os.path.join(BENCHMARKS_DIR, "matches_export.json")

def model_slug(model):
    """Model id as a filename fragment.

    Slugs carry characters a filename can't: "/" is a path separator and ":"
    is trouble on some filesystems. Everything outside [A-Za-z0-9._-] folds
    to an underscore, so openai/gpt-oss-120b -> openai_gpt-oss-120b and
    inclusionai/ling-3.0-flash-vl:free -> inclusionai_ling-3.0-flash-vl_free.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_")


# Derived from the scorer, so a run can't overwrite another model's results
# just because someone forgot to rename the output. Still overridable:
#   BENCHMARK_OUTPUT=benchmark_comparison_llama.json python3 scripts/comparison_benchmark.py
OUTPUT_PATH = os.path.join(
    BENCHMARKS_DIR,
    os.environ.get("BENCHMARK_OUTPUT")
    or f"benchmark_comparison_{model_slug(BENCHMARK_SCORER_MODEL)}.json",
)


# Which gateway this benchmark runs against. Either a name from
# matcher.ROUTERS (openrouter, orcarouter) or an explicit URL:
#   BENCHMARK_ROUTER=orcarouter    python3 scripts/comparison_benchmark.py
#   BENCHMARK_BASE_URL=https://... python3 scripts/comparison_benchmark.py
# Lets the same 100 jobs and the same prompts be replayed across routers.
BENCHMARK_ROUTER = os.environ.get("BENCHMARK_ROUTER", matcher.DEFAULT_ROUTER).strip().lower()
BENCHMARK_BASE_URL = os.environ.get("BENCHMARK_BASE_URL")
CLIENT = matcher.make_client(base_url=BENCHMARK_BASE_URL, router=BENCHMARK_ROUTER)


def classify_resume_type(jd_text):
    content = matcher.call_model(BENCHMARK_CLASSIFIER_MODEL, matcher.build_classifier_prompt(jd_text), client=CLIENT)
    if content is None:
        return matcher.DEFAULT_RESUME_VERSION
    normalized = content.strip().lower()
    if "mobile" in normalized:
        return "mobile_v1"
    if "software" in normalized:
        return "software_engineering_v1"
    return matcher.DEFAULT_RESUME_VERSION


def score_job(resume_text, jd_text):
    # Uses matcher.build_prompt() directly — the real production prompt,
    # including the 5+ year experience cap rule.
    content = matcher.call_model(BENCHMARK_SCORER_MODEL, matcher.build_prompt(resume_text, jd_text), client=CLIENT)
    if content is None:
        return None
    try:
        return matcher.parse_json_response(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  Failed to parse scorer response: {e}")
        return None


def main():
    db.init_db()  # ensures match_benchmark (and any other schema) exists first

    input_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT

    with open(input_path) as f:
        original_records = json.load(f)

    print(f"Loaded {len(original_records)} job records from {input_path}")
    print(f"Router:     {BENCHMARK_ROUTER} ({CLIENT.base_url})")
    print(f"Classifier: {BENCHMARK_CLASSIFIER_MODEL}")
    print(f"Scorer:     {BENCHMARK_SCORER_MODEL}")
    if matcher.RPM_LIMIT > 0:
        print(f"Pacing:     {matcher.RPM_LIMIT:g} req/min "
              f"(~{matcher.MIN_REQUEST_INTERVAL:.1f}s between calls, 2 calls/job)")
    print("Guardrails: ON (5+ year experience cap, clearance pre-filter)\n")

    resumes = matcher.load_all_resumes()
    comparison = []

    # Multi-pass, mirroring matcher.py's main loop: a job that fails all of
    # call_model()'s attempts is simply not added to seen_this_run, so the next
    # pass picks it up again. Most failures are transient rate limiting, and a
    # later pass lands after the window has moved on.
    seen_this_run = set()
    max_passes = 5

    try:
        for pass_num in range(1, max_passes + 1):
            pending = [r for r in original_records if r["job_id"] not in seen_this_run]
            if not pending:
                break

            print(f"\n--- Pass {pass_num}: {len(pending)} job(s) pending ---")
            progressed_this_pass = 0

            for i, old in enumerate(pending, start=1):
                job_id = old["job_id"]
                title, company, jd_text = matcher.get_job_details(job_id)

                if matcher.requires_clearance(jd_text):
                    new_result = {"score": 0, "missing_skills": "N/A", "reasoning": "Excluded: requires security clearance / US citizenship."}
                    new_resume_version = "excluded_clearance"
                    db.save_benchmark_match(job_id, new_result, new_resume_version, "n/a", "n/a")
                    print(f"[{i}/{len(pending)}] [excluded] {title} @ {company}")
                else:
                    new_resume_version = classify_resume_type(jd_text)
                    resume_text = resumes[new_resume_version]
                    new_result = score_job(resume_text, jd_text)

                    if new_result is None:
                        print(f"[{i}/{len(pending)}] Skipping {job_id} ({title} @ {company}) "
                              f"after failed retries — will retry next pass.")
                        continue

                    db.save_benchmark_match(job_id, new_result, new_resume_version, BENCHMARK_CLASSIFIER_MODEL, BENCHMARK_SCORER_MODEL)
                    print(f"[{i}/{len(pending)}] [{new_result.get('score')}%] {title} @ {company} ({new_resume_version}) — was {old.get('score')}%")

                seen_this_run.add(job_id)
                progressed_this_pass += 1

                comparison.append({
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "url": old.get("url"),
                    "old_resume_version": old.get("resume_version"),
                    "old_score": old.get("score"),
                    "old_missing_skills": old.get("missing_skills"),
                    "old_reasoning": old.get("reasoning"),
                    "new_resume_version": new_resume_version,
                    "new_score": new_result.get("score"),
                    "new_missing_skills": new_result.get("missing_skills"),
                    "new_reasoning": new_result.get("reasoning"),
                    "classifier_model": BENCHMARK_CLASSIFIER_MODEL,
                    "scorer_model": BENCHMARK_SCORER_MODEL,
                    "gateway": str(CLIENT.base_url),
                })

                time.sleep(random.uniform(2, 4))  # stay comfortably under rate limits

            if progressed_this_pass == 0:
                # Every remaining job failed again this pass — the cause isn't
                # transient, so looping further would just burn quota.
                print("No progress made this pass — remaining jobs appear to be "
                      "failing persistently. Stopping here rather than looping.")
                break

    except matcher.FatalAPIError as e:
        print(f"\nStopping early — {e}")
    except KeyboardInterrupt:
        # Everything scored so far is already in `comparison`; fall through
        # to the write rather than throwing the run away.
        print("\nInterrupted — keeping whatever was scored so far.")

    os.makedirs(BENCHMARKS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print(f"\n{matcher.byok_summary()}")

    unfinished = [r["job_id"] for r in original_records if r["job_id"] not in seen_this_run]
    print(f"\nDone. {len(comparison)} of {len(original_records)} jobs compared. "
          f"Written to {OUTPUT_PATH}")
    if unfinished:
        print(f"  {len(unfinished)} never completed: {', '.join(unfinished)}")


if __name__ == "__main__":
    main()
