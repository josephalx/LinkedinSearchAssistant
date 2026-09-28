"""
LinkedIn apply-route MCP server — tells an agent how a job can be applied to,
and hands back the employer's application URL when there is one.

    check_job(job_url)      what kind of apply this is, and the URL if external
    login_status()          whether the persisted session is still signed in
    clear_session()         wipe the stored session (sign out / start fresh)

Two-tier by design, because the two tiers carry very different risk:

  * Guest tier (default). Loads the public job page with no account attached.
    LinkedIn labels the apply route even to logged-out visitors — an external
    job carries `apply-link-offsite` / `apply-button__offsite-apply-icon-svg`,
    and a closed one says so — so easy_apply / external / closed are all
    detectable here. No account, so no account to restrict.

  * Authenticated tier. Only entered when the guest tier says the job is
    external AND the caller asked for the URL, because the real employer URL
    is behind a sign-in wall (a logged-out page contains only
    linkedin.com/signup/cold-join links). "Already applied" also lives here —
    it is per-user state that cannot exist without a session.

The session is persisted in a real browser profile directory rather than
re-logging-in per call. Besides avoiding repeated logins, a stable profile
keeps the device fingerprint consistent, which is what keeps LinkedIn from
throwing checkpoints. li_at cookies last weeks, so a login should be rare.

Credentials come from keyring ('linkedin', 'email'/'password'). They are never
written to disk by this server; the profile directory holds session cookies
and is gitignored.

Transport is stdio: stdout carries JSON-RPC, so nothing here prints to it.

Setup:
    pip install "mcp[cli]" playwright && python3 -m playwright install chromium
    python3 -c "import keyring; keyring.set_password('linkedin','email','you@example.com')"
    python3 -c "import keyring; keyring.set_password('linkedin','password','...')"

Run standalone:
    python3 mcp/linkedin/server.py --selftest          # guest tier only
"""

import os
import re
import sys
import json
import time
import random

from urllib.parse import urlparse, parse_qs, unquote

import keyring
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from mcp.server.mcpserver import MCPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.environ.get("LINKEDIN_PROFILE_DIR", os.path.join(HERE, "profile"))

HEADED = os.environ.get("LINKEDIN_HEADED", "").lower() in ("1", "true", "yes")
NAV_TIMEOUT = int(os.environ.get("LINKEDIN_TIMEOUT", "30000"))

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Resource types no detector here reads. Blocking them cuts page weight and
# time-to-answer substantially.
BLOCKED = {"image", "font", "media"}


def log(message):
    """stderr only — stdout belongs to the MCP protocol."""
    print(f"[linkedin-mcp] {message}", file=sys.stderr, flush=True)


def pause(lo=0.8, hi=2.0):
    """Small jitter between actions. Not a serious evasion measure — just
    avoids hammering a page instantly after load."""
    time.sleep(random.uniform(lo, hi))


def normalise_job_url(job_url):
    """Accept a full URL or a bare numeric job id."""
    job_url = str(job_url).strip()
    if job_url.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_url}/"
    if "linkedin.com/jobs/view/" not in job_url:
        return None
    return job_url


def job_id_from(job_url):
    match = re.search(r"(\d+)(?:/|\?|$)", job_url.split("?")[0])
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Guest tier — no account attached
# ---------------------------------------------------------------------------

def classify_guest(html, page_url):
    """Work out the apply route from a logged-out job page.

    Markers taken from a real scraped page rather than guessed:
      external  -> 'apply-link-offsite' in a trk param, or the offsite icon
      closed    -> "no longer accepting applications"
      easy      -> an apply affordance with no offsite marker
    """
    lowered = html.lower()

    if "no longer accepting applications" in lowered or "this job is no longer available" in lowered:
        return "closed"
    if "apply-link-offsite" in lowered or "offsite-apply-icon" in lowered:
        return "external"
    if "easy apply" in lowered or "easyapply" in lowered:
        return "easy_apply"
    if "apply-button" in lowered or "jobs-apply" in lowered:
        # An apply affordance with no offsite marker is Easy Apply in practice.
        return "easy_apply"
    if "authwall" in page_url or "/login" in page_url:
        return "authwall"
    return "unknown"


