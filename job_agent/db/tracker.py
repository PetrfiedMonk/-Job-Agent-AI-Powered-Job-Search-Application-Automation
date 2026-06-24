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
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _content_hash(title: str, company: str, location: str) -> str:
        """
        Platform-agnostic identity for a job posting.
        Same role on Indeed + LinkedIn + company site → same hash → skip duplicate.
        Location excluded: same role in "New York" vs "New York, NY" shouldn't
        create two entries. Word-boundary split avoids collapsing distinct words.
        """
        def norm(s: str) -> str:
            s = (s or '').lower()
            s = re.sub(r'[^a-z0-9\s]', ' ', s)   # punctuation → space, not ''
            s = re.sub(r'\s+', ' ', s).strip()
            return s
        key = f"{norm(title)}|{norm(company)}"
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

                CREATE TABLE IF NOT EXISTS field_memory (
                    id         TEXT PRIMARY KEY,
                    label      TEXT NOT NULL,
                    answer     TEXT NOT NULL,
                    context    TEXT DEFAULT '',
                    use_count  INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_label ON field_memory(label, context);

                CREATE TABLE IF NOT EXISTS run_scores (
                    id             TEXT PRIMARY KEY,
                    started_at     TEXT NOT NULL,
                    ended_at       TEXT,
                    attempted      INTEGER DEFAULT 0,
                    applied        INTEGER DEFAULT 0,
                    manual         INTEGER DEFAULT 0,
                    failed         INTEGER DEFAULT 0,
                    score          INTEGER DEFAULT 0,
                    xp_earned      INTEGER DEFAULT 0,
                    label          TEXT DEFAULT '',
                    grade          TEXT DEFAULT 'F',
                    delta          INTEGER DEFAULT 0,
                    automation_pts INTEGER DEFAULT 0,
                    reach_pts      INTEGER DEFAULT 0,
                    quality_pts    INTEGER DEFAULT 0,
                    velocity_pts   INTEGER DEFAULT 0,
                    breakdown_json TEXT DEFAULT '{}'
                );
            """)
            # Additive migration — add new columns to existing databases.
            # Indexes on new columns must come AFTER the columns are added.
            for col, typedef in [
                ("content_hash",        "TEXT"),
                ("posted_date",         "TEXT"),
                ("description_summary", "TEXT"),
                ("queued",              "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # Column already exists — safe to ignore

            for col, typedef in [
                ("tailored_summary", "TEXT"),
                ("highlighted_skills", "TEXT"),
                ("vault_note_path",  "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {typedef}")
                except Exception:
                    pass

            # run_scores — dimensional score columns (additive migration)
            for col, typedef in [
                ("grade",          "TEXT DEFAULT 'F'"),
                ("delta",          "INTEGER DEFAULT 0"),
                ("automation_pts", "INTEGER DEFAULT 0"),
                ("reach_pts",      "INTEGER DEFAULT 0"),
                ("quality_pts",    "INTEGER DEFAULT 0"),
                ("velocity_pts",   "INTEGER DEFAULT 0"),
                ("breakdown_json", "TEXT DEFAULT '{}'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE run_scores ADD COLUMN {col} {typedef}")
                except Exception:
                    pass

            # improvement_items — point_cost so we can rank by ROI
            try:
                conn.execute("ALTER TABLE improvement_items ADD COLUMN point_cost INTEGER DEFAULT 0")
            except Exception:
                pass

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
        Each job appears exactly once (most recent application status used).
        Recency bonus is computed in SQL so no Python re-sort is needed.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT j.*, a.status as app_status, a.id as app_id,
                       a.tailored_summary, a.highlighted_skills,
                       a.resume_path, a.vault_note_path,
                       CASE
                           WHEN (julianday('now') - julianday(j.posted_date)) * 24 <= 12  THEN 15
                           WHEN (julianday('now') - julianday(j.posted_date)) * 24 <= 24  THEN 12
                           WHEN (julianday('now') - julianday(j.posted_date)) * 24 <= 48  THEN 9
                           WHEN (julianday('now') - julianday(j.posted_date)) * 24 <= 72  THEN 5
                           WHEN (julianday('now') - julianday(j.posted_date)) * 24 <= 120 THEN 2
                           ELSE 0
                       END AS recency_bonus
                FROM jobs j
                LEFT JOIN (
                    SELECT job_id, status, id,
                           tailored_summary, highlighted_skills,
                           resume_path, vault_note_path
                    FROM applications
                    WHERE created_at = (
                        SELECT MAX(a2.created_at) FROM applications a2
                        WHERE a2.job_id = applications.job_id
                    )
                    GROUP BY job_id
                ) a ON j.id = a.job_id
                WHERE j.combined_score >= ?
                ORDER BY (j.combined_score + recency_bonus) DESC
                LIMIT ?
            """, (min_score, limit)).fetchall()

        jobs = [dict(r) for r in rows]
        for j in jobs:
            j['final_score'] = j['combined_score'] + j.get('recency_bonus', 0)
        return jobs

    def already_applied(self, job_id: str) -> bool:
        """
        True if we've already submitted or queued this job — skip it.
        'failed' can be retried; all other terminal statuses block re-attempts.
        Also checks cross-platform via content_hash so the same role found on
        LinkedIn and Indeed doesn't get applied to twice.
        """
        with self._connect() as conn:
            # 1. Direct ID match
            row = conn.execute(
                """SELECT id FROM applications
                   WHERE job_id = ?
                   AND status NOT IN ('failed')""",
                (job_id,)
            ).fetchone()
            if row:
                return True
            # 2. Cross-platform: same content_hash applied under a different job_id
            chash_row = conn.execute(
                "SELECT content_hash FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if chash_row and chash_row["content_hash"]:
                dup = conn.execute(
                    """SELECT a.id FROM applications a
                       JOIN jobs j ON a.job_id = j.id
                       WHERE j.content_hash = ?
                       AND a.status NOT IN ('failed')""",
                    (chash_row["content_hash"],)
                ).fetchone()
                if dup:
                    return True
        return False

    def failure_count(self, job_id: str) -> int:
        """Return how many times this job has been attempted and failed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE job_id=? AND status='failed'",
                (job_id,)
            ).fetchone()
        return row[0] if row else 0

    def needs_manual_count(self, job_id: str) -> int:
        """Return how many times this job ended as needs_manual (systematic blocker)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE job_id=? AND status='needs_manual'",
                (job_id,)
            ).fetchone()
        return row[0] if row else 0

    # ── Run Scores ────────────────────────────────────────────────────────────

    def save_run_score(
        self,
        started_at: str,
        ended_at: str,
        applied: int,
        manual: int,
        failed: int,
        label: str = "",
        job_scores: list = None,
        error_breakdown: dict = None,
    ) -> dict:
        """
        Compute and persist a multi-dimensional run performance score.

        Dimensions (total 0-100):
          automation_pts (0-40): applied / attempted — core success rate
          reach_pts      (0-25): (applied + manual) / attempted — system reach
          quality_pts    (0-20): avg job match score / 100 — targeting quality
          velocity_pts   (0-15): applied/hr capped at 6/hr target

        Grade: S≥90  A≥75  B≥60  C≥45  D≥30  F<30

        XP formula is preserved so user-facing gamification is unchanged.
        Also writes point_cost back to improvement_items so we can rank bugs by ROI.
        """
        attempted = applied + manual + failed

        # ── Dimensional scoring ───────────────────────────────────────────────
        if attempted == 0:
            automation_pts = reach_pts = quality_pts = velocity_pts = 0
        else:
            automation_pts = round(applied / attempted * 40)
            reach_pts      = round((applied + manual) / attempted * 25)

            if job_scores:
                avg_q      = sum(job_scores) / len(job_scores)
                quality_pts = round(avg_q / 100 * 20)
            else:
                quality_pts = 10  # neutral when no job score data

            try:
                t0 = datetime.fromisoformat(started_at)
                t1 = datetime.fromisoformat(ended_at)
                elapsed_hr = max(0.017, (t1 - t0).total_seconds() / 3600)
            except Exception:
                elapsed_hr = 1.0
            apps_per_hr  = applied / elapsed_hr
            velocity_pts = min(15, round(apps_per_hr / 6 * 15))

        score = automation_pts + reach_pts + quality_pts + velocity_pts

        # ── Grade ─────────────────────────────────────────────────────────────
        grade = 'F'
        for threshold, g in [(90,'S'), (75,'A'), (60,'B'), (45,'C'), (30,'D')]:
            if score >= threshold:
                grade = g
                break

        # ── XP (unchanged formula — preserves user-facing progression) ────────
        xp_earned = max(1, score) * max(1, attempted) // 5

        # ── Delta from previous run ───────────────────────────────────────────
        with self._connect() as conn:
            prev = conn.execute(
                "SELECT score FROM run_scores ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        delta = score - (prev["score"] if prev else score)

        # ── Failure cost analysis — how many automation_pts each error type cost ──
        failure_costs: dict = {}
        if error_breakdown and attempted > 0:
            for etype, count in error_breakdown.items():
                cost = round(count / attempted * 40)
                if cost > 0:
                    failure_costs[etype] = cost

        breakdown = {
            "automation_pts": automation_pts,
            "reach_pts":      reach_pts,
            "quality_pts":    quality_pts,
            "velocity_pts":   velocity_pts,
            "failure_costs":  failure_costs,
        }

        rid = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO run_scores
                (id, started_at, ended_at, attempted, applied, manual, failed,
                 score, xp_earned, label, grade, delta,
                 automation_pts, reach_pts, quality_pts, velocity_pts, breakdown_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (rid, started_at, ended_at, attempted, applied, manual, failed,
                  score, xp_earned, label, grade, delta,
                  automation_pts, reach_pts, quality_pts, velocity_pts,
                  json.dumps(breakdown)))

            # Write point_cost back to improvement_items so we rank bugs by ROI
            for etype, cost in failure_costs.items():
                try:
                    conn.execute("""
                        UPDATE improvement_items
                        SET point_cost = ?
                        WHERE category = ? AND status = 'open'
                    """, (cost, etype))
                except Exception:
                    pass

        return {
            "id": rid, "score": score, "grade": grade, "delta": delta,
            "xp_earned": xp_earned, "attempted": attempted,
            "applied": applied, "manual": manual, "failed": failed,
            "breakdown": breakdown,
        }

    def get_run_trends(self, limit: int = 10) -> List[Dict]:
        """Return last N runs with dimensional breakdown for trend display."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, started_at, ended_at, attempted, applied, manual, failed,
                       score, xp_earned, grade, delta,
                       automation_pts, reach_pts, quality_pts, velocity_pts,
                       breakdown_json, label
                FROM run_scores
                ORDER BY started_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["breakdown"] = json.loads(d.get("breakdown_json") or "{}")
            except Exception:
                d["breakdown"] = {}
            results.append(d)
        return list(reversed(results))  # chronological order for sparkline

    def get_score_summary(self) -> dict:
        """
        Return cumulative XP, level, streak, best score, and last 10 run history.
        Level = floor(total_xp / 500). Streak = consecutive runs with score >= 50.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT score, xp_earned, attempted, applied, manual, failed, started_at "
                "FROM run_scores ORDER BY started_at DESC LIMIT 30"
            ).fetchall()

        runs = [dict(r) for r in rows]
        if not runs:
            with self._connect() as conn0:
                ta = conn0.execute(
                    "SELECT COUNT(*) FROM applications WHERE status IN ('applied','interview','offer')"
                ).fetchone()[0]
            return {
                "total_xp": 0, "level": 1, "streak": 0,
                "best_score": 0, "last_score": 0,
                "total_applied": ta, "total_attempted": ta,
                "success_rate": 100 if ta else 0, "history": [],
                "xp_to_next": 100, "xp_pct": 0,
            }

        total_xp = sum(r["xp_earned"] for r in runs)
        level = max(1, total_xp // 500 + 1)
        best_score = max(r["score"] for r in runs)
        last_score = runs[0]["score"] if runs else 0

        # Streak: consecutive most-recent runs with score >= 50
        streak = 0
        for r in runs:
            if r["score"] >= 50:
                streak += 1
            else:
                break

        # Use real application table for total_applied — run_scores misses
        # single-job modal applies and manually-marked applications
        with self._connect() as conn2:
            total_applied = conn2.execute(
                "SELECT COUNT(*) FROM applications WHERE status IN ('applied','interview','offer')"
            ).fetchone()[0]
            total_attempted = conn2.execute(
                "SELECT COUNT(*) FROM applications WHERE status != 'queued'"
            ).fetchone()[0]
        success_rate = round(total_applied / total_attempted * 100) if total_attempted else 0

        return {
            "total_xp":       total_xp,
            "level":          level,
            "xp_to_next":     500 - (total_xp % 500),
            "xp_pct":         round((total_xp % 500) / 500 * 100),
            "streak":         streak,
            "best_score":     best_score,
            "last_score":     last_score,
            "total_applied":  total_applied,
            "total_attempted": total_attempted,
            "success_rate":   success_rate,
            "history":        list(reversed(runs[:10])),
        }

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
                       a.id as app_id, a.error, a.notes, a.created_at,
                       a.resume_path, a.tailored_summary, a.highlighted_skills
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                WHERE a.status = 'needs_manual'
                ORDER BY j.combined_score DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def mark_applied_manually(self, job_id: str):
        """User confirmed they applied to this job manually."""
        with self._connect() as conn:
            now = datetime.now().isoformat()
            existing = conn.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE applications SET status='applied', applied_at=?, updated_at=?,
                       notes = COALESCE(notes, '') || ' [Applied manually]'
                       WHERE job_id=?""",
                    (now, now, job_id)
                )
            else:
                conn.execute(
                    """INSERT INTO applications (id, job_id, status, created_at, updated_at, notes)
                       VALUES (?, ?, 'applied', ?, ?, '[Applied manually]')""",
                    (str(uuid.uuid4()), job_id, now, now)
                )

    def dismiss_job(self, job_id: str):
        """Remove a job from the list — user chose not to apply."""
        with self._connect() as conn:
            now = datetime.now().isoformat()
            existing = conn.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE applications SET status='dismissed', updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
            else:
                conn.execute(
                    """INSERT INTO applications (id, job_id, status, created_at, updated_at)
                       VALUES (?, ?, 'dismissed', ?, ?)""",
                    (str(uuid.uuid4()), job_id, now, now),
                )

    # ── Queue Attack ──────────────────────────────────────────────────────────

    def set_queued(self, job_id: str, queued: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET queued=? WHERE id=?",
                (1 if queued else 0, job_id)
            )

    def get_queued_jobs(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT j.*, a.status as app_status
                   FROM jobs j
                   LEFT JOIN (
                       SELECT job_id, status FROM applications
                       WHERE created_at = (SELECT MAX(a2.created_at) FROM applications a2 WHERE a2.job_id = applications.job_id)
                       GROUP BY job_id
                   ) a ON j.id = a.job_id
                   WHERE j.queued = 1
                   ORDER BY j.combined_score DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_queue(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("UPDATE jobs SET queued=0 WHERE queued=1")
            return cur.rowcount

    # ── Applications ──────────────────────────────────────────────────────────

    def create_application(self, application: Application) -> str:
        """Save a new application record."""
        app_id = application.id or str(uuid.uuid4())
        skills_json = json.dumps(getattr(application.resume, "highlighted_skills", []) or [])
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO applications
                (id, job_id, status, resume_path, keywords_matched, ats_score,
                 tailored_summary, highlighted_skills, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                app_id,
                application.job.id,
                application.status.value,
                getattr(application.resume, "docx_path", None) or "",
                json.dumps(getattr(application.resume, "keywords_matched", []) or []),
                getattr(application.resume, "ats_score_estimate", 0) or 0,
                getattr(application.resume, "tailored_summary", None) or "",
                skills_json,
                application.notes,
            ))
        return app_id

    def update_application(self, app_id: str, **kwargs):
        """Update specific fields on an application."""
        allowed = {"status", "applied_at", "interview_at", "notes", "error", "form_data",
                   "vault_note_path", "tailored_summary", "highlighted_skills", "resume_path"}
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

    def save_resume_path(
        self,
        job_id: str,
        resume_path: str,
        tailored_summary: str = "",
        highlighted_skills: list = None,
    ):
        """Persist a generated resume to the most recent application for a job.
        Creates a placeholder 'found' application record if none exists yet."""
        skills_json = json.dumps(highlighted_skills or [])
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM applications WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE applications
                       SET resume_path=?, tailored_summary=?, highlighted_skills=?, updated_at=?
                       WHERE id=?""",
                    (resume_path, tailored_summary, skills_json,
                     datetime.now().isoformat(), existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO applications
                       (id, job_id, status, resume_path, tailored_summary, highlighted_skills, notes)
                       VALUES (?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), job_id, "found", resume_path,
                     tailored_summary, skills_json, "Resume generated on demand"),
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

    # ── Field Memory (companion teaching) ────────────────────────────────────

    def save_field_memory(self, label: str, answer: str, context: str = "") -> str:
        """Save or update a learned field answer. Returns the memory id."""
        mem_id = str(uuid.uuid4())
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM field_memory WHERE label=? AND context=?", (label, context)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE field_memory SET answer=?, updated_at=datetime('now') WHERE id=?",
                    (answer, existing["id"])
                )
                return existing["id"]
            conn.execute(
                "INSERT INTO field_memory (id, label, answer, context) VALUES (?,?,?,?)",
                (mem_id, label, answer, context)
            )
        return mem_id

    def get_field_memory(self, label: str, context: str = "") -> Optional[str]:
        """Look up a stored answer for a field label. Falls back to context-free match."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, answer FROM field_memory WHERE label=? AND context=? LIMIT 1",
                (label, context)
            ).fetchone()
            if not row and context:
                row = conn.execute(
                    "SELECT id, answer FROM field_memory WHERE label=? AND context='' LIMIT 1",
                    (label,)
                ).fetchone()
            if row:
                conn.execute(
                    "UPDATE field_memory SET use_count=use_count+1 WHERE id=?",
                    (row["id"],)
                )
                return row["answer"]
        return None

    def get_all_memories(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM field_memory ORDER BY use_count DESC, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM field_memory WHERE id=?", (memory_id,))

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
