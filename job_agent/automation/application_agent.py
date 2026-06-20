"""
Application Agent
Playwright-based browser automation for submitting job applications.
Handles Indeed Easy Apply, LinkedIn Easy Apply, and generic company ATS forms.

SAFETY: auto_submit is False by default. The agent will fill forms and
screenshot them, but NOT click submit unless you explicitly enable it.
"""
import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional
from datetime import datetime

from job_agent.models import (
    Application, JobPosting, ApplicationStatus, JobPlatform, TailoredResume
)
from job_agent.config import AutomationConfig


class ApplicationAgent:
    def __init__(
        self,
        config: AutomationConfig,
        screenshots_dir: str = "./output/screenshots",
        captcha_event: Optional[threading.Event] = None,
        captcha_notify_fn: Optional[Callable] = None,
    ):
        self.config = config
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        # CAPTCHA pause-gate — wired up from main.py after orchestrator creates this agent
        self.captcha_event = captcha_event       # threading.Event: set=green, clear=waiting
        self.captcha_notify_fn = captcha_notify_fn  # sync fn(info:dict) → notifies WS

    def apply_batch(self, applications: list) -> list:
        """Run apply_one for a list of Application objects."""
        return asyncio.run(self._apply_batch_async(applications))

    def apply_one(self, application: Application) -> Application:
        """Apply to a single job. Blocks until complete."""
        return asyncio.run(self._apply_one_async(application))

    # ── Async implementation ──────────────────────────────────────────────────

    # Persistent browser profile — same dir as main.py uses for login verification
    BROWSER_PROFILE_DIR = Path(__file__).parent.parent.parent / "output" / "browser_profile"

    async def _apply_batch_async(self, applications: list) -> list:
        from playwright.async_api import async_playwright

        self.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(self.BROWSER_PROFILE_DIR),
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
                args=["--start-maximized"],
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            results = []
            for i, app in enumerate(applications):
                if i >= self.config.max_applications_per_run:
                    print(f"[agent] Reached max_applications_per_run limit ({self.config.max_applications_per_run})")
                    break
                print(f"\n[agent] Applying to {i+1}/{len(applications)}: {app.job.title} @ {app.job.company}")
                result = await self._apply_one_with_context(app, context)
                results.append(result)
                await asyncio.sleep(3)  # Pause between applications

            await context.close()
        return results

    async def _apply_one_async(self, application: Application) -> Application:
        from playwright.async_api import async_playwright

        self.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(self.BROWSER_PROFILE_DIR),
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
                viewport={"width": 1280, "height": 900},
            )
            result = await self._apply_one_with_context(application, context)
            await context.close()
        return result

    async def _apply_one_with_context(self, app: Application, context) -> Application:
        """Core apply logic - dispatch by platform."""
        app.status = ApplicationStatus.APPLYING
        page = await context.new_page()

        try:
            platform = app.job.platform
            if platform == JobPlatform.INDEED:
                await self._apply_indeed(page, app)
            elif platform == JobPlatform.LINKEDIN:
                await self._apply_linkedin(page, app)
            else:
                await self._apply_generic(page, app)

        except Exception as e:
            err = str(e)
            app.error = err
            # Classify: needs_manual if human intervention is the only path forward
            if self._needs_human(err):
                app.status = ApplicationStatus.NEEDS_MANUAL
                print(f"[agent] NEEDS MANUAL ({app.job.company}): {err}")
            else:
                app.status = ApplicationStatus.FAILED
                print(f"[agent] FAILED ({app.job.company}): {err}")
            await self._screenshot(page, app, "error")
        finally:
            await page.close()

        return app

    def _needs_human(self, error: str) -> bool:
        """True if the error means a human must apply — not a bug we can retry."""
        human_signals = [
            "captcha", "login", "authwall", "sign in", "authentication",
            "apply button not found", "could not find apply",
            "workday", "greenhouse", "lever", "icims", "taleo", "smartrecruiters",
            "indeed apply not available", "external application",
            "manual", "sponsorship", "visa",
        ]
        err_lower = error.lower()
        return any(s in err_lower for s in human_signals)

    # ── Platform handlers ─────────────────────────────────────────────────────

    async def _apply_indeed(self, page, app: Application):
        """Handle Indeed application (direct or Indeed Apply)."""
        print(f"[agent] Opening Indeed job: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms)
        await page.wait_for_load_state("networkidle")
        await self._screenshot(page, app, "loaded")

        # Check for CAPTCHA
        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return  # timed out or no gate — needs manual

        # Look for "Apply Now" or "Indeed Apply" button
        apply_btn = await page.query_selector(
            "button[data-testid='IndeedApplyButton'], "
            "button:has-text('Apply now'), "
            "a:has-text('Apply now')"
        )

        if not apply_btn:
            app.status = ApplicationStatus.FAILED
            app.error = "Could not find Apply button"
            return

        await apply_btn.click()
        await asyncio.sleep(2)
        await self._screenshot(page, app, "apply_started")

        # Fill the multi-step form
        await self._fill_indeed_form(page, app)

    async def _fill_indeed_form(self, page, app: Application):
        """Navigate and fill Indeed's multi-step application form."""
        profile = app.resume.profile
        max_steps = 10
        step = 0

        while step < max_steps:
            step += 1
            await asyncio.sleep(1)

            # CAPTCHA can appear mid-form after bot detection
            if await self._is_captcha_page(page):
                if not await self._handle_captcha(page, app):
                    return

            # Check if we've reached the review/submit page
            if await page.query_selector("button[aria-label='Submit your application']"):
                await self._screenshot(page, app, f"step_{step}_review")
                if self.config.auto_submit:
                    await page.click("button[aria-label='Submit your application']")
                    await asyncio.sleep(2)
                    app.status = ApplicationStatus.APPLIED
                    app.applied_at = datetime.now()
                    await self._screenshot(page, app, "submitted")
                    print(f"[agent] ✓ SUBMITTED: {app.job.title} @ {app.job.company}")
                else:
                    app.status = ApplicationStatus.APPLIED  # Mark as "ready to submit"
                    app.notes = "Form filled. auto_submit=False - review and submit manually."
                    print(f"[agent] ✓ Form filled (auto_submit=False). Review at: {page.url}")
                return

            # Fill visible inputs
            await self._fill_visible_fields(page, app)

            # Upload resume if there's a file input
            await self._maybe_upload_resume(page, app)

            # Click Next / Continue
            next_btn = await page.query_selector(
                "button:has-text('Continue'), button:has-text('Next'), "
                "button[aria-label='Continue to next step']"
            )
            if next_btn:
                await next_btn.click()
                await page.wait_for_load_state("networkidle")
            else:
                break

        app.notes = f"Stopped at step {step} - may need manual completion"

    async def _apply_linkedin(self, page, app: Application):
        """Handle LinkedIn Easy Apply."""
        print(f"[agent] Opening LinkedIn job: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms)
        await page.wait_for_load_state("networkidle")
        await self._screenshot(page, app, "loaded")

        if await self._is_captcha_page(page):
            if not await self._handle_captcha(page, app):
                return

        # LinkedIn requires login - check if logged in
        if "login" in page.url or "authwall" in page.url:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "LinkedIn login required — apply manually or log in first"
            print(f"[agent] LinkedIn login wall hit for {app.job.company} — needs_manual")
            return

        # Find Easy Apply button
        easy_apply = await page.query_selector(
            "button.jobs-apply-button:has-text('Easy Apply'), "
            "button[aria-label*='Easy Apply']"
        )

        if not easy_apply:
            # Redirect to external application
            external = await page.query_selector("button:has-text('Apply')")
            if external:
                await external.click()
                await asyncio.sleep(2)
                # Try to fill the external form
                await self._apply_generic(page, app)
                return
            app.status = ApplicationStatus.FAILED
            app.error = "LinkedIn Easy Apply button not found"
            return

        await easy_apply.click()
        await asyncio.sleep(2)
        await self._fill_linkedin_modal(page, app)

    async def _fill_linkedin_modal(self, page, app: Application):
        """Fill LinkedIn Easy Apply modal dialog."""
        max_steps = 10
        for step in range(max_steps):
            await asyncio.sleep(1.5)
            await self._screenshot(page, app, f"linkedin_step_{step}")

            if await self._is_captcha_page(page):
                if not await self._handle_captcha(page, app):
                    return

            # Check for submit
            submit = await page.query_selector("button[aria-label='Submit application']")
            if submit:
                if self.config.auto_submit:
                    await submit.click()
                    await asyncio.sleep(2)
                    app.status = ApplicationStatus.APPLIED
                    app.applied_at = datetime.now()
                    await self._screenshot(page, app, "submitted")
                    print(f"[agent] ✓ LinkedIn SUBMITTED: {app.job.title} @ {app.job.company}")
                else:
                    app.status = ApplicationStatus.APPLIED
                    app.notes = "LinkedIn form filled. auto_submit=False - submit manually in browser."
                    print(f"[agent] ✓ LinkedIn form filled (manual submit required)")
                return

            await self._fill_visible_fields(page, app)
            await self._maybe_upload_resume(page, app)

            # Next step
            next_btn = await page.query_selector(
                "button[aria-label='Continue to next step'], "
                "button:has-text('Next'), button:has-text('Review')"
            )
            if next_btn:
                await next_btn.click()
            else:
                break

    async def _apply_generic(self, page, app: Application):
        """Generic ATS form filler for company career pages."""
        print(f"[agent] Generic apply: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms)
        await page.wait_for_load_state("networkidle")
        await self._screenshot(page, app, "loaded")

        await self._fill_visible_fields(page, app)
        await self._maybe_upload_resume(page, app)
        await self._screenshot(page, app, "filled")

        if self.config.auto_submit:
            submit = await page.query_selector(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Submit'), button:has-text('Apply')"
            )
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                app.status = ApplicationStatus.APPLIED
                app.applied_at = datetime.now()
                await self._screenshot(page, app, "submitted")
        else:
            app.status = ApplicationStatus.APPLIED
            app.notes = "Form filled. Review in browser and submit manually."

    # ── Form field helpers ────────────────────────────────────────────────────

    async def _fill_visible_fields(self, page, app: Application):
        """Intelligently fill all visible text fields based on their labels."""
        profile = app.resume.profile
        field_map = self._build_field_map(profile)

        inputs = await page.query_selector_all("input:visible, textarea:visible, select:visible")
        for inp in inputs:
            try:
                field_type = await inp.get_attribute("type") or "text"
                if field_type in ("submit", "button", "hidden", "file", "checkbox", "radio"):
                    continue

                # Get label
                label = await self._get_field_label(page, inp)
                if not label:
                    continue

                label_lower = label.lower()
                value = self._match_field(label_lower, field_map)

                if value:
                    tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await inp.select_option(label=value)
                    else:
                        await inp.fill(value)
                    app.form_data[label] = value
                    await asyncio.sleep(0.3)

            except Exception:
                continue

        # Handle yes/no questions about employment eligibility
        await self._handle_radio_groups(page, app)

    async def _get_field_label(self, page, input_el) -> str:
        """Try multiple strategies to get a form field's label."""
        # aria-label
        label = await input_el.get_attribute("aria-label") or ""
        if label:
            return label

        # placeholder
        placeholder = await input_el.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder

        # Associated <label> element
        field_id = await input_el.get_attribute("id")
        if field_id:
            label_el = await page.query_selector(f"label[for='{field_id}']")
            if label_el:
                return await label_el.inner_text()

        # name attribute
        name = await input_el.get_attribute("name") or ""
        return name

    def _build_field_map(self, profile) -> Dict[str, str]:
        """Map label keywords to profile values."""
        return {
            # Personal info
            "first name": profile.name.split()[0] if profile.name else "",
            "last name": profile.name.split()[-1] if profile.name else "",
            "full name": profile.name,
            "name": profile.name,
            "email": profile.email,
            "phone": profile.phone,
            "city": profile.location.split(",")[0].strip() if "," in profile.location else profile.location,
            "state": profile.location.split(",")[-1].strip() if "," in profile.location else "",
            "location": profile.location,
            "address": profile.location,
            "zip": "",
            "linkedin": profile.linkedin_url,
            "website": profile.website or profile.linkedin_url,
            "github": profile.github_url,
            # Common questions
            "years of experience": "10+",
            "salary": str(profile.min_salary),
            "desired salary": str(profile.min_salary),
            "expected salary": str(profile.min_salary),
            "work authorization": "Yes",
            "authorized to work": "Yes",
            "require sponsorship": "No",
            "visa sponsorship": "No",
            "relocate": "Yes",
            "willing to relocate": "Yes",
            "cover letter": f"I am excited to apply for this {profile.target_roles[0] if profile.target_roles else 'role'} opportunity.",
        }

    def _match_field(self, label: str, field_map: Dict) -> Optional[str]:
        """Find the best match for a form field label."""
        for key, value in field_map.items():
            if key in label or label in key:
                return value
        return None

    async def _handle_radio_groups(self, page, app: Application):
        """Handle yes/no and eligibility radio buttons."""
        yes_patterns = [
            "authorized", "eligible", "legally", "citizen",
            "willing to relocate", "driver", "background check"
        ]
        no_patterns = ["sponsorship", "require visa", "require work authorization"]

        groups = await page.query_selector_all("fieldset, [role='radiogroup']")
        for group in groups:
            try:
                label_text = (await group.inner_text()).lower()
                radios = await group.query_selector_all("input[type='radio']")
                if not radios:
                    continue

                if any(p in label_text for p in no_patterns):
                    # Find "No" radio
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        lbl = (await self._get_field_label(page, radio)).lower()
                        if "no" in val or "no" in lbl:
                            await radio.check()
                            break
                elif any(p in label_text for p in yes_patterns):
                    # Find "Yes" radio
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        lbl = (await self._get_field_label(page, radio)).lower()
                        if "yes" in val or "yes" in lbl:
                            await radio.check()
                            break
            except Exception:
                continue

    async def _maybe_upload_resume(self, page, app: Application):
        """Upload resume DOCX to any file inputs on the page."""
        if not app.resume.docx_path:
            return

        file_inputs = await page.query_selector_all("input[type='file']")
        for inp in file_inputs:
            try:
                accept = (await inp.get_attribute("accept") or "").lower()
                if accept and ".pdf" not in accept and ".doc" not in accept and "application" not in accept:
                    continue  # Not a resume upload field
                await inp.set_input_files(app.resume.docx_path)
                await asyncio.sleep(1)
                print(f"[agent] Uploaded resume to file input")
                break
            except Exception as e:
                print(f"[agent] Warning: could not upload resume: {e}")

    # ── CAPTCHA detection & human-pause flow ─────────────────────────────────

    async def _is_captcha_page(self, page) -> bool:
        """Detect CAPTCHA via page content AND DOM selectors for reliable coverage."""
        # Fast text scan first
        try:
            content = (await page.content()).lower()
            if any(x in content for x in [
                "g-recaptcha", "h-captcha", "hcaptcha-widget",
                "cf-challenge", "checking your browser",
                "verify you are human", "robot",
            ]):
                return True
        except Exception:
            pass

        # Selector-based checks (more precise — catches lazy-loaded widgets)
        captcha_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='challenges.cloudflare']",
            ".g-recaptcha",
            ".h-captcha",
            "#recaptcha",
            "[data-sitekey]",
            "div[class*='captcha']",
        ]
        for sel in captcha_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _handle_captcha(self, page, app: Application) -> bool:
        """
        Pause the bot and wait up to 2 minutes for the human to solve the CAPTCHA.
        Returns True if solved (continue applying), False if timed out (needs manual).

        Flow:
          1. Bring the browser window to the front so the user can see it.
          2. Notify the UI via WebSocket so a "Solve CAPTCHA" card appears.
          3. Poll every 2 s — resume when:
               a. captcha_event is set (user clicked "I solved it" in the UI), OR
               b. The CAPTCHA widget has disappeared from the page.
          4. On timeout → mark needs_manual (same as before, but user had a chance).
        """
        await self._screenshot(page, app, "captcha_detected")
        print(f"\n{'='*60}")
        print(f"[agent] ⚠  CAPTCHA detected at {app.job.company}")
        if self.config.headless:
            print(f"[agent]    Browser is in HEADLESS mode — you cannot see it.")
            print(f"[agent]    Set  headless: false  in config.yaml to solve CAPTCHAs.")
        else:
            print(f"[agent]    A browser window should appear on your screen.")
            print(f"[agent]    Solve the CAPTCHA there, then click 'I solved it'")
            print(f"[agent]    in the Job Agent UI.")
        print(f"[agent]    Waiting up to 2 minutes...")
        print(f"{'='*60}\n")

        # Bring the browser to front so user can interact
        try:
            await page.bring_to_front()
        except Exception:
            pass

        # Notify the frontend via WebSocket
        if self.captcha_notify_fn:
            try:
                self.captcha_notify_fn({
                    "type": "captcha_detected",
                    "job_title": app.job.title,
                    "company": app.job.company,
                    "url": page.url,
                    "headless": self.config.headless,
                })
            except Exception:
                pass

        # If no gate is configured, fall back to old behavior
        if not self.captcha_event:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "CAPTCHA detected — apply manually at the job URL"
            return False

        # Pause: clear the event so is_set() returns False until user resolves
        self.captcha_event.clear()

        start = time.time()
        timeout = 120.0  # 2 minutes
        solved = False
        while time.time() - start < timeout:
            # Yield to event loop every 2 s (non-blocking — Playwright stays responsive)
            await asyncio.sleep(2)
            # Check if the UI button was clicked (event set from main thread)
            if self.captcha_event.is_set():
                solved = True
                break
            # Also check if CAPTCHA widget disappeared naturally from the page
            if not await self._is_captcha_page(page):
                solved = True
                break

        # Reset gate to green for the next CAPTCHA encounter
        self.captcha_event.set()

        if not solved:
            app.status = ApplicationStatus.NEEDS_MANUAL
            app.error = "CAPTCHA not solved within 2 minutes — apply manually"
            print(f"[agent] CAPTCHA timed out for {app.job.company} — marked needs_manual")
            return False

        print(f"[agent] ✓ CAPTCHA solved for {app.job.company} — resuming application")
        # Wait for any redirect/reload that happens after a CAPTCHA clears
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
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
