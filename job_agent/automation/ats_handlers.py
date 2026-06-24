"""
ATS Detection and Navigation Helpers

Identifies which Applicant Tracking System is running from the URL,
and provides unified selector constants for multi-page form navigation.

Supported ATS platforms:
  Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  iCIMS, Taleo, BambooHR, Jobvite, JazzHR, Workable,
  Breezy HR, Recruitee, Pinpoint
"""
import re


# ── URL fingerprints ───────────────────────────────────────────────────────────

_ATS_PATTERNS = [
    ("greenhouse",      r"boards\.greenhouse\.io|grnh\.se"),
    ("lever",           r"jobs\.lever\.co"),
    ("ashby",           r"jobs\.ashbyhq\.com|app\.ashbyhq\.com"),
    ("workday",         r"myworkdayjobs\.com|wd\d+\.myworkday"),
    ("metacareers",     r"metacareers\.com"),
    ("smartrecruiters", r"jobs\.smartrecruiters\.com"),
    ("icims",           r"careers\.icims\.com|\.icims\.com/jobs"),
    ("taleo",           r"taleo\.net"),
    ("bamboohr",        r"\.bamboohr\.com/jobs"),
    ("jobvite",         r"jobs\.jobvite\.com"),
    ("jazzhr",          r"app\.jazz\.co|hire\.jazz\.co"),
    ("workable",        r"apply\.workable\.com"),
    ("breezyhr",        r"\.breezy\.hr/p/"),
    ("recruitee",       r"\.recruitee\.com/o/"),
    ("pinpoint",        r"app\.pinpointhq\.com"),
    ("rippling",        r"app\.rippling\.com/ats"),
    ("dover",           r"app\.dover\.com/apply"),
    ("ziprecruiter",    r"ziprecruiter\.com"),
    ("glassdoor",       r"glassdoor\.com"),
    ("trakstar",        r"\.hire\.trakstar\.com"),
    ("oracle_fusion",   r"oraclecloud\.com/hcmUI"),
]

# These ATS systems have well-known, consistent form structures we handle natively
KNOWN_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "workable"}


def detect_ats(url: str) -> str:
    """Return ATS platform name from URL, or 'generic'."""
    for name, pattern in _ATS_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return name
    return "generic"


# ── Consolidated selector strings ─────────────────────────────────────────────

# The "Apply" / "Apply for this Job" button on a job listing page
APPLY_BTN_SELECTORS = ", ".join([
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply for this Job')",
    "button:has-text('Apply for this job')",
    "button:has-text('Apply Now')",
    "button.apply-button",
    "a.apply-button",
    "[data-testid*='apply-btn']",
    "[data-qa='btn-apply']",
    "#apply-button",
    "a[href*='/apply']",
])

# ZipRecruiter-specific apply button selectors (1-Click Apply + standard Apply + external)
ZIPRECRUITER_APPLY_SELECTORS = ", ".join([
    "[data-testid='apply-button']",
    "[data-testid='job-apply-button']",
    "button:has-text('1-Click Apply')",
    "button:has-text('Quick Apply')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply Externally')",
    "a:has-text('Apply Externally')",
    "button:has-text('Apply on Company Site')",
    "a:has-text('Apply on Company Site')",
    ".job_apply_button",
    "button[class*='apply']",
    "a[class*='apply']",
])

# Glassdoor-specific apply button selectors (Easy Apply modal + external redirect)
GLASSDOOR_APPLY_SELECTORS = ", ".join([
    "[data-test='applyButton']",
    "button[data-test='applyButton']",
    "[data-brandingid='applyButton']",
    "button:has-text('Easy Apply')",
    "[data-test='easy-apply']",
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    "button:has-text('Apply on Company Site')",
    "a:has-text('Apply on Company Site')",
    "[aria-label*='Apply']",
    "button[class*='apply']",
])

# "Next" / "Continue" buttons inside multi-step forms
NEXT_BTN_SELECTORS = ", ".join([
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Next Step')",
    "button:has-text('Save & Continue')",
    "button:has-text('Save and Continue')",
    "button[data-testid*='next']",
    "button[data-testid*='continue']",
    "button[data-qa*='next']",
    "a:has-text('Next')",
    "input[type='button'][value='Next']",
    "input[type='button'][value='Continue']",
])

# Final submit button
# NOTE: "Apply Now" is intentionally NOT here — it's a job-listing entry button (see APPLY_BTN_SELECTORS).
# Putting it here caused false-positives on ATS job description pages (Oracle Fusion, etc.)
SUBMIT_BTN_SELECTORS = ", ".join([
    "button[type='submit']:has-text('Submit')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit your application')",
    "button:has-text('Send Application')",
    "button:has-text('Complete Application')",
    "button:has-text('Finish Application')",
    "button[aria-label*='Submit']",
    "button[aria-label*='submit']",
    "input[type='submit']",
    "#submit_app",
    "#btn-submit",
    "[data-testid='submit-application']",
    "button[data-testid*='submit']",
    "button[data-qa='btn-submit']",
])

# Page content that strongly signals a successful submission
SUCCESS_KEYWORDS = [
    "application submitted",
    "application received",
    "thank you for applying",
    "thanks for applying",
    "application complete",
    "successfully submitted",
    "we received your application",
    "your application has been submitted",
    "we'll be in touch",
    "we will be in touch",
    "application was submitted",
    "applied successfully",
    "we've received your application",
    "you've submitted your application",
]

# EEO/Demographic field selectors (we default to "prefer not to answer")
EEO_SELECTORS = [
    "select[name*='gender']",
    "select[name*='race']",
    "select[name*='ethnicity']",
    "select[name*='veteran']",
    "select[name*='disability']",
    "select[id*='gender']",
    "select[id*='race']",
    "select[id*='ethnicity']",
    "select[id*='veteran']",
    "select[id*='disability']",
    "select[id*='eeoc']",
    "select[id*='eeo']",
]

EEO_DECLINE_PHRASES = [
    "decline", "prefer not", "don't wish", "do not wish",
    "rather not", "no answer", "not specified", "choose not",
    "prefer to not", "i don't", "i do not", "not disclosed",
    "no selection", "opt out",
]
