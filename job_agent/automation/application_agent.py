"""
Application Agent
Playwright-based browser automation for submitting job applications.

Handles:
  - Indeed Easy Apply (multi-step modal)
  - LinkedIn Easy Apply (modal) + external redirect to company ATS
  - ZipRecruiter 1-Click Apply + external redirect to company ATS
  - Glassdoor Easy Apply (modal) + external redirect to company ATS
  - Company ATS pages: Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
    iCIMS, Taleo, BambooHR, Workable, and generic career pages

Uses SmartFormFiller for intelligent field classification and AI-generated
answers — learning across every form filled so costs drop over time.

SAFETY: auto_submit is False by default. The agent fills forms and takes
screenshots but does NOT click submit unless you explicitly enable it.
"""
import asyncio
import time
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional
from datetime import datetime

from job_agent.models import (
    Application, JobPosting, ApplicationStatus, JobPlatform, TailoredResume
)
from job_agent.config import AutomationConfig
from job_agent.automation.ats_handlers import (
    detect_ats,
    APPLY_BTN_SELECTORS,
    ZIPRECRUITER_APPLY_SELECTORS,
    GLASSDOOR_APPLY_SELECTORS,
    NEXT_BTN_SELECTORS,
    SUBMIT_BTN_SELECTORS,
    SUCCESS_KEYWORDS,
    EEO_SELECTORS,
    EEO_DECLINE_PHRASES,
)


# ── JavaScript for extracting all visible form fields ─────────────────────────

_EXTRACT_FIELDS_JS = """() => {
    const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' &&
               parseFloat(s.opacity) > 0.05 && r.width > 0 && r.height > 0;
    };

    const SKIP_TYPES = new Set(['submit','button','hidden','image','reset']);
    const inputs = Array.from(document.querySelectorAll(
        'input, textarea, select'
    )).filter(el => {
        const t = (el.getAttribute('type') || '').toLowerCase();
        if (SKIP_TYPES.has(t)) return false;
        if (t === 'checkbox' || t === 'radio') return false;
        return isVisible(el);
    });

    const seen = new Set();
    const fields = [];

    inputs.forEach((inp, i) => {
        const raw_id = inp.id || '';
        const raw_name = inp.name || '';
        const field_id = raw_id || raw_name || ('__pos_' + i);
        if (seen.has(field_id)) return;
        seen.add(field_id);

        // --- Label resolution ---
        let label = '';

        // 1. aria-label
        label = inp.getAttribute('aria-label') || '';

        // 2. <label for="id">
        if (!label && raw_id) {
            const lbl = document.querySelector('label[for="' + CSS.escape(raw_id) + '"]');
            if (lbl) label = lbl.textContent.trim();
        }

        // 3. aria-labelledby
        if (!label) {
            const lby = inp.getAttribute('aria-labelledby');
            if (lby) {
                const el2 = document.getElementById(lby);
                if (el2) label = el2.textContent.trim();
            }
        }

        // 4. Nearest ancestor <label>
        if (!label) {
            const wrap = inp.closest('label');
            if (wrap) {
                const c = wrap.cloneNode(true);
                c.querySelectorAll('input,textarea,select').forEach(e => e.remove());
                label = c.textContent.trim();
            }
        }

        // 5. Nearest fieldset/section label
        if (!label) {
            const container = inp.closest(
                '.field, .form-group, .form-field, .input-wrapper, .question, ' +
                '[class*="field"], [class*="input-group"], [class*="form-row"]'
            );
            if (container) {
                const lbl = container.querySelector(
                    'label, .label, [class*="label"]:not(input), ' +
                    'legend, h3, h4, p.title, span.title'
                );
                if (lbl && !lbl.contains(inp)) label = lbl.textContent.trim();
            }
        }

        // 6. Placeholder / name fallback
        if (!label) label = inp.getAttribute('placeholder') || raw_name || '';

        // Strip trailing asterisks (required markers)
        label = label.replace(/[\\s*✱✦●]+$/, '').trim();

        // Group label from fieldset legend
        let groupLabel = '';
        const fs = inp.closest('fieldset');
        if (fs) {
            const leg = fs.querySelector('legend');
            if (leg) groupLabel = leg.textContent.trim();
        }

        // Options for <select>
        const options = [];
        if (inp.tagName === 'SELECT') {
            Array.from(inp.options).forEach(o => {
                const t = o.text.trim();
                if (o.value && t && t !== '--' && !t.startsWith('Select') &&
                    t !== 'Please choose' && t !== 'Choose...') {
                    options.push({ value: o.value, text: t });
                }
            });
        }

        fields.push({
            id:            field_id,
            label:         label || groupLabel,
            group_label:   groupLabel,
            name:          raw_name,
            type:          (inp.getAttribute('type') || inp.tagName.toLowerCase()).toLowerCase(),
            placeholder:   inp.getAttribute('placeholder') || '',
            options:       options,
            required:      inp.required || inp.getAttribute('aria-required') === 'true',
            current_value: inp.value || '',
            tag:           inp.tagName.toLowerCase(),
            pos_index:     i,
        });
    });

    return fields;
}"""


