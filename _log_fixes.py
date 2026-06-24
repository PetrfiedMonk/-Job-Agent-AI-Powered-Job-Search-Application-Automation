"""Log the bugs we fixed during this testing session to the improvement tracker."""
import sys
sys.path.insert(0, '.')
from job_agent.config import load_config
from job_agent.db.improvement_tracker import ImprovementTracker

cfg = load_config()
t = ImprovementTracker(cfg.output.db_path)

# Log fixes as resolved wins
t.log_success("ashby", "https://jobs.ashbyhq.com/runway/e22fd0c9-06f5-4b27-9157-f3d8bac5b80a",
              "Runway", "Engineering Manager", fields_filled=6, auto_submitted=False)

# Mark the bugs fixed (they were previously logged as failures — add context)
t.log_failure("all", "", "all", "all",
    "FIXED: _is_captcha_page false-positive on 'robot' (meta name=robots), "
    "'cf-turnstile' (invisible Turnstile on Ashby forms), and 'g-recaptcha' "
    "(invisible reCAPTCHA v3 script tag). Now only detect interactive challenges "
    "via page-text phrases and visible selector+size checks.",
    step=0, context="bug_fix")

t.log_failure("all", "", "all", "all",
    "FIXED: _navigate_to_apply_form networkidle timeout on SPA tab switches (Ashby). "
    "Tab clicks don't trigger full navigations, so networkidle never fires. "
    "Now uses 8s timeout with fallback asyncio.sleep(2).",
    step=0, context="bug_fix")

t.log_failure("ashby", "", "all", "all",
    "FIXED: Ashby auto-navigation selector used '/jobs/' path segment but Ashby URLs "
    "are /{company}/{slug} with no '/jobs/' segment. Now uses JS evaluation to find "
    "links starting with /{company}/ prefix.",
    step=0, context="bug_fix")

note = t.write_vault_note(cfg.profile.obsidian_vault_path)
print(f"Vault updated: {note}")
