"""
Application Agent
Playwright-based browser automation for submitting job applications.
Handles Indeed Easy Apply, LinkedIn Easy Apply, and generic company ATS forms.

SAFETY: auto_submit is False by default. The agent will fill forms and
screenshot them, but NOT click submit unless you explicitly enable it.
"""
import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

from job_agent.models import (
    Application, JobPosting, ApplicationStatus, JobPlatform, TailoredResume
)
from job_agent.config import AutomationConfig


class ApplicationAgent:
    def __init__(self, config: AutomationConfig, screenshots_dir: str = "./output/screenshots"):
        self.config = config
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def apply_batch(self, applications: list) -> list:
        """Run apply_one for a list of Application objects."""
        return asyncio.run(self._apply_batch_async(applications))

    def apply_one(self, application: Application) -> Application:
        """Apply to a single job. Blocks until complete."""
        return asyncio.run(self._apply_one_async(application))

    # ── Async implementation ──────────────────────────────────────────────────

    async def _apply_batch_async(self, applications: list) -> list:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
                args=["--start-maximized"],
            )
            context = await browser.new_context(
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

            await browser.close()
        return results

    async def _apply_one_async(self, application: Application) -> Application:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            result = await self._apply_one_with_context(application, context)
            await browser.close()
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
            app.status = ApplicationStatus.FAILED
            app.error = str(e)
            print(f"[agent] FAILED: {e}")
            await self._screenshot(page, app, "error")
        finally:
            await page.close()

        return app

    # ── Platform handlers ─────────────────────────────────────────────────────

    async def _apply_indeed(self, page, app: Application):
        """Handle Indeed application (direct or Indeed Apply)."""
        print(f"[agent] Opening Indeed job: {app.job.url}")
        await page.goto(app.job.url, timeout=self.config.timeout_ms)
        await page.wait_for_load_state("networkidle")
        await self._screenshot(page, app, "loaded")

        # Check for CAPTCHA
        if await self._is_captcha_page(page):
            await self._handle_captcha(page, app)
            return

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
            await self._handle_captcha(page, app)
            return

        # LinkedIn requires login - check if logged in
        if "login" in page.url or "authwall" in page.url:
            app.status = ApplicationStatus.FAILED
            app.error = "LinkedIn requires login. Please log in to LinkedIn in the browser first."
            print(f"[agent] ERROR: LinkedIn login required. Open the browser and log in.")
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

    # ── CAPTCHA detection ─────────────────────────────────────────────────────

    async def _is_captcha_page(self, page) -> bool:
        content = (await page.content()).lower()
        return any(x in content for x in ["captcha", "recaptcha", "hcaptcha", "robot", "verify you are human"])

    async def _handle_captcha(self, page, app: Application):
        """Alert user about CAPTCHA and pause."""
        app.status = ApplicationStatus.FAILED
        app.error = "CAPTCHA detected - manual intervention required"
        await self._screenshot(page, app, "captcha")
        if self.config.pause_on_captcha:
            print(f"\n[agent] ⚠️  CAPTCHA detected for {app.job.company}!")
            print(f"[agent]    Please solve it in the browser window, then press ENTER to continue...")
            input()

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
