"""
LinkedIn Job Search Scraper — Playwright port of scrapper.py.

Same logic, same config, same DB writes and dedupe hand-off as the Selenium
version; only the browser driver differs. Kept as a separate file so the
working Selenium scraper stays untouched while this one is evaluated.

Differences that are deliberate, not accidental:
  - Runs headless by default (the Selenium version runs headed). Set
    SCRAPER_HEADED=1 to watch it.
  - Blocks images, fonts, media and stylesheets via request interception.
    Nothing here reads layout or pixels, so those bytes are pure waste —
    this is the main reason Playwright is worth measuring at all.

Usage:
    python3 scripts/scrapper_playwright.py
    SCRAPER_DRY_RUN=1 python3 scripts/scrapper_playwright.py   # no DB writes
    SCRAPER_MAX_PAGES=1 SCRAPER_KEYWORDS="android engineer" \\
        SCRAPER_MAX_JOBS=3 python3 scripts/scrapper_playwright.py  # quick probe
"""

import os
import re
import sys
import time
import random
import signal
import keyring

# Debug dumps land in the project root, one level up from scripts/;
# db.py lives in data/, a sibling of scripts/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db
import dedupe_agent  # same end-of-run cleanup the Selenium scraper triggers

from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---- Config ----
LINKEDIN_EMAIL = keyring.get_password("linkedin", "email") or os.environ.get("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = keyring.get_password("linkedin", "password") or os.environ.get("LINKEDIN_PASSWORD")

if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
    print("Note: LinkedIn credentials not configured in keyring/env — not required for logged-out scraping, continuing anyway.")

SEARCH_KEYWORDS = ["android engineer", "react native developer", "software engineer"]
SEARCH_LOCATION = "United States"
SEARCH_DAYS_BACK = 14  # only postings from the last N days

DRY_RUN = os.environ.get("SCRAPER_DRY_RUN", "").lower() in ("1", "true", "yes")
HEADED = os.environ.get("SCRAPER_HEADED", "").lower() in ("1", "true", "yes")

# Escape hatches for testing — the defaults match scrapper.py exactly.
MAX_PAGES = int(os.environ.get("SCRAPER_MAX_PAGES", "40"))
MAX_JOBS_PER_PAGE = int(os.environ.get("SCRAPER_MAX_JOBS", "25"))
if os.environ.get("SCRAPER_KEYWORDS"):
    SEARCH_KEYWORDS = [k.strip() for k in os.environ["SCRAPER_KEYWORDS"].split(",") if k.strip()]

# Resource types the parser never looks at.
BLOCKED_RESOURCES = {"image", "font", "media", "stylesheet"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def random_delay(min_s=2, max_s=5):
    time.sleep(random.uniform(min_s, max_s))


class Interrupted(Exception):
    """Raised when the dashboard's Stop button (or Ctrl-C) asks us to quit."""


def _handle_stop(signum, frame):
    raise Interrupted(f"signal {signum}")


# The dashboard stops a run with SIGTERM to the whole process group
# (api.py:stop_scraper). Python's default handler exits immediately without
# unwinding, which leaves Playwright's Node driver writing to a pipe that no
# longer has a reader — it dies with an unhandled EPIPE and dumps a Node stack
# trace straight into the scraper log. Turning the signal into an exception
# lets the finally block below close the browser and the driver in order.
signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def init_browser(pw):
    """Returns (browser, page). Mirrors init_driver()'s anti-automation shim."""
    browser = pw.chromium.launch(headless=not HEADED)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
    )
    # Same trick as the Selenium version's Page.addScriptToEvaluateOnNewDocument.
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in BLOCKED_RESOURCES
        else route.continue_(),
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    return browser, context, page


def build_search_url(keywords, location, start=0, days_back=14):
    kw = quote(keywords)
    loc = quote(location)
    seconds_back = days_back * 86400
    # The guest AJAX endpoint the infinite-scroll search page calls behind the
    # scenes — unlike /jobs/search/ it actually respects `start`, and returns
    # the same base-card markup.
    return (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={kw}&location={loc}&sortBy=DD&start={start}&f_TPR=r{seconds_back}"
    )


def hit_authwall(page):
    """Logged-out search walls off after enough requests.

    Deliberate deviation from scrapper.py, which only inspects current_url:
    LinkedIn serves the wall as page *content* at the requested URL rather
    than redirecting, so a URL-only check never fires. It then surfaces as a
    15s selector timeout reported as "No results found" — the right action
    for the wrong reason. Checking the markup catches it immediately.
    """
    if "authwall" in page.url or "session_redirect" in page.url:
        return True
    try:
        html = page.content()
    except Exception:
        return False
    return 'pageKey" content="auth_wall' in html or "authwall" in html.lower()


def get_job_cards(page):
    """Waits for at least one card, then returns all of them."""
    try:
        # state="attached", not the default "visible" — Selenium's
        # presence_of_element_located only requires the node to be in the DOM,
        # and this endpoint returns a bare markup fragment with no stylesheet.
        page.wait_for_selector("div.base-card", state="attached", timeout=15000)
    except PlaywrightTimeoutError:
        # Save what we actually got so selectors can be corrected, not guessed.
        with open(os.path.join(PROJECT_ROOT, "debug_search_page.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_search_page.png"))
        raise
    return page.query_selector_all("div.base-card")


def is_promoted(card):
    """Skip sponsored cards before we spend time/API calls on them."""
    return card.query_selector("xpath=.//*[contains(text(), 'Promoted')]") is not None


def get_job_url_from_card(card):
    link = card.query_selector("a.base-card__full-link")
    return link.get_attribute("href") if link else None


def extract_job_id(url):
    """Stable numeric job ID — grabs the trailing digits in the URL path."""
    path = url.split("?")[0]
    match = re.search(r"(\d+)/?$", path) or re.search(r"currentJobId=(\d+)", url)
    return match.group(1) if match else None


def extract_jd(page, job_url):
    """Visit a job's URL directly and pull title/company/JD text."""
    page.goto(job_url, wait_until="domcontentloaded")
    random_delay(2, 4)

    with open(os.path.join(PROJECT_ROOT, "last_job_debug.html"), "w", encoding="utf-8") as f:
        f.write(page.content())

    def safe_text(selector):
        el = page.query_selector(selector)
        if el is None:
            return None
        # textContent, not inner_text: inner_text is visibility-based and
        # returns a truncated string when the JD is CSS line-clamped.
        text = el.text_content()
        return text.strip() if text else None

    title = safe_text("h1.top-card-layout__title")
    company = safe_text("a.topcard__org-name-link")
    jd_text = safe_text("div.show-more-less-html__markup")

    print(f"  -> title={title!r} company={company!r} jd_len={len(jd_text) if jd_text else 0}")

    return {
        "job_id": extract_job_id(job_url),
        "title": title,
        "company": company,
        "url": job_url,
        "jd_text": jd_text,
    }


def scrape_search_page(page, max_jobs=25, source_keyword=None, new_counts=None):
    cards = get_job_cards(page)

    # Collect URLs first — visiting a job page detaches the card handles.
    job_urls = []
    for card in cards:
        if len(job_urls) >= max_jobs:
            break
        if is_promoted(card):
            continue
        url = get_job_url_from_card(card)
        if url:
            job_urls.append(url)

    results = []
    for url in job_urls:
        try:
            jd = extract_jd(page, url)
            jd["source_keyword"] = source_keyword
            if jd["jd_text"] and jd["job_id"]:
                if DRY_RUN:
                    status = "dry-run, not stored"
                else:
                    inserted = db.insert_job(jd)
                    status = "stored" if inserted else "already in DB"
                    if inserted and new_counts is not None:
                        new_counts[source_keyword] = new_counts.get(source_keyword, 0) + 1
                results.append(jd)
                print(f"Parsed ({status}): {jd['title']} @ {jd['company']}")
        except PlaywrightTimeoutError as e:
            print(f"Skipped {url} due to error: {e}")
            continue

        random_delay(3, 7)  # jittered gap between jobs

    return results


def scrape_keyword(page, keyword, new_counts=None):
    """Paginate through search results for a single keyword."""
    all_jobs = []
    page_size = 25
    start = 0

    while start < page_size * MAX_PAGES:
        url = build_search_url(keyword, SEARCH_LOCATION, start=start, days_back=SEARCH_DAYS_BACK)
        page.goto(url, wait_until="domcontentloaded")
        random_delay(3, 5)

        if hit_authwall(page):
            print(f"[{keyword}] Hit the sign-in wall at start={start}; stopping pagination.")
            break

        try:
            jobs = scrape_search_page(
                page, max_jobs=MAX_JOBS_PER_PAGE, source_keyword=keyword, new_counts=new_counts
            )
        except PlaywrightTimeoutError:
            print(f"[{keyword}] No results found at start={start}; stopping.")
            break

        if not jobs:
            print(f"[{keyword}] No new jobs parsed at start={start}; stopping.")
            break

        all_jobs.extend(jobs)
        start += page_size
        random_delay(5, 10)  # gap between pages, not just between jobs

    return all_jobs


def main():
    if DRY_RUN:
        print("=== DRY RUN: scraping normally, but nothing will be written to the DB ===")
    else:
        db.init_db()

    started = time.time()
    interrupted = False
    with sync_playwright() as pw:
        browser, context, page = init_browser(pw)
        try:
            all_jobs = []
            new_counts = {}  # source_keyword -> count of genuinely new DB inserts
            for keyword in SEARCH_KEYWORDS:
                print(f"\n=== Searching: {keyword} ===")
                all_jobs.extend(scrape_keyword(page, keyword, new_counts=new_counts))
                random_delay(8, 15)  # gap between keyword searches

            print(f"\nTotal parsed across all keywords: {len(all_jobs)}")
            for j in all_jobs:
                print(j["job_id"], j["title"], j["company"], f"[{j.get('source_keyword')}]")

            total_new = sum(new_counts.values())
            print("\n=== New jobs added to DB, by category ===")
            for keyword in SEARCH_KEYWORDS:
                print(f"  {keyword}: {new_counts.get(keyword, 0)}")
            print(f"  TOTAL NEW: {total_new}")
            print(f"\nElapsed: {time.time() - started:.1f}s")

            return all_jobs
        except Interrupted:
            interrupted = True
            print("\n=== Stopped — closing the browser cleanly ===")
            return []
        finally:
            # Drop the route handler before teardown. Closing with requests
            # still in flight cancels their handlers, and Playwright dumps a
            # wall of CancelledError tracebacks — which would stream straight
            # into the dashboard's live log panel.
            #
            # Best-effort: on a Stop, SIGTERM hits the browser and the Node
            # driver at the same moment it hits us, so any of these can find
            # its connection already gone. That's expected, not worth a
            # traceback in the log.
            for step in (
                lambda: context.unroute_all(behavior="ignoreErrors"),
                context.close,
                browser.close,
            ):
                try:
                    step()
                except Exception:
                    pass

            if interrupted:
                # Matches the Selenium scraper's behaviour on Stop: the process
                # was killed outright there, so dedup never ran. Skipping it
                # also avoids firing LLM calls right after a user said stop.
                print("=== Skipping dedup check (run was stopped early) ===")
            else:
                # Same contract as scrapper.py: runs after the browser closes,
                # on every completed run, and honours SCRAPER_DRY_RUN.
                dedupe_agent.run_cleanup()


if __name__ == "__main__":
    main()
