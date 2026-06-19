"""
Application Tracker
SQLite-based persistence for tracking all job applications,
their status, and the pipeline.
"""
import sqlite3
import json
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from job_agent.models import Application, ApplicationStatus, JobPosting, JobPlatform


class Tracker:
    def __init__(self, db_path: str = "./output/applications.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    description TEXT,
                    url TEXT,
                    platform TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    salary_text TEXT,
                    remote INTEGER DEFAULT 0,
                    fit_score REAL DEFAULT 0,
                    salary_score REAL DEFAULT 0,
                    combined_score REAL DEFAULT 0,
                    score_breakdown TEXT,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    status TEXT DEFAULT 'queued',
                    resume_path TEXT,
                    keywords_matched TEXT,
                    ats_score REAL DEFAULT 0,
                    applied_at TIMESTAMP,
                    interview_at TIMESTAMP,
                    notes TEXT,
                    error TEXT,
                    form_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
                CREATE INDEX IF NOT EXISTS idx_apps_job_id ON applications(job_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(combined_score DESC);
            """)
        print(f"[tracker] Database ready: {self.db_path}")

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def upsert_job(self, job: JobPosting) -> bool:
        """Insert or update a job posting. Returns True if new."""
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE id = ?", (job.id,)).fetchone()
            if existing:
                conn.execute("""
                    UPDATE jobs SET fit_score=?, salary_score=?, combined_score=?,
                    score_breakdown=? WHERE id=?
                """, (job.fit_score, job.salary_score, job.combined_score,
                      json.dumps(job.score_breakdown), job.id))
                return False
            else:
                conn.execute("""
                    INSERT INTO jobs (id, title, company, location, description, url,
                    platform, salary_min, salary_max, salary_text, remote,
                    fit_score, salary_score, combined_score, score_breakdown)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    job.id, job.title, job.company, job.location,
                    job.description[:5000],  # Trim for storage
                    job.url, job.platform.value if hasattr(job.platform, 'value') else job.platform,
                    job.salary_min, job.salary_max, job.salary_text,
                    1 if job.remote else 0,
                    job.fit_score, job.salary_score, job.combined_score,
                    json.dumps(job.score_breakdown)
                ))
                return True

    def get_jobs(self, min_score: float = 0, limit: int = 100) -> List[Dict]:
        """Fetch top jobs by score."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT j.*, a.status as app_status, a.id as app_id
                FROM jobs j
                LEFT JOIN applications a ON j.id = a.job_id
                WHERE j.combined_score >= ?
                ORDER BY j.combined_score DESC
                LIMIT ?
            """, (min_score, limit)).fetchall()
        return [dict(r) for r in rows]

    def already_applied(self, job_id: str) -> bool:
        """Check if we've already applied to this job."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM applications WHERE job_id = ? AND status NOT IN ('failed')",
                (job_id,)
            ).fetchone()
        return row is not None

    # ── Applications ──────────────────────────────────────────────────────────

    def create_application(self, application: Application) -> str:
        """Save a new application record."""
        app_id = application.id or str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO applications
                (id, job_id, status, resume_path, keywords_matched, ats_score, notes)
                VALUES (?,?,?,?,?,?,?)
            """, (
                app_id,
                application.job.id,
                application.status.value,
                application.resume.docx_path or "",
                json.dumps(application.resume.keywords_matched),
                application.resume.ats_score_estimate,
                application.notes,
            ))
        return app_id

    def update_application(self, app_id: str, **kwargs):
        """Update specific fields on an application."""
        allowed = {"status", "applied_at", "interview_at", "notes", "error", "form_data"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        # Serialize complex types
        if "status" in updates and hasattr(updates["status"], "value"):
            updates["status"] = updates["status"].value
        if "form_data" in updates and isinstance(updates["form_data"], dict):
            updates["form_data"] = json.dumps(updates["form_data"])

        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE applications SET {set_clause} WHERE id=?",
                list(updates.values()) + [app_id]
            )

    def sync_application(self, application: Application):
        """Sync an Application object's state back to the DB."""
        self.update_application(
            application.id,
            status=application.status,
            applied_at=application.applied_at.isoformat() if application.applied_at else None,
            notes=application.notes,
            error=application.error,
            form_data=application.form_data,
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return pipeline summary stats."""
        with self._connect() as conn:
            total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            apps = conn.execute("""
                SELECT status, COUNT(*) as count FROM applications GROUP BY status
            """).fetchall()
            recent = conn.execute("""
                SELECT j.title, j.company, a.status, a.applied_at
                FROM applications a JOIN jobs j ON a.job_id = j.id
                ORDER BY a.created_at DESC LIMIT 10
            """).fetchall()

        status_counts = {row["status"]: row["count"] for row in apps}
        return {
            "total_jobs_found": total_jobs,
            "total_applications": sum(status_counts.values()),
            "by_status": status_counts,
            "recent": [dict(r) for r in recent],
        }

    def print_dashboard(self):
        """Print a quick status dashboard to console."""
        stats = self.summary()
        print("\n" + "="*60)
        print("  JOB AGENT DASHBOARD")
        print("="*60)
        print(f"  Jobs found:         {stats['total_jobs_found']}")
        print(f"  Applications:       {stats['total_applications']}")
        print()
        for status, count in stats["by_status"].items():
            icon = {"applied": "✓", "interview": "★", "failed": "✗",
                    "queued": "○", "offer": "💰"}.get(status, "·")
            print(f"  {icon}  {status.ljust(15)} {count}")
        print()
        if stats["recent"]:
            print("  Recent applications:")
            for r in stats["recent"][:5]:
                date = r.get("applied_at", "")[:10] if r.get("applied_at") else "pending"
                print(f"    [{date}] {r['title']} @ {r['company']} — {r['status']}")
        print("="*60 + "\n")
