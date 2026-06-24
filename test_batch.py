"""
Batch apply test - runs test_apply.py sequentially for 10 job targets.
Each run is a fresh subprocess so there are no shared stdout/import conflicts.

Usage:
  python test_batch.py
"""
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Force UTF-8 output regardless of terminal encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
APPLY_SCRIPT = HERE / "test_apply.py"
PYTHON = sys.executable

TARGETS = [
    # LinkedIn Easy Apply — auto-searches for fresh PM job
    "https://www.linkedin.com/jobs/view/any",
    # Indeed — auto-searches for remote PM job (may redirect to external ATS)
    "https://www.indeed.com",
    # Ashby companies
    "https://jobs.ashbyhq.com/runway",
    "https://jobs.ashbyhq.com/supabase",
    "https://jobs.ashbyhq.com/linear",
    # Greenhouse — use direct job link format (avoids SPA listing nav issue)
    "https://boards.greenhouse.io/figma",
    "https://boards.greenhouse.io/duolingo",
    # Lever — direct job URL (UUID segment) avoids listing SPA nav
    "https://jobs.lever.co/notion",
    # Second LinkedIn Easy Apply run
    "https://www.linkedin.com/jobs/view/any",
    # Second Indeed run
    "https://www.indeed.com",
]

def run_one(url: str, idx: int, total: int) -> dict:
    domain = url.split("/")[2] if "://" in url else url
    sep = "-" * max(0, 50 - len(domain))
    print(f"\n[{idx}/{total}] -- {domain} {sep}", flush=True)
    try:
        result = subprocess.run(
            [PYTHON, str(APPLY_SCRIPT), url],
            capture_output=False,   # let output stream live to console
            cwd=str(HERE),
            timeout=300,            # 5 min max per job
        )
        return {"url": url, "exit": result.returncode}
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {domain} timed out after 5 min", flush=True)
        return {"url": url, "exit": -1, "error": "timeout"}
    except Exception as e:
        print(f"  [ERROR] {e}", flush=True)
        return {"url": url, "exit": -2, "error": str(e)}


def main():
    total = len(TARGETS)
    print(f"\n{'='*65}")
    print(f"  BATCH APPLY TEST - {total} targets")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}")

    results = []
    for i, url in enumerate(TARGETS, 1):
        results.append(run_one(url, i, total))

    print(f"\n{'='*65}")
    print(f"  BATCH DONE - {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}")
    ok  = sum(1 for r in results if r["exit"] == 0)
    bad = sum(1 for r in results if r["exit"] != 0)
    print(f"  Completed: {ok}/{total}   Errors: {bad}/{total}")
    for r in results:
        icon = "OK" if r["exit"] == 0 else "XX"
        domain = r["url"].split("/")[2] if "://" in r["url"] else r["url"]
        err = f" - {r.get('error','')}" if r.get("error") else ""
        print(f"  [{icon}] {domain}{err}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