def inspect_guest(job_url):
    """Load the public page and classify it. Returns (status, title, company)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not HEADED)
        context = browser.new_context(user_agent=USER_AGENT,
                                      viewport={"width": 1440, "height": 900})
        context.route("**/*", lambda r: r.abort()
                      if r.request.resource_type in BLOCKED else r.continue_())
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT)
        try:
            page.goto(job_url, wait_until="domcontentloaded")
            pause()
            html = page.content()
            status = classify_guest(html, page.url)
            title = text_or_none(page, "h1.top-card-layout__title")
            company = text_or_none(page, "a.topcard__org-name-link")
            return status, title, company
        finally:
            context.unroute_all(behavior="ignoreErrors")
            context.close()
            browser.close()


def first_visible(page, selector):
    """First matching element that's actually on screen.

    LinkedIn ships duplicate markup for responsive layouts — the login page
    carries two email/password pairs with only the second visible — so taking
    query_selector()'s first match fills a hidden field and silently fails.
    """
    for element in page.query_selector_all(selector):
        try:
            if element.is_visible():
                return element
        except Exception:
            continue
    return None


def _settled_content(page, tries=3):
    """page.content() races LinkedIn's post-load redirect; retry briefly."""
    for _ in range(tries):
        try:
            return page.content()
        except Exception:
            page.wait_for_timeout(600)
    return ""


def classify_many(job_urls):
    """Guest-classify several jobs using one browser. {url: status}.

    Two things this gets right that a naive loop doesn't:

      * One browser and one page for the whole batch. Launching per job costs
        ~0.3s each and buys nothing.
      * The jitter between requests is kept. Measured: at full speed (~0.8s
        per check) LinkedIn served the sign-in wall for 6 of 10 requests,
        which classifies as nothing useful. With the pause it's ~3.4s each
        and the verdicts are reliable. Slower is the point.

    No account is involved — this is the guest tier.
    """
    results = {}
    if not job_urls:
        return results

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not HEADED)
        context = browser.new_context(user_agent=USER_AGENT,
                                      viewport={"width": 1440, "height": 900})
        context.route("**/*", lambda r: r.abort()
                      if r.request.resource_type in BLOCKED else r.continue_())
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT)
        try:
            for url in job_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    pause()
                    results[url] = classify_guest(_settled_content(page), page.url)
                except Exception as e:
                    log(f"guest check failed for {url}: {type(e).__name__}")
                    results[url] = "error"
        finally:
            context.unroute_all(behavior="ignoreErrors")
            context.close()
            browser.close()
    return results


def resolve_urls(job_urls):
    """Signed-in pass over jobs already known to be external.

    {url: {"application_url": str|None, "outcome": str}}. One persistent
    context for the batch, so the session is reused and there's a single
    authenticated page load per job — the only account traffic in the flow.
    """
    results = {}
    if not job_urls:
        return results

    with sync_playwright() as pw:
        context = open_authenticated(pw)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not is_signed_in(page):
                ok, message = sign_in(page)
                if not ok:
                    return {url: {"application_url": None, "outcome": f"login_failed: {message}"}
                            for url in job_urls}
            for url in job_urls:
                try:
                    resolved, outcome, _label = resolve_external_url(page, url)
                    results[url] = {"application_url": resolved, "outcome": outcome}
                except Exception as e:
                    log(f"resolve failed for {url}: {type(e).__name__}")
                    results[url] = {"application_url": None, "outcome": f"error: {type(e).__name__}"}
        finally:
            context.close()
    return results


def text_or_none(page, selector):
    element = page.query_selector(selector)
    if element is None:
        return None
    value = element.text_content()
    return value.strip() if value else None


# ---------------------------------------------------------------------------
# Authenticated tier — persisted profile, entered only when needed
# ---------------------------------------------------------------------------

