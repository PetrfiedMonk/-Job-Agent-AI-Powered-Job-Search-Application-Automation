"""
Application Tracker
SQLite-based persistence for tracking all job applications,
their status, and the pipeline.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _content_hash(title: str, company: str, location: str) -> str:
        """
        Platform-agnostic identity for a job posting.
        Same role on Indeed + LinkedIn + company site → same hash → skip duplicate.
        """
        def norm(s: str) -> str:
            s = (s or '').lower()
            s = re.sub(r'[^a-z0-9\s]', '', s)
            s = re.sub(r'\s+', ' ', s).strip()
            return s
        key = f"{norm(title)}|{norm(company)}|{norm(location)}"
        return hashlib.md5(key.encode()).hexdigest()[:16]

    @staticmethod
    def recency_bonus(posted_date) -> float:
        """
        0-15 point bonus added to combined_score based on posting age.
        Jobs posted within 48 h get 3-4x more interview callbacks — reward them.
        Decays to 0 after 7 days so stale posts aren't inflated.
        """
        if not posted_date:
            return 0.0
        if isinstance(posted_date, str):
            try:
                posted_date = datetime.fromisoformat(posted_date)
            except (ValueError, TypeError):
                return 0.0
        age_h = (datetime.now() - posted_date).total_seconds() / 3600
        if age_h <= 12:   return 15.0
        if age_h <= 24:   return 12.0
        if age_h <= 48:   return 9.0
        if age_h <= 72:   return 5.0
        if age_h <= 120:  return 2.0
        return 0.0

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    content_hash TEXT,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    description TEXT,
                    description_summary TEXT,
                    url TEXT,
                    platform TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    salary_text TEXT,
                    remote INTEGER DEFAULT 0,
                    posted_date TEXT,
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
                CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(combined_score DESC);
            """)
            # Additive migration — add new columns to existing databases.
            # Indexes on new columns must come AFTER the columns are added.
            for col, typedef in [
                ("content_hash",        "TEXT"),
                ("posted_date",         "TEXT"),
                ("description_summary", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # Column already exists — safe to ignore

            # Create indexes on the new columns now that they're guaranteed to exist
            for stmt in [
                "CREATE INDEX IF NOT EXISTS idx_jobs_hash   ON jobs(content_hash)",
                "CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_date)",
            ]:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass
        print(f"[tracker] Database ready: {self.db_path}")

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def upsert_job(self, job: JobPosting) -> bool:
        """
        Insert or update a job posting. Returns True if new.
        Cross-platform duplicate check: same title+company+location on a different
        platform is detected via content_hash and skipped (returns False).
        """
        chash = self._content_hash(job.title, job.company, job.location)
        posted = job.posted_date.isoformat() if job.posted_date else None
        summary = getattr(job, 'description_summary', None) or ''

        with self._connect() as conn:
            # 1. Exact ID match — update scores only
            existing_id = conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if existing_id:
                conn.execute("""
                    UPDATE jobs SET fit_score=?, salary_score=?, combined_score=?,
                    score_breakdown=?, description_summary=? WHERE id=?
                """, (job.fit_score, job.salary_score, job.combined_score,
                      json.dumps(job.score_breakdown), summary, job.id))
                return False

            # 2. Content-hash match — cross-platform duplicate, skip
            existing_hash = conn.execute(
                "SELECT id FROM jobs WHERE content_hash = ?", (chash,)
            ).fetchone()
            if existing_hash:
                print(f"[tracker] Skipping duplicate: {job.title} @ {job.company} "
                      f"(already stored as {existing_hash['id']})")
                return False

            # 3. New job — insert
            conn.execute("""
                INSERT INTO jobs (id, content_hash, title, company, location,
                    description, description_summary, url, platform,
                    salary_min, salary_max, salary_text, remote, posted_date,
                    fit_score, salary_score, combined_score, score_breakdown)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                job.id, chash, job.title, job.company, job.location,
                job.description[:5000], summary,
                job.url,
                job.platform.value if hasattr(job.platform, 'value') else job.platform,
                job.salary_min, job.salary_max, job.salary_text,
                1 if job.remote else 0,
                posted,
                job.fit_score, job.salary_score, job.combined_score,
                json.dumps(job.score_breakdown),
            ))
            return True

    def get_jobs(self, min_score: float = 0, limit: int = 100) -> List[Dict]:
        """
        Fetch top jobs sorted by recency-boosted score.
        combined_score + recency_bonus(posted_date) — fresh posts surface first.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT j.*, a.status as app_status, a.id as app_id
                FROM jobs j
                LEFT JOIN applications a ON j.id = a.job_id
                WHERE j.combined_score >= ?
                ORDER BY j.combined_score DESC
                LIMIT ?
            """, (min_score, limit * 3)).fetchall()  # Fetch extra so we can re-sort

        jobs = [dict(r) for r in rows]
        # Apply recency bonus in Python (avoids SQLite datetime math complexity)
        for j in jobs:
            j['recency_bonus']  = self.recency_bonus(j.get('posted_date'))
            j['final_score']    = j['combined_score'] + j['recency_bonus']
        jobs.sort(key=lambda j: j['final_score'], reverse=True)
        return jobs[:limit]

    def already_applied(self, job_id: str) -> bool:
        """
        True if we've already submitted or queued this job — skip it.
        'failed' can be retried; 'needs_manual' blocks automation (human must act).
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id FROM applications
                   WHERE job_id = ?
                   AND status NOT IN ('failed')""",
                (job_id,)
            ).fetchone()
        return row is not None

    def get_application_by_job_id(self, job_id: str) -> Optional[Dict]:
        """Return the most recent application record for a job, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM applications WHERE job_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_needs_manual(self) -> List[Dict]:
        """Return all jobs that need manual application, with job details."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT j.id, j.title, j.company, j.location, j.url,
                       j.combined_score, j.fit_score,
                       a.id as app_id, a.error, a.notes, a.created_at
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                WHERE a.status = 'needs_manual'
                ORDER BY j.combined_score DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def mark_applied_manually(self, job_id: str):
        """User confirmed they applied to this job manually."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE applications SET status='applied', applied_at=?, updated_at=?,
                   notes = COALESCE(notes, '') || ' [Applied manually]'
                   WHERE job_id=?""",
                (datetime.now().isoformat(), datetime.now().isoformat(), job_id)
            )

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
