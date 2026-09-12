# LinkedinBot

Scrapes LinkedIn job postings, scores each one against your resume using an
LLM, and shows results in a local web dashboard — with buttons to trigger
scraping/scoring and watch live logs, from your browser or phone.

## How it fits together

```
scripts/scrapper.py  -->  Postgres (jobs table)  -->  scripts/matcher.py  -->  Postgres (matches)
        |                                                             |
        v  (at end of every scrape)                                   v
scripts/dedupe_agent.py --> prunes duplicate  dashboard/api.py  -->  dashboard/dashboard.html
                            jobs before scoring      |
                                              dashboard/run.html (trigger + live logs)
```

- **`scripts/scrapper.py`** — Selenium scrapes LinkedIn's public (logged-out) job search, stores parsed postings in the `jobs` table.
- **`scripts/dedupe_agent.py`** — runs automatically at the end of every `scrapper.py` run: finds jobs sharing a normalized (title, company) but with different JD text, and asks an LLM whether they're the same posting. Duplicates are removed before scoring; distinct ones are flagged so they're never re-examined.
- **`scripts/matcher.py`** — classifies each job (mobile vs. software engineering) and scores it against the matching resume via OpenRouter, storing results in `matches`.
- **`data/db.py`** — all Postgres connection/schema logic. **This is where DB credentials live.**
- **`dashboard/api.py`** — a local Flask API: serves match data to the dashboard, and can trigger/stop the scraper and matcher with live log streaming over WebSocket.
- **`dashboard/dashboard.html`** — browse, filter, sort matches; mark jobs as applied.
- **`dashboard/run.html`** — buttons to run/stop the scraper and matcher, with a live scrolling log panel.
- **`run.sh` / `run.bat`** — run the scraper or matcher directly from the terminal, in dry-run or prod mode.
- **`scripts/`** — the runnable Python entry points; `run.sh`, `start_api.sh`, and `dashboard/api.py` all point at it.
- **`data/`** — the database layer (`db.py`); every script puts it on `sys.path` before importing it.
- **`start_api.sh` / `start_api.bat`** — start the API server, in dry-run or prod mode.
- **`scripts/export_matches.py`** — dumps `matches` + `jobs` to `results/matches_export.json` (a fallback if you ever want a static export instead of the live dashboard).
- **`results/`** — exported match data. **`benchmarks_results/`** — `comparison_benchmark.py`'s side-by-side model dumps.

## 1. Prerequisites

- **Python 3.11+**
- **PostgreSQL** running locally (or a remote instance — see step 3)
- **Google Chrome** installed (Selenium drives it directly)

## 2. Install dependencies

From the project root, ideally inside a virtual environment (`.venv`):

```bash
pip install selenium psycopg2-binary requests python-docx keyring flask flask-cors flask-socketio
```

## 3. Database setup

Create the database once:

```bash
createdb linkedinbot
```

**Credentials live in `data/db.py`**, at the top:

```python
DB_CONFIG = {
    "dbname": "linkedinbot",
    "host": "localhost",
    "port": 5432,
    # user/password default to local peer auth (your macOS username).
    # Add "user": "..." and "password": "..." here if connecting to a
    # remote Postgres instance (e.g. Neon) instead of a local one.
}
```

Change `host`/`port`/`dbname`, and add `user`/`password` keys if you're pointing at a remote database. No other file needs to know about this — `scrapper.py`, `matcher.py`, `dedupe_agent.py`, and `api.py` all import `db.py` for every database operation.