class ApplicationAgent:
    def __init__(
        self,
        config: AutomationConfig,
        screenshots_dir: str = "./output/screenshots",
        captcha_event: Optional[threading.Event] = None,
        captcha_notify_fn: Optional[Callable] = None,
        smart_filler=None,          # SmartFormFiller instance (optional)
        improvement_tracker=None,   # ImprovementTracker instance (optional)
        memory_lookup_fn: Optional[Callable] = None,  # (label, context) -> str|None
    ):
        self.config = config
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.captcha_event = captcha_event
        self.captcha_notify_fn = captcha_notify_fn
        self._smart_filler = smart_filler
        self._tracker = improvement_tracker
        self.memory_lookup_fn = memory_lookup_fn
        # Separate flag from the threading.Event gate: event.set() always unblocks
        # (even on timeout), but this flag only becomes True when the CAPTCHA was
        # actually solved — prevents a timed-out event from being misread as solved.
        self._captcha_solved: bool = False

    def apply_batch(self, applications: list) -> list:
        return asyncio.run(self._apply_batch_async(applications))

    def apply_one(self, application: Application) -> Application:
        import time as _time
        for attempt in range(3):
            try:
                return asyncio.run(self._apply_one_async(application))
            except Exception as e:
                msg = str(e)
                if "existing browser session" in msg or "already in use" in msg:
                    # Profile dir lock not yet released — wait and retry
                    print(f"[agent] Profile locked, retrying in 5s (attempt {attempt+1}/3)…")
                    _time.sleep(5)
                    continue
                raise
        # All retries exhausted
        application.status = ApplicationStatus.FAILED
        application.error = "Profile dir locked after 3 retries — close other browser windows"
        return application

    # ── Persistent browser profile ────────────────────────────────────────────
    # Use a short absolute path so Windows MAX_PATH (260 chars) is never exceeded,
    # regardless of where the project is cloned.
    BROWSER_PROFILE_DIR = Path.home() / ".job_agent" / "browser_profile"

    # Args that make Playwright-controlled Chromium look like a real browser.
    # Without these LinkedIn/Indeed detect automation and kill the session.
    _STEALTH_ARGS = [
        "--start-maximized",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-extensions-except=",
        "--disable-plugins-discovery",
    ]
    _STEALTH_IGNORE = ["--enable-automation"]
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    # Injected before every page load — hides all Playwright/webdriver fingerprints
    # that Cloudflare and LinkedIn bot-detection check.
    _STEALTH_SCRIPT = """
        (() => {
            // Hide webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Spoof plugins (empty array = headless giveaway)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const p = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ];
                    p.__proto__ = PluginArray.prototype;
                    return p;
                }
            });

            // Spoof languages
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

            // Add chrome runtime object real Chrome has
            if (!window.chrome) {
                window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
            }

            // Remove automation-related properties from window
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

            // Spoof permissions
            const origQuery = window.navigator.permissions ? window.navigator.permissions.query.bind(window.navigator.permissions) : null;
            if (origQuery) {
                window.navigator.permissions.query = (params) =>
                    params.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : origQuery(params);
            }
        })();
    """

    # ── Async orchestration ───────────────────────────────────────────────────

    async def _make_context(self, pw):
        """Create a stealth persistent context and inject fingerprint-hiding script."""
        self.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            str(self.BROWSER_PROFILE_DIR),
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
            args=self._STEALTH_ARGS,
            ignore_default_args=self._STEALTH_IGNORE,
            viewport={"width": 1280, "height": 900},
            user_agent=self._USER_AGENT,
        )
        await context.add_init_script(self._STEALTH_SCRIPT)
        return context

    async def _warm_linkedin_session(self, context):
        """
        Navigate to LinkedIn feed once at batch start so the li_at cookie is
        validated and Cloudflare issues its clearance cookie before we hit job URLs.
        Without this, the first job URL cold-starts and gets a bot challenge.
        """
        has_linkedin = any(
            app.job.platform.value == "linkedin"
            for app in []  # checked in caller
        )
        page = await context.new_page()
        try:
            print("[agent] Warming LinkedIn session...")
            await page.goto("https://www.linkedin.com/feed/", timeout=20000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            if "feed" in page.url:
                print("[agent] LinkedIn session confirmed ✓")
            else:
                print(f"[agent] LinkedIn warm-up landed on: {page.url}")
        except Exception as e:
            print(f"[agent] LinkedIn warm-up skipped: {e}")
        finally:
            await page.close()

    async def _warm_platform_session(self, context, url: str, name: str):
        """Navigate to a platform homepage so existing cookies are validated
        before we hit job-detail pages. Keeps bot challenges at bay."""
        page = await context.new_page()
        try:
            print(f"[agent] Warming {name} session...")
            await page.goto(url, timeout=20000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            print(f"[agent] {name} session warmed ✓")
        except Exception as e:
            print(f"[agent] {name} warm-up skipped: {e}")
        finally:
            await page.close()

    async def _apply_batch_async(self, applications: list) -> list:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            context = await self._make_context(pw)
            results = []
            try:
                from job_agent.models import JobPlatform
                # Warm platform sessions to avoid cold-start bot challenges
                if any(a.job.platform == JobPlatform.LINKEDIN for a in applications):
                    await self._warm_linkedin_session(context)
                if any(a.job.platform == JobPlatform.ZIPRECRUITER for a in applications):
                    await self._warm_platform_session(context, "https://www.ziprecruiter.com", "ziprecruiter")
                if any(a.job.platform == JobPlatform.GLASSDOOR for a in applications):
                    await self._warm_platform_session(context, "https://www.glassdoor.com", "glassdoor")

                for i, app in enumerate(applications):
                    if i >= self.config.max_applications_per_run:
                        print(f"[agent] Reached limit ({self.config.max_applications_per_run})")
                        break
                    print(f"\n[agent] {i+1}/{len(applications)}: {app.job.title} @ {app.job.company}")
                    result = await self._apply_one_with_context(app, context)
                    results.append(result)
                    await asyncio.sleep(1)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
        return results

    async def _apply_one_async(self, application: Application) -> Application:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            context = await self._make_context(pw)
            try:
                result = await self._apply_one_with_context(application, context)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
        return result

    async def _apply_one_with_context(self, app: Application, context) -> Application:
        app.status = ApplicationStatus.APPLYING
        page = await context.new_page()
        ats = detect_ats(app.job.url)
        _page_crashed = False

        try:
            platform = app.job.platform
            if platform == JobPlatform.INDEED:
                await self._apply_indeed(page, app)
            elif platform == JobPlatform.LINKEDIN:
                await self._apply_linkedin(page, app)
            elif platform == JobPlatform.ZIPRECRUITER:
                await self._apply_ziprecruiter(page, app)
            elif platform == JobPlatform.GLASSDOOR:
                await self._apply_glassdoor(page, app)
            else:
                await self._apply_generic(page, app)

            # Resolve any lingering APPLYING status
            if app.status == ApplicationStatus.APPLYING:
                app.status = ApplicationStatus.NEEDS_MANUAL
                app.error = app.error or "Apply: finished without setting a terminal status"

            # Log success to improvement tracker
            if self._tracker and app.status == ApplicationStatus.APPLIED:
                try:
                    self._tracker.log_success(
                        ats=detect_ats(page.url),
                        url=page.url,
                        company=app.job.company,
                        job_title=app.job.title,
                        fields_filled=len(app.form_data),
                        auto_submitted=self.config.auto_submit,
                    )
                except Exception:
                    pass

        except Exception as e:
            err = str(e)
            app.error = err
            is_page_crash = (
                "target page" in err.lower()
                or "browser has been closed" in err.lower()
                or "context or browser" in err.lower()
                or "targetclosed" in err.lower()
            )
            if is_page_crash:
                _page_crashed = True
                print(f"[agent] PAGE CRASH ({app.job.company}) — retrying with fresh page")
            elif self._needs_human(err):
                app.status = ApplicationStatus.NEEDS_MANUAL
                print(f"[agent] NEEDS MANUAL ({app.job.company}): {err}")
            else:
                app.status = ApplicationStatus.FAILED
                app.error = err or f"Unknown failure — check screenshots (platform: {platform})"
                print(f"[agent] FAILED ({app.job.company}): {app.error}")
            try:
                await self._screenshot(page, app, "error")
            except Exception:
                pass

            if not is_page_crash and self._tracker:
                try:
                    self._tracker.log_failure(
                        ats=ats,
                        url=app.job.url,
                        company=app.job.company,
                        job_title=app.job.title,
                        error=err,
                        context={"platform": str(platform), "status": str(app.status)},
                    )
                except Exception:
                    pass
        finally:
            try:
                await page.close()
            except Exception:
                pass

        # Retry once with a fresh page if the page crashed (Instead bot detection kills tabs)
        if _page_crashed:
            print(f"[agent] Retrying {app.job.company} on fresh page…")
            app.status = ApplicationStatus.APPLYING
            app.error = None
            retry_page = None
            try:
                retry_page = await context.new_page()
                platform = app.job.platform
                if platform == JobPlatform.INDEED:
                    await self._apply_indeed(retry_page, app)
                elif platform == JobPlatform.LINKEDIN:
                    await self._apply_linkedin(retry_page, app)
                elif platform == JobPlatform.ZIPRECRUITER:
                    await self._apply_ziprecruiter(retry_page, app)
                elif platform == JobPlatform.GLASSDOOR:
                    await self._apply_glassdoor(retry_page, app)
                else:
                    await self._apply_generic(retry_page, app)
                if app.status == ApplicationStatus.APPLYING:
                    app.status = ApplicationStatus.NEEDS_MANUAL
                    app.error = "Apply: finished without terminal status on retry"
            except Exception as e2:
                app.status = ApplicationStatus.NEEDS_MANUAL
                app.error = f"Browser crash on both attempts — apply manually ({e2})"
                print(f"[agent] RETRY FAILED ({app.job.company}): {e2}")
            finally:
                if retry_page:
                    try:
                        await retry_page.close()
                    except Exception:
                        pass

        return app

    def _needs_human(self, error: str) -> bool:
        """True only when a human must intervene — not just a hard ATS."""
        human_signals = [
            "captcha", "login", "authwall", "sign in", "authentication",
            "apply button not found", "could not find apply",
            "indeed apply not available",
            "manual", "sponsorship required", "visa required",
            # Click intercepted by overlay (lazy-column, modal, etc.) — human can retry
            "elementhandle.click: timeout",
            "locator.click: timeout",
            # Transient network errors — not agent failures, just retry when connection is back
            "net::err_internet_disconnected",
            "net::err_network_changed",
            "net::err_internet_changed",
            "net::err_connection_reset",
            "net::err_connection_refused",
            "net::err_",
        ]
        err_lower = error.lower()
        return any(s in err_lower for s in human_signals)

    # ── Platform handlers ─────────────────────────────────────────────────────

    async def _apply_indeed(self, page, app: Application):
        print(f"[agent] Indeed: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        # Fast-fail on 404 / "page not found" — bad jk value
        try:
            body_text = (await page.inner_text("body")).lower()
            if any(p in body_text for p in [
                "we can't find this page", "page doesn't exist",
                "page not found", "job is no longer available",
                "this job has expired", "job listing is no longer active",
            ]):
                app.status = ApplicationStatus.FAILED
                app.error = "Indeed: job page returned 404 or expired listing"
                return
        except Exception:
            pass

        if not await self._ensure_indeed_logged_in(page, app):
            return

        # Check if page is telling us to sign in before applying
        try:
            pg_text = (await page.inner_text("body")).lower()
            if "sign in to apply" in pg_text or "log in to apply" in pg_text or "create an account to apply" in pg_text:
                print(f"[agent] Indeed requires login to apply — opening login page")
                await page.goto("https://secure.indeed.com/account/login", timeout=30000)
                await asyncio.sleep(3)
                for _ in range(90):
                    await asyncio.sleep(2)
                    try:
                        if "account/login" not in page.url and "indeed.com" in page.url:
                            print(f"[agent] Indeed login detected — retrying job page")
                            await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                await asyncio.sleep(2)
                            break
                    except Exception:
                        pass
                else:
                    app.status = ApplicationStatus.NEEDS_MANUAL
                    app.error = "Indeed: login required but not completed"
                    return
        except Exception:
            pass

        # Indeed Easy Apply button (native modal)
        indeed_apply = await page.query_selector(
            "button[data-testid='IndeedApplyButton'], "
            "button[id*='indeedApplyButton'], "
            "span[data-testid='IndeedApplyButton'], "
            "button[class*='IndeedApply'], "
            "#indeedApplyButton, "
            "button[data-indeed-apply-jobid]"
        )

        if indeed_apply and await indeed_apply.is_visible():
            try:
                await indeed_apply.click(timeout=10000)
            except Exception:
                await page.evaluate("el => el.click()", indeed_apply)
            await asyncio.sleep(2)
            await self._screenshot(page, app, "apply_started")
            await self._fill_indeed_form(page, app)
            return

        # External apply link (company ATS)
        ext_btn = await page.query_selector(
            "a[href*='apply'][target='_blank'], "
            "button:has-text('Apply now'), a:has-text('Apply now'), "
            "button:has-text('Apply on company site'), a:has-text('Apply on company site'), "
            "a[data-jk][href*='apply'], "
            ".jobsearch-IndeedApplyButton, "
            "[data-testid='applyButton']"
        )
        if ext_btn and await ext_btn.is_visible():
            pages_before = len(page.context.pages)
            try:
                await ext_btn.click(timeout=10000)
            except Exception:
                await page.evaluate("el => el.click()", ext_btn)
            # Wait for a new tab to appear (poll up to 3s)
            for _ in range(15):
                await asyncio.sleep(0.2)
                if len(page.context.pages) > pages_before:
                    break

            pages_after = page.context.pages
            if len(pages_after) > pages_before:
                ext_page = pages_after[-1]
                try:
                    await ext_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(1)
                print(f"[agent] Indeed → external ATS: {ext_page.url}")
                await self._apply_ats_page(ext_page, app)
            else:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(2)
                await self._apply_ats_page(page, app)
            return

        # JS fallback: scan all clickable elements for "apply" text
        try:
            apply_el = await page.evaluate_handle("""
                () => {
                    const els = [...document.querySelectorAll('button, a')];
                    return els.find(el => {
                        const t = (el.textContent || el.getAttribute('aria-label') || '').toLowerCase().trim();
                        return t.includes('apply') && !t.includes('applied');
                    }) || null;
                }
            """)
            if apply_el:
                el = apply_el.as_element()
                if el and await el.is_visible():
                    pages_before = len(page.context.pages)
                    try:
                        await el.click(timeout=10000)
                    except Exception:
                        await page.evaluate("el => el.click()", el)
                    await asyncio.sleep(2.5)
                    pages_after = page.context.pages
                    if len(pages_after) > pages_before:
                        ext_page = pages_after[-1]
                        try:
                            await ext_page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            await asyncio.sleep(2)
                        await self._apply_ats_page(ext_page, app)
                    else:
                        await self._apply_ats_page(page, app)
                    return
        except Exception:
            pass

        # No apply button found — job may be expired or require direct navigation
        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = "Could not find Indeed Apply button — job may be expired or require direct site apply"

    async def _ensure_indeed_logged_in(self, page, app: Application) -> bool:
        """Return True if logged in to Indeed. If not, open login and wait up to 3 min."""
        logged_in = await page.query_selector(
            "[data-testid='UserAccountIcon'], .gnav-LoggedInUser, "
            "a[href*='/myresume'], a[href*='/account']"
        )
        if logged_in:
            return True

        sign_in = await page.query_selector(
            "a[href*='/account/login'], a:has-text('Sign in'), "
            "button:has-text('Sign in')"
        )
        if not sign_in:
            return True  # Ambiguous — continue and see

        print(f"\n{'='*60}")
        print(f"[agent] Indeed login required.")
        print(f"[agent]   → Log in to Indeed in the browser window,")
        print(f"[agent]     then the agent will continue automatically.")
        print(f"{'='*60}\n")

        await page.goto("https://secure.indeed.com/account/login", timeout=30000)
        await asyncio.sleep(3)  # Let login page settle

        for _ in range(90):
            await asyncio.sleep(2)
            try:
                current_url = page.url
                if "secure.indeed.com/account/login" not in current_url and "indeed.com" in current_url:
                    print(f"[agent] Indeed login detected — continuing.")
                    await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)
                    return True
            except Exception:
                pass  # Page still navigating

        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = "Indeed login not completed within 3 minutes"
        return False

    async def _fill_indeed_form(self, page, app: Application):
        """Navigate and fill Indeed's multi-step application modal."""
        _INDEED_NEXT = (
            "button:has-text('Continue'), "
            "button[aria-label='Continue to next step'], "
            "button:has-text('Next'), "
            "button:has-text('Next Step'), "
            "button:has-text('Save and continue'), "
            "button:has-text('Save & Continue'), "
            "button[data-testid='continue-button'], "
            "button[data-testid='next-button']"
        )
        _INDEED_SUBMIT = (
            "button[aria-label='Submit your application'], "
            "button:has-text('Submit your application'), "
            "button:has-text('Submit Application'), "
            "button:has-text('Submit'), "
            "button[data-testid='submit-button']"
        )

        max_steps = 30
        for step in range(max_steps):
            await asyncio.sleep(0.3)

            if await self._is_captcha_page(page):
                if not await self._handle_captcha(page, app):
                    return

            # Check for success page
            if await self._is_success_page(page):
                app.status = ApplicationStatus.APPLIED
                app.applied_at = datetime.now()
                await self._screenshot(page, app, "confirmed")
                print(f"[agent] ✓ Indeed application confirmed: {app.job.company}")
                return

            # Submit page check
            submit = await page.query_selector(_INDEED_SUBMIT)
            if submit and await submit.is_visible():
                await self._screenshot(page, app, f"step_{step}_review")
                if self.config.auto_submit:
                    try:
                        await submit.click(timeout=10000)
                    except Exception:
                        await page.evaluate("el => el.click()", submit)
                    await asyncio.sleep(2)
                    app.status = ApplicationStatus.APPLIED
                    app.applied_at = datetime.now()
                    await self._screenshot(page, app, "submitted")
                    print(f"[agent] ✓ SUBMITTED: {app.job.title} @ {app.job.company}")
                else:
                    app.status = ApplicationStatus.APPLIED
                    app.notes = "Indeed form filled. auto_submit=False — review and submit manually."
                return

            await self._smart_fill_page(page, app)
            await self._maybe_upload_resume(page, app)
            await self._handle_radio_groups(page, app)
            await self._handle_eeo_fields(page)

            next_btn = await page.query_selector(_INDEED_NEXT)
            if next_btn and await next_btn.is_visible():
                try:
                    await next_btn.click(timeout=10000)
                except Exception:
                    await page.evaluate("el => el.click()", next_btn)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(2)
            else:
                # No next and no submit — form stalled
                print(f"[agent] Indeed: no nav button on step {step} at {page.url}")
                app.status = ApplicationStatus.NEEDS_MANUAL
                app.error = f"Indeed form stalled at step {step} — no next/submit button found"
                await self._screenshot(page, app, f"stalled_step_{step}")
                return

        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = f"Indeed: exceeded {max_steps} steps without submitting"

    async def _apply_linkedin(self, page, app: Application):
        print(f"[agent] LinkedIn: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        # Dismiss LinkedIn popups (Premium upsell, "open in app", etc.)
        for dismiss_sel in [
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            ".msg-overlay-bubble-header__controls button",
            "[data-test-modal-close-btn]",
        ]:
            try:
                btn = await page.query_selector(dismiss_sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=5000)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        if not await self._ensure_linkedin_logged_in(page, app):
            return

        # Detect closed/expired jobs early
        try:
            content = await page.content()
            if "no longer accepting applications" in content.lower():
                app.status = ApplicationStatus.FAILED
                app.error = "LinkedIn job is closed — no longer accepting applications"
                return
        except Exception:
            pass

        # Scroll to trigger SPA lazy-render before waiting for the apply button
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(400)

        APPLY_BTN_SEL = (
            "button.jobs-apply-button, "
            "a.jobs-apply-button, "
            "button[aria-label*='Easy Apply'], "
            "a[aria-label*='Easy Apply'], "
            "button[aria-label*='Apply'], "
            "a[aria-label*='Apply'], "
            ".jobs-apply-button, "
            "button:has-text('Easy Apply'), "
            "a:has-text('Easy Apply'), "
            "button:has-text('Apply now'), "
            "a:has-text('Apply now'), "
            "button:has-text('Apply on'), "
            "a:has-text('Apply on')"
        )
        try:
            await page.wait_for_selector(APPLY_BTN_SEL, timeout=8000)
        except Exception:
            pass

        apply_btn = await page.query_selector(APPLY_BTN_SEL)

        if apply_btn and await apply_btn.is_visible():
            label = (await apply_btn.text_content() or "").strip().lower()
            if "easy apply" in label:
                try:
                    await apply_btn.click(timeout=8000)
                except Exception:
                    # lazy-column or overlay intercepting — JS click bypasses all blocking
                    try:
                        await page.evaluate("el => el.click()", apply_btn)
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
                await self._fill_linkedin_modal(page, app)
                return
            else:
                # Regular Apply — opens company careers site in new tab or same tab
                pages_before = len(page.context.pages)
                try:
                    await apply_btn.click(timeout=8000)
                except Exception:
                    try:
                        await page.evaluate("el => el.click()", apply_btn)
                    except Exception:
                        pass
                # Poll for new tab (up to 3s)
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    if len(page.context.pages) > pages_before:
                        break
                pages_after = page.context.pages
                if len(pages_after) > pages_before:
                    ext_page = pages_after[-1]
                    try:
                        await ext_page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(1)
                    print(f"[agent] LinkedIn → external ATS: {ext_page.url}")
                    await self._apply_ats_page(ext_page, app)
                else:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(1)
                    print(f"[agent] LinkedIn → same-tab redirect: {page.url}")
                    await self._apply_ats_page(page, app)
                return

        # No button found — job may be closed or company-site-only
        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = "LinkedIn: no Apply button found — job may be closed or apply on company site"

    async def _ensure_linkedin_logged_in(self, page, app: Application) -> bool:
        """Return True if logged in. If not, open the login page and wait up to 3 min."""
        # Check for logged-in indicator (nav menu or profile icon)
        logged_in = await page.query_selector(
            ".global-nav__me-photo, [data-control-name='nav.settings'], "
            "img.global-nav__me-photo, .feed-identity-module"
        )
        if logged_in:
            return True

        # Check for sign-in buttons as a reliable not-logged-in signal
        sign_in = await page.query_selector(
            "a[href*='/login'], a[href*='/signup'], "
            "a:has-text('Sign in'), button:has-text('Sign in')"
        )
        if not sign_in:
            # Ambiguous — assume logged in and continue
            return True

        print(f"\n{'='*60}")
        print(f"[agent] LinkedIn login required.")
        print(f"[agent]   → The browser should be open. Log in to LinkedIn,")
        print(f"[agent]     then press Enter here to continue.")
        print(f"{'='*60}\n")

        # Navigate to LinkedIn login page
        await page.goto("https://www.linkedin.com/login", timeout=30000)
        await asyncio.sleep(3)  # Let login page settle before polling

        for _ in range(90):
            await asyncio.sleep(2)
            try:
                current_url = page.url
                if any(k in current_url for k in ("/feed", "/jobs/", "/mynetwork/", "/in/", "/messaging/")):
                    print(f"[agent] LinkedIn login detected — continuing.")
                    await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)
                    return True
                logged_in = await page.query_selector(".global-nav__me-photo, .feed-identity-module")
                if logged_in:
                    await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)
                    return True
            except Exception:
                pass  # Page still navigating — wait for next tick

        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = "LinkedIn login not completed within 3 minutes"
        return False

    async def _fill_linkedin_modal(self, page, app: Application):
        max_steps = 60
        last_content_hash = None
        stuck_count = 0

        for step in range(max_steps):
            await asyncio.sleep(0.4)
            await self._screenshot(page, app, f"li_step_{step}")

            if await self._is_captcha_page(page):
                if not await self._handle_captcha(page, app):
                    return

            # Scroll to bottom of modal to ensure submit button is in DOM
            try:
                modal = await page.query_selector(".jobs-easy-apply-modal, [data-test-modal], .artdeco-modal__content")
                if modal:
                    await modal.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            submit = await page.query_selector(
                "button[aria-label='Submit application'], "
                "button:has-text('Submit application'), "
                "button[data-control-name='continue_unify']"
            )
            if submit and await submit.is_visible():
                await self._screenshot(page, app, "li_review")
                if self.config.auto_submit:
                    try:
                        # force=True bypasses lazy-column / overlay interceptors
                        await submit.click(timeout=10000, force=True)
                    except Exception:
                        await page.evaluate("el => el.click()", submit)
                    await asyncio.sleep(2)
                    app.status = ApplicationStatus.APPLIED
                    app.applied_at = datetime.now()
                    await self._screenshot(page, app, "submitted")
                    print(f"[agent] LinkedIn SUBMITTED: {app.job.title} @ {app.job.company}")
                else:
                    app.status = ApplicationStatus.APPLIED
                    app.notes = "LinkedIn modal filled (4/4 pages). auto_submit=False — click Submit manually."
                return

            await self._smart_fill_page(page, app)
            await self._handle_radio_groups(page, app)  # work auth, relocation, etc.
            await self._maybe_upload_resume(page, app)

            # Stale-page detection: if content hasn't changed after 3 clicks, we're stuck
            try:
                cur_hash = hash((await page.inner_text(".jobs-easy-apply-content, .jobs-easy-apply-modal, body"))[:500])
                if cur_hash == last_content_hash:
                    stuck_count += 1
                    if stuck_count >= 3:
                        app.status = ApplicationStatus.APPLIED
                        app.notes = "LinkedIn modal at review stage — page not changing. Submit manually."
                        return
                else:
                    stuck_count = 0
                last_content_hash = cur_hash
            except Exception:
                pass

            next_btn = await page.query_selector(
                "button[aria-label='Continue to next step'], "
                "button:has-text('Next'), button:has-text('Review')"
            )
            if next_btn and await next_btn.is_visible():
                try:
                    # force=True bypasses lazy-column overlay that intercepts modal clicks
                    await next_btn.click(timeout=8000, force=True)
                except Exception:
                    try:
                        await page.evaluate("el => el.click()", next_btn)
                    except Exception:
                        pass
            else:
                # On Review page with no Next/Submit found — mark pre-filled for manual submit
                app.status = ApplicationStatus.APPLIED
                app.notes = "LinkedIn modal at review stage. Submit manually in the browser."
                return

        # Loop exhausted without finding submit — still mark as filled, not stuck in APPLYING
        if app.status == ApplicationStatus.APPLYING:
            app.status = ApplicationStatus.APPLIED
            app.notes = f"LinkedIn modal: filled {max_steps} pages. Submit manually — couldn't locate final Submit button."

    async def _ensure_ziprecruiter_logged_in(self, page, app: Application) -> bool:
        """Return True if logged in to ZipRecruiter. If not, open login and wait up to 3 min."""
        logged_in = await page.query_selector(
            "[data-testid='user-menu'], .user-menu, "
            "a[href*='/dashboard'], a[href*='/account'], "
            "[aria-label*='account'], [aria-label*='profile']"
        )
        if logged_in:
            return True

        sign_in = await page.query_selector(
            "a[href*='/login'], a[href*='/signin'], "
            "a:has-text('Sign In'), button:has-text('Sign In'), "
            "a:has-text('Log In'), button:has-text('Log In')"
        )
        if not sign_in:
            return True  # Ambiguous — continue and see

        print(f"\n{'='*60}")
        print(f"[agent] ZipRecruiter login required.")
        print(f"[agent]   → Log in in the browser window, then the agent will continue.")
        print(f"{'='*60}\n")

        # Navigate to home page first — going straight to /login triggers bot detection
        try:
            await page.goto("https://www.ziprecruiter.com", timeout=20000,
                            wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(4)
        await page.goto("https://www.ziprecruiter.com/login", timeout=30000,
                        wait_until="domcontentloaded")
        await asyncio.sleep(3)

        for _ in range(90):
            await asyncio.sleep(2)
            try:
                current_url = page.url.lower()
                on_zr = "ziprecruiter.com" in current_url
                past_login = (
                    "login" not in current_url
                    and "sign-in" not in current_url
                    and "challenges.cloudflare.com" not in current_url
                )
                if on_zr and past_login:
                    print(f"[agent] ZipRecruiter login detected — continuing.")
                    await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        app.status = ApplicationStatus.NEEDS_MANUAL
        app.error = "ZipRecruiter login not completed within 3 minutes"
        return False

    async def _apply_ziprecruiter(self, page, app: Application):
        """ZipRecruiter: click 1-Click Apply / Apply Now, then handle the
        in-page overlay form or the external ATS redirect that follows."""
        print(f"[agent] ZipRecruiter: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        if not await self._ensure_ziprecruiter_logged_in(page, app):
            return

        await self._dismiss_cookie_banners(page)

        # Wait for ZipRecruiter's React-rendered apply button
        try:
            await page.wait_for_selector(ZIPRECRUITER_APPLY_SELECTORS, timeout=10000)
        except Exception:
            pass

        apply_btn = await page.query_selector(ZIPRECRUITER_APPLY_SELECTORS)
        if not apply_btn:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "ZipRecruiter: no Apply button found — job may be external-only or closed"
            return

        pages_before = len(page.context.pages)
        try:
            await apply_btn.click(timeout=8000)
        except Exception:
            try:
                await page.evaluate("el => el.click()", apply_btn)
            except Exception:
                app.status = ApplicationStatus.NEEDS_MANUAL
                app.error = "ZipRecruiter: could not click Apply button"
                return

        await asyncio.sleep(2)

        # If a new tab opened it's an external company ATS — redirect this page there
        pages_after = page.context.pages
        if len(pages_after) > pages_before:
            new_pg = pages_after[-1]
            try:
                await new_pg.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                await asyncio.sleep(2)
            ext_url = new_pg.url
            await new_pg.close()
            if ext_url and ext_url != app.job.url:
                print(f"[agent] ZipRecruiter redirect → {ext_url}")
                await page.goto(ext_url, timeout=self.config.timeout_ms)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(2)

        # Whether in-page overlay or redirected ATS, run the smart form loop
        await self._dismiss_cookie_banners(page)
        await self._multi_page_smart_apply(page, app)

    async def _apply_glassdoor(self, page, app: Application):
        """Glassdoor: click Easy Apply (in-page modal) or Apply on Company Website
        (external redirect). Both paths feed into the smart form loop."""
        print(f"[agent] Glassdoor: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        await self._dismiss_cookie_banners(page)

        # Glassdoor sometimes shows a sign-in modal over the job listing.
        # Close it if present so the apply button becomes clickable.
        for close_sel in [
            "button[aria-label='Close']",
            "button[data-test='modal-close-btn']",
            "button[data-test='modal-close-button']",
            "[data-test='closeButton']",
            "[data-testid='closeButton']",
            ".modal-header button.close",
            "button[aria-label='close']",
        ]:
            try:
                btn = await page.query_selector(close_sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=5000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

        # If Glassdoor is showing a login wall (no apply button visible), prompt login
        try:
            pg_text = (await page.inner_text("body")).lower()
            if any(p in pg_text for p in ["sign in to see jobs", "sign in to apply", "create a free account"]):
                print(f"\n{'='*60}")
                print(f"[agent] Glassdoor login required.")
                print(f"[agent]   → Log in in the browser window, then the agent will continue.")
                print(f"{'='*60}\n")
                await page.goto("https://www.glassdoor.com/profile/login_input.htm", timeout=30000)
                await asyncio.sleep(3)
                logged_in = False
                for _ in range(90):
                    await asyncio.sleep(2)
                    try:
                        if "login" not in page.url and "glassdoor.com" in page.url:
                            print(f"[agent] Glassdoor login detected — retrying job page")
                            await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                await asyncio.sleep(2)
                            logged_in = True
                            break
                    except Exception:
                        pass
                if not logged_in:
                    app.status = ApplicationStatus.NEEDS_MANUAL
                    app.error = "Glassdoor login not completed within 3 minutes"
                    return
        except Exception:
            pass

        # Wait for apply button
        try:
            await page.wait_for_selector(GLASSDOOR_APPLY_SELECTORS, timeout=10000)
        except Exception:
            pass

        apply_btn = await page.query_selector(GLASSDOOR_APPLY_SELECTORS)
        if not apply_btn:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "Glassdoor: no Apply button found — may require login or job is closed"
            return

        # Determine Easy Apply vs external redirect so we can log it
        try:
            btn_text = (await apply_btn.inner_text() or "").strip().lower()
        except Exception:
            btn_text = ""
        is_easy = "easy apply" in btn_text
        print(f"[agent] Glassdoor apply type: {'Easy Apply' if is_easy else 'external redirect'}")

        pages_before = len(page.context.pages)
        try:
            await apply_btn.click(timeout=8000)
        except Exception:
            try:
                await page.evaluate("el => el.click()", apply_btn)
            except Exception:
                app.status = ApplicationStatus.NEEDS_MANUAL
                app.error = "Glassdoor: could not click Apply button"
                return

        await asyncio.sleep(2)

        # External redirect opened in new tab — bring it to this page
        pages_after = page.context.pages
        if len(pages_after) > pages_before:
            new_pg = pages_after[-1]
            try:
                await new_pg.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                await asyncio.sleep(2)
            ext_url = new_pg.url
            await new_pg.close()
            if ext_url and "glassdoor.com" not in ext_url:
                print(f"[agent] Glassdoor redirect → {ext_url}")
                await page.goto(ext_url, timeout=self.config.timeout_ms)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(2)

        await self._dismiss_cookie_banners(page)
        await self._multi_page_smart_apply(page, app)

    async def _apply_generic(self, page, app: Application):
        """Entry point for non-Indeed, non-LinkedIn jobs (direct company ATS links)."""
        print(f"[agent] Generic: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        await self._apply_ats_page(page, app)

    async def _apply_ats_page(self, page, app: Application):
        """
        Core ATS dispatch: detect what ATS is running, navigate to the apply form,
        then run the multi-page smart fill loop.
        """
        ats = detect_ats(page.url)
        print(f"[agent] ATS detected: {ats} @ {page.url}")

        # Meta Careers requires a logged-in Meta account and uses a fully custom
        # React form — no standard inputs are exposed. Skip immediately.
        if ats == "metacareers":
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "Meta Careers requires manual application — custom form not automatable"
            return

        # Dismiss cookie consent / GDPR banners before any interaction
        await self._dismiss_cookie_banners(page)

        # Wait for the Apply button to render (React/SPA pages load it asynchronously)
        try:
            await page.wait_for_selector(APPLY_BTN_SELECTORS, timeout=5000)
        except Exception:
            pass
        await self._navigate_to_apply_form(page, ats)

        # Dismiss cookie banners again — they sometimes re-appear after navigation
        await self._dismiss_cookie_banners(page)

        await self._multi_page_smart_apply(page, app)

    async def _dismiss_cookie_banners(self, page):
        """Click Accept/Decline on common cookie consent dialogs."""
        cookie_accept_selectors = [
            "button:has-text('Accept All')",   # Playwright :has-text is case-insensitive
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept necessary')",
            "button:has-text('I Accept')",
            "button:has-text('Agree')",
            "button:has-text('Got it')",
            "button:has-text('OK')",
            "button:has-text('Allow all')",
            "button#onetrust-accept-btn-handler",
            "button.cookie-consent-accept",
            "[data-testid='cookie-accept']",
            "#accept-cookie-consent",
            "button.empyr-btn-accept",
        ]
        for sel in cookie_accept_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=5000)
                    await asyncio.sleep(0.8)
                    return
            except Exception:
                pass

    async def _navigate_to_apply_form(self, page, ats: str):
        """
        Navigate from an ATS job listing or detail page to the actual apply form.
        - If an Apply/Apply for this Job button exists, click it
        - Otherwise we're probably already on the apply form (or a detail page that IS the form)
        """
        apply_btn = await page.query_selector(APPLY_BTN_SELECTORS)
        if not apply_btn:
            return

        pages_before = len(page.context.pages)
        try:
            await apply_btn.click(timeout=10000)
        except Exception:
            try:
                await page.evaluate("el => el.click()", apply_btn)
            except Exception:
                return
        await asyncio.sleep(1.5)

        pages_after = page.context.pages
        if len(pages_after) > pages_before:
            new_pg = pages_after[-1]
            try:
                await new_pg.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                await asyncio.sleep(2)
            try:
                await page.goto(new_pg.url, timeout=15000)
            except Exception:
                pass
            await new_pg.close()
        else:
            # SPA tab switch or in-page navigation — networkidle may never fire
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                await asyncio.sleep(2)

    # ── Multi-page smart apply loop ───────────────────────────────────────────

    async def _multi_page_smart_apply(self, page, app: Application):
        """
        Fill-navigate-fill loop using SmartFormFiller for intelligent field handling.
        Works on any ATS: known platforms, generic career pages, multi-step forms.
        """
        domain = page.url.split("/")[2] if "/" in page.url else ""
        job_context = {
            "company":     app.job.company,
            "title":       app.job.title,
            "description": getattr(app.job, "description", "") or "",
        }

        # Workday forms have many navigation-only pages with no standard inputs —
        # cap lower so we don't waste 10+ minutes on an unresolvable ATS.
        ats_type = detect_ats(page.url)
        max_pages = 20 if ats_type == "workday" else 60
        empty_steps = 0  # consecutive steps with no fillable fields and no progress buttons
        no_field_steps = 0  # consecutive steps where form extracted zero fillable fields (catches nav/auth loops)
        last_url = ""
        same_url_count = 0       # consecutive steps where URL + hash are both identical
        same_url_only_count = 0  # consecutive steps on the same URL (regardless of hash — catches validation loops)
        last_form_hash = None
        for step in range(max_pages):
            await asyncio.sleep(0.4)
            await self._screenshot(page, app, f"ats_step_{step}")

            if await self._is_captcha_page(page):
                if not await self._handle_captcha(page, app):
                    return

            # Success page?
            if await self._is_success_page(page):
                app.status = ApplicationStatus.APPLIED
                app.applied_at = datetime.now()
                await self._screenshot(page, app, "confirmed")
                print(f"[agent] Application confirmed for {app.job.company}")
                return

            # Extract fields + fill (returns raw_fields for empty-step detection)
            raw_fields = await self._smart_fill_page(page, app, domain, job_context)

            # Handle file upload, radio groups, EEO
            resume_uploaded = await self._maybe_upload_resume(page, app)
            if resume_uploaded:
                empty_steps = 0  # Reset — page will change after upload

            # Track nav/auth loop: if no fields and no resume upload for N consecutive steps, bail
            if raw_fields or resume_uploaded:
                no_field_steps = 0
            else:
                no_field_steps += 1
                if no_field_steps >= 8:
                    app.status = ApplicationStatus.NEEDS_MANUAL
                    app.error = f"Form navigation loop: no fillable fields for {no_field_steps} consecutive steps at {page.url}"
                    return

            await self._handle_radio_groups(page, app)
            await self._handle_eeo_fields(page)

            # Check for submit button
            submit_btn = await page.query_selector(SUBMIT_BTN_SELECTORS)
            if submit_btn and await submit_btn.is_visible():
                await self._screenshot(page, app, f"prefilled_step_{step}")
                if self.config.auto_submit:
                    try:
                        await submit_btn.click(timeout=10000)
                    except Exception:
                        await page.evaluate("el => el.click()", submit_btn)
                    await asyncio.sleep(3)
                    app.status = ApplicationStatus.APPLIED
                    app.applied_at = datetime.now()
                    await self._screenshot(page, app, "submitted")
                    print(f"[agent] SUBMITTED: {app.job.title} @ {app.job.company}")
                else:
                    app.status = ApplicationStatus.APPLIED
                    app.notes = (
                        f"Form pre-filled ({detect_ats(page.url)}). "
                        "auto_submit=False -- review and submit manually."
                    )
                    print(f"[agent] Pre-filled (manual submit): {page.url}")
                return

            # Try Next/Continue
            next_btn = await page.query_selector(NEXT_BTN_SELECTORS)
            if next_btn and await next_btn.is_visible():
                try:
                    await next_btn.click(timeout=10000)
                except Exception:
                    try:
                        await page.evaluate("el => el.click()", next_btn)
                    except Exception:
                        pass
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    await asyncio.sleep(2)
            else:
                # Fallback: look for any visible type=submit button (e.g., Oracle Fusion ">" icon)
                # that wasn't caught by text-based selectors above
                fallback_btn = None
                for fb_sel in ["button[type='submit']", "input[type='submit']", "button[type='button'][aria-label]"]:
                    try:
                        fb = await page.query_selector(fb_sel)
                        if fb and await fb.is_visible():
                            box = await fb.bounding_box()
                            if box and box.get("width", 0) > 5 and box.get("height", 0) > 5:
                                fallback_btn = fb
                                break
                    except Exception:
                        pass

                if fallback_btn:
                    try:
                        await fallback_btn.click(timeout=8000)
                    except Exception:
                        await page.evaluate("el => el.click()", fallback_btn)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)
                elif not raw_fields:
                    empty_steps += 1
                    if empty_steps >= 2:
                        print(f"[agent] No form fields or navigation on step {step} -- likely not an apply page: {page.url}")
                        break
                else:
                    print(f"[agent] No next/submit on step {step} at: {page.url}")
                    break

            # Stale-page detection: if URL and form content are identical after 3 consecutive clicks,
            # the form is rejecting our input (e.g. Oracle validation errors) — bail
            cur_url = page.url
            try:
                form_snippet = await page.inner_text("form, [role='form'], main", timeout=3000)
                cur_hash = hash(form_snippet[:400])
            except Exception:
                cur_hash = hash(cur_url)
            if cur_url == last_url:
                same_url_only_count += 1
                if cur_hash == last_form_hash:
                    same_url_count += 1
                    if same_url_count >= 4:
                        print(f"[agent] Stale form (exact): stuck on {cur_url} for 4+ identical steps")
                        app.status = ApplicationStatus.NEEDS_MANUAL
                        app.error = f"Stale form loop on step {step} at {cur_url}"
                        return
                # Don't reset same_url_count on hash change — a form showing validation
                # errors changes the hash but the URL stays the same; we still need to bail.
                # Validation loop: same URL for 6+ steps regardless of content changes
                if same_url_only_count >= 6:
                    print(f"[agent] Validation loop: URL unchanged for 6+ steps at {cur_url}")
                    app.status = ApplicationStatus.NEEDS_MANUAL
                    app.error = f"Form validation loop (6+ steps same URL) at {cur_url}"
                    return
            else:
                same_url_count = 0
                same_url_only_count = 0
            last_url = cur_url
            last_form_hash = cur_hash

        if app.status == ApplicationStatus.APPLYING:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = f"Could not complete form after {max_pages} steps"

    # ── Smart field extraction & filling ─────────────────────────────────────

    async def _smart_fill_page(
        self, page, app: Application, domain: str = "", job_context: dict = None
    ) -> list:
        """
        Extract fields from current page and fill using SmartFormFiller or fallback.
        Returns the raw_fields list (used by callers to detect empty/non-form pages).
        """
        raw_fields = await page.evaluate(_EXTRACT_FIELDS_JS)

        # Normalise options to text-only list for SmartFormFiller classification
        fields_for_filler = [
            {
                "id":            f["id"],
                "label":         f["label"],
                "name":          f["name"],
                "type":          f["type"],
                "placeholder":   f["placeholder"],
                "options":       [o["text"] for o in f.get("options", [])],
                "required":      f["required"],
                "current_value": f["current_value"],
            }
            for f in raw_fields
        ]

        if self._smart_filler and fields_for_filler:
            try:
                profile = app.resume.profile

                # ── Lazy generation triggers ──────────────────────────────────
                # Before classifying fields, check what types are present so we
                # can generate the content we'll actually need — no earlier.
                _canonical_types = {f.get("type", "") for f in fields_for_filler}
                _labels_lower = " ".join(
                    (f.get("label", "") + " " + f.get("placeholder", "")).lower()
                    for f in fields_for_filler
                )
                _has_open_ended = any(
                    kw in _labels_lower for kw in (
                        "cover letter", "tell us", "why", "describe", "summary",
                        "about yourself", "experience", "motivat", "interest"
                    )
                )
                _has_file = "file" in _canonical_types or any(
                    f.get("type") == "file" for f in fields_for_filler
                )

                if hasattr(app.resume, 'ensure_tailored'):
                    if _has_file:
                        # File upload on this page — generate DOCX (which implies tailoring)
                        await app.resume.ensure_docx()
                    elif _has_open_ended and not app.resume.tailored_summary:
                        # Open-ended question — tailored context improves answer quality
                        await app.resume.ensure_tailored()

                # ── Cover letter field: generate the full CL now ──────────────
                _has_cl_field = any(
                    kw in _labels_lower for kw in ("cover letter", "covering letter")
                )
                if _has_cl_field and hasattr(app.resume, 'ensure_cover_letter'):
                    await app.resume.ensure_cover_letter()

                cl_text = getattr(app.resume, "cover_letter_text", "") or ""
                jc = {
                    **(job_context or {}),
                    "company": app.job.company,
                    "title": app.job.title,
                    "tailored_summary": getattr(app.resume, "tailored_summary", None) or "",
                    "highlighted_skills": getattr(app.resume, "highlighted_skills", []) or [],
                    "keywords_matched": getattr(app.resume, "keywords_matched", []) or [],
                }
                fills, meta = self._smart_filler.fill_fields(
                    fields_for_filler, profile, jc, domain
                )
                # Override with user-taught field memories (highest priority)
                if self.memory_lookup_fn:
                    for fill in fills:
                        lbl = fill.get("label") or fill.get("field_id", "")
                        if not lbl:
                            continue
                        stored = self.memory_lookup_fn(lbl, app.job.company)
                        if stored:
                            fill["value"] = stored
                # Override SmartFiller's cover-letter answer with the lazily-generated one
                if cl_text:
                    for fill in fills:
                        if fill.get("canonical_type") == "question.cover_letter":
                            fill["value"] = cl_text
                print(
                    f"[agent] SmartFiller: {len(fills)} fields | "
                    f"instant={meta['instant_hits']} ai_classify={meta.get('ai_classifier_calls',0)} "
                    f"ai_answers={meta.get('ai_answer_calls',0)} cached={meta.get('answer_cache_hits',0)}"
                )
                options_by_id = {f["id"]: f.get("options", []) for f in raw_fields}
                await self._execute_fills(page, fills, app.resume.docx_path, options_by_id, cl_text)
                await self._verify_fills(page, fills)
                # Record what was filled for the improvement tracker + run log
                if not hasattr(app, 'form_data') or not app.form_data:
                    app.form_data = {}
                for f in fills:
                    if f.get("field_id") and f.get("value") and f["value"] not in ("__resume__", "__file__", ""):
                        app.form_data[f.get("canonical_type", f["field_id"])] = f["value"]
                return raw_fields
            except Exception as e:
                print(f"[agent] SmartFiller error (falling back): {e}")

        # Fallback: simple keyword-based fill
        await self._fill_visible_fields(page, app)
        return raw_fields

    async def _execute_fills(
        self,
        page,
        fills: List[dict],
        resume_path: Optional[str],
        options_by_id: dict,
        cover_letter_text: str = "",
    ):
        """Apply SmartFormFiller decisions to the live DOM."""
        for fill in fills:
            value = fill.get("value", "")
            field_id = fill.get("field_id", "")
            canonical = fill.get("canonical_type", "")

            if not field_id:
                continue

            # File upload
            if value in ("__resume__", "__file__"):
                if value == "__resume__" and resume_path:
                    await self._upload_to_field(page, field_id, resume_path)
                elif value == "__file__" and canonical == "file.cover_letter_doc" and cover_letter_text:
                    cl_path = self._write_cover_letter_file(cover_letter_text)
                    if cl_path:
                        await self._upload_to_field(page, field_id, cl_path)
                        print(f"[agent] Uploaded cover letter to field: {field_id}")
                continue

            if not value:
                continue

            # Locate element
            el = await self._find_field_element(page, field_id)
            if not el:
                continue

            try:
                tag = await el.evaluate("el => el.tagName.toLowerCase()")

                if tag == "select":
                    raw_opts = options_by_id.get(field_id, [])
                    best = self._best_select_option(value, raw_opts)
                    if best:
                        await el.select_option(label=best["text"])
                    else:
                        # Try direct label/value match via Playwright
                        try:
                            await el.select_option(label=value)
                        except Exception:
                            try:
                                await el.select_option(value=value)
                            except Exception:
                                pass
                else:
                    await el.fill(value, timeout=5000)

                await asyncio.sleep(0.25)
            except Exception:
                continue

    async def _verify_fills(self, page, fills: list):
        """
        Re-read text/textarea fields after filling and log any that appear empty.
        Catches silent fill failures: broken selectors, shadow DOM interception,
        or fields that need a JS change event to accept the value.
        """
        mismatches = []
        for fill in fills:
            value = fill.get("value", "")
            field_id = fill.get("field_id", "")
            if not field_id or not value or value in ("__resume__", "__file__", ""):
                continue
            try:
                el = await self._find_field_element(page, field_id)
                if not el:
                    continue
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                if tag not in ("input", "textarea"):
                    continue
                actual = await el.input_value()
                # Flag if actual value is blank or missing the first 10 chars of expected
                if not actual or (len(value) > 10 and value[:10].lower() not in actual.lower()):
                    mismatches.append(fill.get("canonical_type", field_id))
            except Exception:
                continue
        if mismatches:
            print(f"[agent] [WARN] Fill verify: {len(mismatches)} field(s) may not have landed: {mismatches}")

    async def _find_field_element(self, page, field_id: str):
        """Locate a form element by id, name, or positional index."""
        if field_id.startswith("__pos_"):
            idx = int(field_id[6:])
            all_els = await page.query_selector_all(
                'input:not([type="submit"]):not([type="button"]):not([type="hidden"]),'
                'textarea, select'
            )
            return all_els[idx] if idx < len(all_els) else None

        for sel in [f'#{field_id}', f'[id="{field_id}"]', f'[name="{field_id}"]']:
            try:
                el = await page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _upload_to_field(self, page, field_id: str, resume_path: str):
        """Upload a file to a specific file input field."""
        for sel in [
            f'input[type="file"][id="{field_id}"]',
            f'input[type="file"][name="{field_id}"]',
            'input[type="file"]',
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.set_input_files(resume_path)
                    await asyncio.sleep(1.5)
                    print(f"[agent] Uploaded resume: {resume_path}")
                    return
            except Exception:
                continue

    def _write_cover_letter_file(self, text: str) -> Optional[str]:
        """Write cover letter text to a temp .txt file and return the path."""
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8",
                prefix="cover_letter_"
            ) as fh:
                fh.write(text)
                return fh.name
        except Exception as e:
            print(f"[agent] Could not write cover letter file: {e}")
            return None

    def _best_select_option(self, value: str, options: list) -> Optional[dict]:
        """
        Find best option dict from options_by_id raw data (list of {value, text}).
        Returns {value, text} or None.
        """
        if not options:
            return None
        v = value.lower().strip()

        # Exact text match
        for o in options:
            if o["text"].lower() == v:
                return o

        # Partial text match
        for o in options:
            if v in o["text"].lower() or o["text"].lower() in v:
                return o

        # Boolean yes/no match
        if v in ("yes", "true", "1", "i am", "i do"):
            for o in options:
                if o["text"].lower() in ("yes", "y", "true", "i am authorized"):
                    return o
        if v in ("no", "false", "0", "i am not"):
            for o in options:
                if o["text"].lower() in ("no", "n", "false"):
                    return o

        # Numeric range match (for salary options)
        import re
        try:
            num = float(re.sub(r"[^\d.]", "", value))
            for o in options:
                nums = re.findall(r"[\d,]+", o["text"])
                if len(nums) >= 2:
                    lo = float(nums[0].replace(",", ""))
                    hi = float(nums[1].replace(",", ""))
                    if lo <= num <= hi:
                        return o
        except (ValueError, IndexError):
            pass

        return None

    # ── EEO / demographic fields ──────────────────────────────────────────────

    async def _handle_eeo_fields(self, page):
        """
        Handle EEO/demographic selects that appear at the end of Greenhouse/Lever forms.
        Selects 'Decline to self-identify' or equivalent when available.
        """
        for sel in EEO_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el or not await el.is_visible():
                    continue

                options = await el.evaluate(
                    "el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))"
                )
                for opt in options:
                    opt_lower = opt["text"].lower()
                    if any(phrase in opt_lower for phrase in EEO_DECLINE_PHRASES):
                        await el.select_option(value=opt["value"])
                        await asyncio.sleep(0.2)
                        break
            except Exception:
                continue

    # ── Success page detection ────────────────────────────────────────────────

    async def _is_success_page(self, page) -> bool:
        try:
            content = (await page.content()).lower()
            return any(kw in content for kw in SUCCESS_KEYWORDS)
        except Exception:
            return False

    # ── Original form field helpers (kept as fallback) ────────────────────────

    async def _fill_visible_fields(self, page, app: Application):
        """Simple keyword-based fill — used when SmartFormFiller is unavailable."""
        profile = app.resume.profile
        field_map = self._build_field_map(profile)

        inputs = await page.query_selector_all("input:visible, textarea:visible, select:visible")
        for inp in inputs:
            try:
                field_type = await inp.get_attribute("type") or "text"
                if field_type in ("submit", "button", "hidden", "file", "checkbox", "radio"):
                    continue

                label = await self._get_field_label(page, inp)
                if not label:
                    continue

                value = self._match_field(label.lower(), field_map)
                if value:
                    tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await inp.select_option(label=value)
                        except Exception:
                            pass
                    else:
                        await inp.fill(value)
                    app.form_data[label] = value
                    await asyncio.sleep(0.3)
            except Exception:
                continue

        await self._handle_radio_groups(page, app)

    async def _get_field_label(self, page, input_el) -> str:
        label = await input_el.get_attribute("aria-label") or ""
        if label:
            return label
        placeholder = await input_el.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder
        field_id = await input_el.get_attribute("id")
        if field_id:
            label_el = await page.query_selector(f"label[for='{field_id}']")
            if label_el:
                return await label_el.inner_text()
        return await input_el.get_attribute("name") or ""

    def _build_field_map(self, profile) -> Dict[str, str]:
        return {
            "first name":           profile.name.split()[0] if profile.name else "",
            "last name":            profile.name.split()[-1] if profile.name else "",
            "full name":            profile.name or "",
            "name":                 profile.name or "",
            "email":                profile.email or "",
            "phone":                profile.phone or "",
            "city":                 profile.location.split(",")[0].strip() if "," in (profile.location or "") else profile.location or "",
            "state":                profile.location.split(",")[-1].strip() if "," in (profile.location or "") else "",
            "location":             profile.location or "",
            "linkedin":             getattr(profile, "linkedin_url", "") or "",
            "website":              getattr(profile, "website", "") or getattr(profile, "linkedin_url", "") or "",
            "github":               getattr(profile, "github_url", "") or "",
            "years of experience":  "10+",
            "salary":               str(getattr(profile, "min_salary", 80000)),
            "desired salary":       str(getattr(profile, "min_salary", 80000)),
            "expected salary":      str(getattr(profile, "min_salary", 80000)),
            "work authorization":   "Yes",
            "authorized to work":   "Yes",
            "require sponsorship":  "No",
            "visa sponsorship":     "No",
            "relocate":             "Yes",
            "willing to relocate":  "Yes",
        }

    def _match_field(self, label: str, field_map: Dict) -> Optional[str]:
        for key, value in field_map.items():
            if key in label or label in key:
                return value
        return None

    async def _handle_radio_groups(self, page, app: Application):
        yes_patterns = ["authorized", "eligible", "legally", "citizen", "willing to relocate", "driver", "background check"]
        no_patterns  = ["sponsorship", "require visa", "require work authorization"]

        groups = await page.query_selector_all("fieldset, [role='radiogroup']")
        for group in groups:
            try:
                label_text = (await group.inner_text()).lower()
                radios = await group.query_selector_all("input[type='radio']")
                if not radios:
                    continue

                if any(p in label_text for p in no_patterns):
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        lbl = (await self._get_field_label(page, radio)).lower()
                        if "no" in val or "no" in lbl:
                            await radio.check(timeout=5000)
                            break
                elif any(p in label_text for p in yes_patterns):
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        lbl = (await self._get_field_label(page, radio)).lower()
                        if "yes" in val or "yes" in lbl:
                            await radio.check(timeout=5000)
                            break
            except Exception:
                continue

    async def _maybe_upload_resume(self, page, app: Application) -> bool:
        """Upload resume to any visible file input. Returns True if a file was uploaded."""
        file_inputs = await page.query_selector_all("input[type='file']")
        if not file_inputs:
            return False

        # First file upload we've seen — generate the tailored resume now.
        # This is the earliest point we know a resume file is actually needed.
        if not app.resume.docx_path and hasattr(app.resume, 'ensure_docx'):
            await app.resume.ensure_docx()

        if not app.resume.docx_path:
            return False
        for inp in file_inputs:
            try:
                accept = (await inp.get_attribute("accept") or "").lower()
                if accept and ".pdf" not in accept and ".doc" not in accept and "application" not in accept:
                    continue
                await inp.set_input_files(app.resume.docx_path)
                print(f"[agent] Uploaded resume: {app.resume.docx_path}")
                # Wait for upload + server-side parsing (Oracle Fusion can take 4-8s)
                for _ in range(8):
                    await asyncio.sleep(1.5)
                    try:
                        content = (await page.content()).lower()
                        if "uploading" not in content:
                            break
                    except Exception:
                        break
                await asyncio.sleep(1)
                print(f"[agent] Uploaded resume to file input")
                return True
            except Exception as e:
                print(f"[agent] Warning: resume upload failed: {e}")
        return False

    # ── CAPTCHA detection & human-pause ──────────────────────────────────────

    async def _is_captcha_page(self, page) -> bool:
        try:
            content = (await page.content()).lower()
            # Use only specific phrases that ONLY appear in actual CAPTCHA/challenge pages
            # "robot" is intentionally excluded — it matches <meta name="robots"> everywhere
            text_triggers = [
                # Only phrases that appear in PAGE TEXT of actual challenge pages — not in <script> tags
                # g-recaptcha / h-captcha / cf-turnstile are EXCLUDED from content checks:
                # they appear in <script> src or class names even for invisible/background widgets
                # and would match on every legitimate Ashby/Greenhouse form
                "cf-challenge-running",
                "checking your browser before accessing",
                "verify you are human",
                "are you a robot",
                "i am not a robot",
                "enable javascript and cookies to continue",
            ]
            for trigger in text_triggers:
                if trigger in content:
                    return True
        except Exception:
            pass

        # Only match interactive CAPTCHA widgets that require user action.
        # Invisible background CAPTCHAs (Turnstile, invisible reCAPTCHA) have
        # [data-sitekey] but must NOT block the flow — so we skip that attribute alone.
        captcha_selectors = [
            # Visible recaptcha / hcaptcha iframes (interactive challenge boxes)
            "iframe[src*='recaptcha'][title*='challenge']",
            "iframe[src*='hcaptcha'][title*='challenge']",
            "iframe[src*='challenges.cloudflare']",
            # Cloudflare "Checking your browser" spinner overlay
            "#challenge-running",
            "#cf-challenge-running",
            # Visible interactive widget containers
            ".g-recaptcha:not([style*='display: none']):not([style*='display:none'])",
            ".h-captcha:not([style*='display: none']):not([style*='display:none'])",
            "#recaptcha",
        ]
        for sel in captcha_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    # Extra check: element must have non-trivial size (not 0x0 invisible widget)
                    box = await el.bounding_box()
                    if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                        return True
            except Exception:
                continue
        return False

    async def _handle_captcha(self, page, app: Application) -> bool:
        await self._screenshot(page, app, "captcha_detected")
        timeout_seconds = getattr(self.config, "captcha_timeout_seconds", 300)
        print(f"\n{'='*60}")
        print(f"[agent] [CAPTCHA] detected at {app.job.company}")
        print(f"[agent]    Solve it in the browser window, then click")
        print(f"[agent]    'CAPTCHA Defeated -- Resume' in the dashboard.")
        print(f"[agent]    Waiting up to {timeout_seconds}s...")
        print(f"{'='*60}\n")

        # Bring browser to front so the user can interact
        try:
            await page.bring_to_front()
        except Exception:
            pass
        # Windows: raise the Chromium window via pygetwindow if available
        try:
            import subprocess as _sp
            _sp.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "(New-Object -ComObject WScript.Shell).AppActivate('Chromium')"],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except Exception:
            pass

        if self.captcha_notify_fn:
            try:
                self.captcha_notify_fn({
                    "type": "captcha_detected",
                    "job_title": app.job.title,
                    "company": app.job.company,
                    "url": page.url,
                    "headless": self.config.headless,
                    "timeout_seconds": timeout_seconds,
                })
            except Exception:
                pass

        if not self.captcha_event:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "CAPTCHA detected — no event gate; apply manually"
            return False

        self._captcha_solved = False
        self.captcha_event.clear()
        start = time.time()
        timeout = float(timeout_seconds)

        while time.time() - start < timeout:
            await asyncio.sleep(2)
            if self.captcha_event.is_set():
                # event.set() was called — only trust it if our flag confirms actual solve
                if self._captcha_solved:
                    break
                # Stale set from a previous timeout — clear and keep waiting
                self.captcha_event.clear()
            # Auto-detect if user solved CAPTCHA without clicking the button
            if not await self._is_captcha_page(page):
                self._captcha_solved = True
                if self.captcha_notify_fn:
                    try:
                        self.captcha_notify_fn({"type": "captcha_resolved_auto"})
                    except Exception:
                        pass
                break

        # Always ungate the event so the next job's _handle_captcha starts clean
        self.captcha_event.set()

        if not self._captcha_solved:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = f"CAPTCHA not solved within {timeout_seconds}s"
            return False

        print(f"[agent] ✓ CAPTCHA solved — resuming {app.job.company}")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            await asyncio.sleep(2)
        return True

    # ── Screenshot helper ─────────────────────────────────────────────────────

    async def _screenshot(self, page, app: Application, label: str):
        if not self.config.screenshot_on_apply:
            return
        try:
            safe = "".join(c for c in f"{app.job.company}_{label}" if c.isalnum() or c in "_-")
            path = self.screenshots_dir / f"{safe}_{int(time.time())}.png"
            await page.screenshot(path=str(path), full_page=False)
        except Exception:
            pass
