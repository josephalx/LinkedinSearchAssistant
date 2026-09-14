#!/usr/bin/env bash
# run.sh — run the scraper or matcher, in dry-run or prod mode.
#
# Usage:
#   ./run.sh scraper dry      # scrapes and prints, no DB writes
#   ./run.sh scraper prod     # real run, writes to `jobs`
#   ./run.sh scraper-pw dry   # same, via the Playwright port (headless)
#   ./run.sh scraper-pw prod  # real run, writes to `jobs`
#   ./run.sh matcher dry      # scores via real API calls, no DB writes
#   ./run.sh matcher prod     # real run, writes to `matches`
#
# One-time setup: chmod +x run.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

TARGET="${1:-}"
MODE="${2:-}"

usage() {
  echo "Usage: $0 <scraper|scraper-pw|matcher> <dry|prod>"
  echo "  scraper     Selenium scraper (scripts/scrapper.py)"
  echo "  scraper-pw  Playwright port (scripts/scrapper_playwright.py, headless)"
  echo "  matcher     scorer (scripts/matcher.py)"
  exit 1
}

if [[ "$TARGET" != "scraper" && "$TARGET" != "scraper-pw" && "$TARGET" != "matcher" ]]; then
  usage
fi
if [[ "$MODE" != "dry" && "$MODE" != "prod" ]]; then
  usage
fi

if [[ "$TARGET" == "scraper" ]]; then
  SCRIPT="$SCRIPT_DIR/scripts/scrapper.py"
  ENV_VAR="SCRAPER_DRY_RUN"
elif [[ "$TARGET" == "scraper-pw" ]]; then
  # Same dry-run flag as the Selenium scraper — both read SCRAPER_DRY_RUN.
  SCRIPT="$SCRIPT_DIR/scripts/scrapper_playwright.py"
  ENV_VAR="SCRAPER_DRY_RUN"
else
  SCRIPT="$SCRIPT_DIR/scripts/matcher.py"
  ENV_VAR="MATCHER_DRY_RUN"
fi

if [[ "$MODE" == "dry" ]]; then
  echo "=== Running $TARGET in DRY-RUN mode (no DB writes) ==="
  export "$ENV_VAR"=1
else
  echo "=== Running $TARGET in PROD mode (writes to DB) ==="
  unset "$ENV_VAR" || true
fi

exec "$PYTHON" -u "$SCRIPT"