Tables are created automatically — `scrapper.py`, `matcher.py`, and `api.py` each call `db.init_db()` on startup, which is safe to run repeatedly (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

## 4. One-time credential setup (keyring)

Credentials are stored in your OS's secure credential store (macOS Keychain / Windows Credential Manager) via the `keyring` library — never in a plaintext file. Run each of these once:

```bash
# LinkedIn (optional — scraping currently runs logged-out and doesn't use
# these for anything functional; script prints a note and continues fine
# without them. Kept in case authenticated search is needed again later.)
python3 -c "import keyring; keyring.set_password('linkedin', 'email', 'your_email@example.com')"
python3 -c "import keyring; keyring.set_password('linkedin', 'password', 'your_password')"

# OpenRouter API key (required — used by matcher.py for scoring, and by
# dedupe_agent.py for duplicate judgments)
python3 -c "import keyring; keyring.set_password('openrouter', 'api_key', 'your_openrouter_key')"

# Dashboard token (required for the dashboard's run/stop trigger endpoints)
python3 -c "import keyring; keyring.set_password('linkedinbot', 'dashboard_token', 'pick-a-long-random-string')"
```

On a new machine, these need to be run again — keyring stores are machine-local, not synced.

## 5. Resume paths

`scripts/matcher.py` has a `RESUMES` dict pointing at your two resume `.docx` files:

```python
RESUMES = {
    "software_engineering_v1": "/path/to/Chakola_Joseph_Resume_Software_Engineering.docx",
    "mobile_v1": "/path/to/Chakola_Joseph_Resume_Mobile_App_Developer.docx",
}
```

Update these paths if the resumes move, or if setting this up on a different machine.

## 6. Running it

### Scraper / matcher directly (terminal)

```bash
./run.sh scraper dry     # scrapes and parses, no DB writes
./run.sh scraper prod    # real run, writes to `jobs`
./run.sh matcher dry     # scores via real API calls, no DB writes
./run.sh matcher prod    # real run, writes to `matches`
```
(`run.bat` on Windows, same arguments.) First time on Mac/Linux: `chmod +x run.sh`.

### The dashboard

Start the API first:
```bash
./start_api.sh          # dashboard-triggered runs default to dry-run
./start_api.sh prod     # dashboard-triggered runs will write to the DB
```
(`start_api.bat` on Windows.)

Then open `dashboard/dashboard.html` in a browser to view matches, and `dashboard/run.html` to trigger the scraper/matcher with live logs.

**If you're on a different machine or hostname**, update the hardcoded API address near the top of the `<script>` block in both `dashboard.html` and `run.html`:
```js
const API_BASE = "http://josephs-macbook-pro.local:5050";
```
Change `josephs-macbook-pro.local` to your own machine's `.local` hostname (or its IP address) if `.local` mDNS resolution isn't available on your network.

## 7. Search settings

`scripts/scrapper.py` config near the top:
```python
SEARCH_KEYWORDS = ["android engineer", "react native developer", "software engineer"]
SEARCH_LOCATION = "United States"
SEARCH_DAYS_BACK = 14
```
Edit these to change what gets searched for.

## Notes

- **Dry-run vs. prod** is controlled per-script via `SCRAPER_DRY_RUN` / `MATCHER_DRY_RUN` environment variables — see `run.sh`/`start_api.sh` for how these get set. A plain `python3 scripts/scrapper.py` or `python3 scripts/matcher.py` with no env vars set runs in full "prod" mode (real DB writes). `dedupe_agent.py` reads the same `SCRAPER_DRY_RUN` flag, so a dry-run scrape reports what it *would* remove without deleting anything.
- **Duplicate jobs are handled in two layers.** `db.insert_job()` drops exact content duplicates by JD hash at insert time (no API call). `dedupe_agent.py` then handles the ambiguous case — same normalized title + company, *different* JD text — with a single LLM judgment per candidate. It only ever touches unscored jobs, so anything already in `matches` is never deleted, and it defaults to "keep both" on any API failure. Staffing agencies that reuse one generic title across many real openings are explicitly accounted for in the prompt.
- **`jobs.dedup_checked`** makes that step cheap to repeat — once a job is judged (kept or removed) it's never re-examined, so re-running an interrupted scrape is a fast no-op rather than a fresh round of API calls.
- The dashboard API listens on `0.0.0.0:5050`, reachable from other devices on your local network (e.g. your phone) — it is **not** exposed to the internet, and trigger endpoints require the dashboard token.
- `debug_search_page.html`, `last_job_debug.html`, `scraper_run.log`, `matcher_run.log`, and `*.lock` files are all working artifacts generated automatically — safe to delete anytime; they'll be recreated as needed.
