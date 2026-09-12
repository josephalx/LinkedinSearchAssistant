"""
LinkedIn Job Search Scraper — Step 1: open search, parse each JD.

This only handles login + search + JD extraction into a list of dicts.
Storage (jobs table) and scoring (matcher) are separate scripts, added next.
"""

import os
import re
import sys
import time
import random
import keyring

# Debug dumps land in the project root, one level up from scripts/;
# db.py lives in data/, a sibling of scripts/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
import db

from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# ---- Config ----
# Don't hardcode credentials — pull from environment variables instead.
# one-time setup, run once in a python shell:
# keyring.set_password("linkedin", "email", "your_email@example.com")
# keyring.set_password("linkedin", "password", "your_password")

LINKEDIN_EMAIL = keyring.get_password("linkedin", "email") or os.environ.get("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = keyring.get_password("linkedin", "password") or os.environ.get("LINKEDIN_PASSWORD")

if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
    # Not actually required: scraping runs logged-out (see login(), unused
    # below), so these credentials aren't used for anything functional right
    # now. Warn instead of crashing, in case authenticated search is ever
    # needed again later.
    print("Note: LinkedIn credentials not configured in keyring/env — not required for logged-out scraping, continuing anyway.")
SEARCH_KEYWORDS = ["android engineer", "react native developer", "software engineer"]
SEARCH_LOCATION = "United States"
SEARCH_DAYS_BACK = 14  # only postings from the last N days

# Dry-run mode: set SCRAPER_DRY_RUN=1 to scrape and print normally, but skip
# every DB write (init_db and insert_job) — useful for demoing/testing a run
# without touching real data. Unset (the default) is normal "prod" behavior.
DRY_RUN = os.environ.get("SCRAPER_DRY_RUN", "").lower() in ("1", "true", "yes")


def random_delay(min_s=2, max_s=5):
    time.sleep(random.uniform(min_s, max_s))


def init_driver():
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


# Logged-out search removes account-ban risk entirely (no account to restrict),
# so login is skipped. Kept here, unused, in case authenticated search is
# ever needed again for full JD text on postings that truncate when logged out.
# def login(driver):
#     driver.get("https://www.linkedin.com/login")
#     random_delay(2, 4)
    # driver.find_element(By.ID, "username").send_keys(LINKEDIN_EMAIL)
    # random_delay(1, 2)
    # driver.find_element(By.ID, "password").send_keys(LINKEDIN_PASSWORD)
    # random_delay(1, 2)
    # driver.find_element(By.XPATH, "//button[@type='submit']").click()
    # random_delay(3, 6)


def build_search_url(keywords, location, start=0, days_back=14):
    kw = quote(keywords)
    loc = quote(location)
    seconds_back = days_back * 86400
    # The normal /jobs/search/ page ignores `start` past the first ~25 results
    # when logged out (it's built for infinite-scroll, not server pagination).
    # This is the underlying AJAX endpoint that page calls to fetch more —
    # it actually respects `start`, and returns the same base-card markup.
    return (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={kw}&location={loc}&sortBy=DD&start={start}&f_TPR=r{seconds_back}"
    )


def hit_authwall(driver):
    """Logged-out search commonly walls off after a page or two of results."""
    return "authwall" in driver.current_url or "session_redirect" in driver.current_url


def is_promoted(card):
    """Skip sponsored cards before we spend time/API calls on them."""
    try:
        card.find_element(By.XPATH, ".//*[contains(text(), 'Promoted')]")
        return True
    except NoSuchElementException:
        return False


def get_job_cards(driver):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.base-card"))
        )
    except TimeoutException:
        # Save what we actually got so selectors can be corrected instead of guessed again.
        with open(os.path.join(PROJECT_ROOT, "debug_search_page.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(os.path.join(PROJECT_ROOT, "debug_search_page.png"))
        raise
    return driver.find_elements(By.CSS_SELECTOR, "div.base-card")


def get_job_url_from_card(card):
    try:
        return card.find_element(By.CSS_SELECTOR, "a.base-card__full-link").get_attribute("href")
    except NoSuchElementException:
        return None


def extract_job_id(url):
    """Stable numeric job ID — grabs the trailing digits in the URL path,
    which works whether the URL is /jobs/view/1234567 or the public
    /jobs/view/some-role-slug-1234567 format."""
    path = url.split("?")[0]
    match = re.search(r"(\d+)/?$", path) or re.search(r"currentJobId=(\d+)", url)
    return match.group(1) if match else None


# These are the commonly-seen class names on LinkedIn's logged-out /jobs/view
# pages. LinkedIn changes markup periodically, so if fields come back None,
# check debug_search_page.html / a saved job page source and adjust selectors.
def extract_jd(driver, job_url):
    """Visit a job's URL directly and pull title/company/JD text."""
    driver.get(job_url)
    random_delay(2, 4)

    # No need to click "Show more" — textContent below already returns the
    # full text regardless of CSS line-clamp, and clicking risks colliding
    # with sign-in modal overlays that sometimes cover the page.

    # Dump the raw page so we can inspect real class names instead of guessing.
    with open(os.path.join(PROJECT_ROOT, "last_job_debug.html"), "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    def safe_text(selector):
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            # textContent instead of .text: .text is visibility-based and can
            # return a truncated string when content is CSS line-clamped,
            # even though the full text is still present in the DOM.
            return driver.execute_script("return arguments[0].textContent", el).strip()
        except NoSuchElementException:
            return None

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


def scrape_search_page(driver, max_jobs=40, source_keyword=None):
    cards = get_job_cards(driver)

    # Collect URLs first (from the search page) before navigating away from it —
    # once we visit a job page directly, the card elements go stale.
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
            jd = extract_jd(driver, url)
            jd["source_keyword"] = source_keyword
            if jd["jd_text"] and jd["job_id"]:
                if DRY_RUN:
                    status = "dry-run, not stored"
                else:
                    inserted = db.insert_job(jd)
                    status = "stored" if inserted else "already in DB"
                results.append(jd)
                print(f"Parsed ({status}): {jd['title']} @ {jd['company']}")
        except (NoSuchElementException, TimeoutException) as e:
            print(f"Skipped {url} due to error: {e}")
            continue

        random_delay(3, 7)  # jittered gap between jobs

    return results


def scrape_keyword(driver, keyword):
    """Paginate through search results for a single keyword."""
    all_jobs = []
    page_size = 25
    max_pages = 20  # cap pages tried; logged-out search often walls off before this
    start = 0

    while start < page_size * max_pages:
        url = build_search_url(keyword, SEARCH_LOCATION, start=start, days_back=SEARCH_DAYS_BACK)
        driver.get(url)
        random_delay(3, 5)

        if hit_authwall(driver):
            print(f"[{keyword}] Hit the sign-in wall at start={start}; stopping pagination.")
            break

        try:
            jobs = scrape_search_page(driver, max_jobs=page_size, source_keyword=keyword)
        except TimeoutException:
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
    driver = init_driver()
    try:
        all_jobs = []
        for keyword in SEARCH_KEYWORDS:
            print(f"\n=== Searching: {keyword} ===")
            all_jobs.extend(scrape_keyword(driver, keyword))
            random_delay(8, 15)  # gap between keyword searches, not just pages

        print(f"\nTotal parsed across all keywords: {len(all_jobs)}")
        for j in all_jobs:
            print(j["job_id"], j["title"], j["company"], f"[{j.get('source_keyword')}]")
        return all_jobs
    finally:
        driver.quit()


if __name__ == "__main__":
    main()