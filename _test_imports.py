import sys
sys.path.insert(0, '.')

from job_agent.automation.ats_handlers import detect_ats
from job_agent.automation.application_agent import ApplicationAgent
from job_agent.db.improvement_tracker import ImprovementTracker
from job_agent.db.field_semantics import FieldSemanticsDB
from job_agent.ai.form_filler import SmartFormFiller
from job_agent.config import load_config

tests = [
    ('https://boards.greenhouse.io/stripe/jobs/12345', 'greenhouse'),
    ('https://jobs.lever.co/figma/abc-def', 'lever'),
    ('https://jobs.ashbyhq.com/linear/abc', 'ashby'),
    ('https://stripe.wd5.myworkdayjobs.com/jobs', 'workday'),
    ('https://jobs.smartrecruiters.com/Microsoft', 'smartrecruiters'),
    ('https://www.indeed.com/viewjob?jk=abc', 'generic'),
    ('https://www.linkedin.com/jobs/view/123', 'generic'),
    ('https://apply.workable.com/deel/j/abc', 'workable'),
    ('https://stripe.bamboohr.com/jobs/view.php?id=1', 'bamboohr'),
    ('https://app.dover.com/apply/abc', 'dover'),
]

all_ok = True
for url, expected in tests:
    got = detect_ats(url)
    ok = got == expected
    if not ok:
        all_ok = False
    print(f"  {'OK' if ok else 'FAIL (got '+got+')'}: {url[:55]}")

print()
print("ATS detection:", "ALL PASS" if all_ok else "FAILURES FOUND")

# Test config loading
cfg = load_config()
print(f"Config loaded: {cfg.profile.name} | vault: {cfg.profile.obsidian_vault_path[:30]}...")

# Test improvement tracker
itracker = ImprovementTracker(cfg.output.db_path)
stats = itracker.get_weekly_stats()
print(f"Improvement tracker: DB OK, {stats['total']} total attempts tracked")

print("\nAll imports and basic functions: OK")
