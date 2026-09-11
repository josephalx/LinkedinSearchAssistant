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
    python3 comparison_benchmark.py [path/to/matches_export.json]
    (defaults to matches_export.json in this folder)

Output:
    benchmark_comparison_ultra_guardrails.json — old score/reasoning next to new, per job.
"""

import sys
import json
import time
import random

import db
import matcher  # reuse build_classifier_prompt, build_prompt, call_model,
                 # parse_json_response, requires_clearance, get_job_details,
                 # load_all_resumes, FatalAPIError, DEFAULT_RESUME_VERSION

# The two models being benchmarked — Ultra scorer, with streaming now applied
# to call_model() to address the earlier read-timeout issue on this model.
BENCHMARK_CLASSIFIER_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
BENCHMARK_SCORER_MODEL = "inclusionai/ling-3.0-flash-vl:free"

DEFAULT_INPUT = "matches_export.json"
OUTPUT_PATH = "benchmark_comparison_ling-vl.json"


def classify_resume_type(jd_text):
    content = matcher.call_model(BENCHMARK_CLASSIFIER_MODEL, matcher.build_classifier_prompt(jd_text))
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
    content = matcher.call_model(BENCHMARK_SCORER_MODEL, matcher.build_prompt(resume_text, jd_text))
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
    print(f"Classifier: {BENCHMARK_CLASSIFIER_MODEL}")
    print(f"Scorer:     {BENCHMARK_SCORER_MODEL}")
    print("Guardrails: ON (5+ year experience cap, clearance pre-filter)\n")

    resumes = matcher.load_all_resumes()
    comparison = []

    try:
        for i, old in enumerate(original_records, start=1):
            job_id = old["job_id"]
            title, company, jd_text = matcher.get_job_details(job_id)

            if matcher.requires_clearance(jd_text):
                new_result = {"score": 0, "missing_skills": "N/A", "reasoning": "Excluded: requires security clearance / US citizenship."}
                new_resume_version = "excluded_clearance"
                db.save_benchmark_match(job_id, new_result, new_resume_version, "n/a", "n/a")
                print(f"[{i}/{len(original_records)}] [excluded] {title} @ {company}")
            else:
                new_resume_version = classify_resume_type(jd_text)
                resume_text = resumes[new_resume_version]
                new_result = score_job(resume_text, jd_text)

                if new_result is None:
                    print(f"[{i}/{len(original_records)}] Skipping {job_id} ({title} @ {company}) after failed retries.")
                    continue

                db.save_benchmark_match(job_id, new_result, new_resume_version, BENCHMARK_CLASSIFIER_MODEL, BENCHMARK_SCORER_MODEL)
                print(f"[{i}/{len(original_records)}] [{new_result.get('score')}%] {title} @ {company} ({new_resume_version}) — was {old.get('score')}%")

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
            })

            time.sleep(random.uniform(2, 4))  # stay comfortably under rate limits

    except matcher.FatalAPIError as e:
        print(f"\nStopping early — {e}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(comparison)} jobs compared. Written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
