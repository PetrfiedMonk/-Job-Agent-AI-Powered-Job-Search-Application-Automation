"""
Self-Improving Loop — Improvement Tracker

Logs application outcomes (failures, partial wins, full successes) to SQLite.
After each run, writes a human-readable Obsidian vault note so the user (and
future Claude sessions) can see exactly what's broken, what's working, and
what to fix next.

Philosophy: reliability over speed. The goal is getting job seekers interviews.
Every failure is data. Every data point makes the next run smarter.
"""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional


CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_failures (
    id           TEXT PRIMARY KEY,
    ats          TEXT NOT NULL,
    url          TEXT,
    company      TEXT,
    job_title    TEXT,
    error_type   TEXT,
    error_msg    TEXT,
    step_number  INTEGER DEFAULT 0,
    context_json TEXT DEFAULT '{}',
    occurred_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fail_ats ON app_failures(ats);
CREATE INDEX IF NOT EXISTS idx_fail_type ON app_failures(error_type);

CREATE TABLE IF NOT EXISTS app_successes (
    id            TEXT PRIMARY KEY,
    ats           TEXT NOT NULL,
    url           TEXT,
    company       TEXT,
    job_title     TEXT,
    fields_filled INTEGER DEFAULT 0,
    auto_submitted INTEGER DEFAULT 0,
    occurred_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_succ_ats ON app_successes(ats);

CREATE TABLE IF NOT EXISTS improvement_items (
    id           TEXT PRIMARY KEY,
    ats          TEXT,
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    priority     TEXT DEFAULT 'MED',
    status       TEXT DEFAULT 'open',
    occurrences  INTEGER DEFAULT 1,
    first_seen   TEXT,
    last_seen    TEXT,
    fix_notes    TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imp_desc ON improvement_items(ats, description);

CREATE TABLE IF NOT EXISTS user_interventions (
    id              TEXT PRIMARY KEY,
    job_id          TEXT,
    ats             TEXT NOT NULL,
    url             TEXT DEFAULT '',
    company         TEXT DEFAULT '',
    job_title       TEXT DEFAULT '',
    error_type      TEXT DEFAULT '',
    error_msg       TEXT DEFAULT '',
    action_taken    TEXT NOT NULL,
    action_detail   TEXT DEFAULT '',
    resolved        INTEGER DEFAULT 0,
    xp_awarded      INTEGER DEFAULT 50,
    occurred_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_ats ON user_interventions(ats, error_type);
CREATE INDEX IF NOT EXISTS idx_ui_job ON user_interventions(job_id);
"""


def _classify_error(error: str) -> str:
    """Bucket a raw error string into a standard category."""
    e = error.lower()
    if "captcha" in e:
        return "captcha"
    if "login" in e or "authwall" in e or "sign in" in e:
        return "login_wall"
    if "timeout" in e or "time out" in e:
        return "timeout"
    if "no next" in e or "no submit" in e or "no apply" in e or "not found" in e or "could not find" in e:
        return "selector_miss"
    if "upload" in e or "file input" in e:
        return "upload_fail"
    if "step" in e and ("stuck" in e or "stopped" in e or "max" in e):
        return "multistep_stuck"
    if "navigation" in e or "goto" in e or "net::" in e:
        return "navigation_error"
    return "unknown"


class ImprovementTracker:
    """
    Logs outcomes and surfaces improvement opportunities.
    Written to the same SQLite DB as the application tracker.
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(CREATE_SCHEMA)

    # ── Logging ───────────────────────────────────────────────────────────────

    def log_failure(
        self,
        ats: str,
        url: str,
        company: str,
        job_title: str,
        error: str,
        step: int = 0,
        context: dict = None,
    ):
        """Record a failed application and upsert an improvement item."""
        now = datetime.now().isoformat()
        error_type = _classify_error(error)

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO app_failures
                (id, ats, url, company, job_title, error_type, error_msg,
                 step_number, context_json, occurred_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (str(uuid.uuid4()), ats, url, company, job_title,
                  error_type, error[:500], step,
                  json.dumps(context or {}), now))

            # Determine description for the improvement item
            desc = f"{error_type}: {error[:120]}"
            priority = self._error_priority(error_type)
            point_cost = {"HIGH": 10, "MED": 5, "LOW": 2}.get(priority, 2)

            conn.execute("""
                INSERT INTO improvement_items
                (id, ats, category, description, priority, status,
                 occurrences, first_seen, last_seen, point_cost)
                VALUES (?,?,?,?,?,'open',1,?,?,?)
                ON CONFLICT(ats, description) DO UPDATE SET
                    occurrences = occurrences + 1,
                    last_seen   = excluded.last_seen,
                    priority    = CASE WHEN occurrences + 1 >= 5 THEN 'HIGH'
                                       WHEN occurrences + 1 >= 3 THEN 'MED'
                                       ELSE priority END
            """, (str(uuid.uuid4()), ats, error_type, desc, priority, now, now, point_cost))
            conn.commit()

    def log_success(
        self,
        ats: str,
        url: str,
        company: str,
        job_title: str,
        fields_filled: int,
        auto_submitted: bool,
    ):
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO app_successes
                (id, ats, url, company, job_title, fields_filled,
                 auto_submitted, occurred_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (str(uuid.uuid4()), ats, url, company, job_title,
                  fields_filled, int(auto_submitted), now))
            conn.commit()

    def mark_fixed(self, ats: str, category: str, fix_notes: str = ""):
        """Mark an improvement item as resolved."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("""
                UPDATE improvement_items
                SET status='fixed', fix_notes=?, last_seen=?
                WHERE ats=? AND category=?
            """, (fix_notes, now, ats, category))
            conn.commit()

    # ── Analytics ─────────────────────────────────────────────────────────────

    def get_open_improvements(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT ats, category, description, priority, occurrences,
                       first_seen, last_seen, fix_notes
                FROM improvement_items
                WHERE status = 'open'
                ORDER BY
                    CASE priority WHEN 'HIGH' THEN 0 WHEN 'MED' THEN 1 ELSE 2 END,
                    occurrences DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_recent_successes(self, days: int = 7) -> List[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT ats, company, job_title, fields_filled,
                       auto_submitted, occurred_at
                FROM app_successes
                WHERE occurred_at >= ?
                ORDER BY occurred_at DESC
                LIMIT 20
            """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def get_weekly_stats(self) -> dict:
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        with self._connect() as conn:
            total_fail = conn.execute(
                "SELECT COUNT(*) FROM app_failures WHERE occurred_at >= ?", (cutoff,)
            ).fetchone()[0]
            total_succ = conn.execute(
                "SELECT COUNT(*) FROM app_successes WHERE occurred_at >= ?", (cutoff,)
            ).fetchone()[0]
            submitted = conn.execute(
                "SELECT COUNT(*) FROM app_successes WHERE occurred_at >= ? AND auto_submitted=1",
                (cutoff,)
            ).fetchone()[0]
            ats_breakdown = conn.execute("""
                SELECT ats,
                       COUNT(*) as total,
                       SUM(CASE WHEN auto_submitted=1 THEN 1 ELSE 0 END) as submitted
                FROM app_successes
                WHERE occurred_at >= ?
                GROUP BY ats ORDER BY total DESC
            """, (cutoff,)).fetchall()

        total = total_fail + total_succ
        return {
            "total":            total,
            "successes":        total_succ,
            "failures":         total_fail,
            "auto_submitted":   submitted,
            "success_rate":     round(total_succ / total * 100) if total else 0,
            "ats_breakdown":    [dict(r) for r in ats_breakdown],
        }

    # ── Vault Markdown Writer ─────────────────────────────────────────────────

    def write_vault_note(self, vault_path: str) -> Optional[Path]:
        """
        Write (or overwrite) the improvement log to the Obsidian vault.
        Returns the path written, or None if vault_path is empty.
        """
        if not vault_path:
            return None

        vault = Path(vault_path)
        if not vault.exists():
            return None

        improvements = self.get_open_improvements()
        successes    = self.get_recent_successes(days=7)
        stats        = self.get_weekly_stats()

        lines = [
            "# Job Agent — Improvement Log",
            "",
            f"> Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "> This file is auto-generated by the Job Agent after each run.",
            "> Edit the **Improvement Backlog** section to add manual tasks.",
            "",
        ]

        # ── Stats ──────────────────────────────────────────────────────────────
        lines += [
            "## Weekly Stats",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total attempted | {stats['total']} |",
            f"| Successful fills | {stats['successes']} ({stats['success_rate']}%) |",
            f"| Auto-submitted | {stats['auto_submitted']} |",
            f"| Failures | {stats['failures']} |",
            "",
        ]

        if stats["ats_breakdown"]:
            lines += ["**By ATS:**", ""]
            for row in stats["ats_breakdown"]:
                lines.append(f"- **{row['ats']}**: {row['total']} filled, {row['submitted']} submitted")
            lines.append("")

        # ── Open Issues ────────────────────────────────────────────────────────
        if improvements:
            lines += [
                "## Active Issues",
                "",
                "| Priority | ATS | Category | Description | Seen |",
                "|----------|-----|----------|-------------|------|",
            ]
            for item in improvements:
                ats  = item["ats"] or "generic"
                desc = item["description"][:80]
                lines.append(
                    f"| {item['priority']} | {ats} | {item['category']} "
                    f"| {desc} | {item['occurrences']}x |"
                )
            lines.append("")
        else:
            lines += ["## Active Issues", "", "✓ No open issues.", ""]

        # ── Recent Wins ────────────────────────────────────────────────────────
        if successes:
            lines += ["## Recent Wins (7 days)", ""]
            for s in successes[:10]:
                submitted_tag = " ✓ submitted" if s["auto_submitted"] else " (pre-filled)"
                date = s["occurred_at"][:10]
                lines.append(
                    f"- {date}: **{s['company']}** via {s['ats']} "
                    f"— {s['fields_filled']} fields{submitted_tag}"
                )
            lines.append("")
        else:
            lines += ["## Recent Wins (7 days)", "", "No completions yet this week.", ""]

        # ── Improvement Backlog (manual section — preserved if already exists) ──
        lines += [
            "## Improvement Backlog",
            "",
            "> Add items here manually. The agent will attempt known fixes on the next run.",
            "",
        ]

        # Merge with any existing manual items already in the file
        existing_backlog = self._read_existing_backlog(vault)
        if existing_backlog:
            lines += existing_backlog
        else:
            lines += [
                "- [ ] Review any HIGH priority issues above",
                "- [ ] Test new ATS platforms as job listings come in",
                "- [ ] Update EEO decline phrases if new options appear",
            ]

        lines.append("")
        lines.append("---")
        lines.append("*Generated by Job Agent. Do not edit above the Improvement Backlog section.*")

        output = vault / "Job Agent — Improvements.md"
        output.write_text("\n".join(lines), encoding="utf-8")
        print(f"[tracker] Vault improvement log updated: {output}")
        return output

    def _read_existing_backlog(self, vault: Path) -> Optional[List[str]]:
        """Preserve manually-added backlog items between writes."""
        note = vault / "Job Agent — Improvements.md"
        if not note.exists():
            return None
        try:
            text = note.read_text(encoding="utf-8")
            idx = text.find("## Improvement Backlog")
            if idx == -1:
                return None
            section = text[idx:].split("\n", 3)
            # Skip header + blank + description line, return rest
            if len(section) >= 4:
                rest = section[3:]
                # Strip trailing generated lines
                clean = []
                for line in rest:
                    if line.startswith("---") or "Generated by Job Agent" in line:
                        break
                    clean.append(line)
                return clean if any(c.strip() for c in clean) else None
        except Exception:
            return None

    # ── Human-in-the-Loop Assist ──────────────────────────────────────────────

    def log_user_assist(
        self,
        job_id: str,
        ats: str,
        url: str,
        company: str,
        job_title: str,
        error_type: str,
        error_msg: str,
        action_taken: str,
        action_detail: str = "",
    ) -> dict:
        """Record a user intervention and return xp_awarded + total_assists."""
        now = datetime.now().isoformat()
        rid = str(uuid.uuid4())
        # First time solving this ATS+error combo earns a bonus
        existing = self.get_assist_hints(ats, error_type, limit=1)
        xp = 75 if not existing else 50
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO user_interventions
                (id, job_id, ats, url, company, job_title, error_type, error_msg,
                 action_taken, action_detail, resolved, xp_awarded, occurred_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
            """, (rid, str(job_id), ats, url, company, job_title,
                  error_type, error_msg[:300], action_taken, action_detail, xp, now))
            conn.commit()
        return {"xp_awarded": xp, "total_assists": self.get_total_assists()}

    def get_assist_hints(self, ats: str, error_type: str = "", limit: int = 3) -> list:
        """Return most-used intervention actions for this ATS + error combo."""
        with self._connect() as conn:
            if error_type:
                rows = conn.execute("""
                    SELECT action_taken, action_detail, COUNT(*) as times
                    FROM user_interventions
                    WHERE ats=? AND error_type=?
                    GROUP BY action_taken ORDER BY times DESC LIMIT ?
                """, (ats, error_type, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT action_taken, action_detail, COUNT(*) as times
                    FROM user_interventions
                    WHERE ats=?
                    GROUP BY action_taken ORDER BY times DESC LIMIT ?
                """, (ats, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_total_assists(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM user_interventions"
            ).fetchone()[0]

    def get_improvement_roi(self, limit: int = 8) -> list:
        """
        Return open improvement items ranked by point_cost (highest ROI first).
        These are the bugs/issues whose fix would raise the run score the most.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT ats, category, description, priority, occurrences,
                       point_cost, first_seen, last_seen
                FROM improvement_items
                WHERE status = 'open'
                ORDER BY point_cost DESC, occurrences DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_intervention_resolved(self, job_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE user_interventions SET resolved=1 WHERE job_id=? AND resolved=0",
                (str(job_id),),
            )
            conn.commit()

    @staticmethod
    def _error_priority(error_type: str) -> str:
        high = {"login_wall", "captcha", "navigation_error"}
        med  = {"selector_miss", "multistep_stuck", "upload_fail"}
        return "HIGH" if error_type in high else "MED" if error_type in med else "LOW"
