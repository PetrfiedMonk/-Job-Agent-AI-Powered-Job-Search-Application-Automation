"""
Live apply test — runs the full ATS apply pipeline against a real job URL.
No DB writes. Logs everything to console and screenshots to output/test_screenshots/.

Usage:
  python test_apply.py                   # uses TEST_URL below
  python test_apply.py https://boards.greenhouse.io/stripe/jobs/12345
"""
import sys
import asyncio
import uuid
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding so emoji/special chars don't crash prints
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Make sure we can import job_agent
sys.path.insert(0, str(Path(__file__).parent))

from job_agent.config import load_config
from job_agent.models import (
    Application, ApplicationStatus, JobPosting, JobPlatform,
    TailoredResume, UserProfile, WorkExperience
)
from job_agent.automation.application_agent import ApplicationAgent
from job_agent.automation.ats_handlers import detect_ats
from job_agent.db.field_semantics import FieldSemanticsDB
from job_agent.ai.form_filler import SmartFormFiller
from job_agent.db.run_log import RunLog

# ── Test URL ──────────────────────────────────────────────────────────────────
# Pass a listing page and the test will click the first open job automatically,
# OR pass a direct job URL (boards.greenhouse.io/company/jobs/ID)
TEST_URL = "https://jobs.ashbyhq.com/runway"
# LinkedIn:    TEST_URL = "https://www.linkedin.com/jobs/view/any"   (auto-searches for fresh Easy Apply)
# Indeed:      TEST_URL = "https://www.indeed.com"                    (auto-searches for PM job)
# Greenhouse:  TEST_URL = "https://boards.greenhouse.io/anthropic"
# Lever:       TEST_URL = "https://jobs.lever.co/linear"

# ── Minimal profile for testing ────────────────────────────────────────────────
def make_test_profile() -> UserProfile:
    cfg = load_config()
    return UserProfile(
        name=cfg.profile.name or "Justin Carano",
        email=cfg.profile.email or "justincarano@gmail.com",
        phone=cfg.profile.phone or "3303224746",
        location=cfg.profile.location or "Hudson, OH",
        address_line1=cfg.profile.address_line1 or "1590 Middleton Rd",
        address_line2=cfg.profile.address_line2 or "",
        city=cfg.profile.city or "Hudson",
        state=cfg.profile.state or "OH",
        zip_code=cfg.profile.zip_code or "44236",
        country=cfg.profile.country or "United States",
        linkedin_url=cfg.profile.linkedin_url or "https://linkedin.com/in/justincarano",
        github_url=cfg.profile.github_url or "https://github.com/PetrfiedMonk",
        website=cfg.profile.website or "https://seekbridge.ai",
        summary="Product leader and AI builder with 10+ years experience.",
        skills=["Product Management", "AI/ML", "Python", "SQL", "Data Analysis"],
        target_roles=["Product Manager", "Technical Product Manager"],
        min_salary=80000,
        experience=[
            WorkExperience(
                title="Founder / Product Lead",
                company="SeekBridge.ai",
                start_date="2023",
                end_date="Present",
                achievements=["Built AI job automation platform", "Launched MVP with 500+ users"],
            )
        ],
    )