def open_authenticated(pw):
    """A persistent browser context reusing the stored profile.

    launch_persistent_context rather than storage_state: it keeps the whole
    profile (cookies, localStorage, service workers) on disk, so the session
    survives and the fingerprint stays stable between runs.
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=not HEADED,
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
    )
    context.set_default_timeout(NAV_TIMEOUT)
    return context


def is_signed_in(page):
    """Signed in if the feed loads without bouncing to login/authwall."""
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    pause(0.5, 1.2)
    url = page.url
    return not any(marker in url for marker in ("/login", "authwall", "/signup", "checkpoint"))


def sign_in(page):
    """Log in with the keyring credentials. Returns (ok, message).

    Stops at a checkpoint rather than trying to solve it — a 2FA or puzzle
    challenge needs a human (or the Gmail OTP server), and blind retries are
    exactly what escalates to a restriction.
    """
    email = keyring.get_password("linkedin", "email")
    password = keyring.get_password("linkedin", "password")
    if not email or not password:
        return False, ("No LinkedIn credentials in keyring. Set them with "
                       "keyring.set_password('linkedin','email'|'password', ...).")

    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    pause(1.5, 2.5)  # the form is client-rendered; it isn't there on DOMContentLoaded

    # LinkedIn renders the login form with randomised React ids
    # ("«Rsvvriejj35659j6»"), no name attributes and no <form> element, and
    # ships two copies of it with only the second visible. So: match on input
    # type, take the first *visible* one, and fall back to the legacy ids in
    # case an older form is ever served.
    email_box = first_visible(page, "#username") or first_visible(page, "input[type=email]")
    password_box = first_visible(page, "#password") or first_visible(page, "input[type=password]")
    if email_box is None or password_box is None:
        return False, ("Could not find the login fields. LinkedIn may have changed the "
                       "form again — re-run with LINKEDIN_HEADED=1 to look.")

    try:
        email_box.fill(email)
        pause(0.3, 0.8)
        password_box.fill(password)
        pause(0.3, 0.8)

        # The submit control is type="button", not submit, and "Sign in with
        # Apple" also starts with "Sign in" — so match the label exactly.
        submit = page.get_by_role("button", name="Sign in", exact=True)
        count = submit.count()
        clicked = False
        for i in range(count):
            candidate = submit.nth(i)
            if candidate.is_visible():
                candidate.click()
                clicked = True
                break
        if not clicked:
            legacy = first_visible(page, "button[type=submit]")
            if legacy is None:
                return False, "Found the login fields but no visible Sign in button."
            legacy.click()

        page.wait_for_load_state("domcontentloaded")
        pause(2.0, 3.5)
    except PWTimeout:
        return False, "Login form did not respond as expected."

    url = page.url
    if "checkpoint" in url or "challenge" in url:
        return False, ("LinkedIn raised a verification checkpoint. Re-run with "
                       "LINKEDIN_HEADED=1 and complete it once by hand — the "
                       "persisted profile will remember it.")

    # Confirm by loading the feed rather than inspecting the post-submit URL.
    # LinkedIn redirects through intermediate URLs that still contain "login",
    # so a substring check reports failure on a sign-in that actually worked.
    if is_signed_in(page):
        return True, "Signed in."
    return False, "Login was rejected. Check the stored email/password."


def unwrap_safety_url(href):
    """Pull the employer URL out of LinkedIn's redirect wrapper.

    External apply links point at linkedin.com/safety/go/?url=<encoded>, not
    at the employer directly. Unwrapping gives the real destination without
    ever requesting LinkedIn's redirector.
    """
    if not href:
        return None
    if "/safety/go" not in href:
        return href
    query = urlparse(href).query
    target = parse_qs(query).get("url", [None])[0]
    return unquote(target) if target else href


def find_apply_control(page):
    """The Apply affordance, identified by its accessible label.

    Class names are CSS-module hashes ("_7e411247 _4a81d71a") so they're
    useless as selectors, and the element is an <a> whose visible text is just
    "Apply" — the aria-label is what actually distinguishes an external apply
    ("Apply on company website") from Easy Apply. Returns (href, aria, text).
    """
    found = page.eval_on_selector_all(
        "a, button",
        """els => els
             .filter(e => e.offsetParent !== null &&
                          /apply/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')))
             .map(e => ({
                 href: e.getAttribute('href'),
                 aria: e.getAttribute('aria-label') || '',
                 text: (e.innerText || '').trim()
             }))""")
    for element in found:
        # Skip "save"/"share" controls that merely mention applying.
        if element["href"] or "apply" in element["aria"].lower() or \
           element["text"].lower() in ("apply", "easy apply", "applied"):
            return element
    return None


def resolve_external_url(page, job_url):
    """Read the apply route off a signed-in job page.

    No clicking: the employer URL sits in the Apply link's href (behind
    LinkedIn's /safety/go wrapper), so it can be read directly. That avoids
    opening a popup, avoids loading the employer's site, and doesn't depend
    on timing a navigation.
    """
    page.goto(job_url, wait_until="domcontentloaded")
    pause(2.0, 3.5)  # the apply control is client-rendered

    body = page.inner_text("body").lower()
    if "no longer accepting applications" in body:
        return None, "closed", None
    if "you applied" in body or "application submitted" in body:
        return None, "already_applied", None

    control = find_apply_control(page)
    if control is None:
        return None, "no_apply_button", None

    label = control["aria"] or control["text"]
    lowered = label.lower()

    if "easy apply" in lowered or control["text"].lower() == "easy apply":
        return None, "easy_apply", label
    if control["text"].lower() == "applied":
        return None, "already_applied", label

    url = unwrap_safety_url(control["href"])
    if url and "linkedin.com" not in urlparse(url).netloc:
        return url, "external", label
    if url:
        # Still a LinkedIn URL after unwrapping — an in-platform apply flow.
        return None, "easy_apply", label
    return None, "external_url_not_captured", label


mcp = MCPServer(
    name="linkedin",
    instructions=(
        "Checks how a LinkedIn job can be applied to. check_job() returns one "
        "of: external (with the employer's application URL), easy_apply, "
        "closed, or already_applied. It works without signing in unless the "
        "job is external and you ask for the URL, which requires a session. "
        "Use login_status() to see whether the stored session is still valid."
    ),
)


@mcp.tool(description="Check how a LinkedIn job can be applied to; returns the external application URL when there is one.")
def check_job(job_url: str, resolve_url: bool = True) -> str:
    """Guest tier first; sign in only if the job is external and a URL is wanted."""
    url = normalise_job_url(job_url)
    if url is None:
        return json.dumps({
            "ok": False,
            "error": f"Not a LinkedIn job URL: {job_url!r}",
            "hint": "Pass a linkedin.com/jobs/view/... URL or a bare numeric job id.",
        }, indent=2)

    try:
        status, title, company = inspect_guest(url)
    except Exception as e:
        log(f"guest check failed: {type(e).__name__}: {e}")
        return json.dumps({"ok": False, "job_url": url,
                           "error": f"Could not load the job page: {type(e).__name__}: {e}"}, indent=2)

    log(f"check_job {job_id_from(url)} -> guest says {status}")
    base = {"ok": True, "job_id": job_id_from(url), "job_url": url,
            "title": title, "company": company, "apply_type": status}

    if status == "closed":
        return json.dumps({**base, "message": "No longer accepting applications."},
                          indent=2, ensure_ascii=False)
    if status == "easy_apply":
        return json.dumps({**base, "message": "Easy Apply — applied through LinkedIn, no external URL."},
                          indent=2, ensure_ascii=False)
    if status != "external":
        return json.dumps({**base, "message": f"Could not determine the apply route ({status})."},
                          indent=2, ensure_ascii=False)

    if not resolve_url:
        return json.dumps({**base, "message": "External apply. Set resolve_url=true to sign in and fetch the URL."},
                          indent=2, ensure_ascii=False)

    # External + URL wanted: this is the only path that touches the account.
    try:
        with sync_playwright() as pw:
            context = open_authenticated(pw)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if not is_signed_in(page):
                    ok, message = sign_in(page)
                    if not ok:
                        return json.dumps({**base, "ok": False, "error": message}, indent=2)
                resolved, outcome, label = resolve_external_url(page, url)
            finally:
                context.close()
    except Exception as e:
        log(f"authenticated check failed: {type(e).__name__}: {e}")
        return json.dumps({**base, "ok": False,
                           "error": f"Signed-in check failed: {type(e).__name__}: {e}"}, indent=2)

    if outcome == "already_applied":
        return json.dumps({**base, "apply_type": "already_applied",
                           "message": "You have already applied to this job."},
                          indent=2, ensure_ascii=False)
    if outcome == "closed":
        return json.dumps({**base, "apply_type": "closed",
                           "message": "No longer accepting applications."},
                          indent=2, ensure_ascii=False)
    if outcome == "easy_apply":
        return json.dumps({**base, "apply_type": "easy_apply",
                           "message": "Easy Apply — applied through LinkedIn, no external URL."},
                          indent=2, ensure_ascii=False)
    if resolved:
        return json.dumps({**base, "application_url": resolved, "apply_button": label,
                           "message": "External apply — use application_url."},
                          indent=2, ensure_ascii=False)
    return json.dumps({**base, "ok": False,
                       "error": f"External apply, but the URL could not be captured ({outcome}).",
                       "hint": "Re-run with LINKEDIN_HEADED=1 to watch what the page does."},
                      indent=2, ensure_ascii=False)


@mcp.tool(description="Whether the persisted LinkedIn session is still signed in.")
def login_status() -> str:
    if not os.path.isdir(PROFILE_DIR):
        return json.dumps({"signed_in": False, "profile_exists": False,
                           "message": "No stored session yet; the first external lookup will sign in."},
                          indent=2)
    try:
        with sync_playwright() as pw:
            context = open_authenticated(pw)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                signed = is_signed_in(page)
            finally:
                context.close()
    except Exception as e:
        return json.dumps({"signed_in": False, "error": f"{type(e).__name__}: {e}"}, indent=2)

    return json.dumps({
        "signed_in": signed,
        "profile_exists": True,
        "profile_dir": PROFILE_DIR,
        "message": "Session is valid." if signed else "Session expired; the next lookup will sign in again.",
    }, indent=2)


@mcp.tool(description="Delete the stored LinkedIn session (sign out / start fresh).")
def clear_session() -> str:
    import shutil
    if not os.path.isdir(PROFILE_DIR):
        return json.dumps({"ok": True, "message": "No stored session to clear."}, indent=2)
    shutil.rmtree(PROFILE_DIR)
    log("cleared persisted profile")
    return json.dumps({"ok": True, "message": f"Deleted {PROFILE_DIR}. Next lookup will sign in fresh."},
                      indent=2)


def selftest():
    """Guest tier only — makes no login attempt and touches no account."""
    print("normalise_job_url:")
    for candidate in ("4457396098", "https://www.linkedin.com/jobs/view/4457396098/", "nonsense"):
        print(f"  {candidate!r} -> {normalise_job_url(candidate)!r}")

    print("\nclassify_guest against markers from a real scraped page:")
    samples = {
        "offsite trk":   'href="...trk=public_jobs_apply-link-offsite..."',
        "offsite icon":  '<icon data-svg-class-name="apply-button__offsite-apply-icon-svg">',
        "closed":        "<p>No longer accepting applications</p>",
        "easy apply":    '<button class="jobs-apply-button">Easy Apply</button>',
        "nothing":       "<html><body>hello</body></html>",
    }
    for label, html in samples.items():
        print(f"  {label:14} -> {classify_guest(html, 'https://www.linkedin.com/jobs/view/1/')}")

    print("\nlogin_status (reads the profile dir, no login attempted):")
    print(login_status())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mcp.run(transport="stdio")
