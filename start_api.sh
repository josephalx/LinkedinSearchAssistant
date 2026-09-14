#!/usr/bin/env bash
# start_api.sh — start the dashboard API server.
#
# Usage:
#   ./start_api.sh          # defaults to dry-run mode for dashboard-triggered runs
#   ./start_api.sh dry      # same, explicit
#   ./start_api.sh prod     # dashboard-triggered scraper/matcher runs will write to the DB
#
# Scraper engine (which implementation the dashboard's "Run scraper" launches):
#   ./start_api.sh                           # Playwright port (default, headless)
#   SCRAPER_ENGINE=selenium ./start_api.sh   # the original Selenium scraper (headed)
#
# One-time setup: chmod +x start_api.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
MODE="${1:-dry}"

if [[ "$MODE" != "dry" && "$MODE" != "prod" ]]; then
  echo "Usage: $0 [dry|prod]"
  exit 1
fi

if [[ "$MODE" == "dry" ]]; then
  echo "=== Starting API — dashboard-triggered runs will be DRY-RUN (no DB writes) ==="
  export SCRAPER_DRY_RUN=1
  export MATCHER_DRY_RUN=1
else
  echo "=== Starting API — dashboard-triggered runs will be PROD (writes to DB) ==="
  export SCRAPER_DRY_RUN=0
  export MATCHER_DRY_RUN=0
fi

# Passed through to api.py, which picks the scraper script from it.
export SCRAPER_ENGINE="${SCRAPER_ENGINE:-playwright}"

exec "$PYTHON" -u "$SCRIPT_DIR/dashboard/api.py"