async def _find_first_greenhouse_job(context, listing_url: str) -> str:
    """Navigate to Greenhouse listing page and return URL of first open job."""
    from urllib.parse import urlparse
    page = await context.new_page()
    try:
        await page.goto(listing_url, timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            await asyncio.sleep(2)

        # Only accept links with /jobs/<4+ digit ID> to avoid culture/blog pages.
        # Try multiple times with waits — Greenhouse SPA may lazy-render job cards.
        href = None
        for _attempt in range(3):
            href = await page.evaluate("""
                () => {
                    const a = [...document.querySelectorAll('a[href]')]
                        .find(el => /\\/jobs\\/\\d{4,}/.test(el.getAttribute('href') || ''));
                    return a ? a.getAttribute('href') : null;
                }
            """)
            if href:
                break
            await asyncio.sleep(2)

        if href:
            if href.startswith("/"):
                parsed = urlparse(listing_url)
                href = f"{parsed.scheme}://{parsed.netloc}{href}"
            await page.close()
            return href
    except Exception as e:
        print(f"[test] Greenhouse auto-nav error: {e}")
    await page.close()
    return listing_url


async def _find_first_lever_job(context, listing_url: str) -> str:
    """Navigate to Lever listing page and return URL of first open job."""
    page = await context.new_page()
    try:
        await page.goto(listing_url, timeout=30000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        await asyncio.sleep(2)

    # Lever SPA renders job cards asynchronously — scroll to trigger lazy loading
    try:
        await page.evaluate("window.scrollTo(0, 400)")
        await asyncio.sleep(1)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    try:
        await page.wait_for_selector(
            "h5.posting-name a, .posting-title a, a[href*='jobs.lever.co/']",
            timeout=10000,
        )
    except Exception:
        await asyncio.sleep(3)

    # Lever listing structure: .posting > h5.posting-name > a
    job_link = await page.query_selector(
        "h5.posting-name a, .posting-title a, div.posting a[href*='/lever.co/'], "
        ".postings-group a[href*='jobs.lever.co']"
    )
    if not job_link:
        # JS fallback: find a link with UUID path segment (lever job IDs are UUIDs)
        href = await page.evaluate("""
            () => {
                const a = [...document.querySelectorAll('a[href]')]
                    .find(el => {
                        const h = el.getAttribute('href') || '';
                        return h.includes('jobs.lever.co') &&
                               /\\/[0-9a-f]{8}-[0-9a-f]{4}-/.test(h);
                    });
                return a ? a.getAttribute('href') : null;
            }
        """)
        if href:
            await page.close()
            return href
    if job_link:
        href = await job_link.get_attribute("href")
        await page.close()
        return href or listing_url
    await page.close()
    return listing_url


async def _find_first_ashby_job(context, listing_url: str) -> str:
    """Navigate to Ashby listing page and return URL of first open job.

    Ashby job URLs are /{company}/{slug} — no '/jobs/' segment in the path.
    We extract the company slug from the URL and find anchor tags whose href
    starts with /{company}/ and contains more than just the company name.
    """
    from urllib.parse import urlparse
    page = await context.new_page()
    await page.goto(listing_url, timeout=30000)
    await page.wait_for_load_state("networkidle")

    company = listing_url.rstrip("/").split("/")[-1]  # "runway"
    links = await page.evaluate(f"""
        () => {{
            const prefix = '/{company}/';
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.startsWith(prefix) && h.length > prefix.length);
        }}
    """)

    if links:
        href = links[0]
        if href.startswith("/"):
            parsed = urlparse(listing_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        await page.close()
        return href

    await page.close()
    return listing_url


async def _find_linkedin_easy_apply_job(context) -> str:
    """Search LinkedIn for a Product Manager Easy Apply job and return its URL."""
    page = await context.new_page()
    search_url = (
        "https://www.linkedin.com/jobs/search/"
        "?keywords=Product+Manager&f_AL=true&f_WT=2"  # f_AL=Easy Apply, f_WT=2=Remote
    )
    await page.goto(search_url, timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        await asyncio.sleep(3)

    # Click the first job card in results
    job_card = await page.query_selector(
        ".jobs-search-results__list-item a.job-card-list__title, "
        ".job-card-container__link, "
        "a[data-control-name='jobcard_title']"
    )
    if job_card:
        href = await job_card.get_attribute("href")
        if href:
            if href.startswith("/"):
                href = f"https://www.linkedin.com{href}"
            # Strip query params beyond the job ID
            if "?" in href:
                href = href.split("?")[0]
            await page.close()
            return href

    await page.close()
    return ""


async def _find_first_indeed_job(context) -> str:
    """Search Indeed for a remote Product Manager job and return its viewjob URL."""
    page = await context.new_page()
    search_url = "https://www.indeed.com/jobs?q=product+manager&l=remote&sc=0kf%3Aattr%28DSQF7%29%3B"
    await page.goto(search_url, timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        await asyncio.sleep(3)

    # Indeed job cards link to /pagead/clk or /rc/clk or direct viewjob URLs
    job_links = await page.evaluate("""
        () => {
            const links = Array.from(document.querySelectorAll('a[href*="viewjob"], a[href*="/rc/clk"], a[href*="/pagead/clk"]'));
            return links.map(a => a.href).filter(h => h && h.includes('jk='));
        }
    """)

    import re as _re
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

    def _looks_real(jk: str) -> bool:
        """Reject obviously fake jk values (sequential, permuted, or patterned)."""
        if not _re.match(r'^[0-9a-f]{16}$', jk):
            return False
        # Reject perfect hex permutations: every digit 0-f appears exactly once
        # e.g. fedcba9876543210, 123456789abcdef0 — both are test/placeholder values
        if sorted(jk) == list("0123456789abcdef"):
            return False
        # Reject uniform single-step sequences (all diffs identical)
        chars = list(jk)
        diffs = [ord(chars[i+1]) - ord(chars[i]) for i in range(len(chars)-1)]
        if len(set(diffs)) == 1:
            return False
        return True

    for href in job_links:
        parsed = _urlparse(href)
        qs = _parse_qs(parsed.query)
        if "jk" not in qs:
            continue
        jk = qs["jk"][0]
        if _looks_real(jk):
            await page.close()
            return f"https://www.indeed.com/viewjob?jk={jk}"

    await page.close()
    return ""


async def run_test(url: str):
    from playwright.async_api import async_playwright

    cfg = load_config()
    ats = detect_ats(url)
    # Display label: LinkedIn/Indeed are handled by dedicated handlers, not ATS detection
    if "linkedin.com" in url:
        display_platform = "LINKEDIN"
    elif "indeed.com" in url:
        display_platform = "INDEED"
    else:
        display_platform = ats.upper()
    print(f"\n{'='*60}")
    print(f"  ATS TEST — {display_platform}")
    print(f"  URL: {url}")
    print(f"{'='*60}\n")

    # Setup
    screenshots_dir = Path("./output/test_screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    semantics_db = FieldSemanticsDB(cfg.output.db_path)
    smart_filler = SmartFormFiller(cfg.ai, semantics_db)

    # Override: headless=False so we can see what's happening, auto_submit=False for safety
    cfg.automation.headless = False
    cfg.automation.auto_submit = False
    cfg.automation.slow_mo_ms = 300  # slightly slower so we can see the fills

    agent = ApplicationAgent(
        cfg.automation,
        str(screenshots_dir),
        smart_filler=smart_filler,
    )

    profile = make_test_profile()

    # Build a minimal TailoredResume
    resume_path = cfg.profile.resume_path or ""

    # Pick platform enum so we hit the right handler:
    #   LINKEDIN → _apply_linkedin, INDEED → _apply_indeed, anything else → _apply_generic
    if "linkedin.com" in url:
        platform = JobPlatform.LINKEDIN
    elif "indeed.com" in url:
        platform = JobPlatform.INDEED
    else:
        platform = JobPlatform.ZIPRECRUITER  # triggers _apply_generic → ATS detection

    tailored = TailoredResume(
        job=JobPosting(
            id="test-001",
            title="Product Manager",
            company="Test Company",
            location="Remote",
            description="Test application — auto-filled by Job Agent.",
            url=url,
            platform=platform,
        ),
        profile=profile,
        tailored_summary=profile.summary,
        highlighted_skills=profile.skills,
        docx_path=resume_path if resume_path and Path(resume_path).exists() else None,
    )

    app = Application(
        id=str(uuid.uuid4()),
        job=tailored.job,
        resume=tailored,
        status=ApplicationStatus.QUEUED,
    )

    # Short absolute path avoids Windows MAX_PATH issues
    BROWSER_PROFILE_DIR = Path.home() / ".job_agent" / "browser_profile"
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=cfg.automation.headless,
            slow_mo=cfg.automation.slow_mo_ms,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )

        # If URL is a listing page or a known-bad job ID, auto-navigate to a fresh job
        resolved_url = url
        if ats == "greenhouse" and "/jobs/" not in url:
            resolved_url = await _find_first_greenhouse_job(context, url)
            print(f"[test] Auto-selected job: {resolved_url}")
        elif ats == "lever" and url.count("/") <= 4:
            resolved_url = await _find_first_lever_job(context, url)
            print(f"[test] Auto-selected job: {resolved_url}")
        elif ats == "ashby" and "/jobs/" not in url:
            resolved_url = await _find_first_ashby_job(context, url)
            print(f"[test] Auto-selected job: {resolved_url}")
        elif "linkedin.com" in url and "/jobs/view/" in url:
            # Always auto-search for a fresh Easy Apply job — IDs expire quickly
            found = await _find_linkedin_easy_apply_job(context)
            if found:
                resolved_url = found
                print(f"[test] Auto-selected LinkedIn Easy Apply job: {resolved_url}")
        elif "indeed.com" in url and "jk=" not in url:
            # No valid job key — auto-find one
            found = await _find_first_indeed_job(context)
            if found:
                resolved_url = found
                print(f"[test] Auto-selected Indeed job: {resolved_url}")

        app.job.url = resolved_url
        app.resume.job.url = resolved_url

        result = await agent._apply_one_with_context(app, context)
        await context.close()

    # Report
    print(f"\n{'='*60}")
    print(f"  RESULT: {result.status.value.upper()}")
    if result.error:
        print(f"  Error:  {result.error}")
    if result.notes:
        print(f"  Notes:  {result.notes}")
    if result.form_data:
        print(f"  Fields filled: {len(result.form_data)}")
        for label, val in list(result.form_data.items())[:5]:
            print(f"    {label}: {val[:60]}")
    print(f"\n  Screenshots saved to: {screenshots_dir}")
    print(f"{'='*60}\n")

    # Write to run log (shared markdown file for reviewing failures)
    try:
        run_log = RunLog("./output/run_log.md")
        run_log.log_result(
            title="Product Manager",
            company=ats.upper() + " test",
            url=resolved_url,
            status=result.status.value,
            ats=ats,
            error=result.error,
            notes=result.notes,
            fields_filled=len(result.form_data) if result.form_data else 0,
        )
    except Exception as e:
        print(f"[run_log] Could not write: {e}")

    # Write to improvement tracker
    try:
        from job_agent.db.improvement_tracker import ImprovementTracker
        itracker = ImprovementTracker(cfg.output.db_path)
        if result.status == ApplicationStatus.APPLIED:
            itracker.log_success(ats, url, "Test Company", "Product Manager",
                                 len(result.form_data), False)
            print("✓ Logged success to improvement tracker")
        elif result.error:
            itracker.log_failure(ats, url, "Test Company", "Product Manager",
                                 result.error)
            print("⚠ Logged failure to improvement tracker")

        note = itracker.write_vault_note(cfg.profile.obsidian_vault_path)
        if note:
            print(f"✓ Vault note updated: {note}")
    except Exception as e:
        print(f"[tracker] Could not log: {e}")

    return result


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else TEST_URL
    asyncio.run(run_test(url))
