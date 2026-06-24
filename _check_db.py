import sys, sqlite3
sys.path.insert(0, '.')
from job_agent.config import load_config
cfg = load_config()
conn = sqlite3.connect(cfg.output.db_path)
rows = conn.execute(
    "SELECT company, title, url FROM jobs "
    "WHERE url LIKE '%greenhouse%' OR url LIKE '%lever%' OR url LIKE '%ashby%' "
    "OR url LIKE '%workday%' OR url LIKE '%bamboohr%' OR url LIKE '%workable%' "
    "LIMIT 20"
).fetchall()
print(f"{len(rows)} ATS jobs in DB")
for co, title, url in rows:
    print(f"  {str(co)[:20]:20} {str(url)[:75]}")
conn.close()
