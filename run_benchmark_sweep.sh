#!/usr/bin/env bash
# run_benchmark_sweep.sh — benchmark several scorer models in one run.
#
# Runs comparison_benchmark.py once per model, sequentially, against the same
# input file and with the same classifier, so the only thing that varies
# between results is the scorer. Each run writes its own output file, named
# from the scorer model, so nothing overwrites anything.
#
# Usage:
#   ./run_benchmark_sweep.sh                       # default input
#   ./run_benchmark_sweep.sh path/to/sample.json   # explicit input
#
# Override without editing this file:
#   MODELS="a b c" ./run_benchmark_sweep.sh
#   ROUTER=openrouter CLASSIFIER=... OUTDIR=... ./run_benchmark_sweep.sh
#
# One-time setup: chmod +x run_benchmark_sweep.sh

set -uo pipefail   # deliberately NOT -e: one model failing must not abandon
                   # the models after it. Failures are collected and reported.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

# ---------------------------------------------------------------------------
# Scorer models to sweep — edit this list.
#
# These are the bedrock-runtime ID forms, each verified with a real completion
# in us-east-1. IDs are not portable between endpoints: mantle wants
# qwen.qwen3-vl-235b-a22b-instruct, and openai.gpt-oss-120b rather than
# runtime's openai.gpt-oss-120b-1:0. Re-verify before switching endpoint.
#
# Deliberately absent, having been checked and found unusable on this account:
#   openai.gpt-6.1-sol          no model access; also needs mantle's
#                               /openai/v1 path rather than /v1
#   zai.glm-5.3                 gated — "limited to eligible customers"
#   anthropic.claude-sonnet-5*  no model access, and Anthropic models are
#                               served by the Messages API, not Chat
#                               Completions, so mantle rejects them outright
DEFAULT_MODELS=(
  "mistral.mistral-large-3-675b-instruct"
  "deepseek.v3.2"
  "qwen.qwen3-vl-235b-a22b"
  "zai.glm-5"
)

# MODELS="x y z" overrides the list above without editing the file.
if [[ -n "${MODELS:-}" ]]; then
  read -r -a MODEL_LIST <<< "$MODELS"
else
  MODEL_LIST=("${DEFAULT_MODELS[@]}")
fi

ROUTER="${ROUTER:-bedrock}"
OUTDIR="${OUTDIR:-benchmark_results_bedrock}"

# Bedrock attributes usage to a project via the OpenAI-Project header. The
# OpenAI SDK sets that header itself when OPENAI_PROJECT_ID is in the
# environment, so nothing in the Python needs to know about it — but the
# scripts never load .env, so the value has to be put there by this script or
# the header silently goes unsent and the usage is untracked.
#
# Read without sourcing .env, so nothing in that file is executed.
read_env_file() {
  local key="$1" file="$SCRIPT_DIR/.env"
  [[ -f "$file" ]] || return 0
  # -E on BOTH seds: the unquoting expressions use ERE groups, and BSD sed
  # errors with "\1 not defined in the RE" without it, yielding an empty
  # value rather than a visible failure.
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//p" "$file" \
    | tail -1 | tr -d '\r' \
    | sed -E -e 's/[[:space:]]*$//' -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'\$/\1/"
}

OPENAI_PROJECT_ID="${OPENAI_PROJECT_ID:-$(read_env_file OPENAI_PROJECT_ID)}"
if [[ -n "$OPENAI_PROJECT_ID" ]]; then
  export OPENAI_PROJECT_ID
else
  echo "WARNING: OPENAI_PROJECT_ID is not set and not in .env." >&2
  echo "         Runs will work, but Bedrock usage will not be attributed" >&2
  echo "         to a project. Add OPENAI_PROJECT_ID=proj_... to .env." >&2
  echo >&2
fi

# Pinned across every run on purpose. The classifier picks which resume a job
# is scored against, so letting it vary per run would mean a scorer could look
# better merely because a different resume was chosen for it.
CLASSIFIER="${CLASSIFIER:-nvidia.nemotron-nano-9b-v2}"

# The input is always passed explicitly, never left to comparison_benchmark's
# own default. That default is derived from BENCHMARK_DIR, which this script
# overrides to OUTDIR — so without this the run would look for the sample
# inside the results folder and fail on every model.
INPUT="${1:-$SCRIPT_DIR/benchmarks_results/matches_export.json}"

if [[ ! -f "$INPUT" ]]; then
  echo "Input file not found: $INPUT" >&2
  echo "Pass one explicitly:  $0 path/to/sample.json" >&2
  exit 1
fi

mkdir -p "$SCRIPT_DIR/$OUTDIR"

echo "=== Benchmark sweep ==="
echo "  router:     $ROUTER"
echo "  classifier: $CLASSIFIER   (pinned)"
echo "  output dir: $OUTDIR"
echo "  project:    ${OPENAI_PROJECT_ID:-<none — usage untracked>}"
echo "  input:      $INPUT"
echo "  models:     ${#MODEL_LIST[@]}"
for m in "${MODEL_LIST[@]}"; do echo "                $m"; done
echo

SUCCEEDED=()
FAILED=()
SWEEP_START=$SECONDS

for model in "${MODEL_LIST[@]}"; do
  echo "-----------------------------------------------------------------"
  echo ">>> $model"
  echo "-----------------------------------------------------------------"
  START=$SECONDS

  # BENCHMARK_DIR moves input *and* output, so the input path is passed
  # explicitly — otherwise the run would look for the sample inside OUTDIR.
  BENCHMARK_ROUTER="$ROUTER" \
  BENCHMARK_SCORER="$model" \
  BENCHMARK_CLASSIFIER="$CLASSIFIER" \
  BENCHMARK_DIR="$OUTDIR" \
  "$PYTHON" -u "$SCRIPT_DIR/scripts/comparison_benchmark.py" ${INPUT:+"$INPUT"}

  STATUS=$?
  ELAPSED=$((SECONDS - START))

  if [[ $STATUS -eq 0 ]]; then
    SUCCEEDED+=("$model (${ELAPSED}s)")
    echo "<<< $model finished in ${ELAPSED}s"
  else
    FAILED+=("$model (exit $STATUS after ${ELAPSED}s)")
    echo "<<< $model FAILED with exit $STATUS after ${ELAPSED}s — continuing"
  fi
  echo
done

echo "================================================================="
echo "Sweep finished in $((SECONDS - SWEEP_START))s"
echo "  succeeded: ${#SUCCEEDED[@]}"
for m in "${SUCCEEDED[@]:-}"; do [[ -n "$m" ]] && echo "     $m"; done
if (( ${#FAILED[@]} )); then
  echo "  failed:    ${#FAILED[@]}"
  for m in "${FAILED[@]}"; do echo "     $m"; done
fi
echo
echo "Results in $OUTDIR/:"
ls -1 "$SCRIPT_DIR/$OUTDIR" 2>/dev/null | sed 's/^/     /' || true

# Non-zero if anything failed, so the sweep can be chained in CI or a cron.
(( ${#FAILED[@]} == 0 ))
